"""dataset_inventory: read-only, never opens a database or a credential-like file, coverage from names (not mtimes), no writes inside the inspected root."""
import builtins
import hashlib
import json
import os
import sys

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import dataset_inventory as DI  # noqa: E402


def tree(tmp_path):
    r = tmp_path / "datasets"
    (r / "md-1" / "data").mkdir(parents=True)
    for d in ("20261101", "20261102", "20261105"):                          # 20261103 and 20261104 are missing
        (r / "md-1" / "data" / f"BTCUSDT-{d}.jsonl").write_text("x" * 10)
    (r / "md-1" / "state.db").write_bytes(b"SQLite format 3\0" + b"z" * 100)
    (r / "md-1" / "state.db-wal").write_bytes(b"w" * 5)
    (r / "md-1" / ".env").write_text("SECRET=1")
    (r / "md-1" / "api.key").write_text("k")
    (r / "loose.txt").write_text("hi")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "BIG-20200101.jsonl").write_text("o" * 1000)
    os.symlink(str(outside), str(r / "link-out"))
    return r


def test_counts_sizes_named_coverage_and_gaps_without_trusting_mtimes(tmp_path):
    r = tree(tmp_path)
    rep = DI.inventory(str(r))
    d = rep["datasets"]["md-1"]
    assert rep["exists"] and d["named_coverage"]["first"] == "2026-11-01" and d["named_coverage"]["last"] == "2026-11-05"
    assert d["named_coverage"]["distinct_days"] == 3 and d["named_coverage"]["missing_days_between"] == 2 and d["named_coverage"]["missing_days_sample"] == ["2026-11-03", "2026-11-04"]
    assert d["named_coverage"]["source"] == "day tokens in file names" and d["extensions"][".jsonl"] == 3
    assert rep["symlinks_skipped"] >= 1 and "link-out" not in rep["datasets"] and "BIG-20200101" not in json.dumps(rep)          # nothing outside the root is read through a link
    assert sorted(x["path"] for x in rep["databases_present_not_opened"]) == [os.path.join("md-1", "state.db"), os.path.join("md-1", "state.db-wal")]


def test_it_never_opens_a_database_a_credential_like_file_and_writes_nothing_in_the_root(tmp_path, monkeypatch):
    r = tree(tmp_path)
    before = {p: os.stat(p).st_mtime_ns for p in (os.path.join(dp, f) for dp, _d, fs in os.walk(r) for f in fs)}
    opened = []
    real = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    rep = DI.inventory(str(r), do_hash=True)
    monkeypatch.setattr(builtins, "open", real)
    assert not [p for p in opened if p.endswith((".db", "-wal", ".env", ".key"))], opened
    assert sorted(os.listdir(r)) == sorted(["md-1", "loose.txt", "link-out"])
    after = {p: os.stat(p).st_mtime_ns for p in before}
    assert before == after, "an inspected file changed"
    h = rep["datasets"]["md-1"]["sha256"]
    assert hashlib.sha256(b"x" * 10).hexdigest() in h.values() and not any(k.endswith((".db", ".env", ".key")) for k in h)
    assert rep["datasets"]["md-1"]["not_opened_sensitive_or_db"] == 4


def test_a_missing_root_is_reported_not_raised_and_out_inside_a_root_is_refused(tmp_path, capsys):
    assert DI.inventory(str(tmp_path / "nope"))["exists"] is False
    r = tree(tmp_path)
    assert DI.main(["--root", str(r), "--out", str(r / "inv.json")]) == 2 and not (r / "inv.json").exists()
    out = tmp_path / "inv.json"
    assert DI.main(["--root", str(r), "--out", str(out)]) == 0 and oct(os.stat(out).st_mode & 0o777) == "0o600"
    doc = json.loads(out.read_text())
    assert doc["schema"] == "dataset_inventory.v1" and doc["roots"][0]["files"] >= 6 and "no database was opened" in doc["note"]
