# 0044 — a book that can say what it owns, and prove it

## What was missing

The engine knew `funding_collected` and nothing else. **No fee was ever
booked.** No balance was ever reconciled against the venue's wallet. There was
no statement and no journal, and so no way to answer the only question a client
actually asks: *what did I earn, and where is it?*

INVENTORY **D11** has said so since 0033 — *"`cumExecFee` carried per leg;
booking it is Phase E"* — and 0043 showed what the gap costs: funding was booked
on the entry price for four years, a quarter of it missing, and nothing in the
system could notice, because nothing added the books up.

## The one rule

**Every entry balances to zero in USD or it is refused.** Nothing is written and
the caller gets an exception, not a warning.

That single rule is what makes a P&L checkable instead of asserted. A fee that
vanishes, a funding payment booked twice, a realised gain with no matching
cash — each fails to balance, and each fails at the moment it happens rather
than in a quarterly statement.

The journal is **append-only**. There is no delete and no edit; a test asserts
the methods do not exist. A mistake is corrected by a reversing entry that is
itself in the journal. A ledger you can edit cannot detect a bug, a partial
fill, or a fat finger — the argument `reconcile_pair` has made since 0022,
applied to money instead of quantity.

## Why it wraps the broker

A ledger the engine has to remember to call is a ledger the engine can forget to
call, and the forgetting is silent.

The broker is the engine's **only** path to the venue. Every leg, in every
execution mode, under every execution style, goes through `place_market` or
`place_post_only`. `LedgerBroker` decorates it, so a fill is booked because
there is nowhere else for it to go — and no code path anyone adds later can
route around it.

Delegation is **explicit**: there is no `__getattr__` that forwards whatever it
is handed. A broker method added later must be routed deliberately, because the
question for each one is always *does this move money?*, and a wrapper that
guesses answers it silently and wrongly. A test asserts an unknown method raises.

## What it books

| event | entry |
|---|---|
| spot BUY | BTC in, USDT out, **fee taken in BTC** |
| spot SELL | USDT in, BTC out at cost, realised to equity |
| perp open | the short as a quantity; a derivative costs no principal, so only the fee moves cash |
| perp close | realised against the weighted entry price, plus the fee |
| funding | signed — a print that was *paid* is booked (D10) |
| borrow | financing on the spot leg |
| revaluation | the hedged coin marked from entry to exit |

**The spot fee is charged in BTC** — INVENTORY **F3**, now auditable instead of
open. A Bybit spot market BUY takes its taker fee in the coin, so the wallet
holds `cumExecQty − fee` while the perp leg hedges `cumExecQty`, and
`reconcile_pair`'s dust tolerance is 1e-6. `hedgeable_btc()` returns what the
wallet holds, not what filled.

**An unknown fee is never booked as zero.** `LegFill.fee` is `Optional` for a
reason; a cost that reads as $0.00 has left the ledger. They are counted, and
the statement reports how many the venue never stated.

## The cross-check

The replay keeps its own running totals. The journal builds the same number out
of 314 postings, every one of which had to balance before it was written. Over
the full Bybit corpus, in three cost configurations:

| | replay | ledger (realised) | + open position | agrees |
|---|---|---|---|---|
| no impact | $31,498.25 | $31,489.84 | $8.41 | ✓ |
| impact 1.093 bps | $29,766.40 | $29,768.92 | −$2.52 | ✓ |
| borrow 5% | $15,040.78 | $15,032.18 | $8.60 | ✓ |

**To the cent.** Fees agree exactly, because every fee in the journal came from
a fill the engine actually sent through a broker it cannot go around.

The open position's mark is reported **separately and never blended in**. An
open position's value is an estimate, and this file holds the things that are
not estimates.

## Did the hedge hedge?

    perp realised    $    -145,787
    inventory moved  $     144,836
    residual         $        -951   -0.653% of a leg

Four years, BTC from $23k to $110k, and the two legs cancelled **99.35%** of it.
That residual *is* the basis — the only price exposure a delta-neutral overlay
actually runs. It is not expected to be zero. It is expected to be small, and it
is now measured out of a journal rather than asserted in a pitch.

## Reconciliation observes

`reconcile()` compares the journal's BTC, USDT and perp quantities against the
venue's and returns `RECONCILED` or `INCIDENT`. It **never writes**, and it
never adjusts the journal to agree with the venue: a ledger that edits itself to
match whatever it finds cannot detect theft, a bug, or a fat finger. A test
asserts that reconciling against nonsense adds no entries.

## It is live, not just offline

`build_bot` wraps the real `CarryBroker` in a `LedgerBroker`, loads the journal
from the `carry_ledger` table, and `tick()` posts funding and writes it back.
Funding does not pass through the broker, so it is posted from the number the
**engine** reports rather than recomputed — 0043 is the record of what
recomputing it costs.

`Journal.from_rows` re-validates every entry's balance on load, so a database
edited by hand fails at **startup** rather than in a statement. A test asserts
`build_bot` raises on a tampered journal.

## Still open

- **Nothing has reconciled against a real wallet.** `reconcile()` is exercised
  against the journal's own balances and a stub. F2, F3, F7 and F8 are still
  what the drill measures.
- **`fees_unknown` is 0 in replay** because the harness always states a fee. A
  real venue will not always, and each one is a number a human resolves.
- **No revaluation of an open position is posted**, by design — which means a
  statement cut mid-trade reports realised money only, and says so.
