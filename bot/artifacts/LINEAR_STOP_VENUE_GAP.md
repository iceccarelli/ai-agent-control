# LINEAR PROTECTIVE-STOP — VENUE GAP (INDEX, NOT A NEW VERIFICATION)

> **Nothing here is verified. This file completes no checklist item and
> asserts no new fact — it is a one-page index over two documents that
> already exist and already say this correctly.**
>
> **AUDIT NOTE (this fix):** an earlier version of this file said the venue
> items were "done on testnet" and the gate item closed. An audit found the
> eight `bot/artifacts/linear_*.json` transcripts that claim cited do not
> exist in this repository, at this commit or any earlier one — `git log
> --all` on each path is empty.
> `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md` has been reverted to
> blank and `artifacts/slice59_promotion_gate.json`'s
> `linear_protective_stop_verified` back to `complete: false`. The tables
> below are corrected to match; do not restore the "done" wording without a
> real, committed transcript for every cited artifact.

Full checklist: `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
Full ops runbook (commands, venue endpoints, evidence paths):
`docs/promotion/LINEAR_STOP_OPS_INVENTORY.md`

## Sim done vs venue done (checklist), gate JSON incomplete

| half | state | evidence |
|---|---|---|
| **Simulator** | **done** | `backtest.LinearSimulatedExchange`; stop written via `/v5/position/trading-stop`, read back via the real `BybitClient.verify_stop`; liquidation-before-stop ordering covered — `bot/tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd` (25 tests) |
| **Venue** (items 1–4 of the checklist) | **NOT done — unverified** | checklist reverted to blank; no committed transcript exists for any of the four items (see AUDIT NOTE above) |

**Open:** `artifacts/slice59_promotion_gate.json`'s own
`linear_protective_stop_verified.complete` reads `false`. A prior transcribe
via `tools/sync_linear_stop_gate_evidence.py --write` set it `true` against a
checklist whose cited artifacts do not exist; that has been reverted, and the
tool now refuses to sync unless every cited artifact is present on disk as
readable JSON. `promotion_gate.evaluate_promotion_gate()` counts this item
among the incomplete ones; the gate refuses live for this and four other
open items plus the independently consulted live-arming chain.

> **WARNING, added after `tools/drill.py --arm` passed (Phase D, carry
> book):** that transcript — `bot/artifacts/linear_stop_drill.json` — is
> real evidence that `CarryBroker` can pair, reconcile and unwind two legs.
> It is carry evidence, not linear-stop evidence: `CarryBroker` has no
> protective-stop mechanism and that transcript contains zero
> `trading-stop`/`verify_stop` calls. Item 1 below is closed by
> `tools/linear_stop_venue_drill.py` only. Do not cite the carry transcript
> against this item — that is precisely the substitution this file exists
> to name.

## The four venue items — NOT done, unverified

| # | item | state |
|---|---|---|
| 1 | Stop PLACED on the venue and read back from the exchange (not local state) | **open** — run `tools/linear_stop_venue_drill.py --arm --notional 100`, commit the resulting `artifacts/linear_protective_stop_venue.json` transcript |
| 2 | Stop SURVIVES a process restart | **open** — `tools/linear_stop_venue_drill.py --hold` → new process `--verify` → `--flatten`, commit each transcript. NOT `tools/session_tail.py` — that tool is read-only and gate-invariant and never calls `verify_stop` |
| 3 | A NAKED position is detected within one cycle | **open** — `tools/linear_stop_venue_drill.py --induce-naked` → new process `--observe-naked` → `--flatten`, commit each transcript, via the production `TradingEngine.check_naked_positions()`. NOT `tools/drill.py` (no stop mechanism) or `tools/session_tail.py` (never reads a position's stop) |
| 4 | Margin/liquidation behaviour at the proposed notional on BTCUSDT linear, documented | **open** — `tools/linear_stop_venue_drill.py --margin-doc --notional 100` → `--flatten`, commit each transcript and fill `docs/promotion/LINEAR_STOP_MARGIN_MEMO.md` with the real numbers. NOT `tools/drill.py` or `tools/session_tail.py`, and NOT the simulator's liquidation formula (context only) |

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

## What would close this item

All four `observed:` lines in `LINEAR_STOP_VERIFICATION_CHECKLIST.md` filled
from real venue runs, each cited artifact actually committed under
`bot/artifacts/`, its sign-off block signed by a human, and the evidence path
recorded in `artifacts/slice59_promotion_gate.json`
(`linear_protective_stop_verified`).

**None of that is done.** The checklist is blank, its sign-off block is
blank, and `slice59_promotion_gate.json` reads
`linear_protective_stop_verified.complete: false`.
`tools/sync_linear_stop_gate_evidence.py --write` will refuse to flip it
until the checklist is genuinely re-signed AND every artifact it cites
exists on disk as readable JSON; it makes no judgment call of its own.
`promotion_gate_allows_live()` remains `False` (the gate also needs
`human_risk_memo_signed`, `live_trading_ack_present`,
`m4_recent_half_accepted_or_recovered`, and `forward_shadow_clean`, all
still open, plus the independently consulted live-arming chain).
