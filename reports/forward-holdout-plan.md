# Pre-registered forward-holdout plan: BTC overlay carry book

Status: **DRAFT for human sign-off.** It binds nothing until the owner signs the block at the end and commits it. Drafted 2026-10-29 against commit `84ba866`.

## 0. Disclosure of what has been seen (so the plan cannot be accused of hindsight)

- Read before drafting: the repo's modelled historical results (replay, OOS audit, walk-forward, hold audit) and the runner summary of 2026-10-29: forward shadow book `post_gate_v1`, boundary `2026-10-25T18:06:27Z`, 88 observations over 3.3 days, **1 simulated entry, 0 closed trades**; mainnet-public protocol: 63 observations over 57 hours, 8 distinct funding prints, gate open on 73% of observations.
- No closed forward trade outcome has been seen. Therefore the thresholds below do not depend on forward outcomes.
- The historical numbers are in-sample: constants were fixed with the whole 2022-08..2026-09 corpus visible (`carry_oos_audit.py`, `carry_walkforward.py` docstrings). They are used here only to size the test, never to claim edge.

## 1. What is and is not being tested

| ID | Claim | Testable in this window? |
|---|---|---|
| H1 Mechanics | The production stack opens, holds, settles and unwinds the BTC overlay carry on Bybit **testnet** with no naked leg, no unreconciled fill, and a working kill switch | **Yes** |
| H2 Real-market gate behaviour | On real mainnet public data, the frozen entry gate is open often enough and slippage is within the assumed 1.0 bps/leg | **Yes** (observation only) |
| H3 Forward economics | The frozen rule earns net-positive carry after venue-confirmed fees | **Partly**: direction only, not significance (see section 3) |
| H4 "Edge established" | The rule is distinguishable from an edgeless search | **No, not in this window.** Do not claim it. |

## 2. Frozen subject (any change restarts the clock)

Rule: `carry_entry_gate_v1@BTCUSDT`, overlay mode, `CARRY_EXECUTION_STYLE=maker_first`, `CARRY_BORROW_APR=0`, testnet.
Frozen constants at `84ba866`: `ASSUMED_HOLD_DAYS=30.0` (`carry_costs.py:117`), `EWMA_ALPHA=0.5` (`:146`), `TAKER_BPS_SPOT=10.0`, `TAKER_BPS_PERP=5.5` (`:66-67`), `NEGATIVE_FUNDING_EXIT_PRINTS=3` (`carry_engine.py:211`), `FUNDING_HISTORY=8` (`:581`).
First 16 hex characters of `sha256` of the files at `84ba866`: carry_costs.py `7c97cda549b3cc07`, carry_engine.py `f426849764f397af`, carry_risk.py `b41e44c466928a66`, carry_broker.py `2da2178c62110d25`, carry_shadow.py `e83bccd4274c49c3`, main.py `ed2391f7270bf25c`, config.py `d5a4cf13f4c947ab`, promotion_gate.py `1d5d8ea63fd9430e`.
Rule: if any of those files changes in behaviour (comments and tests excluded), the boundary moves and the counts below restart from zero. Bug fixes are allowed only with a dated entry in the amendment log, and they restart the clock if they touch decision logic.

## 3. Sample-size honesty (computed, not asserted)

From the repo's own modelled completed trades (48 trades, taker fee pricing, $100k notional, same corpus): mean net $676, standard deviation $2,808 (ratio 4.15), 52% of trades lose money, trade frequency about 11.9 per year.
Trades needed for 80% power at one-sided 5%, using n = ((1.645+0.84) x sd / mean)^2:

| If the true mean is... | Trades needed | At 11.9 trades/yr |
|---|---|---|
| 100% of in-sample | 107 | about 9 years |
| 50% of in-sample | 426 | about 36 years |
| 25% of in-sample | 1,705 | about 143 years |

**Conclusion: 20 forward trades cannot establish an edge.** The 20-trade/180-day gate (`promotion_gate.py` `MIN_FORWARD_TRADES`, `MIN_FORWARD_DAYS`) is therefore treated as a *mechanics and no-contradiction* gate. Per-trade outcomes are dominated by basis and holding-time noise; funding is the stable component, so this plan also tracks per-settlement funding capture (section 4, P2), which has about 90 observations per month while in position.

## 4. Pre-declared measures

| ID | Measure | Source |
|---|---|---|
| P1 | Closed forward trades and days since boundary | `carry_shadow.py forward-report` |
| P2 | Funding received per day in position, in bps of notional, per distinct 8h settlement (not per hourly observation) | shadow book, runner monitor |
| P3 | Net forward P&L after fees, labelled REALISED only when every fee is venue-confirmed | `carry_report.py` on the drill state DB |
| P4 | Share of net P&L carried by the single best trade | derived from P3 |
| P5 | Max drawdown of cumulative net P&L, % of notional | derived |
| P6 | Gate-open share on real mainnet public data, using distinct funding prints | `mainnet_public.py report` |
| P7 | Measured slippage versus the assumed 1.0 bps per leg at the working size | `mainnet_public.py report` |
| P8 | Liquidation evidence: `liqPrice` and `accountMMRate` captured at every open pair; minimum observed distance | testnet linear drill (inventory F4) |

## 5. Decision rules (thresholds proposed by the drafter; the owner must ratify before signing)

**GO to a *discussion* of capped micro-live** only if all hold:
1. H1 passes: at least 3 complete testnet lifecycles (open pair, held across at least one settlement, unwind) that end flat at the venue and reconcile to the ledger, zero naked-leg events, zero unreconciled fills, one recorded kill-switch drill.
2. P1 reaches at least 20 closed forward trades **and** 180 days (the existing gate, unchanged).
3. P3 net forward P&L is greater than zero and REALISED, not PROVISIONAL.
4. P4: no single trade supplies more than 50% of net P&L.
5. P5: max drawdown at most 2% of notional.
6. P8: liquidation distance was captured on every pair and never fell below the owner-declared floor (floor to be written here before any pair is opened: ______).
7. P6 and P7: mainnet protocol met (500 observations over 30 days as declared 2026-10-27, **plus at least 90 distinct funding prints**), gate-open share and slippage reported, no contradiction with the assumption.
8. The remaining gate items in `slice76_promotion_gate.json` (signed risk memo, M-4, typed acknowledgement) are completed by a human.

**NO-GO (stop and investigate)** on any of: a naked leg, an unreconciled fill, net REALISED P&L below zero at 20 closed trades, drawdown above 2% of notional, or a decision-logic change not logged.

**INCONCLUSIVE (default)** otherwise. Inconclusive means keep collecting; it never converts to "profitable". The only permitted claim at any point is the narrow one the evidence supports, e.g. "mechanics verified on testnet", never "edge".

## 6. Prohibited

Retuning any constant after seeing forward data; extending or shortening the window to rescue a result; dropping or merging trades; redefining a closed trade; excluding a period; quoting testnet P&L as profit; trading mainnet before the gate; adding symbols to this registration. ETH/SOL carry would be a **new, separately registered family** with its own boundary and would not be pooled with this one. It would raise the number of trades per year, but not remove the need for the same discipline.

## 7. Reporting cadence

Weekly (read-only): `scripts/runner_status.sh`, `carry_shadow.py forward-report`, `mainnet_public.py report`. Monthly: a dated note appended below with P1-P8 and the verdict (one of GO-discuss, NO-GO, INCONCLUSIVE). Evidence files and hashes are committed, not edited.

## 8. Amendment log

| Date | Change | Restarts clock? | Signed |
|---|---|---|---|

## 9. Sign-off

Owner: ______________________  Date: ____________  Commit hash of this file: ____________
(Nothing in this plan authorises an order, arms live trading, or clears a kill switch.)
