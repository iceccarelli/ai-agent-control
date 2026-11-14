#!/usr/bin/env python3
"""e1_status.py -- READ-ONLY integrity and freshness check for the registered forward kill test (E1, docs/MONEY_MISSION.md, Round 19 log).

It answers three questions and computes NO result:
  1. Is the engine the registered one?  The six engine files LISTED in ENGINE_FILE_PINS are compared by sha256 with the values they had at the registered revision 56f9da6, and tools/carry_replay.py by
     two source-text digests (every definition except the console section, and the head of main() up to the replay call) that tests/test_carry_replay_concentration.py also pins. Modules those files
     import but that are not listed (execution_record, economic_model, trade_economics) are NOT pinned: the committed replays' nets ($33,049.51 overlay, $16,398.79 acquire) are asserted by a test instead. A mismatch means the experiment's engine moved: re-register it with the owner, never "fix" the pin.
  2. Is there forward data?  The last committed Bybit BTCUSDT funding print and 4h kline are read from the committed corpus and compared with the registered window start. Rows before the window are
     never used. Today none is committed, so the answer is NO_FORWARD_DATA and the test is INCOMPLETE.
  3. When may a statistic exist?  The six-month interim and the twelve-month final are dated from the window start. Before the interim date this tool reports NOT_AVAILABLE_YET and states exactly what is
     missing; it never prints an excess, a bound, a verdict or a trend, and it never reads the owner's holdout, a candidate ledger or a capital anchor.

No network, no credential, no write. The capture commands in the output are for the OWNER to run (Bybit geo-blocks some hosts; the repository never fetches).
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

#: the registered window: Bybit BTCUSDT funding + 4h klines from this settlement onward (docs/MONEY_MISSION.md, E1). A row before it is never used.
WINDOW_START_UTC = "2026-09-09T16:00:00Z"
WINDOW_START_MS = int(dt.datetime(2026, 9, 9, 16, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
REGISTERED_ENGINE_REVISION = "56f9da60d1b1b35c220b771986233f85d019bb86"
INTERIM_MONTHS, FINAL_MONTHS = 6, 12
FUNDING_INTERVAL_MS = 8 * 3600 * 1000
CORPUS = os.path.join(BOT, "data", "real_bybit_btc_4h")

#: sha256 of each engine file at the registered revision (taken from `git show 56f9da6:bot/<path>`; byte-identical at 413175f, the Round 20 base).
ENGINE_FILE_PINS = {
    "carry_engine.py": "d92ee8d9a89e2d4c6e4c9ed64342813a33ad935bbd7d5710e914f0676d884bef",
    "carry_costs.py": "83ffcf0be2ae232e7e0a4d947cdf5d6ed52f35a4661f3309fb510d07d15207da",
    "tools/carry_backtest.py": "ddfc246123e6dd41685a7f76bc7ce5a50f5d11b017b53b6b34de1bb6a2dad365",
    "ledger.py": "52434ff311dda6e13abcc89051c8933fe38383127b51034eb25b8e9096ebc044",
    "carry_risk.py": "57071cd6df823e0904b35e565186495aa109f3f876f2cad14a80a9837d11c54b",
    "market_snapshot.py": "e620bff1f461ab146ec2b2ed23da378b03253c1f66ed34d12043634cd1517fa1",
}
#: tools/carry_replay.py: its console section may change, its computation may not (the digest of every other top-level definition).
REPLAY_FILE = "tools/carry_replay.py"
REPLAY_PRESENTATION = {"main", "capital_lines", "denominator_lines", "usdt_financing_lines", "owner_capital_lines", "concentration_lines"}
REPLAY_COMPUTATION_DIGEST = "e239af91e29c70fdd4809615a01a36b468565aafb6d06d1c70a7cbfea372f545"
#: and the part of main() that decides what the registered command RUNS: every statement up to and including the `replay(...)` assignment (the argparse defaults, the corpus choice, the impact haircut, the
#: cost-of-capital fallback fed to the entry gate, the replay arguments). Only the printing and --json section after it is outside the pin.
REPLAY_MAIN_HEAD_DIGEST = "e02229601f338f8bc0d135363504fbd79c75422e6a29940661fb19ea03c04c7e"

OWNER_COMMANDS = (
    "cd bot && python3 tools/funding_capture.py --start-ms %d --out-dir <NEW directory outside git>      # Bybit BTCUSDT funding from the window start (network: yours)" % WINDOW_START_MS,
    "cd bot && python3 tools/market_history.py --help      # the linear 4h kline dataset: confirm the subcommand, then capture --interval 240 from %s into a NEW root outside git" % WINDOW_START_UTC,
    "cd bot && python3 tools/fetch_borrow_rates.py --help      # the OKX USDT lending path after 2026-09-21T18:00Z (the committed one ends there)",
    "NO REPOSITORY TOOL captures Bybit SPOT BTCUSDT 4h klines or converts a capture to the replay's --bars format: the data bundle cannot be assembled by the repository today",
)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def replay_computation_digest(src: str) -> str:
    """sha256 of the exact source text of every top-level definition of carry_replay.py except the console section."""
    tree = ast.parse(src)
    lines = src.splitlines()
    parts: List[str] = []
    for n in tree.body:
        name = getattr(n, "name", None) or ""
        if name in REPLAY_PRESENTATION or name.startswith("present_"):
            continue
        start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        parts.append("\n".join(lines[start - 1:n.end_lineno]))
    return hashlib.sha256("\n\x00\n".join(parts).encode()).hexdigest()


def replay_main_head_digest(src: str) -> Optional[str]:
    """sha256 of the exact source text of main()'s statements up to and including the replay(...) assignment; None when there is no such statement (a restructured main is not the registered one)."""
    lines = src.splitlines()
    for n in ast.parse(src).body:
        if getattr(n, "name", None) == "main":
            parts: List[str] = []
            for st in n.body:
                parts.append("\n".join(lines[st.lineno - 1:st.end_lineno]))
                if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and getattr(st.value.func, "id", None) == "replay":
                    return hashlib.sha256("\n\x00\n".join(parts).encode()).hexdigest()
    return None


def engine_integrity(root: str = BOT) -> Dict[str, Any]:
    rows = []
    for rel, pin in sorted(ENGINE_FILE_PINS.items()):
        path = os.path.join(root, rel)
        got = _sha(path) if os.path.isfile(path) else None
        rows.append({"file": rel, "pinned": pin, "actual": got, "match": got == pin})
    try:
        src = open(os.path.join(root, REPLAY_FILE), encoding="utf-8").read()
        digest, head = replay_computation_digest(src), replay_main_head_digest(src)
    except (OSError, SyntaxError):
        digest = head = None
    rows.append({"file": REPLAY_FILE + " (computation only)", "pinned": REPLAY_COMPUTATION_DIGEST, "actual": digest, "match": digest == REPLAY_COMPUTATION_DIGEST})
    rows.append({"file": REPLAY_FILE + " (main(): arguments, corpus, haircut, cost-of-capital fallback, replay call)", "pinned": REPLAY_MAIN_HEAD_DIGEST, "actual": head, "match": head == REPLAY_MAIN_HEAD_DIGEST})
    return {"registered_revision": REGISTERED_ENGINE_REVISION, "files": rows, "engine_is_the_registered_one": all(r["match"] for r in rows)}


def _last_ms(path: str, column: str, *, parse_iso: bool = False) -> Optional[int]:
    last: Optional[int] = None
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for rec in csv.DictReader(fh):
                v = rec.get(column)
                if not v:
                    continue
                try:
                    ms = int(dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000) if parse_iso else int(float(v))
                except (ValueError, OverflowError):
                    continue
                last = ms if last is None or ms > last else last
    except OSError:
        return None
    return last


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if ms is None else dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_months(start: dt.datetime, months: int) -> dt.datetime:
    y, m = divmod(start.month - 1 + months, 12)
    return start.replace(year=start.year + y, month=m + 1)


def forward_data(root: str = CORPUS, *, now_ms: int) -> Dict[str, Any]:
    fund = _last_ms(os.path.join(root, "funding", "BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz"), "funding_time_ms")
    kline = _last_ms(os.path.join(root, "ohlcv", "BYBIT_LINEAR_BTC_USDT_4H.csv.gz"), "time_period_end", parse_iso=True)
    spot = _last_ms(os.path.join(root, "ohlcv", "BYBIT_SPOT_BTC_USDT_4H.csv.gz"), "time_period_end", parse_iso=True)
    elapsed_ms = max(0, now_ms - WINDOW_START_MS)
    expected_prints = (elapsed_ms // FUNDING_INTERVAL_MS + 1) if now_ms >= WINDOW_START_MS else 0           # a print settled AT OR BEFORE now counts, the window's first print (its start instant) included
    in_window = max(0, ((fund - WINDOW_START_MS) // FUNDING_INTERVAL_MS) + 1) if (fund is not None and fund >= WINDOW_START_MS) else 0
    state = "NO_FORWARD_DATA" if in_window == 0 else ("FORWARD_DATA_PRESENT_BUT_INCOMPLETE" if in_window < expected_prints else "FORWARD_DATA_CURRENT")
    return {"state": state, "last_committed_funding_print": _iso(fund), "last_committed_linear_4h_kline_close": _iso(kline), "last_committed_spot_4h_kline_close": _iso(spot),
            "window_start": WINDOW_START_UTC, "elapsed_days": round(elapsed_ms / 86_400_000, 3), "funding_prints_expected_since_window_start": int(expected_prints),
            "funding_prints_committed_in_window": int(in_window), "funding_prints_missing": int(max(0, expected_prints - in_window)),
            "rows_before_the_window": "never used", "corpus": os.path.relpath(root, BOT) if root.startswith(BOT) else root}


def schedule(*, now_ms: int) -> Dict[str, Any]:
    start = dt.datetime.fromtimestamp(WINDOW_START_MS / 1000.0, dt.timezone.utc)
    interim, final = _plus_months(start, INTERIM_MONTHS), _plus_months(start, FINAL_MONTHS)
    now = dt.datetime.fromtimestamp(now_ms / 1000.0, dt.timezone.utc)
    out: Dict[str, Any] = {"window_start": WINDOW_START_UTC}
    for name, when in (("interim_six_months", interim), ("final_twelve_months", final)):
        out[name] = {"not_before": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "AVAILABLE_AFTER_DATA_CHECK" if now >= when else "NOT_AVAILABLE_YET",
                     "days_remaining": max(0, (when - now).days)}
    return out


def status(*, now_ms: Optional[int] = None, root: str = BOT, corpus: str = CORPUS) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    eng, data, sched = engine_integrity(root), forward_data(corpus, now_ms=now_ms), schedule(now_ms=now_ms)
    problems: List[str] = []
    if not eng["engine_is_the_registered_one"]:
        problems.append("ENGINE_MOVED: an engine file differs from the registered revision; the experiment is no longer the registered one")
    if data["state"] != "FORWARD_DATA_CURRENT":
        problems.append(f"{data['state']}: {data['funding_prints_missing']} funding print(s) missing since {WINDOW_START_UTC}; the Bybit spot kline and the OKX path are not captured either")
    if sched["interim_six_months"]["status"] == "NOT_AVAILABLE_YET":
        problems.append(f"INTERIM_NOT_AVAILABLE_YET: not before {sched['interim_six_months']['not_before']} ({sched['interim_six_months']['days_remaining']} days)")
    return {"schema": "e1_status/v1", "experiment": "E1 forward kill test of the frozen BTC overlay (registered once; one trial; no holdout)", "now": _iso(now_ms),
            "engine": eng, "data": data, "schedule": sched,
            "result": None, "verdict": "INCOMPLETE" if problems else "DATA_AND_ENGINE_CHECKS_PASS_NO_STATISTIC_COMPUTED_HERE",
            "problems": problems, "owner_commands": list(OWNER_COMMANDS),
            "note": "this tool computes no excess, bound, p-value or verdict and reads no holdout, candidate ledger or capital anchor; an incomplete forward test is not a winner and not a rejection"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--now-ms", type=int, default=None, help="evaluate as of this epoch-ms (tests); default: the clock")
    a = ap.parse_args(argv)
    doc = status(now_ms=a.now_ms)
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0 if not doc["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
