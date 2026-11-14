# Bybit Testnet carry engine

This repository contains a research, replay, paper and Bybit Testnet execution system for evaluating delta-neutral funding-carry and related market-neutral strategies.

**Start here**
1. Read the repository-level [Claude operating brief](../CLAUDE.md).
2. Read the [money mission and current handover](../docs/MONEY_MISSION.md).
3. Read [operational runbooks](RUNBOOKS.md) before any operational action.
4. Read the relevant strategy, data, execution and evidence modules before proposing a change.

## The objective

Maximise verified, risk-adjusted **net realised** return on owner economic capital after fees, spread, slippage, impact, funding, financing, hedge costs and operational costs. Reject strategies that do not survive independent, out-of-sample and cost-stressed evidence. A positive gross backtest, modelled fill, CI pass or Testnet return is not proof of profit.

## Operating boundary

<!-- topology:begin -->
**Operating topology (one authoritative statement; this block is byte-identical in `bot/README.md`, `bot/RUNBOOKS.md` and `docs/MONEY_MISSION.md`, and a test enforces it).**

| Place | Role | May it place an order? |
|---|---|---|
| Codespace or laptop | development, replay, research, data inspection and read-only venue checks (`status`, `evaluate`, `preflight` once the owner holds a capability) | **No.** Never run `loop`, `grant`, `execute` or `unattended_drill` there. |
| One selected Testnet executor | the only place a Testnet order may ever originate, and only after (1) it is deployed from the merged commit and its image is compared with that commit's manifest, (2) the Supabase single-writer lease is proven, (3) the owner has typed the capability and a grant for that exact bounded session | **Not selected and not deployed.** `carry-autopilot` does not exist as a Fly app today (owner-observed 2026-11-14). |
| `carry-drill` (Fly) | read-only market-data recorder and monitor | **No.** Its 2026-11-05 image predates the lease; its conformance path stays disabled (`CARRY_DRILL_ENABLED` absent or not `true`). |
| `carry-book` (Fly) | legacy carry book | **Stopped.** No document authorises starting, redeploying or resizing it. |
| `carry-research` (Fly) | research worker | **Does not exist.** Nothing provisions it until the owner approves a data-transfer and storage plan. |

Paper and Bybit Testnet only. Mainnet is out of scope: nothing in this repository arms it, and `LIVE_TRADING_ACK`, `USE_TESTNET=0` and `PAPER_TRADING=0` are never set. Testnet credentials are `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET` only. A passing check, a capability or a deployed image is not a grant to trade.
<!-- topology:end -->

- Paper and Bybit Testnet only. Mainnet promotion is not part of the current mandate.
- Never set `LIVE_TRADING_ACK`, `USE_TESTNET=0` or `PAPER_TRADING=0`; never weaken the promotion gate, venue/transport guards, risk limits, kill switch, reconciliation or global executor lease.
- Testnet credentials use only `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`. Never print or commit secrets.
- One order-capable executor per Testnet account. See the single-writer and autopilot procedures in [RUNBOOKS.md](RUNBOOKS.md).
- Owner-only Fly, Supabase, Bybit, secret, migration, capability/grant and production changes remain under explicit owner control.

## Development checks

Use the pinned dependency constraints, Makefile and GitHub Actions workflow definitions. For the production baseline and test split, see [CLAUDE.md](../CLAUDE.md). Never run a Testnet order merely to make a demo or test pass. A session must reconcile to venue-flat before it can count as complete.

The canonical strategy status, current blockers and ranked economic priorities are maintained in [`docs/MONEY_MISSION.md`](../docs/MONEY_MISSION.md).
