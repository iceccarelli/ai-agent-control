"""Durable, tamper-evident journal of every look at the frozen prospective evaluation.

``statistical_power.TrialLedger`` lives in memory: three evaluations with three fresh ledgers each report ``n_trials == 1``, and a ledger rebuilt
one entry shorter verifies, so the Holm correction in ``carry_prospective.evaluate`` is only as honest as the ledger it is handed. This module
keeps the chain in an append-only JSONL file and replays it, entry by entry, through ``TrialLedger.record`` so every hash is recomputed. It
never trusts a stored hash.

Truncating or rewriting the file is detectable only against an ANCHOR (count and head hash) kept somewhere the file's writer cannot reach: every
report carries the new anchor, and ``load`` / ``evaluate_logged`` refuse a journal that is shorter than, or does not extend, an anchor given to
them. Without an anchor the journal still catches edits and reordering, but not removal of its tail: the report says so.

Offline. The only file this module opens is the journal path it is given. Nothing is read from runtime data, and no network, process or database is
touched.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_prospective as CP  # noqa: E402
import statistical_power as SP  # noqa: E402

_FIELDS = ("strategy_id", "params_hash", "window_id", "statistic", "pvalue", "n_obs", "plan_digest")


def _reject_constant(name: str) -> Any:
    raise SP.LedgerTamperError(f"journal holds the non-finite number {name}")


def load(path: str, anchor: Optional[SP.LedgerAnchor] = None) -> SP.TrialLedger:
    """Rebuild the ledger from the journal. A missing file is an empty ledger, unless ``anchor`` says something existed."""
    ledger = SP.TrialLedger()
    if os.path.exists(path):
        if os.path.islink(path) or not os.path.isfile(path):
            raise SP.LedgerTamperError("the journal must be a regular file (not a link)")
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        if text and not text.endswith("\n"):
            raise SP.LedgerTamperError("the journal ends in a partial line: an interrupted write or an edit")
        for i, line in enumerate(text.split("\n")[:-1] if text else []):
            try:
                row = json.loads(line, parse_constant=_reject_constant)
            except ValueError as exc:
                raise SP.LedgerTamperError(f"journal line {i + 1} is not JSON") from exc
            if not isinstance(row, dict) or set(row) != set(_FIELDS) | {"index", "prev_hash", "entry_hash"}:
                raise SP.LedgerTamperError(f"journal line {i + 1} does not have the registered fields")
            trial = ledger.record(*(row[k] for k in _FIELDS[:3]), row["statistic"], pvalue=row["pvalue"], n_obs=row["n_obs"], plan_digest=row["plan_digest"])
            if trial.index != row["index"] or trial.prev_hash != row["prev_hash"] or trial.entry_hash != row["entry_hash"]:
                raise SP.LedgerTamperError(f"journal line {i + 1} does not match the hash chain recomputed from the lines before it")
    ledger.verify(anchor)
    return ledger


def _append(path: str, ledger: SP.TrialLedger, already: int) -> None:
    new = ledger.trials[already:]
    if not new:
        return
    lines = "".join(json.dumps({"index": t.index, **{k: getattr(t, k) for k in _FIELDS}, "prev_hash": t.prev_hash, "entry_hash": t.entry_hash},
                               sort_keys=True, allow_nan=False) + "\n" for t in new)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, lines.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def evaluate_logged(prints: Sequence[Tuple[float, float]], hurdle_pct: float, journal_path: str, *, anchor: Optional[SP.LedgerAnchor] = None,
                    now_ms: Optional[int] = None) -> Dict[str, Any]:
    """``carry_prospective.evaluate`` with the trial ledger loaded from, and the new look appended to, the journal. A refused evaluation appends
    nothing (as in ``evaluate``). The report gains ``journal``: the count, head and the anchor to keep OUTSIDE the journal."""
    ledger = load(journal_path, anchor)
    before = ledger.n_trials
    rep = CP.evaluate(prints, hurdle_pct, ledger, now_ms)
    _append(journal_path, ledger, before)
    a = ledger.anchor()
    rep = dict(rep)
    rep["journal"] = {"n_trials": a.n, "head": a.head, "anchor_to_keep_outside_the_journal": {"n": a.n, "head": a.head},
                      "checked_against_an_anchor": anchor is not None,
                      "limit": None if anchor is not None else "NO ANCHOR GIVEN: edits and reordering are caught, but removal of the journal's tail (or of the whole file) is not"}
    return rep
