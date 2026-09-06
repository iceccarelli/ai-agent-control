# HUMAN DATA NOTE — for Slice 61

- t1 (measure end): **2026-08-09T00:00:00Z** (unchanged)
- Product: `funding_carry_fade_btc_v1` only
- Extended files:
  - `data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz`
  - `data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz`
- `synthetic: false` preserved; **no rewrite** of history before t1
- New **closed** daily bars after 2026-08-09: see verification output
- Fetch: public Binance USDT-M REST (`/fapi/v1/klines`, `/fapi/v1/fundingRate`)
- Caps / monitors / promotion gate policy: **unchanged** ($100, M1–M4 frozen)
- Live / model: still **NO** — this pack only enables true forward shadow counting

