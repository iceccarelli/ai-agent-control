# Bybit V5 Trading Bot

A rebuilt-from-audit trading bot for Bybit V5, spot and USDT-perpetual, with a
learned policy behind every risk gate. **Not ready for live capital** — see
[TEST_REPORT.md](TEST_REPORT.md) for the measured reason.

```
config → persistence → memory → market_data → features → policy
       → risk_management → position_sizing → bybit_connection
       → technical_analysis → ml_strategy → trading_engine → main
```

16 modules, ~17,100 lines, **3,112 tests passing** — all offline, no network.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # safe defaults: testnet + paper + spot + POLICY_MODE=off
python3 -m pytest tests/ -q   # 3112 passed, 1 skipped
python3 main.py               # paper mode, takes no real orders
```

Evaluate on **real** data — 61,513 hourly bars (2018-01 → 2025-01) and 3,135
daily bars (2018-01 → 2026-08), covering the 2020-03 crash, the whole 2022 bear
and the 2025-10 liquidation cascade:

```bash
python3 tools/fetch_real_data.py --interval 1d --out data/real_1d
python3 tools/run_evaluation.py --data-dir data/real_1d --interval D
python3 tools/sweep_geometry.py --data-dir data/real_1d   # the geometry surface
```

Train a policy (it will very likely refuse to promote — that is the point):

```bash
python3 tools/train_policy.py --data-dir data/real
```

---

## The invariants

Enforced in code and asserted by tests, not left to discipline.

| Invariant | Where | Test |
|---|---|---|
| A confirmed position always has a **verified** stop | `client.verify_stop`, dispatched by category | `TestNeverNaked`, `TestStopVerificationFailsClosed` |
| Bracket failure → market close + kill switch | `_emergency_close` | `test_a_rejected_stop_closes_the_position_and_trips_the_kill_switch` |
| Every gate **fails closed** | `risk_management` — 0 `except: return True`, AST-asserted | `TestFailClosed` |
| Orders are **idempotent across restarts** | persisted counter + intent hash | `test_sequence_survives_a_restart` |
| One writer, many readers — **enforced** | `StateStore.claim_writer` | `TestSingleWriter` |
| The trade ledger explains the **entire** equity change | reconciled every bar in backtest | `test_ledger_explains_the_entire_equity_change` |
| Only a **human** clears the kill switch | `clear_kill_switch_by_human` | `test_nothing_in_the_engine_can_clear_the_kill_switch` |
| `config.load()` is the **only** environment reader | — | `test_only_config_reads_the_environment` |
| Every `*_PCT` is a **fraction**; >1.0 is rejected | `parse_pct` | `TestFractionConvention` |
| Spot **cannot short**, and says so by name | `_gate_short_capability` | `TestTheGateKnowsAboutShorts` |
| Recorded experience can only **shrink** a position | `memory.size_multiplier`, clamped at source | `TestMemoryOnlyEverShrinks` |
| **A model may only propose** — one field, no geometry | `ml_strategy._attach_probability` | `test_the_only_field_the_model_writes_is_win_probability` |
| In shadow mode the intent is returned **unchanged** | `PolicyStrategy.signal_for` | `test_in_shadow_the_intent_is_returned_byte_for_byte_unchanged` |
| Features cannot see the future — **structurally** | `compute_features` slices before computing | `TestNoLookahead` |
| A model trained on noise is **refused** | `policy.meets_promotion_criteria` | `test_a_noise_model_is_refused` |

---

## Modules

| Module | Lines | Responsibility |
|---|---:|---|
| `config.py` | 801 | The only environment reader. Fractions, bps, the category fork, the live gate, the policy gate. |
| `persistence.py` | 1,332 | SQLite state that survives `kill -9`. Single-writer enforcement. |
| `memory.py` | 989 | The shared reference layer. Read by every module; can only tighten. |
| `market_data.py` | 1,871 | Loading and validating OHLCV + L2. Book reconstruction, no-lookahead replay. |
| `features.py` | 1,573 | 35 scale-free features and triple-barrier labels. One pipeline, offline and online. |
| `policy.py` | 2,267 | The trained artefact. Calibrated probabilities, purged walk-forward CV, promotion criteria that default to refusal. |
| `ml_strategy.py` | 623 | The seam. Off / shadow / live, drift demotion, and the containment. |
| `risk_management.py` | 1,453 | The fail-closed gate chain — 22 gates, evaluated in order. |
| `position_sizing.py` | 741 | One equity-space sizing path. Kelly from persisted fee-inclusive returns. |
| `bybit_connection.py` | 1,495 | One V5 client, two dialects. Signing, real filters, category-correct stops. |
| `pure_indicators.py` | 406 | Wilder-correct indicator math. No I/O, no state, no config. |
| `technical_analysis.py` | 627 | Signals. Confidence ∈ [0,1], HOLD first-class, abstentions count against. |
| `trading_engine.py` | 1,127 | One order lifecycle, end to end. |
| `performance_analytics.py` | 293 | Fee-aware metrics. `None` rather than a confident-looking number. |
| `main.py` | 473 | One orchestrator. Startup reconciliation, health server, graceful shutdown. |
| `backtest.py` | 1,076 | Replays the **live code path** through a simulated transport. |

---

## Real data

`tools/fetch_real_data.py` builds a corpus from
[ff137/bitstamp-btcusd-minute-data](https://github.com/ff137/bitstamp-btcusd-minute-data)
(MIT): Bitstamp BTC/USD 1-minute bars since 2012, resampled to hourly.

```
61,513 bars, 2018-01-01 -> 2025-01-07, 100.0000% complete, 0 rejected
2020-03 covid crash : PRESENT
2022 bear (full year): PRESENT
```

Three caveats, stated because they change what the results mean: it is
**Bitstamp BTC/USD, not Bybit BTCUSDT**; it has **no order book**, so the
liquidity gate abstains and 3 of 35 features are unavailable; and
`trades_count` is written as 0 rather than invented.

The **daily** corpus was extended by a human in slice 38 and is now **3,135
bars, 2018-01-01 → 2026-08-01**; the hourly and 4-hourly corpora still stop at
2025-01-07. The extension's provenance is verified in `EDGE.md` §19a rather
than taken on trust, and its one known defect — a **partial 2025-01-07 bar** —
is recorded there and left unrepaired.

The synthetic corpus in `data/` is retained — it is what the plumbing tests run
against, and it is the only one with an order book.

---

## The learned policy

A model may **propose**. It may not decide, resize, override, or resume.

Its entire contribution is one field, `TradeIntent.win_probability`, which the
cost gate consumes. Every risk gate still runs, unchanged and in the same order.
`memory.size_multiplier` still applies afterwards and still only shrinks.
Nothing in the model path can clear the kill switch.

Three modes: `off` (default, no model loaded), `shadow` (evaluated and
journaled, **provably cannot change an order**), and `live` (needs an exact
acknowledgement token on top of a model that already cleared its promotion
criteria). Promotion is manual; demotion on drift is automatic.

**On seven years of real data the trained model was REFUSED** — mean
out-of-sample AUC 0.5306 against a 0.55 floor, and two folds worse than always
predicting the base rate. `models/current` is empty by design.

Read **[ML_POLICY.md](ML_POLICY.md)** before touching any of it.

---

## Cost awareness — the gate that matters most

```
edge_bps = p·win_bps − (1−p)·loss_bps − (2·taker_fee + slippage + funding)
```

Slice 8's correctness fix lives here. `p` used to be `confidence` — a
signal-strength score with no frequency interpretation. It is now
`win_probability`, filled in only by something calibrated; with nothing
calibrated available the gate falls back to judging the **reward leg alone**
against cost, which is stricter. A broken model makes this system more cautious,
never less.

`MIN_EDGE_BPS` defaults to the round-trip cost + 5 bps and cannot be configured
below cost — that is rejected at load time.

---

## Documentation

- **[RESEARCH_STATUS.md](RESEARCH_STATUS.md)** — **read this first.** Research
  is frozen: what is forbidden, what would be required to reopen a skill claim,
  and the recommended control invocation.
- **[STAGE1_VERDICT.md](STAGE1_VERDICT.md)** — the formal Stage 1 close: what
  was proven, what was refuted, and the standard any future signal layer must
  meet to reopen a skill claim.
- **[HANDOFF.md](HANDOFF.md)** — **start here.** The resumption state: what is
  proven, what is red, what has been ruled out and why, and the next
  measurement. Written so a cold start loses nothing.
- **[TEST_REPORT.md](TEST_REPORT.md)** — every test, the chaos results, the
  measured numbers, and what is not done.
- **[EDGE.md](EDGE.md)** — the skill test, why its first `p = 0.0000` had to be
  thrown away, and what the control proved.
- **[GEOMETRY.md](GEOMETRY.md)** — why the trades lost, what fixed it, and why
  a green gate is not yet an edge.
- **[ML_POLICY.md](ML_POLICY.md)** — the model, its containment, the training
  results, and the finding that outranks the model.
- **[MARKET_CATEGORIES.md](MARKET_CATEGORIES.md)** — spot versus perpetual.
- **[INTEGRATION_MAP.md](INTEGRATION_MAP.md)** — call graph, config flow,
  concurrency model, data-flow contracts.
- **[DEPLOYMENT.md](DEPLOYMENT.md)** — key rotation, the gates from tests to
  live, and how a human clears the kill switch.
- **[data/real/README.md](data/real/README.md)** — real-data provenance.

---

## Status

**NOT READY for live capital** — but slice 9 changed which sentence follows.

The geometry gate is **green**. On daily bars, 163 trades over seven years:
expectancy **+0.548R after fees** with a 95% CI of [+0.321, +0.785], a 55.2%
hit rate against a 36.2% break-even, 3 of 4 out-of-sample folds profitable, and
**zero** book/exchange reconciliation breaks.

It was fixed by arithmetic, not by loosening anything. The take-profit and stop
ratios are **unchanged** from slice 8; only the timeframe moved. On hourly bars
a 25 bps round trip is ~60% of a 1-ATR risk unit and **0 of 84** geometries
have positive net expectancy; on daily bars it is ~3% and **72 of 84** do.

What is still not established is an **edge**:

- the same sweep on the **short** side is 0 of 84 positive, gross or net —
  an asymmetry that says most of this is drift on an asset that rose 8×;
- the strategy returned **+1.39%** against **+675%** buy-and-hold, capturing
  0.2% of the move it was long into;
- the notional cap binds before the risk cap, so realised risk per trade is
  ~30× below budget — the largest lever on the result, and a human's decision.

One number resists the drift story: fold 1 returned **+1.82% while the asset
fell 37.8%**. That is 28 trades, which is not enough to conclude anything, and
it is reported as an open question rather than a finding.

### Slice 10: the skill test exists, and it does not work yet

`tools/skill_test.py` holds drift constant and varies only which bars the
strategy entered on. On real data it read **percentile 100.0, p = 0.0000** —
exactly the result the brief asked for.

Then the control ran. On surrogate series with the time structure destroyed —
where timing is *impossible* — the same instrument reported a **median
percentile of 95.4**. So the reading on real data is an artefact of the
measurement, not a property of the market, and **Edge/Skill stays RED**.

The control now runs on every invocation and the tool exits non-zero when it
fails. Had it been optional, this README would be claiming a breakthrough.

### Slices 11-16: seven nulls, and what the last one found

Stage 1 is still the only work in progress: **make the instrument valid before
reading anything off it.** Seven null designs have now been built and measured,
and none is valid. The tool exits **2** when no null can be constructed at all
and **1** when a null exists and the control fails.

| null | geometry | clustering | counts | control median |
|---|:--:|:--:|:--:|---:|
| uniform bar sampling (s10) | no | no | no | 95.4 |
| circular schedule shift (s11) | no | yes | — | 84.5 |
| flag-sequence rotation (s12) | no | yes | 96 vs 89 | 74.1 |
| + fixed lock-up (s13) | no | yes | 54 vs 54 | **73.1** |
| matched strata (s14) | yes | no | 54 vs 67 | 96.8 |
| stratified rotation (s15) | required | yes | yes | infeasible — 0% |
| block resample (s16) | exact on starts | exact | exact | infeasible — 0% |
| + one trade per run (s17) | exact on starts | exact | exact | 87.8 |
| same, lock-up 1 (s17) | **exact, TV 0.0000** | **exact** | **exact** | **71.6** |

Slice 17 made entry bars and run-start bars the same object: a contiguous run of
flags now contributes **at most one** trade, on its first bar. That closed the
gap slice 16 measured — 100% of entries are run starts, and at `--lockup 1` the
entry geometry matches the null's to **TV = 0.0000**, exactly, with run lengths,
gaps and trade counts all preserved exactly too. The null is constructible again
for the first time since slice 14.

**And the control did not move.** Slice 13's null matched the barrier's geometry
*not at all* and scored 73.1; slice 17's matches it *perfectly* and scores 71.6
(mean 73.1, z = +2.65 against uniform). Four slices of geometry conditioning
bought nothing, and two of the designs built on it were measurably worse than
having no geometry constraint. **The residual bias is not the barrier's
geometry.** The real-series percentiles are not interpreted, because the control
still fails.

### Slice 18: the risk-unit contamination hypothesis is refuted

The next candidate was mechanical: the barrier's risk unit is ATR(14) ending at
the entry bar — the same trailing window the analyser reads — so if the analyser
fired where backward ATR *overstates* what follows, its stop would be softer in
real terms than the null's and the instrument would drift upward with no timing
in it. `tools/residual_diagnostic.py` tests exactly that, on 12 structure-free
surrogates, comparing entry bars against same-cell bars the analyser did not
flag.

**Refuted, with the sign reversed.** The analyser fires where backward ATR
*understates* the range that follows — ratio 0.1685 against 0.1856, 11 of 12
surrogates negative, p < 10⁻⁶ — so its real stop is *tighter* than the null's.
That would bias the control downward. The within-cell ATR imbalance is 0.4%,
about 0.0005 R against a null standard deviation of 0.16 R, and cannot move a
percentile from 50 to 73.

### Slice 19: the bisection re-run — the residual is in what the analyser selects

The `15.4 / 72.7` pair that has anchored every slice since 13 was measured under
the multi-entry-per-run map slice 17 replaced, and had never been re-checked.
`tools/bisection.py` re-runs it on **24 independent surrogates** under the
current instrument: Side A is the real analyser's flags, Side B is flags with
the *same* multiset of run lengths and gaps, interleaved at random, carrying no
information about the bars at all.

```
                          slice 13 (old map)   slice 19 (current map)
real analyser flags              72.7                 70.9   (z = +3.54)
info-free, same shape            15.4                 45.6   (z = -0.74)
```

**The asymmetry survives, and it changed shape.** Side A's elevation reproduces
almost exactly. Side B's depression is gone — under the current map an
information-free flag sequence scores where it should, which is the first
positive evidence that slice 17's one-trade-per-run rule fixed something real
rather than moving a number. What remains is the analyser's own elevation, now
isolated with a correctly-behaving control beside it.

### Slice 20: the null's own filter is not the mechanism either

The last mechanical hypothesis about the *instrument*: the block-resample null
searches for legal re-tilings and then keeps only those whose entry-cell
distribution is within the sampling-floor tolerance. If that filter selected on
a quantity correlated with R, it would depress the accepted pool's mean R and
lift the observed percentile with no timing in it — and Side A is filtered harder
than Side B. `tools/filter_diagnostic.py` ranks the observed side against the
**unfiltered** pool of all legal re-tilings and the accepted pool, from one
identical search.

```
lock-up 1  : tolerance acceptance 100.0% BOTH sides, every surrogate
             dA = dB = +0.0 exactly, mean-R shift +0.00000 exactly
lock-up 24 : acceptance A 84.8% (as low as 22.0%), B 95.8%
             dA median +0.0, mean -0.7   |   A z +2.80 unfiltered vs +2.73 accepted
```

**Refuted.** At lock-up 1 the filter rejects nothing — entries are run starts, so
every legal re-tiling matches the observed cell distribution exactly. At lock-up
24 it rejects up to 78% and still moves the percentile by −0.7 on average, very
slightly *against* the hypothesis. It also corrected slice 19's own observation:
that "acceptance rate 2.35% vs 9.35%" was almost entirely a **solve-rate**
difference (3.4% vs 8.2%), not a filter difference.

### Slice 21: the mechanism, found — and the control finally moves

Rotation is the null with almost no machinery: no geometry constraint, therefore
no placement search and no acceptance filter. It had never been run under the
current one-trade-per-run map. On the **same 24 seeds** as slice 19, changing
nothing but the null:

```
Side A (real analyser flags)   mean 70.9 (z = +3.54)  ->  mean 52.1 (z = +0.36)
Side B (info-free, same shape) mean 45.6 (z = -0.74)  ->  mean 44.7 (z = -0.91)
Wilcoxon on the contrast  0.0101 -> 0.4405     sign-flip  0.0133 -> 0.4578
```

**The block-resample placement search was manufacturing the elevation** — not
its geometry constraint (ruled out s14–17), not its filter (ruled out s20), but
the constraint search itself. Side A's solve rate is 3.4% against Side B's 8.2%;
a search succeeding on 3.4% of attempts returns the arrangements that are *easy
to build*, not a uniform sample of them.

The control under rotation + one trade per run:

```
median 51.0   mean 57.1   z = +0.85 over 12 surrogates
```

against 71–96 for every configuration before it. **The gate is still not met —
51.0 > 50, and the criterion does not move** — but the instrument has gone from
decisively biased to statistically indistinguishable from uniform. No
real-series percentile is interpreted.

### Slice 22: the confirmation, pre-declared — and it FAILS

The design was written and **committed to git before either run**, so the
history evidences that the sample size predates the numbers. Config A (rotation,
lock-up 1) was the declared gate; Config B (lock-up 24) filled the one cell of
the grid never measured under the current map.

```
                     median    mean      z      worst   primary gate (median <= 50)
Config A  lock-up 1    55.0    50.0   +0.00     98.8    FAIL  (the declared gate)
Config B  lock-up 24   44.2    49.6   -0.07     98.8    (not the gate)
```

**FAIL.** Config B clears the line and Config A does not — which is exactly why
the rule was fixed in advance. Reading a pass off B now would be selecting the
configuration that landed on the right side of a threshold.

What the numbers actually say: both *means* are indistinguishable from 50, and
at n = 24 the median's standard error is 10.2 points, so 55.0 and 44.2 sit 0.5 SE
either side of 50 — not distinguishable from 50, nor from each other. Both
configurations are *consistent with* an unbiased instrument; neither is
*established* as one. The gate cannot be resolved at this sample size, and slice
22 measured that fact rather than assuming it.

### Slice 23: the control PASSES a pre-declared, high-power confirmation

The design — gate configuration, statistic, sample size and incomplete-surrogate
rule — was **committed to git before either run** (`6e2c3c6`). The gate was
declared by a human on a stated principle, and it is the configuration that had
*failed* at n = 24 (55.0) while the other cleared (44.2).

```
gate: rotation, lock-up 1, n = 200
  median 48.0   mean 47.6   sd 29.6   worst 99.6   z = -1.18   0/200 incomplete
  required: median <= 50  |  uniformity does not reject  |  incomplete <= 10
```

**PASS**, on all three conditions. A Kolmogorov–Smirnov test against U(0,100) —
descriptive, not part of the rule — does not reject either (D = 0.061,
p = 0.435), and the quartiles sit at 20.6 / 48.0 / 74.5 against an expected
25 / 50 / 75. The `worst` of 99.6 is not a defect: it is the expected maximum of
200 uniform draws, which is exactly why that ceiling was excluded as a gate.

Between slice 22 and slice 23 **nothing changed but the sample size** — 55.0 at
n = 24, 48.0 at n = 200, as SE(median) falls from 10.2 to 3.5 points.

**What this establishes: the ruler no longer lies.** What it establishes about
edge: *nothing*. No real-series skill percentile has ever been interpreted, and
none was here. Model training, promotion, Stage 2, shadow mode and live capital
all remain blocked. Full per-surrogate records are in `artifacts/`.

### Slice 24: the edge measurement — **ABSENT**

With the ruler validated, the measurement it was built for. Design committed to
git before the run (`a76232f`); bars fixed in advance; nothing retuned after.

```
real-series schedule : 47 trades, mean net R +0.2838

M1  rank vs 1,500 rotation replicates      percentile 76.1   bar 95.0   FAIL
M2  vs 200 shape-matched blind schedules   percentile 77.5   bar 95.0   FAIL
    Delta +0.1396, 95% CI [+0.1158, +0.1632]
M3  2 of 4 folds mean net R > 0            soft gate 3 of 4         not met
```

**EDGE_EVIDENCE_ABSENT.** M2 is the one that matters: an information-free
schedule with the analyser's own trade count, run lengths and gaps — placed with
**no knowledge of the bars at all** — earns **+0.1443 R per trade** on this
corpus, against the analyser's +0.2838. **45 of 200 (22.5%) of those blind
schedules beat the analyser outright.** Roughly half the geometry gate's
headline expectancy is available to a schedule that cannot see prices.

Two of M2's three sub-conditions pass — Δ > 0 and its CI excludes zero — but
that CI is about the *mean* of the blind distribution, whose standard error is
0.0122 against a spread of 0.1732. Beating the average blind schedule is a weak
claim; being unusual among them is the question, and 0.81 sd is not unusual
enough. Requiring all three was the point of writing the bar that way first.

The fold table is the result in miniature: one window at percentile 100.0
carrying +1.27R on 13 trades, flanked by two negative folds. That is what a
lucky window looks like.

**On this corpus, at this timeframe, with these thresholds, the classical
analyser's entry timing is not distinguishable from being long at random.**
Nothing was retuned to change that reading, and no model will be bolted onto a
signal that failed its null.

### Slice 25: Stage 1 CLOSED — H25_REJECT

H25 asked whether ABSENT was a small-n / single-asset artefact. Same analyser,
same thresholds, same validated instrument, same bars, re-run on the two other
real corpora in the repo:

```
corpus   bars     trades   strategy R   blind R     M1     M2     Delta
1D       2,564        47    +0.2838    +0.1443    76.1   77.5   +0.1396
4H      15,379       350    +0.0294    -0.0065    73.0   74.0   +0.0358
1H      61,513     2,039    -0.1431    -0.1595    72.2   76.0   +0.0165
```

**H25_REJECT.** Trade count rises **43×** and the percentiles do not move toward
95 — they sit flat at 72–78 and drift slightly *down*. It was never a
sample-size artefact; 72–78 is a stable property of this analyser, measured at
three resolutions.

The analyser does beat the *average* blind schedule reproducibly — Δ > 0 with a
CI excluding zero, three times — but **22–26% of blind schedules beat it
outright** at every timeframe, and Δ shrinks as costs bite (+0.14 → +0.036 →
+0.017). On hourly bars **both sides are negative**. At every resolution exactly
one fold carries the result, and it is the same calendar window: the 2019–2021
bull run.

**This classical signal layer is CLOSED for timing-skill research.** See
**[STAGE1_VERDICT.md](STAGE1_VERDICT.md)** for the formal verdict and the
standard any future signal must meet.

Read **[EDGE.md](EDGE.md)** §4k–6d and **[GEOMETRY.md](GEOMETRY.md)**.

### Slice 26: instrument hygiene and a research freeze

No measurement changed and no question reopened. Two mechanical fixes:

**The ruler's exit code now matches the gates the humans declared.** It used to
exit 1 whenever the worst surrogate crossed a `75` ceiling — a bar a *correct*
instrument fails with near certainty (0.75ⁿ ≈ 10⁻²⁵ at n = 200). Slice 23's
validated control exited 1 on it. Now: **exit 0** when the control is valid
(median ≤ 50, uniformity not rejected, incompletes ≤ 5% of n), **1** when it
runs and fails, **2** when no null can be built. `worst` is still printed and is
never a gate.

Its exit code answers *"is the instrument valid?"*, **not** *"is there skill?"* —
so a valid control no longer prints "GREEN" and says in as many words that it
claims nothing about edge. `edge_measurement.py` is untouched and still exits
non-zero when M1 or M2 misses 95.0.

The `--lockup` default was **deliberately not changed**, so every historical
command block in `EDGE.md` still means what it meant when it ran; the
recommended invocation is documented instead.

**Hygiene is not research progress.** The signal layer stays CLOSED; model,
shadow and live stay BLOCKED.

Read `DEPLOYMENT.md` §0 before anything else: the API keys in this repository's
history must be treated as compromised.
