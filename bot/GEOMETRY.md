# GEOMETRY.md — why the trades lost, and what actually fixed it

Slice 9's brief was one sentence: **find a set of (entry, stop, take-profit)
rules whose expected value is positive after realistic costs.** Everything else
was secondary, because a model trained under a losing geometry is learning to
forecast the outcome of a losing game more precisely.

The answer turned out not to be the ratios.

---

## 1. What slice 8 had measured

```
take-profit (+2 ATR) : 30.7%      break-even at 2:1 : 33.3%
realised win rate    : 32.1%      out-of-sample     : 0/4 folds profitable
```

and, most damningly, a fold that traded **192 times at a 54.2% win rate and
still lost money**. A hit rate that high losing money is not a signal problem.

---

## 2. The theory, stated before the measurement

On a driftless random walk, the probability of touching `+k·ATR` before
`−m·ATR` is exactly `m/(k+m)`. Expectancy in R is therefore:

```
(m/(k+m)) · (k/m)  −  (k/(k+m)) · 1  =  0
```

**Zero, for every k and every m.** Geometry alone cannot manufacture edge. Any
sweep of take-profit and stop ratios is a sweep along a line whose true value
is zero, and a positive cell in such a sweep is drift, serial dependence, or
noise.

What geometry *can* change is how much of the result costs consume, and that is
where the real finding is:

```
cost_R  =  round_trip_cost_bps / (stop_distance_bps)
```

The round trip is fixed in basis points. The R unit scales with volatility over
the bar. So the same fee schedule is a completely different tax depending on
what a bar is.

---

## 3. The sweep — 84 geometries, seven years, real data

`tools/sweep_geometry.py` resolves every bar's barriers with a vectorised scan
(cross-checked against `features.triple_barrier_label` on a random sample — it
refuses to print a table if the two disagree) and reports expectancy in R
before and after a 25 bps round trip.

Timeouts resolve at the **realised** price, not zero. That is deliberate: a
timeout scored as zero would hide drift, which is the one effect a long-only
strategy on an appreciating asset actually has.

### The result

| corpus | bars | cells with positive **net** expectancy |
|---|---:|---:|
| `data/real` (1 hour) | 61,513 | **0 / 84** |
| `data/real_4h` (4 hours) | 15,379 | 8 / 84 |
| `data/real_1d` (1 day) | 2,564 | **72 / 84** |

Identical ratios. Identical filters. Identical costs. Only the bar changed.

### Why, in one column

```
                             stop distance   cost_R at 25 bps
hourly, 1-ATR stop              ~40 bps          ~0.60 R
hourly, 2-ATR stop              ~80 bps          ~0.30 R
daily,  2-ATR stop             ~600 bps          ~0.034 R
```

**On hourly bars a 25 bps round trip consumes 60% of the entire risk unit.**
Nothing survives that. To break even at 2:1 you need 33.3%; to break even at
2:1 *with 0.60R of costs* you need 53.3%. That is the arithmetic behind slice
8's 54.2%-win-rate losing fold — it was almost exactly at that break-even and
the remaining variance did the rest.

On daily bars the same cost is 3.4% of R and the ordinary 2:1 geometry works.

**The take-profit and stop ratios were never the problem. The timeframe was.**

---

## 4. What changed, and what deliberately did not

Changed:

* `PRIMARY_TIMEFRAME` default: `60` → `D`, and `SECONDARY_TIMEFRAMES` `240` → `W`.
* A load-time **warning** when the timeframe is an hour or shorter and the
  round trip is 15 bps or more, naming this document.
* `market_data.load_corpus` accepts any interval suffix, not just `_1H` — it
  had silently found nothing in a 4-hour corpus and reported "no usable OHLCV
  files", which describes the directory rather than the assumption.

Deliberately **not** changed:

* **No ratio was tuned.** The shipped 2-ATR stop and 2:1 reward are exactly
  what slice 8 had. Fitting `take_profit_atr` and `stop_atr` to seven years of
  one asset would produce two more numbers with no out-of-sample meaning, and
  the measured difference between 2:1 and 3:1 (below) does not justify it.
* **No risk limit, no gate, no promotion threshold, no kill switch.**
* **No threshold lowered to produce trades.**

For the record, the tested alternative:

| geometry | trades | return | expectancy net | mean OOS | worst fold |
|---|---:|---:|---:|---:|---:|
| **2 ATR stop, 2:1** (shipped) | 163 | +1.39% | +0.548R | **+0.61%** | **−0.17%** |
| 1 ATR stop, 3:1 | 176 | +2.06% | **+0.788R** | +0.38% | −0.41% |

The 3:1 variant has higher per-trade expectancy and a worse out-of-sample
profile. On this evidence there is no reason to prefer it, and choosing it
would be selecting on the in-sample number.

---

## 5. The measured result on daily bars

`python3 tools/run_evaluation.py --data-dir data/real_1d --interval D`

```
trades closed      : 163
total return       : +1.39%
book/exchange breaks: 0

-- payoff geometry (R = the risk actually taken) --
hit rate (R)       : 55.2%  (90W / 73L)
avg win / loss     : +1.84R / -1.04R
payoff ratio       : 1.76:1
break-even hit     : 36.2%
hit-rate edge      : +19.0 pts — ABOVE break-even
profit factor      : 2.18
expectancy gross   : +0.5821R  95% CI [+0.354, +0.820]
expectancy net     : +0.5479R  95% CI [+0.321, +0.785]
cost drag          : 0.0343R per trade (20.2 bps of fees / 595.9 bps of stop)
```

Walk-forward, out of sample:

| Fold | Period | Trades | Return | Win rate | The asset itself |
|---:|---|---:|---:|---:|---:|
| 1 | 2018-01 → 2019-10 | 28 | **+1.82%** | 78.6% | **−37.8%** |
| 2 | 2019-10 → 2021-07 | 41 | +0.73% | 58.5% | +315.0% |
| 3 | 2021-07 → 2023-04 | 12 | **−0.17%** | 50.0% | −17.9% |
| 4 | 2023-04 → 2025-01 | 14 | +0.08% | 57.1% | +265.9% |

```
folds profitable : 3/4    mean +0.61%    worst -0.17%
probability of ruin : 0.0  (163 trades, enough to estimate for the first time)
```

Against the gates in the brief:

| Gate | Required | Measured | |
|---|---|---|---|
| mean OOS return | > 0 | +0.61% | ✅ |
| losing folds | 0, or 1 marginal | 1, at −0.17% | ✅ marginal |
| trades | ≥ 100 | 163 | ✅ |
| expectancy after fees | > 0 | +0.548R, CI excludes 0 | ✅ |
| ruin probability | < 5% | 0.0% | ✅ |
| book/exchange breaks | 0 | 0 | ✅ |

**The geometry gate is green.** That is a real change from slice 8, and it was
achieved by fixing arithmetic, not by loosening anything.

---

## 6. Now the part that matters more

### The edge is substantially drift, and here is the evidence

Run the identical sweep on the **short** side of the same daily data:

```
LONG  : 72 of 84 cells positive net
SHORT :  0 of 84 cells positive net — and 0 of 84 positive GROSS
```

A genuine market inefficiency does not vanish when you change sign. An
appreciating asset does exactly that. Over this sample BTC went from ~$13k to
~$102k, and a long-only barrier strategy resolves its timeouts into that.

### The benchmark the harness was missing

`tools/run_evaluation.py` now prints it, because its absence is precisely how a
drift-capturing result reads as skill:

```
strategy                        +1.39%
best buy-and-hold             +675.34%
-> the strategy captured 0.2% of the move it was long into.
```

**+1.39% over seven years.** Positive, statistically distinguishable from zero
per unit of risk, and economically negligible.

### But it is not *purely* drift

Fold 1 returned **+1.82% while the asset fell 37.8%**, at a 78.6% win rate.
That is not something a drift-capture story explains, and it is the one
genuinely encouraging number in this document. It rests on 28 trades, which is
not enough to conclude anything, and it is reported here as an open question
rather than a finding.

The honest summary: the geometry is fixed and the arithmetic now works. Whether
there is an *edge* underneath it is not yet established, and the strategy as it
stands is a long-only position so small it is indistinguishable from doing
almost nothing.

### Why the returns are so small — the finding for slice 10

The expectancy is +0.548R over 163 trades: **+89R in total**. At the configured
`RISK_PER_TRADE_PCT=0.005` that would be +44%. It produced +1.39%, so the
realised risk per trade was about **0.016% of equity — roughly 30× smaller than
the budget.**

The reason is that two caps are inconsistent for wide stops:

```
RISK_PER_TRADE_PCT   = 0.005   -> with a 6% stop, needs 8.3% notional
MAX_POSITION_SIZE_PCT = 0.02   -> caps notional at 2%
                                  => realised risk 0.12%, not 0.5%
```

The **notional** cap binds long before the **risk** cap, so the sizer never
approaches the risk budget it was given. On daily bars, where stops are
necessarily wide, this is not a corner case — it is every trade.

This is a **risk-limit** question and slice 9 deliberately did not touch it.
It is written down here because it is the single largest lever on the result
and because changing it is a decision for a human, not a tuning step. Note also
that raising the notional cap raises *real* risk: the current result's tiny
drawdowns (max 2.26%) are tiny partly because the positions are.

---

## 7. Three defects found on the way

**A protective stop placed the wrong side of its own fill.** The exit plan is
built from the signal bar's close; the fill happens on the next bar. When the
market gapped through the planned stop, the stop was placed unmodified — on a
position already beyond it — and fired immediately. A guaranteed −1R that no
signal quality could rescue. `TradingEngine._reanchor` now shifts the whole
bracket to the actual fill, **preserving the stop distance rather than the stop
price**, so the risk unit the sizer worked with survives contact with the fill.
A fill that cannot be repaired inside the plan is closed at market rather than
held behind a fictional stop.

**Dust broke reconciliation, and the break grew with the price.** A partial
exit leaving a remainder below the exchange minimum was closed on the books —
correctly, because it can be neither sold nor protected — but the exchange
still held it. On daily data that divergence was reported **317 times**, and it
appeared only once BTC passed ~$55k, because a fixed quantity of dust crosses a
fixed minimum *notional* as the asset appreciates. Dust is now **booked** to a
persisted per-symbol ledger and included in every reconciliation:
`book/exchange breaks: 0`. The system still cannot trade the dust; it can now
account for it, which is a different and achievable promise.

**The backtester had no way to diagnose a payoff problem.** It reported return
and win rate — the two numbers that, in slice 8, were individually unremarkable
and jointly impossible. `trade_stats.py` now reports R-multiples, the
break-even hit rate implied by the observed payoff, expectancy with a bootstrap
confidence interval, cost drag in R, and time-to-resolution split by outcome.
The line that would have diagnosed slice 8 in one glance:

```
hit rate 54.2%   break-even hit 66.7%   ->  -12.5 pts BELOW break-even
```

---

## 8. What to do next, in order

1. **Resolve the sizing inconsistency** (§6). It is the largest lever and it is
   a human decision about risk appetite, not an optimisation.
2. **Test the fold-1 anomaly.** A long-only strategy that made money in a −38%
   period is either a real counter-trend effect or 28 trades of luck. More
   assets and more history will separate them; nothing else will.
3. **Add assets.** Every number here is one asset, one venue, one direction.
   ETH, SOL and a non-crypto control would say whether any of it generalises.
4. **Only then retrain the policy** — on daily bars, against the new base rate.
   The slice-8 model was trained on hourly data under a geometry with −0.60R of
   cost drag; it was being asked to predict a coin flip that cost 60 bps to
   call. Retraining under a working geometry is the first version of that
   question worth asking.
5. **Do not** lower a promotion threshold, widen a risk limit to make a return
   look better, or read §5 as permission to deploy. The gates being green means
   the arithmetic is no longer against you. It does not mean there is an edge.
