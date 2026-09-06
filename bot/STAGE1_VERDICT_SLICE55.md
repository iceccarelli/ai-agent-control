# SLICE 55 — VERDICT

**Signal: `funding_carry_fade_v1`. The first family in this programme whose
trigger is not a price.**

---

## The mandated fields

| field | value |
|---|---|
| `data_eligible` | **true** — `data/real_funding` and `data/real_linear_1d`, both `synthetic: false`, both pinned by sha256 computed by this slice |
| `count_gate_met` | **true on all three symbols** — 85 / 76 / 162 scored trades against a bar of 50 |
| `control_validated` | **true on all three symbols** — z +0.632 / +0.168 / +1.933, KS p 0.5386 / 0.9242 / 0.1561, **0% incompletes everywhere** |
| `m1` | BTCUSDT **97.0** · ETHUSDT 47.7 · SOLUSDT 2.3 |
| `m2` | BTCUSDT **97.5** · ETHUSDT 48.5 · SOLUSDT 1.0 |
| `positive_rule_met` | **false** — one symbol of the two its own pre-declaration required |
| `edge_verdict` | **ABSENT** |
| `cleared_edge_signal` | **`null`** |
| `frozen_absent_count` | **10** — unchanged; this family is not frozen |
| `models_current_present` | false |
| `live_authorized` | false |
| `execution_mode` | paper |
| `closer_to_autonomous_profit_agent` | **NO** |

## The evidence table

| symbol | bars | funding prints | joined | setups | runs | trades | control | mean net R | M1 | M2 | M2 delta (95% CI) | folds + |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|---|
| BTCUSDT | 1,461 | 4,383 | 1,461 | 417 | 85 | **85** | VALID | +0.0964 | **97.0** | **97.5** | +0.1603 [+0.1487, +0.1719] | 3/4 |
| ETHUSDT | 1,461 | 4,383 | 1,461 | 415 | 76 | **76** | VALID | −0.0358 | 47.7 | 48.5 | −0.0030 [−0.0157, +0.0097] | 1/4 |
| SOLUSDT | 1,461 | 4,458 | 1,461 | 534 | 162 | **162** | VALID | −0.1658 | 2.3 | 1.0 | −0.1315 [−0.1402, −0.1229] | 0/4 |

Bars M1 = M2 = **95.0**, unchanged since `EDGE.md` §5b. Constants exactly as
frozen in `EDGE.md` §36b: `FUND_ABS` 0.0001, stop 1.5 ATR, TP 1.0 R, horizon 5,
next open, one trade per run, 25 bps **plus the funding paid over each hold**.
One run per symbol. No grid, no re-seed, no second parameterisation.

## What happened, in three sentences

A brand-new information set — a cash flow, not a price path — was measured
cleanly on three symbols at the first attempt, which had never happened before
in this programme. It did not clear the conjunction it declared in advance: one
symbol reached both bars, the rule needed two, and the other two symbols read
47.7 / 48.5 and 2.3 / 1.0 — the largest sample of the three pointing the
opposite way with a confidence interval that excludes zero. And the repository
was one untranscribed prose rule away from announcing that it *had* cleared.

## Why one symbol at 97.0 is not an edge

* **One of three is not rare.** Under a global null, at least one of three
  independent symbols clearing a 95th percentile happens about **14%** of the
  time.
* **The three disagree in sign.** SOLUSDT is as far below its bar as BTCUSDT is
  above it, on 162 trades against BTCUSDT's 85.
* **The carry offset explains neither.** The control measured what the
  instrument reads when nothing can be timed: 51.27 on BTCUSDT, 53.97 on
  SOLUSDT. Both real readings are genuine departures from that — in opposite
  directions.
* **The rule was written first.** `EDGE.md` §36e was committed at `d18a3b8`,
  before the loader had ever produced a setup, and it says in terms:
  *single-symbol claims are refused*.

## The defect this slice found

The BTCUSDT artefact is **genuine, unforged and control-attested**, and with the
code as it stood it was **enough**: `cleared_edge_signal_from_artifacts` returned
`funding_carry_fade_v1` and `ProjectStatus` announced a cleared edge. The
multi-symbol rule existed only in prose.

This is slice 35 recurring under a new name. Slice 37 fixed that *signal's*
universe with `DUAL_SYMBOL_REQUIREMENTS`; it did not fix the class of failure:

> **A multi-symbol pre-declaration that is not transcribed into
> `project_status` is enforced by nothing.**

Closed by `MULTI_SYMBOL_MINIMUMS` (k-of-n, so the transcription is faithful
rather than merely stricter), a structural test that every stage-1 record
declaring a multi-symbol rule is registered in code, and
`tools/registration_discipline.py`, whose third battery removes the universe
rules and demonstrates the claim registers without them.

## The prediction that was scored

`tools/control_funding_carry_fade.py` was committed **unexecuted** at `d8311aa`
with a prediction in its docstring: charging the observed schedule the carry it
collects, while replicates are handed directions unrelated to the funding at the
bars they land on, would push control clause (a) positive and might break it.

**Right in sign, wrong in size.** All three z came back positive — a coin flip
three times on a correct instrument — but far smaller than the estimate. SOLUSDT
reached **+1.933 against a bar of 1.96**, which is a **PASS**: the bar was fixed
in slice 37, before this data existed, and was not moved in either direction now
that a number is near it. No fourth clause was added. Whether a rotation null is
the right null for a carry-shaped thesis is an open construction question for a
human pre-declaration, recorded rather than settled here.

## What was refused

* `FUND_ABS`, the stop, the take profit, the horizon and the cost model:
  **unchanged**, before and after every number;
* the funding cost was **not** dropped to rescue the control — refused in
  advance, in writing, before the control ran;
* no second run, no re-seed, no grid, on any symbol;
* the ten frozen OHLC families: **not reopened**;
* `models/current`: **absent**. `POLICY_MODE` off. Live **blocked**;
* a single-symbol registration: **refused by code**, not by discipline.

## Closer to a bank-grade autonomous profit agent?

**NO.** `EDGE.md` §36g fixed that answer before the run: NO unless this slice
produced a genuine **multi-symbol** POSITIVE under a validated control at the
frozen constants. It produced a genuine **single**-symbol positive, which is the
exact case the pre-declaration was written to refuse.

The measurement was a success. The hypothesis was not.

---

**Artefacts**

```
artifacts/slice55_data_eligibility.json
artifacts/slice55_count_finding_funding_carry_fade.log
artifacts/slice55_control_funding_carry_fade_{BTCUSDT,ETHUSDT,SOLUSDT}_n1000.log
artifacts/slice55_edge_funding_carry_fade_{BTCUSDT,ETHUSDT,SOLUSDT}.log
artifacts/slice55_edge_funding_carry_fade_{BTCUSDT,ETHUSDT,SOLUSDT}_summary.json
artifacts/slice55_stage1_funding_carry_fade_v1.json
artifacts/slice55_registration_discipline.log
artifacts/slice55_paper_cert.log
artifacts/slice55_pytest.log
```

Design: `EDGE.md` §36 (before any code). Findings: `EDGE.md` §37.
