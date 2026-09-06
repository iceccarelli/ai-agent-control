# PAPER RUNBOOK

> # Paper success ≠ edge. Timing skill research remains CLOSED.
>
> A paper session that starts, reconciles, stays healthy and shuts down cleanly
> proves the **plumbing** works. It proves nothing about whether this system can
> time a market. Two signal families have been measured against a validated
> instrument and both returned ABSENT
> ([RESEARCH_CLOSE_STAGE1.md](../RESEARCH_CLOSE_STAGE1.md)).

**How to run this bot in paper mode, confirm it is not armed, and stand it down.**

Written at slice 29, extended at slice 30. Everything here is verified by
`tests/test_paper_readiness.py` (84 tests) and
`tests/test_paper_session_log.py` (42 tests) — if a claim in this document stops
being true, a test fails.

**Quickest possible confidence check**, offline, no credentials, no network:

```bash
python3 tools/paper_session_demo.py                     # stand-down
python3 tools/paper_session_demo.py --entries-enabled   # entries on, still paper
# both exit 0 and print the session record
```

---

## 0. Read this first

**There is no measured timing edge in this system.** Two signal families have
been measured against a validated instrument and both returned ABSENT
([RESEARCH_CLOSE_STAGE1.md](../RESEARCH_CLOSE_STAGE1.md)).

**Do not interpret geometry PnL — or any paper-run PnL — as skill.** The
`+0.5479R` expectancy the evaluator reports is payoff geometry on an asset that
appreciated ~675% over the sample; roughly half of it is reproducible by an entry
schedule that cannot see prices at all, and the short-side sweep is 0 of 84
positive. A paper run that makes money has demonstrated that the plumbing works,
nothing more.

Running in paper mode is an **operational** exercise: does the machine start,
reconcile, protect, report, and stand down correctly? That is what this runbook
covers.

---

## 1. Environment for a paper run

Copy `.env.example` to `.env` and leave the safety block exactly as shipped:

```bash
# --- the three that keep you unarmed. All are already the defaults. ---
USE_TESTNET=1
PAPER_TRADING=1
LIVE_TRADING_ACK=            # must be empty

# --- no model. models/current does not exist, by design. ---
POLICY_MODE=off

# --- risk limits: do not change these for a paper run ---
MAX_POSITION_SIZE_PCT=0.02
RISK_PER_TRADE_PCT=0.005

# --- runtime ---
ENTRIES_ENABLED=1
ENABLE_HEALTH_SERVER=1
HEALTHCHECK_PORT=8081
STATE_DB_PATH=state/trading_state.db
```

Credentials are injected at runtime and never baked into an image. `.dockerignore`
excludes every `.env*`. **The API keys in this repository's history must be
treated as compromised and rotated** — see `DEPLOYMENT.md` §0.

Start it:

```bash
python3 main.py
```

---

## 2. Confirm you are not armed

Three independent checks. Do all three; any one of them can be misread alone.

### 2.1 The startup log

`startup()` logs the resolved mode before it does anything:

```
mode: SANDBOX | testnet=True paper=True | SANDBOX (TESTNET+PAPER) — live trading not armed
```

`mode: LIVE` means live orders. If you see it and did not intend it, stop the
process.

### 2.2 The health endpoint

```bash
curl -s localhost:8081 | python3 -m json.tool
```

```json
{
  "healthy": true,
  "reconciled": true,
  "kill_switch": false,
  "kill_reason": "",
  "naked_positions": [],
  "open_positions": 0,
  "paper": true,
  "testnet": true,
  "uptime_s": 12.3,
  "category": "spot",
  "shorts_available": false,
  "memory": {"enabled": true},
  "policy": {"mode": "off", "model_loaded": false}
}
```

**`paper` must be `true` and `testnet` must be `true`.** HTTP status is `200`
when `healthy`, `503` when not. `healthy` is `false` if the kill switch is
engaged, if any position is naked, or if the bot has not reconciled.

### 2.3 Ask the config directly

```bash
python3 -c "import config; c = config.load(); \
print('LIVE_AUTHORIZED:', c.LIVE_AUTHORIZED); print('reason:', c.LIVE_BLOCK_REASON)"
```

`LIVE_AUTHORIZED: False` is what you want. Note it is a **derived** field — it
cannot be set from the environment. Arming live requires all four of
`USE_TESTNET=0`, `PAPER_TRADING=0`, `LIVE_TRADING_ACK=I_UNDERSTAND`, and both
credentials. If live is requested and any part is missing, the loader **degrades
to paper** and logs why, rather than crash-looping or arming.

---

## 3. Standing the machine down without killing it

Set `ENTRIES_ENABLED=0` and restart:

```bash
ENTRIES_ENABLED=0 python3 main.py
```

No signal source is attached. The bot still:

* runs its loop,
* reconciles state against the exchange at startup,
* manages and keeps the verified protective stop on any position it already holds,
* serves health.

It proposes **no new entries**, because there is nothing to propose them.

You will see this in the log:

```
WARNING ENTRIES_ENABLED is false: no signal source will be attached. The loop,
reconciliation, stop management and health stay live; no new entry will be
proposed.
```

> **`ENTRIES_ENABLED` is an operator control, not a safety gate.** The gates are
> the kill switch, the live-arming chain, and the risk gate set. Standing entries
> down does not weaken them, and it is not a substitute for any of them. Never
> rely on this flag to make an unsafe configuration safe.

Any unrecognised value parses as **disabled**, never enabled — a typo costs you
entries, not safety.

---

## 4. The kill switch

### 4.1 What it does

Engaged, it blocks new risk everywhere at once: `startup()` refuses to start,
`should_halt_trading()` returns true, the risk gate chain blocks with
`KILL_SWITCH_ENGAGED`, and health reports `503`. If the switch cannot be *read*,
the gate blocks with `KILL_SWITCH_UNREADABLE` — an unanswerable question is
treated as a "no".

The bot trips it by itself on drawdown breach and on any bracket or stop failure
(the position is closed at market first, then the switch trips).

### 4.2 Engaging it manually

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.trip_kill_switch('operator: standing down for maintenance'); s.close()"
```

It is persisted in SQLite and **survives a restart**.

### 4.3 Clearing it — human only

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.clear_kill_switch_by_human('HUMAN_CLEARED_KILL_SWITCH'); s.close()"
```

The token is the exact literal `HUMAN_CLEARED_KILL_SWITCH`. There is no trimming,
no case folding, no prefix match — a near miss raises `PersistenceError` and the
switch stays engaged. The token is **deliberately not derivable from config** and
is not stored in any `Config` field, so no process can construct it from its own
environment.

**Nothing in the codebase clears it automatically.** The one automatic reset in
`risk_management.py` is the *circuit breaker*, which is a different mechanism with
a cooldown. Tests assert at AST level that no trading module calls
`clear_kill_switch_by_human`.

Before you clear it: read `kill_reason` from the health payload and understand why
it tripped. Clearing without diagnosing is how a protective mechanism becomes a
nuisance to be routed around.

---

## 5. What the health endpoint tells you

| field | meaning | when to worry |
|---|---|---|
| `healthy` | kill switch clear **and** no naked position **and** reconciled | `false` — HTTP 503 |
| `reconciled` | startup reconciliation completed | `false` after startup |
| `kill_switch` / `kill_reason` | engaged state and why | `true` — read the reason before acting |
| `naked_positions` | positions the store knows about with no verified stop | any entry, ever |
| `open_positions` | count | unexpected non-zero in a paper run |
| `paper` / `testnet` | the arming state | either `false` unintentionally |
| `policy.model_loaded` | whether a model is attached | `true` — no model is promoted |

**`naked_positions` non-empty is the one that should get you out of your chair.**
The invariant this whole system exists to enforce is *a confirmed position always
has a verified protective stop*: the stop is placed and then **read back** from
the exchange, and if it cannot be verified the position is closed at market and
the kill switch trips.

---

## 5b. The paper session log

Every session writes one `SESSION_START` and one `SESSION_END` record. It reports
what the machine was configured to do and what it actually did — and deliberately
**no PnL**, because a session log that reported profit would invite reading
profit as skill, which is the specific inference
[RESEARCH_CLOSE_STAGE1.md](../RESEARCH_CLOSE_STAGE1.md) closes.

Enable the operator-facing file:

```bash
PAPER_SESSION_LOG_PATH=state/paper_sessions.jsonl python3 main.py
```

One JSON object per line:

```json
{
  "event": "SESSION_END",
  "schema": "paper_session/1",
  "session_id": "a1696b4c9f084274abf05652cf01c601",
  "started_utc": "2026-07-31T11:59:31Z",
  "ended_utc": "2026-07-31T12:04:02Z",
  "entries_enabled": false,
  "paper": true,
  "testnet": true,
  "live_authorized": false,
  "policy_mode": "off",
  "model_loaded": false,
  "kill_switch_engaged": false,
  "kill_switch_reason": "",
  "ticks": 10,
  "orders_submitted": 0,
  "reconciled": true,
  "no_edge_claim": "NO EDGE CLAIM — timing-skill research CLOSED"
}
```

The same record is journalled to the `decisions` table under symbol `SESSION`, so
it survives a restart even if no file path is set:

```bash
python3 -c "from persistence import StateStore; import json; \
s = StateStore('state/trading_state.db'); \
print(json.dumps([d for d in s.recent_decisions(200) \
                  if d['symbol'] == 'SESSION'], indent=2)); s.close()"
```

**Reading it:**

| field | what to check |
|---|---|
| `live_authorized` | must be `false` in any paper session |
| `orders_submitted` | **must be 0 when `entries_enabled` is false** — counted at the transport, not inferred from decisions |
| `kill_switch_engaged` | `true` means the session ended standing down; read `kill_switch_reason` before clearing |
| `reconciled` | `false` means the bot never established a clean view of the exchange |
| `no_edge_claim` | constant, mandatory, asserted by test |

`orders_submitted` counts requests to `/v5/order/create` that actually left the
process, incremented at the single transport every call passes through. It counts
an order whose response was lost, because that order left. A session log that
under-counted those would be the one that lied.

The log can never take the bot down: an unwritable path or a closed store is
logged at WARNING and the session continues.

---

## 5c. Exact commands for each operational claim

Each block is a claim from §11b of `EDGE.md` and the command that demonstrates it.

**A1 — stand-down submits zero orders**

```bash
python3 tools/paper_session_demo.py --ticks 10
# ENTRIES_ENABLED False | orders_submitted 0 | decisions journalled 0 | healthy True
# exit 0

ENTRIES_ENABLED=0 python3 main.py          # the real thing
```

**A2 — entries on, paper on: gates run, still zero orders**

```bash
python3 tools/paper_session_demo.py --entries-enabled --ticks 10
# orders_submitted 0 | decisions journalled 30 (['ALLOW', 'PAPER'])
# exit 0
```

The 30 journalled decisions are the point: the gates and the sizer ran on every
tick and `PAPER_TRADING` short-circuited before the transport. Zero orders here
means "suppressed", not "nothing happened".

**A3 — kill switch**

```bash
# engage
python3 -c "from persistence import StateStore; s=StateStore('state/trading_state.db'); \
s.trip_kill_switch('operator: standing down'); s.close()"

# confirm it blocks
curl -s localhost:8081 | python3 -m json.tool | grep -E 'healthy|kill'

# clear — exact literal only
python3 -c "from persistence import StateStore; s=StateStore('state/trading_state.db'); \
s.clear_kill_switch_by_human('HUMAN_CLEARED_KILL_SWITCH'); s.close()"
```

**A4 — arming fails closed**

```bash
python3 -c "import config; c = config.load({'USE_TESTNET':'0','PAPER_TRADING':'0'}); \
print('LIVE_AUTHORIZED:', c.LIVE_AUTHORIZED, '| PAPER_TRADING:', c.PAPER_TRADING); \
print(c.LIVE_BLOCK_REASON)"
# LIVE_AUTHORIZED: False | PAPER_TRADING: True
# LIVE_BLOCKED: LIVE_TRADING_ACK must equal 'I_UNDERSTAND' (got unset)
```

Live was *requested* and the loader degraded to paper rather than arming.

**A5 — the session log**

```bash
python3 tools/paper_session_demo.py --json-out /tmp/session.jsonl
python3 -c "import json; \
[print(json.loads(l)['event'], json.loads(l)['orders_submitted']) \
 for l in open('/tmp/session.jsonl')]"
# SESSION_START 0
# SESSION_END 0
```

**Full containment suite**

```bash
python3 -m pytest tests/test_paper_readiness.py tests/test_paper_session_log.py -q
# 126 passed
```

## 6. Shutdown

`SIGINT` or `SIGTERM` triggers an orderly shutdown: release the single-writer
claim, shut down the engine, stop the health server, close the store.

**Protective stops are deliberately left in place** (`cancel_protective=False`).
Cancelling them on the way out would leave a position naked between processes,
which is the exact state the system is built to make impossible.

---

## 7. What this runbook does not authorise

* **Live capital.** Not covered here and blocked by measurement, not by
  procedure. The path after a *cleared* edge remains: stress → constrained policy
  → long shadow → microscopic live → decay monitoring.
* **Shadow mode or model loading.** `POLICY_MODE=off`; `models/current` does not
  exist by design.
* **Raising any risk limit.** A limit change is a human's explicit decision, made
  outside a runbook.
* **Reading paper PnL as evidence of anything.** See §0.

---

*Related: [RESEARCH_CLOSE_STAGE1.md](../RESEARCH_CLOSE_STAGE1.md),
[RESEARCH_STATUS.md](../RESEARCH_STATUS.md), `DEPLOYMENT.md`, `EDGE.md` §10c–§10e.*
