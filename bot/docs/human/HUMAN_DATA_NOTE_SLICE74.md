# HUMAN DATA NOTE — Slice 74

- t1: **2026-08-09T00:00:00Z**
- Product: `funding_carry_fade_btc_v1` only
- Closed linear bars after t1: **12** → ['2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14', '2026-08-15', '2026-08-16', '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20', '2026-08-21']
- linear rows: 1473
- linear sha256 (compressed): `b4dc07bcd6f4a6e20ca952c527067856f6640fb1ff990aec1e1b719a28165b11`
- funding rows: 4422 last 2026-08-22T16:00:00+00:00
- funding sha256 uncompressed: `3d5c95bff85392397dc8ef6ee6e40176448dec17f3c6af7043b80da664f98bc0`
- synthetic: false; append-only; no pre-t1 rewrite
- START_MS used: 1787270400000 (2026-08-21 00:00 UTC). 08-22 NOT appended as linear.
- Ceiling: max(0, 12-5) = **7**
- 08-19 remains SETUP+FLAG in slice-73 artefacts. It is NOT scoreable until last bar ≥ 2026-08-26.
- 08-21 16:00 close-join candidate in this file: 0.00010000
- A flag is not an entry. An entry is not a closed trade.
- Live / model / autonomy / Bybit / ETH-SOL: **NO**
- Pack base: **tradingbot_slice73.zip**
- Agent verifies FILES + hashes, not this note
