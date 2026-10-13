"""The forward-shadow runtime, exercised as shipped.

Everything here drives ``carry_shadow`` itself (the module, its CLI in a real
subprocess, and the real ``CarryEngine`` it feeds). Observations are FIXTURES
and are labelled as such by the evidence class they are booked under; nothing
here is a market observation and no figure is real P&L.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import carry_shadow as cs  # noqa: E402

H8 = 8 * 3600 * 1000
MARK, SPOT = 80_000.0, 79_990.0


def obs(ms, bps=3.0, *, perp=MARK, spot=SPOT, **extra):
    """One observation at settlement time `ms` (epoch ms)."""
    base = {"perp_mark": perp, "spot_mark": spot, "funding_bps": bps,
            "funding_print_bps": bps, "funding_print_ms": ms,
            "observed_at_s": ms / 1000.0, "source": "test fixture"}
    base.update(extra)
    return base


def series(n_hold=4, start=1_700_000_000_000):
    """Three warm-up prints, an entry, `n_hold` held prints, then an inverted
    funding streak that makes the real engine unwind."""
    out, ms = [], start
    for bps in [3.0] * 3 + [3.0] * n_hold + [-4.0] * 8:
        out.append(obs(ms, bps, perp=MARK + len(out) * 10,
                       spot=SPOT + len(out) * 10))
        ms += H8
    return out


def config(origin="synthetic_fixture", **kw):
    args = dict(origin=origin, venue="historical", borrow_apr=0.0,
                execution_mode="overlay", inventory_btc=0.01,
                margin_multiple=5.0)
    args.update(kw)
    return cs.build_config(**args)


def runtime(tmp_path, name="shadow.db", **kw):
    return cs.ShadowRuntime(str(tmp_path / name), config(**kw))


def run_all(rt, observations):
    return [rt.step(o) for o in observations]


def econ_of(path):
    rt = cs.ShadowRuntime(path, config())
    try:
        return rt.economics()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# the engine really runs, and the book really trades
# ---------------------------------------------------------------------------


class TestTheRealEngineRunsAgainstTheSimulatedVenue:
    def test_a_full_lifecycle_opens_holds_and_unwinds(self, tmp_path):
        rt = runtime(tmp_path)
        results = run_all(rt, series())
        actions = [r["record"]["decision"]["action"] for r in results]
        assert any("opened" in a for a in actions), actions
        assert any("unwound" in a for a in actions), actions
        assert rt.engine.position is None
        from carry_engine import CarryEngine
        assert isinstance(rt.engine, CarryEngine)
        assert type(rt.engine).on_candle is CarryEngine.on_candle

    def test_the_decision_comes_from_the_engine_not_from_this_module(self):
        src = open(os.path.join(BOT, "carry_shadow.py")).read()
        for forbidden in ("MIN_ENTRY_FUNDING_BPS <", "evaluate_entry(",
                          "negative_funding_streak +=", "def _unwind",
                          "def _open"):
            assert forbidden not in src, forbidden

    def test_fills_carry_provenance_and_labelled_costs(self, tmp_path):
        rt = runtime(tmp_path)
        results = run_all(rt, series())
        fills = [f for r in results for f in r["record"]["intents_and_fills"]]
        assert fills
        for f in fills:
            assert f["fee_class"] == cs.ASSUMED and f["fee_usd"] > 0
            assert f["slippage_class"] == cs.UNAVAILABLE   # no touch observed
            assert "no order was sent anywhere" in f["fill_basis"]
            assert f["link"].startswith("shadow-")

    def test_slippage_is_estimated_when_the_touch_is_observed(self, tmp_path):
        rt = runtime(tmp_path)
        observations = [dict(o, perp_bid=o["perp_mark"] - 5,
                             perp_ask=o["perp_mark"] + 5) for o in series()]
        results = run_all(rt, observations)
        fills = [f for r in results for f in r["record"]["intents_and_fills"]]
        assert fills and all(f["slippage_class"] == cs.ESTIMATED for f in fills)
        assert all(f["slippage_usd"] == pytest.approx(f["qty"] * 5.0)
                   for f in fills)

    def test_every_step_records_source_revision_config_decision_assumptions(
            self, tmp_path):
        rt = runtime(tmp_path)
        rec = rt.step(series()[0])["record"]
        assert rec["source_ts"]["funding_print_ms"] == series()[0]["funding_print_ms"]
        assert rec["code"]["revision"]
        assert rec["config_fingerprint"] == rt.fingerprint
        assert rec["decision"]["reason"]
        assert rec["evidence_class"] == "synthetic_fixture"
        assert rec["assumptions"]["market_impact"] == cs.UNAVAILABLE
        assert rec["assumptions"]["fees"] == cs.ASSUMED


# ---------------------------------------------------------------------------
# economics
# ---------------------------------------------------------------------------


class TestModelledEconomics:
    def test_components_are_labelled_and_the_net_adds_up(self, tmp_path):
        rt = runtime(tmp_path)
        run_all(rt, series(n_hold=12))
        e = rt.economics()
        c = e["components_usd"]
        assert e["label"].startswith("MODELLED")
        assert c["funding"]["class"] == cs.ESTIMATED and c["funding"]["usd"] > 0
        assert c["fees"]["class"] == cs.ASSUMED and c["fees"]["usd"] < 0
        assert c["market_impact"] == {"usd": None, "class": cs.UNAVAILABLE}
        assert c["slippage"]["class"] == cs.UNAVAILABLE
        assert e["net_is_partial"] is True
        assert "market_impact" in e["unavailable_components_excluded"]
        net = (c["funding"]["usd"] + c["perp_realised"]["usd"]
               + c["hedged_btc_realised"]["usd"] + c["fees"]["usd"]
               + c["financing"]["usd"] + (c["slippage"]["usd"] or 0.0))
        assert e["net_realised_usd"] == pytest.approx(net)
        assert e["open_perp_qty"] == 0

    def test_funding_is_the_engines_own_accrual_booked_once_per_print(
            self, tmp_path):
        rt = runtime(tmp_path)
        run_all(rt, series(n_hold=4))
        rows = rt.store.con.execute(
            "SELECT print_ms, funding_usd, print_bps FROM shadow_funding"
        ).fetchall()
        assert rows and len({r[0] for r in rows}) == len(rows)
        qty = 100.0 / MARK
        for _ms, usd, bps in rows[:1]:
            assert usd == pytest.approx(bps / 1e4 * 0.001 * MARK, rel=0.05)
        assert qty > 0

    def test_financing_follows_the_configured_borrow_rate(self, tmp_path):
        a = runtime(tmp_path, "a.db", borrow_apr=0.0)
        b = runtime(tmp_path, "b.db", borrow_apr=0.10)
        run_all(a, series())
        run_all(b, series())
        assert a.economics()["components_usd"]["financing"]["usd"] == 0
        assert b.economics()["components_usd"]["financing"]["usd"] < 0
        assert b.economics()["components_usd"]["financing"]["class"] == cs.ASSUMED

    def test_identical_inputs_give_identical_economics(self, tmp_path):
        a, b = runtime(tmp_path, "a.db"), runtime(tmp_path, "b.db")
        run_all(a, series())
        run_all(b, series())
        assert a.economics() == b.economics()


# ---------------------------------------------------------------------------
# idempotency and restart
# ---------------------------------------------------------------------------


class TestIdempotencyAndRestart:
    def test_a_duplicate_observation_changes_nothing(self, tmp_path):
        rt = runtime(tmp_path)
        observations = series()
        run_all(rt, observations[:5])
        before = rt.economics()
        steps = rt.store.next_seq()
        again = rt.step(observations[3])
        assert again["outcome"] == "DUPLICATE_OBSERVATION" and again["exit"] == 0
        assert rt.store.next_seq() == steps
        assert rt.economics() == before

    def test_a_duplicate_funding_print_is_booked_once(self, tmp_path):
        rt = runtime(tmp_path)
        observations = series()
        run_all(rt, observations[:5])
        n = rt.store.con.execute("SELECT COUNT(*) FROM shadow_funding").fetchone()[0]
        # a DIFFERENT market observation carrying the SAME settled print
        replay = dict(observations[4], perp_mark=MARK + 50, observed_at_s=
                      observations[4]["observed_at_s"] + 1)
        rt.step(replay)
        assert rt.store.con.execute(
            "SELECT COUNT(*) FROM shadow_funding").fetchone()[0] == n

    def test_a_restart_resumes_identical_economics(self, tmp_path):
        observations = series()
        whole = runtime(tmp_path, "whole.db")
        run_all(whole, observations)

        path = str(tmp_path / "split.db")
        first = cs.ShadowRuntime(path, config())
        run_all(first, observations[:5])           # mid-hold
        assert first.engine.position is not None
        first.close()
        second = cs.ShadowRuntime(path, config())   # a new process, same book
        assert second.engine.position is not None
        run_all(second, observations[5:])
        assert second.economics() == whole.economics()
        a = [json.loads(r[0]) for r in whole.store.con.execute(
            "SELECT record FROM shadow_steps ORDER BY seq")]
        b = [json.loads(r[0]) for r in second.store.con.execute(
            "SELECT record FROM shadow_steps ORDER BY seq")]
        key = lambda r: (r["decision"], r["intents_and_fills"], r["funding"])
        assert [key(x) for x in a] == [key(x) for x in b]

    def test_a_restart_does_not_reopen_or_forget_the_daily_entry(self, tmp_path):
        observations = series()
        path = str(tmp_path / "x.db")
        rt = cs.ShadowRuntime(path, config())
        run_all(rt, observations[:4])
        orders = rt.store.con.execute("SELECT COUNT(*) FROM shadow_fills").fetchone()[0]
        rt.close()
        rt2 = cs.ShadowRuntime(path, config())
        assert rt2.engine.pair_risk._entries == rt.engine.pair_risk._entries
        assert rt2.store.con.execute(
            "SELECT COUNT(*) FROM shadow_fills").fetchone()[0] == orders

    def test_a_failed_step_is_rolled_back_whole(self, tmp_path, monkeypatch):
        rt = runtime(tmp_path)
        observations = series()
        run_all(rt, observations[:3])
        def boom(*a, **k):
            raise RuntimeError("disk fell over")
        monkeypatch.setattr(rt.engine, "on_candle", boom)
        with pytest.raises(RuntimeError):
            rt.step(observations[3])
        assert rt.store.con.execute(
            "SELECT COUNT(*) FROM shadow_observations").fetchone()[0] == 3
        assert not rt.store.con.in_transaction


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def _wall(self, tmp_path, **kw):
        return cs.ShadowRuntime(str(tmp_path / "w.db"),
                                config(origin="supplied", **kw))

    def test_a_stale_observation_is_refused_with_the_input_exit_code(
            self, tmp_path):
        rt = self._wall(tmp_path)
        old = obs(int((time.time() - 3600) * 1000))
        with pytest.raises(cs.ShadowRefusal) as e:
            rt.step(old)
        assert e.value.code == cs.EXIT_REFUSED_INPUT
        assert rt.store.con.execute(
            "SELECT COUNT(*) FROM shadow_observations").fetchone()[0] == 0
        assert rt.store.con.execute(
            "SELECT COUNT(*) FROM shadow_events WHERE kind='observation_refused'"
        ).fetchone()[0] == 1

    @pytest.mark.parametrize("missing", list(cs.REQUIRED_OBS))
    def test_an_incomplete_observation_is_refused(self, tmp_path, missing):
        rt = runtime(tmp_path)
        bad = series()[0]
        bad.pop(missing)
        with pytest.raises(cs.ShadowRefusal) as e:
            rt.step(bad)
        assert e.value.code == cs.EXIT_REFUSED_INPUT

    @pytest.mark.parametrize("patch", [
        {"perp_mark": -1.0}, {"spot_mark": float("nan")},
        {"funding_print_ms": 0}, {"perp_bid": 101.0, "perp_ask": 100.0}])
    def test_an_inconsistent_observation_is_refused(self, tmp_path, patch):
        rt = runtime(tmp_path)
        with pytest.raises(cs.ShadowRefusal):
            rt.step(dict(series()[0], **patch))

    def test_a_print_that_is_a_whole_interval_old_is_refused(self, tmp_path):
        rt = self._wall(tmp_path)
        now = time.time()
        old_print = obs(int((now - 10 * 3600) * 1000), observed_at_s=now)
        with pytest.raises(cs.ShadowRefusal) as e:
            rt.step(old_print)
        assert e.value.code == cs.EXIT_REFUSED_INPUT

    def test_a_wall_clock_book_cannot_be_given_a_private_clock(self, tmp_path):
        rt = self._wall(tmp_path)
        with pytest.raises(cs.ShadowRefusal):
            rt.step(obs(1_700_000_000_000), now_s=1_700_000_000.0)

    def test_a_changed_configuration_is_refused(self, tmp_path):
        runtime(tmp_path).close()
        with pytest.raises(cs.ShadowRefusal) as e:
            runtime(tmp_path, borrow_apr=0.05)
        assert e.value.code == cs.EXIT_CONFIG

    def test_a_book_never_mixes_evidence_classes(self, tmp_path):
        runtime(tmp_path, origin="historical_replay").close()
        with pytest.raises(cs.ShadowRefusal) as e:
            runtime(tmp_path, origin="synthetic_fixture")
        assert e.value.code == cs.EXIT_CONFIG

    def test_a_halted_book_refuses_every_later_observation(self, tmp_path):
        rt = runtime(tmp_path)
        run_all(rt, series()[:5])
        rt.engine._halt("TEST_HALT", "forced")      # the engine's own halt
        assert rt.kill.is_kill_switch_engaged()[0] is True
        out = rt.step(series()[6])
        assert out["exit"] == cs.EXIT_HALTED
        rt.close()
        rt2 = cs.ShadowRuntime(str(tmp_path / "shadow.db"), config())
        assert rt2.step(series()[7])["exit"] == cs.EXIT_HALTED

    def test_overlay_without_inventory_is_a_usage_error(self):
        with pytest.raises(cs.ShadowRefusal) as e:
            config(inventory_btc=None)
        assert e.value.code == cs.EXIT_USAGE

    def test_notional_above_the_cap_is_refused(self):
        with pytest.raises(cs.ShadowRefusal):
            cs.build_config(origin="synthetic_fixture", venue="historical",
                            borrow_apr=0.0, execution_mode="overlay",
                            inventory_btc=1.0, margin_multiple=5.0,
                            notional_usd=10_000.0)


# ---------------------------------------------------------------------------
# isolation: no order-capable method is reachable, no real store is written
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_the_shadow_venue_exposes_only_its_allow_list(self):
        public = {n for n in dir(cs.ShadowBroker)
                  if not n.startswith("_")
                  and callable(getattr(cs.ShadowBroker, n))}
        assert public <= cs.ShadowBroker.ALLOWED, public - cs.ShadowBroker.ALLOWED

    @pytest.mark.parametrize("name", [
        "_request", "client", "create_order", "place_order", "amend_order",
        "cancel_all", "cancel_orders", "set_leverage", "close_position",
        "transport", "base_url", "api_key", "get_equity", "place_conditional"])
    def test_an_exchange_capability_does_not_exist(self, name):
        broker = cs.ShadowBroker(config())
        with pytest.raises(cs.ShadowIsolationError):
            getattr(broker, name)

    def test_the_engine_is_wired_to_the_shadow_venue_and_nothing_else(
            self, tmp_path):
        rt = runtime(tmp_path)
        assert rt.engine.broker is rt.broker
        assert isinstance(rt.broker, cs.ShadowBroker)
        assert rt.engine.kill_switch.__self__ is rt.kill
        assert rt.engine.pair_risk.store is rt.kill

    def test_maker_orders_and_cancels_are_not_modelled(self):
        broker = cs.ShadowBroker(config())
        with pytest.raises(cs.ShadowIsolationError):
            broker.place_post_only(symbol="BTCUSDT", side="Sell", qty=1,
                                   price=1, product="linear")
        with pytest.raises(cs.ShadowIsolationError):
            broker.cancel_order(symbol="BTCUSDT", order_link_id="x",
                                product="linear")

    def test_the_module_imports_nothing_that_can_reach_an_exchange(self):
        tree = ast.parse(open(os.path.join(BOT, "carry_shadow.py")).read())
        banned = {"bybit_connection", "carry_broker", "trading_engine",
                  "persistence", "ledger", "main", "config", "private_ws_consumer",
                  "ws_transport", "websocket", "websockets", "ccxt", "pybit"}
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        assert not (top_level & banned), top_level & banned
        # `requests` exists only inside the public reader's fetch
        assert "requests" not in top_level
        everything = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                everything |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                everything.add(node.module.split(".")[0])
        assert not (everything & banned)

    def test_the_source_names_no_order_endpoint_and_sends_no_post(self):
        src = open(os.path.join(BOT, "carry_shadow.py")).read()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        for needle in ("/v5/order", "/v5/position", "/v5/account",
                       "requests.post", "signed=True", "X-BAPI-SIGN",
                       "api_secret", "orderLinkId"):
            assert needle not in code, needle
        assert set(cs.PUBLIC_PATHS) == {"/v5/market/time", "/v5/market/tickers",
                                        "/v5/market/funding/history"}

    def test_it_runs_with_every_venue_module_made_unimportable(self, tmp_path):
        """Poison the venue-facing modules, then run the whole lifecycle."""
        code = f"""
import sys
for m in ('bybit_connection','carry_broker','trading_engine','requests',
          'persistence','ledger','main','config'):
    sys.modules[m] = None
sys.path.insert(0, {BOT!r})
sys.path.insert(0, {HERE!r})
import carry_shadow as cs, test_carry_shadow as t
rt = cs.ShadowRuntime({str(tmp_path / 'poison.db')!r}, t.config())
out = [rt.step(o) for o in t.series()]
assert any('opened' in r['record']['decision']['action'] for r in out)
print('LIFECYCLE_WITHOUT_VENUE_MODULES_OK')
"""
        done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True)
        assert done.returncode == 0, done.stderr
        assert "LIFECYCLE_WITHOUT_VENUE_MODULES_OK" in done.stdout

    def test_a_run_opens_no_socket(self, tmp_path, monkeypatch):
        import socket

        def no_net(*a, **k):
            raise AssertionError("the shadow runtime opened a socket")

        monkeypatch.setattr(socket.socket, "connect", no_net)
        monkeypatch.setattr(socket, "create_connection", no_net)
        run_all(runtime(tmp_path), series())

    def test_the_account_store_is_never_touched(self, tmp_path):
        from persistence import StateStore
        real = str(tmp_path / "account.db")
        store = StateStore(real)
        store.save_ledger([])
        store.close()
        before = hashlib.sha256(open(real, "rb").read()).hexdigest()
        run_all(runtime(tmp_path, "shadow.db"), series())
        assert hashlib.sha256(open(real, "rb").read()).hexdigest() == before

    def test_a_real_store_cannot_be_used_as_the_shadow_book(self, tmp_path):
        from persistence import StateStore
        real = str(tmp_path / "somebook.db")
        StateStore(real).close()
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.ShadowRuntime(real, config())
        assert e.value.code == cs.EXIT_CONFIG

    def test_the_accounts_default_filename_is_refused(self, tmp_path):
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.ShadowRuntime(str(tmp_path / "trading_state.db"), config())
        assert e.value.code == cs.EXIT_CONFIG

    def test_the_shadow_book_has_only_shadow_tables(self, tmp_path):
        rt = runtime(tmp_path)
        run_all(rt, series())
        names = {r[0] for r in rt.store.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert all(n.startswith("shadow_") or n == "sqlite_sequence"
                   for n in names), names


# ---------------------------------------------------------------------------
# the live reader: public GETs only
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, result):
        self._r = result

    def raise_for_status(self):
        return None

    def json(self):
        return {"retCode": 0, "result": self._r}


def _fake_http(calls):
    now = int(time.time() * 1000)

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        if url.endswith("/v5/market/time"):
            return _Resp({"timeSecond": str(int(time.time()))})
        if url.endswith("/v5/market/tickers"):
            if params["category"] == "linear":
                return _Resp({"list": [{
                    "markPrice": "80000", "fundingRate": "0.0003",
                    "bid1Price": "79999", "ask1Price": "80001"}]})
            return _Resp({"list": [{"lastPrice": "79990"}]})
        if url.endswith("/v5/market/funding/history"):
            return _Resp({"list": [
                {"fundingRate": "0.0003",
                 "fundingRateTimestamp": str(now - k * H8 - 3_600_000)}
                for k in range(8)]})
        raise AssertionError(f"unexpected path {url}")

    return get


class TestPublicReader:
    def test_only_testnet_can_be_named_and_mainnet_is_not_reachable(self):
        assert set(cs.VENUE_BASE) == {"testnet"}
        assert all("api.bybit.com" not in u and "stream.bybit.com" not in u
                   for u in cs.VENUE_BASE.values())
        for venue in ("mainnet", "mainnet_public", "demo"):
            with pytest.raises(cs.ShadowRefusal):
                cs.PublicVenueReader(venue)
        done = cli("step", "--db", "/dev/null/never.db", "--origin",
                   "live_public", "--venue", "mainnet", "--borrow-apr", "0.0",
                   "--execution-mode", "overlay", "--inventory-btc", "0.01",
                   "--margin-multiple", "5", "--live")
        assert done.returncode == cs.EXIT_USAGE      # argparse refuses it
        src = open(os.path.join(BOT, "carry_shadow.py")).read()
        assert "https://api.bybit.com" not in src


    def test_it_touches_only_public_market_paths_and_sends_no_credentials(self):
        calls = []
        reader = cs.PublicVenueReader("testnet", http_get=_fake_http(calls))
        o = reader.observe()
        assert {u.replace(cs.VENUE_BASE["testnet"], "") for u, _ in calls} <= \
            set(cs.PUBLIC_PATHS)
        assert o["perp_mark"] == 80000 and o["spot_mark"] == 79990
        assert o["source"].startswith("bybit-public-rest:")
        assert len(o["settled_prints"]) == 8

    def test_a_non_public_path_cannot_be_requested(self):
        reader = cs.PublicVenueReader("testnet", http_get=_fake_http([]))
        with pytest.raises(cs.ShadowIsolationError):
            reader._fetch("/v5/order/create", {})
        with pytest.raises(cs.ShadowIsolationError):
            reader._fetch("/v5/position/list", {})

    def test_an_unreachable_venue_is_a_refusal_not_a_fault(self):
        def down(url, params, timeout):
            raise OSError("Tunnel connection failed: 403 Forbidden")

        with pytest.raises(cs.ShadowRefusal) as e:
            cs.PublicVenueReader("testnet", http_get=down).observe()
        assert e.value.code == cs.EXIT_REFUSED_INPUT

    def test_a_live_observation_runs_through_the_real_engine(self, tmp_path):
        reader = cs.PublicVenueReader("testnet", http_get=_fake_http([]))
        rt = cs.ShadowRuntime(
            str(tmp_path / "live.db"),
            config(origin="live_public", venue="testnet"))
        out = rt.step(reader.observe())
        assert out["outcome"] == "STEP_RECORDED"
        rec = out["record"]
        assert rec["evidence_class"] == "live_public" and rec["clock"] == "wall"
        assert rec["warmed_prints"] == 8
        assert rec["source"].startswith("bybit-public-rest:")


# ---------------------------------------------------------------------------
# the CLI, in a real subprocess
# ---------------------------------------------------------------------------


def cli(*args, stdin=None, env=None):
    return subprocess.run([sys.executable, os.path.join(BOT, "carry_shadow.py"),
                           *args], input=stdin, capture_output=True, text=True,
                          env=None if env is None else {**os.environ, **env})


def flags(tmp_path, origin, *extra, db="cli.db"):
    return ["--db", str(tmp_path / db), "--origin", origin, "--venue",
            "historical", "--borrow-apr", "0.0", "--execution-mode", "overlay",
            "--inventory-btc", "0.01", "--margin-multiple", "5", *extra]


class TestCli:
    def test_replay_runs_the_lifecycle_and_reports_modelled_economics(
            self, tmp_path):
        f = tmp_path / "obs.jsonl"
        f.write_text("\n".join(json.dumps(o) for o in series()))
        done = cli("replay", *flags(tmp_path, "historical_replay",
                                    "--observations", str(f)))
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert out["replayed"] == len(series())
        assert out["economics"]["label"].startswith("MODELLED")
        assert out["economics"]["fills"] >= 2   # the perp opens and closes
        # the same command again is a no-op: every observation is a duplicate
        again = cli("replay", *flags(tmp_path, "historical_replay",
                                     "--observations", str(f)))
        assert again.returncode == 0
        assert json.loads(again.stdout)["economics"] == out["economics"]

    def test_report_reads_the_book_back(self, tmp_path):
        f = tmp_path / "obs.jsonl"
        f.write_text("\n".join(json.dumps(o) for o in series()))
        cli("replay", *flags(tmp_path, "historical_replay",
                             "--observations", str(f)))
        done = cli("report", *flags(tmp_path, "historical_replay"))
        assert done.returncode == 0
        body = json.loads(done.stdout)
        assert body["book"]["origin"] == "historical_replay"
        assert body["economics"]["net_is_partial"] is True

    def test_a_fresh_supplied_observation_is_accepted_via_stdin(self, tmp_path):
        now = time.time()
        o = obs(int((now - 1800) * 1000), observed_at_s=now)
        done = cli("step", *flags(tmp_path, "supplied", "--observation", "-"),
                   stdin=json.dumps(o))
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["outcome"] == "STEP_RECORDED"

    def test_a_stale_supplied_observation_exits_3(self, tmp_path):
        o = obs(int((time.time() - 7200) * 1000))
        done = cli("step", *flags(tmp_path, "supplied", "--observation", "-"),
                   stdin=json.dumps(o))
        assert done.returncode == cs.EXIT_REFUSED_INPUT, done.stderr
        assert "REFUSED" in done.stderr

    def test_an_incomplete_observation_exits_3(self, tmp_path):
        done = cli("step", *flags(tmp_path, "supplied", "--observation", "-"),
                   stdin=json.dumps({"perp_mark": 1.0}))
        assert done.returncode == cs.EXIT_REFUSED_INPUT

    def test_unreadable_input_exits_2(self, tmp_path):
        done = cli("step", *flags(tmp_path, "supplied", "--observation", "-"),
                   stdin="not json")
        assert done.returncode == cs.EXIT_USAGE

    def test_a_config_change_on_an_existing_book_exits_4(self, tmp_path):
        f = tmp_path / "obs.jsonl"
        f.write_text(json.dumps(series()[0]))
        cli("replay", *flags(tmp_path, "historical_replay",
                             "--observations", str(f)))
        changed = flags(tmp_path, "historical_replay", "--observations", str(f))
        changed[changed.index("--borrow-apr") + 1] = "0.07"
        assert cli("replay", *changed).returncode == cs.EXIT_CONFIG

    def test_live_requires_the_live_origin_and_a_file_cannot_claim_it(
            self, tmp_path):
        a = cli("step", *flags(tmp_path, "supplied", "--live", db="a.db"))
        assert a.returncode == cs.EXIT_USAGE
        b = cli("step", *flags(tmp_path, "live_public", "--observation", "-",
                               db="b.db"), stdin=json.dumps(series()[0]))
        assert b.returncode == cs.EXIT_USAGE

    def test_a_replayed_file_cannot_claim_the_live_origin(self, tmp_path):
        now = time.time()
        f = tmp_path / "obs.jsonl"
        f.write_text(json.dumps(obs(int((now - 1800) * 1000),
                                    observed_at_s=now)))
        done = cli("replay", *flags(tmp_path, "live_public",
                                    "--observations", str(f), db="r.db"))
        assert done.returncode == cs.EXIT_USAGE, done.stderr
        assert not (tmp_path / "r.db").exists()

    def test_a_usage_refusal_leaves_no_book_to_block_the_corrected_command(
            self, tmp_path):
        bad = cli("step", *flags(tmp_path, "supplied", "--live", db="u.db"))
        assert bad.returncode == cs.EXIT_USAGE, bad.stderr
        assert not (tmp_path / "u.db").exists()
        now = time.time()
        o = obs(int((now - 1800) * 1000), observed_at_s=now)
        good = cli("step", *flags(tmp_path, "supplied", "--observation", "-",
                                  db="u.db"), stdin=json.dumps(o))
        assert good.returncode == 0, good.stderr

    def test_live_with_no_network_exits_3_and_creates_no_book(self, tmp_path):
        # a dead proxy makes the venue unreachable on every host, so this
        # never silently skips where the network happens to be open
        dead = {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy":
                "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        done = cli("step", "--db", str(tmp_path / "l.db"), "--origin",
                   "live_public", "--venue", "testnet", "--borrow-apr", "0.0",
                   "--execution-mode", "overlay", "--inventory-btc", "0.01",
                   "--margin-multiple", "5", "--live", env=dead)
        assert done.returncode == cs.EXIT_REFUSED_INPUT, done.stderr
        assert "the venue could not be read" in done.stderr
        assert not (tmp_path / "l.db").exists()

    def test_live_with_no_network_leaves_an_existing_book_byte_identical(
            self, tmp_path):
        path = tmp_path / "l.db"
        cs.ShadowRuntime(str(path), config(origin="live_public",
                                           venue="testnet")).close()
        live = ["--db", str(path), "--origin", "live_public", "--venue",
                "testnet", "--borrow-apr", "0.0", "--execution-mode",
                "overlay", "--inventory-btc", "0.01", "--margin-multiple", "5"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        done = cli("step", *live, "--live",
                   env={"HTTPS_PROXY": "http://127.0.0.1:9",
                        "https_proxy": "http://127.0.0.1:9",
                        "NO_PROXY": "", "no_proxy": ""})
        assert done.returncode == cs.EXIT_REFUSED_INPUT, done.stderr
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# packaging
# ---------------------------------------------------------------------------


class TestShippedInTheProductionImage:
    def _copied(self):
        text = re.sub(r"\\\n", " ", open(os.path.join(BOT, "Dockerfile")).read())
        out = set()
        for line in text.splitlines():
            if line.strip().startswith("COPY"):
                out |= {t.rstrip("/") for t in line.split()[1:-1]}
        return out

    def test_the_runtime_and_its_whole_import_closure_are_copied(self):
        from test_dockerfile import _import_closure
        closure = _import_closure("carry_shadow")
        copied = self._copied()
        missing = [m for m in sorted(closure)
                   if ("signals" if m == "signals" else m + ".py") not in copied]
        assert not missing, missing

    def test_the_image_entrypoint_is_unchanged(self):
        text = open(os.path.join(BOT, "Dockerfile")).read()
        assert 'ENTRYPOINT ["python3", "main.py"]' in text

    def test_it_is_invocable_from_the_image_layout(self, tmp_path):
        """Copy exactly what the Dockerfile copies, then run the CLI there."""
        import shutil
        app = tmp_path / "app"
        app.mkdir()
        for token in self._copied():
            src = os.path.join(BOT, token)
            if token in ("requirements.txt", "constraints.txt") or \
                    not os.path.exists(src):
                continue
            dst = app / token
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy(src, dst)
        o = obs(1_700_000_000_000)
        done = subprocess.run(
            [sys.executable, "-I", "carry_shadow.py", "step", "--db",
             str(tmp_path / "img.db"), "--origin", "synthetic_fixture",
             "--venue", "historical", "--borrow-apr", "0.0",
             "--execution-mode", "overlay", "--inventory-btc", "0.01",
             "--margin-multiple", "5", "--observation", "-"],
            input=json.dumps(o), capture_output=True, text=True, cwd=str(app))
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["outcome"] == "STEP_RECORDED"

    def test_ci_runs_the_runtime_inside_the_built_image(self):
        wf = open(os.path.join(os.path.dirname(BOT), ".github", "workflows",
                               "carry-verify.yml")).read()
        assert "carry_shadow.py" in wf and "tests/test_carry_shadow.py" in wf
