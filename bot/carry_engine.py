"""Delta-neutral carry book: two legs, one position, executed on candle receipt.

WHAT THIS IS
============
The trading system in this tree holds ONE leg at a time and bets on direction.
That trade is arithmetically dead: 25 bps round trip x ~36 entries a year is
900 bps of friction against a ~15 bps edge, and eleven signal families died
proving it.

This module holds TWO legs and bets on nothing.

    LONG spot BTC  +  SHORT perp BTC  =  zero price exposure

Price goes up, the spot leg gains what the perp leg loses. Price goes down, the
reverse. You are flat. What you keep is the funding the leveraged longs pay to
be long, every eight hours, forever.

On this repo's own 4,431-print corpus:

    funding positive          85.4% of prints
    mean rate                 +0.625 bps per 8h
    collected over 4.05 yrs   27.70% of notional  =  6.84%/yr gross
    entry cost                31 bps ONE TIME, not per period

You are not predicting. You are supplying balance sheet to people who want
leverage, and charging them for it. That is a business with an economic reason
to exist, which is exactly what direction-guessing on daily bars never had.

THE THING THAT KILLS THIS TRADE
===============================
One leg landing without the other. A spot buy that fills while the perp short
is rejected is not a hedged book — it is a naked long at whatever size you
sized the CARRY at, which is the size you would never take directionally. The
same trade run carelessly is how funds die: not from the carry going negative,
but from waking up delta-long into a crash because a leg failed at 3am.

So the invariants here are not caution. They are the product:

  1. LEGS LAND TOGETHER OR NOT AT ALL. A partial pair is unwound immediately.
     If the unwind fails, the kill switch trips. There is no third option.
  2. DELTA IS MEASURED, NOT ASSUMED. The hedge drifts as price moves and as
     legs fill at different prices. It is recomputed on every candle.
  3. LEVERAGE IS ON THE SPREAD, NOT ON A VIEW. 10x on a delta-neutral pair is
     10x on a carry spread. 10x on a directional bet is a margin call with a
     countdown. This module physically cannot express the second one: it has
     no direction parameter.
  4. THE SHORT LEG CAN BE LIQUIDATED. Spot cannot. Margin headroom on the perp
     is checked before every action and is the first thing to trip.

WHAT EXECUTES
=============
`on_candle()` is the whole loop. It runs on every closed bar and it either acts
or states why it did not. In order:

    liquidation headroom -> delta drift -> funding decision -> open / rebalance
                                                            / harvest / unwind

No LLM sits anywhere on that path. A forecaster may later size the book by
predicting the next funding rate — a tractable problem where being wrong means
collecting less, not losing — but it will hand this module a NOTIONAL, never a
direction. This module has no direction to give it.
"""
from __future__ import annotations

import logging
import datetime as dt
import math
import time
from decimal import Decimal, InvalidOperation
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Rebalance when |delta| exceeds this fraction of book notional. Below it,
#: churning the legs costs more in fees than the residual exposure costs in
#: risk. 2% of notional on a $100k book is $2k of drift — a rounding error
#: against the carry, and far cheaper than two taker fees to correct.
DELTA_BAND = 0.02

#: Refuse to open, and start unwinding, below this maintenance-margin multiple
#: on the short perp leg. 2.0x means the perp can move 50% against the short
#: before liquidation. The spot leg is the collateral of last resort and can be
#: sold to post margin, but selling spot in a squeeze is the worst execution in
#: the world, so the trigger sits well before it is needed.
MIN_MARGIN_MULTIPLE = 2.0

#: Do not open new carry below this forecast 8h rate. At 0.2 bps per 8h the
#: annualised gross is ~2.2%, which does not clear the 31 bps entry cost inside
#: a reasonable hold. Below it, hold cash.
MIN_ENTRY_FUNDING_BPS = 0.2

#: The two ways this book can hold the long side (0036).
#:
#: ACQUIRE  the book BUYS the spot leg and sells it again on exit. Two extra
#:          legs per round trip, plus financing on the borrowed dollars.
#: OVERLAY  the client already owns the BTC. The book shorts the perp against
#:          inventory it never buys and never sells. Same funding, same basis,
#:          none of the spot round trip and none of the borrow.
#:
#: PHASE1_DECISION chose OVERLAY as the product on 2026-09-08. The engine went
#: on buying spot anyway, and 0032 measured what that costs on the settlement
#: clock: $27,232 of fees against $39,411 of funding, -2.99%/yr at 5%
#: financing. The mode is now an explicit, required decision — there is no
#: default, because the two modes send different orders.
ACQUIRE = "acquire"
OVERLAY = "overlay"
EXECUTION_MODES = (ACQUIRE, OVERLAY)

#: Quantity differences below this fraction of a leg are venue rounding.
QTY_DUST = 1e-9

#: HOW a leg is executed, which is a different question from WHICH legs the
#: book trades (that is EXECUTION_MODES above).
#:
#:   TAKER        cross the spread on every order. What the cost gate prices.
#:   MAKER_FIRST  rest at the touch for a bounded wait, then cross whatever
#:                did not fill.
#:
#: The spread is NOT the argument. Bybit's BTCUSDT touch is 0.1 USDT wide —
#: 0.013 bps on a $100k mark — and crossing it is free. The argument is the
#: FEE TIER: this account pays 5.5 bps taker and 2.0 bps maker per perp leg,
#: so an overlay round trip is 11 bps crossed and 4 bps rested. 0032 measured
#: $27,232 of fees against $39,411 of funding; that 7 bps is the difference
#: between a median trade that cannot pay for its own exit and one that can.
#:
#: What cannot be known offline is FILL PROBABILITY. So every maker path here
#: ends in a taker fallback, the cost gate goes on pricing the TAKER round
#: trip (`round_trip_bps`), and the realised fee of every fill is recorded
#: rather than modelled. Maker is upside, never an assumption.
MAKER_FIRST = "maker_first"
TAKER = "taker"
EXECUTION_STYLES = (MAKER_FIRST, TAKER)

#: How long a resting entry order is given before it is cancelled and crossed.
#: Bounded, and bounded small: the book is deciding on a snapshot, and a quote
#: left in the market after that snapshot is stale is an order placed on a
#: price nobody is looking at any more.
DEFAULT_MAKER_WAIT_S = 2.0


def snap_to_lot(qty: float, step: float) -> float:
    """Round DOWN to the venue's quantity step. Exact, not floating point.

    Bybit linear BTCUSDT has qtyStep 0.001: 0.00127 is not an order, it is a
    rejection. Rounding UP would breach the cap, so it rounds down and the
    caller refuses when what is left is below the minimum.
    """
    if step <= 0:
        return float(qty)
    try:
        lots = Decimal(str(qty)) // Decimal(str(step))
        return float(lots * Decimal(str(step)))
    except (InvalidOperation, ValueError):
        return 0.0


#: Unwind when funding has been negative this many consecutive prints. Negative
#: funding means the trade has inverted: you are now PAYING to hold the hedge.
#: Three prints is one day — long enough not to react to a single squeeze,
#: short enough to leave before a regime.
NEGATIVE_FUNDING_EXIT_PRINTS = 3


def _costs():
    """Import the gates lazily. carry_costs is pure and imports nothing from
    here, so the dependency points one way only."""
    import carry_costs
    return carry_costs


class ColdStart(Enum):
    """What a restart may do, decided before the first tick."""

    CLEAN = "CLEAN"       # nothing in the ledger, nothing at the venue
    RESUME = "RESUME"     # they agree; the book picks the same pair back up
    HALT = "HALT"         # they do not agree; a human looks before anything trades


@dataclass(frozen=True)
class ColdStartDecision:
    action: "ColdStart"
    reason: str
    naked_side: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def plan_cold_start(*, ledger: Optional[Dict[str, Any]],
                    venue_perp_qty: float, venue_spot_qty: float,
                    open_orders: Any = (),
                    dust: float = QTY_DUST) -> ColdStartDecision:
    """Compare what the last run believed against what the venue holds.

    Pure. It reads both sides and returns a verdict; it never writes to either.
    A book that edited its ledger to match the venue could not detect a missed
    fill, a manual order, or a bug — which is the entire reason this function
    exists rather than "load the position and carry on".

    HALT is the answer to every disagreement, including the ones that look
    harmless. The failure this prevents is a container that restarts holding a
    hedge, reads an empty ledger, and opens a SECOND hedge against the same
    margin and the same liquidation price.
    """
    orders = list(open_orders or ())
    if orders:
        return ColdStartDecision(
            ColdStart.HALT,
            f"{len(orders)} carry order(s) are still open at the venue; one "
            "could fill into a book that does not know it exists",
            detail={"open_orders": orders[:8]})

    state = str((ledger or {}).get("book_state", "")) or "FLAT"
    position = (ledger or {}).get("position")
    perp = abs(float(venue_perp_qty or 0.0))
    spot = float(venue_spot_qty or 0.0)

    if state == BookState.HALTED.value:
        return ColdStartDecision(
            ColdStart.HALT,
            "the last run halted and a halt is cleared by a human, never by a "
            "restart",
            detail={"venue_perp_qty": perp})

    if not position:
        if perp > dust:
            return ColdStartDecision(
                ColdStart.HALT,
                f"the venue holds a perp position of {perp:.10g} that the "
                "ledger never recorded",
                naked_side="perp", detail={"venue_perp_qty": perp})
        return ColdStartDecision(ColdStart.CLEAN, "no pair in the ledger, none "
                                 "at the venue")

    ledger_perp = abs(float((position.get("perp") or {}).get("filled_qty", 0.0)))
    ledger_spot = abs(float((position.get("spot") or {}).get("filled_qty", 0.0)))
    mode = str((ledger or {}).get("execution_mode", ACQUIRE))

    if abs(perp - ledger_perp) > dust:
        return ColdStartDecision(
            ColdStart.HALT,
            f"the ledger holds a {ledger_perp:.10g} perp short and the venue "
            f"holds {perp:.10g}",
            naked_side="spot" if perp < ledger_perp else "perp",
            detail={"ledger_perp_qty": ledger_perp, "venue_perp_qty": perp})

    # The long side. In OVERLAY it is the client's inventory and only has to
    # COVER the hedge; in ACQUIRE the book bought a specific quantity and that
    # quantity has to be there.
    if mode == OVERLAY:
        if spot + dust < ledger_spot:
            return ColdStartDecision(
                ColdStart.HALT,
                f"the hedge covers {ledger_spot:.10g} BTC and the account now "
                f"holds {spot:.10g}: the inventory this short was written "
                "against has left",
                naked_side="perp",
                detail={"ledger_spot_qty": ledger_spot, "venue_spot_qty": spot})
    elif abs(spot - ledger_spot) > dust:
        return ColdStartDecision(
            ColdStart.HALT,
            f"the ledger holds {ledger_spot:.10g} of spot and the venue holds "
            f"{spot:.10g}",
            naked_side="spot" if spot < ledger_spot else "perp",
            detail={"ledger_spot_qty": ledger_spot, "venue_spot_qty": spot})

    return ColdStartDecision(
        ColdStart.RESUME, "the ledger and the venue agree on the pair",
        detail={"perp_qty": perp, "spot_qty": spot, "execution_mode": mode})


class BookState(Enum):
    FLAT = "FLAT"
    OPENING = "OPENING"
    HEDGED = "HEDGED"
    DRIFTED = "DRIFTED"
    UNWINDING = "UNWINDING"
    HALTED = "HALTED"


@dataclass
class Leg:
    """One side of the pair. Filled quantity is what the VENUE says, not what
    was requested — the difference between those two numbers is the naked
    exposure this module exists to eliminate."""

    symbol: str
    side: str                    # "Buy" (spot long) or "Sell" (perp short)
    product: str                 # "spot" or "linear"
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    avg_price: float = 0.0
    order_link_id: str = ""
    #: The venue's `cumExecFee` for this leg, or None when it did not say.
    #: Unknown stays unknown: a None the ledger must resolve, never a 0.0 that
    #: makes a cost vanish.
    fee: Optional[float] = None
    #: How this leg was filled. A leg can be both: a post-only order that
    #: partially fills and is then crossed for the remainder is ONE leg with
    #: two fee rates. The split is carried so the ledger can reconcile the
    #: realised fee against the invoice instead of assuming a tier.
    maker_qty: float = 0.0
    taker_qty: float = 0.0

    @property
    def notional(self) -> float:
        return self.filled_qty * self.avg_price

    @property
    def is_complete(self) -> bool:
        if self.requested_qty <= 0:
            return False
        return abs(self.filled_qty - self.requested_qty) <= 1e-9


@dataclass
class CarryPosition:
    """A hedged pair. Delta is DERIVED from fills, never carried as a belief."""

    spot: Leg
    perp: Leg
    opened_ms: int = 0
    funding_collected: float = 0.0
    negative_funding_streak: int = 0

    @property
    def delta_qty(self) -> float:
        """Net BTC exposure. Zero is the whole point of the trade."""
        return self.spot.filled_qty - self.perp.filled_qty

    def delta_fraction(self, mark: float) -> float:
        """|delta| as a fraction of book notional. The number that trips a
        rebalance."""
        book = self.spot.notional + self.perp.notional
        if book <= 0:
            return 0.0
        return abs(self.delta_qty * mark) / book

    @property
    def is_paired(self) -> bool:
        return self.spot.is_complete and self.perp.is_complete


@dataclass
class CarryDecision:
    """What the engine did, and why. `acted` is False unless an order went out."""

    action: str
    acted: bool = False
    reason: str = ""
    state: BookState = BookState.FLAT
    detail: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.acted


class CarryEngine:
    """Holds the pair. Acts on candles. Refuses on anything it cannot verify.

    Constructed with a broker adapter exposing:
        place_market(symbol, side, qty, product) -> {filled_qty, avg_price, order_link_id}
        get_margin_multiple(symbol) -> float
        get_mark(symbol) -> float
    and a kill switch callable. Everything else is arithmetic done here.
    """

    #: Funding prints kept for the EWMA. Eight is ~2.7 days: enough for a
    #: three-print smoother with slack, short enough that a regime change is
    #: not diluted by last week.
    FUNDING_HISTORY = 8

    def __init__(self, *, broker: Any, kill_switch: Any = None,
                 spot_symbol: str = "BTCUSDT", perp_symbol: str = "BTCUSDT",
                 max_notional_usd: float = 100.0,
                 delta_band: float = DELTA_BAND,
                 min_margin_multiple: float = MIN_MARGIN_MULTIPLE,
                 min_entry_funding_bps: float = MIN_ENTRY_FUNDING_BPS,
                 borrow_apr: float, execution_mode: str,
                 persist: Any,
                 execution_style: str = TAKER,
                 maker_wait_s: float = DEFAULT_MAKER_WAIT_S) -> None:
        # borrow_apr has NO default (0033, INVENTORY D7). It decides whether the
        # carry clears its cost of capital, and build_bot never passed it, so
        # the live engine silently financed at 5% whatever the operator meant.
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(
                f"{execution_mode!r} is not an execution mode "
                f"{EXECUTION_MODES}. Refusing rather than defaulting: the two "
                "modes send different orders, and guessing means either "
                "buying spot the client already owns or shorting against "
                "inventory that is not there.")
        if not callable(persist):
            raise TypeError(
                "persist must be callable: the book writes what it holds to "
                "the ledger on every state change. A carry engine with "
                "nowhere to write is a container that restarts flat and opens "
                "a second hedge (INVENTORY D3).")
        # EXECUTION STYLE (0040). Defaulted, and defaulted to TAKER, which is
        # the only default in this constructor: it is not a risk input but the
        # already-priced path. `round_trip_bps` prices the taker round trip
        # whatever this says, so selecting MAKER_FIRST can only make a trade
        # cheaper than the gate assumed, never more expensive.
        if execution_style not in EXECUTION_STYLES:
            raise ValueError(
                f"{execution_style!r} is not an execution style "
                f"{EXECUTION_STYLES}")
        wait = float(maker_wait_s)
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError(
                f"maker_wait_s={maker_wait_s!r}: a resting order needs a "
                "positive, finite wait before it is cancelled and crossed")
        self.execution_style = execution_style
        self.maker_wait_s = wait
        self.persist = persist
        self.execution_mode = execution_mode
        self.broker = broker
        self.kill_switch = kill_switch
        self.spot_symbol = spot_symbol
        self.perp_symbol = perp_symbol
        self.max_notional_usd = float(max_notional_usd)
        self.delta_band = float(delta_band)
        self.min_margin_multiple = float(min_margin_multiple)
        self.min_entry_funding_bps = float(min_entry_funding_bps)
        self.position: Optional[CarryPosition] = None
        self.state = BookState.FLAT
        self.borrow_apr = float(borrow_apr)
        #: Set by build_bot. Consulted in _open BEFORE the first leg goes out,
        #: because a gate that runs after one leg has landed is not a gate, it
        #: is a post-mortem.
        self.pair_risk: Optional[Any] = None
        #: The market view this cycle acted on, set by main.tick. Kept so the
        #: pair gate judges the SAME observation the entry decision used.
        self.snapshot: Optional[Any] = None
        self._funding_history: List[float] = []
        #: Settlement stamp (epoch ms) of the last funding PRINT recorded. A
        #: call to on_candle is a TICK; a print is an event with a stamp, and
        #: only a stamp newer than this one is a new print (0034, INVENTORY D1).
        self._last_print_ms: Optional[int] = None
        #: The last exception a leg raised, so a refusal can name it. A leg
        #: that fails for a reason nobody can read is a leg that fails again.
        self._last_leg_error: str = ""
        #: Market time of the most recent tick, in epoch ms. Carried so the
        #: paths that do not take `timestamp_ms` can still date the day's
        #: entry allowance on the market's calendar rather than the wall
        #: clock (0043).
        self._last_tick_ms: int = 0

    # -- the ledger -------------------------------------------------------

    def to_state(self) -> Dict[str, Any]:
        """Everything a restart needs, as plain JSON-able data."""
        position = None
        if self.position is not None:
            position = {
                "spot": asdict(self.position.spot),
                "perp": asdict(self.position.perp),
                "opened_ms": int(self.position.opened_ms),
                "funding_collected": float(self.position.funding_collected),
                "negative_funding_streak":
                    int(self.position.negative_funding_streak),
            }
        return {
            "book_state": self.state.value,
            "execution_mode": self.execution_mode,
            "position": position,
            "last_print_ms": self._last_print_ms,
            "funding_history": list(self._funding_history),
            "max_notional_usd": self.max_notional_usd,
        }

    def restore(self, state: Dict[str, Any]) -> None:
        """Take the ledger's word for the position. Called only after
        `cold_start` has checked it against the venue."""
        position = state.get("position")
        if position:
            self.position = CarryPosition(
                spot=Leg(**position["spot"]), perp=Leg(**position["perp"]),
                opened_ms=int(position.get("opened_ms", 0)),
                funding_collected=float(position.get("funding_collected", 0.0)),
                negative_funding_streak=int(
                    position.get("negative_funding_streak", 0)))
            self.state = BookState.HEDGED
        else:
            self.position = None
            self.state = BookState.FLAT
        last = state.get("last_print_ms")
        self._last_print_ms = None if last is None else int(last)
        self._funding_history = [float(x) for x in
                                 state.get("funding_history", [])][
                                     -self.FUNDING_HISTORY:]

    def _record(self) -> Optional[CarryDecision]:
        """Write the ledger. A failure to write HALTS.

        The alternative is a book that placed an order the next restart cannot
        see, which is the exact state cold start exists to make impossible.
        """
        try:
            self.persist(self.to_state())
        except Exception as exc:  # noqa: BLE001
            return self._halt("LEDGER_WRITE_FAILED",
                              f"the position could not be written down "
                              f"({type(exc).__name__}: {exc}); a book whose "
                              "ledger is behind its orders must stop",
                              record=False)
        return None

    def cold_start(self, *, ledger: Optional[Dict[str, Any]],
                   venue_perp_qty: float, venue_spot_qty: float,
                   open_orders: Any = ()) -> ColdStartDecision:
        """Reconcile the ledger against the venue BEFORE the first tick.

        RESUME puts the same pair back in the engine. HALT stops the process
        and trips the switch; it deliberately does NOT rewrite the ledger,
        because the disagreement is the evidence a human needs.
        """
        if ledger and ledger.get("execution_mode") and \
                ledger["execution_mode"] != self.execution_mode:
            decision = ColdStartDecision(
                ColdStart.HALT,
                f"the ledger holds a {ledger['execution_mode']!r} position and "
                f"this process is configured {self.execution_mode!r}; "
                "unwinding one as the other would trade the wrong leg",
                detail={"ledger_mode": ledger["execution_mode"],
                        "process_mode": self.execution_mode})
        else:
            decision = plan_cold_start(ledger=ledger,
                                       venue_perp_qty=venue_perp_qty,
                                       venue_spot_qty=venue_spot_qty,
                                       open_orders=open_orders)
        if decision.action is ColdStart.RESUME:
            self.restore(ledger or {})
            logger.warning("carry cold start: RESUMED %s", decision.detail)
        elif decision.action is ColdStart.HALT:
            self.state = BookState.HALTED
            if self.kill_switch is not None:
                try:
                    self.kill_switch(f"CARRY_COLD_START: {decision.reason}")
                except Exception:  # noqa: BLE001
                    logger.critical("kill switch itself failed on cold start")
            logger.critical(
                "carry cold start HALTED: %s (naked side: %s) %s",
                decision.reason, decision.naked_side or "unknown",
                decision.detail)
        return decision

    def warm_funding_history(self, prints: Sequence[Tuple[float, int]]) -> int:
        """Seed the EWMA from SETTLED prints the venue has already published.

        Without this a freshly started book is blind for a full day. The EWMA
        needs EWMA_MIN_PRINTS settled prints before `evaluate_entry` will look
        at anything, prints come every eight hours, so a new deploy — a new
        volume, a first run, a machine moved by the host — stands aside for
        24 hours with `INSUFFICIENT_FUNDING_HISTORY` while the venue has been
        publishing the last two hundred prints the whole time.

        There is no look-ahead here: every one of these is a SETTLED print,
        already paid, exactly the thing `on_candle` would have recorded had
        the process been running. Reading them is not a shortcut, it is
        catching up.

        Refuses to touch a history that already has prints in it — a restart
        that restored its own ledger knows more than the venue's last eight,
        and overwriting that would lose the stamp that tells a tick from a
        print.

        `prints` is newest-LAST, as `(bps, settlement_ms)`.
        """
        if self._funding_history:
            return 0
        clean: List[Tuple[float, int]] = []
        for rate, stamp in prints:
            if not (self._finite(rate) and int(stamp) > 0):
                continue
            clean.append((float(rate), int(stamp)))
        clean.sort(key=lambda row: row[1])
        clean = clean[-self.FUNDING_HISTORY:]
        if not clean:
            return 0
        self._funding_history = [rate for rate, _ms in clean]
        # The newest stamp becomes the watermark, so the print that is already
        # settled is not booked again as if the book had held through it.
        self._last_print_ms = clean[-1][1]
        logger.warning(
            "carry: warmed %d settled funding prints from the venue, newest "
            "%s; the book can decide now instead of in 24 hours",
            len(clean), clean[-1][1])
        return len(clean)

    def round_trip_bps(self) -> float:
        """What a full cycle costs THIS account at THIS venue, in bps.

        Read from the venue's fee table, never from a constant: the number is
        account specific and it is the largest cost in the book. OVERLAY pays
        two perp legs; ACQUIRE pays those plus the spot round trip.

        Raises when the venue will not say. The cost gate then refuses, which
        is the only honest move — a book that cannot price its own exit has no
        business opening.
        """
        perp = float(self.broker.get_fee_rates(self.perp_symbol,
                                               "linear")["taker_bps"])
        if self.execution_mode == OVERLAY:
            return 2.0 * perp
        spot = float(self.broker.get_fee_rates(self.spot_symbol,
                                               "spot")["taker_bps"])
        return 2.0 * (spot + perp)

    # -- the loop ---------------------------------------------------------

    def on_candle(self, *, mark: float, funding_bps: float,
                  spot: Optional[float] = None,
                  timestamp_ms: int = 0,
                  funding_print_ms: Optional[int] = None) -> CarryDecision:
        """One tick. Act or state why not. This is the entire loop.

        `funding_bps` is the rate of the SETTLED print stamped
        `funding_print_ms`. A tick is not a print: main.tick calls this every
        LOOP_INTERVAL_SECONDS, and until 0034 every call was booked as a print
        (60 ticks at 1 bps on $100 booked $0.60; one print is $0.01), counted
        toward the negative streak, and fed the EWMA. Now only a stamp newer
        than the last one is a print. No stamp, no print: nothing is booked,
        and the warm-up cannot complete, so the book cannot open on a rate it
        cannot place in time.

        Order is not cosmetic. Liquidation headroom is checked before anything
        else because a margin call does not wait for the funding decision, and
        delta is checked before opening more because adding size to a drifted
        book compounds the exposure you already failed to hedge.
        """
        self._last_tick_ms = int(timestamp_ms or 0)
        if self.state is BookState.HALTED:
            return CarryDecision("halted", reason="KILL_SWITCH_ENGAGED",
                                 state=self.state)

        if not self._finite(mark) or mark <= 0:
            return self._halt("MARK_UNREADABLE",
                              "a book that cannot be marked cannot be hedged")

        # Every PRINT is recorded whether or not the book acts on it — once.
        # The EWMA is only meaningful if the history is complete, and equally
        # meaningless if one print is counted sixty times.
        new_print = (funding_print_ms is not None
                     and self._finite(funding_bps)
                     and self._finite(funding_print_ms)
                     and (self._last_print_ms is None
                          or int(funding_print_ms) > self._last_print_ms))
        if new_print:
            self._last_print_ms = int(funding_print_ms)
            self._funding_history.append(float(funding_bps))
            del self._funding_history[:-self.FUNDING_HISTORY]

        if self.position is not None:
            headroom = self._check_margin()
            if headroom is not None:
                return headroom

            drift = self.position.delta_fraction(mark)
            if drift > self.delta_band:
                return self._rebalance(mark, drift)

            # A print the pair was HELD through: its stamp is after the open.
            # The print that triggered the entry, or a late record of an
            # older settlement, was not earned by this position.
            held = new_print and int(funding_print_ms) > self.position.opened_ms
            if held:
                # Booked SIGNED, on WHAT THE POSITION IS WORTH NOW.
                #
                # 0034: until then a negative print was paid and never booked,
                # so funding_collected overstated what the book earned (D10).
                #
                # 0043: until then it booked `perp.notional`, which is
                # `filled_qty * avg_price` and is frozen at the fill. The
                # venue pays funding on the position's value at the
                # SETTLEMENT mark. Replaying the engine over the Bybit corpus
                # booked $29,371 where the same 79 trades earn $39,444 — a
                # quarter of the funding missing, because BTC rose while the
                # positions were held and the entry price never moved. The
                # error's sign follows the price, which is intolerable in a
                # book whose whole claim is that price direction does not
                # matter.
                #
                # `mark` is the mark this tick observed. Live it is at most
                # LOOP_INTERVAL_SECONDS away from the settlement mark the
                # venue used; that residual is named in INVENTORY, not hidden.
                if self._finite(mark) and mark > 0:
                    self.position.funding_collected += (
                        funding_bps / 1e4) * self.position.perp.filled_qty * mark
                if funding_bps < 0:
                    self.position.negative_funding_streak += 1
                    if (self.position.negative_funding_streak
                            >= NEGATIVE_FUNDING_EXIT_PRINTS):
                        return self._unwind(mark, "FUNDING_INVERTED")
                else:
                    self.position.negative_funding_streak = 0

            return CarryDecision("hold", reason="HEDGED_AND_COLLECTING",
                                 state=BookState.HEDGED,
                                 detail={"delta_fraction": drift,
                                         "funding_bps": funding_bps,
                                         "new_print": bool(held),
                                         "collected": self.position.funding_collected})

        # THE GATES (0019/0021). Before this, the engine opened on ONE
        # condition: the last funding print cleared a threshold. That is a
        # reflex, not a decision — it does not know what the money costs, what
        # the basis will take back, or whether one print represents the next
        # eight hours. 0018 measured the result: borrow was 57% of gross income
        # and 15 of 20 trades lost money.
        #
        # `spot` is REQUIRED to open. Without it the basis is unknown, and an
        # unknown basis is not a small basis. Refusing is the only honest move:
        # a default here would be a silent risk parameter, which is the exact
        # class of bug 0016 removed from the cap.
        if spot is None:
            return CarryDecision(
                "stand_aside", reason="SPOT_UNAVAILABLE_BASIS_UNKNOWN",
                state=BookState.FLAT,
                detail={"note": "cannot price the basis without a spot mark; "
                                "an unknown basis is not a small one"})

        try:
            round_trip = self.round_trip_bps()
        except Exception as exc:  # noqa: BLE001 - an unpriced exit is a refusal
            return CarryDecision(
                "stand_aside", reason="FEE_RATES_UNREADABLE",
                state=BookState.FLAT,
                detail={"error": f"{type(exc).__name__}: {exc}",
                        "note": "the venue would not say what this account "
                                "pays; the round trip is the biggest cost in "
                                "the book and is never assumed"})

        verdict = _costs().evaluate_entry(
            funding_prints_bps=self._funding_history,
            perp=mark, spot=spot, borrow_apr=self.borrow_apr,
            round_trip_bps=round_trip)
        if not verdict:
            return CarryDecision(
                "stand_aside", reason=verdict.reason, state=BookState.FLAT,
                detail={"funding_bps": funding_bps,
                        "smoothed_bps": verdict.smoothed_funding_bps,
                        "entry_basis_bps": verdict.entry_basis_bps,
                        "net_edge_bps_per_day": verdict.net_edge_bps_per_day})

        return self._open(mark, spot, funding_bps, timestamp_ms)

    # -- actions ----------------------------------------------------------

    def _open(self, mark: float, spot_mark: float, funding_bps: float,
              timestamp_ms: int) -> CarryDecision:
        """Both legs, or neither. There is no partially-open carry position."""
        # THE PRECONDITIONS (0033, INVENTORY D8). Until 0033 the pair gate ran
        # only "if self.pair_risk is not None and self.snapshot is not None",
        # so an engine built without either opened on the cost gates alone. No
        # first leg now leaves without all three: the cost gates (already
        # passed to reach here), the pair gate, and ONE market snapshot whose
        # marks are the marks this decision was made on.
        if self.pair_risk is None:
            return CarryDecision(
                "stand_aside", reason="PAIR_GATE_ABSENT", state=BookState.FLAT,
                detail={"note": "no CarryRisk attached; a pair gate that is "
                                "absent is not a pass"})
        if self.snapshot is None:
            return CarryDecision(
                "stand_aside", reason="SNAPSHOT_ABSENT", state=BookState.FLAT,
                detail={"note": "no market snapshot; the pair gate cannot "
                                "judge freshness, margin or basis"})
        if (getattr(self.snapshot, "perp_mark", None) != mark
                or getattr(self.snapshot, "spot_mark", None) != spot_mark):
            return CarryDecision(
                "stand_aside", reason="SNAPSHOT_MISMATCH", state=BookState.FLAT,
                detail={"decided_on": {"perp": mark, "spot": spot_mark},
                        "snapshot": {"perp": getattr(self.snapshot,
                                                     "perp_mark", None),
                                     "spot": getattr(self.snapshot,
                                                     "spot_mark", None)},
                        "note": "the gate must judge the observation the "
                                "entry was decided on, not a different one"})

        # THE VENUE'S RULES (0036, INVENTORY F5). `cap / mark` is 0.00127 BTC
        # at $79k and Bybit's linear step is 0.001: that order is a rejection,
        # not a small position. Rules are read from the venue and never
        # assumed.
        try:
            rules = self.broker.get_lot_rules(self.perp_symbol, "linear")
        except Exception as exc:  # noqa: BLE001
            return CarryDecision(
                "stand_aside", reason="VENUE_RULES_UNREADABLE",
                state=BookState.FLAT,
                detail={"error": f"{type(exc).__name__}: {exc}"})

        wanted = self.max_notional_usd / mark
        if wanted <= 0 or not self._finite(wanted):
            return self._halt("SIZE_INVALID", "refusing an unsizable book")

        inventory = None
        if self.execution_mode == OVERLAY:
            # THE CLIENT'S BTC. The book hedges a SLICE of it and never buys
            # or sells any. The rest of the stack is the client's exposure,
            # not the book's, and is deliberately not hedged here.
            try:
                inventory = float(
                    self.broker.get_spot_inventory(self.spot_symbol))
            except Exception as exc:  # noqa: BLE001
                return CarryDecision(
                    "stand_aside", reason="INVENTORY_UNREADABLE",
                    state=BookState.FLAT,
                    detail={"error": f"{type(exc).__name__}: {exc}",
                            "note": "an unreadable inventory is not an empty "
                                    "one; refusing rather than shorting "
                                    "against BTC that may not be there"})
            if not self._finite(inventory) or inventory <= 0:
                return CarryDecision(
                    "stand_aside", reason="NO_SPOT_INVENTORY",
                    state=BookState.FLAT,
                    detail={"inventory": inventory,
                            "note": "the overlay hedges BTC the client already "
                                    "owns; with none there is nothing to hedge"})
            wanted = min(wanted, inventory)

        qty = snap_to_lot(wanted, float(rules.get("qty_step", 0.0)))
        min_qty = float(rules.get("min_qty", 0.0))
        min_notional = float(rules.get("min_notional", 0.0))
        if qty < min_qty or qty <= 0 or qty * mark < min_notional:
            return CarryDecision(
                "stand_aside", reason="SIZE_BELOW_VENUE_MINIMUM",
                state=BookState.FLAT,
                detail={"wanted_qty": wanted, "snapped_qty": qty,
                        "min_qty": min_qty, "min_notional_usd": min_notional,
                        "cap_usd": self.max_notional_usd,
                        "inventory": inventory,
                        "note": "the cap (or the inventory) does not reach one "
                                "venue lot; a size the venue will not accept "
                                "is a rejected order, not a smaller book"})

        margin = self._margin_multiple()
        if margin is not None and margin < self.min_margin_multiple:
            return CarryDecision("stand_aside", reason="MARGIN_HEADROOM_TOO_THIN",
                                 state=BookState.FLAT,
                                 detail={"margin_multiple": margin})

        # THE PAIR GATE, before the first leg. Ordered by damage inside
        # CarryRisk: a tripped kill switch outranks a stale price, both outrank
        # a cap. Never skipped: its absence refused above.
        gate = self.pair_risk.gate_open(
            notional_usd=self.max_notional_usd, snapshot=self.snapshot,
            has_open_pair=self.position is not None,
            now=self._gate_now(timestamp_ms))
        if not gate:
            return CarryDecision("stand_aside", reason=gate.reason,
                                 state=BookState.FLAT, detail=gate.detail)

        self.state = BookState.OPENING
        self._last_leg_error = ""
        if self.execution_mode == OVERLAY:
            return self._open_overlay(qty, float(inventory), mark, spot_mark,
                                      funding_bps, timestamp_ms, rules)

        # ACQUIRE NEVER RESTS (0040). Maker-first is worth 7 bps a round trip,
        # and the measurement that justified it — gated overlay on the Bybit
        # settlement clock, $8,781 of fees against $3,193, +7.23%/yr against
        # +8.59%/yr — is an OVERLAY measurement. In this mode the trade is a
        # PAIR, and every second either entry leg spends resting is a second
        # the book is long spot with no hedge behind it, or short with no spot
        # in front. "Both legs land or neither" is the rule this module exists
        # to enforce; buying a fee tier by widening the window where neither
        # is true would sell the rule for the discount.
        spot = self._fire(self.spot_symbol, "Buy", qty, "spot")
        if spot is None or spot.filled_qty <= 0:
            return self._refused("SPOT_LEG_DID_NOT_FILL")

        # Hedge exactly what the spot leg ACTUALLY filled. Hedging the requested
        # size instead is how a rounding difference becomes a permanent short.
        perp = self._fire(self.perp_symbol, "Sell", spot.filled_qty, "linear")
        if perp is None or perp.filled_qty <= 0:
            return self._emergency_unwind_spot(
                spot, "PERP_LEG_DID_NOT_FILL — spot is naked, unwinding now")

        position = CarryPosition(spot=spot, perp=perp, opened_ms=timestamp_ms)
        residual = position.delta_fraction(mark)
        if residual > self.delta_band:
            # Both legs landed but the sizes disagree by more than the band.
            # That is not a hedge, it is a directional position wearing one.
            # It paid a round trip, so it spends the day (INVENTORY D4).
            self.position = position
            self.pair_risk.record_broken_pair(
                now=self._gate_now(timestamp_ms))
            return self._unwind(mark, "LEGS_LANDED_UNPAIRED")

        self.position = position
        self.state = BookState.HEDGED
        # Only now: both legs landed. A refused pair consumed nothing; a
        # broken one spent the day through record_broken_pair instead.
        self.pair_risk.record_entry(now=self._gate_now(timestamp_ms))
        failed = self._record()
        if failed is not None:
            return failed
        return CarryDecision(
            "opened", acted=True, reason="PAIR_LANDED", state=BookState.HEDGED,
            detail={"qty": spot.filled_qty, "spot_price": spot.avg_price,
                    "perp_price": perp.avg_price, "delta_fraction": residual,
                    "funding_bps": funding_bps})

    def _open_overlay(self, qty: float, inventory: float, mark: float,
                      spot_mark: float, funding_bps: float, timestamp_ms: int,
                      rules: Dict[str, Any]) -> CarryDecision:
        """Short the perp against BTC the client already holds. One order.

        "Both legs land or neither" is about never being naked. Here the long
        side was the client's before this book existed, so there is no naked
        spot to create: a perp that does not fill leaves the client exactly as
        they were, and a perp that PARTIALLY fills is a smaller hedged slice,
        which is a correct hedge of less inventory rather than an incident.

        The one thing that IS naked in this mode is shorting MORE than the
        inventory, and that is bought back immediately or the book halts.
        """
        # The ONE place an order may rest (0040). There is no pair to break
        # here: the long side was the client's before this book existed, so
        # until this order fills nothing has changed and nobody is naked. That
        # is what makes the wait free, and it is why ACQUIRE does not get it.
        perp = self._fire(self.perp_symbol, "Sell", qty, "linear",
                          patient=True,
                          qty_step=float(rules.get("qty_step", 0.0)))
        if perp is None or perp.filled_qty <= 0:
            # Nothing was bought and nothing was sold: no round trip was paid,
            # so the day's allowance is untouched.
            return self._refused("PERP_LEG_DID_NOT_FILL")

        if perp.filled_qty > inventory + QTY_DUST:
            excess = snap_to_lot(perp.filled_qty - inventory,
                                 float(rules.get("qty_step", 0.0)))
            closed = (self._fire(self.perp_symbol, "Buy", excess, "linear")
                      if excess > 0 else None)
            if closed is None or closed.filled_qty <= 0:
                return self._halt(
                    "OVERSOLD_BEYOND_INVENTORY",
                    f"short {perp.filled_qty} against {inventory} of "
                    "inventory and the excess did not close; the book is "
                    "naked SHORT")
            perp.filled_qty -= closed.filled_qty
            perp.requested_qty = perp.filled_qty

        hedged = perp.filled_qty
        # The inventory slice this hedge covers. No order was placed for it and
        # none ever will be: order_link_id says INVENTORY so no reader mistakes
        # it for a fill, and the fee is None rather than 0.0 because there is
        # no fee to know.
        spot_leg = Leg(symbol=self.spot_symbol, side="Hold", product="spot",
                       requested_qty=hedged, filled_qty=hedged,
                       avg_price=spot_mark, order_link_id="INVENTORY")
        position = CarryPosition(spot=spot_leg, perp=perp,
                                 opened_ms=timestamp_ms)
        self.position = position
        self.state = BookState.HEDGED
        self.pair_risk.record_entry(now=self._gate_now(timestamp_ms))
        failed = self._record()
        if failed is not None:
            return failed
        return CarryDecision(
            "opened", acted=True, reason="PERP_HEDGE_LANDED",
            state=BookState.HEDGED,
            detail={"qty": hedged, "inventory": inventory,
                    "unhedged_inventory": max(0.0, inventory - hedged),
                    "perp_price": perp.avg_price, "spot_mark": spot_mark,
                    "delta_fraction": position.delta_fraction(mark),
                    "funding_bps": funding_bps,
                    "execution_mode": OVERLAY})

    def _rebalance(self, mark: float, drift: float) -> CarryDecision:
        """Bring delta back inside the band by trading the PERP leg only.

        The perp is the cheap leg (5.5 bps vs 10 bps taker) and the one that can
        be shorted freely. Correcting on the spot side would mean selling
        collateral, which is the last thing to touch.
        """
        position = self.position
        assert position is not None
        delta = position.delta_qty
        side = "Sell" if delta > 0 else "Buy"
        fill = self._fire(self.perp_symbol, side, abs(delta), "linear")
        if fill is None or fill.filled_qty <= 0:
            return self._halt(
                "REBALANCE_FAILED",
                f"delta is {drift:.2%} of book and the correction did not fill")

        if side == "Sell":
            position.perp.filled_qty += fill.filled_qty
        else:
            position.perp.filled_qty -= fill.filled_qty
        self.state = BookState.HEDGED
        failed = self._record()
        if failed is not None:
            return failed
        return CarryDecision(
            "rebalanced", acted=True, reason="DELTA_RETURNED_TO_BAND",
            state=BookState.HEDGED,
            detail={"drift_before": drift, "corrected_qty": fill.filled_qty,
                    "drift_after": position.delta_fraction(mark)})

    def _unwind(self, mark: float, reason: str) -> CarryDecision:
        """Close both legs. Perp first: it is the leg that can be liquidated."""
        position = self.position
        assert position is not None
        self.state = BookState.UNWINDING
        perp = self._fire(self.perp_symbol, "Buy", position.perp.filled_qty,
                          "linear")
        if self.execution_mode == OVERLAY:
            # The long side is the client's inventory. Closing the hedge means
            # buying the perp back and nothing else: selling their BTC would
            # be a liquidation nobody asked for.
            if perp is None or perp.filled_qty <= 0:
                return self._halt(
                    "UNWIND_INCOMPLETE",
                    f"{reason} — the perp hedge did not close and the "
                    "client's BTC is now unhedged")
            self.position = None
            self.state = BookState.FLAT
            failed = self._record()
            if failed is not None:
                return failed
            return CarryDecision("unwound", acted=True, reason=reason,
                                 state=BookState.FLAT,
                                 detail={"collected": position.funding_collected,
                                         "execution_mode": OVERLAY})
        spot = self._fire(self.spot_symbol, "Sell", position.spot.filled_qty,
                          "spot")
        if perp is None or spot is None:
            return self._halt(
                "UNWIND_INCOMPLETE",
                f"{reason} — a leg did not close and the book is now naked")
        self.position = None
        self.state = BookState.FLAT
        failed = self._record()
        if failed is not None:
            return failed
        return CarryDecision("unwound", acted=True, reason=reason,
                             state=BookState.FLAT,
                             detail={"collected": position.funding_collected})

    def _emergency_unwind_spot(self, spot: Leg, reason: str) -> CarryDecision:
        """The perp never landed. Sell the spot immediately; it is naked long."""
        self.state = BookState.UNWINDING
        closed = self._fire(self.spot_symbol, "Sell", spot.filled_qty, "spot")
        if closed is None or closed.filled_qty <= 0:
            return self._halt("NAKED_SPOT_UNWIND_FAILED", reason)
        self.position = None
        self.state = BookState.FLAT
        failed = self._record()
        if failed is not None:
            return failed
        # A spot round trip was paid for nothing. Without this, a venue that
        # rejects the perp leg made the book buy and sell spot every tick:
        # 8 round trips in 10 ticks, measured (INVENTORY D4).
        if self.pair_risk is not None:
            self.pair_risk.record_broken_pair(
                now=self._gate_now(self._last_tick_ms))
        return CarryDecision("unwound", acted=True, reason=reason,
                             state=BookState.FLAT,
                             detail={"error": self._last_leg_error})

    # -- guards -----------------------------------------------------------

    def _check_margin(self) -> Optional[CarryDecision]:
        margin = self._margin_multiple()
        if margin is None:
            return self._halt("MARGIN_UNREADABLE",
                              "an unreadable margin is a margin call you cannot see")
        if margin < self.min_margin_multiple:
            return self._unwind(self.broker.get_mark(self.perp_symbol),
                                f"MARGIN_HEADROOM_{margin:.2f}x_BELOW_FLOOR")
        return None

    def _margin_multiple(self) -> Optional[float]:
        try:
            value = float(self.broker.get_margin_multiple(self.perp_symbol))
        except Exception:  # noqa: BLE001
            return None
        return value if self._finite(value) else None

    # -- execution ---------------------------------------------------------
    #
    # GETTING OUT IS NEVER SLOWED FOR A FEE.
    #
    # `patient=True` is passed by the two ENTRY paths and by nothing else. An
    # entry is optional: if the book does not come to us, the correct outcome
    # is simply not to trade, so waiting in the queue risks nothing but time.
    # Every other order this engine sends exists to REMOVE an exposure — the
    # unwind, the margin-driven exit, the emergency sale of a naked spot leg,
    # the delta rebalance, the buy-back of a short that ran past the
    # inventory — and against the risk of not filling at all, a cheaper fill
    # is worth nothing. Those cross the spread immediately, whatever
    # `execution_style` says. There is no configuration that changes this.

    def _fire(self, symbol: str, side: str, qty: float, product: str,
              *, patient: bool = False,
              qty_step: float = 0.0) -> Optional[Leg]:
        if qty <= 0 or not self._finite(qty):
            return None
        if patient and self.execution_style == MAKER_FIRST:
            return self._rest_then_take(symbol, side, qty, product, qty_step)
        return self._take(symbol, side, qty, product)

    def _take(self, symbol: str, side: str, qty: float,
              product: str) -> Optional[Leg]:
        """Cross the spread. The path the cost gate priced."""
        try:
            result = self.broker.place_market(symbol=symbol, side=side,
                                              qty=qty, product=product)
        except Exception as exc:  # noqa: BLE001
            self._last_leg_error = f"{type(exc).__name__}: {exc}"
            logger.error("leg %s %s %s failed: %s", side, symbol, product, exc)
            return None
        if not result:
            return None
        raw_fee = result.get("fee")
        filled = float(result.get("filled_qty", 0.0) or 0.0)
        return Leg(symbol=symbol, side=side, product=product,
                   requested_qty=qty, filled_qty=filled,
                   avg_price=float(result.get("avg_price", 0.0) or 0.0),
                   order_link_id=str(result.get("order_link_id", "")),
                   fee=None if raw_fee is None else float(raw_fee),
                   taker_qty=filled)

    def _rest_then_take(self, symbol: str, side: str, qty: float,
                        product: str, qty_step: float) -> Optional[Leg]:
        """Join the queue, wait a bounded time, then cross what is left."""
        maker = self._rest(symbol, side, qty, product)
        if self.state is BookState.HALTED:
            # `_rest` could not tell whether an order rests at the venue.
            # Crossing on top of it would be the second leg of a position
            # nobody asked for.
            return None
        maker_qty = 0.0 if maker is None else maker.filled_qty
        remainder = qty - maker_qty
        if qty_step > 0:
            # A remainder below one venue lot is not a smaller order, it is a
            # rejected one. What landed is the leg.
            remainder = snap_to_lot(remainder, qty_step)
        if remainder <= QTY_DUST:
            return self._as_whole_leg(maker)
        taker = self._take(symbol, side, remainder, product)
        if maker is None or maker.filled_qty <= 0:
            return taker
        if taker is None:
            # The rested part is a real fill and hedges what it hedges. The
            # callers pair against filled_qty, never against what was asked
            # for, so a smaller leg is a correct hedge of less — not a loss.
            return self._as_whole_leg(maker)
        return self._merge(maker, taker, qty)

    def _rest(self, symbol: str, side: str, qty: float,
              product: str) -> Optional[Leg]:
        """One post-only order at the touch. Returns what it filled, or None.

        Never crosses: a Sell rests at the ask and a Buy at the bid. None
        means "no maker fill and nothing resting" — the caller crosses. An
        order whose fate cannot be established HALTS instead, because a
        resting order the ledger does not know about is the D3 failure with a
        different name.
        """
        try:
            top = self.broker.get_book_top(symbol, product)
            bid = float(top["bid"])
            ask = float(top["ask"])
        except Exception as exc:  # noqa: BLE001
            # Nothing was sent, so this is not an incident — it is a reason to
            # cross rather than to quote a price nobody quoted.
            logger.warning("book top unreadable for %s/%s (%s); crossing",
                           symbol, product, exc)
            return None
        if not (self._finite(bid) and self._finite(ask)) or bid <= 0 \
                or ask <= 0 or bid >= ask:
            logger.warning("book top for %s/%s is crossed or unusable "
                           "(bid %r ask %r); crossing", symbol, product,
                           bid, ask)
            return None
        price = ask if side == "Sell" else bid
        try:
            placed = self.broker.place_post_only(
                symbol=symbol, side=side, qty=qty, price=price,
                product=product)
        except Exception as exc:  # noqa: BLE001
            self._last_leg_error = f"{type(exc).__name__}: {exc}"
            self._halt(
                "MAKER_ORDER_AMBIGUOUS",
                f"a post-only {side} of {qty} {symbol} may or may not be "
                f"resting at the venue ({exc}); crossing on top of it would "
                "open a second leg, so the book stops instead")
            return None
        if not placed:
            return None                      # rejected: the price moved
        filled = float(placed.get("filled_qty", 0.0) or 0.0)
        raw_fee = placed.get("fee")
        link = str(placed.get("order_link_id", ""))
        leg = Leg(symbol=symbol, side=side, product=product,
                  requested_qty=qty, filled_qty=filled,
                  avg_price=float(placed.get("avg_price", 0.0) or 0.0),
                  order_link_id=link,
                  fee=None if raw_fee is None else float(raw_fee),
                  maker_qty=filled)
        if filled + QTY_DUST >= qty or not placed.get("resting"):
            return leg
        time.sleep(self.maker_wait_s)
        try:
            final = self.broker.cancel_order(symbol=symbol, order_link_id=link,
                                             product=product)
        except Exception as exc:  # noqa: BLE001
            self._last_leg_error = f"{type(exc).__name__}: {exc}"
            self._halt(
                "MAKER_ORDER_UNCANCELLABLE",
                f"a post-only {side} of {qty} {symbol} could not be cancelled "
                f"or read back ({exc}); what it has filled is unknown")
            return None
        total = float((final or {}).get("filled_qty", 0.0) or 0.0)
        if total > leg.filled_qty:
            # The venue's number wins: it is the total on that order, and it
            # may have grown between placement and cancellation.
            leg.filled_qty = total
            leg.maker_qty = total
            leg.avg_price = float((final or {}).get("avg_price", 0.0)
                                  or leg.avg_price)
            fee = (final or {}).get("fee")
            leg.fee = None if fee is None else float(fee)
        return leg

    @staticmethod
    def _as_whole_leg(maker: Optional[Leg]) -> Optional[Leg]:
        """What rested and filled IS the leg, at the size that landed."""
        if maker is None or maker.filled_qty <= 0:
            return None
        maker.requested_qty = maker.filled_qty
        return maker

    @staticmethod
    def _merge(maker: Leg, taker: Leg, requested: float) -> Leg:
        """One leg, two fee rates."""
        total = maker.filled_qty + taker.filled_qty
        price = 0.0
        if total > 0:
            price = (maker.filled_qty * maker.avg_price
                     + taker.filled_qty * taker.avg_price) / total
        # An unknown half makes the TOTAL unknown. Summing the known half and
        # calling it the fee would book a cost smaller than the invoice;
        # maker_qty and taker_qty are carried so the ledger can resolve it.
        fee = (None if maker.fee is None or taker.fee is None
               else maker.fee + taker.fee)
        return Leg(symbol=maker.symbol, side=maker.side, product=maker.product,
                   requested_qty=requested, filled_qty=total, avg_price=price,
                   order_link_id=f"{maker.order_link_id}+{taker.order_link_id}",
                   fee=fee, maker_qty=maker.filled_qty,
                   taker_qty=taker.filled_qty)

    @staticmethod
    def _gate_now(timestamp_ms: int) -> Optional["dt.datetime"]:
        """The MARKET's time for this tick, or None to mean "use the clock".

        `CarryRisk` dates the day's entry allowance. Until 0043 it dated it
        from `datetime.now(utc)` while this method had `timestamp_ms` in hand
        and never passed it. Live those are the same clock and the behaviour
        is unchanged — which is exactly the problem: a daily limit that agrees
        with the calendar only in production is a daily limit nobody can test.
        Replay 4,500 settlements and every one of them falls on today, the
        allowance is spent on the first and never returns, and the book stands
        aside for four years.

        None for a tick with no timestamp, so a caller that never supplied one
        behaves exactly as it did before.
        """
        if not timestamp_ms or timestamp_ms <= 0:
            return None
        try:
            return dt.datetime.fromtimestamp(timestamp_ms / 1000.0,
                                             dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def _refused(self, reason: str) -> CarryDecision:
        """A leg did not land. A HALT survives; anything else returns FLAT."""
        if self.state is BookState.HALTED:
            return CarryDecision("halted", reason="LEG_OUTCOME_UNKNOWN",
                                 state=BookState.HALTED,
                                 detail={"error": self._last_leg_error,
                                         "refused_as": reason})
        self.state = BookState.FLAT
        return CarryDecision("refused", reason=reason, state=BookState.FLAT,
                             detail={"error": self._last_leg_error})

    def _halt(self, reason: str, detail: str,
              record: bool = True) -> CarryDecision:
        self.state = BookState.HALTED
        if self.kill_switch is not None:
            try:
                self.kill_switch(reason)
            except Exception:  # noqa: BLE001
                logger.critical("kill switch itself failed on %s", reason)
        logger.critical("carry book HALTED: %s — %s", reason, detail)
        if record:
            try:
                self.persist(self.to_state())
            except Exception:  # noqa: BLE001 - already halting
                logger.critical("the halt itself could not be written down")
        return CarryDecision("halted", reason=reason, state=BookState.HALTED,
                             detail={"detail": detail})

    @staticmethod
    def _finite(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
