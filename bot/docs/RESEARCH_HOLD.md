# RESEARCH HOLD — ACTIVE

**Written at slice 48, before any code in that slice was changed.**

Timing-skill research on this programme is on **HOLD**. Ten names are frozen,
nothing has cleared, and no measurement slice is legitimate until a human files
a new intake that clears the gate in §4.

**Slice 54 update.** `compression_breakout_v1` was measured in slice 53 — one
symbol measurable, two not — and is now frozen INCONCLUSIVE as the tenth name.
`NEW_SIGNAL_INTAKE.md` is back to **WAITING — EMPTY**, and no measurement slice
is legitimate until a human fills it.

This document is the standing statement of that position.
`docs/RESEARCH_PROGRAM_FREEZE.md` remains the detailed ledger; this one is what
an operator or a returning session should read first.

---

## 1. The ledger — eleven frozen names, two kinds of closure

| # | family | status | what exists |
|---|---|---|---|
| 1 | `technical_analysis` | **ABSENT** | M1/M2 **76.1 / 77.5** (1D), 73.0 / 74.0 (4H), 72.2 / 76.0 (1H) vs 95.0 |
| 2 | `closed_analyser` | **ABSENT** | the same 76.1 / 77.5 reading under its other name |
| 3 | `donchian_breakout_v1` | **ABSENT** | M1/M2 **91.2 / 91.5** (1D) vs 95.0 |
| 4 | `btc_alt_spillover_v1` | **ABSENT** | M1/M2 **94.3 / 92.0** (ETH), **91.3 / 90.0** (SOL) vs 95.0, control VALID |
| 5 | `post_shock_fade_v1` | **ABSENT** | M1/M2 **1.6 / 2.0** (BTC, 41 trades) vs 95.0, control VALID; ETH/SOL not measurable |
| 6 | `range_location_fade_v1` | **INCONCLUSIVE** | **no M1 and no M2 exist.** Control INVALID on all three symbols — z **+3.632 / +3.996 / +2.975**, KS p 0.0004 / 0.0003 / 0.0122, **0% incompletes** |
| 7 | `open_gap_fade_v1` | **INCONCLUSIVE** | **no M1 and no M2 exist.** Counts **1 / 0 / 0** against a 50-trade target, and controls INVALID with **0 usable surrogates of 200** — the event does not occur on a 24/7 venue, and the surrogate deletes the trigger's quantity |
| 8 | `ts_momentum_v1` | **INCONCLUSIVE** | **no M1 and no M2 exist.** Scheduled entries **1 / 1 / 3** against a 50-trade gate, controls INVALID with **0 usable surrogates of 200** — a STATE signal against an EVENT-shaped scheduler, **not** a rare thesis (441 / 209 / 201 sign flips) |
| 9 | `sign_flip_momentum_v1` | **ABSENT** | M1/M2 **45.0 / 46.0** (BTC, 290 trades), **81.4 / 83.5** (ETH, 134), **22.7 / 25.0** (SOL, 131) vs 95.0, **all three controls VALID** at 200/200 usable and 0% incompletes. The best-evidenced refusal in the programme |
| 10 | `compression_breakout_v1` | **INCONCLUSIVE** | one symbol measurable, two not. BTC **69.1 / 73.5** on 76 trades under a VALID control — a trusted ABSENT — while ETH (23 entries) and SOL (35) fell below the >= 50 gate with INVALID controls, so **no M1 and no M2 exist for either**. POSITIVE needed two symbols and was unreachable at the count stage |

| 11 | `funding_carry_fade_v1` | **ABSENT** | the multi-symbol conjunction failed on three symbols that were ALL measured. BTCUSDT **97.0 / 97.5** on 85 trades, control VALID, artefact `EDGE_EVIDENCE_POSITIVE` and **retained unedited**; ETHUSDT **47.7 / 48.5** on 76, control VALID; SOLUSDT **2.3 / 1.0** on 162, control VALID, M2 delta −0.1315 with a CI excluding zero. Needed two of three; one reached it |

`FROZEN_ABSENT` count is **11**. `M1 = M2 = 95.0`, unchanged since they were
pre-declared in `EDGE.md` §5b before any of these numbers existed.

### The two kinds are not interchangeable

**ABSENT** means a trusted instrument measured the hypothesis and it did not
clear the bar. Percentiles exist and are quoted above.

`sign_flip_momentum_v1` is the cleanest example the programme has: three
symbols, three validated controls at 0% incompletes, 290 / 134 / 131 trades,
and three different answers — 45 / 81 / 23. **ETH's 81.4 / 83.5 is a failure**,
notwithstanding a positive M2 delta whose CI excludes zero. Beating a
shape-matched schedule on average is not the test; the percentile against 95.0
is.

**INCONCLUSIVE** means no reading was ever taken. For `range_location_fade_v1`
and `open_gap_fade_v1` there is **no M1 and no M2** — writing one would be
fabrication, not approximation. `project_status.FROZEN_STATUS` holds this
distinction in code so it cannot decay into prose.

The two INCONCLUSIVE names failed for different reasons, and the difference
matters:

* `range_location_fade_v1` was **measurable and mismeasured**. 281 / 112 / 117
  entries, every symbol past its ≥ 50 gate, the instrument exercised 1,000
  times per symbol at 0% incomplete — and it came back biased at z = +3 to +4.
  That is evidence about the instrument.
* `open_gap_fade_v1` was **never measurable at all**. The event does not occur
  on a 24/7 venue (1 / 0 / 0 entries), and the surrogate construction sets every
  bar's open equal to the prior close, deleting the trigger's quantity rather
  than destroying its predictive value. Its INVALID is a construction mismatch
  and is **not** evidence about instrument bias.
* `compression_breakout_v1` is the **mixed** case, and the ledger states it
  precisely rather than rounding it to either neighbour. BTCUSD produced a
  trustworthy ABSENT (69.1 / 73.5, 76 trades, VALID control); ETHUSDT and
  SOLUSDT produced nothing, failing the count gate at 23 and 35 and their
  controls on incompletes. It is not a dual-symbol ABSENT — that refusal was
  never performed — and it is not an empty INCONCLUSIVE either, because one
  good number exists and is worth keeping. Its alts' INVALID is a
  **sparse-sample** failure, not a construction mismatch.
* `ts_momentum_v1` was **measurable in principle and unschedulable in
  practice**. The null preserved its trigger — surrogates found their own
  setups, unlike slice 49's — but a state that is true on nearly every bar
  forms one contiguous flag run, and the schedule builder takes one trade per
  run. 1 / 1 / 3 entries from a thesis with 441 / 209 / 201 sign flips. Its
  INVALID is a construction mismatch in the **schedule builder**, and is also
  **not** evidence about instrument bias.

Three INVALID controls now exist for three different reasons, and only the
`range_location_fade_v1` one — an instrument that ran 1,000 times per symbol at
0% incomplete and came back at z = +3.6 — says anything about bias.

## 2. The hold

**No measurement slice is legitimate right now.** Not a re-run, not a "quick
check", not a variant. Specifically forbidden without a new human intake:

* retuning any frozen family's constants, or gridding over them;
* flipping any direction because the original lost;
* re-scoring edge under a control that failed — a percentile from an instrument
  that failed its own check is not a weak number, it is not a number;
* **patching the null using knowledge of a z-failure.** Slice 46 recorded a
  hypothesis about why `range_location_fade_v1`'s control failed and
  deliberately did not act on it. Acting after seeing which way a failure went
  is a construction chosen to fit a result;
* lowering M1, M2, or any of |z| < 1.96 / KS p ≥ 0.05 / incompletes ≤ 5%;
* reading "almost 95" as anything but a failure.

## 3. What slice 48 is, and is not

**Scope:** a continuous paper-operations pack. Documentation and binding tests
for running the certified paper shell unattended, under an explicit
NO EDGE CLAIM.

**Non-goals, every one of them explicit:**

* **no edge** — no measurement of any kind is run in this slice;
* **no live** — `live_authorized` stays false, no arming path is opened;
* **no model** — nothing trained, promoted or loaded; `models/current` stays
  absent;
* **no Signal 7** — no new signal file, and `NEW_SIGNAL_INTAKE.md` is not
  filled. A hypothesis proposed by the thing that measures it is not an
  independent hypothesis;
* **no weakening** of the slice-42 certification or the slice-45 operator
  runbook. This slice may extend them and must not gut them.

**Paper runtime is not evidence.** A shell that runs for a year is a shell that
ran for a year. Fills are simulated; a paper session that looks profitable is
showing drift and the simulator. That is why no session record carries a PnL,
equity, win-rate, Sharpe or drawdown field, and why that refusal is enforced by
tests rather than by convention.

## 3b. `funding_carry_fade_v1` — measured slice 55, **FROZEN ABSENT slice 56**

Row 11 above, since slice 56. This section is kept in full because the row is
one line and this family is the only one whose closure needs a paragraph: it is
the sole entry on the deny-list closed while a genuine `EDGE_EVIDENCE_POSITIVE`
artefact for it sits in `artifacts/`.

The first family whose trigger is not a price — the entry decision comes
entirely from the venue's published funding rate, with the funding actually paid
over each hold charged as a cost on the observed schedule and on every replicate
alike.

| symbol | trades | control | reading |
|---|---:|---|---|
| BTCUSDT | 85 | VALID, z +0.632 | M1 97.0, M2 97.5 — clears both bars |
| ETHUSDT | 76 | VALID, z +0.168 | M1 47.7, M2 48.5 |
| SOLUSDT | 162 | VALID, z +1.933 | M1 2.3, M2 1.0 — M2 delta −0.1315, CI excludes zero |

Three valid controls, 0% incompletes, every symbol past the 50-trade gate. **The
first time in this programme that all three symbols produced trusted numbers at
once.** The pre-declaration required **two of three** and was in git before any
of those numbers existed. One reached it, so the family reads **ABSENT** and
`cleared_edge_signal` stays `null`.

One of three independent symbols clearing a 95th percentile happens about 14% of
the time under a global null, and these three do not agree in sign — the largest
sample of the three points the opposite way with a confidence interval that
excludes zero.

**The defect it exposed, which outlasts the family.** That BTCUSDT artefact is
genuine, unforged and control-attested, and with the code as it stood it was
enough: the registration hook returned the signal's name and `ProjectStatus`
announced a cleared edge. The multi-symbol rule lived only in prose. Slice 37 had
already fixed one instance of exactly this; slice 55 fixed the class, with
`project_status.MULTI_SYMBOL_MINIMUMS`, a structural test that every stage-1
record declaring a multi-symbol rule is registered in code, and
`tools/registration_discipline.py`, whose third battery removes the universe
rules and shows the claim registers without them.

**A multi-symbol pre-declaration that is not transcribed into `project_status`
is enforced by nothing.** See EDGE.md §37.

**Frozen by human decision in slice 56.** The BTCUSDT artefact stays on disk,
byte-for-byte, still reading `EDGE_EVIDENCE_POSITIVE` — deleting the one
inconvenient file is the worse of the two dishonesties available, and a
repository that removes it is one whose remaining artefacts mean less. It is now
refused twice over, by the universe rule and by the deny-list. Forbidden without
a NEW human intake written first: registering BTC-only, editing that artefact,
lowering `FUND_ABS` or changing the stop, take-profit, horizon or costs, and
narrowing the universe to the symbols that passed. See EDGE.md §38.

## 4. The gate for lifting the hold

A measurement slice becomes legitimate only when a human-filled
`NEW_SIGNAL_INTAKE.md` exists **first** and clears all five:

1. **A different information set** from all eleven frozen names — not different
   weights, thresholds, lookbacks or signs on an existing one.
2. **An expected minimum of 50 scored trades per symbol, stated before any
   code.** Slice 43 discovered after implementing that its rule fired 45 / 10 /
   7 times, which left two symbols unable to validate a control at all.
3. **Every constant frozen in git before any run.**
4. **A control for the construction it uses**, validated before any real-series
   percentile is read. A control is a statement about a construction on a
   series, not a certificate that travels.
5. **M1 = M2 = 95.0** on the pre-declared conjunction, from **one** run.

### Two warnings for whoever writes that intake

* **No eligible corpus contains an order book.** Every order-flow,
  queue-position or microstructure thesis is blocked on data, not on ideas.
* **These venues never close.** There is no session boundary anywhere in the
  eligible corpora: `open[t]` is the print after `close[t-1]`, exactly equal on
  17–51% of bars. Every open-gap, overnight-inventory or auction-discontinuity
  thesis is blocked on data. Slice 49 paid a full slice to learn this; check it
  in one line of arithmetic before implementing.
* **The instrument can only score EVENT-shaped signals.** A thesis expressed as
  a persistent **state** — a regime, a trend sign, a filter on for months —
  collapses to one trade per contiguous episode under `simulate_schedule`,
  however often the state changes. Slice 50 paid a full slice for this one.
  Check `len(runs)` against `flags.sum()` before implementing, not after.
* **The instrument is now demonstrated to work** on close-only, event-shaped
  triggers at 130–290 trades per symbol (slice 52: three VALID controls, three
  clean readings), and on a trigger drawn from a **separate series entirely**
  at 76–162 trades (slice 55: funding, three VALID controls, 0% incompletes).
  That is a narrowing, not an invitation: it says where a thesis can be
  *measured*, not where one is likely to be *found*.
* **State the symbol conjunction in the intake, and expect it to be enforced in
  code.** Slice 55's rule was "at least two of three", it was in git first, and
  one symbol cleared. Had that rule not been transcribed into
  `project_status.MULTI_SYMBOL_MINIMUMS`, a single genuine artefact would have
  registered a cleared edge. Write the universe and the minimum explicitly, and
  expect a test to check that the code knows about it.
* **A cost that the strategy systematically collects will bias a rotation null,
  and the control is where that shows up.** `funding_carry_fade_v1` collects
  carry on every trade by construction while its replicates do not, which was
  predicted before the run and came back **right in sign, wrong in size** — all
  three z positive, one of them at +1.933 against a bar of 1.96. Whether a
  rotation null is the right null for a carry-shaped thesis is an open
  construction question and belongs in a human pre-declaration, not in the
  slice whose numbers depend on the answer.
* **The directed instrument is biased, and the bias tracks the trigger.** The
  same-asset control read a clean 49.05 on a return-based trigger (slice 43),
  +1.90 on the cross-asset return trigger, and **+3.63 on a high/low-based
  one** (slice 46). Three data points, a hypothesis, not a finding — but a
  thesis whose trigger reads highs and lows directly inherits the problem, in
  the direction that flatters. Choose the state variable with that in mind, or
  pre-declare a null fix first.

## 5. Status

```
timing_skill_research              CLOSED
cleared_edge_signal                null
execution_mode                     paper
live_authorized                    false
models_current_present             false
FROZEN_ABSENT count                11
M1 / M2                            95.0 / 95.0
```

**Closer to an autonomous profit agent: NO.**

Eleven hypotheses have been closed and none produced evidence of timing skill.
The
execution shell is certified and documented; the evidence half is empty. A
locked lab with good documentation is an accurate record of having no edge, not
progress toward one. That answer changes when a human thesis clears M1 ≥ 95 and
M2 ≥ 95 under a validated instrument with enough trades to mean something, and
not before.
