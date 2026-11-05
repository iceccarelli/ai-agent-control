"""Phase 1 of the continuation mandate: an unchained row is not evidence, an old anchor is not a current one,
and the report says which of several different things is true instead of one green Boolean."""
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import evidence_chain as EC  # noqa: E402
import profitability_report as PR  # noqa: E402
from capacity_helpers import base_row, noisy, series  # noqa: E402

BASE = base_row()


def lay_down(tmp_path, rows, name="capacity-20261104.jsonl"):
    d = tmp_path / "market"
    d.mkdir(parents=True, exist_ok=True)
    EC.bulk_write(str(d / name), rows)
    return str(d)


def report(d, rows, **kw):
    return PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, **kw)


def tree_digest(d):
    h = hashlib.sha256()
    for name in sorted(os.listdir(d)):
        h.update(name.encode())
        h.update(open(os.path.join(d, name), "rb").read())
    return h.hexdigest()


def long_rows(n=100):
    return series(BASE, n, lambda T, t: 4.0, noisy(4.0, 0.5))


# ---------------- A. an unchained row cannot enter the evidence ----------------

def test_a_valid_row_appended_after_the_last_chain_entry_is_not_accepted_evidence(tmp_path):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows[:-1])
    p = os.path.join(d, "capacity-20261104.jsonl")
    before = EC.verify(p)
    assert before["fully_chained"] and before["economic_evidence_eligible"]
    with open(p, "ab") as fh:
        fh.write(EC.canonical(rows[-1]) + b"\n")             # a perfectly VALID v2 row, never chained
    v = EC.verify(p)
    assert v["unchained_tail"] == 1
    assert v["internally_consistent"] is True               # the prefix still verifies ...
    assert v["fully_chained"] is False                      # ... but the file is not completely chained
    assert v["economic_evidence_eligible"] is False         # ... and so it is not eligible evidence
    rep = report(d, rows)
    assert rep["validation"]["n_valid_rows"] == len(rows) - 1          # the unchained row was never admitted
    assert rep["economics"] is None
    assert any("unchained" in w for w in rep["economics_withheld_because"])
    assert rep["integrity"]["summary"]["unchained_rows"] == 1
    stat = {o["strategy"]: o["status"] for o in rep["opportunities"]}
    assert stat["carry_overlay_taker/v1"] == "INSUFFICIENT_EVIDENCE" and stat["candidate_carry_depth/v1"] == "INSUFFICIENT_EVIDENCE"


def test_the_unchained_row_changes_nothing_in_any_statistic(tmp_path):
    rows = long_rows(100)
    clean = lay_down(tmp_path / "a", rows[:-1])
    dirty = lay_down(tmp_path / "b", rows[:-1])
    with open(os.path.join(dirty, "capacity-20261104.jsonl"), "ab") as fh:
        fh.write(EC.canonical(rows[-1]) + b"\n")
    a, b = report(clean, rows[:-1]), report(dirty, rows[:-1])
    assert a["independence"] == b["independence"]
    assert a["funding_evaluation"]["joined"] == b["funding_evaluation"]["joined"]
    assert a["funding_evaluation"]["realised_bps"]["mean"] == b["funding_evaluation"]["realised_bps"]["mean"]


def test_the_report_never_repairs_its_input(tmp_path):
    rows = long_rows(30)
    d = lay_down(tmp_path, rows[:-1])
    p = os.path.join(d, "capacity-20261104.jsonl")
    with open(p, "ab") as fh:
        fh.write(EC.canonical(rows[-1]) + b"\n")
        fh.write(b'{"torn')
    before = tree_digest(d)
    report(d, rows)
    assert tree_digest(d) == before                          # not one byte, not one new file
    assert not os.path.exists(p + ".torn")


def test_explicit_recovery_is_separate_visible_and_the_only_way_back(tmp_path):
    rows = long_rows(30)
    d = lay_down(tmp_path, rows[:-1])
    p = os.path.join(d, "capacity-20261104.jsonl")
    open(p, "ab").write(EC.canonical(rows[-1]) + b"\n")
    assert report(d, rows)["integrity"]["summary"]["unchained_rows"] == 1
    assert EC.repair(p)["rows_resealed"] == 1                # the explicit act
    rep = report(d, rows)
    s = rep["integrity"]["summary"]
    assert s["unchained_rows"] == 0 and s["resealed_rows"] == 1      # adopted, and SAID to be adopted
    assert rep["validation"]["n_valid_rows"] == len(rows)


def test_a_mutated_file_contributes_no_rows_at_all(tmp_path):
    rows = long_rows(30)
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    open(p, "wb").write(open(p, "rb").read().replace(b'"mode":"overlay"', b'"mode":"overlaz"', 1))
    rep = report(d, rows)
    assert rep["validation"]["n_valid_rows"] == 0 and rep["integrity"]["summary"]["files_internally_verified"] == 0
    assert rep["economics"] is None


# ---------------- B. anchors: current, lagging, stale, absent, conflicting ----------------

def anchors_for(p, upto):
    chain = EC._read_chain(p)
    return [{"n": upto, "head": chain[upto - 1]["h"]}]


def test_an_anchor_exactly_at_the_current_head_is_the_strongest_status(tmp_path):
    d = lay_down(tmp_path, long_rows(100))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    v = EC.verify(p, anchors=anchors_for(p, n))
    assert v["anchor_status"] == "ANCHORED_CURRENT" and v["anchor_lag_rows"] == 0
    assert v["chain_rows"] == n and v["latest_anchor_rows"] == n


def test_an_anchor_ten_rows_behind_is_lagging_and_shows_its_lag(tmp_path):
    d = lay_down(tmp_path, long_rows(100))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    v = EC.verify(p, anchors=anchors_for(p, n - 10))
    assert v["anchor_status"] == "ANCHORED_LAGGING" and v["anchor_lag_rows"] == 10
    assert v["internally_consistent"] and v["economic_evidence_eligible"]


def test_an_anchor_far_behind_is_stale(tmp_path):
    d = lay_down(tmp_path, long_rows(100))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    v = EC.verify(p, anchors=anchors_for(p, n - (EC.MAX_ANCHOR_LAG_ROWS + 1)))
    assert v["anchor_status"] == "STALE_ANCHOR" and v["anchor_lag_rows"] == EC.MAX_ANCHOR_LAG_ROWS + 1


def test_no_anchor_is_unanchored_and_is_not_called_anchored(tmp_path):
    d = lay_down(tmp_path, long_rows(30))
    v = EC.verify(os.path.join(d, "capacity-20261104.jsonl"))
    assert v["anchor_status"] == "UNANCHORED" and v["latest_anchor_rows"] is None and v["anchor_lag_rows"] is None


def test_a_file_truncated_after_an_old_anchor_is_a_conflict_not_a_pass(tmp_path):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    anchors = anchors_for(p, n - 5)                          # an anchor taken a little before the end ...
    os.remove(p + ".chain"); os.remove(p + ".head"); os.remove(p)
    EC.bulk_write(p, rows[: n // 2])                         # ... then the file is consistently cut well short of it
    v = EC.verify(p, anchors=anchors)
    assert v["anchor_status"] == "ANCHOR_CONFLICT" and v["internally_consistent"] is False
    assert any(i["code"] == "TRUNCATED" for i in v["issues"])


def test_an_anchor_newer_than_the_surviving_chain_is_a_conflict(tmp_path):
    d = lay_down(tmp_path, long_rows(30))
    p = os.path.join(d, "capacity-20261104.jsonl")
    v = EC.verify(p, anchors=[{"n": 10_000_000, "head": "ab" * 32}])
    assert v["anchor_status"] == "ANCHOR_CONFLICT" and any(i["code"] == "TRUNCATED" for i in v["issues"])


def test_an_anchor_at_the_right_row_with_the_wrong_head_is_a_conflict(tmp_path):
    d = lay_down(tmp_path, long_rows(30))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    v = EC.verify(p, anchors=[{"n": n, "head": "cd" * 32}])
    assert v["anchor_status"] == "ANCHOR_CONFLICT" and any(i["code"] == "ANCHOR_MISMATCH" for i in v["issues"])


def test_only_the_LATEST_anchor_decides_the_status_but_every_anchor_must_still_match(tmp_path):
    d = lay_down(tmp_path, long_rows(100))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    a = anchors_for(p, n) + anchors_for(p, n - 30) + [{"n": n - 50, "head": "ee" * 32}]       # an old anchor that is wrong
    v = EC.verify(p, anchors=a)
    assert v["anchor_status"] == "ANCHOR_CONFLICT"
    good = anchors_for(p, n) + anchors_for(p, n - 30)
    assert EC.verify(p, anchors=good)["anchor_status"] == "ANCHORED_CURRENT"


# ---------------- D. the report separates the states and never offers one green Boolean ----------------

def history_for(tmp_path, d, upto=None, name="capacity-20261104.jsonl"):
    p = os.path.join(d, name)
    n = EC.verify(p)["n_chained"]
    k = n if upto is None else upto
    h = tmp_path / "monitor.jsonl"
    h.write_text(json.dumps({"recorder_chain_file": name, "recorder_chain_n": k,
                             "recorder_chain_head": EC._read_chain(p)[k - 1]["h"]}) + "\n")
    return str(h)


def test_the_integrity_summary_separates_every_state(tmp_path):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    p = os.path.join(d, "capacity-20261103.jsonl")
    EC.bulk_write(p, [{"at_ms": 1, "curve": [], "environment": "mainnet"}, {"at_ms": 2, "curve": [], "environment": "mainnet"}])   # legacy-only
    rep = report(d, rows, history=history_for(tmp_path, d, upto=EC.verify(os.path.join(d, "capacity-20261104.jsonl"))["n_chained"] - 3))
    s = rep["integrity"]["summary"]
    for k in ("validated_rows", "legacy_quarantined_rows", "unchained_rows", "resealed_rows", "files_total",
              "files_internally_verified", "files_fully_chained", "files_anchored_current", "files_anchored_lagging",
              "files_stale_anchor", "files_unanchored", "files_anchor_conflict", "anchor_location", "economic_evidence_eligible"):
        assert k in s, k
    assert s["legacy_quarantined_rows"] == 2 and s["validated_rows"] == len(rows)
    assert s["files_total"] == 2 and s["files_anchored_lagging"] == 1 and s["files_unanchored"] == 1
    assert "chain_ok_for_v2_files" not in rep["integrity"]            # the old single Boolean is gone


def test_an_anchor_in_the_monitor_history_is_never_called_off_machine(tmp_path):
    rows = long_rows(30)
    d = lay_down(tmp_path, rows)
    s = report(d, rows, history=history_for(tmp_path, d))["integrity"]["summary"]
    assert s["anchor_location"] == "SAME_VOLUME_COPY_NOT_VERIFIED_OFF_MACHINE"
    text = PR.render(report(d, rows, history=history_for(tmp_path, d)))
    assert "ANCHORED_CURRENT" in text and "same volume" in text.lower() and "off-machine" not in text.replace("not verified off-machine", "")


def test_economics_need_the_chain_complete_not_the_anchor_current(tmp_path):
    """An absent or lagging anchor lowers the integrity GRADE (printed), it does not by itself forbid the calculation;
    an unchained row does."""
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    rep = report(d, rows)
    assert rep["integrity"]["summary"]["files_unanchored"] == 1
    assert rep["integrity"]["summary"]["economic_evidence_eligible"] is True
    assert rep["economics"] is not None and "UNANCHORED" in PR.render(rep)
