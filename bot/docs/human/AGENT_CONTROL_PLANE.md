# AGENT CONTROL PLANE — who may touch the order path

Written 2026-09-16. Law for this repository, not a proposal.

---

## The money objective

Build an **accruing trading asset**: Stage B closed-forward evidence first,
micro-live under gates afterwards. Revenue comes from venue P&L earned under
fail-closed risk control.

This is not a chat product, not an "AI agent" product, and not a museum of
logs. Nothing in this file authorises live trading. `allows_live` is False and
only a human moves it.

---

## Roles

Separation of roles is the whole point. A role may do exactly what its row
says and nothing below it.

| Role | Is | May | May never |
|---|---|---|---|
| **EXECUTION BOT** | this repo's runtime | Bybit path, risk gates, kill switch, shadow/carry books | import any LLM client on the order path |
| **BUILDER** | Claude Code | write, test and commit code under human relay | sign a human gate bit; hold venue keys in git |
| **REVIEWER / DECISION-MAKER** | xAI Grok | read artifacts, gate JSON, forward shadow; emit a verdict file | place orders, clear the kill switch, set `allows_live`, raise caps, write the production state DB |
| **LOCAL INFERENCE** | Ollama | offline mirror of reviewer prompts, air-gapped checks | same bans as Reviewer — off-path, no order path |
| **ORCHESTRATOR** | OpenClaw | schedule jobs, route artifacts between Builder / Reviewer / Bot | bypass `promotion_gate`; inject intents into `TradingEngine` |

**ZERO LLM IMPORTS ON THE ORDER PATH.** The existing bans in
`tests/test_carry_wiring.py` and `tests/test_carry_risk.py` stay, and
`tests/test_control_plane_boundary.py` extends them to every order-path
module by AST, so a ban cannot be defeated by a docstring or an alias.

---

## Reviewer I/O contract — OFF-PATH, read-only

The Reviewer is handed files and returns a file. It is never given a client,
a key, or a socket.

**Inputs (read-only):**

- `artifacts/forward_shadow_current.json`
- the promotion gate JSON (`artifacts/slice59_promotion_gate.json`)
- `artifacts/kill_switch_drill.json`
- the data manifests (`data/*/MANIFEST.json`)

**Output — `artifacts/reviewer_verdict.json`:**

```json
{
  "allows_progress": false,
  "blockers": [],
  "stage_b": {"forward_n_trades": 0, "of_20": 20, "closed_forward_bars": 0},
  "risk": {"allows_live_must_be_false": true},
  "next_actions": [],
  "model": "none (local rules only)",
  "key_status": "missing",
  "generated_utc": "1970-01-01T00:00:00Z"
}
```

Implemented by `tools/reviewer_verdict.py`. `allows_progress` and `blockers`
are computed locally from the artifacts; a model's answer is appended to
`next_actions` prefixed `ADVISORY` and can never clear a blocker. `key_status`
is `missing` or `present` — the key itself is never written here, printed, or
logged.

`generated_utc` in this FILE is when the finding last changed, not when the
reviewer last ran. Both `tools/reviewer_verdict.py` and
`tools/control_plane_tick.py` leave the file untouched when the only difference
would be the timestamp, because the path is tracked and an hourly cron plus a
runbook step that each dirty it turn `git status` into noise nobody reads.

Liveness is reported separately and always: `reviewer.computed_utc` in
`artifacts/control_plane_tick.json`, which is regenerated every tick and is not
tracked. A run that prints a verdict also prints a freshly stamped one to
stdout. `--stamp` forces the file write for anyone who wants it recorded there
too.

`risk.allows_live_must_be_false` is always `true`. A verdict is an opinion
about evidence. It is not an authorisation, and no code may read it as one.

**The Reviewer module, when it exists, MUST NOT import**
`bybit_connection`, `trading_engine`, `carry_broker`, or call any `place_*`.
That is asserted mechanically, and asserted *now*, before the module exists —
see the fail-closed placeholder in `tests/test_control_plane_boundary.py`.

---

## OpenClaw: ABSENT

OpenClaw is **not in this repository or this factory**. Searched
`/Users/grimaldi/iceccarelli-factory` on 2026-09-16: no match. No integration
code has been written for it, and none should be invented before it exists.

When it arrives it attaches at exactly these three hook points and nowhere
else:

1. **Artifact directories** — reads `bot/artifacts/`, writes only
   `artifacts/reviewer_verdict.json`. It does not write gate JSON.
2. **Gate JSON schema** — reads `artifacts/slice59_promotion_gate.json` for
   `checklist`, `items_complete`, `items_total`. Read-only, always. The six
   human-owned bits are moved by a human, never by a scheduler.
3. **Refresher schedule** — may own the cron line that runs
   `tools/daily_forward_refresh.py`. That tool has no `--write`; appending
   stays with `tools/append_closed_corpus.py`.

---

## The B-to-A delivery of NEXT_MISSION has no writer

`scripts/REWIRE_MACHINE_A_OPENCLAW.sh` writes an `AGENTS.md` whose first
startup step is *"Read `repos/ai-agent-control/bot/artifacts/NEXT_MISSION.md`
if present (via the clone)"*, and states that MACHINE A syncs with this repo by
git. MACHINE B does produce that file: `tools/control_plane_tick.py` rewrites
it every hour from the cron in `scripts/REWIRE_MACHINE_B_EXECUTION.sh`.

Nothing commits it, and nothing pushes it. Checked 2026-09-17: no `git commit`
or `git push` in `bot/tools/` or `scripts/` outside the role-doc `git add` in
the MACHINE B rewire script, which stages two paths and stops. So the file is
written into a working tree and read from a clone that never receives it. The
bridge is a dead drop.

`artifacts/NEXT_MISSION.md` is therefore gitignored. That is not the fix — it
is the honest spelling of the current state. Committing it instead would put a
derived instruction file under version control, where the failure mode is a
MACHINE A agent acting on orders that went stale hours ago; that is worse than
no delivery, because no delivery is visible.

Two ways to close it, both a human's call, neither taken here:

* **Push side.** A step on MACHINE B that commits and pushes the tick output.
  This makes an unattended hourly cron write to `origin/main`. That is an
  outward-facing, hard-to-reverse act by a scheduler, and it is the reason this
  was not simply wired up.
* **Pull side.** MACHINE A stops expecting the mission over git and reads the
  reviewer verdict, which IS tracked, as its startup input.

Until one is chosen, MACHINE A's startup step 1 is a no-op and should be read
as such.

---

## Keys

No venue keys in git. No `.env` committed. No key prompts in tooling. The
Builder never holds them.
