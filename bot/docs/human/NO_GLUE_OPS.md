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
   python3 tools/daily_forward_refresh.py          # scratch score only; reports full_corpus_health
   ```
   If `forward_n_trades` / `closed_forward_bars` moved, a human promotes the scratch score into `artifacts/forward_shadow_current.json` by hand — this is never automated (see `control_plane_tick.py`'s own charter: "must NEVER promote scratch forward scores into `forward_shadow_current.json`").
7. **Settlement (8h) and borrow corpora:** `data/real_settlement_8h` (`tools/fetch_settlement_klines.py`) and `data/real_borrow` (`tools/fetch_borrow_rates.py`) are NOT wired into the daily path above — they feed `test_settlement_corpus_is_multi_asset.py` / `test_borrow_curve.py`, not Stage B. Both are dry-run-by-default, append/prefix-safe. Run by hand after a funding catch-up moves the tip past their own last window, or their pairing/coverage tests go red on purpose (that is their job).
8. **Forbidden:** orders, kill-switch clear, key printing, inventing corpus writers, skipping the `allows_live` gate.
9. **Cursor / cloud coding agents (this class of session):** offline `pytest` only. No venue curls (Binance/OKX/Bybit or any trading venue). No `--write` against real corpora. No network calls to a trading venue, period.
10. **Factory Mac Terminal + cron:** the only place public corpus catch-up actually runs — `append_closed_corpus.py --write`, `append_spot_corpus.py --write`, and `bot/scripts/stage_b_forward_accrual.sh`. That script only appends corpora and writes a scratch refresh log; it does NOT promote `forward_shadow_current.json` (promotion stays human-by-hand per item 6). Tools live under `bot/tools`, run from `bot/` with `$REPO/.venv/bin/python`.
11. **`allows_live` / mainnet / kill-switch clear:** human only, always — no exceptions for either class of session above.
12. **Money sequence:** dual-tree corpus truth → Stage B calendar (20 forward trades / 180 days) → human gates → micro-live → kill-or-keep → only then any productization (control-plane / execution tooling as a sellable surface).
13. **Green CI proves nothing about edge.** A hygiene PR (tests passing, script refactors, doc edits) is not arming or evidence for the strategy's edge. Edge determination lives in `EDGE.md` / the Stage 1 verdict only — never claim it here.
