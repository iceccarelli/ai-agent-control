"""The micro-live promotion gate. It says no, and it is built so that it keeps saying no.

WHAT THIS IS
============
A checklist with a conjunction over it. `promotion_gate_allows_live()` returns
`True` only when **every** item is complete **and** the live-arming chain is
independently satisfied. On this tree it returns `False`, and there is no
argument, flag or environment variable that changes that today.

WHAT IT IS NOT
==============
**It is not an arming mechanism.** It cannot set `live_authorized`, cannot write
config, cannot clear the kill switch and cannot place an order. It is a
*predicate* — a thing that answers a question — and the answer is currently no.

The distinction matters because a gate that could arm is a gate that can be made
to arm. `config.is_live_authorized` remains the only path to live and it reads
testnet, paper mode, the exact `LIVE_TRADING_ACK` token and both credentials. An
AST test asserts that no module assigns `live_authorized`, this one included.

WHY SIX OF THE EIGHT ITEMS ARE HUMAN ACTS
=========================================
A signed risk memo, an accepted M-4 policy, a recorded kill-switch drill, a
documented stop-verification path, a raised-notional authorisation, a typed
`LIVE_TRADING_ACK` — none of these is something a process can produce for
itself. That is the design. **A gate a process could satisfy on its own is not a
gate**, and this programme has already found one rule (slice 55's multi-symbol
clause) that existed only in prose and therefore enforced nothing.

The two machine-checkable items — the notional cap and the absence of
`models/current` — are checked from the live tree rather than read from the JSON,
so a hand-edited file cannot assert them falsely.

FAIL-CLOSED, INCLUDING AGAINST ITS OWN FILE
===========================================
* a missing gate file means REFUSE, not "assume complete";
* an unreadable gate file means REFUSE;
* an item the file does not mention means REFUSE;
* flipping every bool in the file to `true` still refuses, because the
  machine-checked items are re-derived and the live chain is consulted
  separately. Editing the JSON changes some inputs to a conjunction; it does not
  change the answer while the live chain is unsatisfied.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import shadow

logger = logging.getLogger(__name__)

__all__ = [
    "GATE_PATH",
    "CHECKLIST_ITEMS",
    "MIN_FORWARD_TRADES",
    "MIN_FORWARD_DAYS",
    "GateItem",
    "GateVerdict",
    "evaluate_promotion_gate",
    "promotion_gate_allows_live",
    "approval_digest",
]

GATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "artifacts",
    "slice59_promotion_gate.json")

#: EDGE.md §42e, declared BEFORE any new shadow output was read.
#:
#: 20 is the count at which the LAST monitor arms — M-4 needs 20 closed trades,
#: so below it the gate would be waving through a period in which one of its
#: four instruments was blind. 180 days is two non-overlapping M-2 windows, so
#: the 90-day rolling mean has had two independent looks rather than one.
#:
#: At the pilot's observed rate — 35 capped trades over 731 days — 20 forward
#: trades is roughly 14 months. This gate is not designed to open soon.
MIN_FORWARD_TRADES = 20
MIN_FORWARD_DAYS = 180

#: (key, human description, who can satisfy it)
CHECKLIST_ITEMS: Tuple[Tuple[str, str, str], ...] = (
    ("human_risk_memo_signed",
     "A signed human risk memo exists at a recorded path",
     "human"),
    ("forward_shadow_clean",
     f"Forward shadow with no ALERT across >= {MIN_FORWARD_TRADES} closed "
     f"trades AND >= {MIN_FORWARD_DAYS} calendar days of continuous "
     f"observation",
     "observation"),
    ("m4_recent_half_accepted_or_recovered",
     "The M-4 recent-half WARN is explicitly ACCEPTED in the memo, or has "
     "recovered under the UNCHANGED threshold",
     "human or observation"),
    ("kill_switch_drill_recorded",
     "A kill-switch drill has been performed and recorded",
     "human"),
    ("linear_protective_stop_verified",
     "The linear protective-stop verification path is documented",
     "human"),
    ("notional_cap_within_policy",
     f"max_notional_usd <= {shadow.SHADOW_MAX_NOTIONAL_USD:.2f} unless a human "
     f"memo raises it",
     "machine"),
    ("live_trading_ack_present",
     "The exact LIVE_TRADING_ACK token is present in the environment",
     "human"),
    ("models_current_absent_or_contained",
     "models/current is absent, or (future) contained to win_probability only",
     "machine"),
)


@dataclass(frozen=True)
class GateItem:
    key: str
    description: str
    owner: str
    complete: bool
    evidence: str


@dataclass(frozen=True)
class GateVerdict:
    allows_live: bool
    items: List[GateItem]
    reasons: List[str]

    @property
    def incomplete(self) -> List[str]:
        return [item.key for item in self.items if not item.complete]


def approval_digest(key: str, entry: Any) -> str:
    """SHA-256 of one gate entry exactly as written (key order ignored). A human
    approval is this digest recorded in a file OUTSIDE the repository
    (`tools/promotion_approve.py`), so a commit that flips the flag or edits
    the evidence cannot supply it, and an edit after approval voids it."""
    canonical = json.dumps({"key": key, "entry": entry}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: The checkout. An approvals file inside it could be committed, which is the
#: bypass the file exists to close.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _approvals_location_problem(path: str) -> Tuple[str, Optional[str]]:
    """(real path, why it may not be used). The directory is resolved, the final
    component is not followed: a link is refused, never laundered."""
    abspath = os.path.abspath(path)
    parent = os.path.realpath(os.path.dirname(abspath))
    real = os.path.join(parent, os.path.basename(abspath))
    root = os.path.realpath(REPO_ROOT)
    if real == root or real.startswith(root + os.sep):
        return real, f"approvals file {path!r} is inside the repository"
    return real, None


def _read_approvals(path: str) -> Tuple[Dict[str, str], Optional[str]]:
    """({key: digest}, problem). The file is VALIDATED AND READ THROUGH ONE
    DESCRIPTOR, so it cannot be swapped between the check and the read; a
    symlink, a non-regular file, a foreign owner, group/world write on the file
    or on its parent directory (unless sticky), or a file replaced while being
    opened, approves nothing. Unreadable or malformed content approves nothing.
    `problem` says why when a file was named and refused.

    This establishes that the file is private to the account running the
    process. It does not establish WHO wrote it: an attacker with that
    account's rights can write approvals too. It is a barrier against a commit
    or another account, not proof of human identity."""
    if not path:
        return {}, None
    real, problem = _approvals_location_problem(path)
    if problem is not None:
        return {}, problem
    try:
        lst = os.lstat(real)
    except OSError:
        return {}, None                         # absent: nothing is approved
    if stat.S_ISLNK(lst.st_mode):
        return {}, f"approvals file {path!r} is a symbolic link"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        return {}, f"approvals file {path!r} cannot be opened safely ({exc.strerror})"
    try:
        st = os.fstat(fd)
        why: Optional[str] = None
        if not stat.S_ISREG(st.st_mode):
            why = f"approvals file {path!r} is not a regular file"
        elif os.name == "posix":
            parent = os.path.dirname(real)
            pst = os.stat(parent)
            now = os.stat(real)
            if st.st_uid != os.geteuid():
                why = (f"approvals file {path!r} is owned by uid {st.st_uid}, "
                       f"not the running account")
            elif st.st_mode & 0o022:
                why = (f"approvals file {path!r} is group- or world-writable "
                       f"(mode {st.st_mode & 0o777:o})")
            elif pst.st_mode & 0o022 and not pst.st_mode & stat.S_ISVTX:
                why = (f"the parent directory of {path!r} is writable by "
                       f"others (mode {pst.st_mode & 0o777:o})")
            elif (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                why = f"approvals file {path!r} was replaced while being opened"
        if why is not None:
            return {}, why
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}, None
    finally:
        if fd >= 0:
            os.close(fd)
    approved = payload.get("approved") if isinstance(payload, dict) else None
    if not isinstance(approved, dict):
        return {}, None
    return ({k: v for k, v in approved.items()
             if isinstance(k, str) and isinstance(v, str)}, None)


def _read_gate(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        logger.warning("promotion gate file %s is missing; REFUSING", path)
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:  # noqa: BLE001
        logger.error("promotion gate file %s is unreadable (%s); REFUSING",
                     path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _live_chain_satisfied(config: Any = None) -> Tuple[bool, str]:
    """Ask the ONE authority on live arming. This module is not it.

    `config.is_live_authorized` reads testnet, paper mode, the exact
    `LIVE_TRADING_ACK` and both credentials. The gate consults it and never
    writes it: a gate that could arm is a gate that can be made to arm.
    """
    try:
        import config as _config
        cfg = config if config is not None else _config.load({})
        result = _config.is_live_authorized(cfg)
    except Exception as exc:  # noqa: BLE001
        return False, f"the live-arming chain could not be evaluated ({exc})"
    # `config.is_live_authorized` returns EXACTLY `(live_allowed, reason)`.
    #
    # THIS WAS WRONG AND IT FAILED OPEN. The first version assumed a 3-tuple
    # `(requested, authorized, reason)` and read `result[1]` — which on the real
    # 2-tuple is the REASON STRING, and a non-empty string is truthy. The gate
    # therefore reported the live chain satisfied while the shell was sitting in
    # SANDBOX (TESTNET+PAPER), and `test_flipping_every_bool_still_refuses`
    # caught it: with the checklist flipped, the gate said YES.
    #
    # That is the single worst failure this file could have had, in the one
    # place the design note promised fail-closed behaviour. So the shape is now
    # asserted rather than guessed, and ANYTHING unexpected refuses.
    if not (isinstance(result, tuple) and len(result) == 2):
        return False, (
            f"config.is_live_authorized returned {result!r}, which is not the "
            f"expected (live_allowed, reason) pair; refusing rather than "
            f"guessing which element means what")
    allowed, reason = result
    if not isinstance(allowed, bool):
        return False, (
            f"config.is_live_authorized returned a non-boolean authorisation "
            f"{allowed!r}; refusing")
    return allowed, str(reason)


def evaluate_promotion_gate(*, path: str = GATE_PATH, repo: Optional[str] = None,
                            config: Any = None,
                            approvals_path: Optional[str] = None) -> GateVerdict:
    """Every item, its state, and whether live may be promoted. Fails closed.

    A non-machine item is complete only if its flag is a JSON true AND the
    digest of its entry is in the approvals file (`approvals_path`, else the
    config's PROMOTION_APPROVALS_FILE, else none: nothing is approved)."""
    repo = repo or os.path.dirname(os.path.abspath(__file__))
    payload = _read_gate(path)
    if approvals_path is None:
        approvals_path = str(getattr(config, "PROMOTION_APPROVALS_FILE", "") or "")
    approvals, approvals_problem = _read_approvals(approvals_path)
    declared = (payload or {}).get("checklist", {})
    reasons: List[str] = []
    items: List[GateItem] = []

    if approvals_problem is not None:
        reasons.append(f"{approvals_problem}; no approval from it is counted")
    if payload is None:
        reasons.append(
            "the promotion gate file is missing or unreadable; a gate that "
            "cannot be read is a gate that refuses")

    for key, description, owner in CHECKLIST_ITEMS:
        if owner == "machine":
            # Re-derived from the live tree, never trusted from the file. A
            # hand-edited JSON cannot assert these falsely.
            if key == "notional_cap_within_policy":
                cap = float((payload or {}).get("max_notional_usd",
                                                shadow.SHADOW_MAX_NOTIONAL_USD))
                complete = (cap <= shadow.SHADOW_MAX_NOTIONAL_USD
                            and shadow.SHADOW_MAX_NOTIONAL_USD <= 100.00)
                evidence = (f"shadow.SHADOW_MAX_NOTIONAL_USD = "
                            f"{shadow.SHADOW_MAX_NOTIONAL_USD:.2f}, declared "
                            f"{cap:.2f}")
            else:
                present = os.path.exists(os.path.join(repo, "models", "current"))
                complete = not present
                evidence = f"models/current present: {present}"
        else:
            entry = declared.get(key) if isinstance(declared, dict) else None
            # Only a real JSON true counts. bool() of a non-empty string is
            # True, so a hand-edited "complete": "false" / "0" / "no" would
            # have marked a human-owned item complete.
            complete = (entry.get("complete") is True
                        if isinstance(entry, dict) else False)
            evidence = str((entry or {}).get("evidence", "")) \
                if isinstance(entry, dict) else "not declared in the gate file"
            if not isinstance(entry, dict):
                reasons.append(f"{key}: not declared in the gate file")
            elif complete and approvals.get(key) != approval_digest(key, entry):
                complete = False
                evidence = (f"flag set in the gate file but no matching human "
                            f"approval digest: {evidence}")

        items.append(GateItem(key=key, description=description, owner=owner,
                              complete=complete, evidence=evidence))
        if not complete:
            reasons.append(f"{key}: incomplete ({evidence})")

    checklist_complete = all(item.complete for item in items)

    # The live chain is consulted SEPARATELY and must also be satisfied. This
    # is what makes flipping every bool in the file insufficient.
    live_ok, live_why = _live_chain_satisfied(config)
    if not live_ok:
        reasons.append(f"the live-arming chain is not satisfied: {live_why}")

    return GateVerdict(allows_live=bool(checklist_complete and live_ok),
                       items=items, reasons=reasons)


def strategy_is_the_cleared_signal(strategy: Any, *, config: Any = None,
                                   artifact_dir: Optional[str] = None
                                   ) -> Tuple[bool, str]:
    """Is ``strategy`` the one the research record cleared? ``(ok, reason)``.

    A NEGATIVE-ONLY check: it can refuse a live-armed process whose attached
    strategy is not the cleared signal. It can never grant anything - a True
    here is one necessary condition among several, all evaluated by
    :func:`promotion_gate_allows_live` and config's arming chain. The
    research field stays inert as a permission: this function is the only
    place outside reporting that reads it, and it reads it to say no.

    ``None`` strategy is fine (a loop that only reconciles and protects).
    A strategy without ``signal_name`` is refused: nothing unmeasured may be
    attached to a live process.
    """
    if strategy is None:
        return True, "no strategy attached"
    try:
        import project_status as _ps
        cleared = _ps.current(config, artifact_dir=artifact_dir).cleared_edge_signal
    except Exception as exc:  # noqa: BLE001
        return False, f"project status unreadable ({exc}); refusing"
    name = getattr(strategy, "signal_name", None)
    if not cleared or name != cleared:
        return False, (
            f"attached strategy {type(strategy).__name__} (signal_name={name!r}) "
            f"is not the cleared edge signal {cleared!r}; an unmeasured strategy "
            f"may not trade live")
    return True, f"attached strategy is the cleared signal {cleared!r}"


def promotion_gate_allows_live(*, path: str = GATE_PATH,
                               repo: Optional[str] = None,
                               config: Any = None,
                               approvals_path: Optional[str] = None) -> bool:
    """`True` only if every item is complete AND live is independently armed.

    Returns `False` on this tree. Note what this function does NOT do even when
    it returns `True`: it does not arm anything. It answers a question. Arming
    remains `config.is_live_authorized`'s alone.
    """
    return evaluate_promotion_gate(path=path, repo=repo, config=config,
                                   approvals_path=approvals_path).allows_live
