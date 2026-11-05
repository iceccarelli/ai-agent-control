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
