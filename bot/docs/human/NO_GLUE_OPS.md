# NO-GLUE OPS — Claude ↔ Grok without a human paste loop

1. **Money path:** Stage B accrual (`forward_n_trades` → 20, days → 180) then gated micro-live. `allows_live` stays False until a human flips it.
2. **Orchestrator tick:** `python3 tools/control_plane_tick.py` (or `tools/control_plane_loop.sh once|loop [N]`). Writes `artifacts/control_plane_tick.json` + `artifacts/NEXT_MISSION.md`.
3. **Claude (Builder):** read `artifacts/NEXT_MISSION.md` verbatim; execute DO items; never rewrite into a human essay; never forge human gate signatures; never set `allows_live`.
4. **Grok (Reviewer):** `python3 tools/reviewer_verdict.py --dry-run`; with key, `--xai`. Local rules own `allows_progress`/`blockers`; model lines are `ADVISORY` only.
5. **Corpus:** tick dry-runs `append_closed_corpus.py`; `--write` only if `CONTROL_PLANE_ALLOW_APPEND_WRITE=1`. `append_closed_corpus.py` and `append_spot_corpus.py` both default to syncing PRIMARY + `_full` together (dual-tree, atomic — a refusal on either tree writes neither) and keep their own MANIFEST.json's living BTCUSDT entry truthful on every successful write. `daily_forward_refresh.py` is read-only (scratch score, no promote of `forward_shadow_current.json`).
6. **Daily path, on a host with real venue egress (`CORPUS_OK`):**
   ```
   python3 tools/append_closed_corpus.py --write   # linear + funding, both trees
   python3 tools/append_spot_corpus.py --write     # spot, both trees
   python3 tools/daily_forward_refresh.py --scratch state/forward_shadow_scratch   # scratch score only; reports full_corpus_health
   ```
   `bot/scripts/stage_b_forward_accrual.sh` runs all three with that same fixed scratch dir and, if `forward_n_trades` / `closed_forward_bars` moved, prints a `HUMAN_PROMOTE_HINT` naming the exact command:
   ```
   .venv/bin/python tools/promote_forward_shadow.py --from state/forward_shadow_scratch/forward.json --i-am-human --write
   ```
   A human runs that by hand — this is never automated (see `control_plane_tick.py`'s own charter: "must NEVER promote scratch forward scores into `forward_shadow_current.json`").
7. **Settlement (8h) and borrow corpora:** `data/real_settlement_8h` (`tools/fetch_settlement_klines.py`) and `data/real_borrow` (`tools/fetch_borrow_rates.py`) are NOT wired into the daily path above — they feed `test_settlement_corpus_is_multi_asset.py` / `test_borrow_curve.py`, not Stage B. Both are dry-run-by-default, append/prefix-safe. Run by hand after a funding catch-up moves the tip past their own last window, or their pairing/coverage tests go red on purpose (that is their job).
8. **Forbidden:** orders, kill-switch clear, key printing, inventing corpus writers, skipping the `allows_live` gate.
9. **Cursor / cloud coding agents (this class of session):** offline `pytest` only. No venue curls (Binance/OKX/Bybit or any trading venue). No `--write` against real corpora. No network calls to a trading venue, period.
10. **Factory Mac Terminal + cron:** the only place public corpus catch-up runs *on a schedule* — `append_closed_corpus.py --write`, `append_spot_corpus.py --write`, and `bot/scripts/stage_b_forward_accrual.sh`. That script only appends corpora and writes a scratch refresh log; it does NOT promote `forward_shadow_current.json` (promotion stays human-by-hand per item 6). Tools live under `bot/tools`, run from `bot/` with `$REPO/.venv/bin/python`. **`.github/workflows/stage-b-forward-accrual.yml` runs the identical script but is `workflow_dispatch`-only, never on a cron `schedule:`** — it exists as an explicit, human-triggered failover for when the Factory Mac is down or geo-blocked, not a second scheduled writer. A `schedule:` trigger was removed from that workflow because it raced the Factory Mac's own cron (both fire within an hour of each other) with no ownership check between them — two unsupervised writers that could commit+push the same dual-tree corpus at once. Do not re-add a `schedule:` trigger to that workflow without a real ownership/lease mechanism between it and the Factory Mac; a second independent cron is not that mechanism.
11. **`allows_live` / mainnet / kill-switch clear:** human only, always — no exceptions for either class of session above.
12. **Money sequence:** dual-tree corpus truth → Stage B calendar (20 forward trades / 180 days) → human gates → micro-live → kill-or-keep → only then any productization (control-plane / execution tooling as a sellable surface).
13. **Green CI proves nothing about edge.** A hygiene PR (tests passing, script refactors, doc edits) is not arming or evidence for the strategy's edge. Edge determination lives in `EDGE.md` / the Stage 1 verdict only — never claim it here.

## Offline-suite reds that are supposed to be red (2026-09-21)

Item 7's coverage/pairing tests are currently red in any checkout without a
factory-host `--write` run of `fetch_settlement_klines.py` /
`fetch_borrow_rates.py` past this corpus's frozen window. That is the tests
doing their job, not a bug:

- `tests/test_borrow_curve.py::TestTheRealSeries::test_it_prices_essentially_all_of_the_frozen_window`
- `tests/test_settlement_corpus_is_multi_asset.py::TestThePairingIsLosslessWhereItClaimsToBe::test_the_counts_are_what_the_measurement_assumed[BTC]`
- `tests/test_settlement_corpus_is_multi_asset.py::TestThePairingIsLosslessWhereItClaimsToBe::test_btc_and_eth_drop_nothing_at_all[BTC]`
- `tests/test_settlement_corpus_is_multi_asset.py::TestThePairingIsLosslessWhereItClaimsToBe::test_there_are_no_interior_holes[BTC]`

Do not fix these by editing the pinned counts, xfailing them, or fabricating
corpus rows — refetch on a host with venue egress instead.

## Factory cutover checklist (git → cron → promote)

1. `git pull` on the factory Mac so `bot/scripts/stage_b_forward_accrual.sh`
   (item 10) exists in the tree.
2. Point `crontab` at that TRACKED path, not any prior untracked copy of a
   similarly named script.
3. Delete or stop that untracked copy — it auto-restamped
   `artifacts/forward_shadow_current.json`, which the tracked script and
   `tools/control_plane_tick.py` both refuse to do.
4. Confirm the old cron entry is gone (`crontab -l`) before relying on the
   new one.
5. When a run prints `HUMAN_PROMOTE_HINT`, run the exact command it names
   by hand: `tools/promote_forward_shadow.py --from <scratch>/forward.json
   --i-am-human --write`. Never script or cron this step.
6. `git status` on `artifacts/forward_shadow_current.json` after promoting —
   commit and push it only when a human has looked at the diff and intends
   the change. Never auto-commit from the accrual script or the tick.
7. Crontab, absolute paths:
   `7 1 * * *  $REPO/bot/scripts/stage_b_forward_accrual.sh >> $REPO/bot/state/forward_refresh.log 2>&1`
   `0 * * * *  cd $REPO/bot && $REPO/.venv/bin/python tools/control_plane_tick.py >> $REPO/bot/state/control_plane_tick.log 2>&1`
8. Remove the OLD untracked auto-restamp entry first:
   `crontab -l | grep -v '<old-script-name>' | crontab -`, then `crontab -l` to confirm it is gone.
9. One-shot manual run: `cd $REPO/bot && ./scripts/stage_b_forward_accrual.sh`.
10. Promote by hand: `cd $REPO/bot && .venv/bin/python tools/promote_forward_shadow.py --from state/forward_shadow_scratch/forward.json --i-am-human --write`.
11. The Mac must not sleep through the nightly window — cron never fires asleep. System Settings → Battery/Energy → prevent sleep on power adapter, or `caffeinate -s` on AC. Do NOT add a LaunchAgent that auto-promotes the shadow file or arms live; promotion stays item 10, by hand.
12. This Mac is the factory machine only — never cut this cron over onto a non-factory laptop.

## Product wedge (sellable surface) — only after kill-or-keep

Money sequence (item 12) still holds: dual-tree corpus truth → Stage B
calendar → human gates → micro-live → **kill-or-keep**. Nothing below is
sellable, marketable, or even nameable as a product, before that gate
resolves. This section states the wedge as law, not as a roadmap to build
toward now.

**Becomes sellable only after kill-or-keep says "keep" on micro-live:**
a fail-closed execution / risk / promotion / accounting control plane, as a
service or API, for operators already running overlay-carry strategies —
the gates in this repo (frozen-signal registry, promotion refusal, human-only
kill-switch clear, human-only shadow promotion), packaged for someone else's
strategy, not this one's alpha.

**Not the wedge, ever:**
- dashboards or "AI agent" visualization theater
- new timing/entry signals — this repo freezes signals, it does not sell them
- "AI trading" marketing of any kind
- claiming edge from this strategy as part of the pitch

**Stage B gate numbers are calendar proof, not edge claims.** `20` forward
trades and `180` forward days (item 1) measure how much of the accrual
calendar has elapsed — they say nothing about whether the strategy is
profitable. Per item 13, green CI and passing gates are not edge either.
Any public number about this programme cites `EDGE.md` / the Stage 1
verdict, never this file.
