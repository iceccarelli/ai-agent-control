"""One symbol decides the carry book: the one it TRADES.

`main.tick` used to read the perp mark, funding print and margin for
`TRADING_SYMBOLS[0]` while `CarryEngine` placed orders on `CARRY_PERP_SYMBOL`.
Config accepts the two independently, so a book could size, gate and mark
against one symbol's data and send orders on another. Offline and synthetic:
the broker below records which symbol every read named and has no order methods.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402

H8 = 8 * 3600 * 1000


class Recorder:
    def __init__(self):
        self.reads = []
        self.stamp = int(time.time() * 1000) // H8 * H8

    def _r(self, name, symbol):
        self.reads.append((name, symbol))

    def get_mark(self, s): self._r("mark", s); return 100_000.0
    def get_spot_mark(self, s): self._r("spot", s); return 100_000.0
    def get_funding_bps(self, s): self._r("funding", s); return 0.3
    def get_funding_print(self, s): self._r("print", s); return (0.3, self.stamp)
    def get_perp_position(self, s): self._r("position", s); return 0.0
    def get_margin_multiple(self, s): self._r("margin", s); return 5.0
    def get_lot_rules(self, s, p): return {"qty_step": 1e-6, "min_qty": 1e-6,
                                           "min_notional": 0.0}
    def get_fee_rates(self, s, p): return {"maker_bps": 2.0,
                                           "taker_bps": 10.0 if p == "spot" else 5.5}


def bot(trading_symbols, perp, spot="BTCUSDT"):
    b = _main.TradingBot.__new__(_main.TradingBot)
    b.symbols = list(trading_symbols)
    b.ws_consumer = None
    b.store = mock.Mock(is_kill_switch_engaged=lambda: (False, ""))
    b.strategy = None
    b._stop = mock.Mock(is_set=lambda: False)
    b.session = mock.Mock()
    b.client = mock.Mock(get_equity=lambda: 1_000_000.0)
    b.risk = mock.Mock(should_halt_trading=lambda: False,
                       update_equity=lambda e: None)
    b.engine = mock.Mock(observe_exits=lambda: {})
    rec = Recorder()
    b.carry = CarryEngine(broker=rec, max_notional_usd=100.0, borrow_apr=0.0,
                          execution_mode="acquire", persist=lambda s: None,
                          perp_symbol=perp, spot_symbol=spot)
    b.carry.pair_risk = CarryRisk(max_notional_usd=100.0)
    return b, rec


def perp_reads(rec):
    return {s for n, s in rec.reads if n in ("mark", "funding", "print",
                                             "position", "margin")}


def test_tick_reads_the_symbol_the_book_trades_not_trading_symbols_0():
    b, rec = bot(["ETHUSDT"], perp="BTCUSDT")
    b.tick()
    assert perp_reads(rec) == {"BTCUSDT"}, rec.reads


def test_the_reverse_mismatch_is_also_read_for_the_traded_symbol():
    # The traded symbol is read, not TRADING_SYMBOLS[0]. The spot leg is the
    # same asset: a perp/spot pair on different assets is refused before any
    # venue read (market_snapshot.assert_symbols_map), tested below.
    b, rec = bot(["BTCUSDT"], perp="ETHUSDT", spot="ETHUSDT")
    b.tick()
    assert perp_reads(rec) == {"ETHUSDT"}, rec.reads


def test_a_perp_and_spot_on_different_assets_read_nothing():
    b, rec = bot(["BTCUSDT"], perp="ETHUSDT")
    b.tick()
    assert perp_reads(rec) == set(), rec.reads


def test_default_configuration_is_unchanged():
    b, rec = bot(["BTCUSDT"], perp="BTCUSDT")
    b.tick()
    assert perp_reads(rec) == {"BTCUSDT"}


def test_spot_and_perp_on_different_assets_never_enter():
    # A BTC perp against an ETH spot reads as a ~-97% "basis", which the basis
    # gate would wave through as cheap. Not a hedge: refused before any order.
    b, rec = bot(["BTCUSDT"], perp="BTCUSDT", spot="ETHUSDT")
    for k in (3, 2, 1):                      # enough settled history to qualify
        b.carry.on_candle(mark=100_000.0, funding_bps=5.0, spot=3_000.0,
                          funding_print_ms=rec.stamp - k * H8)
    d = b.carry.on_candle(mark=100_000.0, funding_bps=5.0, spot=3_000.0,
                          funding_print_ms=rec.stamp)
    assert d.action == "stand_aside"
    assert d.reason == "PAIR_SYMBOLS_MISMATCH"
    assert b.carry.position is None


def test_matching_symbols_are_not_refused_for_pairing():
    b, rec = bot(["BTCUSDT"], perp="BTCUSDT", spot="BTCUSDT")
    d = b.carry.on_candle(mark=100_000.0, funding_bps=5.0, spot=100_000.0,
                          funding_print_ms=rec.stamp)
    assert d.reason != "PAIR_SYMBOLS_MISMATCH"


# -- refusing NEW exposure never disables an exit -----------------------------

class FillingVenue(Recorder):
    def __init__(self):
        super().__init__()
        self.orders = []

    held = 0.001                       # the venue's perp short, as it reads

    def get_perp_position(self, s):
        self._r("position", s)
        return -self.held

    def place_market(self, *, symbol, side, qty, product):
        if product == "linear" and side == "Buy":
            self.held = max(0.0, self.held - qty)
        self.orders.append((symbol, side, product))
        return {"filled_qty": qty, "avg_price": 100_000.0,
                "order_link_id": f"x{len(self.orders)}"}


def _held(perp, spot):
    from dataclasses import asdict
    from carry_engine import Leg
    e = CarryEngine(broker=FillingVenue(), max_notional_usd=100.0,
                    borrow_apr=0.0, execution_mode="acquire",
                    persist=lambda s: None, perp_symbol=perp, spot_symbol=spot)
    e.restore({
        "perp_symbol": perp, "last_print_ms": 0, "funding_history": [],
        "position": {
            "spot": asdict(Leg(symbol=spot, side="Buy", product="spot",
                               requested_qty=0.001, filled_qty=0.001,
                               avg_price=100_000.0)),
            "perp": asdict(Leg(symbol=perp, side="Sell", product="linear",
                               requested_qty=0.001, filled_qty=0.001,
                               avg_price=100_000.0)),
            "opened_ms": 1, "funding_collected": 0.0,
            "negative_funding_streak": 0}})
    return e


def test_a_held_pair_still_unwinds_when_cadence_and_pairing_refuse_entry():
    for perp, spot in (("ETHUSDT", "ETHUSDT"), ("BTCUSDT", "ETHUSDT")):
        e = _held(perp, spot)
        assert e.entry_economics(mark=100_000.0, spot=100_000.0).refusal \
            is not None                       # new exposure is refused ...
        d = None
        for k in (1, 2, 3):                   # ... three negative prints exit
            d = e.on_candle(mark=100_000.0, funding_bps=-1.0, spot=100_000.0,
                            funding_print_ms=k * H8)
        assert d.action == "unwound", (perp, spot, d.reason)
        assert e.position is None
        sides = [(s, side) for s, side, _p in e.broker.orders]
        assert (perp, "Buy") in sides         # the perp short was bought back
