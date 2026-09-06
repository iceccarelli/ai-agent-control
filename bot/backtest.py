"""backtest.py — replay the LIVE code path over historical candles.

WHY THIS EXISTS
===============
The project had no backtester at all. Not a broken one — none. Every performance
claim ever attached to this bot was therefore unfalsifiable.

WHAT MAKES THIS ONE HONEST
--------------------------
It plugs in at the **transport seam**, not above it. ``SimulatedExchange``
implements the same ``Transport`` interface that ``RequestsTransport`` does, so
a backtest drives the *real* ``BybitClient``, the *real* ``BillionaireRiskManager``,
the *real* ``BillionairePositionSizing`` and the *real* ``TradingEngine``:

    strategy -> risk gates -> sizing -> BybitClient -> [SimulatedExchange]

Everything above the seam is production code. The same signing, quantisation,
minimum-notional rules, idempotency and fail-closed gates that run against Bybit
run here. A bug in the live path is a bug in the backtest, which is the only
arrangement under which backtest results mean anything.

WHAT IS MODELLED
----------------
* **Taker and maker fees**, applied on both legs, defaulting to Bybit spot rates.
* **Slippage**, as a fraction of price, applied against the trader.
* **Intrabar stop and take-profit fills**, checked against each bar's high/low.
* **Conservative bar resolution**: if a bar's range touches both the stop and a
  take-profit, the **stop is assumed to fill first**. Real data cannot tell us
  the order, and assuming the favourable one is how backtests lie.

WHAT IS NOT MODELLED
--------------------
Stated plainly, because the gap between these and reality is where over-optimism
lives:

* partial fills and queue position for limit orders (legs fill fully or not at all)
* funding, borrow interest, and exchange downtime
* market impact — size is assumed not to move the book
* latency between signal and submission

Because of the last two in particular, live results should be expected to be
**worse** than backtest results, not merely different.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import config as _config
from bybit_connection import BybitClient, Transport
from persistence import StateStore, TradeRecord
from position_sizing import BillionairePositionSizing
from risk_management import BillionaireRiskManager, normalize_side
from technical_analysis import Candles, MarketStrategy, TechnicalAnalysis
from trade_stats import TradeStats, compute_trade_stats
from trading_engine import TradingEngine

logger = logging.getLogger("backtest")

__all__ = [
    "Bar",
    "SimulatedExchange",
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "walk_forward",
    "monte_carlo_ruin",
    "load_bars_csv",
]


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


#: Ledger rows fetched per harvest. The harvest runs only when the position book
#: changed, so this bounds trades closed in ONE bar, not in the run.
_TRADE_HARVEST_LIMIT = 512

#: "No limit" for a ledger read. SQLite needs a number; this is one no backtest
#: will reach, and it is named so nobody reads it as a meaningful cap.
_ALL_TRADES = 100_000_000


@dataclass(frozen=True)
class Bar:
    """One closed OHLCV candle. ``start_ms`` is the bar's open time, epoch ms."""

    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_kline_row(self) -> List[str]:
        """Bybit V5 kline row shape."""
        return [
            str(self.start_ms), str(self.open), str(self.high),
            str(self.low), str(self.close), str(self.volume), "0",
        ]


def load_bars_csv(path: str, has_header: bool = True) -> List[Bar]:
    """Load bars from CSV: ``timestamp,open,high,low,close,volume``.

    Timestamps may be epoch seconds or milliseconds; both are accepted and
    normalised. Rows that cannot be parsed raise rather than being skipped —
    silently dropping data changes the result and hides the reason.
    """
    import csv

    bars: List[Bar] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if has_header:
            next(reader, None)
        for lineno, row in enumerate(reader, start=2 if has_header else 1):
            if not row:
                continue
            try:
                ts = int(float(row[0]))
                if ts < 10**11:        # epoch seconds
                    ts *= 1000
                bars.append(Bar(ts, float(row[1]), float(row[2]),
                                float(row[3]), float(row[4]), float(row[5])))
            except (ValueError, IndexError) as exc:
                raise ValueError(f"{path}:{lineno}: unparseable row {row!r}") from exc
    bars.sort(key=lambda b: b.start_ms)
    return bars


# ---------------------------------------------------------------------------
# the simulated exchange
# ---------------------------------------------------------------------------


@dataclass
class _SimOrder:
    order_link_id: str
    symbol: str
    side: str
    order_type: str
    qty: float
    price: float
    order_filter: str
    trigger_price: float
    trigger_direction: int
    status: str = "New"
    order_id: str = ""
    avg_price: float = 0.0
    cum_qty: float = 0.0
    purpose: str = "entry"


class SimulatedExchange(Transport):
    """A Bybit V5 stand-in backed by historical bars.

    Implements the ``Transport`` interface so the production ``BybitClient``
    talks to it unmodified — same signing path, same quantisation, same
    idempotency guard, same error classification.
    """

    def __init__(
        self,
        bars_by_symbol: Mapping[str, Sequence[Bar]],
        *,
        starting_cash: float = 10_000.0,
        quote_asset: Optional[str] = None,
        taker_fee: float = 0.001,
        maker_fee: float = 0.001,
        slippage: float = 0.0005,
        instrument_filters: Optional[Mapping[str, Mapping[str, str]]] = None,
        books_by_symbol: Optional[Mapping[str, Sequence[Any]]] = None,
    ) -> None:
        self.bars = {s: list(b) for s, b in bars_by_symbol.items()}
        #: Optional per-bar L2 ladders, index-aligned with ``bars``: entry *i*
        #: is the book as it stood at the close of bar *i*, as
        #: ``(bids, asks)`` in Bybit's ``[[price, size], ...]`` shape, or
        #: ``None`` where no book was recorded for that bar.
        #:
        #: Index alignment is the whole safety property here. A book from bar
        #: i+1 answering a question asked at bar i is lookahead, and it is the
        #: kind of lookahead that flatters a liquidity gate specifically.
        self.books = {s: list(b) for s, b in (books_by_symbol or {}).items()}
        # DERIVED from the symbols, not assumed.
        #
        # This was hardcoded to "USDT" and it silently broke the moment a real
        # corpus arrived: the Bitstamp series is BTC/**USD**, so the client
        # asked for a USD balance while the simulator had seeded USDT, and
        # every single buy came back INSUFFICIENT_USD. 15,668 of them, on a run
        # that otherwise looked like a clean "no trades" result.
        #
        # The lesson is not about currency codes. It is that a default which is
        # right for the data you happen to have is indistinguishable from a
        # correct implementation until the data changes — and the failure it
        # produces looks exactly like a strategy that declined to trade.
        self.quote_asset = quote_asset or self._infer_quote_asset(self.bars)
        self.taker_fee = float(taker_fee)
        self.maker_fee = float(maker_fee)
        self.slippage = float(slippage)

        self.balances: Dict[str, float] = {self.quote_asset: float(starting_cash)}
        self.orders: Dict[str, _SimOrder] = {}
        self._order_seq = 0
        self.index = 0                     # index of the current (in-progress) bar
        self.fees_paid = 0.0
        self.fill_log: List[Dict[str, Any]] = []

        self.filters = dict(instrument_filters or {})
        for symbol in self.bars:
            self.filters.setdefault(symbol, {
                "tickSize": "0.01",
                "basePrecision": "0.000001",
                "minOrderQty": "0.000001",
                "minOrderAmt": "1",
            })

    # -- clock -------------------------------------------------------------

    @property
    def primary(self) -> str:
        return next(iter(self.bars))

    def current_bar(self, symbol: Optional[str] = None) -> Bar:
        return self.bars[symbol or self.primary][self.index]

    def equity(self) -> float:
        """Cash plus marked-to-market position value at the current bar close."""
        total = self.balances.get(self.quote_asset, 0.0)
        for symbol, series in self.bars.items():
            base = self._base(symbol)
            held = self.balances.get(base, 0.0)
            if held:
                total += held * series[self.index].close
        return total

    #: Longest first, so USDT is matched before USD. Reversing this order maps
    #: BTCUSDT to base "BTCUSD"/quote "T", which is the kind of bug that
    #: produces a valid-looking order for an asset that does not exist.
    QUOTE_ASSETS = ("USDT", "USDC", "USD", "EUR", "BTC", "ETH")

    @classmethod
    def _infer_quote_asset(cls, bars: Mapping[str, Any]) -> str:
        """The quote currency every symbol in this run shares.

        Raises when the symbols disagree rather than picking one. A simulator
        holding a single cash balance cannot correctly price a mixed-quote
        universe, and a run that quietly settled BTCUSD and ETHEUR into the
        same pot would produce a plausible equity curve that means nothing.
        """
        found = set()
        for symbol in bars:
            for quote in cls.QUOTE_ASSETS:
                if symbol.upper().endswith(quote):
                    found.add(quote)
                    break
            else:
                raise ValueError(
                    f"cannot determine the quote asset for {symbol!r}; pass "
                    "quote_asset= explicitly rather than letting the simulator guess"
                )
        if len(found) > 1:
            raise ValueError(
                f"symbols use more than one quote asset ({sorted(found)}); this "
                "simulator holds one cash balance and cannot price that honestly"
            )
        return found.pop() if found else "USDT"

    def _base(self, symbol: str) -> str:
        return symbol[: -len(self.quote_asset)]

    # -- stepping ----------------------------------------------------------

    def step(self) -> bool:
        """Advance one bar, resolving resting orders against its range.

        Returns False when the data is exhausted.
        """
        if self.index >= len(self.bars[self.primary]) - 1:
            return False
        self.index += 1
        for symbol, series in self.bars.items():
            self._resolve_bar(symbol, series[self.index])
        return True

    def _resolve_bar(self, symbol: str, bar: Bar) -> None:
        """Fill any resting order this bar's range would have touched.

        **Conservative rule:** if the bar touches both a stop and a take-profit,
        the stop fills first. The bar tells us the range, not the path, and
        choosing the favourable order is the classic way backtests flatter
        themselves.
        """
        resting = [
            o for o in self.orders.values()
            if o.symbol == symbol and o.status in ("New", "Untriggered")
        ]
        stops = [o for o in resting if o.order_filter == "StopOrder"]
        limits = [o for o in resting if o.order_filter != "StopOrder"
                  and o.order_type == "Limit"]

        exited = False
        for order in stops:
            touched = (
                bar.low <= order.trigger_price
                if order.trigger_direction == 2
                else bar.high >= order.trigger_price
            )
            if touched:
                # A stop becomes a market order: it fills at the trigger, with
                # slippage against us.
                self._execute(order, order.trigger_price, taker=True)
                if order.status == "Filled":
                    exited = True

        if exited:
            # OCO, enforced WITHIN the bar. Bybit does not link a conditional
            # stop to separate take-profit legs, so nothing on the exchange
            # prevents both from filling if a single candle sweeps through both
            # levels. Left unmodelled, the simulator would sell more than the
            # position holds and report exits that cannot happen.
            #
            # This mirrors the obligation the live bot carries: a local OCO
            # monitor must cancel the siblings the moment a stop fills. The
            # engine does that on the next cycle (`_cancel_protective_orders`),
            # and the residual intra-candle window is a real, acknowledged risk
            # — recorded in TEST_REPORT.md rather than assumed away.
            for order in limits:
                if order.status == "New":
                    order.status = "Cancelled"
            return

        for order in limits:
            if order.status != "New":
                continue
            filled = (
                bar.high >= order.price if order.side == "Sell"
                else bar.low <= order.price
            )
            if filled:
                self._execute(order, order.price, taker=False)
                if order.status == "Filled":
                    # A take-profit that closes the remaining size makes the
                    # protective stop an orphan; cancel it for the same reason.
                    held = self.balances.get(self._base(symbol), 0.0)
                    if held <= 1e-12:
                        for stop in stops:
                            if stop.status == "Untriggered":
                                stop.status = "Cancelled"

    def _execute(self, order: _SimOrder, price: float, *, taker: bool) -> None:
        base = self._base(order.symbol)
        fee_rate = self.taker_fee if taker else self.maker_fee
        fill_price = price * (
            (1 + self.slippage) if (taker and order.side == "Buy")
            else (1 - self.slippage) if taker else 1.0
        )
        notional = order.qty * fill_price
        fee = notional * fee_rate

        if order.side == "Buy":
            if self.balances.get(self.quote_asset, 0.0) < notional + fee:
                order.status = "Rejected"
                return
            self.balances[self.quote_asset] -= notional + fee
            self.balances[base] = self.balances.get(base, 0.0) + order.qty
        else:
            if self.balances.get(base, 0.0) < order.qty - 1e-12:
                order.status = "Rejected"
                return
            self.balances[base] -= order.qty
            self.balances[self.quote_asset] = (
                self.balances.get(self.quote_asset, 0.0) + notional - fee
            )

        self.fees_paid += fee
        order.status = "Filled"
        order.avg_price = fill_price
        order.cum_qty = order.qty
        self.fill_log.append({
            "order_link_id": order.order_link_id, "symbol": order.symbol,
            "side": order.side, "qty": order.qty, "price": fill_price,
            "fee": fee, "purpose": order.purpose, "bar": self.index,
        })

    # -- transport ---------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Tuple[int, str]:
        path = url.split("?", 1)[0]
        for prefix in ("https://api-testnet.bybit.com", "https://api.bybit.com"):
            path = path.replace(prefix, "")
        query = url.split("?", 1)[1] if "?" in url else ""
        query_params = dict(
            kv.split("=", 1) for kv in query.split("&") if "=" in kv
        ) if query else {}
        merged = {**query_params, **dict(params or {})}

        def ok(result: Any) -> Tuple[int, str]:
            return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": result})

        def err(code: int, msg: str) -> Tuple[int, str]:
            return 200, json.dumps({"retCode": code, "retMsg": msg, "result": {}})

        if path == "/v5/market/time":
            ts = self.current_bar().start_ms
            return ok({"timeSecond": str(ts // 1000), "timeNano": str(ts * 1_000_000)})

        if path == "/v5/market/instruments-info":
            symbol = merged.get("symbol", "")
            spec = self.filters.get(symbol)
            if spec is None:
                return ok({"list": []})
            return ok({"list": [{
                "symbol": symbol,
                "priceFilter": {"tickSize": spec["tickSize"]},
                "lotSizeFilter": {
                    "basePrecision": spec["basePrecision"],
                    "minOrderQty": spec["minOrderQty"],
                    "minOrderAmt": spec["minOrderAmt"],
                },
            }]})

        if path == "/v5/market/tickers":
            symbol = merged.get("symbol", "")
            if symbol not in self.bars:
                return ok({"list": []})
            return ok({"list": [{
                "symbol": symbol,
                "lastPrice": str(self.bars[symbol][self.index].close),
            }]})

        if path == "/v5/market/kline":
            symbol = merged.get("symbol", "")
            if symbol not in self.bars:
                return ok({"list": []})
            limit = int(merged.get("limit", 200))
            # Serve up to AND INCLUDING the current bar. The current bar is
            # still forming, and BybitClient.get_klines drops it — so the
            # strategy only ever sees closed candles, exactly as it will live.
            window = self.bars[symbol][: self.index + 1][-limit:]
            return ok({"list": [b.as_kline_row() for b in reversed(window)]})

        if path == "/v5/market/orderbook":
            symbol = merged.get("symbol", "")
            series = self.books.get(symbol)
            if not series:
                # No recorded book for this symbol. Reported as the exchange
                # reports an unavailable book, so the client returns None and
                # the liquidity gate skips — rather than inventing a ladder
                # from the bar, which would make the gate measure nothing while
                # appearing to measure something.
                return err(10001, "no order book recorded for this symbol")
            snapshot = series[self.index] if self.index < len(series) else None
            if not snapshot:
                return err(10001, "no order book at this bar")
            bids, asks = snapshot
            limit = int(merged.get("limit", 25))
            return ok({
                "s": symbol,
                "b": [[str(p), str(q)] for p, q in list(bids)[:limit]],
                "a": [[str(p), str(q)] for p, q in list(asks)[:limit]],
                "ts": self.bars[symbol][self.index].start_ms,
                "u": self.index,
            })

        if path == "/v5/account/wallet-balance":
            return ok({"list": [{
                "totalEquity": str(self.equity()),
                "coin": [
                    {"coin": c, "availableToWithdraw": str(v), "walletBalance": str(v)}
                    for c, v in self.balances.items()
                ],
            }]})

        if path == "/v5/order/create":
            return self._create(json.loads(body or "{}"), ok, err)

        if path in ("/v5/order/realtime", "/v5/order/history"):
            oid = merged.get("orderLinkId", "")
            rows = []
            if oid and oid in self.orders:
                rows = [self._as_row(self.orders[oid])]
            elif not oid:
                rows = [
                    self._as_row(o) for o in self.orders.values()
                    if o.status in ("New", "Untriggered")
                ]
            if path.endswith("realtime"):
                rows = [r for r in rows
                        if r["orderStatus"] in ("New", "Untriggered", "PartiallyFilled")]
            return ok({"list": rows})

        if path == "/v5/order/cancel":
            payload = json.loads(body or "{}")
            oid = payload.get("orderLinkId", "")
            order = self.orders.get(oid)
            if order is None:
                return err(110001, "order not exists")
            if order.status in ("New", "Untriggered"):
                order.status = "Cancelled"
            return ok({"orderLinkId": oid})

        if path == "/v5/order/cancel-all":
            payload = json.loads(body or "{}")
            symbol = payload.get("symbol")
            for order in self.orders.values():
                if symbol and order.symbol != symbol:
                    continue
                if order.status in ("New", "Untriggered"):
                    order.status = "Cancelled"
            return ok({"list": []})

        return err(10001, f"unhandled endpoint {path}")

    def _create(self, body: Dict[str, Any], ok, err) -> Tuple[int, str]:
        oid = body.get("orderLinkId", "")
        if not oid:
            return err(10001, "orderLinkId required")
        if oid in self.orders:
            return err(170130, "duplicate orderLinkId")

        symbol = body.get("symbol", "")
        if symbol not in self.bars:
            return err(10001, f"unknown symbol {symbol}")

        self._order_seq += 1
        order = _SimOrder(
            order_link_id=oid, symbol=symbol, side=body.get("side", ""),
            order_type=body.get("orderType", "Market"),
            qty=float(body.get("qty", 0.0)),
            price=float(body.get("price") or 0.0),
            order_filter=body.get("orderFilter", "Order"),
            trigger_price=float(body.get("triggerPrice") or 0.0),
            trigger_direction=int(body.get("triggerDirection") or 0),
            order_id=f"sim-{self._order_seq}",
        )
        order.purpose = (
            "stop" if order.order_filter == "StopOrder"
            else "tp" if order.order_type == "Limit" else "entry"
        )
        self.orders[oid] = order

        if order.order_filter == "StopOrder":
            order.status = "Untriggered"
        elif order.order_type == "Market":
            self._execute(order, self.bars[symbol][self.index].close, taker=True)
            if order.status == "Rejected":
                return err(110007, "insufficient balance for market order")
        else:
            order.status = "New"
        return ok({"orderId": order.order_id, "orderLinkId": oid})

    @staticmethod
    def _as_row(order: _SimOrder) -> Dict[str, Any]:
        return {
            "orderId": order.order_id, "orderLinkId": order.order_link_id,
            "symbol": order.symbol, "side": order.side,
            "orderType": order.order_type, "qty": str(order.qty),
            "price": str(order.price), "orderStatus": order.status,
            "orderFilter": order.order_filter,
            "triggerPrice": str(order.trigger_price),
            "avgPrice": str(order.avg_price), "cumExecQty": str(order.cum_qty),
        }


# ---------------------------------------------------------------------------
# the backtester
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """Backtest parameters. All rates are FRACTIONS."""

    starting_cash: float = 10_000.0
    taker_fee: float = 0.001
    maker_fee: float = 0.001
    slippage: float = 0.0005
    interval: str = "60"
    higher_interval: Optional[str] = None
    warmup_bars: int = 120
    max_position_pct: float = 0.02
    risk_per_trade_pct: float = 0.005
    min_confidence: float = 0.60
    min_risk_reward: float = 2.0
    max_open_positions: int = 3
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    stop_loss_pct: float = 0.02
    min_component_agreement: float = 0.60
    stop_atr_multiple: float = 2.0
    #: "spot" (long-only) or "linear" (shorts and funding). The simulator
    #: honours the difference; it does not pretend a spot account can short.
    category: str = "spot"
    funding_rate_bps_per_8h: float = 1.0
    max_hold_hours: float = 8.0
    #: ``None`` == derive from costs, which is what live does. Set it only to
    #: measure the effect of a different floor, never to get more trades.
    min_edge_bps: Optional[float] = None
    memory_enabled: bool = False


@dataclass
class BacktestResult:
    """Measured outcome. No projections, no annualised extrapolation.

    WHY THERE IS AN R BLOCK IN HERE
    -------------------------------
    Slice 8 produced a fold with a **54.2% win rate that lost money**, and
    nothing in this class could explain it — ``total_return`` says *that* it
    lost, ``win_rate`` says the entries were fine, and neither says why those
    two facts are compatible. They are compatible when the average loss is
    bigger than the average win, and the only way to see that is to measure
    every trade in units of the risk it took.

    So ``trade_details`` records what R needs (initial stop price, entry and
    exit bar, exit reason, the excursions), and ``r_stats`` is the diagnosis.
    ``total_return`` and ``win_rate`` are untouched and still mean exactly what
    they meant: this is additive.
    """

    bars: int = 0
    trades: int = 0
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    fees_paid: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    trade_returns: List[float] = field(default_factory=list)
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    signals_evaluated: int = 0
    signals_actionable: int = 0
    blocked_reasons: Dict[str, int] = field(default_factory=dict)
    reconciliation_breaks: int = 0
    notes: List[str] = field(default_factory=list)
    #: One dict per closed trade, oldest first, in the shape
    #: ``trade_stats.Trade.from_mapping`` consumes. Kept as plain dicts so a
    #: tool can re-analyse a run — sweep the barrier geometry, split by regime —
    #: without re-running it.
    trade_details: List[Dict[str, Any]] = field(default_factory=list)
    #: The payoff geometry. ``None`` only when no trade closed.
    r_stats: Optional[TradeStats] = None
    #: Trades whose recorded protective stop was on the *wrong* side of the
    #: price they actually filled at. Their risk is not ``|entry - stop|``, so
    #: they are excluded from the R block rather than given an R computed
    #: against a stop that never protected them. Counted rather than hidden:
    #: a non-zero value here is a finding about the live path, not noise.
    stops_on_the_wrong_side: int = 0

    @property
    def total_return(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity

    def summary(self) -> str:
        lines = [
            f"bars replayed      : {self.bars}",
            f"signals evaluated  : {self.signals_evaluated} "
            f"({self.signals_actionable} actionable)",
            f"trades closed      : {self.trades}",
            f"starting equity    : {self.starting_equity:,.2f}",
            f"ending equity      : {self.ending_equity:,.2f}",
            f"total return       : {self.total_return:+.2%}",
            f"fees paid          : {self.fees_paid:,.2f}",
            f"max drawdown       : {self.max_drawdown:.2%}",
        ]
        lines.append(
            f"win rate           : {self.win_rate:.1%}"
            if self.win_rate is not None
            else "win rate           : n/a (no closed trades)"
        )
        lines.append(
            f"expectancy / trade : {self.expectancy:+.4f}"
            if self.expectancy is not None
            else "expectancy / trade : n/a"
        )
        lines.append(f"book/exchange breaks: {self.reconciliation_breaks}")
        if self.stops_on_the_wrong_side:
            # Printed only when it happened, so a clean run's report keeps the
            # exact shape it has always had.
            lines.append(
                f"stops on wrong side: {self.stops_on_the_wrong_side} "
                "(entered beyond their own stop; excluded from the R block)"
            )
        if self.blocked_reasons:
            top = sorted(self.blocked_reasons.items(), key=lambda kv: -kv[1])[:5]
            lines.append("top block reasons  : " +
                         ", ".join(f"{k}={v}" for k, v in top))
        for note in self.notes:
            lines.append(f"note               : {note}")
        if self.r_stats is not None:
            # Appended rather than interleaved so the block above keeps the
            # exact shape every previous report had, and so a reader can see at
            # a glance which numbers are new.
            lines.append("")
            lines.append(self.r_stats.summary())
        return "\n".join(lines)


def _backtest_config_view(cfg: BacktestConfig) -> Any:
    """Build a REAL :class:`config.Config` for the live modules to consume.

    This used to be a hand-written class listing thirty-odd attributes. That is
    the same shape as the defect this project spent a slice removing: a
    hand-maintained mirror of a schema, which is correct exactly until the
    schema grows. It had already gone stale — slice 7 added ``CATEGORY`` and the
    strategy raised ``AttributeError`` on every backtest signal.

    So the view is now produced by ``config.load()`` over an explicit
    environment dict. Three properties follow for free and permanently:

    * every field the live modules can read exists, because the loader built it;
    * every cross-field validation runs, so an incoherent backtest is refused at
      construction rather than producing plausible nonsense;
    * ``os.environ`` is never read and never written, so a backtest cannot be
      influenced by the ambient shell.
    """
    env: Dict[str, str] = {
        # Sandbox. USE_TESTNET=1 means live is never even requested; the
        # simulator IS the exchange, so PAPER_TRADING must be off or the engine
        # would short-circuit before submitting anything.
        "USE_TESTNET": "1",
        "PAPER_TRADING": "0",
        "BYBIT_API_KEY": "sim",
        "BYBIT_API_SECRET": "sim",
        "SYMBOL_FILTERS_TTL": "86400",
        "ORDERLINK_PREFIX": "BT",
        "CATEGORY": cfg.category,
        "USE_LEVERAGE": "0",
        "DEFAULT_LEVERAGE": "1",
        "MAX_LEVERAGE_EU": "1",

        "MAX_POSITION_SIZE_PCT": repr(cfg.max_position_pct),
        "MAX_TOTAL_EXPOSURE_PCT": repr(
            min(1.0, cfg.max_position_pct * cfg.max_open_positions)
        ),
        "MAX_CORRELATION_EXPOSURE_PCT": "1.0",
        "RISK_PER_TRADE_PCT": repr(cfg.risk_per_trade_pct),
        "MAX_DAILY_LOSS_PCT": repr(cfg.max_daily_loss_pct),
        "MAX_DRAWDOWN_PCT": repr(cfg.max_drawdown_pct),
        "STOP_LOSS_PCT": repr(cfg.stop_loss_pct),
        "MAX_OPEN_POSITIONS": str(cfg.max_open_positions),
        # The consecutive-loss breaker and the cooldown are OFF in a backtest on
        # purpose: they are operational safeties, and leaving them on would make
        # the measured edge a function of the safety rather than the strategy.
        # This is a deliberate difference from live and is stated in the notes.
        "MAX_CONSECUTIVE_LOSSES": "100",
        "TRADE_COOLDOWN_SECONDS": "0",
        "CIRCUIT_BREAKER_HOURS": "0",

        "TAKER_FEE_BPS": repr(cfg.taker_fee * 10_000.0),
        "MAKER_FEE_BPS": repr(cfg.maker_fee * 10_000.0),
        "SLIPPAGE_BPS": repr(cfg.slippage * 10_000.0),
        "FUNDING_RATE_BPS_PER_8H": repr(cfg.funding_rate_bps_per_8h),
        "MAX_HOLD_HOURS": repr(cfg.max_hold_hours),

        "MIN_CONFIDENCE": repr(cfg.min_confidence),
        "MIN_COMPONENT_AGREEMENT": repr(cfg.min_component_agreement),
        "MIN_RISK_REWARD_RATIO": repr(cfg.min_risk_reward),
        "STOP_ATR_MULTIPLE": repr(cfg.stop_atr_multiple),
        "PRIMARY_TIMEFRAME": cfg.interval,
        "SECONDARY_TIMEFRAMES": cfg.higher_interval or "",
        "BREAKEVEN_AFTER_FIRST_TP": "0",
        # The reference layer is off by default in a backtest: a throttle
        # derived from the same trades being measured would fold the result back
        # into itself. Turn it on deliberately to measure its effect.
        "MEMORY_ENABLED": "1" if cfg.memory_enabled else "0",
        "STATE_DB_PATH": ":memory:",
    }
    if cfg.min_edge_bps is not None:
        env["MIN_EDGE_BPS"] = repr(cfg.min_edge_bps)
    return _config.load(env)


class Backtester:
    """Replays bars through the live stack."""

    def __init__(
        self,
        bars_by_symbol: Mapping[str, Sequence[Bar]],
        config: Optional[BacktestConfig] = None,
        db_path: str = ":memory:",
        books_by_symbol: Optional[Mapping[str, Sequence[Any]]] = None,
    ) -> None:
        self.bars = {s: list(b) for s, b in bars_by_symbol.items()}
        self.cfg = config or BacktestConfig()
        self.db_path = db_path
        #: Per-bar L2 ladders. Optional: without them the liquidity gate has
        #: nothing to evaluate and abstains, which the result notes record.
        self.books = {s: list(b) for s, b in (books_by_symbol or {}).items()}

    def run(self) -> BacktestResult:
        cfg = self.cfg
        if cfg.category != "spot":
            # SimulatedExchange keeps **cash accounting**: a Buy debits quote
            # and credits base, a Sell does the reverse, and a Sell is rejected
            # when the base balance is short. That is spot, exactly.
            #
            # A perpetual is a different animal: no base asset changes hands,
            # the position is margin-collateralised, unrealised PnL marks
            # against equity continuously, funding accrues every eight hours,
            # and a losing position can be liquidated before its stop is
            # reached. Running the linear path through cash accounting would
            # not fail — it would *succeed*, and produce an equity curve with
            # no funding drag and no liquidation. The number would be wrong in
            # the flattering direction, which is the worst kind of wrong.
            #
            # So this refuses. The linear ORDER path is implemented and tested
            # against a fake exchange (position-attached stops, positionIdx,
            # margin checks, the funding gate); what does not exist yet is a
            # simulator that can price it. Until it does, there is no honest
            # linear backtest to report, and a missing number is better than an
            # invented one.
            raise NotImplementedError(
                f"the simulator models spot cash accounting only; a "
                f"category={cfg.category!r} backtest would omit funding, margin "
                "and liquidation and report a flattering result. The linear "
                "live path is implemented and unit-tested; perp simulation is "
                "not. See MARKET_CATEGORIES.md."
            )
        view = _backtest_config_view(cfg)
        exchange = SimulatedExchange(
            self.bars, starting_cash=cfg.starting_cash,
            taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee,
            slippage=cfg.slippage, books_by_symbol=self.books,
        )
        store = StateStore(self.db_path)
        client = BybitClient(config=view, store=store, transport=exchange)
        risk = BillionaireRiskManager(config=view, store=store)
        sizer = BillionairePositionSizing(config=view, risk_manager=risk)
        engine = TradingEngine(
            client=client, risk_manager=risk, position_sizer=sizer,
            store=store, config=view,
        )
        strategy = MarketStrategy(
            client=client, analyser=TechnicalAnalysis(config=view),
            interval=cfg.interval, higher_interval=cfg.higher_interval,
            config=view,
        )

        result = BacktestResult(starting_equity=cfg.starting_cash)
        exchange.index = min(cfg.warmup_bars, len(self.bars[exchange.primary]) - 2)
        store.update_equity(exchange.equity())

        # An R-multiple needs the risk the trade actually took, and the trade
        # ledger does not carry it: ``TradeRecord`` records what a trade earned,
        # while the stop that defined its risk lives on the *position* row and
        # is deleted the moment the position closes. The same is true of when
        # the trade started — ``opened_epoch`` is wall-clock, not a bar.
        #
        # So the position book is snapshotted each bar and the ledger is
        # harvested whenever it changes. This is pure observation: nothing here
        # feeds back into a decision, and removing it would change no order.
        snapshots: Dict[str, Dict[str, Any]] = {}
        last_trade_id = 0

        while True:
            equity = exchange.equity()
            store.update_equity(equity)
            result.equity_curve.append(equity)

            # Reap positions the simulator closed via a resting stop or TP.
            inventory_before = self._position_inventory(store)
            self._reap_filled_exits(exchange, store, result, engine)
            if self._position_inventory(store) != inventory_before:
                # Every path that writes a trade either removes the position or
                # shrinks it, so a changed inventory is an exact trigger for
                # "the ledger may have grown" — and polling the ledger only
                # then keeps this O(trades) rather than O(bars).
                last_trade_id = self._harvest_trades(
                    exchange, store, result, snapshots, last_trade_id
                )

            # Reconcile the position book against exchange inventory every bar.
            # This check is what surfaced the same-symbol re-entry collision:
            # the book silently overwrote a position while the exchange kept
            # both lots, so the equity curve and the trade ledger disagreed by
            # three orders of magnitude. A backtest that does not verify its own
            # bookkeeping cannot be evidence for anything.
            drift = self._book_vs_exchange_drift(exchange, store)
            if drift:
                result.reconciliation_breaks += 1
                if result.reconciliation_breaks <= 3:
                    result.notes.append(
                        f"bar {result.bars}: position book disagrees with exchange "
                        f"inventory: {drift}"
                    )

            if not risk.should_halt_trading():
                for symbol in self.bars:
                    result.signals_evaluated += 1
                    intent = strategy.signal_for(symbol)
                    if intent is None:
                        last = strategy.last_signal
                        if last is not None:
                            reason = last.reason.split(":")[0]
                            result.blocked_reasons[reason] = (
                                result.blocked_reasons.get(reason, 0) + 1
                            )
                        continue
                    result.signals_actionable += 1
                    report = engine.execute(intent)
                    if not report.ok:
                        key = report.reason.split(":")[0]
                        result.blocked_reasons[key] = (
                            result.blocked_reasons.get(key, 0) + 1
                        )

            # After the entries for this bar, so a position that filled now is
            # stamped with the bar it actually filled on and with the stop it
            # was protected by.
            self._snapshot_open_positions(exchange, store, snapshots)

            if not exchange.step():
                break
            result.bars += 1

        # Close anything still open at the final bar, so the equity curve is real.
        for position in list(store.open_positions()):
            engine.close_position(symbol=str(position["symbol"]), reason="backtest_end")
        last_trade_id = self._harvest_trades(
            exchange, store, result, snapshots, last_trade_id
        )

        result.ending_equity = exchange.equity()
        result.fees_paid = exchange.fees_paid
        result.net_pnl = result.ending_equity - result.starting_equity

        # ``recent_trades()`` defaults to the newest 200 rows. Reading the
        # ledger through that default meant a run with more than 200 trades
        # reported 200 — and slice 8's fold 2 closed 192, which is close enough
        # to that ceiling that the next parameter change could have crossed it
        # silently. The ledger is the source of truth for the headline block, so
        # it is read in full.
        trades = store.recent_trades(_ALL_TRADES)
        result.trades = len(trades)
        if trades:
            nets = [float(t["net_pnl"]) for t in trades]
            wins = [x for x in nets if x > 0]
            result.win_rate = len(wins) / len(nets)
            result.expectancy = sum(nets) / len(nets)
            result.trade_returns = [
                float(t["net_pnl"]) / max(1e-12, abs(float(t["qty"]) * float(t["entry_price"])))
                for t in trades
            ]
        result.max_drawdown = self._max_drawdown(result.equity_curve)

        if len(result.trade_details) != result.trades:
            # The harvest and the ledger are two independent counts of the same
            # thing. Saying so when they disagree is the same reflex as the
            # book-versus-exchange reconciliation above: a measurement that
            # cannot check itself is not evidence.
            result.notes.append(
                f"harvested {len(result.trade_details)} per-trade records for "
                f"{result.trades} ledger trades; the R block below describes "
                "only the harvested subset"
            )
        if result.trade_details:
            # Round-trip cost on config.py's definition, for spot: two taker
            # legs plus slippage. Funding is a linear-only term and this
            # simulator refuses linear, so it is absent rather than assumed 0.
            round_trip_bps = (
                2.0 * cfg.taker_fee + cfg.slippage
            ) * 10_000.0
            try:
                result.r_stats = compute_trade_stats(
                    result.trade_details, round_trip_cost_bps=round_trip_bps
                )
            except ValueError as exc:
                # A record that cannot be expressed in R is a defect worth
                # shouting about, but it must not delete an equity curve that
                # took an hour to produce. No R block is printed and the reason
                # is in the notes, which are printed.
                result.notes.append(
                    f"R statistics not computed: {exc}"
                )

        if result.trades == 0:
            result.notes.append(
                "no trades were taken — the strategy's confidence never cleared "
                f"MIN_CONFIDENCE={cfg.min_confidence}. This is a real result, not "
                "a failure of the harness; see blocked_reasons."
            )
        store.close()
        return result

    # -- per-trade detail, for the R block ---------------------------------

    @staticmethod
    def _position_inventory(store: StateStore) -> Dict[str, float]:
        """``{symbol: qty}`` for the open book. A change means a trade closed."""
        return {
            str(p["symbol"]): float(p["qty"]) for p in store.open_positions()
        }

    def _snapshot_open_positions(
        self,
        exchange: SimulatedExchange,
        store: StateStore,
        snapshots: Dict[str, Dict[str, Any]],
    ) -> None:
        """Remember the bar and the stop each open position started with.

        Keeping the **first** stop seen for a given position is what makes it
        the *initial* stop rather than whatever it had been moved to by the
        end — and R measured against a stop that has already been trailed to
        break-even is not R, it is a number that flatters every winner.

        Position identity is ``(entry order id, opened_epoch)``, not the symbol:
        the same symbol re-entered after a close is a different trade with a
        different stop, and treating those two as one is the same class of
        defect as the same-symbol re-entry collision slice 6 found.
        """
        for row in store.open_positions():
            symbol = str(row["symbol"])
            identity = (
                str(row.get("order_link_id") or ""),
                float(row.get("opened_epoch") or 0.0),
            )
            stop = float(row.get("stop_price") or 0.0)
            snapshot = snapshots.get(symbol)
            if snapshot is None or snapshot["identity"] != identity:
                snapshots[symbol] = {
                    "identity": identity,
                    "entry_index": exchange.index,
                    # 0.0 is how this stack spells "no stop recorded yet", and
                    # it must not become a stop at price zero.
                    "initial_stop": stop if stop > 0.0 else None,
                }
            elif snapshot["initial_stop"] is None and stop > 0.0:
                snapshot["initial_stop"] = stop

    def _excursion_prices(
        self, symbol: str, entry_index: Optional[int], exit_index: int, side: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Worst and best price reached **after** the entry filled.

        The entry is a market order filling at the close of its bar, so that
        bar's own range happened before we were in the market and is excluded.
        When entry and exit fall on the same bar there is no post-entry range on
        record at all, and both excursions are ``None`` rather than 0.0 — a
        trade that resolved inside one candle has an MAE this data cannot see,
        and 0.0 would claim it never went against us.
        """
        if entry_index is None:
            return None, None
        series = self.bars.get(symbol) or []
        start = int(entry_index) + 1
        end = min(int(exit_index), len(series) - 1)
        if start > end:
            return None, None
        window = series[start:end + 1]
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        if normalize_side(side) == "Buy":
            return lowest, highest
        return highest, lowest

    def _harvest_trades(
        self,
        exchange: SimulatedExchange,
        store: StateStore,
        result: BacktestResult,
        snapshots: Dict[str, Dict[str, Any]],
        last_trade_id: int,
    ) -> int:
        """Copy newly written ledger rows into ``result.trade_details``.

        Rows are matched to the position snapshot for their symbol, which is
        still in hand because the snapshot is only replaced when a *new*
        position appears — so a trade written by the close that removed the
        position still finds the stop it was risking.

        Returns the highest trade id now accounted for.
        """
        rows = [
            row for row in store.recent_trades(_TRADE_HARVEST_LIMIT)
            if int(row.get("id") or 0) > last_trade_id
        ]
        if not rows:
            return last_trade_id
        rows.sort(key=lambda row: int(row["id"]))

        for row in rows:
            symbol = str(row["symbol"])
            side = str(row["side"])
            snapshot = snapshots.get(symbol)
            try:
                meta = json.loads(str(row.get("meta") or "{}"))
            except (TypeError, ValueError):
                meta = {}
            entry_index = None if snapshot is None else snapshot["entry_index"]
            initial_stop = None if snapshot is None else snapshot["initial_stop"]
            entry_price = float(row["entry_price"])
            if initial_stop is not None and (
                (entry_price - initial_stop)
                * (1.0 if normalize_side(side) == "Buy" else -1.0)
            ) <= 0.0:
                # The stop is on the PROFITABLE side of the fill, which means it
                # was never protection: it triggers immediately. This is a real
                # property of the live path, not a bookkeeping artefact — the
                # stop is derived from the signal bar's close while the entry
                # fills at the next bar's close, so a gap wider than the stop
                # distance opens a position that is already past its own stop.
                #
                # The risk taken on such a trade is not |entry - stop|, so no R
                # is claimed for it. Counted and named instead.
                result.stops_on_the_wrong_side += 1
                if result.stops_on_the_wrong_side == 1:
                    result.notes.append(
                        f"bar {entry_index}: {symbol} filled at "
                        f"{entry_price:,.2f} with its protective stop at "
                        f"{initial_stop:,.2f} — the wrong side of the fill for "
                        f"a {side}. The stop was planned against the signal "
                        "bar's close and the entry filled at the next bar's "
                        "close, so the position opened beyond its own stop. "
                        "Excluded from the R block: its risk is not "
                        "|entry - stop|"
                    )
                initial_stop = None
            mae_price, mfe_price = self._excursion_prices(
                symbol, entry_index, exchange.index, side
            )
            result.trade_details.append({
                "symbol": symbol,
                "side": side,
                "qty": float(row["qty"]),
                "entry_price": entry_price,
                "exit_price": float(row["exit_price"]),
                "gross_pnl": float(row["gross_pnl"]),
                "entry_fee": float(row["entry_fee"]),
                "exit_fee": float(row["exit_fee"]),
                "net_pnl": float(row["net_pnl"]),
                "initial_stop_price": initial_stop,
                "entry_index": entry_index,
                "exit_index": exchange.index,
                "exit_reason": str(meta.get("reason") or "unknown"),
                "mae_price": mae_price,
                "mfe_price": mfe_price,
            })
            if snapshot is None and not any(
                n.startswith("a closed trade had no position snapshot")
                for n in result.notes
            ):
                result.notes.append(
                    "a closed trade had no position snapshot, so its risk and "
                    "holding time are unknown and it is excluded from the R "
                    "block rather than given an assumed stop"
                )
        return int(rows[-1]["id"])

    def _reap_filled_exits(
        self, exchange: SimulatedExchange, store: StateStore, result: BacktestResult,
        engine: TradingEngine,
    ) -> None:
        """Feed exchange-side exit fills into the engine's position accounting.

        Delegates to ``TradingEngine.record_exit_fill`` rather than writing
        trades itself, so the backtest and the live path share **one**
        implementation of "a take-profit filled, now what". Duplicating that
        logic here is how a backtest starts measuring something the live bot
        does not do.
        """
        open_symbols = {str(p["symbol"]) for p in store.open_positions()}
        unprocessed = [
            f for f in exchange.fill_log
            if f["purpose"] in ("stop", "tp") and f["symbol"] in open_symbols
        ]
        for fill in unprocessed:
            if fill["symbol"] not in {str(p["symbol"]) for p in store.open_positions()}:
                # A previous fill in this same batch already closed the position
                # and cancelled its siblings; this one is an orphan.
                continue
            engine.record_exit_fill(
                symbol=fill["symbol"], qty=fill["qty"], price=fill["price"],
                fee=fill["fee"], reason=fill["purpose"],
                order_link_id=fill["order_link_id"],
            )
        if unprocessed:
            exchange.fill_log = [
                f for f in exchange.fill_log if f["purpose"] not in ("stop", "tp")
            ]

    @staticmethod
    def _book_vs_exchange_drift(
        exchange: SimulatedExchange, store: StateStore
    ) -> Dict[str, float]:
        """Symbols where the recorded position size differs from real inventory."""
        drift: Dict[str, float] = {}
        booked = {
            str(p["symbol"]): float(p["qty"]) for p in store.open_positions()
        }
        for symbol in exchange.bars:
            held = exchange.balances.get(exchange._base(symbol), 0.0)
            recorded = booked.get(symbol, 0.0)
            difference = held - recorded
            # A difference the exchange would refuse to trade is not a
            # position. Dust is measured in VALUE against the minimum notional,
            # not in units against the minimum quantity: accumulated dust can
            # exceed min_qty in units while still being worth a fraction of a
            # cent and remaining unsellable. Anything worth more than the
            # exchange minimum is a genuine divergence and is reported.
            # Dust the engine has explicitly BOOKED is accounted inventory,
            # not a divergence. Subtracting it here is what makes the check a
            # statement about bookkeeping rather than about tradeability: an
            # unbooked remainder is a break at any size, and a booked one is
            # never a break however large the price grows.
            recorded += store.dust(symbol)
            difference = held - recorded
            price = exchange.bars[symbol][exchange.index].close
            min_notional = float(exchange.filters[symbol]["minOrderAmt"])
            untradeable = (min_notional / price) if price > 0 else 0.0
            if abs(difference) > max(untradeable, 1e-8):
                drift[symbol] = round(difference, 10)
        return drift

    @staticmethod
    def _max_drawdown(curve: Sequence[float]) -> float:
        peak = 0.0
        worst = 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
        return worst


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------


def walk_forward(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    config: Optional[BacktestConfig] = None,
    folds: int = 4,
    train_fraction: float = 0.6,
    books_by_symbol: Optional[Mapping[str, Sequence[Any]]] = None,
) -> List[Dict[str, Any]]:
    """Roll train/test windows forward and report **out-of-sample** results only.

    This implementation does not fit parameters on the training window — the
    strategy has no fitted parameters yet. The training window is therefore used
    only as warm-up, and every reported number is out-of-sample. When parameter
    fitting is added, it goes in the training window and nowhere else.

    Reporting in-sample results as if they were out-of-sample is the single most
    common way a backtest misleads, so the distinction is structural here rather
    than a matter of discipline.
    """
    cfg = config or BacktestConfig()
    primary = next(iter(bars_by_symbol))
    total = len(bars_by_symbol[primary])
    if total < folds * 50:
        raise ValueError(
            f"need at least {folds * 50} bars for {folds} folds, got {total}"
        )

    fold_size = total // folds
    out: List[Dict[str, Any]] = []
    for i in range(folds):
        start = i * fold_size
        end = min(total, start + fold_size)
        train_end = start + int((end - start) * train_fraction)
        if end - train_end < 30:
            continue
        window = {
            symbol: list(bars)[start:end]
            for symbol, bars in bars_by_symbol.items()
        }
        # The books are sliced with the SAME indices as the bars. Any other
        # slicing — or forgetting to slice them at all — would hand fold 2 the
        # book from fold 1's bars, which is not merely wrong but wrong in a
        # direction that is hard to notice.
        book_window = {
            symbol: list(series)[start:end]
            for symbol, series in (books_by_symbol or {}).items()
            if symbol in window
        }
        fold_cfg = BacktestConfig(**{**cfg.__dict__})
        fold_cfg.warmup_bars = train_end - start
        result = Backtester(window, fold_cfg, books_by_symbol=book_window).run()
        out.append({
            "fold": i + 1,
            "train_bars": train_end - start,
            "test_bars": end - train_end,
            "trades": result.trades,
            "return": result.total_return,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "expectancy": result.expectancy,
            "fees": result.fees_paid,
            "blocked_reasons": dict(result.blocked_reasons),
        })
    return out


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def monte_carlo_ruin(
    trade_returns: Sequence[float],
    *,
    starting_equity: float = 10_000.0,
    position_fraction: float = 0.02,
    runs: int = 10_000,
    horizon: Optional[int] = None,
    ruin_threshold: float = 0.5,
    seed: int = 12345,
) -> Dict[str, Any]:
    """Bootstrap the trade sequence to estimate the probability of ruin.

    Resamples the observed per-trade returns **with replacement**, which
    deliberately destroys ordering. That answers "how bad could the same edge
    have been in a different order?" — a question a single historical path
    cannot answer.

    ``position_fraction`` is the share of equity a single position represents
    (0.02 == 2%), and is what converts a per-notional trade return into an
    account return. ``ruin_threshold`` is a FRACTION of starting equity
    (0.5 == "lost half").

    Returns ``{"insufficient_data": True}`` rather than a number when there are
    too few trades. A ruin probability from six trades is not a small number, it
    is noise.
    """
    returns = [float(r) for r in trade_returns if math.isfinite(float(r))]
    if len(returns) < 30:
        return {
            "insufficient_data": True,
            "trades_available": len(returns),
            "trades_required": 30,
            "note": "ruin probability not estimated; too few trades to resample",
        }

    rng = random.Random(seed)
    horizon = horizon or len(returns)
    final_equities: List[float] = []
    ruined = 0
    worst_drawdowns: List[float] = []

    for _ in range(runs):
        equity = starting_equity
        peak = equity
        worst = 0.0
        for _ in range(horizon):
            sampled = returns[rng.randrange(len(returns))]
            # `sampled` is a return per unit of NOTIONAL, so it must be scaled
            # by the fraction of equity that notional represents.
            #
            # An earlier version wrote `risk_fraction * sampled / risk_fraction`,
            # in which the factor cancels — applying each trade's full notional
            # return to the entire account and producing a wildly optimistic
            # median. Caught by noticing a +186% median on a strategy whose
            # walk-forward mean was negative.
            equity *= (1.0 + position_fraction * sampled)
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak if peak > 0 else 0.0)
            if equity <= starting_equity * (1.0 - ruin_threshold):
                ruined += 1
                break
        final_equities.append(equity)
        worst_drawdowns.append(worst)

    final_equities.sort()
    worst_drawdowns.sort()

    def percentile(values: Sequence[float], q: float) -> float:
        if not values:
            return 0.0
        idx = min(len(values) - 1, max(0, int(q * (len(values) - 1))))
        return values[idx]

    return {
        "insufficient_data": False,
        "runs": runs,
        "horizon": horizon,
        "trades_sampled_from": len(returns),
        "position_fraction": position_fraction,
        "ruin_threshold": ruin_threshold,
        "probability_of_ruin": ruined / runs,
        "median_final_equity": percentile(final_equities, 0.50),
        "p05_final_equity": percentile(final_equities, 0.05),
        "p95_final_equity": percentile(final_equities, 0.95),
        "median_max_drawdown": percentile(worst_drawdowns, 0.50),
        "p95_max_drawdown": percentile(worst_drawdowns, 0.95),
    }
