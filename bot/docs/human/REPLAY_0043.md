# 0043 — the numbers came from a program that is not the trading program

## What was wrong

`tools/carry_backtest.py` does not import `CarryEngine`. It never has.

It shares exactly one thing with the trading program — `carry_costs.evaluate_entry` —
and reimplements everything else in its own loop: sizing, both-legs-or-neither,
the negative-funding streak, the delta band, the margin floor, the unwind, the
fee accounting, the day's entry allowance.

So **every number this repository has ever published** — +7.23 %/yr, the
walk-forward lines, the 0039 sweep, the maker-first comparison — came from a
program that is not the one that would trade. Under a banner reading:

    [x] rule_is_what_the_engine_runs

That box is true of the **gate**. It was never true of the **state machine**,
and the state machine is what decides whether an order is sent.

## What replaces it

`tools/carry_replay.py` drives the real `CarryEngine` through history along the
exact path `main.tick` uses against a live venue:

    take_snapshot(broker, ...)  ->  view.assert_fresh()  ->  engine.on_candle(...)

against a `ReplayBroker` that answers every call the engine and the snapshot
make. Nothing about the decision is reimplemented. The harness supplies a venue
and a clock; the engine does the rest, including refusing.

    python3 tools/carry_replay.py --repo . --diff

## The verdict

| | engine | simulator |
|---|---|---|
| trades | 79 | 79 |
| in market | 90.98 % | 90.98 % |
| funding | $39,431.80 | $39,444.21 |
| fees | −$8,711.51 | −$8,780.94 |
| basis + impact | −$953.90 | −$967.50 |
| **net** | **$29,766.40** | **$29,695.77** |
| **annualised** | **+7.25 %/yr** | **+7.23 %/yr** |

**The published number survives contact with the real class**, and the $70.63
that remains is attributed to the cent:

- **$66.64** — one trade is still open when the window ends. The simulator
  marks it closed and charges an exit fee; the engine has not sent that order,
  so it has not paid it. *The engine is right.*
- **−$12.41** — the engine snaps every size to Bybit's 0.001 lot step, which
  the simulator has no concept of, so it holds fractionally less and earns
  fractionally less funding. *The engine is right.*
- **−$0.20** on basis, over $778. With impact off the two agree to twenty cents.

`test_they_agree_on_the_trades_and_the_money` pins this, and
`test_the_disagreement_that_remains_is_an_exit_that_never_happened` pins the
attribution. Neither could exist before this commit.

## Two defects in the engine, found by making it replayable

### D23 — the risk gate read the wall clock

`CarryRisk.gate_open` and `record_entry` both dated an entry from
`datetime.now(utc)`, while `CarryEngine._open` had the tick's `timestamp_ms` in
hand and never passed it.

Live those are the same clock, so **this is not a live bug** and it is not
reported as one. What it is: a risk limit that can only be observed in
production. Replay 4,500 settlements and every one of them lands on today; the
day's allowance is spent on the first and never returns; the book stands aside
for four years. The first run of this harness opened **one** trade in 4.11
years and refused 3,571 times with `ENTRY_LIMIT_REACHED_TODAY`.

A daily limit that agrees with the calendar only in production is a daily limit
nobody can test. The engine now passes the tick's market time; behaviour live
is byte-identical.

### D24 — funding was booked on the entry price

`funding_collected += (funding_bps / 1e4) * self.position.perp.notional`, and
`notional` is `filled_qty * avg_price` — **frozen at the fill**.

The venue pays funding on the position's value at the **settlement mark**. Over
this corpus the engine booked **$29,371 where the same 79 trades earn $39,444**
— a quarter of the funding missing, because BTC rose while positions were held
and the entry price never moved.

It changes no decision; nothing gates on `funding_collected`. It is the number
a client is shown as *funding collected*, and Phase E calls itself a bankable
ledger. Worse, **the error's sign follows the price** — understated in a rising
market, overstated in a falling one. A book whose entire claim is that price
direction does not matter must not report a P&L whose error is a function of
price direction.

Fixed to book `filled_qty * mark`. The residual — that `mark` is the tick's
mark, at most `LOOP_INTERVAL_SECONDS` from the venue's settlement mark — is
named, not hidden.

## Three defects in the harness, found by diffing

Worth recording, because they are the argument for the diff existing:

1. **The overlay's basis dropped the long leg.** The book never *trades* the
   client's spot, but the spot is still what the short is hedging. Dropping it
   turned a delta-neutral pair into a naked short and printed **−32.76 %/yr**
   for a position that is flat to price.
2. **Funding was read after the unwind**, missing the print booked during it —
   and a book exits on negative prints by construction, so the miss flattered
   the replay by **$392**.
3. **The exit price came from the bar, not the fill**, so the closing leg's
   impact haircut vanished — **exactly half** the total impact, $882.

Each was found by the number failing to reconcile, not by reading the code.

## Feeding it your own data

`--bars` replays a supplied settlement series instead of the repo corpus:

    python3 tools/carry_replay.py --bars my_settlements.csv --diff

CSV with a header, or JSON as a list of objects. **One row per funding
settlement**, not per candle — the engine's clock is the settlement clock
(0034), so a file of 1-minute candles is resampled to settlements first.

| column | meaning |
|---|---|
| `ms` | settlement stamp, epoch **milliseconds**, UTC |
| `perp` | perp mark at that settlement |
| `spot` | spot price at that settlement, **same venue** |
| `rate` | the **settled** funding rate as a **fraction** (0.0001 = 1 bp) — not bps, and not the ticker's forecast |
| `perp_high` | perp high over the interval ending at this settlement |
| `perp_low` | perp low over the same interval |

It refuses rather than guesses: a missing column is named; a `rate` above 5 %
for one 8h print is rejected as "almost certainly bps, not a fraction" (the
commonest upload error); duplicate stamps are refused rather than deduplicated,
because two prints with one stamp means the file was built from two sources and
nobody knows which is right.

## What this harness does not model

Named in every report, not buried:

- **latency** — a fill happens at the price the decision was made on;
- **fill probability** for post-only orders — every rest fills (0040 F10);
- the ticker's funding **forecast** — the settled print is used, because the
  only forecast available offline is the next print, which is the future;
- `positionIM / positionMM` — a declared constant that never trips the margin
  floor, because INVENTORY **F4** says that field is a risk-tier ratio and not
  liquidation headroom, and there is no honest way to reconstruct it offline;
- partial fills, rate limits, venue rejections.

The first four are exactly what the Phase D drill exists to measure, and none
of them can be closed by more code.
