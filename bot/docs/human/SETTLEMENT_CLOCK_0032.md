# SETTLEMENT CLOCK — what the carry book looks like at 00/08/16 UTC

Written 2026-09-11 by 0032. Measurement, not a decision. Nothing here is
quotable; the tool prints why.

## Corpus

`data/real_bybit_btc_4h/` — promoted from the 0028 study set by
`tools/promote_bybit_study_set.py`. Spot, perp and funding are all **Bybit**,
the venue `CarryBroker` trades on. Price at a settlement = close of the 4h bar
ending at it (0027's convention).

| File | Rows | sha256 of content |
|---|---:|---|
| `ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz` | 8,999 | `d8736429ccd140d31a65608d3f9ab04f429d5dc791f09c4b325bac9ecccbc3d5` |
| `ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz` | 8,999 | `6d8ec4a20f453662455e1a1bb5b58dfb62fc3818a377bf4285b142e3975a6b19` |
| `funding/BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz` | 4,600 | `8fbe00f6702de1db03be5ea17375084c383bdc8ce8ddfb38f293f6932d4d9546` |

The study set carried one **open 4h bar** in each kline file (opened
2026-09-09 08:00, observed 10:43:53 — 2h44m into a 4h bar). It was dropped,
never written as a close, and the drop is in the manifest.

## Result

```
python3 tools/carry_backtest.py --repo . --clock 8h --matrix
```

Window 2022-08-01T16:00Z .. 2026-09-09T08:00Z, 4,500 prints, $100k,
impact 1.09 bps/leg (1.00 DECLARED floor + 0.09 walk through the thinnest
measured 1% band), all taker.

| borrow | UNGATED | GATED (in-sample) |
|---:|---|---|
| 0% | **+2.24%/yr** n=87 | +3.96%/yr n=66 |
| 3% | −0.90%/yr n=87 | +2.60%/yr n=49 |
| 5% | −2.99%/yr n=87 | +1.70%/yr n=40 |
| 8% | −6.13%/yr n=87 | +2.35%/yr n=6 |

Against the daily-close Binance matrix (PHASE1_DECISION): 0% ungated
**+9.49% → +2.24%**.

Ungated, 5% borrow, the identity:

```
funding  +39,411.36
basis       +866.55   = spot leg +164,675.13 + perp leg −163,808.58
fees     −27,232.18
borrow   −21,490.23
impact    −3,841.76
other         0.00
= NET    −12,286.27   residual 1.6e-11
```

## Why the number fell — it is the exit rule, counted in its own units

`NEGATIVE_FUNDING_EXIT_PRINTS = 3` means three consecutive negative **prints**
(one day). The daily simulation could only see the last print of each day,
so it needed three negative **days** to exit. On the settlement clock the rule
does what its docstring says:

- 92 runs of ≥3 consecutive negative prints in 4,500 (808 negative prints)
- 86 of 87 trades exit on `FUNDING_INVERTED`
- median hold **5.3 days** (mean 16.0); `carry_costs.ASSUMED_HOLD_DAYS` is 30
- median funding per trade **$27**; fees per trade **$313**

`carry_costs`' own docstring: *"At a 7-day hold the round trip alone is 4.43
bps/day … the trade cannot pay for its own entry."* The engine's exit rule
produces a 5-day median hold. **As specified, the book is a fee generator on
the clock it actually runs on.**

Not done, deliberately: retuning `NEGATIVE_FUNDING_EXIT_PRINTS`,
`ASSUMED_HOLD_DAYS` or anything else to make this table prettier (rule 20).
Every window from 2022-06-29 to 2026-09-09 has now been read and recorded;
any changed exit rule is a NEW hypothesis, registered before it is scored,
and scored only on settlements after 2026-09-09T08:00Z.

## Stress (a pair already on, held through the window)

```
python3 tools/carry_backtest.py --repo . --clock 8h --stress
```

| Window | Status | Pair NET on $100k | Short-leg adverse (4h high) |
|---|---|---:|---:|
| 2020-03-12 | DATA_ABSENT — the same-venue corpus starts 2022-08-01; nothing substituted | — | — |
| 2022-11-06..14 FTX | measured; 16 negative prints; basis −12.1..+19.5 bps | −361.76 | +0.26% |
| 2024-08-03..07 | measured; 1m layer is Binance (cross-venue) | −50.54 | +1.13% |
| 2025-02-02..04 | measured | −25.73 | +1.87% |
| 2026-04-14..16 | measured | −67.51 | +2.19% |
| USDT depeg | NOT_MEASURABLE — all P&L is USDT; no USDT/USD series in the tree | — | — |

Close survival is not wick survival. A 4h high is a floor on the wick; a 1m
high is a tighter floor; neither is the tick the liquidation engine sees.

## Records

- `artifacts/data_read_ledger.json`: every read above, plus the 0018–0031
  carry_backtest and 0028 venue_study reads that were never recorded
  (INVENTORY D6), marked retroactive.
- `artifacts/hypothesis_registry.json`: `carry_ungated_v1@BTCUSDT` (measured)
  and `carry_cost_gates_v1@BTCUSDT` (measured_in_sample; forward-only holdout).
  31 trials; family-wise false-positive rate 79.6%.
