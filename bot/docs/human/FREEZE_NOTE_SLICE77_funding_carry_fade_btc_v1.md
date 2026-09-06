# HUMAN FREEZE NOTE — SLICE 77

**Family:** `funding_carry_fade_btc_v1`
**Decision:** FREEZE ABSENT
**Written:** 2026-09-06
**Written by:** operator, on evidence produced in the slice-77 audit
**Status of this file:** UNSIGNED. Nothing in software changes until the
signature line at the bottom is filled in by a human.

---

## What I am freezing and why

I am freezing the only product this programme has ever cleared. I am not doing
it because a number came back ugly. `-0.8358` on n=2 is not a result and I will
not pretend it is one. I am doing it because the evidence chain that cleared
this family has a defect that no amount of further waiting can repair.

### The defect

Slice 55 measured `funding_carry_fade_v1` across three symbols on the full
1,461-bar BTC linear daily corpus:

| Symbol | M1/M2 | n | mean net R |
|---|---|---|---|
| BTCUSDT | **97.0 / 97.5** | 85 | **+0.0964** |
| ETHUSDT | 47.7 / 48.5 | 76 | −0.0358 |
| SOLUSDT | 2.3 / 1.0 | 162 | −0.1658 |

The pre-declared 2-of-3 conjunction failed. The family was frozen ABSENT. That
refusal is the best-evidenced decision in this programme and I stand by it.

Slice 57 then declared a NEW product, BTC only, over the late half of the same
series: `t0 2022-08-10 → t_mid 2024-08-09 → t1 2026-08-09`, `n_bars_total 1461`.
It cleared at M1/M2 = 95.13 / 96.0 on n=41.

**That late half is a subset of the 1,461 bars that produced the 97.0 / 97.5
reading.** The window was out-of-sample with respect to *fitting*. It was not
out-of-sample with respect to *selection* — and selection is what happened.
BTCUSDT became the universe because BTCUSDT was the symbol that won.

### What was done correctly, and why it was not enough

I want this recorded properly, because the procedure here was better than most
of what I have seen anywhere:

- `NEW_SIGNAL_INTAKE.md` was written and it names the frozen predecessor
  explicitly, states the material difference, and says in plain words *"not a
  claim that the multi-coin rule worked."*
- the fold file uses `index_midpoint_50pct`, derived from timestamps and index
  position only — not from returns, not from funding.
- the fold file carries a `forbidden` clause barring anyone from moving `t_mid`
  after `n_OOS`, M1, M2 or the mean net R exists, and that clause was honoured.
- no grid was run, no constant moved, no symbol was dropped after the fact.

Every procedural step was followed. The step that does not exist in this
programme is the one that would have caught it: **nothing asked whether those
bytes had already been read.** The invariants check that a cut was declared
before scoring, that folds are unmodified, that constants did not move. All were
satisfied. None of them can see contamination that arrives through *selection*
rather than through *fitting*.

That gap is now closed by `tools/reserved_holdout.py`, added in this slice. Run
against the slice-57 window it returns `ALREADY_READ` and names the three prior
reads that overlap it.

### The search width

The registry counts 12 trials at family level. The artefacts show more:

```
17  distinct family x symbol edge measurements in artifacts/
38  control runs
```

`tools/deflated_sharpe.py` on those widths:

```
N = 11   P(>=1 clears a 95th-percentile bar under a global null) = 43.12%
N = 17                                                           = 58.19%
N = 38                                                           = 85.76%
```

One clear at a width of 17 is not evidence. The registry's own note already says
the count is *"a floor, not a census"*, and I am recording that the true width is
wider still because abandoned variants leave no artefact.

### What the forward data does and does not add

Two closed trades, `-1.0632` and `-0.6084`, mean `-0.8358`. Monitors read
`INSUFFICIENT_DATA` and they are right to. **I am not freezing on n=2.** I am
noting only that the forward sign agrees with what was already visible inside
Stage-1: `M4_halves` read WARN with an earlier half of `+0.4669` on 17 and a
recent half of `-0.0798` on 18. The decay was in the data before forward
observation began.

### What I am NOT claiming

- I am not claiming the code is wrong. It is not. 5,039 tests pass.
- I am not claiming the signal is definitely noise. I am claiming the evidence
  cannot distinguish it from noise, which is a different and weaker statement,
  and it is the one the arithmetic supports.
- I am not retuning anything. `FUND_ABS` stays `0.0001`. The stop stays 1.5 ATR.
  The horizon stays 5. The caps stay `$100`. `M1`/`M2` do not move.
- I am not re-scoring Stage-1 OOS. Those artefacts stay unedited, including the
  flattering ones, exactly as slice 55's did.
- I am not touching `GATE_PATH` or `live_authorized`.

### The one thing that would reopen this

A clean holdout: BTC linear daily bars **after 2026-08-24**, which nothing in
this programme has read at the time of writing, certified by
`tools/reserved_holdout.py` before any scoring, with the trial width declared in
advance and the deflated bar applied. That is a genuine test. It is also slow —
roughly 20 closed trades at the observed rate is on the order of a year — and
slow is the correct speed. Nothing about wanting it faster makes it faster.

---

## What changes in software when this is signed

1. `project_status.FROZEN_ABSENT` gains `funding_carry_fade_btc_v1` with this
   note as its reason.
2. `ProjectStatus.cleared_edge_signal` becomes `None`.
3. `artifacts/hypothesis_registry.json` records the 17 family x symbol trials.
4. `artifacts/data_read_ledger.json` is seeded with the historical reads.

Items 3 and 4 are evidence and are written by this slice regardless. **Items 1
and 2 are a human decision and are NOT applied by any tool.** The tree's own
rule is that promotion is manual and demotion is automatic; a demotion on
*statistical* grounds is a judgement, not a monitor trip, so it takes a
signature.

---

## Signature

```
I have read the above and I accept the freeze.

  name:
  date:
  git commit at time of signing:
```

Until this block is filled in, `funding_carry_fade_btc_v1` remains the cleared
signal in `project_status`, the shell still executes no edge, and
`promotion_gate_allows_live()` remains `False` for the six human items it has
always been false for.
