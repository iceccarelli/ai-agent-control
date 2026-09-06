"""What this process is, stated truthfully, at startup and in every health check.

WHY THIS EXISTS
===============
Timing-skill research on this codebase is CLOSED. Two signal families were
measured against a validated instrument and both returned ABSENT
(`RESEARCH_CLOSE_STAGE1.md`):

    technical_analysis    M1/M2  76.1 / 77.5   against a bar of 95.0
    donchian_breakout_v1  M1/M2  91.2 / 91.5   against a bar of 95.0

A process in that position should not be capable of *implying* otherwise — not in
a log line, not in a health payload, not in a stored session record. This module
is how it says so out loud:

    timing_skill_research   CLOSED
    cleared_edge_signal     None
    execution_mode          paper

That is **operational self-knowledge, not autonomy**. Nothing here seeks profit,
arms anything, or unblocks anything. It is the prerequisite for a future
Stage-1-POSITIVE registration path, and it is emphatically not a substitute for
M1/M2 >= 95.

THE REGISTRATION HOOK REFUSES
=============================
:func:`cleared_edge_signal_from_artifacts` is the *only* way a signal name can
ever appear in :attr:`ProjectStatus.cleared_edge_signal`, and it is written to
refuse. It reads `edge_measurement` summary JSON and returns a name only when the
artefact says `EDGE_EVIDENCE_POSITIVE` **and** M1 >= 95.0 **and** M2 >= 95.0
**and** M2's own pass flag is set.

Every artefact in this repository fails that test. Pointed at the real
slice-24/25/26/28/29 summaries it returns ``None``, and
`tests/test_project_status.py` proves it by walking `artifacts/` and asserting so.

There is deliberately **no** setter, no override, no environment variable and no
"force" argument. A field that could be made to say `donchian_breakout_v1` by any
route other than a genuine POSITIVE measurement would be a lie with a plausible
shape, which is worse than no field at all.

WHAT THIS MUST NEVER CONTAIN
============================
Credentials, and performance metrics. The reasoning is the same as
`session_log.py`: a snapshot that reported profit would invite reading profit as
skill, which is the specific inference the research close forbids. The forbidden
field rule is asserted two ways, over a live snapshot and over the dataclass
fields read via AST.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List, Optional, Tuple

from session_log import NO_EDGE_CLAIM

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectStatus",
    "current",
    "cleared_edge_signal_from_artifacts",
    "ABSENT_SIGNALS",
    "FROZEN_ABSENT",
    "FROZEN_STATUS",
    "DUAL_SYMBOL_REQUIREMENTS",
    "MULTI_SYMBOL_MINIMUMS",
    "OOS_GATED_REGISTRATION",
    "M1_BAR",
    "M2_BAR",
    "RESEARCH_CLOSED",
    "FORBIDDEN_FIELD_MARKERS",
]

#: The one value `timing_skill_research` takes today. It is a constant rather
#: than a literal sprinkled through the code so that changing it is a visible,
#: reviewable act.
RESEARCH_CLOSED = "CLOSED"

#: The bars, unchanged since they were pre-declared in EDGE.md §5b. They are
#: duplicated here rather than imported from `tools/edge_measurement.py` because
#: `tools/` is not an installed package and this module must not depend on it —
#: `tests/test_project_status.py` asserts the two agree.
M1_BAR = 95.0
M2_BAR = 95.0

#: Measured failures, each with the reading that closed it and the artefact a
#: reader can check. Named here so a test can assert they never appear as a
#: cleared edge, and so a reader of this file learns why the field is None.
#:
#: A mapping rather than a bare tuple since slice 41: a name on a deny-list is
#: only as trustworthy as the evidence behind it, and "which numbers closed
#: this, and where are they" should not require a trip through EDGE.md. Removing
#: an entry means deleting its evidence line, which is a visible, reviewable act
#: — exactly as intended.
FROZEN_ABSENT: Dict[str, str] = {
    "technical_analysis":
        "M1/M2 = 76.1/77.5 (1D), 73.0/74.0 (4H), 72.2/76.0 (1H) vs a bar of "
        "95.0 — slice 24, see STAGE1_VERDICT.md and EDGE.md §5",
    "closed_analyser":
        "the same 76.1/77.5 (1D) reading under this signal's other name, "
        "against the same bar of 95.0; CLOSED for timing-skill research in "
        "slice 25 (H25_REJECT), see RESEARCH_CLOSE_STAGE1.md",
    "donchian_breakout_v1":
        "M1/M2 = 91.2/91.5 (1D) vs 95.0 — slice 28, "
        "artifacts/slice29_edge_donchian_regression_summary.json",
    "btc_alt_spillover_v1":
        "M1/M2 = 94.3/92.0 (ETHUSDT) and 91.3/90.0 (SOLUSDT) vs 95.0, on the "
        "full 1,461-date history under a control that PASSED its pre-declared "
        "three-clause rule (control_validated true on both artefacts) — "
        "slice 40, artifacts/slice40_edge_btc_alt_spillover_*_summary.json, "
        "frozen by human decision in slice 41, see EDGE.md §22",
    "post_shock_fade_v1":
        "M1/M2 = 1.6/2.0 (BTCUSD) vs 95.0 on 41 trades, under a control that "
        "PASSED (z -1.074, KS p 0.3767, 0.0% incomplete), control_validated "
        "true — artifacts/slice43_edge_post_shock_fade_BTCUSD_summary.json. "
        "The fade scored BELOW its own rotations (mean net R -0.2991 against a "
        "null of -0.0517; M2 delta -0.2480, CI excludes 0; 0 of 4 folds "
        "positive). ETHUSDT and SOLUSDT were NOT MEASURED: their controls were "
        "INVALID at 75.4% and 99.8% incomplete and their 10 and 7 setups fall "
        "below the 30-entry floor, so no percentile exists for either. Family "
        "verdict INCONCLUSIVE under the intake's multi-symbol rule (needs two "
        "symbols at >= 50 scored trades; zero reach it) — slice 43, frozen by "
        "human decision in slice 44, see EDGE.md §24f-§24h and §25",
    "range_location_fade_v1":
        "CONTROL INVALID on all three universe symbols — z = +3.632 (BTCUSD), "
        "+3.996 (ETHUSDT), +2.975 (SOLUSDT) against a bar of |z| < 1.96, KS "
        "p = 0.0004 / 0.0003 / 0.0122 against p >= 0.05, with 0% incompletes "
        "on every symbol, so the instrument was fully exercised and came back "
        "biased. EDGE WAS NOT MEASURED: there is NO M1 and NO M2 for this "
        "family, and any percentile quoted for it would be fabricated. Status "
        "INCONCLUSIVE — this is NOT an ABSENT percentile reading. The design "
        "itself was measurable: 281 / 112 / 117 entries after "
        "one-trade-per-run, every symbol clearing the intake's >= 50 gate. "
        "Logs: artifacts/slice46_control_range_location_fade_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log — slice 46, frozen by human "
        "decision in slice 47, see EDGE.md §27g-§27i and §28",
    "open_gap_fade_v1":
        "COUNT SHORTFALL AND CONTROL INVALID on all three universe symbols. "
        "The rule produced 1 / 0 / 0 scoreable entries (BTCUSD / ETHUSDT / "
        "SOLUSDT) against the intake's design target of >= 50 per symbol, and "
        "every control returned INVALID with 0 usable surrogates of 200 and "
        "100% incompletes. EDGE WAS NOT MEASURED: there is NO M1 and NO M2 "
        "for this family, and any percentile quoted for it would be "
        "fabricated. Status INCONCLUSIVE — this is NOT an ABSENT percentile "
        "reading. TWO SEPARATE CAUSES, recorded apart because they evidence "
        "different things. (1) The counts are a fact about the market: these "
        "are 24/7 spot venues with no overnight session, so open[t] is the "
        "print after close[t-1] — open[t] == close[t-1] exactly on 17.2% / "
        "48.1% / 50.7% of bars and the median gap is 0.0135% / 0.0002% / "
        "0.0000% of price against a 0.75-ATR threshold. (2) The control is a "
        "CONSTRUCTION MISMATCH, NOT instrument bias: skill_test."
        "surrogate_series re-bases with scale = price / bar.open, so every "
        "surrogate bar opens exactly at the prior close and the gap is "
        "identically zero — the null deletes the trigger's quantity instead "
        "of destroying its predictive value, and it must never be cited "
        "alongside the slice-46 z = +3.6 bias evidence. GAP_K stays 0.75; no "
        "grid was run and the null was NOT repaired after seeing its verdict. "
        "Logs: artifacts/slice49_count_finding_open_gap_fade.log, "
        "artifacts/slice49_control_open_gap_fade_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log, "
        "artifacts/slice49_no_edge_run_attestation.log — slice 49, frozen by "
        "human decision in slice 50, see EDGE.md §30g-§30l",
    "ts_momentum_v1":
        "COUNT SHORTFALL AND CONTROL INVALID on all three universe symbols. "
        "Scheduled entries 1 / 1 / 3 (BTCUSD / ETHUSDT / SOLUSDT) against the "
        "intake's hard gate of >= 50 per symbol, and every control returned "
        "INVALID with 0 usable surrogates of 200 and 100% incompletes. EDGE "
        "WAS NOT MEASURED: there is NO M1 and NO M2 for this family, and any "
        "percentile quoted for it would be fabricated. Status INCONCLUSIVE — "
        "this is NOT an ABSENT percentile reading; the correct statement is "
        "that NO TRUSTED STAGE-1 NUMBER EXISTS under the pre-declared "
        "construction. ROOT CAUSE: a STATE signal against an EVENT-shaped "
        "scheduler. The sign of a 10-bar return is defined on nearly every "
        "bar, so the flags form 1 / 1 / 3 CONTIGUOUS RUNS over 2934 / 1260 / "
        "1258 flagged bars, and skill_test.simulate_schedule takes at most "
        "ONE-TRADE-PER-RUN, on the run's first bar (slice 17, the rule that "
        "makes the rotation null valid). One run, one trade. THE THESIS IS "
        "NOT RARE: there are 441 / 209 / 201 sign flips in the three corpora, "
        "so the sample size is governed by the run structure rather than by "
        "how often the state changes. The controls failed the same way and "
        "NOT for slice 49's reason — the null PRESERVED this trigger (every "
        "surrogate reported 1 scoreable entry, where slice 49's reported 0) "
        "and the scheduler collapsed each surrogate below the 20-trade floor, "
        "so this is a CONSTRUCTION MISMATCH in the schedule builder and NOT "
        "instrument bias. LOOKBACK stays 10, SKIP stays 1, and the "
        "one-trade-per-run scheduler was NOT altered after the count "
        "shortfall was seen. Logs: "
        "artifacts/slice50_count_finding_ts_momentum.log, "
        "artifacts/slice50_control_ts_momentum_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log, "
        "artifacts/slice50_no_edge_run_attestation.log — slice 50, frozen by "
        "human decision in slice 51, see EDGE.md §31g-§31k and §32",
    "sign_flip_momentum_v1":
        "M1/M2 = 45.0/46.0 (BTCUSD, 290 trades), 81.4/83.5 (ETHUSDT, 134 "
        "trades) and 22.7/25.0 (SOLUSDT, 131 trades) vs a bar of 95.0, EVERY "
        "SYMBOL MEASURED UNDER A CONTROL THAT PASSED all three pre-declared "
        "clauses at 200/200 usable surrogates and 0% incompletes (z = +0.937 "
        "/ -0.760 / -0.766, KS p = 0.3336 / 0.4502 / 0.6325), "
        "control_validated true on all three summaries. Status ABSENT — a "
        "trusted instrument measured this hypothesis and it lost. This is NOT "
        "an INCONCLUSIVE closure: unlike range_location_fade_v1, "
        "open_gap_fade_v1 and ts_momentum_v1, percentiles EXIST here and they "
        "are quoted above. Counts cleared the intake's gate with room to "
        "spare: 290 / 134 / 131 scheduled entries against >= 50 required and "
        ">= 100 expected. ETHUSDT's 81.4/83.5 is the second-highest reading "
        "in this programme's history and is a FAILURE; its M2 delta is "
        "positive (+0.0671, CI excludes 0), which means the entries beat a "
        "shape-matched schedule on average, and that is NOT the test — the "
        "test is the percentile against 95.0. Three liquid majors returning "
        "45 / 81 / 23 on one rule is itself evidence against a real effect. "
        "LOOKBACK stays 10 and SKIP stays 1; no retune toward ETH's reading, "
        "no second run, no single-symbol rescue. Artefacts: "
        "artifacts/slice52_edge_sign_flip_momentum_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_summary.json, controls "
        "artifacts/slice52_control_sign_flip_momentum_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log — slice 52, frozen by human "
        "decision in slice 53, see EDGE.md §33g-§33l",
    "compression_breakout_v1":
        "FAMILY POSITIVE RULE NOT MET — one symbol measurable, two not. "
        "BTCUSD: M1/M2 = 69.1/73.5 on 76 scheduled entries under a control "
        "that PASSED all three pre-declared clauses (200/200 usable "
        "surrogates, z -1.483, KS p 0.1123, 0.0% incomplete), "
        "control_validated true — a trusted ABSENT reading against a bar of "
        "95.0, and 26 and 22 points short of it. ETHUSDT and SOLUSDT were NOT "
        "MEASURED: their counts were 23 and 35 against the intake's >= 50 "
        "hard gate, and their controls were INVALID on incompletes (11.5% and "
        "5.5% against a 5.0% bar; ETHUSDT also failed KS at p = 0.0137). "
        "THERE IS NO M1 AND NO M2 FOR ETHUSDT OR SOLUSDT and any percentile "
        "quoted for either would be fabricated. Status INCONCLUSIVE: this is "
        "NOT a dual-symbol ABSENT closure — the intake's POSITIVE rule "
        "requires two symbols at >= 50 scored trades and only one reached it, "
        "so the family verdict was already unreachable AT THE COUNT STAGE, "
        "before any control ran and long before BTCUSD's 69.1 existed. Nor is "
        "it an empty INCONCLUSIVE like range_location_fade_v1, "
        "open_gap_fade_v1 or ts_momentum_v1: one trustworthy reading exists "
        "and is quoted above. The compression filter did real work (it "
        "removed 64-78% of channel breaks: 93 of 310, 26 of 116, 48 of 135), "
        "so the design was materially different from the frozen "
        "donchian_breakout_v1 whose break half it shares. The alts' INVALID "
        "is a SPARSE-SAMPLE failure and is NOT the slice-49 or slice-50 "
        "construction mismatch — the null found compressed breaks and "
        "scheduled them, there were simply too few per surrogate. COMP_MAX "
        "stays 25 and BREAK_N stays 20; no grid was run, no percentile was "
        "computed under an INVALID control, and no single-symbol claim is "
        "made from BTCUSD. Logs: "
        "artifacts/slice53_count_finding_compression_breakout.log, "
        "artifacts/slice53_control_compression_breakout_"
        "{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log, "
        "artifacts/slice53_edge_compression_breakout_BTCUSD_summary.json — "
        "slice 53, frozen by human decision in slice 54, see EDGE.md "
        "§34g-§34k and §35",
    "funding_carry_fade_v1":
        "FAMILY ABSENT — the pre-declared >= 2-SYMBOL CONJUNCTION FAILED, on "
        "three symbols that were ALL successfully measured. This is the only "
        "entry on this list closed while a genuine EDGE_EVIDENCE_POSITIVE "
        "artefact for it sits in artifacts/, and that artefact is RETAINED "
        "UNEDITED. BTCUSDT: M1/M2 = 97.0/97.5 on 85 scheduled entries, mean "
        "net R +0.0964, verdict EDGE_EVIDENCE_POSITIVE, control_validated "
        "true (z +0.632, KS p 0.5386, 0.0% incomplete) — a real reading that "
        "clears both bars on ONE symbol, kept as evidence and NOT as a "
        "cleared edge: artifacts/slice55_edge_funding_carry_fade_BTCUSDT_"
        "summary.json. ETHUSDT: M1/M2 = 47.7/48.5 on 76 entries, mean net R "
        "-0.0358, control VALID (z +0.168, KS p 0.9242, 0.0% incomplete) — "
        "ABSENT. SOLUSDT: M1/M2 = 2.3/1.0 on 162 entries, mean net R -0.1658, "
        "control VALID (z +1.933, KS p 0.1561, 0.0% incomplete) — ABSENT, and "
        "strongly so: its M2 delta is -0.1315 with a 95% CI of [-0.1402, "
        "-0.1229], excluding zero on the WRONG side, on the LARGEST sample of "
        "the three. Every symbol cleared the >= 50-trade gate (85/76/162) and "
        "every control passed all three pre-declared clauses at 0% "
        "incompletes, so NOTHING here is INCONCLUSIVE — this is a MEASURED "
        "refusal, and the best-evidenced one in the programme. EDGE.md §36e, "
        "committed at d18a3b8 before the loader had produced a single setup, "
        "required control_validated on every symbol claimed AND at least TWO "
        "of {BTCUSDT, ETHUSDT, SOLUSDT} at >= 50 trades AND each of those at "
        "M1 >= 95.0 and M2 >= 95.0, and stated that single-symbol claims are "
        "refused. One symbol reached it; positive_rule_met is false. THIS IS "
        "NOT AN 'ALMOST POSITIVE' AND NOT A DUAL-SYMBOL CLEAR: best-of-three "
        "at a 95th percentile occurs about 14% of the time under a global "
        "null, and the three readings disagree in SIGN with the largest "
        "sample disagreeing hardest. Registration REFUSED the single-symbol "
        "promotion before this freeze existed — project_status."
        "MULTI_SYMBOL_MINIMUMS, added in slice 55 after that artefact was "
        "found to be sufficient on its own — so this name is now refused "
        "TWICE OVER, by the universe rule and by this deny-list. FUND_ABS "
        "stays 0.0001, the stop stays 1.5 ATR, the take profit stays 1.0 R, "
        "the horizon stays 5 and the cost model keeps charging funding over "
        "the hold; no grid was run, no symbol was dropped from the universe "
        "after the fact, no run was repeated, and no BTC-only rescue is "
        "permitted without a NEW human intake written BEFORE any re-use of "
        "these numbers. Logs and artefacts: "
        "artifacts/slice55_stage1_funding_carry_fade_v1.json, "
        "artifacts/slice55_edge_funding_carry_fade_"
        "{BTCUSDT,ETHUSDT,SOLUSDT}_summary.json, "
        "artifacts/slice55_control_funding_carry_fade_"
        "{BTCUSDT,ETHUSDT,SOLUSDT}_n1000.log, "
        "artifacts/slice55_count_finding_funding_carry_fade.log, "
        "artifacts/slice55_data_eligibility.json — slice 55, frozen by human "
        "decision in slice 56, see EDGE.md §36, §37 and §38",
}

#: How each frozen name was closed. Both statuses are refused identically by
#: the registration hook — the deny-list does not grade its entries — but the
#: record must not blur them, so the distinction lives here rather than only in
#: prose.
#:
#: ``ABSENT``        a trusted instrument measured it and it did not clear the
#:                   bar. There are percentiles, and they are in the evidence.
#: ``INCONCLUSIVE``  the instrument failed its own pre-declared control, so no
#:                   reading was taken. There are NO percentiles, and inventing
#:                   one would be fabrication rather than approximation.
FROZEN_STATUS: Dict[str, str] = {
    "technical_analysis": "ABSENT",
    "closed_analyser": "ABSENT",
    "donchian_breakout_v1": "ABSENT",
    "btc_alt_spillover_v1": "ABSENT",
    "post_shock_fade_v1": "ABSENT",
    "range_location_fade_v1": "INCONCLUSIVE",
    "open_gap_fade_v1": "INCONCLUSIVE",
    "ts_momentum_v1": "INCONCLUSIVE",
    "sign_flip_momentum_v1": "ABSENT",
    "compression_breakout_v1": "INCONCLUSIVE",
    # ABSENT, not INCONCLUSIVE, and the distinction is doing real work here.
    # All three symbols were measured under controls that passed; the family
    # verdict failed because the conjunction its own pre-declaration named was
    # not met. A reading was taken. Three were.
    "funding_carry_fade_v1": "ABSENT",
}

#: The deny-list itself, derived so the two can never disagree.
ABSENT_SIGNALS = tuple(FROZEN_ABSENT)

#: Signals whose pre-declaration requires EVERY listed symbol to clear both bars
#: before any claim may be registered. Added slice 37.
#:
#: `btc_alt_spillover_v1`'s intake form declared a two-symbol universe and an
#: anti-cherry-pick rule: "POSITIVE requires BOTH symbols... one symbol passing
#: and the other failing is ABSENT, not a partial success". Until now that rule
#: lived only in prose, and the hook would have registered a claim from a single
#: passing artefact. Slice 35 produced exactly that shape -- ETH 97.3/98.0 with
#: SOL at 85.1/81.5 -- so the rule is enforced in code rather than trusted to a
#: reader's discipline.
DUAL_SYMBOL_REQUIREMENTS: Dict[str, tuple] = {
    "btc_alt_spillover_v1": ("ETHUSDT", "SOLUSDT"),
}

#: Signals whose pre-declaration requires **at least K of N** named symbols to
#: clear both bars. Added slice 55, for the same reason slice 37 added the
#: all-of rule above and against a live example rather than a hypothetical one.
#:
#: `funding_carry_fade_v1`'s design note (EDGE.md §36e, committed at d18a3b8
#: before any number existed) says: "POSITIVE requires ... **at least two** of
#: {BTCUSDT, ETHUSDT, SOLUSDT} with >= 50 scored trades ... **Single-symbol
#: claims are refused.**" Slice 55 then measured all three: BTCUSDT cleared both
#: bars at 97.0 / 97.5 on 85 trades under a VALID control, ETHUSDT read
#: 47.7 / 48.5 and SOLUSDT read 2.3 / 1.0.
#:
#: With only `DUAL_SYMBOL_REQUIREMENTS` in place, that single **genuine,
#: unforged** artefact was enough: this function returned "funding_carry_fade_v1"
#: and `ProjectStatus` announced a cleared edge. That is slice 35's failure
#: recurring under a new name — a real positive on one symbol, the companions
#: failing, and a pre-declaration that existed only in prose.
#:
#: The lesson generalises past this entry, so it is written down here rather than
#: only in EDGE.md: **a multi-symbol rule that is not transcribed into this
#: module is not enforced by anything.** `tests/test_project_status.py` now
#: asserts that every stage-1 record in `artifacts/` which declares a
#: multi-symbol requirement has its signal registered in one of these two
#: mappings, so the next family cannot repeat it silently.
MULTI_SYMBOL_MINIMUMS: Dict[str, Tuple[tuple, int]] = {
    "funding_carry_fade_v1": (("BTCUSDT", "ETHUSDT", "SOLUSDT"), 2),
}

#: Signals whose pre-declaration makes an OUT-OF-SAMPLE window the only source
#: that may register a claim. Added slice 57.
#:
#: `funding_carry_fade_btc_v1`'s human intake is explicit, in a table with two
#: rows reading "NO": neither the slice-55 BTCUSDT summary nor a full-sample
#: replay may set `cleared_edge_signal` for this name. Only the late-50% window
#: may, and only if all five of its clauses hold.
#:
#: Slice 55 taught the general lesson the hard way and slice 56 wrote it down:
#: **a rule that lives only in prose is enforced by nothing.** A full-sample
#: diagnostic for this product exists in `artifacts/` and reports
#: EDGE_EVIDENCE_POSITIVE at 97.0 / 97.5. It is refused today only because it
#: carries no control attestation — attach one and it would register, and the
#: whole out-of-sample framing would have been decoration. So the requirement
#: is in code:
#:
#: * the summary must declare `registration_eligible: true`;
#: * it must declare `window: "oos_late"`;
#: * its `oos_folds_sha256` must match the fold calendar ON DISK, so a claim
#:   scored under one cut cannot survive the cut being edited;
#: * it must clear the intake's trade floor;
#: * and its observed mean net R must be **> 0** — the intake's fifth clause,
#:   which the generic bars do not cover. M1 and M2 are RELATIVE: a schedule can
#:   rank above its own rotations while still losing money after costs. Nothing
#:   before this product had to satisfy both.
#:
#: value = (folds path relative to the repo root, minimum scored trades)
OOS_GATED_REGISTRATION: Dict[str, Tuple[str, int]] = {
    "funding_carry_fade_btc_v1": (
        "artifacts/funding_carry_fade_btc_v1_folds.json", 40),
}


def _oos_gate_refusal(summary: Dict[str, Any],
                      signal: str) -> Optional[str]:
    """Why this artefact may not register `signal`, or None if it may.

    Returns a human-readable reason rather than a bool so the refusal can be
    logged with its cause — an operator reading a log should not have to guess
    which clause stopped it.
    """
    folds_rel, floor = OOS_GATED_REGISTRATION[signal]

    if summary.get("registration_eligible") is not True:
        return (f"it does not declare registration_eligible: true "
                f"(found {summary.get('registration_eligible')!r}). For "
                f"{signal} only the pre-declared out-of-sample window may "
                f"register; full-sample replays and prior exploratory "
                f"artefacts may not.")
    if summary.get("window") != "oos_late":
        return (f"its window is {summary.get('window')!r}, not 'oos_late'.")

    # Resolve the calendar against THIS MODULE's repository, not against the
    # artefact directory being read. The calendar is a committed repo-level
    # record; deriving its location from wherever the summaries happen to live
    # would let a claim be validated against a calendar dropped next to it.
    repo = os.path.dirname(os.path.abspath(__file__))
    folds_path = os.path.join(repo, folds_rel)
    if not os.path.isfile(folds_path):
        return (f"the fold calendar {folds_rel} is not on disk, so the cut "
                f"this claim was scored under cannot be verified.")
    with open(folds_path, "rb") as handle:
        on_disk = hashlib.sha256(handle.read()).hexdigest()
    if summary.get("oos_folds_sha256") != on_disk:
        return (f"its oos_folds_sha256 does not match the fold calendar on "
                f"disk. The cut has changed since this was scored, or the "
                f"claim was scored under a different one.")

    trades = ((summary.get("observed") or {}).get("n_trades"))
    if not isinstance(trades, int) or trades < floor:
        return (f"it reports {trades!r} scored trades against a pre-declared "
                f"floor of {floor}.")

    mean_r = ((summary.get("observed") or {}).get("mean_r"))
    try:
        mean_r = float(mean_r)
    except (TypeError, ValueError):
        return "its observed mean net R is missing or unreadable."
    if not mean_r > 0.0:
        return (f"its observed mean net R is {mean_r:+.6f}, which is not > 0. "
                f"M1 and M2 are RELATIVE — a schedule can beat its own "
                f"rotations while still losing money after costs — so the "
                f"pre-declaration requires the absolute sign as well.")
    return None


#: Same rule as `session_log.FORBIDDEN_FIELD_MARKERS`, same reason.
FORBIDDEN_FIELD_MARKERS = (
    "pnl", "profit", "return", "win_rate", "winrate", "equity",
    "performance", "sharpe", "expectancy", "drawdown",
)


def cleared_edge_signal_from_artifacts(
    artifact_dir: str, *, m1_bar: float = M1_BAR, m2_bar: float = M2_BAR,
) -> Optional[str]:
    """The name of a signal that has genuinely cleared Stage 1, or ``None``.

    This is the **only** route by which `ProjectStatus.cleared_edge_signal` can
    become non-None, and it is written to refuse.

    A summary qualifies only if **all four** hold:

    * ``verdict == "EDGE_EVIDENCE_POSITIVE"``
    * ``m1.percentile >= m1_bar``
    * ``m2.percentile >= m2_bar``
    * ``m2.passed`` is true — M2 is the drift-controlled contrast and is
      mandatory; M1 alone cannot carry a claim, because a long-only strategy on
      an appreciating asset ranks well against nulls that move entries without
      removing exposure.
    * ``control_validated`` is **explicitly true** — the instrument that
      produced the reading passed its own control for the construction used.
    * **every symbol its pre-declaration named has its own qualifying artefact**
      (:data:`DUAL_SYMBOL_REQUIREMENTS`). A signal that cleared on one symbol
      and failed on another has not cleared.

    That last requirement was added in slice 35, in response to a real failure
    rather than a hypothetical one. `btc_alt_spillover_v1` produced a genuine,
    unforged `EDGE_EVIDENCE_POSITIVE` summary on ETH (M1 97.3 / M2 98.0) from a
    run whose directed control had **failed** its pre-declared validation, and
    whose companion symbol had failed outright at 85.1 / 81.5. This function
    accepted it and `ProjectStatus` announced a cleared edge.

    Checking the verdict and the bars is not enough: a big number measured with
    an unvalidated ruler is exactly what this project exists not to believe. No
    artefact in this repository carries the attestation, so the answer is `None`
    — which is the correct answer, and it now has to be *earned* rather than
    merely not-forged.

    **And the signal must not be one of** :data:`ABSENT_SIGNALS`. Those were
    measured and failed; an artefact claiming otherwise contradicts
    `artifacts/slice29_edge_*_summary.json` and is refused whatever produced it.
    This is the belt to the braces: without it, dropping one hand-written JSON
    file into `artifacts/` would be enough to make this process announce a
    cleared edge for a signal that scored 91.2. If a future human genuinely
    re-measures one of these under a new pre-declared design and it passes, they
    remove it from that tuple deliberately — a visible, reviewable act, not a
    side effect of a file appearing on disk.

    A malformed or unreadable artefact is skipped, not guessed at. Fail closed:
    the answer to "has anything cleared?" defaults to no.

    Today this returns ``None`` for every artefact in the repository, and that is
    the correct answer, not a defect.
    """
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return None

    cleared: List[str] = []
    cleared_symbols: List[tuple] = []
    for name in sorted(os.listdir(artifact_dir)):
        if not name.endswith("_summary.json"):
            continue
        path = os.path.join(artifact_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
        except Exception as exc:  # noqa: BLE001
            logger.warning("unreadable edge artefact %s: %s", name, exc)
            continue

        if not isinstance(summary, dict):
            continue
        if summary.get("verdict") != "EDGE_EVIDENCE_POSITIVE":
            continue
        m1 = summary.get("m1") or {}
        m2 = summary.get("m2") or {}
        try:
            m1_pct = float(m1.get("percentile"))
            m2_pct = float(m2.get("percentile"))
        except (TypeError, ValueError):
            continue
        if m1_pct < float(m1_bar) or m2_pct < float(m2_bar):
            continue
        if not bool(m2.get("passed")):
            continue
        if summary.get("control_validated") is not True:
            logger.warning(
                "artefact %s reports %s but carries no validated-control "
                "attestation; refusing. A number measured with an unchecked "
                "ruler is not a claim.", name, summary.get("verdict"))
            continue
        signal = str(summary.get("signal") or "").strip()
        if not signal:
            continue
        if signal in ABSENT_SIGNALS:
            # Measured and failed. An artefact saying otherwise contradicts the
            # record, so the artefact is what is wrong. The evidence travels
            # with the refusal: an operator reading the log should not have to
            # go looking for the numbers that closed the signal.
            logger.error(
                "artefact %s claims a cleared edge for %r, which is FROZEN "
                "(%s): %s. Refusing.",
                name, signal, FROZEN_STATUS.get(signal, "FROZEN"),
                FROZEN_ABSENT[signal],
            )
            continue
        # SLICE 58 — a human may withdraw a cleared edge. Checked before the
        # gate, because a revoked claim is not a claim whatever its artefact
        # says, and checked by IMPORTING the module that owns the rule rather
        # than reimplementing it here: two copies of a revocation check is one
        # copy too many.
        try:
            import shadow as _shadow  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            _shadow = None
        if _shadow is not None and _shadow.is_revoked(signal):
            logger.error(
                "artefact %s claims a cleared edge for %r, which a human has "
                "REVOKED. The artefact is not wrong; the claim has been "
                "withdrawn. Refusing.", name, signal)
            continue

        if signal in OOS_GATED_REGISTRATION:
            refusal = _oos_gate_refusal(summary, signal)
            if refusal is not None:
                logger.warning(
                    "artefact %s claims a cleared edge for %r, whose "
                    "pre-declaration makes an out-of-sample window the only "
                    "registering source, but %s Refusing.",
                    name, signal, refusal)
                continue
        cleared.append(signal)
        cleared_symbols.append((signal, str(summary.get("symbol") or "")))

    # Multi-symbol pre-declarations, in two shapes. Both answer the same
    # question — did this clear on the universe it named, or only on the symbol
    # that happened to look best — and both exist because a real artefact once
    # got past their absence.
    #
    #   all-of  (DUAL_SYMBOL_REQUIREMENTS, slice 37): every declared symbol must
    #           have its own qualifying artefact;
    #   k-of-n  (MULTI_SYMBOL_MINIMUMS, slice 55): at least K of the N declared
    #           symbols must, which is what a universe rule looks like when the
    #           pre-declaration does not demand unanimity.
    qualified: List[str] = []
    for signal in sorted(set(cleared)):
        have = {sym for name, sym in cleared_symbols if name == signal}

        required = DUAL_SYMBOL_REQUIREMENTS.get(signal)
        if required is not None:
            missing = [sym for sym in required if sym not in have]
            if missing:
                logger.warning(
                    "%s cleared on %s but its pre-declaration requires %s; "
                    "missing %s. One symbol passing and another failing is "
                    "ABSENT, not a partial success. Refusing.",
                    signal, ", ".join(sorted(have)) or "nothing",
                    ", ".join(required), ", ".join(missing))
                continue

        minimum = MULTI_SYMBOL_MINIMUMS.get(signal)
        if minimum is not None:
            universe, floor = minimum
            passing = sorted(sym for sym in universe if sym in have)
            if len(passing) < int(floor):
                logger.warning(
                    "%s cleared on %s (%d of the %d symbols its "
                    "pre-declaration names: %s), but that pre-declaration "
                    "requires at least %d. A single symbol clearing while its "
                    "companions fail is ABSENT, not an almost-POSITIVE. "
                    "Refusing.",
                    signal, ", ".join(passing) or "nothing", len(passing),
                    len(universe), ", ".join(universe), int(floor))
                continue

        qualified.append(signal)
    cleared = [s for s in cleared if s in qualified]

    if not cleared:
        return None
    if len(set(cleared)) > 1:
        # Two different signals both claiming to have cleared is a state a human
        # must adjudicate, not something to resolve by picking one.
        logger.error(
            "multiple signals claim a cleared edge (%s); refusing to choose",
            ", ".join(sorted(set(cleared))),
        )
        return None
    return cleared[0]


@dataclass(frozen=True)
class ProjectStatus:
    """An immutable statement of what this process is and is not.

    Frozen on purpose. A snapshot a caller can mutate is a snapshot that can be
    made to say something untrue between being read and being logged.
    """

    timing_skill_research: str
    cleared_edge_signal: Optional[str]
    execution_mode: str
    policy_mode: str
    no_edge_claim: str
    entries_enabled: bool
    live_authorized: bool
    models_current_present: bool

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary_line(self) -> str:
        """One line for the startup log. Deliberately blunt."""
        cleared = self.cleared_edge_signal or "none"
        return (
            f"PROJECT MODE | timing_skill_research={self.timing_skill_research} "
            f"| cleared_edge_signal={cleared} "
            f"| execution_mode={self.execution_mode} "
            f"| policy_mode={self.policy_mode} "
            f"| entries_enabled={self.entries_enabled} "
            f"| live_authorized={self.live_authorized} "
            f"| {self.no_edge_claim}"
        )


def current(config: Any = None, *, artifact_dir: Optional[str] = None,
            repo_root: Optional[str] = None) -> ProjectStatus:
    """Build the snapshot from real state. Nothing here is a stored opinion.

    Every field is derived at call time: `live_authorized` from the config's own
    derived gate, `models_current_present` from the filesystem,
    `cleared_edge_signal` from the refusing artefact reader.
    """
    root = repo_root or os.path.dirname(os.path.abspath(__file__))
    artifacts = artifact_dir if artifact_dir is not None else os.path.join(
        root, "artifacts")

    live_authorized = bool(getattr(config, "LIVE_AUTHORIZED", False))
    models_present = os.path.exists(os.path.join(root, "models", "current"))
    cleared = cleared_edge_signal_from_artifacts(artifacts)

    return ProjectStatus(
        timing_skill_research=RESEARCH_CLOSED,
        cleared_edge_signal=cleared,
        # Derived, never set. `live` requires the full arming chain to have
        # already succeeded in config; this only reports what that decided.
        execution_mode="live" if live_authorized else "paper",
        policy_mode=str(getattr(config, "POLICY_MODE", "off") or "off").lower(),
        no_edge_claim=NO_EDGE_CLAIM,
        entries_enabled=bool(getattr(config, "ENTRIES_ENABLED", True)),
        live_authorized=live_authorized,
        models_current_present=models_present,
    )


def field_names() -> List[str]:
    """Declared field names, for the forbidden-field test."""
    return [f.name for f in fields(ProjectStatus)]
