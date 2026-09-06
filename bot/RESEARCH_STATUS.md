# RESEARCH STATUS — FROZEN

**Read this before doing anything. It takes two minutes and it will save you
from repeating fifteen slices of work.**

---

> # Paper success ≠ edge.
>
> A paper session that starts, reconciles, stays healthy and shuts down cleanly
> proves the **plumbing** works. It is not evidence of timing skill.

## TIMING_SKILL_RESEARCH: **CLOSED** on the current signals and the current data. **Seven families produced trusted numbers against pre-declared bars, seven failed; three more were never measured at all; one reached a single symbol — eleven frozen names.** The bar has never moved: M1 = M2 = 95.0

**The eleven frozen names, in code order:** `technical_analysis`, `closed_analyser`, `donchian_breakout_v1`, `btc_alt_spillover_v1`, `post_shock_fade_v1`, `sign_flip_momentum_v1` (ABSENT — percentiles exist); `range_location_fade_v1`, `open_gap_fade_v1`, `ts_momentum_v1` (INCONCLUSIVE — **no M1 and no M2 exist**); `compression_breakout_v1` (INCONCLUSIVE — **one symbol read, two not**); `funding_carry_fade_v1` (ABSENT — **all three read, conjunction failed**).
## WAITING_ON: a human-written NEW SIGNAL intake with **material difference** from all eleven frozen names, and an expected event count stated before implementation (s43's lesson: a rule firing 45/10/7 times cannot validate an instrument, let alone clear a bar; s50's: a rule firing on every bar schedules ONE trade). **Not an N-sweep. Not a parameter variant.** `NEW_SIGNAL_INTAKE.md` is marked **WAITING — EMPTY** and no agent may fill it.
## ALSO OWED (informational, gates unchanged): a pre-declared fix for the ~1.5-point residual upward bias in the DIRECTED instrument — suspects are the percentile tie convention and the `minimum_trades >= 20` replicate filter. Any future directed thesis inherits it.
## EXECUTION_STACK: **in scope** — paper readiness (s29), paper ops maturity (s30), project-mode truthfulness (s31), data intake contract (s32), acquisition FAILED CLOSED (s33), real multi-asset data registered (s34), BTC daily extended and verified (s38).
## SIGNAL 3 — `btc_alt_spillover_v1`: **FROZEN ABSENT** by human decision (s41). Measured on the full history under a valid control (s40): ETH 94.3/92.0, SOL 91.3/90.0 vs a bar of 95.0. Retuning is forbidden and the registration hook refuses a POSITIVE artefact naming it — even a perfect one. **94.3 is not a near miss.**
## SIGNAL 4 — `post_shock_fade_v1`: **FROZEN ABSENT / INCONCLUSIVE** by human decision (s44). BTC 41 trades under a VALID control read **M1 1.6 / M2 2.0** — the fade did worse than reshuffling its own trades. ETH/SOL never measured (controls INVALID at 75.4% / 99.8% incomplete; 10 and 7 setups). Family INCONCLUSIVE under the intake's multi-symbol rule. The registration hook refuses a POSITIVE naming it — even a perfect one. **Do not flip the sign because the fade lost. Do not re-search K.**
## SIGNAL 5 — `range_location_fade_v1`: **FROZEN INCONCLUSIVE** by human decision (s47). NOT an ABSENT reading — **no M1 and no M2 exist.** The design was measurable (281 / 112 / 117 entries, every symbol past the >=50 gate); the INSTRUMENT failed: control INVALID on all three symbols (z +3.632 / +3.996 / +2.975, KS p 0.0004 / 0.0003 / 0.0122, **0% incompletes** — fully exercised, not underpowered). Edge was correctly not run. The hook refuses a POSITIVE naming it. **Do not re-score under the failed control. Do not 'fix' the null knowing which way it failed.**
## SIGNAL 6 — `open_gap_fade_v1`: **FROZEN INCONCLUSIVE** by human decision (s50). NOT an ABSENT reading — **no M1 and no M2 exist.** Counts **1 / 0 / 0** against a >=50 gate: these venues never close, so `open[t]` is the print after `close[t-1]` (exactly equal on 17-51% of bars). Controls INVALID at **0 usable surrogates** because `surrogate_series` re-bases every bar's open to the prior close, **deleting** the trigger's quantity. A CONSTRUCTION MISMATCH in the null, **not** instrument bias. **Do not lower GAP_K. Do not repair the null and re-run in the same breath.**
## SIGNAL 7 — `ts_momentum_v1`: **FROZEN INCONCLUSIVE** by human decision (s51). NOT an ABSENT reading — **no M1 and no M2 exist.** Scheduled entries **1 / 1 / 3** against a >=50 gate, controls INVALID at **0 usable surrogates**. The thesis is **NOT rare** — 441 / 209 / 201 sign flips over 2934 / 1260 / 1258 flagged bars — but a STATE signal true on nearly every bar forms ONE contiguous flag run, and `simulate_schedule` takes one trade per run. A CONSTRUCTION MISMATCH in the **schedule builder**, **not** instrument bias. **Do not change LOOKBACK/SKIP. Do not alter one-trade-per-run scheduling after seeing the shortfall.**
## SIGNAL 8 — `sign_flip_momentum_v1`: **FROZEN ABSENT** by human decision (s53). Measured once per symbol in s52 under controls that **PASSED on all three** (200/200 usable, 0% incompletes): BTC **45.0/46.0** on 290 trades, ETH **81.4/83.5** on 134, SOL **22.7/25.0** on 131, vs a bar of 95.0. The best-evidenced refusal in the programme and the first since s43 to be refused by a NUMBER rather than a broken construction. **ETH's 83.5 is a FAILURE** — its M2 delta is positive with a CI excluding zero, which is not the test. **Do not retune LOOKBACK toward it. Do not rescue a single symbol.**
## SIGNAL 9 — `compression_breakout_v1`: **FROZEN INCONCLUSIVE** by human decision (s54). The MIXED case, and the ledger states it precisely. BTCUSD: **M1 69.1 / M2 73.5 on 76 trades under a VALID control** — a trusted ABSENT, 26 and 22 points short. ETHUSDT (23 entries) and SOLUSDT (35) fell below the >=50 gate and their controls were INVALID on incompletes (11.5% / 5.5% vs 5.0%), so **no M1 and no M2 exist for either**. POSITIVE needed two symbols and died at the COUNT stage. The compression filter removed 64-78% of channel breaks, so the design was materially different from the frozen Donchian half it shares. **Do not raise COMP_MAX. Do not cut BREAK_N. Do not score the alts under INVALID controls. 69.1 is not "almost 95".**
## SIGNAL 10 — `funding_carry_fade_v1`: **FROZEN ABSENT** by human decision (s56; measured s55). The FIRST family whose trigger is not a price: the entry decision is made entirely from the venue's published funding rate. The first slice in the programme where **all three symbols produced trusted numbers at once** — valid controls, 0% incompletes, every symbol past the >=50 gate (85 / 76 / 162 trades). BTCUSDT read **M1 97.0 / M2 97.5** and clears both bars. ETHUSDT read **47.7 / 48.5** and SOLUSDT read **2.3 / 1.0** — SOL's M2 delta is −0.1315 with a CI excluding zero, on the LARGEST sample of the three. §36e required **two of three** and was committed before any number existed, so the family verdict is ABSENT and `cleared_edge_signal` stays `null`. One of three symbols clearing a 95th percentile happens ~14% of the time under a global null, and these three do not agree in sign. **Do not retune FUND_ABS. Do not register a single symbol. Do not drop ETH or SOL from the universe after the fact. 97.0 on BTC alone is the exact case the multi-symbol rule was written to refuse.** The BTC POSITIVE artefact is **retained unedited** — deleting the one inconvenient file is the worse dishonesty, and it is now refused twice over, by the universe rule and by the deny-list. A forgery supplying all three symbols, which satisfies the universe rule completely, is still refused.
## THE s55 DEFECT — a prose rule is not an enforced rule: that BTCUSDT artefact is **genuine and unforged**, and with the code as it stood it was **enough**. The hook checked verdict, both bars, `m2.passed`, the attestation and the deny-list; `funding_carry_fade_v1` passed all of them and `ProjectStatus` announced a cleared edge. This is s35 (`btc_alt_spillover_v1`, ETH 97.3/98.0 with SOL failing) recurring under a new name. s37 fixed that instance with `DUAL_SYMBOL_REQUIREMENTS`; s55 fixed the CLASS: `MULTI_SYMBOL_MINIMUMS` (k-of-n, so the transcription is faithful rather than merely stricter), a structural test asserting every stage-1 record declaring a multi-symbol rule is registered in code, and `tools/registration_discipline.py` — which removes the universe rules and shows the claim registers without them, because a guard that would have refused anyway is not a guard. **A multi-symbol pre-declaration that is not transcribed into `project_status` is enforced by nothing.**
## THREE INVALID CONTROLS, THREE CAUSES: only `range_location_fade_v1` (s46) is evidence of instrument **bias** — it ran 1,000 times per symbol at 0% incomplete and came back at z = +3.6. The other two never ran. Do not cite them together.
## Model / shadow / live: **BLOCKED**.

Formal close-out: **[RESEARCH_CLOSE_STAGE1.md](RESEARCH_CLOSE_STAGE1.md)**.
Programme freeze (s45, authoritative ledger): **[docs/RESEARCH_PROGRAM_FREEZE.md](docs/RESEARCH_PROGRAM_FREEZE.md)**.
Paper operator pack: **[docs/PAPER_OPERATOR_RUNBOOK.md](docs/PAPER_OPERATOR_RUNBOOK.md)**.
Operator procedure: **[docs/PAPER_RUNBOOK.md](docs/PAPER_RUNBOOK.md)**.

```
Safety & measurement   GREEN       verified stops, single-writer, fail-closed
                                   gates, human-only kill switch, 3,112 tests
Geometry               GREEN       +0.5479R net on BTC daily, CI excludes 0
Skill instrument       VALIDATED   median 48.0 at a pre-declared n=200 (s23,
                                   reproduced exactly in s28 and s29)
Edge — closed analyser ABSENT      M1/M2 = 76.1/77.5 (1D), 73.0/74.0 (4H),
                                   72.2/76.0 (1H) -- against a bar of 95.0
Edge — donchian_v1     ABSENT      M1/M2 = 91.2/91.5 (1D) -- against 95.0
Edge — range_location  INCONCLUSIVE  no M1/M2 exists -- CONTROL INVALID on
                                   BTC/ETH/SOL (z +3.632 / +3.996 / +2.975,
                                   KS p 0.0004 / 0.0003 / 0.0122, 0%
                                   incompletes). Edge correctly NOT run. The
                                   design was measurable (281/112/117 entries);
                                   the ruler was not. Frozen s47.
Edge — post_shock_fade ABSENT      M1/M2 = 1.6/2.0 (BTC, 41 trades) -- against
                                   a bar of 95.0, under a control that PASSED
                                   (z -1.074, KS p 0.3767). The fade scored
                                   BELOW its own rotations. ETH/SOL NOT
                                   MEASURED (controls INVALID, 75.4% / 99.8%
                                   incomplete). Family INCONCLUSIVE under the
                                   intake's multi-symbol rule. Frozen s44.
Edge — btc_alt_spill   ABSENT      M1/M2 = 94.3/92.0 (ETH), 91.3/90.0 (SOL)
                                   -- against a bar of 95.0, on the FULL 1,461-
                                   date history, under a control that PASSED
                                   (ETH z +1.899 KS p 0.0712; SOL z +1.609
                                   KS p 0.2213, n=1000, seed 20250730), with
                                   control_validated=true on both artefacts.
                                   s40 repaired the directed null: S1 horizon
                                   embargo on both sides, S2 direction sequence
                                   = scored entries only, S3 shared schedule
                                   builder with a rotated phase. Slice 35's
                                   seductive ETH 97.3/98.0 was a truncated
                                   window read with a biased ruler; it does not
                                   survive either fix. DEFECT: ~1.5 points of
                                   upward bias remain and ETH's control clears
                                   by 0.061 of a z -- which makes ABSENT the
                                   STRONGER reading, not the weaker one.
                                   See EDGE.md 21d-21f.
Signal layer           CLOSED      H25_REJECT (s25); donchian ABSENT (s28)
Execution stack        PAPER-READY 84 containment + 42 session-log + 71
                                   project-mode + 68 data-contract + 41
                                   acquisition tests (s29-s34)
Project mode           TRUTHFUL    startup log, health payload and every stored
                                   session state: research CLOSED, cleared edge
                                   none, execution paper. Registration hook
                                   exists and REFUSES.
Data intake            CONTRACTED  5 eligible corpora. Synthetic ETH/SOL under
                                   data/ohlcv remain SYNTHETIC and ineligible.
Multi-asset data       PRESENT     real BINANCE SPOT ETH_USDT + SOL_USDT, 1D
                                   (1,461 bars) and 1H (35,063 bars), 2022-08 ->
                                   2026-08. Supplied by a human in s34 and
                                   verified against known market history.
                                   DATA ONLY -- eligibility is not edge.
Model promotion        BLOCKED     models/current does not exist, by design
```

**Signal 1 — `technical_analysis` (oscillator committee): CLOSED, ABSENT.**
**Signal 2 — `donchian_breakout_v1`: SCORED_ABSENT (slice 28). Parameterisation closed.**

**Headline: NOT READY for live capital. There is no measured timing edge to
deploy.**

The full reasoning for signal 1 is in **[STAGE1_VERDICT.md](STAGE1_VERDICT.md)**.
The one-line version: an entry schedule with this analyser's own trade count, run
lengths and gaps — **placed with no knowledge of the bars at all** — earns
+0.1443 R per trade on BTC daily. The analyser earns +0.2838. **22–26% of those
blind schedules beat it outright**, at every timeframe tested.

### Signal 2, in one paragraph (EDGE.md §9c)

`donchian_breakout_v1` — close above the 55-bar high, event-gated, same barrier,
same ruler — scored **M1 91.2 / M2 91.5** on BTC daily. That is **better and
still not enough**: it roughly doubles the drift-controlled contrast over blind
schedules (Δ +0.2690 vs +0.1396) and is the first change in this project that has
moved M1/M2 at all. It is also a clear FAIL — at 91.2, about one rotation
replicate in eleven beats it outright, and blind schedules that cannot see a
single price still earn +0.1585 R per trade against its +0.4274.

**91.2 is close to 95, which is precisely the condition under which bar-shopping
feels reasonable. It is not.** See Forbidden below.

---

## Forbidden

These are not style preferences. Each one is blocked by a measurement.

* **No machine learning on this entry process.** Not with more features, not
  with a different architecture, not "just to see". A policy fitted to entries
  indistinguishable from random learns to forecast drift, and it would be graded
  by the same instrument that returned ABSENT three times.
* **No live or shadow capital on geometry alone.** Half the geometry headline is
  reproducible by a schedule that cannot see prices.
* **No threshold or bar shopping on this analyser.** No retuning
  `MIN_CONFIDENCE`, `MIN_COMPONENT_AGREEMENT`, stops, size, horizon or cost
  gates to improve a skill reading — not before a run, not after seeing one.
  M1 and M2 stay at 95.0.
* **No reopening this signal without a NEW definition.** More slices on the same
  analyser are not research; the answer is measured and stable across a 43×
  range of sample sizes.
* **No N-grid search on `donchian_breakout_v1`.** N = 55 was fixed before the
  run and the run returned ABSENT. Sweeping N — or the ATR multiples, or the
  horizon — until something clears 95 is a search over dozens of correlated
  hypotheses reported as one, and on a 2,564-bar corpus it would succeed by
  construction. The near miss makes this *more* tempting, not more legitimate.
  Same for re-running it on 4H/1H: the brief permits other timeframes **only
  after a POSITIVE**, under a new pre-declared design.
* **No lowering M1/M2 to 90** to accommodate 91.2/91.5. The bars were frozen in
  EDGE.md §9b before the code was written, let alone run.
* **No multi-asset EDGE claims.** Real non-BTC data now exists (s34) and is
  eligible — but **no cross-asset measurement has been run**. Eligibility is not
  edge. A multi-asset claim requires a pre-declared Stage 1 design and M1/M2 ≥ 95,
  exactly as a single-asset one does. **The ETH and SOL files in `data/ohlcv/`
  remain synthetic** and are still ineligible; select corpora by path.
* **No running Stage 1 against a corpus the data contract marks ineligible.**
  The contract is machine-checkable for exactly this reason.
* **No interpreting `btc_alt_spillover_v1`'s ETH reading.** 97.3 / 98.0 came
  from a construction whose control did not validate, alongside a companion
  symbol at 85.1 / 81.5. Re-testing it needs a NEW pre-declared design with a
  control that actually passes — not a re-read of these artefacts.
* **No registering a cleared edge without `"control_validated": true`.** The
  number is not enough; the ruler must have been checked for the construction
  that produced it.
* **No "autonomous agent" or readiness claims.** There is nothing measured to
  deploy.

---

## To reopen ANY skill claim

The full standard is **[STAGE1_VERDICT.md](STAGE1_VERDICT.md) §6**. In short, a
candidate must have:

1. **A design committed to git before the run.** The design commit must precede
   the results commit — gate configuration, statistic, sample size, stopping
   rule.
2. **A validated control path.** Rotation + lock-up 1 + one-trade-per-run, or a
   successor passing the same control at n ≥ 200: median ≤ 50, uniformity not
   rejected, incompletes within budget.
3. **M1 and M2 at ≥ 95.0**, unless a human moves the bars **before** the run and
   records why. M2 — the contrast against shape-matched blind schedules — is
   mandatory. M1 alone cannot carry a claim: a long-only strategy on an
   appreciating asset ranks well against nulls that move entries without
   removing exposure.
4. **No threshold shopping after numbers.**
5. **No ML on a process that failed these bars.**

Anything that skips these is a backtest, not a claim.

---

## Geometry GREEN is not permission to trade

`+0.5479R` net with a confidence interval excluding zero is a real number and it
is **not** evidence of timing skill. Recorded alongside it since slice 9:

* **+1.39% over seven years against +675% buy-and-hold** — 0.2% of the move the
  strategy was long into.
* **The short-side sweep is 0 of 84 positive**, gross or net. A real
  inefficiency does not vanish when you change sign; an appreciating asset does.
* The notional cap binds before the risk cap, so realised risk per trade is
  ~30× below budget — a human's decision about risk appetite, not a tuning step.

---

## The recommended control invocation

The validated path. Note `--null rotation --lockup 1` explicitly — the CLI
default for `--lockup` is the horizon, and it was **deliberately not changed**
so that historical command blocks in `EDGE.md` still mean what they meant when
they were run (EDGE.md §7b, H2).

```bash
python3 tools/skill_test.py --data-dir data/real_1d --interval D \
        --runs 1500 --control-runs 200 --null rotation --lockup 1
```

Since slice 26 this exits **0** when the control is valid — median ≤ 50,
uniformity not rejected, incompletes within budget — and **1** when it is not,
and **2** when no null can be built. Its exit code answers *"is the instrument
valid?"*, **not** *"is there skill?"*. The edge claim belongs to
`tools/edge_measurement.py`, which still exits non-zero when M1 or M2 misses
95.0.

The contaminated path is kept as a regression and must keep failing:

```bash
python3 tools/skill_test.py --data-dir data/real_1d --interval D \
        --runs 500 --control-runs 6 --null block_resample --lockup 1
        # expect median ~82.5, exit 1. If this ever nears 50, something broke.
```

---

## Execution stack — paper-ready, and that is not an edge claim

Slice 29 proved the machine can be **operated** safely by a strategy that cannot
claim edge it does not have. 84 tests in `tests/test_paper_readiness.py` assert,
at the operational boundary: the kill switch blocks new risk and only the exact
human token clears it; live arming still needs all four conditions and degrades
to paper when any is missing; the stop-verification invariant holds; memory
cannot enlarge a size under hostile input; the model path cannot create or
reverse a trade; a ten-tick paper run submits zero orders.

Slice 29 also added **one** config key, `ENTRIES_ENABLED` (default 1), wiring the
already-existing `build_bot(attach_strategy=False)` path to the environment so an
operator can stand the machine down — loop, reconciliation, stop management and
health stay live; no new entries are proposed.

> **`ENTRIES_ENABLED` is an operator control, NOT a safety gate.** The gates are
> the kill switch, the live-arming chain and the risk gate set. It does not
> weaken them and does not replace them.

### Slice 30 — paper ops maturity

Measured, not assumed:

```
ENTRIES_ENABLED=0  ->  10 ticks, 0 decisions journalled, 0 orders submitted,
                       reconciled, healthy
ENTRIES_ENABLED=1  ->  30 decisions journalled (ALLOW + PAPER), 0 orders,
   + PAPER_TRADING     live_authorized false
```

The contrast is the evidence. With entries on, the gates and the sizer ran on
every tick and `PAPER_TRADING` short-circuited before the transport; with entries
off nothing was even considered. Zero orders means "suppressed" in one case and
"never proposed" in the other — and without both, "zero orders" would be
indistinguishable from a broken harness.

Every session now writes a structured record (`session_log.py`): 15 fields,
**no PnL**, no credentials, and a mandatory constant line —
`NO EDGE CLAIM — timing-skill research CLOSED`. `orders_submitted` is counted at
the transport (`/v5/order/create`), not inferred from the decision journal,
because the only honest answer to "did this session submit an order?" is what
crossed the wire.

Reproduce it offline in seconds, no credentials, no network:

```bash
python3 tools/paper_session_demo.py                     # stand-down
python3 tools/paper_session_demo.py --entries-enabled   # entries on, still paper
```

**None of this is evidence of edge, and no paper-run PnL is a skill claim.**

### Slice 31 — the process states its own mode

`project_status.py` exposes a frozen snapshot, wired into the startup log, the
health payload and every stored session record:

```json
{
  "timing_skill_research": "CLOSED",
  "cleared_edge_signal": null,
  "execution_mode": "paper",
  "policy_mode": "off",
  "entries_enabled": true,
  "live_authorized": false,
  "models_current_present": false,
  "no_edge_claim": "NO EDGE CLAIM — timing-skill research CLOSED"
}
```

```bash
python3 tools/print_project_status.py      # the above, exit 0
```

**`cleared_edge_signal` can only ever be set by a genuine measurement.**
`cleared_edge_signal_from_artifacts()` returns a name only when an
`edge_measurement` summary says `EDGE_EVIDENCE_POSITIVE` with M1 **and** M2 at or
above 95.0 — and never for a signal in `ABSENT_SIGNALS`. There is no setter, no
environment variable and no force argument. Every artefact in this repository
fails the check, so the answer is `null`, and that is correct rather than broken.

**This adds truthfulness, not capability.** It creates no autonomy, arms nothing
and unblocks nothing. It is the prerequisite for a future Stage-1-POSITIVE
registration path, not a substitute for M1/M2 ≥ 95.

### Slice 32 — which corpora may Stage 1 even use

```bash
python3 tools/data_intake_report.py     # exit 0
```

| path | interval | synthetic | bars | eligible |
|---|---|---|---:|---|
| `data/real_1d` | D | false | **3,135** | **yes** |
| `data/real_4h` | 4H | false | 15,379 | **yes** |
| `data/real` | 1H | false | 61,513 | **yes** |
| `data/ohlcv` | 1H | **true** | 5,000 | no — SYNTHETIC |
| `data/orderbook`, `data/quotes`, `data/anomalies` | — | true | — | no |

```
multi_asset_corpus_present        : false
non_btc_real_symbols              : []
non_btc_synthetic_symbols_present : ETH_USDT, SOL_USDT
```

**Read that last line carefully.** `data/ohlcv/` contains
`BYBIT_SPOT_ETH_USDT_1H.csv.gz` and `BYBIT_SPOT_SOL_USDT_1H.csv.gz` — 5,000 bars
each, correct headers, plausible prices, and **generated by
`tools/make_dataset.py`**. Anyone going looking for "multi-asset data" finds them
first. They are not multi-asset coverage and Stage 1 must never be run against
them. The contract exists to make that impossible to get wrong by accident.

The three eligible corpora carry **one instrument from one Bitstamp source
file**. Thresholds (`500` daily / `2000` intraday bars) were frozen in EDGE.md
§13b *before* the scanner was written.

**Eligibility is NOT edge.** All three eligible corpora produced ABSENT readings.
A corpus being eligible means a measurement may legitimately be run against it —
nothing more. Nothing in the contract can reach `cleared_edge_signal`, and a test
asserts it.

### Slice 37 — the control rule is human-adopted and PASSES. Data still missing.

**The control gate was replaced by a human pre-declaration, not by me.** Slice 36
made the case and deliberately refused to act on it; the human then declared:

```
CONTROL VALID iff  |z| < 1.96  AND  KS vs U(0,100) p >= 0.05  AND  incompletes <= 5%
```

Implemented as a pure, per-clause-tested function (24 tests). `median <= 50` is
**retired as a gate for the directed path** and kept as an informational log
line. Both directed controls now PASS at n = 1,000:

| symbol | z | KS p | incompletes | median (info) | verdict |
|---|---:|---:|---:|---:|---|
| ETHUSDT | +0.372 | 0.6921 | 0.0% | 51.47 | **VALID** |
| SOLUSDT | +0.287 | 0.6744 | 0.0% | 50.40 | **VALID** |

Both medians are **above 50** — under the retired rule both would have been
INVALID a third time, while KS p-values of 0.69 and 0.67 say the distributions
are as uniform as samples of 1,000 get. That is the clearest available proof the
old clause was rejecting a correct instrument.

**The measurement still did not happen.** `tradingbot_slice37_ready.zip` did not
extend the BTC driver — it still ends **2025-01-07** — so the sanctioned edge run
was not performed. (The zip was also a *regression*: it is this session's
slice-33 code plus data, with no signal module, no directed control and an
unfilled intake form. It was not used as a base.)

> **On the truncated 890-date window**, with the control now valid, the existing
> slice-35 artefacts become interpretable and read **ABSENT**: ETH clears both
> bars (97.3 / 98.0), SOL clears neither (85.1 / 81.5), and the pre-declared
> dual-symbol rule makes one-of-two an ABSENT. That is an observation about
> artefacts already on disk — not a measurement of the full history.

**The anti-cherry-pick rule now lives in code, not prose.** The registration hook
requires every symbol a signal's pre-declaration named to have its own qualifying
artefact. `cleared_edge_signal` is guarded three ways: the bars, the control
attestation, and the declared universe. It remains `null`.

**Required to proceed:** ship extended Bitstamp BTC_USD daily covering
2025-01-08 → 2026-08-01 (571 bars). Do not substitute Binance BTCUSDT — different
instrument, needs a new intake.

---

### Slice 36 — the ruler is fine; the GATE is a coin flip. Nothing was changed.

Slice 36 set out to remove the two blockers on slice 35's measurement. **It
removed neither, and it settled which one was real.**

**The directed construction is NOT broken.** Run at n = 1,000 surrogates it is
uniform:

```
median 50.40 | mean 49.53 | z = -0.51 | 0/1000 incomplete
buckets  10.0 / 15.1 / 24.7 / 27.8 / 13.7 / 8.7   vs   10 / 15 / 25 / 25 / 15 / 10
```

So the suspicion that the null mishandled one-trade-per-run or the next-open
fill is **refuted**, and five new tests pin the mechanics.

**What fails is the gate.** `median ≤ 50` is symmetric about 50 for a *perfect*
instrument, so `P(median > 50) ≈ 0.50` at **every** n — established analytically
in §11a, confirmed empirically here. Four directed symbol-runs have now been
gated on it; the chance of all four passing was 1 in 16.

> **The criterion was NOT replaced.** The argument for replacing it existed six
> slices before it failed. Producing it now, in the slice whose measurement it
> would unblock, is the shopping move however good the statistics are. A
> calibrated replacement (|z| < 1.96 **and** KS vs U(0,100) not rejected **and**
> incompletes ≤ 5%) is offered in EDGE.md §17e as a **proposal for a human to
> pre-declare** — not adopted.

**BTC could not be extended.** Egress is blocked (403 at the proxy, both
sources). The gap is exact: **571 daily bars, 2025-01-08 → 2026-08-01**. Nothing
was invented, forward-filled, or sourced from a synthetic corpus, and the driver
was not swapped to another venue.

`edge_verdict` stays **INCONCLUSIVE**; `cleared_edge_signal` stays `null`.

---

### Slice 35 — `btc_alt_spillover_v1`: measured, INCONCLUSIVE, no claim

The first cross-asset signal: a BTC close-to-close shock scaled by BTC's own
Wilder ATR(14) triggers a two-sided ETH/SOL trade at the alt's next open.
Information set disjoint from both closed families. Design frozen in `EDGE.md`
§16b (commit `38c73da`) before any scoring code existed.

```
                        ETHUSDT        SOLUSDT
M1                       97.3           85.1     vs 95.0
M2                       98.0           81.5     vs 95.0
directed control      median 52.2    median 50.4  -> INVALID (both > 50)
```

**`edge_verdict: INCONCLUSIVE`. Neither percentile is interpretable**, for two
independently pre-declared reasons:

1. **The directed construction's control FAILED.** The signal is two-sided and
   fills at the next open — a construction slice 23 never validated — so §16b
   committed to re-validating at n ≥ 200 first. Median came out 52.2 (ETH) and
   50.4 (SOL) against a `≤ 50` criterion. Uniformity did *not* reject
   (z = +0.86, +0.05), and §11a already showed this clause is a coin flip for a
   perfect instrument — but it was the pre-declared criterion and it failed.
   Relaxing it now would be threshold shopping.
2. **The anti-cherry-pick rule.** §16b, before any number: POSITIVE needs BOTH
   symbols over both bars. ETH cleared; SOL did not.

**ETH's 97.3 / 98.0 is the most seductive number this project has produced. It
is not a result.** Had either rule been written after seeing it, it would not
have been written.

> **Two real defects were found in this slice's own machinery.** The null's
> direction sequence was built from every *flagged* bar instead of the bars
> actually entered, so **37 of 72 ETH trades were scored in the wrong
> direction** — the first control run was measuring that corruption. And the
> slice-31 registration hook **accepted the unforged ETH POSITIVE**, making
> `ProjectStatus` announce a cleared edge. Both fixed; a summary now needs
> `"control_validated": true`, which nothing in this repository has.

---

### Slice 34 — real multi-asset data is now PRESENT and ELIGIBLE (data only)

A human supplied real **BINANCE SPOT** history, which slice 33 could not fetch:

| corpus | symbols | interval | bars each | range (UTC) |
|---|---|---|---:|---|
| `data/real_multi_1d` | ETH_USDT, SOL_USDT | D | 1,461 | 2022-08-02 → 2026-08-01 |
| `data/real_multi_1h` | ETH_USDT, SOL_USDT | 1H | 35,063 | 2022-08-01 → 2026-08-01 |

```
multi_asset_corpus_present : true
non_btc_real_symbols       : ETH_USDT, SOL_USDT
```

**The `synthetic: false` claim was verified, not trusted.** The series match
known market history at dates no generator reproduces — SOL's $12.37 low on the
FTX collapse day, its $8.00 cycle bottom on 2022-12-29, its $295.83 all-time high
on 2025-01-19, ETH's $2,111 wick in the 2024-08-05 unwind, ETH's $4,956.78 high
on 2025-08-24 — and both symbols share an identical missing hour and flat
zero-trade bar at 2023-03-24 12:00–14:00 UTC, the signature of an exchange-wide
halt. Full detail and the honest limits of that check: `EDGE.md` §15a.

> **`ETH_USDT` now names BOTH a real corpus and a synthetic one.** `data/ohlcv`
> still holds generated ETH and SOL. **Select a corpus by PATH, never by symbol
> name.** The intake report prints both lists keyed by path for this reason
> (schema `data_intake/2`).

**This did not reopen research and cleared no signal.** `cleared_edge_signal` is
still `null`, M1/M2 are still 95.0, both measured signals are still ABSENT, and
**no Stage 1 run was performed on this data**. More instruments is not more edge.

---

### Slice 33 — real multi-asset data could NOT be obtained. FAIL CLOSED.

Every documented source was tried and every one is unreachable from this
environment:

| source | direct HTTPS | WebFetch |
|---|---|---|
| Binance `/api/v3/klines` | `Tunnel connection failed: 403 Forbidden` | `ROBOTS_DISALLOWED` |
| Bybit `/v5/market/kline` | `Tunnel connection failed: 403 Forbidden` | `403 CLIENT_ERROR` |
| Bitstamp `/api/v2/ohlc/ethusd` | `Tunnel connection failed: 403 Forbidden` | `ROBOTS_DISALLOWED` |

TCP reaches all three hosts; the refusal is the egress proxy's allowlist, which
permits package registries and nothing else (`pip download` succeeds).

**`multi_asset_corpus_present` remains `false`, which is the true value.** No
synthetic substitute was written, no threshold was lowered, no bar was invented,
and the existing BTC corpora were not relabelled.

**The acquisition path was built and tested anyway.**
`tools/fetch_binance_klines.py` + 41 offline tests
(`tests/test_fetch_klines.py`) — everything but the socket, driven through an
injected pager. It refuses empty series, all-NaN series, a single NaN bar in
fifty, duplicate and non-increasing timestamps, silent truncation below the
frozen floor, and forward-fill/interpolation (structurally). Run it from a
machine with egress:

```bash
python3 tools/fetch_binance_klines.py --symbols ETHUSDT SOLUSDT \
        --intervals 1d 1h --years 4 --out data/real_multi
python3 tools/data_intake_report.py
```

One test proves the flag is not simply dead: it writes a real-shaped ETH corpus
into a temporary tree and asserts `multi_asset_corpus_present` flips
`false → true` with `non_btc_real_symbols == ["ETH_USDT"]`, while a BTC-only
corpus leaves it false. **The flag is false today because the data is absent, not
because the mechanism does not work.**

---

## A note on short control checks

`--control-runs 12` or `24` is **not** a cheap substitute for the pre-declared
`n=200`. A slice-30 run at n=24 returned median 55.0 → `CONTROL: INVALID`,
exit 1 — while its mean was exactly 50.0 and its uniformity z was +0.00.

`P(median > 50)` for a *perfect* instrument is ≈ 0.50 at n = 12, 24 **and** 200.
Sample size buys precision, not pass probability: `median ≤ 50` is a deliberately
conservative one-sided clause that can only ever refuse a good instrument, never
bless a biased one, and it is one clause of three alongside the calibrated
uniformity test. **The gate is not being changed.** Use `--control-runs 200`, and
do not read a short-run failure as a regression. Full reasoning: `EDGE.md` §11a.

---

## If you are picking this up cold

Read in this order: **RESEARCH_STATUS.md** (this file) →
**[RESEARCH_CLOSE_STAGE1.md](RESEARCH_CLOSE_STAGE1.md)** →
**[STAGE1_VERDICT.md](STAGE1_VERDICT.md)** → **[HANDOFF.md](HANDOFF.md)** →
`EDGE.md` if you need the full history. If you are going to *run* it:
**[docs/PAPER_RUNBOOK.md](docs/PAPER_RUNBOOK.md)**.

The next legitimate step is a **human decision**, not a queued task: stop, or
define a genuinely new signal and put it through the standard above. The
infrastructure to judge it fairly already exists and is validated. That is what
Stage 1 leaves behind — not an edge, but a ruler you can trust and a documented
reason not to deploy.

**Slice 27 was offered a new-signal mission with the definition block left
blank. It declined to invent one**, produced
[NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md), and stopped.

**Slice 28 filled that form in** with `donchian_breakout_v1`, committed the
design before the code (`d707262`), implemented it behind
`signals/donchian_breakout_v1.py`, and ran it once. Result: ABSENT at 91.2/91.5.
The intake process worked exactly as intended — a new thesis, pre-declared,
measured once, and reported honestly whichever way it fell.

The next legitimate step is again a **human decision**: stop, or write a new
intake form for a genuinely different thesis. Not an N sweep. The infrastructure
to judge one fairly exists, is validated, and has now correctly returned ABSENT
for two independent signals — including one that came close.

**Slice 29 closed the research formally** ([RESEARCH_CLOSE_STAGE1.md](RESEARCH_CLOSE_STAGE1.md))
and turned to the execution stack, which is the one thing that could still be
improved without a new measurement. It implemented no signal, ran no search, and
moved no bar.

The three options, stated so nobody has to infer them:

1. **Stop** capital ambitions until there is new data or a new thesis.
2. **Add real multi-year non-BTC (or otherwise material) data** under `data/`,
   then write a new Stage 1 design and pre-declare it.
3. **A new pre-declared thesis with material difference** — through
   [NEW_SIGNAL_INTAKE.md](NEW_SIGNAL_INTAKE.md). **Not a Donchian N-search.**
