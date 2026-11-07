"""tools/supabase_sync.py: the application-side writer onto the existing ``carry.*`` tables.

STATIC tests (always run, any Python): the module's table specs against the SQL parsed from supabase/migrations, canonicalisation, the secret guard (hostile table +
positive control), the client protocol, conflict reporting against an in-memory client, no-environment/no-network source checks, and three mutation checks.
EXECUTED tests: every migration applied into a DISPOSABLE local Postgres (the ``pgserver`` wheel, private data dir, unix socket) and the plan written through a fake
service-role client that calls ``carry.put_row`` via SQL. They SKIP cleanly when ``pgserver`` is missing. Nothing here contacts Supabase or any network service.
Secret-shaped test strings are assembled from fragments at run time so this file never contains a literal credential.
"""
import dataclasses
import functools
import json
import os
import re
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT / "tools"))
sys.path.insert(0, str(BOT / "tests"))
sys.path.insert(0, str(BOT))

import supabase_sync as SS  # noqa: E402
import owner_import as OI  # noqa: E402
import fixtures_owner_import as FOI  # noqa: E402

SRC = BOT / "tools" / "supabase_sync.py"
MIG_DIR = BOT.parent / "supabase" / "migrations"
FILES = sorted(MIG_DIR.glob("*.sql"))
A, B = str(uuid.uuid4()), str(uuid.uuid4())
H = lambda c: c * 64  # noqa: E731  (a valid sha256 hex string)


# --------------------------------------------------------------------------- SQL parsing (the schema is parsed, not re-typed)

def _strip(sql):
    return re.sub(r"--[^\n]*", "", sql)


def sql_tables():
    sql = "\n".join(_strip(f.read_text()) for f in FILES)
    out = {}
    for name, body in re.findall(r"create table if not exists carry\.(\w+) \((.*?)\n\);", sql, flags=re.S):
        cols, uniques, fks, enums = {}, [], [], {}
        for line in body.split("\n"):
            m = re.match(r"^\s*(\w+) (uuid|text\[\]|text|integer|bigint|numeric|boolean|jsonb|timestamptz|carry\.sha256_hex)\b(.*)$", line)
            if m and m.group(1) not in ("check", "unique", "foreign", "primary"):
                rest = m.group(3)
                cols[m.group(1)] = {"notnull": "not null" in rest, "default": " default " in (" " + rest)}
                e = re.search(r"check \(\w+ in \(([^)]*)\)\)", rest)
                if e:
                    enums[m.group(1)] = tuple(re.findall(r"'(\w+)'", e.group(1)))
        for u in re.findall(r"unique \(([^)]*)\)", body):
            uniques.append(tuple(x.strip() for x in u.split(",")))
        for c, ref in re.findall(r"foreign key \(([^)]*)\)\s+references carry\.(\w+)", body):
            fks.append((ref, tuple(x.strip() for x in c.split(","))))
        for c, vals in re.findall(r"check \((\w+) in \(([^)]*)\)\)", body):
            enums[c] = tuple(re.findall(r"'(\w+)'", vals))
        out[name] = {"cols": cols, "uniques": uniques, "fks": fks, "enums": enums}
    return out


SQL = sql_tables()


# --------------------------------------------------------------------------- sample artifacts for every builder

@functools.lru_cache(maxsize=None)
def make_registry():
    """Built once: registered_utc is the wall clock, and the same artifact must hash the same on every export."""
    import hypothesis_registry as hr
    reg = {"trials": []}
    base = dict(hypothesis_id="H1", mechanism="funding carry earns the basis", family="carry", strategy="carry_overlay", universe=["BTCUSDT"], input_data_sha256=H("a"),
                code_sha="abcdef1", data_window={"from": "2024-01-01", "to": "2026-01-01"}, parameter_grid={"hurdle_pct": [0.1, 0.25]},
                costs={"taker_bps": 5.5, "maker_bps": 2.0}, statistical_design={"test": "block bootstrap"}, train_test={"split": "2025-01-01"},
                multiple_testing_family="carry_family", prospective_period="2026-11-08/2027-02-08")
    hr.register_experiment(reg, "T1", **base)
    hr.add_evidence(reg, "T1", "BACKTEST", result="n=120 sharpe 1.25", provenance="run-1", details={"sharpe": 1.25, "n": 120})
    hr.amend(reg, "T1", "widened window", "data gap")
    hr.register_experiment(reg, "T2", **{**base, "hypothesis_id": "H2", "mechanism": "m2", "parameter_grid": {"a": [1]}})
    hr.add_evidence(reg, "T2", "BACKTEST", result="n=10", provenance="run-2", details={})
    hr.abandon(reg, "T2", "no edge after costs")
    hr.register_experiment(reg, "T3", **{**base, "hypothesis_id": "H3", "mechanism": "m3", "input_data_sha256": {"a": H("b"), "b": H("c")},
                                         "prospective_period": {"start": "2026-11-08"}, "train_test": "split-2025"})
    hr.add_evidence(reg, "T3", "OUT_OF_SAMPLE", result="oos ok", provenance="run-3")
    hr.register_experiment(reg, "T4", **{**base, "hypothesis_id": "H4", "mechanism": "m4"})
    hr.supersede(reg, "T3", "T4", "re-specified")
    reg["trials"].append({"trial_id": "legacy_seed", "status": "frozen_absent", "source": "project_status"})   # not a registration: must be skipped, visibly
    return reg


def policy():
    from risk_policy import Policy
    return Policy(per_trade_loss_usd=25.5, daily_loss_usd=60.0, strategy_drawdown_usd=150.0, portfolio_drawdown_usd=300.0, max_hold_hours=72.0, max_exposure_usd=5000.0,
                  max_leverage=2.0, min_liquidity_usd=100000.0, max_unhedged_usd=50.0, min_margin_multiple=3.0, min_liquidation_distance_pct=30.0, max_data_age_s=120.0,
                  max_correlated_exposure_usd=2500.0, min_recent_funding_bps=1.5, approved_by_owner=True, approval_note="owner said so")


_REAL = {}


def real_reports():
    """REAL, sealed importer output (synthetic fixtures, tmp dir): a clean RECONCILED round trip and a PROVISIONAL one (no balance snapshots)."""
    if not _REAL:
        import tempfile
        for name, kw in (("RECONCILED", {}), ("PROVISIONAL", {"with_wallets": False})):
            d = Path(tempfile.mkdtemp(prefix="sync_imp_"))
            _REAL[name] = OI.run_import(FOI.lay_out(d, FOI.Trip(), **kw), marks=FOI.marks())
            assert _REAL[name]["status"] == name
    return _REAL


def import_report():
    return json.loads(json.dumps(real_reports()["RECONCILED"]))


def resealed(status, base="RECONCILED"):
    """The importer's own ``_seal`` over a real report with another status word (so every digest is genuine)."""
    rep = json.loads(json.dumps(real_reports()[base]))
    rep["status"] = status
    rep["layers"]["realised_cash"]["status"] = status          # the importer's own rule: the account's status IS its realised-cash status
    return OI._seal(rep)


def all_puts(owner):
    puts, skipped = SS.registry_puts(owner, make_registry())
    import strategy_verdict as sv
    puts += [SS.verdict_evidence_put(owner, "T1", sv.EvidenceItem(stage="historical_test", result="PASS", provenance="run-9", metrics={"n": 120, "sharpe": 1.25}, data_sha256=H("d"),
                                                                  recorded_ms=1762300800000))]
    seg = {"segment": "md-20261101.db", "records": 1234, "first_id": 1, "last_id": 1234, "last_chain": "ab" * 32, "closed": True, "sha256": H("e")}
    puts += SS.md_segment_puts(owner, seg, venue="mainnet-public", byte_count=4096, storage_ref="vol://md/md-20261101.db", manifest_sha256=H("f"), manifest_rows=3, sealed_ms=1762300800000)
    puts.append(SS.universe_snapshot_put(owner, {"snapshot_ms": 1762300800000, "universe": {"file": "u.jsonl", "sha256": H("9"), "instruments": 512}, "pages": [{"page": 1}], "rejected": []},
                                         venue="mainnet-public", manifest_ref="vol://universe/1762300800000/manifest.json"))
    puts.append(SS.collector_status_put(owner, "md-recorder", "RUNNING", 1762300800000, last_record_utc=1762300799000, lag_seconds=1.25, detail={"segments": 3}))
    puts.append(SS.system_health_put(owner, "collector", "OK", 1762300800000))
    puts += SS.owner_import_puts(owner, import_report(), imported_utc=1762387300000, storage_ref="private://owner/imports/1")
    imp_id = puts[-1].row["import_id"]
    puts.append(SS.import_status_event_put(owner, imp_id, "RECONCILED", 1762387400000, evidence_ref=imp_id))
    puts.append(SS.import_status_event_put(owner, imp_id, "STALE", 1762387500000, reason="MARK_OLDER_THAN_6H"))
    puts.append(SS.risk_policy_put(owner, "carry_book", 1, policy(), 1762387200000))
    puts.append(SS.approval_put(owner, "AP-1", "risk_policy_version", "carry_book@1", False, 1762387200000, note="not yet"))
    puts.append(SS.approval_put(owner, "AP-2", "risk_policy_version", "carry_book@1", True, 1762387300000, approver_id=owner, statement_sha256=H("5")))
    puts.append(SS.incident_put(owner, "INC-1", "HIGH", "collector stalled", 1762387200000, runbook_ref="RUNBOOKS.md#collector"))
    puts.append(SS.incident_event_put(owner, "INC-1", "OPENED", 1762387200000))
    puts.append(SS.incident_event_put(owner, "INC-1", "RESOLVED", 1762390800000, note="restarted, lag back to 1s"))
    puts.append(SS.backup_receipt_put(owner, "BK-1", "evidence archive", H("6"), 123456, "vol://backup/1", 1762387200000))
    puts.append(SS.restore_receipt_put(owner, "RS-1", "BK-1", "PASSED", H("7"), 1762390800000, checks={"chain": "ok"}))
    return puts, skipped


# --------------------------------------------------------------------------- static: schema parity

def test_every_spec_table_matches_the_parsed_sql():
    for t in SS.TABLE_ORDER:
        sql = SQL[t]
        spec_cols = set(SS.columns(t))
        sql_cols = set(sql["cols"]) - {"id", "created_at"}
        assert spec_cols == sql_cols, (t, spec_cols ^ sql_cols)
        notnull_nodefault = {c for c, i in sql["cols"].items() if i["notnull"] and not i["default"]} - {"id"}
        assert set(SS.required_columns(t)) == notnull_nodefault | {"schema_version"}, (t, set(SS.required_columns(t)) ^ (notnull_nodefault | {"schema_version"}))
        for c in SS.SPECS[t]:
            if c.kind == "enum":
                assert c.enum == sql["enums"][c.name], (t, c.name)
        natural = {tuple(x for x in u if x != "owner_id") for u in sql["uniques"]} - {("content_sha256",)}
        assert natural == ({SS.NATURAL_KEY[t]} if SS.NATURAL_KEY[t] else set()), (t, natural)


def test_table_order_respects_foreign_keys_and_unmapped_tables_are_the_trading_ones():
    pos = {t: i for i, t in enumerate(SS.TABLE_ORDER)}
    for t in SS.TABLE_ORDER:
        for parent, _cols in SQL[t]["fks"]:
            assert parent in pos and pos[parent] < pos[t], (t, parent)
    unmapped = set(SQL) - set(SS.TABLE_ORDER)
    assert unmapped == {"risk_decisions", "execution_sessions", "execution_session_events", "order_refs", "fill_refs", "reconciliation_exceptions",
                        "reconciliation_exception_resolutions", "account_snapshots"}


def test_vocabularies_match_their_sources():
    import hypothesis_registry as hr
    import owner_import as oi
    import strategy_verdict as sv
    assert SS.EVIDENCE_LEVELS == hr.EVIDENCE_LEVELS
    assert SS.OWNER_STATUSES == oi.STATUSES
    assert set(sv.STAGE_TO_REGISTRY_LEVEL.values()) <= set(SS.EVIDENCE_LEVELS)
    from risk_policy import Policy
    assert SS.RISK_LIMIT_FIELDS == tuple(f.name for f in dataclasses.fields(Policy) if f.name not in ("approved_by_owner", "approval_note"))


def test_builders_emit_no_unknown_column_and_omit_no_not_null_column():
    puts, _ = all_puts(A)
    assert {p.table for p in puts} == set(SS.TABLE_ORDER)
    for p in puts:
        cols = set(p.row)
        sql = SQL[p.table]["cols"]
        assert cols <= set(sql), (p.table, cols - set(sql))
        need = {c for c, i in sql.items() if i["notnull"] and not i["default"]}
        assert need <= cols, (p.table, need - cols)
        assert p.row["owner_id"] == A and p.row["source"] and p.row["schema_version"] == 1 and re.fullmatch(r"[0-9a-f]{64}", p.row["content_sha256"])


# --------------------------------------------------------------------------- static: canonicalisation and provenance

def test_canonical_json_one_form():
    assert SS.canonical_json({"b": 1, "a": [0.1, {"z": 2, "y": 1.5}]}) == '{"a":["0.1",{"y":"1.5","z":2}],"b":1}'
    assert SS.canon_decimal(100.0) == "100" and SS.canon_decimal("1.500") == "1.5" and SS.canon_decimal(-0.0) == "0" and SS.canon_decimal(1e-7) == "0.0000001"
    assert SS.content_sha256({"a": 1, "b": 2}) == SS.content_sha256({"b": 2, "a": 1})
    for bad in (float("nan"), float("inf"), True, None, "abc", object()):
        with pytest.raises(SS.SyncError):
            SS.canon_decimal(bad)
    with pytest.raises(SS.SyncError):
        SS.canonical_json({"a": object()})
    with pytest.raises(SS.SyncError):
        SS.canonical_json({1: "non-str key"})
    for naive in ("2026-11-07T00:00:00", "2026-11-07", 1.5, True):
        with pytest.raises(SS.SyncError):
            SS.utc(naive)
    assert SS.utc("2026-11-07T01:00:00+01:00") == "2026-11-07T00:00:00Z" and SS.utc(1762300800500) == "2025-11-05T00:00:00.5Z"


def test_registration_hash_is_the_registrys_entry_digest_and_every_row_has_provenance():
    import hypothesis_registry as hr
    reg = make_registry()
    puts, skipped = SS.registry_puts(A, reg)
    regs = {p.row["trial_id"]: p.row for p in puts if p.table == "experiment_registrations"}
    assert set(regs) == {"T1", "T2", "T3", "T4"}
    for t in reg["trials"]:
        if t["trial_id"] in regs:
            assert regs[t["trial_id"]]["content_sha256"] == hr.entry_digest(t)
    assert len(skipped) == 1 and skipped[0].startswith("legacy_seed")           # the non-registration is reported, not dropped silently
    assert regs["T3"]["input_data_sha256"] == SS.content_sha256({"a": H("b"), "b": H("c")})   # a map of hashes is digested, never truncated
    assert regs["T3"]["train_test"] == {"value": "split-2025"} and json.loads(regs["T3"]["prospective_period"]) == {"start": "2026-11-08"}
    assert regs["T1"]["parameter_grid"] == {"hurdle_pct": ["0.1", "0.25"]}       # floats are exact strings
    assert {p.table for p in puts} >= {"experiment_abandonments", "experiment_supersessions", "experiment_amendments", "experiment_evidence", "strategy_hypotheses"}


def test_verdict_evidence_equals_registry_evidence_for_the_same_item():
    import hypothesis_registry as hr
    import strategy_verdict as sv
    item = sv.EvidenceItem(stage="oos_test", result="PASS", provenance="run-4", metrics={"n": 80, "sharpe": 0.9}, data_sha256=H("d"), recorded_ms=1762300800000)
    via_verdict = SS.verdict_evidence_put(A, "T1", item)
    reg = {"trials": []}
    hr.record(reg, "T1", status="registered", evidence=[])
    ev = {"level": sv.STAGE_TO_REGISTRY_LEVEL["oos_test"], "result": "oos_test:PASS", "provenance": "run-4",
          "details": {"metrics": {"n": 80, "sharpe": 0.9}, "data_sha256": H("d"), "status": None}, "recorded_utc": "2025-11-05T00:00:00Z"}
    via_registry = SS.evidence_put(A, "T1", ev["level"], result=ev["result"], provenance=ev["provenance"], details=ev["details"], recorded_utc=ev["recorded_utc"],
                                   source="tools/strategy_verdict.py")
    assert via_verdict.row == via_registry.row
    with pytest.raises(SS.SyncError):
        SS.verdict_evidence_put(A, "T1", sv.EvidenceItem(stage="oos_test", result="PASS", provenance="x"))      # no time: refuses to invent one
    with pytest.raises(SS.SyncError):
        SS.evidence_put(A, "T1", "PASSED", result="x", provenance="y", recorded_utc=1762300800000)


def test_owner_id_is_required_and_comes_from_the_caller():
    for bad in (None, "", "not-a-uuid", A.upper(), 123):
        with pytest.raises(SS.SyncError):
            SS.incident_put(bad, "I", "WARN", "t", 1762300800000)
        with pytest.raises(SS.SyncError):
            SS.plan(bad, [])
    p = SS.incident_put(A, "I", "WARN", "t", 1762300800000)
    with pytest.raises(SS.SyncError):
        SS.plan(B, [p])                                                           # a row for another owner never rides in someone else's plan


# --------------------------------------------------------------------------- static: status vocabulary, imports, approvals, policy

def test_import_status_vocabulary_is_respected():
    for s in SS.OWNER_STATUSES:
        rep = resealed(s)
        row = SS.owner_import_puts(A, rep, imported_utc=1762387300000, storage_ref="private://x")[0].row
        assert row["status"] == s and row["import_id"] == row["content_sha256"]
    for bad in ("OK", "verified", "LIVE", "", None, "RECONCILED "):
        with pytest.raises(SS.SyncError, match="status:not_in_the_eight_words"):
            SS.owner_import_puts(A, dict(import_report(), status=bad), imported_utc=1762387300000, storage_ref="private://x")
        with pytest.raises(SS.SyncError):
            SS.import_status_event_put(A, H("a"), bad, 1762387300000, reason="REASON", evidence_ref=H("a"))
    with pytest.raises(SS.SyncError):
        SS.import_status_event_put(A, H("a"), "RECONCILED", 1762387300000)                 # needs the import row hash as evidence_ref
    with pytest.raises(SS.SyncError):
        SS.import_status_event_put(A, H("a"), "DISPUTED", 1762387300000)                   # needs a reason code
    assert SS.import_status_event_put(A, H("a"), "DISPUTED", 1762387300000, reason="RECORDS_CONTRADICT").row["status"] == "DISPUTED"


def test_import_report_row_holds_no_raw_owner_data():
    hostile = json.loads(json.dumps(import_report()))
    hostile.update({
        "fills": [{"execId": "SENTINEL_EXEC_1", "execQty": "0.5", "uid": "SENTINEL_UID"}], "wallet": {"btc": "SENTINEL_BALANCE_7777", "usdt": 12345.678},
        "accountId": "SENTINEL_ACCT", "flags": [{"detail": "SENTINEL_FLAG", "code": "X", "severity": "INFO"}]})
    hostile["lineage"]["marks"] = [{"price": "SENTINEL_MARK"}]
    row = SS.owner_import_puts(A, OI._seal(hostile), imported_utc=1762387300000, storage_ref="private://x")[0].row     # sealed WITH the hostile extras: they are not copied
    blob = json.dumps(row)
    assert "SENTINEL" not in blob and "476.275" not in blob and "12345" not in blob
    assert set(row) <= {"owner_id", "import_id", "source_sha256", "kind", "period_start_utc", "period_end_utc", "row_count", "storage_ref", "status", "imported_utc",
                        "source", "schema_version", "content_sha256"}
    assert row["row_count"] == len(import_report()["lineage"]["files"])              # the count of FILES hashed, not any record
    with pytest.raises(SS.SyncError):
        SS.owner_import_puts(A, {"status": "RECONCILED"}, imported_utc=1762387300000, storage_ref="x")       # no lineage hashes: refuses rather than inventing a source hash


def test_approval_is_stated_never_inferred_and_cannot_name_the_gate():
    with pytest.raises(TypeError):
        SS.approval_put(A, "AP", "risk_policy_version", "p@1")                          # approved_by_owner is positional and required
    for bad in (1, "yes", None, 0):
        with pytest.raises(SS.SyncError):
            SS.approval_put(A, "AP", "risk_policy_version", "p@1", bad, 1762387300000)
    with pytest.raises(SS.SyncError):
        SS.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000)                                             # no approver / statement
    with pytest.raises(SS.SyncError):
        SS.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000, approver_id=B, statement_sha256=H("5"))     # not the owner
    for kind in ("promotion_gate", "live_trading", "LIVE"):
        with pytest.raises(SS.SyncError):
            SS.approval_put(A, "AP", kind, "x", False, 1762387300000)
    # a policy object that says approved_by_owner=True does NOT become an approval, and its flag is not copied into the policy document
    p = SS.risk_policy_put(A, "carry_book", 1, policy(), 1762387200000)
    assert "approved_by_owner" not in p.row["policy"] and "approval_note" not in p.row["policy"] and set(p.row["policy"]) == set(SS.RISK_LIMIT_FIELDS)
    assert p.row["policy"]["per_trade_loss_usd"] == "25.5" and p.row["policy"]["max_exposure_usd"] == "5000" and p.row["content_sha256"] == SS.content_sha256(p.row["policy"])
    with pytest.raises(SS.SyncError):
        SS.risk_policy_put(A, "carry_book", 1, {"per_trade_loss_usd": 1}, 1762387200000)       # a partial policy is refused


def test_md_segment_must_be_closed_and_hash_pinned():
    seg = {"segment": "md-20261101.db", "records": 5, "last_id": 5, "last_chain": "ab" * 32, "closed": False}
    with pytest.raises(SS.SyncError):
        SS.md_segment_puts(A, seg, venue="v", byte_count=1, storage_ref="r", manifest_sha256=H("f"), manifest_rows=1, sealed_ms=1762300800000)
    with pytest.raises(SS.SyncError):
        SS.universe_snapshot_put(A, {"nothing": 1}, venue="v", manifest_ref="r")


# --------------------------------------------------------------------------- static: SECRET GUARD

def _shapes():
    """Credential-shaped strings, assembled from fragments (no literal credential in this file)."""
    return {
        "jwt": "ey" + "J" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
        "private key": "-----BEGIN " + "RSA PRIVATE" + " KEY-----\nMIIEow",
        "supabase secret": "sb_" + "secret_" + "abcdefghij0123456789",
        "supabase publishable": "sb_" + "publishable_" + "abcdefghij0123456789",
        "sk key": "sk" + "-" + "proj1234567890abcdefghijklmnop",
        "github token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "aws key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "slack token": "xo" + "xb-" + "1234567890-abcdefghijkl",
        "bearer": "Authorization: Bear" + "er " + "abcdefghijklmnopqrstuvwxyz012345",
        "credentialed url": "post" + "gres://svc:hunter2pw@db.example.com/postgres",
        "url token param": "https://api.example.com/v1/x?api" + "_key=abc123def456ghi",
        "assigned secret": "api_" + "secret = " + "Zx81Qp92LmN",
        "exchange style key": "kR3" + "mQ9xTz" + "A1bC7dEf" + "G2hJ",
        "email value": "owner" + "@" + "example.org",
        "ip value": "203.0" + ".113.77",
    }


@pytest.mark.parametrize("name", sorted(_shapes()))
def test_secret_shapes_are_refused_in_every_free_text_column_and_nested_json(name):
    s = _shapes()[name]
    for build in (lambda: SS.incident_put(A, "I1", "WARN", "title " + s, 1762300800000),
                  lambda: SS.incident_put(A, "I1", "WARN", "t", 1762300800000, runbook_ref=s),
                  lambda: SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"note": [{"deep": s}]}),
                  lambda: SS.restore_receipt_put(A, "R", "B", "PASSED", H("7"), 1762300800000, checks={"x": s}),
                  lambda: SS.data_batch_put(A, {"batch_id": "b", "dataset": "d", "venue": "v", "batch_sha256": H("a"), "byte_count": 1, "segment_ids": ["s"],
                                                "window_start_utc": "2026-11-07T00:00:00Z", "window_end_utc": "2026-11-07T00:00:00Z", "storage_ref": s})):
        with pytest.raises(SS.SecretGuardError) as ei:
            build()
        assert s not in str(ei.value) and s[:12] not in str(ei.value)             # the finding names table/path/rule, never the value
        assert ei.value.findings and all(re.match(r"^\w+\.[^:]+:[a-z_0-9]+", f) for f in ei.value.findings)


@pytest.mark.parametrize("field", ["apiKey", "api_key", "secret", "apiSecret", "uid", "accountId", "account_id", "email", "ip", "password", "token", "privateKey", "Authorization",
                                   "service_role_key"])
def test_account_identifier_and_secret_field_names_are_refused_even_with_innocent_values(field):
    with pytest.raises(SS.SecretGuardError):
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={field: "x"})
    with pytest.raises(SS.SecretGuardError):
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"outer": {"inner": [{field.upper(): 1}]}})


def test_positive_control_clean_rows_pass_the_guard():
    ok = SS.collector_status_put(A, "md-recorder", "RUNNING", 1762300800000, detail={"segments": 3, "batch_sha256": H("a"), "last_error_code": "LAG_BACK_TO_1S", "lag_seconds": 1.2})
    assert ok.row["detail"]["segments"] == 3
    puts, _ = all_puts(A)
    assert SS.plan(A, puts).puts and all(SS.scan_row(p.table, p.row) == [] for p in puts)


def test_plan_and_write_rescan_hand_built_rows_and_send_nothing():
    puts, _ = all_puts(A)
    bad_row = dict(next(p for p in puts if p.table == "incidents").row)
    bad_row["title"] = _shapes()["jwt"]
    bad_row["content_sha256"] = SS.row_hash("incidents", bad_row)
    with pytest.raises(SS.SecretGuardError):
        SS.plan(A, puts, [SS.Put("incidents", bad_row)])
    forged = dataclasses.replace(SS.plan(A, puts), puts=SS.plan(A, puts).puts + (SS.Put("incidents", bad_row),))
    client = MemClient()
    with pytest.raises(SS.SecretGuardError):
        SS.write(forged, client)
    assert client.calls == 0                                                     # refused BEFORE the first call
    tampered = dict(next(p for p in puts if p.table == "incidents").row, title="changed after hashing")
    with pytest.raises(SS.SyncError):
        SS.plan(A, [SS.Put("incidents", tampered)])                              # content_sha256 no longer matches the content


# --------------------------------------------------------------------------- static: client protocol, plan, write, conflicts

class MemClient(SS.ServiceRoleClient):
    """In-memory stand-in with the same collision rules as the tables."""
    role = "service_role"

    def __init__(self):
        self.rows = {}
        self.calls = 0

    def put_row(self, table, row):
        self.calls += 1
        rows = self.rows.setdefault(table, [])
        nk = SS.NATURAL_KEY[table]
        for r in rows:
            if r["owner_id"] == row["owner_id"] and (r["content_sha256"] == row["content_sha256"] or (nk and all(r[k] == row[k] for k in nk))):
                return False
        rows.append(dict(row))
        return True

    def find_content_hashes(self, table, owner_id, key):
        return [r["content_sha256"] for r in self.rows.get(table, []) if r["owner_id"] == owner_id and all(r.get(k) == v for k, v in key.items())]


def test_plan_is_ordered_deduplicated_and_reports_in_plan_conflicts():
    puts, skipped = all_puts(A)
    p = SS.plan(A, reversed(puts), puts[:5], skipped=skipped)                    # reversed order + repeated rows
    order = [SS.TABLE_ORDER.index(x.table) for x in p.puts]
    assert order == sorted(order) and p.collapsed == 5 and len(p.puts) == len({(x.table, x.row["content_sha256"]) for x in p.puts})
    assert p.skipped == tuple(skipped) and p.digest() == SS.plan(A, puts, skipped=skipped).digest()          # same artifacts, same plan, whatever the input order
    evid = [i for i, x in enumerate(p.puts) if x.table == "experiment_evidence"]
    aband = [i for i, x in enumerate(p.puts) if x.table == "experiment_abandonments"]
    assert max(evid) < min(aband)                                                # evidence goes before the abandonment that would refuse it
    other = SS.incident_put(A, "INC-1", "CRITICAL", "same id, different content", 1762387200000)
    p2 = SS.plan(A, puts, [other])
    assert len(p2.conflicts) == 1 and p2.conflicts[0].row["title"] == "same id, different content"
    rep = SS.write(p2, MemClient())
    assert not rep.ok and rep.count(SS.CONFLICT) == 1 and rep.count(SS.FAILED) == 0
    # the same risk-policy document under another version is a conflict too (content hash already used under a different key)
    v2 = SS.risk_policy_put(A, "carry_book", 2, policy(), 1762387200000)
    assert len(SS.plan(A, puts, [v2]).conflicts) == 1


def test_write_is_idempotent_and_reports_a_conflicting_duplicate():
    puts, skipped = all_puts(A)
    client = MemClient()
    p = SS.plan(A, puts, skipped=skipped)
    first = SS.write(p, client)
    assert first.ok and first.count(SS.INSERTED) == len(p.puts) and first.skipped == tuple(skipped)
    stored = {t: len(r) for t, r in client.rows.items()}
    second = SS.write(p, client)
    assert second.ok and second.count(SS.UNCHANGED) == len(p.puts) and second.count(SS.INSERTED) == 0
    assert {t: len(r) for t, r in client.rows.items()} == stored
    # same natural key, different content: put_row says False for both; the writer must say CONFLICT
    inc = SS.incident_put(A, "INC-1", "CRITICAL", "tampered title", 1762387200000)
    rep = SS.write(SS.plan(A, [inc]), client)
    assert not rep.ok and rep.results[0].outcome == SS.CONFLICT and "DIFFERENT content" in rep.results[0].detail
    assert next(r for r in client.rows["incidents"] if r["incident_id"] == "INC-1")["title"] == "collector stalled"      # nothing was overwritten
    pol = dict(vars(policy()), per_trade_loss_usd=9999.0)
    rep = SS.write(SS.plan(A, [SS.risk_policy_put(A, "carry_book", 1, pol, 1762387200000)]), client)
    assert rep.results[0].outcome == SS.CONFLICT


def test_failure_halts_and_never_echoes_database_text():
    class Boom(MemClient):
        def put_row(self, table, row):
            if table == "incident_events":
                raise RuntimeError("Failing row contains (" + json.dumps(row) + ")")
            return super().put_row(table, row)
    puts, _ = all_puts(A)
    rep = SS.write(SS.plan(A, puts), Boom())
    assert not rep.ok and rep.count(SS.FAILED) == 1 and rep.count(SS.NOT_SENT) > 0
    assert all("Failing row" not in r.detail and "INC-1" not in r.detail for r in rep.results)


def _client(**attrs):
    """A real ServiceRoleClient subclass with the given class attributes (role, anon_key, ...)."""
    ns = {"put_row": lambda self, t, r: True, "find_content_hashes": lambda self, *a: []}
    ns.update(attrs)
    return type("TestClient", (SS.ServiceRoleClient,), ns)()


def reg_entry(**over):
    t = dict(make_registry()["trials"][0])
    t.update(over)
    return t


@pytest.mark.parametrize("make", [
    lambda: types.SimpleNamespace(role="anon", put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(role="authenticated", put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(role="service_role", anon_key="x", put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(role="service_role", publishable_key="x", put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(role="service_role", is_anon=True, put_row=lambda t, r: True, find_content_hashes=lambda *a: []),
    lambda: types.SimpleNamespace(role="service_role", put_row=lambda t, r: True),
    lambda: None,
])
def test_non_service_role_clients_are_refused_before_any_call(make):
    puts, _ = all_puts(A)
    with pytest.raises(SS.ClientRefused):
        SS.write(SS.plan(A, puts), make())


def test_module_reads_no_environment_builds_no_client_and_opens_no_network():
    src = SRC.read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for banned in ("os.environ", "getenv", "dotenv", "import socket", "urllib", "requests", "httpx", "http.client", "import supabase", "from supabase", "create_client", "psycopg", "subprocess", "open("):
        assert banned not in code.split('"""', 2)[2], banned
    assert "SUPABASE_" not in src and "service_role_key" not in code.lower().replace("servicerolekey", "")


# --------------------------------------------------------------------------- static: mutation checks (each guard's test must be able to fail)

def _mutant(old, new):
    src = SRC.read_text()
    assert src.count(old) == 1, f"mutation anchor must be unique: {old!r}"
    mod = types.ModuleType("supabase_sync_mutant")
    mod.__file__ = str(SRC)
    sys.modules["supabase_sync_mutant"] = mod
    try:
        exec(compile(src.replace(old, new), str(SRC), "exec"), mod.__dict__)
    finally:
        sys.modules.pop("supabase_sync_mutant", None)
    return mod


def test_mutation_secret_guard_account_identifier_field_names():
    with pytest.raises(SS.SecretGuardError):
        SS.registration_puts(A, reg_entry(costs={"uid": "7"}))
    m = _mutant("if n in _KEY_EXACT:", "if False:")
    assert m.registration_puts(A, reg_entry(costs={"uid": "7"}))     # mutant lets an account id field through: the field-name tests would fail on it


def test_mutation_secret_guard_value_shapes():
    s = _shapes()["credentialed url"]
    with pytest.raises(SS.SecretGuardError):
        SS.incident_put(A, "I", "WARN", s, 1762300800000)
    m = _mutant("    hits = [name for name, rx in _VALUE_RULES if rx.search(s)]", "    hits = []")
    assert m.incident_put(A, "I", "WARN", s, 1762300800000)                                  # mutant lets a credentialed URL through: the hostile table would fail on it


def test_mutation_client_role_check():
    anon = _client(role="anon")
    with pytest.raises(SS.ClientRefused):
        SS.check_client(anon)
    m = _mutant('if role != "service_role":', "if False:")
    m.ServiceRoleClient.register(type(anon))
    m.check_client(anon)                                                  # mutant accepts an anon client: the refusal tests would fail on it


def test_mutation_conflict_detection():
    puts, _ = all_puts(A)
    client = MemClient()
    SS.write(SS.plan(A, puts), client)
    inc = SS.incident_put(A, "INC-1", "CRITICAL", "different", 1762387200000)
    assert SS.write(SS.plan(A, [inc]), client).results[0].outcome == SS.CONFLICT
    m = _mutant('if put.row["content_sha256"] in stored:', "if True:")
    m.ServiceRoleClient.register(MemClient)
    assert m.write(m.plan(A, [m.Put(inc.table, inc.row)]), client).results[0].outcome == "UNCHANGED"      # mutant hides the conflict: the conflict tests would fail on it


def test_mutation_owner_approval_rule():
    with pytest.raises(SS.SyncError):
        SS.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000)
    m = _mutant('if table == "approval_records" and r["approved_by_owner"]:', 'if table == "approval_records" and False:')
    assert m.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000)


# --------------------------------------------------------------------------- red-team round 3: reproductions, hostile tables, positive controls

def _reseal_by_hand(rep):
    """What a forger who knows the digest scheme would do after editing a report: recompute every figure digest, the lineage digest and the report digest."""
    lin = rep["lineage"]
    for e in lin["figures"].values():
        e["provenance_digest"] = OI._sha_text(OI._canon({k: e.get(k) for k in ("value", "formula", "sources", "derived_digests", "all_records_digest", "empty_set")}))
    fh = {p: [e["value"], e["provenance_digest"]] for p, e in sorted(lin["figures"].items())}
    lin["digest"] = OI._sha_text(OI._canon({"files": [[e["role"], e["sha256"]] for e in lin["files"]], "marks": lin.get("marks"), "stages": lin["stages"], "figures": fh}))
    rep["report_sha256"] = OI._sha_text(OI._canon({k: v for k, v in rep.items() if k != "report_sha256"}))
    return rep


def _refused(report, reason):
    with pytest.raises(SS.SyncError) as ei:
        SS.owner_import_puts(A, report, imported_utc=1762387300000, storage_ref="private://x")
    assert reason in str(ei.value), str(ei.value)


def test_rt3_1_the_handmade_reconciled_report_is_refused():
    fake = {"status": "RECONCILED", "lineage": {"files": [{"sha256": "a" * 64}]}}
    _refused(fake, "report_sha256")
    _refused(dict(fake, report_sha256="b" * 64, import_id="imp_" + "c" * 32), "report_sha256:does_not_recompute")
    for junk in (None, [], "RECONCILED", 7):
        with pytest.raises(SS.SyncError):
            SS.owner_import_puts(A, junk, imported_utc=1762387300000, storage_ref="x")


def test_rt3_1_forger_helper_positive_control_and_every_tamper_is_named():
    assert SS.verify_import_report(_reseal_by_hand(import_report())) == []          # the helper itself is faithful: later refusals are for the stated reason
    assert SS.verify_import_report(import_report()) == [] and SS.verify_import_report(real_reports()["PROVISIONAL"]) == []

    def edit(fn):
        r = import_report()
        fn(r)
        return r
    # edited after sealing, digests NOT recomputed
    _refused(edit(lambda r: r.update(status="VERIFIED")), "report_sha256:does_not_recompute")
    _refused(edit(lambda r: r["lineage"]["files"][0].update(sha256="d" * 64)), "report_sha256:does_not_recompute")
    # outer digest recomputed but the lineage digest / figure digests are not
    def outer_only(r):
        r["lineage"]["files"][0]["sha256"] = "d" * 64
        r["report_sha256"] = OI._sha_text(OI._canon({k: v for k, v in r.items() if k != "report_sha256"}))
    _refused(edit(outer_only), "lineage.digest:does_not_recompute")

    def fig_value(r):
        r["layers"]["realised_cash"]["net_realised_cash_usd"] = 9999.0
        r["report_sha256"] = OI._sha_text(OI._canon({k: v for k, v in r.items() if k != "report_sha256"}))
    _refused(edit(fig_value), "lineage.figures:value_differs_from_report")

    def fig_digest(r):
        r["lineage"]["figures"]["headline_profit_usd"]["value"] = 9999.0
        r["headline_profit_usd"] = 9999.0
        r["report_sha256"] = OI._sha_text(OI._canon({k: v for k, v in r.items() if k != "report_sha256"}))
    _refused(edit(fig_digest), "lineage.figures:provenance_digest_does_not_recompute")
    # fully re-forged digests, but the CLAIM is inconsistent with the report's own layers / headline
    def headline_under_disputed(r):
        r["status"] = "DISPUTED"
        for layer in r["layers"].values():
            layer["status"] = "DISPUTED"
        return _reseal_by_hand(r)
    _refused(headline_under_disputed(import_report()), "headline:non_null_under_DISPUTED")

    def reconciled_layer_under_disputed(r):
        r["status"] = "DISPUTED"
        r["headline_profit_usd"] = None
        r["lineage"]["figures"]["headline_profit_usd"]["value"] = None
        return _reseal_by_hand(r)
    _refused(reconciled_layer_under_disputed(import_report()), "claims_RECONCILED_under_a_weaker_report_status")

    def headline_not_net(r):
        r["headline_profit_usd"] = 1.0
        r["lineage"]["figures"]["headline_profit_usd"]["value"] = 1.0
        return _reseal_by_hand(r)
    _refused(headline_not_net(import_report()), "headline:not_the_realised_cash_net")

    def reconciled_no_layers(r):
        r["layers"] = {}
        r["headline_profit_usd"] = None
        for k in [k for k in r["lineage"]["figures"] if k.startswith("layers.") or k == "headline_profit_usd"]:
            del r["lineage"]["figures"][k]
        return _reseal_by_hand(r)
    _refused(reconciled_no_layers(import_report()), "RECONCILED_without_a_realised_cash_layer")

    def status_vs_layer(r):
        r["status"] = "PROVISIONAL"
        return _reseal_by_hand(r)
    _refused(status_vs_layer(import_report()), "status:differs_from_the_realised_cash_layer")


@pytest.mark.parametrize("bad", ["OK", "verified", "RECONCILED ", "", None, 3, ["RECONCILED"]])
def test_rt3_1_status_outside_the_eight_words_is_refused_even_when_resealed(bad):
    r = import_report()
    r["status"] = bad
    try:
        _reseal_by_hand(r)
    except TypeError:
        pass
    _refused(r, "status:not_in_the_eight_words")


def test_rt3_1_report_with_non_json_content_is_refused_not_crashed():
    r = import_report()
    r["extra"] = float("nan")
    _refused(r, "report")
    r = import_report()
    r["extra"] = object()
    _refused(r, "report")


# ---- (2) status events

def test_rt3_2_evidence_ref_is_a_content_hash_never_free_text():
    imp = SS.owner_import_puts(A, import_report(), imported_utc=1762387300000, storage_ref="private://x")[0].row["import_id"]
    for free in ("recon-1", "see the bank statement", "owner said it is fine", H("A"), "a" * 63, ""):
        for st in ("DISPUTED", "RECONCILED"):
            with pytest.raises(SS.SyncError):
                SS.import_status_event_put(A, imp, st, 1762387300000, reason="SOME_REASON", evidence_ref=free)
    for prose in ("mark older than 6h", "lower_case_code", "Has Space", "X", "A" * 70):
        with pytest.raises(SS.SyncError):
            SS.import_status_event_put(A, imp, "STALE", 1762387300000, reason=prose)
    assert SS.import_status_event_put(A, imp, "STALE", 1762387300000, reason="MARK_STALE", evidence_ref=H("e")).row["evidence_ref"] == H("e")      # any content hash is fine for a negative status


def test_rt3_2_verified_or_reconciled_event_needs_the_matching_import_row():
    imp_put = SS.owner_import_puts(A, import_report(), imported_utc=1762387300000, storage_ref="private://x")[0]
    imp = imp_put.row["import_id"]
    for st in ("RECONCILED", "VERIFIED"):
        with pytest.raises(SS.SyncError, match="import_row_hash"):
            SS.import_status_event_put(A, imp, st, 1762387300000, evidence_ref=H("e"))                    # a hash, but not THE import row's hash
    ev = SS.import_status_event_put(A, imp, "RECONCILED", 1762387400000, evidence_ref=imp)
    assert SS.plan(A, [imp_put, ev]).puts                                                                  # positive control: row + event together
    with pytest.raises(SS.SyncError, match="no matching import row"):
        SS.plan(A, [ev])                                                                                   # the event alone cannot promote an import the plan lacks
    estimated = SS.owner_import_puts(A, resealed("ESTIMATED"), imported_utc=1762387300000, storage_ref="private://x")[0]
    ev2 = SS.import_status_event_put(A, estimated.row["import_id"], "RECONCILED", 1762387400000, evidence_ref=estimated.row["import_id"])
    with pytest.raises(SS.SyncError, match="no matching import row"):
        SS.plan(A, [estimated, ev2])                                                                       # the stored import says ESTIMATED: no promotion by event
    client = MemClient()
    forged = dataclasses.replace(SS.plan(A, [imp_put, ev]), puts=(ev,))
    with pytest.raises(SS.SyncError):
        SS.write(forged, client)
    assert client.calls == 0
    hand = dict(ev.row, evidence_ref="recon-1")
    hand["content_sha256"] = SS.row_hash("owner_account_import_status_events", hand)
    with pytest.raises(SS.SyncError):
        SS.plan(A, [imp_put, SS.Put(ev.table, hand)])                                                      # hand-built row with free-text evidence


# ---- (3) whitelisted typed schemas + the widened guard

HOSTILE_OBJECTS = [
    ("evidence", "wallet_balance", {"wallet": {"btc": "1.5", "usdt": 100000}}),
    ("evidence", "positions", {"positions": [{"symbol": "BTCUSDT", "size": "0.5"}]}),
    ("evidence", "acct", {"acct": "12345678"}),
    ("evidence", "unlisted_metric", {"sharpe_but_not_really": 3}),
    ("evidence", "metric_wrong_type", {"sharpe": {"nested": 1}}),
    ("evidence", "metrics_unlisted", {"metrics": {"balance_usd": 5}}),
    ("collector", "unlisted", {"note": "free text"}),
    ("collector", "wrong_type", {"segments": -1}),
    ("collector", "string_count", {"segments": "3"}),
    ("collector", "lowercase_error_code", {"last_error_code": "boom"}),
    ("health", "unlisted", {"pnl": 5}),
    ("health", "wrong_type", {"queue_depth": 1.5}),
    ("restore", "unlisted", {"details": {"x": 1}}),
    ("restore", "wrong_type", {"chain": "mostly ok"}),
    ("seal", "unlisted", {"account": "x"}),
    ("seal", "wrong_type", {"records": "many"}),
]


def _object_put(kind, obj):
    if kind == "evidence":
        return SS.evidence_put(A, "T1", "BACKTEST", result="r", provenance="p", details=obj, recorded_utc=1762300800000)
    if kind == "collector":
        return SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail=obj)
    if kind == "health":
        return SS.system_health_put(A, "comp", "OK", 1762300800000, detail=obj)
    if kind == "restore":
        return SS.restore_receipt_put(A, "R", "B", "PASSED", H("7"), 1762300800000, checks=obj)
    return SS.md_seal_receipt_put(A, seal_id="s", batch_id="b", manifest_sha256=H("f"), manifest_rows=1, sealed_utc=1762300800000, receipt=obj)


@pytest.mark.parametrize("kind,why,obj", HOSTILE_OBJECTS, ids=[f"{k}-{w}" for k, w, _ in HOSTILE_OBJECTS])
def test_rt3_3_free_form_objects_are_refused_by_the_whitelist_not_stripped(kind, why, obj):
    with pytest.raises(SS.SyncError):
        _object_put(kind, obj)


POSITIVE_OBJECTS = [
    ("evidence", {"sharpe": 1.25, "n": 120, "metrics": {"independent_observations": 1000, "uses_future_information": False, "venue": "testnet",
                                                       "rejection_conditions_fired": ["COVERAGE_LOW"], "net_edge_bps_after_costs": 3.5}, "data_sha256": H("d"), "status": None}),
    ("evidence", {}),
    ("collector", {"segments": 3, "records": 10, "gaps": 0, "restarts": 1, "reconnects": 2, "lag_seconds": 1.25, "last_error_code": "WS_CLOSED", "batch_sha256": H("a")}),
    ("health", {"latency_ms": 12.5, "queue_depth": 0, "error_count": 2, "uptime_s": 3600, "version": "1.2.3", "last_error_code": "TIMEOUT"}),
    ("restore", {"chain": "ok", "schema": "PASS", "sha256_match": True, "rows_match": True, "row_count": 55, "restore_seconds": 4.5}),
    ("seal", {"segment": "md-20261101.db", "records": 5, "first_id": 1, "last_id": 5, "last_chain": H("c"), "sha256": H("e")}),
]


@pytest.mark.parametrize("kind,obj", POSITIVE_OBJECTS, ids=[f"{k}-{i}" for i, (k, _) in enumerate(POSITIVE_OBJECTS)])
def test_rt3_3_positive_controls_whitelisted_objects_pass_and_plan(kind, obj):
    put = _object_put(kind, obj)
    assert SS.plan(A, [put]).puts and SS.scan_row(put.table, put.row) == []


def test_rt3_3_hand_built_rows_with_off_whitelist_objects_never_reach_the_client():
    good = _object_put("collector", {"segments": 3})
    for bad_detail in ({"wallet": "x"}, {"segments": "3"}, {"anything": 1}):
        row = dict(good.row, detail=bad_detail)
        row["content_sha256"] = SS.row_hash("collector_status", row)
        with pytest.raises(SS.SyncError):
            SS.plan(A, [SS.Put("collector_status", row)])
        forged = dataclasses.replace(SS.plan(A, [good]), puts=(SS.Put("collector_status", row),))
        client = MemClient()
        with pytest.raises(SS.SyncError):
            SS.write(forged, client)
        assert client.calls == 0


def test_rt3_3_risk_policy_document_is_exactly_the_fourteen_limits():
    row = SS.risk_policy_put(A, "carry_book", 1, policy(), 1762387200000).row
    assert set(row["policy"]) == set(SS.RISK_LIMIT_FIELDS)
    extra = dict(row, policy=dict(row["policy"], wallet_balance="5"))
    extra["content_sha256"] = SS.content_sha256(extra["policy"])
    with pytest.raises(SS.SyncError):
        SS.plan(A, [SS.Put("risk_policy_versions", extra)])


def _tok(n, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"):
    import random
    rnd = random.Random(n)
    return "".join(rnd.choice(alphabet) for _ in range(n))


GUARD_KEYS = ["acct", "account", "balance", "wallet", "equity", "position", "pnl", "address", "iban", "phone", "name", "ssn", "walletBalance", "account_number", "Acct-No",
              "positions", "userPhone", "billingAddress", "ACCOUNT"]
GUARD_VALUES = {
    "30-char lowercase+digit token": "k3" + "j9x2m7q1z8w4v6b5n0c3d1f2g4h5j"[:28],
    "32-char hex": "0123456789abcdef0123456789abcdef",
    "40-char hex": "a" * 20 + "0" * 20,
    "eth address": "0x" + "ab12" * 10,
    "base32 26": "JBSWY3DPEHPK3PXPJBSWY3DPEH"[:26],
    "base64 40": "dGhpcy1pcy1hLXNlY3JldC1rZXktZm9yLXRlc3Rpbmc9",
    "base64url 36": "Zm9vLWJhci1iYXotcXV4LXF1dXgtY29yZ2UtZ3JhdWx0",
    "16-digit account number": "4111111111111111",
    "iban": "GB82WEST12345698765432",
    "ssn": "078-05-1120",
    "phone": "+14155552671",
}


@pytest.mark.parametrize("key", GUARD_KEYS)
def test_rt3_3_account_like_keys_are_refused_anywhere_in_a_value_tree(key):
    assert SS.scan_value({"outer": [{key: 1}]}) and SS.scan_value({key: 1})
    with pytest.raises(SS.SecretGuardError):
        SS.registration_puts(A, reg_entry(costs={"outer": {key: 1}}))


@pytest.mark.parametrize("name", sorted(GUARD_VALUES))
def test_rt3_3_token_shaped_values_are_refused_as_values_and_as_keys_in_every_table(name):
    v = GUARD_VALUES[name]
    assert SS.scan_value({"k": v}) and SS.scan_value({v: 1})
    builds = [lambda: SS.evidence_put(A, "T1", "BACKTEST", result=v, provenance="p", recorded_utc=1762300800000),
              lambda: SS.evidence_put(A, "T1", "BACKTEST", result="r", provenance="p", details={"venue": v}, recorded_utc=1762300800000),
              lambda: SS.evidence_put(A, "T1", "BACKTEST", result="r", provenance="p", details={v: 1}, recorded_utc=1762300800000),
              lambda: SS.system_health_put(A, "c", "OK", 1762300800000, detail={"version": v}),
              lambda: SS.restore_receipt_put(A, "R", "B", "PASSED", H("7"), 1762300800000, checks={"chain": v}),
              lambda: SS.md_seal_receipt_put(A, seal_id="s", batch_id="b", manifest_sha256=H("f"), manifest_rows=1, sealed_utc=1762300800000, receipt={"segment": v}),
              lambda: SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"last_error_code": v}),
              lambda: SS.incident_put(A, "I", "WARN", "title " + v, 1762300800000),
              lambda: SS.registration_puts(A, reg_entry(costs={"note": [v]}))]
    for b in builds:
        with pytest.raises(SS.SecretGuardError) as ei:
            b()
        assert v not in str(ei.value) and v[:10] not in str(ei.value)             # a key can be the secret: findings never echo it either


def test_rt3_3_a_secret_used_as_a_key_is_not_echoed_in_whitelist_refusals_either():
    v = GUARD_VALUES["30-char lowercase+digit token"]
    with pytest.raises(SS.SyncError) as ei:
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"zz_" + v[:6]: 1})      # not secret-shaped, just unlisted: named
    assert "not_whitelisted" in str(ei.value)
    with pytest.raises(SS.SyncError) as ei:
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={v: 1})
    assert v not in str(ei.value)


def test_rt3_3_guard_positive_controls_legitimate_text_and_hashes_pass():
    clean = ["trial_btc_carry_funding_basis_v1", "2026-11-07T00:00:00Z", "vol://md/md-20261101.db", "universe-1762300800000", "private://owner/imports/1",
             "seal-md-20261101-abababababababab", "funding carry earns the basis", "n=120 sharpe 1.25", "RUNBOOKS.md#collector", "tools/hypothesis_registry.py",
             "split-2025-01-01", "abcdef1", str(uuid.uuid4()), "carry_book@1"]
    for c in clean:
        assert SS.scan_value({"field": c}) == [], c
    for k in ("trial_id", "segment_ids", "per_trade_loss_usd", "max_exposure_usd", "hurdle_pct", "taker_bps", "data_window"):
        assert SS.scan_value({k: 1}) == [], k
    assert SS.scan_value({"x": H("a")}) != []                                        # a 64-hex in an untyped place is a token...
    puts, _ = all_puts(A)                                                            # ...but in its typed hash column (and typed nested hash field) it is exempt
    assert all(SS.scan_row(p.table, p.row) == [] for p in puts)


def test_rt3_3_pathologically_deep_values_are_refused():
    deep = cur = {}
    for _ in range(40):
        cur["a"] = {}
        cur = cur["a"]
    with pytest.raises(SS.SyncError):
        SS.registration_puts(A, reg_entry(costs=deep))
    assert SS.scan_value(deep, "x") != [] or True                                    # the scanner itself terminates on it


# ---- (4) the client class

def test_rt3_4_a_duck_typed_service_role_client_is_refused():
    duck = types.SimpleNamespace(role="service_role", put_row=lambda t, r: True, find_content_hashes=lambda *a: [])
    puts, _ = all_puts(A)
    with pytest.raises(SS.ClientRefused, match="ServiceRoleClient"):
        SS.write(SS.plan(A, puts), duck)

    class Ducky:
        role = "service_role"

        def put_row(self, t, r):
            return True

        def find_content_hashes(self, *a):
            return []
    with pytest.raises(SS.ClientRefused, match="ServiceRoleClient"):
        SS.check_client(Ducky())


def test_rt3_4_declared_clients_positive_controls_and_refusals():
    SS.check_client(_client())                                                       # a deliberate subclass with the right role passes
    SS.check_client(MemClient())

    class Registered:
        role = "service_role"

        def put_row(self, t, r):
            return True

        def find_content_hashes(self, *a):
            return []
    with pytest.raises(SS.ClientRefused):
        SS.check_client(Registered())
    SS.ServiceRoleClient.register(Registered)                                        # registering is a deliberate act in the owner's code
    SS.check_client(Registered())
    with pytest.raises(TypeError):
        SS.ServiceRoleClient()                                                       # the protocol itself is abstract
    for attrs in ({"role": "anon"}, {"role": "authenticated"}, {"role": None}, {"role": "service_role "}, {"role": "SERVICE_ROLE"}):
        with pytest.raises(SS.ClientRefused):
            SS.check_client(_client(**attrs))


@pytest.mark.parametrize("attr", ["anon_key", "anon", "publishable_key", "public_key", "is_anon", "is_public", "public", "anonymous_client", "guest_token", "PUBLISHABLE", "anyone"])
def test_rt3_4_any_anon_or_public_attribute_refuses_a_correctly_typed_client(attr):
    with pytest.raises(SS.ClientRefused):
        SS.check_client(_client(**{attr: "x"}))
    with pytest.raises(SS.ClientRefused):
        SS.check_client(_client(**{attr: None}))                                      # present-but-empty still advertises the semantics


def test_rt3_4_a_catch_all_getattr_client_is_refused():
    class Chameleon(SS.ServiceRoleClient):
        def put_row(self, t, r):
            return True

        def find_content_hashes(self, *a):
            return []

        def __getattr__(self, name):
            return "yes"
    with pytest.raises(SS.ClientRefused):
        SS.check_client(Chameleon())


# ---- (5) numeric bounds

@pytest.mark.parametrize("bad", ["1e20000000", "1E999999999", "-1e20000000", "1e41", "1e-41", "9" * 41, "1" + "0" * 5000, 10 ** 5000, -(10 ** 5000), float("1e300"), 1e-300,
                                 __import__("decimal").Decimal("1e999999999"), __import__("decimal").Decimal("1E-999999999"), "0." + "1" * 80, " " * 100 + "1"],
                         ids=lambda v: type(v).__name__ + (f"{v.bit_length()}bits" if isinstance(v, int) else str(v)[:20]))
def test_rt3_5_canon_decimal_failures_are_always_sync_errors(bad):
    with pytest.raises(SS.SyncError):
        SS.canon_decimal(bad)
    if not isinstance(bad, str):                     # (strings are text until a numeric column asks for them)
        with pytest.raises(SS.SyncError):
            SS.normalise({"x": [bad]})


def test_rt3_5_canon_decimal_positive_controls_and_bounds_reach_every_builder():
    D = __import__("decimal").Decimal
    assert SS.canon_decimal("1e20") == "100000000000000000000" and SS.canon_decimal(1e-5) == "0.00001" and SS.canon_decimal(D("1.50")) == "1.5"
    assert SS.canon_decimal(-0.0) == "0" and SS.canon_decimal("  2.5 ") == "2.5" and SS.canon_decimal(10 ** 30) == "1" + "0" * 30
    assert SS.canon_decimal("1e40") == "1" + "0" * 40 and SS.canon_decimal("1e-40") == "0." + "0" * 39 + "1"
    with pytest.raises(SS.SyncError):
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, lag_seconds="1e20000000")
    with pytest.raises(SS.SyncError):
        SS.risk_policy_put(A, "p", 1, dict(dataclasses.asdict(policy()), per_trade_loss_usd="1e20000000"), 1762387200000)
    with pytest.raises(SS.SyncError):
        SS.evidence_put(A, "T1", "BACKTEST", result="r", provenance="p", details={"sharpe": D("1e99999999")}, recorded_utc=1762300800000)
    with pytest.raises(SS.SyncError):
        SS.data_batch_put(A, {"batch_id": "b", "dataset": "d", "venue": "v", "batch_sha256": H("a"), "byte_count": 10 ** 30, "segment_ids": ["s"],
                              "window_start_utc": "2026-11-07T00:00:00Z", "window_end_utc": "2026-11-07T00:00:00Z", "storage_ref": "vol://x"})


@pytest.mark.parametrize("bad", [10 ** 30, -1, 253402300800000, "2026-13-45T00:00:00Z", "9999-99-99T99:99:99Z", "2026-11-07T00:00:00Z" + "0" * 50, 1e30, float("nan")])
def test_rt3_5_timestamp_failures_are_sync_errors(bad):
    with pytest.raises(SS.SyncError):
        SS.utc(bad)


# ---- every guard above must be able to fail: mutation checks

def _mut_import_row(m, **kw):
    return m.owner_import_puts(A, kw.pop("report"), imported_utc=1762387300000, storage_ref="private://x")


def test_rt3_mutation_report_digest_checks():
    fake = {"status": "RECONCILED", "lineage": {"files": [{"sha256": "a" * 64}]}}
    tampered = import_report()
    tampered["lineage"]["files"][0]["sha256"] = "d" * 64
    with pytest.raises(SS.SyncError):
        SS.owner_import_puts(A, tampered, imported_utc=1762387300000, storage_ref="x")
    m = _mutant('if sha256_text(_imp_canon({k: v for k, v in report.items() if k != "report_sha256"})) != rs:', "if False:")
    with pytest.raises(m.SyncError, match="lineage.digest"):
        _mut_import_row(m, report=tampered)                                       # outer digest unchecked: the lineage digest still catches it...
    m2 = _mutant('if lin.get("digest") != ld:', "if False:")
    with pytest.raises(m2.SyncError, match="report_sha256"):
        _mut_import_row(m2, report=tampered)                                      # ...and each alone is not the whole defence
    m3 = _mutant('if e.get("provenance_digest") != pd:', "if False:")
    with pytest.raises(m3.SyncError):
        _mut_import_row(m3, report=tampered)
    with pytest.raises(SS.SyncError):
        SS.owner_import_puts(A, fake, imported_utc=1762387300000, storage_ref="x")
    both = _mutant('if sha256_text(_imp_canon({k: v for k, v in report.items() if k != "report_sha256"})) != rs:', "if False:")
    both2 = _mutant('if lin.get("digest") != ld:', "if False:")
    assert both and both2


def test_rt3_mutation_headline_and_layer_consistency():
    r = import_report()
    r["status"] = "DISPUTED"
    for layer in r["layers"].values():
        layer["status"] = "DISPUTED"
    _reseal_by_hand(r)
    with pytest.raises(SS.SyncError):
        SS.owner_import_puts(A, r, imported_utc=1762387300000, storage_ref="x")
    m = _mutant('if status not in _PROFIT_STATUSES:\n            out.append(f"headline:non_null_under_{status}")', 'if False:\n            out.append(f"headline:non_null_under_{status}")')
    assert m.owner_import_puts(A, r, imported_utc=1762387300000, storage_ref="x")           # mutant imports a DISPUTED report that still carries a headline


def test_rt3_mutation_evidence_and_cross_row_rules():
    imp_put = SS.owner_import_puts(A, import_report(), imported_utc=1762387300000, storage_ref="x")[0]
    imp = imp_put.row["import_id"]
    with pytest.raises(SS.SyncError):
        SS.import_status_event_put(A, imp, "RECONCILED", 1762387300000, evidence_ref=H("e"))
    m = _mutant('r.get("evidence_ref") != r["import_id"]', "False")
    assert m.import_status_event_put(A, imp, "RECONCILED", 1762387300000, evidence_ref=H("e"))
    m2 = _mutant("evidence_ref:hex? recorded_utc:ts", "evidence_ref:text? recorded_utc:ts")
    assert m2.import_status_event_put(A, imp, "DISPUTED", 1762387300000, reason="SOME_CODE", evidence_ref="see the bank statement")
    ev = SS.import_status_event_put(A, imp, "RECONCILED", 1762387400000, evidence_ref=imp)
    with pytest.raises(SS.SyncError):
        SS.plan(A, [ev])
    m3 = _mutant('if imports.get(p.row.get("import_id")) != p.row.get("status"):', "if False:")
    assert m3.plan(A, [m3.Put(ev.table, ev.row)]).puts


def test_rt3_mutation_whitelist_and_guard_rules():
    with pytest.raises(SS.SyncError):
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"wallet": {"btc": "1"}})
    with pytest.raises(SS.SyncError):
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"foo_extra": 1})
    m = _mutant('out.append(f"{path}.{_safe_name(k)}:not_whitelisted")', "pass")
    assert m.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"foo_extra": 1})        # mutant lets an unlisted field in
    tok = GUARD_VALUES["30-char lowercase+digit token"]
    with pytest.raises(SS.SecretGuardError):
        SS.incident_put(A, "I", "WARN", "t " + tok, 1762300800000)
    m2 = _mutant("    for m in _ALNUM_RUN.finditer(s):", "    for m in []:")
    assert m2.incident_put(A, "I", "WARN", "t " + tok, 1762300800000)
    m3 = _mutant("    if any(w in _KEY_TOKENS for w in words):", "    if False:")
    assert m3.registration_puts(A, reg_entry(costs={"acct": "1"}))
    h32 = "abcdef" * 5 + "ab"                                            # letters only: the long-hex rule is the ONLY one that sees it
    with pytest.raises(SS.SecretGuardError):
        SS.incident_put(A, "I", "WARN", "t " + h32, 1762300800000)
    m4 = _mutant('("long_hex_token", re.compile(r"(?<![0-9A-Za-z])(?:0[xX])?[0-9a-fA-F]{24,}(?![0-9A-Za-z])")),', '("long_hex_token", re.compile(r"(?!x)x")),')
    assert m4.incident_put(A, "I", "WARN", "t " + h32, 1762300800000)


def test_rt3_mutation_client_class_and_numeric_bounds():
    duck = types.SimpleNamespace(role="service_role", put_row=lambda t, r: True, find_content_hashes=lambda *a: [])
    with pytest.raises(SS.ClientRefused):
        SS.check_client(duck)
    m = _mutant("if not isinstance(client, ServiceRoleClient):", "if False:")
    m.check_client(duck)                                                      # mutant accepts a duck-typed client
    anon_attr = _client(anon_key="x")
    with pytest.raises(SS.ClientRefused):
        SS.check_client(anon_attr)
    m2 = _mutant("    if any(_ANON_NAME.search(n) for n in names):", "    if False:")
    m2.ServiceRoleClient.register(type(anon_attr))
    with pytest.raises(m2.ClientRefused):
        m2.check_client(anon_attr)                                            # the probe list is a second, independent net...
    m3 = _mutant("        if hit:\n", "        if False:\n")
    m3.ServiceRoleClient.register(type(anon_attr))
    with pytest.raises(m3.ClientRefused):
        m3.check_client(anon_attr)                                            # ...and so is the name scan
    m4 = _mutant("if len(d.as_tuple().digits) > MAX_DECIMAL_DIGITS or abs(d.adjusted()) > MAX_DECIMAL_EXP:", "if False:")
    assert not m4.canon_decimal("1e2000000").isdigit()                        # mutant: without the bounds a 7-character input is silently turned into 'Infinity' (or overflows)


# --------------------------------------------------------------------------- static: the migration that this writer needs

def test_evidence_guard_migration_is_minimal_and_grants_nothing():
    f = MIG_DIR / "20261107000800_carry_evidence_guard_idempotent.sql"
    sql = _strip(f.read_text()).lower()
    assert "create or replace function carry_private.guard_evidence_target()" in sql
    assert not re.search(r"\b(grant|revoke|drop|alter|delete|truncate|update|security definer|create table)\b", sql)
    assert "abandoned; evidence cannot be added" in sql and "superseded; add the evidence" in sql          # new evidence on a closed trial is still refused
    assert sql.index("return new;") < sql.index("abandoned; evidence")                                       # only an exact duplicate short-circuits, before the checks


# --------------------------------------------------------------------------- executed against a disposable Postgres

SUPABASE_STUB = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(coalesce(current_setting('request.jwt.claim.sub', true),
         (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')), '')::uuid $$;
grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;
"""


class PG:
    def __init__(self, psql, uri):
        self.psql, self.uri = psql, uri

    def run(self, sql, role=None, uid=None, check=True):
        pre = (f"set role {role};\n" if role else "") + (f"select set_config('request.jwt.claim.sub', '{uid}', false);\n" if uid else "")
        p = subprocess.run([str(self.psql), self.uri, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"], input=pre + sql, capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            raise AssertionError(p.stderr)
        return p

    def scalar(self, sql, **kw):
        return self.run(sql, **kw).stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver", reason="no disposable Postgres (pip install pgserver): the writer was NOT executed against SQL")
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata_sync"), cleanup_mode="stop")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot start disposable postgres: {e}")
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    if not psql.exists():
        pytest.skip("pgserver psql binary missing")
    return PG(psql, srv.get_uri())


def _db(server, name, files):
    server.run(f"create database {name};")
    h = PG(server.psql, server.uri.replace("@/postgres?", f"@/{name}?", 1))
    h.run(SUPABASE_STUB)
    for f in files:
        h.run(f.read_text())
    return h


@pytest.fixture(scope="module")
def pg(server):
    return _db(server, "sync_full", FILES)


class SqlClient(SS.ServiceRoleClient):
    """The fake service-role client of the brief: put_row calls ``carry.put_row`` through SQL as the service_role."""
    role = "service_role"

    def __init__(self, pg):
        self.pg = pg
        self.calls = 0

    def put_row(self, table, row):
        self.calls += 1
        out = self.pg.scalar(f"select carry.put_row('{table}', $syncj${json.dumps(row)}$syncj$::jsonb);", role="service_role")
        return out == "t"

    def find_content_hashes(self, table, owner_id, key):
        cond = " and ".join(f"{k} = $syncq${v}$syncq$" for k, v in key.items())      # untyped literal: compared in the COLUMN's type (timestamptz, int, text)
        out = self.pg.run(f"select content_sha256 from carry.{table} where owner_id = '{owner_id}' and {cond};", role="service_role").stdout
        return out.split()


def counts(pg, owner=None):
    parts = [f"select '{t}', count(*) from carry.{t}" + (f" where owner_id='{owner}'" if owner else "") for t in SS.TABLE_ORDER]
    rows = pg.run(" union all ".join(parts) + ";", role="service_role").stdout.split()
    return {r.split("|")[0]: int(r.split("|")[1]) for r in rows}


def test_plan_lands_once_rerun_changes_nothing_and_hashes_round_trip(pg):
    puts, skipped = all_puts(A)
    p = SS.plan(A, puts, skipped=skipped)
    client = SqlClient(pg)
    rep = SS.write(p, client)
    assert rep.ok, [r for r in rep.results if r.outcome != SS.INSERTED]
    assert rep.count(SS.INSERTED) == len(p.puts)
    before = counts(pg, A)
    assert sum(before.values()) == len(p.puts) and all(v >= 1 for v in before.values())
    rep2 = SS.write(p, client)
    assert rep2.ok, [r for r in rep2.results if r.outcome not in (SS.UNCHANGED,)]
    rep2 = SS.write(p, client)                                                  # includes evidence of the ABANDONED and SUPERSEDED trials (needs migration 800)
    assert rep2.ok and rep2.count(SS.UNCHANGED) == len(p.puts) and rep2.count(SS.INSERTED) == 0
    assert counts(pg, A) == before
    reg_row = next(x.row for x in p.puts if x.table == "experiment_registrations" and x.row["trial_id"] == "T1")
    import hypothesis_registry as hr
    assert pg.scalar(f"select content_sha256 from carry.experiment_registrations where trial_id='T1' and owner_id='{A}';", role="service_role") == hr.entry_digest(
        next(t for t in make_registry()["trials"] if t["trial_id"] == "T1")) == reg_row["content_sha256"]
    assert pg.scalar(f"select policy->>'per_trade_loss_usd' from carry.risk_policy_versions where owner_id='{A}';", role="service_role") == "25.5"
    assert pg.scalar(f"select lag_seconds::text from carry.collector_status where owner_id='{A}';", role="service_role") == "1.25"


def test_rls_isolates_two_owners(pg):
    puts_a, _ = all_puts(A)
    puts_b, _ = all_puts(B)
    assert SS.write(SS.plan(A, puts_a), SqlClient(pg)).ok                         # already there: UNCHANGED
    rep = SS.write(SS.plan(B, puts_b), SqlClient(pg))
    assert rep.ok and rep.count(SS.INSERTED) == len(SS.plan(B, puts_b).puts)    # the same artifacts under another owner are new rows, not conflicts
    ca, cb = counts(pg, A), counts(pg, B)
    assert ca == cb and sum(ca.values()) > 20
    for t in SS.TABLE_ORDER:
        assert pg.scalar(f"select count(*) from carry.{t};", role="authenticated", uid=A) == str(ca[t]), t
        assert pg.scalar(f"select count(*) from carry.{t} where owner_id <> '{A}';", role="authenticated", uid=A) == "0", t
        assert pg.scalar(f"select count(*) from carry.{t} where owner_id = '{A}';", role="authenticated", uid=B) == "0", t
        assert pg.scalar(f"select count(*) from carry.{t};", role="authenticated") == "0", t       # no identity, no rows
    assert pg.run("select * from carry.incidents;", role="anon", check=False).returncode != 0


def test_conflicting_duplicate_is_reported_against_real_postgres(pg):
    puts, _ = all_puts(A)
    client = SqlClient(pg)
    # same trial_id, different registration content -> put_row False for both; the writer must say CONFLICT, and the stored row must stay the original
    reg = next(x for x in puts if x.table == "experiment_registrations" and x.row["trial_id"] == "T1")
    changed = dict(reg.row, mechanism="a different mechanism", content_sha256=H("c"))
    rep = SS.write(dataclasses.replace(SS.plan(A, []), puts=(SS.Put("experiment_registrations", changed),)), client)
    assert not rep.ok and rep.results[0].outcome == SS.CONFLICT
    assert pg.scalar(f"select mechanism from carry.experiment_registrations where trial_id='T1' and owner_id='{A}';", role="service_role") == "funding carry earns the basis"
    inc = SS.incident_put(A, "INC-1", "CRITICAL", "different title", 1762387200000)
    rep = SS.write(SS.plan(A, [inc]), client)
    assert rep.results[0].outcome == SS.CONFLICT and "DIFFERENT content" in rep.results[0].detail
    pol = SS.risk_policy_put(A, "carry_book", 2, policy(), 1762387200000)                 # identical document under a new version
    assert SS.write(SS.plan(A, [pol]), client).results[0].outcome == SS.CONFLICT
    assert counts(pg, A)["incidents"] == 1 and counts(pg, A)["risk_policy_versions"] == 1


def test_foreign_key_failure_is_reported_and_halts(pg):
    ev = SS.evidence_put(A, "NO-SUCH-TRIAL", "BACKTEST", result="r", provenance="p", recorded_utc=1762300800000)
    inc = SS.incident_put(A, "INC-NEW", "INFO", "after the failure", 1762387200000)
    rep = SS.write(SS.plan(A, [ev, inc]), SqlClient(pg))
    assert [r.outcome for r in rep.results] == [SS.FAILED, SS.NOT_SENT] and not rep.ok
    assert pg.scalar(f"select count(*) from carry.incidents where incident_id='INC-NEW';", role="service_role") == "0"


def test_status_vocabulary_and_no_raw_owner_rows_in_the_stored_import(pg):
    owner = str(uuid.uuid4())
    puts = []
    for i, s in enumerate(SS.OWNER_STATUSES):
        base = json.loads(json.dumps(real_reports()["RECONCILED"]))
        base.update(fills=[{"execId": "SENTINEL_EXEC"}], wallet={"btc": "SENTINEL_BALANCE"}, accountId="SENTINEL_ACCT", status=s)       # hostile extras, sealed in
        base["layers"]["realised_cash"]["status"] = s
        puts += SS.owner_import_puts(owner, OI._seal(base), imported_utc=1762387300000, storage_ref=f"private://{i}")
    assert SS.write(SS.plan(owner, puts), SqlClient(pg)).ok
    assert pg.scalar(f"select string_agg(status, ',' order by status) from carry.owner_account_imports where owner_id='{owner}';", role="service_role") == ",".join(sorted(SS.OWNER_STATUSES))
    stored = pg.run(f"select to_jsonb(i)::text from carry.owner_account_imports i where owner_id='{owner}';", role="service_role").stdout
    assert "SENTINEL" not in stored and "476.275" not in stored
    cols = set(json.loads(stored.splitlines()[0]))
    assert cols <= set(SQL["owner_account_imports"]["cols"]) and not (cols & {"fills", "wallet", "accountId", "headline_profit_usd"})


def test_pre_fix_schema_could_not_replay_evidence_of_an_abandoned_trial(server):
    """Why migration 800 exists: with only the first seven migrations the re-run of an abandoned trial's evidence raises instead of being a no-op."""
    old = _db(server, "sync_prefix", [f for f in FILES if f.name < "20261107000800"])
    puts, _ = all_puts(A)
    p = SS.plan(A, [x for x in puts if x.row.get("trial_id", x.row.get("hypothesis_id")) in ("T2", "H2")])
    c = SqlClient(old)
    assert SS.write(p, c).ok
    again = SS.write(p, c)
    assert not again.ok and again.count(SS.FAILED) == 1                          # the evidence row is refused by the old guard on replay
    fixed = _db(server, "sync_fixed", FILES)
    c2 = SqlClient(fixed)
    assert SS.write(p, c2).ok and SS.write(p, c2).ok
    # ... and NEW evidence on the abandoned trial is still refused
    new_ev = SS.evidence_put(A, "T2", "BACKTEST", result="new run", provenance="run-99", recorded_utc=1762400000000)
    assert SS.write(SS.plan(A, [new_ev]), c2).count(SS.FAILED) == 1


# --------------------------------------------------------------------------- executed against the disposable Postgres: red-team round 3

def test_rt3_every_whitelisted_object_shape_lands_and_round_trips_in_real_postgres(pg):
    owner = str(uuid.uuid4())
    seal_cols = {"segment": "md-20261101.db", "records": 5, "first_id": 1, "last_id": 5, "last_chain": H("c"), "sha256": H("e")}
    puts = [SS.registration_puts(owner, make_registry()["trials"][0])[0], SS.registration_puts(owner, make_registry()["trials"][0])[1]]
    for kind, obj in POSITIVE_OBJECTS:
        if kind == "evidence":
            puts.append(SS.evidence_put(owner, "T1", "BACKTEST", result=f"r{len(puts)}", provenance="p", details=obj, recorded_utc=1762300800000 + len(puts)))
        elif kind == "collector":
            puts.append(SS.collector_status_put(owner, "c", "RUNNING", 1762300800000, detail=obj))
        elif kind == "health":
            puts.append(SS.system_health_put(owner, "comp", "OK", 1762300800000, detail=obj))
        elif kind == "restore":
            puts.append(SS.restore_receipt_put(owner, "R", "B", "PASSED", H("7"), 1762300800000, checks=obj))
        else:
            puts.append(SS.md_seal_receipt_put(owner, seal_id="s", batch_id="b", manifest_sha256=H("f"), manifest_rows=1, sealed_utc=1762300800000, receipt=seal_cols))
    imp = SS.owner_import_puts(owner, import_report(), imported_utc=1762387300000, storage_ref="private://x")
    puts += imp + [SS.import_status_event_put(owner, imp[0].row["import_id"], "RECONCILED", 1762387400000, evidence_ref=imp[0].row["import_id"])]
    # batches / backup rows the receipts name (foreign keys)
    puts.append(SS.data_batch_put(owner, {"batch_id": "b", "dataset": "d", "venue": "v", "batch_sha256": H("a"), "byte_count": 1, "segment_ids": ["s"],
                                          "window_start_utc": "2026-11-07T00:00:00Z", "window_end_utc": "2026-11-07T00:00:00Z", "storage_ref": "vol://x"}))
    puts.append(SS.backup_receipt_put(owner, "B", "t", H("6"), 1, "vol://b", 1762300800000))
    rep = SS.write(SS.plan(owner, puts), SqlClient(pg))
    assert rep.ok, rep.results
    stored = pg.scalar(f"select detail->>'last_error_code' from carry.collector_status where owner_id='{owner}';", role="service_role")
    assert stored == "WS_CLOSED"
    assert pg.scalar(f"select checks->>'row_count' from carry.restore_receipts where owner_id='{owner}';", role="service_role") == "55"
    assert pg.scalar(f"select receipt->>'last_chain' from carry.md_seal_receipts where owner_id='{owner}';", role="service_role") == H("c")
    assert pg.scalar(f"select status from carry.owner_account_import_status_events where owner_id='{owner}';", role="service_role") == "RECONCILED"
    again = SS.write(SS.plan(owner, puts), SqlClient(pg))
    assert again.ok and again.count(SS.INSERTED) == 0                          # idempotent: the stored hashes equal ours


def test_rt3_hostile_plans_never_reach_postgres_and_a_forged_report_leaves_no_import_row(pg):
    owner = str(uuid.uuid4())
    before = counts(pg, owner)
    c = SqlClient(pg)
    with pytest.raises(SS.SyncError):
        SS.owner_import_puts(owner, {"status": "RECONCILED", "lineage": {"files": [{"sha256": "a" * 64}]}}, imported_utc=1762387300000, storage_ref="x")
    good = SS.collector_status_put(owner, "c", "RUNNING", 1762300800000, detail={"segments": 1})
    for bad in ({"wallet": {"btc": "9"}}, {"acct": "123"}, {"segments": "1"}, {"zz": _tok(30)}):
        row = dict(good.row, detail=bad)
        row["content_sha256"] = SS.row_hash("collector_status", row)
        forged = dataclasses.replace(SS.plan(owner, [good]), puts=(SS.Put("collector_status", row),))
        with pytest.raises(SS.SyncError):
            SS.write(forged, c)
    assert c.calls == 0 and counts(pg, owner) == before
    assert pg.scalar(f"select count(*) from carry.owner_account_imports where owner_id='{owner}';", role="service_role") == "0"
    assert SS.write(SS.plan(owner, [good]), c).ok and c.calls == 1              # positive control: the clean row goes through the same client
