# Open work

Only what is still open. History is in `git log`. Nothing here retunes a signal or relaxes a gate. Status as of 2026-10-29, `main` at `2f8a29a`.

## Where the evidence stands

- Forward shadow book `post_gate_v1`: 91 observations over 3.4 days, 1 simulated entry, **0 closed trades**. Verdict `INSUFFICIENT_FORWARD_SAMPLE`.
- Public-market protocol: 66 observations over 60.3 hours, but only **9 distinct funding prints**. Gate open on 46 of 66 observations, so the share is far less independent than the count. The frozen verdict needs 500 observations over 30 days (earliest about 2026-11-26).
- Measured slippage on 64 public order books: round trip median 0.85 bps at $100k, against 1.0 bps assumed per leg. 4% of legs at $100k cost more than assumed.
- Historical replay (`tools/carry_replay.py`, defaults): +$33,049.51, +8.05%/yr on a fixed $100,000 notional. It reproduces exactly. It is in-sample, assumes zero financing and zero-latency fills, trades only the perp leg, and is not evidence of profit. With 5% borrow the same engine gives about +3.5%/yr.
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
| E2 | Report return on total capital (spot inventory plus perp margin), not only on notional | Decides whether the yield is worth running |
| E3 | Add max drawdown and best-trade share of net to every report | Needed to judge whether returns are spike-driven; the forward plan already requires both (P4, P5) |
| E4 | Ledger cross-check: compare `funding_venue_usd` and `funding_settlements` with the reconciled settlements | Today a ledger may state a funding total the captured settlements do not support |
| E5 | `statement()` is called without `now_ms`, so the stale-record age-out never runs for an open lifecycle | Unverified end to end; reproduce before fixing |
| E6 | Promotion-gate human items are booleans in a JSON file in the same repo, with no signature | A PR could flip them; the live-chain predicate still has to pass |
| E7 | Kill switch is checked in the risk gate and the carry tick, not at the transport | Deliberate: exits and unwinds share `/v5/order/create`. A new caller that bypasses the gate also bypasses the switch |
| E8 | `simulate()` in the Python simulator hard-codes the funding window and EWMA constants that the C++ core takes as parameters | A sweep with non-default values is not Python-equivalent |
| E10 | Referenced documents are absent (`EDGE.md`, `AGENT_CONTROL_PLANE.md`, `PROMOTION_GATE_MICRO_LIVE.md`, `PAPER_AGENT_CERTIFICATION.md`) | Code and artifacts cite them; restore the originals or remove the citations |
| E11 | The reconciliation stack is Bybit-specific and unpackaged (field names `cumExecQty`, `execId`, `orderLinkId`; `sys.path` inserts) | Only worth doing once someone outside the project says they would run it |

## Rules that stay in force

No constant is retuned after forward data is seen. The forward book is never re-initialised. A testnet result is never quoted as profit. Live trading, any profit claim and any real capital stay blocked until the frozen protocol has venue-reconciled closed forward trades.
