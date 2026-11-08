"""FAULT-INJECTED FULL LIFECYCLE on the REAL integrated path under the PRODUCTION entry authority.

    market data -> strategy -> economic gate -> risk -> capital -> order -> acknowledgement -> fill -> fee -> funding -> exit -> reconciliation -> ledger -> owner P&L

``config.load -> main.build_bot -> TradingBot.tick -> CarryEngine -> LedgerBroker -> CarryBroker -> BybitClient -> SyntheticVenue`` (tests/test_testnet_lifecycle_offline.py),
with the entry authority ``entry_authority.build_for_book`` installs, given an approved policy file and a measured state (the seam of tests/test_entry_authority_e2e.py
``approve()``). The last stage, "owner P&L", is the REAL ``tools/owner_import.py`` run over the venue's own records exported in the documented Bybit V5 shapes.

Scenarios NOT in test_testnet_lifecycle_offline.py / test_entry_authority_e2e.py (those already cover: the clean overlay cycle to the cent, partial fills, a lost
acknowledgement, an invisible order, a cancel/fill race, duplicate and reordered executions, death after accept / before accept / after the exit, the refusal gates
(cap, kill switch, benefit), FLAT from the venue, hostile fee/funding answers WITHOUT the production authority, the no-policy refusal, a breach latch within one process):

  1. the clean cycle all the way to owner P&L, journal funding RECONCILED, reservation ledger on disk
  2. stale market data (the settled funding print too old): zero orders
  3. stale / unreadable / unmeasured ACCOUNT state: zero orders
  4. wrong fee tier answers (another symbol's row, percent scale, zeros) under the production authority: zero orders
  5. wrong funding (sign, x10): reconciliation PROVISIONAL, owner P&L DISPUTED with a null headline, the venue's own cash unaffected
  6. a restart on the same state db AND the same authority directory with an unresolved position: halts, no order, the reservation survives
  7. recovery after a crash between the acknowledgement and the ledger write: a human flattens, clears, the next entry is ONE new order
  8. a risk latch survives the restart and a clean measured state does not lift it; the exit is still open
  9. a corrupted market-data backup copy fails restore-check while the original verifies
 10. a corrupted state-db backup fails verify, is refused by restore and promote, and the live file is untouched

Every expected cash / position / P&L is literal arithmetic written next to the assertion and checked against the VENUE's own state. Modelled behaviour only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_review as AR  # noqa: E402
import bybit_connection as bc  # noqa: E402
import entry_authority as EA  # noqa: E402
from main import _entry_hedge_read  # noqa: E402
import fixtures_owner_import as F  # noqa: E402
import md_seal as SL  # noqa: E402
import owner_import as OI  # noqa: E402
import risk_policy as RP  # noqa: E402
import state_backup as SB  # noqa: E402
from carry_engine import BookState  # noqa: E402
from synthetic_venue import VenueKill  # noqa: E402
from test_entry_authority import GOOD, POLICY  # noqa: E402
from test_entry_authority_e2e import approve  # noqa: E402
from test_md_seal import DAY1, NOW, make_store  # noqa: E402
from test_testnet_lifecycle_offline import CREATE, B, H8, clock, creates, no_live_ack, no_network, world  # noqa: E402,F401

pytestmark = pytest.mark.real_entry_authority

_REAL_READ_HISTORY = AR._read_history
_HISTORY = {}


@pytest.fixture(autouse=True)
def _read_the_committed_history_once(monkeypatch):
    def cached(path, default_name, use_default):
        if path is None and use_default:
            if default_name not in _HISTORY:
                _HISTORY[default_name] = _REAL_READ_HISTORY(path, default_name, use_default)
            return _HISTORY[default_name]
        return _REAL_READ_HISTORY(path, default_name, use_default)
    monkeypatch.setattr(AR, "_read_history", cached)


#: the world's clock sits in Oct 2025, inside the committed public history. The OWNER export is shifted by a whole number of 8 h settlement periods into Oct 2026 so
#: that the importer's public-history comparison (which the synthetic prints would of course contradict) does not run: the shift changes no figure.
SHIFT = ((F.T0 - B) // H8 + 1) * H8
USDT0, BTC0 = 10_000.0, 0.05


# ------------------------------------------------------------------------------------------------ helpers

def approve_with(w, tmp_path, state_provider):
    """``approve`` with an arbitrary measured-state provider (it may raise)."""
    path = tmp_path / "approved_policy.json"
    path.write_text(json.dumps(dataclasses.asdict(POLICY)))
    w.bot.client.entry_authority = EA.build_for_book(
        venue="testnet", store=w.store, state_dir=os.path.dirname(w.path), max_notional_usd=102.0,
        mark_provider=lambda s: 50_000.0, policy_path=str(path), state_provider=state_provider, require_risk_policy=True,
        perp_symbol=w.bot.carry.perp_symbol, spot_symbol=w.bot.carry.spot_symbol, hedge_reader=lambda: _entry_hedge_read(w.bot))


def refusal_reasons(w):
    return [r for _l, rs in w.bot.client.entry_authority.refusals for r in rs]


def untouched(w):
    """The VENUE's own state says nothing happened: no create request, no order, no execution, flat, wallet as opened."""
    v = w.venue
    assert v.requests_by_path[CREATE] == 0 and v.orders == {} and v.executions == [] and v.settlements == []
    assert v.net_position() == 0.0 and v.open_orders() == []
    assert v.wallet == {"USDT": USDT0, "BTC": BTC0}
    assert w.bot.carry.state is BookState.FLAT


def settle(w, rate, *, tamper=None, then_mark=None):
    """The venue settles at the next boundary; ``tamper`` may corrupt the rows the venue SERVES (its wallet already moved by the true amount)."""
    w.clock.to_next_boundary()
    rows = w.venue.settle_funding(rate, at_ms=w.clock.now)
    if tamper:
        tamper(rows)
    if then_mark is not None:
        w.venue.set_mark(then_mark)
    return w.tick(90)


def run_cycle(w, tmp_path, *, tamper_first=None):
    """prime -> approve -> entry -> +0.06 -0.01 -0.01 -0.01 funding -> exit at 51,000 -> a day of reconciliation polls."""
    w.prime()
    approve(w, tmp_path)
    assert w.tick()["action"] == "opened"
    assert settle(w, 0.0006, tamper=tamper_first)["reason"] == "HEDGED_AND_COLLECTING"
    for _ in range(2):
        settle(w, -0.0001)
    d = settle(w, -0.0001, then_mark=51_000.0)
    assert d["action"] == "unwound" and d["reason"] == "FUNDING_INVERTED"
    for _ in range(30):
        w.tick(3600)
    return d


def export_owner_files(w, out, t_open_ms, t_close_ms, spot_open, spot_close):
    """The venue's own records as the owner would export them (documented V5 shapes): linear executions, a transaction log with TRADE and SETTLEMENT rows and the
    running USDT balance, and two wallet-balance envelopes. Built from the venue's rows, never from the book. Times shifted by SHIFT."""
    v = w.venue
    ev = [(int(r["execTime"]), 0, r) for r in v.executions if r["category"] == "linear"] + [(int(r["transactionTime"]), 1, r) for r in v.settlements]
    ev.sort(key=lambda x: (x[0], x[1]))
    rates = {ts: rate for ts, rate in v.funding_prints}
    bal, pos, rows_tx, rows_ex, n = USDT0, 0.0, [], [], 0
    for ts, kind, r in ev:
        n += 1
        if kind == 0:
            qty = float(r["execQty"]) * (1 if r["side"] == "Buy" else -1)
            pos = 0.0 if abs(pos + qty) < 1e-12 else pos + qty
            fee = float(r["execFee"])
            cash = sum(float(c["closedPnl"]) for c in v.closed_pnl if int(c["updatedTime"]) == ts)
            bal += cash - fee
            rows_ex.append(dict(r, execTime=str(ts + SHIFT)))
            rows_tx.append(F.trade_row(n, ts + SHIFT, r["execId"], r["side"], pos, fee, cash, bal))
        else:
            pay = float(r["funding"])
            bal += pay
            rows_tx.append(F.settlement_row(n, ts + SHIFT, float(r["size"]), rates[ts], pay, bal))
    exec_p = F.write_jsonl(out / "exec.jsonl", rows_ex)
    tx_p = F.write_jsonl(out / "txlog.jsonl", rows_tx)
    w0 = F.write_json(out / "w0.json", F.wallet(t_open_ms + SHIFT, USDT0, BTC0))
    w1 = F.write_json(out / "w1.json", F.wallet(t_close_ms + SHIFT, v.wallet["USDT"], v.wallet["BTC"]))
    marks = [{"instrument": "BTC_SPOT", "ts_ms": t_open_ms + SHIFT, "price": spot_open, "source": "synthetic venue spot"},
             {"instrument": "BTC_SPOT", "ts_ms": t_close_ms + SHIFT, "price": spot_close, "source": "synthetic venue spot"}]
    return [("linear_execution", exec_p), ("transaction_log", tx_p), ("wallet_opening", w0), ("wallet_closing", w1)], marks


def owner_pnl(w, tmp_path):
    out = tmp_path / "owner"
    out.mkdir(exist_ok=True)
    pairs, marks = export_owner_files(w, out, B, w.clock.now, 50_000.0, w.venue.spot)
    return OI.run_import(pairs, marks=marks)


# ================================================================================================
# 1. the clean cycle, all the way to the owner's P&L
# ================================================================================================

def test_the_clean_cycle_reaches_a_reconciled_owner_pnl_equal_to_the_venues_own_cash(world, tmp_path):
    """Mark 50,000 (51,000 at the exit), size 0.002 BTC, taker 5.5 bps, funding +0.06 -0.01 -0.01 -0.01.

      realised = (50,000 - 51,000) * 0.002                  = -2.0000
      fees     = 0.002*50,000*0.00055 + 0.002*51,000*0.00055 = 0.0550 + 0.0561 = 0.1111
      funding  = 0.06 - 0.03                                  = +0.0300
      CASH     = -2.0 - 0.1111 + 0.03                         = -2.0811    (the venue wallet's USDT change)
      BTC leg  = 0.05 * (51,000 - 50,000)                     = +50.0 (marked, not cash)    economic = -2.0811 + 50 = 47.9189
    """
    w = world(mode="overlay")
    run_cycle(w, tmp_path)
    v = w.venue
    assert v.net_position() == 0.0 and v.wallet["BTC"] == BTC0 and len(v.orders) == 2 and v.requests_by_path[CREATE] == 2
    assert v.wallet["USDT"] == pytest.approx(USDT0 - 2.0811)
    assert [b["side"] for b in creates(v)] == ["Sell", "Buy"] and creates(v)[1]["reduceOnly"] is True
    st = w.journal.statement()
    assert st["net_usd"] == pytest.approx(-2.0811) == pytest.approx(v.wallet["USDT"] - USDT0)
    assert st["funding_reconciled"] is True and st["net_basis"] == "confirmed"
    assert [x["state"] for x in st["funding_lifecycles"]] == ["FULLY_RECONCILED"]
    # the reservation ledger is durable and names the one entry
    rows = [json.loads(x) for x in open(os.path.join(os.path.dirname(w.path), "entry_reservations.jsonl")).read().splitlines() if x.strip()]
    assert len(rows) == 1 and rows[0]["link"] == creates(v)[0]["orderLinkId"] and rows[0]["qty"] == "0.002"
    # owner P&L from the venue's own rows
    rep = owner_pnl(w, tmp_path)
    cash = rep["layers"]["realised_cash"]
    assert rep["status"] == "RECONCILED"
    assert cash["figures"]["perp_realised_usd"] == pytest.approx(-2.0)
    assert cash["figures"]["perp_fees_usd"] == pytest.approx(-0.1111)
    assert cash["figures"]["funding_net_usd"] == pytest.approx(0.03)
    assert rep["headline_realised_cash_usd"] == pytest.approx(-2.0 - 0.1111 + 0.03) == pytest.approx(-2.0811)
    assert rep["layers"]["economic_total"]["net_economic_pnl_usd"] == pytest.approx(-2.0811 + 50.0)
    assert abs(rep["ledger"]["residuals"]["usdt"]) < 1e-6                      # the venue's closing wallet is explained to the cent
    # and every figure of that owner report carries lineage down to the venue's rows
    fees = rep["lineage"]["figures"]["layers.realised_cash.figures.perp_fees_usd"]
    assert [s["record_id"] for s in fees["sources"]] == ["exec:" + r["execId"] for r in v.executions] and fees["source_check"] == "MATCH"
    fund = rep["lineage"]["figures"]["layers.realised_cash.figures.funding_net_usd"]
    assert len(fund["sources"]) == 4 and fund["source_sum"] == pytest.approx(0.03)


# ================================================================================================
# 2. stale market data
# ================================================================================================

def test_stale_market_data_refuses_the_entry_with_zero_orders_and_a_fresh_print_lets_it_through(world, tmp_path):
    """The newest SETTLED funding print is B; the next tick is 14 h + 1 h later (> the 12 h bound). The ticker still answers (it always does), but a view built on a
    settlement that old is refused: not one /v5/order/create, the venue exactly as opened. CONTROL: after a settlement prints, the same bot enters."""
    w = world(mode="overlay")
    w.prime()
    approve(w, tmp_path)
    d = w.tick(14 * 3600)
    assert d["action"] == "skipped" and d["reason"] == "MARKET_VIEW_UNUSABLE", d
    untouched(w)
    assert refusal_reasons(w) == []                                           # refused by the engine, before the authority was ever asked
    w.venue.add_funding_print(0.0006, B + H8)                                 # the 8 h settlement that has since happened
    d = w.tick()
    assert d["action"] == "opened", d
    assert w.venue.requests_by_path[CREATE] == 1 and w.venue.net_position() == pytest.approx(-0.002)
    assert w.venue.wallet["USDT"] == pytest.approx(USDT0 - 0.002 * 50_000.0 * 0.00055)        # 0.055 fee, nothing else moved


# ================================================================================================
# 3. stale / unreadable / unmeasured ACCOUNT state
# ================================================================================================

@pytest.mark.parametrize("how,expected", [
    ("data_age_91s", "BREACH:max_data_age_s"),            # the measured account/market read is older than the policy's 90 s
    ("data_age_1h", "BREACH:max_data_age_s"),
    ("data_age_missing", "MISSING_INPUT:data_age_s"),     # not measured is not fresh
    ("nothing_measured", "MISSING_INPUT:trade_loss_usd"),
    ("provider_raises", "RISK_STATE_UNAVAILABLE"),         # the measurement itself cannot be read
])
def test_stale_or_unreadable_account_state_refuses_the_entry_and_sends_nothing(world, tmp_path, how, expected):
    w = world(mode="overlay")
    w.prime()
    if how == "data_age_91s":
        approve(w, tmp_path, state=dataclasses.replace(GOOD, data_age_s=91.0))
    elif how == "data_age_1h":
        approve(w, tmp_path, state=dataclasses.replace(GOOD, data_age_s=3600.0))
    elif how == "data_age_missing":
        approve(w, tmp_path, state=dataclasses.replace(GOOD, data_age_s=None))
    elif how == "nothing_measured":
        approve(w, tmp_path, state=RP.State())
    else:
        def unreadable():
            raise RuntimeError("wallet read failed")
        approve_with(w, tmp_path, unreadable)
    for _ in range(3):
        d = w.tick()
        assert d["action"] != "opened", d
    assert any(expected in r for r in refusal_reasons(w)), refusal_reasons(w)
    untouched(w)
    # CONTROL: a fresh, complete measurement on the same venue enters
    approve(w, tmp_path, state=dataclasses.replace(GOOD, data_age_s=90.0))
    assert w.tick()["action"] == "opened" and w.venue.requests_by_path[CREATE] == 1


@pytest.mark.parametrize("how", ["http_502", "no_accounts"])
def test_an_unreadable_venue_wallet_blocks_the_entry_with_zero_orders(world, tmp_path, how):
    """The venue's own wallet read fails (HTTP 502 / an empty account list) while the policy would allow the entry: the capital authority cannot size, so it stands
    aside. The venue sees wallet-balance requests and no create."""
    w = world(mode="overlay")
    w.prime()
    approve(w, tmp_path)
    if how == "http_502":
        w.venue.inject("/v5/account/wallet-balance", (502, "bad gateway"), times=200)
    else:
        w.venue.inject("/v5/account/wallet-balance", (200, json.dumps({"retCode": 0, "retMsg": "OK", "result": {"list": []}})), times=200)
    for _ in range(3):
        d = w.tick()
        assert d["action"] == "stand_aside" and d["reason"] == "PORTFOLIO_BLOCKED", d
    assert w.venue.requests_by_path["/v5/account/wallet-balance"] > 0
    untouched(w)


# ================================================================================================
# 4. incorrect fee tier, under the production authority
# ================================================================================================

@pytest.mark.parametrize("how", ["fee_row_for_another_symbol", "fee_in_percent", "fee_rates_zero"])
def test_a_wrong_fee_tier_answer_sends_nothing_even_with_an_approved_policy(world, tmp_path, how):
    """The fee tier is read once and priced into the round trip. An ETHUSDT row, a percent-scaled row (100x) and a row of zeros each leave the entry unpriced or
    unprofitable-looking: zero orders, venue untouched. (test_testnet_lifecycle_offline runs the first two WITHOUT the production authority; this is the same
    refusal with an approved policy and measured state that WOULD allow the order.)"""
    w = world(mode="overlay")
    w.boot()
    if how == "fee_row_for_another_symbol":
        w.venue.fee_row_symbol = "ETHUSDT"
    elif how == "fee_in_percent":
        w.venue.fee_scale = 100.0
    else:
        w.venue.fee_scale = 0.0
    approve(w, tmp_path)
    decisions = [w.tick() for _ in range(3)]
    for d in decisions:
        assert d["action"] in ("skipped", "stand_aside", "halted"), d
    untouched(w)
    assert refusal_reasons(w) == []                                           # refused before the authority was consulted: the price was never established


# ================================================================================================
# 5. incorrect funding
# ================================================================================================

@pytest.mark.parametrize("how,journal_funding,book_net,reason", [
    # the venue SERVES the first settlement (+0.06) with the wrong sign / x10; its wallet moved by the true +0.06
    ("sign_flipped", -0.06 - 0.03, -2.0811 - 0.12, "FUNDING_SIGN_CONTRADICTS_PUBLIC_RATE"),         # -0.06 -0.01*3 = -0.09 ; book net = -2.0811 + (-0.09 - 0.03)
    ("times_ten", 0.6 - 0.03, -2.0811 + 0.54, "FUNDING_MAGNITUDE_CONTRADICTS_PUBLIC_RATE"),          # +0.60 -0.03 = +0.57 ; book net = -2.0811 + (0.57 - 0.03)
])
def test_wrong_funding_leaves_reconciliation_provisional_and_the_owner_headline_null(world, tmp_path, how, journal_funding, book_net, reason):
    def tamper(rows):
        for r in rows:
            true = float(r["funding"])
            bad = -true if how == "sign_flipped" else true * 10
            r["funding"] = r["change"] = repr(bad)
    w = world(mode="overlay")
    run_cycle(w, tmp_path, tamper_first=tamper)
    v = w.venue
    # the venue's own money is the TRUE money: 10,000 - 2.0811, flat, two orders
    assert v.wallet["USDT"] == pytest.approx(USDT0 - 2.0811) and v.net_position() == 0.0 and v.requests_by_path[CREATE] == 2
    assert sum(float(r["funding"]) for r in v.settlements) == pytest.approx(journal_funding + 0.03 - 0.03 + (0.0))
    st = w.journal.statement()
    # the book followed the rows it was served ...
    assert st["funding_venue_usd"] == pytest.approx(journal_funding + 0.0)
    assert st["net_usd"] == pytest.approx(book_net) and st["net_usd"] != pytest.approx(v.wallet["USDT"] - USDT0)
    # ... and therefore refuses to call it reconciled
    assert st["funding_reconciled"] is False and st["funding_confirmed"] is False and st["net_basis"] == "provisional_funding_unverified"
    (lc,) = st["funding_lifecycles"]
    assert lc["state"] == "PROVISIONAL" and reason in lc["reasons"]
    # the owner P&L over the same rows: the contradiction is found independently, no headline
    rep = owner_pnl(w, tmp_path)
    assert rep["status"] == "DISPUTED" and rep["headline_realised_cash_usd"] is None
    assert rep["layers"]["realised_cash"]["status"] == "DISPUTED" and rep["layers"]["economic_total"]["net_economic_pnl_usd"] is None
    hard = {f["code"] for f in rep["flags"] if f["severity"] == "HARD"}
    assert ("FUNDING_SIGN_CONTRADICTION" if how == "sign_flipped" else "FUNDING_MAGNITUDE_IMPLAUSIBLE") in hard
    assert rep["lineage"]["figures"]["headline_realised_cash_usd"]["value"] is None and rep["lineage"]["figures"]["headline_realised_cash_usd"]["why"]


def test_a_correct_cycle_is_the_control_for_the_wrong_funding_cases(world, tmp_path):
    w = world(mode="overlay")
    run_cycle(w, tmp_path)
    st = w.journal.statement()
    assert st["funding_reconciled"] is True and st["funding_venue_usd"] == pytest.approx(0.06 - 0.03)
    assert owner_pnl(w, tmp_path)["headline_realised_cash_usd"] == pytest.approx(-2.0811)


# ================================================================================================
# 6, 7. restart with an unresolved position; recovery after a crash between the acknowledgement and the ledger write
# ================================================================================================

def _crash_after_ack(world, tmp_path):
    w = world(mode="overlay")
    w.venue.kill_at("create:after_accept", nth=1)
    w.prime()
    approve(w, tmp_path)
    with pytest.raises(VenueKill):
        w.tick()                                                              # the venue booked and filled the short; the process died before it could write the ledger
    v = w.venue
    assert v.net_position() == pytest.approx(-0.002) and v.requests_by_path[CREATE] == 1
    return w, v


def _reservations(w):
    path = os.path.join(os.path.dirname(w.path), "entry_reservations.jsonl")
    return [json.loads(x) for x in open(path).read().splitlines() if x.strip()]


def test_a_restart_with_an_unresolved_position_halts_sends_no_order_and_the_reservation_survives(world, tmp_path):
    w, v = _crash_after_ack(world, tmp_path)
    link = creates(v)[0]["orderLinkId"]
    before = _reservations(w)
    assert [(r["link"], r["resolved"]) for r in before] == [(link, False)]
    w.clock.advance(3600)
    w.boot()                                                                  # a new process: new store handle, client, bot - SAME state db, SAME authority directory
    approve(w, tmp_path)                                                      # a fresh production authority over the same directory, with a state that would ALLOW entries
    assert w.started is False and w.bot.carry.state is BookState.HALTED
    engaged, why = w.store.is_kill_switch_engaged()
    assert engaged is True and "never recorded" in why
    for _ in range(3):
        w.tick()
    assert v.requests_by_path[CREATE] == 1 and len(v.orders) == 1 and v.net_position() == pytest.approx(-0.002)       # nothing added, nothing flattened
    assert _reservations(w) == before                                         # the unresolved reservation was neither lost nor duplicated
    # even a direct entry through the client is refused by the authority (kill switch), and the venue does not change
    with pytest.raises(Exception, match="KILL_SWITCH"):
        w.bot.client._request("POST", CREATE, body={"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001",
                                                    "orderLinkId": "DIRECTAFTERCRASH1"}, signed=True)
    assert v.requests_by_path[CREATE] == 1 and v.net_position() == pytest.approx(-0.002)
    assert v.wallet["USDT"] == pytest.approx(USDT0 - 0.055)


def test_recovery_after_a_crash_between_ack_and_ledger_write_is_a_human_flatten_then_exactly_one_new_order(world, tmp_path):
    """After the halt a human closes the orphan short with a reduce-only buy (an exit is never gated), clears the switch with the operator acknowledgement, and the
    restarted book starts FLAT; its next entry is ONE new order on a NEW link (the orphan's reservation stays unresolved: it is history, not a lock).

      USDT = 10,000 - 0.055 (orphan entry) - 0.055 (human buy-back at the same 50,000, no P&L) - 0.055 (new entry) = 9,999.835
    """
    w, v = _crash_after_ack(world, tmp_path)
    orphan = creates(v)[0]["orderLinkId"]
    w.clock.advance(3600)
    w.boot()
    approve(w, tmp_path)
    assert w.started is False
    w.bot.client._request("POST", CREATE, body={"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "0.002", "reduceOnly": True,
                                                "orderLinkId": "HUMANFLATTEN1"}, signed=True)
    assert abs(v.net_position()) < 1e-12 and v.wallet["USDT"] == pytest.approx(USDT0 - 0.11)
    w.store.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")
    w.clock.advance(3600)
    w.boot()
    approve(w, tmp_path)
    assert w.started is True and w.bot.carry.state is BookState.FLAT and w.store.is_kill_switch_engaged()[0] is False
    d = w.tick()
    assert d["action"] == "opened", d
    links = [b["orderLinkId"] for b in creates(v)]
    assert len(links) == 3 and links[0] == orphan and links[1] == "HUMANFLATTEN1" and links[2] not in (orphan, "HUMANFLATTEN1")
    assert all(n == 1 for n in v.creates_by_link.values())
    assert v.net_position() == pytest.approx(-0.002) and v.wallet["USDT"] == pytest.approx(USDT0 - 0.165) == pytest.approx(9_999.835)
    assert [r["link"] for r in _reservations(w)] == [orphan, links[2]]


# ================================================================================================
# 8. a latch survives the restart
# ================================================================================================

def test_a_risk_latch_survives_the_restart_and_a_clean_measured_state_does_not_lift_it(world, tmp_path):
    w = world(mode="overlay")
    w.prime()
    approve(w, tmp_path)
    assert w.tick()["action"] == "opened" and w.bot.carry.state is BookState.HEDGED
    v = w.venue
    held = abs(v.net_position())
    approve(w, tmp_path, state=dataclasses.replace(GOOD, loss_today_usd=99.0))               # the daily loss limit (3.0) is breached
    entry = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "LATCHTEST1"}
    with pytest.raises(bc.PermanentAPIError, match="BREACH:daily_loss_usd"):
        w.bot.client._request("POST", CREATE, body=entry, signed=True)
    latch_path = os.path.join(os.path.dirname(w.path), "risk_latch.json")
    assert os.path.isfile(latch_path)
    # --- the process dies and a new one starts on the same db and the same directory
    w.clock.advance(60)
    w.boot()
    assert w.started is True and w.bot.carry.state is BookState.HEDGED                         # the ledger holds the pair and so does the venue: a clean restart
    approve(w, tmp_path)                                                                       # a CLEAN measured state, a brand-new authority object
    assert RP.RiskLatch(latch_path).is_suspended("btcusdt_funding_carry_overlay")[0] is True   # a third, independent reader of the file agrees
    with pytest.raises(bc.PermanentAPIError, match="LATCHED"):
        w.bot.client._request("POST", CREATE, body={**entry, "orderLinkId": "LATCHTEST2"}, signed=True)
    assert v.requests_by_path[CREATE] == 1 and abs(v.net_position()) == pytest.approx(held)     # neither refused entry reached the venue
    # the exit is never gated: reduce-only buy of the held 0.002 at the unchanged 50,000 -> flat; USDT = 10,000 - 0.055 - 0.055 (no P&L)
    w.bot.client._request("POST", CREATE, body={"category": "linear", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": f"{held:.3f}", "reduceOnly": True,
                                                "orderLinkId": "EXITAFTERLATCH1"}, signed=True)
    assert v.requests_by_path[CREATE] == 2 and abs(v.net_position()) < 1e-12 and v.wallet["USDT"] == pytest.approx(USDT0 - 0.11)


# ================================================================================================
# 9. a corrupted market-data backup
# ================================================================================================

def _flip(path):
    with open(path, "r+b") as fh:
        fh.seek(os.path.getsize(path) // 2)
        b = fh.read(8)
        fh.seek(-len(b), 1)
        fh.write(bytes(x ^ 0xFF for x in b))


def test_a_corrupted_market_data_backup_fails_restore_check_while_the_original_verifies(tmp_path):
    md = make_store(tmp_path / "md", per_day=(10, 4))
    manifest = str(tmp_path / "off" / "manifest.jsonl")
    os.makedirs(os.path.dirname(manifest))
    SL.seal(md, manifest, now_ms=NOW)
    dest, rec = str(tmp_path / "offvol"), str(tmp_path / "off" / "receipts.jsonl")
    res = SL.backup(md, dest, rec, manifest=manifest, now_ms=NOW)
    assert [d["records"] for d in res["backed_up"]] == [10]                                   # day 1 (10 records) is closed; day 2 is still being written
    assert SL.restore_check(dest, rec, manifest) == {"ok": True, "problems": [], "checked": 1}
    # two corrupted COPIES of the good backup; the receipts are shared
    flipped, cut = str(tmp_path / "copy_flipped"), str(tmp_path / "copy_cut")
    shutil.copytree(dest, flipped)
    shutil.copytree(dest, cut)
    name = SL.segment_names(dest)[0]
    _flip(os.path.join(flipped, name))
    bad = SL.restore_check(flipped, rec, manifest)
    assert bad["ok"] is False and any("hash differs from its receipt" in p or "chain" in p for p in bad["problems"])
    os.remove(os.path.join(cut, name))
    gone = SL.restore_check(cut, rec, manifest)
    assert gone["ok"] is False and any("missing from the backup" in p for p in gone["problems"])
    # the ORIGINAL store and the intact backup are unaffected and still verify, deeply
    chk = SL.check(md, manifest, deep=True)
    assert chk["ok"] and chk["problems"] == []
    assert SL.restore_check(dest, rec, manifest)["ok"] is True
    # a flipped ORIGINAL day is never "ok". DEFECT REPORTED (not fixed here): ``md_seal.check(deep=True)`` lets sqlite3.DatabaseError ("database disk image is
    # malformed") escape as a traceback instead of returning {"ok": False, "problems": [...]}; it fails closed, but a caller that does not expect the exception crashes.
    _flip(os.path.join(md, name))
    try:
        still_ok = SL.check(md, manifest, deep=True)["ok"]
    except sqlite3.DatabaseError:
        still_ok = False
    assert still_ok is False


# ================================================================================================
# 10. a corrupted state-db backup
# ================================================================================================

def test_a_corrupted_state_db_backup_fails_verify_and_promote_and_restore_refuse_it(world, tmp_path):
    w = world(mode="overlay")
    w.prime()
    approve(w, tmp_path)
    assert w.tick()["action"] == "opened"                                      # the db now holds a real journal
    out = str(tmp_path / "backups")
    manifest = SB.backup(w.path, out)
    good = os.path.join(out, manifest["backup_file"])
    assert manifest["integrity_ok"] is True and manifest["tables"]
    assert SB.verify(good)["ok"] is True
    live = tmp_path / "live" / "live.db"
    live.parent.mkdir()
    shutil.copyfile(good, live)
    live_sha = hashlib.sha256(live.read_bytes()).hexdigest()
    # (a) a bit-flipped copy, manifest untouched
    bad = os.path.join(out, "bad-flipped.db")
    shutil.copyfile(good, bad)
    shutil.copyfile(good + SB.MANIFEST_SUFFIX, bad + SB.MANIFEST_SUFFIX)
    _flip(bad)
    v = SB.verify(bad)
    assert v["ok"] is False and any("SHA-256 differs" in p or "integrity" in p or "not a readable" in p for p in v["problems"])
    # (b) a truncated copy whose manifest hash was RE-SEALED over the damage: sha agrees, the database itself does not
    cut = os.path.join(out, "bad-cut.db")
    data = open(good, "rb").read()
    open(cut, "wb").write(data[: len(data) // 2])
    m = json.load(open(good + SB.MANIFEST_SUFFIX))
    m["sha256"], m["bytes"] = hashlib.sha256(open(cut, "rb").read()).hexdigest(), os.path.getsize(cut)
    json.dump(m, open(cut + SB.MANIFEST_SUFFIX, "w"))
    v2 = SB.verify(cut)
    assert v2["ok"] is False and not any("SHA-256 differs" in p for p in v2["problems"])      # caught by the database's own integrity / counts, not by the hash
    for badfile in (bad, cut):
        with pytest.raises(SystemExit, match="does not verify"):
            SB.restore(badfile, str(tmp_path / ("restore_" + os.path.basename(badfile))))
        assert not os.path.exists(str(tmp_path / ("restore_" + os.path.basename(badfile))))
        with pytest.raises(SystemExit, match="does not verify"):
            SB.promote(badfile, str(live), confirm_owner=True)
    assert hashlib.sha256(live.read_bytes()).hexdigest() == live_sha                         # the live file was never touched
    assert sorted(os.listdir(live.parent)) == ["live.db"]                                   # and no ".replaced" / staging debris
    # the ORIGINAL (live) database still passes its own integrity check and the good backup still verifies
    con = sqlite3.connect(f"file:{w.path}?mode=ro", uri=True)
    try:
        assert [r[0] for r in con.execute("PRAGMA integrity_check")] == ["ok"]
    finally:
        con.close()
    assert SB.verify(good)["ok"] is True
