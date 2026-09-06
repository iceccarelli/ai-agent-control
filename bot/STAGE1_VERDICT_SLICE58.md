# SLICE 58 — VERDICT

```
SLICE58_VERDICT:                    PASS
cleared_edge_signal:                funding_carry_fade_btc_v1
oos_artefacts_intact:               YES  (n=41, M1 95.13, M2 96.0, mean R +0.1736,
                                          control_validated true — not re-scored)
constants_unchanged:                YES  (fingerprint 662de0115880871352d5d623b1020eaa)
shadow_path_wired:                  YES
live_authorized:                    false
models_current_present:             false
hard_caps_enforced_in_tests:        YES  (12 tests)
decay_monitors_predeclared:         YES  (4 monitors, thresholds in EDGE §41e
                                          before shadow.py existed)
revoke_hook_present:                YES  (human-only, 11 tests)
eleven_freezes_intact:              YES
Closer to autonomous profit agent?: NO
Closer to post-POSITIVE
  operational readiness?:           YES
```

## Evidence table

| claim | observed | file / command |
|---|---|---|
| the clear is slice 57's, unrescored | n=41, M1 95.13, M2 96.0, mean R +0.1736, control true, folds hash matches | `artifacts/slice57_oos_edge_BTCUSDT_summary.json` |
| no OOS re-run, no fold re-cut | no `slice58_*edge*` or `*control*` artefact exists | `ls artifacts/slice58_*` |
| constants byte-frozen | fingerprint `662de011…` asserted against the pinned value | `TestTheShadowedRuleIsTheClearedRule` |
| design note precedes the wiring | EDGE §41 commit `--stat` lists `EDGE.md` only; `shadow.py` absent | `git show --stat` |
| caps declared before implementation | 1 position / 1 per day / 100.00 USD / BTCUSDT, in §41d | `artifacts/slice58_shadow_pack.json` |
| caps enforced | second position, second same-day entry, 100.01 USD, ETH/SOL/BTCUSD/empty symbol, zero and negative notional all refused; exactly 100.00 allowed | `TestTheHardCapsAreEnforced` (12 tests) |
| the notional cap is fixed, not a fraction | float literal; no equity term near it | `test_the_notional_cap_is_a_fixed_dollar_figure` |
| monitors pre-declared | four, thresholds in §41e before `shadow.py` existed | `test_every_threshold_matches_the_design_note` |
| thresholds match their stated derivations | −0.25 beyond the worst fold (−0.2124); 0.60 above the OOS sample's own 0.41 | `test_the_alert_threshold_is_worse_than_this_rules_worst_fold` |
| monitors are pure | `asof` injected, never `date.today()`; M-2 keys on the exit date | `TestM1RollingTrades` … `TestM4Halves` (18 tests) |
| monitors never trade | AST over 7 monitor functions: no constant, no order, no kill switch, no revoke | `TestMonitorsDoNotTrade` |
| an ALERT blocks entries and nothing else | WARN does not block; refusal text says the signal is not retuned | `test_it_refuses_while_a_monitor_alerts` |
| revoke is human-only | literal `HUMAN_REVOKED_CLEARED_EDGE`, asserted absent from a dumped config | `TestRevokeIsHumanOnly` (11 tests) |
| no trading or model module can revoke | AST over 9 modules | `test_no_trading_or_model_module_can_call_the_revoke_path` |
| a corrupt revocations file revokes everything | "cannot tell" ≠ "not revoked" | `test_a_corrupt_file_revokes_everything` |
| revocation reaches registration | hook returns `None` for a revoked signal; returns the name again when lifted | `test_a_revoked_signal_cannot_register` + its control |
| shadow refuses out of scope | null flag, wrong flag, non-paper, armed shell, revoked, wrong symbol | `TestShadowIsPermittedOnlyInScope` |
| **the shadow pilots the cleared schedule** | 41 candidates — the same number Stage-1 scheduled | `test_the_replay_used_the_cleared_rules_schedule` |
| the replay is labelled non-evidence | `is_stage1_evidence: false`, not a `_summary.json`, cannot register | `TestTheShadowReplayArtefact` |
| **M-4 fires WARN on the real window** | earlier half +0.4669 on 17, recent half −0.0798 on 18 | `artifacts/slice58_shadow_replay.json` |
| the clear was not touched by the replay | `cleared_edge_signal_after_replay` unchanged | same artefact |
| the session surface states the thinness | `research_clear`: window, 41 trades, M1 95.13, `live_claim: false` | `TestTheSessionSurface` |
| no PnL-shaped field anywhere new | every `FORBIDDEN_FIELD_MARKER` absent from the snapshot and the block | `test_the_snapshot_carries_no_pnl_field` |
| nothing was armed | live false, policy off, `models/current` absent, no promote path | `TestNothingWasArmed` |
| eleven freezes intact | count 11, forged POSITIVE still refused | `artifacts/slice58_registration_discipline.log` |
| paper certification green | exit 0 | `artifacts/slice58_paper_cert.log` |
| full suite green | **3,854 passed, 2 skipped** | `artifacts/slice58_pytest.log` |

## The two findings a reader should not skip

**1. The first shadow was piloting a cousin of the cleared rule, and the replay
caught it.** The initial wiring proposed an entry on every bar where the funding
threshold was met. The cleared rule enters once per *contiguous run* — rich
funding persists for days, so these are not nearly the same thing:

| | schedule | trades | mean net R |
|---|---|---:|---:|
| the cleared rule | one per run | 41 | **+0.1736** |
| the first shadow | every flagged bar | 55 | **−0.0464** |
| the fixed shadow | one per run, then caps | 35 | +0.1857 |

The pilot would have been monitoring a rule that loses money while reporting
healthy status on a research clear that does not. Nothing would have looked
wrong — the monitors would have computed correct statistics about the wrong
object, which is worse than no monitors because it looks like diligence. Fixed
causally (propose only when the previous decision bar was not a setup) and
pinned by four tests.

**2. The pre-declared decay monitor fired on the first real data it saw.** M-4
reads **WARN** over the out-of-sample window: earlier half +0.4669 on 17 trades,
recent half −0.0798 on 18. Its threshold was written into EDGE §41e before
`shadow.py` existed. This is the decay §41b said to expect — the uncapped Q1
+0.3164 / Q2 +0.0236 split — showing up again, more sharply, in the capped pilot.

**Nothing was changed in response.** No threshold moved, no constant was touched,
entries were not blocked (WARN never blocks, by design), and the research flag is
untouched. A threshold adjusted because it fired is a description of the past,
not a monitor. A test now asserts M-4 still reads WARN, so a later slice cannot
quietly make it stop.

## What was refused

* **no re-score.** The clear is asserted from slice 57's artefacts on disk; no
  measurement, control or percentile was recomputed;
* **no retune.** `FUND_ABS`, the stop, the take profit, the horizon, the costs
  and the join are byte-identical, and the replay tool exposes no flag that could
  change one;
* **no auto-revoke.** Considered and refused in §41f: a monitor that can silently
  null the research flag can be gamed by a quiet period, and withdrawing a
  research claim belongs to the human who made it;
* **no model, no promote path, no live arming**, and no code that could produce
  one;
* **no scoreboard.** Shadow reports a mean R and monitor states; it reports no
  equity, no win rate, no drawdown, and `FORBIDDEN_FIELD_MARKERS` still refuses
  them.

## Closer to what

**Closer to an autonomous profit agent: NO.** Live is blocked, no model exists,
nothing is armed, and this slice adds no path toward any of them.

**Closer to post-POSITIVE operational readiness: YES.** There is now apparatus
that can pilot a thin clear under caps fixed in advance, watch it decay against
thresholds fixed in advance, and be switched off by a human. On its first
exposure to real data that apparatus caught a wiring defect that would have
invalidated the whole pilot, and one of its monitors fired. Both of those are the
apparatus working.

---

A research clear is not a production edge. A shadow fill is not a licence to arm.
The next legitimate step is a human decision about a micro-live checklist, taken
with the WARN above in view.
