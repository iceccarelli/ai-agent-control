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

def _num(value: Any) -> Optional[float]:
    """A finite float, or None. Booleans and blanks are not numbers."""
    if value is None or isinstance(value, bool) or str(value).strip() == "":
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
                self.executions.setdefault(str(row.get("execId", "")), []) \
                    .append((env, row, stamp))
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
        if claim.get(name) is None or str(claim.get(name)).strip() == "":
            issues.append(f"MISSING_FIELD:{name}")
    if str(claim.get("source", "")) not in SOURCES:
        issues.append("BAD_SOURCE")
    if str(claim.get("environment", "")) not in ve.ENVIRONMENTS:
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
    source, env = str(claim.get("source", "")), str(claim.get("environment", ""))
    if source == "simulation" and env in ve.NETWORKED_ENVIRONMENTS:
        issues.append("SIMULATION_CLAIMS_A_VENUE")
    if source == "testnet_claim" and env not in SANDBOXES:
        issues.append("TESTNET_CLAIM_ENVIRONMENT_MISMATCH")
    if env == "mainnet":
        issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
    return issues


def _base_class(claim: Mapping[str, Any], issues: List[str]) -> str:
    source, env = str(claim.get("source", "")), str(claim.get("environment", ""))
    if source == "simulation" or env == "simulation":
        return SIMULATED
    if source == "testnet_claim" and env in SANDBOXES:
        if not (str(claim.get("exec_id", "")).strip()
                and str(claim.get("order_id", "")).strip()):
            issues.append("NO_VENUE_IDENTIFIERS")
            return LOCAL_RECORD
        return CLAIMED_TESTNET_FILL
    return LOCAL_RECORD


def _match_fill(claim: Mapping[str, Any], row: Mapping[str, Any]) -> List[str]:
    diffs: List[str] = []
    if str(row.get("orderId", "")) != str(claim.get("order_id", "")):
        diffs.append("order_id")
    link = str(claim.get("order_link_id", "") or "")
    if link and str(row.get("orderLinkId", "")) != link:
        diffs.append("order_link_id")
    if str(row.get("symbol", "")) != str(claim.get("symbol", "")):
        diffs.append("symbol")
    if str(row.get("side", "")) != str(claim.get("side", "")):
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
    currency = str(claim.get("fee_currency", "") or "")
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
        eid = str(c.get("exec_id", "") or "").strip()
        if eid:
            by_exec.setdefault(eid, []).append(i)
        link = str(c.get("order_link_id", "") or "").strip()
        if link and str(c.get("order_id", "") or "").strip():
            link_orders.setdefault(link, set()).add(str(c["order_id"]))

    for i, claim in enumerate(claims):
        issues = _structural_issues(claim, now_ms)
        if "_malformed" in claim:
            issues.append("MALFORMED_CLAIM")
        provenance = _base_class(claim, issues)
        eid = str(claim.get("exec_id", "") or "").strip()
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
        link = str(claim.get("order_link_id", "") or "").strip()
        if link and len(link_orders.get(link, ())) > 1:
            issues.append("AMBIGUOUS_ORDER_LINK_ID")
        if chain_broken:
            issues.append("CAPTURE_CHAIN_BROKEN")

        reconciled = False
        if provenance == CLAIMED_TESTNET_FILL and not issues_block(issues):
            reconciled = _reconcile_one(claim, indexes, issues)
        if reconciled and not issues_block(issues):
            provenance = VENUE_RECONCILED
        out.append({"claim_id": str(claim.get("claim_id", f"#{i}")),
                    "kind": "fill", "provenance": provenance,
                    "resolved": provenance == VENUE_RECONCILED
                    and not issues_block(issues),
                    "counted": not duplicate_of_earlier,
                    "issues": sorted(set(issues))})
    return out


#: Issues that forbid the upgrade, however well the venue rows match.
_BLOCKING_PREFIXES = ("MALFORMED", "CAPTURE_TIME", "MISSING_FIELD", "BAD_", "SIMULATION_", "TESTNET_CLAIM_",
                      "MAINNET_", "NO_VENUE", "DUPLICATE", "CONFLICTING",
                      "AMBIGUOUS", "CAPTURE_CHAIN", "VENUE_", "ORDER_",
                      "NO_CORROBORATION", "NOT_IN_CAPTURE", "ENV_MISMATCH",
                      "CAPTURED_BEFORE")


def issues_block(issues: Iterable[str]) -> bool:
    return any(i.startswith(_BLOCKING_PREFIXES) for i in issues)


def _reconcile_one(claim: Mapping[str, Any], indexes: Sequence[Tuple[Capture, VenueIndex]],
                   issues: List[str]) -> bool:
    eid = str(claim["exec_id"]).strip()
    every = [(env, row, stamp) for _c, idx in indexes
             for env, row, stamp in idx.executions.get(eid, [])]
    if not every:
        issues.append("NOT_IN_CAPTURE")
        return False
    claimed_env = str(claim.get("environment"))
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
                  for e, r in idx.orders.get(str(claim["order_id"]), [])
                  if e == env]
    corroborated = False
    for order in order_rows:
        cum = _num(order.get("cumExecQty"))
        qty = _num(claim.get("qty"))
        if (str(order.get("symbol", "")) == str(claim["symbol"])
                and str(order.get("side", "")) == str(claim["side"])
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
            key = (str(claim["environment"]), str(claim["order_id"]))
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
        for name in ("claim_id", "symbol", "time_ms", "amount"):
            if claim.get(name) is None or str(claim.get(name)).strip() == "":
                issues.append(f"MISSING_FIELD:{name}")
        amount, stamp = _num(claim.get("amount")), _int(claim.get("time_ms"))
        if claim.get("amount") is not None and amount is None:
            issues.append("BAD_AMOUNT")
        if claim.get("time_ms") is not None and (
                stamp is None or stamp < EARLIEST_PLAUSIBLE_MS
                or stamp > now_ms + MAX_FUTURE_SKEW_MS):
            issues.append("BAD_TIMESTAMP")
        source, env = str(claim.get("source", "")), str(claim.get("environment", ""))
        if source not in SOURCES:
            issues.append("BAD_SOURCE")
        if env not in ve.ENVIRONMENTS:
            issues.append("BAD_ENVIRONMENT")
        if env == "mainnet":
            issues.append("MAINNET_EVIDENCE_NOT_ACCEPTED")
        key = (str(claim.get("symbol")), stamp)
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
        out.append({"claim_id": str(claim.get("claim_id", f"#{i}")),
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
              ledgers: Sequence[Mapping[str, Any]] = (),
              captures: Sequence[Capture] = (), now_ms: int) -> Dict[str, Any]:
    fill_claims = [c if isinstance(c, dict) else {"_malformed": repr(c)[:80]}
                   for c in (fill_claims or [])]
    fills = reconcile_fills(fill_claims, captures, now_ms=now_ms)
    funding = reconcile_funding(funding_claims, captures, now_ms=now_ms) \
        if funding_claims else []
    orders = reconcile_order_totals(fill_claims, fills, captures)
    summaries = [summarise_ledger(l, fills, fill_claims) for l in ledgers]
    everything = fills + funding + summaries
    counts = {c: sum(1 for v in everything if v["provenance"] == c)
              for c in CLASSES}
    issue_counts: Dict[str, int] = {}
    for v in everything:
        for i in v["issues"]:
            issue_counts[i] = issue_counts.get(i, 0) + 1
    allowed = bool(everything) and all(v["resolved"] for v in fills + funding) \
        and bool(fills) and not summaries_block(summaries) \
        and all(o["complete"] for o in orders) \
        and all(not v["issues"] for v in fills + funding) \
        and all(c.intact for c in captures)
    return {
        "schema": "evidence_reconciliation/1",
        "fills": fills, "funding": funding, "orders": orders,
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
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claims", help="JSON file: {fill_claims:[...], "
                                     "funding_claims:[...]}")
    ap.add_argument("--evidence", action="append", default=[],
                    help="venue_evidence JSONL chain (repeatable, read-only)")
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
        with open(args.claims, encoding="utf-8") as handle:
            claims = json.load(handle)
    ledgers = []
    for path in args.ledger:
        with open(path, encoding="utf-8") as handle:
            ledgers.append(json.load(handle))
    captures = [load_capture(p) for p in args.evidence]
    report = reconcile(fill_claims=claims.get("fill_claims", []),
                       funding_claims=claims.get("funding_claims", []),
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
