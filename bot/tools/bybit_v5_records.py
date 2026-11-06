"""Bybit V5 account records -> the canonical columns of ``owner_ledger`` / ``account_review``. Pure functions over rows ALREADY READ from a local file:
no file, network, credential or venue access happens here, and nothing in this module can place an order.

Only the DOCUMENTED field names and sign conventions of the V5 API reference are used (checked 2026-11-06 against bybit-exchange.github.io/docs/v5):

  execution list   GET /v5/execution/list        execId execTime symbol side execQty execPrice execValue execFee feeCurrency isMaker execType ...
                   execFee > 0 is a fee PAID, < 0 a rebate (stated on the transaction-log page as the opposite of its ``funding``, and on the help centre).
  transaction log  GET /v5/account/transaction-log (UTA)   id tradeId transactionTime type symbol side size currency feeRate funding fee cashFlow change cashBalance
                   ``change = cashFlow + funding - fee``; ``funding`` > 0 is RECEIVED (the classic contract log has the OPPOSITE sign and is NOT accepted);
                   ``fee`` > 0 is an expense, < 0 a rebate; ``size`` is the position left after the trade, signed (a short is negative);
                   on a SETTLEMENT row ``feeRate`` is the market funding rate for side Buy and its negative for side Sell.

Rows are the documented API items (one JSON object per line, string values as the API returns them). The web-UI CSV export is NOT supported directly:
Bybit publishes no column list for it, so a UI export is read only through an explicit ``column_map`` in the manifest that renames its columns to the
documented API names above; this module never guesses a column.

Everything the documentation does not settle is refused or surfaced, never assumed. The list is in ``UNDOCUMENTED`` and travels in every report.
Each output row keeps the venue's own identifiers (``exec_id`` is ``execId``; a log row's id is ``id|tradeId|transactionTime``, because the documentation's own
example repeats an ``id``), so every canonical record is traceable to one raw line.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import account_review as AR

RecordError = AR.RecordError

ADAPTERS = ("bybit_v5_execution", "bybit_v5_spot_execution", "bybit_v5_transaction_log")

DOC_BASIS = {
    "bybit_v5_execution": "https://bybit-exchange.github.io/docs/v5/order/execution (category=linear)",
    "bybit_v5_spot_execution": "https://bybit-exchange.github.io/docs/v5/order/execution (category=spot) and /docs/v5/enum (Spot Fee Currency Instruction)",
    "bybit_v5_transaction_log": "https://bybit-exchange.github.io/docs/v5/account/transaction-log (UTA) and /docs/v5/enum (type(uta-translog))",
}

UNDOCUMENTED = (
    "column names, order, language and signs of the web-UI Transaction Log / Order History CSV (no official list); read them only through an explicit column_map",
    "whether the UI Order History holds one row per execution with an execution id",
    "the fee currency of derivative fills before the 2025-09-18 API change (the field is empty); treated as USDT and flagged FEE_CURRENCY_NOT_STATED",
    "how /v5/execution/list returns execType=Funding rows on a UTA and their sign: such rows are refused; funding is read from the transaction log only",
    "that txlog tradeId equals execution execId, that txlog id is unique, the log's sort order, and the same-millisecond order (every order is tried)",
    "that cashBalance_i = cashBalance_(i-1) + change_i for every row and currency (holds in the documentation's example): a break is reported, never repaired",
    "any historical wallet balance or equity at a past instant (only an end-of-day Account Statement PDF exists): opening and closing balances come from snapshot records",
    "the field semantics of spot rows in the transaction log beyond change/cashBalance, and the USD value of a fee paid in BTC",
    "whether API closedPnl or cumRealisedPnl include funding (help-centre text says the UI figure does); they are not used",
    "whether BTCUSDT has left the 8-hour settlement cycle under dynamic funding frequency (excluded 'initially'): the settlement grid is validated by account_review",
)

# transaction-log types this module maps; EVERYTHING else becomes an UNMAPPED_<TYPE> line that the ledger reports as unclassified (it is never income)
_LOG_TRADE = "TRADE"
_LOG_SETTLEMENT = "SETTLEMENT"
_LOG_IN, _LOG_OUT = "TRANSFER_IN", "TRANSFER_OUT"
_LOG_INTEREST = "INTEREST"
_USD = ("USDT", "USD")
_ASSETS = ("USDT", "USD", "BTC")
_ID_TOL = 1e-8                                   # the API prints eight decimals

# the documented fields each adapter reads: any other column in the file is listed as ignored, never silently used
EXECUTION_USED = ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execValue", "execFee", "feeCurrency", "isMaker", "execType")
SPOT_USED = ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execFee", "feeCurrency", "execType")
TXLOG_USED = ("id", "tradeId", "transactionTime", "type", "category", "symbol", "side", "size", "currency", "feeRate", "funding", "fee", "cashFlow", "change", "cashBalance")


def _need(row: Mapping[str, Any], keys: Sequence[str], where: str) -> None:
    missing = [k for k in keys if k not in row]
    if missing:
        raise RecordError(f"{where}: missing documented field(s) {', '.join(missing)}")


def _f(row: Mapping[str, Any], key: str, where: str) -> float:
    return AR._num(row.get(key), key, where)


def _s(row: Mapping[str, Any], key: str, where: str) -> str:
    return AR._str(row.get(key), key, where)


def _bool(v: Any, name: str, where: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise RecordError(f"{where}: {name} must be a boolean (true = maker), got {v!r}")


def _note(notes: List[Dict[str, Any]], code: str, severity: str, example: str, detail: str) -> None:
    for n in notes:
        if n["code"] == code:
            n["count"] += 1
            if len(n["examples"]) < 5:
                n["examples"].append(example)
            return
    notes.append({"code": code, "severity": severity, "count": 1, "detail": detail, "examples": [example]})


def execution_to_fills(rows: Sequence[Mapping[str, Any]], label: str, symbol: str = "BTCUSDT") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Linear-perpetual executions. Only ``execType == "Trade"`` is accepted: a funding row (sign undocumented on a UTA), a liquidation or ADL fill, a block
    trade or a moved position is a different economic event that must be read from its own record, so each is refused by name."""
    out: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        _need(r, ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execFee", "isMaker", "execType"), w)
        et = _s(r, "execType", w)
        if et != "Trade":
            raise RecordError(f"{w}: execType {et!r} is not 'Trade'. Funding rows (sign undocumented for a UTA) belong to the transaction log; AdlTrade / BustTrade / "
                              "BlockTrade / MovePosition / settlement types are not strategy fills and are not guessed at")
        sym = _s(r, "symbol", w)
        if sym != symbol:
            raise RecordError(f"{w}: symbol {sym!r} is not {symbol!r}")
        side = _s(r, "side", w)
        if side not in ("Buy", "Sell"):
            raise RecordError(f"{w}: side {side!r} is not Buy or Sell")
        qty, price, fee = _f(r, "execQty", w), _f(r, "execPrice", w), _f(r, "execFee", w)
        if "execValue" in r and r.get("execValue") not in (None, ""):
            ev = _f(r, "execValue", w)
            if not math.isclose(ev, qty * price, rel_tol=5e-3, abs_tol=0.01):
                raise RecordError(f"{w}: execValue {ev} is not execQty x execPrice ({qty * price:.8f}): a unit or column mix-up")
        cur = str(r.get("feeCurrency") or "").strip().upper()
        if cur in ("",):
            _note(notes, "FEE_CURRENCY_NOT_STATED", "GAP", str(r.get("execId")),
                  "feeCurrency is empty on these derivative fills (documented only from 2025-09-18): the fee is treated as USDT, which the documentation does not state")
        elif cur not in _USD:
            raise RecordError(f"{w}: feeCurrency {cur!r} is not USDT: a linear BTCUSDT fee is settled in USDT, so another currency is a mapping error")
        out.append({"exec_id": _s(r, "execId", w), "ts_ms": r.get("execTime"), "symbol": sym, "side": side, "qty": qty, "price": price, "fee_usd": fee,
                    "liquidity": "maker" if _bool(r.get("isMaker"), "isMaker", w) else "taker"})
    return out, notes


def spot_execution_to_trades(rows: Sequence[Mapping[str, Any]], label: str, symbol: str = "BTCUSDT") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Spot executions (category=spot). The fee currency must be STATED (``feeCurrency``, documented since 2024-01-08): inferring it from the maker/taker rate rule
    is not done."""
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        _need(r, ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execFee", "feeCurrency", "execType"), w)
        if _s(r, "execType", w) != "Trade":
            raise RecordError(f"{w}: execType {r.get('execType')!r} is not 'Trade'")
        out.append({"trade_id": _s(r, "execId", w), "ts_ms": r.get("execTime"), "symbol": _s(r, "symbol", w), "side": _s(r, "side", w),
                    "qty": _f(r, "execQty", w), "price": _f(r, "execPrice", w), "fee": _f(r, "execFee", w), "fee_coin": _s(r, "feeCurrency", w).upper()})
    return out, []


def transaction_log_to_records(rows: Sequence[Mapping[str, Any]], label: str, symbol: str = "BTCUSDT") -> Dict[str, Any]:
    """UTA transaction-log rows -> funding rows, statement lines (with the running balance), external flows. Returns
    ``{"funding": [...], "cash_lines": [...], "external_flows": [...], "notes": [...]}``."""
    funding: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []
    flows: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        _need(r, ("id", "transactionTime", "type", "currency", "change", "cashBalance", "cashFlow", "funding", "fee"), w)
        cur = _s(r, "currency", w).upper()
        typ = _s(r, "type", w).upper()
        rid = f"{_s(r, 'id', w)}|{str(r.get('tradeId') or '').strip()}|{r.get('transactionTime')}"
        if cur not in _ASSETS:
            _note(notes, "ADAPTER_SKIPPED_ROWS", "GAP", f"{rid}:{cur}",
                  "rows in currencies other than USDT/BTC were not mapped: whatever they did to the account is not explained here, so the balances can no longer be reconciled")
            continue
        change, cash_flow, fund, fee = _f(r, "change", w), _f(r, "cashFlow", w), _f(r, "funding", w), _f(r, "fee", w)
        bal = _f(r, "cashBalance", w)
        if not math.isclose(change, cash_flow + fund - fee, rel_tol=1e-9, abs_tol=_ID_TOL):
            raise RecordError(f"{w}: change {change} != cashFlow {cash_flow} + funding {fund} - fee {fee}: the documented identity fails, so a sign or a column is wrong "
                              "(the classic contract log has the opposite funding sign and is not accepted)")
        ts = r.get("transactionTime")

        def line(suffix: str, category: str, amount: float, with_balance: bool) -> None:
            lines.append({"line_id": f"{rid}#{suffix}", "row_id": rid, "ts_ms": ts, "coin": cur, "category": category, "change": amount,
                          "cash_balance": bal if with_balance else None, "ref_id": rid})

        if typ == _LOG_SETTLEMENT:
            _need(r, ("symbol", "side", "size", "feeRate"), w)
            if cur not in _USD or str(r.get("category") or "linear").lower() != "linear":
                raise RecordError(f"{w}: only a USDT linear-perpetual SETTLEMENT is supported (USDC settlement mixes funding with session settlement)")
            if cash_flow != 0 or fee != 0:
                raise RecordError(f"{w}: a SETTLEMENT row with a cash flow or a fee is a session settlement, not a funding payment")
            if _s(r, "symbol", w) != symbol:
                raise RecordError(f"{w}: symbol {r.get('symbol')!r} is not {symbol!r}")
            side = _s(r, "side", w)
            fr = _f(r, "feeRate", w)
            if side == "Buy":
                rate = fr
            elif side == "Sell":
                rate = -fr
            else:
                raise RecordError(f"{w}: a SETTLEMENT row needs side Buy or Sell to recover the market rate from feeRate (got {side!r})")
            funding.append({"exec_id": rid, "ts_ms": ts, "symbol": symbol, "position_qty": _f(r, "size", w), "rate": rate, "payment_usd": fund})
            line("funding", "FUNDING", fund, True)
        elif typ == _LOG_TRADE:
            cat = str(r.get("category") or "").lower()
            if cat == "spot":
                line("spot", "SPOT", change, True)
            elif cat in ("", "linear"):
                if cur not in _USD:
                    raise RecordError(f"{w}: a linear TRADE row in {cur}: only USDT is supported")
                if cash_flow != 0 or fee == 0:
                    line("cash", "TRADE", cash_flow, fee == 0)
                if fee != 0:
                    line("fee", "FEE" if fee > 0 else "REBATE", -fee, True)
                if fund != 0:
                    raise RecordError(f"{w}: a TRADE row carrying funding is not a shape this module can attribute")
            else:
                raise RecordError(f"{w}: TRADE category {cat!r} is not linear or spot")
        elif typ in (_LOG_IN, _LOG_OUT):
            if (typ == _LOG_IN) != (change > 0) or change == 0:
                raise RecordError(f"{w}: {typ} with change {change}: the sign contradicts the type")
            line("transfer", "EXTERNAL", change, True)
            flows.append({"flow_id": rid, "ts_ms": ts, "coin": cur, "amount": change, "kind": "DEPOSIT" if typ == _LOG_IN else "WITHDRAWAL"})
            _note(notes, "TRANSFERS_TREATED_AS_EXTERNAL", "INFO", rid,
                  "TRANSFER_IN / TRANSFER_OUT are treated as external flows of the trading account (money crossing its boundary): the log does not say what the other side is")
        elif typ == _LOG_INTEREST:
            line("interest", "INTEREST", change, True)
        else:
            line("unmapped", f"UNMAPPED_{typ}", change, True)
    return {"funding": funding, "cash_lines": lines, "external_flows": flows, "notes": notes}
