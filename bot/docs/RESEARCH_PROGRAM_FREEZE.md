# RESEARCH PROGRAM — FROZEN

**Written at slice 45, before any code in that slice was changed.**

This is the authoritative ledger of what was measured, what it read, and what
is now closed. It exists so that the next person — or the next session — cannot
reopen a settled question by accident, by optimism, or by forgetting.

It softens nothing.

---

## 1. The finding

**Six hypotheses produced trusted numbers against bars pre-declared before any
of those numbers existed. All six failed. Three more were never measured at all
— one because its instrument failed its own control, one because the event it
tests does not occur in these markets, and one because a state-shaped signal
cannot be scheduled by an event-shaped builder. A tenth reached one symbol and
not the two its own rule required. An eleventh was measured cleanly on three
symbols and failed the same way, holding a real 97th-percentile reading on one
of them. Eleven names are frozen.**

`ProjectStatus.cleared_edge_signal` is `null` and has never been anything else.
No signal has ever cleared M1 or M2. The bar is 95.0 today exactly as it was
when it was written down in `EDGE.md` §5b.

## 2. The ledger

| # | family | reading | bar | evidence | closed |
|---|---|---|---:|---|---|
| 1 | `technical_analysis` / `closed_analyser` | M1/M2 **76.1 / 77.5** (1D), 73.0 / 74.0 (4H), 72.2 / 76.0 (1H) | 95.0 | `STAGE1_VERDICT.md`, EDGE.md §5 | slices 24–25 |
| 2 | `donchian_breakout_v1` | M1/M2 **91.2 / 91.5** (1D) | 95.0 | `artifacts/slice29_edge_donchian_regression_summary.json` | slice 28 |
| 3 | `btc_alt_spillover_v1` | M1/M2 **94.3 / 92.0** (ETHUSDT), **91.3 / 90.0** (SOLUSDT) | 95.0 | `artifacts/slice40_edge_btc_alt_spillover_*_summary.json`, control `artifacts/slice40_control_directed_*_n1000.log` | slice 40, frozen 41 |
| 4 | `post_shock_fade_v1` | M1/M2 **1.6 / 2.0** (BTCUSD, 41 trades); ETHUSDT and SOLUSDT **not measurable** | 95.0 | `artifacts/slice43_edge_post_shock_fade_BTCUSD_summary.json`, control `artifacts/slice43_control_post_shock_fade_BTCUSD_n1000.log` | slice 43, frozen 44 |
| 5 | `range_location_fade_v1` | **no M1/M2 exists** — control INVALID, z **+3.632 / +3.996 / +2.975**, KS p 0.0004 / 0.0003 / 0.0122, 0% incompletes | n/a | `artifacts/slice46_control_range_location_fade_{BTCUSD,ETHUSDT,SOLUSDT}_n1000.log` | slice 46, frozen 47 |
| 6 | `open_gap_fade_v1` | **no M1/M2 exists** — counts **1 / 0 / 0** vs a 50-trade target; controls INVALID at **0 usable surrogates of 200**, 100% incomplete | n/a | `artifacts/slice49_count_finding_open_gap_fade.log`, `artifacts/slice49_control_open_gap_fade_*_n1000.log` | slice 49, frozen 50 |
| 7 | `ts_momentum_v1` | **no M1/M2 exists** — scheduled entries **1 / 1 / 3** vs a 50-trade gate; controls INVALID at **0 usable surrogates of 200**, 100% incomplete | n/a | `artifacts/slice50_count_finding_ts_momentum.log`, `artifacts/slice50_control_ts_momentum_*_n1000.log`, `artifacts/slice50_no_edge_run_attestation.log` | slice 50, frozen 51 |
| 8 | `sign_flip_momentum_v1` | M1/M2 **45.0 / 46.0** (BTC, 290 trades), **81.4 / 83.5** (ETH, 134), **22.7 / 25.0** (SOL, 131); **all three controls VALID**, 200/200 usable, 0% incomplete | 95.0 | `artifacts/slice52_edge_sign_flip_momentum_*_summary.json`, `artifacts/slice52_control_sign_flip_momentum_*_n1000.log` | slice 52, frozen 53 |
| 9 | `compression_breakout_v1` | BTC **69.1 / 73.5** on 76 trades, control VALID — a trusted ABSENT. ETH (23 entries) and SOL (35) below the >= 50 gate, controls INVALID on incompletes: **no M1/M2 exists for either** | 95.0 (BTC only) | `artifacts/slice53_edge_compression_breakout_BTCUSD_summary.json`, `artifacts/slice53_count_finding_compression_breakout.log`, `artifacts/slice53_control_compression_breakout_*_n1000.log` | slice 53, frozen 54 |

Ten names, six measurements: `technical_analysis` and `closed_analyser` are
the same reading under two names, and both are denied.

### ABSENT and INCONCLUSIVE are not the same closure

Rows 1-4 are **ABSENT**: a trusted instrument measured them and they did not
clear the bar. There are percentiles, and they are in the table.

Rows 5, 6 and 7 are **INCONCLUSIVE**: **no reading was ever taken**. There is
**no M1 and no M2** for `range_location_fade_v1`, `open_gap_fade_v1` or
`ts_momentum_v1`, and any percentile quoted for any of them would be fabricated
rather than approximate. `project_status.FROZEN_STATUS` holds the distinction in
code so it cannot decay into prose.

The three are INCONCLUSIVE for different reasons and must not be merged:

* **Row 5 was measurable and mismeasured.** The instrument ran 1,000 times per
  symbol at 0% incomplete and came back biased at z = +3.6. This is the
  programme's only instrument-bias evidence.
* **Row 6 was never measurable.** 1 / 0 / 0 entries because a 24/7 venue has no
  session boundary, and 0 usable surrogates because `surrogate_series` sets
  every bar's open to the prior close, **deleting** the quantity the trigger
  reads.
* **Row 7 was measurable in principle and unschedulable in practice.** The null
  **preserved** its trigger — surrogates found their own setups, unlike row 6's
  — but a state true on nearly every bar forms one contiguous flag run and
  `simulate_schedule` takes one trade per run. 1 / 1 / 3 entries from a thesis
  with 441 / 209 / 201 sign flips.

**Only row 5's INVALID is evidence about the instrument's bias.** Rows 6 and 7
are construction mismatches, in two different components, and citing them
alongside row 5 would overstate what this programme knows.

The design was not the problem: 281 / 112 / 117 entries after one-trade-per-run,
every symbol clearing the intake's >= 50 gate and its >= 80 expectation. **The
ruler was the problem** — 0% incompletes means the instrument was fully
exercised a thousand times per symbol and came back biased at z = +3 to +4, the
largest instrument bias this project has recorded.

### What the shape of the failures says

```
76.1  →  91.2  →  94.3  →  1.6      (measured)
                            ?       (range_location_fade_v1 — never measured)
```

The third came closest and was still refused — and it was read on an instrument
later shown to be biased **upward** by ~1.5 percentile points, so its 94.3 was
flattered rather than unlucky. The fourth tested the **opposite** directional
hypothesis and was refused harder than anything before it: at M1 1.6 the fade
performed worse than reshuffling its own trades.

Nothing here is trending toward a discovery. Four independent information sets —
an oscillator committee, a same-asset channel breakout, a cross-asset
continuation, a same-asset reversion — were asked the same question on the same
bars and none of them could time an entry better than its own dates.

### Two of the four could not be fully measured, and that is recorded too

* `post_shock_fade_v1` on **ETHUSDT and SOLUSDT**: K = 2.0 fired 10 and 7
  times. Their controls were INVALID at 75.4% and 99.8% incomplete, and both
  fell below the 30-entry floor. **No percentile exists for either symbol**, and
  none was computed. The family verdict is INCONCLUSIVE under the intake's own
  multi-symbol rule, which required two symbols at ≥ 50 scored trades.
* That is a fact about the pre-declared design, not a licence to change it.

## 3. What is frozen, and how

`project_status.FROZEN_ABSENT` maps each closed name to the reading that closed
it and the artefact a reader can check. `ABSENT_SIGNALS` is derived from its
keys, so the deny-list and its evidence cannot drift apart.

The registration hook refuses any artefact naming a frozen family **even when
that artefact is otherwise perfect**: correct verdict, both bars cleared,
`m2.passed`, a genuine control attestation, and every declared symbol present.
The refusal outranks the multi-symbol rule and everything else.

Reopening a name means deleting its entry — a visible, reviewable act with a
human attached, not a side effect of a file appearing in `artifacts/`.

## 4. Forbidden on this program, permanently

Without a **new** human-filled intake, written before any run it enables:

* no retune of any frozen family's constants — thresholds, lookbacks, stops,
  take-profits, horizons, costs;
* no grid or sweep over any of them;
* no **sign flip** on `post_shock_fade_v1` because the fade lost. A direction
  chosen because the opposite one underperformed is a parameter fitted to an
  observed result, and it would arrive dressed as an insight;
* no lowering of K to raise a fire rate;
* no single-symbol rescue of a family that failed its own conjunction;
* no `_v2` presented as the same signal. A different constant is a **new
  family**: new intake, new material-difference argument, its own control;
* no **re-scoring under a failed control**. A percentile from an instrument that
  failed its own check is not a weak number — it is not a number;
* no **"fixing" the null using knowledge of a z-failure**. Slice 46 recorded a
  hypothesis about why `range_location_fade_v1`'s control failed and did not act
  on it. Acting on it after seeing which way the failure went would be a
  construction chosen to fit an observed result. A null change is a human
  pre-declaration, in writing, before the run it enables;
* no lowering of M1, M2 or any of the three control clauses;
* no reading of "almost 95" as anything but a failure.

## 5. The gate for any future Stage 1

A measurement slice is **not legitimate** unless a human-filled
`NEW_SIGNAL_INTAKE.md` exists first and clears all of:

1. **A different information set** from all eleven frozen names. Not different
   weights, thresholds, lookbacks or signs on an existing one.
2. **An expected minimum of 50 scored trades per symbol, stated up front.**
   This is slice 43's lesson paid forward: a rule that fires 45 / 10 / 7 times
   cannot validate an instrument, let alone clear a bar, and discovering that
   after implementation wastes a slice.
3. **Every constant frozen before any run**, in writing, in git.
4. **A control for the construction it uses**, validated before any real-series
   percentile is read. A control is a statement about a construction on a
   series, not a certificate that travels — slices 35–40 are the whole argument.
5. **M1 = M2 = 95.0**, unchanged, on the pre-declared conjunction, from **one**
   run.
6. **The symbol universe and the conjunction stated explicitly**, and expect
   them to be transcribed into `project_status.DUAL_SYMBOL_REQUIREMENTS` or
   `MULTI_SYMBOL_MINIMUMS`. Slice 55 is the argument: a rule that lived only in
   prose let a genuine single-symbol POSITIVE register a cleared edge.

Until such an intake exists, there is no legitimate measurement work to do, and
a slice that measures anyway is producing numbers no one pre-committed to.

## 5b. `funding_carry_fade_v1` — measured slice 55, **FROZEN ABSENT slice 56**

The eleventh closed line, and the only one on this ledger closed while a
genuine `EDGE_EVIDENCE_POSITIVE` artefact for it sits in `artifacts/`. It has a
section rather than a table row because that fact needs a paragraph.

It is the first family in this programme whose trigger is not a price. The entry
decision is made entirely from the venue's published funding rate — a cash flow
between position holders — joined to a linear daily decision clock, with the
funding actually paid over each hold charged as a cost on both the observed
schedule and every replicate.

| symbol | trades | control | M1 | M2 | reading |
|---|---:|---|---:|---:|---|
| BTCUSDT | 85 | VALID (z +0.632) | 97.0 | 97.5 | clears both bars |
| ETHUSDT | 76 | VALID (z +0.168) | 47.7 | 48.5 | ABSENT |
| SOLUSDT | 162 | VALID (z +1.933) | 2.3 | 1.0 | ABSENT, strongly |

**Two things happened here and they should not be run together.**

**One: the instrument worked, on all three symbols, first time.** Valid controls,
0% incompletes, every symbol past the 50-trade gate. That had never happened
before in this programme — slices 43, 46, 49, 50 and 53 all lost at least one
symbol to a broken or underpowered control. Whatever else slice 55 is, it is the
first clean three-symbol reading the instrument has produced.

**Two: it did not clear, and one symbol looked as though it had.** The design
note required **two of three** and was in git before any number existed. One
reached it. Under a global null, one of three independent symbols clearing a 95th
percentile happens about 14% of the time; and the three do not agree in sign —
SOLUSDT's M2 delta is −0.1315 with a confidence interval excluding zero, on the
largest sample of the three. So the family reads ABSENT and
`cleared_edge_signal` stays `null`.

**And the reason this section is in the ledger rather than only in EDGE.md:** the
BTCUSDT artefact is genuine, unforged and attested, and with the code as it stood
it was **enough to register a cleared edge**. The multi-symbol rule existed only
in prose. That is slice 35 recurring — `btc_alt_spillover_v1`, ETH at 94.3 / 92.0
on the honest re-measurement but 97.3 / 98.0 on the truncated one that first got
through — and slice 37's fix covered that signal's universe rather than the class
of failure. Slice 55 added `project_status.MULTI_SYMBOL_MINIMUMS`, a structural
test that every stage-1 record declaring a multi-symbol rule is registered in
code, and `tools/registration_discipline.py`, which removes the universe rules
and demonstrates the claim registers without them.

**The rule, for whoever writes the next intake: a multi-symbol pre-declaration
that is not transcribed into `project_status` is enforced by nothing.**

**Frozen by human decision in slice 56.** `project_status.FROZEN_ABSENT` gains
an eleventh entry with all three readings, and the BTCUSDT artefact stays on
disk **unedited**, still saying `EDGE_EVIDENCE_POSITIVE`. It is refused twice
over now — by the universe rule and by the freeze — and a forged artefact
supplying all three symbols, which satisfies the universe rule completely, is
still refused. Permanently forbidden without a NEW human intake written before
any re-use of these numbers: registering BTC-only, editing that artefact,
lowering `FUND_ABS` or changing the stop, take-profit, horizon or cost model,
and narrowing the universe to the symbol that passed. See EDGE.md §38.

Evidence: `artifacts/slice55_stage1_funding_carry_fade_v1.json`,
`artifacts/slice55_edge_funding_carry_fade_*_summary.json`,
`artifacts/slice55_control_funding_carry_fade_*_n1000.log`,
`artifacts/slice55_count_finding_funding_carry_fade.log`,
`artifacts/slice55_data_eligibility.json`, EDGE.md §36 and §37.

## 6. Two standing obstacles

Stated so the next intake is written with open eyes rather than discovering
them halfway:

* **No eligible corpus contains an order book.** `data/real*` are OHLCV only;
  the one corpus with a book is synthetic and therefore never Stage-1 evidence.
  Every order-flow, queue-position or microstructure thesis is blocked on data,
  not on ideas.
* **The directed instrument is biased, and slice 46 widened the picture rather
  than narrowing it.** The cross-asset path carries ~1.5 points of unexplained
  upward bias (EDGE.md §22d). The same-asset path read a clean 49.05 on a
  return-based trigger (slice 43) and **+3.63 on a high/low-based one**
  (slice 46). The bias appears to track how directly a trigger reads the same
  bar geometry the barrier prices its stops from — three data points, a
  hypothesis, not a finding. Two suspects remain unpaid: the percentile tie
  convention and the `minimum_trades >= 20` replicate filter.
  **Practical consequence for the next intake: a thesis whose trigger reads
  highs and lows directly inherits this problem, in the direction that
  flatters.**

## 7. What this freeze is not

It is not a claim that no edge exists in these markets. It is a claim about
**these measured hypotheses, on this data, under bars fixed in advance** — and
that claim is that they did not clear. For the unmeasured ones it is a narrower
claim still: they could not be read at all.

Nor is it a claim that a percentile above 95 can never appear. One did, in slice
55, on one symbol of three (§5b). What the freeze asserts is that a reading is
only a claim when it clears the conjunction its own pre-declaration named — and
that the difference between those two things is now enforced in code rather than
trusted to a reader.

It is also not a pause on the execution stack. The paper shell is certified for
continuous unattended operation under an explicit NO EDGE CLAIM
(`docs/PAPER_AGENT_CERTIFICATION.md`, `docs/PAPER_OPERATOR_RUNBOOK.md`). That
half is finished. The missing half is evidence.

**Closer to an autonomous profit agent: NO.** Nothing in this freeze moves that
answer, and nothing will until a future human thesis clears M1 ≥ 95 and M2 ≥ 95
under a validated instrument with enough trades to mean something.
