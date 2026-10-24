#!/usr/bin/env python3
"""Offline evidence validation: what a claimed fill actually rests on.

A fill, a fee or a funding payment recorded by this bot is only a CLAIM until a
venue capture says the same thing. This module classifies every claim into one
of four provenance classes and says, per claim, why it is not resolved:

    SIMULATED             a backtest or paper fill. Never venue evidence.
    LOCAL_RECORD          a local ledger / database event, or a claim with no
                          venue identifiers. It records what the bot believes.
    CLAIMED_TESTNET_FILL  the claim carries testnet/demo venue identifiers, but
                          no verified capture reproduces it (or the capture
                          contradicts it, or nothing corroborates it).
    VENUE_RECONCILED      the claim matches, field for field, a row in a
                          hash-chain-verified capture taken from the exact host
                          of the claimed environment, AND a second, different
                          kind of venue record corroborates it. A FILL is
                          corroborated by an order row (cumExecQty covers it)
                          or by a private-WS event restating the same fill. A
                          FUNDING payment has only one venue endpoint in the
                          capture set (the account transaction log), so its
                          corroboration is the row's own wallet-change
                          arithmetic (`change = funding + cashFlow - fee`);
                          that is weaker, and is stated here rather than
                          implied.

ONLY VENUE_RECONCILED IS RESOLVED. Everything else is unresolved and carries
machine-readable issue codes. `pnl_claim_allowed` is False unless every claim in
the input is resolved and nothing is flagged; this module never computes a
final PnL, and a local ledger entry alone can never produce one.

WHAT A CAPTURE CAN AND CANNOT PROVE
-----------------------------------
`venue_evidence` chains records by hash. That detects an edit, insertion or
deletion AFTER capture; it does not authenticate who wrote the file (there is no
signature), so a fabricated chain is internally consistent. Reconciliation is
therefore a consistency check against captured venue rows, not a notarisation:
the highest class means "the captured venue records agree with the claim and
with each other", and the real confirmation is still the account owner reading
the venue's own history. No capture ships with this repository; the real backup
is validated by pointing the CLI at it.

Pure and offline: no network, no clock (callers pass `now_ms`), no writes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import venue_evidence as ve  # noqa: E402

SIMULATED = "SIMULATED"
LOCAL_RECORD = "LOCAL_RECORD"
CLAIMED_TESTNET_FILL = "CLAIMED_TESTNET_FILL"
VENUE_RECONCILED = "VENUE_RECONCILED"
CLASSES = (SIMULATED, LOCAL_RECORD, CLAIMED_TESTNET_FILL, VENUE_RECONCILED)

#: Sources a claim may state about itself.
SOURCES = frozenset({"simulation", "local", "testnet_claim"})
#: Evidence older than this cannot be a trade of this system.
EARLIEST_PLAUSIBLE_MS = 1_420_070_400_000          # 2015-01-01 UTC
#: A venue stamp this far ahead of `now` is a clock fault, not a fill.
MAX_FUTURE_SKEW_MS = 5 * 60_000
QTY_REL_TOL = 1e-9
PRICE_REL_TOL = 1e-9
FEE_ABS_TOL = 1e-9
SIDES = frozenset({"Buy", "Sell"})
SANDBOXES = frozenset({"testnet", "demo"})


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

def _blank(value: Any) -> bool:
    try:
        return value is None or str(value).strip() == ""
    except (ValueError, TypeError):            # e.g. an int of 10**5000 digits
        return False


def _s(value: Any) -> str:
    """str() that cannot raise (a 5,000-digit integer refuses to stringify)."""
    try:
        return str(value)
    except (ValueError, TypeError, RecursionError):
        return "<unreadable>"


def _num(value: Any) -> Optional[float]:
    """A finite float, or None. Booleans and blanks are not numbers; neither is
    anything that cannot be read (huge ints, containers)."""
    if isinstance(value, bool) or _blank(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and abs(number) < 1e18 else None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _is_str_in(value: Any, allowed: Any) -> bool:
    return isinstance(value, str) and value in allowed


def _close(a: float, b: float, rel: float, absolute: float = 0.0) -> bool:
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


# ---------------------------------------------------------------------------
# the capture chain
# ---------------------------------------------------------------------------

class Capture:
    """A loaded evidence chain: the records and whether the chain verifies."""

    def __init__(self, records: List[Dict[str, Any]], chain_issues: List[str],
                 label: str = "") -> None:
        self.records = records
        self.chain_issues = chain_issues
        self.label = label

    @property
    def intact(self) -> bool:
        return not self.chain_issues


def load_capture(path: str) -> Capture:
    """Read an evidence JSONL file WITHOUT trusting it, and never raise on a
    bad line: a corrupt file is reported as a broken chain, not a crash.
    The file is opened read-only and never modified."""
    records: List[Dict[str, Any]] = []
    issues: List[str] = []
    prev = ve.GENESIS_PREV_HASH
    try:
        handle = open(path, encoding="utf-8")
    except OSError as exc:
        return Capture([], [f"unreadable: {exc.__class__.__name__}"], path)
    with handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                issues.append(f"line {lineno}: not valid JSON")
                continue
            if not isinstance(record, dict):
                issues.append(f"line {lineno}: not an object")
                continue
            body = {k: v for k, v in record.items() if k != "record_hash"}
            if record.get("record_hash") != ve.content_hash(body):
                issues.append(f"line {lineno}: record_hash does not match "
                              "its content")
            if record.get("prev_hash") != prev:
                issues.append(f"line {lineno}: prev_hash does not chain")
            prev = str(record.get("record_hash", ""))
            records.append(record)
    return Capture(records, issues, os.path.basename(path))


def capture_from_records(records: Sequence[Dict[str, Any]]) -> Capture:
    """The same verification for records already in memory (tests, tools)."""
    issues: List[str] = []
    prev = ve.GENESIS_PREV_HASH
    kept: List[Dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            issues.append(f"record {index}: not an object")
            continue
        kept.append(record)
        body = {k: v for k, v in record.items() if k != "record_hash"}
        if record.get("record_hash") != ve.content_hash(body):
            issues.append(f"record {index}: record_hash does not match content")
        if record.get("prev_hash") != prev:
            issues.append(f"record {index}: prev_hash does not chain")
        prev = str(record.get("record_hash", ""))
    return Capture(kept, issues)


def _record_environment_ok(record: Mapping[str, Any]) -> Optional[str]:
    """The record's environment when its URL is EXACTLY that venue's host over
    https/wss, else None. Substring matches are not accepted: a look-alike
    host or a path containing the venue name proves nothing."""
    env = str(record.get("environment", ""))
    try:
        parts = urlsplit(str(record.get("request_url", "")))
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 443) or parts.username or parts.password:
        return None
    table = ve.WS_VENUE_HOSTS if record.get("transport") == "ws" else ve.VENUE_HOSTS
    expected = table.get(env)
    scheme_ok = parts.scheme == ("wss" if record.get("transport") == "ws"
                                 else "https")
    if env in ve.NETWORKED_ENVIRONMENTS and expected and scheme_ok \
            and parts.hostname == expected:
        return env
    return None


def _ret_ok(response: Mapping[str, Any]) -> bool:
    return (_int(response.get("retCode")) == 0
            and _int(response.get("_status_code", 200)) == 200)


def _rows(response: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = response.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _captured_ms(record: Mapping[str, Any]) -> Optional[int]:
    try:
        stamp = dt.datetime.strptime(str(record.get("captured_at_utc", "")),
                                     "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int(stamp.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _path(record: Mapping[str, Any]) -> str:
    return urlsplit(str(record.get("request_url", ""))).path


class VenueIndex:
    """The verified rows of a capture, keyed for matching. Rows from records
    whose host does not prove their environment are not indexed at all, and a
    record that cannot be read is skipped rather than allowed to raise."""

    def __init__(self, capture: Capture) -> None:
        #: execId -> [(environment, row, captured_ms or None)]
        self.executions: Dict[str, List[Tuple[str, Dict[str, Any], Optional[int]]]] = {}
        #: orderId -> [(environment, row)]
        self.orders: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        #: execId -> [(environment, row)] from private-WS execution events
        self.ws_rows: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
        self.settlements: Dict[Tuple[str, int], List[Tuple[str, Dict[str, Any], Optional[int]]]] = {}
        #: (environment, orderId) / (environment, orderLinkId) -> {execId: row}
        self.fills_by_order: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        self.fills_by_link: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        #: execIds seen with two different bodies in one environment
        self.exec_conflicts: set = set()
        self.skipped = 0
        for record in capture.records:
            try:
                self._add(record)
            except Exception:                      # noqa: BLE001 - never raise
                self.skipped += 1

    def _add(self, record: Mapping[str, Any]) -> None:
        env = _record_environment_ok(record)
        response = record.get("response")
        if env is None or not isinstance(response, dict):
            self.skipped += 1
            return
        if record.get("transport") == "ws":
            request = record.get("request")
            topic = str(request.get("topic", "")) if isinstance(request, dict) else ""
            if topic.startswith("execution"):
                for row in _rows({"result": {"list": response.get("data")}}):
                    if str(row.get("execId", "")):
                        self.ws_rows.setdefault(str(row["execId"]), []) \
                            .append((env, row))
            return
        if record.get("method") != "GET" or not _ret_ok(response):
            return
        stamp = _captured_ms(record)
        path = _path(record)
        if path == "/v5/execution/list":
            for row in _rows(response):
                eid = str(row.get("execId", ""))
                self.executions.setdefault(eid, []).append((env, row, stamp))
                for table, key in ((self.fills_by_order, str(row.get("orderId", ""))),
                                   (self.fills_by_link, str(row.get("orderLinkId", "")))):
                    if not key:
                        continue
                    bucket = table.setdefault((env, key), {})
                    if eid in bucket and bucket[eid] != row:
                        self.exec_conflicts.add((env, eid))
                    bucket[eid] = row
        elif path in ("/v5/order/history", "/v5/order/realtime"):
            for row in _rows(response):
                self.orders.setdefault(str(row.get("orderId", "")), []) \
                    .append((env, row))
        elif path == "/v5/account/transaction-log":
            for row in _rows(response):
                if str(row.get("type", "")) == "SETTLEMENT":
                    t = _int(row.get("transactionTime"))
                    if t is not None:
                        self.settlements.setdefault(
                            (str(row.get("symbol", "")), t), []) \
                            .append((env, row, stamp))


# ---------------------------------------------------------------------------
# fill claims
# ---------------------------------------------------------------------------

def _structural_issues(claim: Mapping[str, Any], now_ms: int) -> List[str]:
    issues: List[str] = []
    for name in ("claim_id", "symbol", "side", "qty", "price", "fee",
                 "exec_time_ms"):
        if _blank(claim.get(name)):
            issues.append(f"MISSING_FIELD:{name}")
    if _s(claim.get("source", "")) not in SOURCES:
        issues.append("BAD_SOURCE")
    if _s(claim.get("environment", "")) not in ve.ENVIRONMENTS:
        issues.append("BAD_ENVIRONMENT")
    side = claim.get("side")
    if not (isinstance(side, str) and side in SIDES) \
            and "MISSING_FIELD:side" not in issues:
        issues.append("BAD_SIDE")
    qty, price, fee = _num(claim.get("qty")), _num(claim.get("price")), \
        _num(claim.get("fee"))
    if claim.get("qty") is not None and (qty is None or qty <= 0):
        issues.append("BAD_QTY")
    if claim.get("price") is not None and (price is None or price <= 0):
        issues.append("BAD_PRICE")
    if claim.get("fee") is not None and fee is None:
        issues.append("BAD_FEE")
    stamp = _int(claim.get("exec_time_ms"))
    if claim.get("exec_time_ms") is not None and (
            stamp is None or stamp < EARLIEST_PLAUSIBLE_MS
            or stamp > now_ms + MAX_FUTURE_SKEW_MS):
        issues.append("BAD_TIMESTAMP")
    source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
    if source == "simulation" and env in ve.NETWORKED_ENVIRONMENTS:
        issues.append("SIMULATION_CLAIMS_A_VENUE")
    if source == "testnet_claim" and env not in SANDBOXES:
        issues.append("TESTNET_CLAIM_ENVIRONMENT_MISMATCH")
    if env == "mainnet":
        issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
    return issues


def _base_class(claim: Mapping[str, Any], issues: List[str]) -> str:
    source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
    if source == "simulation" or env == "simulation":
        return SIMULATED
    if source == "testnet_claim" and env in SANDBOXES:
        if not (_s(claim.get("exec_id", "")).strip()
                and _s(claim.get("order_id", "")).strip()):
            issues.append("NO_VENUE_IDENTIFIERS")
            return LOCAL_RECORD
        return CLAIMED_TESTNET_FILL
    return LOCAL_RECORD


def _match_fill(claim: Mapping[str, Any], row: Mapping[str, Any]) -> List[str]:
    diffs: List[str] = []
    if str(row.get("orderId", "")) != _s(claim.get("order_id", "")):
        diffs.append("order_id")
    link = _s(claim.get("order_link_id", "") or "")
    if link and str(row.get("orderLinkId", "")) != link:
        diffs.append("order_link_id")
    if str(row.get("symbol", "")) != _s(claim.get("symbol", "")):
        diffs.append("symbol")
    if str(row.get("side", "")) != _s(claim.get("side", "")):
        diffs.append("side")
    for name, key, rel, ab in (("qty", "execQty", QTY_REL_TOL, 0.0),
                               ("price", "execPrice", PRICE_REL_TOL, 0.0),
                               ("fee", "execFee", 0.0, FEE_ABS_TOL)):
        a, b = _num(claim.get(name)), _num(row.get(key))
        if a is None or b is None or not _close(a, b, rel, ab):
            diffs.append(name)
    if _int(row.get("execTime")) != _int(claim.get("exec_time_ms")):
        diffs.append("exec_time_ms")
    # a funding, liquidation or ADL row is not an ordinary trade fill
    if str(row.get("execType", "")) != "Trade":
        diffs.append("exec_type")
    currency = _s(claim.get("fee_currency", "") or "")
    if currency and str(row.get("feeCurrency", "")) != currency:
        diffs.append("fee_currency")
    return diffs


def reconcile_fills(claims: Sequence[Mapping[str, Any]], captures: Sequence[Capture],
                    *, now_ms: int) -> List[Dict[str, Any]]:
    claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
              for c in (claims or [])]
    indexes = [(c, VenueIndex(c)) for c in captures]
    chain_broken = any(not c.intact for c in captures)
    out: List[Dict[str, Any]] = []

    # duplicate / conflicting identities, judged across the whole claim set
    by_exec: Dict[str, List[int]] = {}
    link_orders: Dict[str, set] = {}
    for i, c in enumerate(claims):
        eid = _s(c.get("exec_id", "") or "").strip()
        if eid:
            by_exec.setdefault(eid, []).append(i)
        link = _s(c.get("order_link_id", "") or "").strip()
        if link and _s(c.get("order_id", "") or "").strip():
            link_orders.setdefault(link, set()).add(str(c["order_id"]))

    for i, claim in enumerate(claims):
        issues = _structural_issues(claim, now_ms)
        if "_malformed" in claim:
            issues.append("MALFORMED_CLAIM")
        if _s(claim.get("claim_id")) == "<unreadable>":
            issues.append("MALFORMED_CLAIM")
        provenance = _base_class(claim, issues)
        eid = _s(claim.get("exec_id", "") or "").strip()
        group = by_exec.get(eid, []) if eid else []
        duplicate_of_earlier = False
        if len(group) > 1:
            signature = lambda c: (c.get("order_id"), c.get("symbol"),
                                   c.get("side"), _num(c.get("qty")),
                                   _num(c.get("price")), _num(c.get("fee")),
                                   _int(c.get("exec_time_ms")))
            same = all(signature(claims[j]) == signature(claim) for j in group)
            if not same:
                issues.append("CONFLICTING_DUPLICATE")
            elif group[0] != i:
                # an identical repeat: the first copy stands, later ones are
                # flagged and not counted (a replayed event is not a 2nd fill)
                issues.append("DUPLICATE_EXEC_ID")
                duplicate_of_earlier = True
        link = _s(claim.get("order_link_id", "") or "").strip()
        if link and len(link_orders.get(link, ())) > 1:
            issues.append("AMBIGUOUS_ORDER_LINK_ID")
        if chain_broken:
            issues.append("CAPTURE_CHAIN_BROKEN")

        reconciled = False
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(issues):
            reconciled = _reconcile_one(claim, indexes, issues)
        if reconciled and not issues_block(issues):
            provenance = VENUE_RECONCILED
        out.append({"claim_id": _s(claim.get("claim_id", f"#{i}"))[:80],
                    "kind": "fill", "provenance": provenance,
                    "resolved": provenance == VENUE_RECONCILED
                    and not issues_block(issues),
                    "counted": not duplicate_of_earlier,
                    "issues": sorted(set(issues))})
    return out


#: Issues that forbid the upgrade, however well the venue rows match.
_BLOCKING_PREFIXES = ("FEE_UNCONVERTIBLE", "LOCAL_CANCELLED", "NO_FINAL", "EXIT_ALLOCATION", "EXIT_OVERCLAIMED", "ENTRY_OVERCLAIMED",
                      "LOCAL_GROSS", "ORDER_STATUS_CONFLICT", "DUPLICATE_EXCHANGE",
                      "MALFORMED", "CAPTURE_TIME", "MISSING_FIELD", "BAD_", "SIMULATION_", "TESTNET_CLAIM_",
                      "MAINNET_", "NO_VENUE", "DUPLICATE", "CONFLICTING",
                      "AMBIGUOUS", "CAPTURE_CHAIN", "VENUE_", "ORDER_",
                      "NO_CORROBORATION", "NOT_IN_CAPTURE", "ENV_MISMATCH",
                      "CAPTURED_BEFORE")


def issues_block(issues: Iterable[str]) -> bool:
    return any(i.startswith(_BLOCKING_PREFIXES) for i in issues)


def _reconcile_one(claim: Mapping[str, Any], indexes: Sequence[Tuple[Capture, VenueIndex]],
                   issues: List[str]) -> bool:
    eid = _s(claim["exec_id"]).strip()
    every = [(env, row, stamp) for _c, idx in indexes
             for env, row, stamp in idx.executions.get(eid, [])]
    if not every:
        issues.append("NOT_IN_CAPTURE")
        return False
    claimed_env = _s(claim.get("environment"))
    found = [f for f in every if f[0] == claimed_env]
    if not found:
        issues.append("ENV_MISMATCH")
        return False
    distinct = {json.dumps(r, sort_keys=True) for _e, r, _s in found}
    if len(distinct) > 1:
        issues.append("VENUE_ROWS_CONTRADICT")
        return False
    env, row, stamp = found[0]
    diffs = _match_fill(claim, row)
    if diffs:
        issues.extend(f"VENUE_DISAGREES:{d}" for d in diffs)
        return False
    exec_ms = _int(row.get("execTime")) or 0
    if stamp is None:
        issues.append("CAPTURE_TIME_INVALID")
        return False
    if stamp + 1000 < exec_ms:
        issues.append("CAPTURED_BEFORE_EXECUTION")
        return False
    # corroboration: a DIFFERENT kind of venue record says the same thing
    order_rows = [r for _c, idx in indexes
                  for e, r in idx.orders.get(_s(claim["order_id"]), [])
                  if e == env]
    corroborated = False
    for order in order_rows:
        cum = _num(order.get("cumExecQty"))
        qty = _num(claim.get("qty"))
        if (str(order.get("symbol", "")) == _s(claim["symbol"])
                and str(order.get("side", "")) == _s(claim["side"])
                and cum is not None and qty is not None and cum + 1e-12 >= qty):
            corroborated = True
    # a WS event corroborates only if it restates the SAME fill, field for field
    ws = any(e == env and not _match_fill(claim, r)
             for _c, idx in indexes for e, r in idx.ws_rows.get(eid, []))
    if not (corroborated or ws):
        issues.append("NO_CORROBORATION")
        return False
    return True


def reconcile_order_totals(claims: Sequence[Mapping[str, Any]],
                           fills: Sequence[Dict[str, Any]],
                           captures: Sequence[Capture]) -> List[Dict[str, Any]]:
    """Per (environment, order): do the reconciled fills add up to the venue's
    cumExecQty? A fill that reconciles on its own is still not the whole order.
    Snapshots of one order are taken at different times and cumExecQty only
    grows, so the largest one stated is the venue's latest word."""
    indexes = [VenueIndex(c) for c in captures]
    totals: Dict[Tuple[str, str], float] = {}
    for claim, verdict in zip(claims, fills):
        if verdict["resolved"] and verdict["counted"]:
            key = (_s(claim["environment"]), _s(claim["order_id"]))
            totals[key] = totals.get(key, 0.0) + float(_num(claim["qty"]) or 0.0)
    report = []
    for (env, oid), qty in sorted(totals.items()):
        cums = [_num(r.get("cumExecQty")) for idx in indexes
                for e, r in idx.orders.get(oid, []) if e == env]
        cums = [c for c in cums if c is not None]
        latest = max(cums) if cums else None
        complete = latest is not None and _close(qty, latest, QTY_REL_TOL, 1e-12)
        report.append({"environment": env, "order_id": oid,
                       "reconciled_qty": qty, "venue_cum_exec_qty": latest,
                       "complete": complete})
    return report


# ---------------------------------------------------------------------------
# local orders and trades: what the bot's own database says happened
# ---------------------------------------------------------------------------

#: local `orders.status` -> the venue `orderStatus` values that agree with it.
#: Anything not listed (pending, new, unknown, partially_filled...) is a
#: non-final local belief and is never reconciled to a final outcome.
#: A cancelled conditional (stop) order is reported by Bybit as `Deactivated`,
#: and a part-filled order that was then cancelled as `PartiallyFilledCanceled`.
_ORDER_STATUS_AGREES = {
    "filled": {"Filled"},
    "cancelled": {"Cancelled", "Deactivated", "PartiallyFilledCanceled"},
    "rejected": {"Rejected"}}
_FINAL_VENUE_STATES = {"Filled", "Cancelled", "Rejected", "Deactivated",
                       "PartiallyFilledCanceled"}
_LONG = {"Buy", "buy", "long", "LONG"}
_SHORT = {"Sell", "sell", "short", "SHORT"}


def _claim_class(claim: Mapping[str, Any], issues: List[str]) -> str:
    source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
    if source not in SOURCES:
        issues.append("BAD_SOURCE")
    if env not in ve.ENVIRONMENTS:
        issues.append("BAD_ENVIRONMENT")
    if env == "mainnet":
        issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
    if source == "simulation" or env == "simulation":
        return SIMULATED
    if source == "testnet_claim" and env in SANDBOXES:
        return CLAIMED_TESTNET_FILL
    return LOCAL_RECORD


def _order_state(indexes: Sequence[VenueIndex], env: str, order_id: str
                 ) -> Tuple[List[Dict[str, Any]], set, Optional[float]]:
    """(snapshots, final statuses stated, largest cumExecQty) for one order."""
    rows = [r for idx in indexes for e, r in idx.orders.get(order_id, []) if e == env]
    finals = {str(r.get("orderStatus", "")) for r in rows} & _FINAL_VENUE_STATES
    cums = [c for c in (_num(r.get("cumExecQty")) for r in rows) if c is not None]
    return rows, finals, (max(cums) if cums else None)


def _order_fills(indexes: Sequence[VenueIndex], env: str, order_id: str
                 ) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for idx in indexes:
        out.update(idx.fills_by_order.get((env, order_id), {}))
    return out


def _fills_match(rows: Mapping[str, Mapping[str, Any]], symbol: str, side: str) -> bool:
    return bool(rows) and all(
        str(r.get("symbol", "")) == symbol and str(r.get("side", "")) == side
        and str(r.get("execType", "")) == "Trade" for r in rows.values())


def reconcile_orders(claims: Sequence[Mapping[str, Any]],
                     captures: Sequence[Capture]) -> List[Dict[str, Any]]:
    """Does each locally recorded order exist at the venue, with the outcome the
    bot believes? A local `filled` without a venue order id is a belief, not a
    fill; a status the bot has not settled (pending, unknown) stays unresolved."""
    claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
              for c in (claims or [])]
    indexes = [VenueIndex(c) for c in captures]
    broken = any(not c.intact for c in captures)
    ids: Dict[Tuple[str, str], int] = {}
    for c in claims:
        key = (_s(c.get("environment")), _s(c.get("exchange_id", "") or "").strip())
        if key[1]:
            ids[key] = ids.get(key, 0) + 1
    out = []
    for i, claim in enumerate(claims):
        issues: List[str] = []
        if "_malformed" in claim:
            issues.append("MALFORMED_CLAIM")
        if _s(claim.get("claim_id")) == "<unreadable>":
            issues.append("MALFORMED_CLAIM")
        for name in ("claim_id", "order_link_id", "symbol", "side", "status"):
            if _blank(claim.get(name)):
                issues.append(f"MISSING_FIELD:{name}")
        if not (_is_str_in(claim.get("side"), SIDES)) and "MISSING_FIELD:side" not in issues:
            issues.append("BAD_SIDE")
        qty = _num(claim.get("qty"))
        if claim.get("qty") is not None and (qty is None or qty <= 0):
            issues.append("BAD_QTY")
        provenance = _claim_class(claim, issues)
        status = claim.get("status")
        status = status if isinstance(status, str) else ""
        if status not in _ORDER_STATUS_AGREES:
            issues.append(f"ORDER_OUTCOME_UNSETTLED:{status or 'blank'}")
        order_id = claim.get("exchange_id")
        order_id = order_id.strip() if isinstance(order_id, str) else ""
        if provenance == CLAIMED_TESTNET_FILL and not order_id:
            issues.append("NO_VENUE_IDENTIFIERS")
            provenance = LOCAL_RECORD
        if order_id and ids.get((_s(claim.get("environment")), order_id), 0) > 1:
            issues.append("DUPLICATE_EXCHANGE_ID")
        if broken:
            issues.append("CAPTURE_CHAIN_BROKEN")
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(
                [x for x in issues if not x.startswith("ORDER_OUTCOME")]):
            env = _s(claim["environment"])
            rows, finals, cum = _order_state(indexes, env, order_id)
            fills = _order_fills(indexes, env, order_id)
            fill_qty = sum(_num(r.get("execQty")) or 0.0 for r in fills.values())
            if not rows:
                issues.append("NOT_IN_CAPTURE")
            elif not all(str(r.get("symbol", "")) == _s(claim["symbol"])
                         and str(r.get("side", "")) == _s(claim["side"])
                         and str(r.get("orderLinkId", "")) == _s(claim["order_link_id"])
                         for r in rows):
                issues.append("VENUE_DISAGREES:order_identity")
            elif len(finals) > 1 and not (
                    finals <= _ORDER_STATUS_AGREES.get(status, set())):
                issues.append("ORDER_STATUS_CONFLICT")
            elif not finals:
                # the capture only ever saw the order in a live state (e.g. a
                # stop read before it was cancelled): that is stale, not a
                # disagreement - a later read of the order is missing
                issues.append("NO_FINAL_VENUE_STATE")
            elif status in _ORDER_STATUS_AGREES and not (
                    finals <= _ORDER_STATUS_AGREES[status]):
                issues.append("VENUE_DISAGREES:order_status")
            elif status == "filled":
                if qty is None or cum is None or not _close(cum, qty, QTY_REL_TOL, 1e-12):
                    issues.append("VENUE_DISAGREES:filled_qty")
                elif not _fills_match(fills, _s(claim["symbol"]), _s(claim["side"])) \
                        or not _close(fill_qty, qty, QTY_REL_TOL, 1e-12):
                    issues.append("NO_CORROBORATION")
                else:
                    provenance = VENUE_RECONCILED
            elif status in ("cancelled", "rejected"):
                if cum:
                    # the local ledger says nothing executed, the venue says
                    # something did: a partial fill the books may not hold
                    issues.append("LOCAL_CANCELLED_BUT_VENUE_EXECUTED")
                elif cum is None or not _close(fill_qty, cum, QTY_REL_TOL, 1e-12) \
                        or (fills and not _fills_match(
                            fills, _s(claim["symbol"]), _s(claim["side"]))) \
                        or (status == "rejected" and cum):
                    issues.append("VENUE_DISAGREES:cancelled_fills")
                else:
                    provenance = VENUE_RECONCILED
        resolved = provenance == VENUE_RECONCILED and not issues_block(issues) \
            and not any(x.startswith("ORDER_OUTCOME") for x in issues)
        out.append({"claim_id": _s(claim.get("claim_id", f"#{i}"))[:80],
                    "kind": "order", "provenance": provenance,
                    "resolved": resolved, "counted": True,
                    "issues": sorted(set(issues))})
    return out


def _leg(idx_list: Sequence[VenueIndex], env: str, *, link: str = "",
         orders: Sequence[str] = ()) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    rows: Dict[str, Dict[str, Any]] = {}
    conflict = False
    for idx in idx_list:
        buckets = ([idx.fills_by_link.get((env, link), {})] if link else []) + \
            [idx.fills_by_order.get((env, o), {}) for o in orders]
        for bucket in buckets:
            for eid, row in bucket.items():
                if eid in rows and rows[eid] != row:
                    conflict = True
                rows[eid] = row
        conflict = conflict or any(k[0] == env and k[1] in rows
                                   for k in idx.exec_conflicts)
    return rows, conflict


def _vwap(rows: Mapping[str, Mapping[str, Any]]) -> Tuple[float, float, Dict[str, float]]:
    """(total qty, vwap price, fees by currency) over venue rows. NaNs and an
    empty fee map when any row is unusable or names no fee currency."""
    bad = (float("nan"), float("nan"), {})
    qty = price_q = 0.0
    fees: Dict[str, float] = {}
    for r in rows.values():
        q, p, f = _num(r.get("execQty")), _num(r.get("execPrice")), _num(r.get("execFee"))
        currency = r.get("feeCurrency")
        if q is None or p is None or f is None or q <= 0 or p <= 0 \
                or not isinstance(currency, str) or not currency.strip():
            return bad
        qty += q
        price_q += q * p
        key = currency.strip().upper()
        fees[key] = fees.get(key, 0.0) + f
    return qty, (price_q / qty if qty else float("nan")), fees


def _fee_in_unit(fees: Mapping[str, float], symbol: str,
                 leg_price: float) -> Optional[float]:
    """The venue fees expressed in the symbol's quote unit, by the SAME rule
    the engine books them with (venue_fees.convert_fee_set): the quote coin as
    it is, the base coin at this leg's own trade price. Any other coin has no
    recorded basis - None, never a guessed rate."""
    import venue_fees
    base, unit = venue_fees.split_symbol_units(symbol)
    if not unit:
        return None
    total = 0.0
    for currency, amount in fees.items():
        if currency == unit.upper():
            total += amount
        elif currency == base.upper():
            total += amount * leg_price
        else:
            return None
    return total


def _leg_orders_corroborated(indexes: Sequence[VenueIndex], env: str,
                             rows: Mapping[str, Mapping[str, Any]],
                             symbol: str, side: str) -> bool:
    """EVERY order that produced a leg row has an order record, of the same
    symbol and side, whose cumExecQty covers the executions captured for it."""
    by_order: Dict[str, float] = {}
    for r in rows.values():
        oid = str(r.get("orderId", ""))
        by_order[oid] = by_order.get(oid, 0.0) + (_num(r.get("execQty")) or 0.0)
    for oid, executed in by_order.items():
        snaps = [r for idx in indexes for e, r in idx.orders.get(oid, []) if e == env]
        if not snaps or not any(
                str(r.get("symbol", "")) == symbol and str(r.get("side", "")) == side
                and (_num(r.get("cumExecQty")) or 0.0) + 1e-12 >= executed * (1 - QTY_REL_TOL)
                for r in snaps):
            return False
        finals = {str(r.get("orderStatus", "")) for r in snaps} & _FINAL_VENUE_STATES
        kinds = {"filled" if f == "Filled" else "cancelled" for f in finals}
        if "Rejected" in finals or len(kinds) > 1:
            return False        # a rejected order has no fills; finals conflict
    return True


def _exit_ids(value: Any) -> Optional[List[str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return None
    ids = []
    for x in value:
        if not isinstance(x, str):
            return None
        if x.strip() and x.strip() not in ids:
            ids.append(x.strip())
    return ids


def reconcile_trades(claims: Sequence[Mapping[str, Any]], captures: Sequence[Capture],
                     *, now_ms: int) -> List[Dict[str, Any]]:
    """Reconcile the bot's own closed-trade rows against venue executions.

    The local `trades` table keeps prices and fees per leg but no `execId`; it
    does keep the entry `orderLinkId` and the exit order ids. So each leg is
    rebuilt from the captured executions of those orders and compared:

    * entry: venue VWAP == the entry price, venue quantity covers the trade, one
      fee currency, and the entry fee is this trade's pro-rata share.
    * exit: the exit price is the venue VWAP only when the exit orders filled
      exactly this trade's quantity; otherwise the split between trade rows is
      not recorded locally and the trade is unresolved (EXIT_ALLOCATION_UNPROVEN).
    * venue quantity is CONSUMED: all claims sharing an entry link may not close
      more than that entry filled, and all claims naming an exit order may not
      close more than that order filled. Claiming the same fills twice resolves
      neither.
    * every order behind a leg needs an order record of the same symbol and
      side that covers the executions; leg times must be coherent.
    * a provisional exit price, an unrealised fee, a missing or inconsistent
      gross P&L is unresolved.
    """
    claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
              for c in (claims or [])]
    indexes = [VenueIndex(c) for c in captures]
    broken = any(not c.intact for c in captures)

    # quantity each claim asks the venue to have supplied, per shared leg
    entry_use: Dict[Tuple[str, str], float] = {}
    exit_use: Dict[Tuple[str, str], float] = {}
    exec_use: Dict[Tuple[str, str], int] = {}
    for c in claims:
        env, q = _s(c.get("environment")), _num(c.get("qty")) or 0.0
        link = c.get("entry_order_link_id")
        if isinstance(link, str) and link.strip():
            key = (env, link.strip())
            entry_use[key] = entry_use.get(key, 0.0) + q
        for oid in _exit_ids(c.get("exit_order_ids")) or []:
            exit_use[(env, oid)] = exit_use.get((env, oid), 0.0) + q
        for eid in _exit_ids(c.get("exit_exec_ids")) or []:
            exec_use[(env, eid)] = exec_use.get((env, eid), 0) + 1

    out = []
    for i, claim in enumerate(claims):
        issues: List[str] = []
        if "_malformed" in claim:
            issues.append("MALFORMED_CLAIM")
        if _s(claim.get("claim_id")) == "<unreadable>":
            issues.append("MALFORMED_CLAIM")
        for name in ("claim_id", "symbol", "side", "qty", "entry_price",
                     "exit_price", "gross_pnl", "entry_fee", "exit_fee",
                     "opened_ms", "closed_ms", "entry_order_link_id"):
            if _blank(claim.get(name)):
                issues.append(f"MISSING_FIELD:{name}")
        qty, entry, exit_ = (_num(claim.get("qty")), _num(claim.get("entry_price")),
                             _num(claim.get("exit_price")))
        provisional = claim.get("price_provisional") is True
        for name, v, bad in (("qty", qty, "BAD_QTY"),
                             ("entry_price", entry, "BAD_PRICE"),
                             ("exit_price", exit_, "BAD_PRICE")):
            if not _blank(claim.get(name)) and (v is None or v <= 0) \
                    and not (name == "exit_price" and provisional):
                issues.append(bad)
        side = claim.get("side")
        direction = (1.0 if _is_str_in(side, _LONG)
                     else -1.0 if _is_str_in(side, _SHORT) else 0.0)
        if direction == 0.0 and "MISSING_FIELD:side" not in issues:
            issues.append("BAD_SIDE")
        opened, closed = _int(claim.get("opened_ms")), _int(claim.get("closed_ms"))
        for name, v in (("opened_ms", opened), ("closed_ms", closed)):
            if not _blank(claim.get(name)) and (
                    v is None or v < EARLIEST_PLAUSIBLE_MS
                    or v > now_ms + MAX_FUTURE_SKEW_MS):
                issues.append("BAD_TIMESTAMP")
        if opened is not None and closed is not None and closed < opened:
            issues.append("BAD_TIMESTAMP")
        for name in ("entry_fee", "exit_fee", "gross_pnl"):
            if not _blank(claim.get(name)) and _num(claim.get(name)) is None:
                issues.append("BAD_FEE" if name != "gross_pnl" else "BAD_GROSS")
        provenance = _claim_class(claim, issues)
        if provisional:
            issues.append("EXIT_PRICE_PROVISIONAL")
        for leg in ("entry", "exit"):
            if claim.get(f"{leg}_fee_realized") is not True:
                issues.append(f"{leg.upper()}_FEE_NOT_REALIZED")
        exit_ids = _exit_ids(claim.get("exit_order_ids"))
        exec_sel = _exit_ids(claim.get("exit_exec_ids"))
        if exit_ids is None or exec_sel is None:
            issues.append("MALFORMED_CLAIM")
            exit_ids, exec_sel = exit_ids or [], exec_sel or []
        link = claim.get("entry_order_link_id")
        link = link.strip() if isinstance(link, str) else ""
        env = _s(claim.get("environment"))
        if provenance == CLAIMED_TESTNET_FILL and not (link and exit_ids):
            issues.append("NO_VENUE_IDENTIFIERS")
            provenance = LOCAL_RECORD
        gross = _num(claim.get("gross_pnl"))
        if None not in (gross, qty, entry, exit_) and direction and not provisional:
            if not _close(gross, direction * (exit_ - entry) * qty, 1e-9, 1e-9):
                issues.append("LOCAL_GROSS_ARITHMETIC")
        if broken:
            issues.append("CAPTURE_CHAIN_BROKEN")
        gate = [x for x in issues if not x.endswith("_NOT_REALIZED")
                and x != "EXIT_PRICE_PROVISIONAL"]
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(gate):
            e_rows, e_conf = _leg(indexes, env, link=link)
            x_rows, x_conf = _leg(indexes, env, orders=exit_ids)
            x_all = x_rows
            if exec_sel and not (e_conf or x_conf):
                # the store names exactly which executions priced this exit
                # (a reconciled provisional exit): the allocation is PROVEN, not
                # inferred from the order total
                if any(e not in x_all for e in exec_sel):
                    issues.append("VENUE_DISAGREES:exit_exec_missing")
                    x_rows = {}
                else:
                    x_rows = {e: x_all[e] for e in exec_sel}
            if e_conf or x_conf:
                issues.append("VENUE_ROWS_CONTRADICT")
            elif "VENUE_DISAGREES:exit_exec_missing" in issues:
                pass
            elif not e_rows or not x_rows:
                issues.append("NOT_IN_CAPTURE")
            elif any(str(r.get("execType", "")) != "Trade" or
                     str(r.get("symbol", "")) != _s(claim["symbol"])
                     for r in list(e_rows.values()) + list(x_rows.values())):
                issues.append("VENUE_DISAGREES:leg_identity")
            else:
                eq, ep, e_fees = _vwap(e_rows)
                xq, xp, x_fees = _vwap(x_rows)
                e_times = [_int(r.get("execTime")) for r in e_rows.values()]
                x_times = [_int(r.get("execTime")) for r in x_rows.values()]
                if any(math.isnan(v) for v in (eq, ep, xq, xp)) \
                        or not e_fees or not x_fees:
                    issues.append("VENUE_DISAGREES:leg_rows_unusable")
                elif None in e_times or None in x_times or not all(
                        t >= EARLIEST_PLAUSIBLE_MS for t in e_times + x_times):
                    issues.append("VENUE_DISAGREES:exec_time_unusable")
                else:
                    want_e, want_x = (("Buy", "Sell") if direction > 0
                                      else ("Sell", "Buy"))
                    if {str(r.get("side")) for r in e_rows.values()} != {want_e} \
                            or {str(r.get("side")) for r in x_rows.values()} != {want_x}:
                        issues.append("VENUE_DISAGREES:side")
                    if eq * (1 + QTY_REL_TOL) + 1e-12 < qty \
                            or xq * (1 + QTY_REL_TOL) + 1e-12 < qty:
                        issues.append("VENUE_DISAGREES:leg_qty")
                    if entry_use.get((env, link), 0.0) > eq * (1 + QTY_REL_TOL) + 1e-12:
                        issues.append("ENTRY_OVERCLAIMED")
                    if exec_sel:
                        if any(exec_use.get((env, e), 0) > 1 for e in exec_sel):
                            issues.append("EXIT_OVERCLAIMED")
                    elif any(exit_use.get((env, o), 0.0) > sum(
                            _num(r.get("execQty")) or 0.0 for r in x_rows.values()
                            if str(r.get("orderId")) == o) * (1 + QTY_REL_TOL) + 1e-12
                             for o in exit_ids):
                        issues.append("EXIT_OVERCLAIMED")
                    # The books hold each fee in the symbol's quote unit; the
                    # claim's stated fee currency is that accounting unit.
                    import venue_fees
                    _base, unit = venue_fees.split_symbol_units(_s(claim["symbol"]))
                    for leg in ("entry", "exit"):
                        stated = claim.get(f"{leg}_fee_currency")
                        if isinstance(stated, str) and stated \
                                and stated.upper() != unit.upper():
                            issues.append("VENUE_DISAGREES:fee_currency")
                    if not _close(ep, entry, PRICE_REL_TOL):
                        issues.append("VENUE_DISAGREES:entry_price")
                    # A spot BUY pays its fee in the BASE coin, so the position
                    # (and this trade's quantity) is what was received NET of
                    # that fee. The engine apportions the entry fee by
                    # qty / position, so the same net quantity is the basis here.
                    base_coin = venue_fees.split_symbol_units(_s(claim["symbol"]))[0]
                    eq_net = eq - (e_fees.get(base_coin.upper(), 0.0)
                                   if want_e == "Buy" else 0.0)
                    # A trade that closes (all but lot-step dust of) the net
                    # position carries the WHOLE entry fee, exactly as the
                    # engine books it (closed_qty / held == 1); a smaller
                    # trade carries its pro-rata share.
                    if eq_net <= 0:
                        share = float("nan")
                    elif qty >= eq_net * (1.0 - 1e-3):
                        share = 1.0
                    else:
                        share = qty / eq_net
                    e_fee, x_fee = _num(claim.get("entry_fee")), _num(claim.get("exit_fee"))
                    e_venue = _fee_in_unit(e_fees, _s(claim["symbol"]), entry or ep)
                    x_venue = _fee_in_unit(x_fees, _s(claim["symbol"]), exit_ or xp)
                    if e_venue is None or x_venue is None:
                        issues.append("FEE_UNCONVERTIBLE")
                    if e_venue is not None and (e_fee is None or not _close(
                            e_fee, e_venue * share, 1e-9, FEE_ABS_TOL)):
                        issues.append("VENUE_DISAGREES:entry_fee")
                    exact_exit = _close(xq, qty, QTY_REL_TOL, 1e-12)
                    if provisional:
                        pass
                    elif not exact_exit:
                        issues.append("EXIT_ALLOCATION_UNPROVEN")
                    elif exit_ is None or not _close(xp, exit_, PRICE_REL_TOL):
                        issues.append("VENUE_DISAGREES:exit_price")
                    elif x_venue is not None and (x_fee is None or not _close(
                            x_fee, x_venue, 1e-9, FEE_ABS_TOL)):
                        issues.append("VENUE_DISAGREES:exit_fee")
                    if max(e_times) > min(x_times):
                        issues.append("VENUE_DISAGREES:exit_before_entry")
                    if opened is not None and abs(opened - min(e_times)) > 600_000:
                        issues.append("VENUE_DISAGREES:opened_time")
                    if closed is not None and abs(closed - max(x_times)) > 600_000:
                        issues.append("VENUE_DISAGREES:closed_time")
                    if not (_leg_orders_corroborated(indexes, env, e_rows,
                                                     _s(claim["symbol"]), want_e)
                            and _leg_orders_corroborated(indexes, env, x_rows,
                                                         _s(claim["symbol"]), want_x)):
                        issues.append("NO_CORROBORATION")
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(issues) \
                and not any(x in issues for x in (
                    "EXIT_PRICE_PROVISIONAL", "ENTRY_FEE_NOT_REALIZED",
                    "EXIT_FEE_NOT_REALIZED")):
            provenance = VENUE_RECONCILED
        out.append({"claim_id": _s(claim.get("claim_id", f"#{i}"))[:80],
                    "kind": "trade", "provenance": provenance,
                    "resolved": provenance == VENUE_RECONCILED, "counted": True,
                    "issues": sorted(set(issues))})
    return out


# ---------------------------------------------------------------------------
# funding claims
# ---------------------------------------------------------------------------

def reconcile_funding(claims: Sequence[Mapping[str, Any]], captures: Sequence[Capture],
                      *, now_ms: int) -> List[Dict[str, Any]]:
    from carry_broker import funding_sign_check
    claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
              for c in (claims or [])]
    indexes = [VenueIndex(c) for c in captures]
    chain_broken = any(not c.intact for c in captures)
    seen: Dict[Tuple[str, Any], int] = {}
    out = []
    for i, claim in enumerate(claims):
        issues: List[str] = []
        if "_malformed" in claim:
            issues.append("MALFORMED_CLAIM")
        if _s(claim.get("claim_id")) == "<unreadable>":
            issues.append("MALFORMED_CLAIM")
        for name in ("claim_id", "symbol", "time_ms", "amount"):
            if _blank(claim.get(name)):
                issues.append(f"MISSING_FIELD:{name}")
        amount, stamp = _num(claim.get("amount")), _int(claim.get("time_ms"))
        if claim.get("amount") is not None and amount is None:
            issues.append("BAD_AMOUNT")
        if claim.get("time_ms") is not None and (
                stamp is None or stamp < EARLIEST_PLAUSIBLE_MS
                or stamp > now_ms + MAX_FUTURE_SKEW_MS):
            issues.append("BAD_TIMESTAMP")
        source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
        if source not in SOURCES:
            issues.append("BAD_SOURCE")
        if env not in ve.ENVIRONMENTS:
            issues.append("BAD_ENVIRONMENT")
        if env == "mainnet":
            issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
        key = (_s(claim.get("symbol")), stamp)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            issues.append("DUPLICATE_SETTLEMENT")
        if chain_broken:
            issues.append("CAPTURE_CHAIN_BROKEN")
        if source == "simulation" or env == "simulation":
            provenance = SIMULATED
        elif source == "testnet_claim" and env in SANDBOXES:
            provenance = CLAIMED_TESTNET_FILL
        else:
            provenance = LOCAL_RECORD
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(issues):
            every = [(e, r, st) for idx in indexes
                     for e, r, st in idx.settlements.get(key, [])]
            rows = [x for x in every if x[0] == env]
            if not every:
                issues.append("NOT_IN_CAPTURE")
            elif not rows:
                issues.append("ENV_MISMATCH")
            elif len({json.dumps(r, sort_keys=True) for _e, r, _s in rows}) > 1:
                issues.append("VENUE_ROWS_CONTRADICT")
            else:
                e, row, cap_ms = rows[0]
                funding = _num(row.get("funding"))
                if cap_ms is None:
                    issues.append("CAPTURE_TIME_INVALID")
                elif stamp is not None and cap_ms + 1000 < stamp:
                    issues.append("CAPTURED_BEFORE_EXECUTION")
                elif funding is None or amount is None \
                        or not _close(funding, amount, 1e-9, 1e-12):
                    issues.append("VENUE_DISAGREES:amount")
                else:
                    verdict, _why = funding_sign_check(
                        funding, row.get("change"), row.get("cashFlow"),
                        row.get("fee"))
                    if verdict == "confirmed":
                        provenance = VENUE_RECONCILED
                    else:
                        issues.append(f"VENUE_SIGN_{verdict.upper()}")
        out.append({"claim_id": _s(claim.get("claim_id", f"#{i}"))[:80],
                    "kind": "funding", "provenance": provenance,
                    "resolved": provenance == VENUE_RECONCILED
                    and not issues_block(issues),
                    "counted": seen[key] == 1, "issues": sorted(set(issues))})
    return out


# ---------------------------------------------------------------------------
# local ledger summaries: counts and totals carry no identifiers
# ---------------------------------------------------------------------------

def summarise_ledger(payload: Mapping[str, Any], fills: Sequence[Dict[str, Any]],
                     fill_claims: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """A `cash_ledger/*` file is a LOCAL_RECORD, always. It states how many
    fills there were and what they cost but names none of them, so it cannot be
    reconciled by itself; here it is only cross-checked against the claims."""
    if not isinstance(payload, Mapping):
        return {"kind": "ledger_summary", "schema": "", "provenance": LOCAL_RECORD,
                "resolved": False, "issues": ["MALFORMED_LEDGER",
                                              "UNKNOWN_LEDGER_SCHEMA"]}
    issues: List[str] = ["LEDGER_SUMMARY_HAS_NO_FILL_IDENTIFIERS"]
    schema = str(payload.get("schema", ""))
    if schema not in ("cash_ledger/1", "cash_ledger/2"):
        issues.append("UNKNOWN_LEDGER_SCHEMA")
    stated = _int(payload.get("fills", payload.get("n_fills")))
    counted = [v for v in fills if v["counted"]]
    if stated is None:
        issues.append("LEDGER_FILL_COUNT_MISSING")
    elif stated != len(counted):
        issues.append("LEDGER_FILL_COUNT_DISAGREES")
    if payload.get("fees_confirmed") is False:
        issues.append("LEDGER_FEES_UNCONFIRMED")
    status = str(payload.get("funding_status", ""))
    if status and status not in ("CONFIRMED_FROM_SETTLEMENTS",):
        issues.append(f"LEDGER_FUNDING_{status}")
    ledger_fee = _num(payload.get("fees_usd"))
    claimed_fee = sum(_num(c.get("fee")) or 0.0
                      for c, v in zip(fill_claims, fills) if v["counted"])
    if ledger_fee is None:
        issues.append("LEDGER_FEES_MISSING")
    elif counted and not _close(ledger_fee, claimed_fee, 1e-6, 1e-9):
        issues.append("LEDGER_FEES_DISAGREE_WITH_FILLS")
    if payload.get("allows_live") is not False:
        issues.append("LEDGER_DOES_NOT_STATE_ALLOWS_LIVE_FALSE")
    return {"kind": "ledger_summary", "schema": schema,
            "provenance": LOCAL_RECORD, "resolved": False,
            "issues": sorted(set(issues))}


# ---------------------------------------------------------------------------
# the whole report
# ---------------------------------------------------------------------------

def reconcile(*, fill_claims: Sequence[Mapping[str, Any]] = (),
              funding_claims: Sequence[Mapping[str, Any]] = (),
              trade_claims: Sequence[Mapping[str, Any]] = (),
              order_claims: Sequence[Mapping[str, Any]] = (),
              ledgers: Sequence[Mapping[str, Any]] = (),
              captures: Sequence[Capture] = (), now_ms: int) -> Dict[str, Any]:
    fill_claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
                   for c in (fill_claims or [])]
    fills = reconcile_fills(fill_claims, captures, now_ms=now_ms)
    funding = reconcile_funding(funding_claims, captures, now_ms=now_ms) \
        if funding_claims else []
    orders = reconcile_order_totals(fill_claims, fills, captures)
    trades = reconcile_trades(trade_claims, captures, now_ms=now_ms) \
        if trade_claims else []
    order_claim_verdicts = reconcile_orders(order_claims, captures) \
        if order_claims else []
    summaries = [summarise_ledger(l, fills, fill_claims) for l in ledgers]
    everything = fills + funding + trades + order_claim_verdicts + summaries
    counts = {c: sum(1 for v in everything if v["provenance"] == c)
              for c in CLASSES}
    issue_counts: Dict[str, int] = {}
    for v in everything:
        for i in v["issues"]:
            issue_counts[i] = issue_counts.get(i, 0) + 1
    checked = fills + funding + trades + order_claim_verdicts
    allowed = bool(everything) and all(v["resolved"] for v in checked) \
        and bool(fills or trades) and not summaries_block(summaries) \
        and all(o["complete"] for o in orders) \
        and all(not v["issues"] for v in checked) \
        and all(c.intact for c in captures)
    return {
        "schema": "evidence_reconciliation/1",
        "fills": fills, "funding": funding, "orders": orders,
        "trades": trades, "order_claims": order_claim_verdicts,
        "ledger_summaries": summaries,
        "captures": [{"label": c.label, "records": len(c.records),
                      "intact": c.intact, "chain_issues": c.chain_issues[:20]}
                     for c in captures],
        "counts_by_provenance": counts, "issue_counts": issue_counts,
        "pnl_claim_allowed": allowed,
        "pnl_note": ("False unless every fill and funding claim is "
                     "VENUE_RECONCILED with no issue, every order is complete, "
                     "and every capture chain verifies. Even then this reports "
                     "consistency of captured records, not profitability, and "
                     "testnet dollars are not cash."),
    }


def summaries_block(summaries: Sequence[Mapping[str, Any]]) -> bool:
    """A ledger summary never blocks by being a summary (it always has no ids);
    only a DISAGREEMENT with the reconciled fills does."""
    return any(i.startswith(("LEDGER_FILL_COUNT_DISAGREES",
                             "LEDGER_FEES_DISAGREE", "LEDGER_FEES_MISSING",
                             "LEDGER_FILL_COUNT_MISSING", "UNKNOWN_LEDGER",
                             "LEDGER_FEES_UNCONFIRMED", "LEDGER_FUNDING_", "MALFORMED_LEDGER",
                             "LEDGER_DOES_NOT"))
               for s in summaries for i in s["issues"])


# ---------------------------------------------------------------------------
# the bot's own database -> claims (read-only)
# ---------------------------------------------------------------------------

class StoreUnreadable(ValueError):
    """The database cannot be read as a faithful copy of what it holds."""


def claims_from_store(db_path: str, *, environment: str = "unknown",
                      source: str = "local") -> Dict[str, List[Dict[str, Any]]]:
    """`order_claims` and `trade_claims` from a `persistence.StateStore` file.

    Opened read-only AND immutable, so SQLite cannot create `-wal`/`-shm`
    sidecars or touch the file; nothing is written, migrated or repaired. An
    immutable open would silently ignore committed-but-unmerged WAL content, so
    a database that still has a non-empty `-wal` is REFUSED: copy it together
    with its `-wal`, let SQLite checkpoint the COPY, and point this at that.

    The database does not know which venue it traded on, so the OPERATOR states
    `environment` and `source`: the default `source="local"` makes every row a
    LOCAL_RECORD, and a row only becomes a testnet claim when the caller
    asserts `source="testnet_claim"` for a sandbox environment. A row that
    cannot be read becomes a malformed claim (unresolved), never an exception
    that hides the other rows.
    """
    import sqlite3
    from urllib.parse import quote
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        raise StoreUnreadable(f"no such database file: {os.path.basename(path)}")
    wal = path + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise StoreUnreadable(
            "the database has an unmerged -wal file; an immutable read would "
            "silently ignore it. Checkpoint a COPY of the database first")
    uri = "file:" + quote(path) + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            orders = [dict(r) for r in conn.execute("SELECT * FROM orders")]
            trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StoreUnreadable(f"not a readable state database: {exc}") from exc

    order_claims: List[Dict[str, Any]] = []
    for o in orders:
        try:
            order_claims.append({
                "claim_id": f"order:{o['order_link_id']}", "source": source,
                "environment": environment, "order_link_id": o["order_link_id"],
                "exchange_id": o["exchange_id"], "symbol": o["symbol"],
                "side": o["side"], "qty": o["qty"], "status": o["status"]})
        except (KeyError, TypeError, ValueError):
            order_claims.append({"_malformed": "unreadable order row"})
    trade_claims: List[Dict[str, Any]] = []
    for t in trades:
        try:
            try:
                meta = json.loads(t.get("meta") or "{}")
            except (ValueError, TypeError):
                meta = {}
            ids = meta.get("exit_order_ids") if isinstance(meta, dict) else None
            recon = meta.get("reconciliation") if isinstance(meta, dict) else None
            evid = recon.get("evidence") if isinstance(recon, dict) else None
            exec_ids = evid.get("exec_ids") if isinstance(evid, dict) else None
            booked = meta.get("exit_exec_ids") if isinstance(meta, dict) else None
            if isinstance(booked, list) and booked:
                # recorded with the exit itself, in the same transaction
                exec_ids = booked
            trade_claims.append({
                "claim_id": f"trade:{t['id']}", "source": source,
                "environment": environment, "symbol": t["symbol"],
                "side": t["side"], "qty": t["qty"],
                "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                "gross_pnl": t["gross_pnl"], "entry_fee": t["entry_fee"],
                "exit_fee": t["exit_fee"],
                "entry_fee_currency": t.get("entry_fee_currency") or "",
                "exit_fee_currency": t.get("exit_fee_currency") or "",
                "entry_fee_realized": bool(t.get("entry_fee_realized")),
                "exit_fee_realized": bool(t.get("exit_fee_realized")),
                "price_provisional": bool(t.get("price_provisional")),
                "opened_ms": int(float(t["opened_epoch"]) * 1000),
                "closed_ms": int(float(t["closed_epoch"]) * 1000),
                "entry_order_link_id": (
                    meta.get("entry_order_link_id")
                    if isinstance(meta, dict) and meta.get("entry_order_link_id")
                    else t.get("order_link_id", "")),
                "exit_order_ids": ids, "exit_exec_ids": exec_ids})
        except (KeyError, TypeError, ValueError, OverflowError):
            trade_claims.append({"_malformed": "unreadable trade row"})
    return {"order_claims": order_claims, "trade_claims": trade_claims}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_json_input(path: str, flag: str) -> Any:
    """The parsed JSON object in `path`, or None after saying why on stderr.
    A missing or malformed input is a refusal (exit 3), not a traceback: a
    session that wrote no ledger (zero fills) must read as 'no such file', not
    as a crash that hides what the validator would have said."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        print(f"refusing: {flag} {os.path.basename(path)!r} is not readable "
              f"({exc.strerror or exc.__class__.__name__})", file=sys.stderr)
        return None
    except ValueError:
        print(f"refusing: {flag} {os.path.basename(path)!r} is not valid JSON",
              file=sys.stderr)
        return None
    if not isinstance(value, dict):
        print(f"refusing: {flag} {os.path.basename(path)!r} is not a JSON object",
              file=sys.stderr)
        return None
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claims", help="JSON file: {fill_claims:[...], "
                                     "funding_claims:[...]}")
    ap.add_argument("--evidence", action="append", default=[],
                    help="venue_evidence JSONL chain (repeatable, read-only)")
    ap.add_argument("--store", help="a StateStore SQLite file, opened read-only; "
                                    "its orders and trades become claims")
    ap.add_argument("--store-environment", default="unknown",
                    choices=sorted(ve.ENVIRONMENTS))
    ap.add_argument("--store-source", default="local", choices=sorted(SOURCES),
                    help="what the operator asserts about the store; "
                         "'local' keeps every row a LOCAL_RECORD")
    ap.add_argument("--ledger", action="append", default=[],
                    help="cash_ledger/* JSON (repeatable)")
    ap.add_argument("--now-ms", type=int, required=True,
                    help="reference time in epoch ms (explicit, so a report is "
                         "reproducible)")
    ap.add_argument("--json", default="", help="write the report here "
                                               "(never inside an --evidence file's directory)")
    args = ap.parse_args(argv)
    claims: Dict[str, Any] = {}
    if args.claims:
        claims = _read_json_input(args.claims, "--claims")
        if claims is None:
            return 3
    ledgers = []
    for path in args.ledger:
        payload = _read_json_input(path, "--ledger")
        if payload is None:
            return 3
        ledgers.append(payload)
    captures = [load_capture(p) for p in args.evidence]
    order_claims = list(claims.get("order_claims", []))
    trade_claims = list(claims.get("trade_claims", []))
    if args.store:
        try:
            derived = claims_from_store(args.store,
                                        environment=args.store_environment,
                                        source=args.store_source)
        except StoreUnreadable as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 3
        order_claims += derived["order_claims"]
        trade_claims += derived["trade_claims"]
    report = reconcile(fill_claims=claims.get("fill_claims", []),
                       funding_claims=claims.get("funding_claims", []),
                       order_claims=order_claims, trade_claims=trade_claims,
                       ledgers=ledgers, captures=captures, now_ms=args.now_ms)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        out_dir = os.path.dirname(os.path.abspath(args.json))
        if any(os.path.dirname(os.path.abspath(p)) == out_dir
               for p in args.evidence):
            print("refusing to write the report beside the evidence it read",
                  file=sys.stderr)
            return 3
        os.makedirs(out_dir, exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    return 0 if report["pnl_claim_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
