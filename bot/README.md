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

- Paper and Bybit Testnet only. Mainnet promotion is not part of the current mandate.
- Never set `LIVE_TRADING_ACK`, `USE_TESTNET=0` or `PAPER_TRADING=0`; never weaken the promotion gate, venue/transport guards, risk limits, kill switch, reconciliation or global executor lease.
- Testnet credentials use only `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`. Never print or commit secrets.
- One order-capable executor per Testnet account. See the single-writer and autopilot procedures in [RUNBOOKS.md](RUNBOOKS.md).
- Owner-only Fly, Supabase, Bybit, secret, migration, capability/grant and production changes remain under explicit owner control.

## Development checks

Use the pinned dependency constraints, Makefile and GitHub Actions workflow definitions. For the production baseline and test split, see [CLAUDE.md](../CLAUDE.md). Never run a Testnet order merely to make a demo or test pass. A session must reconcile to venue-flat before it can count as complete.

The canonical strategy status, current blockers and ranked economic priorities are maintained in [`docs/MONEY_MISSION.md`](../docs/MONEY_MISSION.md).
