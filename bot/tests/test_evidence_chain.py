import json
import os

import pytest

import evidence_chain as EC


def rows(n):
    return [{"id": i, "v": i * 1.5} for i in range(n)]


def write(tmp_path, n=6):
    p = str(tmp_path / "capacity-20261105.jsonl")
    for r in rows(n):
        EC.append(p, r)
    return p


def rewrite(path, fn):
    data = open(path, "rb").read()          # read FIRST: opening for write truncates
    open(path, "wb").write(fn(data))


def codes(v):
    return [i["code"] for i in v["issues"]]


def test_a_clean_file_verifies_and_the_head_advances(tmp_path):
    p = write(tmp_path)
    v = EC.verify(p)
    assert v["ok"] and v["n_rows"] == v["n_chained"] == 6 and v["head"]["n"] == 6
    n, h = EC.append(p, {"id": 6})
    assert (n, EC.verify(p)["head"]["head"]) == (7, h)


def test_mutation_is_detected(tmp_path):
    p = write(tmp_path)
    rewrite(p, lambda d: d.replace(b'"v":3.0', b'"v":9.9'))
    assert "MUTATED" in codes(EC.verify(p))


def test_a_removed_interior_row_is_detected(tmp_path):
    p = write(tmp_path)
    def drop(d):
        ls = d.split(b"\n")
        del ls[2]
        return b"\n".join(ls)
    rewrite(p, drop)
    assert "MISSING_INTERIOR" in codes(EC.verify(p))


def test_truncation_is_detected_against_the_sidecar_and_against_an_external_anchor(tmp_path):
    p = write(tmp_path)
    anchor = {"n": 6, "head": EC.verify(p)["head"]["head"]}
    rewrite(p, lambda d: b"\n".join(d.split(b"\n")[:3]) + b"\n")
    assert "TRUNCATED" in codes(EC.verify(p))
    # an attacker who also truncates the sidecar and head is still caught by the external anchor
    cp, hp = p + ".chain", p + ".head"
    kept = open(cp).readlines()[:3]
    open(cp, "w").write("".join(kept))
    h3 = json.loads(kept[-1])
    open(hp, "w").write(json.dumps({"n": 3, "head": h3["h"]}))
    v = EC.verify(p, anchors=[anchor])
    assert "TRUNCATED" in codes(v) and not v["tamper_evident_ok"]


def test_duplication_and_reordering_are_detected(tmp_path):
    p = write(tmp_path)
    def dup(d):
        ls = d.split(b"\n")
        return b"\n".join(ls[:2] + [ls[1]] + ls[2:])
    rewrite(p, dup)
    assert {"DUPLICATED"} & set(codes(EC.verify(p)))
    (tmp_path / "x").mkdir()
    p2 = write(tmp_path / "x")

    def swap(d):
        ls = d.split(b"\n")
        ls[1], ls[2] = ls[2], ls[1]
        return b"\n".join(ls)
    rewrite(p2, swap)
    assert "REORDERED" in codes(EC.verify(p2))


def test_a_torn_final_line_is_quarantined_and_visible_not_parsed(tmp_path):
    p = write(tmp_path)
    open(p, "ab").write(b'{"id": 99, "v"')
    v = EC.verify(p)
    assert v["quarantined_lines"] and v["quarantined_lines"][0]["bytes"] > 0
    assert v["n_rows"] == 6 and v["tamper_evident_ok"]
    lines, q = EC.read_lines(p)
    assert len(lines) == 6 and q


def test_a_row_without_its_chain_entry_is_an_unchained_tail_until_resealed_visibly(tmp_path):
    p = write(tmp_path)
    open(p, "ab").write(EC.canonical({"id": 6}) + b"\n")           # crash between the row and its chain line
    v = EC.verify(p)
    assert codes(v) == ["UNCHAINED_TAIL"] and v["unchained_tail"] == 1 and v["tamper_evident_ok"] and not v["ok"]
    assert EC.reseal_tail(p) == 1
    v2 = EC.verify(p)
    assert v2["ok"] and json.loads(open(p + ".chain").readlines()[-1])["resealed"] is True


def test_reseal_refuses_a_tampered_file(tmp_path):
    p = write(tmp_path)
    rewrite(p, lambda d: d.replace(b'"v":3.0', b'"v":9.9') + EC.canonical({"id": 6}) + b"\n")
    assert EC.reseal_tail(p) == 0


def test_a_missing_head_is_unverifiable_and_a_wrong_anchor_is_a_mismatch(tmp_path):
    p = write(tmp_path)
    os.remove(p + ".head")
    assert "HEAD_MISSING" in codes(EC.verify(p))
    (tmp_path / "y").mkdir()
    p2 = write(tmp_path / "y")
    bad = {"n": 4, "head": "ab" * 32}
    assert "ANCHOR_MISMATCH" in codes(EC.verify(p2, anchors=[bad]))


def test_editing_the_sidecar_to_hide_a_mutation_is_caught_by_its_own_chain(tmp_path):
    p = write(tmp_path)
    rewrite(p, lambda d: d.replace(b'"v":3.0', b'"v":9.9'))
    cp = p + ".chain"
    es = [json.loads(l) for l in open(cp)]
    es[2]["r"] = EC.row_hash(open(p, "rb").read().split(b"\n")[2])     # re-hash the row but not the chain
    open(cp, "w").write("".join(json.dumps(e, sort_keys=True) + "\n" for e in es))
    assert "SIDECAR_INCONSISTENT" in codes(EC.verify(p))


def test_bulk_write_produces_exactly_what_row_by_row_append_produces(tmp_path):
    a = str(tmp_path / "a.jsonl")
    b = str(tmp_path / "b.jsonl")
    for r in rows(9):
        EC.append(a, r)
    assert EC.bulk_write(b, rows(9)) == (9, EC.verify(a)["head"]["head"])
    assert open(a, "rb").read() == open(b, "rb").read() and open(a + ".chain").read() == open(b + ".chain").read()
    assert EC.verify(b)["ok"]
    with pytest.raises(FileExistsError):
        EC.bulk_write(b, rows(1))


# ---- adversarial-review findings: crash safety, visibility, and a sidecar that cannot crash the reader ------------

def test_appending_after_a_torn_tail_is_refused_not_welded_into_the_record(tmp_path):
    """Before: the new row's bytes merged with the torn fragment and the file was permanently MUTATED/TRUNCATED."""
    p = write(tmp_path, 4)
    with open(p, "ab") as fh:
        fh.write(b'{"id":99,"v')                       # power cut mid-row
    with pytest.raises(EC.ChainDesync):
        EC.append(p, {"id": 5})
    assert open(p, "rb").read().endswith(b'{"id":99,"v')      # nothing was added
    r = EC.repair(p)
    assert r["appendable"] and r["torn_bytes_set_aside"] == len(b'{"id":99,"v')
    assert open(p + ".torn", "rb").read().startswith(b'{"id":99,"v')      # kept, not destroyed
    EC.append(p, {"id": 5})
    v = EC.verify(p)
    assert v["ok"] and v["n_rows"] == v["n_chained"] == 5


def test_a_crash_between_the_row_and_its_chain_entry_is_repaired_visibly_and_blocks_blind_appends(tmp_path):
    p = write(tmp_path, 3)
    with open(p, "ab") as fh:
        fh.write(EC.canonical({"id": 3, "v": 4.5}) + b"\n")           # the row landed, the chain entry did not
    with pytest.raises(EC.ChainDesync):
        EC.append(p, {"id": 4})                                      # was: a permanent INSERTED
    r = EC.repair(p)
    assert r["rows_resealed"] == 1 and r["appendable"]
    EC.append(p, {"id": 4})
    v = EC.verify(p)
    assert v["ok"] and v["n_rows"] == 5
    assert v["resealed_rows"] == 1                                   # the repair is surfaced, not silent


def test_a_torn_sidecar_line_is_healed_and_a_corrupt_one_is_reported_not_raised(tmp_path):
    p = write(tmp_path, 3)
    with open(p + ".chain", "ab") as fh:
        fh.write(b'{"n":4,"r":"ab')                                  # torn chain line, benign
    assert EC.verify(p)["sidecar_torn_tail"] is True
    with pytest.raises(EC.ChainDesync):
        EC.append(p, {"id": 3})
    for k, junk in enumerate((b"[1,2,3]\n", b'"just a string"\n', b'{"n":"x","r":5,"h":[]}\n', b"not json at all\n")):
        q = str(tmp_path / f"capacity-2026111{k}.jsonl")
        for r in rows(3):
            EC.append(q, r)
        with open(q + ".chain", "ab") as fh:
            fh.write(junk)
        v = EC.verify(q)                                              # must not raise
        assert "SIDECAR_CORRUPT" in codes(v) and v["tamper_evident_ok"] is False


def test_appended_junk_that_is_resealed_stays_visible_in_verify(tmp_path):
    p = write(tmp_path, 3)
    with open(p, "ab") as fh:
        fh.write(b'{"injected":true}\n')
    assert EC.reseal_tail(p) == 1
    v = EC.verify(p)
    assert v["ok"] and v["resealed_rows"] == 1                      # ok on the chain, but flagged as adopted


def test_without_an_anchor_a_clean_verify_says_so(tmp_path):
    p = write(tmp_path, 3)
    assert EC.verify(p)["anchored"] is False
    assert EC.verify(p, anchors=[{"n": 3, "head": EC.verify(p)["head"]["head"]}])["anchored"] is True


# ---- second review: the first row of a UTC day, bounded adoption, and a reader that cannot be crashed ------------------

def fresh(tmp_path, name="capacity-20261106.jsonl"):
    return str(tmp_path / name)


def test_every_crash_state_of_the_FIRST_row_of_a_day_is_recoverable(tmp_path):
    """The review: with no prior rows, a torn first row / first row without a chain entry / chain line without a head were
    permanently stuck (HEAD_MISSING is not repairable), so append raised every five minutes until midnight."""
    row = EC.canonical({"id": 0})
    states = {
        "torn first row": lambda p: open(p, "wb").write(row[:7]),
        "first row, no chain entry": lambda p: open(p, "wb").write(row + b"\n"),
        "first row and chain line, no head": lambda p: (open(p, "wb").write(row + b"\n"),
                                                        open(p + ".chain", "w").write(json.dumps(
                                                            {"n": 1, "r": EC.row_hash(row), "h": EC.link(EC.GENESIS, EC.row_hash(row))}, sort_keys=True) + "\n")),
        "torn first chain line": lambda p: (open(p, "wb").write(row + b"\n"), open(p + ".chain", "wb").write(b'{"n":1,"r":"ab')),
        "empty file": lambda p: open(p, "wb").write(b""),
    }
    for k, (name, make) in enumerate(states.items()):
        p = fresh(tmp_path, f"capacity-2026110{k}.jsonl")
        make(p)
        assert EC.needs_repair(p) or name == "empty file", name
        r = EC.repair(p)
        assert r["appendable"], (name, r)
        EC.append(p, {"id": 1})
        v = EC.verify(p)
        assert v["tamper_evident_ok"] and v["n_rows"] == v["n_chained"], (name, v["issues"])


def test_more_unchained_rows_than_a_crash_can_explain_is_a_serious_failure_and_is_not_adopted(tmp_path):
    p = write(tmp_path, 3)
    with open(p, "ab") as fh:
        for i in range(60):
            fh.write(EC.canonical({"injected": i}) + b"\n")
    v = EC.verify(p)
    assert "UNCHAINED_TAIL_EXCESS" in codes(v) and v["tamper_evident_ok"] is False
    assert EC.reseal_tail(p) == 0 and EC.repair(p)["appendable"] is False       # refused: it is not a crash, it is injection
    assert EC.verify(p)["n_chained"] == 3


def test_adoption_over_a_files_life_is_capped(tmp_path):
    p = write(tmp_path, 2)
    for k in range(EC.MAX_RESEALED_PER_FILE):
        open(p, "ab").write(EC.canonical({"crash": k}) + b"\n")
        assert EC.reseal_tail(p) == 1
    open(p, "ab").write(EC.canonical({"one too many": True}) + b"\n")
    assert EC.reseal_tail(p) == 0 and "UNCHAINED_TAIL" in codes(EC.verify(p))
    # and a file that somehow holds more resealed entries than that is flagged by verify itself
    q = str(tmp_path / "capacity-20261109.jsonl")
    EC.bulk_write(q, rows(2))
    with open(q + ".chain", "w") as fh:
        prev = EC.GENESIS
        for i, ln in enumerate(EC.read_lines(q)[0], 1):
            r = EC.row_hash(ln)
            prev = EC.link(prev, r, resealed=True)
            fh.write(json.dumps({"n": i, "r": r, "h": prev, "resealed": True}, sort_keys=True) + "\n")
    EC.bulk_write(q + "x", [])               # (no-op helper call: keeps the file layout identical to production)
    assert EC.verify(q)["resealed_rows"] == 2


def test_a_newline_terminated_non_json_final_line_is_set_aside_not_welded_in(tmp_path):
    p = write(tmp_path, 3)
    with open(p, "ab") as fh:
        fh.write(b"garbage that ends in a newline\n")
    with pytest.raises(EC.ChainDesync):
        EC.append(p, {"id": 9})
    r = EC.repair(p)
    assert r["appendable"] and b"garbage" in open(p + ".torn", "rb").read()
    EC.append(p, {"id": 9})
    v = EC.verify(p)
    assert v["ok"] and v["n_rows"] == 4 and v["torn_aside_bytes"] > 0            # surfaced to the report


@pytest.mark.parametrize("head", [b'{"n":"3","head":"x"}', b'{"n":null,"head":"x"}', b'{"n":[1],"head":"x"}', b'{"n":3,"head":7}',
                                  b'{"n":true,"head":"x"}', b"[]", b"7"])
def test_a_head_file_of_the_wrong_shape_is_reported_not_raised(tmp_path, head):
    p = write(tmp_path, 3)
    open(p + ".head", "wb").write(head)
    v = EC.verify(p)                                                              # must not raise
    assert "HEAD_MISSING" in codes(v) and v["tamper_evident_ok"] is False
    assert EC.repair(p)["appendable"] in (True, False)                            # and repair must not raise either


# ---- the resealed mark is part of the hash (a flipped flag must not make a valid row disappear, nor an adopted row count) ----

def _rewrite_chain(path, fn):
    entries = [json.loads(ln) for ln in open(path + ".chain").read().splitlines()]
    fn(entries)
    with open(path + ".chain", "w") as fh:
        for e in entries:
            fh.write(json.dumps(e, sort_keys=True) + "\n")


def test_flipping_the_resealed_flag_on_interior_entries_is_caught_not_a_silent_row_removal(tmp_path):
    p = write(tmp_path, 12)
    anchor = [{"n": 12, "head": EC.verify(p)["head"]["head"]}]
    clean_rows, clean_v = EC.admitted_rows(p, anchor)
    assert len(clean_rows) == 12 and clean_v["economic_evidence_eligible"] and clean_v["anchor_status"] == EC.ANCHORED_CURRENT

    def flip(entries):
        for i in (3, 5, 7):
            entries[i]["resealed"] = True
    _rewrite_chain(p, flip)
    v = EC.verify(p, anchor)
    assert "SIDECAR_INCONSISTENT" in codes(v)
    assert v["economic_evidence_eligible"] is False and v["internally_consistent"] is False
    assert v["anchor_status"] != EC.ANCHORED_CURRENT
    rows_, _ = EC.admitted_rows(p, anchor)
    assert rows_ == []                                    # fail closed: nothing is admitted from a file whose sidecar does not recompute


def test_stripping_the_mark_from_an_adopted_entry_is_caught(tmp_path):
    p = write(tmp_path, 4)
    open(p, "ab").write(EC.canonical({"adopted": True}) + b"\n")
    assert EC.reseal_tail(p) == 1
    _, v = EC.admitted_rows(p)
    assert v["ok"] and v["resealed_rows"] == 1

    def strip(entries):
        entries[-1].pop("resealed")
    _rewrite_chain(p, strip)
    v2 = EC.verify(p)
    assert "SIDECAR_INCONSISTENT" in codes(v2) and v2["economic_evidence_eligible"] is False
    assert EC.admitted_rows(p)[0] == []


@pytest.mark.parametrize("value", [False, 1, 0, "true", "yes", None, [], {}])
def test_the_mark_is_exactly_true_or_absent(tmp_path, value):
    p = write(tmp_path, 4)

    def put(entries):
        entries[2]["resealed"] = value
    _rewrite_chain(p, put)
    v = EC.verify(p)
    assert "SIDECAR_INCONSISTENT" in codes(v) and v["economic_evidence_eligible"] is False


def test_a_legitimate_repair_still_verifies_counts_and_is_excluded_after_the_change(tmp_path):
    p = write(tmp_path, 5)
    for k in range(2):
        open(p, "ab").write(EC.canonical({"crash": k}) + b"\n")
        assert EC.reseal_tail(p) == 1
    rows_, v = EC.admitted_rows(p)
    assert v["ok"] and v["resealed_rows"] == 2 and v["resealed_indices"] == [5, 6]
    assert len(rows_) == 5 and all("crash" not in r for r in rows_)
    n, _h = EC.append(p, {"after": True})                # normal appends continue to chain normally after a reseal
    assert n == 8 and EC.verify(p)["ok"]


def test_an_entry_without_the_mark_hashes_exactly_as_before():
    a, b = "ab" * 32, "cd" * 32
    import hashlib
    assert EC.link(a, b) == hashlib.sha256(bytes.fromhex(a) + bytes.fromhex(b)).hexdigest()
    assert EC.link(a, b, resealed=True) != EC.link(a, b)
