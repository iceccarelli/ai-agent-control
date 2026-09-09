# CORPUS POLICY — frozen research record, live venue book

Decided 2026-09-09, after refreshing the committed corpora broke three tests.

---

## The decision

**The corpora committed to this repository are FROZEN. They are not refreshed
in git.**

## What forced it

Running `append_closed_corpus.py --write` brought all three series current and
immediately failed the suite at 5,322 instead of 5,333:

```
tests/test_operator_tools.py                    pins linear sha 293774ee…
                                                pins funding sha c652c9b0…
tests/test_slice76_fifteen_day_window.py        pins 1,476 rows
tests/test_slice77_selection_contamination.py   pins 1,476 rows
```

Those are not brittle tests. They are **provenance**. `test_slice76` asserts
facts about the 1,476-bar corpus that slice 76 actually measured. Updating them
to 1,491 would retroactively falsify what slice 76 saw — the same reason sixteen
historical tools are digest-pinned and are allowed to be un-runnable.

There were three options and only two were coherent:

| | |
|---|---|
| **A** freeze a copy, refresh a live one | correct, costs ~25 MB and a patch |
| **B** don't refresh in git at all | **chosen** |
| ~~C~~ update the pinned numbers to match | editing the record to agree with whatever was just fetched — precisely what `tools/reserved_holdout.py` exists to detect |

## Why B

The live book **does not read these files.** It reads the venue through
`MarketSnapshot`, which has its own freshness gate, its own clock-skew check,
and refuses rather than trading on a view it cannot vouch for.

The corpus exists to answer one question: *what did the backtest measure?* A
frozen answer is the only useful kind.

## What this means in practice

**Refreshing is a working-copy act.** Refresh, run the backtest, read the
number, and `git restore` the data files. Never commit them.

```bash
python3 tools/append_closed_corpus.py --write
python3 tools/append_spot_corpus.py --write
python3 tools/carry_backtest.py --repo . --matrix
git restore --staged --worktree bot/data/       # note: --staged too
```

`git checkout -- bot/data/` is **not** enough once the files have been staged.
That cost two rounds to spot.

**Staleness is not a defect on this corpus.** `corpus_health` still reports it,
because an operator should see it, but `carry_backtest` runs
`corpus_verdict(repo)` with `max_stale_days=10_000` and refuses only on
STRUCTURAL defects:

```
OPEN_BAR_IN_FILE   a day priced as closed that had not finished
NOT_MONOTONIC      timestamps out of order
DUPLICATE_DAYS     a daily bar repeating a date
EMPTY / MISSING
```

A number computed over any of those is **wrong**, not old. A number computed
over a stale-but-intact corpus is simply a number about that period.

**Every backtest now publishes the digests it was computed from.** A figure
without its provenance is a rumour, and this programme has already been burned
once by a number (`6.84%/yr`) that turned out to be an input rather than a
result.

## When this decision should be revisited

If a research question needs current data with the same rigour the slice tests
have, take option A: copy the frozen corpus to `data/frozen/slice76/`, point the
provenance tests there, and let `data/real_*` move. That is a deliberate patch
with its own tests, not something to do by accident while refreshing.

---

```
signed:
date:
git commit at signing:
```
