# 0040 — join the book instead of crossing it

## The lever, measured

The overlay's round trip is two perp legs. This account's tier:

| | per leg | overlay round trip |
|---|---|---|
| taker | 5.5 bps | **11.0 bps** |
| maker | 2.0 bps | **4.0 bps** |

Scored on the Bybit settlement clock, same-venue, impact applied, engine's
gated rule, 0% borrow, $100k notional, 2022-08-01 → 2026-09-09 (4.11 yr,
4,500 prints, 79 trades):

| fills charged at | fees | net | annualised | losing trades |
|---|---|---|---|---|
| 5.5 bps/leg (taker) | $8,780.94 | $29,695.77 | **+7.23 %/yr** | 54 of 79 |
| 2.0 bps/leg (maker) | $3,193.07 | $35,283.64 | **+8.59 %/yr** | 42 of 79 |

Reproduce:

    python3 tools/carry_backtest.py --repo . --clock 8h --venue bybit \
        --mode overlay --gated --borrow-apr 0.0 [--exec-fee-bps 2.0]

**+1.36 %/yr is the UPPER BOUND**, not the expectation. `--exec-fee-bps 2.0`
charges every leg the maker rate, which asserts that every entry rested and
filled. Nothing offline can establish a fill probability, so the realised
value is `fill_rate × 1.36 %/yr` and the honest forecast is unknown until the
drill. It is an upper bound in one more way: the perp leg's 2.0 bps is the
tier, and a resting order that is never hit pays the taker rate anyway.

## The spread is not the argument

Bybit's BTCUSDT touch is 0.1 USDT wide — 0.013 bps on a $100k mark. Crossing
it is free. The whole 7 bps is the fee tier, which is why this is worth a wait
and why the wait can be short.

## What rests, and what never does

**Only the overlay's entry.** One order, and until it fills nothing has
changed: the long side was the client's before this book existed, so nobody is
naked while it sits in the queue. That is what makes the wait free.

Everything else crosses, immediately, whatever `CARRY_EXECUTION_STYLE` says:

- the unwind, the margin-driven exit, the emergency sale of a naked spot leg,
  the delta rebalance, the buy-back of a short that ran past the inventory —
  every one of these REMOVES an exposure, and against the risk of not filling
  at all, a cheaper fill is worth nothing;
- **both ACQUIRE entry legs.** That mode holds a PAIR, and every second an
  entry leg rests is a second the book is long spot with no hedge behind it.
  "Both legs land or neither" is the rule this engine exists to enforce;
  widening the window where neither is true, to buy a fee tier, would be
  selling the rule for the discount. The measurement above is an overlay
  measurement and the lever stays where the measurement is.

`tests/test_carry_maker.py::test_only_the_two_entry_paths_may_be_patient`
walks the engine's AST and asserts `patient=True` appears in `_open_overlay`
and nowhere else. It is an invariant, not a comment.

## The gate still prices the taker

`CarryEngine.round_trip_bps()` returns 11.0 under both styles. A gate that
priced 4.0 and a fill that came back taker is a trade admitted on a cost it
did not pay. Maker is upside; it is never an assumption. `--exec-fee-bps`
keeps the same separation offline: it changes what the fills are charged and
leaves what the gate prices alone.

## The one dangerous case, and what happens

A post-only order can fail in a way that leaves it **resting at the venue**. If
the caller then crossed, the book would hold two legs where it intended one.

So no error code is interpreted. The venue is asked the only question that
matters — *does this order exist?* — via `/v5/order/realtime` on the
deterministic `orderLinkId`:

| the venue says | meaning | what happens |
|---|---|---|
| no row | the order was rejected; nothing exists | cross instead (not an incident) |
| `New` / `PartiallyFilled` | it is resting | wait `CARRY_MAKER_WAIT_S`, cancel, cross the remainder |
| terminal with a fill | it filled | take that fill; cross the remainder |
| the query itself fails | **unknown** | `PairIncident` → the engine **HALTS** |

A cancel that arrives too late is not an error either: it means the order
filled, and the fill is read back from the order, never from the cancel's own
response.

## Fees are recorded, not modelled

`Leg` now carries `maker_qty` and `taker_qty`. One leg can be both — a
post-only that partially fills and is then crossed is one leg at two rates —
and the split is what lets Phase E reconcile the realised fee against the
invoice instead of assuming a tier. When either half's fee is unknown the
total is `None`, never the known half: a cost booked smaller than the invoice
is a cost that disappears.

## Configuration

| key | default | bounds |
|---|---|---|
| `CARRY_EXECUTION_STYLE` | `taker` | `taker` \| `maker_first` |
| `CARRY_MAKER_WAIT_S` | `2.0` | `(0, 600]` seconds |

The default is `taker` — the path the gate prices and exactly the behaviour of
every build before this one. `maker_first` is a deliberate operator act.

## Still not known

- **Fill probability.** The number this whole change is worth is multiplied by
  it, and it is unmeasured. Phase D's recorded drill is where it is first seen.
- **The wait's price.** The taker fallback fills up to `CARRY_MAKER_WAIT_S`
  after the snapshot the entry was priced on. At 40% annualised vol that is
  about 1 bps of one-sided noise per entry at 2 seconds — zero mean, so not a
  cost, but it is not modelled in any backtest either.
- **Whether Bybit returns a filled post-only order from `/v5/order/realtime`.**
  If it does not, `place_post_only` raises and the book halts: fail-closed, but
  a spurious halt. Verified at the drill (INVENTORY F10).
