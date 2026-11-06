"""The trading engine and the reconciliation engines must agree on the same input.

The REAL CarryEngine (overlay, taker) runs a full lifecycle through the REAL LedgerBroker and Journal against an in-process scripted venue: no network, no
files, no credentials, synthetic prices only. The venue then "pays" funding exactly as a short position is owed it, and three independent readers are asked what
that account earned:

  * the engine's own journal statement (``Journal.statement()``),
  * ``tools/account_review.py`` fed the same fills and settlement rows, and
  * ``tools/owner_ledger.py`` fed the same records plus opening/closing balances whose expected values are written out by hand below.

The expected numbers are computed in the test from first principles (a 0.1 BTC short against 1.0 BTC owned, entered at 100,000, closed at 101,000, with
the settlement payments the venue owes), never read back from the code under test.

REGRESSION REPRODUCED HERE (found by an independent trace of the engine against the reconciler): a FUNDING_INVERTED exit is decided at the first tick after the
settlement it reacts to, so the closing fill is booked seconds after that settlement. The ledger then refused to claim the final payment (ownership judged
from the bot's own booking stamps within +/-60 s), booked it at $0 and understated the loss of the cycle by exactly that payment, while ``account_review`` called
the same settlement ambiguous: both omitted a real venue debit. The venue row's own signed size now resolves it (see ``ledger._attribution_problem``).
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_review as AR  # noqa: E402
import funding_reconcile as R  # noqa: E402
import ledger as L  # noqa: E402
import owner_ledger as OL  # noqa: E402
from carry_broker import FundingHistory, SettlementEvidence  # noqa: E402
from carry_engine import CarryEngine, OVERLAY  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

H8 = 8 * 3600 * 1000
S0 = (1_700_000_000_000 // H8) * H8                       # a real 8h UTC settlement stamp
RATES = [3.0, 3.0, 3.0, 3.0, 3.0, -4.0, -4.0, -4.0]       # public prints, bps, at S0 .. S7
MARKS = [100_000.0, 100_000.0, 100_000.0, 100_000.0, 100_200.0, 100_400.0, 100_800.0, 101_000.0]
CAP = 10_000.0
QTY = 0.1                                                  # CAP / 100,000, already on the 0.001 lot
SPOT_RATIO = 0.9998
TAKER_BPS = 5.5


class _Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms


class _Venue:
    """Just enough venue for CarryEngine + LedgerBroker. Fills at the scripted mark/spot; fee = taker rate on notional, in USDT."""

    VENUE = "scripted"

    def __init__(self, clock: _Clock, inventory: float = 1.0) -> None:
        self.clock, self.inventory = clock, inventory
        self.mark = self.spot = 100_000.0
        self.short = 0.0
        self.orders, self.n = [], 0

    def get_mark(self, symbol): return self.mark
    def get_spot_mark(self, symbol): return self.spot
    def get_margin_multiple(self, symbol): return 5.0
    def get_lot_rules(self, symbol, product): return {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0}
    def get_fee_rates(self, symbol, product): return {"maker_bps": 2.0, "taker_bps": TAKER_BPS}
    def get_spot_inventory(self, symbol): return self.inventory
    def get_perp_position(self, symbol): return self.short

    def get_book_top(self, symbol, product):
        px = self.mark if product == "linear" else self.spot
        return {"bid": px - 0.05, "ask": px + 0.05}

    def place_market(self, *, symbol, side, qty, product, order_link_id=None):
        self.n += 1
        px = self.mark if product == "linear" else self.spot
        fee = qty * px * TAKER_BPS / 1e4
        if product == "linear":
            self.short += qty if side == "Sell" else -qty
        self.orders.append((self.clock.ms, product, side, qty, px))
        return {"filled_qty": qty, "avg_price": px, "order_link_id": order_link_id or f"scr-{self.n}", "fee": fee,
                "fee_detail": {"known": True, "source": "cumFeeDetail", "note": "", "components": [{"amount": fee, "currency": "USDT"}]},
                "fill_ms": self.clock.ms}


def _run(lag_ms: int):
    """Drive the engine one tick per settlement. The tick that first sees print k runs ``lag_ms`` after the settlement (the live loop is 60 s)."""
    clock = _Clock(S0)
    venue = _Venue(clock)
    journal = L.Journal()
    lb = L.LedgerBroker(venue, journal, ms=lambda: int(clock.ms), book_overlay_inventory=True)
    eng = CarryEngine(broker=lb, max_notional_usd=CAP, borrow_apr=0.0, execution_mode=OVERLAY, persist=lambda s: None)
    eng.pair_risk = CarryRisk(max_notional_usd=CAP)
    actions = []
    with mock.patch("time.time", lambda: clock.ms / 1000.0), mock.patch("time.sleep", lambda s: None):
        for k, bps in enumerate(RATES):
            sk = S0 + k * H8
            clock.ms = sk + lag_ms
            venue.mark, venue.spot = MARKS[k], MARKS[k] * SPOT_RATIO
            eng.snapshot = MarketSnapshot(perp_mark=venue.mark, spot_mark=venue.spot, funding_bps=bps, margin_multiple=5.0, observed_at_s=clock.ms / 1000.0,
                                          funding_print_bps=bps, funding_print_ms=sk)
            d = eng.on_candle(mark=venue.mark, funding_bps=bps, spot=venue.spot, timestamp_ms=clock.ms, funding_print_ms=sk)
            actions.append(d.action)
            if d.action == "unwound":
                break
    return clock, venue, journal, lb, eng, actions


def _venue_payments(extra=None, size=-QTY):
    """What the venue owes a short of QTY at each settlement the position existed at: it was opened at the tick after S2, so S3..S7."""
    out = []
    for k in range(3, 8):
        usd = QTY * MARKS[k] * RATES[k] / 1e4              # a short RECEIVES when the rate is positive
        out.append({"id": f"p{k}", "ms": S0 + k * H8, "usd": usd, "currency": "USDT", "size": size, "symbol": "BTCUSDT", "sign": "confirmed"})
    return out + list(extra or [])


# hand arithmetic, independent of the code under test
FUNDING_OWED = sum(QTY * MARKS[k] * RATES[k] / 1e4 for k in range(3, 8))                         # 3.0 + 3.006 - 4.016 - 4.032 - 4.040
FEES = QTY * 100_000.0 * TAKER_BPS / 1e4 + QTY * 101_000.0 * TAKER_BPS / 1e4                     # 5.5 + 5.555
PERP_REALISED = (100_000.0 - 101_000.0) * QTY                                                    # a short loses when price rises
BASIS = QTY * (100_000.0 - 100_000.0 * SPOT_RATIO) - QTY * (101_000.0 - 101_000.0 * SPOT_RATIO)  # sell leg basis minus buy leg basis
PAIR_NET = FUNDING_OWED + BASIS - FEES                                                            # what the HEDGE earned


def test_the_hand_arithmetic_itself():
    assert FUNDING_OWED == pytest.approx(-6.082) and FEES == pytest.approx(11.055)
    assert PERP_REALISED == pytest.approx(-100.0) and BASIS == pytest.approx(-0.02) and PAIR_NET == pytest.approx(-17.157)


@pytest.mark.parametrize("lag_s", [20, 59, 61, 120])
def test_the_booked_economics_do_not_depend_on_how_long_after_the_print_the_tick_runs(lag_s):
    clock, venue, journal, lb, eng, actions = _run(lag_s * 1000)
    assert actions[-1] == "unwound" and venue.short == pytest.approx(0.0) and [o[2] for o in venue.orders] == ["Sell", "Buy"]
    clock.ms += 30_000
    res = L.book_funding_payments(journal, _venue_payments(), ms=clock.ms)
    assert res["booked"] == 5 and res["unattributed"] == 0, res
    st = journal.statement(now_ms=clock.ms)
    assert st["funding_usd"] == pytest.approx(FUNDING_OWED, abs=1e-9)
    assert st["fees_usd"] == pytest.approx(FEES, abs=1e-9)
    assert st["pair_net_usd"] == pytest.approx(PAIR_NET, abs=1e-6)
    assert st["funding_unattributed"] == 0


def _records_for_account_review(journal, lb, payments):
    reval = {e.fee_raw["close_ref"]: e.fee_raw for e in journal.entries if e.kind == "revalue"}
    fills = []
    for e in journal.entries:
        if e.kind not in ("perp_open", "perp_close"):
            continue
        f = next(x for x in lb.fills if x["ref"] == e.ref)
        spot_ref = e.fee_raw.get("hedge_spot_ref") if e.kind == "perp_open" else reval.get(e.ref, {}).get("exit_spot")
        ex = e.fee_raw.get("execution", {})
        t = int(ex.get("fill_ms") or e.ms)
        fills.append(AR.Fill(e.ref, t, f["side"], f["qty"], f["price"], f["fee_usdt"] + f["fee_btc"] * f["price"], "taker", None, spot_ref))
    rows = [AR.FundingRow(p["id"], p["ms"], p["ms"], p["size"], RATES[int(p["id"][1:])] / 1e4, p["usd"]) for p in payments]
    return fills, rows


@pytest.mark.parametrize("lag_s", [20, 120])
def test_the_engine_and_account_review_agree_on_fees_funding_perp_pnl_and_the_hedge_net(lag_s):
    clock, venue, journal, lb, eng, _ = _run(lag_s * 1000)
    pays = _venue_payments()
    clock.ms += 30_000
    L.book_funding_payments(journal, pays, ms=clock.ms)
    st = journal.statement(now_ms=clock.ms)
    fills, rows = _records_for_account_review(journal, lb, pays)
    rep = AR.review(fills, rows, taker_bps=TAKER_BPS, maker_bps=2.0)
    t = rep["round_trips"][0]
    assert rep["settlements"]["ambiguous"] == 0 and rep["settlements"]["missing"] == 0 and not [f for f in rep["flags"] if f["severity"] == AR.SEVERITY_HARD]
    assert t["funding_usd"] == pytest.approx(st["funding_usd"], abs=1e-9) == pytest.approx(FUNDING_OWED, abs=1e-9)
    assert t["fees_usd"] == pytest.approx(st["fees_usd"], abs=1e-9)
    assert t["perp_pnl_usd"] == pytest.approx(PERP_REALISED, abs=1e-9)
    assert t["basis_pnl_usd"] == pytest.approx(BASIS, abs=1e-9)
    assert t["net_usd"] == pytest.approx(st["pair_net_usd"], abs=1e-6) == pytest.approx(PAIR_NET, abs=1e-6)


def test_the_owner_ledger_reaches_the_same_figures_and_ties_the_account_to_the_cent():
    clock, venue, journal, lb, eng, _ = _run(20_000)
    pays = _venue_payments()
    clock.ms += 30_000
    L.book_funding_payments(journal, pays, ms=clock.ms)
    st = journal.statement(now_ms=clock.ms)
    fills, rows = _records_for_account_review(journal, lb, pays)
    t_open, t_close = fills[0].ts_ms - 3_600_000, fills[1].ts_ms + 3_600_000
    p0, p1 = 100_000.0 * SPOT_RATIO, 101_000.0 * SPOT_RATIO                         # the owned BTC is priced at SPOT, as the engine's inventory is
    usdt0, btc0 = 50_000.0, 1.0
    # the account after the cycle, by hand: cash moves by realised P&L, fees and funding; the 1.0 BTC is untouched and simply re-priced
    usdt1 = usdt0 + PERP_REALISED - FEES + FUNDING_OWED
    snaps = [OL.Snapshot("open", t_open, usdt0, btc0, p0, 0.0, None, 0.0, None, None, 0.0, None, None, "t"),
             OL.Snapshot("close", t_close, usdt1, btc0, p1, 0.0, None, 0.0, None, None, 0.0, None, None, "t")]
    inp = OL.Inputs(fills, rows, [], [], [], snaps, {"t": "0" * 64}, {}, {"public": None, "bar_open": None, "bar_range": None, "spot_range": None, "spot_open": None,
                                                                         "caller_histories": []}, TAKER_BPS, 2.0)
    rep = OL.build(inp)
    b = rep["equity_bridge"]
    assert rep["verdict"] == "INCOMPLETE" and "NO_STATEMENT" in [f["code"] for f in rep["flags"]]            # no statement was supplied: never a clean pass
    assert [f for f in rep["flags"] if f["severity"] == OL.SEVERITY_HARD] == []
    assert rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6) and b["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)
    assert b["perp_realised_usd"] == pytest.approx(PERP_REALISED) and b["perp_fees_usd"] == pytest.approx(-FEES) and b["funding_net_usd"] == pytest.approx(FUNDING_OWED)
    assert b["btc_economic_pnl_usd"] == pytest.approx(btc0 * (p1 - p0))
    s = rep["strategy_contribution"]
    assert s["status"] == "COMPUTED" and s["net_usd"] == pytest.approx(st["pair_net_usd"], abs=1e-6) == pytest.approx(PAIR_NET, abs=1e-6)
    # the account moved by the hedge's net PLUS the price gain on the 0.9 BTC that was never hedged, which is not the strategy's P&L: the two are kept apart
    assert b["change_in_equity_usd"] == pytest.approx(PAIR_NET + (btc0 - QTY) * (p1 - p0), abs=1e-6)
    assert b["change_in_equity_usd"] != pytest.approx(s["net_usd"], abs=1.0)


# ---------------------------------------------------------------------------------------------------------------- refusals stay refusals

def test_a_row_with_the_wrong_sign_of_size_is_not_claimed_and_the_omission_is_visible():
    clock, venue, journal, lb, eng, _ = _run(20_000)
    clock.ms += 30_000
    res = L.book_funding_payments(journal, _venue_payments(size=+QTY), ms=clock.ms)         # a LONG of the same size is not ours
    assert res["booked"] == 0 and res["unattributed"] == 5
    st = journal.statement(now_ms=clock.ms)
    assert st["funding_usd"] == 0.0 and st["funding_unattributed"] == 5 and st["net_basis"] == "provisional_funding_unverified" and st["funding_confirmed"] is False


def test_a_row_sized_like_the_new_entry_at_the_entry_print_is_not_claimed():
    """The engine enters AFTER the print, so a payment at S2 (the entry print) sized like the position it just opened is the suspicious case."""
    clock, venue, journal, lb, eng, _ = _run(20_000)
    clock.ms += 30_000
    phantom = {"id": "p2", "ms": S0 + 2 * H8, "usd": 3.0, "currency": "USDT", "size": -QTY, "symbol": "BTCUSDT", "sign": "confirmed"}
    res = L.book_funding_payments(journal, _venue_payments([phantom]), ms=clock.ms)
    assert res["booked"] == 5 and res["unattributed"] == 1
    assert journal.statement(now_ms=clock.ms)["funding_usd"] == pytest.approx(FUNDING_OWED, abs=1e-9)      # the phantom added nothing


def test_the_funding_reconciliation_counts_the_resolved_exit_settlement_as_expected_and_booked():
    clock, venue, journal, lb, eng, _ = _run(20_000)
    pays = _venue_payments()
    now = clock.ms + 10 * H8
    L.book_funding_payments(journal, pays, ms=clock.ms + 30_000)
    lo = journal.entries[0].ms - 120_000
    hist = FundingHistory(start_ms=lo, end_ms=now, prints=[(RATES[k], S0 + k * H8) for k in range(len(RATES))])
    ev = SettlementEvidence(start_ms=lo, end_ms=now, payments=pays, windows=1, pages=1, rows_returned=len(pays))
    rec = R.reconcile_funding(journal, history=hist, evidence=ev, now_ms=now)[0]
    assert rec["evidence"]["account_rows_attributed"] == 5 and rec["evidence"]["account_rows_unattributed"] == 0
    assert rec["evidence"]["expected_settlements"] == 5 and rec["evidence"]["matched_settlements"] == 5 and rec["evidence"]["missing_expected"] == 0
    # The ENTRY print (S2) stays ambiguous by design: the position was opened 20 s after it, nothing is owed, and the reconciler does not assume that from the
    # absence of a row. The lifecycle therefore stays PROVISIONAL (conservative); it is never reported as confirmed.
    assert rec["reasons"] == ["OWNERSHIP_AMBIGUOUS_AT_SETTLEMENT"] and rec["state"] == "PROVISIONAL"
    assert journal.statement(now_ms=now)["funding_confirmed"] is False


# ------------------------------------------------------------------------------------------------------ the account_review side of the rule

def _fill(eid, ts, side, qty=QTY, price=100_000.0):
    return AR.Fill(eid, ts, side, qty, price, qty * price * TAKER_BPS / 1e4, "taker", None, price * 0.9998)


def test_account_review_resolves_an_exit_after_the_settlement_by_the_rows_stated_position_and_nothing_else():
    t = S0 + 7 * H8
    book = AR.Book([_fill("o", S0 + 2 * H8 + 20_000, "Sell"), _fill("c", t + 20_000, "Buy")])
    row = AR.FundingRow("r", t, t, -QTY, 0.0004, 1.0)
    assert AR.classify_settlement(book, t) == "ambiguous"                                  # the fills alone cannot place the exit
    assert AR.classify_settlement(book, t, row) == "held"                                  # the row states the position the book entered the window with
    assert AR.classify_settlement(book, t, AR.FundingRow("r", t, t, -2 * QTY, 0.0004, 1.0)) == "ambiguous"       # matches neither
    assert AR.classify_settlement(book, t, AR.FundingRow("r", t, t, +QTY, 0.0004, 1.0)) == "ambiguous"          # wrong sign
    # a fill at or before the settlement contradicts a row that names the earlier position
    early = AR.Book([_fill("o", S0 + 2 * H8 + 20_000, "Sell"), _fill("c", t - 10_000, "Buy")])
    assert AR.classify_settlement(early, t, row) == "ambiguous"
    # the entry side is never resolved this way: a row sized like the position opened just AFTER the print
    entry = AR.Book([_fill("o", t + 20_000, "Sell")])
    assert AR.classify_settlement(entry, t, row) == "ambiguous"


def test_the_two_reconcilers_share_one_attribution_slack_and_one_position_tolerance_family():
    assert AR.SLACK_MS == L.ATTRIBUTION_SLACK_MS and AR.SLACK_MS == R.L.ATTRIBUTION_SLACK_MS
    assert L.SIZE_REL_TOL >= AR.POSITION_REL_TOL         # the ledger is never stricter than the reviewer on what counts as the same position
