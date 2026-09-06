# REAL market data — BITSTAMP BTC/USD

**This is real exchange data, not synthetic.** It is the corpus to evaluate and
train against. `../README.md` describes the synthetic corpus, which exists only
to exercise the machinery offline.

## Provenance

| | |
|---|---|
| Source | local file /tmp/bts/data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz |
| File | `btcusd_bitstamp_1min_2012-2025.csv.gz` |
| Licence | MIT |
| Source sha256 | `46fe0bbe0491b33b8e85dfec079d6b96049705e87892e985a13ef43ff8cafbde` |
| Exchange | BITSTAMP |
| Instrument | BTC/USD |
| Native resolution | 1m |
| Produced by | `tools/fetch_real_data.py` |

## What it covers

- **15,379 bars** at 14400s
- 2018-01-01T00:00:00.0000000Z to 2025-01-07T04:00:00.0000000Z
- completeness **100.0000%** (0 missing buckets, left as gaps and never filled)

- 2020-03 covid crash: **present**
- 2022 bear (full year): **present**
- 2021 bull: **present**

## Three things this corpus is not

1. **It is not Bybit.** Bitstamp BTC/USD is a different venue with different
   liquidity and a different fee schedule. The price series is genuine; the
   execution assumptions applied to it in the backtest are Bybit's.
2. **It has no order book.** OHLC only. `_gate_book_liquidity` abstains here,
   and `load_corpus` says so in its notes. Real multi-year L2 archives are a
   paid product and are not worth buying before a model on OHLCV alone shows
   positive expectancy under realistic costs.
3. **`trades_count` is 0**, because the source does not carry it. It is not
   invented, and nothing in this codebase reads it.

## Rebuild

```bash
python3 tools/fetch_real_data.py --from 2018-01-01 --interval 1h
python3 -c "import market_data; print(market_data.verify_manifest('data/real'))"
python3 tools/run_evaluation.py --data-dir data/real
```
