# STAGE 1 — FORMAL VERDICT

**Signal configuration under judgement:** the classical `technical_analysis`
analyser at its shipped thresholds (`MIN_CONFIDENCE = 0.12`,
`MIN_COMPONENT_AGREEMENT = 0.40`), scored on Bitstamp BTC/USD daily bars with
the shipped barrier (+4 / −2 ATR, 24-bar horizon, 25 bps round trip).

**Verdict: timing skill for this configuration is NOT demonstrated.**
**Stage 1 is CLOSED. This signal layer is CLOSED for timing-skill research.**

Stage 1 ran from slice 10 to slice 25. It built an instrument capable of
answering one question honestly, validated that instrument against a
pre-declared standard, asked the question, and got a negative answer. All three
of those are results. The third is the one that governs what happens next.

---

## 1. Geometry — GREEN, and what it is worth

```
python3 tools/run_evaluation.py --data-dir data/real_1d --interval D \
        --min-confidence 0.12 --min-agreement 0.4

trades closed       : 163            hit rate (R)   : 55.2%  (90W / 73L)
total return        : +1.39%         break-even hit : 36.2%
expectancy net      : +0.5479R       95% CI [+0.321, +0.785]
cost drag           : 0.0343R per trade
folds profitable    : 3/4            probability of ruin : 0.0
book/exchange breaks: 0
```

The expectancy is real, statistically distinguishable from zero, and it is
**not evidence of timing skill.** The context recorded alongside it since slice
9 says why:

* **+1.39% over seven years against +675% buy-and-hold** — the strategy
  captured 0.2% of the move it was long into.
* **The short-side sweep is 0 of 84 positive, gross or net.** A genuine
  inefficiency does not vanish when you change sign; an appreciating asset does
  exactly that.
* The notional cap binds before the risk cap, so realised risk per trade is
  ~30× below budget. That is the largest lever on the headline and it is a
  human's decision about risk appetite, not a tuning step.

A positive-expectancy geometry means the arithmetic is no longer against you.
It never meant there was an edge.

## 2. The instrument — VALIDATED under a pre-declared standard (slice 23)

The skill test ranks the strategy's mean net R against a null built by
re-placing its own entries. It is validated by a **control**: the identical test
on surrogate series whose time structure has been destroyed, where nothing can
be timed. A sound instrument must report a uniform distribution of percentiles
there.

Eleven null designs were built and measured before one did (EDGE.md §4a–4u).
The last artefact — the block-resample **placement search**, which returned the
arrangements that were *easy to build* rather than a uniform sample — was found
in slice 21. The confirmation, at a sample size and gate committed to git
*before* the run:

```
rotation, lock-up 1, one trade per run, n = 200 surrogates
  median 48.0        required <= 50              PASS
  uniformity z       -1.18, does not reject      PASS
  incomplete         0 of 200, required <= 10    PASS
  KS vs U(0,100)     D = 0.061, p = 0.435        (descriptive, does not reject)
```

**The ruler does not lie.** That is what fourteen slices bought.

## 3. The edge measurement — ABSENT (slice 24)

Design committed before the run. Bars fixed in advance at 95.0 and never moved.

```
real-series schedule : 47 trades, mean net R +0.2838

M1  rank vs 1,500 rotation replicates   percentile 76.1   bar 95.0   FAIL
    null mean +0.1552 (sd 0.1831); 358 of 1,500 (23.9%) beat the analyser

M2  vs 200 shape-matched blind schedules percentile 77.5  bar 95.0   FAIL
    null mean +0.1443 (sd 0.1732); Delta +0.1396, 95% CI [+0.1158, +0.1632]
    45 of 200 (22.5%) blind schedules beat the analyser outright

M3  fold 1 -0.1614 | fold 2 +1.2690 | fold 3 -0.3281 (n/a, 8 trades) | fold 4 +0.1003
    2 of 4 folds positive, soft gate 3 of 4                          not met
```

**The number that decides it:**

```
a blind schedule with the analyser's own trade count, run lengths and gaps,
placed with NO knowledge of the bars   earns  +0.1443 R per trade
the analyser                           earns  +0.2838 R per trade
```

Roughly half the geometry gate's headline expectancy is available to a schedule
that cannot see prices, and **22.5% of such schedules beat the analyser
outright**. That is not a near miss.

Two of M2's three sub-conditions pass — Δ > 0, and its CI excludes zero — and
the reason the third does not is the reason the bar required all three: the CI
is about the **mean** of the blind distribution (standard error 0.0122), while
the percentile is about its **spread** (sd 0.1732). Beating the average blind
schedule is a weak claim. Being unusual among them is the question, and 0.81
standard deviations is not unusual enough.

## 4. What this verdict forbids

**Timing skill for this analyser + these thresholds + this BTC daily
configuration is NOT demonstrated under the validated instrument.**

* **Model training on this entry process is forbidden** until a configuration
  clears the same bars. A policy fitted to entries that are indistinguishable
  from random would be learning to forecast drift, and it would be graded by
  the instrument that returned this reading. `models/current` stays empty.
* **Live or shadow capital is forbidden on geometry alone.** A positive
  expectancy that a blind schedule half-reproduces is not a reason to risk
  money.
* **No threshold, stop, size, horizon, confidence or agreement parameter may be
  retuned to change a skill reading.** Not before a run, not after seeing one.
* **No claim that the system is an autonomous profit-seeking agent**, or is
  close to being one.

## 5. Sacred invariants — unchanged, in force for the project's lifetime

* A confirmed position always has a verified protective stop.
* Single-writer state. Fail-closed gates. **Human-only kill switch** — the bot
  may trip it; only a human clears it. Nothing may override a risk limit or
  reset the kill switch autonomously.
* A model may only ever write `TradeIntent.win_probability`. It may never
  resize, override a risk limit, clear the kill switch, or place an order in
  shadow.
* Promotion criteria default to REFUSE and are never lowered.
* Every `*_PCT` is a fraction. Costs are in bps. Fail closed on missing data.
* The skill-test control runs **unconditionally** and exits non-zero when it
  fails.
* The API keys in this repository's history must be treated as compromised —
  see `DEPLOYMENT.md` §0.

## 6. What ANY future signal layer must do to reopen a skill claim

This is the standard, and it applies to every candidate that follows:

1. **A design committed before the run.** Gate configuration, statistic, sample
   size and stopping rule, written down and version-controlled *before* a
   number exists. The git history must show the design commit preceding the
   results commit. This project has done that for every gated measurement since
   slice 22 and it is not optional.
2. **A validated control path.** Rotation + lock-up 1 + one-trade-per-run, or a
   successor that passes the same control at **n ≥ 200** on structure-free
   surrogates: median ≤ 50, uniformity not rejected, incomplete ≤ 5%.
3. **M1 and M2 bars at ≥ 95.0**, unless a human changes the bars **before** the
   run and records the reasoning. M2 — the drift-controlled contrast against
   shape-matched information-free schedules — is mandatory, not optional. M1
   alone cannot carry a claim, because a long-only strategy on an appreciating
   asset can rank well against a null that moves entries without removing
   exposure.
4. **No threshold shopping after numbers.** No configuration may be promoted
   because it happened to land on the right side of a line when another did
   not — the specific error declined in slices 22 and 24.
5. **No machine learning on a process that failed these bars.** Ever. Not with
   more features, not with a different architecture, not "just to see".

Anything that cannot meet all five is not a skill claim. It is a backtest.

---

**Headline, unchanged: NOT READY for live capital. There is no measured timing
edge to deploy.**

*Stage 1 CLOSED at slice 25; instrument hygiene and research freeze at slice 26. Records: `EDGE.md` §4a–6d, `GEOMETRY.md`,
`artifacts/slice23_*` and `artifacts/slice24_*`, including every one of the
1,500 rotation and 200 shape-matched replicates behind the verdict above.*
