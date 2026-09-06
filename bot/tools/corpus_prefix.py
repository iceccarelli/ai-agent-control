#!/usr/bin/env python3
"""The prefix-anchored corpus invariant. EDGE.md §45c.

WHY THIS MODULE EXISTS
======================
Until slice 62 the repository asserted corpus integrity with a whole-file digest:

    sha256(the whole file today) == the value pinned when it was measured

That assertion conflated two different claims, and slice 62 is where they came
apart. A human supplied a genuine extension — one closed daily bar after `t1`
and five funding prints — and eight tests went red. Not one of them had found a
defect. Every one of them was reporting that *a file had grown*, which is the
event the entire programme is waiting for.

The tempting repair is to move the numbers: 1461 -> 1462, 4383 -> 4388. **That
is threshold-moving.** A test edited to agree with whatever is on disk asserts
nothing at all, and the next extension edits it again, and the fiftieth edits it
while a rewrite slips past unnoticed.

WHAT REPLACES IT
================
A pinned digest is the digest of the corpus **as of the pin**. So assert it
against the **prefix of the file at the pinned row count**, and constrain growth
beyond that prefix:

    1. sha256(header + first `pinned_rows` data rows) == the pinned digest
       -> the measured history is byte-for-byte the history that was measured;
    2. rows_on_disk >= pinned_rows
       -> a corpus may grow. It may never shrink;
    3. every appended row's timestamp is STRICTLY AFTER `t1`
       -> growth belongs to the future.

STRENGTH, STATED HONESTLY
=========================
Against the whole-file form this is:

* **equal** on the invariant that matters. Any edit to a measured byte still
  fails, because clause 1 still fails;
* **strictly stronger** in one place. A whole-file digest cannot distinguish an
  append from a rewrite — it only says "different" — and it never checked that
  new rows belong to the future. A back-filled row quietly appended with a
  timestamp *before* `t1` contaminates a measured window without altering one
  existing byte. Clause 3 names that case and fails it by name;
* **weaker in exactly one place, deliberately.** It no longer proves that *no
  extension exists*. That was never an invariant — it was slice 61's dated
  finding, and `artifacts/slice61_data_freshness.json` continues to record it.

WHERE THE PINS COME FROM
========================
`artifacts/slice55_data_eligibility.json` — a **dated** artefact, never
rewritten, which records for each corpus file the row count, the uncompressed
sha256 and the first and last timestamps as of the eligibility run. The fold
calendar pins the same two BTCUSDT digests independently, so the two records
corroborate each other and `pinned_digests_agree()` checks that they do.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PINS_ARTEFACT = "artifacts/slice55_data_eligibility.json"
FOLDS_ARTEFACT = "artifacts/funding_carry_fade_btc_v1_folds.json"

LINEAR_BTC = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
FUNDING_BTC = "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"

TIME_COLUMN = {
    "data/real_linear_1d": "time_period_start",
    "data/real_funding": "funding_time",
}


# --------------------------------------------------------------------------
# raw reading. Lines, not parsed rows, because the invariant is about bytes.
# --------------------------------------------------------------------------
def read_text(path: str) -> str:
    with gzip.open(os.path.join(REPO, path), "rt", encoding="utf-8",
                   newline="") as handle:
        return handle.read()


def read_lines(path: str) -> List[str]:
    """Header first, then one entry per data row, line endings preserved."""
    return read_text(path).splitlines(keepends=True)


def read_rows(path: str) -> List[dict]:
    return list(csv.DictReader(io.StringIO(read_text(path))))


def sha256_uncompressed(path: str) -> str:
    return hashlib.sha256(read_text(path).encode("utf-8")).hexdigest()


def sha256_prefix(path: str, data_rows: int) -> str:
    """sha256 of the header plus the first `data_rows` data rows.

    Raises if the file is shorter than the prefix asked for: a corpus that has
    lost rows is a defect, and returning the digest of whatever happens to be
    present would let it pass as a mismatch of unknown cause.
    """
    lines = read_lines(path)
    if len(lines) < data_rows + 1:
        raise ValueError(
            f"{path}: asked for a {data_rows}-row prefix but the file holds "
            f"{max(0, len(lines) - 1)} rows. A corpus may grow; it may never "
            f"shrink.")
    return hashlib.sha256("".join(lines[:data_rows + 1])
                          .encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the pins
# --------------------------------------------------------------------------
def pins() -> Dict[str, dict]:
    """Row count, digest and dates per corpus file, from the dated artefact."""
    with open(os.path.join(REPO, PINS_ARTEFACT), encoding="utf-8") as handle:
        eligibility = json.load(handle)
    out: Dict[str, dict] = {}
    for corpus, block in eligibility["corpora"].items():
        for symbol, entry in block["symbols"].items():
            out[entry["file"]] = {
                "corpus": corpus,
                "symbol": symbol,
                "rows": int(entry["rows"]),
                "sha256_uncompressed": entry["sha256_uncompressed"],
                "first_utc": entry["first_utc"],
                "last_utc": entry["last_utc"],
                "time_column": TIME_COLUMN[corpus],
                "pinned_by": PINS_ARTEFACT,
            }
    return out


def folds() -> dict:
    with open(os.path.join(REPO, FOLDS_ARTEFACT), encoding="utf-8") as handle:
        return json.load(handle)


def t1_micros() -> int:
    """`t1` in MICROseconds, the unit `market_data.parse_timestamp` returns.

    The fold calendar's `t1_ms` field is genuinely milliseconds
    (1786233600000 == 2026-08-09T00:00:00Z) and `backtest.Bar.start_ms` is in
    the same unit, so those two compare directly. `parse_timestamp`, which
    reads the CSV text, returns microseconds. This helper exists so the
    conversion happens once, in a function whose name states the unit, rather
    than as a bare `* 1000` at each call site where a reader has to work out
    which of three units is meant.
    """
    return int(folds()["t1_ms"]) * 1000


def pinned_digests_agree() -> Dict[str, bool]:
    """Two independent records pin the same two BTCUSDT digests. Do they match?

    The eligibility artefact was written in slice 55 and the fold calendar was
    locked in slice 57 from a separate code path. If they ever disagree, one of
    them has been edited, and neither can be trusted until a human says which.
    """
    pinned, source = pins(), folds()["sources"]
    return {
        LINEAR_BTC: (pinned[LINEAR_BTC]["sha256_uncompressed"]
                     == source["linear_bars"]["sha256_uncompressed"]),
        FUNDING_BTC: (pinned[FUNDING_BTC]["sha256_uncompressed"]
                      == source["funding"]["sha256_uncompressed"]),
    }


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PrefixCheck:
    path: str
    time_column: str

    pinned_rows: int
    pinned_sha256: str
    rows_on_disk: int
    whole_file_sha256: str
    prefix_sha256: Optional[str]

    history_unchanged: bool
    never_shrank: bool
    appended_rows: int
    appended_timestamps: Tuple[str, ...] = field(default=())
    appended_all_strictly_after_t1: bool = True
    appended_before_or_at_t1: Tuple[str, ...] = field(default=())

    @property
    def append_only(self) -> bool:
        """The full invariant. All three clauses of §45c, and nothing less."""
        return bool(self.history_unchanged
                    and self.never_shrank
                    and self.appended_all_strictly_after_t1)

    @property
    def extended(self) -> bool:
        return self.appended_rows > 0

    def why_not(self) -> str:
        if not self.never_shrank:
            return (f"{self.path} holds {self.rows_on_disk} rows where "
                    f"{self.pinned_rows} were pinned. A corpus may grow; it "
                    f"may never shrink.")
        if not self.history_unchanged:
            return (f"{self.path}: the first {self.pinned_rows} rows no longer "
                    f"hash to the pinned digest. MEASURED HISTORY HAS BEEN "
                    f"REWRITTEN, which invalidates every number taken over it.")
        if not self.appended_all_strictly_after_t1:
            return (f"{self.path}: {len(self.appended_before_or_at_t1)} "
                    f"appended row(s) are stamped at or before t1 "
                    f"({', '.join(self.appended_before_or_at_t1[:4])}). Growth "
                    f"must belong to the future; a back-fill into a measured "
                    f"window contaminates it without changing one existing "
                    f"byte.")
        return ""


def check(path: str, *, pinned: Optional[dict] = None,
          t1_microseconds: Optional[int] = None) -> PrefixCheck:
    entry = pinned if pinned is not None else pins()[path]
    boundary = t1_micros() if t1_microseconds is None else int(t1_microseconds)
    column = entry["time_column"]

    lines = read_lines(path)
    rows_on_disk = max(0, len(lines) - 1)
    never_shrank = rows_on_disk >= entry["rows"]

    prefix = sha256_prefix(path, entry["rows"]) if never_shrank else None
    unchanged = bool(prefix == entry["sha256_uncompressed"])

    appended = read_rows(path)[entry["rows"]:] if never_shrank else []
    stamps = tuple(str(row[column]) for row in appended)

    # Parsed with the production parser, not with a local date reader: the
    # boundary a decision uses and the boundary this check uses must be the
    # same boundary. A second implementation is a second opinion.
    import market_data as md  # noqa: PLC0415  (import cost, and REPO on path)
    early = tuple(s for s in stamps if md.parse_timestamp(s) <= boundary)

    return PrefixCheck(
        path=path,
        time_column=column,
        pinned_rows=entry["rows"],
        pinned_sha256=entry["sha256_uncompressed"],
        rows_on_disk=rows_on_disk,
        whole_file_sha256=sha256_uncompressed(path),
        prefix_sha256=prefix,
        history_unchanged=unchanged,
        never_shrank=never_shrank,
        appended_rows=len(appended),
        appended_timestamps=stamps,
        appended_all_strictly_after_t1=not early,
        appended_before_or_at_t1=early,
    )


def check_all() -> Dict[str, PrefixCheck]:
    boundary = t1_micros()
    return {path: check(path, pinned=entry, t1_microseconds=boundary)
            for path, entry in pins().items()}


def as_dict(result: PrefixCheck) -> dict:
    return {
        "path": result.path,
        "pinned_rows": result.pinned_rows,
        "pinned_sha256_uncompressed": result.pinned_sha256,
        "rows_on_disk": result.rows_on_disk,
        "whole_file_sha256_uncompressed": result.whole_file_sha256,
        "prefix_sha256_uncompressed": result.prefix_sha256,
        "history_unchanged": result.history_unchanged,
        "never_shrank": result.never_shrank,
        "appended_rows": result.appended_rows,
        "appended_timestamps": list(result.appended_timestamps),
        "appended_all_strictly_after_t1": result.appended_all_strictly_after_t1,
        "appended_before_or_at_t1": list(result.appended_before_or_at_t1),
        "append_only": result.append_only,
        "extended": result.extended,
        "why_not": result.why_not(),
    }


# --------------------------------------------------------------------------
# the funding seam. EDGE.md §45d.
# --------------------------------------------------------------------------
def funding_gap_hours(path: str, *, first: int = 0,
                      last: Optional[int] = None) -> List[float]:
    import datetime as dt  # noqa: PLC0415
    rows = read_rows(path)[first:last]
    stamps = [dt.datetime.fromisoformat(r["funding_time"]) for r in rows]
    return [(b - a).total_seconds() / 3600.0
            for a, b in zip(stamps, stamps[1:])]


def missing_funding_prints(path: str, *, cadence_hours: float = 8.0) -> List[str]:
    """Timestamps an 8-hour series should contain and does not.

    Slice 62's extension began at the next DAY boundary rather than the next
    PRINT, leaving a hole at 2026-08-09T16:00Z. It changes no number in this
    slice — but "fail closed on missing data" is a standing rule, and a hole
    tolerated silently today is a hole that silently feeds a stale funding rate
    to a decision in a later slice with more bars. So it is named, not absorbed
    into a widened bound.
    """
    import datetime as dt  # noqa: PLC0415
    rows = read_rows(path)
    stamps = [dt.datetime.fromisoformat(r["funding_time"]) for r in rows]
    step = dt.timedelta(hours=cadence_hours)
    missing: List[str] = []
    for earlier, later in zip(stamps, stamps[1:]):
        cursor = earlier + step
        while cursor < later:
            missing.append(cursor.isoformat())
            cursor += step
    return missing


if __name__ == "__main__":  # pragma: no cover - a convenience, not the check
    import sys
    sys.path.insert(0, REPO)
    print(json.dumps(
        {"pinned_digests_agree": pinned_digests_agree(),
         "checks": {p: as_dict(r) for p, r in check_all().items()},
         "missing_funding_prints_btc":
             missing_funding_prints(FUNDING_BTC)},
        indent=2))
