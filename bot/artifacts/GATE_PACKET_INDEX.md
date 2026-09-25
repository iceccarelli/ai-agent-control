# GATE PACKET INDEX — 8 items, owner, state, evidence

> Facts only, read from `artifacts/slice59_promotion_gate.json`,
> `artifacts/kill_switch_drill.json`, and `artifacts/forward_shadow_current.json`
> (the 46-bar factory copy installed this session). No decision fields, no
> signatures. `promotion_gate.evaluate_promotion_gate()` remains the one
> authority — this table restates its inputs, it does not replace it.
>
> `artifacts/control_plane_tick.json` is **not present in this checkout**
> (cloud sandbox never runs the money-path tick — see `docs/human/NO_GLUE_OPS.md`
> item 9). Rows below cite the gate JSON and the drill transcript directly
> instead.

| # | item | owner | state | evidence path |
|---|---|---|---|---|
| 1 | `human_risk_memo_signed` | human | **incomplete** | no memo path recorded — draft at `docs/promotion/RISK_MEMO_MICRO_LIVE_DRAFT.md` (unsigned, refreshed to 3/20, 46/180). Ops steps for the sign-off itself: `artifacts/HUMAN_GATE_OPS_PACKET.md` Part 2 — the packet does not complete this item, only a signed memo does |
| 2 | `forward_shadow_clean` | observation | **incomplete** | `artifacts/forward_shadow_current.json` — 3 of 20 forward closed trades, 46 of 180 forward days, ladder 9/9/8/4/4/3. `artifacts/TRADE_SCARCITY_AUTOPSY.md` (this session): VERDICT DESIGNED_RARITY |
| 3 | `m4_recent_half_accepted_or_recovered` | human or observation | **incomplete** | historical M-4 reads WARN (earlier half +0.4669/17, recent half −0.0798/18); forward M-4 reads `INSUFFICIENT_DATA` (needs 20 closed trades, has 3). Not accepted in any signed memo, not recovered |
| 4 | `kill_switch_drill_recorded` | human | **complete** | `artifacts/kill_switch_drill.json` — 9/9 steps `as_scripted`, trip → entries blocked (`KILL_SWITCH_ENGAGED`) + `should_halt_trading` true + health 503 → cleared with the exact `HUMAN_CLEARED_KILL_SWITCH` token → disengaged. Scratch database only. Witnessed by Vincenzo Ceccarelli, 2026-09-16 |
| 5 | `linear_protective_stop_verified` | human | **incomplete** | not documented. See `artifacts/LINEAR_STOP_VENUE_GAP.md` and `docs/promotion/LINEAR_STOP_OPS_INVENTORY.md` — simulator half done (`test_linear_simulator.py`, 25 tests), all 4 venue items open. Ops steps: `artifacts/HUMAN_GATE_OPS_PACKET.md` Part 1 — the packet does not complete this item, only a signed checklist does |
| 6 | `notional_cap_within_policy` | machine | **complete** | `shadow.SHADOW_MAX_NOTIONAL_USD = 100.00`; re-derived from the live tree by the gate, never trusted from the JSON |
| 7 | `live_trading_ack_present` | human | **incomplete** (absent — correct) | `LIVE_TRADING_ACK` not satisfied; `live_authorized: false` in `forward_shadow_current.json` |
| 8 | `models_current_absent_or_contained` | machine | **complete** | `models/current` absent; re-derived from the live tree by the gate |

**3 of 8 complete. `promotion_gate_allows_live()` returns `False`.**

Fail-closed properties (`docs/PROMOTION_GATE_MICRO_LIVE.md`, unchanged by this
session): a missing or unreadable gate file refuses; an item the file doesn't
mention refuses; flipping every bool in the file still refuses because the
gate also independently requires `config.is_live_authorized`; the two machine
items are re-derived from the live tree, not read from JSON, so a hand-edited
file cannot assert them falsely.
