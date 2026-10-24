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

CARRY DRILL EVIDENCE
--------------------
`tools/drill.py --evidence-path` captures the carry drill's REST traffic and
writes, in its `--out` report, the legs it booked (`claims`). `reconcile_carry_legs`
checks each perp leg against FIVE kinds of captured row: the order-create
request the bot sent (`POST /v5/order/create`, whose `orderLinkId` lives inside
`request.body`), the create response (`orderId`), the order's own final record
(`order/realtime` / `order/history`), its execution rows (`execution/list`),
and, for the cycle, a position read after the close and the account's
SETTLEMENT rows. `reconcile_carry_cycle` then refuses a cycle that has an order
the books do not claim, an exit before the entry, unequal entry and exit
quantities, no proof of a flat position, or a settlement that the books did
not claim. A drill report written before this format existed is read by
`claims_from_drill_report`; what it cannot name is said, not guessed.

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
        #: (environment, orderLinkId) -> [(request body, response result, ms)]
        #: of SUCCESSFUL order-create POSTs: what the bot actually sent.
        self.creates: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], Dict[str, Any], Optional[int]]]] = {}
        #: (environment, orderLinkId) -> [order rows]
        self.orders_by_link: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        #: [(environment, symbol, total |size|, captured_ms, chain order)] from
        #: position/list
        self.positions: List[Tuple[str, str, float, Optional[int], int]] = []
        #: execId -> chain order of the LAST record that carried it
        self.exec_seq: Dict[str, int] = {}
        #: [(environment, path, label)] of every successful POST to an order or
        #: position endpoint, whether or not it carried an order link id
        self.order_posts: List[Tuple[str, str, str]] = []
        #: POSTs to an order/position endpoint whose host proves nothing
        self.unverified_posts = 0
        #: [(environment, captured_ms, startTime, endTime, cursor)] of every
        #: transaction-log read, including ones that returned no row
        self.txlog_reads: List[Tuple[str, Optional[int], Optional[int], Optional[int], str]] = []
        self._n = 0
        self.skipped = 0
        for record in capture.records:
            self._n += 1
            try:
                self._add(record)
            except Exception:                      # noqa: BLE001 - never raise
                self.skipped += 1

    def _add(self, record: Mapping[str, Any]) -> None:
        env = _record_environment_ok(record)
        response = record.get("response")
        if env is None or not isinstance(response, dict):
            self.skipped += 1
            if record.get("method") == "POST" and _path(record).startswith(
                    ("/v5/order/", "/v5/position/")):
                self.unverified_posts += 1
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
        if record.get("method") == "POST" and _path(record).startswith(
                ("/v5/order/", "/v5/position/")) and _ret_ok(response):
            result = response.get("result")
            label = ""
            if isinstance(result, dict):
                label = str(result.get("orderId") or result.get("orderLinkId") or "")
            self.order_posts.append((env, _path(record), label))
        if record.get("method") == "POST" and _path(record) == "/v5/order/create" \
                and _ret_ok(response):
            request = record.get("request")
            raw = request.get("body") if isinstance(request, dict) else None
            try:
                sent = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                sent = None
            result = response.get("result")
            if isinstance(sent, dict) and isinstance(result, dict):
                link = str(sent.get("orderLinkId") or result.get("orderLinkId") or "")
                if link:
                    self.creates.setdefault((env, link), []).append(
                        (sent, result, _captured_ms(record)))
            return
        if record.get("method") != "GET" or not _ret_ok(response):
            return
        stamp = _captured_ms(record)
        path = _path(record)
        if path == "/v5/execution/list":
            for row in _rows(response):
                eid = str(row.get("execId", ""))
                self.exec_seq[eid] = self._n
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
                if str(row.get("orderLinkId", "")):
                    self.orders_by_link.setdefault(
                        (env, str(row["orderLinkId"])), []).append(row)
        elif path == "/v5/position/list":
            rows = _rows(response)
            sizes = [_num(r.get("size")) for r in rows]
            symbols = {str(r.get("symbol", "")) for r in rows}
            # an empty list says nothing; an unreadable size is not zero
            if rows and None not in sizes and len(symbols) == 1:
                self.positions.append((env, symbols.pop(),
                                       sum(abs(x) for x in sizes), stamp, self._n))
        elif path == "/v5/account/transaction-log":
            request = record.get("request")
            params = request.get("params") if isinstance(request, dict) else None
            params = params if isinstance(params, dict) else {}
            result = response.get("result")
            self.txlog_reads.append((
                env, stamp, _int(params.get("startTime")), _int(params.get("endTime")),
                str(result.get("nextPageCursor") or "") if isinstance(result, dict) else "?"))
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
                      "CAPTURED_BEFORE", "NO_CREATE", "FEE_NOT_CONFIRMED",
                      "UNCLAIMED_", "CYCLE_", "FUNDING_OUTSIDE", "LEG_UNRESOLVED",
                      "NO_FLAT")


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
                elif not _blank(claim.get("venue_id")) and not _blank(row.get("id")) \
                        and _s(claim["venue_id"]) != _s(row["id"]):
                    issues.append("VENUE_DISAGREES:venue_id")
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
# carry drill: perp legs and the cycle they make
# ---------------------------------------------------------------------------

#: The drill books the order's cumulative price; the venue's execution rows are
#: priced per fill. One part in a million is far inside a basis point.
CARRY_PRICE_REL_TOL = 1e-6
#: Per-fill fees are rounded to 8 decimals; the order's total is rounded once.
CARRY_FEE_ABS_TOL = 1e-7
#: A linear perp is settled and charged in this coin, whatever a claim says.
CARRY_SETTLE_COIN = "USDT"
CARRY_KINDS = {"perp_open": ("Sell", False), "perp_close": ("Buy", True)}


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _order_snapshots(indexes: Sequence[VenueIndex], env: str, order_id: str,
                     link: str) -> List[Dict[str, Any]]:
    rows = [r for idx in indexes for e, r in idx.orders.get(order_id, []) if e == env]
    return rows + [r for idx in indexes
                   for r in idx.orders_by_link.get((env, link), [])
                   if str(r.get("orderId", "")) in ("", order_id)
                   and r not in rows]


def _carry_structural(claim: Mapping[str, Any], now_ms: int) -> List[str]:
    issues: List[str] = []
    for name in ("claim_id", "kind", "symbol", "side", "qty", "price", "fee",
                 "order_link_id"):
        if _blank(claim.get(name)):
            issues.append(f"MISSING_FIELD:{name}")
    if _s(claim.get("source", "")) not in SOURCES:
        issues.append("BAD_SOURCE")
    if _s(claim.get("environment", "")) not in ve.ENVIRONMENTS:
        issues.append("BAD_ENVIRONMENT")
    kind = claim.get("kind")
    want = CARRY_KINDS.get(kind) if isinstance(kind, str) else None
    if want is None and "MISSING_FIELD:kind" not in issues:
        issues.append("BAD_KIND")
    if want is not None:
        if claim.get("side") != want[0]:
            issues.append("BAD_SIDE")
        if _truthy(claim.get("reduce_only")) != want[1]:
            issues.append("BAD_KIND")
    elif not _is_str_in(claim.get("side"), SIDES) and "MISSING_FIELD:side" not in issues:
        issues.append("BAD_SIDE")
    for name, bad, positive in (("qty", "BAD_QTY", True), ("price", "BAD_PRICE", True),
                                ("fee", "BAD_FEE", False)):
        v = _num(claim.get(name))
        if claim.get(name) is not None and (v is None or (positive and v <= 0)):
            issues.append(bad)
    if _num(claim.get("fee")) is not None and _num(claim.get("fee")) < 0:
        issues.append("BAD_FEE")
    status = claim.get("fee_status")
    if status not in (None, "") and status != "confirmed":
        issues.append("FEE_NOT_CONFIRMED")
    cur = claim.get("fee_currency")
    if isinstance(cur, str) and cur and cur.upper() != CARRY_SETTLE_COIN:
        issues.append("VENUE_DISAGREES:fee_currency")
    source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
    if source == "simulation" and env in ve.NETWORKED_ENVIRONMENTS:
        issues.append("SIMULATION_CLAIMS_A_VENUE")
    if source == "testnet_claim" and env not in SANDBOXES:
        issues.append("TESTNET_CLAIM_ENVIRONMENT_MISMATCH")
    if env == "mainnet":
        issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
    return issues


def _carry_one(claim: Mapping[str, Any], indexes: Sequence[VenueIndex],
               now_ms: int, issues: List[str], detail: Dict[str, Any]) -> bool:
    """Match one perp leg to the captured rows; append every disagreement."""
    env, link = _s(claim["environment"]), _s(claim["order_link_id"]).strip()
    symbol, side = _s(claim["symbol"]), _s(claim["side"])
    qty, price, fee = _num(claim["qty"]), _num(claim["price"]), _num(claim["fee"])

    # 1. the request the bot sent, and the venue's answer to it
    sent = [c for idx in indexes for c in idx.creates.get((env, link), [])]
    if not sent:
        issues.append("NO_CREATE_REQUEST")
        return False
    if len({_s(r.get("orderId")) for _b, r, _t in sent}) > 1:
        issues.append("VENUE_ROWS_CONTRADICT")
        return False
    order_id = _s(sent[0][1].get("orderId")).strip()
    if not order_id:
        issues.append("VENUE_DISAGREES:create_order_id")
        return False
    detail["order_id"] = order_id
    if len({json.dumps(b, sort_keys=True) for b, _a, _t in sent}) > 1:
        issues.append("VENUE_ROWS_CONTRADICT")      # a replay that differs
        return False
    # EVERY captured create for this link must agree (a replay with other
    # parameters is not "the" create), and the answer must not name another link
    for body, answer, _t in sent:
        asked = _num(body.get("qty"))
        for field, ok in (
                ("category", str(body.get("category", "")) == "linear"),
                ("symbol", str(body.get("symbol", "")) == symbol),
                ("side", str(body.get("side", "")) == side),
                ("order_type", str(body.get("orderType", "")) == "Market"),
                ("qty", asked is not None and qty is not None
                 and asked * (1 + QTY_REL_TOL) + 1e-12 >= qty),
                ("reduce_only", _truthy(body.get("reduceOnly"))
                 == _truthy(claim.get("reduce_only"))),
                ("link", _s(answer.get("orderLinkId", "")) in ("", link))):
            if not ok:
                issues.append(f"VENUE_DISAGREES:create_{field}")
    create_ms = min((t for _b, _a, t in sent if t is not None), default=None)
    if create_ms is None:
        issues.append("CAPTURE_TIME_INVALID")
        return False

    # 2. the executions, from the venue's own list
    rows: Dict[str, Dict[str, Any]] = {}
    conflict = False
    for idx in indexes:
        for bucket in (idx.fills_by_order.get((env, order_id), {}),
                       idx.fills_by_link.get((env, link), {})):
            for eid, row in bucket.items():
                conflict = conflict or (eid in rows and rows[eid] != row)
                rows[eid] = row
        conflict = conflict or any(k[0] == env and k[1] in rows
                                   for k in idx.exec_conflicts)
    if conflict:
        issues.append("VENUE_ROWS_CONTRADICT")
        return False
    if not rows:
        issues.append("NOT_IN_CAPTURE")          # no execution/list row at all
        return False
    if any(str(r.get("orderId", "")) != order_id
           or str(r.get("symbol", "")) != symbol or str(r.get("side", "")) != side
           or str(r.get("execType", "")) != "Trade"
           or (str(r.get("orderLinkId", "")) not in ("", link))
           for r in rows.values()):
        issues.append("VENUE_DISAGREES:leg_identity")
        return False
    vq, vp, fees = _vwap(rows)
    times = [_int(r.get("execTime")) for r in rows.values()]
    if math.isnan(vq) or not fees:
        issues.append("VENUE_DISAGREES:leg_rows_unusable")
        return False
    if None in times or not all(EARLIEST_PLAUSIBLE_MS <= t <= now_ms + MAX_FUTURE_SKEW_MS
                                for t in times):
        issues.append("VENUE_DISAGREES:exec_time_unusable")
        return False
    detail.update(venue_qty=vq, venue_price=vp, venue_fees=fees,
                  first_exec_ms=min(times), last_exec_ms=max(times),
                  exec_ids=sorted(rows))
    if min(times) + 1000 < create_ms:
        # captures are stamped to the second; a fill can not precede the
        # request that created its order
        issues.append("VENUE_DISAGREES:exec_before_create")
    if not _close(vq, qty, QTY_REL_TOL, 1e-12):
        issues.append("VENUE_DISAGREES:qty")
    if not _close(vp, price, CARRY_PRICE_REL_TOL):
        issues.append("VENUE_DISAGREES:price")
    if set(fees) != {CARRY_SETTLE_COIN} or not _close(
            fees[CARRY_SETTLE_COIN], fee, 0.0, CARRY_FEE_ABS_TOL):
        issues.append("VENUE_DISAGREES:fee")
    for eid in rows:
        stamps = [st for idx in indexes for e, _r, st in idx.executions.get(eid, [])
                  if e == env]
        if not stamps or None in stamps:
            issues.append("CAPTURE_TIME_INVALID")
        elif min(stamps) + 1000 < _int(rows[eid].get("execTime")):
            issues.append("CAPTURED_BEFORE_EXECUTION")

    # 3. the order's own final record must agree with the same numbers
    snaps = _order_snapshots(indexes, env, order_id, link)
    if not snaps:
        issues.append("NO_CORROBORATION")
        return False
    if any(str(r.get("symbol", "")) != symbol or str(r.get("side", "")) != side
           or str(r.get("orderLinkId", link)) != link for r in snaps):
        issues.append("VENUE_DISAGREES:order_identity")
        return False
    finals = [r for r in snaps if str(r.get("orderStatus", "")) in _FINAL_VENUE_STATES]
    if not finals:
        issues.append("NO_FINAL_VENUE_STATE")
        return False
    if len({str(r.get("orderStatus")) for r in finals}) > 1:
        issues.append("ORDER_STATUS_CONFLICT")
        return False
    for last in finals:                    # every final snapshot, not just the last
        if str(last.get("orderStatus")) != "Filled":
            issues.append("VENUE_DISAGREES:order_status")
            return False
        cum, avg = _num(last.get("cumExecQty")), _num(last.get("avgPrice"))
        if cum is None or not _close(cum, qty, QTY_REL_TOL, 1e-12):
            issues.append("VENUE_DISAGREES:order_qty")
        if avg is None or not _close(avg, price, CARRY_PRICE_REL_TOL):
            issues.append("VENUE_DISAGREES:order_price")
        order_fee = _num(last.get("cumExecFee"))
        if order_fee is None or not _close(order_fee, fee, 0.0, CARRY_FEE_ABS_TOL):
            issues.append("VENUE_DISAGREES:order_fee")
        if "reduceOnly" in last and _truthy(last.get("reduceOnly")) != _truthy(
                claim.get("reduce_only")):
            issues.append("VENUE_DISAGREES:order_reduce_only")
    return True


def reconcile_carry_legs(claims: Sequence[Mapping[str, Any]],
                         captures: Sequence[Capture], *, now_ms: int
                         ) -> List[Dict[str, Any]]:
    """Reconcile the perp legs a carry drill booked against captured venue rows.

    A leg is VENUE_RECONCILED only when the order-create request the bot sent,
    the order's final record AND its execution rows all say the same quantity,
    price and fee, in the settle coin, with the right side, reduce-only flag and
    order identity. Execution rows are required: the order record alone is the
    row the bot's own fill was read from, so it is not independent of the claim.
    """
    claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
              for c in (claims or [])]
    indexes = [VenueIndex(c) for c in captures]
    broken = any(not c.intact for c in captures)
    links: Dict[str, int] = {}
    for c in claims:
        link = _s(c.get("order_link_id", "") or "").strip()
        if link:
            links[link] = links.get(link, 0) + 1
    out: List[Dict[str, Any]] = []
    for i, claim in enumerate(claims):
        issues = _carry_structural(claim, now_ms)
        if "_malformed" in claim or _s(claim.get("claim_id")) == "<unreadable>":
            issues.append("MALFORMED_CLAIM")
        source, env = _s(claim.get("source", "")), _s(claim.get("environment", ""))
        if source == "simulation" or env == "simulation":
            provenance = SIMULATED
        elif source == "testnet_claim" and env in SANDBOXES:
            provenance = CLAIMED_TESTNET_FILL
            if _blank(claim.get("order_link_id")):
                issues.append("NO_VENUE_IDENTIFIERS")
                provenance = LOCAL_RECORD
        else:
            provenance = LOCAL_RECORD
        link = _s(claim.get("order_link_id", "") or "").strip()
        if link and links.get(link, 0) > 1:
            issues.append("DUPLICATE_ORDER_LINK_ID")
        if broken:
            issues.append("CAPTURE_CHAIN_BROKEN")
        detail: Dict[str, Any] = {}
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(issues):
            if _carry_one(claim, indexes, now_ms, issues, detail) \
                    and not issues_block(issues):
                provenance = VENUE_RECONCILED
        out.append({"claim_id": _s(claim.get("claim_id", f"#{i}"))[:80],
                    "kind": "carry_leg", "provenance": provenance,
                    "resolved": provenance == VENUE_RECONCILED
                    and not issues_block(issues),
                    "counted": True, "issues": sorted(set(issues)),
                    "detail": detail})
    return out


def reconcile_carry_cycle(claims: Sequence[Mapping[str, Any]],
                          legs: Sequence[Mapping[str, Any]],
                          funding_claims: Sequence[Mapping[str, Any]],
                          funding: Sequence[Mapping[str, Any]],
                          captures: Sequence[Capture], *,
                          funding_window: Any = None) -> Dict[str, Any]:
    """Is the whole cycle accounted for, and what did it cost?

    Beyond each leg: nothing the capture shows the venue doing is unclaimed
    (every order POST in any environment, every order row and every execution
    row of the drill's window), entry and exit quantities are equal and in that
    order, a position read taken AFTER the close's execution list (chain order,
    not just clock) shows the symbol flat, a transaction-log read covering the
    hold exists (so "no settlement" is a finding, not an absence of looking),
    and every SETTLEMENT row paid inside the hold is sign-checked and claimed.
    Economics are summed from the VENUE's rows of resolved legs only, and only
    when nothing is flagged.
    """
    claims = [c if isinstance(c, Mapping) else {} for c in claims]
    legs = [v if isinstance(v, Mapping) and "resolved" in v else
            {"resolved": False, "detail": {}, "issues": ["MALFORMED_VERDICT"]}
            for v in legs]
    funding_claims = [c if isinstance(c, Mapping) else {} for c in funding_claims]
    funding = [v if isinstance(v, Mapping) and "resolved" in v else
               {"resolved": False, "issues": ["MALFORMED_VERDICT"]}
               for v in funding]
    indexes = [VenueIndex(c) for c in captures]
    issues: List[str] = []
    by_env: Dict[str, List[Tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for c, v in zip(claims, legs):
        by_env.setdefault(_s(c.get("environment")), []).append((c, v))
    envs = [e for e in by_env if e in SANDBOXES]
    if len(envs) != 1 or len(by_env) != 1:
        issues.append("CYCLE_ENVIRONMENT_NOT_ONE_SANDBOX")
        return _cycle_out(issues, legs, {})
    env = envs[0]
    pairs = by_env[env]
    symbols = {_s(c.get("symbol")) for c, _v in pairs}
    if len(symbols) != 1:
        issues.append("CYCLE_MIXED_SYMBOLS")
        return _cycle_out(issues, legs, {})
    symbol = symbols.pop()
    if any(not v["resolved"] for _c, v in pairs):
        issues.append("LEG_UNRESOLVED")

    # ---- nothing happened that the books do not claim ----------------------
    claimed_links = {_s(c.get("order_link_id")).strip() for c, _v in pairs}
    claimed_orders = {_s((v.get("detail") or {}).get("order_id")) for _c, v in pairs}
    claimed_orders.discard("")
    window_start = None
    for idx in indexes:
        for (e, link), sent in sorted(idx.creates.items()):
            if e == env and link in claimed_links:
                for _b, _a, t in sent:
                    if t is not None and (window_start is None or t < window_start):
                        window_start = t
        if idx.unverified_posts:
            issues.append("UNCLAIMED_ORDER:post_to_an_unverified_host")
        for e, path, label in idx.order_posts:
            if e != env:
                issues.append(f"UNCLAIMED_ORDER:{e}:{path}")
            elif path != "/v5/order/create":
                issues.append(f"UNCLAIMED_ORDER:{path}")
        for (e, link), sent in sorted(idx.creates.items()):
            if e == env and link not in claimed_links:
                issues.append(f"UNCLAIMED_ORDER:{link}")
        n_create = sum(1 for e, path, _l in idx.order_posts
                       if e == env and path == "/v5/order/create")
        n_linked = sum(len(v) for (e, _l), v in idx.creates.items() if e == env)
        if n_create > n_linked:
            issues.append("UNCLAIMED_ORDER:create_without_a_link_id")
    floor = (window_start - 60_000) if window_start is not None else None
    for idx in indexes:
        for oid, rows in idx.orders.items():
            for e, row in rows:
                if e != env or oid in claimed_orders:
                    continue
                created = _int(row.get("createdTime"))
                if floor is None or created is None or created >= floor:
                    issues.append(f"UNCLAIMED_ORDER:{oid or 'no_id'}")
        for eid, rows in idx.executions.items():
            for e, row, _st in rows:
                if e != env or _s(row.get("orderId")) in claimed_orders:
                    continue
                t = _int(row.get("execTime"))
                if floor is None or t is None or t >= floor:
                    issues.append(f"UNCLAIMED_EXECUTION:{eid}")
    resolved = [(c, v) for c, v in pairs if v["resolved"]]
    opens = [(c, v) for c, v in resolved if c.get("kind") == "perp_open"]
    closes = [(c, v) for c, v in resolved if c.get("kind") == "perp_close"]
    if not opens:
        issues.append("CYCLE_NO_ENTRY")
    if not closes:
        issues.append("CYCLE_NO_EXIT")
    econ: Dict[str, Any] = {}
    if opens and closes:
        oq = sum(_num(c["qty"]) for c, _v in opens)
        cq = sum(_num(c["qty"]) for c, _v in closes)
        if not _close(oq, cq, QTY_REL_TOL, 1e-12):
            issues.append("CYCLE_QTY_MISMATCH")
        first_in = min(v["detail"]["first_exec_ms"] for _c, v in opens)
        last_in = max(v["detail"]["last_exec_ms"] for _c, v in opens)
        first_out = min(v["detail"]["first_exec_ms"] for _c, v in closes)
        last_out = max(v["detail"]["last_exec_ms"] for _c, v in closes)
        if first_out < last_in:
            issues.append("CYCLE_EXIT_BEFORE_ENTRY")

        # ---- flat: a position read AFTER the close's execution list ---------
        close_ids = {e for _c, v in closes for e in v["detail"].get("exec_ids", [])}
        flat_reads: List[Tuple[int, float]] = []
        for idx in indexes:
            seen = [idx.exec_seq[e] for e in close_ids if e in idx.exec_seq]
            if not seen:
                continue
            after_seq = max(seen)
            flat_reads += [(st, size) for e, sym, size, st, seq in idx.positions
                           if e == env and sym == symbol and st is not None
                           and seq > after_seq and st + 1000 >= last_out]
        if not flat_reads:
            issues.append("NO_FINAL_POSITION_READ")
        elif max(flat_reads, key=lambda x: x[0])[1] > FLAT_POSITION_DUST:
            issues.append("CYCLE_NOT_FLAT")

        # ---- funding: looked at, then sign-checked and claimed --------------
        from carry_broker import funding_sign_check
        looked = any(
            e == env and st is not None and st + 1000 >= first_out
            and start is not None and start <= last_in
            and end is not None and end + 1000 >= first_out and not cursor
            for idx in indexes for e, st, start, end, cursor in idx.txlog_reads)
        if not looked:
            issues.append("CYCLE_FUNDING_NOT_CAPTURED")
        rows = sorted({(t, json.dumps(row, sort_keys=True)) for idx in indexes
                       for (sym, t), lst in idx.settlements.items() if sym == symbol
                       for e, row, _st in lst if e == env and last_in < t <= first_out})
        window_rows = [(t, json.loads(r)) for t, r in rows]
        fclaims = [(c, v) for c, v in zip(funding_claims, funding)
                   if _s(c.get("environment")) == env and _s(c.get("symbol")) == symbol]
        if any(not v["resolved"] for _c, v in fclaims):
            issues.append("LEG_UNRESOLVED")
        claimed_t = {_int(c.get("time_ms")) for c, _v in fclaims}
        got = 0.0
        for t, row in window_rows:
            amount = _num(row.get("funding"))
            if amount is None:
                issues.append("CYCLE_FUNDING_UNREADABLE")
            else:
                got += amount
                verdict, _why = funding_sign_check(
                    amount, row.get("change"), row.get("cashFlow"), row.get("fee"))
                if verdict != "confirmed":
                    issues.append(f"VENUE_SIGN_{verdict.upper()}")
            size = _num(row.get("size"))
            if size is None:
                issues.append("CYCLE_FUNDING_SIZE_UNVERIFIED")
            elif not _close(abs(size), oq, 1e-6, 1e-9):
                issues.append("CYCLE_FUNDING_SIZE_MISMATCH")
            if fclaims and t not in claimed_t:
                issues.append(f"UNCLAIMED_SETTLEMENT:{t}")
        for c, _v in fclaims:
            t = _int(c.get("time_ms"))
            if t is None or not (last_in < t <= first_out):
                issues.append("FUNDING_OUTSIDE_HOLD")
        n_settle = len(window_rows)
        funding_usd = None
        if fclaims:
            funding_usd = sum(_num(c.get("amount")) or 0.0 for c, _v in fclaims)
        elif funding_window is not None:
            if not isinstance(funding_window, Mapping):
                issues.append("CYCLE_FUNDING_WINDOW_MALFORMED")
            else:
                n_claim = _int(funding_window.get("count"))
                total = _num(funding_window.get("total_usd"))
                if n_claim != n_settle or total is None or not _close(
                        got, total, 1e-9, 1e-9):
                    issues.append("FUNDING_OUTSIDE_HOLD:window_total_disagrees")
                else:
                    funding_usd = got
        elif n_settle:
            issues.append("UNCLAIMED_SETTLEMENT:all")
        if funding_usd is None:
            funding_usd = 0.0

        # ---- economics: the VENUE's numbers for the resolved legs -----------
        def side_totals(group):
            q = sum(v["detail"]["venue_qty"] for _c, v in group)
            px = sum(v["detail"]["venue_qty"] * v["detail"]["venue_price"]
                     for _c, v in group) / q
            fee_ = sum(v["detail"]["venue_fees"].get(CARRY_SETTLE_COIN, 0.0)
                       for _c, v in group)
            return q, px, fee_
        vq_in, entry, fee_in = side_totals(opens)
        vq_out, exit_, fee_out = side_totals(closes)
        fees = fee_in + fee_out
        gross = (entry - exit_) * vq_out
        econ = {
            "unit": "USDT (testnet dollars, not cash)", "qty": vq_out,
            "entry_price": entry, "exit_price": exit_,
            "perp_gross_usdt": gross, "fees_usdt": fees,
            "fee_bps_of_entry_notional": fees / (entry * vq_in) * 1e4,
            "settlements": n_settle, "funding_usdt": funding_usd,
            "hold_ms": first_out - last_in,
            "net_usdt": gross - fees + funding_usd,
            "scope": "perp leg + the funding it was paid; the client's spot "
                     "inventory price change is not included",
            "inference": "NONE: one round trip cannot distinguish skill from "
                         "noise, and testnet prices and funding are not the "
                         "real ones"}
    return _cycle_out(issues, legs, econ)


#: A position read this small is flat.
FLAT_POSITION_DUST = 1e-8


def _cycle_out(issues: List[str], legs: Sequence[Any],
               econ: Dict[str, Any]) -> Dict[str, Any]:
    issues = sorted(set(issues))
    ok = bool(legs) and not issues_block(issues) and all(v["resolved"] for v in legs)
    return {"kind": "carry_cycle", "provenance": VENUE_RECONCILED if ok else
            CLAIMED_TESTNET_FILL, "resolved": ok, "counted": True,
            "issues": issues,
            "economics": econ if ok else {k: econ[k] for k in
                                          ("unit", "scope", "inference")
                                          if k in econ}}


def claims_from_drill_report(report: Any, cash_ledger: Any = None
                             ) -> Dict[str, Any]:
    """`carry_leg_claims`, `funding_claims` and (legacy) `funding_window` from a
    `tools/drill.py --out` report.

    A report written with this format carries its own `claims`. An older one
    (attempt-1/2 shape) is read as far as it states things: the entry leg from
    the `open` stage, the exit leg from a `cash_ledger/2` (`exit_price`, `qty`,
    and the exit fee as `fees_usd` minus the entry fee, only when the ledger
    says exactly two fills with confirmed fees), funding as a count and total
    over the hold. The exit order's link id is not in an old report, so the
    exit claim carries none and is matched to the capture's single reduce-only
    create (`link_inferred`): the match is by the numbers, and the verdict says
    so. Anything it cannot state it leaves out; nothing is invented.
    """
    if not isinstance(report, Mapping):
        return {"carry_leg_claims": [], "funding_claims": [],
                "derivation": "unreadable"}
    claims = report.get("claims")
    if isinstance(claims, Mapping) and claims.get("schema") == "drill_claims/1":
        legs_, funds_ = (claims.get("carry_leg_claims") or []), \
            (claims.get("funding_claims") or [])
        if not isinstance(legs_, list) or not isinstance(funds_, list):
            return {"carry_leg_claims": [], "funding_claims": [],
                    "derivation": "unreadable"}
        return {"carry_leg_claims": list(legs_), "funding_claims": list(funds_),
                "derivation": "report.claims"}
    venue = _s(report.get("venue", ""))
    source, env = (("testnet_claim", "testnet") if venue == "testnet"
                   else ("local", "unknown"))
    raw_stages = report.get("stages")
    stages = {st["stage"]: st for st in (raw_stages if isinstance(raw_stages, list)
                                         else [])
              if isinstance(st, Mapping) and isinstance(st.get("stage"), str)}
    legs: List[Dict[str, Any]] = []
    opened = stages.get("open")
    ev = opened.get("evidence") if isinstance(opened, Mapping) and opened.get("ok") else None
    entry_fee = None
    if isinstance(ev, Mapping) and ev.get("order_link_id"):
        entry_fee = _num(ev.get("fee"))
        legs.append({"claim_id": f"leg:{ev['order_link_id']}", "source": source,
                     "environment": env, "kind": "perp_open", "symbol": "BTCUSDT",
                     "side": "Sell", "qty": ev.get("filled_qty"),
                     "price": ev.get("avg_price"), "fee": ev.get("fee"),
                     "fee_currency": "", "order_link_id": ev["order_link_id"],
                     "reduce_only": False})
    window = None
    if isinstance(cash_ledger, Mapping) and cash_ledger.get("category") == "carry" \
            and cash_ledger.get("schema") == "cash_ledger/2":
        qty, exit_price = _num(cash_ledger.get("qty")), _num(cash_ledger.get("exit_price"))
        total_fee = _num(cash_ledger.get("fees_usd"))
        if (legs and cash_ledger.get("flat") is True
                and _int(cash_ledger.get("fills")) == 2
                and cash_ledger.get("fees_confirmed") is True
                and None not in (qty, exit_price, total_fee, entry_fee)
                and exit_price > 0):
            legs.append({"claim_id": "leg:exit(from cash ledger)", "source": source,
                         "environment": env, "kind": "perp_close",
                         "symbol": cash_ledger.get("symbol") or "BTCUSDT",
                         "side": "Buy", "qty": qty, "price": exit_price,
                         "fee": total_fee - entry_fee, "fee_currency": "",
                         "order_link_id": "", "reduce_only": True,
                         "link_inferred": True})
        count = _int(cash_ledger.get("funding_settlements"))
        total = _num(cash_ledger.get("funding_venue_usd"))
        if count is not None and total is not None:
            window = {"count": count, "total_usd": total}
    return {"carry_leg_claims": legs, "funding_claims": [],
            "funding_window": window, "derivation": "legacy_report"}


def resolve_inferred_links(claims: Sequence[Dict[str, Any]],
                           captures: Sequence[Capture]) -> List[Dict[str, Any]]:
    """Fill the order link id of a `link_inferred` claim from the capture, but
    only when exactly ONE successful reduce-only create of that symbol and side
    exists in its environment. Zero or several: the link stays blank and the
    leg reports NO_VENUE_IDENTIFIERS. Returns new dicts; inputs are untouched."""
    indexes = [VenueIndex(c) for c in captures]
    out = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            out.append(claim)
            continue
        claim = dict(claim)
        if claim.get("link_inferred") and _blank(claim.get("order_link_id")):
            env = _s(claim.get("environment"))
            found = {link for idx in indexes
                     for (e, link), sent in idx.creates.items() if e == env
                     for b, _r, _t in sent
                     if _s(b.get("symbol")) == _s(claim.get("symbol"))
                     and _s(b.get("side")) == _s(claim.get("side"))
                     and _truthy(b.get("reduceOnly")) == _truthy(claim.get("reduce_only"))}
            if len(found) == 1:
                claim["order_link_id"] = found.pop()
                claim["claim_id"] = f"leg:{claim['order_link_id']}"
            else:
                claim["_link_ambiguous"] = len(found)
        out.append(claim)
    return out


# ---------------------------------------------------------------------------
# local ledger summaries: counts and totals carry no identifiers
# ---------------------------------------------------------------------------

def summarise_ledger(payload: Mapping[str, Any], fills: Sequence[Dict[str, Any]],
                     fill_claims: Sequence[Mapping[str, Any]],
                     carry_claims: Sequence[Mapping[str, Any]] = (),
                     carry_legs: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
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
    counted = [v for v in fills if v["counted"]] + list(carry_legs)
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
                      for c, v in zip(fill_claims, fills) if v["counted"]) + \
        sum(_num(c.get("fee")) or 0.0 for c in carry_claims if isinstance(c, dict))
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
              carry_claims: Sequence[Mapping[str, Any]] = (),
              funding_window: Optional[Mapping[str, Any]] = None,
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
    funding_claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
                      for c in (funding_claims or [])]
    carry_claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
                    for c in (carry_claims or [])]
    carry_claims = resolve_inferred_links(carry_claims, captures)
    carry_legs = reconcile_carry_legs(carry_claims, captures, now_ms=now_ms) \
        if carry_claims else []
    for claim, leg in zip(carry_claims, carry_legs):
        if claim.get("link_inferred"):
            if claim.get("_link_ambiguous") is not None:
                leg["issues"] = sorted(set(leg["issues"]) | {"NO_VENUE_IDENTIFIERS"})
            else:
                # reconciled by its numbers; the link was found, not stated
                leg["issues"] = sorted(set(leg["issues"]) | {"LEG_LINK_INFERRED"})
    carry_cycle = [reconcile_carry_cycle(
        carry_claims, carry_legs, list(funding_claims or []), funding,
        captures, funding_window=funding_window)] if carry_claims else []
    summaries = [summarise_ledger(l, fills, fill_claims, carry_claims, carry_legs)
                 for l in ledgers]
    everything = fills + funding + trades + order_claim_verdicts + summaries \
        + carry_legs + carry_cycle
    counts = {c: sum(1 for v in everything if v["provenance"] == c)
              for c in CLASSES}
    issue_counts: Dict[str, int] = {}
    for v in everything:
        for i in v["issues"]:
            issue_counts[i] = issue_counts.get(i, 0) + 1
    checked = fills + funding + trades + order_claim_verdicts + carry_legs \
        + carry_cycle
    allowed = bool(everything) and all(v["resolved"] for v in checked) \
        and bool(fills or trades or carry_legs) \
        and not summaries_block(summaries) \
        and all(o["complete"] for o in orders) \
        and all(not v["issues"] for v in checked) \
        and all(c.intact for c in captures)
    return {
        "schema": "evidence_reconciliation/1",
        "fills": fills, "funding": funding, "orders": orders,
        "trades": trades, "order_claims": order_claim_verdicts,
        "carry_legs": carry_legs, "carry_cycle": carry_cycle,
        "ledger_summaries": summaries,
        "captures": [{"label": c.label, "records": len(c.records),
                      "intact": c.intact, "chain_issues": c.chain_issues[:20]}
                     for c in captures],
        "counts_by_provenance": counts, "issue_counts": issue_counts,
        "pnl_claim_allowed": allowed,
        "verified_economics": verified_economics(
            [c if isinstance(c, dict) else {} for c in (trade_claims or [])],
            trades),
        "pnl_note": ("False unless every fill and funding claim is "
                     "VENUE_RECONCILED with no issue, every order is complete, "
                     "and every capture chain verifies. Even then this reports "
                     "consistency of captured records, not profitability, and "
                     "testnet dollars are not cash."),
    }


#: Below this many reconciled trades no statement about edge is made at all.
MIN_TRADES_FOR_ANY_INFERENCE = 30


def verified_economics(claims: Sequence[Any],
                       verdicts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the VENUE-RECONCILED trades earned, and nothing else.

    A trade contributes only if its verdict is resolved (its legs matched
    captured venue executions and its stated gross follows from its prices);
    everything unresolved is counted and left out, never estimated. Fees are
    summed only when both legs state USDT; a trade fee-denominated in another
    coin is excluded and counted, not converted at a guessed rate. Funding is
    not included. These are TESTNET dollars. Whatever the totals say, no
    inference about edge is made below MIN_TRADES_FOR_ANY_INFERENCE trades, and
    above it the figures still carry no uncertainty analysis, no out-of-sample
    test and no funding, so they are an input to that analysis, not its result.
    """
    nets: List[float] = []
    gross_total = fees_total = 0.0
    excluded = {"unresolved": 0, "fee_not_usdt": 0}
    for claim, verdict in zip(claims, verdicts):
        if not verdict.get("resolved"):
            excluded["unresolved"] += 1
            continue
        gross = _num(claim.get("gross_pnl")) if isinstance(claim, dict) else None
        e_fee = _num(claim.get("entry_fee")) if isinstance(claim, dict) else None
        x_fee = _num(claim.get("exit_fee")) if isinstance(claim, dict) else None
        currencies = ({claim.get("entry_fee_currency"), claim.get("exit_fee_currency")}
                      if isinstance(claim, dict) else set())
        if None in (gross, e_fee, x_fee) or currencies != {"USDT"}:
            excluded["fee_not_usdt"] += 1
            continue
        net = gross - e_fee - x_fee
        nets.append(net)
        gross_total += gross
        fees_total += e_fee + x_fee
    n = len(nets)
    enough = n >= MIN_TRADES_FOR_ANY_INFERENCE
    return {
        "unit": "USDT (testnet dollars, not cash)",
        "reconciled_trades": n,
        "excluded": excluded,
        "gross_usdt": gross_total, "fees_usdt": fees_total,
        "net_usdt": sum(nets),
        "mean_net_usdt": (sum(nets) / n) if n else None,
        "wins": sum(1 for x in nets if x > 0), "losses": sum(1 for x in nets if x < 0),
        "funding_included": False,
        "inference": (
            f"NONE: {n} reconciled trade(s); fewer than "
            f"{MIN_TRADES_FOR_ANY_INFERENCE} cannot distinguish skill from noise"
            if not enough else
            f"{n} reconciled trades: still NOT a profitability claim - no "
            "uncertainty interval, out-of-sample test or funding is applied here"),
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
    ap.add_argument("--drill-report", action="append", default=[],
                    help="a tools/drill.py --out report (repeatable): its perp "
                         "legs and funding become claims (an older report is "
                         "read as far as it states things; pair it with "
                         "--ledger <cash_ledger/2>)")
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
    carry_claims = list(claims.get("carry_leg_claims", []))
    funding_claims = list(claims.get("funding_claims", []))
    funding_window = claims.get("funding_window")
    for path in args.drill_report:
        drill_report = _read_json_input(path, "--drill-report")
        if drill_report is None:
            return 3
        cash = next((l for l in ledgers if l.get("category") == "carry"), None)
        derived_carry = claims_from_drill_report(drill_report, cash)
        if derived_carry["derivation"] == "unreadable":
            print("refusing: --drill-report "
                  f"{os.path.basename(path)!r} is not a readable drill report",
                  file=sys.stderr)
            return 3
        carry_claims += derived_carry["carry_leg_claims"]
        funding_claims += derived_carry["funding_claims"]
        funding_window = derived_carry.get("funding_window") or funding_window
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
                       funding_claims=funding_claims,
                       carry_claims=carry_claims, funding_window=funding_window,
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
