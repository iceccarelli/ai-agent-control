"""The carry book's order LIFECYCLE, through the real integrated path, against a synthetic venue.

    config.load(dict) -> main.build_bot -> TradingBot.tick -> CarryEngine -> LedgerBroker
        -> CarryBroker -> BybitClient (signing, host assertion, retries) -> SyntheticVenue

Only the HTTP bytes are simulated (``synthetic_venue.SyntheticVenue`` is a ``Transport``). The
configuration is an in-memory dict (``USE_TESTNET=1 BYBIT_VENUE=testnet PAPER_TRADING=0``): the
process environment is never read or written, ``LIVE_TRADING_ACK`` is never set, the state
database lives in ``tmp_path`` and every socket fails the test.

Every test asserts on the VENUE'S OWN state (its position, wallet, orders, executions, request
counters), never only on what the local book believes. Where a number is checked it is the
HAND-COMPUTED one, with the arithmetic beside it.

What this does NOT prove: that the real exchange behaves like ``SyntheticVenue`` (see its docstring
for what it does not model: matching, margin, liquidation, queueing, rate limits). It proves the
bot's handling of the semantics modelled here, and that is all.

One configuration choice needs saying: ``CARRY_ENTRY_BENEFIT_MODE=observe``. The default,
``enforce``, refuses every entry until an owner-supplied strategy-evidence verdict exists (none
does in this tree), which is the intended fail-closed state; it would leave no order lifecycle to
test. ``observe`` still computes and logs the verdict. Test 8 runs the default and shows the
refusal.
"""
from __future__ import annotations

import os
import socket
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import config as _config  # noqa: E402
import drill as D  # noqa: E402
import main as _main  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from carry_engine import DELTA_BAND, BookState  # noqa: E402
from fake_bybit import API_KEY, API_SECRET  # noqa: E402
from persistence import StateStore  # noqa: E402
from synthetic_venue import Plan, SyntheticVenue, VenueKill  # noqa: E402

H8 = 8 * 3600 * 1000
#: a funding settlement boundary; the first tick of every world is one hour after it
B = (1_760_000_000_000 // H8 + 1) * H8
CREATE = "/v5/order/create"


class NetworkAttempted(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to resolve, connect or open a connection fails the test."""
    def boom(*_a, **_k):
        raise NetworkAttempted("this offline test tried to touch the network")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    # and the production transport itself: a client built WITHOUT the synthetic transport dies here
    monkeypatch.setattr(bc.RequestsTransport, "request", lambda *a, **k: boom())


@pytest.fixture(autouse=True)
def no_live_ack(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    assert os.environ.get("LIVE_TRADING_ACK") is None


class Clock:
    """ONE clock for the venue, the journal and the engine."""

    def __init__(self, start_ms: int) -> None:
        self.now = int(start_ms)
        self.sleeps: List[float] = []

    def ms(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)

    def to_next_boundary(self) -> None:
        self.now = (self.now // H8 + 1) * H8


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(B + 3600 * 1000)
    monkeypatch.setattr(time, "time", lambda: c.now / 1000.0)
    monkeypatch.setattr(time, "sleep", lambda s: c.sleeps.append(float(s)))
    return c


class World:
    """One bot wired exactly as production wires it, over one synthetic venue."""

    def __init__(self, tmp_path, clock: Clock, *, mode: str = "overlay", style: str = "taker",
                 benefit: str = "observe", btc: Optional[float] = None,
                 qty_step: Optional[str] = None, mark: float = 50_000.0) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.clock, self.mode = clock, mode
        self.path = str(tmp_path / "state.db")
        env = {"BOOK_MODE": "carry", "CARRY_EXECUTION_MODE": mode,
               "CARRY_BORROW_APR": "0.0" if mode == "overlay" else "0.05",
               "CARRY_EXECUTION_STYLE": style, "CARRY_MAKER_WAIT_S": "1",
               "CARRY_ENTRY_BENEFIT_MODE": benefit,
               "USE_TESTNET": "1", "BYBIT_VENUE": "testnet", "PAPER_TRADING": "0",
               "BYBIT_API_KEY": API_KEY, "BYBIT_API_SECRET": API_SECRET,
               "STATE_DB_PATH": self.path}
        assert "LIVE_TRADING_ACK" not in env
        self.cfg = _config.load(env)
        self.venue = SyntheticVenue(clock_ms=clock.ms, mark=mark,
                                    btc=(0.05 if mode == "overlay" else 0.0) if btc is None else btc)
        if qty_step:
            self.venue.qty_step = self.venue.min_qty = qty_step
        for i in range(10):                       # ten settled prints at 6 bps, newest AT the boundary
            self.venue.add_funding_print(0.0006, B - (9 - i) * H8)
        self.store: Optional[StateStore] = None
        self.client = self.bot = None
        self.started = True

    # -- lifecycle of the process ------------------------------------------------------------
    def boot(self, *, cold_start: bool = True):
        """(Re)start the process: a new store handle on the SAME file, a new client, a new bot."""
        if self.store is not None:
            self.store.close()
        self.store = StateStore(self.path)
        self.client = bc.BybitClient(config=self.cfg, store=self.store, transport=self.venue)
        self.bot = _main.build_bot(config=self.cfg, store=self.store, client=self.client)
        # carry_capital.CarryCapitalAuthority binds ``clock=time.time`` as a DEFAULT ARGUMENT, i.e. the
        # real clock of whichever test first imported the module. build_bot passes none, so the fake
        # clock is handed to the one object that holds it (a seam, reported; production is untouched).
        self.bot.carry.capital_authority.clock = lambda: self.clock.now / 1000.0
        self.started = self.bot.carry_cold_start() if cold_start else True
        return self.bot

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None

    def tick(self, advance_s: float = 60) -> Dict[str, Any]:
        self.clock.advance(advance_s)
        self.bot.tick()
        return dict(self.bot._last_carry_decision)

    def prime(self) -> None:
        """Boot, then the first tick of a brand-new database: it writes the (empty) ledger, and
        the portfolio authority stands aside until a ledger exists. No order may leave."""
        self.boot()
        assert self.started is True
        d = self.tick()
        assert d["reason"] == "PORTFOLIO_BLOCKED", d
        assert self.venue.requests_by_path[CREATE] == 0

    def open(self) -> Dict[str, Any]:
        self.prime()
        return self.tick()

    def settle(self, rate: float, *, then_mark: Optional[float] = None, advance_s: float = 90):
        """The venue settles funding at the next boundary at its CURRENT mark; the bot ticks."""
        self.clock.to_next_boundary()
        self.venue.settle_funding(rate, at_ms=self.clock.now)
        if then_mark is not None:
            self.venue.set_mark(then_mark)
        return self.tick(advance_s)

    @property
    def journal(self):
        return self.bot.carry_journal

    @property
    def adapter(self):
        return self.bot.carry.broker.inner


@pytest.fixture()
def world(tmp_path, clock):
    made: List[World] = []

    def make(**kw) -> World:
        w = World(tmp_path / f"w{len(made)}", clock, **kw)
        made.append(w)
        return w
    yield make
    for w in made:
        w.close()


def creates(v: SyntheticVenue) -> List[Dict[str, Any]]:
    return v.create_bodies()


def _kinds(journal) -> List[str]:
    return [e.kind for e in journal.entries]


# ======================================================================================
# 1. the whole overlay cycle, to the cent
# ======================================================================================

def test_overlay_cycle_entry_funding_exit_equals_the_hand_computed_books(world):
    """Entry -> one positive and three negative SETTLEMENT rows -> FUNDING_INVERTED exit.

    Mark 50,000 (spot 50,000) all along, 51,000 for the exit. Size = $100 cap / 50,000 = 0.002 BTC
    (the venue's step is 0.001, so it is already a whole lot). Taker 5.5 bps on linear.

      entry fee   = 0.002 * 50,000 * 0.00055            =  0.0550
      funding     = 0.002 * 50,000 * (+0.0006 -3*0.0001) = +0.06 -0.03 = +0.0300
      exit fee    = 0.002 * 51,000 * 0.00055            =  0.0561
      perp PnL    = (50,000 - 51,000) * 0.002           = -2.0000   (short bought back higher)
      fees        = 0.0550 + 0.0561                     =  0.1111
      CASH net    = 0.03 - 0.1111 - 2.0                 = -2.0811   (= the venue wallet's USDT change)
      inventory   = +0.002 * (51,000 - 50,000)          = +2.0      (the client's BTC, marked)
      pair net    = -2.0811 + 2.0                       = -0.0811
    """
    w = world(mode="overlay")
    v = w.venue
    usdt0, btc0 = v.wallet["USDT"], v.wallet["BTC"]

    d = w.open()
    assert d["action"] == "opened" and d["reason"] == "PERP_HEDGE_LANDED"
    # the venue's own books after the entry
    assert v.net_position() == pytest.approx(-0.002)
    assert v.avg_price["BTCUSDT"] == pytest.approx(50_000.0)
    assert v.wallet["USDT"] == pytest.approx(usdt0 - 0.055)
    assert [b["side"] for b in creates(v)] == ["Sell"]
    assert creates(v)[0]["orderType"] == "Market" and creates(v)[0]["qty"] == "0.002"
    assert "reduceOnly" not in creates(v)[0]

    assert w.settle(0.0006)["reason"] == "HEDGED_AND_COLLECTING"            # +0.06 (short receives)
    assert [r["funding"] for r in v.settlements] == ["0.06"]
    for _ in range(2):
        assert w.settle(-0.0001)["reason"] == "HEDGED_AND_COLLECTING"       # -0.01 each
    assert v.net_position() == pytest.approx(-0.002)                        # still held
    d = w.settle(-0.0001, then_mark=51_000.0)                               # third inverted print
    assert d["action"] == "unwound" and d["reason"] == "FUNDING_INVERTED"

    # the venue is flat, its wallet moved by exactly the hand-computed cash, the exit was reduce-only
    assert v.net_position() == 0.0 and v.open_orders() == []
    assert v.wallet["USDT"] == pytest.approx(usdt0 - 2.0811)
    assert v.wallet["BTC"] == btc0                                           # overlay never trades spot
    assert [b["side"] for b in creates(v)] == ["Sell", "Buy"]
    assert creates(v)[1]["reduceOnly"] is True and creates(v)[1]["qty"] == "0.002"
    assert not [b for b in creates(v) if b["category"] == "spot"]
    assert [r["funding"] for r in v.settlements] == ["0.06", "-0.01", "-0.01", "-0.01"]
    assert [r["closedPnl"] for r in v.closed_pnl] == ["-2"]
    assert len(v.orders) == 2 and all(n == 1 for n in v.creates_by_link.values())

    # the book agrees with the venue to the cent
    st = w.journal.statement()
    assert st["fees_usd"] == pytest.approx(0.1111)
    assert st["funding_venue_usd"] == pytest.approx(0.03)
    assert st["realised_usd"] == pytest.approx(-2.0)
    assert st["net_usd"] == pytest.approx(-2.0811)
    assert st["inventory_mark_usd"] == pytest.approx(2.0)
    assert st["pair_net_usd"] == pytest.approx(-0.0811)
    assert st["net_usd"] == pytest.approx(v.wallet["USDT"] - usdt0)          # journal cash == venue cash
    assert st["funding_venue_usd"] == pytest.approx(sum(float(r["funding"]) for r in v.settlements))
    assert st["fills"] == 2 and st["fees_confirmed"] is True
    assert _kinds(w.journal).count("funding") == 4                           # one per settlement, once
    assert st["funding_unattributed"] == 0 and "funding_unattributed" not in _kinds(w.journal)   # re-polled rows are not re-claimed


# ======================================================================================
# 2. a partial entry
# ======================================================================================

def test_a_partial_entry_whose_remainder_lands_is_booked_once_at_the_total(world):
    """0.0008 fills at once, the other 0.0012 on the second state read. ONE create; the book waits
    for the order to be final and books 0.002; the venue's two execution rows add up to it.

      fee = 0.0008*50,000*0.00055 + 0.0012*50,000*0.00055 = 0.022 + 0.033 = 0.055
    """
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(fills=[0.0008, 0.0012]))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened"
    assert len(v.orders) == 1 and v.requests_by_path[CREATE] == 1           # never a second order
    (order,) = v.orders.values()
    assert order.status == "Filled" and order.cum_qty == pytest.approx(0.002)
    assert [r["execQty"] for r in order.exec_rows] == ["0.0008", "0.0012"]
    assert v.net_position() == pytest.approx(-0.002)
    assert w.bot.carry.position.perp.filled_qty == pytest.approx(0.002)
    assert [e for e in w.journal.entries if e.kind == "perp_open"][0].lines[0].qty == pytest.approx(-0.002)
    assert w.journal.statement()["fees_usd"] == pytest.approx(0.055)
    assert sum(float(r["execFee"]) for r in order.exec_rows) == pytest.approx(0.055)


def test_a_partial_entry_whose_remainder_is_cancelled_is_a_smaller_hedge_not_a_second_order(world):
    """Only 0.0008 fills; the venue cancels the rest. The hedge is 0.0008 (of 0.05 BTC held), the
    book does not chase the 0.0012, and it books 0.0008 * 50,000 * 0.00055 = 0.022 of fee."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(fills=[0.0008]))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened"
    (order,) = v.orders.values()
    assert order.status == "Cancelled" and order.cum_qty == pytest.approx(0.0008)
    assert v.net_position() == pytest.approx(-0.0008)
    assert v.requests_by_path[CREATE] == 1
    assert w.bot.carry.position.perp.filled_qty == pytest.approx(0.0008)
    assert w.journal.statement()["fees_usd"] == pytest.approx(0.022)
    for _ in range(3):                                                      # and it does not chase later
        assert w.tick()["action"] == "hold"
    assert v.requests_by_path[CREATE] == 1 and v.net_position() == pytest.approx(-0.0008)


def test_a_partial_entry_that_never_completes_halts_with_one_order_and_the_short_kept(world):
    """The remainder does not arrive within the book's read-back: a market order that is still live has
    no final fill, so the book halts. It sends nothing on top, and it does not flatten or forget the
    0.0008 short that is already at the venue."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(fills=[0.0008, 0.0012], reads_between_fills=99))
    d = w.open()
    v = w.venue
    assert d["action"] != "opened" and w.bot.carry.state is BookState.HALTED
    assert v.net_position() == pytest.approx(-0.0008)
    (order,) = v.orders.values()
    assert order.status == "PartiallyFilled"
    for _ in range(3):
        w.tick()
    assert v.requests_by_path[CREATE] == 1 and len(v.orders) == 1           # nothing sent on top
    assert w.store.is_kill_switch_engaged()[0] is True


# ======================================================================================
# 3. an acknowledgement lost after the venue accepted
# ======================================================================================

@pytest.mark.parametrize("mode,leg,ack", [
    ("overlay", 1, "drop_after_accept"),     # the perp SELL of an overlay
    ("overlay", 1, "timeout"),
    ("acquire", 1, "drop_after_accept"),     # the SPOT buy of an acquire pair: a naked long if mishandled
    ("acquire", 2, "drop_after_accept"),     # the PERP sell of an acquire pair
])
def test_a_lost_acknowledgement_is_read_back_exactly_one_order_per_link_and_no_naked_leg(world, mode, leg, ack):
    """The venue ACCEPTS the order and fills it; the response never arrives. The client retries the same
    request (same orderLinkId) and meets the venue's duplicate refusal; the broker then asks the venue
    what the link did. Exactly one order exists per link, the pair is hedged to the unit, and the venue's
    position/wallet - not the local book - say so."""
    w = world(mode=mode, qty_step="0.00001" if mode == "acquire" else None)
    w.venue.script_nth_create(leg, Plan(ack=ack, ack_failures=1))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened", d
    assert all(n == 1 for n in v.creates_by_link.values()), dict(v.creates_by_link)
    lost = list(v.orders)[leg - 1]
    assert v.create_attempts_by_link[lost] == 2                              # the retry reached the venue ...
    assert v.requests_by_path[CREATE] == len(v.orders) + 1                    # ... and was refused as a duplicate
    if mode == "overlay":
        assert len(v.orders) == 1 and v.net_position() == pytest.approx(-0.002)
        assert v.wallet["BTC"] == 0.05
    else:
        # spot 0.002 bought, 0.002 * 0.1% = 0.000002 BTC taken as fee, perp = floor(0.001998, 1e-5) = 0.00199
        assert len(v.orders) == 2
        assert v.wallet["BTC"] == pytest.approx(0.001998)
        assert v.net_position() == pytest.approx(-0.00199)
        residual = v.wallet["BTC"] + v.net_position()                        # 0.001998 - 0.00199 = 0.000008 BTC
        assert residual == pytest.approx(0.000008, abs=1e-12) and residual / v.wallet["BTC"] < DELTA_BAND
    assert [e.kind for e in w.journal.entries].count("perp_open") == 1
    assert w.store.is_kill_switch_engaged()[0] is False


def test_a_5xx_that_hid_an_accepted_order_is_resolved_by_asking_the_venue(world):
    """Same as above through the other ambiguous channel: HTTP 502 on a request the venue DID book."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(ack="5xx", accepted_on_failure=True, ack_failures=1))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened" and len(v.orders) == 1
    assert v.net_position() == pytest.approx(-0.002)
    assert all(n == 1 for n in v.creates_by_link.values())


def test_a_5xx_that_never_reached_the_venue_with_no_row_halts_and_does_not_send_a_second_order(world):
    """Every attempt of the create gets a 502 and the venue never books it. The client gives up after its
    retries; the venue shows no row for the link. Whether it will still land is unknown, so the book
    halts - and nothing, on this tick or later, creates a second order."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(ack="5xx", accepted_on_failure=False, ack_failures=99))
    d = w.open()
    v = w.venue
    assert d["action"] != "opened" and w.bot.carry.state is BookState.HALTED
    assert v.requests_by_path[CREATE] == bc.BybitClient.REQUEST_ATTEMPTS       # every retry, same link
    assert len(set(b["orderLinkId"] for b in creates(v))) == 1
    assert v.orders == {} and v.net_position() == 0.0
    for _ in range(2):
        w.tick()
    assert v.requests_by_path[CREATE] == bc.BybitClient.REQUEST_ATTEMPTS


# ======================================================================================
# 4. an order the venue cannot show yet
# ======================================================================================

def test_an_order_invisible_for_two_reads_is_found_by_the_readback_poll_and_booked_once(world):
    """The create is acknowledged, but order/realtime and order/history show nothing for the link on the
    first two reads. The broker's bounded poll asks again; the third read shows the Filled order."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(visibility_delay=2))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened"
    link = next(iter(v.orders))
    reads = [r for r in v.requests if r["path"] == "/v5/order/realtime" and r["params"].get("orderLinkId") == link]
    assert len(reads) == 3                                                    # two blind reads, then the order
    assert v.requests_by_path[CREATE] == 1 and v.net_position() == pytest.approx(-0.002)
    assert [e.kind for e in w.journal.entries].count("perp_open") == 1


def test_an_order_that_stays_invisible_halts_the_book_with_the_short_still_open_and_no_second_order(world):
    """Invisible for more reads than the poll allows (1 + ORDER_READBACK_POLLS): the fill is UNKNOWN, so the
    book halts and engages the kill switch. The venue really did fill it - the short stays, nothing is sent
    on top of it, and no later tick re-sends."""
    w = world(mode="overlay")
    polls = CarryBroker.ORDER_READBACK_POLLS
    w.venue.script_nth_create(1, Plan(visibility_delay=polls + 3))
    d = w.open()
    v = w.venue
    assert d["action"] != "opened" and w.bot.carry.state is BookState.HALTED
    assert v.net_position() == pytest.approx(-0.002)                         # it DID fill; the bot could not see it
    assert w.store.is_kill_switch_engaged()[0] is True
    for _ in range(3):
        w.tick()
    assert v.requests_by_path[CREATE] == 1 and len(v.orders) == 1


# ======================================================================================
# 5. a cancel that loses the race with a fill
# ======================================================================================

def test_a_cancel_that_arrives_after_the_fill_books_the_fill_and_does_not_cross_the_remainder(world):
    """maker_first: the entry rests as a post-only SELL at the ask; when the wait is up the book cancels and
    the venue fills the whole order first (cancel answered 110001). The book must take the order's FINAL
    state: 0.002 at the limit price, maker fee - and send no market order for a 'remainder' that is not there.

      ask = 50,000 * 1.0001 = 50,005        fee = 0.002 * 50,005 * 0.0002 = 0.020002
    """
    w = world(mode="overlay", style="maker_first")
    w.venue.script_nth_create(1, Plan(cancel_fills_first=True))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened"
    (order,) = v.orders.values()
    assert order.order_type == "Limit" and order.tif == "PostOnly" and order.price == pytest.approx(50_005.0)
    assert order.status == "Filled" and order.cum_qty == pytest.approx(0.002)
    assert v.requests_by_path["/v5/order/cancel"] == 1 and v.requests_by_path[CREATE] == 1
    assert v.net_position() == pytest.approx(-0.002)
    assert v.avg_price["BTCUSDT"] == pytest.approx(50_005.0)
    assert w.clock.sleeps.count(1.0) >= 1                                    # the maker wait really ran
    assert w.bot.carry.position.perp.avg_price == pytest.approx(50_005.0)
    assert w.journal.statement()["fees_usd"] == pytest.approx(0.020002)
    assert order.exec_rows[0]["isMaker"] is True and float(order.exec_rows[0]["execFee"]) == pytest.approx(0.020002)
    assert w.venue.open_orders() == []


# ======================================================================================
# 6. duplicate and reordered execution rows
# ======================================================================================

@pytest.mark.parametrize("flags,served,capture_complete", [
    ({"reverse_exec_rows": True}, ["syn-exec-2", "syn-exec-1"], True),
    ({"duplicate_exec_rows": True}, ["syn-exec-1", "syn-exec-1", "syn-exec-2", "syn-exec-2"], False),
    ({"duplicate_exec_rows": True, "reverse_exec_rows": True},
     ["syn-exec-2", "syn-exec-2", "syn-exec-1", "syn-exec-1"], False),
])
def test_duplicate_and_reordered_execution_rows_never_change_what_is_booked(world, flags, served, capture_complete):
    """The venue serves the fills of an order twice and/or newest first. The book books from the ORDER's
    cumulative fields, so the journal is unchanged: one perp_open of 0.002, fee 0.055. The raw rows are
    visible through the broker, de-duplicated by execId they add up to the order, and the drill's final
    capture - which sums raw rows - fails CLOSED on a duplicate (incomplete: a human looks) instead of
    counting a fill twice; reordering alone is harmless."""
    w = world(mode="overlay")
    w.venue.script_nth_create(1, Plan(fills=[0.0008, 0.0012], **flags))
    d = w.open()
    v = w.venue
    assert d["action"] == "opened"
    (order,) = v.orders.values()
    assert [e.kind for e in w.journal.entries].count("perp_open") == 1
    assert w.journal.statement()["fees_usd"] == pytest.approx(0.055)
    assert v.net_position() == pytest.approx(-0.002)
    rows = w.adapter.get_order_executions("BTCUSDT", order_link_id=order.link)
    assert [r["execId"] for r in rows] == served                          # exactly as the venue scripted them
    unique = {r["execId"]: r for r in rows}.values()
    assert sum(float(r["execQty"]) for r in unique) == pytest.approx(order.cum_qty)
    assert sum(float(r["execFee"]) for r in unique) == pytest.approx(0.055)
    report = D.capture_final_venue_state(w.bot.carry.broker, sleep=lambda s: None)
    assert report["complete"] is capture_complete, report
    if not capture_complete:
        assert "execution rows add up to" in report["failures"][0]
    assert v.requests_by_path[CREATE] == 1


# ======================================================================================
# 7. the process dies mid-flow
# ======================================================================================

def test_death_after_the_venue_booked_the_short_halts_the_restart_and_places_no_second_order(world):
    """The venue accepts and fills the entry; the process is killed (BaseException) before it reads the
    answer. A NEW bot on the same database must not read 'no pair, venue flat' (it is not flat): cold start
    HALTS on a short the ledger never recorded, engages the kill switch and sends nothing."""
    w = world(mode="overlay")
    w.venue.kill_at("create:after_accept", nth=1)
    w.prime()
    with pytest.raises(VenueKill):
        w.tick()
    v = w.venue
    assert v.net_position() == pytest.approx(-0.002) and v.requests_by_path[CREATE] == 1
    w.clock.advance(3600)
    w.boot()                                                                 # new process, same db
    assert w.started is False                                                # refused to start
    assert w.store.is_kill_switch_engaged()[0] is True
    assert w.bot.carry.state is BookState.HALTED
    for _ in range(3):
        w.tick()
    assert v.requests_by_path[CREATE] == 1 and len(v.orders) == 1             # no duplicate, no flatten
    assert v.net_position() == pytest.approx(-0.002)                         # the venue still holds it: a human reconciles


def test_death_before_the_request_left_is_proven_absent_and_a_fresh_entry_sends_exactly_one_order(world):
    """Killed before the venue ever saw the create. After the absence window the restart finds the venue
    flat and the order proven never sent: CLEAN. The next entry is a NEW link and the venue ends with one
    short of 0.002 - not two."""
    w = world(mode="overlay")
    w.venue.kill_at("create:before_accept", nth=1)
    w.prime()
    with pytest.raises(VenueKill):
        w.tick()
    v = w.venue
    assert v.orders == {} and v.net_position() == 0.0
    w.clock.advance(3600)
    w.boot()
    assert w.started is True and w.bot.carry.state is BookState.FLAT
    assert w.store.is_kill_switch_engaged()[0] is False
    d = w.tick()
    assert d["action"] == "opened"
    assert len(v.orders) == 1 and v.net_position() == pytest.approx(-0.002)
    links = {b["orderLinkId"] for b in creates(v)}
    assert len(links) == 2                                                    # the dead attempt's link and the new one
    assert all(n == 1 for n in v.creates_by_link.values())


def test_a_restart_inside_the_absence_window_cannot_prove_the_order_never_landed_and_refuses_to_start(world):
    """Same death (before the venue saw the create) but the process is back at once. The venue's silence proves
    nothing yet: the order is 'reconciliation required', the book refuses to start and engages the kill switch.
    After the window it starts FLAT, but the switch is a human's to clear, so no entry follows."""
    w = world(mode="overlay")
    w.venue.kill_at("create:before_accept", nth=1)
    w.prime()
    with pytest.raises(VenueKill):
        w.tick()
    v = w.venue
    w.boot()                                                                  # immediately
    assert w.started is False
    engaged, why = w.store.is_kill_switch_engaged()
    assert engaged is True and "unresolved carry orders" in why
    (row,) = w.store.unresolved_carry_orders()
    assert row["status"] == "reconciliation_required" and row["side"] == "Sell"
    w.tick()
    assert v.orders == {} and v.net_position() == 0.0 and v.requests_by_path[CREATE] == 1
    w.clock.advance(3600)
    w.boot()
    assert w.started is True and w.store.unresolved_carry_orders() == []
    assert w.store.is_kill_switch_engaged()[0] is True                       # still engaged: a human clears it
    assert w.tick() == {}                      # the tick stopped at the risk layer: the book was never consulted
    assert v.orders == {} and v.requests_by_path[CREATE] == 1


def test_death_after_the_exit_filled_halts_the_restart_on_a_ledger_that_still_holds_the_pair(world):
    """The exit buy-back is accepted and fills; the process dies before it can record the close. The venue is
    flat but the ledger says HEDGED. A restart must not carry on as if the pair were still there (it would
    manage a hedge that no longer exists) nor start CLEAN over it: it halts for a human, sending nothing."""
    w = world(mode="overlay")
    w.open()
    for _ in range(2):
        w.settle(-0.0001)
    v = w.venue
    v.kill_at("create:after_accept", nth=2)
    v.set_mark(51_000.0)
    w.clock.to_next_boundary()
    v.settle_funding(-0.0001, at_ms=w.clock.now)
    with pytest.raises(VenueKill):
        w.tick(90)
    assert v.net_position() == 0.0 and v.requests_by_path[CREATE] == 2
    w.clock.advance(3600)
    w.boot()
    assert w.started is False
    assert w.store.is_kill_switch_engaged()[0] is True
    for _ in range(2):
        w.tick()
    assert v.requests_by_path[CREATE] == 2 and v.net_position() == 0.0


# ======================================================================================
# 8. the gates refuse BEFORE any order leaves
# ======================================================================================

@pytest.mark.parametrize("how,reason", [
    ("cap", "NOTIONAL_ABOVE_CAP"),
    ("kill_switch", "PORTFOLIO_BLOCKED"),            # the portfolio authority reads the engaged switch first
    ("benefit_enforced", "ENTRY_BENEFIT_UNAVAILABLE"),
])
def test_a_refused_entry_places_zero_create_requests(world, how, reason):
    """The book DID read the market and price the entry (tickers, wallet, instrument rules were requested), and
    then a gate refused it. Not one /v5/order/create reached the venue and nothing changed there.

    cap: the pair gate's cap is tightened to $50 while the engine sizes $100 (0.002 BTC * 50,000)."""
    w = world(mode="overlay", benefit="enforce" if how == "benefit_enforced" else "observe")
    w.prime()
    v = w.venue
    if how == "cap":
        w.bot.carry.pair_risk.max_notional_usd = 50.0
    elif how == "kill_switch":
        w.store.trip_kill_switch("test: operator stop")
    d = w.tick()
    assert d["action"] == "stand_aside", d
    assert d["reason"] == reason, d
    assert v.requests_by_path[CREATE] == 0 and v.orders == {} and v.executions == []
    assert v.net_position() == 0.0 and v.wallet == {"USDT": 10_000.0, "BTC": 0.05}
    assert v.requests_by_path["/v5/market/tickers"] > 0                       # it did look at the market
    assert w.bot.carry.state is BookState.FLAT


def test_a_kill_switch_engaged_between_the_legs_stops_the_second_entry_order_but_not_the_exit(world):
    """Acquire: the spot BUY is accepted and, at that instant, the operator engages the kill switch. The perp
    SELL is an entry-side order and the broker's own gate refuses it (nothing is sent); the naked spot is then
    sold - an exit is never gated - so the venue ends with no perp at all and no BTC.

      spot BUY 0.002 @ 50,000 = 100 USDT out, 0.001998 BTC in (0.000002 BTC fee)
      spot SELL 0.001998 @ 50,000 = 99.9 USDT in, fee 0.1% = 0.0999 USDT
      USDT = 10,000 - 100 + 99.9 - 0.0999 = 9,999.8001
    """
    w = world(mode="acquire", qty_step="0.00001")
    w.prime()
    fired: List[int] = []

    def trip(_venue, _order):
        if not fired:
            fired.append(1)
            w.store.trip_kill_switch("operator stop between the legs")
    w.venue.on_accept.append(trip)
    d = w.tick()
    v = w.venue
    assert fired == [1]
    assert d["action"] == "unwound" and "PERP_LEG_DID_NOT_FILL" in d["reason"], d
    assert [(o.category, o.side, o.status) for o in v.orders.values()] == [("spot", "Buy", "Filled"), ("spot", "Sell", "Filled")]
    assert not [b for b in creates(v) if b["category"] == "linear"]            # no perp order was ever sent
    assert v.net_position() == 0.0 and v.wallet["BTC"] == pytest.approx(0.0, abs=1e-12)
    assert v.wallet["USDT"] == pytest.approx(9_999.8001)
    assert w.store.is_kill_switch_engaged()[0] is True


# ======================================================================================
# 9. FLAT is the venue's word
# ======================================================================================

def test_flat_is_proven_from_the_venues_position_and_order_reads_whatever_the_journal_says(world):
    """After a completed cycle the local journal is made deliberately WRONG in both directions; the drill's
    flat proof asks the venue, and the verdict follows the venue every time."""
    w = world(mode="overlay")
    w.open()
    for _ in range(2):
        w.settle(-0.0001)
    w.settle(-0.0001, then_mark=51_000.0)
    v = w.venue
    assert v.net_position() == 0.0
    broker = w.bot.carry.broker

    # (a) the venue is flat, the journal LIES that a short is open
    from ledger import Entry, Line
    w.journal.post(Entry(ms=w.clock.now + 1, kind="perp_open", ref="lie-open",
                         lines=[Line("POSITION:PERP", 0.0, -0.5), Line("ASSET:USDT", 0.0, 0.0)],
                         memo="deliberately wrong: an open short that does not exist"))
    assert w.journal.statement()["open_perp_qty"] == pytest.approx(-0.5)
    mark = len(v.requests)
    verdict, evidence, why = D.venue_flatness(broker, inventory_before=0.05)
    assert verdict == "FLAT" and why == "", (verdict, why)
    paths = [r["path"] for r in v.requests[mark:]]
    assert "/v5/position/list" in paths and "/v5/order/realtime" in paths and "/v5/account/wallet-balance" in paths
    assert evidence["venue_perp_qty"] == 0.0 and evidence["open_orders"] == 0

    # (b) a short really exists at the venue (placed outside the book), the journal says flat
    v.position["BTCUSDT"], v.avg_price["BTCUSDT"] = -0.003, 50_000.0
    verdict, evidence, why = D.venue_flatness(broker, inventory_before=0.05)
    assert verdict == "NOT_FLAT" and "still holds 0.003 perp" in why
    v.position["BTCUSDT"] = 0.0

    # (c) a resting order at the venue with a flat position and a clean journal
    placed = w.adapter.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001,
                                       price=round(v.mark * 1.0002, 1), product="linear")
    assert placed["resting"] is True and len(v.open_orders()) == 1
    verdict, evidence, why = D.venue_flatness(broker, inventory_before=0.05)
    assert verdict == "NOT_FLAT" and "1 carry order(s) are still open" in why

    # (d) the client's spot moved: the overlay never trades spot, so that is a trade nobody asked for
    w.adapter.cancel_order(symbol="BTCUSDT", order_link_id=placed["order_link_id"], product="linear")
    v.wallet["BTC"] = 0.04
    verdict, evidence, why = D.venue_flatness(broker, inventory_before=0.05)
    assert verdict == "NOT_FLAT" and "spot inventory moved" in why
    v.wallet["BTC"] = 0.05
    assert D.venue_flatness(broker, inventory_before=0.05)[0] == "FLAT"


def test_a_close_the_fills_call_complete_is_not_declared_flat_while_the_venues_position_read_says_short(world):
    """The exit buy fills in full, but the position list the book reads afterwards still shows the short (a
    lagging replica, frozen before the close). 'FLAT' must come from that read, not from fill arithmetic: the
    book keeps the position, halts, and sends nothing more."""
    w = world(mode="overlay")
    w.open()
    for _ in range(2):
        w.settle(-0.0001)
    v = w.venue
    v.set_mark(51_000.0)
    w.clock.to_next_boundary()
    v.settle_funding(-0.0001, at_ms=w.clock.now)
    v.freeze_positions()                                                      # the replica stops here: short 0.002
    d = w.tick(90)
    assert v.net_position() == 0.0                                            # the venue IS flat ...
    assert d["action"] != "unwound" and w.bot.carry.state is not BookState.FLAT       # ... the book was not told so
    assert w.bot.carry.state is BookState.HALTED
    assert w.bot.carry.position is not None
    assert v.requests_by_path[CREATE] == 2
    for _ in range(2):
        w.tick()
    assert v.requests_by_path[CREATE] == 2


# ======================================================================================
# 10. acquire: the base-coin fee shrinks what is held, and the hedge follows
# ======================================================================================

def test_acquire_pair_hedge_equals_the_quantity_net_of_the_base_coin_fee(world):
    """Spot BUY 0.002 BTC at 50,000; the venue takes its 0.1% in BTC (the coin received).

      fee       = 0.002 * 0.001                 = 0.000002 BTC
      held      = 0.002 - 0.000002              = 0.001998 BTC
      perp lot  = 0.00001 (the venue's step)    -> hedge = floor(0.001998 / 0.00001) * 0.00001 = 0.00199
      exit      = perp Buy 0.00199, spot Sell of the NET 0.001998 (not 0.002: the wallet cannot sell it)
    """
    w = world(mode="acquire", qty_step="0.00001")
    v = w.venue
    d = w.open()
    assert d["action"] == "opened" and d["reason"] == "PAIR_LANDED"
    bodies = creates(v)
    assert [(b["category"], b["side"], b["qty"]) for b in bodies] == [("spot", "Buy", "0.002"), ("linear", "Sell", "0.00199")]
    assert bodies[0]["marketUnit"] == "baseCoin"
    # the VENUE: wallet holds the net BTC, the short is the net rounded down to the lot (not the gross 0.002)
    assert v.wallet["BTC"] == pytest.approx(0.001998)
    assert v.net_position() == pytest.approx(-0.00199)
    assert v.net_position() != pytest.approx(-0.002)
    spot_order = next(o for o in v.orders.values() if o.category == "spot")
    assert spot_order.cum_fee == {"BTC": pytest.approx(0.000002)}
    assert w.bot.carry.position.spot.fee_base_qty == pytest.approx(0.000002)
    assert w.bot.carry.position.perp.filled_qty == pytest.approx(0.00199)
    assert w.venue.wallet["USDT"] == pytest.approx(10_000.0 - 0.002 * 50_000 - 0.00199 * 50_000 * 0.00055)

    w.settle(0.0006)
    for _ in range(2):
        w.settle(-0.0001)
    d = w.settle(-0.0001)
    assert d["action"] == "unwound" and d["reason"] == "FUNDING_INVERTED"
    assert v.net_position() == 0.0 and v.wallet["BTC"] == pytest.approx(0.0, abs=1e-12)
    bodies = creates(v)
    assert [(b["category"], b["side"], b["qty"]) for b in bodies[2:]] == [("linear", "Buy", "0.00199"), ("spot", "Sell", "0.001998")]
    assert v.open_orders() == []


def test_acquire_refuses_a_lot_so_coarse_that_the_fee_leaves_the_hedge_unpaired_and_sends_nothing(world):
    """With the venue's default 0.001 step the net 0.001998 floors to 0.001: half the spot would sit unhedged
    (50% against a 2% band). Refused before the spot BUY - zero creates."""
    w = world(mode="acquire")                                                # qty step 0.001
    w.boot()
    assert w.started is True
    d = w.tick()
    assert d["action"] == "stand_aside" and d["reason"] == "ACQUIRE_LOT_TOO_COARSE_FOR_FEE", d
    assert w.venue.requests_by_path[CREATE] == 0 and w.venue.wallet["BTC"] == 0.0


# ======================================================================================
# 11. the safety envelope
# ======================================================================================

def test_every_request_of_a_whole_run_stays_on_the_testnet_host_and_signed_and_nothing_else_is_touched(world):
    """Across an entry, funding, an exit and a restart: every URL starts with the testnet host, none names
    another host, every private request is signed and accepted, no /v5/asset path is requested, the venue
    never answered 'unknown endpoint', and the live acknowledgement is absent everywhere."""
    w = world(mode="overlay")
    w.open()
    for _ in range(2):
        w.settle(-0.0001)
    w.settle(-0.0001, then_mark=51_000.0)
    w.clock.advance(3600)
    w.boot()                                                                  # a restart on the same db
    v = w.venue
    assert len(v.requests) > 100
    assert all(u.startswith(bc.TESTNET_REST + "/v5/") for u in v.urls())
    assert {urlsplit(u).hostname for u in v.urls()} == {"api-testnet.bybit.com"}
    assert not [p for p in v.requests_by_path if p.startswith("/v5/asset")]
    assert not [p for p in v.requests_by_path if "withdraw" in p or "transfer" in p]
    assert v.unhandled == [] and v.auth_failures == []
    private = [r for r in v.requests if r["path"] in ("/v5/order/create", "/v5/position/list", "/v5/account/wallet-balance")]
    assert private and all(r["headers"].get("X-BAPI-SIGN") and r["headers"].get("X-BAPI-API-KEY") == API_KEY for r in private)
    assert w.client.venue == "testnet" and w.client.base_url == bc.TESTNET_REST
    # and the host is re-asserted on every request: with the client's URL rewritten to the live host, a whole
    # tick is refused before one more byte reaches the transport
    sent = len(v.requests)
    w.client.base_url = "https://api.bybit.com"
    w.tick()
    assert len(v.requests) == sent and w.bot.carry.state is not BookState.HEDGED
    w.client.base_url = bc.TESTNET_REST
    assert w.cfg.USE_TESTNET is True and w.cfg.PAPER_TRADING is False
    assert w.cfg.LIVE_TRADING_ACK == "" and os.environ.get("LIVE_TRADING_ACK") is None
    armed, why = _config.is_live_authorized(w.cfg)
    assert armed is not True
    # the only exposure endpoints used are the two the client gates, and only /order/create was needed
    assert set(p for p in v.requests_by_path if p in bc.EXPOSURE_ENDPOINTS) == {CREATE}


# ======================================================================================
# 8b. a hostile or mis-united venue answer is refused through the WHOLE stack (adversarial review: the new reader refusals were only unit-tested)
# ======================================================================================

@pytest.mark.parametrize("how,reason", [
    ("funding_as_percent", "MARKET_VIEW_UNUSABLE"),          # fundingRate "0.5": a unit slip / wrong field, 5000 bps
    ("interval_4h", "MARKET_VIEW_UNUSABLE"),                 # fundingIntervalHour "4": every downstream number assumes 8h
    ("fee_other_symbol", None),                              # an ETHUSDT fee row for BTCUSDT
    ("fee_in_percent", None),                                # takerFeeRate in percent: 100x too large. NOTE: the OLD broker also stood aside here (the cost gate
                                                             # refuses a 1100 bps round trip): an OVER-priced fee is the conservative direction. The dangerous
                                                             # direction (under-priced: the wrong-symbol row of zeros) is fee_other_symbol, which the old code let through.
])
def test_a_mis_united_or_wrong_row_from_the_venue_stops_the_entry_with_zero_orders(world, how, reason):
    """Each case is a real venue-shaped answer with ONE wrong thing. The real BybitClient and CarryBroker read it, refuse it, and the
    engine places no order and stays FLAT: the cost/funding number it would have priced is never invented."""
    w = world(mode="overlay")
    w.boot()                                   # the hostile answer is in place BEFORE the first tick: the fee tier is read once and cached
    v = w.venue
    if how == "funding_as_percent":
        v.funding_rate = 0.5
    elif how == "interval_4h":
        v.funding_interval_hour = "4"
    elif how == "fee_other_symbol":
        v.fee_row_symbol = "ETHUSDT"
    elif how == "fee_in_percent":
        v.fee_scale = 100.0
    decisions = [w.tick(), w.tick(), w.tick()]           # the first tick of a new database stands aside; the next ones would enter
    for d in decisions:
        assert d["action"] in ("skipped", "stand_aside", "halted"), d
    assert v.requests_by_path[CREATE] == 0 and v.orders == {} and v.net_position() == 0.0, decisions
    assert w.bot.carry.state is BookState.FLAT, decisions
    if reason:
        assert decisions[-1]["reason"] == reason, decisions
    # CONTROL: the same bot on a clean venue enters, so the refusal above was about the answer and not a broken harness.
    clean = world(mode="overlay")
    clean.open()
    assert clean.venue.requests_by_path[CREATE] >= 1 and clean.venue.net_position() < 0.0
