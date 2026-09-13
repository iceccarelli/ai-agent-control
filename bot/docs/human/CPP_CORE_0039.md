# The native core — where C++ pays, and where it does not

Written 2026-09-13 by 0039. Measured first, built second.

## The measurement that decides the question

On this tree, before a line of C++ existed:

| | |
|---|---|
| one `CarryEngine.on_candle` decision | **1.8 µs** |
| decisions the book actually makes | **3 per day** (the funding clock) |
| one 4-year settlement simulation, 4,500 prints | **5 ms** |

So on the decision path Python is roughly **five orders of magnitude faster
than necessary**. A book that holds for a median of 5.3 days and decides three
times a day does not lose a cent to interpreter overhead, and the order path is
bounded by a 50–200 ms round trip to Bybit that no language changes.

**Nothing in this repository trades faster because of C++, and nothing ever
will.** If that is the pitch, it is a lie.

## Where Python genuinely is too slow

Search. Every open question in this project is a sweep:

- which exit rule survives out of sample (0032 measured the current one
  leaving on three negative prints, median hold 5.3 days, median trade
  collecting $27 against a round trip it cannot pay for);
- what a maker fill does to the 11 bps round trip, queue position included;
- where impact eats the edge at size, against real L2 rather than a declared
  haircut;
- what the wick does to the short leg at tick resolution, where the current
  answer is "4h highs are a floor on it".

Those are 10⁴–10⁷ simulations. At 5 ms each, a morning's question becomes a
week.

## What was built

`cpp/carrycore.cpp` — one translation unit, no dependencies, a C ABI, loaded
by `ctypes` (no pybind11, no wheel, nothing to install). It reimplements the
settlement-clock simulator **and** `carry_costs.evaluate_entry` gate for gate,
in the same order, so the two agree.

Measured on the same 4,500-settlement corpus:

```
  python      6.23 ms/run     net +29,696.41
  c++         3.18 ms/run     net +29,696.41     difference $0.0000
  sweep       1,440 configurations in 42 ms = 34,056/s, 29 us each
  the same grid in python: 9 s                   212x
```

A single call is marshalling-bound (4,500 structs across the ABI each time);
the sweep marshals once, which is why it is the honest number. Two cores here;
it scales with them.

**The differential test is the point.** `tests/test_carry_core.py` runs six
configurations — overlay and acquire, gated and ungated, three borrow rates,
with and without impact — and fails the build unless every term (funding,
basis, fees, borrow, impact, net) matches the Python to **the cent**, and
unless the trade counts match exactly. A fast simulator that disagrees with the
slow one is not fast, it is wrong.

## The tool that uses it, and the discipline it carries

`tools/carry_sweep.py` scores 1,440 rule configurations in 43 ms — and then
**refuses to name a winner**:

- every run writes its **search width** to `hypothesis_registry.json`, because
  a multiple-testing correction that does not know how many cells were looked
  at is not a correction;
- `--best` demands an out-of-sample window and asks the read ledger whether
  anything has touched it. Everything through 2026-09-09 has been read, so the
  answer today is a refusal naming the reads, and the only admissible holdout
  is **forward**.

The in-sample map prints under a banner saying exactly that. For the record,
because hiding it would be worse: the best in-sample cell is +11.98%/yr
against the engine's current +7.23%, and it is **not adopted**. That is the
same shape of number that cleared `funding_carry_fade_btc_v1` and then lost
money forward twice.

## What the core does not touch

No orders, no venue, no keys, no state between calls, no network headers. A
test greps for every one of those. The engine, the broker and the gates stay
Python: they are where correctness matters and where 1.8 µs is already
free.

## Next, if the search says so

1. **Tick replay** — 1-minute and L2 events instead of 8-hour settlements.
   That is 2.1 M bars per year and the same simulator with a finer clock; it is
   the first thing that genuinely needs the core rather than merely enjoying it.
2. **Maker queue simulation** — fill probability at the touch, which turns the
   11 bps round trip into something between 4 and 11 and decides whether the
   median trade pays for its exit.
3. **A risk kernel on the mark stream** — the one place where latency, not
   throughput, is the argument, and only once the book is levered.
