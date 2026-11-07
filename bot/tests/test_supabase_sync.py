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
sys.path.insert(0, str(BOT))

import supabase_sync as SS  # noqa: E402

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


def import_report():
    return {"status": "RECONCILED", "lineage": {"files": [{"role": "linear_execution", "sha256": H("1"), "file": "EXEC.jsonl"}, {"role": "wallet_closing", "sha256": H("2"), "file": "W1.json"}]},
            "ledger": {"window": {"opening_ts_ms": 1762300800000, "closing_ts_ms": 1762387200000}}}


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
    puts.append(SS.import_status_event_put(owner, imp_id, "RECONCILED", 1762387400000, evidence_ref="recon-1"))
    puts.append(SS.import_status_event_put(owner, imp_id, "STALE", 1762387500000, reason="mark older than 6h"))
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
        rep = dict(import_report(), status=s)
        row = SS.owner_import_puts(A, rep, imported_utc=1762387300000, storage_ref="private://x")[0].row
        assert row["status"] == s and row["import_id"] == row["content_sha256"]
    for bad in ("OK", "verified", "LIVE", "", None, "RECONCILED "):
        with pytest.raises(SS.SyncError):
            SS.owner_import_puts(A, dict(import_report(), status=bad), imported_utc=1762387300000, storage_ref="private://x")
        with pytest.raises(SS.SyncError):
            SS.import_status_event_put(A, H("a"), bad, 1762387300000, reason="r", evidence_ref="e")
    with pytest.raises(SS.SyncError):
        SS.import_status_event_put(A, H("a"), "RECONCILED", 1762387300000)                 # needs evidence_ref
    with pytest.raises(SS.SyncError):
        SS.import_status_event_put(A, H("a"), "DISPUTED", 1762387300000)                   # needs a reason
    assert SS.import_status_event_put(A, H("a"), "DISPUTED", 1762387300000, reason="records contradict").row["status"] == "DISPUTED"


def test_import_report_row_holds_no_raw_owner_data():
    hostile = dict(import_report())
    hostile.update({
        "fills": [{"execId": "SENTINEL_EXEC_1", "execQty": "0.5", "uid": "SENTINEL_UID"}], "wallet": {"btc": "SENTINEL_BALANCE_7777", "usdt": 12345.678},
        "headline_profit_usd": 4242.42, "accountId": "SENTINEL_ACCT", "layers": {"realised_cash": {"figures": {"funding_net_usd": 77.77}}},
        "flags": [{"detail": "SENTINEL_FLAG"}], "import_id": "imp_SENTINEL"})
    hostile["lineage"] = dict(hostile["lineage"], marks=[{"price": "SENTINEL_MARK"}])
    row = SS.owner_import_puts(A, hostile, imported_utc=1762387300000, storage_ref="private://x")[0].row
    blob = json.dumps(row)
    assert "SENTINEL" not in blob and "4242" not in blob and "12345" not in blob and "77.77" not in blob
    assert set(row) <= {"owner_id", "import_id", "source_sha256", "kind", "period_start_utc", "period_end_utc", "row_count", "storage_ref", "status", "imported_utc",
                        "source", "schema_version", "content_sha256"}
    assert row["row_count"] == 2                         # the count of FILES hashed, not any record
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
    ok = SS.collector_status_put(A, "md-recorder", "RUNNING", 1762300800000, detail={"segments": 3, "note": "lag back to 1.2s", "hash": H("a"), "code": "abcdef1", "ref": "vol://md/x.db",
                                                                                   "window": "2026-11-07T00:00:00Z", "version": "1.2.3", "id": "trial_btc_carry-v1"})
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

class MemClient:
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
        SS.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"uid": "7"})
    m = _mutant("if n in _KEY_EXACT:", "if False:")
    assert m.collector_status_put(A, "c", "RUNNING", 1762300800000, detail={"uid": "7"})     # mutant lets an account id field through: the field-name tests would fail on it


def test_mutation_secret_guard_value_shapes():
    s = _shapes()["credentialed url"]
    with pytest.raises(SS.SecretGuardError):
        SS.incident_put(A, "I", "WARN", s, 1762300800000)
    m = _mutant("    hits = [name for name, rx in _VALUE_RULES if rx.search(s)]", "    hits = []")
    assert m.incident_put(A, "I", "WARN", s, 1762300800000)                                  # mutant lets a credentialed URL through: the hostile table would fail on it


def test_mutation_client_role_check():
    anon = types.SimpleNamespace(role="anon", put_row=lambda t, r: True, find_content_hashes=lambda *a: [])
    with pytest.raises(SS.ClientRefused):
        SS.check_client(anon)
    m = _mutant('if role != "service_role":', "if False:")
    m.check_client(anon)                                                  # mutant accepts an anon client: the refusal tests would fail on it


def test_mutation_conflict_detection():
    puts, _ = all_puts(A)
    client = MemClient()
    SS.write(SS.plan(A, puts), client)
    inc = SS.incident_put(A, "INC-1", "CRITICAL", "different", 1762387200000)
    assert SS.write(SS.plan(A, [inc]), client).results[0].outcome == SS.CONFLICT
    m = _mutant('if put.row["content_sha256"] in stored:', "if True:")
    assert m.write(m.plan(A, [m.Put(inc.table, inc.row)]), client).results[0].outcome == "UNCHANGED"      # mutant hides the conflict: the conflict tests would fail on it


def test_mutation_owner_approval_rule():
    with pytest.raises(SS.SyncError):
        SS.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000)
    m = _mutant('if table == "approval_records" and r["approved_by_owner"]:', 'if table == "approval_records" and False:')
    assert m.approval_put(A, "AP", "risk_policy_version", "p@1", True, 1762387300000)


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


class SqlClient:
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
    hostile = dict(import_report(), fills=[{"execId": "SENTINEL_EXEC"}], wallet={"btc": "SENTINEL_BALANCE"}, accountId="SENTINEL_ACCT", headline_profit_usd=4242.42)
    puts = []
    for i, s in enumerate(SS.OWNER_STATUSES):
        rep = dict(hostile, status=s, lineage={"files": [{"sha256": SS.sha256_text(f"f{i}")}]})
        puts += SS.owner_import_puts(owner, rep, imported_utc=1762387300000, storage_ref=f"private://{i}")
    assert SS.write(SS.plan(owner, puts), SqlClient(pg)).ok
    assert pg.scalar(f"select string_agg(status, ',' order by status) from carry.owner_account_imports where owner_id='{owner}';", role="service_role") == ",".join(sorted(SS.OWNER_STATUSES))
    stored = pg.run(f"select to_jsonb(i)::text from carry.owner_account_imports i where owner_id='{owner}';", role="service_role").stdout
    assert "SENTINEL" not in stored and "4242" not in stored
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
