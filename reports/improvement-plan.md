# Open work

Only what is still open. History is in `git log`. Nothing here retunes a signal or relaxes a gate. Status as of 2026-10-29, `main` at `2f8a29a`.

## Where the evidence stands

- Forward shadow book `post_gate_v1`: 91 observations over 3.4 days, 1 simulated entry, **0 closed trades**. Verdict `INSUFFICIENT_FORWARD_SAMPLE`.
- Public-market protocol: 66 observations over 60.3 hours, but only **9 distinct funding prints**. Gate open on 46 of 66 observations, so the share is far less independent than the count. The frozen verdict needs 500 observations over 30 days (earliest about 2026-11-26).
- Measured slippage on 64 public order books: round trip median 0.85 bps at $100k, against 1.0 bps assumed per leg. 4% of legs at $100k cost more than assumed.
- Historical replay (`tools/carry_replay.py`, defaults): +$33,049.51, +8.05%/yr on a fixed $100,000 notional. It reproduces exactly. It is in-sample, assumes zero financing and zero-latency fills, trades only the perp leg, and is not evidence of profit. Financing is the dominant lever, not fees: the same engine returns +8.05%/yr at 0% borrow, +5.87% at 3% (25 trades) and +3.53% at 5% (6 trades, 77% of the net from one trade). Maker-first orders cut fees from $5,426 to $3,712 and add only +0.55 points (+8.60%/yr), and that is an upper bound because the replay assumes every resting order fills. It is also concentrated: one 190-day hold (2023-10 to 2024-04, a bull-market funding regime) supplied 56.9% of the net, the best five trades 90.0%, and 51% of the 49 trades lose money. Without the best trade it is +3.47%/yr, without the best five +0.81%/yr (both at 0% borrow charged) (`tools/carry_replay.py` now prints this on every run, along with the return on total capital: +7.3% / +6.7% / +6.0%/yr at an assumed 10% / 20% / 33% perp margin). By regime (`tools/funding_regimes.py`, rank-thirds of the 17 quarters, no tuned threshold): average funding was only 6.77%/yr, only 3 of 17 quarters ran above 10%/yr, 76.2% of the replay's net was earned in the highest third of quarters, and the lowest third (6 quarters) earned $415 in total. The last two full quarters (2026Q1 +1.54%, 2026Q2 +0.86%) were low, which is why the forward book has so few entries; the latest 90 days average +3.99%/yr. Does the entry gate avoid the lean quarters? Partly: at 0% borrow it is in the market 58% / 87% / 95% of the days in the lowest / middle / highest third and earns $1.38 / $16.33 / $58.09 per day there; at 3% borrow the lowest third is 27% of days at -$5.11 per day, and at 5% the book is out of the lowest third entirely. So the strategy earns in rich funding regimes and roughly nothing (or a loss, once financing is charged) in lean ones. This describes a corpus already seen: it is not a forecast.
- **The headline assumes margin that cannot run out.** With the production liquidation gate on at a declared isolated leverage (a scenario, not the venue's engine), the same replay returns +3.48%/yr at 2x, +6.26% at 3x and +1.60% at 5x. At 10x it used to churn (open, unwind on the next read because the liquidation price sat inside the 15% floor at entry, open again): 830 trades, -190%/yr. It now flattens and halts after one trade (-$942). `DEFAULT_LEVERAGE` is 1 in production; anything above 6 is contradictory with the floor.
- **Where trades leak:** the 19 trades held under 7 days win 5% of the time and lose $108 each (-$2,054); the 13 holds of 30+ days win 100% and earn $33,925 (more than the whole net). The gate's own input (8-print EWMA) is the same at entry for short and long holds (8.0 / 8.0 / 7.5), so it cannot predict persistence; no threshold was tuned from this.
- **Longer history (Binance funding as a proxy; its 2022Q3+ mean, 6.83%/yr, matches Bybit's 6.77%):** before 2022Q3 BTC funding averaged 18.2%/yr (7 of 12 quarters above 10%), so the 4-year corpus is a lean stretch of a longer cycle. BTC and ETH quarterly funding correlate 0.98 (ETH adds almost no diversification); SOL has severe tails (one quarter near -119%/yr), and an equal-weight three-asset book is worse than BTC alone (mean 8.3% vs 11.1%, worst quarter -38%).
- Nothing is established about forward profitability, liquidation behaviour of a long-held short, or venue-confirmed fees over a full lifecycle.

## Needs a decision from the owner

| # | Item | Why it matters | Safe next step |
|---|---|---|---|
| D1 | `bot/state/trading_state.db` is tracked in git and is also your working database (kept `skip-worktree`) | Test fixture and operator state share one path; the kill-switch history depends on a git-tracked binary | Back up the working file outside the tree and record its hash; generate any needed fixture inside tests; stop tracking; restore the working file. The removal must not be pulled until the backup exists |
| D2 | No LICENSE | Required before any outside use | Owner chooses |
| D3 | Key rotation of the original Bybit keys is unverified (`bot/DEPLOYMENT.md`) | The doc says treat them as compromised | Confirm in the Bybit console |
| D4 | Should a closed trade reconcile when the same capture holds an unclaimed second entry? | Today it can; blocking it may reject legitimate partial claim sets | Decide; then add `UNCLAIMED_EXECUTION` to the trade and order paths |
| D5 | `setup-flyctl@master` is not pinned to a commit | Secrets are now scoped away from it, but the action can still change under you | Supply `gh api repos/superfly/flyctl-actions/commits/master --jq .sha` |

## Engineering still open

| # | Item | Notes |
|---|---|---|
| E1 | Capture `liqPrice` and `accountMMRate` at every open pair and replay them, so the liquidation gate is evaluated and not `NOT_EVALUATED` | Holds are long (mean 16.9 days, 15% at least 30 days in the audited replay) and a short perp's liquidation risk grows with time held |
| E6 | Promotion-gate human items are booleans in a JSON file in the same repo, with no signature | A PR could flip them; the live-chain predicate still has to pass |
| E7 | Kill switch is checked in the risk gate and the carry tick, not at the transport | Deliberate: exits and unwinds share `/v5/order/create`. A new caller that bypasses the gate also bypasses the switch |
| E8 | `simulate()` in the Python simulator hard-codes the funding window and EWMA constants that the C++ core takes as parameters | A sweep with non-default values is not Python-equivalent |
| E10 | Referenced documents are absent (`EDGE.md`, `AGENT_CONTROL_PLANE.md`, `PROMOTION_GATE_MICRO_LIVE.md`, `PAPER_AGENT_CERTIFICATION.md`) | Code and artifacts cite them; restore the originals or remove the citations |
| E11 | The reconciliation stack is Bybit-specific and unpackaged (field names `cumExecQty`, `execId`, `orderLinkId`; `sys.path` inserts) | Only worth doing once someone outside the project says they would run it |

## Rules that stay in force

No constant is retuned after forward data is seen. The forward book is never re-initialised. A testnet result is never quoted as profit. Live trading, any profit claim and any real capital stay blocked until the frozen protocol has venue-reconciled closed forward trades.
