# What to fix, enhance, adapt and implement better

2026-10-29. Every item cites evidence from the audit or from output you pasted. Nothing here retunes a signal or relaxes a gate. "Dominate the niche" is not something evidence can promise; what the evidence supports is a narrower claim: this repo is unusually honest about what it knows, and that is a defensible advantage if it also produces a positive forward record.

## Current verified state

- Full suite on current `main`: 8,932 passed, 1 failed, 4 skipped, 15 xpassed (your Codespace, 2026-10-29).
- Fly runner healthy: 88 monitor cycles 2026-10-27..29, gate open 40/48 cycles on testnet, mainnet public observations recorded, forward shadow book running (1 simulated entry, 0 closed).
- Preflight with economics settings: `ENTRY_ECONOMICS_PASS`, exit 0.
- Not yet established: forward profitability, liquidation behaviour of a long-held short (F4), venue-confirmed fees for a full lifecycle.

## FIX (defects and hazards)

| # | Item | Evidence | Action |
|---|---|---|---|
| F1 | Tracked runtime DB is hidden with `skip-worktree`; your working copy has the kill switch cleared while the committed file has it engaged ("certification test"); the one failing test is this mismatch | `git ls-files -v` shows `S`; hashes differ (`dd862afb...` vs `751e248b...`) | Stop tracking mutable state. Move the live state path out of the tree (default `STATE_DB_PATH`), generate the engaged-switch fixture inside the test instead of committing a database, remove `skip-worktree`. A human decides whether to keep the cleared state |
| F2 | No LICENSE | repo root | Owner chooses; needed before any outside use |
| F3 | Referenced documents missing (EDGE.md, AGENT_CONTROL_PLANE.md, PROMOTION_GATE_MICRO_LIVE.md, PAPER_AGENT_CERTIFICATION.md) | code and artifacts cite them | Restore from the original source, or remove citations |
| F4 | Key-rotation of original Bybit keys unverified | `DEPLOYMENT.md:7-25` | Confirm in the Bybit console |
| F5 | Stale doc numbers (618 tests, 4,953 tests) | `DEPLOYMENT.md`, `constraints.txt` | Update from a single measured run |
| F6 | CI runs a subset with an admitted failing baseline | `carry-verify.yml` header | Run the full suite in CI now that it is green |

## ENHANCE (same logic, better evidence)

| # | Item | Why it matters |
|---|---|---|
| E1 | Capture `liqPrice` and `accountMMRate` at every pair and feed them into replay so the liquidation gate is evaluated, not `NOT_EVALUATED` | The profit lives in long holds (cutting at 14 days keeps 2.3% of net); long holds are where liquidation risk sits (inventory F4) |
| E2 | Count evidence in distinct 8h funding prints, not hourly observations (already begun: "8 distinct latest funding prints") | 63 observations were only 8 independent prints |
| E3 | Report return on **total capital** (spot inventory plus perp margin), not only on notional | The %/yr shown is on a $100k book; capital efficiency decides whether it is worth running |
| E4 | Label per-trade fee basis in the hold audit (it always prices taker; reconciles only with `--style taker`) | Avoids a misleading "reconciles: False" |
| E5 | Add drawdown and best-trade-share to every report | Returns are spike-driven: best 6 of 38 months supplied 67.7% of net |

## ADAPT (the regime and the venue)

| # | Item | Why |
|---|---|---|
| A1 | Treat the modelled +8-9%/yr as a bull-window figure (corpus price rose +168%) and stress it against low-funding regimes | The existing stress scenarios are the right place; report them first |
| A2 | Keep testnet for mechanics only; use `mainnet_public.py` (read-only) for the real market | Tool docstring: testnet basis swings hundreds of bps |
| A3 | Consider a second venue's funding for comparison (Hyperliquid) only as research data | Do not add execution until the BTC book has a forward record |

## IMPLEMENT BETTER (speed of evidence)

| # | Item | Why |
|---|---|---|
| I1 | At about 12 trades a year, BTC alone takes about 20 months to reach 20 closed trades, and about 9 years to reach the 107 trades needed for 80% power if the in-sample mean were real | Computed in `forward-holdout-plan.md` section 3 |
| I2 | Breadth: ETH and SOL carry as separately registered families would multiply trades per year, but they are correlated and each needs its own boundary and gate | Raises the rate of evidence, not the odds of edge |
| I3 | Per-settlement funding capture as the primary economic series (about 90 observations per month in position) instead of per-trade net | Far lower variance than per-trade outcomes |

## What I will not do

Remove or soften a gate to speed things up; tune the constants; present testnet P&L as profit; say the system is "perfectly aligned" or guaranteed to profit. The gates are the reason money survives long enough for any edge to matter. They are the alignment.

## Business track (after a positive forward record, not before)

Reports in `reports/business-model.md` and `competitive-analysis.md` stay valid: the near-term saleable thing is the evidence discipline and the fail-closed execution guard, not the carry yield. Do not sell the yield.
