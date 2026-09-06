# HUMAN DATA NOTE — Slice 75

- t1: **2026-08-09T00:00:00Z**
- Product: `funding_carry_fade_btc_v1` only
- Closed linear bars after t1: **13** → ['2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14', '2026-08-15', '2026-08-16', '2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20', '2026-08-21', '2026-08-22']
- linear rows: 1474
- linear sha256 (compressed): `7fadf2656ca4855c9fab2df11bb549e4fd790c6ee19f5d9178fafb5408de21df`
- funding rows: 4424 last 2026-08-23T08:00:00+00:00
- funding sha256 uncompressed: `fec0ee8c33b528cb0baeda4de0c3d5b0c3bb15a144bc5537cd379e3bda4219b2`
- synthetic: false; append-only; no pre-t1 rewrite
- START_MS used: 1787356800000 (2026-08-22 00:00 UTC). 08-23 NOT appended as linear.
- Ceiling: max(0, 13-5) = **8**
- 08-19 remains SETUP+FLAG. It is NOT scoreable until last bar ≥ 2026-08-26 (four more closed days from 08-22: 23,24,25,26). Compute the gap from the file, not from leftover prose.
- 08-21 was a last-bar SETUP in slice 74. With 08-22 present it should become a FLAG. Still not scoreable until last ≥ 2026-08-28.
- 08-22 16:00 close-join candidate in this file: 0.00010000
- FUND_ABS=0.0001 is the venue modal/base rate (~26.6% of prints). Language correction only. Do NOT move the bar.
- A flag is not an entry. An entry is not a closed trade.
- Live / model / autonomy / Bybit / ETH-SOL: **NO**
- Pack base: **tradingbot_slice74.zip**
- Agent verifies FILES + hashes, not this note
