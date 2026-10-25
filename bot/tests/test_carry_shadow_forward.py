"""The post-gate FORWARD shadow book: a new identity, a frozen protocol, and no
way to mix evidence or resume under a different rule.

Offline. The "venue" is a scripted public-REST fake behind the real
`PublicVenueReader`; nothing here is a market observation and no figure is real
P&L. The previous shadow book is never touched: refusals are asserted to leave
the file byte-identical.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

import carry_costs  # noqa: E402
import carry_shadow as cs  # noqa: E402

H8 = 8 * 3600 * 1000
INV, MARGIN = 0.01, 5.0


class _Resp:
    def __init__(self, result):
        self._r = result

    def raise_for_status(self):
        return None

    def json(self):
        return {"retCode": 0, "result": self._r}


def fake_http(rate_bps=3.0, *, age_hours=1.0, calls=None):
    def get(url, params, timeout):
        if calls is not None:
            calls.append((url, dict(params)))
        now = int(time.time() * 1000)
        if url.endswith("/v5/market/time"):
            return _Resp({"timeSecond": str(int(time.time()))})
        if url.endswith("/v5/market/tickers"):
            if params["category"] == "linear":
                return _Resp({"list": [{
                    "markPrice": "80000", "fundingRate": str(rate_bps / 1e4),
                    "bid1Price": "79999", "ask1Price": "80001"}]})
            return _Resp({"list": [{"lastPrice": "79990"}]})
        if url.endswith("/v5/market/funding/history"):
            return _Resp({"list": [
                {"fundingRate": str(rate_bps / 1e4),
                 "fundingRateTimestamp": str(now - int(age_hours * 3_600_000) - k * H8)}
                for k in range(8)]})
        raise AssertionError(f"unexpected path {url}")
    return get


def reader(**kw):
    return cs.PublicVenueReader("testnet", http_get=fake_http(**kw))


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def new_book(tmp_path, name="post_gate.db", book_id="post_gate_v1"):
    db = str(tmp_path / name)
    cs.forward_init(db, cs.forward_config(book_id=book_id, inventory_btc=INV,
                                          margin_multiple=MARGIN))
    return db


def cli(*args):
    return subprocess.run([sys.executable, os.path.join(BOT, "carry_shadow.py"),
                           *args], capture_output=True, text=True, timeout=120)


# -- a separate identity ------------------------------------------------------

class TestANewBookWithItsOwnIdentity:
    def test_it_initialises_separately_and_records_its_identity(self, tmp_path):
        db = new_book(tmp_path)
        meta = cs._stored_meta(db)
        assert meta["book_id"] == "post_gate_v1"
        assert meta["rule_id"] == "carry_entry_gate_v1@BTCUSDT"
        assert meta["origin"] == "live_public"
        assert meta["protocol_sha"] == cs.protocol_hash()
        assert meta["boundary_utc"] == "2026-10-25T18:06:27Z"
        assert meta["config_fingerprint"]
        assert json.loads(meta["code_revision_at_creation"])["revision"]
        con = sqlite3.connect(db)
        kinds = [r[0] for r in con.execute("SELECT kind FROM shadow_events")]
        assert kinds == ["book_created"]
        assert con.execute("SELECT COUNT(*) FROM shadow_observations").fetchone()[0] == 0

    def test_the_identity_is_part_of_the_fingerprint(self):
        a = cs.forward_config(book_id="a", inventory_btc=INV, margin_multiple=MARGIN)
        b = cs.forward_config(book_id="b", inventory_btc=INV, margin_multiple=MARGIN)
        plain = cs.build_config(origin="live_public", venue="testnet",
                                borrow_apr=0.0, execution_mode="overlay",
                                inventory_btc=INV, margin_multiple=MARGIN)
        fps = {cs.config_fingerprint(x) for x in (a, b, plain)}
        assert len(fps) == 3

    def test_its_configuration_is_the_production_rule_unchanged(self):
        c = cs.forward_config(book_id="x", inventory_btc=INV, margin_multiple=MARGIN)
        k = c["engine_constants"]
        assert k["ASSUMED_HOLD_DAYS"] == 30.0 and k["DEMONSTRATED_HOLD_DAYS"] == 5.8
        assert c["origin"] == "live_public" and c["venue"] == "testnet"
        assert c["execution_style"] == "taker" and c["borrow_apr"] == 0.0

    @pytest.mark.parametrize("bad", ["", "  ", "a b", "x/y", "x;y", None])
    def test_a_book_id_must_be_a_short_plain_name(self, bad):
        with pytest.raises(cs.ShadowRefusal):
            cs.forward_book_identity(bad)

    def test_the_boundary_is_the_gate_merge(self):
        b = cs.POST_GATE_BOUNDARY
        assert b["merge_commit"].startswith("3aadf2f")
        parsed = dt.datetime.strptime(b["merged_utc"], "%Y-%m-%dT%H:%M:%SZ"
                                      ).replace(tzinfo=dt.timezone.utc)
        assert int(parsed.timestamp() * 1000) == b["merged_ms"]

    def test_it_will_not_be_created_before_the_boundary(self, tmp_path):
        db = str(tmp_path / "early.db")
        with pytest.raises(cs.ShadowRefusal):
            cs.forward_init(db, cs.forward_config(
                book_id="x", inventory_btc=INV, margin_multiple=MARGIN),
                now_s=cs.POST_GATE_BOUNDARY["merged_ms"] / 1000.0 - 1)
        assert not os.path.exists(db)


# -- an existing database is never reused, reset, migrated or overwritten --------

def legacy_book(tmp_path, name="legacy.db"):
    """A live_public book as the PREVIOUS rule built it: engine constants without
    the measured-hold entry and no book identity."""
    cfg = cs.build_config(origin="live_public", venue="testnet", borrow_apr=0.0,
                          execution_mode="overlay", inventory_btc=INV,
                          margin_multiple=MARGIN)
    del cfg["engine_constants"]["DEMONSTRATED_HOLD_DAYS"]
    db = str(tmp_path / name)
    rt = cs.ShadowRuntime(db, cfg)
    rt.close()
    return db, cfg


class TestOldBooksRefuseAndAreLeftAlone:
    def test_forward_init_refuses_every_existing_path(self, tmp_path):
        legacy, _ = legacy_book(tmp_path)
        stray = tmp_path / "stray.db"
        stray.write_bytes(b"not a database")
        for path in (legacy, str(stray), new_book(tmp_path)):
            before = digest(path)
            with pytest.raises(cs.ShadowRefusal) as e:
                cs.forward_init(path, cs.forward_config(
                    book_id="again", inventory_btc=INV, margin_multiple=MARGIN))
            assert e.value.code == cs.EXIT_CONFIG
            assert digest(path) == before

    def test_the_previous_book_refuses_to_resume_under_the_new_rule(self, tmp_path):
        legacy, cfg = legacy_book(tmp_path)
        before = digest(legacy)
        new_cfg = cs.build_config(origin="live_public", venue="testnet",
                                  borrow_apr=0.0, execution_mode="overlay",
                                  inventory_btc=INV, margin_multiple=MARGIN)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.ShadowRuntime(legacy, new_cfg)
        assert e.value.code == cs.EXIT_CONFIG
        assert "frozen configuration mismatch" in str(e.value)
        assert digest(legacy) == before          # not deleted, reset or migrated

    def test_forward_commands_refuse_a_book_with_no_forward_identity(self, tmp_path):
        legacy, _ = legacy_book(tmp_path)
        before = digest(legacy)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_step(legacy, observer=reader())
        assert e.value.code == cs.EXIT_CONFIG and "not a forward book" in str(e.value)
        with pytest.raises(cs.ShadowRefusal):
            cs.forward_report(legacy)
        assert digest(legacy) == before

    def test_a_forward_book_refuses_to_resume_if_the_rule_constants_moved(
            self, tmp_path, monkeypatch):
        db = new_book(tmp_path)
        cs.forward_step(db, observer=reader())
        before = digest(db)
        monkeypatch.setattr(carry_costs, "DEMONSTRATED_HOLD_DAYS", 6.0)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_step(db, observer=reader())
        assert e.value.code == cs.EXIT_CONFIG
        assert "frozen configuration mismatch" in str(e.value)
        with pytest.raises(cs.ShadowRefusal):
            cs.forward_report(db)
        assert digest(db) == before

    def test_the_wrong_book_id_is_refused(self, tmp_path):
        db = new_book(tmp_path)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_step(db, book_id="other", observer=reader())
        assert e.value.code == cs.EXIT_CONFIG

    def test_a_missing_book_is_never_created_by_step_or_report(self, tmp_path):
        db = str(tmp_path / "ghost.db")
        for fn in (lambda: cs.forward_step(db, observer=reader()),
                   lambda: cs.forward_report(db)):
            with pytest.raises(cs.ShadowRefusal) as e:
                fn()
            assert e.value.code == cs.EXIT_USAGE
        assert not os.path.exists(db)


# -- evidence classes cannot be mixed ----------------------------------------------

class TestEvidenceClassesStaySeparate:
    @pytest.mark.parametrize("origin", ["supplied", "historical_replay",
                                        "synthetic_fixture"])
    def test_another_class_cannot_write_into_the_forward_book(self, tmp_path, origin):
        db = new_book(tmp_path)
        before = digest(db)
        other = cs.build_config(origin=origin, venue="historical", borrow_apr=0.0,
                                execution_mode="overlay", inventory_btc=INV,
                                margin_multiple=MARGIN)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.ShadowRuntime(db, other)
        assert e.value.code == cs.EXIT_CONFIG and "refused" in str(e.value)
        assert digest(db) == before

    def test_the_forward_book_cannot_be_opened_as_a_plain_live_book(self, tmp_path):
        db = new_book(tmp_path)
        plain = cs.build_config(origin="live_public", venue="testnet",
                                borrow_apr=0.0, execution_mode="overlay",
                                inventory_btc=INV, margin_multiple=MARGIN)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.ShadowRuntime(db, plain)
        assert "frozen configuration mismatch" in str(e.value)

    def test_a_live_book_cannot_receive_a_replayed_or_supplied_observation(
            self, tmp_path):
        db = new_book(tmp_path)
        before = digest(db)
        now = time.time()
        for origin in ("supplied", "historical_replay"):
            # fresh for `supplied` (judged on the wall clock), stamped on its own
            # clock for a replay: each is a VALID observation of its class, so the
            # only thing that can refuse it is the book's evidence-class binding
            at = now if origin == "supplied" else 1_700_000_000.0
            line = json.dumps({
                "perp_mark": 80000.0, "spot_mark": 79990.0, "funding_bps": 3.0,
                "funding_print_bps": 3.0, "funding_print_ms": int((at - 3600) * 1000),
                "observed_at_s": at, "source": "test fixture"})
            f = tmp_path / f"{origin}.jsonl"
            f.write_text(line + "\n")
            r = cli("replay", "--db", db, "--origin", origin, "--venue",
                    "historical", "--borrow-apr", "0.0", "--execution-mode",
                    "overlay", "--inventory-btc", "0.01", "--margin-multiple",
                    "5", "--observations", str(f))
            # refused by the book's own evidence-class binding, not by usage
            assert r.returncode == cs.EXIT_CONFIG, (origin, r.stderr)
            assert "refused" in r.stderr
        assert digest(db) == before

    def test_a_forward_config_has_no_origin_to_choose(self):
        import inspect
        assert "origin" not in inspect.signature(cs.forward_config).parameters
        assert "venue" not in inspect.signature(cs.forward_config).parameters

    def test_a_stale_observation_books_nothing(self, tmp_path):
        db = new_book(tmp_path)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_step(db, observer=reader(age_hours=20.0))
        assert e.value.code == cs.EXIT_REFUSED_INPUT
        rep = cs.forward_report(db)
        assert rep["summary"]["observations"] == 0

    def test_a_venue_that_cannot_be_read_books_nothing(self, tmp_path):
        db = new_book(tmp_path)

        def down(url, params, timeout):
            raise OSError("no route")
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_step(db, observer=cs.PublicVenueReader(
                "testnet", http_get=down))
        assert e.value.code == cs.EXIT_REFUSED_INPUT
        assert cs.forward_report(db)["summary"]["observations"] == 0


# -- what the book records and reports ------------------------------------------------

class TestWhatIsRecordedAndReported:
    def test_an_eligible_observation_is_a_simulated_entry_through_the_production_gate(
            self, tmp_path):
        db = new_book(tmp_path)
        calls = []
        res = cs.forward_step(db, observer=cs.PublicVenueReader(
            "testnet", http_get=fake_http(3.0, calls=calls)))
        rec = res["record"]
        assert res["outcome"] == "STEP_RECORDED"
        assert rec["evidence_class"] == "live_public" and rec["origin"] == "live_public"
        assert rec["source"].startswith("bybit-public-rest:")
        assert rec["source_ts"]["funding_print_ms"] > 0 and rec["source_ts"]["observed_at_s"] > 0
        assert rec["config_fingerprint"] and rec["code"]["revision"]
        assert rec["decision"]["reason"] and rec["intents_and_fills"]
        for f in rec["intents_and_fills"]:
            assert f["fee_class"] == cs.ASSUMED
            assert f["slippage_class"] == cs.ESTIMATED
            assert "no order was sent anywhere" in f["fill_basis"]
        a = rec["assumptions"]
        assert a["fees"] == cs.ASSUMED and a["market_impact"] == cs.UNAVAILABLE
        assert a["liquidation_distance"] == cs.UNAVAILABLE
        assert {c[0].split("/v5")[1] for c in calls} <= {
            "/market/time", "/market/tickers", "/market/funding/history"}
        assert all("api-testnet.bybit.com" in c[0] for c in calls)

    def test_a_gate_refusal_is_recorded_as_a_reason_and_sends_nothing(self, tmp_path):
        db = new_book(tmp_path)
        res = cs.forward_step(db, observer=reader(rate_bps=0.3))
        assert res["record"]["decision"]["reason"] == \
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"
        assert res["record"]["intents_and_fills"] == []
        rep = cs.forward_report(db)
        assert rep["summary"]["entry_gate_refusals"] == {
            "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD": 1}
        assert rep["summary"]["simulated_entries"] == 0

    def test_the_report_states_what_it_is_and_what_it_is_not(self, tmp_path):
        db = new_book(tmp_path)
        cs.forward_step(db, observer=reader(rate_bps=3.0))
        rep = cs.forward_report(db)
        assert rep["summary"]["observations"] == 1
        assert rep["summary"]["verdict"] == "INSUFFICIENT_FORWARD_SAMPLE"
        assert rep["summary"]["pnl_claim_allowed"] is False
        assert rep["evidence"]["pnl_claim_allowed"] is False
        assert rep["evidence"]["class"] == "live_public"
        text = rep["evidence"]["what_it_is_not"]
        assert "no order was ever sent" in text and "assumed" in text
        assert "unavailable" in text and "partial" in text
        assert rep["economics"]["net_is_partial"] is True
        assert "NOT REAL ACCOUNT P&L" in rep["economics"]["label"]
        assert rep["book"]["rule_id"] == "carry_entry_gate_v1@BTCUSDT"
        assert any("closed trades" in s for s in rep["summary"]["shortfalls"])
        assert any("days" in s for s in rep["summary"]["shortfalls"])

    def test_a_repeated_identical_observation_is_a_no_op(self, tmp_path):
        db = new_book(tmp_path)
        obs = reader().observe()
        cs.precheck_observation(cs._config_of_forward_book(db, None), obs)
        rt = cs.ShadowRuntime(db, cs._config_of_forward_book(db, None))
        try:
            assert rt.step(obs)["outcome"] == "STEP_RECORDED"
            assert rt.step(obs)["outcome"] == "DUPLICATE_OBSERVATION"
        finally:
            rt.close()
        assert cs.forward_report(db)["summary"]["observations"] == 1


# -- the frozen protocol -----------------------------------------------------------------

class TestTheProtocolIsFrozenBeforeData:
    def test_its_thresholds_are_declared_and_say_no_claim_is_possible(self):
        p = cs.FORWARD_PROTOCOL
        assert p["adequacy"]["min_closed_trades"] == 60
        assert p["adequacy"]["min_window_days"] == 90
        assert p["pnl_claim_allowed"] is False
        assert p["evidence_class"] == "live_public"
        assert set(p["verdicts"]) == {"INSUFFICIENT_FORWARD_SAMPLE",
                                      "FORWARD_SAMPLE_ADEQUATE_FOR_HUMAN_REVIEW"}
        assert "a parameter change" in p["never"]

    def test_the_report_refuses_if_the_protocol_was_edited_after_the_book_began(
            self, tmp_path, monkeypatch):
        db = new_book(tmp_path)
        edited = json.loads(json.dumps(cs.FORWARD_PROTOCOL))
        edited["adequacy"]["min_closed_trades"] = 5
        monkeypatch.setattr(cs, "FORWARD_PROTOCOL", edited)
        with pytest.raises(cs.ShadowRefusal) as e:
            cs.forward_report(db)
        assert "protocol" in str(e.value)

    def test_the_strongest_verdict_still_claims_no_profit(self):
        con = sqlite3.connect(":memory:")
        con.executescript(cs._DDL)
        # 60 closed round trips over 100 days: the sample is "adequate" for a
        # human to look at; it is not a result
        for i in range(60):
            con.execute("INSERT INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"o{i}", i, f"k{i}", "Sell", "linear", .01, 80000., 79990.,
                         .4, "assumed", None, "unavailable"))
            con.execute("INSERT INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"c{i}", i, f"k{i}", "Buy", "linear", .01, 80000., 79990.,
                         .4, "assumed", None, "unavailable"))
        for i, t in enumerate((1.0e9, 1.0e9 + 100 * 86400)):
            rec = {"decision": {"reason": "HEDGED_AND_COLLECTING"},
                   "source_ts": {"observed_at_s": t}, "assumptions": {}}
            con.execute("INSERT INTO shadow_steps VALUES (?,?,?)",
                        (i + 1, f"k{i}", json.dumps(rec)))
        s = cs.forward_summary(con)
        assert s["verdict"] == "FORWARD_SAMPLE_ADEQUATE_FOR_HUMAN_REVIEW"
        assert s["pnl_claim_allowed"] is False and s["shortfalls"] == []


# -- it cannot trade ---------------------------------------------------------------------

class TestItCannotPlaceOrCancel:
    def test_the_shipped_module_has_no_exchange_client_or_order_path(self):
        src = open(os.path.join(BOT, "carry_shadow.py"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines()
                         if not l.lstrip().startswith("#"))
        for forbidden in ("import bybit_connection", "import carry_broker",
                          "import trading_engine", "from bybit_connection",
                          "from carry_broker", "/v5/order/create", "requests.post",
                          "api.bybit.com", "BYBIT_API_KEY", "BYBIT_API_SECRET"):
            assert forbidden not in code, forbidden

    def test_a_forward_step_cannot_reach_a_cancel_or_a_resting_order(self, tmp_path):
        db = new_book(tmp_path)
        config = cs._config_of_forward_book(db, None)
        broker = cs.ShadowBroker(config)
        with pytest.raises(cs.ShadowIsolationError):
            broker.cancel_order(symbol="BTCUSDT", order_link_id="x", product="linear")
        with pytest.raises(cs.ShadowIsolationError):
            broker.place_post_only(symbol="BTCUSDT", side="Sell", qty=.001,
                                   price=1.0, product="linear")
        with pytest.raises(cs.ShadowIsolationError):
            broker.amend_order()                 # nothing off the allow-list exists

    def test_the_forward_book_is_not_the_accounts_store(self, tmp_path):
        with pytest.raises(cs.ShadowRefusal):
            cs.forward_init(str(tmp_path / "trading_state.db"), cs.forward_config(
                book_id="x", inventory_btc=INV, margin_multiple=MARGIN))


# -- the command line -----------------------------------------------------------------------

class TestTheCommandLine:
    def test_init_then_report_then_a_second_init_is_refused(self, tmp_path):
        db = str(tmp_path / "cli.db")
        r = cli("forward-init", "--db", db, "--book-id", "post_gate_v1",
                "--inventory-btc", "0.01", "--margin-multiple", "5")
        assert r.returncode == cs.EXIT_OK, r.stderr
        out = json.loads(r.stdout)
        assert out["book"]["book_id"] == "post_gate_v1"
        assert out["orders"].startswith("none")
        before = digest(db)
        rep = cli("forward-report", "--db", db)
        assert rep.returncode == cs.EXIT_OK, rep.stderr
        body = json.loads(rep.stdout)
        assert body["summary"]["verdict"] == "INSUFFICIENT_FORWARD_SAMPLE"
        assert body["summary"]["observations"] == 0
        again = cli("forward-init", "--db", db, "--book-id", "other",
                    "--inventory-btc", "0.01", "--margin-multiple", "5")
        assert again.returncode == cs.EXIT_CONFIG
        assert "already exists" in again.stderr
        assert digest(db) == before

    def test_step_and_report_on_a_missing_book_are_usage_errors(self, tmp_path):
        db = str(tmp_path / "none.db")
        for cmd in ("forward-step", "forward-report"):
            r = cli(cmd, "--db", db)
            assert r.returncode == cs.EXIT_USAGE
        assert not os.path.exists(db)
