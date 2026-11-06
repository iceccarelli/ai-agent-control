"""tools/account_review.py: an offline cross-check of owner-supplied account records.

Every record in this file is SYNTHETIC: a generator turns the committed PUBLIC Bybit history (funding prints and 4h opens) into a
perfectly consistent set of "account" records, and each test then breaks exactly one thing and asserts that the tool names it. No
account, no order, no network and no runtime state is involved; every file lives in a pytest tmp directory.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import random
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import carry_teardown as CT  # noqa: E402

SETTLE = AR.SETTLE_MS
BAR = CT.BAR_MS
TS, F, PERP, _SPOT = CT.load_study()
PUB = dict(zip(TS, F))
OPEN = {t: o for t, (o, _c) in PERP.items()}
K0 = 2000


def _open_at(ms: int) -> float:
    return OPEN[ms - ms % BAR]


def make_trip(k0: int = K0, n: int = 12, *, qty: float = 0.5, b_in: float = 2.0, b_out: float = 0.5, fee_bps: float = 5.5,
              tag: str = "a", spot: bool = True, liquidity: str = "taker", ref: bool = False, lead_ms: int = 3 * 3600_000):
    """One short round trip across settlements TS[k0 .. k0+n-1]: sell before the first, buy after the last. Returns (fills, funding,
    expected) where ``expected`` is the closed-form answer computed here, independently of the tool."""
    assert TS[k0 + n - 1] - TS[k0] == (n - 1) * SETTLE
    t0, t1 = TS[k0] - lead_ms, TS[k0 + n - 1] + 3 * 3600_000
    p0, p1 = _open_at(t0), _open_at(t1)
    s0, s1 = p0 * (1 - b_in / 1e4), p1 * (1 - b_out / 1e4)
    fills = []
    for side, t, p, s, suffix in (("Sell", t0, p0, s0, "s"), ("Buy", t1, p1, s1, "b")):
        row = {"exec_id": f"{tag}-{suffix}", "ts_ms": t, "symbol": "BTCUSDT", "side": side, "qty": qty, "price": p,
               "fee_usd": qty * p * fee_bps / 1e4, "liquidity": liquidity}
        if spot:
            row["spot_ref"] = s
        if ref:
            row["ref_price"] = p * (1 - (0.0001 if side == "Buy" else -0.0001))     # both fills 1 bp WORSE than the reference
        fills.append(row)
    funding, funding_usd = [], 0.0
    for j in range(n):
        t, mark = TS[k0 + j], OPEN[TS[k0 + j]]
        rate = F[k0 + j] / 1e4
        pay = qty * mark * rate
        funding_usd += pay
        funding.append({"exec_id": f"{tag}-f{j}", "ts_ms": t, "symbol": "BTCUSDT", "position_qty": -qty, "rate": rate,
                        "payment_usd": pay})
    fees = sum(r["fee_usd"] for r in fills)
    basis = qty * (p0 - s0) - qty * (p1 - s1)
    expected = {"funding_usd": funding_usd, "fees_usd": fees, "basis_usd": basis, "net_usd": funding_usd + basis - fees,
                "notional": qty * p0, "n": n, "hold_days": (t1 - t0) / AR.DAY_MS}
    return fills, funding, expected


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(path)


def review_rows(fills, funding, tmp_path, **kw):
    fp = write_jsonl(tmp_path / "fills.jsonl", fills)
    gp = write_jsonl(tmp_path / "funding.jsonl", funding)
    return AR.run(fp, gp, **kw)


def codes(rep, severity=None):
    return sorted(f["code"] for f in rep["flags"] if severity is None or f["severity"] == severity)


def hard(rep):
    return codes(rep, AR.SEVERITY_HARD)


# --------------------------------------------------------------------------------------------------------- the clean case

def test_a_consistent_trip_reconciles_to_the_closed_form(tmp_path):
    fills, funding, e = make_trip()
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert hard(rep) == []
    t = rep["round_trips"][0]
    assert t["status"] == "COMPLETE"
    assert t["settlements_held"] == t["settlements_recorded"] == 12 and t["settlements_missing"] == 0
    assert t["funding_usd"] == pytest.approx(e["funding_usd"])
    assert t["fees_usd"] == pytest.approx(e["fees_usd"])
    assert t["basis_pnl_usd"] == pytest.approx(e["basis_usd"])
    assert t["net_usd"] == pytest.approx(e["net_usd"])
    assert t["net_bps_of_notional"] == pytest.approx(e["net_usd"] / e["notional"] * 1e4)
    assert t["hold_days"] == pytest.approx(e["hold_days"])
    assert t["annualised_pct_on_notional"] == pytest.approx(e["net_usd"] / e["notional"] * 365.0 / e["hold_days"] * 100.0)
    assert rep["economics"]["net_usd"] == pytest.approx(e["net_usd"])


def test_one_small_trip_is_consistent_but_too_small_never_verified(tmp_path):
    fills, funding, _ = make_trip()
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert rep["verdict"] == "CONSISTENT_BUT_TOO_SMALL"
    assert rep["sufficiency"]["meets_engine_floors"] is False
    assert rep["authorizes_trading"] is False and rep["changes_any_status"] is False
    assert rep["provenance"] == AR.PROVENANCE == "OWNER_SUPPLIED_UNVERIFIED"


def test_three_trips_reaching_the_engine_floors_are_consistent_not_verified(tmp_path):
    allf, allg, nets = [], [], 0.0
    for i, k in enumerate((2000, 2100, 2200)):
        f, g, e = make_trip(k, 31, tag=f"t{i}")
        allf += f
        allg += g
        nets += e["net_usd"]
    rep = review_rows(allf, allg, tmp_path, taker_bps=5.5)
    assert hard(rep) == [] and rep["verdict"] == "CONSISTENT_NOT_VERIFIED"
    assert rep["sufficiency"]["settlements_recorded_in_complete_round_trips"] == 93
    assert rep["economics"]["net_usd"] == pytest.approx(nets)
    assert rep["economics"]["interval"] is None and "NO_INTERVAL" in rep["economics"]["reason"]


def test_no_output_ever_claims_account_verification(tmp_path):
    fills, funding, _ = make_trip()
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    blob = json.dumps(rep) + AR.render(rep)
    assert "ACCOUNT_VERIFIED" not in blob and "ACCOUNT_SETTLED" not in blob
    assert AR.PROVENANCE in blob


def test_random_trips_match_the_closed_form(tmp_path):
    rng = random.Random(7)
    for i in range(40):
        n = rng.randint(2, 30)
        k0 = rng.randint(100, 4000)
        qty = rng.uniform(0.01, 3.0)
        b_in, b_out = rng.uniform(-10, 10), rng.uniform(-10, 10)
        fee = rng.uniform(0, 8)
        fills, funding, e = make_trip(k0, n, qty=qty, b_in=b_in, b_out=b_out, fee_bps=fee, tag=f"r{i}")
        d = tmp_path / f"c{i}"
        d.mkdir()
        rep = review_rows(fills, funding, d, taker_bps=fee)
        t = rep["round_trips"][0]
        assert t["status"] == "COMPLETE" and hard(rep) == []
        assert t["net_usd"] == pytest.approx(e["net_usd"], rel=1e-9, abs=1e-6)


def test_slippage_vs_reference_is_a_decomposition_never_subtracted_twice(tmp_path):
    f1, g1, _ = make_trip(ref=False)
    f2, g2, _ = make_trip(ref=True)
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    a = review_rows(f1, g1, tmp_path / "x", taker_bps=5.5)
    b = review_rows(f2, g2, tmp_path / "y", taker_bps=5.5)
    assert a["round_trips"][0]["net_usd"] == pytest.approx(b["round_trips"][0]["net_usd"])
    assert a["slippage_vs_reference"]["n"] == 0 and "UNKNOWN" in str(a["slippage_vs_reference"]["mean_adverse_bps"])
    assert b["slippage_vs_reference"]["n"] == 2 and b["slippage_vs_reference"]["mean_adverse_bps"] == pytest.approx(1.0, abs=1e-3)


def test_scale_in_and_partial_reduce_are_one_lifecycle_with_exact_basis(tmp_path):
    fills, funding, _ = make_trip(n=12, qty=1.0)
    # sell 1.0 -> sell another 0.5 mid-way -> buy 0.5 -> buy 1.0 at the end
    base = fills[0]
    mid = TS[K0 + 3] + 2 * 3600_000
    p_mid = _open_at(mid)
    extra_sell = dict(base, exec_id="x1", ts_ms=mid, qty=0.5, price=p_mid, spot_ref=p_mid * (1 - 1.0 / 1e4), fee_usd=0.5 * p_mid * 5.5 / 1e4)
    red = TS[K0 + 7] + 2 * 3600_000
    p_red = _open_at(red)
    reduce_buy = dict(fills[1], exec_id="x2", ts_ms=red, qty=0.5, price=p_red, spot_ref=p_red * (1 - 0.8 / 1e4), fee_usd=0.5 * p_red * 5.5 / 1e4)
    fills2 = [fills[0], extra_sell, reduce_buy, dict(fills[1], qty=1.0)]
    # funding rows follow the real position: -1.0 before `mid`, -1.5 until `red`, -1.0 after
    for j, row in enumerate(funding):
        t = row["ts_ms"]
        q = 1.0 if t < mid else (1.5 if t < red else 1.0)
        row["position_qty"] = -q
        row["payment_usd"] = q * OPEN[t] * row["rate"]
    rep = review_rows(fills2, funding, tmp_path, taker_bps=5.5)
    assert hard(rep) == [], rep["flags"]
    t = rep["round_trips"][0]
    assert t["fills"] == 4 and t["status"] == "COMPLETE"
    sells = [fills2[0], extra_sell]
    buys = [reduce_buy, fills2[3]]
    basis = sum(f["qty"] * (f["price"] - f["spot_ref"]) for f in sells) - sum(f["qty"] * (f["price"] - f["spot_ref"]) for f in buys)
    assert t["basis_pnl_usd"] == pytest.approx(basis)
    assert t["perp_pnl_usd"] == pytest.approx(sum(f["qty"] * f["price"] for f in sells) - sum(f["qty"] * f["price"] for f in buys))
    assert t["entry_notional_usd"] == pytest.approx(sum(f["qty"] * f["price"] for f in sells))
    assert t["funding_usd"] == pytest.approx(sum(r["payment_usd"] for r in funding))


# --------------------------------------------------------------------------------------------------------- each break is named

def test_a_missing_funding_row_is_a_named_gap_and_withholds_the_net(tmp_path):
    fills, funding, _ = make_trip()
    gone = funding.pop(5)
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "MISSING_SETTLEMENT" in codes(rep, AR.SEVERITY_GAP)
    t = rep["round_trips"][0]
    assert t["status"] == "INCOMPLETE_FUNDING" and t["settlements_missing"] == 1 and "net_usd" not in t
    assert rep["verdict"] == "INCOMPLETE" and rep["economics"]["tag"] == "UNKNOWN"
    miss = [f for f in rep["flags"] if f["code"] == "MISSING_SETTLEMENT"][0]
    assert AR._iso(gone["ts_ms"]) in miss["examples"]


def test_a_funding_row_while_flat_is_unattributed_and_rejects(tmp_path):
    fills, funding, _ = make_trip()
    stray = dict(funding[0], exec_id="stray", ts_ms=TS[K0 + 40])
    rep = review_rows(fills, funding + [stray], tmp_path, taker_bps=5.5)
    assert "UNATTRIBUTED_FUNDING" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"
    assert rep["settlements"]["funding_rows_unattributed"] == 1


def test_a_funding_row_before_the_first_fill_is_unattributed(tmp_path):
    fills, funding, _ = make_trip()
    early = dict(funding[0], exec_id="early", ts_ms=TS[K0 - 5])
    assert "UNATTRIBUTED_FUNDING" in hard(review_rows(fills, funding + [early], tmp_path, taker_bps=5.5))


def test_the_stated_position_must_equal_the_position_the_fills_imply(tmp_path):
    fills, funding, _ = make_trip()
    funding[3]["position_qty"] = -0.4
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "POSITION_MISMATCH" in hard(rep)


def test_tiny_rounding_in_the_stated_position_is_tolerated(tmp_path):
    fills, funding, _ = make_trip()
    funding[3]["position_qty"] = -0.5000001
    assert "POSITION_MISMATCH" not in hard(review_rows(fills, funding, tmp_path, taker_bps=5.5))


def test_a_rate_that_differs_from_the_public_print_is_rejected(tmp_path):
    fills, funding, _ = make_trip()
    funding[4]["rate"] += 0.0001            # +1 bp
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "RATE_DISAGREES_WITH_PUBLIC" in hard(rep)


def test_without_a_public_history_the_rate_check_is_a_named_gap_not_a_pass(tmp_path):
    fills, funding, _ = make_trip()
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5, use_default_history=False)
    assert "RATE_CHECK_NOT_RUN" in codes(rep, AR.SEVERITY_GAP) and "MARK_CHECK_NOT_RUN" in codes(rep, AR.SEVERITY_GAP)


def test_every_payment_sign_inverted_is_flagged_and_not_silently_flipped(tmp_path):
    fills, funding, e = make_trip()
    for r in funding:
        r["payment_usd"] = -r["payment_usd"]
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "SIGN_CONVENTION_INVERTED" in hard(rep) and "FUNDING_SIGN_WRONG" not in hard(rep)
    assert rep["round_trips"][0]["funding_usd"] == pytest.approx(-e["funding_usd"])


def test_one_wrong_payment_sign_is_flagged(tmp_path):
    fills, funding, _ = make_trip()
    i = next(j for j, r in enumerate(funding) if abs(r["rate"]) > 1e-9)
    funding[i]["payment_usd"] = -funding[i]["payment_usd"]
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "FUNDING_SIGN_WRONG" in hard(rep)


def test_a_payment_far_from_position_times_mark_times_rate_is_rejected(tmp_path):
    fills, funding, _ = make_trip()
    i = next(j for j, r in enumerate(funding) if abs(r["rate"]) > 1e-9)
    funding[i]["payment_usd"] *= 1.10
    assert "PAYMENT_DISAGREES_WITH_MARK" in hard(review_rows(fills, funding, tmp_path, taker_bps=5.5))


def test_a_one_percent_payment_deviation_is_inside_the_stated_tolerance(tmp_path):
    fills, funding, _ = make_trip()
    i = next(j for j, r in enumerate(funding) if abs(r["rate"]) > 1e-9)
    funding[i]["payment_usd"] *= 1.01
    assert "PAYMENT_DISAGREES_WITH_MARK" not in hard(review_rows(fills, funding, tmp_path, taker_bps=5.5))


def test_fees_must_match_the_stated_tier(tmp_path):
    fills, funding, _ = make_trip(fee_bps=5.5)
    ok = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "FEE_DISAGREES_WITH_TIER" not in hard(ok)
    (tmp_path / "w").mkdir()
    bad = review_rows(fills, funding, tmp_path / "w", taker_bps=4.0)
    assert "FEE_DISAGREES_WITH_TIER" in hard(bad)
    flagged = [f for f in bad["flags"] if f["code"] == "FEE_DISAGREES_WITH_TIER"][0]
    assert flagged["count"] == 2


def test_without_a_tier_the_fees_are_reported_not_compared(tmp_path):
    fills, funding, _ = make_trip(fee_bps=7.0)
    rep = review_rows(fills, funding, tmp_path)
    assert "FEE_TIER_NOT_STATED" in codes(rep, AR.SEVERITY_GAP) and "FEE_DISAGREES_WITH_TIER" not in hard(rep)
    assert rep["fees"]["taker"]["mean_bps"] == pytest.approx(7.0)
    assert rep["actual_vs_assumed"]["round_trip_fee_bps"]["actual_from_records"] == pytest.approx(
        rep["round_trips"][0]["fees_usd"] / rep["round_trips"][0]["entry_notional_usd"] * 1e4)


def test_maker_rebates_are_read_with_their_sign(tmp_path):
    fills, funding, _ = make_trip(fee_bps=-1.0, liquidity="maker")
    rep = review_rows(fills, funding, tmp_path, maker_bps=-1.0)
    assert hard(rep) == []
    assert rep["fees"]["maker"]["mean_bps"] == pytest.approx(-1.0) and rep["fees"]["taker"]["n"] == 0
    assert rep["round_trips"][0]["fees_usd"] < 0


def test_without_a_spot_reference_the_net_is_unknown_and_cash_only_is_marked(tmp_path):
    fills, funding, e = make_trip(spot=False)
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    t = rep["round_trips"][0]
    assert t["status"] == "NET_UNKNOWN_NO_SPOT_REFERENCE" and "net_usd" not in t and t["basis_pnl_usd"] is None
    assert t["cash_only_usd"] == pytest.approx(e["funding_usd"] - e["fees_usd"]) and "EXCLUDES basis" in t["cash_only_note"]
    assert rep["economics"]["tag"] == "UNKNOWN" and "NO_SPOT_REFERENCE" in codes(rep, AR.SEVERITY_GAP)
    assert rep["verdict"] == "INCOMPLETE"


def test_an_open_lifecycle_is_excluded_not_marked_to_market(tmp_path):
    fills, funding, _ = make_trip()
    rep = review_rows(fills[:1], funding, tmp_path, taker_bps=5.5)
    assert rep["round_trips"][0]["status"] == "EXCLUDED_OPEN" and "OPEN_LIFECYCLE" in codes(rep, AR.SEVERITY_GAP)
    assert rep["verdict"] == "INCOMPLETE"


def test_a_long_perpetual_is_not_a_carry_book_and_is_excluded(tmp_path):
    fills, funding, _ = make_trip()
    flipped = [dict(fills[0], side="Buy"), dict(fills[1], side="Sell")]
    # the funding rows below describe a long book (position +0.5, payment sign reversed)
    bad_funding = [dict(r, position_qty=0.5, payment_usd=-r["payment_usd"]) for r in funding]
    rep = review_rows(flipped, bad_funding, tmp_path, taker_bps=5.5)
    assert "LONG_EXPOSURE" in hard(rep)
    assert rep["round_trips"][0]["status"] == "EXCLUDED_LONG_EXPOSURE" and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_a_fill_within_a_minute_of_a_settlement_makes_it_ambiguous_not_missing(tmp_path):
    fills, funding, _ = make_trip(lead_ms=30_000)           # the sell lands 30 s before the first settlement
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert rep["settlements"]["ambiguous"] == 1 and rep["settlements"]["missing"] == 0
    assert {"AMBIGUOUS_SETTLEMENT", "FUNDING_ROW_IN_AMBIGUOUS_WINDOW"} <= set(codes(rep, AR.SEVERITY_GAP))
    t = rep["round_trips"][0]
    assert t["settlements_held"] == 11 and t["settlements_ambiguous"] >= 1
    assert t["funding_usd"] == pytest.approx(sum(r["payment_usd"] for r in funding[1:]))      # the ambiguous row is kept out of the totals


def test_records_beyond_the_public_history_are_a_partial_check_not_a_failure(tmp_path):
    fills, funding, _ = make_trip()
    short = {t: v for t, v in PUB.items() if t < TS[K0 + 6]}
    rep = AR.review(*AR.parse_fills(fills, "BTCUSDT")[:1], AR.parse_funding(funding, "BTCUSDT")[0], taker_bps=5.5, public=short, bar_open=OPEN)
    assert "RATE_CHECK_PARTIAL" in codes(rep, AR.SEVERITY_GAP) and "RATE_DISAGREES_WITH_PUBLIC" not in hard(rep)


# --------------------------------------------------------------------------------------------------------- statistics

def test_an_interval_appears_only_from_eight_round_trips_and_contains_the_mean(tmp_path):
    allf, allg = [], []
    rng = random.Random(3)
    for i in range(9):
        f, g, _ = make_trip(100 + 60 * i, 6, tag=f"i{i}", b_in=rng.uniform(0, 4), b_out=rng.uniform(0, 4))
        allf += f
        allg += g
    rep = review_rows(allf, allg, tmp_path, taker_bps=5.5)
    e = rep["economics"]
    assert e["complete_round_trips"] == 9 and e["interval"] is not None
    lo, hi = e["interval"]
    assert lo < e["mean_net_bps_of_notional"] < hi and "indicative" in e["reading"]


def test_capital_annualisation_exists_only_when_capital_is_stated(tmp_path):
    fills, funding, e = make_trip()
    no = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    assert "annualised_pct_on_stated_capital" not in no["round_trips"][0]
    (tmp_path / "c").mkdir()
    yes = review_rows(fills, funding, tmp_path / "c", taker_bps=5.5, capital_usd=100_000.0)
    assert yes["round_trips"][0]["annualised_pct_on_stated_capital"] == pytest.approx(e["net_usd"] / 100_000.0 * 365.0 / e["hold_days"] * 100.0)


def test_bad_review_parameters_are_refused():
    fills, funding, _ = make_trip()
    f, _ = AR.parse_fills(fills, "BTCUSDT")
    g, _ = AR.parse_funding(funding, "BTCUSDT")
    for kw in ({"taker_bps": float("nan")}, {"taker_bps": True}, {"maker_bps": 1000.0}, {"capital_usd": -1.0}, {"capital_usd": float("inf")}):
        with pytest.raises(AR.RecordError):
            AR.review(f, g, **kw)
    with pytest.raises(AR.RecordError):
        AR.review([], g)


# --------------------------------------------------------------------------------------------------------- strict parsing

def _fill(**kw):
    base = {"exec_id": "e1", "ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Sell", "qty": 0.1, "price": 60000.0,
            "fee_usd": 3.3, "liquidity": "taker"}
    base.update(kw)
    return base


def _fund(**kw):
    base = {"exec_id": "g1", "ts_ms": 1_700_000_000_000 // SETTLE * SETTLE, "symbol": "BTCUSDT", "position_qty": -0.1, "rate": 0.0001,
            "payment_usd": 0.6}
    base.update(kw)
    return base


@pytest.mark.parametrize("bad", [
    {"qty": 0}, {"qty": -1}, {"price": 0}, {"qty": float("nan")}, {"price": float("inf")}, {"qty": True}, {"qty": "abc"}, {"side": "buy"},
    {"side": ""}, {"liquidity": "both"}, {"symbol": "ETHUSDT"}, {"ts_ms": 1.5}, {"ts_ms": 1_700_000_000}, {"ts_ms": 10 ** 14},
    {"exec_id": ""}, {"fee_usd": "x"}, {"ref_price": -1.0}, {"spot_ref": 0}])
def test_malformed_fill_rows_are_errors(bad):
    with pytest.raises(AR.RecordError):
        AR.parse_fills([_fill(**bad)], "BTCUSDT")


def test_fill_duplicates_and_missing_columns_are_errors():
    with pytest.raises(AR.RecordError, match="duplicate exec_id"):
        AR.parse_fills([_fill(), _fill(ts_ms=1_700_000_001_000)], "BTCUSDT")
    row = _fill()
    del row["fee_usd"]
    with pytest.raises(AR.RecordError, match="required column"):
        AR.parse_fills([row], "BTCUSDT")


@pytest.mark.parametrize("bad", [
    {"rate": 0.5}, {"rate": 1.0}, {"rate": float("nan")}, {"payment_usd": float("inf")}, {"position_qty": "x"}, {"symbol": "SOLUSDT"},
    {"ts_ms": 1_700_000_000_000 // SETTLE * SETTLE + 3 * 60_000}, {"exec_id": " "}])
def test_malformed_funding_rows_are_errors(bad):
    with pytest.raises(AR.RecordError):
        AR.parse_funding([_fund(**bad)], "BTCUSDT")


def test_funding_snaps_to_the_grid_only_within_sixty_seconds_and_keeps_both_times():
    g0 = 1_700_000_000_000 // SETTLE * SETTLE
    rows, _ = AR.parse_funding([_fund(ts_ms=g0 + 45_000)], "BTCUSDT")
    assert rows[0].grid_ms == g0 and rows[0].ts_ms == g0 + 45_000
    rows, _ = AR.parse_funding([_fund(ts_ms=g0 - 59_000)], "BTCUSDT")
    assert rows[0].grid_ms == g0
    with pytest.raises(AR.RecordError, match="grid"):
        AR.parse_funding([_fund(ts_ms=g0 + 61_000)], "BTCUSDT")


def test_two_funding_rows_for_one_settlement_are_an_error():
    g0 = 1_700_000_000_000 // SETTLE * SETTLE
    with pytest.raises(AR.RecordError, match="second funding row"):
        AR.parse_funding([_fund(ts_ms=g0), _fund(exec_id="g2", ts_ms=g0 + 1000)], "BTCUSDT")
    with pytest.raises(AR.RecordError, match="duplicate exec_id"):
        AR.parse_funding([_fund(ts_ms=g0), _fund(ts_ms=g0 + SETTLE)], "BTCUSDT")


def test_numeric_strings_are_accepted_and_unknown_columns_are_listed_not_used():
    rows, ignored = AR.parse_fills([dict(_fill(qty="0.1", price="60000.5", fee_usd="3.3"), note="x", venue_tag="y")], "BTCUSDT")
    assert rows[0].qty == 0.1 and rows[0].price == 60000.5 and ignored == ["note", "venue_tag"]


def test_empty_optional_references_read_as_absent():
    rows, _ = AR.parse_fills([_fill(ref_price="", spot_ref=None)], "BTCUSDT")
    assert rows[0].ref_price is None and rows[0].spot_ref is None


# --------------------------------------------------------------------------------------------------------- files

def test_csv_and_jsonl_give_the_same_review(tmp_path):
    fills, funding, _ = make_trip()
    a = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    (tmp_path / "c").mkdir()
    for name, rows in (("fills.csv", fills), ("funding.csv", funding)):
        cols = sorted({k for r in rows for k in r})
        with open(tmp_path / "c" / name, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    b = AR.run(str(tmp_path / "c" / "fills.csv"), str(tmp_path / "c" / "funding.csv"), taker_bps=5.5)
    assert a["round_trips"][0]["net_usd"] == pytest.approx(b["round_trips"][0]["net_usd"])
    assert a["verdict"] == b["verdict"] and codes(a) == codes(b)
    assert a["inputs"]["sha256"]["fills"] != b["inputs"]["sha256"]["fills"]


def test_input_digests_are_the_sha256_of_the_bytes(tmp_path):
    fills, funding, _ = make_trip()
    rep = review_rows(fills, funding, tmp_path, taker_bps=5.5)
    for key, name in (("fills", "fills.jsonl"), ("funding", "funding.jsonl")):
        with open(tmp_path / name, "rb") as fh:
            assert rep["inputs"]["sha256"][key] == hashlib.sha256(fh.read()).hexdigest()
    assert {"public_funding", "perp_4h"} <= set(rep["inputs"]["sha256"])


@pytest.mark.parametrize("rel", ["trading_state.db", "trading_state.db-wal", "trading_state.db-shm", "x.sqlite", ".env", ".env.local",
                                 "testnet_session_evidence.jsonl", "id.pem", "api.key", "runs/fills.jsonl", "bot/state/fills.jsonl"])
def test_protected_paths_are_refused_even_when_they_hold_valid_records(tmp_path, rel):
    fills, funding, _ = make_trip()
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(p, fills)
    ok = write_jsonl(tmp_path / "funding.jsonl", funding)
    for a, b in ((str(p), ok), (ok, str(p))):
        with pytest.raises(AR.RecordError, match="refusing to read"):
            AR.run(a, b)


def test_only_known_text_formats_and_real_files_are_read(tmp_path):
    for name in ("x.json", "x.txt", "x.gz"):
        (tmp_path / name).write_text("{}")
        with pytest.raises(AR.RecordError, match="only .jsonl"):
            AR.read_rows(str(tmp_path / name))
    with pytest.raises(AR.RecordError, match="not a file"):
        AR.read_rows(str(tmp_path / "missing.jsonl"))
    (tmp_path / "e.jsonl").write_text("\n\n")
    with pytest.raises(AR.RecordError, match="no records"):
        AR.read_rows(str(tmp_path / "e.jsonl"))
    (tmp_path / "b.jsonl").write_text("{not json}\n")
    with pytest.raises(AR.RecordError, match="not JSON"):
        AR.read_rows(str(tmp_path / "b.jsonl"))
    (tmp_path / "l.jsonl").write_text("[1,2]\n")
    with pytest.raises(AR.RecordError, match="not an object"):
        AR.read_rows(str(tmp_path / "l.jsonl"))
    (tmp_path / "u.jsonl").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(AR.RecordError, match="UTF-8"):
        AR.read_rows(str(tmp_path / "u.jsonl"))


def _tree_digest(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_a_review_writes_nothing_anywhere_near_its_inputs(tmp_path):
    fills, funding, _ = make_trip()
    write_jsonl(tmp_path / "fills.jsonl", fills)
    write_jsonl(tmp_path / "funding.jsonl", funding)
    before = _tree_digest(tmp_path)
    AR.run(str(tmp_path / "fills.jsonl"), str(tmp_path / "funding.jsonl"), taker_bps=5.5)
    AR.main(["--fills", str(tmp_path / "fills.jsonl"), "--funding", str(tmp_path / "funding.jsonl"), "--json"])
    assert _tree_digest(tmp_path) == before


def test_module_imports_nothing_that_can_reach_a_network_a_database_or_a_process():
    import ast
    tree = ast.parse(open(AR.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "bisect", "csv", "datetime", "hashlib", "json", "math", "os", "re", "sys", "dataclasses", "typing",
               "statistical_power", "carry_teardown"}
    assert imported <= allowed, sorted(imported - allowed)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    plain = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute)}
    assert not (plain & {"exec", "eval", "compile", "__import__"}), plain
    assert not (attrs & {"system", "popen", "connect", "urlopen", "unlink", "rmtree", "rename", "write", "writelines", "mkdir", "makedirs",
                         "chmod", "listdir", "walk", "scandir"}), attrs
    # no direct os.<function>() call at all: only os.path.* helpers and os.sep are used
    assert not [n for n in calls if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"]
    # nothing here opens a file for writing
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            mode = n.args[1] if len(n.args) > 1 else None
            assert mode is None or (isinstance(mode, ast.Constant) and mode.value in ("rb", "r")), ast.dump(n)[:200]


# --------------------------------------------------------------------------------------------------------- CLI

def test_cli_text_and_json_and_errors(tmp_path, capsys):
    fills, funding, _ = make_trip()
    fp, gp = write_jsonl(tmp_path / "fills.jsonl", fills), write_jsonl(tmp_path / "funding.jsonl", funding)
    assert AR.main(["--fills", fp, "--funding", gp, "--taker-bps", "5.5"]) == 0
    text = capsys.readouterr().out
    assert "OWNER_SUPPLIED_UNVERIFIED" in text and "authorizes trading: NO" in text and "WHAT WOULD UPGRADE THIS" in text
    assert AR.main(["--fills", fp, "--funding", gp, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["authorizes_trading"] is False and doc["provenance"] == "OWNER_SUPPLIED_UNVERIFIED"
    assert AR.main(["--fills", str(tmp_path / "nope.jsonl"), "--funding", gp]) == 2
    assert "account_review:" in capsys.readouterr().err
    bad = write_jsonl(tmp_path / "bad.jsonl", [_fill(qty=-1)])
    assert AR.main(["--fills", bad, "--funding", gp]) == 2


def test_cli_refuses_a_protected_path(tmp_path, capsys):
    fills, funding, _ = make_trip()
    db = tmp_path / "trading_state.db"
    write_jsonl(db, fills)
    gp = write_jsonl(tmp_path / "funding.jsonl", funding)
    assert AR.main(["--fills", str(db), "--funding", gp]) == 2
    assert "refusing to read" in capsys.readouterr().err


def test_render_lists_every_flag_and_the_upgrade_path(tmp_path):
    fills, funding, _ = make_trip()
    funding.pop(2)
    rep = review_rows(fills, funding, tmp_path)
    text = AR.render(rep)
    for f in rep["flags"]:
        assert f["code"] in text
    assert all(u in text for u in rep["what_would_upgrade_this"])
    assert math.isfinite(rep["round_trips"][0]["fee_bps_of_notional"])
