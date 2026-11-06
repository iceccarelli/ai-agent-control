"""The anchor archive: an append-only, hash-chained copy of chain anchors that lives off the recorder machine."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import anchor_archive as AA  # noqa: E402
import evidence_chain as EC  # noqa: E402
import profitability_report as PR  # noqa: E402
from capacity_helpers import base_row, noisy, series  # noqa: E402

H = lambda c: c * 64                                                               # noqa: E731


def history(tmp_path, lines, name="monitor_tail.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return str(p)


def line(f, n, h, at="t"):
    return {"at": at, "outcome": "ENTRY_NOT_ELIGIBLE", "recorder_chain_file": f, "recorder_chain_n": n, "recorder_chain_head": h}


def test_append_extracts_anchors_in_order_and_is_idempotent(tmp_path):
    h = history(tmp_path, [line("capacity-1.jsonl", 10, H("a"), "t1"), {"at": "x", "no_anchor": True}, line("capacity-1.jsonl", 20, H("b"), "t2"),
                           line("capacity-2.jsonl", 5, H("c"), "t3")])
    arc = str(tmp_path / "anchors.jsonl")
    r1 = AA.append(h, arc)
    assert r1["ok"] and r1["added"] == 3 and r1["entries"] == 3
    r2 = AA.append(h, arc)                                                          # nothing new
    assert r2["ok"] and r2["added"] == 0 and r2["archive_head"] == r1["archive_head"]
    assert AA.latest_per_file(AA.read_archive(arc)[0]) == {"capacity-1.jsonl": {"n": 20, "head": H("b"), "observed": "t2"},
                                                         "capacity-2.jsonl": {"n": 5, "head": H("c"), "observed": "t3"}}


def test_malformed_and_torn_history_lines_are_skipped_not_fatal(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text(json.dumps(line("capacity-1.jsonl", 3, H("a"))) + "\nnot json\n[1,2]\n" + json.dumps(line("capacity-1.jsonl", "x", H("a"))) + "\n"
                 + json.dumps(line("capacity-1.jsonl", 4, "short")) + '\n{"torn')
    assert [a["n"] for a in AA.extract_anchors(str(p))] == [3]


def test_a_file_that_went_backwards_is_refused_because_the_evidence_was_truncated(tmp_path):
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-1.jsonl", 100, H("a"))]), arc)
    before = open(arc, "rb").read()
    r = AA.append(history(tmp_path, [line("capacity-1.jsonl", 90, H("b"))], "h2.jsonl"), arc)
    assert r["ok"] is False and any("ANCHOR_REGRESSION" in i and "truncated" in i for i in r["issues"])
    assert open(arc, "rb").read() == before                                          # nothing was written


def test_the_same_row_count_with_a_different_head_is_refused_because_the_chain_was_rewritten(tmp_path):
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-1.jsonl", 100, H("a"))]), arc)
    r = AA.append(history(tmp_path, [line("capacity-1.jsonl", 100, H("f"))], "h2.jsonl"), arc)
    assert r["ok"] is False and any("ANCHOR_CONFLICT" in i for i in r["issues"])
    # one bad line refuses the WHOLE batch: a good anchor beside it is not smuggled in
    r2 = AA.append(history(tmp_path, [line("capacity-1.jsonl", 120, H("c")), line("capacity-1.jsonl", 100, H("f"))], "h3.jsonl"), arc)
    assert r2["ok"] is False and r2["added"] == 0 and AA.read_archive(arc)[0][-1]["n"] == 100


def test_editing_removing_reordering_or_forging_an_archived_line_breaks_the_chain(tmp_path):
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-1.jsonl", n, H("a")) for n in (10, 20, 30, 40)]), arc)
    original = open(arc, "rb").read().split(b"\n")[:-1]
    assert AA.verify(arc)["internally_intact"] and len(original) == 4

    def rewrite(lines):
        open(arc, "wb").write(b"\n".join(lines) + b"\n")

    rewrite([original[0], original[1].replace(b'"n":20', b'"n":21'), original[2], original[3]])
    assert not AA.verify(arc)["internally_intact"] and "LINE_2_BREAKS_THE_CHAIN" in AA.verify(arc)["problems"][0]
    rewrite([original[0], original[2], original[3]])                                      # a line removed
    assert not AA.verify(arc)["internally_intact"]
    rewrite([original[0], original[2], original[1], original[3]])                         # reordered
    assert not AA.verify(arc)["internally_intact"]
    rewrite(original[:3])                                                                 # TRUNCATED archive is internally intact ...
    v = AA.verify(arc)
    assert v["internally_intact"] and v["entries"] == 3


def test_the_owner_held_head_catches_a_truncated_or_replaced_archive_and_accepts_growth(tmp_path):
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-1.jsonl", n, H("a")) for n in (10, 20, 30)]), arc)
    held = AA.verify(arc)["archive_head"]
    held_n = AA.verify(arc)["entries"]
    assert AA.verify(arc, owner_held_head=held)["owner_held_head_matches"] is True
    assert AA.verify(arc, owner_held_head=held)["location"] == "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"
    AA.append(history(tmp_path, [line("capacity-1.jsonl", 40, H("b"))], "h2.jsonl"), arc)
    assert AA.verify(arc, owner_held_head=held)["owner_held_head_matches"] is False                # exact match required without a count
    assert AA.verify(arc, owner_held_head=held, owner_held_entries=held_n)["owner_held_head_matches"] is True       # it only grew
    open(arc, "wb").write(b"\n".join(open(arc, "rb").read().split(b"\n")[:2]) + b"\n")                # truncate to 2 entries
    assert AA.verify(arc, owner_held_head=held, owner_held_entries=held_n)["owner_held_head_matches"] is False
    assert AA.verify(arc, owner_held_head=held)["location"] != "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"


def test_a_torn_final_line_in_the_archive_is_a_named_problem(tmp_path):
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-1.jsonl", 10, H("a"))]), arc)
    open(arc, "ab").write(b'{"schema":"anchor_archive/1","seq":2')
    v = AA.verify(arc)
    assert not v["internally_intact"] and any("TORN_FINAL_LINE" in p for p in v["problems"])
    assert AA.append(history(tmp_path, [line("capacity-1.jsonl", 20, H("b"))], "h2.jsonl"), arc)["ok"] is False


def test_the_cli_exit_codes(tmp_path, capsys):
    arc = str(tmp_path / "a.jsonl")
    h = history(tmp_path, [line("capacity-1.jsonl", 10, H("a"))])
    assert AA.main(["append", "--history", h, "--archive", arc]) == 0
    assert AA.main(["verify", "--archive", arc]) == 0
    assert AA.main(["verify", "--archive", arc, "--archive-head", H("9")]) == 2
    out = capsys.readouterr().out
    assert "owner_held_head_matches" in out
    assert AA.main(["latest", "--archive", arc]) == 0 and "store_this_head_somewhere" in capsys.readouterr().out
    assert AA.main(["append", "--history", history(tmp_path, [line("capacity-1.jsonl", 5, H("z"))], "h2.jsonl"), "--archive", arc]) == 2


# ---- the report uses the archive ----

def lay(tmp_path, rows):
    d = tmp_path / "market"
    d.mkdir(parents=True, exist_ok=True)
    EC.bulk_write(str(d / "capacity-20261104.jsonl"), rows)
    return str(d)


def test_the_report_upgrades_the_anchor_location_only_when_the_owner_held_head_matches(tmp_path):
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    chain = EC._read_chain(p)
    hist = history(tmp_path, [line("capacity-20261104.jsonl", len(chain) - 4, chain[len(chain) - 5]["h"])])
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(hist, arc)
    head = AA.verify(arc)["archive_head"]
    now = rows[-1]["at_ms"] + 60_000
    plain = PR.build(d, now_ms=now, anchor_archive=arc)["integrity"]["summary"]
    assert plain["anchor_location"] == "ARCHIVE_PRESENT_HEAD_NOT_CONFIRMED_AGAINST_AN_OWNER_HELD_VALUE" and plain["files_anchored_lagging"] == 1
    held = PR.build(d, now_ms=now, anchor_archive=arc, archive_head=head)["integrity"]["summary"]
    assert held["anchor_location"] == "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE" and held["economic_evidence_eligible"]
    wrong = PR.build(d, now_ms=now, anchor_archive=arc, archive_head=H("e"))
    assert wrong["integrity"]["summary"]["economic_evidence_eligible"] is False
    assert any("does NOT match the head the owner stored" in b for b in wrong["integrity"]["summary"]["economic_evidence_blockers"])


def test_an_archive_anchor_catches_a_consistently_truncated_evidence_file(tmp_path):
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    chain = EC._read_chain(p)
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-20261104.jsonl", len(chain), chain[-1]["h"])]), arc)
    for ext in ("", ".chain", ".head"):
        os.remove(p + ext)
    EC.bulk_write(p, rows[: len(rows) - 50])                                      # cut, and every sidecar rewritten to match
    rep = PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, anchor_archive=arc)
    assert rep["integrity"]["summary"]["files_anchor_conflict"] == 1 and rep["economics"] is None


def test_a_file_only_the_recorders_own_volume_anchors_cannot_borrow_the_archives_location(tmp_path):
    """The archive knows file A only; monitor.jsonl anchors A and the newest day B. Both verify, the location is the owner-matched
    archive, and the grade must still not be ANCHORED: B has no external anchor."""
    import opportunity_engine as OE
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    a = os.path.join(d, "capacity-20261104.jsonl")
    b = os.path.join(d, "capacity-20261105.jsonl")
    EC.bulk_write(b, rows)
    ca, cb = EC._read_chain(a), EC._read_chain(b)
    both = history(tmp_path, [line("capacity-20261104.jsonl", len(ca), ca[-1]["h"]), line("capacity-20261105.jsonl", len(cb), cb[-1]["h"])], "both.jsonl")
    only_a = history(tmp_path, [line("capacity-20261104.jsonl", len(ca), ca[-1]["h"])], "only_a.jsonl")
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(only_a, arc)
    head = AA.verify(arc)["archive_head"]
    s = PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, history=both, anchor_archive=arc, archive_head=head)["integrity"]["summary"]
    assert s["anchor_location"] == "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"
    assert s["files_anchored_current"] + s["files_anchored_lagging"] == 2           # the merged anchors verify both files
    assert s["files_anchored_by_archive"] == 1                                      # ... but the archive anchors one
    assert OE.integrity_grade({"integrity": s}) == "ANCHORED_SAME_VOLUME_ONLY"
    # once the archive also holds B, the grade is ANCHORED
    AA.append(both, arc)
    head2 = AA.verify(arc)["archive_head"]
    s2 = PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, history=both, anchor_archive=arc, archive_head=head2)["integrity"]["summary"]
    assert s2["files_anchored_by_archive"] == 2 and OE.integrity_grade({"integrity": s2}) == "ANCHORED"


def test_without_an_archive_no_file_is_anchored_by_one(tmp_path):
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    chain = EC._read_chain(os.path.join(d, "capacity-20261104.jsonl"))
    hist = history(tmp_path, [line("capacity-20261104.jsonl", len(chain), chain[-1]["h"])])
    s = PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, history=hist)["integrity"]["summary"]
    assert s["files_anchored_by_archive"] == 0 and s["anchor_location"] == "SAME_VOLUME_COPY_NOT_VERIFIED_OFF_MACHINE"


def test_ROUND3_only_the_archive_entries_the_owner_pinned_count_as_external_anchors(tmp_path):
    """The archive may have grown since the owner stored its head. Entries appended after the stored prefix are not pinned by the owner, so
    they must not make a file 'anchored by the archive' (the owner-pinned prefix alone says STALE)."""
    import opportunity_engine as OE
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    chain = EC._read_chain(p)
    assert len(chain) > 60                                        # so that an anchor at row 1 is far behind (stale)
    arc = str(tmp_path / "anchors.jsonl")
    AA.append(history(tmp_path, [line("capacity-20261104.jsonl", 1, chain[0]["h"])], "first.jsonl"), arc)
    held = AA.verify(arc)["archive_head"]
    AA.append(history(tmp_path, [line("capacity-20261104.jsonl", len(chain), chain[-1]["h"])], "later.jsonl"), arc)       # appended AFTER the owner stored `held`
    now = rows[-1]["at_ms"] + 60_000
    s = PR.build(d, now_ms=now, anchor_archive=arc, archive_head=held, archive_entries=1)["integrity"]["summary"]
    assert s["anchor_location"] == "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"      # the prefix the owner stored is intact
    assert s["files_anchored_current"] + s["files_anchored_lagging"] == 1                  # the merged anchors do cover the file
    assert s["files_anchored_by_archive"] == 0                                              # ... but not through anything the owner pinned
    assert OE.integrity_grade({"integrity": s}) == "ANCHORED_SAME_VOLUME_ONLY"
    head_now = AA.verify(arc)["archive_head"]                                               # once the owner stores the NEW head, the later entry is pinned
    s2 = PR.build(d, now_ms=now, anchor_archive=arc, archive_head=head_now)["integrity"]["summary"]
    assert s2["files_anchored_by_archive"] == 1 and OE.integrity_grade({"integrity": s2}) == "ANCHORED"


def test_ROUND3_the_rendered_report_says_how_many_files_the_archive_itself_anchors(tmp_path):
    rows = series(base_row(), 6, lambda T, t: 0.6, noisy(0.6, 0.1))
    d = lay(tmp_path, rows)
    p = os.path.join(d, "capacity-20261104.jsonl")
    chain = EC._read_chain(p)
    hist = history(tmp_path, [line("capacity-20261104.jsonl", len(chain), chain[-1]["h"])])
    text = PR.render(PR.build(d, now_ms=rows[-1]["at_ms"] + 60_000, history=hist))
    assert "anchored by the ARCHIVE itself: 0 of 1 chained files" in text
