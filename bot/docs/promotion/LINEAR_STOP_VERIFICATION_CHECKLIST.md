# LINEAR PROTECTIVE-STOP VERIFICATION (TEMPLATE — NOT YET VERIFIED)

> **Blank. Nothing here has been verified. This document's existence completes
> no checklist item.**

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

**It changes nothing about items 1–4 below.** Those are statements about a
VENUE, not about a simulator, and no simulator can make them true. They remain
unverified.

Every standing safety claim in this repository — *"a confirmed position always
has a verified protective stop"* — is asserted and tested on the spot path. This
checklist exists because that claim has not been demonstrated on linear, and
"probably fine" is not a verification.

## What "verified" has to mean before micro-live

Not "the code exists". Not "a unit test passes". All four, demonstrated and
recorded:

```
[ ] 1. A protective stop is PLACED on the venue for a linear position, and its
       existence is confirmed by reading it back from the exchange — not from
       local state.
       observed: ______________________________________________

[ ] 2. The stop SURVIVES a process restart. Kill the process with a position
       open; confirm on restart that the stop is still on the venue and that
       reconciliation sees it.
       observed: ______________________________________________

[ ] 3. A position that somehow ends up NAKED is detected within one cycle and
       either re-protected or flattened. Induce this deliberately.
       observed: ______________________________________________

[ ] 4. Margin and liquidation behaviour on linear is understood and documented
       for the proposed notional, including what happens at the cap.
       observed: ______________________________________________
```

## The simulator gap — say which of the two was done

```
[x] the linear simulator was extended to model margin/funding/liquidation,
    and the extension is tested
        path to the work: backtest.LinearSimulatedExchange
                          bot/tests/test_linear_simulator.py (25 tests)
    NOTE: this box is an ENGINEERING fact and it does not sign anything off.
    The four venue items above are untouched by it and the signature block
    below is still blank.

[ ] OR micro-live proceeds on TESTNET only, with the simulator gap accepted
    in writing, and the memo says so explicitly
        memo section: ______________________
```

> Choosing neither is not an option. An unverified stop path on the instrument
> the signal trades is the failure mode that turns a 100 USD pilot into an
> unbounded one.

## Sign-off

```
Verified by         : ______________________  date: __________
Reviewed by         : ______________________  date: __________
Evidence log path   : ______________________
```
