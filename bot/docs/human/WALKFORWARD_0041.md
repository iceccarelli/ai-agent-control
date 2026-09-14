# 0041 — the one untickable box, and what can be said without it

`carry_backtest --clock 8h --mode overlay --gated` prints **+7.23 %/yr** and
`QUOTABLE: False`, four of five conditions met and one missing:

    [ ] rule_scored_on_an_untouched_window

The gated rule's parameters were settled after 0018 with the whole corpus
visible. Nothing in +7.23 %/yr separates a carry edge that exists from one that
was fitted, and that separation is the whole distance between this book and an
asset.

Only a registered **forward** holdout ticks the box, and it needs settlements
that have not printed yet. This is the strongest thing obtainable today.

## The method

Every 30 days: refit using only what had printed by that boundary, trade the
next 30 days with that choice, never let a choice be scored on a settlement it
was allowed to see. 365-day burn-in, 38 out-of-sample months.

P&L for a segment is a **difference of prefixes** — `net(rows[:t2]) −
net(rows[:t1])`, same cell. Both runs start at row 0, so the EWMA is warm and
the segment is not handed a book that springs into existence flat, and the rule
is credited with the position it would already have been holding under its own
history. Summing the segments reproduces one continuous run **to the bit**;
`test_every_additive_term_telescopes_exactly` asserts it for every additive
term. These lines are a decomposition, not a stack of little backtests.

    python3 tools/carry_walkforward.py --repo .        # 20,520 sims, 1.6 s

## The defect it fixes first

**The gated rule never reads an entry threshold.** `may_open_gated` decides on
the EWMA of funding, the cost of capital and the basis budget. `entry_bps`
appears only in the ungated branch. Measured:

    entry 0.1 bps -> net $29,696.41, 79 trades
    entry 2.4 bps -> net $29,696.41, 79 trades

So 0039's "1,440 configurations" was **60 distinct gated rules printed 24
times**, the number handed to the multiple-testing correction described the
loop rather than the search, and the parameters the shipped rule actually has —
`ewma_alpha` above all — were never searched at all. The axes are now
`hold_days × negative_exit_prints × ewma_alpha`, 540 distinct rules.

## What it found

OVERLAY, $100k, 0% borrow, impact applied, 38 OOS months (2023-08 → 2026-09):

| line | net | %/yr | +months | median mo | trades | fees | in mkt |
|---|---|---|---|---|---|---|---|
| FROZEN (as shipped) | $26,489 | **+8.53 %** | 28/38 | $326 | 57 | $6,353 | 92% |
| WALK-FORWARD (blind refit) | $42,600 | **+13.71 %** | 36/38 | $443 | 6 | $779 | 89% |
| ORACLE (whole window visible) | $43,605 | +14.04 % | 35/38 | $443 | 6 | $781 | 92% |

**1. The parameter surface is stable.** The hindsight premium — what seeing the
whole window bought over refitting blind — is **$1,004 of $43,605, 2.3%**. Four
distinct cells chosen across 38 refits. The best cell is persistently the best,
not a lucky corner.

**2. The shipped exit rule churns, and now there is a number for it.** 57
trades against 6, for the *same* market exposure. $6,353 of fees against $779.
That is INVENTORY D14 stated as plainly as it can be: the book pays 51 extra
round trips to hold the position it was holding anyway.

**3. It churns because it will not sit through two bad days.** Blind refitting
picks `exit 6 prints` — wait two full days of negative funding before leaving —
over the shipped `3`. Funding is positive on 85.4% of prints; three negatives is
a common wobble, six is a regime.

**4. The two lines are different products.** The cheap one is cheap because it
holds **168 days per trade** against the frozen rule's 18. Over that horizon the
adverse excursion on the short leg is the price rise itself — **+168% measured
on this corpus** — which the margin must absorb from the spot collateral or be
liquidated. Whether it can is INVENTORY **F4**, and F4 is open:
`positionIM/positionMM` is a risk-tier ratio, not liquidation distance. The net
column cannot choose between these two books.

**5. Carry earns in spikes.** The best 6 months of 38 carry **58.9%** of the
walk-forward net and **71.7%** of the frozen net. The median month is $326–443
on $100k. A mean that hides that is a mean nobody can size against.

**6. And after the correction, none of it is distinguishable from noise.**

| line | Sharpe | expected max under null | trials | DSR | z |
|---|---|---|---|---|---|
| walk-forward | +0.635 | **+0.663** | 20,520 | 0.393 | **−0.272** |
| frozen (≥) | +0.495 | **+0.506** | 540 | 0.456 | **−0.109** |

On 38 monthly observations, a Sharpe of 0.635 is *below* what the best of
20,520 edgeless rules would be expected to show. The frozen line is charged one
grid rather than one trial, deliberately: its constants were not
pre-registered — they were chosen after 0018 with this corpus visible — so 540
is a floor on its true trial count and −0.109 is an **upper** bound on its
evidence. Skew +3.2 and kurtosis 15.1 say the Sharpe is a poor summary anyway;
the concentration line above is the better one.

## What is NOT concluded

**Nothing is adopted.** The tool returns no winner and the report carries no
`recommended` key — `test_it_names_no_winner_to_ship` asserts it. Pasting
`exit 6` into `carry_engine.NEGATIVE_FUNDING_EXIT_PRINTS` because a search
liked it is rule 20, and it is the exact path that cleared
`funding_carry_fade_btc_v1` and then lost money forward. Changing the exit rule
means registering a new hypothesis and scoring it on settlements that have not
printed.

**Nothing is quotable.** `is_a_quotable_return` is False on every run and the
holdout box stays unticked. Walk-forward reuses a corpus that was read before
the parameters were chosen and reads it 20,520 more times.

The honest reading of all six findings together: *the rule family is stable and
the shipped exit rule is measurably worse than the family's own centre, but the
whole corpus is too short and too spiky to establish that any of it beats zero.*
That is not a reason to stop. It is the reason the forward holdout is the next
piece of work that matters, and it is why the $100 drill is worth doing on
settlements nobody has seen.
