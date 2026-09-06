MISSION — SLICE 70
One more closed day. Measure. Do not invent a business.

You are a senior trading-systems / risk engineer.
Execute STEP 0→6 in order. Fail closed. Design before mutation. Bytes beat prose.

BINDING
- Signal: funding_carry_fade_btc_v1. Do NOT re-score OOS (n=41, 95.13/96.0).
- t1 = 2026-08-09. Forward = closed bars strictly after t1.
- Expect after_t1 = 8 (2026-08-10 … 2026-08-17). NO 2026-08-18.
- linear n expected 1469. Pack base = slice 69.
- Ceiling = max(0, N-5) = 3. Declare BEFORE scores.
- Join = last funding at-or-before BAR CLOSE. Do not rescue 08-12 00:00.
- FUND_ABS = 0.0001 frozen. Caps $100 / 1 / 1.
  Fingerprint 662de0115880871352d5d623b1020eaa.
- is_forward_observation = (forward_n_trades > 0)
- live = false. models/current absent. closer_to_autonomous_profit_agent = NO
- 11 freezes stay closed. No new signal. No alts. No Colab.

STEPS
0 Baseline + pack integrity (restore if amputated to an older tree)
1 Design note: ceiling=3 before any score
2 Freshness JSON from FILES (note path = HUMAN_DATA_NOTE_SLICE70.md)
   Finding text must say N=8, not leftover “six/seven bars”
3 Forward shadow once; per-bar close-join setups; observed ≤ ceiling
4 Gate REFUSE
5 Full pytest
6 Verdict: N, dates, grew?, ceiling, trades, setups, oos_re_scored=NO,
   live=NO, closer=NO

PASS = honest 8 days + honest 0-or-more trades + ceiling 3 + gate locked
FAIL = open bar, fake forward, re-score, live/model, join/FUND_ABS change

Expected if pack correct:
  after_t1_linear = 8
  the_window_grew = true
  ceiling_max_possible_trades = 3
  forward_n_trades = 0 or >=1 (08-17 close-join was 9.2e-5 — likely still 0)
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Win this slice = truth. Not a fill. Not autonomy. Not “dominate.”
