# LINEAR PROTECTIVE-STOP VERIFICATION (TEMPLATE — NOT YET PERFORMED)

> **Blank. No venue drill has been run against this checklist. This
> document's existence completes no checklist item.** An earlier version of
> this file recorded all four venue items `[x]`, a filled `observed:` line
> per item, and a human Sign-off — but the eight `bot/artifacts/linear_*.json`
> transcripts it cited were never committed to this repository and have no
> git history at all. The claim was typed, not backed by a file anyone can
> read. That version was a forgery of exactly the kind this checklist exists
> to prevent, and it has been reverted to blank here. Nothing in this file
> flips `allows_live`, and nothing in this file may be marked `[x]` again
> until a real transcript exists on disk for it.
---
## Why this item exists, and why it is the one most likely to be waved through
`docs/PAPER_OPERATOR_RUNBOOK.md` §H records a limitation that matters more here
than anywhere else in the system:
> **Perp (`linear`) simulation raises `NotImplementedError`.** The linear *live*
> path is implemented and unit-tested; the *simulator* models spot cash
> accounting only, so a linear backtest would omit funding, margin and
> liquidation and report a flattering number.
The consequence for this pilot is specific and uncomfortable:
**`funding_carry_fade_btc_v1` trades a USDT-margined linear perpetual. The
protective-stop behaviour that every other part of this system takes for granted
had therefore never been exercised end to end in simulation on the instrument
this signal actually trades.**
**That half is now done, and it is the SIMULATION half only.** ROADMAP Stage A
item 1 added `LinearSimulatedExchange`, in which the stop is written by
`/v5/position/trading-stop`, read back off `/v5/position/list` through the real
`BybitClient.verify_stop`, and triggered against the bar — including the case
where liquidation is nearer than the stop and fills first. See
`bot/tests/test_linear_simulator.py::TestTheProtectiveStopEndToEnd`.
**Venue Items 1–4 below are NOT recorded.** They remain unverified: no venue
drill via `bot/tools/linear_stop_venue_drill.py` (not the Phase D carry drill
`bot/tools/drill.py` / `artifacts/linear_stop_drill.json`, which never calls
`trading-stop` / `verify_stop`) has produced a transcript that is actually on
disk in this repository.
Every standing safety claim in this repository — *"a confirmed position always
has a verified protective stop"* — is asserted and tested on the spot path and
on the linear simulator. This checklist exists because the claim must also be
demonstrated on the **live venue** for linear, and the SIMULATION half being
done does not make the VENUE half true.
## What "verified" has to mean before micro-live
Not "the code exists". Not "a unit test passes". Not "an `observed:` line was
typed". All four, demonstrated and recorded, with the transcript actually
committed at the path named:
[ ] 1. A protective stop is PLACED on the venue for a linear position, and its
existence is confirmed by reading it back from the exchange — not from
local state.
observed: ______________________
[ ] 2. The stop SURVIVES a process restart. Kill the process with a position
open; confirm on restart that the stop is still on the venue and that
reconciliation sees it.
observed: ______________________
[ ] 3. A position that somehow ends up NAKED is detected within one cycle and
either re-protected or flattened. Induce this deliberately.
observed: ______________________
[ ] 4. Margin and liquidation behaviour on linear is understood and documented
for the proposed notional, including what happens at the cap.
observed: ______________________


## The simulator gap — say which of the two was done


[x] the linear simulator was extended to model margin/funding/liquidation,
and the extension is tested
path to the work: backtest.LinearSimulatedExchange
bot/tests/test_linear_simulator.py (25 tests)
NOTE: engineering fact only. Venue Items 1–4 are separately recorded above,
and are UNCHECKED until real venue evidence exists.


[ ] OR micro-live proceeds on TESTNET only, with the simulator gap accepted
in writing, and the memo says so explicitly
memo section: docs/promotion/LINEAR_STOP_MARGIN_MEMO.md §5
+ this checklist Sign-off


> Choosing neither is not an option. An unverified stop path on the instrument
> the signal trades is the failure mode that turns a 100 USD pilot into an
> unbounded one. Items 1–4 must close the venue stop questions on testnet
> before either box above may be ticked.

## Sign-off


Verified by         : ______________________  date: __________
Reviewed by         : ______________________  date: __________
Evidence log path   : ______________________
