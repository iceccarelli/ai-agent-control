# HANDOFF — resume here

**Written at the end of slice 47.**

```
TIMING_SKILL: no cleared edge. FOUR families measured against pre-declared
           bars, FOUR ABSENT; a FIFTH never measured at all because its control
           was INVALID -- SIX frozen names in two categories. The bar
           (M1=M2=95.0) has never moved: 76 -> 91 -> 94 -> 1.6, then a blank.
RESEARCH: closed for indicator shopping on current BTC-only data.
EXECUTION: paper-ready (s29) + paper ops maturity (s30) + project-mode
           truthfulness (s31) — 84 containment, 42 session-log, 71 project-mode,
           57 data-contract and 41 acquisition tests; ENTRIES_ENABLED operator
           control; session log with a mandatory NO EDGE CLAIM line;
           docs/PAPER_RUNBOOK.md.
PROJECT MODE: startup log, health payload and every stored session record state
           timing_skill_research=CLOSED, cleared_edge_signal=none,
           execution_mode=paper. The registration hook exists and REFUSES.
DATA INTAKE: contract shipped (s32). 5 eligible corpora. The synthetic ETH/SOL
           under data/ohlcv remain SYNTHETIC and ineligible.
MULTI-ASSET: PRESENT and ELIGIBLE (s34). Real BINANCE SPOT ETH_USDT + SOL_USDT
           at 1D (1,461 bars) and 1H (35,063 bars), 2022-08 -> 2026-08, supplied
           by a human and verified against known market history. DATA ONLY: no
           Stage 1 run, no signal cleared. ETH_USDT also names a SYNTHETIC
           corpus under data/ohlcv -- select by PATH, never by symbol.
SIGNAL 3: btc_alt_spillover_v1 -- FROZEN ABSENT (s41), measured ABSENT (s40).
           The human froze it. It is in ABSENT_SIGNALS / FROZEN_ABSENT, so the
           registration hook refuses a POSITIVE artefact naming it EVEN IF that
           artefact is perfect -- right verdict, both bars, m2.passed, a valid
           control attestation, and both declared symbols. That exact shape
           registered successfully until s41. Retuning shock / stop / TP /
           horizon / cost is forbidden. 94.3 IS NOT A NEAR MISS.
           ETH M1/M2 94.3/92.0, SOL 91.3/90.0, against a bar of 95.0, on the
           full 1,461-date history, under controls that PASSED the human's
           three-clause rule (ETH z +1.899 KS p 0.0712; SOL z +1.609 KS p
           0.2213), control_validated=true on both artefacts. The directed null
           was repaired first: S1 hard horizon embargo on BOTH sides, S2
           direction sequence = scored entries only, S3 shared schedule builder
           with a genuinely rotated phase.
           Slice 35's ETH 97.3/98.0 was a truncated window read with a biased
           ruler. It survives neither the window nor the repair.
           DEFECT, declared before any M1 existed: ~1.5 points of upward bias
           remain and ETH's control clears by 0.061 of a z. Upward bias makes
           ABSENT the STRONGER reading. Remaining suspects, for a human to
           pre-declare: the percentile tie convention, the minimum_trades >= 20
           replicate filter. See EDGE.md 21d-21f.
           HISTORY: implemented (s35), diagnosed (s36), control
           rule adopted (s37), control FAILED on the full history (s38).
           CONTROL RULE (human pre-declared, s37): |z| < 1.96 AND KS vs
           U(0,100) p >= 0.05 AND incompletes <= 5%. median <= 50 RETIRED as a
           gate for the directed path, kept as an informational line.
           DATA IS NO LONGER THE BLOCKER. The human shipped the 571 BTC bars in
           s38; data/real_1d is 3,135 bars, 2018-01-01 -> 2026-08-01, and the
           intersection is the full 1,461 alt dates. Provenance verified, not
           assumed (EDGE.md 19a).
           THE INSTRUMENT IS THE BLOCKER. On the full window both controls are
           INVALID at n=1000, seed 20250730: ETH z +2.118 (KS p 0.0510);
           SOL z +2.165 AND KS p 0.0404. The same rule and seed passed on 890
           dates (z +0.372 / +0.287). The bias is ~2 percentile points, it is
           COMMON-MODE across two unrelated alt series (+1.62 and +1.75), and
           the bigger sample resolved it rather than caused it -- the first
           2,564 Bar objects are identical across the old and new corpus files.
           s39 APPLIED THE HUMAN'S ONE-DAY EXCLUSION (2025-01-07) to both paths
           and the control reproduced s38 BYTE-IDENTICALLY: same verdict blocks,
           and all 1,000 surrogate percentile lines per symbol identical. The
           exclusion changed no setup at all (341 -> 341). So the bias is NOT a
           calendar-date defect; it is a property of the DIRECTED CONSTRUCTION.
           It is also the direction that FLATTERS the signal, which is why the
           97.3/98.0 ETH scored in s35 is not to be believed either.
           NO real-series percentile has been computed for the full window. See
           EDGE.md 19c-19e and 20b-20f.
PAPER AGENT: CERTIFIED for continuous paper operation under NO EDGE CLAIM
           (s42). Standard: docs/PAPER_AGENT_CERTIFICATION.md, written before
           the tests. 90 certification tests in six groups -- truthfulness
           surfaces, human-only kill switch, operator control is not a risk
           gate, paper stays paper (credentials degrade CLOSED), freeze
           integrity, and an end-to-end fake-exchange cycle that leaves no
           naked position. Artefact:
           artifacts/slice42_paper_agent_certification.json, with its
           limitations and its revocation conditions.
           CERTIFIED SHELL != PROFIT AGENT. It says the process will not lie,
           will not arm, and will not strand a position. It says nothing about
           whether trading it would make money.
SIGNAL 4: post_shock_fade_v1 -- FROZEN ABSENT / INCONCLUSIVE (s44), measured
           (s43). In ABSENT_SIGNALS / FROZEN_ABSENT, so a POSITIVE artefact
           naming it is refused even if perfect -- right verdict, both bars,
           m2.passed, a valid attestation, all three universe symbols.
           DO NOT FLIP THE SIGN BECAUSE THE FADE LOST. DO NOT RE-SEARCH K.
           Human intake, same-asset
           shock FADE, K=2.0. Implemented exactly; design note committed before
           the code.
           NOT MEASURABLE AS PRE-DECLARED: K=2.0 needs a 9-13% single-day move
           and fires 45 / 10 / 7 times on BTC / ETH / SOL. The intake needs two
           symbols with >= 50 scored trades; zero reach it. ETH and SOL cannot
           even validate a control (75.4% and 99.8% of surrogates incomplete),
           so NO percentile was computed for them.
           WHERE IT COULD BE MEASURED, IT LOST. BTC: control VALID (z -1.074,
           KS p 0.3767, 0% incomplete), 41 trades, M1 1.6 / M2 2.0, mean net R
           -0.2991 against a null of -0.0517, delta -0.2480 with a CI excluding
           zero, 0/4 folds positive. Post-shock BTC leans CONTINUATION at this
           threshold and horizon.
           NOT A LICENCE: do not flip the sign because the fade lost, do not
           re-search K, do not extend the horizon, do not move the 1:1 target,
           do not add BNB/AVAX, no 4H/1H variant. Each is a new human intake.
           NOTE, useful: BTC's same-asset control mean is 49.05 -- BELOW 50. The
           ~1.5-point upward bias recorded for the CROSS-ASSET directed path
           does not appear here, which localises it further. Observation only.
SIGNAL 5: range_location_fade_v1 -- FROZEN INCONCLUSIVE (s47), measured as far
           as it could be (s46). In ABSENT_SIGNALS / FROZEN_ABSENT, so a
           POSITIVE artefact naming it is refused even if perfect.
           NOT AN ABSENT READING: there is NO M1 and NO M2. The control failed
           its own check before any percentile was taken, and project_status.
           FROZEN_STATUS holds that distinction in code (ABSENT vs
           INCONCLUSIVE) so it cannot decay into prose. Quoting a percentile
           for this family would be fabrication.
           DO NOT re-score under the failed control. DO NOT 'fix' the null
           knowing which way it failed -- that is a construction chosen to fit
           an observed result. Human intake, same-asset
           range-location fade, R=20, 0.90/0.10. Implemented exactly; design
           note committed before the code; counts committed before the control.
           THE DESIGN IS MEASURABLE -- 281 / 112 / 117 entries, every symbol
           clearing the >=50 gate that s43's design could not.
           THE RULER FAILED. All three controls INVALID at n=1000:
           BTC z +3.632 (KS p 0.0004), ETH z +3.996 (0.0003),
           SOL z +2.975 (0.0122) -- with 0% incompletes, so the instrument WAS
           exercised and came back biased. Largest bias recorded here.
           NO percentile was computed for any symbol.
           HYPOTHESIS, untested and NOT acted on: this trigger reads the same
           intrabar high/low geometry the barrier prices stops from, so
           state-matched direction may carry a mechanical advantage the
           recycled null direction does not. Ranking supports it -- s43's
           return-based same-asset control was CLEAN at 49.05, the cross-asset
           return trigger was +1.90, this high/low trigger is +3.63.
           A NULL FIX IS A HUMAN PRE-DECLARATION, written before the run it
           enables. Not mine to invent. See EDGE.md 27g-27i.
MODEL / SHADOW / LIVE: BLOCKED.
RESEARCH PROGRAM: FROZEN (s45). Authoritative ledger with every closing
           number and artefact path: docs/RESEARCH_PROGRAM_FREEZE.md.
           Operator pack for the certified paper shell:
           docs/PAPER_OPERATOR_RUNBOOK.md. Slice artefact:
           artifacts/slice45_research_freeze_and_paper_ops.json.
           NO measurement slice is legitimate until a human-filled intake
           clears the five-part gate in that document.
PROFIT PATH (what would actually move it, EDGE.md 25f):
    The certified paper shell is the EXECUTION half and it is DONE (s42). The
    missing half is EVIDENCE, and no engineering substitutes for it:
      1. a new material thesis, human-written, before any code -- a different
         INFORMATION SET from all four closed families, with its expected event
         count stated up front;
      2. a control for the construction it uses, validated before any
         percentile is read;
      3. M1 >= 95 AND M2 >= 95 on the pre-declared conjunction, from ONE run;
      4. only then walk-forward -> constrained policy (win_probability only)
         -> long shadow -> microscopic live.
    TWO STANDING OBSTACLES: no eligible corpus contains an ORDER BOOK, which
    closes off book/flow theses until data is supplied; and the ~1.5-point
    upward bias in the CROSS-ASSET directed instrument is unpaid (the
    same-asset control came in at 49.05, so it is not generic).
    THE BLOCKER IS NOT THE MACHINERY. It is that four honest attempts found
    nothing. The fastest route to a profit agent is a better hypothesis, not a
    lower bar.

ELEVEN CLOSED RESEARCH LINES (as of slice 56). Two kinds of closure, and
they are not interchangeable:

    ABSENT -- a trusted instrument measured it and it lost. Percentiles exist:
        technical_analysis / closed_analyser  76.1 / 77.5
        donchian_breakout_v1                  91.2 / 91.5
        btc_alt_spillover_v1                  94.3 / 92.0 and 91.3 / 90.0
        post_shock_fade_v1                    1.6 / 2.0 (BTC; ETH/SOL not measurable)
        sign_flip_momentum_v1                 45.0 / 46.0 (BTC, 290 trades)
                                              81.4 / 83.5 (ETH, 134 trades)
                                              22.7 / 25.0 (SOL, 131 trades)
                                              -- all three controls VALID at
                                              0% incomplete. The best-evidenced
                                              refusal here. ETH's 83.5 is a
                                              FAILURE, not a near miss.

    INCONCLUSIVE -- the family verdict could not be reached. For three of
    these there is no M1 and no M2 anywhere, and quoting one would be
    fabrication; the fourth read one symbol and not the two its rule needs:
        range_location_fade_v1  control INVALID, z +3.6 -- the instrument ran
                                1,000x per symbol at 0% incomplete and came
                                back BIASED. This is the programme's only
                                instrument-bias evidence.
        open_gap_fade_v1        counts 1/0/0 and 0 usable surrogates -- these
                                venues never close, and the null DELETES an
                                open-vs-prior-close trigger. Construction
                                mismatch in the NULL.
        ts_momentum_v1          entries 1/1/3 and 0 usable surrogates -- a
                                STATE signal forms one contiguous flag run and
                                the builder takes one trade per run. NOT a rare
                                thesis: 441/209/201 sign flips. Construction
                                mismatch in the SCHEDULE BUILDER.
        compression_breakout_v1 the MIXED case. BTC 69.1/73.5 on 76 trades
                                under a VALID control -- a trusted ABSENT --
                                while ETH (23) and SOL (35) fell below the
                                >=50 gate with INVALID controls, so NO M1/M2
                                exists for either. POSITIVE needed two symbols
                                and died at the COUNT stage. Sparse-sample
                                failure, NOT a construction mismatch.

ABSENT, FROZEN s56 -- funding_carry_fade_v1 (measured s55). FROZEN_ABSENT is
now ELEVEN. The first family whose trigger is not a price: the entry decision comes from
the venue's published funding rate, with the funding actually paid over each
hold charged as a cost on the observed schedule AND every replicate.

    BTCUSDT   85 trades   control VALID (z +0.632)   M1 97.0   M2 97.5
    ETHUSDT   76 trades   control VALID (z +0.168)   M1 47.7   M2 48.5
    SOLUSDT  162 trades   control VALID (z +1.933)   M1  2.3   M2  1.0

    Three VALID controls, 0% incompletes, every symbol past the >=50 gate --
    the FIRST time all three symbols produced trusted numbers at once. The
    pre-declaration (EDGE.md 36e, in git before any number) required TWO of
    three. One reached it, so the family reads ABSENT and cleared_edge_signal
    stays null. One of three symbols clearing 95 happens ~14% of the time
    under a global null, and these three DISAGREE IN SIGN: SOL's M2 delta is
    -0.1315 with a CI excluding zero, on the LARGEST sample.

    THE DEFECT THIS FOUND, WHICH MATTERS MORE THAN THE FAMILY: that BTCUSDT
    artefact is GENUINE, UNFORGED AND ATTESTED, and with the code as it stood
    it was ENOUGH -- the hook returned funding_carry_fade_v1 and ProjectStatus
    announced a cleared edge. The multi-symbol rule lived only in prose. That
    is slice 35 recurring under a new name; slice 37 fixed that instance,
    slice 55 fixed the class:
      - project_status.MULTI_SYMBOL_MINIMUMS (k-of-n, so the transcription is
        FAITHFUL rather than merely stricter -- a gate may never be lowered,
        and inventing a stricter one after seeing numbers is its own problem);
      - a structural test that every stage-1 record declaring a multi-symbol
        rule is registered in code;
      - tools/registration_discipline.py, whose third battery removes the
        universe rules and shows the claim registers without them. A guard
        that would have refused anyway is not a guard.
    A MULTI-SYMBOL PRE-DECLARATION NOT TRANSCRIBED INTO project_status IS
    ENFORCED BY NOTHING. See EDGE.md 37.

    FROZEN BY HUMAN DECISION IN SLICE 56 (EDGE.md 38). The BTC artefact stays
    on disk BYTE-FOR-BYTE, still reading EDGE_EVIDENCE_POSITIVE -- deleting
    the one inconvenient file is the worse of the two dishonesties available,
    and a repo that removes it is one whose remaining artefacts mean less. It
    is refused TWICE OVER now, and a forgery supplying ALL THREE symbols --
    which satisfies the universe rule completely -- is still refused.
    Forbidden forever without a NEW human intake written BEFORE any re-use of
    these numbers: registering BTC-only; editing that artefact; lowering
    FUND_ABS or changing stop/TP/horizon/costs; DROPPING ETH OR SOL FROM THE
    UNIVERSE POST-HOC; re-scoring under a changed universe.

    Do not cite the last two alongside the first as bias evidence.

NEXT HUMAN DECISION ONLY:
    (1) STOP. Ten lines closed, nothing cleared. The best-motivated thesis
        got to 94.3 against a bar of 95.0 on an instrument later shown to
        flatter it, and the best-MEASURED one (s52: three symbols, three valid
        controls, 555 trades) returned 45 / 81 / 23. This is a legitimate
        ending; or
    (2) WRITE A NEW INTAKE YOURSELF. NEW_SIGNAL_INTAKE.md is marked
        WAITING -- EMPTY and no agent may fill it -- a hypothesis proposed by
        the thing that measures it is not an independent hypothesis. It must
        have a DIFFERENT INFORMATION SET from all ten frozen names, not
        different weights, thresholds or lookbacks on the same one. Check two
        things in one line of arithmetic BEFORE implementing: how many events
        the rule produces, and how many contiguous FLAG RUNS they form; or
    (3) pre-declare, IN WRITING AND BEFORE ANY SCORED RUN, a fix for the ~1.5
        points of upward bias still in the directed instrument. S1-S3 removed
        only 12% of ETH's and 27% of SOL's. Two suspects remain, both outside
        S1-S3 and both deliberately untouched in s40: the percentile convention
        ((d < value).mean(), strictly-less-than, so ties count as not below) and
        the minimum_trades >= 20 filter on replicates, which truncates the null
        asymmetrically when a rotation yields a short schedule. This does NOT
        reopen spillover -- it is owed to whatever directed thesis comes next.
        Keep the seed, keep n=1000, keep the three-clause rule.
    NOT a Donchian N-search. NOT a retuned oscillator committee. NOT a
    spillover with a different shock multiplier. NOT another seed, and NOT
    n=200 -- at n=200 the directed bias is invisible and both symbols pass.
NOT READY for live capital.
```

**Cold-start reading order:** [RESEARCH_STATUS.md](RESEARCH_STATUS.md) →
[RESEARCH_CLOSE_STAGE1.md](RESEARCH_CLOSE_STAGE1.md) →
[STAGE1_VERDICT.md](STAGE1_VERDICT.md) → this file → `EDGE.md`.
To *run* it: [docs/PAPER_RUNBOOK.md](docs/PAPER_RUNBOOK.md).

---

**STAGE 1 IS CLOSED FOR BOTH SIGNALS MEASURED SO FAR.**
**Status: TWO SIGNALS SCORED, BOTH ABSENT.**

* `technical_analysis` (oscillator committee) — **CLOSED**, M1/M2 76.1/77.5 on
  1D and comparable on 4H/1H (slices 24–25).
* `donchian_breakout_v1` — **SCORED_ABSENT**, M1/M2 **91.2/91.5** vs a bar of
  95.0 (slice 28, EDGE.md §9c). Better than the analyser by roughly fifteen
  percentile points and still a FAIL. **This parameterisation is closed to
  bar-shopping.**

Model, shadow and live remain **BLOCKED** — unchanged, and not conditional on
either result.

Detail on the two measurements: `EDGE.md` §9a–§9d (slice 28) and §10a–§10e
(slice 29). Intake form for a new thesis:
**[NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md)**.

This file exists because the working environment is discarded between sessions.
Everything needed to resume at **slice 38** is here: what is proven, what is
still red, what has been ruled out and why. Where a number matters it is written
down rather than described.

### If you are slice 38, read this first

The tempting move is an N sweep. 91.2 is close enough to 95 that one feels owed.
**Do not.** N = 55 was fixed in EDGE.md §9b before the code existed; the run
returned ABSENT; sweeping N over a 2,564-bar corpus would clear 95 by
construction and mean nothing. Same for the ATR multiples, the horizon, and
re-running on 4H/1H — other timeframes were permitted only *after* a POSITIVE.

Slices 29 and 30 already did the honourable version of "what can be improved
without a new measurement": 29 closed the research formally and proved the
execution stack is safe to operate in paper; 30 hardened paper operations and
made the stand-down claim *measured* rather than assumed. That seam is now
worked out. The legitimate next step is one of the three human decisions in the
status block above.

Note also §11a of `EDGE.md`: **do not use a short control run as a gate.** At
n=12 or n=24 the `median <= 50` clause fails for a perfect instrument about half
the time. Use `--control-runs 200`.

---

## 0. How to restart from cold

```bash
unzip tradingbot_slice37.zip -d tradingbot && cd tradingbot
pip install -r requirements.txt
python3 -m pytest tests/ -q          # expect: 3112 passed, 1 skipped  (~5 min)
```

Re-establish the baseline before touching anything — every slice has begun this
way and it is the only defence against building on a remembered number:

```bash
# geometry: must reproduce EXACTLY the block in section 2
python3 tools/run_evaluation.py --data-dir data/real_1d --interval D \
        --min-confidence 0.12 --min-agreement 0.4          # ~4 min

# CONTAMINATED REGRESSION -- must keep FAILING. exit 1.
# If this ever nears 50, something broke; stop and diagnose.
python3 tools/skill_test.py --data-dir data/real_1d --interval D \
        --runs 500 --control-runs 6 --null block_resample --lockup 1   # ~12 min
        # expect median 82.5, mean 76.5, z = +2.25, CONTROL: INVALID, exit 1

# THE RECOMMENDED CONTROL INVOCATION (validated, slice 23; ~31 min).
# Note --null rotation --lockup 1 EXPLICITLY: --lockup still defaults to the
# horizon and was deliberately NOT changed (EDGE.md 7b, H2), so that historical
# command blocks in EDGE.md still mean what they meant when they were run.
# median 48.0, mean 47.6, z = -1.18, 0 of 200 incomplete, KS p = 0.435.
# Since slice 26 this exits 0 -- its exit code answers "is the instrument
# valid?", NOT "is there skill?". Edge claims belong to edge_measurement.py.
python3 tools/skill_test.py --data-dir data/real_1d --interval D \
        --runs 1500 --control-runs 200 --null rotation --lockup 1

# the edge measurement, CLOSED analyser -- still ABSENT, still exits 1 at bars 95.0
# (--signal defaults to closed_analyser, so this command is unchanged from s24)
python3 tools/edge_measurement.py --runs 1500 --shape-schedules 200
        # expect M1 76.1, M2 77.5, Delta +0.1396, 2/4 folds, ABSENT, exit 1

# the edge measurement, donchian_breakout_v1 -- ABSENT, exit 1
python3 tools/edge_measurement.py --data-dir data/real_1d --interval D \
        --signal donchian_breakout_v1 --runs 1500 --shape-schedules 200
        # expect 77 trades, M1 91.2, M2 91.5, Delta +0.2690, 3/4 folds, exit 1
```

Long runs must go in the background — several exceed any single command timeout.
Two traps cost slice 28 real time and are worth avoiding:

* **`setsid nohup … & disown`, not plain `nohup … &`.** A plain background job
  was killed mid-run at surrogate 168 of 200 when its parent shell died, and had
  to be restarted from zero (~45 min).
* **`cd` into the repo explicitly inside the backgrounded command.** Background
  shells do not inherit the interactive cwd, and the failure mode is a confusing
  `can't open file '/home/claude/tools/skill_test.py'`.

```bash
setsid nohup bash -c 'cd /path/to/repo && python3 tools/… > /tmp/out.txt 2>&1; \
    echo "EXIT=$?" >> /tmp/out.txt' < /dev/null > /dev/null 2>&1 & disown
```

Wall-clock on a 2-core box: rotation control n=200 ≈ 45 min, edge measurement
≈ 25 min, full pytest ≈ 4 min idle but ~16 min under contention. Do not run three
at once. The bisection in section 6 took roughly 45 minutes per batch of 12
surrogates.

---

## 1. The one-paragraph state

A positive-expectancy **geometry** exists and is green. **As of slice 23 the
skill instrument is validated**: under a design committed to git *before* the
run, on 200 structure-free surrogates where nothing can be timed, the control
reports median **48.0**, mean 47.6, uniformity **z = −1.18**, 0 of 200
incomplete, and a KS test against U(0,100) that does not reject (p = 0.435).
Every one of the three pre-declared conditions is met. That took fourteen
slices and eleven null designs, and the last artefact — the block-resample
placement search — was found in slice 21.

**Slice 24 then made the measurement the ruler was built for, and the answer
is negative.** Under the validated path, on the real series: M1 percentile
**76.1** against a pre-declared bar of 95.0, and M2 — the drift-controlled
contrast — **77.5** against the same bar, with **45 of 200 (22.5%)
information-free schedules of identical shape beating the analyser outright**.
2 of 4 folds positive against a soft gate of 3. **EDGE_EVIDENCE_ABSENT.**

The number that explains the project: an information-free schedule with the
analyser's own trade count, run lengths and gaps, placed with **no knowledge of
the bars**, earns **+0.1443 R per trade** on this corpus. The analyser earns
+0.2838. Roughly half the geometry gate's headline expectancy is available to a
schedule that cannot see prices.

**Slice 25 closed Stage 1.** H25 asked whether the ABSENT reading was a
small-n / single-asset artefact. Re-run on the two other real corpora in the
repo, same analyser, same instrument, same bars:

```
corpus   bars     trades   strategy R   blind R    M1     M2     Delta
1D       2,564        47    +0.2838    +0.1443   76.1   77.5   +0.1396
4H      15,379       350    +0.0294    -0.0065   73.0   74.0   +0.0358
1H      61,513     2,039    -0.1431    -0.1595   72.2   76.0   +0.0165
```

**H25_REJECT.** Trade count rises **43x** and the percentiles do not move
toward 95 — they sit flat at 72–78. It was never a sample-size artefact. The
analyser does beat the *average* blind schedule reproducibly (Delta > 0, CI
excludes 0, three times), but **22–26% of blind schedules beat it outright** at
every timeframe. On 1H both sides are negative. At every resolution exactly one
fold carries the result, and it is the same calendar window — the 2019–2021
bull run.

**This classical signal layer is CLOSED for timing-skill research.** Model
training, shadow mode and live capital remain blocked — for a measured reason,
not a procedural one.

**Headline: NOT READY for live capital.**

| Gate | Status |
|---|---|
| Safety & measurement | **GREEN** — 3,112 tests, verified stops, single-writer, fail-closed gates, human-only kill switch |
| Geometry | **GREEN** — since slice 9 |
| Edge / Skill — analyser | instrument **VALIDATED** (s23) — **edge ABSENT on all three timeframes**: M1/M2 = 76.1/77.5 (1D), 73.0/74.0 (4H), 72.2/76.0 (1H) against a bar of 95.0. **Signal layer CLOSED** (s25) |
| Edge / Skill — `donchian_breakout_v1` | **ABSENT** (s28) — M1/M2 = **91.2/91.5** on 1D against 95.0. Δ over blind schedules +0.2690, M3 3/4 folds (soft). **Parameterisation closed** |
| Model promotion | **BLOCKED** — `models/current` does not exist, by design |

---

## 1b. Signal 2 — `donchian_breakout_v1` (slice 28)

Design pre-declared in `EDGE.md` §9b and committed (`d707262`) **before** the
code was written. Result in §9c. One run, one verdict, no retune.

```
signals/donchian_breakout_v1.py     pure flags, no R arithmetic
tools/edge_measurement.py --signal donchian_breakout_v1   (default: closed_analyser)
tests/test_donchian_breakout_v1.py  24 tests
```

Rule: `close[i] > max(high[i-55:i])` **and** `close[i-1] <= max(high[i-56:i-1])`
— the second clause makes it a breakout *event*, so a ten-bar trend above the
channel produces one flag, not ten. Long-only, spot. Barrier identical to the
validated instrument (2.0/4.0 ATR, horizon 24, ATR 14, 25 bps, lock-up 1), so
the only difference from slice 24's measurement is *which bars were chosen*.

```
                       closed analyser    donchian_breakout_v1
trades                              47                      77
strategy mean net R           +0.2838                 +0.4274
blind same-shape schedules    +0.1443                 +0.1585
Delta over blind              +0.1396                 +0.2690
M1 / M2                     76.1/77.5               91.2/91.5
M3 folds positive                2 of 4                  3 of 4
```

**The honest reading: better, and still not enough.** It is the first change in
this project that has moved M1/M2 at all — four slices of geometry matching moved
nothing and a 43× sample-size change moved nothing. It is also a clear FAIL: at
M1 = 91.2 about one rotation replicate in eleven beats the strategy outright, and
schedules that cannot see a single price still earn +0.1585 R per trade.

---

## 1c. Execution stack — paper-ready (slice 29)

Slice 29 made no skill claim and implemented no signal. It closed the research
formally ([RESEARCH_CLOSE_STAGE1.md](RESEARCH_CLOSE_STAGE1.md)) and proved the
machine can be **operated** safely by a strategy that cannot claim edge it does
not have.

**One config key was added.** `ENTRIES_ENABLED` (default `1`) wires the
already-existing `build_bot(attach_strategy=False)` path to the environment, so
an operator running `python3 main.py` can stand the machine down: the loop runs,
state reconciles, existing positions keep their verified stops, health serves,
and no new entry is proposed. `parse_bool` maps every unrecognised value to
`False`, so a typo costs entries, never safety.

> **`ENTRIES_ENABLED` is an operator control, NOT a safety gate.** The gates are
> the kill switch, the live-arming chain and the risk gate set. It does not
> weaken them and is not a substitute for any of them.

Before slice 29 there was **no** `entries_enabled` / research-freeze flag at all
— an exhaustive search for eleven plausible names returned nothing. The kill
switch is not a substitute (it refuses `startup()` entirely, taking reconciliation
and stop management with it) and neither is `PAPER_TRADING` (it suppresses order
submission but still runs the whole decision path).

`tests/test_paper_readiness.py` — **84 tests**, all passing:

```
kill switch      engaged -> startup refuses, gate blocks KILL_SWITCH_ENGAGED,
                 no order reaches the stub, health 503, survives restart;
                 unreadable -> KILL_SWITCH_UNREADABLE, still blocked
human-only clear the exact literal "HUMAN_CLEARED_KILL_SWITCH"; 7 near-misses
                 raise; token absent from every Config field; no trading module
                 CALLS the clear method (AST, so docstrings survive)
live arming      defaults non-live; missing ack or credentials -> degrades to
                 paper; 4 near-miss acks do not arm
stops            rejected and unverifiable stops each close the position and
                 trip the kill switch
sizing           memory returning 1.0 / 1.5 / 10.0 / inf / nan never enlarges;
                 notional cap binds; no risk limit moved (asserted)
model            no artefact, POLICY_MODE=off, bot starts healthy; the model
                 writes exactly {"win_probability"} and constructs no order
paper run        10 ticks -> zero orders, kill switch clear, healthy
```

Operator procedure: **[docs/PAPER_RUNBOOK.md](docs/PAPER_RUNBOOK.md)**.

**None of this is evidence of edge, and no paper-run PnL is a skill claim.**

---

## 1d. Paper ops maturity (slice 30)

No signal, no thesis, no model. The NEW SIGNAL block was empty by human decision
and slice 30 did not invent one.

**The measured claim**, reproducible offline in seconds with no credentials and
no network:

```bash
python3 tools/paper_session_demo.py                     # stand-down
python3 tools/paper_session_demo.py --entries-enabled   # entries on, still paper
```

```
ENTRIES_ENABLED=0  ->  10 ticks, 0 decisions journalled, 0 orders, healthy, reconciled
ENTRIES_ENABLED=1  ->  10 ticks, 30 decisions (ALLOW + PAPER), 0 orders, not live
```

**Run both.** The contrast is the evidence: with entries on the gates and the
sizer ran every tick and `PAPER_TRADING` short-circuited before the transport;
with entries off nothing was considered at all. On its own, "zero orders" is
indistinguishable from a broken harness — the demo now fails if entries are
enabled and no decision is journalled.

**The session log** (`session_log.py`) writes one `SESSION_START` and one
`SESSION_END` per session, to the `decisions` journal under symbol `SESSION` and
optionally to `PAPER_SESSION_LOG_PATH` as JSONL. Fifteen fields; **no PnL**; no
credentials; and a mandatory constant, `NO EDGE CLAIM — timing-skill research
CLOSED`. `orders_submitted` is counted in `BybitClient._request` when the
endpoint is `/v5/order/create` — the transport every call passes through — rather
than inferred from the journal, because the only honest answer to "did this
session submit an order?" is what crossed the wire. It counts an order whose
response was lost, because that order left.

42 tests in `tests/test_paper_session_log.py`. Two defects were found by *running*
the demo rather than by testing it: `close()` was not idempotent, so every session
emitted two `SESSION_END` records; and the first entries-enabled demo printed
"0 orders" without showing anything had been considered. Both fixed, both now
covered.

**None of this is evidence of edge, and no paper-run PnL is a skill claim.**

---

## 1e. Project mode — the process states what it is (slice 31)

No signal, no thesis, no model. The NEW SIGNAL block was empty by human decision.

```bash
python3 tools/print_project_status.py
```

```json
{
  "timing_skill_research": "CLOSED",
  "cleared_edge_signal": null,
  "execution_mode": "paper",
  "policy_mode": "off",
  "entries_enabled": true,
  "live_authorized": false,
  "models_current_present": false,
  "no_edge_claim": "NO EDGE CLAIM — timing-skill research CLOSED"
}
```

The same snapshot appears in the startup log (`PROJECT MODE | ...`), in
`health()["project"]`, and in every stored `SESSION_START` / `SESSION_END` record.
`ProjectStatus` is a **frozen** dataclass: a snapshot a caller can mutate is one
that can be made to say something untrue between being read and being logged.

**The registration hook refuses.** `cleared_edge_signal_from_artifacts()` is the
only route to a non-null `cleared_edge_signal`. It returns a name only when an
`edge_measurement` summary says `EDGE_EVIDENCE_POSITIVE` **and** M1 >= 95.0
**and** M2 >= 95.0 **and** `m2.passed` — and never for a signal in
`ABSENT_SIGNALS`. Pointed at the real `artifacts/` directory it returns `None`,
and a test walks every summary in the repo to assert both that, and that no
artefact records a POSITIVE — so "always None" cannot silently mean "always
broken".

**Writing that test found a real backdoor.** The first implementation checked
only the verdict and the bars, so one hand-written JSON file dropped into
`artifacts/` claiming POSITIVE for `donchian_breakout_v1` would have made the
process announce a cleared edge for a signal that scored 91.2. The
`ABSENT_SIGNALS` refusal was added in response. If a future human genuinely
re-measures one of those under a new pre-declared design and it passes, they
remove it from that tuple deliberately — a visible act, not a side effect of a
file appearing on disk.

**This adds truthfulness, not capability.** It creates no autonomy and unblocks
nothing.

---

## 1f. Data intake contract (slice 32)

No signal, no thesis, no model. The NEW SIGNAL block was empty by human decision.

```bash
python3 tools/data_intake_report.py --json-out artifacts/slice32_data_intake_report.json
```

| path | interval | synthetic | bars | eligible |
|---|---|---|---:|---|
| `data/real_1d` | D | false | **3,135** | **yes** |
| `data/real_4h` | 4H | false | 15,379 | **yes** |
| `data/real` | 1H | false | 61,513 | **yes** |
| `data/ohlcv` | 1H | **true** | 5,000 | no — SYNTHETIC |
| `data/orderbook` / `data/quotes` / `data/anomalies` | — | true | — | no |

```
multi_asset_corpus_present        : false
non_btc_real_symbols              : []
non_btc_synthetic_symbols_present : ETH_USDT, SOL_USDT
```

**The trap this closes is already on disk.** `data/ohlcv/` holds
`BYBIT_SPOT_ETH_USDT_1H.csv.gz` and `BYBIT_SPOT_SOL_USDT_1H.csv.gz`: 5,000 bars
each, correct CoinAPI headers, plausible prices, **generated by
`tools/make_dataset.py`**. A future session looking for multi-asset data finds
them first. The report names them and says `SYNTHETIC` in the same breath.

Thresholds (`500` daily / `2000` intraday) were frozen in EDGE.md §13b before the
scanner existed, so they could not be chosen to make a corpus pass. `is_synthetic`
is checked **first and independently** — a test forces every other field to a
passing value and the corpus is still refused.

**Eligibility is NOT edge.** All three eligible corpora produced ABSENT readings.
Nothing in `data_contract.py` can reach `cleared_edge_signal`; `project_status.py`
does not import it; a test asserts both, and another asserts
`RESEARCH_CLOSE_STAGE1.md` is unweakened.

---

## 1g. Multi-asset data — attempted, NOT obtained (slice 33)

```
SLICE33_VERDICT: FAIL  (fail closed)
multi_asset_real_eligible : NO
```

Binance, Bybit and Bitstamp public REST were all tried, by direct HTTPS **and**
by the sanctioned `WebFetch`. TCP reaches every host; the sandbox egress proxy
answers `CONNECT` with **403 Forbidden** for anything outside the package-registry
allowlist, and `WebFetch` is independently refused by `robots.txt` (Binance,
Bitstamp) or 403 by the target (Bybit). Full detail: `EDGE.md` §14c.

**Nothing was written under `data/`.** No synthetic substitute, no lowered
threshold, no invented or forward-filled bar, no relabelling of the BTC corpora.
`multi_asset_corpus_present` stays `false` because that is true.

**What did ship:** `tools/fetch_binance_klines.py` and 41 offline tests. The
pager is injected, so every code path except the socket is exercised. It refuses
empty and all-NaN series, a single NaN bar in fifty, duplicate and non-increasing
timestamps, silent truncation below the frozen floor, malformed klines, and — 
structurally — forward-fill, interpolation, resampling, and importing the
synthetic generator.

To obtain the data, from a machine with egress:

```bash
python3 tools/fetch_binance_klines.py --symbols ETHUSDT SOLUSDT \
        --intervals 1d 1h --years 4 --out data/real_multi
python3 tools/data_intake_report.py     # decides eligibility
```

A test writes a real-shaped ETH corpus into a temp tree and asserts the flag
flips `false → true` with `non_btc_real_symbols == ["ETH_USDT"]`; a BTC-only
corpus leaves it false. **The flag is false because the data is absent, not
because the mechanism is dead.**

---

## 1h. Real multi-asset data — PRESENT, eligible, unmeasured (slice 34)

A human supplied what slice 33 could not fetch.

| corpus | symbols | interval | bars each | range (UTC) |
|---|---|---|---:|---|
| `data/real_multi_1d` | ETH_USDT, SOL_USDT | D | 1,461 | 2022-08-02 → 2026-08-01 |
| `data/real_multi_1h` | ETH_USDT, SOL_USDT | 1H | 35,063 | 2022-08-01 → 2026-08-01 |

```bash
python3 tools/data_intake_report.py     # exit 0
# multi_asset_corpus_present : true
# non_btc_real_symbols       : ETH_USDT, SOL_USDT
```

Discovered by `data_contract` with **zero special-casing** — the slice-32 design
held. Provenance was **verified, not trusted**: the series match known market
history at dates no generator reproduces (SOL $12.37 on the FTX collapse day,
$8.00 bottom 2022-12-29, $295.83 ATH 2025-01-19; ETH $2,111 wick 2024-08-05,
$4,956.78 high 2025-08-24), and both symbols share an identical exchange-halt
hour at 2023-03-24 12:00–14:00 UTC. Bars after ~2026-05 could not be
cross-checked against outside knowledge; that limit is recorded in `EDGE.md` §15a.

> **`ETH_USDT` now names a real corpus AND a synthetic one** (`data/ohlcv`).
> Select by **path**, never by symbol name. The report keys both lists by path
> (schema `data_intake/2`) precisely because registration created this ambiguity.

**Nothing was measured.** No Stage 1 run, no signal cleared, `cleared_edge_signal`
still `null`, M1/M2 still 95.0, both signals still ABSENT. More instruments is
not more edge.

---

## 1i. Signal 3 — `btc_alt_spillover_v1`, measured and INCONCLUSIVE (slice 35)

A BTC close-to-close shock, scaled by BTC's own Wilder ATR(14), triggering a
**two-sided** ETH/SOL trade at the alt's next open. Information set disjoint from
both closed families: the trigger never looks at the alt. Design frozen in
`EDGE.md` §16b (commit `38c73da`) before any scoring code was written.

```
                        ETHUSDT        SOLUSDT
setups / trades          93 / 72        93 / 72
strategy mean net R      +0.2351        +0.1757
M1                        97.3           85.1     vs 95.0
M2                        98.0           81.5     vs 95.0
directed control       median 52.2    median 50.4  -> INVALID
```

**`edge_verdict: INCONCLUSIVE`. Do not read ETH's 97.3 as anything.** Two rules,
both pre-declared before any number existed, independently forbid it:

1. the **directed control failed** — the two-sided, next-open construction was
   never validated by slice 23, §16b required re-validating it at n ≥ 200, and
   the median came out above 50 on both symbols;
2. the **anti-cherry-pick rule** — POSITIVE needs both symbols over both bars,
   and SOL did not clear.

Uniformity did not reject (z = +0.86, +0.05) and §11a shows `median ≤ 50` is a
coin flip for a perfect instrument. Both true — and neither licenses relaxing a
criterion after seeing it fail.

**Two defects were found in this slice's own machinery**, and both are worth
knowing about:

* the null's direction sequence was built from every *flagged* bar rather than
  the bars actually entered, so **37 of 72 ETH trades were scored in the wrong
  direction**. The first control run measured that corruption, not the
  instrument;
* the slice-31 registration hook **accepted the unforged ETH POSITIVE** and
  `ProjectStatus` announced a cleared edge. A summary now additionally requires
  `"control_validated": true`, written only when `--control-attestation` points
  at a log saying `CONTROL: **VALID**`. Nothing in this repository carries it.

The offending ETH artefact is kept on disk deliberately, and a test asserts the
hook refuses that exact file.

**What would make a re-test meaningful:** the BTC driver ends 2025-01-07 while
the alts run to 2026-08-01, so nineteen months of ETH/SOL history were never
reachable. Extending the BTC corpus is the cheapest way to get out-of-sample
bars. That, plus a directed control that actually validates, plus a multi-symbol
rule that does not reduce to "report the best one" — BNB and AVAX are now on
disk, which makes cherry-picking easier, not harder.

---

## 1j. Slice 36 — what is actually blocking the measurement

Two blockers were on slice 35's measurement. Slice 36 removed neither and
**settled which one is real**.

**The directed construction is sound.** At n = 1,000 surrogates:

```
median 50.40 | mean 49.53 | z = -0.51 | 0/1000 incomplete
buckets  10.0 / 15.1 / 24.7 / 27.8 / 13.7 / 8.7  vs  10 / 15 / 25 / 25 / 15 / 10
```

Uniform. The two-sided, next-open, one-trade-per-run null does what it should,
and five tests now pin the mechanics
(`TestTheNullRespectsTheConstruction`). The suspicion that it mishandled
one-trade-per-run is refuted.

**The `median ≤ 50` gate is the artefact.** It is symmetric about 50 for a
*perfect* instrument, so it fails ~50% of the time at **any** n — §11a proved it
analytically, this slice confirmed it empirically at n = 1,000, where the
distribution is uniform and the clause *still* fails by 0.25 standard errors.

**It was deliberately not changed.** The argument for replacing it predates the
failure by six slices; producing it inside the slice it would unblock is
shopping. §17e proposes a calibrated replacement — `|z| < 1.96` **and** KS vs
U(0,100) not rejected **and** incompletes ≤ 5% — for a **human to pre-declare
before the run it enables**. It is stricter where it matters (it can see a wrong
*shape*, which a median cannot) and it does not fail correct instruments by
construction.

**BTC still cannot be extended.** Egress blocked, 403 at the proxy. Exact gap:
**571 daily bars, 2025-01-08 → 2026-08-01**. Until they exist, 39% of the
available ETH/SOL history is unreachable and the measurement is confined to 890
dates. Nothing was invented or forward-filled; the driver was not swapped to
another venue (that would change the signal).

Two things must happen before this signal can be measured interpretably, and
**both are human decisions**: pre-declare a control criterion that is not a coin
flip, and supply the missing BTC bars.

---

## 1k. Slice 37 — control rule adopted and PASSED; data still missing

**The gate was replaced by a human pre-declaration**, which is why it was
allowed to change at all:

```
CONTROL VALID iff  |z| < 1.96  AND  KS vs U(0,100) p >= 0.05  AND  incompletes <= 5%
```

`median <= 50` is **retired as a gate for the directed path** and kept as an
informational log line. Implemented as a pure `evaluate_control()` with 24
per-clause tests — including one proving a median above 50 alone cannot
invalidate, and one proving a distribution with the right *mean* and the wrong
*shape* is caught by KS (a z test and the old median clause were both blind to
it).

```bash
python3 tools/control_directed.py --symbol ETHUSDT --control-runs 1000 --runs 1500
python3 tools/control_directed.py --symbol SOLUSDT --control-runs 1000 --runs 1500
```

| symbol | z | KS p | incompletes | median (info) | verdict |
|---|---:|---:|---:|---:|---|
| ETHUSDT | +0.372 | 0.6921 | 0.0% | 51.47 | **VALID** |
| SOLUSDT | +0.287 | 0.6744 | 0.0% | 50.40 | **VALID** |

Both medians are above 50. The retired rule would have failed both a third time.

**The measurement still did not happen.** The BTC driver still ends
**2025-01-07**; 571 daily bars (2025-01-08 → 2026-08-01) are missing, so the
intersection is still 890 dates and STEP 0's fail-closed mandate applied.

> On that truncated window, with the control now valid, the **existing**
> slice-35 artefacts are interpretable and read **ABSENT**: ETH clears both bars
> (97.3 / 98.0), SOL clears neither (85.1 / 81.5), and one-of-two is ABSENT
> under the dual-symbol rule. An observation about files on disk — not a
> measurement of the full history.

**The anti-cherry-pick rule is now code.** `DUAL_SYMBOL_REQUIREMENTS` maps
`btc_alt_spillover_v1 → (ETHUSDT, SOLUSDT)` and the hook refuses unless every
named symbol has its own qualifying artefact. `cleared_edge_signal` is guarded
three ways — bars, control attestation, declared universe — and is `null`.

**To proceed:** ship extended Bitstamp BTC_USD daily for 2025-01-08 → 2026-08-01.
Not Binance BTCUSDT — different instrument, needs a new intake. Then re-validate
the control on the new window (a control is a statement about a construction on a
series, not a certificate that travels) and run the measurement once.

---

## 1l. Slice 38 — the data arrived; the instrument failed

**The human shipped the 571 bars.** `data/real_1d` is **3,135 bars,
2018-01-01 → 2026-08-01**, and the intersection with ETH/SOL is the **full
1,461-date alt history**. Data is no longer the blocker.

**Provenance was verified, not assumed** (EDGE.md §19a). The 2,564-row prefix is
byte-identical; the 571 new bars keep the fat tails and volatility clustering a
generator does not produce (kurtosis 8.17, acf₁|r| 0.198); they correlate
0.848 / 0.815 with the *untouched* real ETH/SOL corpus and track a correlation
shift visible in files nobody edited; 2025-10-10 and the 126,272 high on
2025-10-06 match public history.

### Two silent-failure defects, found before anything was run

`market_data.load_ohlcv` returned **2,564 bars from a 3,135-row file** — the
extension writes `+00:00` where the old rows write `Z`, and leaves
`trades_count` blank where they write `0`. `tools/data_intake_report.py` said
`3135 bars — eligible: True` throughout, because it counted CSV rows and read
the timestamp policy off row 0. Nothing raised. The measurement would have run
on the **same 890 dates as slice 37** and been written up as the full history.

Fixed without touching a bar: `parse_timestamp` accepts `Z`, `+00:00`, `+0000`
(non-zero offsets and `-00:00` still rejected); a blank `trades_count` means
"not reported" while a blank price or volume stays fatal; `data_contract` now
asks the loader itself and refuses eligibility if **any** row is rejected.

### And then the control failed

| symbol | z | KS p | incompletes | verdict |
|---|---:|---:|---:|---|
| ETHUSDT | **+2.118** | 0.0510 | 0.0% | **INVALID** — clause (a) |
| SOLUSDT | **+2.165** | **0.0404** | 0.0% | **INVALID** — clauses (a) and (b) |

Same rule, same seed (20250730), same n (1,000), same frozen signal. On 890
dates the same construction gave z +0.372 / +0.287. The mean percentile bias is
about **2 points**, it is **common-mode** (+1.62 on ETH, +1.75 on SOL — two
unrelated price paths landing in the same place), and the larger sample
**resolved** it rather than caused it: the first 2,564 `Bar` objects are
identical across the old and new corpus files.

So **no real-series percentile was computed.** Not run, not looked at, not
estimated. Running the control first is what makes that easy rather than
virtuous.

Three passing controls were available and all three were refused: another seed
(both failures are marginal), n = 200 (the rule's minimum, where a 2-point bias
is invisible), and reporting ETH's two-of-three as a partial pass.

**To proceed:** a human must pre-declare a fix to the directed construction
before any scored run — the 36 → 37 handoff is the template. The candidates are
common-mode: BTC shock-date placement, the rotation null, direction-sequence
recycling, the percentile computation. Keep the seed, keep n, keep the rule.

---

## 1m. Slice 39 — the declared fix was applied, and changed nothing

The human pre-declared one change: **exclude UTC 2025-01-07 from the BTC driver
in both the real and the null path**, because that stub bar was thought to
manufacture a spurious shock on 2025-01-08.

It is implemented, permanently, at one chokepoint —
`EXCLUDED_BTC_UTC_DATES = ("2025-01-07",)` filtered inside `btc_setups()`, which
is the single function both paths reach the driver through. 12 tests, including
`array_equal` between the control builder's flag array and the real path's, and
a sweep proving exactly one date in 1,200 is dropped.

**It changed nothing, and EDGE.md §20b predicted that before the run.**

```
BTC bars      3,135 -> 3,134          shock dates   341 -> 341
setups added/removed/flipped: none    shared dates  1,461 -> 1,460
```

The stub's close (102,263) sits 0.017% from 2025-01-06's close (102,280), so
dropping it moves the 2025-01-08 return from −7.0387% to −7.0542% — still a
`SHORT_SETUP`, marginally larger. The spurious shock is caused by the **true**
2025-01-07 close (~96,900) being *absent* from the corpus, not by the stub being
*present*. Deleting a bar cannot restore information the file never had.

The control then reproduced slice 38 **byte-identically** — verdict blocks and
all 1,000 surrogate percentile lines per symbol, `diff` reports zero differing
lines. ETH z +2.118, SOL z +2.165. Both INVALID. Per pre-declaration 5 the slice
stopped there: no measurement, no artefact, no percentile.

**What the two slices jointly establish:** the ~2-point upward bias is not a
data defect on any date, it is common-mode across two unrelated series, and it
is therefore a property of the directed construction — the rotation null,
direction-sequence recycling, or the percentile computation. It was invisible at
890 dates because the window was too short to resolve it.

**It biases upward, which flatters the signal.** That is why no percentile from
this path is to be believed until it is fixed — including slice 35's 97.3/98.0.

Three changes would likely have flipped the control and all three were refused,
because each would have been invented *after* seeing the declared fix fail:
extending the exclusion to 2025-01-08, excluding the whole seam, or touching the
null.

---

## 1n. Slice 40 — the null was repaired, the measurement ran, and it is ABSENT

The human pre-declared three structural repairs and they are implemented and
tested (42 tests, `tests/test_directed_null_repair.py`):

**S1 — hard horizon embargo.** `tradable_flags(flags, eligible)` is now the one
array both sides build schedules from. The real path had been using raw `flags`
and the nulls `flags & eligible` — the same builder, one trade apart, before a
single R was read. 125 → 124 entries; the lost one is 2026-07-31, whose
five-bar horizon runs off the end of the series. It was never *scored*; it was
still *ranked*.

**S2 — direction sequence = scored entries only.** 124 == 124 == 124, against
153 flagged bars. Before, the nulls recycled 125 directions over 124 slots,
ending in a `SHORT_SETUP` no trade ever realised.

**S3 — same schedule builder, genuinely rotated phase.** Both null generators
call `simulate_schedule` on `tradable_flags`, and the direction offset is drawn
per replicate rather than being fixed at 0 for all 1,500.

**A second instance of the same defect, found by a test rather than by reading:
`np.roll` wraps.** Rotating the raw post-warm-up region put flags into the
embargoed tail, where they were dropped later by a lookup miss — costing the
replicate trades *and* shifting its direction phase, on the null side only,
because the observed schedule is never rotated. Both nulls now work in
eligible-index space.

### The control passed — barely

```
ETHUSDT  z = +1.899  KS p 0.0712  0% incomplete  VALID
SOLUSDT  z = +1.609  KS p 0.2213  0% incomplete  VALID
```

The repair removed **12%** of ETH's bias and **27%** of SOL's (51.96 → 51.73,
52.01 → 51.47). ETH clears a 1.96 bar by 0.061. **~1.5 points of upward bias
remain and are unexplained.** That was written into EDGE.md §21d before any M1
existed, along with the decision to report the slice as PASS_WITH_DEFECTS
whatever the edge verdict turned out to be.

### The measurement

```
ETHUSDT  M1 94.3  M2 92.0  (delta +0.1382, CI [+0.1255, +0.1513])  ABSENT
SOLUSDT  M1 91.3  M2 90.0  (delta +0.1088, CI [+0.0966, +0.1210])  ABSENT
```

Both bars missed on both symbols. `control_validated: true` on both artefacts.
`cleared_edge_signal` is `null` — the dual-symbol rule did not even have to
fire, because there was no single-symbol pass to refuse.

**Slice 35's ETH 97.3/98.0 does not survive.** It was 890 dates read with a
biased ruler; on the full window with the repaired instrument ETH is 94.3/92.0.

**ABSENT is the strong reading, not the weak one.** The residual bias pushes
percentiles *up*. A signal reading 94.3 and 91.3 on an instrument tilted in its
favour would read lower on an honest one. The deltas are positive with CIs
excluding zero — the setups are not worthless, they are just not rare enough
among their own rotations. And the M3 folds show the shape of a drift artefact:
ETH's fold 3 is −0.0778 (percentile 36.0), and SOL's whole reading leans on
fold 1 (+0.4243) with the other three at +0.006 to +0.012.

---

## 2. Geometry — GREEN, and the exact numbers it must reproduce

```
python3 tools/run_evaluation.py --data-dir data/real_1d --interval D \
        --min-confidence 0.12 --min-agreement 0.4

trades closed       : 163            hit rate (R)   : 55.2%  (90W / 73L)
total return        : +1.39%         break-even hit : 36.2%
expectancy net      : +0.5479R       95% CI [+0.321, +0.785]
cost drag           : 0.0343R per trade (20.2 bps fees / 595.9 bps stop)
folds profitable    : 3/4            probability of ruin : 0.0
book/exchange breaks: 0
```

Walk-forward, out of sample:

| Fold | Period | Trades | Return | Win rate | The asset itself |
|---:|---|---:|---:|---:|---:|
| 1 | 2018-01 → 2019-10 | 28 | **+1.82%** | 78.6% | **−37.8%** |
| 2 | 2019-10 → 2021-07 | 41 | +0.73% | 58.5% | +315.0% |
| 3 | 2021-07 → 2023-04 | 12 | −0.17% | 50.0% | −17.9% |
| 4 | 2023-04 → 2025-01 | 14 | +0.08% | 57.1% | +265.9% |

**Do not touch geometry, risk limits, or classical entry/exit.** The fix in
slice 9 was arithmetic, not tuning: the take-profit and stop *ratios* are
unchanged from slice 8; only the **timeframe** moved from hourly to daily. On
hourly bars a 25 bps round trip is ~60% of a 1-ATR risk unit and **0 of 84**
geometries have positive net expectancy; on daily bars it is ~3% and **72 of
84** do.

Three caveats that are already written down and must not be forgotten: the
short-side sweep is **0 of 84** positive (gross or net), which says most of this
is drift on an asset that rose 8×; the strategy returned +1.39% against +675%
buy-and-hold; and the **notional cap binds before the risk cap**, so realised
risk per trade is ~30× below budget. That last one is the largest lever on the
result and is explicitly a human's decision, not a tuning step.

---

## 3. Stage 1 — the problem that blocks everything

`tools/skill_test.py` asks one question: holding drift, exposure, costs and
trade count constant, did the strategy's **choice of entry bars** contribute
anything? It ranks the strategy's expectancy against a null distribution built
by re-placing its own entries.

The instrument is validated by a **control**: run the identical test on
surrogate series whose time structure has been destroyed (whole bars shuffled
and re-based, so the return distribution and total drift survive and every
relation between bars does not). On those series nothing can be timed, so a
sound instrument must report ~50. It does not.

**The control runs unconditionally. Exit 1 when it fails, exit 2 when no null
can be built. It has never been made optional and must not be.**

### Every null built so far

| # | slice | null design | geometry | clustering | counts | control median |
|---:|---:|---|:--:|:--:|:--:|---:|
| 1 | 10 | uniform bar sampling | no | no | no | 95.4 |
| 2 | 11 | circular schedule shift | no | yes | — | 84.5 |
| 3 | 12 | flag-sequence rotation | no | yes | 96 vs 89 | 74.1 |
| 4 | 13 | + fixed lock-up | no | yes | 54 vs 54 | **73.1** |
| 5 | 14 | matched strata | yes | no | 54 vs 67 | 96.8 |
| 6 | 15 | stratified rotation | required | yes | yes | **infeasible, 0%** (best TV 0.2523) |
| 7 | 16 | block resample | exact on starts | exact | exact | **infeasible, 0%** (best TV 0.2097) |
| 8 | 17 | + one trade per run | exact on starts | exact | exact | 87.8 |
| 9 | 17 | same, lock-up 1 | **exact, TV 0.0000** | **exact** | **exact** | **71.6** (mean 73.1, z = +2.65) |
| 10 | 21 | **flag rotation + one trade per run, lock-up 1** | no | yes | yes | 51.0 at n=12; **55.0** at n=24 (mean **50.0**, **z = +0.00**) |
| 11 | 22 | flag rotation + one trade per run, lock-up 24 | no | yes | yes | 44.2 at n=24; **47.8** at n=200 (z = −1.00) |
| — | 23 | **the same lock-up-1 config at pre-declared n = 200** | no | yes | yes | **48.0** — median ≤ 50, z = −1.18, KS p 0.435, 0/200 incomplete → **PASS** |

`--null` takes four values: `rotation` (default), `matched`,
`stratified_rotation`, `block_resample`. `--lockup` defaults to the horizon
(24); **lock-up 1 is the configuration slice 21 measured at 51.0** and it is not
the default — see §4p for the three measured reasons the default was not
switched. `tools/bisection.py` also takes `--null` now (default unchanged).

---

## 4. What has been RULED OUT — do not re-litigate any of this

**Geometry matching is not the fix (slices 14–17).** Slice 13's null matched the
barrier's (ATR/price, range-position) geometry *not at all* and scored 73.1.
Slice 17's matches it *perfectly* — every entry a run start, every start in its
own 5×5 quantile cell, entry-geometry TV exactly 0.0000, run lengths, gaps and
trade counts all exact — and scores 71.6. **Four slices of geometry conditioning
moved the control by nothing**, and two designs built on it (s14 at 96.8, s16
infeasible) were measurably *worse* than having no geometry constraint at all.

**The geometry cells are clustered in time**, because ATR/price is a regime
variable and regimes persist — temporal sd 266–482 bars against 683 for a
uniform spread. That is why matching geometry and preserving the temporal
arrangement pull against each other, and it is what made slices 15 and 16
infeasible rather than merely biased.

**Barrier risk-unit contamination is refuted, with the sign reversed (slice
18).** The hypothesis was that the analyser fires where backward ATR(14)
*overstates* the range that follows, making its nominal 2-ATR stop softer in
real terms than the null's. Measured on 12 structure-free surrogates:

```
pooled flagged   n =    572   median ratio 0.1685
pooled unflagged n = 10,148   median ratio 0.1856
gap −0.0171   Mann-Whitney p = 0.000000   11 of 12 surrogates negative
backward ATR/price : flagged 0.04723  unflagged 0.04931
forward range/price: flagged 0.28116  unflagged 0.26768
```

The analyser fires where backward ATR **understates** what follows, so its stop
is *tighter* in real terms — which would bias the control **downward**. The
within-cell ATR imbalance is only **0.4%** (median ratio 0.9958, 122 of 206
cells below 1, sign test p = 0.0098); the pooled 4.2% figure is mostly a
composition effect. 0.4% is ~0.0005 R against a null sd of 0.16 R and cannot
move a percentile from 50 to 73.

**The null's acceptance filter is not the mechanism (slice 20).** The
block-resample null searches for legal re-tilings and then keeps only those
whose entry-cell distribution is within the sampling-floor tolerance. Ranking
the observed side against the **unfiltered** pool of all legal re-tilings as
well as the accepted pool, on the same searches:

```
lock-up 1  : tolerance acceptance 100.0% BOTH sides, every surrogate
             dA = dB = +0.0 exactly; mean-R shift +0.00000 exactly
             A unfiltered z = +2.93   B unfiltered z = -0.93
lock-up 24 : tolerance acceptance A 84.8% (as low as 22.0%), B 95.8%
             dA median +0.0 mean -0.7;  dB median +0.0 mean -0.0
             A unfiltered z = +2.80   A accepted z = +2.73
```

At lock-up 1 the filter rejects nothing — entries are run starts, the placement
matches every run-start cell, so TV is 0.0000 for every legal re-tiling. At
lock-up 24 it rejects up to 78% and still moves the percentile by **−0.7 on
average**, i.e. very slightly *against* the hypothesis. Side A's elevation
survives its removal in both configurations.

**And slice 20 corrected slice 19's own observation.** The "acceptance rate
2.35% vs 9.35%" that motivated the hypothesis was almost entirely a **solve-rate**
difference (3.4% vs 8.2%), not a filter difference — the analyser's run starts
sit in unusual cells, so its constraint-satisfaction problem is simply harder.
Never quote `accepted / searches` again without splitting it into
`legal / searches` and `accepted / legal`.

**The confirmation at n = 24 does not resolve the gate (slice 22).** Both
rotation configurations have a mean of exactly 50 (z = +0.00, −0.07) and medians
of 55.0 and 44.2. At n = 24 the median's SE is 10.2 points. The declared gate
(Config A, lock-up 1) **fails** at 55.0. Config B's 44.2 was not promoted to a
pass — it was not the declared gate, and with two configurations run against a
threshold in the middle of a noisy distribution, the chance one lands below 50
by luck is close to three in four.

**The block-resample PLACEMENT SEARCH was the mechanism (slice 21).** Rotation
has no geometry constraint, therefore no placement search and no acceptance
filter — the least construction machinery of any null built. Run on the same 24
seeds as §4l, changing nothing but the null:

```
Side A  mean 70.9 (z = +3.54)  ->  mean 52.1 (z = +0.36)
Side B  mean 45.6 (z = -0.74)  ->  mean 44.7 (z = -0.91)
Wilcoxon on the contrast  0.0101 -> 0.4405      sign-flip  0.0133 -> 0.4578
```

Side A's elevation does not survive removal of the search; Side B barely moves,
which is what makes it interpretable. The mechanism is visible in slice 20's own
numbers: Side A's solve rate is 3.4% against Side B's 8.2%, because the
analyser's run starts sit in unusual, temporally clustered cells. **A search
that succeeds on 3.4% of attempts is not sampling the arrangement space
uniformly — it returns the arrangements that are easy to build, and for Side A
those are a narrow, systematically different subfamily.**

Treat every control median produced with `--null block_resample` as
contaminated by this.

**No scoring-side story can explain the residual at all**, because the scoring
is byte-for-byte identical on both sides of the slice-19 bisection. Any new
hypothesis must respect that.

---

## 5. What slice 17 FIXED, and the honest cost

`simulate_schedule` — the map from flags to entries — now takes **at most one
entry per contiguous flag run**, on the run's first bar, then advances past the
run and the fixed lock-up; a run whose start falls inside a previous lock-up
contributes nothing rather than being entered at an interior bar.

Before, of 54 observed entries only **26 were run starts**. Now 100% are. The
observed schedule goes from 54 entries to 34 (or 47 at `--lockup 1`). That is a
change to a **scoring convention applied equally to both sides** — not a
threshold, not a risk limit, not a tolerance, not a filter on which signals
count. `--lockup 1` collapses the rule to exactly one entry per run, which is
the only configuration where entry geometry equals start geometry exactly; it
is what the bisection uses, and it is not the shipped default because
consecutive trades' scoring windows then overlap.

---

## 6. Slice 19 — the result that points at slice 20

The pair `15.4 / 72.7` had anchored every slice since 13 but was measured under
the multi-entry map slice 17 replaced. Re-run on **24 independent surrogates**,
1,500 replicates per side, via `tools/bisection.py`:

- **Side A** — the real analyser's flags.
- **Side B** — `shape_matched_flags`: the *same* multiset of run lengths and the
  *same* multiset of gaps, interleaved at random. Its independence from the
  price path is **structural** — the whole signature is
  `(run_lengths, gaps, n_bars, warmup, rng)`; there is no channel through which
  bar content could reach the placement.

```
Side A (real analyser flags)   : median 79.7   mean 70.9   sd 28.0   z = +3.54
Side B (info-free, same shape) : median 42.9   mean 45.6   sd 27.2   z = −0.74
contrast A−B                   : median +35.2  mean +25.2   17 of 24 positive
sign test p = 0.0639 | Wilcoxon p = 0.0101 | sign-flip permutation p = 0.0133
```

**Decision: A — the asymmetry survives.** And it changed shape, which is the
finding:

```
                          slice 13 (old map)   slice 19 (current map)
real analyser flags              72.7                 70.9
info-free, same shape            15.4                 45.6
```

Side A's elevation reproduces almost exactly. **Side B's depression is gone** —
under the current map an information-free flag sequence scores where it should.
That is the first positive evidence that slice 17's change fixed something real
rather than moving a number. The residual is the analyser's own elevation, now
isolated with a correctly-behaving control beside it.

**An arbitration to carry forward, stated openly.** `tools/bisection.py`'s
*implemented* rule adds a paired sign test at p < 0.05 that the slice-19 brief's
stated criteria did not require; that clause returns **C** at both n = 12 and
n = 24. The sign test discards magnitude, and the magnitudes here are strongly
asymmetric. Two tests that use magnitude reject. **The tool's code was
deliberately not edited to adopt them** — changing a decision rule after seeing
data is the failure this project exists to avoid. If slice 20 wants a different
rule, it should say so before the run.

---

## 7. WHERE THIS STANDS — waiting on a human signal definition

Stage 1 is closed and the freeze is in place. **Slice 27 arrived with the NEW
SIGNAL block unfilled — every field a `[HUMAN: …]` placeholder — so it ran the
baseline, produced [NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md), and stopped.**

No signal was invented. No indicator was implemented. Nothing was scored, and
nothing was trained. That was the mission's own instruction for an empty block,
and it is also the right call on its own terms: a thesis produced by whoever
happens to be running the slice is precisely the kind of thing that survives a
backtest and dies with money on it.

```
Stage 1 (old analyser)   CLOSED        H25_REJECT, edge ABSENT on 1D/4H/1H
Skill instrument         VALIDATED     median 48.0 at pre-declared n=200
Geometry                 GREEN         +0.5479R -- and NOT permission to trade
New signal               NONE DEFINED  waiting on a human
Model / shadow / live    BLOCKED
```

### The only two legitimate next moves — both human

1. **Stop.** The verdict is documented and better-evidenced than most systems
   ever produce about themselves.
2. **Fill in the block in [NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md)** and
   re-issue the mission with it completed. The next slice would then be DESIGN +
   IMPLEMENT + TESTS only — the first scored edge run comes the slice after,
   under bars frozen in advance, unless the block explicitly says otherwise.

**What will not happen in the meantime:** the closed analyser will not be
retuned, extended with another indicator, or reopened; no model will be trained
on the closed entry process; nothing goes to shadow or live on geometry alone;
and M1/M2 stay at 95.0 unless a human moves them *before* a run and records why.

### Housekeeping still open — each needs its own pre-declaration

* `--lockup` still defaults to the horizon; the recommended invocation is
  documented rather than made default (EDGE.md §7b, H2), so historical command
  blocks keep meaning what they meant.

## 8. Repository map

**17 production modules**, ~17,100 lines, entry point `main.py`:

```
config → persistence → memory → market_data → features → policy
       → risk_management → position_sizing → bybit_connection
       → technical_analysis → ml_strategy → trading_engine → main
```

`tools/`

| file | what it does |
|---|---|
| `run_evaluation.py` | the walk-forward harness. Provenance first, verdict last, buy-and-hold benchmark |
| `sweep_geometry.py` | the 84-cell geometry surface, cross-checked against `features.triple_barrier_label` |
| `skill_test.py` | **the Stage 1 instrument.** Four nulls, the block resampler, the control |
| `residual_diagnostic.py` | slice 18. Backward ATR vs forward range on surrogates |
| `bisection.py` | slice 19. Side A vs Side B under the current map |
| `filter_diagnostic.py` | slice 20. Filtered vs unfiltered null pool, one search |
| `bisection.py --null` | slice 21. Any null kind, threaded to both sides |
| `parse_control_log.py` | slice 23. Control log -> CSV, recomputes the summary |
| `edge_measurement.py` | slice 24. M1 / M2 / M3 real-series edge, thin wrapper |
| `train_policy.py` | purged walk-forward + holdout. Exits 1 on refusal |
| `make_dataset.py` | synthetic CoinAPI-schema corpus generator |
| `fetch_real_data.py` | clones ff137/bitstamp-btcusd-minute-data (MIT), resamples, writes a manifest |

**Docs:** `RESEARCH_STATUS.md` (the freeze), `STAGE1_VERDICT.md` (the formal
close), `NEW_SIGNAL_INTAKE.md` (the blank intake), `EDGE.md` (§4a–8a, the full Stage 1 history — the authoritative
record), `GEOMETRY.md`, `ML_POLICY.md`, `TEST_REPORT.md`, `INTEGRATION_MAP.md`,
`DEPLOYMENT.md`, `MARKET_CATEGORIES.md`, `README.md`.

**Data:** `data/` synthetic (the only corpus with an order book),
`data/real` 61,513 hourly, `data/real_4h` 15,379, `data/real_1d` **3,135** — all
Bitstamp BTC/**USD**, 100% complete, 0 rejected rows, the 2020-03 crash and the
whole 2022 bear present. Not Bybit BTCUSDT, no order book, `trades_count`
written as 0 rather than invented (blank in the slice-38 extension, which the
loader reads as "not reported").

The daily corpus runs **2018-01 → 2026-08**; the two intraday corpora still
stop at 2025-01. The daily extension (571 bars, 2025-01-08 → 2026-08-01) was
shipped by the human in slice 38 and its provenance is verified in EDGE.md
19a — not merely accepted. Its one known defect is the **partial 2025-01-07
bar**, left in place rather than repaired.

### Key numbers of the instrument on `data/real_1d`

```
eligible bars 2,339 of 2,564      analyser flags 973 bars in 47 runs
gaps 48, lengths 1–157            run lengths 1–77
entries: 54 (old map) → 34 (lock-up 24) → 47 (lock-up 1)
strata: 5×5 quantile cells on (ATR(14)/close, close-within-14-bar-range),
        25 populated, 42–155 candidates per cell
tolerance: median TV of independent multinomial resamples of the same size
           from the observed distribution — a RULE, never a tuned number
placement search: ~4% solve rate, 6.2 solutions/sec at a 2,000-node budget
```

---

## 9. Defects found and fixed — do not reintroduce

| # | defect | fix |
|---:|---|---|
| 14 | cost gate used `p = confidence`, a score with no frequency meaning → zero trades in 7 years | separate `win_probability`; fallback judges the reward leg alone, which is stricter |
| 15 | `SimulatedExchange` hardcoded `quote_asset="USDT"`; corpus is BTC/**USD** → 15,668 false `INSUFFICIENT_USD` | derived from symbols; mixed-quote universe refused |
| 16 | `_apply_memory_throttle` passed a derived property to `dataclasses.replace` — would raise exactly when reducing risk | `qty=0.0` *is* "do not trade" |
| 17 | `_BacktestConfigView` was a hand-written mirror of the schema and went stale | built by the real `config.load()` |
| 18 | protective stop placed the wrong side of its own fill → guaranteed −1R | `_reanchor` preserves stop **distance**, not stop price |
| 19 | booked-out dust broke reconciliation 317 times, only above ~$55k | dust booked to a persisted ledger and reconciled → 0 breaks |
| 20 | `Backtester.run` read the ledger through a 200-row default | limit raised, cross-checked against ledger count |
| — | `load_corpus` only recognised `_1H` → "no usable OHLCV files" on 4h/1d | generalised to a closed set of interval suffixes |
| — | skill-test v1 gave replicates the holding *lengths* not the exit *rule* | both sides exit on the same barrier |
| — | `shape_matched_flags` shuffled a zero-length edge gap into an interior slot, **merging two runs** and corrupting the multiset | zeros pinned to leading/trailing slots; >2 zeros raises |
| — | slice 19 quoted `accepted / searches` as an acceptance rate; it is the product of solve rate and tolerance acceptance, and the difference between the sides was almost all solve rate | the two are reported separately |

---

## 10. The rules that govern slice 38

Sacred, for the lifetime of the project:

- A confirmed position always has a verified protective stop.
- Single-writer state. Fail-closed gates. **Human-only kill switch** — the bot
  may trip it, only a human clears it. Nothing may override risk limits or reset
  the kill switch autonomously.
- A model may only ever write `TradeIntent.win_probability`.
- Promotion criteria default to REFUSE and are never lowered.
- Every `*_PCT` is a fraction, costs are in bps, fail closed on missing data.
- The control runs unconditionally and exits non-zero when it fails.

Blocked until Edge/Skill is GREEN under a valid instrument:

- No improving classical entry/exit logic, thresholds, horizons or filters.
- No treating paper-mode PnL, a dry-run result, or geometry as timing skill.
- No running Stage 1 against a corpus the data contract marks ineligible, and
  no claiming multi-asset coverage from the synthetic ETH/SOL files on disk.
- No writing a `synthetic: false` MANIFEST over data that was not fetched from a
  real exchange. That single line is the difference between a corpus and a lie.
- No selecting a corpus by symbol name. `ETH_USDT` names a real corpus AND a
  synthetic one; only the path disambiguates.
- No treating the arrival of real ETH/SOL as evidence of anything.
- No interpreting btc_alt_spillover_v1's ETH 97.3/98.0. The control that
  produced it did not validate, and SOL read 85.1/81.5 under a pre-declared
  rule that makes one-of-two ABSENT.
- No registering a cleared edge from a summary without "control_validated":
  true. Checking the number is not enough; the ruler must have been checked.
- No replacing the control criterion inside the slice whose measurement it
  would unblock. s36 declined to, on purpose; the human pre-declared it in s37.
  The rule is now |z| < 1.96 AND KS vs U(0,100) p >= 0.05 AND incompletes <= 5%.
  No fallback to median <= 50. No fourth clause added after seeing numbers.
- No registering a claim for a multi-symbol signal from a single symbol's
  artefact. DUAL_SYMBOL_REQUIREMENTS enforces an all-of universe and
  MULTI_SYMBOL_MINIMUMS a k-of-n one. **Both mappings must be updated when a
  new intake declares a symbol universe** — slice 55 proved that a rule stated
  only in prose is enforced by nothing, and a test now fails if a stage-1
  record declares a multi-symbol requirement the code does not know about.
- No reading `slice55_edge_funding_carry_fade_BTCUSDT_summary.json` as a
  result. It is a real 97.0 / 97.5 under a valid control on one symbol of the
  two its own rule required, and it is on disk unedited precisely so that the
  refusal is checkable. Do not retune FUND_ABS. Do not re-run it. Do not
  register it.
- No setting `cleared_edge_signal` by any route other than a genuine
  EDGE_EVIDENCE_POSITIVE artefact with M1 and M2 both >= 95.0. No setter, no env
  var, no force argument, and never for a signal in ABSENT_SIGNALS.
- No adding a PnL, return, win-rate or equity field to the session log.
- No removing or editing the NO EDGE CLAIM line.
- No using a short control run (n=12/24) as a gate — see EDGE.md 11a.
- No relying on `ENTRIES_ENABLED` as a safety gate; it is an operator control.
- No loosening a risk limit "for readiness".
- **No N-grid search on `donchian_breakout_v1`**, and no sweep of its ATR
  multiples or horizon. It scored 91.2/91.5 against 95.0 and a near miss is not
  a licence to search. A variant is a NEW hypothesis with its own design commit.
- **No re-running `donchian_breakout_v1` on 4H/1H** — the brief permits other
  timeframes only after a POSITIVE on daily, under a new pre-declared design.
- No retraining, promoting or loading a model.
- No raising `MAX_POSITION_SIZE_PCT` or any risk limit — that is a human's
  explicit decision and out of scope.
- No multi-asset work until open multi-year data is actually in the repo.
- No Stage 2, no shadow trading, no live proposals.
- **Never interpret a real-series percentile while the control is failing.**
- Never loosen a tolerance, the sampling-floor rule, or the median ≤ 50
  criterion. Never treat the control median as a hyperparameter.
- Never trade one constraint for another; never silently fall back to a weaker
  null; never re-introduce multi-entry-per-run scoring.

Read `DEPLOYMENT.md` §0 before anything operational: **the API keys in this
repository's history must be treated as compromised.**
