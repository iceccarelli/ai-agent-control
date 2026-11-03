"""Execution-record persistence must not lose evidence, and its miss rate must
use the true number of filled orders.

(A) `append_execution_records` marked each record as written BEFORE the file
write. A failed write (unwritable path, full disk) therefore lost those records
for good: the drill finishes twice, the second pass found nothing "new", wrote
nothing and raised nothing, and the run still read as PASSED.

(B) The report's miss rate divided unfilled orders by the number of VALID,
book-measured observations, not by the number of orders that filled. Filled
orders that could not be priced (reference is the mark, record unverifiable)
vanished from the denominator, so the miss rate was overstated and the true
denominator hidden.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools"),
          HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import drill                                                 # noqa: E402
import execution_samples_report as esr                       # noqa: E402
import ledger as L                                           # noqa: E402
from execution_fixtures import valid_record                  # noqa: E402

T0 = 1_700_000_000_000


def journal_with(n, *, basis=None):
    j = L.Journal()
    for i in range(n):
        rec = valid_record("Sell", "linear", ms=T0 + 10_000 * i, link=f"o{i}")
        if basis:
            rec["reference_basis"] = basis
        L.perp_open(j, ms=T0 + 10_000 * i, ref=f"o{i}", qty=0.01,
                    price=60000.0, fee_usd=0.2, fee_status="confirmed",
                    fee_raw={"execution": rec})
    return j


def test_a_failed_write_does_not_mark_the_records_as_written(tmp_path):
    j = journal_with(3)
    written = set()
    bad = tmp_path                                  # a directory: open() raises
    try:
        drill.append_execution_records(str(bad), j, "testnet", written)
        raised = False
    except OSError:
        raised = True
    assert raised and written == set()              # nothing is "done"
    good = tmp_path / "records.jsonl"
    # the second pass (the drill finishes twice) now writes what the first lost
    assert drill.append_execution_records(str(good), j, "testnet", written) == 3
    assert len(good.read_text().splitlines()) == 3


def test_a_successful_write_is_still_written_once(tmp_path):
    j = journal_with(2)
    written = set()
    path = str(tmp_path / "r.jsonl")
    assert drill.append_execution_records(path, j, "testnet", written) == 2
    assert drill.append_execution_records(path, j, "testnet", written) == 0
    assert len(open(path).read().splitlines()) == 2


def test_the_miss_rate_counts_filled_orders_that_could_not_be_priced(tmp_path):
    # 10 orders FILLED but their reference was the mark (not book-measured),
    # and 2 market orders that filled nothing.
    j = journal_with(10, basis="mark")
    outcomes = [{"status": "NO_FILL", "product": "linear", "side": "Sell",
                 "order_type": "market", "symbol": "BTCUSDT",
                 "order_link_id": f"miss{i}", "decision_ms": T0 + 500_000 + i,
                 "recorded_ms": T0 + 500_000 + i,
                 "reasons": ["NO_FILL: venue returned no fill"]}
                for i in range(2)]
    path = str(tmp_path / "r.jsonl")
    drill.append_execution_records(path, j, "testnet", set(), outcomes=outcomes)
    out = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=T0 + 600_000, records=[path],
                    environment="testnet")
    leg = out["outcomes"]["miss_rate"]["linear/Sell/market"]
    assert leg["unfilled_orders"] == 2
    assert leg["filled_orders"] == 10                # not 0
    assert abs(leg["miss_rate"] - 2 / 12) < 1e-9


# -- the records path is stated, not guessed -----------------------------------

def test_the_records_file_sits_beside_the_run_folder_in_both_modes():
    import unattended_drill as ud
    # the loop's run folder is ROOT/run-<time>: the file is in ROOT
    assert ud.records_path_for("/data/runs/run-20261102T000000Z") == \
        "/data/runs/execution_records.jsonl"
    # a single run IS the folder given: the file is beside it
    assert ud.records_path_for("/data/runs") == "/data/execution_records.jsonl"


def test_a_runs_log_says_where_its_records_go():
    src = open(os.path.join(os.path.dirname(HERE), "tools",
                            "unattended_drill.py")).read()
    assert "execution records: " in src and "records = records_path_for(run_dir)" in src
    assert "--once" not in src          # a flag the program does not have
