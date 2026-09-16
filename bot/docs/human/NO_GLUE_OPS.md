# NO-GLUE OPS — Claude ↔ Grok without a human paste loop

1. **Money path:** Stage B accrual (`forward_n_trades` → 20, days → 180) then gated micro-live. `allows_live` stays False until a human flips it.
2. **Orchestrator tick:** `python3 tools/control_plane_tick.py` (or `tools/control_plane_loop.sh once|loop [N]`). Writes `artifacts/control_plane_tick.json` + `artifacts/NEXT_MISSION.md`.
3. **Claude (Builder):** read `artifacts/NEXT_MISSION.md` verbatim; execute DO items; never rewrite into a human essay; never forge human gate signatures; never set `allows_live`.
4. **Grok (Reviewer):** `python3 tools/reviewer_verdict.py --dry-run`; with key, `--xai`. Local rules own `allows_progress`/`blockers`; model lines are `ADVISORY` only.
5. **Corpus:** tick dry-runs `append_closed_corpus.py`; `--write` only if `CONTROL_PLANE_ALLOW_APPEND_WRITE=1`. `daily_forward_refresh.py` is read-only (scratch score, no promote of `forward_shadow_current.json`).
6. **Forbidden:** orders, kill-switch clear, key printing, inventing corpus writers, skipping the `allows_live` gate.
