#!/usr/bin/env python3
"""Report how far behind the forward corpus is, and why it may not catch up yet.

THE FINDING THIS TOOL EXISTS TO KEEP VISIBLE
============================================
The pilot's forward-trade count has read `0 of 20` for sixteen slices. It was
never the rule waiting. `t1` is 2026-08-09, the daily corpus ends 2026-08-24,
and Binance has published a bar every day since — so the window has been frozen
at 15 bars while three weeks of data accumulated at the venue. Barrier
eligibility reaches back seven bars from the end of the file, so every flagged
setup sat stranded in the tail.

Appending the bars the venue already has takes the window from 15 to 37 and the
ladder from `5/4/0/0/0/0` to `8/8/8/4/4/3`. **Three closed forward trades**, the
first the programme has ever had. Measured, then reverted — see below.

AND WHY IT DOES NOT WRITE
=========================
`data/real_linear_1d` and `data/real_funding` were read by TWO programmes with
incompatible requirements, and nothing in the tree said so:

* the forward pilot needs a corpus that GROWS;
* the carry baseline read the same files through `carry_backtest.simulate`,
  which loaded them **whole, with no date bound**, and `PHASE1_DECISION.md`
  quotes the result — **+9.66 %/yr, n=15** — in two places.

So every bar the pilot gained silently restated a shipped decision figure, and
the counter sat at 0 of 20 for sixteen slices while the bars it needed were
already committed to the tree. That was never patience; it was two questions
sharing one unbounded read.

RESOLVED, AND THIS IS THE SHAPE OF IT
=====================================
* the FROZEN read is cut at `carry_backtest.FROZEN_SNAPSHOT_CUT_*` — daily
  through 2026-08-24, funding through 2026-08-25 — so a `corpus="frozen"`
  measurement cannot see an append at all. It reproduces **9.6578 %/yr on 15
  trades** before and after. Bounding at `t1` was tried first and measured
  9.5464, outside the pin's tolerance: the quoted figure was always computed
  over the whole file, 15 post-t1 bars included;
* the PILOT is scored against the append-only **full** corpora, which track the
  venue. That is `FORWARD_DATA_DIR` / `FORWARD_FUNDING` below.

Those two corpora are not interchangeable as measurements — `full` starts
2019-09-08 and spans a different regime. It supplies the forward COUNTER; no
carry figure may be quoted from it.

WHAT IT DOES
============
1. asks the venue what has closed since the corpus's last bar — reading only;
2. reports what an append WOULD add, and what the window would become;
3. checks the frozen baseline still reproduces 9.66 / 15, and says so;
4. scores the forward window to a scratch path, never to `artifacts/`.

`--write` is deliberately absent. Appending is done by
`tools/append_closed_corpus.py`, which already exists, is tested, refuses on a
digest mismatch and refuses on a gap. This tool does not duplicate it.

    7 1 * * *  cd /path/to/bot && /path/to/.venv/bin/python \\
               tools/daily_forward_refresh.py >> state/forward_refresh.log 2>&1

Exit codes: 0 reported; 1 the venue or the corpus refused; 3 the frozen
baseline is ALREADY not reproducing, which means someone appended.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import append_closed_corpus as acc                  # noqa: E402
import corpus_prefix as cp                          # noqa: E402
import promotion_gate as _gate                      # noqa: E402

FORWARD_TOOL = "slice76_forward_shadow.py"

#: The APPEND-ONLY corpora the Stage B counter is scored against. Not the
#: frozen pair: those are cut at the snapshot boundary so the carry figure
#: stays reproducible, and scoring a forward counter against a deliberately
#: bounded corpus is how it read 0 of 20 while the bars were already in tree.
FORWARD_DATA_DIR = "data/real_linear_1d_full"
FORWARD_FUNDING = ("data/real_funding_full/funding/"
                   "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")

#: Rolling evidence, not a slice artefact. `connector_check.json` and
#: `kill_switch_drill.json` are the precedent: a recurring measurement that
#: carries no slice number and writes no prose.
FORWARD_EVIDENCE = os.path.join("artifacts", "forward_shadow_current.json")

#: The figure PHASE1_DECISION.md quotes, and the reason this tool will not
#: write. Not re-derived here: it is a number in a shipped document.
PHASE1_ANNUALISED_PCT = 9.66
PHASE1_TRADES = 15


def frozen_baseline_intact() -> Dict[str, Any]:
    """Does the carry baseline still reproduce the figure PHASE1 quotes?"""
    os.environ.setdefault("LEDGER_DISABLED", "1")
    import carry_backtest as cb                     # noqa: PLC0415
    result = cb.simulate(REPO, borrow_apr=0.0, gated=True)
    pct, trades = result["net_annualised_pct"], result["trades"]
    return {
        "net_annualised_pct": round(float(pct), 4),
        "trades": int(trades),
        "expected_pct": PHASE1_ANNUALISED_PCT,
        "expected_trades": PHASE1_TRADES,
        "intact": abs(float(pct) - PHASE1_ANNUALISED_PCT) < 0.01
                  and int(trades) == PHASE1_TRADES,
    }


def score_forward(scratch: str, observed_utc: str) -> Optional[Dict[str, Any]]:
    """Run the forward scorer to a scratch path. NEVER to artifacts/.

    The scorer defaults `--out` to a committed slice artefact, and re-running
    it at a later date overwrites that record with different numbers. Three
    committed artefacts were clobbered that way during 0060 and had to be
    restored from git, so the path is always passed explicitly.

    THE PILOT READS `full`, NOT `frozen`. Those are two different questions
    over the same venue. `frozen` is a SNAPSHOT bounded at
    `carry_backtest.FROZEN_SNAPSHOT_CUT_*` so the carry figure
    `PHASE1_DECISION.md` quotes stays reproducible; `full` is append-only and
    tracks the venue, which is what a forward counter has to do. Scoring the
    pilot against `frozen` is what kept the counter at 0 while the bars it
    needed were already on disk.
    """
    out = os.path.join(scratch, "forward.json")
    log = os.path.join(scratch, "forward.log")
    done = subprocess.run(
        [sys.executable, os.path.join(HERE, FORWARD_TOOL),
         "--observed-at-utc", observed_utc,
         "--data-dir", FORWARD_DATA_DIR,
         "--funding-data", FORWARD_FUNDING,
         "--out", out, "--log", log],
        cwd=REPO, capture_output=True, text=True, timeout=600)
    if done.returncode != 0 or not os.path.exists(out):
        return None
    with open(out, encoding="utf-8") as handle:
        return json.load(handle)


def run(*, scratch: Optional[str] = None) -> Dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    observed = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # READ ONLY. write is not a parameter of this function on purpose.
    behind = acc.run(observed_at_utc=observed, write=False)

    report: Dict[str, Any] = {
        "tool": "daily_forward_refresh",
        "observed_utc": observed,
        "writes": False,
        "would_append": {
            "closed_bars": behind["new_closed_bars"],
            "funding_prints": behind["new_funding_prints"],
            "corpus_last": behind["linear_last_before"],
            "corpus_would_end": behind["linear_last_after"],
        },
        "append_tool": "tools/append_closed_corpus.py",
    }

    window = {}
    for path in (cp.LINEAR_BTC, cp.FUNDING_BTC):
        check = cp.check(path)
        window[os.path.basename(path)] = {
            "rows_on_disk": check.rows_on_disk,
            "appended_rows": check.appended_rows,
            "history_unchanged": bool(check.history_unchanged),
        }
    report["corpus"] = window

    report["frozen_baseline"] = frozen_baseline_intact()

    with tempfile.TemporaryDirectory() as tmp:
        scored = score_forward(scratch or tmp, observed)
    if scored is not None:
        report["state_ladder"] = scored.get("state_ladder")
        report["forward_n_trades"] = scored.get("forward_n_trades")
        report["forward_bars"] = (scored.get("ceiling") or {}).get(
            "closed_forward_bars")
    report["gate_requires"] = {
        "closed_forward_trades": _gate.MIN_FORWARD_TRADES,
        "forward_days": _gate.MIN_FORWARD_DAYS,
    }
    report["previously_blocked_because"] = (
        "RESOLVED. carry_backtest.simulate used to read data/real_funding and "
        "data/real_linear_1d WHOLE, with no date bound, so every bar the "
        "forward pilot gained silently restated the +9.66%/yr n=15 figure "
        "PHASE1_DECISION.md quotes — which is why the counter sat at 0 while "
        "the bars it needed were already in tree. The frozen read is now cut "
        "at carry_backtest.FROZEN_SNAPSHOT_CUT_* and reproduces 9.6578 / 15 "
        "after the append; the pilot is scored against the append-only full "
        "corpora instead. The two questions no longer share one unbounded "
        "read.")
    report["appending_is_not_this_tools_job"] = (
        "tools/append_closed_corpus.py — it hash-checks before writing and "
        "refuses a gap. This tool reads and reports.")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scratch", default=None,
                        help="where to score (default: a temporary directory; "
                             "NEVER artifacts/)")
    args = parser.parse_args(argv)

    try:
        report = run(scratch=args.scratch)
    except acc.Refuse as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                        # noqa: BLE001
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    behind = len(report["would_append"]["closed_bars"])
    trades = report.get("forward_n_trades")
    print(f"\ncorpus is {behind} closed bars behind the venue; forward closed "
          f"trades {trades} of {_gate.MIN_FORWARD_TRADES}", file=sys.stderr)
    if not report["frozen_baseline"]["intact"]:
        print("THE FROZEN BASELINE NO LONGER REPRODUCES PHASE1_DECISION — "
              "someone appended to a corpus it is pinned to.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
