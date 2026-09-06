# ML_POLICY.md — the learned policy, and everything it is not allowed to do

Slice 8 adds a trainable model. This document is mostly about the constraints,
because the constraints are the engineering; a gradient-boosted tree on
engineered features is a weekend, and a model that cannot hurt you is the part
that takes the rest of the time.

    A model may PROPOSE. It may not decide, resize, override, or resume.

---

## 1. What was actually built

| Module | Responsibility |
|---|---|
| `features.py` | 35 scale-free features + triple-barrier labels. No lookahead, structurally. |
| `policy.py` | The trained artefact: calibrated probabilities, purged walk-forward CV, promotion criteria that default to refusal. |
| `ml_strategy.py` | The seam. Off / shadow / live. The only place a model output touches a trade. |
| `tools/train_policy.py` | The offline loop. Writes a promoted model, or writes a rejection and says why. |
| `tools/fetch_real_data.py` | Real multi-year data, because a model fitted to a generated price path learns the generator. |

The data flow, end to end:

```
data/real (OHLCV, 7 years)  ->  features.build_dataset  ->  policy.train
                                                               |
                                                    promotion criteria
                                                     (default: REFUSE)
                                                               |
                                                        models/current
                                                               |
   candles -> features.compute_features -> policy.predict_edge -> p
                                                               |
                       ml_strategy attaches p to TradeIntent.win_probability
                                                               |
      risk_management (EVERY gate, unchanged) -> position_sizing -> memory
                                                               |
                                         trading_engine (verified stop)
```

Note where the model sits: **upstream of every gate, and nowhere else.** It is
an input to the cost gate's arithmetic, in the same position a hand-written
estimate would occupy.

---

## 2. The one field a model may write

`TradeIntent.win_probability` — a calibrated probability that this trade reaches
its target before its stop. That is the whole interface.

Everything about the trade's *geometry* — symbol, direction, entry, stop, the
take-profit ladder — still comes from the classical strategy. The model cannot
propose a trade that the classical strategy did not propose, cannot reverse one,
cannot move a stop, and cannot set a size.

`test_the_only_field_the_model_writes_is_win_probability` parses
`ml_strategy._attach_probability` and asserts the set of fields written is
exactly `{"win_probability"}`. Adding a second one fails the test.

### The correctness fix underneath it

Through slice 7 the cost gate computed `p = confidence`. That was a category
error, and finding it is the most useful thing this slice did.

`confidence` is a **signal-strength score**: the agreement-weighted magnitude of
the component votes. It lives in [0, 1] and it looks like a probability, which
is exactly why the conflation survived six slices and a full audit. It has no
frequency interpretation — 0.15 means "the components mildly agree", not "wins
15% of the time".

The consequence was arithmetic and severe. At `p = 0.15` with a 2:1 payoff:

```
0.15 × 2 − 0.85 × 1 = −0.55       (before a single basis point of cost)
```

Negative before costs, for every trade, forever. On seven years of real BTC data
the strategy took **zero** trades, and `BELOW_MIN_CONFIDENCE` and
`EDGE_BELOW_COST` between them accounted for 35,447 blocks. The gate was
rejecting essentially everything for a reason that was a property of its input
rather than a property of any trade.

The two concepts are now separate fields. `win_probability` is the only one the
gate's arithmetic consumes, and it is only ever filled in by something
calibrated. `confidence` is still carried and still journaled, so the decision
record shows what the strategy thought — it simply no longer drives the maths.

**The fallback when there is no calibrated probability is the reward leg alone
against cost**, which is stricter than assuming a coin flip and much stricter
than assuming the confidence score. A model failure therefore makes the system
*more* cautious. That direction is not incidental; it is the design.

---

## 3. Three modes, and why the middle one exists

| Mode | Model loaded | Model evaluated | Can it change an order? |
|---|---|---|---|
| `off` (default) | no | no | no |
| `shadow` | yes | yes | **no — provably** |
| `live` + `POLICY_ACK` | yes | yes | it may attach a probability |

In shadow mode `PolicyStrategy.signal_for` returns the classical intent object
**itself** — not a copy with a `None` probability, the same object. The
load-bearing test asserts identity:

```python
result = strategy.signal_for("BTCUSDT")
assert result is base._intent
```

If the wrapper cannot change the intent at all, then no argument about model
quality, calibration or drift can make shadow mode unsafe. That is worth more
than any amount of care taken inside the wrapper.

Shadow mode is where an unproven model belongs, and it produces something no
backtest can: a record of what the model would have done on data that did not
exist when it was trained. Each record keeps **both** views — the model's and
the classical strategy's — because the question an operator eventually has to
answer is not "was the model right" but "was it right *where it disagreed*", and
a record with only one side cannot answer that.

Arming `live` takes three separate acts: a training report where the model
already cleared `meets_promotion_criteria`, `POLICY_MODE=live`, and the exact
`POLICY_ACK` token. Anything less degrades to shadow and logs why — the same
shape as the live-trading gate, because it is the same kind of decision.

---

## 4. Promotion is manual. Demotion is automatic.

That asymmetry is deliberate, and it is the opposite of what an "autonomous
agent" would do.

**Promotion** requires a human, every time. `policy.meets_promotion_criteria()`
defaults to refusal and demands all of:

| Criterion | Default | Why |
|---|---|---|
| usable folds | ≥ 3 | two folds cannot distinguish "consistent" from "lucky twice" |
| samples | ≥ 1000 | below this every confidence interval is wider than the claimed effect |
| mean OOS AUC | ≥ 0.55 | the smallest ranking edge that survives a ~25 bps round trip |
| **AUC in every fold** | ≥ 0.52 | excellent-in-one and useless-in-three is one lucky regime, and the mean hides it |
| Brier skill | ≥ 0.01 | must beat "always predict the base rate"; a model that cannot is adding noise to the edge gate |
| calibration error | ≤ 0.05 | 5 points of miscalibration on p=0.6 is a ~10% error in the reward term — the same order as the entire cost budget |

The criteria are a **conjunction**, and the noise-model test shows why. A model
trained on pure noise has *fine calibration*: it learns to say "about 0.5" and is
right about half the time. A calibration-only check would pass it.

**Demotion** requires nothing. `PolicyDriftMonitor` compares realised outcomes
against the model's predictions using the Brier score — squared error, because
that is what the gate's arithmetic is sensitive to — and when live performance
diverges from the training baseline past a tolerance, `may_propose()` starts
returning False and the system reverts to the classical strategy.

It never un-fires on its own. A model that drifted and then looked fine again
for twenty trades has not been vindicated; it has produced twenty trades.

Automatic promotion is how a system talks itself into a bigger position after a
lucky streak. Automatic demotion is how it stops paying for a broken one.

---

## 5. The two ways ML backtests lie, and what was done about them

**Lookahead.** `features.compute_features` slices the candle series down to
`[index - MAX_LOOKBACK + 1 : index + 1]` on the second line of its body, and
nothing below that ever sees the original. The future is not avoided by
discipline — it is not present. `TestNoLookahead` verifies it from the outside:
computing at bar *i* on the full series must equal computing on the series
truncated at *i*, for many random *i*.

**Label leakage.** A triple-barrier label resolves over up to 24 subsequent
bars, so consecutive samples share outcome windows — on the real corpus, **100%
of consecutive rows overlap**. A plain k-fold on that data produces a beautiful,
meaningless validation score, and it is the single biggest reason published
crypto ML results do not survive contact with an account.

`policy.purged_walk_forward_folds` removes, from each training fold, every
sample whose outcome window reaches into the validation fold, plus an embargo
gap (defaulting to `features.MAX_LOOKBACK` = 264 bars, because that is exactly
how far feature serial correlation reaches).

The purge is tested two ways: on **indices**, against exact expected arrays; and
empirically, by constructing data with overlapping windows and no generalisable
signal, where the un-purged run scores ~0.59 AUC and the purged run ~0.48,
winning in 6 of 6 seeds. The gap *is* the leak.

---

## 6. What the training run actually produced

Run against `data/real` — 61,513 hourly Bitstamp BTC/USD bars, 2018-01 → 2025-01,
covering the 2020-03 crash and the whole 2022 bear.

```
rows kept          : 61,233 / 61,513  (99.54%)
labels             : 18,790 take-profit / 39,950 stop / 2,493 timeout
base rate          : 0.3013
features used      : 32 of 35 (the 3 book features are structurally unavailable)
train / validate   : 48,718 rows, 2018-01-11 -> 2023-08-04
holdout            : 12,247 rows, 2023-08-15 -> 2025-01-06  (never seen)
```

| fold | validation period | Brier | baseline | skill | AUC |
|---|---|---:|---:|---:|---:|
| 0 | 2019-02 → 2020-04 | 0.2109 | 0.2098 | **−0.0051** | 0.5361 |
| 1 | 2020-04 → 2021-05 | 0.2215 | 0.2201 | **−0.0065** | 0.5266 |
| 2 | 2021-05 → 2022-06 | 0.2043 | 0.2044 | 0.0005 | 0.5399 |
| 3 | 2022-06 → 2023-08 | 0.2126 | 0.2127 | 0.0004 | 0.5199 |

Holdout: AUC 0.5380, Brier 0.2217 vs 0.2223 baseline.

### Verdict: REFUSED

```
AUC_MEAN_TOO_LOW: 0.5306 < 0.55
AUC_INCONSISTENT_ACROSS_FOLDS: fold 3 = 0.5199 < 0.52
BRIER_NO_BETTER_THAN_BASE_RATE: skill = -0.0027
HOLDOUT_AUC_TOO_LOW: 0.5380 < 0.55
HOLDOUT_BRIER_NO_BETTER_THAN_BASE_RATE: skill = 0.0024 < 0.01
```

The artefact went to `models/rejected/`. `models/current` was not written.
Nothing was tuned against the holdout to make it pass.

**Folds 0 and 1 are worse than always predicting the base rate.** Calibration is
fine (ECE 0.032, monotone) — but the model only ever predicts between 0.18 and
0.41, which is another way of saying it barely moves off the base rate. A
well-calibrated model with almost no information is exactly what an honest
pipeline should produce from features that do not contain much, and exactly what
a promotion check that only looked at calibration would have waved through.

Feature importances are at least reassuring about the *pipeline*: the top
contributors are `dist_sma100_atr`, `di_spread`, `dist_sma50_atr`, `ret_24`,
`rv_72` — plausible things. The cyclical clock features sit at ~0.000, which is
the answer you want; a model keying on the hour of day would have suggested
leakage or overfitting.

---

## 7. The finding that outranks the model

From the labelling study, on seven years of real data:

```
take-profit (+2 ATR) : 30.7%
stop        (−1 ATR) : 65.2%
timeout              :  4.1%
```

At a 2:1 payoff that is:

```
0.307 × 2 − 0.652 × 1 = −0.038 ATR per trade,  BEFORE costs
```

**The trade geometry this strategy uses has negative expectancy independent of
signal quality.** A 1-ATR stop is nearer than a 2-ATR target and therefore wins
every bar in which both are touched. No model that predicts this label
distribution can fix that, because the arithmetic is upstream of the prediction:
to break even at 2:1 you need a 33.3% hit rate, and the unconditional rate is
30.7%. A model would have to add ~3 points of hit rate just to reach zero, then
another ~4 to cover a 25 bps round trip.

This is the most useful number produced in eight slices, and it says the next
piece of work is **not** a better model. It is the exit policy: barrier ratios,
holding horizon, whether a fixed ATR multiple is the right stop at all. That is
measurable with `features.triple_barrier_label` and a parameter sweep, and it
costs nothing to run.

---

## 8. What is still not true

- **No promoted model exists.** `models/current` is empty by design. Nothing in
  the live stack loads a policy artefact today because there is no artefact
  worth loading.
- **The model is price-only.** The real corpus has no order book, so 3 of 35
  features are structurally unavailable. Whether L2 features would help is
  untested — and per the migration plan, not worth buying multi-year L2
  archives to find out until the OHLCV-only version shows something.
- **It is Bitstamp BTC/USD, not Bybit BTCUSDT.** A different venue with
  different liquidity and fees. The price series is genuine; the execution
  assumptions applied to it are Bybit's.
- **No live shadow run.** Shadow mode is implemented and tested; nobody has run
  it against a live feed for weeks, which is the point of it.
- **No regime-conditional models, no experience replay, no canary deployment.**
  Those are Phase 3 in the migration plan and they are premature: every one of
  them is a way of making a model better, and the current model is not
  *slightly* short of the bar.

---

## 9. If you are picking this up next

In order, and the order matters:

1. **Sweep the barrier geometry.** `features.BarrierSpec` takes
   `take_profit_atr`, `stop_atr` and `max_horizon`. Find whether any
   combination has positive expectancy at its own hit rate, before costs. If
   none does, no model will save it and the answer is a different exit policy.
2. **Only then, richer features.** Cross-asset, funding, on-chain, longer
   horizons. Add them to the schema, bump `FEATURE_SCHEMA_VERSION`, retrain. The
   version bump is not bureaucracy: `policy.load` refuses an artefact whose
   schema does not match, which is what stops a silently reordered feature
   vector producing plausible, wrong predictions forever.
3. **Run shadow mode for weeks** before even thinking about `live`.
4. **Never lower a promotion criterion to get a model promoted.** The criteria
   are in `policy.PromotionCriteria`, in code, where a change is reviewable —
   and deliberately not exposed as a command-line flag. If a model cannot clear
   0.55 AUC, the honest response is that it has not found anything.
