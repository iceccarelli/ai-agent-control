# TEST_REPORT.md

**3,112 tests passing, 1 skipped.** All offline. No test reaches the network.

> **Headline verdict: NOT READY for live capital — but the geometry gate is
> now GREEN.**
>
> Slice 9 fixed the payoff arithmetic without loosening anything. On daily
> bars, 163 trades over seven years: expectancy **+0.548R after fees**, 95% CI
> [+0.321, +0.785], hit rate 55.2% against a 36.2% break-even, 3/4 folds
> profitable, **0** reconciliation breaks.
>
> The take-profit and stop ratios are UNCHANGED. Only the timeframe moved: a 25
> bps round trip is ~60% of an hourly 1-ATR risk unit and ~3% of a daily one.
> Across 84 geometries: **0/84** hourly cells positive, **72/84** daily.
>
> What is still missing is an **edge**, and slice 10 built the instrument that
> will decide it. On real data the skill test read **percentile 100.0,
> p = 0.0000**. Its control — the same test on series with the time structure
> destroyed, where timing is impossible — read a **median of 95.4**. The
> instrument is therefore invalid and **Edge/Skill stays RED**. The control now
> runs unconditionally and the tool exits non-zero when it fails.
> See EDGE.md and GEOMETRY.md.

### Gate status

| Gate | Status | Evidence |
|---|---|---|
| Geometry (daily) | **GREEN** | +0.548R net, CI [+0.321, +0.785], 163 trades, 3/4 OOS folds, ruin 0.0%, breaks 0 |
| Edge / Skill | **RED** | Six nulls in. The stratified rotation is **infeasible**: 0 of ~2,300 offsets meet a tolerance fixed by rule before evaluation (best TV 0.2523 vs a 0.2037 resampling floor), so the tool exits 2 with no null to test against. This measures the slice-13 bisection as a geometric fact — the analyser's flags sit on bars whose ATR/range distribution cannot be reproduced by placing those flags anywhere else in the series. No skill reading is interpretable |
| Model promotion | **RED** | `models/current` empty; Phase B correctly not started while Phase A is red |
| Safety & measurement | **GREEN** | 3,112 tests, verified stops, single-writer, fail-closed gates, human-only kill switch |

---

## 1. Test inventory

| Suite | Tests | What it defends |
|---|---:|---|
| `test_config.py` | 97 | one loader, fractions, bps, the live gate, the policy gate |
| `test_risk_management.py` | 90 | equity-space risk, fail-closed gates, persistence across restart |
| `test_position_sizing.py` | 49 | Kelly floor removed, never-bump-up, no fabricated inputs |
| `test_bybit_connection.py` | 74 | signing, category-correct stops, idempotency, reconciliation |
| `test_lifecycle_integration.py` | 38 | end-to-end: never naked, no double execution, chaos |
| `test_orchestrator.py` | 21 | startup order, health server, loop, shutdown |
| `test_indicators.py` | 32 | Wilder smoothing vs published reference values |
| `test_strategy_and_analytics.py` | 51 | confidence ∈ [0,1], HOLD first-class, fee-aware metrics |
| `test_backtest.py` | 29 | harness honesty, walk-forward, Monte Carlo |
| `test_concurrency_and_dataflow.py` | 53 | single-writer enforcement, unit integrity, conservation |
| `test_market_data.py` | 211 | every rejection path, book reconstruction, **no lookahead**, three spellings of UTC and no fourth, blank `trades_count` only |
| `test_memory.py` | 334 | the reference layer: only tightens, never leaks, survives restart |
| `test_slice7_integration.py` | 82 | the category fork, one book measurement, memory wiring |
| `test_features.py` | 286 | scale invariance, **no lookahead**, label honesty, batch == single-bar |
| `test_policy.py` | 248 | calibration, **purged** CV, a noise model is refused |
| `test_training.py` | 133 | the holdout is never trained on, determinism, refusal writes nothing live |
| `test_ml_containment.py` | 63 | **a model may propose and nothing else** |
| `test_trade_stats.py` | 184 | R-multiples, break-even hit rate, expectancy CIs |
| `test_no_dead_imports.py` | 134 | structural guards across all 17 modules |
| `test_skill_test.py` | 88 | block-resampler invariants, **every entry is a run start**, Wilder-ATR and forward-range hand checks, shape-matched flags are **exactly** shape-matched |
| `test_data_contract.py` | 76 | synthetic is never eligible; the contract must agree with the loader |
| `test_control_rule.py` | 38 | the three clauses, one isolating case each; the median decides nothing |
| `test_btc_alt_spillover_v1.py` | 57 | the frozen signal: directions, next-open fills, the null's construction |
| `test_project_status.py` | 139 | the registration hook REFUSES: bars, attestation, universe, dual symbol |
| `test_directed_null_repair.py` | 42 | S1 embargo, S2 direction sequence, S3 shared builder; the slice-40 ABSENT artefacts |
| `test_paper_agent_certification.py` | 138 | groups A-F: truthfulness, human-only kill switch, operator control vs risk gates, paper stays paper, freeze integrity, a paper cycle |
| `test_post_shock_fade_v1.py` | 49 | the fade: direction, the hand-derived firing boundary, no forked R maths, the same-asset control |
| `test_range_location_fade_v1.py` | 58 | bar t excluded from its own range, inclusive thresholds, fade direction, the same-asset control |
| **Total** | **3,113 collected, 3,112 pass, 1 skipped** | |

Run with `python3 -m pytest tests/ -q` (~180 s).

---

## 2. Indicator validation against published references

The strongest evidence in this report, because it is checked against numbers
this project did not produce.

`pure_indicators.rsi()` reproduces Wilder's canonical worked example exactly:

| Bar | Expected | Measured |
|---|---:|---:|
| 1 | 70.46 | **70.46** |
| 2 | 66.25 | **66.25** |
| 3 | 66.48 | **66.48** |

A span-EMA implementation — what the legacy module used — cannot produce these
values. `test_rsi_matches_the_published_reference` is the guard.

---

## 3. Real data

Slice 8's first act was to stop measuring against a generated price path.

```
source     : github.com/ff137/bitstamp-btcusd-minute-data   (MIT)
instrument : BITSTAMP BTC/USD, 1-minute native, resampled to 1h
bars       : 61,513   2018-01-01 -> 2025-01-07
complete   : 100.0000%   (0 missing buckets, 0 rejected rows)
2020-03 covid crash  : PRESENT
2022 bear (full year): PRESENT
2021 bull            : PRESENT
```

Built by `tools/fetch_real_data.py`, which records the source file's sha256, the
licence, and the coverage of each stress period in `data/real/MANIFEST.json`.
`market_data.verify_manifest` refuses a corpus that does not match its manifest.

Three caveats that change what the numbers mean, stated in the corpus README, in
the manifest, and by the evaluation tool before it prints anything:

1. **Bitstamp BTC/USD, not Bybit BTCUSDT.** Different venue, different fees,
   different liquidity. The price series is genuine; the execution assumptions
   applied to it are Bybit's.
2. **No order book.** OHLC only, so the liquidity gate abstains and 3 of the 35
   features are structurally unavailable. Multi-year L2 archives are a paid
   product and are not worth buying before an OHLCV-only model shows something.
3. **`trades_count` is 0**, because the source does not carry it. Not invented.

---

## 4. Bugs found *by* this test cycle

Slice 9 added three, slice 8 four. The earlier thirteen are retained in summary.

| # | Defect | How it surfaced | Status |
|---|---|---|---|
| 18 | **A protective stop was placed the wrong side of its own fill.** The exit plan is built from the signal bar's close; the fill happens on the next bar. A gap through the planned stop meant placing a stop the position was already beyond — it fired instantly, a guaranteed −1R no signal quality could rescue | the new R-multiple instrumentation: a long filled at 10,501 with its stop at 10,593 | fixed — `_reanchor` shifts the whole bracket to the actual fill, **preserving the stop distance, not the stop price**, so the risk unit the sizer used survives the fill |
| 19 | **Booked-out dust broke reconciliation, and the break grew with the price.** A sub-minimum remainder was closed on the books — correctly, it can be neither sold nor protected — while the exchange still held it. Reported **317 times** on daily data, appearing only above ~$55k, because fixed dust crosses a fixed minimum *notional* as an asset appreciates | daily backtest reconciliation | fixed — dust is **booked** to a persisted per-symbol ledger and included in every reconciliation. `breaks: 0` |
| 20 | `Backtester.run` read the ledger through `recent_trades()`'s 200-row default, so a run closing more than 200 trades would have reported 200. Slice 8's fold 2 closed 192 | building the R-multiple harvest | fixed; no published number changes, and the harvest is now cross-checked against the ledger count |
| 14 | **The cost gate treated a signal score as a win probability.** `p = confidence`, where `confidence` is an agreement-weighted vote magnitude with no frequency interpretation. At p=0.15 with a 2:1 payoff the expected edge is negative *before costs*, for every trade, forever | seven years of real data producing **zero** trades, with 35,447 blocks between `BELOW_MIN_CONFIDENCE` and `EDGE_BELOW_COST` | fixed — `win_probability` is a separate field, filled only by something calibrated; with nothing calibrated the gate judges the reward leg alone, which is **stricter** |
| 15 | **`SimulatedExchange` hardcoded `quote_asset="USDT"`.** The real corpus is BTC/**USD**, so the client asked for a USD balance the simulator had never seeded, and every buy returned `INSUFFICIENT_USD` | 15,668 rejections on a run that otherwise read as a clean "no trades" result | fixed — derived from the symbols, and a mixed-quote universe is **refused** rather than settled into one pot |
| 16 | `_apply_memory_throttle` passed `should_trade=` to `dataclasses.replace`, but it is a derived property — the risk-*reduction* path would have raised `TypeError` at exactly the moment it was needed | writing the throttle tests | fixed — `qty=0.0` *is* "do not trade" |
| 17 | The backtest config view was a hand-written mirror of the config schema and went stale the moment the schema grew | first slice-7 backtest run | fixed — built by the real `config.load()` |

Bug 15 is the more instructive of the two big ones, and its lesson is not about
currency codes. **A default that is right for the data you happen to have is
indistinguishable from a correct implementation until the data changes** — and
the failure it produced looked exactly like a strategy that had declined to
trade. It survived seven slices, 1,255 tests and a synthetic corpus that was
named `BYBIT_SPOT_BTC_USDT` precisely because that is what the code assumed.

Bug 14 is the more consequential. It is the same class as the same-symbol
re-entry defect from slice 6: two things with the same shape, treated as the
same thing. `confidence` lives in [0,1] and looks like a probability, which is
exactly why the conflation survived an audit and six rebuilds.

Earlier defects, still fixed and still guarded: side matched by prefix
(`"Sideways"` → SELL); a filled take-profit leaving the position book unchanged;
same-symbol re-entry overwriting a position row (equity +7,673, ledger −26); a
stop not resized after a partial exit; Monte Carlo's `position_fraction`
cancelling out of the compounding term; `record_order`'s SELECT-then-INSERT
race; the risk/reward gate measuring only the first TP leg; the strategy's own
ladder averaging 0.75× its required RR; `MIN_EDGE_BPS` defaulting below cost;
`settleCoin` sent on spot; the order-book warning logged once per loop
iteration; a data generator that labelled a **+46%** phase a "crash".

---

## 5. Chaos and failure-mode tests

Every one asserts the system ends in a **safe** state, not merely that it
survives.

| Scenario | Expected | Result |
|---|---|---|
| Stop placement rejected after entry fills | market-close + kill switch | ✅ |
| Stop unverifiable, or a perp position with an empty `stopLoss` | treated as no stop → same | ✅ |
| Fill never confirmed | **no second entry**, symbol cooldown | ✅ |
| `kill -9` between submit and ack, then restart | reconciled, **no duplicate order** | ✅ |
| Restart with a naked position on the books | protected or closed | ✅ |
| Exchange returns HTML instead of JSON | no trade, no crash | ✅ |
| Transient 5xx during submit | retried with the **same** `orderLinkId` | ✅ |
| Kill switch engaged | every trade blocked; nothing in the engine, memory **or model** can clear it | ✅ |
| Order book crossed, one-sided, or empty | no mid, no microprice, the gate blocks | ✅ |
| Memory layer raises on every call | sizing unchanged, trade proceeds | ✅ |
| Corpus file edited after generation | manifest verification refuses the load | ✅ |
| **scikit-learn not installed** | model permanently unavailable, classical strategy runs | ✅ |
| **Model returns NaN, or a probability of 1.7** | unusable → the gate gets **stricter** | ✅ |
| **Model disagrees with the classical signal** | the model is ignored, never obeyed | ✅ |
| **Model drifts against live outcomes** | demoted automatically, never re-armed automatically | ✅ |

---

## 6. The classical strategy on real data

### Full period — 61,312 bars, 2018→2025, in-sample, spot

```
signals evaluated  : 61313 (19829 actionable)
trades closed      : 53
starting equity    : 10,000.00
ending equity      : 9,966.44
total return       : -0.34%
fees paid          : 10.75
max drawdown       : 0.50%
win rate           : 32.1%
expectancy / trade : -0.6366
book/exchange breaks: 0
top block reasons  : BELOW_MIN_CONFIDENCE=19935, SHORT_SUPPRESSED_ON_SPOT=18539,
                     NO_RISK_BUDGET=13857, RISK_REWARD_TOO_LOW=3894,
                     WEAK_AGREEMENT=2369
```

### Walk-forward — out-of-sample only

| Fold | Train | Test | Trades | Return | Max DD | Win rate |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 9,226 | 6,152 | 30 | **−0.19%** | 0.20% | 43.3% |
| 2 | 9,226 | 6,152 | 192 | **−0.17%** | 0.95% | 54.2% |
| 3 | 9,226 | 6,152 | 31 | **−0.30%** | 0.37% | 35.5% |
| 4 | 9,226 | 6,152 | 30 | **−0.14%** | 0.19% | 40.0% |

```
folds profitable  : 0/4
mean fold return  : -0.20%
LOSING FOLDS      : [1, 2, 3, 4]
```

**All four folds lose money, and every one of them traded.** This is a
materially better result *as evidence* than slice 7's, where four folds took
zero trades and were counted as "profitable" for doing nothing. A strategy that
loses in every out-of-sample period is a clear answer; a strategy that never
trades is not an answer at all.

Fold 2 is worth a second look: 192 trades at a **54.2% win rate**, and still
negative. That is the payoff geometry, not the signal — a 54% hit rate loses
money when the average loss is larger than the average win, and it says the next
piece of work is the exit policy rather than the entry.

### Monte Carlo — bootstrap ruin estimate

```
trades_sampled_from : 53          (30 required — enough, for the first time)
runs                : 5000
probability_of_ruin : 0.0
median_final_equity : 9,998.85
p05 / p95           : 9,883.85 / 10,118.45
median_max_drawdown : 0.71%
```

A ruin probability of zero here means only that a 0.5% max drawdown at 2%
position sizing cannot bankrupt an account. It is not a positive finding. The
median outcome is a **loss**, and the 95th percentile of a strategy resampled
from its own losing trades is +1.18%.

### Verdict, as the tool prints it

```
NOT READY for live capital.
  - mean out-of-sample return is -0.20% (must be > 0)
  - folds [1, 2, 3, 4] lost money out of sample
```

---

## 7. The learned policy on real data

Trained by `tools/train_policy.py` against the same corpus: 48,718 rows for
train/validate (2018-01 → 2023-08), **12,247 rows held out entirely**
(2023-08 → 2025-01), purged walk-forward with a 264-bar embargo.

| fold | validation period | Brier | baseline | skill | AUC |
|---|---|---:|---:|---:|---:|
| 0 | 2019-02 → 2020-04 | 0.2109 | 0.2098 | **−0.0051** | 0.5361 |
| 1 | 2020-04 → 2021-05 | 0.2215 | 0.2201 | **−0.0065** | 0.5266 |
| 2 | 2021-05 → 2022-06 | 0.2043 | 0.2044 | 0.0005 | 0.5399 |
| 3 | 2022-06 → 2023-08 | 0.2126 | 0.2127 | 0.0004 | 0.5199 |

Holdout: AUC 0.5380, Brier 0.2217 vs 0.2223 baseline, ECE 0.032.

### Verdict: REFUSED

```
AUC_MEAN_TOO_LOW: 0.5306 < 0.55
AUC_INCONSISTENT_ACROSS_FOLDS: fold 3 = 0.5199 < 0.52
BRIER_NO_BETTER_THAN_BASE_RATE: skill = -0.0027
HOLDOUT_AUC_TOO_LOW: 0.5380 < 0.55
```

The artefact went to `models/rejected/`. `models/current` was not written.
Nothing was tuned against the holdout to make it pass.

**Two folds are worse than always predicting the base rate.** Calibration is
fine and monotone — but the model only ever predicts between 0.18 and 0.41,
which is another way of saying it barely moves off the base rate. A
well-calibrated model with almost no information is exactly what an honest
pipeline produces from features that do not contain much, and exactly what a
promotion check looking only at calibration would have waved through. That is
why the criteria are a conjunction.

The feature importances are at least reassuring about the *pipeline*:
`dist_sma100_atr`, `di_spread`, `dist_sma50_atr`, `ret_24`, `rv_72` — plausible
things. The cyclical clock features sit at ~0.000, which is the answer you want;
a model keying on the hour of day would have suggested leakage.

### The evidence that the purge is real

Constructed data with overlapping label windows and no generalisable signal:
un-purged validation AUC ~0.59, purged ~0.48, un-purged winning in **6 of 6**
seeds. The gap *is* the leak. Purging is also asserted on indices, against exact
expected arrays, so the mechanism is checked as well as its effect.

On the real corpus, **100% of consecutive rows have overlapping outcome
windows** — a plain k-fold there would have produced a beautiful, meaningless
number.

---

## 7b. Slice 9 — the geometry, measured

`tools/sweep_geometry.py` resolves every bar's barriers across 84 geometries
and reports expectancy in R before and after a 25 bps round trip. The
vectorised scan is cross-checked against `features.triple_barrier_label` and
**refuses to print a table if the two disagree**.

| corpus | bars | cells with positive net expectancy |
|---|---:|---:|
| 1 hour | 61,513 | **0 / 84** |
| 4 hours | 15,379 | 8 / 84 |
| 1 day | 2,564 | **72 / 84** |

Identical ratios, identical filters, identical costs. Only the bar changed.

```
                        stop distance    cost_R at 25 bps
hourly, 1-ATR stop         ~40 bps           ~0.60 R
daily,  2-ATR stop        ~600 bps           ~0.034 R
```

To break even at 2:1 you need 33.3%. With 0.60R of cost drag you need 53.3% —
which is why slice 8's 54.2%-win-rate fold was a coin flip that lost.

### Daily, out of sample

| Fold | Period | Trades | Return | Win rate | The asset |
|---:|---|---:|---:|---:|---:|
| 1 | 2018-01 → 2019-10 | 28 | **+1.82%** | 78.6% | **−37.8%** |
| 2 | 2019-10 → 2021-07 | 41 | +0.73% | 58.5% | +315.0% |
| 3 | 2021-07 → 2023-04 | 12 | −0.17% | 50.0% | −17.9% |
| 4 | 2023-04 → 2025-01 | 14 | +0.08% | 57.1% | +265.9% |

Every gate in the brief is met: mean OOS +0.61%, one marginal losing fold at
−0.17%, 163 trades, positive expectancy after fees with a CI excluding zero,
ruin probability 0.0%, zero reconciliation breaks.

### And the reason not to celebrate

The same sweep on the **short** side of the same data: **0 of 84 cells
positive, gross or net.** A real inefficiency does not vanish when you change
sign; an appreciating asset does. The evaluation tool now prints the benchmark
that makes this unmissable:

```
strategy                        +1.39%
best buy-and-hold             +675.34%
-> the strategy captured 0.2% of the move it was long into.
```

One number resists that story — fold 1 made money while the asset fell 37.8% —
and it rests on 28 trades. It is recorded as an open question.

### Why the returns are negligible

+0.548R over 163 trades is +89R. At `RISK_PER_TRADE_PCT=0.005` that is +44%; it
produced +1.39%, so realised risk per trade was ~30× below budget. The
**notional** cap (2%) binds long before the **risk** cap (0.5%), because a 6%
stop would need 8.3% notional to risk 0.5%. On daily bars, where stops are
necessarily wide, that is every trade — not a corner case.

Slice 9 did **not** change it. It is a risk limit, and it is a human's decision.

---

## 8. The finding that outranks everything else in this report

From the labelling study, seven years, real data:

```
take-profit (+2 ATR) : 30.7%
stop        (−1 ATR) : 65.2%
timeout              :  4.1%
```

At a 2:1 payoff:

```
0.307 × 2 − 0.652 × 1 = −0.038 ATR per trade,  BEFORE costs
```

**The trade geometry has negative expectancy independent of signal quality.** A
1-ATR stop is nearer than a 2-ATR target and therefore wins every bar in which
both are touched. Break-even at 2:1 needs 33.3%; the unconditional rate is
30.7%. A model would have to add ~3 points of hit rate to reach zero, and
another ~4 to cover a 25 bps round trip.

This is measured, not argued, and it explains the full-period result better than
any property of the signal: the strategy's realised win rate was 32.1%, which is
almost exactly the unconditional rate. **The entry signal is adding roughly
nothing, and the exit policy is losing money on its own.**

It also says what to do next, and it is not "a better model":
`features.BarrierSpec` takes `take_profit_atr`, `stop_atr` and `max_horizon`.
Sweeping those three parameters against their own realised hit rates costs one
afternoon and tests the assumption that has been load-bearing since slice 1.

---

## 9. What slice 8 did NOT do

- **Did not lower a threshold to produce trades.** `MIN_CONFIDENCE` and
  `MIN_COMPONENT_AGREEMENT` are unchanged. The strategy now trades because a
  category error in the cost gate was fixed, not because a gate was loosened.
- **Did not promote a model.** It was refused, and the refusal is reported here
  in full rather than being followed by a hyper-parameter search against the
  holdout.
- **Did not weaken a single invariant to admit the model.** The model writes one
  field on a `TradeIntent`; every gate then runs unchanged. 63 tests in
  `test_ml_containment.py` exist to make that checkable rather than promised.
- **Did not claim an "AI trading agent".** There is a trainable policy, an
  honest harness, and a model that failed it.

---

## 10. Not done, stated plainly

- **No promoted model exists.** `models/current` is empty by design.
- **No live shadow run.** Shadow mode is implemented and tested; nobody has run
  it against a live feed for weeks, which is the entire point of it.
- **No linear backtest.** The simulator does cash accounting; a perp needs
  funding accrual, mark-to-market and liquidation. `Backtester.run()` raises
  rather than reporting a flattering number.
- **No order book on the real corpus**, so the liquidity gate and 3 of 35
  features are untested against real market microstructure.
- **No 48-hour continuous paper run.** Not possible in a session-bound sandbox.
- **Private WebSocket is not connected.** Auth-frame construction is implemented
  and tested; the socket loop is not wired. Fills are confirmed by REST polling.
- **Not modelled in the backtester:** partial fills and queue position, borrow
  interest, market impact, signal-to-submission latency. Live results should be
  expected to be **worse** than backtest results.
