MISSION — SLICE 66: FORWARD SHADOW ON GROWN POST-t1 WINDOW (4 DAYS)

You are a senior trading-systems / risk engineer under bank-grade discipline.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.

Fail closed. Design before mutation. Prove with paths, hashes, artefact JSON.

HUMAN DECISIONS (binding)
1. cleared_edge_signal = funding_carry_fade_btc_v1
   OOS frozen: n=41, M1≈95.13, M2=96.0. Do NOT re-score.
   Artefact: artifacts/slice57_oos_edge_BTCUSDT_summary.json
2. t1 = 2026-08-09T00:00:00Z (folds lock unmodified).
   Forward = closed decision bars STRICTLY AFTER t1 only.
3. Human extended real BTCUSDT linear 1d + funding.
   docs/human/HUMAN_DATA_NOTE_SLICE66.md is prose only.
   Verify via FILE sha + last dates + after_t1 count.
   Expect after_t1_linear >= 4 including:
     2026-08-10, 2026-08-11, 2026-08-12, 2026-08-13
   linear rows expected 1465; no open 2026-08-14 bar.
4. Shadow pack FROZEN:
   fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run
   caps $100 / 1 pos / 1 entry/day
   M1–M4 thresholds UNCHANGED
5. Labels:
   extension_present = (after_t1_linear >= 1)
   is_forward_observation = (forward_n_trades > 0)
   Report delta vs slice 65 (window grew?).
   Ceiling from bar count; observed <= ceiling.
   ~6 closed forward bars needed before one trade can complete.
   With 4–5 days, forward_n_trades may still be 0 — VALID.
6. promotion_gate_allows_live must stay False
   (≥20 forward trades AND ≥180 forward days + human items).
7. closer_to_autonomous_profit_agent: NO

FORBIDDEN
Invented bars; false forward on ≤t1 data; Stage-1 re-score;
threshold/cap/schedule/constant changes; live; models/current; training;
frozen reopen; exit clamping; shadow mean as Stage-1; fake growth.

STEPS
0 Baseline: pytest; print_project_status; 11 freezes; OOS unchanged;
  disk after_t1 + sha256; gate allows_live False.
  If pack regression, restore byte-identically and record restore artefact.
1 Design note before code.
2 artifacts/slice66_data_freshness.json — FILES only; delta vs 65;
  prefix-append integrity; bars_fabricated=0.
3 artifacts/slice66_forward_shadow.json — frozen pack; forward after t1;
  ceiling; forward_n_trades; is_forward_observation only if trades > 0;
  per-bar funding_setup_present; no exit clamping;
  monitors honest; registration_eligible false; is_stage1_evidence false.
4 promotion_gate → allows_live False
5 Full pytest; paper cert; no models/current
6 Verdict:
   SLICE66_VERDICT / extension_present / after_t1_linear / dates /
   new_linear_bars_since_slice65 / the_window_grew /
   forward_n_trades / is_forward_observation /
   ceiling_max_possible_trades / within_ceiling /
   oos_re_scored: NO / promotion_gate_allows_live: NO /
   closer_to_autonomous_profit_agent: NO

PASS if honest growth + honest forward count (even 0), gate REFUSE,
clear intact, suite green.
FAIL if invented bars, false forward, re-score, live/model, pack mutated.

Execute STEP 0→6. Prove with paths and JSON. Be ruthless.
