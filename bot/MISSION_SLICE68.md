MISSION — SLICE 68: FIRST STRUCTURAL NON-ZERO CEILING (after_t1 = 6)

You are a senior trading-systems / risk engineer under bank-grade discipline.
Execute STEP 0→6 in order. Full compute. Fail closed. Design before mutation.
Prove every claim with paths, hashes, artefact JSON.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT. You do not raise notional.

═══════════════════════════════════════════════════════════════
PURPOSE
═══════════════════════════════════════════════════════════════
| Slice | after_t1 | Ceiling | Trades |
|---|---|---|---|
| 62–67 | 1→5 | 0 | 0 |
| 68 | THIS | expect 1 | 0 or ≥1 |

Human claims linear corpus through **2026-08-15** (after_t1 = 6).
This is the FIRST slice where max(0, N-HORIZON) can be **1**.
A completed forward trade is structurally allowed IF funding_setup fires.
Zero trades at ceiling 1 is valid information (rule quiet) — not a defect.

═══════════════════════════════════════════════════════════════
HUMAN DECISIONS (binding)
═══════════════════════════════════════════════════════════════
1. cleared_edge_signal = funding_carry_fade_btc_v1
   OOS frozen: n=41, M1≈95.13, M2=96.0. Do NOT re-score.
   Artefact: artifacts/slice57_oos_edge_BTCUSDT_summary.json
2. t1 = 2026-08-09T00:00:00Z. Forward = closed bars STRICTLY AFTER t1 only.
3. Human extended real BTCUSDT linear 1d + funding.
   docs/human/HUMAN_DATA_NOTE_SLICE68.md is prose only.
   Verify via FILE sha + last dates + after_t1 count.
   Expect after_t1_linear >= 6 including:
     2026-08-10, 2026-08-11, 2026-08-12, 2026-08-13, 2026-08-14, 2026-08-15
   No open 2026-08-16 bar.
4. Shadow pack FROZEN:
   fingerprint 662de0115880871352d5d623b1020eaa
   one_entry_per_contiguous_run
   caps $100 / 1 pos / 1 entry/day
   M1–M4 thresholds UNCHANGED
5. Labels:
   extension_present = (after_t1_linear >= 1)
   is_forward_observation = (forward_n_trades > 0)
   Ceiling: max(0, N - 5) → N=6 ⇒ ceiling = 1
   Declare ceiling BEFORE scoring.
   observed <= ceiling; if exceeded → FAIL (bug).
6. promotion_gate_allows_live must stay False
   (≥20 forward trades AND ≥180 forward days + human items).
7. closer_to_autonomous_profit_agent: NO
8. No model training. No live. No Colab weights in models/current.

FORBIDDEN
Invented bars; false forward; Stage-1 re-score; live; model; threshold moves;
exit clamping; claiming ceiling 0 when N=6; autonomy YES; parameter rescue.

STEPS
0 Baseline: pytest; print_project_status; 11 freezes; OOS unchanged;
  disk after_t1 + sha256; gate allows_live False.
  If pack regression, restore byte-identically and record restore artefact.
1 Design note before code — declare ceiling=1 before any score.
2 artifacts/slice68_data_freshness.json — FILES only; delta vs 67 (or 66);
  prefix-append integrity; bars_fabricated=0; BTC-only manifest discipline.
3 artifacts/slice68_forward_shadow.json — frozen pack; forward after t1;
  ceiling=1; forward_n_trades; is_forward_observation only if trades > 0;
  per-bar funding_setup_present; no exit clamping;
  monitors honest; registration_eligible false; is_stage1_evidence false.
4 promotion_gate → allows_live False
5 Full pytest; paper cert; no models/current
6 Verdict:
   SLICE68_VERDICT / extension_present / after_t1_linear / dates /
   new_linear_bars_since_prior / the_window_grew /
   forward_n_trades / is_forward_observation /
   ceiling_max_possible_trades / within_ceiling /
   oos_re_scored: NO / promotion_gate_allows_live: NO /
   closer_to_autonomous_profit_agent: NO

PASS if honest growth to 6 + honest trade count (0 or more), gate REFUSE,
clear intact, suite green, ceiling correctly 1.
FAIL if invented bars, false forward, re-score, live/model, wrong ceiling.

Expected if pack correct:
  after_t1_linear = 6
  the_window_grew = true
  ceiling_max_possible_trades = 1
  forward_n_trades = 0 or >=1 (both valid)
  is_forward_observation = (forward_n_trades > 0)
  promotion_gate_allows_live = false
  closer_to_autonomous_profit_agent = false

Execute STEP 0→6. Prove with paths and JSON. Be ruthless.
