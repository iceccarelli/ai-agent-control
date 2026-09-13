# OVERLAY — the book stops buying BTC the client already owns

Written 2026-09-13 by 0036. Measurement and execution, not a decision.
Nothing here is quotable; the tool still prints why.

## The gap this closes

`PHASE1_DECISION` (2026-09-08) chose the product: **an overlay on BTC that is
already held**. The engine did not implement it. Every entry bought spot and
every exit sold it, so the book paid a four-leg round trip and financing for
inventory the client already had.

0032 measured what that costs once the simulation runs on the real settlement
clock: **$27,232 of fees against $39,411 of funding**, 87 trades, −2.99%/yr at
5% financing. The fees were not a detail. They were the product.

## What 0036 changes in the execution

| | ACQUIRE (as built) | OVERLAY (0036) |
|---|---|---|
| Entry | buy spot, short perp | short perp only |
| Exit | buy perp, sell spot | buy perp only |
| Round trip | 2×spot + 2×perp = 31 bps | 2×perp = **11 bps** |
| Financing | borrow on the spot leg | none — the BTC is the client's |
| A leg that does not fill | naked spot → unwind immediately | nothing naked; the client is as they were |
| A partial perp fill | unpaired → unwind | a smaller hedged slice, which is a correct hedge |
| The client's other BTC | n/a | untouched and unhedged, by design |

The mode is a required, explicit decision: `CARRY_EXECUTION_MODE` has no
default and `build_bot` refuses without it, because the two modes send
different orders.

Two other things the venue now decides instead of the source:

- **Lot rules** (`/v5/market/instruments-info`). Linear BTCUSDT has
  `qtyStep = minOrderQty = 0.001`. The engine sized `cap / mark` = 0.00127 BTC
  at $79k, which is a **rejected order**, not a small one (INVENTORY F5). Size
  is now snapped down to the step and refused below the minimum.
- **Fee rates** (`/v5/account/fee-rate`). The round trip is read from the
  account's own tier and handed to the cost gate. A constant in the source is
  a cost model that quietly disagrees with the invoice.

## The numbers

Same corpus, same clock, same impact haircut, same gate constants — only the
execution changes. `--clock 8h --mode overlay --matrix`:

| borrow | ACQUIRE ungated | OVERLAY ungated | ACQUIRE gated | OVERLAY gated |
|---:|---|---|---|---|
| **0%** (the product) | +2.24%/yr n=87 | **+6.99%/yr n=87** | +3.96%/yr n=66 | **+7.23%/yr n=79** |
| 3% | −0.90% | +3.85% | +2.60% | +4.65% |
| 5% | −2.99% | +1.76% | +1.70% | +3.38% |
| 8% | −6.13% | −1.39% | +2.35% | +1.56% |

Overlay, 0% borrow, ungated, the identity:

```
funding  +39,411.36
basis       +866.55
fees      -9,660.09      (was -27,232.18)
borrow         0.00      (was -21,490.23)
impact    -1,920.43      (one leg, not two)
= NET    +28,697.38      +6.99%/yr   residual 1e-11
```

The gate admits MORE entries in overlay (79 vs 66) because it is told the
truth about the exit: at 31 bps it refused trades that pay for themselves at
11. No gate constant moved — `ASSUMED_HOLD_DAYS`, `EWMA_ALPHA`,
`MIN_ENTRY_FUNDING_BPS`, `NEGATIVE_FUNDING_EXIT_PRINTS` are untouched. The
round trip stopped being a constant and became an input.

## What is still true, and still uncomfortable

- **Not quotable.** Three of five conditions hold; the rule is in-sample and
  its only admissible holdout is forward, after 2026-09-09T08:00Z. Registered
  as `carry_overlay_v1@BTCUSDT` (32 trials, family-wise FPR 80.6%).
- **The exit rule is still the biggest lever.** Median hold 5.3 days ≈ 9.5 bps
  of carry against an 11 bps taker round trip: the median trade still does not
  pay for its own exit, and the aggregate is carried by a few long holds
  (best trade +$18,451 of 87). Maker execution (2 bps/leg on this account's
  tier → 4 bps round trip) is the next lever, and a changed exit rule is a new
  hypothesis that may only be scored forward.
- **62 of 87 trades still lose money.** A carry book is a few long holds and a
  lot of small losses; size it knowing that.
- **The short leg still needs margin.** Over a months-long hold the perp's
  unrealised loss is the price rise itself, absorbed by the client's BTC as
  collateral or liquidated. `positionIM/positionMM` is not that measurement
  (INVENTORY F4) and is still open.
