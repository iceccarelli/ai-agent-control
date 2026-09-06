# PAPER OPERATOR RUNBOOK

**The one-page contract for running this process unattended, continuously.**

For the detailed procedures — exact environment blocks, the health payload
field by field, the session-log schema, the command for every operational claim
— see **[PAPER_RUNBOOK.md](PAPER_RUNBOOK.md)**. This document does not repeat
them: duplicated procedure drifts, and a drifted runbook is worse than no
runbook. What is here is the part an operator must hold in their head.

Sections **A–F** are the whole contract. If you read nothing else, read A, then
B, then C.

---

## A. What you are running

A **certified paper execution shell** under an explicit **NO EDGE CLAIM**.

| | |
|---|---|
| Certified for | continuous unattended operation in paper mode |
| Standard | [PAPER_AGENT_CERTIFICATION.md](PAPER_AGENT_CERTIFICATION.md) |
| Research state | **HOLD** — [RESEARCH_HOLD.md](RESEARCH_HOLD.md) |
| Research ledger | [RESEARCH_PROGRAM_FREEZE.md](RESEARCH_PROGRAM_FREEZE.md) |
| Frozen families | **11**, none cleared |
| Cleared edge | **none.** `cleared_edge_signal` is `null` |
| Live | **blocked** |
| Model | **blocked**; `models/current` does not exist |

**It is not a profit agent and running it longer will not make it one.** Nine
hypotheses have been closed: six produced trusted numbers against bars fixed in
advance and all six failed; three were never measured at all — one because its
instrument failed its own pre-declared control, one because the event it tests
does not occur in a market that never closes, and one because a state-shaped
signal cannot be scheduled by an event-shaped builder. Ten names are frozen
because two of them are the same reading under two names.

A tenth hypothesis, `funding_carry_fade_v1`, was measured cleanly on three
symbols in slice 55 and **also did not clear**: one symbol cleared both bars,
its own pre-declaration required two, and the other two read 47.7 / 48.5 and
2.3 / 1.0. A human froze it ABSENT in slice 56, which is why the count above is
eleven. It changes nothing else on this page. `cleared_edge_signal` is still
`null`, and it is null for a reason the code enforces rather than a reason a
reader has to trust.

Paper fills are simulated, and a paper session that looks profitable is showing
you drift and the simulator, not skill. That is why no session record carries a
PnL field — see D.

**Runtime is not evidence.** Uptime, cycle count, number of simulated fills and
a clean health history are facts about the shell. None of them is a fact about
whether an entry beats its own date. Nothing you can observe from operating this
process can promote it out of NO EDGE CLAIM; only a human-filed thesis clearing
M1 ≥ 95 and M2 ≥ 95 under a validated instrument can, and the gate for that is
in [RESEARCH_HOLD.md](RESEARCH_HOLD.md) §4.

## B. Startup checklist

```bash
python3 -m pytest tests/ -q          # must be green before you start anything
python3 tools/print_project_status.py
python3 main.py
```

`print_project_status.py` must show, before you start the bot:

```
timing_skill_research  = CLOSED
cleared_edge_signal    = funding_carry_fade_btc_v1
execution_mode         = paper
live_authorized        = false
models_current_present = false
```

**`cleared_edge_signal` stopped being `null` in slice 57**, and an operator
should understand exactly what that does and does not mean before starting
anything.

It means one product — `funding_carry_fade_btc_v1`, BTCUSDT only — passed a
Stage-1 gate that was written down before it was measured: 41 trades on the
late half of the history, a validated control, M1 95.1, M2 96.0, mean net R
positive. `artifacts/slice57_stage1_funding_carry_fade_btc_v1.json` carries the
numbers **and** a section named `HOW_FRAGILE_THIS_RESULT_IS`. Read that section.

It does **not** mean this shell trades it. Nothing wires that signal into the
engine, no trading path consults the field, and the `NO EDGE CLAIM` line is
unchanged — that line is a statement about this process, which still executes no
edge. The status tool prints an `ADVISORY` naming both facts and exits 0.

Those five are real `ProjectStatus` field names, not prose — a test asserts that
each one is a field on the dataclass and that the value the code returns under a
stock configuration is the value printed above. If the field names in this
runbook ever stop matching the code, that test goes red before an operator can
be misled by it.

**If any of those five reads differently, stop.** Something has changed that
this runbook does not cover, and the correct response is to find out what before
starting a process that trades.

On startup the log prints the project-mode line and the session-start line, both
of which carry `NO EDGE CLAIM — timing-skill research CLOSED`. If you do not see
that string, see E.

## C. Kill switch

**The bot may trip it. Only a human clears it.** That asymmetry is the point,
and there is no code path in this repository that clears it automatically.

Trip it (also happens automatically on a drawdown breach or any bracket/stop
failure):

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.trip_kill_switch('operator: standing down'); s.close()"
```

Engaged, it blocks new risk everywhere at once: startup refuses, the gate chain
blocks with `KILL_SWITCH_ENGAGED`, health reports unhealthy. **If the switch
cannot be read, it is treated as engaged** — an unanswerable question is a "no".
It is persisted and survives a restart.

Clearing requires the exact operator acknowledgement, which is deliberately not
derivable from config and not stored in any settings file:

```bash
python3 -c "from persistence import StateStore; \
s = StateStore('state/trading_state.db'); \
s.clear_kill_switch_by_human('HUMAN_CLEARED_KILL_SWITCH'); s.close()"
```

That token is executed by a test, not merely quoted here: the test trips a real
switch and clears it with the literal string printed above. A runbook command
that has never been run is a guess.

Before you clear it, know why it tripped. A switch cleared without a diagnosis
is a switch that will trip again with a position on.

## D. Session / health log contract

Every startup line, every health payload and every stored session record:

* `no_edge_claim` = `NO EDGE CLAIM — timing-skill research CLOSED`
* `cleared_edge_signal` = `null`
* `timing_skill_research` = `CLOSED`

**And what they must never show.** No PnL, profit, return, win-rate, equity,
performance, Sharpe, expectancy or drawdown field, in any session surface.

This is a deliberate refusal of a feature, and it is load-bearing. A scoreboard
is the mechanism by which a paper shell becomes a profit claim: once a record
carries an equity curve, the next reader compares numbers across runs, and a
comparison of numbers is a performance claim whether or not anyone meant one.
`session_log.FORBIDDEN_FIELD_MARKERS` enforces it, and a structural test fails
if any module tries to add one.

For a long-running process the temptation is sharper, not weaker: a week of
sessions invites a summary, and a summary invites a total. There is no supported
way to produce one from these records, and that is the intended state.

## E. Pause, stand-down and revocation

### E.1 Pause — stand down without killing

```bash
ENTRIES_ENABLED=0 python3 main.py
```

The loop, reconciliation, stop management and health stay live. No signal source
is attached, so no new entry is proposed. Existing positions keep their verified
protective stop.

> **`ENTRIES_ENABLED` is an operator convenience, not a safety gate.** The gates
> are the kill switch, the live-arming chain and the risk-gate set. Standing
> entries down does not weaken them and is **never** a substitute for any of
> them. Never use it to make an unsafe configuration safe.

Any unrecognised value parses as **disabled**. A typo costs you entries, not
safety.

### E.2 Revocation — when this certification stops applying

If any of these becomes true, the shell is **no longer certified** and must not
run unattended until it is green again. Do not renegotiate them; fix them.

* a session surface stops carrying `NO EDGE CLAIM`;
* a non-human path clears the kill switch;
* `gate_order` consults `ENTRIES_ENABLED`;
* a credential failure degrades to **live** rather than paper;
* a frozen family becomes registrable;
* a naked position survives a cycle;
* a PnL-shaped field appears in a session record.

Each has a test in `tests/test_paper_agent_certification.py`. If one goes red,
that is the revocation notice. Stand entries down (E.1) or trip the kill switch
(C) first, diagnose second.

## F. Pointers

| you want | read |
|---|---|
| every command, in full | [PAPER_RUNBOOK.md](PAPER_RUNBOOK.md) |
| what is certified, and how it is tested | [PAPER_AGENT_CERTIFICATION.md](PAPER_AGENT_CERTIFICATION.md) |
| why no research is running | [RESEARCH_HOLD.md](RESEARCH_HOLD.md) |
| what was measured and what it read | [RESEARCH_PROGRAM_FREEZE.md](RESEARCH_PROGRAM_FREEZE.md) |
| the bars, the instrument, the slice-by-slice record | `EDGE.md` |
| how a new hypothesis is allowed to start | `NEW_SIGNAL_INTAKE.md` (human-filled, before any code) |
| the frozen names in code | `project_status.FROZEN_ABSENT`, `FROZEN_STATUS` |

## G. Forbidden

Not "discouraged" — each of these is refused by code, and every one has a test:

| forbidden | what stops you |
|---|---|
| arming live | needs testnet off **and** paper off **and** the exact `LIVE_TRADING_ACK` **and** both credentials. Missing any one degrades to paper **and logs the reason** |
| loading a model | `models/current` does not exist; `POLICY_MODE` is `off`; a model could only ever write `win_probability` |
| registering an edge | the hook refuses all eleven frozen names even given a perfect artefact — and refuses a **genuine, attested** POSITIVE whose family's pre-declared symbol universe is not satisfied (`MULTI_SYMBOL_MINIMUMS`, slice 55). There is one such artefact on disk today, and since slice 56 it is refused twice over |
| a scoreboard field | `FORBIDDEN_FIELD_MARKERS`, plus an AST test over the session module |
| clearing the kill switch from code | no trading module calls the clear path; exactly one statement in the codebase clears it |
| lowering a bar | M1 = M2 = 95.0 and the three control clauses are asserted by tests |

Also forbidden, by human decision rather than by code, and therefore your
responsibility: **do not treat a paper fill, a geometry number, corpus
eligibility, or a long clean uptime as evidence of timing skill.** None of them
is.

## H. Honest limitations

* **No Stage-1 POSITIVE exists.** The shell executes a strategy-neutral
  configuration; there is no evidence its entries beat their own dates.
* **Perp (`linear`) simulation raises `NotImplementedError`.** The linear *live*
  path is implemented and unit-tested; the *simulator* models spot cash
  accounting only, so a linear backtest would omit funding, margin and
  liquidation and report a flattering number. A missing number is better than
  an invented one.
* **The corpora are Bitstamp BTC/USD and Binance spot, not Bybit.** No order
  book, so the liquidity gate abstains and 3 of 35 features are unavailable.
* **Certification is of the shell, not of a strategy.** It says the process
  will not lie, will not arm and will not strand a position. It says nothing
  about whether trading it would make money — and the measured answer to that
  is eleven families closed with nothing cleared — the eleventh measured in
  slice 55 and frozen in slice 56, having cleared on one symbol of the two its
  own rule required.
* **A percentile above 95 exists in `artifacts/`, and it is not an edge claim.**
  `slice55_edge_funding_carry_fade_BTCUSDT_summary.json` reads M1 97.0 / M2 97.5
  with a validated control. It registers nothing, because its family declared a
  three-symbol universe needing two and the companions failed. If you are ever
  tempted to read a single summary file as a result, read that one and then read
  the two beside it.
