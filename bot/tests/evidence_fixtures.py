"""Deterministic, SYNTHETIC venue-capture fixtures for evidence reconciliation.

Every identifier, price and quantity here is invented. Nothing is, or resembles,
a real account's trading record, and no credential appears. Hosts are the real
public venue hostnames only because the validator checks them.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, os.path.dirname(HERE))

import venue_evidence as ve  # noqa: E402

GIT = "a" * 40
T0 = 1_760_000_000_000                       # a fixed instant, ms
NOW = T0 + 3_600_000

TESTNET_REST = "https://api-testnet.bybit.com"


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def exec_row(i: int = 1, *, order="ORD-1", link="LNK-1", symbol="BTCUSDT",
             side="Buy", qty="0.002", price="60000", fee="0.00012",
             t: Optional[int] = None) -> Dict[str, Any]:
    return {"execId": f"EXEC-{i}", "orderId": order, "orderLinkId": link,
            "symbol": symbol, "side": side, "execQty": qty, "execPrice": price,
            "execFee": fee, "feeCurrency": "USDT", "execType": "Trade",
            "execTime": str(T0 + 1000 * i if t is None else t)}


def order_row(order="ORD-1", *, cum="0.002", symbol="BTCUSDT", side="Buy",
              status="Filled") -> Dict[str, Any]:
    return {"orderId": order, "symbol": symbol, "side": side,
            "cumExecQty": cum, "orderStatus": status, "qty": cum}


def settlement_row(*, symbol="BTCUSDT", t=T0 + 8 * 3_600_000, funding="-0.05",
                   change="-0.05", cash="0", fee="0") -> Dict[str, Any]:
    return {"type": "SETTLEMENT", "symbol": symbol, "transactionTime": str(t),
            "funding": funding, "change": change, "cashFlow": cash, "fee": fee}


def body(rows: List[Dict[str, Any]], ret=0) -> Dict[str, Any]:
    return {"retCode": ret, "retMsg": "OK", "result": {"list": rows},
            "_status_code": 200}


def record(path: str, rows, *, env="testnet", host=TESTNET_REST, prev: str,
           at: Optional[int] = None, ret=0) -> Dict[str, Any]:
    r = ve.EvidenceRecord(
        venue="bybit", environment=env, captured_at_utc=iso(at or T0 + 60_000),
        method="GET", request_url=host + path, request={"params": {}},
        response=body(rows, ret), order_link_id="", git_commit=GIT,
        prev_hash=prev).to_dict()
    return r


def chain(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`specs`: dicts of kwargs for `record` minus `prev`; returns a valid chain."""
    out: List[Dict[str, Any]] = []
    prev = ve.GENESIS_PREV_HASH
    for spec in specs:
        rec = record(prev=prev, **spec)
        out.append(rec)
        prev = rec["record_hash"]
    return out


def claim(i: int = 1, **over) -> Dict[str, Any]:
    c = {"claim_id": f"C{i}", "source": "testnet_claim",
         "environment": "testnet", "symbol": "BTCUSDT", "side": "Buy",
         "order_id": "ORD-1", "order_link_id": "LNK-1", "exec_id": f"EXEC-{i}",
         "qty": 0.002, "price": 60000.0, "fee": 0.00012,
         "exec_time_ms": T0 + 1000 * i}
    c.update(over)
    return c


def good_chain() -> List[Dict[str, Any]]:
    return chain([
        dict(path="/v5/execution/list", rows=[exec_row(1)]),
        dict(path="/v5/order/history", rows=[order_row()]),
    ])
