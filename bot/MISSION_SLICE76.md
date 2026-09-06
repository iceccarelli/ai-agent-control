MISSION — SLICE 76: CATCH-UP TWO CLOSED DAYS (08-23 AND 08-24). MEASURE. DO NOT INVENT A BUSINESS.
You are a senior trading-systems / risk engineer under bank-grade discipline.
Execute STEP 0→6 in order. Fail closed. Design before mutation. Bytes beat prose.
You do not invent bars. You do not re-score Stage-1. You do not train models.
You do not arm live. You do not move FUND_ABS. You do not append 2026-08-25.
BINDING
1. cleared_edge_signal = funding_carry_fade_btc_v1. Do NOT re-score OOS
   (n=41, M1≈95.13, M2=96.0, sha 28b7dfe0…).
2. t1 = 2026-08-09. Forward = closed bars strictly after t1.
3. Expect after_t1 = 15 (2026-08-10 … 2026-08-24). linear n = 1476.
   NO 2026-08-25 linear. Pack base = slice 75.
4. Predecessor linear uncompressed (slice-75 CURRENT, not the buggy field):
   6e64847c54991ef38c0aaaad3095446ea955c822e07f14938ff6943c8c93b0a7
   Never self-compare. identical and differs must not both be true.
5. Ceiling = max(0, N-5) = 10. Declare BEFORE scores.
6. Join = last funding at-or-before BAR CLOSE. FUND_ABS = 0.0001 frozen.
   Fingerprint 662de0115880871352d5d623b1020eaa. Caps $100 / 1 / 1.
7. is_forward_observation = (forward_n_trades > 0)
8. live = false. models/current absent. closer_to_autonomous_profit_agent = NO
9. 11 freezes stay closed. No new signal. No alts. No Colab.
10. 08-19 still NOT scoreable until last >= 2026-08-26. Recompute gap.
STEPS
0 Baseline + pack integrity. If after_t1 still 13: HOLD, do not invent.
1 Design note: ceiling=10 before any score. Catch-up is two closed bars.
2 Freshness JSON from FILES; predecessor = 6e64847c…; bars_fabricated=0.
3 Forward shadow once; per-bar close-join; observed <= ceiling;
  registration_eligible false; is_stage1_evidence false.
4 Gate REFUSE. Fix why_not_closer if it still says "One post-t1 day."
5 Full pytest; amend EXPIRES tests, do not rename them.
6 Verdict: N, dates, grew?, ceiling, trades, setups, oos_re_scored=NO,
  live=NO, closer=NO, predecessor_self_compared=NO
PASS = honest 15 days + honest 0-or-more trades + ceiling 10 + gate locked
FAIL = open 08-25 bar, fake forward, re-score, live/model, digest self-compare
Expected if pack correct:
  after_t1_linear = 15
  the_window_grew = true
  ceiling_max_possible_trades = 10
  forward_n_trades = 0
  is_forward_observation = false
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false
Win this slice = truth. Not a fill. Not autonomy.
Execute STEP 0→6. Be ruthless.
