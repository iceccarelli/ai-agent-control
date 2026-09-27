# LINEAR PROTECTIVE-STOP — VENUE GAP (INDEX, NOT A NEW VERIFICATION)

> **Nothing here is verified. This file completes no checklist item and
> asserts no new fact — it is a one-page index over two documents that
> already exist and already say this correctly.**

Full checklist: `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
Full ops runbook (commands, venue endpoints, evidence paths):
`docs/promotion/LINEAR_STOP_OPS_INVENTORY.md`

## Sim done vs venue unverified

| half | state | evidence |
|---|---|---|
| **Simulator** | **done** | `backtest.LinearSimulatedExchange`; stop written via `/v5/position/trading-stop`, read back via the real `BybitClient.verify_stop`; liquidation-before-stop ordering covered — `bot/tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd` (25 tests) |
| **Venue** (items 1–4 of the checklist) | **not started** | no `observed:` line in the checklist has been filled from a real venue run |

> **WARNING, added after `tools/drill.py --arm` passed (Phase D, carry
> book):** that transcript — `bot/artifacts/linear_stop_drill.json` — is
> real evidence that `CarryBroker` can pair, reconcile and unwind two legs.
> It is carry evidence, not linear-stop evidence: `CarryBroker` has no
> protective-stop mechanism and that transcript contains zero
> `trading-stop`/`verify_stop` calls. Item 1 below is closed by
> `tools/linear_stop_venue_drill.py` only. Do not cite the carry transcript
> against this item — that is precisely the substitution this file exists
> to name.

## The four venue items — still open

| # | item | state |
|---|---|---|
| 1 | Stop PLACED on the venue and read back from the exchange (not local state) | open — command: `tools/linear_stop_venue_drill.py --arm --notional 100`; evidence `artifacts/linear_protective_stop_venue.json` |
| 2 | Stop SURVIVES a process restart | open — `tools/linear_stop_venue_drill.py --hold` (opens+attaches, exits leaving the position+stop live), then a NEW process `--verify` (must read `VERIFIED`), then `--flatten` (required cleanup). NOT `tools/session_tail.py` — that tool is read-only and gate-invariant and never calls `verify_stop` |
| 3 | A NAKED position is detected within one cycle | open — must be induced deliberately (cancel the stop venue-side) |
| 4 | Margin/liquidation behaviour at 100 USD notional on BTCUSDT linear, documented | open |

Prerequisite for all four: `python3 tools/connector_check.py` →
`bybit_testnet_verdict: BYBIT_TESTNET_OK` (as of the last committed
`artifacts/connector_check.json`, this already passes with `keys_used: false`,
`orders_placed: false` — re-run before any session, it is a same-day fact, not
a standing one).

Bybit endpoints involved (from `LINEAR_STOP_OPS_INVENTORY.md` §3): `POST
/v5/position/trading-stop` (writes the stop), `GET /v5/position/list` (the
read-back a linear stop is verified through — a linear stop is a position
field, not an order, unlike spot).

## What would close this item

All four `observed:` lines in `LINEAR_STOP_VERIFICATION_CHECKLIST.md` filled
from real venue runs, its sign-off block signed by a human, and the evidence
path recorded by a human in `artifacts/slice59_promotion_gate.json`
(`linear_protective_stop_verified`). **Not by this file, and not by any
agent** — this index changes none of that; `linear_protective_stop_verified`
remains `complete: false` and `promotion_gate_allows_live()` remains `False`.
