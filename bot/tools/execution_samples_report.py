#!/usr/bin/env python3
"""Read-only: how many real execution samples this book holds, and how many more
the entry net-benefit gate needs before it can price a round trip.

    python3 tools/execution_samples_report.py --environment testnet|mainnet \\
        [--db PATH] [--records PATH] --mode overlay|acquire --notional-usd N

`--records` reads the JSONL a drill appends (`tools/drill.py
--execution-records`); only lines labelled with `--environment` count, so
testnet fills are never mixed into a mainnet tally. `--db` needs the same
`--environment` and is REFUSED (exit 2) unless the database itself recorded
exactly that environment (`carry_performance.load_environment_record`); the
report states both under `environment`. `--db` and `--records` may be
combined; the same fill is counted once. Orders that did not fill are counted
under `outcomes` (with a per-leg miss rate) and never as samples.

Opens the database read-only, sends nothing, contacts no venue. For each leg of
a round trip it prints the orders on record, those usable for the planned size,
the newest sample's age, and the shortfall against the policy. Exit 0 when every
leg is measurable, 3 when any is not. The numbers are this book's own fills (a
testnet book's are testnet fills, which are not evidence of any market).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import execution_cost_estimate as est  # noqa: E402


class _Fill:
    """The four fields the estimator reads, from one JSONL record."""
    __slots__ = ("kind", "ms", "ref", "fee_raw")

    def __init__(self, kind, ms, ref, fee_raw):
        self.kind, self.ms, self.ref, self.fee_raw = kind, ms, ref, fee_raw


#: The kind of a JSONL line that records an order which did NOT fill
#: (`tools/drill.py` OUTCOME_KIND). Repeated, not imported: this reader stays
#: standalone, and a test pins the two together.
OUTCOME_KIND = "execution_outcome"
#: An order the venue accepted whose local booking failed (`tools/drill.py`
#: UNBOOKED_KIND). Not a fill, not a miss: reported as uncertainty.
UNBOOKED_KIND = "execution_unbooked"
SCHEMA = "execution_records/1"


def _env_agrees(record, environment) -> bool:
    """A record that states its own environment must state THIS one. The line's
    label is what the drill wrote; a record that contradicts it is not trusted
    for either."""
    stated = record.get("environment") if isinstance(record, dict) else None
    return stated in (None, "", environment)


def load_records_ex(paths, environment):
    """(fills, outcomes, skipped) from drill JSONL files.

    `fills` are the lines of a filled order (a ledger fill kind with its
    execution record under `fee_raw`); they are the only lines the estimator
    ever sees. `outcomes` are the records of orders that did not fill
    (kind `execution_outcome`); they are returned apart and are only ever
    counted. A line that is unreadable, of another schema or labelled with
    another environment is not a sample; it is counted as skipped, never
    guessed at. The same outcome repeated is kept once."""
    fills, outcomes, skipped = [], [], 0
    seen_outcomes = set()
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if not (d.get("schema") == SCHEMA
                            and d.get("environment") == environment):
                        skipped += 1
                        continue
                    if d.get("kind") in (OUTCOME_KIND, UNBOOKED_KIND):
                        rec = d.get("execution")
                        if not isinstance(rec, dict) \
                                or not _env_agrees(rec, environment):
                            skipped += 1
                            continue
                        key = json.dumps(rec, sort_keys=True, default=str)
                        if key not in seen_outcomes:
                            seen_outcomes.add(key)
                            outcomes.append(rec)
                        continue
                    fee_raw = d.get("fee_raw")
                    if not isinstance(fee_raw, dict) or not _env_agrees(
                            fee_raw.get("execution"), environment):
                        skipped += 1
                        continue
                    fills.append(_Fill(d["kind"], int(d["ms"]), str(d["ref"]),
                                       fee_raw))
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    skipped += 1
    return fills, outcomes, skipped


def load_records(paths, environment):
    """(fills, skipped): `load_records_ex` without the outcomes."""
    fills, _outcomes, skipped = load_records_ex(paths, environment)
    return fills, skipped


def summarize_outcomes(outcomes, filled_by_leg=None) -> dict:
    """What the attempts that did not fill look like, per leg. Nothing here is a
    cost sample: `counted_as_samples` is 0 by construction and the estimator is
    never given these records."""
    by_leg, reasons = {}, {}
    outcomes = [r for r in outcomes if r.get("status") != "UNBOOKED"]
    for rec in outcomes:
        leg = "/".join(str(rec.get(k) or "?")
                       for k in ("product", "side", "order_type"))
        status = str(rec.get("status") or "?")
        by_leg.setdefault(leg, {})
        by_leg[leg][status] = by_leg[leg].get(status, 0) + 1
        first = (rec.get("reasons") or ["?"])[0]
        why = str(first).split(":", 1)[0]
        reasons[why] = reasons.get(why, 0) + 1
    # Miss rate per leg: orders that filled nothing / all orders attempted
    # (those plus the filled orders on record). None when nothing was attempted.
    miss = {}
    for leg, counts in by_leg.items():
        missed = counts.get("NO_FILL", 0)
        filled = int((filled_by_leg or {}).get(leg, 0))
        miss[leg] = {"unfilled_orders": missed, "filled_orders": filled,
                     "miss_rate": (missed / (missed + filled)
                                   if missed + filled else None)}
    return {"total": len(outcomes), "by_leg": by_leg, "reasons": reasons,
            "miss_rate": miss, "counted_as_samples": 0,
            "note": "orders that did not fill; execution-outcome evidence "
                    "only, never a filled-order cost sample"}


def partial_fills(entries) -> dict:
    """Per leg: filled orders that filled LESS than they asked for, and the
    quantity left unfilled. A partial fill is a cost sample for what filled,
    but its remainder is an execution outcome the cost bound cannot see; it is
    reported here so it is not mistaken for a clean fill."""
    out = {}
    for e in entries:
        rec = (getattr(e, "fee_raw", None) or {}).get("execution")
        if not isinstance(rec, dict):
            continue
        try:
            filled, asked = float(rec["filled_qty"]), float(rec["requested_qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (filled > 0 and asked > 0) or filled >= asked * (1 - 1e-6):
            continue
        leg = "/".join(str(rec.get(k) or "?")
                       for k in ("product", "side", "order_type"))
        row = out.setdefault(leg, {"orders": 0, "unfilled_qty": 0.0})
        row["orders"] += 1
        row["unfilled_qty"] += asked - filled
    return out


def summarize_unbooked(outcomes) -> dict:
    """Orders the venue ACCEPTED whose local booking failed. They are neither
    fills nor misses and never samples: the venue reported a fill the book does
    not hold, so the true execution is UNCERTAIN until the position is verified
    independently. Reported so they are not silently absent."""
    by_leg = {}
    for rec in outcomes:
        if rec.get("status") != "UNBOOKED":
            continue
        leg = "/".join(str(rec.get(k) or "?")
                       for k in ("product", "side", "order_type"))
        by_leg[leg] = by_leg.get(leg, 0) + 1
    return {"total": sum(by_leg.values()), "by_leg": by_leg,
            "counted_as_samples": 0,
            "note": "UNCERTAIN: the venue accepted these orders and reported a "
                    "fill the local book failed to record. They are not cost "
                    "samples and not misses; verify the position independently "
                    "before trusting the counts above."}


def check_db_environment(db_path: str, environment: str) -> dict:
    """The environment the database itself recorded, checked against the one
    the report is asked for. Raises ValueError (a refusal) unless the database
    recorded exactly that environment and no other: a testnet book's round
    trips are not evidence for mainnet, and a database that never said whose
    orders it booked cannot be assumed to be anyone's."""
    import carry_performance
    record = carry_performance.load_environment_record(db_path)
    seen = list(record.get("seen") or [])
    recorded = record.get("recorded")
    info = {"declared": environment, "recorded": recorded,
            "recorded_seen": seen, "equity_scope": record.get("equity_scope")}
    if not recorded and not seen:
        raise ValueError(
            "the database never recorded which environment's orders it books "
            f"(ENVIRONMENT_NOT_RECORDED), so it cannot be counted as {environment}")
    if recorded != environment or any(e != environment for e in seen):
        raise ValueError(
            f"ENVIRONMENT_MISMATCH: the report asks for {environment!r} but the "
            f"database recorded {recorded!r} (ever seen: {seen})")
    return info


def build(db_path: str, *, mode: str, notional_usd: float, perp: str, spot: str,
          now_ms: int = 0, records=(), environment: str = "") -> dict:
    entries, skipped, outcomes = [], 0, []
    env_info = {"declared": environment or None, "database": None,
                "records_label": environment if records else None}
    if db_path:
        import carry_performance
        if environment:
            env_info["database"] = check_db_environment(db_path, environment)
        entries.extend(carry_performance.load_journal(db_path).entries)
    if records:
        extra, outcomes, skipped = load_records_ex(records, environment)
        entries.extend(extra)
    now = now_ms or int(time.time() * 1000)
    out = est.estimate_round_trip(
        entries, as_of_ms=now, execution_mode=mode, perp_symbol=perp,
        spot_symbol=spot, planned_notional_usd=notional_usd)
    out["environment"] = env_info
    out["records_skipped"] = skipped
    out["partial_fills"] = partial_fills(entries)
    # Orders that FILLED on each leg, priced or not: the valid observations
    # plus the fills the estimator had to reject (mark-referenced, unverifiable,
    # malformed...). Using only the valid ones hid the true denominator and
    # overstated the miss rate. A fill with no execution record at all cannot
    # be assigned to a leg and is not counted here; the later age and size-band
    # rejections are subsets of the valid ones and are not added again.
    not_per_leg = {"NO_EXECUTION_RECORD", "TOO_OLD", "OUTSIDE_SIZE_BAND"}
    filled = {l["leg"]: l["n"] + sum(
        int(v) for k, v in (l.get("rejected") or {}).items()
        if k not in not_per_leg) for l in out["legs"]}
    out["outcomes"] = summarize_outcomes(outcomes, filled)
    out["unbooked_orders"] = summarize_unbooked(outcomes)
    need = est.DEFAULT_POLICY.min_samples
    out["shortfall"] = {l["leg"]: max(0, need - int(l["n_in_size_band"]))
                        for l in out["legs"]}
    return out


def main(argv=None, env=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="")
    ap.add_argument("--records", action="append", default=[],
                    help="drill JSONL (repeatable); needs --environment")
    ap.add_argument("--environment", default="",
                    help="count only records labelled with this environment")
    ap.add_argument("--mode", choices=("overlay", "acquire"), default=None,
                    help="default: CARRY_EXECUTION_MODE from the environment")
    ap.add_argument("--notional-usd", type=float, required=True,
                    help="the planned position size in USD")
    ap.add_argument("--perp", default=None,
                    help="default: CARRY_PERP_SYMBOL from the environment")
    ap.add_argument("--spot", default=None,
                    help="default: CARRY_SPOT_SYMBOL from the environment")
    args = ap.parse_args(argv)
    if not (args.notional_usd == args.notional_usd and args.notional_usd > 0
            and args.notional_usd != float("inf")):
        ap.error("--notional-usd must be a positive, finite number")
    env = os.environ if env is None else env
    args.mode = args.mode or str(env.get("CARRY_EXECUTION_MODE", "")).strip().lower()
    if args.mode not in ("overlay", "acquire"):
        ap.error("--mode is required (or set CARRY_EXECUTION_MODE to overlay/acquire)")
    args.perp = args.perp or str(env.get("CARRY_PERP_SYMBOL", "BTCUSDT")).strip().upper()
    args.spot = args.spot or str(env.get("CARRY_SPOT_SYMBOL", "BTCUSDT")).strip().upper()
    if not args.db and not args.records:
        ap.error("give --db and/or --records")
    if (args.records or args.db) and not args.environment:
        ap.error("--environment is required with --db or --records: testnet "
                 "fills must never be counted as mainnet, and a report that "
                 "does not say whose fills it counts is not evidence")
    for missing in ([args.db] if args.db else []) + args.records:
        if not os.path.exists(missing):
            print(json.dumps({"error": f"no such file: {missing}"}))
            return 2
    try:
        report = build(args.db, mode=args.mode, notional_usd=args.notional_usd,
                       perp=args.perp, spot=args.spot,
                       records=args.records, environment=args.environment)
    except ValueError as exc:
        print(json.dumps({"error": str(exc), "refused": True}))
        return 2
    except Exception as exc:  # noqa: BLE001 - any failure is exit 2, never a traceback
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["status"] == est.MEASURED else 3


if __name__ == "__main__":
    sys.exit(main())
