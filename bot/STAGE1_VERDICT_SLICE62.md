# SLICE 62 — VERDICT

```
SLICE62_VERDICT:                    PASS
extension_present:                  YES   (1 closed linear bar, 2026-08-10)
after_t1_linear:                    1
after_t1_funding:                   5
forward_observations_to_date:       0
forward_n_trades:                   0
is_forward_observation:             false
history_rewritten:                  NO    (prefix hashes == the slice-57 pins)
bars_fabricated:                    0
oos_re_scored:                      NO
constants_fingerprint:              662de0115880871352d5d623b1020eaa  (unchanged)
caps:                               1 position / 1 entry per day / 100.00 USD  (unchanged)
monitor_thresholds_moved:           NO
monitor_status (forward segment):   INSUFFICIENT_DATA — 0 closed forward trades
monitor_status (historical):        M-4 still WARN, not recomputed, not erased
frozen_families:                    11  (unchanged)
promotion_gate_allows_live:         NO    (2 of 8, unchanged)
closer_to_autonomous_profit_agent:  NO
```

## What happened

For the first time in six slices, **the data was actually there.**

The human supplied a full 502-member project tree with a real extension on
disk. Every substantive claim in `HUMAN_DATA_NOTE_SLICE62.md` turned out to be
independently true — established by hashing the files, not by reading the note.
Had the note been false, the same arithmetic would have said so, exactly as it
did in slice 61 when the same two promises resolved in opposite directions.

And the forward shadow still produced **zero observations**, which is the
correct answer and was declared as the ceiling before the run.

## The extension is an append, and that is provable

An extension makes "history untouched" harder to verify, not easier: once a file
grows, its whole-file digest necessarily differs from the pin, and a digest that
differs says only *"different"*. It cannot tell an append from a rewrite.

Hash the **prefix** at the pinned row count instead:

```
LINEAR    pinned 1461 rows -> disk 1462   (+1)
          sha256(prefix)   3c2e8601114c10135efc8f0e6e58e097de11beeb5e084d6e5827cb259babed55
          slice-57 pin     3c2e8601114c10135efc8f0e6e58e097de11beeb5e084d6e5827cb259babed55   MATCH

FUNDING   pinned 4383 rows -> disk 4388   (+5)
          sha256(prefix)   9f5ceefce6adcc2c396f7b15c83ae64125439c41773fbc48cf4dad1f2f266145
          slice-57 pin     9f5ceefce6adcc2c396f7b15c83ae64125439c41773fbc48cf4dad1f2f266145   MATCH

all six corpus files append-only                        TRUE
every appended row strictly after t1                    TRUE
ETHUSDT and SOLUSDT untouched                           TRUE
```

**Not one byte of measured history has moved.** The 41 OOS trades, M1 95.13,
M2 96.0 and mean net R +0.1736 all still describe exactly the bytes they
described.

## A bar arriving is not an observation

```
extension_present                                       TRUE
closed forward bars                                     1     (2026-08-10)
closed forward bars needed for one closed trade         6     (HORIZON + 1)
max possible forward closed trades today                0     declared in EDGE.md §45e
observed forward closed trades                          0
is_forward_observation                                  FALSE
```

The two lines are reported separately and the separation is the entire point.
Slice 59's failure mode was history relabelled as forward experience; the one
available to this slice was **a bar relabelled as experience** — announcing an
observation because data finally arrived. A trade is an observation only when it
**closes**, and none has.

There is a second, independent reason the count is zero, computed rather than
asserted: **the rule stood aside.** The forward bar carried no funding setup —
the appended prints run 7.5e-05, 7.9e-05 and 5.1e-05, all inside
`FUND_ABS = 1e-4` — and it is the last bar of the corpus, which
`directed_signal_bars` excludes on its own because there is no next bar to fill
on.

## Eight red tests that had found nothing

The baseline suite was **8 failed / 4,036 passed / 2 skipped**, and every
failure was a test reporting that a file had grown.

The tempting repair was to move the numbers — 1461 → 1462, 4383 → 4388,
`(8.0, 8.0)` → `(8.0, 16.0)`. That is threshold-moving, and a test edited to
agree with whatever is on disk asserts nothing. The repair taken instead is the
three-clause prefix invariant (`tools/corpus_prefix.py`, EDGE.md §45c), which is
**equal** on the invariant that matters, **strictly stronger** in one place, and
weaker in exactly one place that was never an invariant:

| | old whole-file form | new prefix form |
|---|---|---|
| a measured byte is edited | caught | caught |
| the corpus shrinks | caught | caught, and named |
| an append vs a rewrite | **cannot tell** | distinguished |
| a back-fill stamped inside the measured window | **never looked** | caught, and named |
| proves no extension exists | yes | **no — and that was never an invariant** |

Six control tests build corrupted corpora in a temporary tree and require the
check to go red — rewritten history row, truncation, back-fill, a row stamped
exactly at `t1` — with an unmutated control that must pass and a legitimate
second extension that must also pass. **A guard never observed to fail is not
known to be a guard.**

## The funding hole, named rather than absorbed

The extension began at the next **day** boundary rather than the next **print**,
leaving exactly one gap:

```
2026-08-09T08:00Z   6.667e-05   <- last row of the measured prefix
------------------------------- 16 HOURS: 2026-08-09T16:00Z is MISSING
2026-08-10T00:00Z   7.497e-05   <- first appended row
```

Widening the cadence bound to `(8.0, 16.0)` would have made the suite green and
made the hole invisible. Instead the cadence is asserted flat over the measured
prefix and the hole is enumerated by name, with ETHUSDT — untouched, flat 8
hours, zero holes — as a control proving the enumerator finds nothing where
there is nothing. The hole is after `t1`, changes no number in this slice, and
is carried forward for the human to fill.

## Left deliberately unedited

* **the MANIFESTs** — still declaring 1461 and 4383 rows, now wrong about the
  files beside them. Rewriting them would make the delivery look internally
  consistent when it is not;
* **`artifacts/slice61_data_freshness.json`** — it says the corpus ends at `t1`
  and always will, because that was true when it was written. A new test asserts
  the **disk has since moved past it**, so editing the artefact to keep up would
  now fail.

## Evidence table

| claim | observed | file |
|---|---|---|
| pack is a real tree | 502 members, 13,270,308 bytes, `c4dbc96d…` | `tradingbot_slice62_dataready.zip` |
| history untouched | both prefix digests match the slice-57 pins | `artifacts/slice62_data_freshness.json` |
| append-only | all 6 corpus files | `artifacts/slice62_data_freshness.json` |
| closed bars after t1 | 1 (`2026-08-10`, closed `2026-08-11T00:00Z`) | `artifacts/slice62_data_freshness.json` |
| open bar absent | `2026-08-11` correctly not present | `artifacts/slice62_data_freshness.json` |
| appended bar plausible | volume 19th pctile, trades 25th pctile | `artifacts/slice62_data_freshness.json` |
| missing funding print | exactly one, `2026-08-09T16:00Z` | `artifacts/slice62_data_freshness.json` |
| forward trades | 0, equal to the pre-declared ceiling | `artifacts/slice62_forward_shadow.json` |
| rule stood aside | no funding setup; last bar of corpus | `artifacts/slice62_forward_shadow.json` |
| bar array not sliced | flags zeroed, not sliced | `artifacts/slice62_forward_shadow.json` |
| no exit clamping | a trade out of data has not closed | `artifacts/slice62_forward_shadow.json` |
| gate refuses | 2 / 8, `allows_live` false | `artifacts/slice62_promotion_gate.json` |
| registration discipline | HELD, three batteries | `artifacts/slice62_registration_discipline.log` |
| suite | 4,104 passed / 0 failed / 2 skipped | `artifacts/slice62_pytest.log` |

## Forbidden list — every item, answered

| forbidden | this slice |
|---|---|
| synthetic / invented / forward-filled bars labelled real | 0 bars fabricated; no corpus written by any tool |
| relabelling bars ≤ `t1` as `is_forward_observation` | `in_forward_window` is strict at `t1`; two tests pin it |
| trusting `HUMAN_DATA_NOTE_*.md` without verifying | every claim re-derived from file hashes and dates |
| re-scoring OOS / Stage-1 | `cleared_edge_re_scored_this_slice: false`; artefact byte-identical |
| changing constants, schedule, caps, thresholds | fingerprint `662de011…`; caps 1/1/100.00; thresholds unmoved |
| raising `max_notional_usd` above 100 | 100.00, asserted |
| arming live, writing `models/current`, training | live dark; `models/current` absent; no model |
| reopening any of the 11 frozen families | 11, unchanged |
| `closer_to_autonomous_profit_agent = true` | false |
| shadow mean R as Stage-1 / registration evidence | `forward_mean_net_r` is null; both flags false |
| completing human checklist items in code | `human_items_completed_in_code: 0`; no signature |

## What would move this forward

Five more closed daily bars. At six, the first forward trade can close — if the
rule signals at all, which on the one day available it did not. Twenty closed
forward trades and 180 days are what the gate asks for, and neither number moved
this slice.

**One post-t1 day is pilot progress, not autonomy.**
