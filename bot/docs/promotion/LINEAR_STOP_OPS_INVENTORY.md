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

```bash
python3 tools/drill.py                                   # read-only first
python3 tools/drill.py --arm --notional 100 --out artifacts/linear_stop_drill.json
```

Then confirm through the exchange, not the log:

```python
live, detail = client.verify_stop(symbol="BTCUSDT", order_link_id="<id>")
```

`verify_stop` dispatches by category deliberately: a spot conditional stop is an
**order**, a linear stop is a **field on the position** (`/v5/position/list`).
Item 1 is satisfied only by the linear branch returning `live=True`.

Evidence: `bot/artifacts/linear_stop_drill.json` · `observed:` line in the checklist

### Item 2 — the stop SURVIVES a process restart

```bash
# with a position open:
kill <pid>                      # not a graceful shutdown — that is the test
# restart, then confirm reconciliation sees the venue-side stop
python3 tools/session_tail.py
```

Evidence: session log showing reconcile-on-start finding the stop still on the
venue. Path: `bot/state/` session log + a recorded `verify_stop` read after restart.

### Item 3 — a NAKED position is detected within one cycle

Must be induced deliberately — cancel the stop venue-side while the position is
open, then observe the next cycle either re-protect or flatten.

Evidence: log excerpt with timestamps showing detection inside one cycle, and
which of the two actions was taken.

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
