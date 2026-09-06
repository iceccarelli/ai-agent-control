# KILL-SWITCH DRILL — SCRIPT (TEMPLATE — NOT YET PERFORMED)

> **Blank. No drill has been performed. This document's existence completes no
> checklist item.**

The point of a drill is not to prove the switch exists — a test already does
that. It is to prove that **a human can stop this process under time pressure,
and that stopping it actually stops entries**, on the day it matters.

---

## Before you start

```
Operator            : ______________________
Date / time (UTC)   : ______________________
Tree / commit       : ______________________
Log path for this drill: ____________________
```

## Step 1 — establish the baseline

```bash
python3 tools/print_project_status.py
```

Record:

```
timing_skill_research  : ______
cleared_edge_signal    : ______
execution_mode         : ______
live_authorized        : ______   (must be false)
models_current_present : ______   (must be false)
```

## Step 2 — trip the switch

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.trip_kill_switch('drill: <operator> <date>'); s.close()"
```

## Step 3 — verify it actually blocks

Confirm **all three**, and write down what you saw, not what you expected:

```
[ ] startup refuses                        observed: ______________________
[ ] the gate chain blocks with
    KILL_SWITCH_ENGAGED                    observed: ______________________
[ ] health reports unhealthy               observed: ______________________
```

Then verify the property that matters most:

```
[ ] the shadow path proposes NO entry while the switch is engaged
    observed: ______________________
```

## Step 4 — verify it survives a restart

```
[ ] restart the process; the switch is still engaged
    observed: ______________________
```

> **If it cannot be read, it is treated as engaged.** An unanswerable question is
> a "no". Confirm that behaviour too if you can induce it safely.

## Step 5 — clear it, with the human token

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.clear_kill_switch_by_human('HUMAN_CLEARED_KILL_SWITCH'); s.close()"
```

```
[ ] a wrong token does NOT clear it        observed: ______________________
[ ] the exact token clears it              observed: ______________________
```

> **Before you clear it in anger rather than in a drill, know why it tripped.** A
> switch cleared without a diagnosis is a switch that will trip again with a
> position on.

## Step 6 — sign off

```
Drill performed by  : ______________________  date: __________
Witnessed by        : ______________________  date: __________
Anything that did not behave as scripted:
____________________________________________________________
```

> A drill in which nothing surprised you is worth recording. A drill you did not
> run is worth nothing, and marking this item complete without running one is a
> forgery.
