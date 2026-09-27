# LINEAR PROTECTIVE-STOP — WHAT IS STILL MISSING, AND THE EXACT COMMANDS

> **This inventory verifies nothing and completes nothing.**
> `linear_protective_stop_verified` is **open**, owner **human**, and this file
> does not change that. It exists so the human doing the work does not also
> have to reconstruct *how*.

**This is not a second checklist.** `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
is the authority on *what must be true* — its four venue items and its sign-off
block are unchanged and are not restated here. What was missing was the
operational layer: which command, on which venue, producing which file. That is
all this document adds.

Drafted 2026-09-17 by an agent. No box below is ticked.

---

## 0. The state of the item, stated plainly

| | |
|---|---|
| Gate entry | *"not documented … A human must document how a protective stop is verified on linear before any micro-live."* |
| Simulator half | **done** — `backtest.LinearSimulatedExchange`, `tests/test_linear_simulator.py` (25 tests), stop written via `/v5/position/trading-stop`, read back through the real `BybitClient.verify_stop`, liquidation-before-stop ordering covered |
| Venue half | **not started.** Items 1–4 of the checklist are statements about a VENUE. No simulator can make them true. |

The uncomfortable sentence from the checklist, still true: the standing claim
*"a confirmed position always has a verified protective stop"* is asserted and
tested **on the spot path**, and `funding_carry_fade_btc_v1` trades a
USDT-margined **linear perpetual**.

---

## 1. Prerequisite — can this host even reach the sandbox?

Nothing below is attemptable until this passes. It uses no keys and places no
orders.

```bash
cd bot
python3 tools/connector_check.py
```

Evidence: `bot/artifacts/connector_check.json`
Read `bybit_testnet_verdict`. If it is `BYBIT_TESTNET_BLOCKED`, the remaining
steps cannot run from this host and that fact belongs in the memo — not a
workaround.

**As of the committed artifact this prerequisite already passes:**
`bybit_testnet_verdict: BYBIT_TESTNET_OK`, `corpus_path_verdict: CORPUS_PATH_OK`,
`track_d_permitted_from_this_host: true`, with `keys_used: false` and
`orders_placed: false`. Re-run it anyway before a session — it is a statement
about a host and a network on the day it was made, not a standing fact.

## 2. Pointing at the sandbox, without the classic mistake

`config.parse_venue` makes `BYBIT_VENUE` authoritative and **derives**
`USE_TESTNET` from it. Setting both to contradictory values is **refused**, by
design:

> *"two sources of truth about which exchange you are pointed at is how a
> testnet key ends up on mainnet."*

```bash
export BYBIT_VENUE=testnet      # authoritative; do NOT also set USE_TESTNET
export PAPER_TRADING=0          # a paper run cannot verify a venue stop
export CATEGORY=linear          # the whole point of this item
```

Keys come from the environment and are never written to a file, a report, or a
commit.

## 3. The four items, each with its command and its evidence

### Item 1 — a stop is PLACED and READ BACK from the exchange

The read-back is the point. Local state agreeing with itself proves nothing.

**Not `bot/tools/drill.py`.** That tool drills the CARRY book (`CarryBroker`
pairs a spot + linear leg; it has no protective-stop mechanism of its own —
`get_liquidation_view` is a *read*, never a write). Its transcript,
`bot/artifacts/linear_stop_drill.json`, is real Phase D evidence for the carry
book and contains zero `trading-stop` / `verify_stop` calls. Citing it here
was the exact substitution this document warns against elsewhere — a number
from one measurement standing in for a different question's answer.

The tool for THIS item is `bot/tools/linear_stop_venue_drill.py`, which drives
the same `BybitClient.place_stop_order` / `verify_stop` path the directional
order path and `tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd`
already use. From `bot/` (per §1's `cd bot`):

```bash
python3 tools/linear_stop_venue_drill.py                # read-only first
python3 tools/linear_stop_venue_drill.py --arm --notional 100 \
    --out artifacts/linear_protective_stop_venue.json
```

`verify_stop`'s call IS the tool's `verify` stage — there is no separate
manual `client.verify_stop(...)` step to run. It dispatches by category
deliberately: a spot conditional stop is an **order**, a linear stop is a
**field on the position** (`/v5/position/list`). Item 1 is satisfied only by
the `verify` stage's `live=True`, and only when the transcript's `verdict` is
`PASSED` — a `FAILED` transcript with a stray `live=True` somewhere in its
evidence is not a pass; read `verdict`, not a grep.

Evidence: `bot/artifacts/linear_protective_stop_venue.json` · `observed:` line
in the checklist

### Item 2 — the stop SURVIVES a process restart

**Not `tools/session_tail.py`.** That tool is a read-only, gate-invariant
checker (`allows_live` / `FUND_ABS` / cap / `LIVE_AUTHORIZED`) — it never
calls `verify_stop` and never reads a position's stop fields. Pointing Item 2
at it would satisfy nothing: it cannot see whether a stop is on the venue at
all.

This process cannot outlive its own restart to watch itself. What CAN be
proven is the only boundary a real process death would ever cross: a second,
independent process reading the venue cold after the first one is gone.
`bot/tools/linear_stop_venue_drill.py` now has three phases for exactly this,
from `bot/`:

```bash
# 1) HOLD — opens + attaches a real stop, then EXITS LEAVING THE POSITION
#    AND STOP LIVE ON THE VENUE. It never flattens. Note the pid it prints.
python3 tools/linear_stop_venue_drill.py --hold --notional 100 \
    --out artifacts/linear_stop_restart_hold.json

# 2) kill the HOLD process if it is somehow still around (normally it has
#    already exited on its own right after `attach`/`verify` pass — that
#    exit IS the process death under test):
kill -9 <pid>          # only if still running; not a graceful shutdown

# 3) VERIFY — a NEW process, no shared state, reads the venue cold:
python3 tools/linear_stop_venue_drill.py --verify \
    --out artifacts/linear_stop_restart_verify.json
# verdict must be VERIFIED (get_position non-empty AND verify_stop live=True)

# 4) FLATTEN — required cleanup, closes the position if VERIFY found one:
python3 tools/linear_stop_venue_drill.py --flatten \
    --out artifacts/linear_stop_restart_flatten.json
```

Be precise about the claim: the "restart" being proven is venue-side stop
survival across process death, evidenced by `verify_stop` reading back
`live=True` from a *different* process than the one that placed it — not a
literal `kill -9` of a long-running daemon (there is no long-running daemon
here to kill; HOLD's own exit after `attach`/`verify` is the death).

Evidence: `artifacts/linear_stop_restart_hold.json`,
`artifacts/linear_stop_restart_verify.json` (verdict `VERIFIED` is the Item 2
proof), `artifacts/linear_stop_restart_flatten.json` · `observed:` line in
the checklist.

### Item 3 — a NAKED position is detected within one cycle

`reconcile()`'s naked check (`StateStore.positions_without_stops()`) trusts the
LOCAL LEDGER's `stop_price` column — it catches a stop this process itself
never recorded, but not a stop that was live and was then cleared AT THE
VENUE while the ledger still believes it is fine. That gap is Item 3. The
fix is `TradingEngine.check_naked_positions()`, called every cycle from
`main.py`'s `tick()` (right after `observe_exits()`), which reads `verify_stop`
straight from the exchange for every open linear position and reacts through
the SAME functions `reconcile()` already used at startup —
`TradingEngine._protect_or_close_naked()` → `_protect()` /
`_emergency_close()`.

Induce it deliberately and observe the reaction with
`bot/tools/linear_stop_venue_drill.py`, from `bot/`:

```bash
# 1) INDUCE-NAKED — opens + attaches a real stop, records it in the ledger
#    exactly as TradingEngine.enter() would, then clears the stop AT THE
#    VENUE ONLY via BybitClient.clear_position_stop. The ledger's stop_price
#    is left untouched - it still believes the stop is live.
python3 tools/linear_stop_venue_drill.py --induce-naked --notional 100 \
    --out artifacts/linear_stop_naked_induce.json
# verdict must be NAKED (position open, venue verify_stop live=False)

# 2) OBSERVE-NAKED — a NEW process, reading the same --state-db, calls the
#    production TradingEngine.check_naked_positions() - the one cycle:
python3 tools/linear_stop_venue_drill.py --observe-naked \
    --from artifacts/linear_stop_naked_induce.json \
    --out artifacts/linear_stop_naked_observe.json
# verdict must be REPROTECTED or FLATTENED - fails closed (FAILED) if the
# position is still naked after the cycle

# 3) required cleanup if REPROTECTED left the position open:
python3 tools/linear_stop_venue_drill.py --flatten \
    --out artifacts/linear_stop_naked_flatten.json
# (ALREADY_FLAT is fine if OBSERVE-NAKED already flattened it)
```

**Not `tools/drill.py`** (the carry drill never calls `place_stop_order` /
`verify_stop` at all) and **not `tools/session_tail.py`** (read-only,
gate-invariant, never reads a position's stop fields) — neither can be Item 3
evidence.

Evidence: `artifacts/linear_stop_naked_induce.json` and
`artifacts/linear_stop_naked_observe.json` — the OBSERVE transcript's
`timestamps_ms` shows detection and reaction happened inside one cycle, and
its `verdict` (`REPROTECTED` or `FLATTENED`) names which action was taken.
`observed:` line in the checklist.

### Item 4 — margin and liquidation at the proposed notional

Document, for 100.00 USD on BTCUSDT linear: initial and maintenance margin, the
liquidation price relative to the stop, and what happens **at the cap**. The
simulator solves liquidation from `cash + dir*S*(p−E) <= mmr*S*p`; item 4 asks
what the **venue** does.

Evidence: written section in the memo, plus the venue's own position risk
readout.

## 4. The choice the checklist forces

The checklist's simulator-gap block has its first box ticked as an engineering
fact. The second choice is still open and is a **human** decision:

```
[ ] micro-live proceeds on TESTNET only, with the simulator gap accepted
    in writing, and the memo says so explicitly
        memo section: ______________________
```

Choosing neither is not an option.

## 5. What would complete the gate item

All four `observed:` lines filled from real venue runs, the sign-off block in
the **checklist** signed, and the evidence path recorded in
`artifacts/slice59_promotion_gate.json` by a human.

**Not** by this file, and **not** by any agent. Until then the item reads
`complete: false`, and `promotion_gate_allows_live()` returns False — which is
the correct answer.
