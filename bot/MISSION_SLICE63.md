MISSION — SLICE 63: FORWARD SHADOW REFRESH ON GROWING POST-t1 CORPUS

You are a senior trading-systems / risk engineer under bank-grade discipline.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.

Fail closed. Design before mutation. Prove with paths, hashes, artefact JSON.

HUMAN DECISIONS (binding)
1. cleared_edge_signal = funding_carry_fade_btc_v1
   OOS frozen: n=41, M1≈95.13, M2=96.0. Do NOT re-score.
2. t1 = 2026-08-09T00:00:00Z.
   Forward = closed decision bars STRICTLY AFTER t1 only.
3. Human extended real BTCUSDT linear 1d + funding
   (docs/human/HUMAN_DATA_NOTE_SLICE63.md). Verify via FILE sha + last dates
   + after_t1 count — not the note alone.
4. Shadow pack FROZEN:
   fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run
   caps $100 / 1 pos / 1 entry/day
   M1–M4 thresholds UNCHANGED
5. Label rule (locked in slice 62):
   is_forward_observation = (forward_n_trades > 0)
   extension_present = (after_t1_linear >= 1)
   A bar is not a trade. Horizon 5 ⇒ ~6 closed forward bars before one trade
   can complete. Zero trades with few bars is expected, not a defect.
6. promotion_gate_allows_live must stay False
   (need ≥20 forward closed trades AND ≥180 forward days + human items).
7. closer_to_autonomous_profit_agent: NO

FORBIDDEN
Invented bars; false forward labels on ≤t1 data; Stage-1 re-score;
threshold/cap/schedule/constant changes; live; models/current; training;
frozen family reopen; treating shadow mean as Stage-1.

STEPS
0 Baseline: pytest counts; print_project_status; 11 freezes; OOS unchanged;
  disk after_t1 count + sha256; gate allows_live False.
1 Design note before code.
2 artifacts/slice63_data_freshness.json — extension from FILES only;
  prefix-append integrity if asserted.
3 artifacts/slice63_forward_shadow.json
   - same pack/fingerprint/schedule/caps
   - forward segment strictly after t1
   - report ceiling (max possible trades given bar count)
   - forward_n_trades; is_forward_observation only if trades > 0
   - monitors: insufficient data if sample too small; do not rebrand
     historical M4 WARN as forward
   - registration_eligible false; is_stage1_evidence false
4 promotion_gate → allows_live False
5 Full pytest; paper cert; no models/current
6 Verdict block:
   SLICE63_VERDICT / extension_present / after_t1_linear /
   forward_n_trades / is_forward_observation / oos_re_scored: NO /
   promotion_gate_allows_live: NO / closer_to_autonomous_profit_agent: NO

PASS if honest extension + honest forward count (even if 0), gate REFUSE,
clear intact, suite green.
FAIL if invented bars, false forward, re-score, live/model, pack mutated.

Execute STEP 0→6. Prove with paths and JSON. Be ruthless.
