MISSION — SLICE 61: TRUE FORWARD SHADOW AFTER t1

You are a senior trading-systems / risk engineer under bank-grade discipline.

You do not invent bars. You do not re-score Stage-1.
You do not silence M4. You do not train models. You do not arm live.
You do not reopen FROZEN_ABSENT families. You do not raise notional.

Fail closed. Design before mutation. Prove with paths, tests, artefact JSON.

═══════════════════════════════════════════════════════════════
HUMAN DECISIONS (binding)
═══════════════════════════════════════════════════════════════
1. cleared_edge_signal remains funding_carry_fade_btc_v1.
   OOS evidence frozen (n=41, M1≈95.13, M2=96.0). Do NOT re-score.

2. t1 measure end = 2026-08-09T00:00:00Z.
   Forward = closed decision bars STRICTLY AFTER t1 only.

3. Human supplied extended real BTCUSDT linear 1d + funding
   (see HUMAN_DATA_NOTE_SLICE61.md). synthetic=false. No history rewrite.

4. Shadow pack frozen:
   - fingerprint 662de0115880871352d5d623b1020eaa
   - one_entry_per_contiguous_run
   - caps $100 / 1 pos / 1 entry/day
   - M1–M4 thresholds UNCHANGED

5. promotion_gate_allows_live must stay False this slice
   (20 forward trades + 180 days + human items not met by a few new days).

6. closer_to_autonomous_profit_agent: NO

═══════════════════════════════════════════════════════════════
FORBIDDEN
═══════════════════════════════════════════════════════════════
- Fake forward labels on bars ≤ t1
- Stage-1 / OOS re-score
- Threshold / cap / schedule / constant changes
- Live, models/current, training
- Reopening frozen families
- Treating shadow mean as Stage-1

═══════════════════════════════════════════════════════════════
STEPS
═══════════════════════════════════════════════════════════════
STEP 0 — Baseline from this tree
  pytest counts; print_project_status; confirm clear + 11 freezes;
  confirm OOS artefact unchanged; record linear/funding last timestamps;
  confirm HUMAN_DATA_NOTE_SLICE61.md present.

STEP 1 — Design note before code (EDGE.md)
  True forward only after t1; HOLD if still 0; no live/model.

STEP 2 — Data freshness
  artifacts/slice61_data_freshness.json
  new_linear_bars_count, new_funding_prints_count
  extension_present true only if closed linear bars > t1 exist
  If false → HOLD, is_forward_observation false, do not invent.

STEP 3 — Forward shadow (only if extension_present)
  artifacts/slice61_forward_shadow.json
  - window / trades with entry STRICTLY after t1 counted as forward
  - schedule one-per-run; caps 100/1/1; fingerprint match
  - monitors M1–M4 unchanged thresholds
  - is_forward_observation true only if forward_observations_to_date > 0
  - registration_eligible false; is_stage1_evidence false
  - report monitor_status; leave M4 logic unmoved

STEP 4 — Promotion gate re-eval → must allow_live False

STEP 5 — Regression: full pytest; paper cert; no models/current

STEP 6 — Verdict block:
  SLICE61_VERDICT: PASS | FAIL | PASS_WITH_DEFECTS
  extension_present: YES/NO
  forward_observations_to_date: N
  is_forward_observation: true|false
  oos_re_scored: NO
  monitor_status: …
  promotion_gate_allows_live: NO
  closer_to_autonomous_profit_agent: NO

PASS if: honest forward metrics on real post-t1 bars (or honest HOLD if
human data missing), gate REFUSE, clear intact, suite green.
FAIL if: invented bars, false forward, re-score, live/model, threshold moves.

Execute STEP 0→6. Prove with paths and JSON. Be ruthless.
