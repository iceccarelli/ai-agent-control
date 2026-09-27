# LINEAR PROTECTIVE-STOP — VENUE GAP (INDEX, NOT A NEW VERIFICATION)

> **Nothing here is verified. This file completes no checklist item and
> asserts no new fact — it is a one-page index over two documents that
> already exist and already say this correctly.**

Full checklist: `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
Full ops runbook (commands, venue endpoints, evidence paths):
`docs/promotion/LINEAR_STOP_OPS_INVENTORY.md`

## Sim done vs venue done (checklist), gate JSON now synced

| half | state | evidence |
|---|---|---|
| **Simulator** | **done** | `backtest.LinearSimulatedExchange`; stop written via `/v5/position/trading-stop`, read back via the real `BybitClient.verify_stop`; liquidation-before-stop ordering covered — `bot/tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd` (25 tests) |
| **Venue** (items 1–4 of the checklist) | **done on testnet** | all four `observed:` lines filled from real venue runs; `LINEAR_STOP_VERIFICATION_CHECKLIST.md` signed (Verified by Vincenzo Ceccarelli, 2026-09-27) |

**Closed:** `artifacts/slice59_promotion_gate.json`'s own
`linear_protective_stop_verified.complete` now reads `true`, transcribed
from the signed checklist by `tools/sync_linear_stop_gate_evidence.py
--write` — the JSON's `signature` block records the checklist path, the
margin memo path, and the exact `Verified by` / date it read.
`promotion_gate.evaluate_promotion_gate()` now counts this item; the gate
still refuses live for the three other open items and the independently
consulted live-arming chain.

> **WARNING, added after `tools/drill.py --arm` passed (Phase D, carry
> book):** that transcript — `bot/artifacts/linear_stop_drill.json` — is
> real evidence that `CarryBroker` can pair, reconcile and unwind two legs.
> It is carry evidence, not linear-stop evidence: `CarryBroker` has no
> protective-stop mechanism and that transcript contains zero
> `trading-stop`/`verify_stop` calls. Item 1 below is closed by
> `tools/linear_stop_venue_drill.py` only. Do not cite the carry transcript
> against this item — that is precisely the substitution this file exists
> to name.

## The four venue items — done on testnet

| # | item | state |
|---|---|---|
| 1 | Stop PLACED on the venue and read back from the exchange (not local state) | **done** — `tools/linear_stop_venue_drill.py --arm --notional 100`; evidence `artifacts/linear_protective_stop_venue.json`, verdict PASSED (tip 2c84e89) |
| 2 | Stop SURVIVES a process restart | **done** — `tools/linear_stop_venue_drill.py --hold` → new process `--verify` → `--flatten`; verdict VERIFIED (tip 228fb87). NOT `tools/session_tail.py` — that tool is read-only and gate-invariant and never calls `verify_stop` |
| 3 | A NAKED position is detected within one cycle | **done** — `tools/linear_stop_venue_drill.py --induce-naked` → new process `--observe-naked` → `--flatten`; verdict REPROTECTED via the production `TradingEngine.check_naked_positions()` (tip b10cfc7). NOT `tools/drill.py` (no stop mechanism) or `tools/session_tail.py` (never reads a position's stop) |
| 4 | Margin/liquidation behaviour at the proposed notional on BTCUSDT linear, documented | **done** — `tools/linear_stop_venue_drill.py --margin-doc --notional 100` → `--flatten`; verdict DOCUMENTED (tip b8dcd84), memo filled at `docs/promotion/LINEAR_STOP_MARGIN_MEMO.md`. NOT `tools/drill.py` or `tools/session_tail.py`, and NOT the simulator's liquidation formula (context only) |

Full transcripts and exact commands for each: see
`LINEAR_STOP_VERIFICATION_CHECKLIST.md`'s own `observed:` lines and
`LINEAR_STOP_OPS_INVENTORY.md` §3 — this table only points at them.

Prerequisite for all four: `python3 tools/connector_check.py` →
`bybit_testnet_verdict: BYBIT_TESTNET_OK` (as of the last committed
`artifacts/connector_check.json`, this already passes with `keys_used: false`,
`orders_placed: false` — re-run before any session, it is a same-day fact, not
a standing one).

Bybit endpoints involved (from `LINEAR_STOP_OPS_INVENTORY.md` §3): `POST
/v5/position/trading-stop` (writes the stop), `GET /v5/position/list` (the
read-back a linear stop is verified through — a linear stop is a position
field, not an order, unlike spot).

## What closed this item

All four `observed:` lines in `LINEAR_STOP_VERIFICATION_CHECKLIST.md` filled
from real venue runs, its sign-off block signed by a human, and the evidence
path recorded in `artifacts/slice59_promotion_gate.json`
(`linear_protective_stop_verified`).

**All three are now done.** The four `observed:` lines are filled, the
checklist's `Verified by` is signed (Vincenzo Ceccarelli, 2026-09-27;
`Reviewed by` still blank), and `slice59_promotion_gate.json` now reads
`linear_protective_stop_verified.complete: true` — transcribed by
`tools/sync_linear_stop_gate_evidence.py --write`, which refuses to write
anything unless the checklist is already fully signed; it makes no judgment
call of its own. `promotion_gate_allows_live()` still remains `False`
(the gate needs `human_risk_memo_signed`, `live_trading_ack_present`,
`m4_recent_half_accepted_or_recovered`, and `forward_shadow_clean` too, all
still open, plus the independently consulted live-arming chain).
