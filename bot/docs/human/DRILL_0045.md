# 0045 — the drill, as a program

## What was missing

Phase D is *"venue truth at $100, recorded drill"*, and **there was no drill.**
There was a list of things a human would remember to do in the right order, in
a markdown file, with nothing checking that they happened or that they happened
in that order.

That is the distance between a plan and an asset. A drill that lives in prose
gets half-run at 2am by somebody tired, and its evidence is a terminal
scrollback nobody kept.

## What it is

Twelve stages. Each produces **evidence** — a number the venue actually
returned, never a checkmark — and the drill refuses to advance when one fails.

| stage | what it establishes |
|---|---|
| `reachability` | this host can reach the venue; the marks and the basis |
| `flat` | the venue is not already holding something (if it is: stop) |
| `venue_rules` | lot step and minimum, **read not assumed** (0036) |
| `fee_tier` | what *this account* pays, read not assumed; the round trip |
| `inventory` | BTC the client owns, sized to a whole venue lot |
| `warmup` | the settled prints the EWMA needs before it will decide anything |
| `margin` | headroom against the floor |
| `cap` | the size, which is $100 and gets no exemption |
| `open` | one perp leg against the inventory |
| `pair` | both sides agree — against the **venue**, not against the ledger |
| `unwind` | close it |
| `final_reconcile` | flat at the venue, not flat in our opinion |
| `statement` | what it cost, out of the double-entry journal (0044) |

The transcript it writes is the Phase D deliverable: what was asked, what the
venue answered, what was sent, what came back, what the ledger says.

## Safe by default

Without `--arm` it reads, reports and **sends nothing**. With `--arm` it goes
through the same order gate as every other path — PAPER refuses, mainnet needs
live authorisation *and* a signed promotion gate.

It has no order path of its own. Every order goes through `CarryEngine`, which
goes through the broker it was **handed** — in production a `LedgerBroker`
around a `CarryBroker` built by `build_bot`. Tests assert the module never
constructs a client, never touches `_request`, and that the CLI takes its
broker from `build_bot`. A drill with its own POST would be a second order path
with its own rules, which is the thing this repository exists not to have.

## Why it is tested against a venue that misbehaves on command

Because the sequence has to be right **before** it meets a real one. Every
failure mode is exercised at zero cost in `tests/test_drill.py`: an unreachable
venue, an empty wallet, inventory below one lot, thin margin, a position
already at the venue, a notional above the cap, a perp leg that never lands,
and an unwind that does not fill.

The first run against Bybit will be the hundredth run of this sequence.

The worst outcome has its own test. When the unwind does not fill, the verdict
is `FAILED`, `still_open` is `true`, and `next_action` reads:

> A HUMAN MUST ACT NOW: the book is not flat. Close the perp position by hand
> at the venue, then clear the kill switch. Nothing automated will retry.

## What the drill found before touching a venue

**A freshly started book is blind for 24 hours.**

`evaluate_entry` refuses below `EWMA_MIN_PRINTS` settled prints. Prints come
every eight hours. So a new deploy — a new volume, a first run, a machine the
host moved — stood aside with `INSUFFICIENT_FUNDING_HISTORY` for a full day
while `/v5/market/funding/history` had been returning the last two hundred
prints on request the whole time.

The armed drill hit it on its first run and could not open at all.

`CarryEngine.warm_funding_history()` seeds the EWMA from settled prints, and
`carry_cold_start` reads them from the venue before the first tick. **There is
no look-ahead**: every one is already settled and already paid, exactly what
`on_candle` would have recorded had the process been running. It is catching
up, not peeking.

It refuses to overwrite a history that already has prints in it — a restart
that restored its own ledger knows more than the venue's last eight, including
which prints this book actually held through — and the newest stamp becomes the
watermark, so a settled print is not booked again as if the book had held
through it.

A venue that will not hand the history over is not a reason to refuse to start.
It logs, and the book waits the day.

## Running it

```bash
python3 tools/drill.py                                  # preflight, sends nothing
python3 tools/drill.py --arm --out artifacts/drill.json # the real thing
```

On Fly: `fly ssh console -C "python3 tools/drill.py"` for the preflight, which
answers every question that does not need an order — reachability, rules, fee
tier, wallet, margin — before anything is armed.

## What a pass still does not establish

Printed on every run and written into the transcript, because a document that
lists only what passed will be read as *everything passed*:

- **liquidation behaviour.** `positionIM/positionMM` is a risk-tier ratio, not
  headroom (F4). One $100 pair does not exercise it.
- **rate limits and maintenance windows** (F8): two orders in a minute meets
  neither.
- **the create/query race** (F2): a response arriving after the fill is a
  timing accident this drill cannot schedule.
- **duplicate-order codes** 110072/170130 (F7): nothing here sends a duplicate.
- **post-only fill probability** (F10/F11): the drill is taker.
- **anything at all about size.** $100 is not evidence about $100,000.
