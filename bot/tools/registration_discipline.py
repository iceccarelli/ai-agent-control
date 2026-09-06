#!/usr/bin/env python3
"""Can anything on disk make this process announce a cleared edge? Try to.

WHY THIS TOOL EXISTS AS A TOOL
==============================
Slices 49 through 54 each ran a forgery battery from a throwaway inline script
and committed only its log. That is one reproducibility step short: a log nobody
can regenerate is an assertion, not a check. This file is that script, kept.

WHAT IT DOES
============
Three batteries, in increasing order of how hard they are to refuse.

**1. The frozen names.** For every name in `project_status.FROZEN_ABSENT`, write
a perfectly-formed `EDGE_EVIDENCE_POSITIVE` summary on three symbols — bars
cleared, `m2.passed` true, `control_validated` true — and check the hook refuses
all of them and says why, quoting the evidence that closed each family. These
are forgeries: no such measurement happened.

**2. The universe rules.** For the two families whose pre-declaration named a
symbol universe, check that a proper subset registers nothing and that the full
requirement still registers something. A guard that refuses everything is
indistinguishable from a broken guard, so both directions are exercised.

**3. The real artefacts, unedited.** Point the hook at `artifacts/` and check it
returns `None`. Since slice 55 this is no longer trivially true: that directory
contains `slice55_edge_funding_carry_fade_BTCUSDT_summary.json`, a **genuine,
unforged** POSITIVE at M1 97.0 / M2 97.5 on 85 trades with an attested control.
Two independent guards refuse it — `MULTI_SYMBOL_MINIMUMS`, because
`funding_carry_fade_v1`'s design note declared a three-symbol universe requiring
two and only one cleared; and `FROZEN_ABSENT`, because a human froze the family
in slice 56. The battery lifts them one at a time and prints all four readings,
so "refused twice over" is demonstrated rather than claimed.

Battery 3 is the interesting one now. Batteries 1 and 2 refuse things that are
false. Battery 3 refuses something that is **true about one symbol** — and the
whole point of a pre-declared universe rule is that being true about one symbol
is not the claim that was declared.

WHAT IT DOES NOT DO
===================
It writes nothing into `artifacts/`; the forgeries live in a temporary directory
and are destroyed. It has no flag that lets a forgery through.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from typing import Dict, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import project_status as ps  # noqa: E402

SYMBOLS = ("BTCUSD", "ETHUSDT", "SOLUSDT")


def perfect_summary(signal: str, symbol: str) -> Dict:
    """A forgery that passes every check except the ones that matter."""
    return {
        "symbol": symbol, "bars": 1461, "signal": signal,
        "observed": {"n_trades": 120, "mean_r": 0.51},
        "m1": {"percentile": 99.9, "bar": 95.0, "passed": True},
        "m2": {"percentile": 99.9, "bar": 95.0, "passed": True,
               "delta": 0.44, "ci_low": 0.41, "ci_high": 0.47},
        "m3": {"folds": 4, "positive": 4, "soft_gate": 3, "soft_gate_met": True},
        "control_validated": True,
        "verdict": "EDGE_EVIDENCE_POSITIVE",
    }


def write(directory: str, signal: str, symbol: str) -> str:
    name = f"{signal}_{symbol}_summary.json"
    with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
        json.dump(perfect_summary(signal, symbol), handle)
    return name


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record):  # noqa: D102
        self.lines.append(record.getMessage())


def battery_one(tmp: str) -> bool:
    print("=" * 78)
    print("BATTERY 1 — FORGERIES FOR EVERY FROZEN NAME")
    print("=" * 78)
    print(f"deny-list size: {len(ps.FROZEN_ABSENT)}")
    print()
    ok = True
    for signal in ps.FROZEN_ABSENT:
        work = os.path.join(tmp, "b1", signal)
        os.makedirs(work, exist_ok=True)
        for symbol in SYMBOLS:
            write(work, signal, symbol)
        capture = Capture()
        ps.logger.addHandler(capture)
        try:
            result = ps.cleared_edge_signal_from_artifacts(work)
        finally:
            ps.logger.removeHandler(capture)
        for line in capture.lines:
            print(f"    {line}")
        refused = result is None
        explained = any(signal in line for line in capture.lines)
        ok = ok and refused and explained
        print(f"  {signal:26s} {'REFUSED' if refused else 'ACCEPTED — DEFECT'}"
              f"   status {ps.FROZEN_STATUS.get(signal, '?')}"
              f"{'' if explained else '   (UNEXPLAINED — DEFECT)'}")
    print()
    print(f"battery 1: {'PASS' if ok else 'FAIL'}")
    return ok


def battery_two(tmp: str) -> bool:
    print()
    print("=" * 78)
    print("BATTERY 2 — THE UNIVERSE RULES, IN BOTH DIRECTIONS")
    print("=" * 78)
    ok = True

    for signal, (universe, floor) in sorted(ps.MULTI_SYMBOL_MINIMUMS.items()):
        print(f"\n  {signal}: k-of-n, needs {floor} of "
              f"{len(universe)} ({', '.join(universe)})")
        for count in range(0, len(universe) + 1):
            work = os.path.join(tmp, "b2", f"{signal}_{count}")
            os.makedirs(work, exist_ok=True)
            for symbol in universe[:count]:
                write(work, signal, symbol)
            result = ps.cleared_edge_signal_from_artifacts(work)
            registers = result == signal
            expected = count >= floor and signal not in ps.ABSENT_SIGNALS
            good = registers == expected
            ok = ok and good
            print(f"    {count} of {len(universe)} clearing -> "
                  f"{'REGISTERS' if registers else 'refuses '}"
                  f"   expected {'REGISTERS' if expected else 'refuses'}"
                  f"   {'ok' if good else 'DEFECT'}")

        outside = os.path.join(tmp, "b2", f"{signal}_outside")
        os.makedirs(outside, exist_ok=True)
        for symbol in (universe[0], "DOGEUSDT", "XRPUSDT"):
            write(outside, signal, symbol)
        result = ps.cleared_edge_signal_from_artifacts(outside)
        good = result is None
        ok = ok and good
        print(f"    1 in universe + 2 outside it -> "
              f"{'refuses ' if good else 'REGISTERS — DEFECT'}"
              f"   (padding the count with symbols the pre-declaration never "
              f"named)")

    for signal, universe in sorted(ps.DUAL_SYMBOL_REQUIREMENTS.items()):
        frozen = signal in ps.ABSENT_SIGNALS
        print(f"\n  {signal}: all-of, needs {', '.join(universe)}"
              f"{'   (also FROZEN, so refused twice over)' if frozen else ''}")
        for count in range(0, len(universe) + 1):
            work = os.path.join(tmp, "b2d", f"{signal}_{count}")
            os.makedirs(work, exist_ok=True)
            for symbol in universe[:count]:
                write(work, signal, symbol)
            result = ps.cleared_edge_signal_from_artifacts(work)
            registers = result == signal
            expected = count == len(universe) and not frozen
            good = registers == expected
            ok = ok and good
            print(f"    {count} of {len(universe)} clearing -> "
                  f"{'REGISTERS' if registers else 'refuses '}"
                  f"   expected {'REGISTERS' if expected else 'refuses'}"
                  f"   {'ok' if good else 'DEFECT'}")

    print()
    print("  The all-of rule's only entry is frozen, so it can no longer show")
    print("  its teeth on a real name. tests/test_project_status.py exercises")
    print("  it on a hypothetical signal instead; the k-of-n rows above are")
    print("  the live demonstration that a universe rule still registers")
    print("  something when it is genuinely satisfied.")
    print()
    print(f"battery 2: {'PASS' if ok else 'FAIL'}")
    return ok


def battery_three() -> bool:
    """Every POSITIVE on disk is refused, EXCEPT the one its gate permits.

    REWRITTEN IN SLICE 57, BECAUSE THE INVARIANT CHANGED
    ====================================================
    This battery asserted that the hook returns None over the real `artifacts/`
    directory. That was the right invariant for as long as nothing had cleared,
    and it stopped being an invariant the moment something did. A check that
    keeps asserting a condition the programme has legitimately left behind is
    not discipline, it is a tripwire pointed at the wrong door.

    What replaces it is stricter, not looser. For EVERY artefact reporting
    EDGE_EVIDENCE_POSITIVE, exactly one of two things must be true:

    * it is REFUSED, and the reason is something registered in code — a freeze,
      a universe rule, a missing control attestation, or an OOS gate clause;
    * it is the ONE artefact its own pre-declaration permits to register, and
      breaking any single clause of that pre-declaration stops it registering.

    The second half is the part that matters now. A gate that admits a claim is
    only trustworthy if it would have refused a claim that fell short, so each
    clause is broken in turn and the refusal is demonstrated rather than
    asserted.
    """
    print()
    print("=" * 78)
    print("BATTERY 3 — EVERY POSITIVE ON DISK, AND WHAT REFUSES IT")
    print("=" * 78)
    artifacts = os.path.join(REPO, "artifacts")
    summaries = sorted(n for n in os.listdir(artifacts)
                       if n.endswith("_summary.json"))
    positives = []
    for name in summaries:
        with open(os.path.join(artifacts, name), encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("verdict") == "EDGE_EVIDENCE_POSITIVE":
            positives.append((name, data))

    registered = ps.cleared_edge_signal_from_artifacts(artifacts)

    print(f"  summaries on disk       : {len(summaries)}")
    print(f"  reporting POSITIVE      : {len(positives)}")
    print(f"  hook over artifacts/    : {registered!r}")
    print()

    ok = True
    permitted = []
    for name, data in positives:
        signal = str(data.get("signal") or "")
        attested = data.get("control_validated") is True
        eligible = data.get("registration_eligible")
        frozen = signal in ps.ABSENT_SIGNALS
        gated = signal in ps.OOS_GATED_REGISTRATION
        m1 = (data.get("m1") or {}).get("percentile")
        m2 = (data.get("m2") or {}).get("percentile")
        n = (data.get("observed") or {}).get("n_trades")

        if frozen:
            why = "FROZEN — refused by the deny-list"
        elif not attested:
            why = "no validated-control attestation — refused"
        elif gated and eligible is not True:
            why = (f"OOS-gated and registration_eligible={eligible!r} — "
                   f"refused ({data.get('window')!r} window)")
        elif gated and eligible is True:
            why = "PERMITTED by its own OOS pre-declaration"
            permitted.append((name, data))
        else:
            why = "no registered reason to refuse it"
            ok = False

        print(f"    {name}")
        print(f"      signal {signal}   M1 {m1}   M2 {m2}   n {n}")
        print(f"      -> {why}")

    print()
    if len(permitted) > 1:
        print("  MORE THAN ONE ARTEFACT IS PERMITTED — a human must adjudicate")
        ok = False
    elif not permitted:
        print("  Nothing is permitted to register. cleared_edge_signal is "
              f"{registered!r}.")
        ok = ok and registered is None
    else:
        name, data = permitted[0]
        signal = data["signal"]
        print(f"  ONE artefact is permitted: {name}")
        print(f"  It registers {signal!r}, and the hook agrees: "
              f"{registered == signal}")
        ok = ok and registered == signal
        print()
        print("  Breaking each clause of its pre-declaration in turn — a gate")
        print("  that admits a claim is only worth something if it would have")
        print("  refused one that fell short:")
        folds_rel, floor = ps.OOS_GATED_REGISTRATION[signal]
        breaks = [
            ("registration_eligible false",
             lambda d: d.update(registration_eligible=False)),
            ("window = full_sample",
             lambda d: d.update(window="full_sample")),
            ("fold hash from a different cut",
             lambda d: d.update(oos_folds_sha256="0" * 64)),
            (f"n_trades = {floor - 1} (floor {floor})",
             lambda d: d.update(observed=dict(d["observed"],
                                              n_trades=floor - 1))),
            ("mean net R = -0.05",
             lambda d: d.update(observed=dict(d["observed"], mean_r=-0.05))),
            ("mean net R = 0.0 (the rule is > 0, not >= 0)",
             lambda d: d.update(observed=dict(d["observed"], mean_r=0.0))),
            ("M1 = 94.9",
             lambda d: d.update(m1=dict(d["m1"], percentile=94.9))),
            ("M2 not passed",
             lambda d: d.update(m2=dict(d["m2"], passed=False))),
            ("control_validated false",
             lambda d: d.update(control_validated=False)),
        ]
        for label, mutate in breaks:
            work = tempfile.mkdtemp(prefix="oos_gate_")
            try:
                broken = json.loads(json.dumps(data))
                mutate(broken)
                with open(os.path.join(work, "x_summary.json"), "w",
                          encoding="utf-8") as handle:
                    json.dump(broken, handle)
                result = ps.cleared_edge_signal_from_artifacts(work)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            good = result is None
            ok = ok and good
            print(f"    {label:46s} -> "
                  f"{'refuses ' if good else 'REGISTERS — DEFECT'}"
                  f"   {'ok' if good else 'DEFECT'}")

        # And the control: unbroken, it does register.
        work = tempfile.mkdtemp(prefix="oos_gate_")
        try:
            with open(os.path.join(work, "x_summary.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(data, handle)
            result = ps.cleared_edge_signal_from_artifacts(work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        good = result == signal
        ok = ok and good
        print(f"    {'unbroken (the control)':46s} -> "
              f"{'REGISTERS' if good else 'refuses — DEFECT'}   "
              f"{'ok' if good else 'DEFECT'}")

    print()
    print(f"battery 3: {'PASS' if ok else 'FAIL'}")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="registration_discipline_")
    try:
        one = battery_one(tmp)
        two = battery_two(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    three = battery_three()

    status = ps.current()
    print()
    print("=" * 78)
    print("PROJECT STATUS AFTER ALL THREE BATTERIES")
    print("=" * 78)
    for key, value in status.as_dict().items():
        print(f"  {key:24s} = {value!r}")
    print()
    # `cleared_edge_signal` is no longer required to be None — slice 57
    # registered one under a pre-declared OOS gate, and battery 3 checks that
    # it is the ONLY thing that could. What must still hold unconditionally is
    # that a research record arms nothing: no model, no live, still paper.
    clean = (one and two and three
             and status.cleared_edge_signal not in ps.ABSENT_SIGNALS
             and status.models_current_present is False
             and status.live_authorized is False
             and status.execution_mode == "paper")
    print("=" * 78)
    print(f"REGISTRATION DISCIPLINE: {'HELD' if clean else 'BREACHED'}")
    print("=" * 78)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
