"""One money identity, checked against an INDEPENDENT reference (round 10, priority three).

The reference below is written from first principles from the VENUE's raw execution rows and imports no production pricer, ledger or fee parser. For a flat round trip it computes what the venue wallet MUST have
done (cash, and the base coin a fee taken in kind removed) and the tests compare the production session record with the fake venue's real wallet, to the cent and to 1e-6:

  * the production net (price leg - fees + funding) equals the wallet's USDT change, with the fee in USDT AND with the fee taken from the received coin;
  * a published component is either UNKNOWN or equal to what the reference says ("unknown is fine, wrong is not"): a spot fee with no currency stated, a rebate, a third-currency fee and a corrupt execution
    row each end as UNKNOWN, never as a measured number that the wallet does not bear out.

Reproduced first by the round-10 execution specialist on a scratch copy (spec_probe_money.py: M1 blank currency read as USDT and MEASURED, understated about 60,000 times; M2 a rebate clamped to a MEASURED zero; C1 one
zero-quantity row raised out of the session loop with the hedge open; the fuzz found 438 of 3,000 rows with a MEASURED fee and no currency stated).
"""
from __future__ import annotations

import os
import random
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_accounting as AC  # noqa: E402
import ms_testnet_session as S  # noqa: E402
from ms_testnet_world import World, begin  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.ready()
    return world


def drive(w, run, n=6):
    for _ in range(n):
        w.clock.sleep(3)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
    return run.state()


def reference_from_raw_rows(execs, base_coin="BTC"):
    """(USDT change, base-coin change) a flat round trip must have made, from the venue's raw rows only (Decimal). Spot BUY: cash -= qty*px, the base coin received is qty (less a fee taken in kind); spot SELL: cash += qty*px;
    perp: cash -= signed(qty*px) (a flat round trip is its realised result); every fee in USDT is cash; a Funding row's cash is -execFee (the venue's own convention for a Trade row)."""
    usdt, base = D(0), D(0)
    for e in execs:
        qty, px, fee = D(e["execQty"]), D(e["execPrice"]), D(e["execFee"])
        sgn = 1 if e["side"] == "Buy" else -1
        if e["execType"] == "Funding":
            usdt += -fee
            continue
        usdt -= sgn * qty * px
        if e["category"] == "spot":
            base += sgn * qty
            if e["feeCurrency"] == "USDT":
                usdt -= fee
            elif e["feeCurrency"] == base_coin:
                base -= fee
        else:
            usdt -= fee
    return usdt, base


# ------------------------------------------------------------------------------------------------ the identity holds today: pinned so it can never regress silently
@pytest.mark.parametrize("fee_in_kind", [False, True])
def test_the_published_net_equals_the_venue_wallets_change_for_a_flat_round_trip(w, fee_in_kind):
    w.fake.spot_fee_in_base = fee_in_kind
    w.fake.funding_rate = "0.0080"
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "600") == 0, w.text()
    d = w.plan_doc()["plan_digest"]
    usdt0, btc0 = w.fake.wallet["USDT"], w.fake.wallet["BTC"]
    start, state, real = w.clock.now(), {"done": False}, w.clock.sleep

    def sleep(s):
        real(s)
        if not state["done"] and w.clock.now() >= start + 300_000 and any(p["size"] != 0 for p in w.fake.positions.values()):
            w.fake.settle_funding("BTCUSDT", "0.0080", ts_ms=w.clock.now())          # one settlement while the pair is held
            state["done"] = True
    w.clock.sleep = sleep
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True
    ref_usdt, ref_base = reference_from_raw_rows(w.fake.execs)
    assert ref_usdt == w.fake.wallet["USDT"] - usdt0 and ref_base == w.fake.wallet["BTC"] - btc0, "the reference IS the venue's wallet: an exact identity"
    n = rec["net"]
    assert None not in (n["price_pnl_usd"], n["fees_usd"], n["funding_usd"]), n
    net = D(str(n["price_pnl_usd"])) - D(str(n["fees_usd"])) + D(str(n["funding_usd"]))
    assert abs(net - (w.fake.wallet["USDT"] - usdt0)) < D("1e-6"), (net, w.fake.wallet["USDT"] - usdt0)


# ------------------------------------------------------------------------------------------------ a published fee is MEASURED only when its currency is KNOWN
def _blank_currency_on_spot_buys(w):
    orig = w.fake._fill

    def fill(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["category"] == "spot" and o["side"] == "Buy":
            w.fake.execs[-1]["feeCurrency"] = ""                                      # the row states no currency
    w.fake._fill = fill


def test_a_spot_fee_with_no_currency_stated_is_unknown_not_a_measured_quote_fee(w):
    """M1: the venue took 0.001 of the 0.001 BTC received (0.06 USD); the row says no currency; reading it as USDT booked 1e-6 USD, MEASURED, about 60,000 times too small"""
    w.fake.spot_fee_in_base = True
    _blank_currency_on_spot_buys(w)
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    x = [e for e in run.ex.venue_state()["executions"] if e["category"] == "spot" and e["side"] == "Buy"][0]
    assert x["fee_known"] is False and x["prov"] == "UNKNOWN" and x["fee_signed_usd"] is None, {k: x[k] for k in ("fee_usd", "fee_known", "prov", "fee_ccy_venue")}


def test_a_perp_fee_with_no_currency_stated_is_the_contracts_settle_coin(w):
    """a USDT-settled linear contract's fee is paid in USDT by definition: a missing field there is not an unknown currency (it would turn every perp fee unknown if the venue omits the field)"""
    orig = w.fake._fill

    def fill(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["category"] == "linear":
            w.fake.execs[-1]["feeCurrency"] = ""
    w.fake._fill = fill
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    x = [e for e in run.ex.venue_state()["executions"] if e["category"] == "linear"][0]
    assert x["fee_known"] is True and x["prov"] == "MEASURED" and x["fee_usd"] > 0


def test_a_session_with_a_blank_spot_fee_currency_publishes_unknown_fees_never_a_wrong_number(w):
    w.fake.spot_fee_in_base = True
    _blank_currency_on_spot_buys(w)
    d = w.plan_doc()["plan_digest"]
    w.run("execute", "--confirm-plan-digest", d, typed=d)
    n = w.record()["net"]
    assert n["fees_usd"] is None, "an unknown fee makes the total unknown: it must not publish the understated figure"


def test_a_rebate_is_an_unknown_fee_everywhere_not_a_measured_zero_in_the_ledger_and_a_cost_in_the_quality_record(w):
    """M2: the executor clamped a negative fee to a MEASURED zero (the ledger booked no rebate flag) while the quality record used the signed figure: two answers to one question"""
    w.fake.taker_rate = D("-0.0001")
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    xs = run.ex.venue_state()["executions"]
    assert xs and all(e["fee_known"] is False and e["prov"] == "UNKNOWN" and e["fee_signed_usd"] is None for e in xs), [(e["category"], e["fee_usd"], e["fee_known"], e["prov"]) for e in xs]
    run.feed_ledger()
    p = run.ledger.pnl()
    assert p["fees"] is None and "FEES_UNKNOWN" in p["flags"] and not any("REBATE" in f for f in p["flags"] if f.startswith("FEE_REBATE")), p["flags"]


def test_a_corrupt_but_parsable_execution_row_does_not_kill_the_session_with_the_hedge_open(w):
    """C1: one row with a zero quantity raised LedgerError out of the step, out of the loop, with the perp still open. It is set aside (surfaced as unattributed) and the session goes on unwinding."""
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    assert run.state() == "HEDGED"
    w.fake.execs.append({"execId": "E999001", "orderId": "O9", "orderLinkId": f"{run.gid}-p", "symbol": "BTCUSDT", "category": "linear", "side": "Sell", "execQty": "0", "execPrice": "60000",
                         "execFee": "0", "feeCurrency": "USDT", "isMaker": False, "execTime": str(w.clock.now()), "execType": "Trade"})
    S._step_once(ctx, run, rec)                                                   # must not raise
    assert any(u["exec_id"] == "E999001" for u in run.ex.unattributed), "the corrupt row is kept as evidence, not dropped silently"
    run.feed_ledger()
    assert run.ledger.pnl()["fees"] is not None, "the corrupt row changed nothing the ledger holds: the real fills' fees are still a known number"


# ------------------------------------------------------------------------------------------------ boundary fuzz: invariants of the parser, not a count of findings
def test_the_execution_row_parser_never_publishes_a_measured_fee_it_cannot_justify(w):
    run, ctx, rec = begin(w, submit=False)
    ex = run.ex
    rng = random.Random(11)
    ccys = ["USDT", "usdt", " USDT ", "BTC", "btc", "MNT", "USDC", "", None]
    fees = ["0", "0.001", "-0.002", "", None, "0.000001"]
    qtys = ["0.001", "0.003", "0", "-0.001"]
    checked = 0
    for i in range(3000):
        cat, side = rng.choice(["spot", "linear"]), rng.choice(["Buy", "Sell"])
        row = {"execId": f"Z{i}", "orderLinkId": f"{run.gid}-s", "orderId": "O", "symbol": "BTCUSDT", "side": side, "execQty": rng.choice(qtys), "execPrice": "60000", "execFee": rng.choice(fees),
               "feeCurrency": rng.choice(ccys), "isMaker": False, "execTime": str(w.clock.now()), "execType": "Trade"}
        row = {k: v for k, v in row.items() if v is not None}
        out = []
        ex._take_exec(row, cat, out)                                              # must never raise
        for x in out:
            checked += 1
            ccy = str(row.get("feeCurrency") or "").strip().upper()
            assert x["qty"] > 0, "a non-positive quantity is set aside, never passed on"
            if x["fee_known"]:
                assert x["prov"] == "MEASURED" and x["fee_usd"] >= 0 and x["fee_signed_usd"] is not None and x["fee_signed_usd"] >= 0, (row, x)
                assert not str(row.get("execFee", "")).startswith("-"), ("a rebate was published as a measured fee", row, x)
                assert ccy in ("USDT", "BTC") or (ccy == "" and cat == "linear"), ("a measured fee in a currency that is neither the quote, the base coin nor a contract's settle coin", row, x)
                assert not (ccy == "" and cat == "spot"), ("a spot fee with no currency stated was published as measured", row, x)
    assert checked > 500
