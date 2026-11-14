"""ms_pipeline: the integration proof of the multi-symbol system. Every test runs the REAL scanner -> allocator -> risk -> authority -> lifecycle -> simulated venue / fill model ->
ledger -> artifacts -> control centre stack on SYNTHETIC series planted by the test (nothing here is an exchange observation; every output is labelled INJECTED_TEST_DATA).
Nothing is sent anywhere: the only order sink is ``ms_execution.SimVenue``, the authority is bound to an in-process simulation, there is no clock and no network.

The ``check_*`` functions are the proofs; the MUTATION tests at the bottom break one behaviour at a time and require the matching proof to FAIL (so a proof that cannot fail is caught).
"""
from __future__ import annotations

import collections
import json
import math
import os
import socket
import sys
from dataclasses import replace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import control_centre as CCE  # noqa: E402
import entry_authority as EA  # noqa: E402
import ms_accounting as MACC  # noqa: E402
import ms_authority as MAUTH  # noqa: E402
import ms_execution as EX  # noqa: E402
import ms_fillmodel as FM  # noqa: E402
import ms_pipeline as MP  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_risk as MR  # noqa: E402
import ms_types as T  # noqa: E402

H = 3_600_000
D = 24 * H
T0 = 1_700_000_000_000 // D * D
CAND, DIGEST = "cand_carry_v1", "d" * 64
SPEC = {"kind": "SPOT_PERP_CARRY", "style": "MAKER_FIRST"}
FILL = FM.FillParams(bar_policy="ASSUME_THROUGH_MULT", bar_through_mult=1.0, markout_mean_bps=1.0)
GROUP_STATES = {s.value for s in T.GroupState}
PROMOTED_ROW = {"candidate": CAND, "name": "cash_and_carry", "verdict": "INSUFFICIENT_DATA", "readiness_level": "PROMISING", "rejection_reasons": []}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Nothing in this file may open a socket."""
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# ------------------------------------------------------------------------------------------------ the synthetic world

def ser(sym, interval=480, rate=5.0, n_days=14, base=100.0, spot_listed=True, delivery_day=None, wig=0.05, mark_from_h=0, ticks=None, status=None, flat=False):
    """One synthetic perp: marks hourly (a tiny sine wiggle so bar-based fills exist), a PLANTED constant funding rate every ``interval`` minutes, optional delivery (marks and
    funding stop there), optional explicit non-Trading status, optional measured trade ticks."""
    last = n_days * 24 if delivery_day is None else delivery_day * 24
    w = 0.0 if flat else wig
    marks = tuple((T0 + i * H, base + w * math.sin(1.1 * i + 0.3 * sum(map(ord, sym)))) for i in range(mark_from_h, last + 1))
    fund = tuple((T0 + k * interval * 60_000, rate) for k in range(1, (last * 60) // interval + 1))
    inst = None
    if status is not None:
        inst = T.Instrument(sym, None, status, 0, None if delivery_day is None else T0 + delivery_day * D, interval, sym[:-4], "USDT", "0.01", "0.01", "0.01", "1000000", "1000000", "5",
                            "10", sym, "HEDGEABLE_ON_BYBIT" if spot_listed else "PERP_ONLY")
    return RP.ReplaySeries(sym, fund, marks, interval, sym[:-4], delivery_ms=None if delivery_day is None else T0 + delivery_day * D, spot_listed=spot_listed, ticks=ticks, instrument=inst)


def world(**kw):
    """Mixed 480 / 240 minute funding, hedgeable and PERP_ONLY, one contract Closed mid-window, one never Trading."""
    return [ser("AAAUSDT", 480, 5.0, **kw), ser("BBBUSDT", 240, 3.0, **kw), ser("CCCUSDT", 480, 6.0, **kw), ser("DDDUSDT", 480, 8.0, spot_listed=False, **kw),
            ser("EEEUSDT", 240, 4.0, delivery_day=8, **kw), ser("FFFUSDT", 480, 9.0, status="PreLaunch", **kw)]


def cfg(**kw):
    d = dict(start_ms=T0, end_ms=T0 + 13 * D, fill=FILL, hold_ms=4 * D, decision_every_ms=8 * H, spec_digest=DIGEST,
             portfolio=PF.PortfolioPolicy(max_wait_ms=3 * H, style="MAKER_FIRST", max_symbol_notional_usd=10_000.0))
    d.update(kw)
    return RP.ReplayConfig(**d)


def go(tmp_path, series=None, c=None, name="art", spec=SPEC, **kw):
    params = dict(kw.pop("scan_params", None) or {})
    if "persistence" in MP.SC.DEFAULT_PARAMS:                    # the scanner's modelled funding-persistence haircut is a separate knob: these proofs plant EXACT funding and check the arithmetic, so it is off (1.0)
        params.setdefault("persistence", 1.0)
    params.setdefault("opportunity_cost_apr", 0.0)                     # a STATED 0% (an unstated rate is INSUFFICIENT_OWNER_INPUT: test_ms_pipeline_cost_of_capital); these proofs are about the stack's arithmetic
    kw["scan_params"] = params
    kw.setdefault("competition_row", PROMOTED_ROW)                  # these proofs are about the stack, not the gate: the candidate is promoted (test_ms_pipeline_cli covers the gate)
    return MP.run(series if series is not None else world(), spec=spec, candidate_id=CAND, spec_digest=DIGEST, replay_cfg=c or cfg(), artifacts_dir=str(tmp_path / name), **kw)


def cc_state(pr, **kw):
    return CCE.build_state(data_root=None, universe_dir=None, artifacts_dir=os.path.dirname(pr.files["replay_report.json"]), env={}, now_ms=pr.result.cfg.end_ms + 1000, **kw)


def art(pr, name):
    with open(pr.files[name], encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def happy(tmp_path_factory):
    d = tmp_path_factory.mktemp("happy")
    series = world()
    return series, go(d, series)


# ------------------------------------------------------------------------------------------------ proofs (also used by the mutations)

def expected_funding(res, series):
    """Independent re-derivation from the FILLS and the series (not from the ledger): for every symbol and every settlement print in the window, the signed perp position just before the
    print (a fill AT the print instant is not paid) times the mark at the print time times the rate, with the sign of the position (a short with a positive rate RECEIVES)."""
    out = {}
    fills = [(f, res.fills_cat.get(f.fill_id, "linear")) for f in res.fills]
    for s in series:
        evs = sorted((f.ts_ms, f.qty if f.side == "Buy" else -f.qty) for f, c in fills if f.symbol == s.symbol and c == "linear")
        per_ts = {}
        for ts, rate in s.funding:
            if ts > res.cfg.end_ms:
                break
            q = sum(d for t, d in evs if t < ts)
            if abs(q) > 1e-12:
                m = [p for t, p in s.mark if t <= ts][-1]
                per_ts[ts] = -q * m * rate / 1e4
        out[s.symbol] = per_ts
    return out


def check_funding(pr, series):
    res, led = pr.result, pr.ledger
    exp = expected_funding(res, series)
    booked = led.ledger._funding
    n = 0
    for s in series:
        got = {ts: a for ts, (a, _p) in booked.get(s.symbol, {}).items()}
        assert sorted(got) == sorted(exp[s.symbol]), s.symbol                               # credited exactly at the settlement timestamps, no other
        for ts, a in exp[s.symbol].items():
            assert got[ts] == pytest.approx(a, rel=1e-9, abs=1e-9)                          # with the right sign and size
            n += 1
    assert n > 0
    assert led.pnl()["funding"] == pytest.approx(sum(sum(v.values()) for v in exp.values()))
    return exp


def check_fees(pr):
    res, led = pr.result, pr.ledger
    fees = led.pnl()["ledger"]["fees"]
    assert fees is not None and fees > 0
    assert fees == pytest.approx(sum(f.fee_usd for f in res.fills))
    assert all(f.fee_usd > 0 for f in res.fills if not f.fill_id.startswith("FORCED"))
    return fees


def check_net_identity(pr):
    """ONE net definition everywhere: net = gross price leg AT MID + funding - fees - taker slippage - adverse selection, each modelled cost exactly once. Re-derived here from the
    FILLS, the series' funding and the groups' own adverse selection (never from the ledger), then compared with every place a net is stated."""
    res, led = pr.result, pr.ledger
    fills = [(f, res.fills_cat.get(f.fill_id, "linear")) for f in res.fills]
    cash = sum((-1.0 if f.side == "Buy" else 1.0) * f.qty * f.price for f, _c in fills)
    openq = collections.defaultdict(float)
    for f, c in fills:
        openq[(f.symbol, c)] += f.qty if f.side == "Buy" else -f.qty
    # OPEN legs are valued at what CLOSING them would yield (deliberate change from "at mid"): a long at the BID, a short at the ASK, less the modelled taker exit fee. The synthetic book
    # is mid -/+ half the configured spread, so the exit price is re-derived here from the mark and the config, not read back from the ledger.
    half = res.cfg.synthetic_spread_bps / 2e4
    mtm = 0.0
    for (sym, c), q in openq.items():
        if abs(q) > 1e-12:
            px = led.marks[(sym, c)] * (1 - half if q > 0 else 1 + half)
            mtm += q * px - abs(q) * px * (res.cfg.fill.taker_bps_perp if c == "linear" else res.cfg.fill.taker_bps_spot) / 1e4
    funding = led.pnl()["funding"]
    fees = sum(f.fee_usd for f, _c in fills)
    adverse = sum(g.adverse_usd for g in res.groups.values())
    taker = sum(g.taker_slip_usd for g in res.groups.values())
    net = cash + mtm + funding - fees - adverse                                              # taker slippage is already inside the fill prices: subtracted ONCE, there
    assert adverse > 0 and fees > 0
    p = led.pnl()
    rr, lp = art(pr, "replay_report.json"), art(pr, "ledger_pnl.json")
    tol = dict(rel=1e-9, abs=1e-6)
    assert p["net"] == pytest.approx(net, **tol)
    assert res.pnl["net"] == pytest.approx(net, **tol)
    assert rr["net_pnl_usd"] == pytest.approx(net, **tol)
    assert lp["realised_net_usd"] + lp["unrealised_usd"] == pytest.approx(net, **tol)
    assert pr.report["pnl"]["net_usd"] == pytest.approx(net, **tol)
    assert pr.summary["ledger"]["net"] == pytest.approx(net, **tol)
    assert res.equity_curve[-1][1] - res.cfg.equity0 == pytest.approx(net, **tol)             # the equity curve (and so the drawdown) is on the same net
    assert sum(r["net_pnl_usd"] for r in rr["symbols"]) == pytest.approx(net, **tol)
    # gross is AT MID: net = gross - fees - taker slippage - adverse selection
    assert rr["gross_pnl_usd"] - rr["fees_usd"] - rr["slippage_usd"] == pytest.approx(rr["net_pnl_usd"], **tol)
    assert rr["slippage_usd"] == pytest.approx(taker + adverse, **tol) and rr["adverse_selection_usd"] == pytest.approx(adverse, **tol)
    pp = pr.report["pnl"]
    assert pp["gross_usd"] - pp["fees_usd"] - pp["slippage_usd"] == pytest.approx(pp["net_usd"], **tol)
    # per group: the per-trade observation net is the same definition; ledger net == sum over EVERY group == closed observations + the groups still open / never closed
    gnet = {gid: g.price_usd + g.funding_usd - g.fees_usd - g.adverse_usd for gid, g in res.groups.items()}
    assert sum(gnet.values()) == pytest.approx(net, **tol)
    obs = RP.observations(res)
    assert obs and all(o.net_usd == pytest.approx(gnet[o.group_id], **tol) for o in obs)
    rest = sum(v for gid, v in gnet.items() if gid not in {o.group_id for o in obs})
    assert sum(o.net_usd for o in obs) + rest == pytest.approx(lp["realised_net_usd"] + lp["unrealised_usd"], **tol)
    return net


def check_consistent(pr, *, allow_open=True):
    """The invariants that must hold after ANY run, faulted or not."""
    res, led = pr.result, pr.ledger
    st = cc_state(pr)
    for sec in ("portfolio", "execution", "risk"):
        assert st[sec]["state"] == "OK", (sec, st[sec].get("error"))
    assert st["portfolio"]["pnl"]["state"] == "OK" and st["research"]["opportunities"]["state"] == "OK" and st["research"]["competition"]["state"] == "OK"
    # the pipeline writes eight files; universe_scope / funnel / testnet_session come from other tools, so NO_DATA is correct for them, ERROR never is
    _other = {"universe_scope.json", "funnel.json", "testnet_session.json"}
    assert all(s["state"] == "OK" or (s["file"] in _other and s["state"] == "NO_DATA") for s in st["evidence"]["sources"]), [(s["file"], s["state"], s["error"]) for s in st["evidence"]["sources"] if s["state"] != "OK"]
    states = {gid: (s.value if hasattr(s, "value") else s) for gid, s in res.states.items()}
    assert set(states.values()) <= GROUP_STATES
    assert art(pr, "replay_report.json")["groups_by_state"] == dict(sorted(collections.Counter(states.values()).items()))
    assert art(pr, "replay_report.json")["n_groups"] == len(states)
    p = led.pnl()["ledger"]
    # fills arithmetic == the venue's OWN position read == the ledger's open quantities (per symbol and category)
    venue = {k: v for k, v in res.venue.positions().items() if abs(v) > 1e-9}
    ledq = collections.defaultdict(float)
    for key, leg in p["per_leg"].items():
        gid, rest = key.split("/")
        ledq[rest.replace(":", ":", 1)] += leg["qty"]
    led_pos = {f"{k.split(':')[0]}:{k.split(':')[1]}": v for k, v in ledq.items() if abs(v) > 1e-9}
    assert led_pos.keys() == venue.keys() and all(led_pos[k] == pytest.approx(venue[k], abs=1e-6) for k in venue)
    assert p["fees"] == pytest.approx(sum(f.fee_usd for f in res.fills))
    # the artifacts state the ledger's own numbers; per-symbol rows add up to the total
    rr, lp = art(pr, "replay_report.json"), art(pr, "ledger_pnl.json")
    if p["net"] is not None:
        net = led.pnl()["net"]                                                               # the ONE net: the raw ledger's net minus the modelled adverse selection
        assert rr["net_pnl_usd"] == pytest.approx(net) == pytest.approx(res.pnl["net"])
        assert lp["realised_net_usd"] + lp["unrealised_usd"] == pytest.approx(net)
        assert sum(r["net_pnl_usd"] for r in rr["symbols"]) == pytest.approx(net)
        # the raw ledger values open legs AT MID; the one net deliberately values them at the exit side (exit_adjustment, incl. the taker exit fee)
        assert net == pytest.approx(p["net"] + led.pnl()["exit_adjustment"] - led.pnl()["adverse_selection"])
    # no unreconciled leg is ever a claimable profit
    open_legs = [k for k, leg in p["per_leg"].items() if abs(leg["qty"]) > 1e-9]
    if open_legs:
        assert p["claimable_profit"] is None and p["status"] == "UNRECONCILED"
    if p["claimable_profit"] is not None:                                                    # a claimable figure exists only for a book that is flat on the venue's own read
        assert not venue and not any(s in ("HEDGED", "UNHEDGED", "UNKNOWN_OUTCOME", "PARTIAL", "EXITING") for s in states.values())
    assert art(pr, "competition.json")["candidates"][0]["status"] != "SUFFICIENT_EVIDENCE"
    bad = [g for g, s in states.items() if s in ("UNHEDGED", "UNKNOWN_OUTCOME")]
    if bad:
        assert any(a["code"] == "GROUPS_UNHEDGED_OR_UNKNOWN" for a in art(pr, "risk_state.json")["alerts"])
        assert st["execution"]["unhedged_or_unknown"] == len(bad)
    return states


def svals(res):
    return [getattr(v, "value", v) for v in res.states.values()]


def tamper(monkeypatch):
    """Alter the quantity of every body the pipeline is about to ask the authority about (what a bug or an attacker between the plan and the wire would do)."""
    real = MP.body_from_request

    def bad(req):
        b = real(req)
        b["qty"] = format(float(b["qty"]) * 2, "f")
        return b
    monkeypatch.setattr(MP, "body_from_request", bad)


def assert_tamper_blocked(pr):
    assert pr.gates.refusals, "the authority refused nothing"
    assert any("BODY_FIELD_NOT_PLAN:qty" in r for _l, rs in pr.gates.refusals for r in rs)
    assert pr.result.venue.orders == {} and pr.result.fills == []                          # nothing reached the venue, nothing was booked
    assert not any(s in ("HEDGED", "CLOSED") for s in svals(pr.result))


def touch_world():
    """Flat marks and measured trades that only TOUCH the resting sell's price (100.01): a touch is not a trade-through."""
    ticks = tuple(FM.Tick(T0 + i * H + 1000, 100.01, 1e6) for i in range(0, 80))
    return [ser("TTTUSDT", 480, 8.0, n_days=3, flat=True, ticks=ticks)]


def through_world():
    ticks = tuple(FM.Tick(T0 + i * H + 1000, 100.5, 1e6) for i in range(0, 80))
    return [ser("TTTUSDT", 480, 8.0, n_days=3, flat=True, ticks=ticks)]


SHORT = dict(end_ms=T0 + 3 * D)


def assert_touch_is_not_a_fill(pr):
    maker = [f for f in pr.result.fills if f.liquidity == "MAKER"]
    assert maker == [], maker
    assert not any(s in ("HEDGED", "CLOSED") for s in svals(pr.result))


def risk_world_cfg():
    return cfg(end_ms=T0 + 6 * D), dict(aggregate_notional_usd=15_000.0)


def assert_risk_ceiling_held(pr):
    pol = pr.policy
    assert pr.result.stats["max_gross_usd"] <= pol["max_gross_exposure_usd"] + 1e-6, pr.result.stats["max_gross_usd"]
    assert any(any("AGGREGATE_NOTIONAL_ABOVE_CAP" in r for r in d["reasons"]) for d in pr.result.decisions)


def assert_interval_used(pr):
    bbb = [o for _ms, ops in pr.recorder.scans for o in ops if o.legs and o.legs[-1][0] == "BBBUSDT" and not o.exclusions]
    assert bbb
    for o in bbb:
        assert o.expected_funding_bps_horizon == pytest.approx(3.0 * 6 * 4)          # 3 bps per 4h print x 6 prints/day x 4 days; a 480 default would give half


# ================================================================================================ (a) happy path

def test_artifacts_are_written_in_the_control_centre_schema_and_read_back_without_error(happy):
    series, pr = happy
    assert set(MP.ARTIFACT_FILES) | {MP.SUMMARY_FILE, MP.RECONCILE_FILE} <= set(CCE.ARTIFACTS)
    for name in MP.ARTIFACT_FILES:
        assert os.path.isfile(pr.files[name]) and os.path.getsize(pr.files[name]) < CCE.MAX_ARTIFACT_BYTES
        assert art(pr, name)["data_label"] == "INJECTED_TEST_DATA" and art(pr, name)["generated_ms"] == pr.result.cfg.end_ms
    check_consistent(pr)
    st = cc_state(pr)
    assert [s["state"] for s in st["evidence"]["sources"] if s["state"] != "NO_DATA"] == ["OK"] * 8 and {s["file"] for s in st["evidence"]["sources"] if s["state"] == "NO_DATA"} <= {"universe_scope.json", "funnel.json", "testnet_session.json"}
    # basis labels: simulated results, estimated opportunities, simulated competition numbers; the file bases are inside each file's allowed set
    assert {n: art(pr, n)["basis"] for n in MP.ARTIFACT_FILES} == {"replay_report.json": "SIMULATED", "portfolio_state.json": "SIMULATED", "risk_state.json": "SIMULATED",
                                                                   "ledger_pnl.json": "SIMULATED", "competition.json": "SIMULATED", "opportunities.json": "ESTIMATED"}
    assert st["execution"]["basis"] == "SIMULATED" and st["execution"]["net"]["basis"] == "SIMULATED" and st["portfolio"]["pnl"]["kind"] == "SIMULATED_NOT_REALISED"
    assert st["portfolio"]["pnl"]["simulated_net"]["v"] is not None and st["portfolio"]["pnl"]["realised_net"]["v"] is None          # simulated is never realised
    assert st["execution"]["survivorship"]["survivorship_free"] is False and st["execution"]["universe_scope"] == "SURVIVORS_ONLY"
    # injected data is never MEASURED
    items = art(pr, "opportunities.json")["items"]
    assert items and all(i["prov"] != "MEASURED" for i in items)
    assert all(c["prov"] != "MEASURED" for c in art(pr, "competition.json")["candidates"])
    assert pr.summary["data_label"] == "INJECTED_TEST_DATA" and pr.summary["promotion_eligible"] is False


def test_the_replay_actually_trades_several_symbols_and_the_numbers_add_up(happy):
    series, pr = happy
    rr = art(pr, "replay_report.json")
    assert rr["n_groups"] >= 10 and {r["symbol"] for r in rr["symbols"]} >= {"AAAUSDT", "BBBUSDT", "CCCUSDT", "EEEUSDT"}
    assert rr["groups_by_state"].get("CLOSED", 0) >= 5
    assert rr["gross_pnl_usd"] - rr["fees_usd"] - rr["slippage_usd"] == pytest.approx(rr["net_pnl_usd"])        # gross is AT MID: net = gross - fees - slippage (taker + adverse)
    lp = art(pr, "ledger_pnl.json")
    assert lp["funding_usd"] > 0 and lp["fees_usd"] > 0
    assert lp["realised_net_usd"] + lp["unrealised_usd"] == pytest.approx(rr["net_pnl_usd"])      # ledger net == replay net
    assert pr.result.pnl["net"] == pytest.approx(rr["net_pnl_usd"])
    assert all(f.qty >= 1e-9 for f in pr.result.fills)                                      # no zero-quantity float residue was ever booked
    assert all(f.prov in ("SIMULATED", "MODELLED") for f in pr.result.fills)


def test_one_net_definition_includes_the_modelled_adverse_selection_everywhere(happy):
    series, pr = happy
    net = check_net_identity(pr)
    assert pr.result.pnl["adverse_selection"] > 0
    assert net < pr.ledger.pnl()["ledger"]["net"] - 0.99 * pr.result.pnl["adverse_selection"]      # ms_accounting's own net omits it: the displayed net must not
    note = pr.report["pnl"]["note"]
    assert "adverse" in note and "once" in note


def test_mutation_dropping_adverse_selection_from_the_ledger_net_is_caught(tmp_path, monkeypatch):
    check_net_identity(go(tmp_path, world(), name="ok"))
    real = MP.LedgerAdapter.record_slippage
    monkeypatch.setattr(MP.LedgerAdapter, "record_slippage", lambda self, usd, kind, symbol=None: real(self, 0.0 if kind == "adverse" else usd, kind, symbol))
    fails(check_net_identity, go(tmp_path, world(), name="bad"))


def test_mutation_the_report_identity_assertion_refuses_a_ledger_that_does_not_add_up(tmp_path):
    pr = go(tmp_path, world(), name="ok")
    RP.build_report([pr.result])                                                             # the positive control: it adds up
    bad = replace(pr.result, pnl={**pr.result.pnl, "net": pr.result.pnl["net"] + 5.0})
    with pytest.raises(RP.ReportIdentityError):
        RP.build_report([bad])


def test_funding_is_credited_at_each_symbols_own_settlement_times_with_the_sign_of_the_position(happy):
    series, pr = happy
    exp = check_funding(pr, series)
    held = lambda sym: len(exp[sym])                                                         # noqa: E731
    assert held("BBBUSDT") > 0 and held("AAAUSDT") > 0
    gaps4 = {b - a for a, b in zip(sorted(exp["BBBUSDT"]), sorted(exp["BBBUSDT"])[1:])}
    assert min(gaps4) == 4 * H                                                               # the 240 minute symbol is paid every 4h, the 480 minute symbols every 8h
    assert min({b - a for a, b in zip(sorted(exp["AAAUSDT"]), sorted(exp["AAAUSDT"])[1:])}) == 8 * H
    assert all(a > 0 for v in exp.values() for a in v.values())                              # shorts in positive funding RECEIVE


def test_a_long_leg_in_negative_funding_is_credited_and_a_short_in_positive_funding_too(tmp_path):
    ss = [ser("AAAUSDT", 480, 8.0), ser("BBBUSDT", 480, -4.0), ser("CCCUSDT", 240, 6.0), ser("DDDUSDT", 240, -3.0)]
    pr = go(tmp_path, ss, cfg(end_ms=T0 + 8 * D, hold_ms=3 * D), spec={"kind": "PERP_FUNDING_SPREAD", "style": "MAKER_FIRST"},
            scan_params={"price_vol_bps_per_day": {s.symbol: 150.0 for s in ss}})
    exp = check_funding(pr, ss)
    longs = [f for f in pr.result.fills if f.side == "Buy" and f.symbol in ("BBBUSDT", "DDDUSDT") and pr.result.fills_cat[f.fill_id] == "linear"]
    assert longs and all(a > 0 for v in exp.values() for a in v.values())                    # a long in NEGATIVE funding receives: the sign is by position side, both ways
    assert all(r["kind"] == "PERP_FUNDING_SPREAD" for r in art(pr, "replay_report.json")["symbols"])
    check_consistent(pr)
    st = cc_state(pr)
    assert all(s["is_carry"] is False for s in st["execution"]["symbols"])                  # a perp spread is never called carry


def test_fees_are_charged_on_every_fill_and_the_mix_is_maker_entry_taker_exit(happy):
    series, pr = happy
    check_fees(pr)
    real = [f for f in pr.result.fills if not f.fill_id.startswith("FORCED")]
    assert {f.liquidity for f in real} == {"MAKER", "TAKER"}
    perp_maker = [f for f in real if f.liquidity == "MAKER" and pr.result.fills_cat[f.fill_id] == "linear"]
    assert perp_maker and all(f.fee_usd / (f.qty * f.price) * 1e4 == pytest.approx(FILL.maker_bps_perp) for f in perp_maker)


def test_no_maker_fill_without_a_strict_trade_through(tmp_path):
    pr = go(tmp_path, touch_world(), cfg(**SHORT))
    assert_touch_is_not_a_fill(pr)
    assert pr.result.stats["transport"]["maker_orders"] > 0                                  # the order WAS resting: the touch just did not fill it
    pr2 = go(tmp_path, through_world(), cfg(**SHORT), name="through")
    perp = [f for f in pr2.result.fills if f.liquidity == "MAKER" and pr2.result.fills_cat[f.fill_id] == "linear"]
    assert perp and all(f.price == pytest.approx(100.01) for f in perp)                      # positive control: a print THROUGH 100.01 fills it, at the limit price
    assert "TRADES" in pr2.result.measured                                                   # the fills came from MEASURED trade ticks, not from bars


def test_bars_without_a_wiggle_never_fill_and_the_artifacts_say_so_honestly(tmp_path):
    ss = [ser("AAAUSDT", 480, 8.0, flat=True), ser("BBBUSDT", 240, 8.0, flat=True)]
    pr = go(tmp_path, ss, cfg(end_ms=T0 + 4 * D))
    assert pr.result.fills == []
    rr = art(pr, "replay_report.json")
    assert rr["net_pnl_usd"] == 0.0 and not any(k in ("HEDGED", "CLOSED") for k in rr["groups_by_state"])
    check_consistent(pr)


def test_perp_only_is_never_carry_and_non_trading_symbols_get_no_allocation(happy):
    series, pr = happy
    st = cc_state(pr)
    opps = {tuple(i["symbols"]): i for i in st["research"]["opportunities"]["items"]}
    d = opps[("DDDUSDT",)]
    assert d["spot_hedge"] == "PERP_ONLY" and d["is_carry"] is False and d["eligible"] is False and "NO_SPOT_HEDGE" in d["exclusions"]
    assert "PERP_ONLY" not in {r["spot_hedge"] for r in art(pr, "replay_report.json")["symbols"]}
    traded = {s for g in pr.result.groups.values() for s in g.symbols}
    assert "DDDUSDT" not in traded and "FFFUSDT" not in traded                              # PERP_ONLY (not a carry candidate) and a PreLaunch contract: never allocated
    f = opps[("FFFUSDT",)]
    assert any(e.startswith("NOT_TRADING") for e in f["exclusions"]) and f["eligible"] is False
    # EEE is Closed mid-window: nothing opened on it once it can no longer be held to the end of the hold, and nothing ever after its delivery
    deliv = T0 + 8 * D
    eee = [f for f in pr.result.fills if f.symbol == "EEEUSDT"]
    assert eee and max(f.ts_ms for f in eee) <= deliv
    entries = [g for g in pr.result.groups.values() if "EEEUSDT" in g.symbols and g.entry_ts is not None]
    assert entries and all(g.entry_ts <= deliv - 2 * cfg().step_ms for g in entries)
    assert all(s.get("is_carry", True) for s in st["execution"]["symbols"])                  # every traded symbol here IS a hedged spot-perp carry


def test_exits_close_the_perp_first_and_the_authority_authorised_every_leg(happy):
    series, pr = happy
    ast = pr.gates.stats()
    assert ast["orders_authorised"] > 30 and ast["orders_refused"] == 0 and ast["groups_refused_at_registration"] == 0, ast
    assert ast["mode"] == "SIMULATED" and ast["transport"] == "IN_PROCESS_SIMULATION"
    assert len(pr.gates.authorized) == len(set(pr.gates.authorized))                       # one authorisation per order link (resends are identical, not new)
    exits = collections.defaultdict(list)
    for link, o in pr.result.venue.orders.items():
        if link.split("-")[1].startswith("x"):
            exits[o["group_id"]].append(o)
    assert exits
    for gid, os_ in exits.items():
        cats = [o["category"] for o in os_]
        if "spot" in cats and "linear" in cats:
            assert cats.index("linear") < cats.index("spot")                                # a spot sell is an exit only once the perp it hedges is closed (authority) and a naked short perp is the worse half-state
    assert all(o["reduce_only"] for o in pr.result.venue.orders.values() if o["link_id"].split("-")[1].startswith("x") and o["category"] == "linear")


# ================================================================================================ (b) faults through the whole stack

F = RP.Fault
FAULTS = {
    "lost_ack": (F(T0, "LOST_ACK"),),
    "duplicate_fill": (F(T0, "DUPLICATE_FILL"),),
    "restart_with_open_orders": (F(T0 + 2 * H, "RESTART"), F(T0 + 3 * D, "RESTART")),
    "stale_account": (F(T0 + 1 * D, "STALE_ACCOUNT", until_ms=T0 + 2 * D),),
    "stale_book": (F(T0 + 1 * D, "STALE_BOOK", until_ms=T0 + 2 * D),),
    "risk_breach": (F(T0 + 2 * D, "RISK_BREACH"),),
    "instrument_halt": (F(T0 + 1 * D, "INSTRUMENT_HALT", symbol="CCCUSDT", until_ms=T0 + 2 * D),),
    "positions_unknown": (F(T0 + 1 * D, "POSITIONS_UNKNOWN", until_ms=T0 + 2 * D),),
}


@pytest.mark.parametrize("name", sorted(FAULTS))
def test_fault_runs_keep_ledger_lifecycle_and_artifacts_consistent(tmp_path, name):
    ss = world()
    pr = go(tmp_path, ss, cfg(end_ms=T0 + 8 * D, faults=FAULTS[name]))
    states = check_consistent(pr)
    res = pr.result
    assert res.fault_log and {f["kind"] for f in res.fault_log} == {f.kind for f in FAULTS[name]}
    assert art(pr, "risk_state.json")["alerts"] and any(a["code"].startswith("FAULT_INJECTED:") for a in art(pr, "risk_state.json")["alerts"])
    assert max(res.venue.send_calls.values()) <= 2                                           # never a blind resend storm
    check_fees(pr)
    check_funding(pr, ss)
    plans = [g for g in res.groups.values()]
    if name == "lost_ack":
        assert max(res.venue.send_calls.values()) == 1 and any(e["type"] == "ACK_LOST" for e in res.engine.journal.entries)
        assert any(s in ("CLOSED", "HEDGED") for s in states.values())
    if name == "duplicate_fill":
        assert res.stats["dup_fills_injected"] >= 1 and res.stats["fill_dups"] >= 1
        assert len({f.fill_id for f in res.fills}) == len(res.fills)                         # the ledger saw each fill once
    if name == "restart_with_open_orders":
        assert max(res.venue.send_calls.values()) == 1
        assert sum(1 for e in res.engine.journal.entries if e["type"] == "PLAN") == len(states)
    if name in ("stale_account", "stale_book"):
        lo, hi = T0 + 1 * D, T0 + 2 * D
        decided = [d for d in res.decisions if lo <= d["ts_ms"] <= hi and d["action"] == "ALLOCATE"]
        assert decided == []                                                                 # nothing was allocated while the account / the book was stale
    if name == "risk_breach":
        st = cc_state(pr)
        assert art(pr, "risk_state.json")["halted"] is True and st["risk"]["halted"] is True and any(a["source"] == "risk" for a in st["overview"]["attention"])
        assert not [g for g in plans if g.entry_ts is not None and g.entry_ts > T0 + 2 * D + 12 * H]     # no NEW exposure after the breach
        assert any(s in ("CLOSED", "HEDGED") for s in states.values())
    if name == "instrument_halt":
        plan_ts = {e["group_id"]: e["ts_ms"] for e in res.engine.journal.entries if e["type"] == "PLAN"}
        ccc = [g for g in res.groups.values() if "CCCUSDT" in g.symbols]
        assert not [g for g in ccc if plan_ts[g.group_id] > T0 + 1 * D and plan_ts[g.group_id] <= T0 + 2 * D and g.entry_ts is not None]     # nothing planned during the halt ever traded


BREACH = T0 + 2 * D


def check_allocator_respects_halts(pr):
    res = pr.result
    plan_ts = {e["group_id"]: e["ts_ms"] for e in res.engine.journal.entries if e["type"] == "PLAN"}
    assert plan_ts and not [g for g, ts in plan_ts.items() if ts >= BREACH]                  # nothing was planned once the engine was halted (the breach never lifts)
    failed_unsent = [gid for gid, st in res.states.items() if getattr(st, "value", st) == "FAILED" and not any(f.group_id == gid for f in res.fills)]
    assert failed_unsent == [], failed_unsent                                                # no wasted allocation: no group planned only to be failed by the halt
    fl = pr.report["fills"]
    assert fl["groups_planned"] == len(res.groups) == len(plan_ts)
    assert fl["groups_planned"] == fl["groups_submitted"] + fl["groups_refused_at_approval"] + fl["groups_refused_at_submit"]
    assert fl["groups_refused_at_submit"] == 0                                               # the halt was consulted BEFORE allocating, so no plan met a halted engine at submit
    assert fl["groups_refused_by_halt"] > 0 and fl["opportunities_withheld_by_halt"] > 0     # the withheld candidates are reported separately, not as planned groups
    assert any(d["action"] == "WITHHELD_BY_HALT" for d in res.decisions)


def test_the_allocator_does_not_allocate_while_the_engine_is_halted_and_the_counts_are_separate(tmp_path):
    base = go(tmp_path, world(), cfg(end_ms=T0 + 8 * D), name="base")
    fl0 = base.report["fills"]
    assert fl0["groups_refused_by_halt"] == 0 and fl0["groups_planned"] == fl0["groups_submitted"] + fl0["groups_refused_at_approval"] + fl0["groups_refused_at_submit"]       # positive control: no halt, nothing withheld
    assert fl0["groups_submitted"] > 0
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 8 * D, faults=FAULTS["risk_breach"]))
    check_allocator_respects_halts(pr)
    check_consistent(pr)


def test_mutation_an_allocator_blind_to_halts_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "halt_gate", lambda eng: (None, frozenset()))
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 8 * D, faults=FAULTS["risk_breach"]))
    fails(check_allocator_respects_halts, pr)


class RejectingExits(EX.SimVenue):
    """The venue refuses the first ``n`` exit orders it is sent (any group), then behaves."""
    def __init__(self, n=2):
        super().__init__()
        self.left = n
        self.rejected = []

    def send_order(self, req):
        if req.link_id.split("-", 1)[1].startswith("x") and self.left > 0 and req.link_id not in self.orders:
            self.left -= 1
            self.rejected.append(req.link_id)
            return EX.Ack(req.link_id, "REJECTED", None, "TEST_VENUE_REJECTS_THE_EXIT")
        return super().send_order(req)


def check_exit_retries_after_rejections_still_close(pr, venue):
    assert len(venue.rejected) == 2
    refused = [r for _l, rs in pr.gates.refusals for r in rs]
    assert not [r for r in refused if "EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY" in r], refused       # the retries were not starved of exit quantity
    gid = venue.rejected[0].split("-", 1)[0]
    assert getattr(pr.result.states[gid], "value", pr.result.states[gid]) == "CLOSED"
    net = collections.defaultdict(float)
    for f in pr.result.venue.executions:                                                       # the VENUE's executions for this group's orders, not the engine's arithmetic
        if f["link_id"].startswith(gid + "-"):
            net[(f["symbol"], f["category"])] += f["qty"] if f["side"] == "Buy" else -f["qty"]
    assert net and all(abs(v) < 1e-9 for v in net.values()), dict(net)                         # the group's positions are closed on the venue
    assert pr.gates.stats()["exit_reservations_released"] >= 2


def test_an_exit_rejected_twice_then_filled_still_closes_the_position_because_the_authority_gets_the_quantity_back(tmp_path):
    venue = RejectingExits(2)
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 8 * D), venue=venue)
    check_exit_retries_after_rejections_still_close(pr, venue)
    check_consistent(pr)


def test_mutation_never_releasing_a_rejected_exit_starves_the_retries_and_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(MAUTH.MultiSymbolAuthority, "release_exit", lambda self, link, unfilled_qty=None: False)
    venue = RejectingExits(2)
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 8 * D), venue=venue)
    fails(check_exit_retries_after_rejections_still_close, pr, venue)


def one_leg_world():
    """The perp's measured trades go THROUGH the resting sell; the spot buy never trades through its price: the perp fills, the spot leg never does."""
    ticks = tuple(FM.Tick(T0 + i * H + 1000, 100.5, 1e6) for i in range(0, 80))
    return [ser("LLLUSDT", 480, 8.0, n_days=3, flat=True, ticks=ticks)]


def test_one_leg_unfilled_becomes_unhedged_is_unwound_reduce_only_and_never_shows_as_profit(tmp_path):
    pr = go(tmp_path, one_leg_world(), cfg(**SHORT))
    res = pr.result
    hist = collections.defaultdict(list)
    for e in res.engine.journal.entries:
        if e["type"] == "STATE":
            hist[e["group_id"]].append((e["body"]["from"], e["body"]["to"]))
    went_unhedged = [g for g, h in hist.items() if any(t == "UNHEDGED" for _f, t in h)]
    assert went_unhedged, dict(hist)
    states = check_consistent(pr)
    exits = [o for o in res.venue.orders.values() if o["link_id"].split("-")[1].startswith("x")]
    assert exits and all(o["reduce_only"] and o["category"] == "linear" and o["side"] == "Buy" for o in exits)
    assert any(l.split("-")[1].startswith("x") for l in pr.gates.authorized)                # the unwind of the naked perp passed THROUGH the authority
    assert art(pr, "ledger_pnl.json")["realised_net_usd"] is not None
    comp = art(pr, "competition.json")["candidates"][0]
    assert comp["status"] in ("INSUFFICIENT_EVIDENCE", "REJECTED") and comp["reason"]
    closed = [g for g in went_unhedged if states[g] == "CLOSED"]
    assert closed                                                                              # recovery ran to the end: the naked perp was bought back
    for g in closed:                                                                           # ... and the venue's OWN read agrees the symbol is flat before the group is called closed
        assert any(e["type"] == "FLAT_VERIFIED" and e["group_id"] == g for e in res.engine.journal.entries)
    p = pr.ledger.pnl()["ledger"]
    assert p["fees"] > 0 and (p["claimable_profit"] is None or p["claimable_profit"] == p["net"])      # a figure is claimable only once flat and reconciled, and then it is the NET after fees


# ================================================================================================ (c) authority in the loop

def test_a_tampered_leg_body_is_refused_and_the_group_does_not_proceed(tmp_path, monkeypatch):
    tamper(monkeypatch)
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 3 * D))
    assert_tamper_blocked(pr)
    check_consistent(pr)
    assert pr.gates.stats()["orders_refused"] > 0 and pr.gates.stats()["orders_authorised"] == 0
    assert art(pr, "ledger_pnl.json")["fees_usd"] == 0.0 and art(pr, "replay_report.json")["net_pnl_usd"] == 0.0


def test_the_authority_refuses_a_body_outside_the_plan_a_wrong_venue_and_an_unregistered_group(happy):
    series, pr = happy
    auth = pr.gates.authority
    eng = pr.result.engine
    open_gid = next(g for g, gr in eng.groups.items() if gr.state.value == "HEDGED" and g in pr.gates._registered and g not in pr.gates._resolved)
    plan = eng.groups[open_gid].plan
    leg = next(l for l in plan.legs if l.category == "linear")
    good = MP.body_from_request(EX.OrderRequest(open_gid, CAND, DIGEST, leg, f"{open_gid}-{leg.leg_id}"))
    venue = pr.policy["venue"]

    def refused(body, v=venue):
        with pytest.raises(EA.EntryRefused) as ei:
            auth.authorize(body, venue=v)
        return ei.value.reasons

    assert any("BODY_FIELD_NOT_PLAN:qty" in r for r in refused({**good, "qty": format(float(good["qty"]) * 3, "f")}))
    assert any("BODY_FIELD_NOT_PLAN:symbol" in r for r in refused({**good, "symbol": "AAAUSDT" if good["symbol"] != "AAAUSDT" else "BBBUSDT"}))
    assert any("BODY_FIELD_NOT_PLAN:side" in r for r in refused({**good, "side": "Buy" if good["side"] == "Sell" else "Sell"}))
    assert any("IS_LEVERAGE_NOT_ALLOWED" in r for r in refused({**good, "isLeverage": 1}))
    assert refused({**good, "orderLinkId": "gdeadbeef00000000-l1"}) == ("GROUP_NOT_REGISTERED",)
    for bad_venue in ("mainnet", "eu", "demo", "Testnet", ""):
        assert any(r.startswith("VENUE_NOT_ALLOWED") for r in refused(good, bad_venue)), bad_venue


@pytest.mark.parametrize("venue,code", [("mainnet", "POLICY_VENUE_NOT_SANDBOX"), ("eu", "POLICY_VENUE_UNKNOWN_OR_UNQUALIFIED"), ("Testnet", "POLICY_VENUE_NOT_CANONICAL")])
def test_a_policy_on_a_live_or_unqualified_venue_stops_the_pipeline_before_anything_runs(tmp_path, venue, code):
    pol = MP.derive_risk_policy([s.symbol for s in world()], candidate_id=CAND, spec_digest=DIGEST, portfolio=cfg().portfolio, equity_usd=100_000.0, venue=venue)
    with pytest.raises(MP.PipelineRefused, match=code):
        go(tmp_path, world(), risk_policy=pol)
    assert not os.path.exists(tmp_path / "art" / "replay_report.json")


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p.update(allowed_symbols=["*"]), "POLICY_ALLOWED_SYMBOLS_INVALID"),
    (lambda p: p.update(allowed_symbols=["ALL"]), "POLICY_ALLOWED_SYMBOLS_INVALID"),
    (lambda p: p.update(allowed_symbols=[]), "POLICY_ALLOWED_SYMBOLS_INVALID"),
    (lambda p: p.update(wildcard=True), "POLICY_UNKNOWN_KEYS"),
    (lambda p: p.pop("max_aggregate_notional_usd"), "POLICY_MISSING_KEYS"),
    (lambda p: p.update(max_aggregate_notional_usd=float("nan")), "POLICY_LIMIT_INVALID"),
])
def test_a_wildcard_or_malformed_policy_is_refused(tmp_path, mutate, code):
    pol = MP.derive_risk_policy([s.symbol for s in world()], candidate_id=CAND, spec_digest=DIGEST, portfolio=cfg().portfolio, equity_usd=100_000.0)
    mutate(pol)
    with pytest.raises(MP.PipelineRefused, match=code):
        go(tmp_path, world(), risk_policy=pol)


def test_a_policy_that_does_not_list_a_series_symbol_is_refused_never_widened(tmp_path):
    pol = MP.derive_risk_policy(["AAAUSDT"], candidate_id=CAND, spec_digest=DIGEST, portfolio=cfg().portfolio, equity_usd=100_000.0)
    with pytest.raises(MP.PipelineRefused, match="ALLOW_LIST_LACKS"):
        go(tmp_path, world(), risk_policy=pol)


def test_the_derived_policy_is_explicit_conservative_and_closed():
    syms = ["BBBUSDT", "AAAUSDT"]
    pol = MP.derive_risk_policy(syms, candidate_id=CAND, spec_digest=DIGEST, portfolio=PF.PortfolioPolicy(style="MAKER_FIRST"), equity_usd=100_000.0)
    assert MR.policy_refusals(pol) == [] and pol["allowed_symbols"] == ["AAAUSDT", "BBBUSDT"] and pol["venue"] == "testnet"
    assert pol["max_aggregate_notional_usd"] == 50_000.0 and pol["max_aggregate_notional_usd"] < 100_000.0
    assert set(pol) == set(MR.POLICY_KEYS) | {"max_drawdown_frac"}              # the hard drawdown limit is on by default (the replay state carries equity, peak)
    with pytest.raises(MP.PipelineRefused):
        MP.derive_risk_policy([], candidate_id=CAND, spec_digest=DIGEST, portfolio=PF.PortfolioPolicy(), equity_usd=1.0)
    with pytest.raises(MP.PipelineRefused):
        MP.derive_risk_policy(["AAAUSDT", "AAAUSDT"], candidate_id=CAND, spec_digest=DIGEST, portfolio=PF.PortfolioPolicy(), equity_usd=1.0)


def test_the_authority_is_bound_to_a_simulated_transport_and_a_real_one_cannot_be_wired_in(tmp_path):
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 2 * D))
    assert pr.gates.authority.mode == "SIMULATED" and MAUTH.is_simulated_transport(pr.gates.authority.bound_transport)
    import bybit_connection as bc
    assert MAUTH.is_simulated_transport(bc.RequestsTransport()) is False                    # (constructed only to be classified: it has no session until used, and nothing is sent)
    with pytest.raises(MAUTH.AuthorityConfigError):
        MAUTH.MultiSymbolAuthority(policy=pr.policy, transport=bc.RequestsTransport(), state_dir=str(tmp_path / "x"), strategy="s", state_provider=lambda: {}, snapshot_provider=lambda: {},
                                   kill_switch_reader=lambda: (False, ""))
    src = open(os.path.join(BOT, "tools", "ms_pipeline.py"), encoding="utf-8").read()
    for banned in ("BybitClient", "RequestsTransport", "requests", "urllib", "socket", "os.environ", "time.time", "datetime.now"):
        assert banned not in src, banned


def test_a_replay_gate_that_refuses_a_plan_leaves_it_failed_and_never_submitted(tmp_path):
    class Refuse:
        def __init__(self):
            self.bound, self.orders = None, 0

        def bind(self, probe):
            self.bound = probe

        def on_plan(self, plan):
            return MR.RiskDecision(False, ("GATE_SAYS_NO",), {})

        def on_order(self, req):
            self.orders += 1
            return None

        def on_step(self, now):
            assert self.bound.now() == now and isinstance(self.bound.snapshots(), dict) and "equity_usd" in self.bound.risk_state()

    series = [replace(s, instrument=MP._instrument_for(s, cfg())) for s in world()]
    g = Refuse()
    rec = MP.ScanRecorder(SPEC, CAND, DIGEST, {"hold_days": 4.0, "opportunity_cost_apr": 0.0})
    res = RP.run_replay(series, cfg(end_ms=T0 + 3 * D), scan_fn=rec, risk_assess=MR.risk_assess,
                        risk_policy=MP.derive_risk_policy([s.symbol for s in series], candidate_id=CAND, spec_digest=DIGEST, portfolio=cfg().portfolio, equity_usd=100_000.0), gates=g)
    assert res.states and set(svals(res)) == {"FAILED"} and g.orders == 0 and res.fills == [] and res.venue.orders == {}
    assert any("GATE_SAYS_NO" in e["body"].get("reasons", [""])[0] or "GATE_SAYS_NO" in str(e) for e in res.engine.journal.entries if e["type"] == "RISK")


# ================================================================================================ (d) determinism, paper mode, argument refusals

def test_same_inputs_give_identical_artifact_bytes(tmp_path):
    a = go(tmp_path, world(), name="r1")
    b = go(tmp_path, world(), name="r2")
    assert MP.file_digests(str(tmp_path / "r1")) == MP.file_digests(str(tmp_path / "r2"))
    assert a.result.journal_head == b.result.journal_head
    c = go(tmp_path, world(), name="r3", generated_ms=T0 + 20 * D)
    d1, d3 = MP.file_digests(str(tmp_path / "r1")), MP.file_digests(str(tmp_path / "r3"))
    assert d1 != d3 and art(c, "ledger_pnl.json")["generated_ms"] == T0 + 20 * D          # only the injected generated_ms differs ...
    for n in MP.ARTIFACT_FILES:
        x, y = art(a, n), art(c, n)
        for z in (x, y):
            z.pop("generated_ms"), z.pop("identity")                                       # the stamp (and the run id derived from it) is the only thing that moves
        assert x == y                                                                       # ... and nothing else


def test_paper_mode_is_the_same_code_with_an_injected_clock_and_simulated_transport(tmp_path):
    ticks = iter(range(T0, T0 + 20 * D, H))
    p = go(tmp_path, world(), cfg(end_ms=T0 + 6 * D), name="paper", mode="PAPER", clock=lambda: next(ticks))
    r = go(tmp_path, world(), cfg(end_ms=T0 + 6 * D), name="replay")
    assert p.mode == "PAPER" and r.mode == "REPLAY" and p.summary["mode"] == "PAPER"
    for n in MP.ARTIFACT_FILES:                                                             # identical but for the identity block (its mode and run id differ by design)
        x, y = art(p, n), art(r, n)
        assert x["identity"]["mode"] == "PAPER" and y["identity"]["mode"] == "REPLAY"
        x.pop("identity"), y.pop("identity")
        assert x == y
    assert p.gates.authority.mode == "SIMULATED" and all(f.prov in ("SIMULATED", "MODELLED") for f in p.result.fills)
    check_consistent(p)


@pytest.mark.parametrize("kw,match", [
    (dict(mode="PAPER"), "INJECTED_CLOCK"), (dict(mode="LIVE"), "MODE_MUST_BE"), (dict(clock=lambda: 1), "GRID_NOT_A_CLOCK"),
    (dict(spec={"kind": "SPOT_PERP_CARRY", "style": "TAKER"}), "DOES_NOT_MATCH_PORTFOLIO_STYLE"), (dict(spec={"kind": "NOPE"}), "SPEC_KIND_OR_STYLE_INVALID"),
])
def test_bad_run_arguments_are_refused_before_anything_runs(tmp_path, kw, match):
    with pytest.raises(MP.PipelineRefused, match=match):
        go(tmp_path, world(), **kw)
    with pytest.raises(MP.PipelineRefused, match="DIGEST_MISMATCH"):
        MP.run(world(), spec=SPEC, candidate_id=CAND, spec_digest="e" * 64, replay_cfg=cfg(), artifacts_dir=str(tmp_path / "z"))
    with pytest.raises(MP.PipelineRefused, match="NO_SERIES"):
        MP.run([], spec=SPEC, candidate_id=CAND, spec_digest=DIGEST, replay_cfg=cfg(), artifacts_dir=str(tmp_path / "z"))
    with pytest.raises(MP.PipelineRefused, match="UNIVERSE_DIR_REQUIRED"):
        MP.run(str(tmp_path), spec=SPEC, candidate_id=CAND, spec_digest=DIGEST, replay_cfg=cfg(), artifacts_dir=str(tmp_path / "z"))


# ================================================================================================ (e) look-ahead

def altered_future(series, cut):
    """Same history up to ``cut``; AFTER it the funding rates are flipped and the marks jump."""
    out = []
    for s in series:
        out.append(replace(s, funding=tuple((t, r if t <= cut else -3.0 * r) for t, r in s.funding), mark=tuple((t, p if t <= cut else p * 1.3) for t, p in s.mark)))
    return out


def check_no_look_ahead(a, b, cut):
    da = [d for d in a.result.decisions if d["ts_ms"] <= cut]
    db = [d for d in b.result.decisions if d["ts_ms"] <= cut]
    assert da and da == db
    sa = [(ms, [(o.legs, o.net_bps_horizon, o.expected_funding_bps_horizon, o.exclusions) for o in ops]) for ms, ops in a.recorder.scans if ms <= cut]
    sb = [(ms, [(o.legs, o.net_bps_horizon, o.expected_funding_bps_horizon, o.exclusions) for o in ops]) for ms, ops in b.recorder.scans if ms <= cut]
    assert sa and sa == sb
    fa = [f for f in a.result.fills if f.ts_ms <= cut]
    fb = [f for f in b.result.fills if f.ts_ms <= cut]
    assert fa and fa == fb


def test_altering_the_future_never_changes_an_earlier_decision(tmp_path):
    cut = T0 + 6 * D
    base = world()
    a = go(tmp_path, base, name="a")
    b = go(tmp_path, altered_future(base, cut), name="b")
    check_no_look_ahead(a, b, cut)
    assert [d for d in a.result.decisions if d["ts_ms"] > cut + 2 * D] != [d for d in b.result.decisions if d["ts_ms"] > cut + 2 * D]       # positive control: the later decisions DO change


# ================================================================================================ (f) unknown never becomes zero

def blocked_reasons(pr, sym):
    out = set()
    for _ms, ops in pr.recorder.scans:
        for o in ops:
            if any(s in (sym, "SPOT:" + sym) for s, _ in o.legs):
                out |= set(o.exclusions)
    return out


def test_a_missing_mark_blocks_the_symbol_with_a_reason_code(tmp_path):
    ss = world()
    ss[0] = ser("AAAUSDT", 480, 5.0, mark_from_h=24 * 5)                                     # no mark at all for the first 5 days
    pr = go(tmp_path, ss, cfg(end_ms=T0 + 8 * D))
    aaa = [g for g in pr.result.groups.values() if "AAAUSDT" in g.symbols]
    assert all(g.entry_ts is None or g.entry_ts >= T0 + 5 * D for g in aaa)
    assert "NO_SNAPSHOT" in blocked_reasons(pr, "AAAUSDT")
    early = [o for ms, ops in pr.recorder.scans if ms < T0 + 5 * D for o in ops if o.legs and o.legs[-1][0] == "AAAUSDT"]
    assert early and all(o.net_bps_horizon is None and o.rank_score is None for o in early)  # unknown, not zero
    check_consistent(pr)


def test_a_gap_in_the_marks_makes_the_price_unknown_not_the_last_price(tmp_path):
    a = ser("AAAUSDT", 480, 5.0)
    gap = replace(a, mark=tuple((t, p) for t, p in a.mark if not (T0 + 3 * D < t < T0 + 7 * D)))
    pr = go(tmp_path, [gap, ser("BBBUSDT", 240, 3.0)], cfg(end_ms=T0 + 9 * D))
    assert "NO_SNAPSHOT" in blocked_reasons(pr, "AAAUSDT")
    assert not [g for g in pr.result.groups.values() if "AAAUSDT" in g.symbols and g.entry_ts is not None and T0 + 3 * D + 2 * H < g.entry_ts < T0 + 7 * D]
    check_consistent(pr)


def test_a_held_position_whose_settlement_price_is_unknown_makes_the_ledger_net_unknown_not_zero(tmp_path):
    a = ser("AAAUSDT", 480, 6.0)
    gap = replace(a, mark=tuple((t, p) for t, p in a.mark if not (T0 + 2 * D + 2 * H < t < T0 + 4 * D)))
    pr = go(tmp_path, [gap], cfg(end_ms=T0 + 6 * D))
    assert pr.result.stats.get("funding_unknown", 0) > 0
    p = pr.ledger.pnl()
    assert p["net_complete"] is False and p["ledger"]["net"] is None and any(f.startswith("FUNDING_AMOUNT_UNKNOWN") for f in p["flags"])
    rr, lp = art(pr, "replay_report.json"), art(pr, "ledger_pnl.json")
    assert rr["net_pnl_usd"] is None and lp["realised_net_usd"] is None and lp["funding_usd"] is None and lp["ledger_status"] == "UNRECONCILED"
    st = cc_state(pr)
    assert st["execution"]["state"] == "OK" and st["execution"]["net"]["v"] is None and st["execution"]["net"]["basis"] == "MISSING"
    assert st["portfolio"]["pnl"]["simulated_net"]["v"] is None and st["portfolio"]["equity"]["v"] is None
    assert any(a["code"] == "LEDGER_INCOMPLETE" for a in art(pr, "risk_state.json")["alerts"])


def test_an_unknown_funding_interval_is_never_defaulted_to_480(tmp_path):
    a = ser("AAAUSDT", 480, 5.0)
    nointerval = replace(a, interval_min=None)
    pr = go(tmp_path, [nointerval, ser("BBBUSDT", 240, 3.0)], cfg(end_ms=T0 + 6 * D))
    assert "FUNDING_INTERVAL_UNKNOWN" in blocked_reasons(pr, "AAAUSDT")
    assert not [g for g in pr.result.groups.values() if "AAAUSDT" in g.symbols]
    assert any("BBBUSDT" in g.symbols for g in pr.result.groups.values())
    rr = RP.from_symbol_series(type("S", (), dict(symbol="XXXUSDT", interval_min=None, funding=[(T0, 1.0)], mark=[(T0, 1.0)], launch_ms=0, delivery_ms=None, spot_listed=None, oi_usd=None,
                                                  funding_intervals=None, key_basis=None))())
    assert rr.interval_min is None                                                          # the adapter from the loader's series does not invent 480 either


SCANNER_NORMALISES_PER_PRINT = "FUNDING_INTERVAL_PER_PRINT_UNKNOWN" in open(os.path.join(BOT, "tools", "ms_scanner.py"), encoding="utf-8").read()
PER_PRINT = pytest.mark.skipif(not SCANNER_NORMALISES_PER_PRINT, reason="activates with the scanner's per-print interval normalisation (FUNDING_INTERVAL_PER_PRINT_UNKNOWN)")


def changing_interval_series(sym, *, only_new_segment=False, change_day=4):
    """True flat 15 bps/day funding; the interval changes 480 -> 240 at ``change_day``."""
    chg = T0 + change_day * D
    marks = tuple((T0 + i * H, 100.0 + 0.05 * math.sin(1.1 * i)) for i in range(0, 14 * 24 + 1))
    old = tuple((T0 + k * 480 * 60_000, 5.0) for k in range(1, change_day * 3 + 1))
    new = tuple((chg + k * 240 * 60_000, 2.5) for k in range(1, (14 - change_day) * 6 + 1))
    segs = ((new[0][0], new[-1][0], 240),) if only_new_segment else ((old[0][0], old[-1][0], 480), (new[0][0], new[-1][0], 240))
    return RP.ReplaySeries(sym, old + new, marks, 240, sym[:-4], funding_intervals=segs, spot_listed=True)


@PER_PRINT
def test_the_scanner_sees_a_flat_15_bps_per_day_across_an_interval_change_inside_the_trailing_window(tmp_path):
    pr = go(tmp_path, [changing_interval_series("AAAUSDT")], cfg(end_ms=T0 + 8 * D))
    at = [(ms, o) for ms, ops in pr.recorder.scans for o in ops if not o.exclusions and ms == T0 + 5 * D]          # 6 prints at 240 and 6 at 480 in the window
    assert at, [(ms, [o.exclusions for o in ops]) for ms, ops in pr.recorder.scans]
    assert at[0][1].expected_funding_bps_horizon == pytest.approx(15.0 * (4 * D / D))               # hold 4 days x 15 bps/day, whatever the mix of intervals in the window


@PER_PRINT
def test_a_symbol_whose_window_has_an_unknown_print_interval_is_excluded_and_never_allocated(tmp_path):
    pr = go(tmp_path, [changing_interval_series("AAAUSDT"), changing_interval_series("BBBUSDT", only_new_segment=True)], cfg(end_ms=T0 + 6 * D))
    unknown_scan = [o for ms, ops in pr.recorder.scans if T0 + 4 * D < ms < T0 + 6 * D for o in ops if o.legs and o.legs[-1][0].replace("SPOT:", "") == "BBBUSDT"]
    assert unknown_scan and all(o.exclusions for o in unknown_scan)
    assert not [g for g in pr.result.groups.values() if "BBBUSDT" in g.symbols and g.entry_ts is not None and g.entry_ts < T0 + 6 * D]


def test_an_unknown_account_timestamp_blocks_allocation_and_the_authority_with_reason_codes(tmp_path, monkeypatch):
    real = PF.PortfolioState.to_risk_state

    def no_account(self):
        s = real(self)
        s["account_ts_ms"] = None
        return s
    monkeypatch.setattr(PF.PortfolioState, "to_risk_state", no_account)
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 4 * D))
    assert pr.result.fills == [] and pr.result.venue.orders == {}
    reasons = {r for d in pr.result.decisions for r in d["reasons"]}
    assert "ACCOUNT_TS_UNKNOWN" in reasons and "RISK_REJECTED" in reasons
    assert not any(d["action"] == "ALLOCATE" for d in pr.result.decisions)
    check_consistent(pr)


def test_the_authority_refuses_when_the_account_state_it_reads_has_no_timestamp(tmp_path, monkeypatch):
    real = MP.AuthorityGates._state

    def no_account(self):
        s = real(self)
        s.pop("account_ts_ms")
        return s
    monkeypatch.setattr(MP.AuthorityGates, "_state", no_account)
    pr = go(tmp_path, world(), cfg(end_ms=T0 + 3 * D))
    assert pr.result.fills == [] and pr.result.venue.orders == {}
    assert pr.gates.plan_refusals and any("ACCOUNT_TS_UNKNOWN" in r for _g, rs in pr.gates.plan_refusals for r in rs)
    assert set(svals(pr.result)) == {"FAILED"}


# ================================================================================================ (g) no edge is not green

def test_zero_profitable_opportunities_show_an_honest_no_edge_state_not_green(tmp_path):
    ss = [ser("AAAUSDT", 480, 0.2), ser("BBBUSDT", 240, -1.0), ser("CCCUSDT", 480, 0.0)]
    pr = go(tmp_path, ss, cfg(end_ms=T0 + 6 * D))
    assert pr.result.states == {} and pr.result.fills == []
    st = cc_state(pr)
    op = st["research"]["opportunities"]
    assert op["state"] == "OK" and op["n_items"] == 3 and op["n_positive_net"] == 0 and op["n_eligible"] == 0 and op["est_net_usd_per_day"]["v"] is None
    assert art(pr, "opportunities.json")["est_net_usd_per_day"] is None                     # null = unknown, not a made-up zero
    comp = st["research"]["competition"]["candidates"][0]
    assert comp["status"] == "REJECTED" and comp["reason"].startswith("NO_EDGE_IN_WINDOW") and comp["n_observations"] == 0
    assert comp["net"]["v"] is None and comp["net"]["basis"] == "MISSING"
    assert st["execution"]["n_groups"] == 0 and st["execution"]["groups_by_state"] == {}
    assert st["overview"]["headline"]["status"] != "OK"
    assert not any(c["id"] == "opportunity" and c["status"] == "OK" for c in st["overview"]["cards"])
    assert art(pr, "competition.json")["basis"] == "ESTIMATED"                               # no replay round trip exists to simulate: the basis says estimated
    check_consistent(pr)


# ================================================================================================ the seams, one by one

def test_the_ledger_adapter_books_exits_on_the_entry_position_translates_spot_marks_and_declares_no_funding():
    led = MP.LedgerAdapter("ASSUMED")
    mk = lambda i, sym, side, q, px, cat, liq="TAKER": (T.Fill(i, "g1", i, sym, side, q, px, q * px * 5.5e-4, "USDT", liq, T0 + int(i[1:]) * H, "SIMULATED"), cat)  # noqa: E731
    for f, c in (mk("a1", "XXXUSDT", "Sell", 10.0, 100.0, "linear"), mk("a2", "XXXUSDT", "Buy", 10.0, 100.0, "spot")):
        led.record_fill(f, c)
    led.mark("XXXUSDT", T0 + 3 * H, 100.0)
    led.mark("SPOT:XXXUSDT", T0 + 3 * H, 100.0)
    p = led.pnl()
    assert p["net_complete"] and p["funding"] == 0.0 and p["net"] == pytest.approx(-p["fees"])         # the declared 'no print yet' is a stated zero, not an unknown
    led.record_funding("XXXUSDT", T0 + 4 * H, 1.5, "DERIVED")
    for f, c in (mk("a5", "XXXUSDT", "Buy", 10.0, 101.0, "linear"), mk("a6", "XXXUSDT", "Sell", 10.0, 101.0, "spot")):          # exits carry NEW leg ids in the engine
        led.record_fill(f, c)
    p = led.pnl()
    assert p["ledger"]["status"] == "RECONCILED" and p["ledger"]["claimable_profit"] == p["ledger"]["net"] and p["net_complete"]
    assert p["net"] == pytest.approx(0.0 + 1.5 - p["fees"])                                  # perp -10 on the way up, spot +10: the hedge nets the price leg to zero
    assert {k.split("/")[1] for k in p["ledger"]["per_leg"]} == {"linear:XXXUSDT", "spot:XXXUSDT"}
    led.record_funding("XXXUSDT", T0 + 4 * H, 1.5, "DERIVED")                                # a duplicate (symbol, ts) is ignored, not summed
    assert led.pnl()["funding"] == 1.5
    led.record_funding("XXXUSDT", T0 + 5 * H, None, "UNKNOWN")                                # an unknown amount makes net UNKNOWN (the float is only the replay's equity input)
    q = led.pnl()
    assert q["net_complete"] is False and q["ledger"]["net"] is None and isinstance(q["net"], float)
    led.record_slippage(2.0, "adverse")
    led.record_slippage(1.0, "taker")
    p2 = led.pnl()
    assert p2["slippage"] == 3.0 and p2["taker_slippage"] == 1.0 and p2["adverse_selection"] == 2.0
    assert p2["net"] == pytest.approx(q["net"] - 2.0)                                        # adverse selection is a modelled cost NOT in the fill prices: deducted once
    assert p2["price_leg"] == pytest.approx(q["price_leg"] + 1.0)                            # the price leg is AT MID: the taker walk is added back, then deducted as slippage
    assert p2["net"] == pytest.approx(p2["price_leg"] + p2["funding"] - p2["fees"] - p2["slippage"])
    with pytest.raises(ValueError):
        MP.LedgerAdapter("NOPE")


def test_a_ledger_without_any_fill_is_a_stated_zero_and_a_fee_free_fill_is_unknown_not_free():
    led = MP.LedgerAdapter()
    assert led.pnl()["net"] == 0.0 and led.pnl()["net_complete"] is True
    led.record_fill(T.Fill("z1", "g", "l", "XXXUSDT", "Sell", 1.0, 100.0, 0.0, "USDT", "MAKER", T0 + H, "SIMULATED"), "linear")
    p = led.pnl()
    assert p["net_complete"] is False and any(f.startswith("ZERO_FEE_UNVERIFIED") for f in p["flags"])      # a free fill nobody measured is not a free fill


def test_the_scan_adapter_binds_spec_candidate_digest_and_views_instruments_at_the_decision_time():
    inst = T.Instrument("XXXUSDT", None, "Trading", T0 + 2 * D, T0 + 9 * D, 480, "XXX", "USDT", "0.01", "0.01", "0.01", "100", "100", "5", "10", "XXXUSDT", "HEDGEABLE_ON_BYBIT")
    at = MP.ScanRecorder.instrument_at
    assert at(inst, T0 + 1 * D).status == "PreLaunch" and at(inst, T0 + 3 * D).status == "Trading" and at(inst, T0 + 9 * D).status == "Closed" and at(inst, T0 + 20 * D).status == "Closed"
    halted = replace(inst, status="Settling", launch_ms=None, delivery_ms=None)
    assert at(halted, T0 + 3 * D).status == "Settling"                                       # an explicit non-Trading status with no lifecycle bound stays as recorded
    rec = MP.ScanRecorder(SPEC, "c", "dd", {"hold_days": 2.0})
    s = ser("XXXUSDT", 480, 5.0, n_days=5)
    snap = T.MarketSnapshot("XXXUSDT", T0 + 3 * D, 100.0, 100.0, (T.BookLevel(99.99, 1e6),), (T.BookLevel(100.01, 1e6),), 0, 5.0, (5.0,) * 9, 480, T0 + 3 * D + 8 * H, None, None, 100.0)
    out = rec(T0 + 3 * D, {"XXXUSDT": snap}, {"XXXUSDT": replace(inst, delivery_ms=None)})
    assert out and all(o.candidate_id == "c" and o.spec_digest == "dd" for o in out) and rec.scans[0][0] == T0 + 3 * D and s.symbol == "XXXUSDT"


def test_basis_follows_the_weakest_provenance_rule():
    assert MP._basis_from_provs(["MEASURED", "VENUE_STATED", "DERIVED"]) == "CALCULATED"
    assert MP._basis_from_provs(["MEASURED", "MODELLED"]) == "ESTIMATED" and MP._basis_from_provs(["DERIVED", "ASSUMED"]) == "ESTIMATED"
    assert MP._basis_from_provs(["MEASURED", "UNKNOWN"]) == "ESTIMATED" and MP._basis_from_provs([]) == "ESTIMATED"
    assert MP._prov_cap("MEASURED", MP.DATA_INJECTED) == "ASSUMED" and MP._prov_cap("MEASURED", MP.DATA_HISTORY) == "MEASURED"
    assert MP._prov_cap("UNKNOWN", MP.DATA_INJECTED) == "UNKNOWN"


def test_the_pipeline_never_reports_sufficient_evidence_or_promotion(happy):
    series, pr = happy
    c = art(pr, "competition.json")["candidates"][0]
    assert c["status"] in ("INSUFFICIENT_EVIDENCE", "REJECTED") and c["reason"]
    assert pr.report["promotion_eligible"] is False and pr.report["promotion_blockers"]
    st = cc_state(pr)
    assert not st["research"]["sufficient_evidence"]


# ================================================================================================ the verified history-root path

def test_a_verified_history_root_goes_through_the_loader_and_is_labelled_not_injected(tmp_path, monkeypatch):
    import test_candidate_readiness as TCR
    import test_market_history as TMH
    root, uni, spot = TCR._hist(tmp_path)
    c = RP.ReplayConfig(start_ms=TMH.FIRST + 20 * TMH.H8, end_ms=TMH.FIRST + 40 * TMH.H8, fill=FILL, hold_ms=2 * D, decision_every_ms=8 * H, spec_digest=DIGEST,
                        portfolio=PF.PortfolioPolicy(max_wait_ms=3 * H, style="MAKER_FIRST"))
    import history_holdout as HH
    # This proof is about the LOADER's labelling, not the holdout (test_history_holdout.py / test_ms_pipeline_cli.py prove the guarded path): the explicit, stamped sentinel reads the whole window.
    loaded = MP.verify_inputs(root, universe_dir=uni, spot_dir=spot, mark_interval=TCR.MARK_IV, holdout_ledger=HH.UNGUARDED)       # the ONLY way a history root enters the pipeline (the CLI does the same with the registry)
    pr = MP.run(loaded, spec=SPEC, candidate_id=CAND, spec_digest=DIGEST, replay_cfg=c, artifacts_dir=str(tmp_path / "hist_art"), competition_row=PROMOTED_ROW)
    assert pr.data_label == "VERIFIED_HISTORY_ROOT" and art(pr, "replay_report.json")["data_label"] == "VERIFIED_HISTORY_ROOT"
    assert "NOT_GUARDED" in json.dumps(pr.summary["data_parts"]), f"the whole-window read must say so in the run's own identity: {pr.summary['data_parts']}"
    assert art(pr, "replay_report.json")["universe_scope"] == "DELISTINGS_OBSERVED"          # what the loader reported (its Closed partition served rows) ...
    assert cc_state(pr)["execution"]["survivorship"]["survivorship_free"] is False           # ... is still not believed without a verified universe capture beside it in the control centre
    items = {tuple(i["symbols"]): i for i in art(pr, "opportunities.json")["items"]}
    assert items[("BTCUSDT",)]["spot_hedge"] == "HEDGEABLE_ON_BYBIT" and items[("ETHUSDT",)]["spot_hedge"] == "PERP_ONLY" and items[("FASTUSDT",)]["spot_hedge"] == "HEDGE_UNKNOWN"
    assert "NO_SPOT_HEDGE" in items[("ETHUSDT",)]["exclusions"] and "HEDGE_UNKNOWN" in items[("FASTUSDT",)]["exclusions"]
    assert "INSTRUMENT_FILTERS_DEFAULTED" in pr.summary["assumed_inputs"]                    # the capture carries no tick / step / min-notional: said so, not hidden
    assert pr.policy["allowed_symbols"] == sorted(pr.policy["allowed_symbols"]) and "*" not in pr.policy["allowed_symbols"]
    check_consistent(pr)
    with pytest.raises(MP.PipelineRefused, match="HISTORY_REFUSED"):
        MP.verify_inputs(root, universe_dir=str(tmp_path / "nowhere"), spot_dir=None, mark_interval=TCR.MARK_IV, holdout_ledger=HH.UNGUARDED)
    with pytest.raises(MP.PipelineRefused, match="HOLDOUT_LEDGER_REQUIRED"):                    # a history root handed straight to run() has no registry argument: refused outright, a missing argument is never an opt-out
        MP.run(root, universe_dir=uni, spot_dir=spot, mark_interval=TCR.MARK_IV, spec=SPEC, candidate_id=CAND, spec_digest=DIGEST, replay_cfg=c, artifacts_dir=str(tmp_path / "h3"))
    with pytest.raises(MP.PipelineRefused, match="HOLDOUT_LEDGER_REQUIRED"):
        MP.verify_inputs(root, universe_dir=uni, spot_dir=spot, mark_interval=TCR.MARK_IV)


# ================================================================================================ MUTATIONS: each break must make the matching proof fail

def fails(check, *a, **k):
    with pytest.raises(AssertionError):
        check(*a, **k)


def test_mutation_bypassing_the_authority_is_caught(tmp_path, monkeypatch):
    series = world()
    tamper(monkeypatch)
    monkeypatch.setattr(MAUTH.MultiSymbolAuthority, "authorize", lambda self, body, *, venue: EA.intent_from_body(body))
    monkeypatch.setattr(MAUTH.MultiSymbolAuthority, "register_group", lambda self, plan: MR.RiskDecision(True, (), {}))
    pr = go(tmp_path, series, cfg(end_ms=T0 + 3 * D))
    fails(assert_tamper_blocked, pr)


def test_mutation_bypassing_risk_is_caught(tmp_path, monkeypatch):
    c, kw = risk_world_cfg()
    ok = go(tmp_path, world(), c, name="ok", **kw)
    assert_risk_ceiling_held(ok)                                                             # the unmutated stack holds the explicit aggregate ceiling
    monkeypatch.setattr(MR, "risk_assess", lambda plan, state, policy, snaps: MR.RiskDecision(True, (), {}))
    bad = go(tmp_path, world(), c, name="bad", **kw)
    fails(assert_risk_ceiling_held, bad)


def test_mutation_treating_a_touch_as_a_fill_is_caught(tmp_path, monkeypatch):
    assert_touch_is_not_a_fill(go(tmp_path, touch_world(), cfg(**SHORT), name="ok"))
    real = FM.advance

    def touch_is_through(o, ticks, t, params):
        step = 0.5 if o.side == "Sell" else -0.5
        return real(o, [FM.Tick(k.ts_ms, o.price + step if abs(k.price - o.price) < 1e-9 else k.price, k.qty) for k in ticks], t, params)
    monkeypatch.setattr(FM, "advance", touch_is_through)
    fails(assert_touch_is_not_a_fill, go(tmp_path, touch_world(), cfg(**SHORT), name="bad"))


def test_mutation_dropping_fees_is_caught(tmp_path, monkeypatch):
    check_fees(go(tmp_path, world(), name="ok"))
    monkeypatch.setattr(FM, "fee_bps", lambda liquidity, category, p: 0.0)
    fails(check_fees, go(tmp_path, world(), name="bad"))


def test_mutation_flipping_the_funding_sign_is_caught(tmp_path, monkeypatch):
    series = world()
    check_funding(go(tmp_path, series, name="ok"), series)
    real = MP.LedgerAdapter.record_funding
    monkeypatch.setattr(MP.LedgerAdapter, "record_funding", lambda self, symbol, ts, amount, prov: real(self, symbol, ts, None if amount is None else -amount, prov))
    fails(check_funding, go(tmp_path, series, name="bad"), series)


def test_mutation_ignoring_the_funding_interval_is_caught(tmp_path, monkeypatch):
    series = world()
    assert_interval_used(go(tmp_path, series, name="ok"))
    monkeypatch.setattr(RP.World, "interval_min", lambda self, sym, t: 480)
    monkeypatch.setattr(RP.World, "print_interval_min", lambda self, sym, ts: 480)           # the scanner now normalises each trailing print by ITS interval
    fails(assert_interval_used, go(tmp_path, series, name="bad"))


def test_mutation_reading_the_future_is_caught(tmp_path, monkeypatch):
    cut = T0 + 6 * D
    base = world()
    a = go(tmp_path, base, name="a")
    ok_b = go(tmp_path, altered_future(base, cut), name="b")
    check_no_look_ahead(a, ok_b, cut)
    real = RP.World.trailing_funding

    def peek(self, sym, t, n=12):
        i = len([1 for k, _ in self.s[sym].funding if k <= t])
        return tuple(r for _, r in self.s[sym].funding[max(0, i + 1 - n):i + 1])             # one print too many: the NEXT print
    monkeypatch.setattr(RP.World, "trailing_funding", peek)
    a2 = go(tmp_path, base, name="a2")
    b2 = go(tmp_path, altered_future(base, cut), name="b2")
    fails(check_no_look_ahead, a2, b2, cut)
    assert real is not peek


class _StubAuthority:
    """Stands in for the authority: records release calls, refuses with a fixed reason, and says whether the link was reserved before / after the call."""

    def __init__(self, reason, reserved_before, reserved_after):
        self.reason, self.reserved = reason, reserved_before
        self.after = reserved_after
        self.released = []

    def exit_reserved(self, link):
        return self.reserved

    def authorize(self, body, venue):
        self.reserved = self.after
        raise EA.EntryRefused([self.reason])

    def release_exit(self, link, unfilled_qty=None):
        self.released.append(link)
        return True


def _gates_with(stub):
    g = MP.AuthorityGates.__new__(MP.AuthorityGates)
    g.policy, g.probe, g._current_group = {"venue": "testnet"}, None, None
    g.refusals, g.authorized, g.authority = [], [], stub
    return g


def _exit_req():
    lp = T.LegPlan("x1", "BTCUSDT", "linear", "Buy", "10", "Market", None, True, False, "IOC")
    return EX.OrderRequest("G1", CAND, DIGEST, lp, "G1-x1")


@pytest.mark.parametrize("reason,before,after,released", [
    ("EXIT_LINK_CONFLICT", True, True, False),            # a conflicting resend of an already-reserved live link must not free the first order's reservation
    ("EXIT_LINK_RELEASED", False, False, False),
    ("EXIT_CUMULATIVE_QTY_EXCEEDS_ENTRY", False, False, False),   # refused before anything was reserved by this call
    ("SOME_LATER_GATE", True, True, False),               # a duplicate of an open reservation refused later: still not this call's reservation
    ("SOME_LATER_GATE", False, True, True),               # this call reserved it and a later gate refused: give it back (positive control)
])
def test_a_refused_exit_releases_only_a_reservation_this_call_made(reason, before, after, released):
    stub = _StubAuthority(reason, before, after)
    g = _gates_with(stub)
    assert g.on_order(_exit_req()) == reason
    assert stub.released == (["G1-x1"] if released else [])


def test_the_pnl_card_shows_the_replay_net_with_realised_and_unrealised_beside_it_for_a_run_with_open_legs(tmp_path):
    pr = go(tmp_path, world(), cfg(**SHORT))
    rr, lp = art(pr, "replay_report.json"), art(pr, "ledger_pnl.json")
    assert art(pr, "portfolio_state.json")["positions"], "the window must end with legs still open"
    st = cc_state(pr)
    pnl = st["portfolio"]["pnl"]
    assert lp["unrealised_usd"] not in (None, 0.0)
    assert pnl["total_net"]["v"] == pytest.approx(rr["net_pnl_usd"]) == pytest.approx(lp["net_usd"])        # ONE net: the replay / execution net
    assert pnl["simulated_net"]["v"] + pnl["unrealised"]["v"] == pytest.approx(pnl["total_net"]["v"])        # realised + unrealised == net
    assert st["execution"]["net"]["v"] == pytest.approx(pnl["total_net"]["v"])
    card = next(c for c in st["overview"]["cards"] if c["id"] == "pnl")
    assert f"${rr['net_pnl_usd']:,.2f}" in card["text"] and "realised" in card["text"] and "unrealised" in card["text"] and "not realised cash" in card["text"]
    assert card["basis"] == "SIMULATED"


def test_a_long_and_a_short_open_at_the_end_of_a_run_are_valued_at_the_exit_side_and_the_report_says_so(tmp_path):
    pr = go(tmp_path, world(), cfg(**SHORT))
    res, led = pr.result, pr.ledger
    legs = collections.defaultdict(list)
    for f in res.fills:
        legs[(f.group_id, f.symbol, res.fills_cat.get(f.fill_id, "linear"))].append(f)
    half = res.cfg.synthetic_spread_bps / 2e4
    unreal_mid = unreal_exit = 0.0
    signs = set()
    for (gid, sym, c), fs in legs.items():
        q = sum(f.qty if f.side == "Buy" else -f.qty for f in fs)
        if abs(q) < 1e-12:
            continue
        signs.add(q > 0)
        entry = [f for f in fs if (f.side == "Buy") == (q > 0)]
        avg = sum(f.qty * f.price for f in entry) / sum(f.qty for f in entry)                       # the open leg's cost basis
        m = led.marks[(sym, c)]
        px = m * (1 - half if q > 0 else 1 + half)                                                  # long -> BID, short -> ASK
        fee = abs(q) * px * (res.cfg.fill.taker_bps_perp if c == "linear" else res.cfg.fill.taker_bps_spot) / 1e4
        unreal_mid += q * (m - avg)
        unreal_exit += q * (px - avg) - fee
    assert signs == {True, False}, "a long (spot) and a short (perp) must both be open"
    assert unreal_exit < unreal_mid
    exit_side, at_mid = unreal_exit, unreal_mid
    p = led.pnl()
    assert p["unrealised"] == pytest.approx(exit_side, rel=1e-9, abs=1e-6)                           # unrealised == hand-computed exit-side value minus exit fees
    assert p["exit_adjustment"] == pytest.approx(exit_side - at_mid, rel=1e-9, abs=1e-6)
    assert p["valuation"] == "EXIT_SIDE" and "MID_FALLBACK" not in p["flags"]
    assert p["unrealised"] - p["exit_adjustment"] == pytest.approx(at_mid, rel=1e-9, abs=1e-6)       # mid-valued unrealised + (exit side - mid) == exit-side unrealised
    assert pr.report["pnl"]["valuation"] == "EXIT_SIDE"


def test_open_legs_are_valued_at_the_exit_side_of_the_book_less_the_taker_exit_fee():
    led = MP.LedgerAdapter("ASSUMED", exit_fee_bps_perp=5.5, exit_fee_bps_spot=10.0)
    now = 1_700_000_000_000
    led.record_fill(T.Fill("f1", "G", "P", "XUSDT", "Sell", 10.0, 100.0, 0.02, "USDT", "MAKER", now, "SIMULATED"), category="linear")
    led.record_fill(T.Fill("f2", "G", "S", "XUSDT", "Buy", 10.0, 100.0, 0.02, "USDT", "MAKER", now, "SIMULATED"), category="spot")
    led.mark("XUSDT", now + 1, 100.0)
    led.mark("SPOT:XUSDT", now + 1, 100.0)
    base = led.pnl()
    assert base["valuation"] == "MID_FALLBACK" and "MID_FALLBACK" in base["flags"]                  # no book: valued at the mark AND flagged, never silently mid
    assert base["unrealised"] == pytest.approx(0.0) and base["exit_adjustment"] == 0.0               # no book: valued at the mark, as before
    led.mark("XUSDT", now + 2, 100.0, bid=99.5, ask=100.5)                                           # short perp closes by BUYING at the ask
    led.mark("SPOT:XUSDT", now + 2, 100.0, bid=99.8, ask=100.2)                                      # long spot closes by SELLING at the bid
    p = led.pnl()
    perp = (100.0 - 100.5) * 10 - 10 * 100.5 * 5.5 / 1e4                                             # short loses 0.5/unit to the ask, plus the exit fee
    spot = (99.8 - 100.0) * 10 - 10 * 99.8 * 10.0 / 1e4
    assert p["unrealised"] == pytest.approx(perp + spot) and p["unrealised"] < base["unrealised"]
    assert p["net"] == pytest.approx(base["net"] + perp + spot)
    assert p["realised"] + p["unrealised"] == pytest.approx(p["net"])
    assert led.symbol_pnl("XUSDT")["net"] == pytest.approx(p["net"])


def test_every_control_centre_accounting_identity_passes_on_a_filled_run_with_slippage_and_adverse_selection(happy):
    """REPRODUCED (review, then here): the control centre's gross_minus_fees ignored slippage and adverse selection, and ledger_components added the funding the producer had already put inside
    realised_gross_usd and omitted slippage: two FAILs on a run whose numbers are correct. The consumer and the producer must share ONE net definition."""
    series, pr = happy
    assert pr.result.pnl["adverse_selection"] > 0 and art(pr, "replay_report.json")["slippage_usd"] > 0 and art(pr, "ledger_pnl.json")["funding_usd"] != 0
    acc = cc_state(pr)["accounting"]
    states = {c["id"]: c["state"] for c in acc["checks"]}
    assert "gross_minus_fees" in states and "ledger_components" in states
    assert not [k for k, v in states.items() if v == "FAIL"], states
    assert states["gross_minus_fees"] == "PASS" and states["ledger_components"] == "PASS", states
    assert acc["integrity"] == "PASS"


def test_the_ledger_gross_is_the_price_result_alone_so_funding_is_counted_once(happy):
    series, pr = happy
    lp = art(pr, "ledger_pnl.json")
    slip = lp["taker_slippage_usd"] + lp["adverse_selection_usd"]
    # realised gross = realised price result at mid; net = gross + funding - fees - modelled slippage; funding is a separate line and is never inside the gross
    assert lp["realised_gross_usd"] + lp["funding_usd"] - lp["fees_usd"] - slip == pytest.approx(lp["realised_net_usd"], rel=1e-9, abs=1e-6)


def test_the_run_summary_publishes_no_ledger_net_when_the_ledger_is_incomplete_and_labels_the_subtotal(tmp_path):
    """The summary's ``ledger.net`` is the headline number a reader quotes: with an unknown settlement price it must be None (never a partial sum), the subtotal must be labelled as such and the flags carried."""
    a = ser("AAAUSDT", 480, 6.0)
    gap = replace(a, mark=tuple((t, p) for t, p in a.mark if not (T0 + 2 * D + 2 * H < t < T0 + 4 * D)))
    pr = go(tmp_path, [gap], cfg(end_ms=T0 + 6 * D))
    assert pr.ledger.pnl()["net_complete"] is False, "control: the scenario really leaves the ledger incomplete"
    led = pr.summary["ledger"]
    assert led["net_complete"] is False and led["net"] is None, f"an incomplete ledger published a net: {led}"
    assert isinstance(led["net_subtotal"], float) and any(f.startswith("FUNDING_AMOUNT_UNKNOWN") for f in led["flags"])
    ok = go(tmp_path, [ser("AAAUSDT", 480, 6.0)], cfg(end_ms=T0 + 6 * D), name="complete")
    assert ok.ledger.pnl()["net_complete"] is True, "control: a clean run has a complete ledger"
    assert ok.summary["ledger"]["net"] == pytest.approx(ok.ledger.pnl()["net"]) and ok.summary["ledger"]["net_complete"] is True


# ------------------------------------------------------------------------------------------------ Round 20: an unstated cost of capital is UNKNOWN, never zero

def _coc_scans(pr):
    return [o for _ms, ops in pr.recorder.scans for o in ops]


def test_an_unstated_cost_of_capital_is_a_missing_owner_input_not_a_zero_net_and_nothing_trades(tmp_path):
    """REPRODUCED (Round 20): with no stated alternative return the scanner priced financing 0.0 'ASSUMED', ranked, allocated and published a positive net (competition +3.2 bps/group,
    est_net_usd_per_day $1.75). A cost the owner has not stated is unknown: no row has a net or a rank, nothing is allocated, and the status says WHY (INSUFFICIENT_OWNER_INPUT), not 'the market
    offered nothing'."""
    pr = go(tmp_path, scan_params={"opportunity_cost_apr": None}, name="unstated")
    rows = _coc_scans(pr)
    assert rows and all(o.net_bps_horizon is None and o.rank_score is None for o in rows)          # no row has a net or a rank (a row excluded earlier for another reason never reaches the financing line)
    assert any("FINANCING_UNKNOWN" in o.exclusions for o in rows), "control: a row that WAS priceable is excluded for the unknown cost, not for lack of edge"
    st = pr.summary["status"]
    assert "INSUFFICIENT_OWNER_INPUT" in st["codes"] and st["codes"].index("INSUFFICIENT_OWNER_INPUT") < st["codes"].index("NO_OPPORTUNITY")      # named before the consequence it causes
    assert st["primary"] in ("INSUFFICIENT_OWNER_INPUT", "INADEQUATE_HISTORY") and st["n_groups"] == 0 and st["n_fills"] == 0   # history is the more fundamental gap when both apply
    assert "INSUFFICIENT_OWNER_INPUT" in st["reasons"] and "--opportunity-cost-apr" in st["reasons"]["INSUFFICIENT_OWNER_INPUT"]
    assert st["positive_opportunity_scans"] == 0
    assert art(pr, "opportunities.json")["est_net_usd_per_day"] is None
    comp = art(pr, "competition.json")["candidates"][0]
    assert comp["status"] == "INSUFFICIENT_EVIDENCE" and comp["reason"].startswith("INSUFFICIENT_OWNER_INPUT") and "REJECTED" not in comp["status"]
    assert comp["net_bps_horizon"] is None and comp["net_bps_after_cost_of_capital"] is None
    assert comp["cost_of_capital"]["state"] == "UNSTATED" and comp["cost_of_capital"]["prov"] == "UNKNOWN" and comp["cost_of_capital"]["apr"] is None
    assert pr.summary["cost_of_capital"] == {"state": "UNSTATED", "apr": None, "prov": "UNKNOWN"}
    assert "COST_OF_CAPITAL_UNSTATED" in pr.summary["assumed_inputs"]
    assert pr.summary["promotion_eligible"] is False


def test_a_stated_zero_is_a_legitimate_statement_and_is_distinguishable_from_unstated(tmp_path):
    stated, unstated = go(tmp_path, scan_params={"opportunity_cost_apr": 0.0}, name="stated0"), go(tmp_path, scan_params={"opportunity_cost_apr": None}, name="unst")
    assert stated.summary["status"]["primary"] != "INSUFFICIENT_OWNER_INPUT" and stated.summary["status"]["n_groups"] > 0
    assert stated.summary["cost_of_capital"] == {"state": "STATED", "apr": 0.0, "prov": "MODELLED"}
    assert "COST_OF_CAPITAL_UNSTATED" not in stated.summary["assumed_inputs"]
    assert unstated.summary["status"]["n_groups"] == 0 and stated.summary["status"]["positive_opportunity_scans"] > unstated.summary["status"]["positive_opportunity_scans"] == 0
    comp = art(stated, "competition.json")["candidates"][0]
    assert comp["cost_of_capital"]["complete"] is True and comp["net_bps_after_cost_of_capital"] == pytest.approx(comp["net_bps_horizon"])


def test_the_realised_net_deducts_a_stated_cost_of_capital_as_its_own_component(tmp_path):
    """REPRODUCED (Round 20): the scan net carried the stated financing but the replay's REALISED net per closed group never did, so the published competition net was more optimistic than the
    scan that selected the trade by exactly the cost of capital. The component is deducted from the same stated rate on the same capital factor (the scanner's own), per group holding time."""
    apr = 0.10
    pr = go(tmp_path, scan_params={"opportunity_cost_apr": apr}, name="stated10")
    comp = art(pr, "competition.json")["candidates"][0]
    closed = [g for g in pr.result.groups.values() if g.closed and g.notional_usd > 0]
    assert closed, "control: the scenario must close a group"
    factor = MP.SC.committed_capital_factor("SPOT_PERP_CARRY", MP.SC.DEFAULT_PARAMS["margin_frac"])
    expected = [apr / 365.0 * 1e4 * ((g.exit_ts - g.entry_ts) / D) * factor for g in closed]
    assert comp["cost_of_capital"]["mean_charge_bps"] == pytest.approx(sum(expected) / len(expected))
    assert comp["cost_of_capital"]["mean_charge_bps"] > 0
    assert comp["net_bps_after_cost_of_capital"] == pytest.approx(comp["net_bps_horizon"] - comp["cost_of_capital"]["mean_charge_bps"])
    assert "BEFORE cost of capital" in comp["net_definition"]
    # the ledger identity is untouched: financing is a separate component, not a ledger line
    lp = art(pr, "ledger_pnl.json")
    assert lp["realised_gross_usd"] + lp["funding_usd"] - lp["fees_usd"] - lp["taker_slippage_usd"] - lp["adverse_selection_usd"] == pytest.approx(lp["realised_net_usd"], rel=1e-9, abs=1e-6)


def test_the_cost_of_capital_can_only_remove_opportunities_never_add_them(tmp_path):
    """Monotone: raising the stated rate can only shrink the eligible set; an unstated rate leaves none. (The pre-Round-20 default priced it as zero and so admitted the most.)"""
    counts = []
    for i, apr in enumerate((None, 0.0, 0.05, 0.30)):
        pr = go(tmp_path, scan_params={"opportunity_cost_apr": apr}, name=f"mono{i}")
        counts.append(pr.summary["status"]["positive_opportunity_scans"])
    assert counts[0] == 0 and counts[1] >= counts[2] >= counts[3], counts
