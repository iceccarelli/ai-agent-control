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
    assert rep["validation"]["n_valid_rows"] == len(rows) - 1          # the adopted row is counted as adopted and left OUT of the evidence


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


# ---------------- third review: legacy classification, deleted days, adopted rows ----------------

def anchored_history(tmp_path, d, name="capacity-20261104.jsonl"):
    return history_for(tmp_path, d, name=name)


def test_an_anchored_file_emptied_and_stripped_of_its_sidecars_is_a_conflict_not_a_legacy_file(tmp_path):
    rows = long_rows(40)
    d = lay_down(tmp_path, rows)
    hist = anchored_history(tmp_path, d)
    p = os.path.join(d, "capacity-20261104.jsonl")
    for ext in (".chain", ".head"):
        os.remove(p + ext)
    open(p, "wb").write(b"")
    rep = report(d, rows, history=hist)
    assert rep["integrity"]["summary"]["economic_evidence_eligible"] is False and rep["economics"] is None
    f = rep["integrity"]["files"][0]
    assert f["state"] == "CHAIN_BROKEN" and f["chain"]["anchor_status"] == "ANCHOR_CONFLICT"


def test_schema_less_rows_substituted_for_an_anchored_file_do_not_make_it_a_legacy_file(tmp_path):
    rows = long_rows(40)
    d = lay_down(tmp_path, rows)
    hist = anchored_history(tmp_path, d)
    p = os.path.join(d, "capacity-20261104.jsonl")
    for ext in (".chain", ".head"):
        os.remove(p + ext)
    open(p, "wb").write(b'{"at_ms":1,"curve":[],"environment":"mainnet"}\n' * 3)
    rep = report(d, rows, history=hist)
    assert rep["integrity"]["files"][0]["state"] == "CHAIN_BROKEN" and rep["integrity"]["summary"]["economic_evidence_eligible"] is False


def test_a_genuine_legacy_file_with_no_anchor_still_does_not_block(tmp_path):
    rows = long_rows(40)
    d = lay_down(tmp_path, rows)
    open(os.path.join(d, "capacity-20261001.jsonl"), "wb").write(b'{"at_ms":1,"curve":[],"environment":"mainnet"}\n')
    rep = report(d, rows)
    assert [f["state"] for f in rep["integrity"]["files"]] == ["LEGACY_PRE_CHAIN", "ELIGIBLE"]
    assert rep["integrity"]["summary"]["economic_evidence_eligible"] is True


def test_a_whole_anchored_day_that_has_been_removed_blocks_the_economics(tmp_path):
    """Selective erasure of a day, which only an anchor can catch: the surviving days are internally perfect."""
    rows = long_rows(40)
    d = lay_down(tmp_path, rows)
    keep = os.path.join(d, "capacity-20261104.jsonl")
    EC.bulk_write(os.path.join(d, "capacity-20261103.jsonl"), [dict(r, at_ms=r["at_ms"] - 86_400_000) for r in rows[:50]])
    gone = os.path.join(d, "capacity-20261103.jsonl")
    n = EC.verify(gone)["n_chained"]
    h = tmp_path / "monitor.jsonl"
    h.write_text("".join(json.dumps({"recorder_chain_file": os.path.basename(f), "recorder_chain_n": EC.verify(f)["n_chained"],
                                     "recorder_chain_head": EC._read_chain(f)[-1]["h"]}) + "\n" for f in (gone, keep)))
    for ext in ("", ".chain", ".head"):
        os.remove(gone + ext)
    rep = report(d, rows, history=str(h))
    s = rep["integrity"]["summary"]
    assert s["files_missing_but_anchored"] == ["capacity-20261103.jsonl"]
    assert s["economic_evidence_eligible"] is False and rep["economics"] is None
    assert any("removed, not pruned" in b for b in s["economic_evidence_blockers"])


def test_an_anchored_file_older_than_the_retention_may_have_been_pruned_and_is_not_a_blocker(tmp_path):
    rows = long_rows(40)
    d = lay_down(tmp_path, rows)
    h = tmp_path / "monitor.jsonl"
    h.write_text(json.dumps({"recorder_chain_file": "capacity-20240101.jsonl", "recorder_chain_n": 5, "recorder_chain_head": "ab" * 32}) + "\n")
    s = report(d, rows, history=str(h))["integrity"]["summary"]
    assert s["files_missing_but_anchored"] == [] and s["economic_evidence_eligible"] is True


def test_a_row_adopted_by_the_recorders_own_repair_is_left_out_of_every_statistic(tmp_path):
    """A forged-but-valid v2 line appended to the evidence file is chained by the recorder's repair, marked resealed, and still
    never admitted as evidence."""
    rows = long_rows(100)
    clean = lay_down(tmp_path / "a", rows[:-1])
    dirty = lay_down(tmp_path / "b", rows[:-1])
    p = os.path.join(dirty, "capacity-20261104.jsonl")
    open(p, "ab").write(EC.canonical(rows[-1]) + b"\n")
    assert EC.repair(p)["rows_resealed"] == 1
    v = EC.verify(p)
    assert v["ok"] and v["resealed_rows"] == 1 and v["resealed_indices"] == [len(rows) - 1]
    a, b = report(clean, rows[:-1]), report(dirty, rows[:-1])
    assert b["integrity"]["summary"]["resealed_rows"] == 1
    assert b["validation"]["n_valid_rows"] == a["validation"]["n_valid_rows"]               # the adopted row was not admitted
    assert a["independence"] == b["independence"] and a["funding_evaluation"]["joined"] == b["funding_evaluation"]["joined"]


def test_non_object_lines_in_the_monitor_history_do_not_crash_the_report(tmp_path):
    rows = long_rows(30)
    d = lay_down(tmp_path, rows)
    h = tmp_path / "monitor.jsonl"
    h.write_text('[1]\n"x"\n7\nnull\n{"recorder_chain_file":"capacity-20261104.jsonl","recorder_chain_n":3,"recorder_chain_head":"' + "ab" * 32 + '"}\n')
    rep = report(d, rows, history=str(h))                                                     # must not raise
    assert rep["integrity"]["summary"]["files_anchor_conflict"] == 1                          # the one real (wrong) anchor is still read


# ---- mutation survivors named by the review: these assertions fail if the guarded branches are weakened ----

def test_an_anchor_mismatch_is_serious_not_merely_noted(tmp_path):
    d = lay_down(tmp_path, long_rows(30))
    p = os.path.join(d, "capacity-20261104.jsonl")
    n = EC.verify(p)["n_chained"]
    v = EC.verify(p, anchors=[{"n": n - 3, "head": "cd" * 32}])
    assert v["internally_consistent"] is False and v["economic_evidence_eligible"] is False and v["verified_prefix_rows"] == 0
    rows, _ = EC.admitted_rows(p, anchors=[{"n": n - 3, "head": "cd" * 32}])
    assert rows == []


def test_a_head_that_records_more_rows_than_the_chain_holds_is_a_truncation(tmp_path):
    d = lay_down(tmp_path, long_rows(30))
    p = os.path.join(d, "capacity-20261104.jsonl")
    head = EC.head_of(p)
    open(p + ".head", "w").write(json.dumps({"n": head["n"] + 5, "head": head["head"]}))
    v = EC.verify(p)
    assert any(i["code"] == "TRUNCATED" for i in v["issues"]) and v["internally_consistent"] is False


# ---------------- the retention test uses the OWNER's stated retention and the recorder's own day comparison (delta review L1, third review M2) ----------------

def _gone_anchor(tmp_path, d, rows, day, retention=None, status=None):
    """An anchor for a day-file that is not on disk. `retention` is the owner's --capacity-retention-days (None = the default 365); `status` is
    written into the evidence directory's recorder_status.json to show that nothing there is believed."""
    if status is not None:
        with open(os.path.join(d, "recorder_status.json"), "w") as fh:
            json.dump(status, fh)
    h = tmp_path / "monitor.jsonl"
    h.write_text(json.dumps({"recorder_chain_file": f"capacity-{day}.jsonl", "recorder_chain_n": 5, "recorder_chain_head": "ab" * 32}) + "\n")
    kw = {} if retention is None else {"capacity_retention_days": retention}
    return PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, history=str(h), **kw)["integrity"]["summary"]


def _now(rows):
    return rows[-1]["at_ms"] + 60_000


@pytest.mark.parametrize("age_days,blocks", [(0, True), (2, True), (3, True), (4, False), (30, False)])
def test_a_shorter_stated_retention_is_honoured_and_the_boundary_day_is_not_prunable(tmp_path, age_days, blocks):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    day = PR._utc_day(_now(rows) - age_days * 86_400_000)
    s = _gone_anchor(tmp_path, d, rows, day, retention=3)
    assert bool(s["files_missing_but_anchored"]) is blocks, (age_days, s["files_missing_but_anchored"])
    assert s["economic_evidence_eligible"] is (not blocks)


def test_with_no_stated_retention_the_default_applies_and_a_recent_removal_blocks(tmp_path):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    s = _gone_anchor(tmp_path, d, rows, PR._utc_day(_now(rows) - 100 * 86_400_000))
    assert s["files_missing_but_anchored"] and not s["economic_evidence_eligible"]
    s = _gone_anchor(tmp_path, d, rows, PR._utc_day(_now(rows) - 400 * 86_400_000))
    assert s["files_missing_but_anchored"] == []


def test_a_recorder_that_keeps_everything_cannot_have_pruned_any_anchored_day(tmp_path):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    s = _gone_anchor(tmp_path, d, rows, "20240101", retention=0)
    assert s["files_missing_but_anchored"] == ["capacity-20240101.jsonl"] and not s["economic_evidence_eligible"]
    assert any("keeps every day" in b for b in s["economic_evidence_blockers"])


@pytest.mark.parametrize("status", [{"capacity_retention_days": 1}, {"store": {"capacity_retention_days": 1}}, {"capacity_retention_days": 0},
                                    {"capacity_retention_days": 10 ** 30}, {"store": {"capacity_retention_days": 10 ** 30}}])
def test_a_status_file_in_the_evidence_directory_cannot_shorten_the_retention_or_crash_the_report(tmp_path, status):
    """Whoever can delete a day of evidence can edit a status file next to it, so the report believes only the owner's argument."""
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    s = _gone_anchor(tmp_path, d, rows, PR._utc_day(_now(rows) - 100 * 86_400_000), status=status)
    assert s["files_missing_but_anchored"] and s["economic_evidence_eligible"] is False


@pytest.mark.parametrize("bad", [True, "3", -1, 3.5, None, 10 ** 30, 36_501, float("nan")])
def test_a_retention_argument_that_is_not_a_sane_integer_is_refused(tmp_path, bad):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    with pytest.raises(ValueError):
        PR.build(d, now_ms=_now(rows), capacity_retention_days=bad)


def test_the_command_line_takes_the_retention_from_the_owner(tmp_path, capsys):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    day = PR._utc_day(_now(rows) - 100 * 86_400_000)
    h = tmp_path / "monitor.jsonl"
    h.write_text(json.dumps({"recorder_chain_file": f"capacity-{day}.jsonl", "recorder_chain_n": 5, "recorder_chain_head": "ab" * 32}) + "\n")
    base = ["--dir", d, "--history", str(h), "--now-ms", str(_now(rows)), "--json"]
    assert PR.main(base) == 0
    blocked = json.loads(capsys.readouterr().out)["integrity"]["summary"]
    assert blocked["files_missing_but_anchored"] == [f"capacity-{day}.jsonl"]            # 365 days: a 100-day-old day cannot have been pruned
    assert PR.main(base + ["--capacity-retention-days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"]["summary"]["files_missing_but_anchored"] == []     # the owner says 30: it was pruned


# ---------------- hostile bytes in monitor.jsonl are skipped, not a crash (delta review L2) ----------------

def test_invalid_utf8_and_a_deeply_nested_line_in_the_history_do_not_stop_the_report_or_the_archive(tmp_path):
    import anchor_archive as AA
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    f = os.path.join(d, "capacity-20261104.jsonl")
    good = json.dumps({"recorder_chain_file": os.path.basename(f), "recorder_chain_n": EC.verify(f)["n_chained"],
                       "recorder_chain_head": EC._read_chain(f)[-1]["h"]}).encode()
    h = tmp_path / "monitor.jsonl"
    h.write_bytes(b"\xff\xfe not utf-8 \x80\n" + b"[" * 200_000 + b"]" * 200_000 + b"\n" + good + b"\n")
    s = report(d, rows, history=str(h))["integrity"]["summary"]
    assert s["files_anchored_current"] == 1                          # the one good line survived the junk before it
    anchors = AA.extract_anchors(str(h))
    assert len(anchors) == 1 and anchors[0]["file"] == os.path.basename(f)


@pytest.mark.parametrize("args", [["--capacity-retention-days", "-1"], ["--capacity-retention-days", "99999"], ["--archive-entries", "0"]])
def test_ROUND4_the_command_line_refuses_bad_numbers_before_reading_anything(tmp_path, capsys, args):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    with pytest.raises(SystemExit) as exc:
        PR.main(["--dir", d, "--now-ms", str(_now(rows))] + args)
    assert exc.value.code == 2 and "must be" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [True, False, 1.0, 0, -1, "1"])
def test_ROUND4_the_archive_entry_count_is_validated_once_and_a_bool_or_float_is_refused(tmp_path, bad):
    rows = long_rows(100)
    d = lay_down(tmp_path, rows)
    with pytest.raises(ValueError, match="archive_entries"):
        PR.build(d, now_ms=_now(rows), anchor_archive=str(tmp_path / "a.jsonl"), archive_head="ab" * 32, archive_entries=bad)
