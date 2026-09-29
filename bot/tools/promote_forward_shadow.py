#!/usr/bin/env python3
"""Human-only promotion of a scratch forward score into
artifacts/forward_shadow_current.json.

WHY THIS TOOL EXISTS
====================
`tools/daily_forward_refresh.py` scores Stage B's forward counters to a
scratch path and NEVER writes to `artifacts/`. `docs/human/NO_GLUE_OPS.md`
(items 5-6) and `tools/control_plane_tick.py`'s own "WHAT IT MUST NEVER DO"
header both say the promotion of that scratch score into
`artifacts/forward_shadow_current.json` is done by a human, by hand, and is
never automated. This tool is that explicit, audited step — a human runs it
themselves, on purpose, never from a script or a cron job.

`bot/scripts/stage_b_forward_accrual.sh` MUST NEVER invoke this tool.

FAIL CLOSED
===========
Without BOTH `--write` and `--i-am-human`, this tool prints a before/after
summary and REFUSES with a non-zero exit. It never writes
`forward_shadow_current.json` unless a human passes both flags explicitly.

USAGE
=====
    python3 tools/promote_forward_shadow.py --from /tmp/scratch/forward.json
    python3 tools/promote_forward_shadow.py --from /tmp/scratch/forward.json \\
        --i-am-human --write

`--from` takes the scratch score file a human already produced — either
`daily_forward_refresh.py --scratch DIR`'s `DIR/forward.json`, or a direct
`tools/slice76_forward_shadow.py --out FILE` run. Both are the same schema as
`artifacts/forward_shadow_current.json`, so promotion is a straight copy once
a human has looked at the diff and decided it is correct.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)

from stage_b_bars import resolve_closed_forward_bars       # noqa: E402
import provenance as prov                                  # noqa: E402
import shadow                                               # noqa: E402
from signals import funding_carry_fade_btc_v1 as _signal    # noqa: E402

DEFAULT_SHADOW_PATH = os.path.join(BOT, "artifacts", "forward_shadow_current.json")

#: The exact corpus `tools/daily_forward_refresh.py` points
#: `slice76_forward_shadow.py --data-dir` at (see its `FORWARD_DATA_DIR`) —
#: the append-only, full-history tree the forward window is actually scored
#: against, NOT `data/real_linear_1d` (a different, shorter tree). A
#: candidate's `forward_window` claims (corpus frontier, bar count, bar
#: list) are only trustworthy if they match what is actually in this file
#: on disk in the tree being promoted into.
FULL_LINEAR_PATH = os.path.join(
    BOT, "data", "real_linear_1d_full", "ohlcv",
    "BINANCE_LINEAR_BTC_USDT_1D.csv.gz")

DAY_MS = 86_400_000

#: The counters Stage B accrual actually cares about; surfaced first in the
#: before/after summary. Everything else in the scratch report is copied
#: through verbatim on promotion (same schema as forward_shadow_current.json).
#: `closed_forward_bars` is resolved via `resolve_closed_forward_bars` below,
#: not read directly — the scorer nests it under `ceiling`/`forward_window`.
COUNTER_KEYS = ("forward_n_trades", "closed_forward_bars")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def content_hash(doc: Dict[str, Any]) -> str:
    """Canonical sha256 over ``doc``'s logical content.

    ``sort_keys=True`` makes the hash independent of key order, so a
    reformatted-but-identical scratch file (or a shadow file whose keys were
    ever emitted in a different order) still hashes equal. This is a content
    hash for no-op detection, separate from the on-disk byte layout the file
    is actually written in.
    """
    canonical = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    lines = []
    for key in COUNTER_KEYS:
        if key == "closed_forward_bars":
            before: Any = resolve_closed_forward_bars(old)
            after: Any = resolve_closed_forward_bars(new)
        else:
            before = old.get(key)
            after = new.get(key)
        marker = "  (CHANGED)" if before != after else ""
        lines.append(f"  {key}: {before!r} -> {after!r}{marker}")
    return "\n".join(lines)


def find_regressions(old: Dict[str, Any], new: Dict[str, Any]) -> list:
    """Monotonic fields that moved backward. A promotion that regresses one of
    these is evidence corruption (a rolled-back scratch file, a hand-edited
    counter, a scorer bug) rather than genuine forward accrual, so promotion
    must refuse it rather than silently overwrite good evidence with worse.

    Returns a list of human-readable reasons; empty means no regression.
    """
    reasons = []
    for key in COUNTER_KEYS:
        if key == "closed_forward_bars":
            before: Any = resolve_closed_forward_bars(old)
            after: Any = resolve_closed_forward_bars(new)
        else:
            before = old.get(key)
            after = new.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) \
                and after < before:
            reasons.append(
                f"{key} would go backward: {before!r} -> {after!r}")

    old_ts = old.get("observed_at_utc")
    new_ts = new.get("observed_at_utc")
    if isinstance(old_ts, str) and isinstance(new_ts, str) and new_ts < old_ts:
        reasons.append(
            f"observed_at_utc would go backward: {old_ts!r} -> {new_ts!r}")

    return reasons


def _parse_iso_utc_ms(value: Any) -> Optional[int]:
    """Milliseconds since epoch for an ISO-8601 UTC timestamp, or None if
    ``value`` is not a parseable string. Accepts both the `Z` suffix the
    scorer emits and a `+00:00` offset, since the corpus CSV uses the
    latter and candidate JSON uses the former."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.astimezone(dt.timezone.utc).timestamp() * 1000)


def _iso_z(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_full_linear_corpus(path: Optional[str] = None
                            ) -> List[Tuple[int, str]]:
    """(open_ms, open_iso) for every row of the append-only full-history
    linear corpus, sorted by open time. Raises on anything that makes the
    file untrustworthy as a frontier authority (missing, empty, malformed
    row, duplicate or out-of-order/non-daily bars) rather than returning a
    partial or silently-deduped view — a corpus this tool cannot fully
    trust is not one it can promote against."""
    if path is None:
        path = FULL_LINEAR_PATH
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        lines = [ln for ln in handle.read().split("\n") if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"corpus at {path!r} has no data rows")
    rows: List[Tuple[int, str]] = []
    for line in lines[1:]:
        fields = line.rstrip("\r").split(",")
        if len(fields) < 3:
            raise ValueError(f"corpus row is malformed: {line!r}")
        open_ms = _parse_iso_utc_ms(fields[2])  # time_open column
        if open_ms is None:
            raise ValueError(f"corpus row has an unparseable time_open: {line!r}")
        rows.append((open_ms, _iso_z(open_ms)))
    rows.sort(key=lambda r: r[0])
    for (a_ms, _), (b_ms, _) in zip(rows, rows[1:]):
        if b_ms <= a_ms:
            raise ValueError(
                "corpus has a duplicate or out-of-order daily bar around "
                f"{_iso_z(a_ms)}")
        if b_ms - a_ms != DAY_MS:
            raise ValueError(
                f"corpus has a gap between {_iso_z(a_ms)} and {_iso_z(b_ms)}")
    return rows


#: `forward_window` sub-fields a real slice7x candidate always carries
#: together (see `tools/slice76_forward_shadow.py`'s `build()`). If the
#: candidate declares a `forward_window` at all, every one of these must be
#: present — a partial claim cannot be independently checked, so it is
#: refused rather than silently trusted for the fields that happen to be
#: there.
REQUIRED_FORWARD_WINDOW_FIELDS = (
    "t1", "bars_strictly_after_t1", "of_which_closed", "corpus_last_bar_utc",
)


def validate_forward_window(candidate: Dict[str, Any],
                            corpus_path: Optional[str] = None) -> List[str]:
    """Independently re-derive the candidate's forward-window claims from
    the actual corpus file on disk, closing the stale-scratch attack: a
    candidate cannot claim a later corpus frontier, more closed forward
    bars, or a bar list the currently authoritative corpus does not
    actually support.

    A candidate with no `forward_window` at all (older schema, or a
    non-scorer artefact) is not checked here — there is nothing self-styled
    to re-derive, and `validate_provenance`'s other checks already cover
    signal/symbol/constants/git identity for that case.
    """
    window = candidate.get("forward_window")
    if window is None:
        return []
    if not isinstance(window, dict):
        return ["forward_window is present but is not an object"]

    missing = [f for f in REQUIRED_FORWARD_WINDOW_FIELDS if f not in window]
    if missing:
        return [
            "forward_window is missing required field(s) "
            f"{missing!r}; a partial forward-window claim cannot be "
            "independently verified and is refused rather than trusted"]

    if corpus_path is None:
        corpus_path = FULL_LINEAR_PATH

    reasons: List[str] = []

    t1_ms = _parse_iso_utc_ms(window.get("t1"))
    if t1_ms is None:
        reasons.append(f"forward_window.t1 is not a parseable timestamp: "
                       f"{window.get('t1')!r}")

    observed_ms = _parse_iso_utc_ms(candidate.get("observed_at_utc"))
    if observed_ms is None:
        reasons.append(
            "observed_at_utc is not a parseable timestamp: "
            f"{candidate.get('observed_at_utc')!r}")

    if reasons:
        return reasons

    try:
        corpus_rows = load_full_linear_corpus(corpus_path)
    except (OSError, ValueError) as exc:
        return [
            f"could not independently verify forward_window against the "
            f"authoritative corpus ({corpus_path!r}): {exc}"]

    if not corpus_rows:
        return ["the authoritative corpus has zero rows; nothing to "
               "promote a forward-window claim against"]

    actual_last_bar_utc = corpus_rows[-1][1]
    declared_last_bar_utc = window.get("corpus_last_bar_utc")
    if declared_last_bar_utc != actual_last_bar_utc:
        reasons.append(
            "forward_window.corpus_last_bar_utc mismatch: candidate "
            f"declares {declared_last_bar_utc!r}, the authoritative corpus "
            f"({corpus_path}) actually ends at {actual_last_bar_utc!r}")

    # Reproduce slice76_forward_shadow.py's own closed-bar rule exactly:
    # a daily bar open at t is a closed forward decision iff t > t1 and
    # t + 1 day <= observed_at.
    expected_bar_utcs = [
        iso for (ms, iso) in corpus_rows
        if ms > t1_ms and ms + DAY_MS <= observed_ms
    ]

    declared_count = window.get("bars_strictly_after_t1")
    if declared_count != len(expected_bar_utcs):
        reasons.append(
            "forward_window.bars_strictly_after_t1 mismatch: candidate "
            f"declares {declared_count!r}, independently recomputed from "
            f"the authoritative corpus it is {len(expected_bar_utcs)}")

    declared_closed = window.get("of_which_closed")
    if declared_closed != len(expected_bar_utcs):
        reasons.append(
            "forward_window.of_which_closed mismatch: candidate declares "
            f"{declared_closed!r}, independently recomputed it is "
            f"{len(expected_bar_utcs)}")

    declared_bar_utcs = window.get("bar_utcs")
    if declared_bar_utcs is not None and declared_bar_utcs != expected_bar_utcs:
        reasons.append(
            "forward_window.bar_utcs does not match the bars independently "
            "recomputed from the authoritative corpus — a fabricated, "
            "reordered, or stale bar list cannot be promoted")

    return reasons


def validate_provenance(candidate: Dict[str, Any]) -> List[str]:
    """Independently re-derive every fact the candidate claims about itself.

    A scratch score cannot certify its own provenance — every field checked
    here is recomputed from the actual frozen signal module, the actual
    shadow scope (`shadow.py`), or the actual git tree, and the candidate's
    self-reported value is refused whenever it disagrees. Returns a list of
    human-readable reasons; empty means the candidate's provenance checks
    out against the repository it is being promoted into.

    Only the CANDIDATE is checked. The current (already-promoted) shadow
    file may be historical evidence written before this check existed —
    that history is not rewritten or re-validated here.
    """
    reasons: List[str] = []

    signal = candidate.get("signal")
    if signal != shadow.SHADOW_SIGNAL:
        reasons.append(
            f"signal identity mismatch: candidate declares {signal!r}, the "
            f"shadow scope is {shadow.SHADOW_SIGNAL!r}")

    symbol = candidate.get("symbol")
    if symbol != shadow.SHADOW_SYMBOL:
        reasons.append(
            f"symbol mismatch: candidate declares {symbol!r}, the shadow "
            f"scope is {shadow.SHADOW_SYMBOL!r}")

    # Every real producer (slice6x/7x_forward_shadow.py) truncates the full
    # sha256 to 32 hex chars before writing `constants_fingerprint`, to match
    # the truncated `shadow.CONSTANTS_FINGERPRINT` pin — see e.g.
    # tools/slice76_forward_shadow.py's `shadow.constants_fingerprint()[:32]`.
    live_fingerprint_full = shadow.constants_fingerprint()
    live_fingerprint = live_fingerprint_full[:32]
    declared_fingerprint = candidate.get("constants_fingerprint")
    if declared_fingerprint != live_fingerprint:
        reasons.append(
            f"constants_fingerprint mismatch: candidate declares "
            f"{declared_fingerprint!r}, recomputed live from the frozen "
            f"signal module it is {live_fingerprint!r}")
    if live_fingerprint != shadow.CONSTANTS_FINGERPRINT:
        reasons.append(
            f"the live constants fingerprint ({live_fingerprint!r}) no "
            f"longer matches the frozen pin ({shadow.CONSTANTS_FINGERPRINT!r})"
            f" in this tree; a frozen constant changed and promotion must "
            f"not proceed from a tree in that state")

    declared_constants = candidate.get("constants")
    if isinstance(declared_constants, dict):
        declared_constants_fingerprint = hashlib.sha256(
            json.dumps(declared_constants, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        if declared_constants_fingerprint != live_fingerprint:
            reasons.append(
                "candidate's own 'constants' payload does not hash to the "
                "live frozen fingerprint — its constants dict and its "
                "declared constants_fingerprint have diverged")

    live_fund_abs = _signal.FUND_ABS
    declared_fund_abs = candidate.get("fund_abs")
    if declared_fund_abs != live_fund_abs:
        reasons.append(
            f"fund_abs mismatch: candidate declares {declared_fund_abs!r}, "
            f"the frozen signal constant is {live_fund_abs!r}")

    expected_caps = {
        "max_concurrent_positions": shadow.SHADOW_MAX_CONCURRENT_POSITIONS,
        "max_entries_per_day": shadow.SHADOW_MAX_ENTRIES_PER_DAY,
        "max_notional_usd": shadow.SHADOW_MAX_NOTIONAL_USD,
        "symbol": shadow.SHADOW_SYMBOL,
    }
    declared_caps = candidate.get("caps")
    if declared_caps != expected_caps:
        reasons.append(
            f"caps mismatch: candidate declares {declared_caps!r}, the "
            f"frozen shadow caps are {expected_caps!r}")

    declared_git = candidate.get("git_commit")
    actual_git = prov.git_commit()
    if not prov.is_real_commit(declared_git):
        reasons.append(
            f"git_commit is not a real commit sha ({declared_git!r}); "
            f"refusing rather than trusting an unverifiable claim")
    elif not prov.is_real_commit(actual_git):
        reasons.append(
            f"could not independently verify this tree's git identity "
            f"({actual_git}); refusing rather than trusting the "
            f"candidate's self-reported git_commit")
    elif declared_git != actual_git:
        reasons.append(
            f"git_commit mismatch: candidate declares {declared_git!r}, "
            f"this tree is actually at {actual_git!r} (including dirty "
            f"state) — promote only from the exact checkout the scratch "
            f"score was produced from")

    reasons.extend(validate_forward_window(candidate))

    return reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from", dest="from_path", required=True,
        help="scratch forward-score JSON to promote (daily_forward_refresh.py "
             "--scratch DIR's DIR/forward.json, or a slice76_forward_shadow.py "
             "--out file)")
    parser.add_argument(
        "--shadow-path", default=DEFAULT_SHADOW_PATH,
        help="artifacts/forward_shadow_current.json to update "
             "(override only for tests — never in normal use)")
    parser.add_argument("--write", action="store_true",
                        help="persist (default dry-run; requires --i-am-human too)")
    parser.add_argument(
        "--i-am-human", action="store_true", dest="i_am_human",
        help="confirms a human is running this by hand, not a script; "
             "required together with --write")
    args = parser.parse_args(argv)

    scratch = load_json(args.from_path)
    current = (load_json(args.shadow_path)
              if os.path.exists(args.shadow_path) else {})

    previous_hash = content_hash(current) if current else None
    candidate_hash = content_hash(scratch)

    print(f"scratch score : {args.from_path}")
    print(f"shadow file   : {args.shadow_path}")
    print(f"previous_hash : {previous_hash!r}")
    print(f"candidate_hash: {candidate_hash!r}")
    print("counters:")
    print(summarize(current, scratch))

    provenance_problems = validate_provenance(scratch)
    if provenance_problems:
        print(
            "\nREFUSED: the candidate's provenance does not check out "
            "against this tree's actual frozen signal, shadow scope, and "
            "git identity. Human flags do not override this — a candidate "
            "cannot certify itself:",
            file=sys.stderr)
        for reason in provenance_problems:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    regressions = find_regressions(current, scratch)
    if regressions:
        print(
            "\nREFUSED: promotion would move evidence backward, which can "
            "only mean a rolled-back scratch file, a hand-edited counter, or "
            "a scorer bug — never genuine forward accrual:",
            file=sys.stderr)
        for reason in regressions:
            print(f"  - {reason}", file=sys.stderr)
        return 1

    if not (args.write and args.i_am_human):
        print(
            "\nREFUSED: promotion requires BOTH --write and --i-am-human. "
            "This is a human-only step — see docs/human/NO_GLUE_OPS.md #5-6 "
            "and tools/control_plane_tick.py's charter ('must NEVER promote "
            "scratch forward scores into artifacts/forward_shadow_current.json'). "
            "Never call this tool from stage_b_forward_accrual.sh or any other "
            "automated script.", file=sys.stderr)
        return 1

    if previous_hash is not None and candidate_hash == previous_hash:
        # Identical canonical content: nothing to promote. Do not touch the
        # file merely to rewrite identical bytes or a fresh mtime — a repeated
        # valid promotion must be a true no-op, not a divergent artifact.
        print(
            f"\nPROMOTION_NOOP: candidate content is identical to the "
            f"current shadow file (hash {candidate_hash}); zero write")
        return 0

    tmp_path = f"{args.shadow_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(scratch, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, args.shadow_path)

    written_hash = content_hash(load_json(args.shadow_path))
    if written_hash != candidate_hash:
        print(
            f"\nPROMOTION FAILED VERIFICATION: wrote {args.shadow_path} but "
            f"its hash ({written_hash}) does not match the candidate "
            f"({candidate_hash}); treat the file as untrusted and "
            f"investigate before relying on it", file=sys.stderr)
        return 1

    print(
        f"\nPROMOTION_ACCEPTED by a human: wrote {args.shadow_path} "
        f"(hash {candidate_hash})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
