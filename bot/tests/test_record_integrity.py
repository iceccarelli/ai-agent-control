"""What the records file does and does not guarantee.

Threat model, stated plainly. The file is plain append-only JSONL written by an
unprivileged process; anyone with access to the volume (the operator, or a
compromised process) can edit it. So the defensible properties are:

  * accident / crash safety: a torn tail cannot swallow the next record, a
    line of garbage cannot stop the report, a repeated line is counted once,
    concurrent appenders cannot interleave a line, a newly created file's
    directory entry is synced;
  * consistency checks that make an UNSKILLED edit fail (a line time that
    disagrees with its record, a record that does not reproduce its own
    verdict).

It does NOT, and cannot, prove authenticity against an adversary who can
rewrite the file consistently: a hash chain written by the same process with no
key or anchor held elsewhere can be recomputed by that adversary, and cannot
show that a tail was deleted. The report says so rather than implying it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import drill                                                  # noqa: E402
import execution_cost_estimate as est                         # noqa: E402
import execution_samples_report as esr                        # noqa: E402
import ledger as L                                            # noqa: E402
from execution_fixtures import valid_record                   # noqa: E402

T0 = 1_700_000_000_000


class E:
    def __init__(self, ms, rec, i):
        self.kind, self.ms, self.ref = "perp_open", ms, f"o{i}"
        self.fee_raw = {"execution": rec}


def extract_with(mutate, n=25):
    now = int(time.time() * 1000)
    es = []
    for i in range(n):
        line = now - 60_000 - 2000 * i
        rec = valid_record("Sell", "linear", ms=line - 100 * 86_400_000,
                           link=f"o{i}")                    # 100 days old
        mutate(rec)
        es.append(E(line, rec, i))
    return est.extract(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                       side="Sell")


# -- the line-time consistency check cannot be bypassed by deleting a field --------

@pytest.mark.parametrize("field", ["fill_ms", "submit_ms", "decision_ms",
                                   "observed_ms"])
def test_deleting_one_time_field_does_not_pass_an_old_record_as_fresh(field):
    obs, rej = extract_with(lambda r: r.pop(field, None))
    assert obs == []                                      # not 25 fresh samples


def test_a_record_with_no_time_anchor_at_all_is_not_a_sample():
    def strip(r):
        for k in ("fill_ms", "submit_ms", "decision_ms", "observed_ms"):
            r.pop(k, None)
    obs, rej = extract_with(strip)
    assert obs == [] and sum(rej.values()) >= 25


def test_control_a_consistent_record_is_still_a_sample():
    now = int(time.time() * 1000)
    es = [E(now - 60_000 - 2000 * i,
            valid_record("Sell", "linear", ms=now - 60_000 - 2000 * i,
                         link=f"o{i}"), i) for i in range(25)]
    obs, _ = est.extract(es, as_of_ms=now, symbol="BTCUSDT", product="linear",
                         side="Sell")
    assert len(obs) == 25


# -- torn tail ---------------------------------------------------------------------

def good_line(i=0, **kw):
    rec = valid_record("Sell", "linear", ms=T0 + 10_000 * i, link=f"o{i}", **kw)
    return drill._fill_line("testnet", "perp_open", T0 + 10_000 * i,
                            f"o{i}", {"execution": rec})


def test_a_torn_tail_cannot_swallow_the_next_record(tmp_path):
    path = tmp_path / "r.jsonl"
    full = good_line(0)
    path.write_bytes(full.encode()[: len(full) // 2])         # cut off mid-line
    drill.durable_append(str(path), (good_line(1) + "\n").encode())
    fills, skipped = esr.load_records([str(path)], "testnet")
    assert len(fills) == 1 and skipped == 1                  # new one survives


def test_a_clean_file_gets_no_extra_blank_line(tmp_path):
    path = tmp_path / "r.jsonl"
    drill.durable_append(str(path), (good_line(0) + "\n").encode())
    drill.durable_append(str(path), (good_line(1) + "\n").encode())
    assert len(path.read_text().splitlines()) == 2
    assert "\n\n" not in path.read_text()


# -- corrupt content ---------------------------------------------------------------

def test_invalid_utf8_and_nul_bytes_skip_the_line_not_the_report(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_bytes(b"\xff\xfe\x00 not json at all\n"
                     + (good_line(0) + "\n").encode()
                     + b'{"schema": "execution_records/1", "x": "\x00\x00"}\n')
    fills, skipped = esr.load_records([str(path)], "testnet")
    assert len(fills) == 1 and skipped >= 2


# -- duplicates --------------------------------------------------------------------

def test_an_exact_duplicate_line_is_counted_once_and_reported(tmp_path):
    path = tmp_path / "r.jsonl"
    rec = valid_record("Sell", "linear", q=0.004, req=0.010, ms=T0, link="o0")
    line = drill._fill_line("testnet", "perp_open", T0, "o0",
                            {"execution": rec})
    path.write_text((line + "\n") * 3)                    # a replayed append
    out = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=T0 + 100_000, records=[str(path)],
                    environment="testnet")
    assert out["partial_fills"]["linear/Sell/market"]["orders"] == 1
    assert out["records_duplicate_lines"] == 2


def test_two_processes_with_separate_key_sets_are_a_visible_duplicate(tmp_path):
    """A restart (or a second process) has a fresh in-memory key set, so the
    same event can be appended twice. The reader, not the writer, is what makes
    that harmless - and it says it saw it."""
    path = str(tmp_path / "r.jsonl")
    rec = valid_record("Sell", "linear", ms=T0, link="o0")
    event = {"type": "fill", "kind": "perp_open", "ms": T0, "ref": "o0",
             "fee_raw": {"execution": rec}}
    drill.RecordWriter(path, "testnet", set())(event)
    drill.RecordWriter(path, "testnet", set())(event)
    assert len(open(path).read().splitlines()) == 2
    fills, _ = esr.load_records([path], "testnet")
    assert len(fills) == 1


# -- concurrent appenders ----------------------------------------------------------

def test_concurrent_processes_never_interleave_a_line(tmp_path):
    path = str(tmp_path / "r.jsonl")
    script = textwrap.dedent(f"""
        import sys
        sys.path[:0] = {[BOT, os.path.join(BOT, "tools")]!r}
        import drill
        pid = sys.argv[1]
        for i in range(25):
            line = '{{"p": "%s", "i": %d, "pad": "%s"}}\\n' % (pid, i, "x" * 20000)
            drill.durable_append({path!r}, line.encode())
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(n)])
             for n in range(4)]
    assert all(p.wait(60) == 0 for p in procs)
    text = open(path).read()
    assert "\n\n" not in text                  # serialized: no stray newline
    rows = [json.loads(x) for x in text.splitlines()]
    assert len(rows) == 100 and all(len(r["pad"]) == 20000 for r in rows)


# -- directory entries -------------------------------------------------------------

def test_a_created_file_and_directories_are_synced_once(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(drill, "_fsync_dir", lambda d: synced.append(d))
    path = str(tmp_path / "a" / "b" / "r.jsonl")
    drill.durable_append(path, b"{}\n")
    assert str(tmp_path / "a" / "b") in synced            # the file's directory
    assert str(tmp_path) in synced                         # where a/ was made
    synced.clear()
    drill.durable_append(path, b"{}\n")                    # existing file
    assert synced == []


def test_a_directory_sync_failure_is_reported_not_raised_and_not_rewritten(
        tmp_path, monkeypatch):
    def boom(d):
        raise OSError(5, "dir sync failed")
    monkeypatch.setattr(drill, "_fsync_dir", boom)
    path = str(tmp_path / "r.jsonl")
    res = drill.durable_append(path, b"{}\n")
    assert res["synced"] is False and "dir sync failed" in res["sync_error"]
    assert open(path).read() == "{}\n"


# -- what the report claims about integrity ----------------------------------------

def test_the_report_states_the_limits_of_what_it_can_prove(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(good_line(0) + "\n")
    out = esr.build("", mode="overlay", notional_usd=600.0, perp="BTCUSDT",
                    spot="BTCUSDT", now_ms=T0 + 100_000, records=[str(path)],
                    environment="testnet")
    integ = out["integrity"]
    assert integ["tamper_evident"] is False
    assert any("deleted" in x for x in integ["cannot_detect"])
    assert any("forg" in x for x in integ["cannot_detect"])
    assert integ["detects"]
