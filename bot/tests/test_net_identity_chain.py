"""ONE meaning of money, end to end. Known inputs are carried through every module that states a net and the numbers must agree EXACTLY (Decimal; floats only at a module's own boundary, compared at 1e-9):

    fills + funding  ->  ms_accounting.Ledger.canonical_net  ==  ms_replay.LocalLedger  ==  ms_pipeline.LedgerAdapter           (accounting: three ledgers, one definition)
    the same session on the venue  ->  record net (price, fees, funding)  ==  the MEASURED execution costs                       (execution quality ties to accounting)
    ... ==  the wallet's USDT change                                                                                              (cash is the arbiter)
    ... ==  what the capital book credits                                                                                         (capital: only what cash explains)

Plus property and mutation tests for signs, duplicates, missing events, fees and cash transfers: a wrong sign, a double count or a missing record must change the answer or make it UNKNOWN, never pass."""
import os
import random
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import ms_accounting as ACC  # noqa: E402
import ms_capital as CAP  # noqa: E402
import ms_pipeline as PIPE  # noqa: E402
import ms_replay as RP  # noqa: E402
import ms_types as T  # noqa: E402
from ms_testnet_world import World  # noqa: E402
from test_funding_settlement import _held_session  # noqa: E402

T0 = 1_794_312_000_000
EPS = 1e-9


def F(fid, sym, side, qty, px, fee, cat_leg, ts):
    return T.Fill(fid, "G", cat_leg, sym, side, float(qty), float(px), float(fee), "USDT", "TAKER", ts, "MEASURED")


# one hedged round trip with known numbers (all exactly representable in Decimal, and every product exact in binary-free arithmetic)
QTY = D("0.5")
PERP_IN, SPOT_IN, PERP_OUT, SPOT_OUT = D("60000.0"), D("60001.0"), D("60001.0"), D("59999.0")
FEE = {"p_in": D("16.5"), "s_in": D("30.0003"), "p_out": D("16.50027"), "s_out": D("29.99995")}           # arbitrary exact fees in USD
FUNDING = D("3.0")


def known_net():
    price = QTY * (PERP_IN - PERP_OUT) + QTY * (SPOT_OUT - SPOT_IN)                  # short perp: sold high, bought back; long spot: bought, sold back
    return price, sum(FEE.values()), FUNDING, price + FUNDING - sum(FEE.values())


def feed(ledger, *, funding=True):
    ledger.record_fill(F("f1", "BTCUSDT", "Sell", QTY, PERP_IN, FEE["p_in"], "P", T0), category="linear")
    ledger.record_fill(F("f2", "BTCUSDT", "Buy", QTY, SPOT_IN, FEE["s_in"], "S", T0 + 1), category="spot")
    ledger.record_fill(F("f3", "BTCUSDT", "Buy", QTY, PERP_OUT, FEE["p_out"], "P", T0 + 9_000), category="linear")
    ledger.record_fill(F("f4", "BTCUSDT", "Sell", QTY, SPOT_OUT, FEE["s_out"], "S", T0 + 9_001), category="spot")
    if funding:
        ledger.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING), "MEASURED")


# ------------------------------------------------------------------ the three ledgers, one definition
def test_the_canonical_ledger_the_replay_ledger_and_the_pipeline_adapter_give_the_exact_same_net():
    price, fees, funding, net = known_net()
    led = ACC.Ledger()
    feed(led)
    c = led.canonical_net()
    assert c["net"] == pytest.approx(float(net), abs=EPS)
    assert c["price_leg"] == pytest.approx(float(price), abs=EPS) and c["fees"] == pytest.approx(float(fees), abs=EPS) and c["funding"] == pytest.approx(float(funding), abs=EPS)
    ex = led.pnl_exact()                                                               # the exact Decimal figures behind it
    assert abs(ex["net"] - (price + funding - fees)) < D("1e-12")

    rep = RP.LocalLedger()
    rep.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING), "MEASURED")
    feed_cat = lambda l: [l.record_fill(F(fid, "BTCUSDT", side, QTY, px, fee, leg, ts), category=cat) for fid, side, px, fee, leg, cat, ts in (        # noqa: E731
        ("f1", "Sell", PERP_IN, FEE["p_in"], "P", "linear", T0), ("f2", "Buy", SPOT_IN, FEE["s_in"], "S", "spot", T0 + 1),
        ("f3", "Buy", PERP_OUT, FEE["p_out"], "P", "linear", T0 + 9_000), ("f4", "Sell", SPOT_OUT, FEE["s_out"], "S", "spot", T0 + 9_001))]
    feed_cat(rep)
    rp = rep.pnl()
    assert rp["net"] == pytest.approx(float(net), abs=EPS) and rp["fees"] == pytest.approx(float(fees), abs=EPS)
    RP.assert_net_identity(rp)

    ad = PIPE.LedgerAdapter()
    ad.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING), "MEASURED")
    feed_cat(ad)
    ap = ad.pnl()
    assert ap["net"] == pytest.approx(float(net), abs=EPS) and ap["net_complete"] is True
    assert c["net"] == pytest.approx(rp["net"], abs=EPS) == pytest.approx(ap["net"], abs=EPS)


def test_the_taker_walk_and_adverse_selection_are_charged_exactly_once_in_every_ledger():
    """The fill prices already contain a 2.00 taker walk; the identity adds it back to the price leg at mid and charges it once as slippage, so it nets to zero; adverse selection (in no fill price)
    is the only extra deduction."""
    _p, _f, _fu, net = known_net()
    walk, adverse = D("2.0"), D("0.75")
    led = ACC.Ledger()
    feed(led)
    led.record_slippage(float(walk), "taker", "BTCUSDT")
    led.record_slippage(float(adverse), "adverse", "BTCUSDT")
    assert led.canonical_net()["net"] == pytest.approx(float(net - adverse), abs=EPS)
    ad = PIPE.LedgerAdapter()
    ad.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING), "MEASURED")
    for fid, side, px, fee, leg, cat, ts in (("f1", "Sell", PERP_IN, FEE["p_in"], "P", "linear", T0), ("f2", "Buy", SPOT_IN, FEE["s_in"], "S", "spot", T0 + 1),
                                             ("f3", "Buy", PERP_OUT, FEE["p_out"], "P", "linear", T0 + 9_000), ("f4", "Sell", SPOT_OUT, FEE["s_out"], "S", "spot", T0 + 9_001)):
        ad.record_fill(F(fid, "BTCUSDT", side, QTY, px, fee, leg, ts), category=cat)
    ad.record_slippage(float(walk), "taker", "BTCUSDT")
    ad.record_slippage(float(adverse), "adverse", "BTCUSDT")
    assert ad.pnl()["net"] == pytest.approx(float(net - adverse), abs=EPS)


# ------------------------------------------------------------------ properties and mutations
def test_duplicates_never_change_the_net_and_a_conflicting_duplicate_is_flagged():
    a, b = ACC.Ledger(), ACC.Ledger()
    feed(a)
    feed(b)
    feed(b)                                                                            # every fill and the funding delivered twice
    assert a.canonical_net()["net"] == b.canonical_net()["net"]
    b.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING) + 1, "MEASURED")             # same settlement, another amount
    assert any(f.startswith("FUNDING_CONFLICT") for f in b.canonical_net()["flags"]) and b.canonical_net()["net"] == a.canonical_net()["net"]


def test_event_order_does_not_change_the_net_property():
    rng = random.Random(20261111)
    base = ACC.Ledger()
    feed(base)
    want = base.canonical_net()["net"]
    fills = [("f1", "Sell", PERP_IN, FEE["p_in"], "P", "linear", T0), ("f2", "Buy", SPOT_IN, FEE["s_in"], "S", "spot", T0 + 1),
             ("f3", "Buy", PERP_OUT, FEE["p_out"], "P", "linear", T0 + 9_000), ("f4", "Sell", SPOT_OUT, FEE["s_out"], "S", "spot", T0 + 9_001)]
    for _ in range(40):
        rng.shuffle(fills)
        led = ACC.Ledger()
        led.record_funding("BTCUSDT", T0 + 4_000, float(FUNDING), "MEASURED")
        for fid, side, px, fee, leg, cat, ts in fills:
            led.record_fill(F(fid, "BTCUSDT", side, QTY, px, fee, leg, ts), category=cat)
        assert led.canonical_net()["net"] == pytest.approx(want, abs=EPS)


def test_a_missing_funding_record_is_unknown_never_zero_and_a_declared_none_is_zero():
    led = ACC.Ledger()
    feed(led, funding=False)
    assert led.canonical_net()["net"] is None and led.canonical_net()["funding"] is None
    led.declare_no_funding("BTCUSDT", "held between two prints", "DERIVED")
    _p, fees, _fu, _n = known_net()
    price, _, _, _ = known_net()
    assert led.canonical_net()["net"] == pytest.approx(float(price - fees), abs=EPS)
    led2 = ACC.Ledger()
    feed(led2, funding=False)
    led2.record_funding("BTCUSDT", T0 + 4_000, None, "UNKNOWN")                          # the print happened, the amount is unknown
    assert led2.canonical_net()["net"] is None


def test_mutations_a_wrong_sign_a_double_count_or_a_lost_fee_all_change_the_answer():
    price, fees, funding, net = known_net()
    base = ACC.Ledger()
    feed(base)
    assert base.canonical_net()["net"] == pytest.approx(float(net), abs=EPS)
    # sign of funding
    neg = ACC.Ledger()
    feed(neg, funding=False)
    neg.record_funding("BTCUSDT", T0 + 4_000, -float(FUNDING), "MEASURED")
    assert neg.canonical_net()["net"] == pytest.approx(float(net - 2 * funding), abs=EPS)
    # a fee that is NOT deducted (the classic omission) would read higher by exactly the fees
    assert float(net + fees) != pytest.approx(base.canonical_net()["net"], abs=1e-6)
    # a fee that is deducted twice
    dbl = ACC.Ledger()
    feed(dbl)
    dbl.record_fill(F("f5", "BTCUSDT", "Buy", D("0.0000001"), PERP_OUT, FEE["p_in"], "P", T0 + 9_500), category="linear")
    assert dbl.canonical_net()["net"] != pytest.approx(base.canonical_net()["net"], abs=1e-3)


# ------------------------------------------------------------------ the session, measured execution, wallet cash and the capital book
@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_measured_execution_costs_accounting_wallet_cash_and_capital_credit_are_one_number(w, tmp_path):
    import json
    wallet_before = w.fake.wallet["USDT"]
    run, rec = _held_session(w, rate="0.0005")                                         # a held hedge across one settlement; the venue pays it into the wallet
    net = rec["net"]
    quality = json.load(open(os.path.join(w.state_dir, "execution_quality.json")))
    legs = quality["legs"]
    # 1. the measured execution costs ARE the ledger's price leg and fees (a constant book: mid-to-mid P&L is zero, so the price leg is exactly minus the cost against the mid)
    assert D(str(net["fees_usd"])).quantize(D("0.00000001")) == sum(D(l["cost"]["fee_usd"]) for l in legs).quantize(D("0.00000001"))
    assert D(str(net["price_pnl_usd"])).quantize(D("0.0001")) == (-sum(D(l["cost"]["price_vs_mid_usd"]) for l in legs)).quantize(D("0.0001"))
    # 2. the venue's own cash agrees: wallet change = price leg - fees + funding
    wallet_after = w.fake.wallet["USDT"]
    assert net["funding_usd"] is not None and net["funding_usd"] > 0
    assert abs(float(wallet_after - wallet_before) - (net["price_pnl_usd"] - net["fees_usd"] + net["funding_usd"])) < 1e-7
    # 3. the capital book credits exactly that, because cash explains it
    known = D(str(net["price_pnl_usd"])) - D(str(net["fees_usd"])) + D(str(net["funding_usd"]))
    book = CAP.CapitalBook(str(tmp_path / "cap"))
    book.ensure_baseline(str(wallet_before))
    assert book.park("s1", str(wallet_before), str(known), True, True)
    r = book.settle(str(wallet_after))
    assert r["status"] == "SETTLED", r
    assert abs(D(r["delta_usd"]) - (wallet_after - wallet_before)) < D("1e-7") and abs(D(r["delta_usd"]) - known) < D("1e-7")      # cash change == ledger net, to the 7th decimal
    assert D(r["credited_usd"]) == D(r["delta_usd"]) if D(r["delta_usd"]) < 0 else abs(D(r["credited_usd"]) - known) < D("1e-7")      # a loss is booked in full; a gain only as far as cash AND the ledger agree


def test_a_ledger_that_omits_funding_that_cash_shows_is_not_credited_as_if_it_were_explained(tmp_path):
    book = CAP.CapitalBook(str(tmp_path))
    book.ensure_baseline("10000")
    book.park("s1", "10000", "10.00", False, True)                                      # funding was never read: the ledger explains only 10.00
    r = book.settle("10013.00")                                                         # the wallet gained 13.00
    assert D(r["credited_usd"]) == D("10.00") and D(r["unverified_excess_usd"]) == D("3.00")          # the unexplained 3.00 is never credited
    book2 = CAP.CapitalBook(str(tmp_path / "b2"))
    book2.ensure_baseline("10000")
    book2.park("s1", "10000", "10.00", True, True)                                      # funding WAS read and the ledger says 10.00, but cash says 13.00: that is a contradiction, not a profit
    assert book2.settle("10013.00")["status"] == "UNEXPLAINED_CASH"


def test_a_declared_deposit_is_never_profit_and_a_withdrawal_never_a_loss(tmp_path, monkeypatch):
    import time as _t
    clock = {"t": 1000.0}
    monkeypatch.setattr(_t, "time", lambda: clock["t"])
    book = CAP.CapitalBook(str(tmp_path))
    book.ensure_baseline("10000")
    book.park("s1", "10000", "5.00", True, True, cash_read_ms=1_000_000)
    clock["t"] = 1500.0
    book.declare_transfer("dep", "250")                                                  # a deposit during the session
    r = book.settle("10255.00")                                                          # 5.00 of trading plus the 250 deposit
    assert D(r["delta_usd"]) == D("5.00") and D(r["transfers_removed_usd"]) == D("250")
