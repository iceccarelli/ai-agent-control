"""A record has to DESCRIBE the fill it is about, and a reader must not take a
record's word for it.

F5  the decision context must be for this fill (symbol, product, side, venue),
    the book must be timestamped and observed no later than the decision, not
    locked or crossed, the mark must agree with the book, and the fill must be
    something that book could have produced.
F6  the estimator re-derives a stored record's verdict and figures from its own
    fields; forged "MEASURED" lines do not make a sample, and a hostile number
    in the JSONL is a skipped line, not a crash.
F7  `--db` is attributed to an environment the database itself recorded.
F8  maker-first price improvement is not credited; the miss rate is reported.
F10 two fills that share a (ref, ms, qty) are not merged unless they are the
    same fill.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, HERE)

import execution_cost_estimate as est                        # noqa: E402
import execution_record as xr                                # noqa: E402
import execution_samples_report as esr                       # noqa: E402
import ledger as L                                           # noqa: E402
from execution_fixtures import valid_record                  # noqa: E402

T = 1_700_000_000_000
NOW = 1_800_000_000_000
DAY = est.DAY_MS


def ctx(**kw):
    base = dict(venue="bybit", symbol="BTCUSDT", product="linear", side="Sell",
                decision_ms=T, qty=0.01, mark=100_000.0, bid=99_995.0,
                ask=100_005.0, observed_ms=T - 50)
    base.update(kw)
    return xr.DecisionContext(**base)


def assess(c, **kw):
    args = dict(side="Sell", symbol="BTCUSDT", product="linear", venue="bybit",
                order_type="market", requested_qty=0.01, filled_qty=0.01,
                avg_price=99_994.0, now_ms=T + 200, submit_ms=T + 10,
                fill_ms=T + 100, fee_usd=0.5, fee_confirmed=True)
    args.update(kw)
    return xr.assess_fill(c, **args)


def test_the_baseline_is_measured():
    r = assess(ctx())
    assert r["status"] == xr.MEASURED and r["reference_basis"] == "executable_side"


# -- F5 -----------------------------------------------------------------------------

@pytest.mark.parametrize("bad,tag", [
    (dict(side="Buy"), "CONTEXT_SIDE_MISMATCH"),
    (dict(symbol="ETHUSDT"), "CONTEXT_SYMBOL_MISMATCH"),
    (dict(product="spot"), "CONTEXT_PRODUCT_MISMATCH"),
    (dict(venue="other"), "CONTEXT_VENUE_MISMATCH"),
])
def test_a_context_for_another_order_does_not_score_this_fill(bad, tag):
    r = assess(ctx(**bad))
    assert r["status"] == xr.UNAVAILABLE
    assert any(x.startswith(tag) for x in r["reasons"]), r["reasons"]
    assert r["slippage_usd"] is None and r["reference_price"] is None


def test_the_reported_ethusdt_buy_context_on_a_btc_sell_is_refused():
    """A Buy ETHUSDT context at ~$3k scoring a BTCUSDT Sell at ~$100k used to
    be MEASURED with a slippage of tens of thousands of dollars."""
    c = ctx(symbol="ETHUSDT", side="Buy", bid=2_990.0, ask=3_000.0, mark=2_995.0,
            qty=0.01)
    r = assess(c, avg_price=100_000.0)
    assert r["status"] == xr.UNAVAILABLE and r["slippage_usd"] is None


def test_a_sell_filled_far_above_the_whole_book_is_not_a_fill_of_that_book():
    r = assess(ctx(), avg_price=100_005.0 * 1.0067)          # +67 bps over the ask
    assert r["status"] == xr.UNAVAILABLE
    assert any(x.startswith("FILL_OUTSIDE_BOOK") for x in r["reasons"])
    assert r["slippage_usd"] is None


def test_a_buy_filled_far_below_the_whole_book_is_refused():
    c = ctx(side="Buy")
    r = assess(c, side="Buy", avg_price=99_995.0 * (1 - 0.0067))
    assert r["status"] == xr.UNAVAILABLE


def test_ordinary_slippage_and_modest_improvement_are_still_measured():
    for px in (99_994.0, 99_990.0, 99_900.0,          # worse than the bid
               100_005.0, 100_010.0):                  # a few bps of book move
        assert assess(ctx(), avg_price=px)["status"] == xr.MEASURED, px


def test_garbage_adverse_fills_are_refused_not_measured():
    assert assess(ctx(), avg_price=99_995.0 * (1 - 0.03))["status"] == xr.UNAVAILABLE


def test_a_book_observed_after_the_decision_is_look_ahead_even_by_a_second():
    r = assess(ctx(observed_ms=T + 1_000))
    assert r["status"] == xr.UNAVAILABLE
    assert "OBSERVATION_AFTER_DECISION" in r["reasons"]


def test_a_book_with_no_observation_time_is_not_fresh():
    r = assess(ctx(observed_ms=None))
    assert r["status"] == xr.UNAVAILABLE and "BOOK_UNTIMESTAMPED" in r["reasons"]


def test_a_bare_mark_is_still_stamped_with_the_decision_time():
    r = assess(ctx(bid=None, ask=None, observed_ms=None))
    assert r["reference_basis"] == "mark"        # not a sample, but not an error


def test_a_locked_or_crossed_book_is_refused():
    for bid, ask, tag in ((100_000.0, 100_000.0, "BOOK_LOCKED"),
                          (100_010.0, 100_000.0, "BOOK_CROSSED")):
        r = assess(ctx(bid=bid, ask=ask))
        assert r["status"] == xr.UNAVAILABLE and tag in r["reasons"]


def test_a_mark_one_percent_from_the_mid_contradicts_the_book():
    assert assess(ctx(mark=100_000.0 * 1.02))["status"] == xr.UNAVAILABLE
    assert assess(ctx(mark=100_000.0 * 1.002))["status"] == xr.MEASURED


# -- F6 -----------------------------------------------------------------------------

class E:
    def __init__(self, kind, ms, ref, rec):
        self.kind, self.ms, self.ref = kind, ms, ref
        self.fee_raw = {"execution": rec}


LEG = dict(as_of_ms=NOW, symbol="BTCUSDT", product="linear", side="Sell",
           planned_notional_usd=600.0)


def genuine(n, **kw):
    return [E("perp_open", NOW - DAY - 1000 * i, f"BB-{i}",
              valid_record(side="Sell", ms=NOW - DAY - 1000 * i, link=f"BB-{i}",
                           **kw)) for i in range(n)]


def test_genuine_records_pass_the_re_validation():
    out = est.estimate_leg(genuine(25), **LEG)
    assert out["status"] == est.MEASURED and out["rejected"] == {}


def forged(n, **over):
    """Zero slippage, a tiny spread, every flag right, nothing behind it."""
    rows = []
    for i in range(n):
        r = {"schema": 1, "status": "MEASURED", "reference_basis": "executable_side",
             "symbol": "BTCUSDT", "product": "linear", "side": "Sell",
             "order_type": "market", "filled_qty": 0.01, "requested_qty": 0.01,
             "avg_price": 60000.0, "reference_price": 60000.0,
             "slippage_usd": 0.0, "spread_cost_usd": 0.0001,
             "decision_to_submit_ms": 5, "submit_to_fill_ms": 40}
        r.update(over)
        rows.append(E("perp_open", NOW - DAY - 1000 * i, f"BB-{i}", r))
    return rows


def test_forty_forged_lines_with_zero_slippage_are_not_a_measurement():
    out = est.estimate_leg(forged(40), **LEG)
    assert out["status"] == est.UNAVAILABLE and out["n"] == 0
    assert out["rejected"] == {"RECORD_UNVERIFIABLE": 40}


def test_forged_records_with_a_book_but_figures_that_do_not_follow_are_refused():
    rows = genuine(30)
    for e in rows:
        e.fee_raw["execution"]["slippage_usd"] = 0.0           # claims no cost
    out = est.estimate_leg(rows, **LEG)
    assert out["status"] == est.UNAVAILABLE
    assert out["rejected"] == {"RECORD_INCONSISTENT": 30}


def test_a_stored_record_that_no_longer_validates_is_refused():
    rows = genuine(30)
    for e in rows:                                  # book observed AFTER the decision
        e.fee_raw["execution"]["observed_ms"] = e.fee_raw["execution"]["decision_ms"] + 5_000
    out = est.estimate_leg(rows, **LEG)
    assert out["rejected"] == {"RECORD_UNVERIFIABLE": 30}


def test_a_record_for_another_instrument_in_the_stored_fields_is_refused():
    rows = genuine(30)
    for e in rows:
        e.fee_raw["execution"]["venue"] = "bybit"
        e.fee_raw["execution"]["avg_price"] = 3_000.0     # an ETH-sized fill
    out = est.estimate_leg(rows, **LEG)
    assert out["status"] == est.UNAVAILABLE and out["n"] == 0


def test_an_infinite_time_in_a_records_line_is_a_skipped_line_not_a_crash(tmp_path):
    good = genuine(1)[0]
    path = tmp_path / "r.jsonl"
    hostile = ('{"schema": "execution_records/1", "environment": "testnet", '
               '"kind": "perp_open", "ms": Infinity, "ref": "x", '
               '"fee_raw": {"execution": {}}}')
    nan = hostile.replace("Infinity", "NaN")
    huge = hostile.replace("Infinity", "1e999")
    ok = json.dumps({"schema": "execution_records/1", "environment": "testnet",
                     "kind": "perp_open", "ms": good.ms, "ref": good.ref,
                     "fee_raw": good.fee_raw})
    path.write_text("\n".join([hostile, nan, huge, ok]) + "\n")
    fills, outcomes, skipped = esr.load_records_ex([str(path)], "testnet")
    assert len(fills) == 1 and skipped == 3 and outcomes == []
    # and the whole tool does not die on it
    rep = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=NOW, records=[str(path)],
                    environment="testnet")
    assert rep["records_skipped"] == 3


def test_a_record_with_a_non_finite_ms_never_reaches_the_estimator():
    e = genuine(1)[0]
    e.ms = float("inf")
    obs, rejected = est.extract([e], as_of_ms=NOW, symbol="BTCUSDT",
                                product="linear", side="Sell")
    assert obs == [] and rejected == {"NO_TIME": 1}


# -- F7 -----------------------------------------------------------------------------

def make_db(tmp_path, environment, n=22, name="s.db"):
    from persistence import StateStore
    path = str(tmp_path / name)
    store = StateStore(path)
    j = L.Journal()
    import time
    base = int(time.time() * 1000) - 86_400_000
    for i in range(n):
        L.perp_open(j, ms=base + 2000 * i, ref=f"BB-o-{i}", qty=0.01, price=60000.0,
                    fee_usd=0.2, fee_status="confirmed",
                    fee_raw={"execution": valid_record(
                        side="Sell", ms=base + 2000 * i, link=f"BB-o-{i}")})
        L.perp_close(j, ms=base + 2000 * i + 1000, ref=f"BB-c-{i}", qty=0.01,
                     price=60000.0, entry_price=60000.0, fee_usd=0.2,
                     fee_status="confirmed",
                     fee_raw={"execution": valid_record(
                         side="Buy", ms=base + 2000 * i + 1000,
                         link=f"BB-c-{i}")})
    store.save_ledger(j.to_rows())
    if environment:
        store.record_execution_environment(environment)
    store.close()
    return path


def args(path, env):
    return dict(mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                spot="BTCUSDT")


def test_a_testnet_database_is_labelled_and_not_counted_as_mainnet(tmp_path):
    path = make_db(tmp_path, "testnet")
    rep = esr.build(path, environment="testnet", **args(path, "testnet"))
    assert rep["status"] == "MEASURED"
    assert rep["environment"]["declared"] == "testnet"
    assert rep["environment"]["database"]["recorded"] == "testnet"
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        esr.build(path, environment="mainnet", **args(path, "mainnet"))


def test_a_database_that_never_recorded_an_environment_is_refused(tmp_path):
    path = make_db(tmp_path, None)
    with pytest.raises(ValueError, match="ENVIRONMENT_NOT_RECORDED"):
        esr.build(path, environment="testnet", **args(path, "testnet"))


def test_a_database_that_ever_booked_another_environment_is_refused(tmp_path):
    from persistence import StateStore
    path = make_db(tmp_path, "testnet")
    s = StateStore(path)
    s.record_execution_environment("mainnet")
    s.close()
    with pytest.raises(ValueError, match="ENVIRONMENT_MISMATCH"):
        esr.build(path, environment="mainnet", **args(path, "mainnet"))


def test_the_tool_refuses_a_database_with_no_environment_flag_or_a_wrong_one(tmp_path):
    import io
    from contextlib import redirect_stdout
    path = make_db(tmp_path, "testnet")
    base = ["--db", path, "--mode", "overlay", "--notional-usd", "600"]
    with pytest.raises(SystemExit):
        esr.main(base, env={})
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = esr.main(base + ["--environment", "mainnet"], env={})
    assert code == 2 and json.loads(buf.getvalue())["refused"] is True


# -- F8 -----------------------------------------------------------------------------

def test_improvement_in_some_orders_does_not_lower_the_bound():
    mixed = []
    for i in range(30):
        slip = -0.5 if i % 2 else 0.1                  # maker fills that beat the touch
        mixed.append(E("perp_open", NOW - DAY - 1000 * i, f"BB-{i}",
                       valid_record(side="Sell", slip_usd=slip, spr_usd=0.4,
                                    ms=NOW - DAY - 1000 * i, link=f"BB-{i}")))
    only_costly = [E("perp_open", NOW - DAY - 1000 * i, f"BB-{i}",
                     valid_record(side="Sell", slip_usd=0.1, spr_usd=0.4,
                                  ms=NOW - DAY - 1000 * i, link=f"BB-{i}"))
                   for i in range(30)]
    a = est.estimate_leg(mixed, **LEG)
    b = est.estimate_leg(only_costly, **LEG)
    assert a["status"] == b["status"] == est.MEASURED
    assert a["price_improved_orders"] == 15
    # the raw mean is lower for the mixed set, the bound is not below the one
    # computed with the improvements counted as zero
    assert a["slippage_bps"]["mean"] < b["slippage_bps"]["mean"]
    assert a["slippage_bps"]["bound"] >= 0.5 * b["slippage_bps"]["bound"]
    floored = est._bound([max(0.0, o.slippage_bps) for o in est.extract(
        mixed, as_of_ms=NOW, symbol="BTCUSDT", product="linear",
        side="Sell")[0]], est.DEFAULT_POLICY.z)[2]
    assert a["slippage_bps"]["bound"] >= floored - 1e-12


def test_the_report_states_the_miss_rate_per_leg(tmp_path):
    path = tmp_path / "r.jsonl"
    rows = []
    for i in range(6):
        e = genuine(1)[0]
        rows.append({"schema": "execution_records/1", "environment": "testnet",
                     "kind": "perp_open", "ms": NOW - DAY - 1000 * i,
                     "ref": f"X{i}", "fee_raw": {"execution": valid_record(
                         side="Sell", ms=NOW - DAY - 1000 * i, link=f"X{i}")}})
    for i in range(2):
        rec = xr.failed_submission(None, side="Sell", symbol="BTCUSDT",
                                   product="linear", venue="bybit",
                                   order_type="market", requested_qty=0.01,
                                   why=f"NO_ORDER: {i}", recorded_ms=NOW - DAY)
        rows.append({"schema": "execution_records/1", "environment": "testnet",
                     "kind": "execution_outcome", "ms": NOW - DAY, "ref": "",
                     "execution": rec})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=NOW, records=[str(path)],
                    environment="testnet")
    m = rep["outcomes"]["miss_rate"]["linear/Sell/market"]
    assert (m["unfilled_orders"], m["filled_orders"]) == (2, 6)
    assert m["miss_rate"] == pytest.approx(0.25)


# -- F10 ----------------------------------------------------------------------------

def test_two_orders_with_no_link_in_the_same_millisecond_are_two_refs():
    class Inner:
        VENUE = "bybit"

        def place_market(self, **kw):
            return {"filled_qty": 0.01, "avg_price": 100_000.0,
                    "fee": 0.1, "venue": "bybit"}            # no order_link_id

    j = L.Journal()
    lb = L.LedgerBroker(Inner(), j, ms=lambda: T)
    for _ in range(2):
        lb.place_market(symbol="BTCUSDT", side="Sell", qty=0.01, product="linear")
    refs = [e.ref for e in j.entries]
    assert len(set(refs)) == 2, refs


def test_two_equal_fills_that_differ_in_their_stamps_are_both_counted():
    a = valid_record(side="Sell", ms=NOW - DAY, link="K")
    b = valid_record(side="Sell", ms=NOW - DAY, link="K")
    b["submit_ms"] += 3                                  # a different send
    b["fill_ms"] += 3
    b["decision_to_submit_ms"] += 3
    ra, rb = E("perp_open", NOW - DAY, "K", a), E("perp_open", NOW - DAY, "K", b)
    obs, rejected = est.extract([ra, rb], as_of_ms=NOW, symbol="BTCUSDT",
                                product="linear", side="Sell")
    assert rejected == {} and len(obs) == 1              # one LINK: one order, merged
    assert obs[0].notional_usd == pytest.approx(2 * 0.01 * 60000.0 * 1.0, rel=1e-3)


def test_the_very_same_row_twice_is_still_dropped():
    r = E("perp_open", NOW - DAY, "K", valid_record(side="Sell", ms=NOW - DAY,
                                                     link="K"))
    obs, rejected = est.extract([r, r], as_of_ms=NOW, symbol="BTCUSDT",
                                product="linear", side="Sell")
    assert rejected == {"DUPLICATE_ROW": 1} and len(obs) == 1
