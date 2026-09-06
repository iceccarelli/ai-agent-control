# RESEARCH CLOSE — STAGE 1

**Formal close-out of timing-skill research on the signals and data currently in
this repository. Written at slice 29, extended at slice 41.**

> **Slice 41 addendum — a third family joins the list.** `btc_alt_spillover_v1`
> was the cross-asset thesis this document's §5 said would be needed: a
> genuinely different information set, human-written, pre-declared before
> implementation. It was implemented (slice 35), its instrument was diagnosed
> and repaired over slices 36–40, and it was measured on the full 1,461-date
> history under controls that passed their own pre-declared three-clause rule.
>
> **ETH 94.3 / 92.0. SOL 91.3 / 90.0. Bar 95.0. EDGE_EVIDENCE_ABSENT.**
>
> The human froze it. It is now in `project_status.FROZEN_ABSENT` alongside the
> other two, and the registration hook refuses a POSITIVE artefact naming it
> even if that artefact is otherwise perfect. Everything this document forbids
> for the first two families is forbidden for the third: no retune of the shock
> multiplier, the ATR period, the stop, the take-profit, the horizon or the
> cost; no grid; no extra filters; **no "almost 95."** See EDGE.md §21e and §22.
>
> The honest summary of Stage 1 is now: *three families, three pre-declared
> measurements, three ABSENT, and a bar that never moved.*

This document exists so that the next person — or the next session — cannot
reopen a measured question by accident, by optimism, or by forgetting. It states
what was measured, what the numbers were, and what is forbidden as a consequence.
It does not soften anything.

---

## 1. Status

```
TIMING_SKILL_RESEARCH   CLOSED     on the current signals and the current data
Skill instrument        VALIDATED  rotation + lock-up 1, n=200, median 48.0
Geometry                GREEN      +0.5479R on BTC daily -- NOT a skill claim
Signal 1 technical_analysis   ABSENT   M1/M2 76.1/77.5 (1D), 73.0/74.0 (4H), 72.2/76.0 (1H)
Signal 2 donchian_breakout_v1 ABSENT   M1/M2 91.2/91.5 (1D)
Signal 3 btc_alt_spillover_v1 ABSENT   M1/M2 94.3/92.0 (ETH), 91.3/90.0 (SOL)
                                       FROZEN at slice 41; control_validated true
Model / shadow / live   BLOCKED    unchanged, and not conditional on any of the above
```

**There is no deployable timing edge under M1/M2 ≥ 95 on the available BTC data
for these two signal families.**

That sentence is the whole document. Everything below is the evidence for it and
the fence around it.

---

## 2. The instrument is not the problem

It took fourteen slices and eleven null designs to build a ruler that does not
lie, and the ruler was validated *before* it was used to close anything.

**Validation (slice 23, design committed before the run):** on 200 structure-free
surrogate series — whole bars shuffled and re-based, so the return distribution
and total drift survive but every inter-bar relation is destroyed — the
instrument reports median **48.0**, mean 47.6, uniformity **z = −1.18**, KS
against U(0,100) **p = 0.435**, and **0 of 200** incomplete. On a series where
nothing can be timed, it reports what a correct instrument must.

**Hygiene (slice 26):** the control's exit code now answers *"is the instrument
valid?"* — 0 valid, 1 invalid, 2 no null can be built — and never *"is there
skill?"*. Edge claims belong to `tools/edge_measurement.py`, which exits non-zero
when M1 or M2 misses 95.0.

**Reproduced again in slice 29** (EDGE.md §10a): median 48.0, exit 0. The
contaminated block-resample path still fails at median 82.5, exit 1, as it must.

The instrument has been kept honest in both directions. That matters here,
because a negative result from a broken ruler would be worthless, and this one
is not.

---

## 3. Signal 1 — `technical_analysis` (oscillator committee): ABSENT

RSI, MACD, Bollinger, Supertrend and ADX combined through a weighted confidence
score and a component-agreement vote. Measured in slices 24–25.

```
corpus   bars     trades   strategy R   blind R    M1     M2     Delta
1D       2,564        47    +0.2838    +0.1443   76.1   77.5   +0.1396
4H      15,379       350    +0.0294    -0.0065   73.0   74.0   +0.0358
1H      61,513     2,039    -0.1431    -0.1595   72.2   76.0   +0.0165
```

**H25_REJECT.** The pre-declared hypothesis was that the 1D result was a
small-sample artefact. Trade count rises **43×** across the three corpora and the
percentiles do not move toward 95 — they sit flat at 72–78. On 1H both sides are
negative. At every resolution exactly one fold carries the result, and it is the
same calendar window: the 2019–2021 bull run.

The decisive number: an entry schedule with the analyser's own trade count, run
lengths and gaps, **placed with no knowledge of the bars at all**, earns +0.1443
R per trade on BTC daily. The analyser earns +0.2838. **22–26% of those blind
schedules beat it outright**, at every timeframe.

---

## 4. Signal 2 — `donchian_breakout_v1`: ABSENT

A pure price-path channel breakout — `close[i] > max(high[i-55:i])` with an event
clause so a sustained trend produces one entry rather than many. Disjoint
information set from Signal 1: no oscillator, no band, no committee. Design
pre-declared in EDGE.md §9b and committed (`d707262`) before the code existed;
measured once in slice 28; never retuned.

```
77 trades from 77 flag runs
strategy mean net R  +0.4274
M1  null +0.1550 (1,500/1,500 replicates)   percentile 91.2   bar 95.0   FAIL
M2  Side N +0.1585 (200/200 schedules)      percentile 91.5   bar 95.0   FAIL
    Delta +0.2690   95% CI [+0.2418, +0.2962]
M3  3 of 4 folds positive -- soft gate, supporting only
```

### Why 91 is not a pass

This is the section that matters, because 91.2 is close enough to 95 to feel
owed, and that feeling is the exact mechanism by which research programmes
deceive themselves.

* **The bar was set before the number existed.** M1/M2 = 95.0 was frozen in
  §9b, in a commit that precedes the results commit in git history. A bar that
  moves after you see the number is not a bar; it is a description of the number.
* **91.2 means roughly one null replicate in eleven beats the strategy
  outright.** That is not a near-miss on a knife edge — it is a one-in-eleven
  chance of seeing this or better from a schedule that has no information.
* **Blind schedules still earn +0.1585 R per trade here** — 37% of the signal's
  +0.4274 — without being able to see a single price. Most of what looks like
  performance is exposure to an asset that appreciated +675% over the sample.
* **M3 cannot rescue it.** 3-of-4 folds is a soft gate, explicitly supporting
  only, and one of those folds has ten trades.

### What is nonetheless true, stated without inflation

Signal 2 roughly **doubles the drift-controlled contrast** over Signal 1 (Δ
+0.2690 vs +0.1396) and moves both percentiles about fifteen points. It is the
first change in this project that has moved M1/M2 at all — four slices of
geometry matching moved nothing, and a 43× change in sample size moved nothing.

That is a real and interesting difference between two theses measured by the same
ruler on the same bars. **It is not evidence of edge, and it is not a reason to
search.** A result that is directionally encouraging and quantitatively failing
is still failing.

---

## 5. What is forbidden as a consequence

Each of these is blocked by a measurement, not by taste.

* **Parameter search on Donchian N after 91.2 is forbidden data mining.** N = 55
  was fixed before the run and the run returned ABSENT. A grid over N on a
  2,564-bar corpus is a search over dozens of heavily correlated hypotheses that
  would be reported as one; it would clear 95 by construction and the number
  would mean nothing. The same applies to the ATR multiples, the horizon, the
  cost assumption, and the lock-up.
* **Re-running Signal 2 on 4H/1H is forbidden** in the absence of a POSITIVE on
  daily. Other timeframes were permitted only as a follow-on to a pass, under a
  new pre-declared design.
* **Lowering M1 or M2 below 95.0 is forbidden** without a human recording the
  new bar and the reasoning *before* a run.
* **Machine learning on either entry process is forbidden until a process clears
  the bars.** Not with more features, not with a different architecture, not
  "just to see". A policy fitted to entries that are statistically
  indistinguishable from random placement learns to forecast drift, and it would
  then be graded by the same instrument that has now returned ABSENT four times.
* **Reopening either signal for "one more tweak" is forbidden.** More slices on a
  measured-and-stable answer are not research.
* **Live or shadow capital on geometry alone is forbidden.** See §6.
* **Multi-asset claims are forbidden** until real multi-year non-BTC data is
  actually in the repository. See §7.

---

## 6. Geometry is GREEN and is NOT a skill claim

`+0.5479R` net expectancy with a 95% CI of [+0.321, +0.785] over 163 trades is a
real number, reproduced in every slice since 9 and again in slice 29. It says the
*payoff geometry* — stop distance, target distance, hit rate — is positive. It
says nothing about whether the entries were timed.

Recorded alongside it, and equally durable:

* **+1.39% over seven years against roughly +675% buy-and-hold** — about 0.2% of
  the move the strategy was long into.
* **The short-side sweep is 0 of 84 positive**, gross and net. A real
  inefficiency does not vanish when you change sign. An appreciating asset does.
* **Roughly half the headline expectancy is available to a schedule that cannot
  see prices** (§3).
* The notional cap binds before the risk cap, so realised risk per trade sits
  ~30× below budget. That is a human's decision about risk appetite, not a
  tuning result.

**Geometry GREEN is not permission to trade.**

---

## 7. The data gap

Every corpus in this repository is **Bitstamp BTC/USD from a single source file**,
resampled to 1D, 4H and 1H. The three "timeframes" are not three independent
tests; they are three views of one asset over one history.

This is a **data gap, not a result**. It is not evidence that an edge exists
elsewhere, and it is not a reason to hope. It does mean the close-out in §1 is
correctly scoped: *these two families, on this data*. (Slice 41: a third
family, on multi-asset data, has since joined them — see the addendum at the
top. The scoping sentence stood; the answer did not change.) Multi-asset or
multi-venue claims are blocked until real multi-year non-BTC files exist under
`data/` — at which point they would need a new Stage 1 design, pre-declared, not
a re-reading of these artefacts.

---

## 8. Conditions to reopen ANY skill claim

Unchanged from `STAGE1_VERDICT.md` §6, restated here so this document stands
alone. A candidate must have all five:

1. **A design committed to git before the run** — gate configuration, statistic,
   sample size, stopping rule. The design commit must precede the results commit.
2. **A validated control path** — rotation + lock-up 1 + one-trade-per-run, or a
   successor passing the same control at **n ≥ 200** on structure-free
   surrogates: median ≤ 50, uniformity not rejected, incomplete ≤ 5%.
3. **M1 and M2 at ≥ 95.0**, unless a human moves the bars *before* the run and
   records why. M2 is mandatory; M1 alone cannot carry a claim, because a
   long-only strategy on an appreciating asset ranks well against nulls that move
   entries without removing exposure.
4. **No threshold shopping after numbers.**
5. **No ML on a process that failed these bars.**

Anything that skips these is a backtest, not a claim.

**A materially new signal must also pass the intake in
[NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md)** — in particular the material-
difference test, which "same indicators, new weights" fails and which
`donchian_breakout_v1` passed. Passing intake buys a measurement, not a
presumption.

---

## 9. What this close-out does not close

Stage 1 research is closed. The **execution stack** is not, and slice 29 turns to
it: proving the machine can be operated in paper mode, safely, by strategies that
cannot claim edge they do not have. See EDGE.md §10c–§10d and
[docs/PAPER_RUNBOOK.md](docs/PAPER_RUNBOOK.md).

That work adds no trading logic, loosens no limit, and makes no skill claim. It
exists because the honest position after two ABSENT readings is *"we have a
validated ruler and a serious safety architecture, and no edge"* — and the second
half of that sentence is worth being able to demonstrate rather than assert.

---

**Headline, unchanged: NOT READY for live capital. There is no measured timing
edge to deploy.**

*Records: `EDGE.md` §4a–§10e, `STAGE1_VERDICT.md`, `GEOMETRY.md`,
`artifacts/slice23_*`, `artifacts/slice24_*`, `artifacts/slice25_*`,
`artifacts/slice28_*`.*
