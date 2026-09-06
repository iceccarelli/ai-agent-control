# PROMOTION GATE — MICRO-LIVE

**Status: REFUSE. 2 of 8 items complete. `promotion_gate_allows_live()` returns
`False` and nothing in this repository can make it return `True` today.**

---

## What this document is

The checklist a human must complete before micro-live trading of
`funding_carry_fade_btc_v1` could even be *proposed*. It is not a plan to go
live. It is the list of things that would have to be true first, written down
before any of them are, so that "we are ready" becomes a claim someone can check
rather than a feeling someone has.

Declared in `EDGE.md` §42e, **before any new shadow output was read**. That
ordering is the only thing that makes the minimum observation counts a gate
rather than a description of whatever happened to occur.

## What is being gated, and how thin it is

| | |
|---|---|
| signal | `funding_carry_fade_btc_v1`, BTCUSDT only |
| cleared by | slice 57, pre-declared out-of-sample gate |
| evidence | **41 trades**, M1 **95.13** against a bar of 95.0, M2 96.0, mean net R +0.1736 |
| M1 margin | **three rotation replicates.** 73 of 1,500 beat the observed mean; 76 would have failed it |
| decay | uncapped OOS Q1 +0.3164 → Q2 +0.0236; capped pilot earlier half +0.4669 → recent half **−0.0798** |
| concentration | three of 41 trades carry 41% of the net R |
| evidence type | a time split of data that had already been looked at — **not held-out data** |

**This is the thinnest possible thing that can honestly be called a clear.** The
gate is shaped by that: it is not designed to open soon.

## The checklist

Machine state: `artifacts/slice59_promotion_gate.json`.
Evaluator: `promotion_gate.evaluate_promotion_gate()`.

| # | item | owner | state |
|---|---|---|---|
| 1 | Human risk memo signed, path recorded | human | **incomplete** |
| 2 | Forward shadow, no ALERT, ≥ 20 closed trades **and** ≥ 180 days | observation | **incomplete — 0 / 20, 0 / 180** |
| 3 | M-4 recent-half WARN explicitly accepted in the memo, or recovered under the unchanged threshold | human or observation | **incomplete** |
| 4 | Kill-switch drill performed and recorded | human | **incomplete** |
| 5 | Linear protective-stop verification documented | human | **incomplete** |
| 6 | `max_notional_usd` ≤ 100.00 unless a human memo raises it | machine | **complete** (100.00) |
| 7 | Exact `LIVE_TRADING_ACK` token present | human | **incomplete** (absent — correct) |
| 8 | `models/current` absent, or contained to `win_probability` only | machine | **complete** (absent) |

### Why 20 trades and 180 days

**20** is the count at which the *last* monitor arms. M-4 needs 20 closed trades;
below that the gate would be waving through a period in which one of its four
instruments was blind.

**180 days** is two non-overlapping M-2 windows, so the 90-day rolling mean has
had two independent looks rather than one.

At the pilot's observed rate — 35 capped trades over 731 days — 20 forward trades
is roughly **fourteen months**. That is not an accident of the arithmetic; it is
the intended cost of arming something this thin.

### Why item 2 reads 0 / 20 today

There is **no genuinely forward data**. The linear corpus ends 2026-08-09, which
is the same data the cleared measurement was taken over. Zero bars have arrived
since.

`artifacts/slice59_forward_shadow.json` is a *forward-compatible* record — it
uses the schema, caps, schedule and thresholds a real forward run will use, so
the record's shape is fixed before real observations land in it — and it declares
`is_forward_observation: false`, `forward_observations_to_date: 0`.

**A replay over the cleared window does not count and never will.** Relabelling
history as experience is precisely the substitution this item exists to prevent.

### Why item 5 is not a formality

`docs/PAPER_OPERATOR_RUNBOOK.md` §H records that the **linear simulator raises
`NotImplementedError`**: the live linear path is implemented and unit-tested, but
the *simulator* models spot cash accounting only. So the protective-stop
behaviour that every other part of this system takes for granted has not been
exercised end to end on the instrument this signal trades. A human must document
and demonstrate that path before any micro-live. This is the item most likely to
be waved through and the one least safe to wave through.

## How the gate fails closed

* **a missing gate file refuses.** A gate that cannot be read is a gate that says
  no;
* **an unreadable gate file refuses**;
* **an item the file does not mention refuses**;
* **flipping every bool in the file to `true` still refuses.** The gate requires
  the checklist **and** `config.is_live_authorized` independently. Editing the
  JSON changes some inputs to a conjunction, not the answer;
* **the two machine items are re-derived from the live tree**, not read from the
  file, so a hand-edited JSON cannot assert the notional cap or the absence of
  `models/current` falsely;
* **the gate cannot arm anything.** It is a predicate. It does not write
  `live_authorized`, does not touch config, does not clear the kill switch and
  does not place orders. An AST test asserts that no module assigns
  `live_authorized`, this one included.

## What happens if the pilot decays

The monitors raise status. An ALERT blocks new shadow entries. **Nothing
auto-revokes the research clear and nothing auto-arms anything.** Withdrawing the
clear is a human act requiring the literal `HUMAN_REVOKED_CLEARED_EDGE`
(`shadow.revoke_cleared_edge`), and an AST test asserts no trading or model
module can call it.

If a monitor fires, the honest responses are: accept it in the memo, wait for
more observation, or revoke. **Moving the threshold is not on that list.** A
threshold adjusted because it fired is a description of the past.

## What completing this checklist would and would not authorise

It would authorise a human to **propose** micro-live at the pilot's frozen
notional, under every existing gate, with the kill switch armed and a documented
stop-verification path.

It would **not** authorise: raising the notional, adding a symbol, loading a
model, running unattended without the monitors, or describing the result as an
autonomous profit agent. Those are separate decisions with separate evidence
requirements that do not exist yet.

---

**This gate operates the brakes on a thin clear. It is not a path to live; it is
the thing standing in the way of one.**
