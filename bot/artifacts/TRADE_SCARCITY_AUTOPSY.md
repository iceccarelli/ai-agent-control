# TRADE SCARCITY AUTOPSY — why 3 of 20 forward trades

**Source of every number below:** `bot/artifacts/forward_shadow_current.json`
(factory-produced, installed this session), `forward_window.corpus_last_bar_utc
= 2026-09-24T00:00:00Z`, `forward_n_trades = 3`, `forward_window.of_which_closed
= 46`. Cross-checked byte-identical against the just-installed
`bot/state/forward_shadow_scratch/forward.json`. `EDGE.md` / `slice76_forward_shadow.py`
docstrings are cited only as background corroboration — every count in this
file is read from the two JSONs, not from prose.

## 1. The counts

| stage | count | ladder field |
|---|---|---|
| forward decision bars (closed, post-`t1`) | 46 | `forward_window.of_which_closed` |
| 1 — setups (close-join reaches `FUND_ABS`) | 9 | `state_ladder.1_setups` |
| 2 — flagged (rule directs a trade) | 9 | `state_ladder.2_flagged` |
| 3 — eligible AND flagged (barrier outcome computable) | 8 | `state_ladder.3_eligible_and_flagged` |
| 4 — candidates after schedule (`one_entry_per_contiguous_run` + lockup) | 4 | `state_ladder.4_candidates_after_schedule` |
| 5 — entries taken (fills + cap refusals + no-entry-bar-yet + truncated) | 4 | `state_ladder.5_entries_taken` |
| 6 — closed trades (entered AND exited on closed bars) | **3** | `state_ladder.6_closed_trades` |

Decomposition of `forward_decisions`:
- `entries_taken`: 3 (fills)
- `entries_refused_by_caps`: 1
- `decisions_with_no_entry_bar_yet`: 0
- `decisions_whose_exit_bar_does_not_exist_yet`: 0
- `entries_blocked_by_monitor`: **false** (all 4 monitors read `INSUFFICIENT_DATA`, none has armed yet — see §3)

**Reason histogram for the 6 units of attrition between 9 setups and 3 closed trades:**

| step | lost | reason | frozen since |
|---|---|---|---|
| setup → flag | 0 | none of the 9 setups sits on the corpus's last bar | — |
| flag → eligible | 1 | `2026-09-19` sits inside the 7-bar unscoreable tail (`barrier_r_for_all_bars`, `eligible[max(0, n-horizon-entry_offset-1):] = False`) | slice 35 (`barrier_eligibility.not_modified_by_this_slice = true`) |
| eligible → candidate | 4 | `one_entry_per_contiguous_run` collapses the 5-bar contiguous setup run `2026-08-21..25` into exactly 1 entry, per the cleared rule's own schedule | Stage-1 clear (slice 58 §41i: bar-by-bar scheduling instead scored 55 trades at −0.0464 mean net R vs the cleared rule's 41 at +0.1736) |
| candidate → entry | 1 | `max_concurrent_positions = 1` refuses the `2026-08-31` candidate: the `2026-08-30 → 2026-09-03` trade is still open (`open_until` not yet reached) when `2026-08-31`'s candidate is evaluated | cap frozen (`caps_changed_this_slice: false`, `shadow.SHADOW_MAX_CONCURRENT_POSITIONS = 1`) |

Every step in that table is a **pre-declared, frozen, unit-tested constant** —
`constants_fingerprint_matches_frozen: true`, `thresholds_moved_this_slice:
false`, `caps_changed_this_slice: false`. None of the three reductions is a
join, a threshold, or a cap that moved to produce this result; all three
pre-date this forward window by multiple slices.

## 2. The three closed trades

| entry_utc | exit_utc | direction | net_r | notional_usd |
|---|---|---|---|---|
| 2026-08-20T00:00:00Z | 2026-08-20T00:00:00Z | SHORT_SETUP | −1.0629 | 100.00 |
| 2026-08-22T00:00:00Z | 2026-08-27T00:00:00Z | SHORT_SETUP | −0.6065 | 100.00 |
| 2026-08-30T00:00:00Z | 2026-09-03T00:00:00Z | SHORT_SETUP | −1.0293 | 100.00 |

`forward_mean_net_r = −0.8996`. The first trade's `entry_utc == exit_utc`
looks like a defect at a glance; it is `held = side_used.get(index, HORIZON)`
resolving to 0 bars — the stop barrier hit within the entry bar itself, which
`barrier_r_for_all_bars` (frozen since slice 35, scored both sides of the
Stage-1 clear) is designed to allow. All three monitors that could react to
this string (M1, M2, M3) read `INSUFFICIENT_DATA` — they need 10, 5-in-90-days,
and 10 trades respectively; only 3 exist. **This mean is not a monitor
reading, not Stage-1 evidence, and not comparable to the OOS +0.1736** — the
artifact says so in `forward_mean_note` and this autopsy repeats it rather
than treats −0.90 as a verdict on the edge, which is a separate, still-open
question WS1 is not scoped to answer.

## 3. Starve check — which mechanism is actually gating entries

Four candidate starvation points, checked against the installed JSONs:

- **Funding join** — NOT the binding constraint this window. `why_neither_moved`
  documents three near-miss prints deliberately left unrescued (2026-08-12
  exact-match-but-superseded-before-close, 2026-08-17 short by 8.0e-6,
  2026-08-18 short by 6.4e-5), but the join and `FUND_ABS = 0.0001` are
  unchanged (`fund_abs_moved_this_slice: false`, `join_changed_this_slice:
  false`) and 9 setups DID fire in this window — the join is working, not
  starving.
- **Barrier eligibility** — a real, but small (1-bar) and self-healing
  contributor: `2026-09-19` will become scoreable as soon as 7 more closed
  bars exist past it (`closed_days_until_scoreable` counts down 1→7 for the
  7 unscoreable tail bars). This is definitional, not a leak: any forward
  measurement's most recent setups are always still baking.
- **Schedule (`one_entry_per_contiguous_run` + lockup)** — the dominant
  contributor: 4 of the 6 total attrition units. This is the cleared rule's
  own schedule, verified against the historical 41-trade OOS clear, not a
  forward-only artifact.
- **Corpus tip / caps** — `max_concurrent_positions = 1` cost exactly 1
  candidate this window because two setups landed close enough together that
  the second one's evaluation still found a position open. This is the same
  cap that has applied since before this forward window began.

**Rate check against the pilot's own history**, the standard this repo already
declares (`docs/PROMOTION_GATE_MICRO_LIVE.md`): the historical clear scheduled
35 capped trades over 731 days ≈ 1 trade / 20.9 days. This forward window has
produced 3 trades over 46 days ≈ 1 trade / 15.3 days — **faster than the
historical rate, not slower**. There is no scarcity to explain beyond what the
frozen rule has always produced; if anything this window is running slightly
ahead of the pilot's own pace.

## 4. Verdict

VERDICT: DESIGNED_RARITY — at the observed rate (3 trades / 46 days ≈ 1 per 15.3 days, matching or beating the pilot's historical 1-per-20.9-day rate), roughly 260 more calendar days are needed to reach 20 closed trades (≈307 days total from `t1`), and the 180-forward-day floor (134 days remaining) is not the binding constraint.

## 5. What this autopsy does not do

Per the mission's own rule: WS1 verdict is DESIGNED_RARITY, so **zero
strategy, threshold, edge, cap, or schedule diffs accompany this file.** No
PR is opened for WS1. The −0.90 forward mean is reported, not acted on — all
four monitors remain `INSUFFICIENT_DATA` by design until 5–20 trades exist,
and moving any threshold to "fix" scarcity or the mean would be exactly the
substitution `EDGE.md` and `docs/PROMOTION_GATE_MICRO_LIVE.md` both forbid.
