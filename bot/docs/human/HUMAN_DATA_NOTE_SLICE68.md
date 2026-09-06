# HUMAN DATA NOTE — Slice 68

- t1: **2026-08-09T00:00:00Z** (unchanged)
- Product: `funding_carry_fade_btc_v1` only
- Closed linear bars after t1: **6** → ['2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-14', '2026-08-15']
- linear rows: 1467
- linear sha256: `f7acf20712fcbee9e48ce2b0143849f8746f1c1a72cb5ebdd8d5bc933bf8e259`
- synthetic: false; append-only; no pre-t1 rewrite
- **FIRST structural non-zero ceiling:** max(0, 6-5) = **1**
- Trade still requires funding_setup (|rate| ≥ FUND_ABS); zero trades still valid if quiet
- Live / model / autonomy: still **NO**
- Agent must verify from files + hashes, not this note
- Pack: catch-up after skipped Sat pack; includes closed 2026-08-15
