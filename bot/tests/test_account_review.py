"""tools/account_review.py: an offline cross-check of owner-supplied account records.

Every record in this file is SYNTHETIC: a generator turns the committed PUBLIC Bybit history (funding prints and 4h opens) into a
perfectly consistent set of "account" records, and each test then breaks exactly one thing and asserts that the tool names it. No
account, no order, no network and no runtime state is involved; every file lives in a pytest tmp directory.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
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


_REAL_READ_HISTORY = AR._read_history


@pytest.fixture(autouse=True)
def _read_the_committed_history_once(monkeypatch):
    """The three committed public histories are the same on every call; parse them once per session, not once per test."""
    real = _REAL_READ_HISTORY

    def cached(path, default_name, use_default):
        if path is None and use_default:
            key = default_name
            if key not in _HISTORY:
                _HISTORY[key] = real(path, default_name, use_default)
            return _HISTORY[key]
        return real(path, default_name, use_default)
    monkeypatch.setattr(AR, "_read_history", cached)


_HISTORY = {}


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
    assert rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"


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


def test_a_payment_within_the_half_percent_tolerance_passes_and_one_percent_does_not(tmp_path):
    fills, funding, _ = make_trip()
    i = next(j for j, r in enumerate(funding) if abs(r["rate"]) > 1e-9)
    funding[i]["payment_usd"] *= 1.003
    assert "PAYMENT_DISAGREES_WITH_MARK" not in hard(review_rows(fills, funding, tmp_path, taker_bps=5.5))
    (tmp_path / "x").mkdir()
    funding[i]["payment_usd"] *= 1.01 / 1.003
    assert "PAYMENT_DISAGREES_WITH_MARK" in hard(review_rows(fills, funding, tmp_path / "x", taker_bps=5.5))      # 1 % is outside 0.5 %


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
    assert rep["verdict"] == "INCOMPLETE"                       # ... and a review that had to leave a settlement out says it is not complete


def test_records_beyond_the_public_history_are_a_partial_check_not_a_failure(tmp_path):
    fills, funding, _ = make_trip()
    short = {t: v for t, v in PUB.items() if t < TS[K0 + 6]}
    rep = AR.review(*AR.parse_fills(fills, "BTCUSDT")[:1], AR.parse_funding(funding, "BTCUSDT")[0], taker_bps=5.5, public=short, bar_open=OPEN)
    assert "RATE_CHECK_PARTIAL" in codes(rep, AR.SEVERITY_GAP) and "RATE_DISAGREES_WITH_PUBLIC" not in hard(rep)
    assert rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"          # a partial check is never reported as a pass


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
    allowed = {"__future__", "argparse", "bisect", "csv", "datetime", "hashlib", "io", "json", "math", "os", "re", "stat", "sys", "zlib",
               "dataclasses", "typing", "statistical_power", "carry_teardown"}
    assert imported <= allowed, sorted(imported - allowed)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    plain = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute)}
    assert not (plain & {"exec", "eval", "compile", "__import__"}), plain
    assert not (attrs & {"system", "popen", "connect", "urlopen", "unlink", "rmtree", "rename", "write", "writelines", "mkdir", "makedirs",
                         "chmod", "listdir", "walk", "scandir"}), attrs
    # no direct os.<function>() call except os.stat (the hard-link test): otherwise only os.path.* helpers and os.sep are used
    direct = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"}
    assert direct <= {"stat", "fstat"}, direct
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


# ================================================================================================================================
# The independent review's findings, each reproduced as a failing scenario before the fix (HIGH 1-2, MEDIUM 3-6, LOW 8-13)
# ================================================================================================================================

def shifted(fills, funding, n_settlements):
    """The same records moved n settlements later in time (their prices and rates are kept): used to put perfectly consistent records
    OUTSIDE the committed public history, which ends 2026-09-09."""
    d = n_settlements * SETTLE
    return ([dict(r, ts_ms=r["ts_ms"] + d) for r in fills], [dict(r, ts_ms=r["ts_ms"] + d) for r in funding])


def three_trips(shift=0):
    allf, allg = [], []
    for i, k in enumerate((2000, 2100, 2200)):
        f, g, _ = make_trip(k, 31, tag=f"t{i}")
        allf += f
        allg += g
    return shifted(allf, allg, shift) if shift else (allf, allg)


def test_FINDING_1_records_beyond_the_public_history_are_never_called_consistent(tmp_path):
    f, g = three_trips(shift=3000)                     # 2022-06 + 3000 x 8h puts them after the committed history's last print
    assert f[0]["ts_ms"] > TS[-1]
    # invent 3 bp rates (three times the largest print of the last year) and pay accordingly: nothing public can contradict them
    for r in g:
        r["rate"] = 0.0003
        r["payment_usd"] = 0.5 * 90000.0 * 0.0003
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"
    assert {"RATE_CHECK_PARTIAL", "MARK_CHECK_PARTIAL", "PRICE_CHECK_PARTIAL"} <= set(codes(rep, AR.SEVERITY_GAP))
    assert rep["sufficiency"]["meets_engine_floors"] is True            # the floors alone must not be what makes it 'consistent'


def test_FINDING_1_with_no_history_and_no_tier_nothing_is_compared_and_the_verdict_says_so(tmp_path):
    f, g = three_trips()
    rep = review_rows(f, g, tmp_path, use_default_history=False)
    assert rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"
    assert {"RATE_CHECK_NOT_RUN", "MARK_CHECK_NOT_RUN", "PRICE_CHECK_NOT_RUN", "SPOT_REF_CHECK_NOT_RUN", "FEE_TIER_NOT_STATED"} <= set(codes(rep))


def test_FINDING_1_another_symbol_gets_no_default_history_and_so_no_clean_verdict(tmp_path):
    f, g = three_trips()
    for r in f + g:
        r["symbol"] = "ETHUSDT"
    rep = review_rows(f, g, tmp_path, symbol="ETHUSDT", taker_bps=5.5)
    assert rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC" and "RATE_CHECK_NOT_RUN" in codes(rep)


def test_the_fully_checked_clean_case_is_the_only_way_to_CONSISTENT_NOT_VERIFIED(tmp_path):
    f, g = three_trips()
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["verdict"] == "CONSISTENT_NOT_VERIFIED" and [c for c in codes(rep, AR.SEVERITY_GAP)] == []


def _first_negative_settlement(k_lo=100, k_hi=1500, span=12):
    """A window of `span` settlements that contains a negative print (so dropping it flatters the book)."""
    for k in range(k_lo, k_hi):
        if min(F[k:k + span]) < -0.5 and TS[k + span - 1] - TS[k] == (span - 1) * SETTLE:
            return k, next(j for j in range(span) if F[k + j] < -0.5)
    raise AssertionError("no negative window")


def test_FINDING_2_a_tiny_trade_pair_near_a_settlement_does_not_hide_it_when_the_book_is_short_throughout(tmp_path):
    k, j = _first_negative_settlement()
    f, g, _ = make_trip(k, 12, tag="n")
    t = TS[k + j]
    p = _open_at(t)
    pair = [{"exec_id": "x-b", "ts_ms": t - 30_000, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.001, "price": p, "fee_usd": 0.001 * p * 5.5 / 1e4,
             "liquidity": "taker", "spot_ref": p},
            {"exec_id": "x-s", "ts_ms": t - 20_000, "symbol": "BTCUSDT", "side": "Sell", "qty": 0.001, "price": p, "fee_usd": 0.001 * p * 5.5 / 1e4,
             "liquidity": "taker", "spot_ref": p}]
    honest = review_rows(f + pair, g, tmp_path, taker_bps=5.5)
    assert hard(honest) == [] and honest["settlements"]["missing"] == 0 and honest["settlements"]["ambiguous"] == 0
    dropped = [r for i, r in enumerate(g) if i != j]                  # remove the negative settlement's row
    (tmp_path / "d").mkdir()
    rep = review_rows(f + pair, dropped, tmp_path / "d", taker_bps=5.5)
    assert rep["settlements"]["missing"] == 1 and "MISSING_SETTLEMENT" in codes(rep, AR.SEVERITY_GAP)
    assert rep["verdict"] == "INCOMPLETE" and rep["round_trips"][0]["status"] == "INCOMPLETE_FUNDING"


def test_FINDING_2_a_real_open_near_a_settlement_is_still_ambiguous_and_makes_the_review_incomplete(tmp_path):
    f, g, _ = make_trip(lead_ms=30_000)
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["settlements"]["ambiguous"] == 1 and rep["verdict"] == "INCOMPLETE"


def test_the_stated_position_may_be_the_one_before_or_after_a_rebalance_inside_the_minute(tmp_path):
    f, g, _ = make_trip(n=6)
    t = TS[K0 + 2]
    p = _open_at(t)

    def leg(eid, side, dt):
        return {"exec_id": eid, "ts_ms": t + dt, "symbol": "BTCUSDT", "side": side, "qty": 0.1, "price": p, "fee_usd": 0.1 * p * 5.5 / 1e4,
                "liquidity": "taker", "spot_ref": p}
    pair = [leg("r-1", "Sell", -15_000), leg("r-2", "Buy", 15_000)]            # short 0.5 -> 0.6 -> 0.5 across the settlement
    for stated in (-0.5, -0.6):
        gg = [dict(r) for r in g]
        gg[2]["position_qty"] = stated
        gg[2]["payment_usd"] = -stated * OPEN[t] * gg[2]["rate"]
        d = tmp_path / str(stated).replace("-", "m").replace(".", "_")
        d.mkdir()
        rep = review_rows(f + pair, gg, d, taker_bps=5.5)
        assert "POSITION_MISMATCH" not in hard(rep), stated
    gg = [dict(r) for r in g]
    gg[2]["position_qty"] = -0.9
    (tmp_path / "bad").mkdir()
    assert "POSITION_MISMATCH" in hard(review_rows(f + pair, gg, tmp_path / "bad", taker_bps=5.5))


def test_FINDING_3_a_spot_reference_far_from_the_fill_is_rejected(tmp_path):
    for factor in (0.5, 0.95, 1.05, 2.0):
        f, g, _ = make_trip()
        for r in f:
            r["spot_ref"] = r["price"] * factor
        d = tmp_path / str(factor).replace(".", "_")
        d.mkdir()
        rep = review_rows(f, g, d, taker_bps=5.5)
        assert "SPOT_REF_IMPLAUSIBLE" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT", factor


def test_FINDING_3_a_spot_reference_inside_the_deviation_cap_but_far_from_the_public_basis_is_rejected(tmp_path):
    f, g, _ = make_trip()
    for r in f:
        r["spot_ref"] = r["price"] / 1.004                         # a basis of +40 bps on every fill: under the 0.5 % cap, far above any 4h basis
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "BASIS_OFF_PUBLIC_BAR" in hard(rep) and "SPOT_REF_IMPLAUSIBLE" not in hard(rep)
    assert rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_FINDING_3_a_fill_price_far_from_the_public_bar_is_rejected(tmp_path):
    f, g, _ = make_trip()
    f[0]["price"] *= 1.3
    f[0]["spot_ref"] = f[0]["price"]
    f[0]["fee_usd"] = f[0]["qty"] * f[0]["price"] * 5.5 / 1e4
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "FILL_PRICE_OUTSIDE_PUBLIC_RANGE" in hard(rep)


def test_FINDING_3_fabricated_trips_that_span_no_settlement_are_not_carry_round_trips_and_do_not_pad_the_counts(tmp_path):
    real_f, real_g, e = make_trip(2000, 95, tag="real")
    fakes = []
    for i in range(2):
        t0 = TS[K0 + 200 + 10 * i] + 3600_000
        p = _open_at(t0)
        for side, dt, suffix, ps in (("Sell", 0, "s", p), ("Buy", 600_000, "b", p * (1 - 0.0005))):      # a 5 bp 'profit' in ten minutes
            fakes.append({"exec_id": f"fake{i}-{suffix}", "ts_ms": t0 + dt, "symbol": "BTCUSDT", "side": side, "qty": 0.5, "price": ps,
                          "fee_usd": 0.5 * ps * 5.5 / 1e4, "liquidity": "taker", "spot_ref": p})
    rep = review_rows(real_f + fakes, real_g, tmp_path, taker_bps=5.5)
    stat = [t["status"] for t in rep["round_trips"]]
    assert stat.count("COMPLETE") == 1 and stat.count("EXCLUDED_NO_SETTLEMENT_SPANNED") == 2
    assert rep["sufficiency"]["complete_round_trips"] == 1 and rep["sufficiency"]["meets_engine_floors"] is False
    assert hard(rep) == [], rep["flags"]
    assert "ROUND_TRIP_WITHOUT_SETTLEMENT" in codes(rep, AR.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    fake_rows = [t for t in rep["round_trips"] if t["status"] == "EXCLUDED_NO_SETTLEMENT_SPANNED"]
    assert all("annualised_pct_on_notional" not in t and "net_usd" not in t for t in fake_rows)      # their costs are shown, no rate is invented


def test_FINDING_3_a_trip_under_a_day_is_not_annualised(tmp_path):
    f, g, _ = make_trip(n=1)
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    t = rep["round_trips"][0]
    assert t["status"] == "COMPLETE" and t["annualised_pct_on_notional"] is None and "not annualised" in t["annualisation_note"]


def test_FINDING_4_dropping_the_spot_reference_from_the_losing_trip_cannot_improve_the_verdict(tmp_path):
    allf, allg = [], []
    for i, k in enumerate((2000, 2100, 2200)):
        f, g, _ = make_trip(k, 31, tag=f"t{i}", b_in=3.0, b_out=22.0 if i == 1 else 3.0)        # trip 1 widens 22 bps against the hedge
        if i == 1:
            for r in f:
                r.pop("spot_ref")
        allf += f
        allg += g
    rep = review_rows(allf, allg, tmp_path, taker_bps=5.5)
    assert "NO_SPOT_REFERENCE" in codes(rep, AR.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    assert rep["economics"]["complete_round_trips"] == 2


def test_FINDING_4_relabelling_fills_as_maker_without_a_maker_tier_is_a_named_gap(tmp_path):
    f, g, _ = make_trip()
    for r in f:
        r["liquidity"] = "maker"
        r["fee_usd"] = -r["qty"] * r["price"] * 16.5 / 1e4                 # a 16.5 bp rebate nobody can check
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "FEE_TIER_MISSING_FOR_CLASS" in codes(rep, AR.SEVERITY_GAP)
    assert rep["verdict"] != "CONSISTENT_NOT_VERIFIED"


def test_FINDING_5_a_funding_row_with_a_zero_rate_pays_nothing(tmp_path):
    z = next(i for i, x in enumerate(F) if x == 0.0)
    k = max(0, z - 3)
    f, g, _ = make_trip(k, 8, tag="z")
    idx = next(j for j, r in enumerate(g) if r["rate"] == 0.0)
    ok = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "PAYMENT_DISAGREES_WITH_MARK" not in hard(ok)
    g[idx]["payment_usd"] = 5000.0
    (tmp_path / "x").mkdir()
    rep = review_rows(f, g, tmp_path / "x", taker_bps=5.5)
    assert "PAYMENT_DISAGREES_WITH_MARK" in hard(rep)


def test_a_near_zero_rate_tolerates_the_eight_decimal_rounding_but_not_a_real_payment(tmp_path):
    f, g, _ = make_trip(n=6)
    g[1]["rate"] = 1e-8                                                    # printed rate 0.00000001
    g[1]["payment_usd"] = 0.5 * OPEN[g[1]["ts_ms"]] * 1e-8
    assert "PAYMENT_DISAGREES_WITH_MARK" not in hard(review_rows(f, g, tmp_path, taker_bps=5.5, use_default_history=False))
    (tmp_path / "b").mkdir()
    g[1]["payment_usd"] = 50.0
    rep = review_rows(f, g, tmp_path / "b", taker_bps=5.5)
    assert "PAYMENT_DISAGREES_WITH_MARK" in hard(rep) or "RATE_DISAGREES_WITH_PUBLIC" in hard(rep)


@pytest.mark.parametrize("bad", [{"qty": 1e300}, {"price": 1e10}, {"qty": 1e-9}, {"qty": 10 ** 400}, {"price": 10 ** 400}, {"fee_usd": 1e12},
                                 {"fee_usd": 10 ** 400}, {"fee_usd": 5000.0}, {"ref_price": 90000.0 * 1.2}, {"spot_ref": 1e12}])
def test_FINDING_6_magnitudes_are_bounded_and_overflow_is_a_record_error(bad):
    with pytest.raises(AR.RecordError):
        AR.parse_fills([_fill(**bad)], "BTCUSDT")


@pytest.mark.parametrize("bad", [{"payment_usd": 1e12}, {"position_qty": 1e9}, {"payment_usd": 10 ** 400}, {"position_qty": -10 ** 400}])
def test_FINDING_6_funding_magnitudes_are_bounded(bad):
    with pytest.raises(AR.RecordError):
        AR.parse_funding([_fund(**bad)], "BTCUSDT")


def test_FINDING_6_the_json_output_is_always_valid_json(tmp_path, capsys):
    f, g, _ = make_trip()
    fp, gp = write_jsonl(tmp_path / "fills.jsonl", f), write_jsonl(tmp_path / "funding.jsonl", g)
    assert AR.main(["--fills", fp, "--funding", gp, "--json"]) == 0

    def refuse(c):
        raise AssertionError(f"non-JSON constant {c}")
    json.loads(capsys.readouterr().out, parse_constant=refuse)


def test_FINDING_8_the_order_of_same_millisecond_fills_does_not_change_the_review(tmp_path):
    def build(buy_id, sell_id):
        f, g, _ = make_trip(n=6, qty=1.0)
        t1 = f[1]["ts_ms"]
        p = f[1]["price"]
        close = [{"exec_id": buy_id, "ts_ms": t1, "symbol": "BTCUSDT", "side": "Buy", "qty": 1.5, "price": p, "fee_usd": 1.5 * p * 5.5 / 1e4,
                  "liquidity": "taker", "spot_ref": f[1]["spot_ref"]},
                 {"exec_id": sell_id, "ts_ms": t1, "symbol": "BTCUSDT", "side": "Sell", "qty": 0.5, "price": p, "fee_usd": 0.5 * p * 5.5 / 1e4,
                  "liquidity": "taker", "spot_ref": f[1]["spot_ref"]}]
        return [f[0]] + close, g
    out = []
    for i, ids in enumerate((("a-buy", "z-sell"), ("z-buy", "a-sell"))):
        f, g = build(*ids)
        d = tmp_path / str(i)
        d.mkdir()
        rep = review_rows(f, g, d, taker_bps=5.5)
        out.append((rep["verdict"], [t["status"] for t in rep["round_trips"]], codes(rep), rep["round_trips"][0].get("net_usd")))
    assert out[0] == out[1] and out[0][1] == ["COMPLETE"] and "LONG_EXPOSURE" not in out[0][2]


def test_FINDING_8_a_same_millisecond_close_and_reopen_is_one_lifecycle_whatever_the_ids(tmp_path):
    book_a = AR.Book(AR.parse_fills([_fill(exec_id="a", side="Sell", qty=1.0, ts_ms=1_700_000_000_000),
                                     _fill(exec_id="z-buy", side="Buy", qty=1.0, ts_ms=1_700_000_100_000),
                                     _fill(exec_id="a-sell", side="Sell", qty=1.0, ts_ms=1_700_000_100_000),
                                     _fill(exec_id="b", side="Buy", qty=1.0, ts_ms=1_700_000_200_000)], "BTCUSDT")[0])
    book_b = AR.Book(AR.parse_fills([_fill(exec_id="a", side="Sell", qty=1.0, ts_ms=1_700_000_000_000),
                                     _fill(exec_id="a-buy", side="Buy", qty=1.0, ts_ms=1_700_000_100_000),
                                     _fill(exec_id="z-sell", side="Sell", qty=1.0, ts_ms=1_700_000_100_000),
                                     _fill(exec_id="b", side="Buy", qty=1.0, ts_ms=1_700_000_200_000)], "BTCUSDT")[0])
    la, lb = book_a.lifecycles(), book_b.lifecycles()
    assert len(la) == len(lb) == 1 and la[0]["end_ms"] == lb[0]["end_ms"] == 1_700_000_200_000
    assert book_a.cum == book_b.cum


def test_FINDING_9_nothing_is_called_consistent_on_a_default_run_beyond_the_history_even_when_the_floors_are_met(tmp_path):
    f, g = three_trips(shift=3000)
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["sufficiency"]["meets_engine_floors"] is True and rep["verdict"] != "CONSISTENT_NOT_VERIFIED"


def test_FINDING_11_hostile_inputs_exit_with_a_message_not_a_traceback(tmp_path, capsys):
    f, g, _ = make_trip()
    gp = write_jsonl(tmp_path / "funding.jsonl", g)
    cases = []
    cases.append(write_jsonl(tmp_path / "dust.jsonl", [_fill(qty=1e-9)]))
    cases.append(write_jsonl(tmp_path / "big.jsonl", [_fill(qty=10 ** 400)]))
    csvp = tmp_path / "extra.csv"
    csvp.write_text("exec_id,ts_ms,symbol\na,1700000000000,BTCUSDT,EXTRA\n")
    cases.append(str(csvp))
    short = tmp_path / "short.csv"
    short.write_text("exec_id,ts_ms,symbol,side\na,1700000000000\n")
    cases.append(str(short))
    for c in cases:
        assert AR.main(["--fills", c, "--funding", gp]) == 2, c
        assert "account_review:" in capsys.readouterr().err
    fp = write_jsonl(tmp_path / "fills.jsonl", f)
    notjson = tmp_path / "pub.json"
    notjson.write_text("this is not json")
    assert AR.main(["--fills", fp, "--funding", gp, "--public-funding", str(notjson)]) == 2
    assert "not a readable public history" in capsys.readouterr().err
    bad_gz = tmp_path / "bad.json.gz"
    bad_gz.write_bytes(b"not gzip at all")
    assert AR.main(["--fills", fp, "--funding", gp, "--perp-4h", str(bad_gz)]) == 2
    assert AR.main(["--fills", fp, "--funding", gp, "--perp-4h", str(tmp_path / "missing.json")]) == 2


def test_FINDING_11_a_deeply_nested_record_is_a_message_not_a_recursion_error(tmp_path):
    p = tmp_path / "deep.jsonl"
    p.write_text("[" * 200_000 + "]" * 200_000 + "\n")
    with pytest.raises(AR.RecordError):
        AR.read_rows(str(p))
    q = tmp_path / "deep2.jsonl"
    q.write_text('{"a": ' + "[" * 200_000 + "]" * 200_000 + "}\n")
    with pytest.raises(AR.RecordError, match="nested too deeply|not JSON"):
        AR.read_rows(str(q))


def test_FINDING_12_symlinks_hard_links_and_rotated_names_are_refused(tmp_path):
    f, g, _ = make_trip()
    ok = write_jsonl(tmp_path / "funding.jsonl", g)
    secret = tmp_path / "trading_state.db"
    write_jsonl(secret, f)
    link = tmp_path / "sl.jsonl"
    os.symlink(secret, link)
    chain = tmp_path / "sl2.jsonl"
    os.symlink(link, chain)
    hard_link = tmp_path / "fills.jsonl"
    os.link(secret, hard_link)
    for p in (link, chain, hard_link):
        with pytest.raises(AR.RecordError, match="refusing to read"):
            AR.run(str(p), ok)
    for name in ("trading_state.db.jsonl", "Trading_State.DB", "testnet_session_evidence_2026.jsonl", "prod.env", "credentials.csv",
                 "my_secrets.jsonl", "id_rsa.jsonl", "x.p12", "x.pfx", "trading_state.db-journal", "state.sqlite-wal", "state.sqlite3-shm", ".netrc.jsonl"):
        p = tmp_path / name
        p.write_text("{}\n")
        with pytest.raises(AR.RecordError, match="refusing to read"):
            AR.read_rows(str(p))


def test_FINDING_12_a_file_that_merely_has_two_hard_links_is_refused_even_with_an_innocent_name(tmp_path):
    f, g, _ = make_trip()
    one = tmp_path / "a.jsonl"
    write_jsonl(one, f)
    two = tmp_path / "b.jsonl"
    os.link(one, two)
    with pytest.raises(AR.RecordError, match="hard link"):
        AR.read_rows(str(one))


def test_FINDING_12_the_committed_default_history_is_not_blocked_by_a_checkout_under_a_directory_called_runs(tmp_path, monkeypatch):
    study = tmp_path / "runs" / "study"
    study.mkdir(parents=True)
    (study / "funding_BTCUSDT.json.gz").write_bytes(gzip.compress(b"[]"))
    monkeypatch.setattr(AR.CT, "STUDY", str(study))
    data, digest = _REAL_READ_HISTORY(None, "funding_BTCUSDT.json.gz", True)
    assert data == [] and len(digest) == 64
    with pytest.raises(AR.RecordError, match="refusing to read"):
        _REAL_READ_HISTORY(str(study / "funding_BTCUSDT.json.gz"), "x", True)       # but a path the CALLER names under runs/ is refused


def test_FINDING_13_a_duplicate_key_inside_one_record_is_an_error(tmp_path):
    p = tmp_path / "dup.jsonl"
    p.write_text('{"exec_id": "a", "qty": 100, "qty": 0.1}\n')
    with pytest.raises(AR.RecordError, match="duplicate key"):
        AR.read_rows(str(p))


@pytest.mark.parametrize("line", ['{"qty": NaN}', '{"qty": Infinity}', '{"qty": -Infinity}'])
def test_FINDING_13_json_constants_that_are_not_numbers_are_errors(tmp_path, line):
    p = tmp_path / "c.jsonl"
    p.write_text(line + "\n")
    with pytest.raises(AR.RecordError, match="not a number"):
        AR.read_rows(str(p))


def test_FINDING_13_a_utf8_bom_csv_is_read_and_the_digest_is_of_the_bytes_parsed(tmp_path):
    p = tmp_path / "bom.csv"
    raw = b"\xef\xbb\xbfexec_id,ts_ms\nA,1\n"
    p.write_bytes(raw)
    rows, digest = AR.read_rows(str(p))
    assert rows == [{"exec_id": "A", "ts_ms": "1"}] and digest == hashlib.sha256(raw).hexdigest()


def test_the_basis_is_the_perp_pnl_plus_the_overlay_pnl_computed_independently_from_the_spot_references(tmp_path):
    """An expected value that does not re-use the tool's own basis formula: perp P&L = -sum(signed qty x fill price); the overlay BTC's
    P&L over the same dates = +sum(signed qty x spot_ref); their sum is the tool's basis P&L."""
    rng = random.Random(21)
    for i in range(25):
        f, g, _ = make_trip(rng.randint(100, 3000), rng.randint(2, 20), qty=rng.uniform(0.05, 4.0), b_in=rng.uniform(-30, 30), b_out=rng.uniform(-30, 30),
                            tag=f"i{i}")
        sq = [(-r["qty"] if r["side"] == "Sell" else r["qty"], r["price"], r["spot_ref"]) for r in f]
        perp_pnl = -sum(q * p for q, p, _s in sq)
        overlay_pnl = sum(q * s for q, _p, s in sq)
        d = tmp_path / f"v{i}"
        d.mkdir()
        rep = review_rows(f, g, d, taker_bps=5.5)
        t = rep["round_trips"][0]
        assert t["perp_pnl_usd"] == pytest.approx(perp_pnl, rel=1e-9, abs=1e-6)
        assert t["basis_pnl_usd"] == pytest.approx(perp_pnl + overlay_pnl, rel=1e-9, abs=1e-6)


# ================================================================================================================================
# Second-round review: the basis is the one free variable, 'public' histories are the caller's, and a verdict must not default open
# ================================================================================================================================

def _raw_committed(name):
    with gzip.open(os.path.join(CT.STUDY, name), "rt") as fh:
        return json.load(fh)


def _write_gz(path, obj):
    with gzip.open(path, "wt") as fh:
        json.dump(obj, fh)
    return str(path)


def test_ROUND2_1_a_fabricated_basis_at_the_edge_of_the_deviation_cap_is_rejected_against_the_public_basis(tmp_path):
    f, g, _ = make_trip()
    for r in f:
        r["spot_ref"] = r["price"] / 1.0049              # 49 bps: inside the 0.5 % cap, ~45 bps from what the public bar shows
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "BASIS_OFF_PUBLIC_BAR" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_ROUND2_1_what_the_basis_screen_cannot_exclude_is_stated_next_to_the_net(tmp_path):
    f, g = three_trips()
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    e = rep["economics"]
    assert e["net_is_upper_bound"] is True and e["basis_unverifiable_bps_per_round_trip"] == 2 * AR.BASIS_SCREEN_BPS
    assert any("borrow" in x for x in e["excludes"]) and any("opportunity cost" in x for x in e["excludes"])
    assert "not distinguishable from zero" in e["basis_reading"]
    text = AR.render(rep)
    assert "UPPER BOUND: excludes" in text and "not distinguishable from zero" in text


def test_ROUND2_1_a_spot_reference_copied_from_the_price_is_no_reference(tmp_path):
    f, g, _ = make_trip()
    for r in f:
        r["spot_ref"] = r["price"]
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    t = rep["round_trips"][0]
    assert t["status"] == "NET_UNKNOWN_NO_SPOT_REFERENCE" and t["basis_pnl_usd"] is None and "copy of the price" in t["note"]
    assert {"SPOT_REF_IS_A_COPY_OF_THE_PRICE", "NO_SPOT_REFERENCE"} <= set(codes(rep, AR.SEVERITY_GAP)) and rep["verdict"] == "INCOMPLETE"


def test_ROUND2_1_copying_the_price_into_only_the_losing_trip_cannot_improve_the_verdict(tmp_path):
    allf, allg = [], []
    for i, k in enumerate((2000, 2100, 2200)):
        f, g, _ = make_trip(k, 31, tag=f"t{i}", b_in=3.0, b_out=22.0 if i == 1 else 3.0)
        if i == 1:
            for r in f:
                r["spot_ref"] = r["price"]
        allf += f
        allg += g
    rep = review_rows(allf, allg, tmp_path, taker_bps=5.5)
    assert rep["verdict"] == "INCOMPLETE" and rep["economics"]["complete_round_trips"] == 2


def test_ROUND2_2_a_caller_supplied_history_is_marked_and_stops_the_top_verdict(tmp_path):
    f, g = three_trips()
    funding_rows = _raw_committed("funding_BTCUSDT.json.gz")
    altered = [dict(r) for r in funding_rows]
    altered[0]["fundingRate"] = "0.00009999"                          # one print changed: not the committed file any more
    pub = _write_gz(tmp_path / "pub.json.gz", altered)
    rep = review_rows(f, g, tmp_path, taker_bps=5.5, public_path=pub)
    assert "PUBLIC_HISTORY_CALLER_SUPPLIED" in codes(rep, AR.SEVERITY_GAP) and rep["verdict"] == "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"
    # the committed file itself, given by path, is recognised by its digest and is not flagged
    (tmp_path / "same").mkdir()
    same = os.path.join(CT.STUDY, "funding_BTCUSDT.json.gz")
    rep2 = review_rows(f, g, tmp_path / "same", taker_bps=5.5, public_path=same)
    assert "PUBLIC_HISTORY_CALLER_SUPPLIED" not in codes(rep2) and rep2["verdict"] == "CONSISTENT_NOT_VERIFIED"


def test_ROUND2_2_klines_whose_high_and_low_do_not_bracket_the_bar_are_refused_not_vacuous(tmp_path, capsys):
    f, g, _ = make_trip()
    fp, gp = write_jsonl(tmp_path / "fills.jsonl", f), write_jsonl(tmp_path / "funding.jsonl", g)
    bars = _raw_committed("linear_BTCUSDT_240.json.gz")
    for label, mutate in (("inf_high", lambda r: [r[0], r[1], "Infinity", "0", r[4]]), ("neg_low", lambda r: [r[0], r[1], "1e999", "-1e999", r[4]]),
                          ("none_high", lambda r: [r[0], r[1], None, r[3], r[4]]), ("dict_row", lambda r: {"a": 1}), ("short_row", lambda r: r[:3]),
                          ("low_above_open", lambda r: [r[0], r[1], r[2], str(float(r[1]) * 2), r[4]])):
        path = _write_gz(tmp_path / f"{label}.json.gz", [mutate(r) if i == 0 else r for i, r in enumerate(bars)])
        assert AR.main(["--fills", fp, "--funding", gp, "--perp-4h", path]) == 2, label
        assert "account_review:" in capsys.readouterr().err
    with pytest.raises(CT.DataError):
        CT.parse_klines([{"a": 1}])


def test_ROUND2_5_every_gap_code_the_module_can_emit_is_classified_and_an_unknown_one_blocks_the_top_verdict():
    src = open(AR.__file__, encoding="utf-8").read()
    emitted = set(re.findall(r'_flag\(flags, "([A-Z_]+)", SEVERITY_GAP', src))
    assert emitted, "found no GAP flags"
    assert emitted <= set(AR.STRUCTURAL_GAPS) | set(AR.CHECK_GAPS), sorted(emitted - set(AR.STRUCTURAL_GAPS) - set(AR.CHECK_GAPS))
    # default-closed: a code in neither list is structural (the verdict code subtracts the CHECK list, not the other way round)
    assert "gap_codes - set(CHECK_GAPS)" in src


def test_ROUND2_9_path_names_secret_as_a_token_not_as_a_substring_and_the_evidence_names_the_repos_tools_write(tmp_path):
    for name in ("secretary_fills.csv", "credit_suisse_export.csv", "fills_2026.jsonl", "environment.csv"):
        p = tmp_path / name
        p.write_text("{}\n")
        AR._refuse_protected(str(p))                                            # no exception: these are ordinary names
    for name in ("secret_sauce_fills.csv", "my-credentials.csv", "evidence.jsonl", "ws_evidence.jsonl", "testnet_conformance_evidence.jsonl",
                 "close_evidence.jsonl", "env.csv", "api_token.csv", "api-key.jsonl", "apikey.csv"):
        p = tmp_path / name
        p.write_text("{}\n")
        with pytest.raises(AR.RecordError, match="refusing to read"):
            AR._refuse_protected(str(p))
    for d in ("runs", "runs-2026", "runs_old", "runs.bak"):
        (tmp_path / d).mkdir()
        p = tmp_path / d / "fills.jsonl"
        p.write_text("{}\n")
        with pytest.raises(AR.RecordError, match="refusing to read"):
            AR._refuse_protected(str(p))


def test_ROUND2_9_a_hard_link_made_after_the_name_check_is_caught_on_the_open_file(tmp_path):
    p = tmp_path / "late.jsonl"
    p.write_text("{}\n")
    AR._refuse_protected(str(p))                                                # the name and link count are fine at check time
    os.link(p, tmp_path / "other_name")                                         # ... and the file gains a second name before it is opened
    with pytest.raises(AR.RecordError, match="hard link"):
        AR._read_bytes(str(p), "late.jsonl")
    assert AR._read_bytes(str(p), "late.jsonl", strict_links=False) == b"{}\n"


def test_ROUND2_10_duplicate_csv_headers_unicode_separators_and_non_ascii_digits(tmp_path):
    dup = tmp_path / "dup.csv"
    dup.write_text("exec_id,price,price\na,1,2\n")
    with pytest.raises(AR.RecordError, match="duplicate column"):
        AR.read_rows(str(dup))
    sep = tmp_path / "sep.jsonl"
    sep.write_text(json.dumps({"exec_id": "a b", "x": 1}, ensure_ascii=False) + "\n")           # U+2028 INSIDE a string is one record
    rows, _ = AR.read_rows(str(sep))
    assert len(rows) == 1 and rows[0]["exec_id"] == "a b"
    joined = tmp_path / "joined.jsonl"
    joined.write_text('{"a": 1} {"a": 2}\n')                                                    # two objects joined by U+2028 are NOT two rows
    with pytest.raises(AR.RecordError):
        AR.read_rows(str(joined))
    with pytest.raises(AR.RecordError):
        AR.parse_fills([_fill(qty="١٢٣")], "BTCUSDT")


def test_ROUND2_11_a_compressed_history_that_expands_past_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(AR, "MAX_DECOMPRESSED_BYTES", 1000)
    p = tmp_path / "bomb.json.gz"
    p.write_bytes(gzip.compress(b"[" + b"0," * 5000 + b"0]"))
    with pytest.raises(AR.RecordError, match="expands to more than"):
        _REAL_READ_HISTORY(str(p), "x", False)


def test_ROUND2_mutant_a_close_and_reopen_at_different_milliseconds_inside_the_minute_is_ambiguous(tmp_path):
    f, g, _ = make_trip(n=6)
    t = TS[K0 + 2]
    p = _open_at(t)

    def leg(eid, side, dt):
        return {"exec_id": eid, "ts_ms": t + dt, "symbol": "BTCUSDT", "side": side, "qty": 0.5, "price": p, "fee_usd": 0.5 * p * 5.5 / 1e4,
                "liquidity": "taker", "spot_ref": p / 1.0003}
    rep = review_rows(f + [leg("c-1", "Buy", -40_000), leg("c-2", "Sell", 10_000)], g, tmp_path, taker_bps=5.5)
    assert rep["settlements"]["ambiguous"] == 1 and rep["verdict"] == "INCOMPLETE"       # flat in the middle of the window: not provably held


def test_ROUND2_mutant_one_missing_spot_ref_makes_the_whole_trip_net_unknown(tmp_path):
    f, g, _ = make_trip()
    f[1].pop("spot_ref")
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["round_trips"][0]["status"] == "NET_UNKNOWN_NO_SPOT_REFERENCE" and rep["verdict"] == "INCOMPLETE"


def test_ROUND2_mutant_a_partial_spot_history_is_a_named_check_gap():
    fl, _ = AR.parse_fills(make_trip()[0], "BTCUSDT")
    fu, _ = AR.parse_funding(make_trip()[1], "BTCUSDT")
    rep = AR.review(fl, fu, taker_bps=5.5, public=PUB, bar_open=OPEN, bar_range={}, spot_range={}, spot_open={})
    assert "SPOT_REF_CHECK_PARTIAL" in codes(rep, AR.SEVERITY_GAP) and "PRICE_CHECK_PARTIAL" in codes(rep, AR.SEVERITY_GAP)


def test_ROUND2_mutant_complete_trips_plus_an_open_lifecycle_is_incomplete(tmp_path):
    f, g = three_trips()
    t = TS[K0 + 900]
    p = _open_at(t)
    f = f + [{"exec_id": "open-1", "ts_ms": t, "symbol": "BTCUSDT", "side": "Sell", "qty": 0.5, "price": p, "fee_usd": 0.5 * p * 5.5 / 1e4,
              "liquidity": "taker", "spot_ref": p / 1.0003}]
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert "OPEN_LIFECYCLE" in codes(rep, AR.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE" and rep["sufficiency"]["complete_round_trips"] == 3


def test_ROUND2_mutant_maker_fills_are_compared_with_the_maker_tier_not_the_taker_tier(tmp_path):
    def maker_trip(fee_bps):
        f, g, _ = make_trip(fee_bps=fee_bps, liquidity="maker")
        return f, g
    f, g = maker_trip(2.0)
    assert "FEE_DISAGREES_WITH_TIER" not in hard(review_rows(f, g, tmp_path, taker_bps=5.5, maker_bps=2.0))
    (tmp_path / "a").mkdir()
    assert "FEE_DISAGREES_WITH_TIER" in hard(review_rows(f, g, tmp_path / "a", taker_bps=2.0, maker_bps=5.5))     # swapped tiers


def test_ROUND2_mutant_a_residual_below_the_dust_threshold_still_closes_the_lifecycle(tmp_path):
    f, g, _ = make_trip()
    f[1]["qty"] = f[0]["qty"] + 1e-9
    f[1]["fee_usd"] = f[1]["qty"] * f[1]["price"] * 5.5 / 1e4
    rep = review_rows(f, g, tmp_path, taker_bps=5.5)
    assert rep["round_trips"][0]["status"] == "COMPLETE" and "OPEN_LIFECYCLE" not in codes(rep)


def test_ROUND2_mutant_the_interval_uses_n_minus_one_degrees_of_freedom():
    iv = AR._interval([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    half = 2.306004 * math.sqrt(60.0 / 8.0) / 3.0                  # t(0.975, df 8) x s / sqrt(n); df 9 would give 2.2622
    assert iv["interval"] == pytest.approx([5.0 - half, 5.0 + half], abs=1e-4)


def test_ROUND2_13_the_near_zero_rate_tolerance_is_tested_against_a_real_zero_print(tmp_path):
    z = next(i for i, x in enumerate(F) if x == 0.0)
    f, g, _ = make_trip(max(0, z - 3), 8, tag="z")
    idx = next(j for j, r in enumerate(g) if r["rate"] == 0.0)
    g[idx]["payment_usd"] = 1e-4                                    # inside the 8-decimal rounding noise for 0.5 BTC
    assert "PAYMENT_DISAGREES_WITH_MARK" not in hard(review_rows(f, g, tmp_path, taker_bps=5.5))
    (tmp_path / "b").mkdir()
    g[idx]["payment_usd"] = 0.5                                     # 5,000x the rounding noise
    assert "PAYMENT_DISAGREES_WITH_MARK" in hard(review_rows(f, g, tmp_path / "b", taker_bps=5.5))


# ================================================================================================================================
# A period that starts with a position already open and ends with one still open (the owner ledger needs both)
# ================================================================================================================================

def _carried(n=12, **kw):
    """The closing leg of a trip only, plus the position the book already held when the period began."""
    f, g, e = make_trip(n=n, **kw)
    t_open = f[0]["ts_ms"]
    fills, _ = AR.parse_fills(f[1:], "BTCUSDT")
    funding, _ = AR.parse_funding(g, "BTCUSDT")
    return fills, funding, t_open, -f[0]["qty"]


def _review_carried(fills, funding, t_open, qty, **kw):
    return AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN, opening_qty=qty, opening_ms=t_open, **kw)


def test_OPENING_a_carried_in_short_owes_a_row_for_every_settlement_after_the_opening_instant_and_none_before():
    fills, funding, t_open, q = _carried()
    rep = _review_carried(fills, funding, t_open, q)
    assert hard(rep) == [] and rep["settlements"]["held"] == 12 and rep["settlements"]["missing"] == 0
    assert [t["status"] for t in rep["round_trips"]] == ["EXCLUDED_CARRIED_IN"]
    assert rep["economics"]["tag"] == "UNKNOWN"                          # its entry basis is outside the records
    info = [f for f in rep["flags"] if f["code"] == "CARRIED_IN_POSITION"]
    assert info and info[0]["severity"] == "INFO"


def test_OPENING_the_same_records_without_the_opening_position_are_unattributed_funding():
    fills, funding, _t, _q = _carried()
    rep = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN)
    assert "UNATTRIBUTED_FUNDING" in hard(rep)                          # flat before the first fill, yet paid: exactly the false alarm the opening position removes


def test_OPENING_a_settlement_exactly_at_the_opening_instant_belongs_to_the_opening_balance():
    fills, funding, _t, q = _carried()
    first = funding[0].grid_ms
    rep = _review_carried(fills, funding[1:], first, q)                  # the period opens AT the first settlement; its row is not in the period
    assert hard(rep) == [] and rep["settlements"]["missing"] == 0 and rep["settlements"]["held"] == 11
    rep2 = _review_carried(fills, funding[1:], first - 1000, q)           # opened a second earlier: that settlement is owed and absent
    assert rep2["settlements"]["missing"] == 1 and "MISSING_SETTLEMENT" in codes(rep2, AR.SEVERITY_GAP)


def test_OPENING_a_carried_in_long_is_not_a_carry_book():
    fills, funding, t_open, _q = _carried()
    rep = _review_carried(fills, funding, t_open, +0.5)
    assert "LONG_EXPOSURE" in hard(rep)


def test_OPENING_a_carried_in_position_with_no_fills_at_all_still_checks_its_settlements():
    _f, funding, t_open, q = _carried()
    rep = AR.review([], funding, taker_bps=5.5, public=PUB, bar_open=OPEN, opening_qty=q, opening_ms=t_open, closing_ms=funding[-1].grid_ms + 3600_000)
    assert hard(rep) == [] and rep["settlements"]["held"] == 12 and rep["settlements"]["missing"] == 0
    rep2 = AR.review([], funding[:-1], taker_bps=5.5, public=PUB, bar_open=OPEN, opening_qty=q, opening_ms=t_open, closing_ms=funding[-1].grid_ms + 3600_000)
    assert rep2["settlements"]["missing"] == 1                            # the last row is missing and the position never closed


def test_CLOSING_ms_extends_the_owed_settlements_to_the_end_of_the_period():
    f, g, _ = make_trip(n=12)
    fills, _ = AR.parse_fills(f[:1], "BTCUSDT")                           # opened, never closed within the records
    funding, _ = AR.parse_funding(g[:-1], "BTCUSDT")                      # ... and the last settlement's row is absent
    without = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN)
    with_close = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN, closing_ms=g[-1]["ts_ms"] + 3600_000)
    assert without["settlements"]["missing"] == 0                         # silent: no later record forces the question
    assert with_close["settlements"]["missing"] == 1                      # the period's end does


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, 1e9, "0.5", None])
def test_OPENING_qty_is_validated(bad):
    fills, funding, t_open, _q = _carried()
    with pytest.raises(AR.RecordError):
        AR.review(fills, funding, opening_qty=bad, opening_ms=t_open)


def test_OPENING_a_nonzero_position_needs_its_time():
    fills, funding, _t, q = _carried()
    with pytest.raises(AR.RecordError, match="opening_ms"):
        AR.review(fills, funding, opening_qty=q)
    with pytest.raises(AR.RecordError, match="opening_ms"):
        AR.review(fills, funding, opening_qty=q, opening_ms=True)


def test_OPENING_zero_changes_nothing_at_all():
    f, g, _ = make_trip()
    fills, _ = AR.parse_fills(f, "BTCUSDT")
    funding, _ = AR.parse_funding(g, "BTCUSDT")
    a = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN)
    b = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN, opening_qty=0.0, opening_ms=None, closing_ms=None)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_a_fill_dated_before_the_carried_in_position_is_refused_as_a_double_count(tmp_path):
    f, g, _ = make_trip(n=6)
    fills, _ = AR.parse_fills(f, "BTCUSDT")
    funding, _ = AR.parse_funding(g, "BTCUSDT")
    with pytest.raises(AR.RecordError, match="before the opening instant"):
        AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN, opening_qty=-0.5, opening_ms=fills[0].ts_ms + 1_000, closing_ms=fills[-1].ts_ms + 1)
