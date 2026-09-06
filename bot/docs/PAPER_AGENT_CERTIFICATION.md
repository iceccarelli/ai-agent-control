# PAPER AGENT CERTIFICATION

**Written at slice 42, before any test or line of code in that slice was
written.** The order matters and is the point: a certification checklist
assembled after the evidence is a description of what passed, not a standard.

---

## 0. What this certifies, and what it does not

This document certifies that the process in this repository can be left
**running unattended in paper mode** without lying about itself, arming
anything, or leaving a position unprotected.

It is not a claim about profit, and it is not a step toward one.

| | |
|---|---|
| Closer to an autonomous **profit** agent? | **NO.** Nothing here measures or produces edge. |
| Closer to an autonomous **paper execution shell**? | Yes — if every group in §3 is green. |

**Paper success ≠ edge.** A session that starts, reconciles, stays healthy and
shuts down cleanly proves the plumbing works. Fills in paper mode are
simulated; a positive paper PnL is an artefact of drift and the simulator, and
this repository deliberately keeps PnL out of the session log so that nobody —
human or agent — can mistake one for the other.

## 1. Research is frozen. This slice does not touch it.

Three families have been measured against bars pre-declared in EDGE.md §5b
before any of their numbers existed. All three read ABSENT. The bar has never
moved.

| family | reading | bar | closed |
|---|---|---:|---|
| `technical_analysis` / `closed_analyser` | 76.1 / 77.5 (1D), 73.0 / 74.0 (4H), 72.2 / 76.0 (1H) | 95.0 | slices 24–25 |
| `donchian_breakout_v1` | 91.2 / 91.5 (1D) | 95.0 | slice 28 |
| `btc_alt_spillover_v1` | 94.3 / 92.0 (ETHUSDT), 91.3 / 90.0 (SOLUSDT) | 95.0 | slice 40, frozen slice 41 |

`ProjectStatus.cleared_edge_signal` is `null` and stays `null`.

### Explicit non-goals of this slice

* **No new signal.** `NEW_SIGNAL_INTAKE.md` stays WAITING and is not filled —
  a hypothesis proposed by the thing that measures it is not an independent
  hypothesis.
* **No reopening** of any frozen family, by retune, grid, extra filter or
  "almost 95".
* **No model.** Nothing is trained, promoted or loaded; `models/current` stays
  absent.
* **No live.** `LIVE_TRADING_ACK`, `POLICY_MODE=live` and every other arming
  path stay shut.
* **No bar or clause changes.** M1 = M2 = 95.0; the control stays |z| < 1.96
  **and** KS p ≥ 0.05 **and** incompletes ≤ 5%.
* **No scoreboard.** No PnL, win-rate or equity field is added to any session
  surface. The reason is in §4.

### The residual directed-control bias is informational only

The directed instrument carries roughly **1.5 percentile points of unexplained
upward bias** (EDGE.md §22d). It is recorded so that a future *directed* thesis
inherits the warning. **It is not to be "fixed" here.** Fixing it changes a
measurement instrument, which requires a human pre-declaration made in writing
before the run it enables. This slice runs no measurement and touches no
instrument.

## 2. The standard

A paper agent is certified when it can run unattended and **all six** of the
following hold. Each is a property of the code, asserted by tests, not a
statement of intent.

1. it always advertises **NO EDGE CLAIM**;
2. it can never clear its own kill switch;
3. it never substitutes an operator convenience for a risk gate;
4. it never opens a live path, and degrades **closed** when credentials are
   missing;
5. it cannot be talked into registering an edge for a frozen family;
6. it ends a session with no naked position.

## 3. Certification checklist

The groups below are the standard. Each is implemented as tests in
`tests/test_paper_agent_certification.py` and run in order.

### Group A — Truthfulness surfaces

Startup logging, the health payload and every stored session record state
`timing_skill_research=CLOSED`, `cleared_edge_signal=null`, and carry the
`NO EDGE CLAIM` line, while the freeze is current. The claim must be derived
from the freeze state rather than hard-coded, so that it cannot drift away from
what is true.

### Group B — Kill switch

No trading module — engine, memory, strategy, ML, risk — may clear the kill
switch. The only clearing path is an explicit human operator call requiring an
acknowledgement token. Asserted structurally (AST) rather than by text search,
because a text ban forces the code to stop explaining what it forbids — a
lesson this repository learned five times.

### Group C — Operator control vs risk gates

`ENTRIES_ENABLED` selects whether a signal source is attached. It is an
operator control, **not a safety gate**, and `gate_order` must never consult
it: a risk decision that can be turned off by a convenience flag is not a risk
decision. Re-asserted here because the invariant is easy to erode by
accident.

### Group D — Paper stays paper

Paper mode refuses live endpoints. Missing or malformed credentials degrade to
paper **and log the reason** — never a silent live path, never a silent
success. Live requires an explicit acknowledgement that the stock environment
does not provide.

### Group E — Freeze integrity

A forged `EDGE_EVIDENCE_POSITIVE` artefact naming any frozen family is refused
even when it is otherwise perfect: correct verdict, both bars cleared,
`m2.passed`, a genuine control attestation, and every declared symbol present.

### Group F — Paper cycle harness

A short end-to-end cycle against the fake exchange asserting: zero live
authorisation throughout, no naked position at the end, and session lines still
carrying `NO EDGE CLAIM`.

## 4. Why there is no PnL in the session log

A scoreboard is the mechanism by which a paper shell becomes a profit claim.
Once a session record carries an equity curve, the next reader — human or
session — compares numbers across runs, and a comparison of numbers is a
performance claim whether or not anyone intended one. The measured position of
this project is that **no timing skill has been demonstrated**, so any number
that looks like performance is drift, simulator artefact, or noise.

`session_log.FORBIDDEN_FIELD_MARKERS` enforces this: `pnl`, `profit`,
`return`, `win_rate`, `equity`, `sharpe`, `expectancy`, `drawdown` and their
neighbours may not appear as fields. That is a deliberate refusal of a feature,
and it is load-bearing.

## 5. Honest limitations of a certified paper agent

Certification is bounded, and the bound is stated here rather than discovered
later:

* **No Stage-1 POSITIVE exists.** The agent executes a strategy-neutral shell.
  There is no evidence any of its entries are better than their own dates.
* **It is not a profit agent** and cannot become one by running longer.
* **Perp (`linear`) simulation raises `NotImplementedError`.** The linear
  *live* path is implemented and unit-tested; the *simulator* models spot cash
  accounting only, so a linear backtest would omit funding, margin and
  liquidation and report a flattering result. A missing number is better than
  an invented one. See `MARKET_CATEGORIES.md`.
* **The corpus is Bitstamp BTC/USD and Binance spot**, not Bybit. No order
  book, so the liquidity gate abstains and 3 of 35 features are unavailable.
* **The directed measurement instrument retains ~1.5 points of upward bias**
  (§1). It is not used by the paper path, but it bounds what any future
  directed reading is worth.
* **Certification is of the shell, not of a strategy.** It says the process
  will not lie, will not arm, and will not strand a position. It says nothing
  about whether trading it would make money, and the measured answer to that
  question is currently *no evidence either way, leaning no*.

## 6. What would revoke this certification

Any of: a session surface that stops carrying `NO EDGE CLAIM`; a non-human
path that clears the kill switch; `gate_order` consulting `ENTRIES_ENABLED`;
a credential failure that degrades to live rather than paper; a frozen family
becoming registrable; a naked position surviving a cycle; or a PnL-shaped
field appearing in a session record.

Each of those has a test. If one goes red, the certification is void until it
is green again — not renegotiated.
