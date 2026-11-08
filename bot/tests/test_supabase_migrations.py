"""Supabase/Postgres migrations for the carry programme's structured application data.

Two layers:
  * STATIC tests (always run): parse supabase/migrations/*.sql.
  * EXECUTED tests: apply every migration in order into a DISPOSABLE local Postgres (the `pgserver` wheel,
    a private data dir under pytest's tmp path, unix-socket only) and exercise RLS, grants, triggers and
    constraints. They SKIP cleanly when `pgserver` is not importable, UNLESS `REQUIRE_PG=1` (the CI job
    `supabase-migrations` sets it): then a missing pgserver, a Postgres that will not start or a missing psql is a
    FAILURE, so the guarantees cannot silently stop being executed. They never contact any network service,
    any Supabase project, or any existing database. Supabase's roles and auth.uid() are stubbed locally.
"""
import hashlib
import importlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

MIG_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
FILES = sorted(MIG_DIR.glob("*.sql"))

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


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def all_sql() -> str:
    return "\n".join(strip_comments(f.read_text()) for f in FILES)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --------------------------------------------------------------------------- static

def test_migrations_present_ordered_and_timestamped():
    assert len(FILES) >= 7
    names = [f.name for f in FILES]
    assert all(re.fullmatch(r"20261107\d{6}_[a-z0-9_]+\.sql", n) for n in names), names
    assert names == sorted(names) and len(set(n.split("_")[0] for n in names)) == len(names)
    assert names[0].startswith("20261107000100_")


def test_every_table_has_pk_owner_provenance_baseline_and_retention():
    sql = all_sql()
    tables = re.findall(r"create table if not exists (carry\.\w+) \((.*?)\n\);", sql, flags=re.S)
    assert len(tables) >= 25
    for name, body in tables:
        assert re.search(r"\bprimary key\b", body), name
        assert "owner_id uuid not null" in body, name
        for col in ("source text not null", "schema_version integer not null default 1",
                    "content_sha256 carry.sha256_hex not null", "created_at timestamptz not null default now()"):
            assert col in body, (name, col)
        assert "unique (owner_id, content_sha256)" in body or "unique (owner_id, import_id)" in body, name
        assert f"select carry_private.secure_table('{name}');" in sql, name
    # no create table outside the carry schema, none without the idempotent form
    assert len(re.findall(r"\bcreate table\b", sql)) == len(tables)


def test_secure_table_baseline_text():
    f = strip_comments((MIG_DIR / "20261107000100_carry_foundation.sql").read_text())
    for needle in ("enable row level security", "force row level security",
                   "revoke all on table %s from public, anon, authenticated, service_role",
                   "to authenticated using (owner_id = (select auth.uid()))",
                   "as restrictive for all to anon using (false)",
                   "before update or delete or truncate"):
        assert needle in f, needle
    assert "grant select, insert on table %s to service_role" in f  # never update/delete


def test_no_forbidden_statements_or_secrets():
    sql = all_sql()
    low = sql.lower()
    assert not re.search(r"\bdrop\b", low)
    assert not re.search(r"\btruncate\s+(table\s+)?(only\s+)?[a-z_\"]*\.?[a-z_\"]+\s*;", low)
    assert not re.search(r"\bdelete\s+from\b", low)
    assert not re.search(r"grant\s[^;]*\bto\b[^;]*\b(anon|public)\b", low)
    assert not re.search(r"grant\s[^;]*\b(update|delete|truncate|all)\b[^;]*\bto\b", low)
    assert "disable row level security" not in low and "no force row level security" not in low
    assert "bypassrls" not in low and "security definer" not in low
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}|sb_secret_|service_role_key|-----BEGIN", sql, flags=re.I)
    assert "create schema" in low and "supabase_realtime" not in low


def test_vocabularies_match_sources():
    sql = all_sql()
    for v in ("VERIFIED", "RECONCILED", "ESTIMATED", "PROVISIONAL", "STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE"):
        assert sql.count(f"'{v}'") >= 3, v
    m = re.search(r"EVIDENCE_LEVELS = \((.*?)\)", (Path(__file__).resolve().parents[1] / "tools" / "hypothesis_registry.py").read_text(), re.S)
    levels = re.findall(r'"(\w+)"', m.group(1))
    assert len(levels) == 7 and all(f"'{lv}'" in sql for lv in levels)
    m = re.search(r"REQUIRED_REGISTRATION = \((.*?)\)", (Path(__file__).resolve().parents[1] / "tools" / "hypothesis_registry.py").read_text(), re.S)
    reg = re.search(r"create table if not exists carry\.experiment_registrations \((.*?)\n\);", sql, re.S).group(1)
    for f in re.findall(r'"(\w+)"', m.group(1)):
        assert re.search(rf"^\s*{f} ", reg, re.M), f
    assert "'LIVE'" not in sql and "approved_by_owner boolean not null," in sql
    assert not re.search(r"approved_by_owner[^,\n]*default", sql)


# --------------------------------------------------------------------------- executed

def _unavailable(reason, require):
    """No disposable Postgres: SKIP, or FAIL when the environment promised one (REQUIRE_PG=1)."""
    if require:
        pytest.fail(f"REQUIRE_PG=1 but the executed Supabase tests cannot run: {reason}", pytrace=False)
    pytest.skip(reason)


def _pgserver(require, importer=importlib.import_module):
    try:
        return importer("pgserver")
    except ImportError:
        _unavailable("no disposable Postgres (pip install pgserver): RLS not executed", require)


def test_require_pg_turns_a_missing_pgserver_into_a_failure_not_a_skip():
    def missing(name):
        raise ImportError(name)

    with pytest.raises(pytest.fail.Exception, match="REQUIRE_PG=1"):
        _pgserver(True, missing)
    with pytest.raises(pytest.skip.Exception):
        _pgserver(False, missing)                       # positive control: without the promise it still skips
    sentinel = object()
    assert _pgserver(True, lambda name: sentinel) is sentinel
    with pytest.raises(pytest.fail.Exception):
        _unavailable("postgres will not start", True)


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    require = os.environ.get("REQUIRE_PG") == "1"
    pgserver = _pgserver(require)
    d = tmp_path_factory.mktemp("pgdata")
    try:
        srv = pgserver.get_server(d, cleanup_mode="stop")
    except Exception as e:  # pragma: no cover
        _unavailable(f"cannot start disposable postgres: {e}", require)
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    if not psql.exists():
        _unavailable("pgserver psql binary missing", require)
    uri = srv.get_uri()

    class PG:
        def run(self, sql, role=None, uid=None, check=True):
            pre = ""
            if role:
                pre += f"set role {role};\n"
            if uid:
                pre += f"select set_config('request.jwt.claim.sub', '{uid}', false);\n"
            p = subprocess.run([str(psql), uri, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"], input=pre + sql,
                               capture_output=True, text=True, timeout=120)
            if check and p.returncode != 0:
                raise AssertionError(p.stderr)
            return p

        def fails(self, sql, match, **kw):
            p = self.run(sql, check=False, **kw)
            assert p.returncode != 0, f"expected failure: {sql}"
            assert re.search(match, p.stderr, re.I), p.stderr
            return p

        def scalar(self, sql, **kw):
            return self.run(sql, **kw).stdout.strip().splitlines()[-1]

    h = PG()
    h.run(SUPABASE_STUB)
    for f in FILES:
        h.run(f.read_text())
    return h


A, B = str(uuid.uuid4()), str(uuid.uuid4())


def put(pg, table, row):
    return pg.scalar(f"select carry.put_row('{table}', $j${json.dumps(row)}$j$::jsonb);", role="service_role")


def hyp(owner, hid="H1"):
    return {"owner_id": owner, "hypothesis_id": hid, "mechanism": "funding", "family": "carry",
            "statement": "s", "source": "test", "content_sha256": sha(owner + hid)}


def reg(owner, tid="T1", hid="H1", **over):
    r = {"owner_id": owner, "trial_id": tid, "hypothesis_id": hid, "mechanism": "m", "family": "f", "strategy": "s",
         "universe": {"a": 1}, "input_data_sha256": sha("d"), "code_sha": "abcdef1", "data_window": {"a": 1},
         "parameter_grid": {"a": [1]}, "costs": {"a": 1}, "statistical_design": {"a": 1}, "train_test": {"a": 1},
         "multiple_testing_family": "fam", "prospective_period": "none", "registered_utc": "2026-11-07T00:00:00Z",
         "source": "test", "content_sha256": sha(owner + tid)}
    r.update(over)
    return r


def tables(pg):
    return pg.run("select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                  "where n.nspname='carry' and c.relkind='r' order by 1;").stdout.split()


def test_reapply_is_idempotent(pg):
    before = pg.scalar("select count(*) from pg_policies where schemaname='carry';")
    for f in FILES:
        pg.run(f.read_text())
    assert pg.scalar("select count(*) from pg_policies where schemaname='carry';") == before
    assert len(tables(pg)) >= 25


def test_rls_enabled_forced_with_policies_and_comments(pg):
    out = pg.run("select count(*) filter (where not relrowsecurity), count(*) filter (where not relforcerowsecurity) "
                 "from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='carry' and relkind='r';").stdout.strip()
    assert out == "0|0"
    for t in tables(pg):
        assert int(pg.scalar(f"select count(*) from pg_policies where schemaname='carry' and tablename='{t}';")) >= 3, t
        assert "RETENTION" in pg.scalar(f"select obj_description('carry.{t}'::regclass);"), t
        assert pg.scalar(f"select has_table_privilege('public','carry.{t}','select')") == "f"


def test_anon_has_no_access(pg):
    for t in tables(pg):
        pg.fails(f"select * from carry.{t};", "permission denied", role="anon")
        pg.fails(f"insert into carry.{t} default values;", "permission denied", role="anon")
    pg.fails("select carry.put_row('data_batches','{}'::jsonb);", "permission denied", role="anon")
    pg.fails("select carry.put_row('data_batches','{}'::jsonb);", "permission denied", role="authenticated")


def test_owner_isolation_and_authenticated_read_only(pg):
    assert put(pg, "strategy_hypotheses", hyp(A)) == "t"
    assert put(pg, "strategy_hypotheses", hyp(B)) == "t"
    assert pg.scalar("select count(*) from carry.strategy_hypotheses;", role="authenticated", uid=A) == "1"
    assert pg.scalar("select owner_id::text from carry.strategy_hypotheses;", role="authenticated", uid=A) == A
    assert pg.scalar("select owner_id::text from carry.strategy_hypotheses;", role="authenticated", uid=B) == B
    assert pg.scalar("select count(*) from carry.strategy_hypotheses;", role="authenticated") == "0"  # no uid
    pg.fails(f"insert into carry.strategy_hypotheses (owner_id,hypothesis_id,mechanism,family,statement,source,content_sha256) "
             f"values ('{A}','X','m','f','s','t','{sha('x')}');", "permission denied", role="authenticated", uid=A)


def test_append_only_blocks_update_delete_truncate_for_every_table(pg):
    for t in tables(pg):
        for stmt in (f"update carry.{t} set source = source;", f"delete from carry.{t};", f"truncate carry.{t} cascade;"):
            pg.fails(stmt, "append-only")  # superuser path: trigger
            pg.fails(stmt, "permission denied", role="service_role")  # privilege path
            pg.fails(stmt, "permission denied", role="authenticated", uid=A)


def test_put_row_idempotent_and_guarded(pg):
    row = hyp(A, "H-idem")
    assert put(pg, "strategy_hypotheses", row) == "t"
    assert put(pg, "strategy_hypotheses", row) == "f"
    assert pg.scalar(f"select count(*) from carry.strategy_hypotheses where hypothesis_id='H-idem';") == "1"
    pg.fails("select carry.put_row('strategy_hypotheses', '{\"bogus\":1}'::jsonb);", "unknown column", role="service_role")
    pg.fails("select carry.put_row('pg_class', '{}'::jsonb);", "unknown carry table", role="service_role")
    pg.fails("select carry.put_row('x; drop table y', '{}'::jsonb);", "bad table name", role="service_role")


def test_registry_constraints_and_evidence_rules(pg):
    assert put(pg, "experiment_registrations", reg(A)) == "t"
    for bad in ({"universe": {}}, {"parameter_grid": []}, {"prospective_period": " "}, {"code_sha": "XYZ"},
                {"input_data_sha256": "abc"}, {"hypothesis_id": "NOPE", "trial_id": "T9", "content_sha256": sha("n")}):
        pg.fails(f"select carry.put_row('experiment_registrations', $j${json.dumps(reg(A, 'Tb', **bad))}$j$::jsonb);",
                 "violates|constraint", role="service_role")
    ev = {"owner_id": A, "trial_id": "T1", "level": "BACKTEST", "result": "r", "provenance": "p",
          "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("ev1")}
    assert put(pg, "experiment_evidence", ev) == "t"
    pg.fails("select carry.put_row('experiment_evidence', $j$%s$j$::jsonb);" % json.dumps({**ev, "level": "PASSED", "content_sha256": sha("ev2")}),
             "violates", role="service_role")
    assert put(pg, "experiment_registrations", reg(A, "T2", content_sha256=sha("t2"))) == "t"
    ab = {"owner_id": A, "trial_id": "T2", "reason": "r", "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("ab")}
    assert put(pg, "experiment_abandonments", ab) == "t"
    pg.fails("select carry.put_row('experiment_evidence', $j$%s$j$::jsonb);" % json.dumps({**ev, "trial_id": "T2", "content_sha256": sha("ev3")}),
             "abandoned", role="service_role")


def test_owner_import_status_vocabulary_and_rules(pg):
    imp = {"owner_id": A, "import_id": sha("imp"), "source_sha256": sha("src"), "kind": "statement", "storage_ref": "s3://x",
           "status": "PROVISIONAL", "imported_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("imp")}
    assert put(pg, "owner_account_imports", imp) == "t"
    assert put(pg, "owner_account_imports", imp) == "f"
    for bad in ({"status": "OK"}, {"status": "verified"}, {"content_sha256": sha("other")}):
        pg.fails("select carry.put_row('owner_account_imports', $j$%s$j$::jsonb);" % json.dumps({**imp, **bad, "import_id": sha("imp2") if "status" in bad else imp["import_id"]}),
                 "violates", role="service_role")
    ev = {"owner_id": A, "import_id": sha("imp"), "status": "RECONCILED", "recorded_utc": "2026-11-07T00:00:00Z",
          "source": "t", "content_sha256": sha("e1")}
    pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps(ev), "violates", role="service_role")  # no evidence_ref
    pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps({**ev, "evidence_ref": "recon-1"}),
             "violates", role="service_role")  # free text is not evidence: it must be the import row's own hash
    assert put(pg, "owner_account_import_status_events", {**ev, "evidence_ref": sha("imp")}) == "t"
    pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps({**ev, "status": "DISPUTED", "content_sha256": sha("e2")}),
             "violates", role="service_role")  # no reason


def test_approval_never_defaults_true_and_needs_owner(pg):
    assert pg.scalar("select column_default is null from information_schema.columns where table_schema='carry' "
                     "and table_name='approval_records' and column_name='approved_by_owner';") == "t"
    base = {"owner_id": A, "approval_id": "AP1", "subject_kind": "risk_policy_version", "subject_ref": "p@1",
            "decided_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("ap1")}
    pg.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps(base), "not-null|null value", role="service_role")
    pg.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps({**base, "approved_by_owner": True}), "violates", role="service_role")
    pg.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps(
        {**base, "approved_by_owner": True, "approver_id": B, "statement_sha256": sha("s")}), "violates", role="service_role")
    pg.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps({**base, "subject_kind": "promotion_gate", "approved_by_owner": False}),
             "violates", role="service_role")
    assert put(pg, "approval_records", {**base, "approved_by_owner": False}) == "t"
    assert put(pg, "approval_records", {**base, "approval_id": "AP2", "content_sha256": sha("ap2"), "approved_by_owner": True,
                                        "approver_id": A, "statement_sha256": sha("s")}) == "t"


def test_execution_session_modes_and_flat_requires_venue_read(pg):
    s = {"owner_id": A, "session_id": "S1", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
         "started_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("s1")}
    for bad_mode in ("LIVE", "MAINNET", "paper"):
        pg.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps({**s, "mode": bad_mode, "session_id": "SX", "content_sha256": sha("sx" + bad_mode)}),
                 "violates", role="service_role")
    assert put(pg, "execution_sessions", s) == "t"
    e = {"owner_id": A, "session_id": "S1", "state": "FLAT_CONFIRMED", "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("e")}
    pg.fails("select carry.put_row('execution_session_events', $j$%s$j$::jsonb);" % json.dumps(e), "violates", role="service_role")
    pg.fails("select carry.put_row('execution_session_events', $j$%s$j$::jsonb);" % json.dumps(
        {**e, "state": "HALTED", "venue_position_read_sha256": sha("r"), "content_sha256": sha("e3")}), "violates", role="service_role")
    assert put(pg, "execution_session_events", {**e, "venue_position_read_sha256": sha("r")}) == "t"


def test_manifest_constraints(pg):
    b = {"owner_id": A, "batch_id": "B1", "dataset": "d", "venue": "v", "batch_sha256": sha("b"), "byte_count": 10,
         "segment_ids": ["s1"], "window_start_utc": "2026-11-07T01:00:00Z", "window_end_utc": "2026-11-07T00:00:00Z",
         "storage_ref": "ref", "source": "t", "content_sha256": sha("b1")}
    pg.fails("select carry.put_row('data_batches', $j$%s$j$::jsonb);" % json.dumps(b), "violates", role="service_role")  # window reversed
    pg.fails("select carry.put_row('data_batches', $j$%s$j$::jsonb);" % json.dumps({**b, "window_end_utc": "2026-11-07T02:00:00Z", "segment_ids": []}), "violates", role="service_role")
    pg.fails("select carry.put_row('data_batches', $j$%s$j$::jsonb);" % json.dumps({**b, "window_end_utc": "2026-11-07T02:00:00Z", "byte_count": -1}), "violates", role="service_role")
    assert put(pg, "data_batches", {**b, "window_end_utc": "2026-11-07T02:00:00Z"}) == "t"


# --------------------------------------------------------------------------- 20261107000900 write-path hardening
# The executed form of the red-team probes (probe.sql / probe2.sql): every case has a positive control.

HARDENING = MIG_DIR / "20261107000900_carry_write_path_hardening.sql"


def test_hardening_migration_is_last_additive_and_documents_what_it_cannot_close():
    assert FILES[-1] == HARDENING
    raw = HARDENING.read_text()
    code = strip_comments(raw).lower()
    assert not re.search(r"\bcreate table\b|\bdrop\b|\bgrant\b[^;]*\b(insert|update|delete|truncate|all)\b|disable row level|security definer", code)
    assert code.count("not valid;") + code.count("not valid\n") >= 2          # existing rows are never rescanned
    assert "NOT CLOSABLE AT DATABASE LEVEL" in raw
    for gap in ("forged owner approval", "content_sha256 correctness", "superuser"):
        assert gap in raw, gap


def test_the_sandbox_venue_list_is_the_registry_sandbox_set():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import venues
    sandbox = sorted(v.name for v in venues._QUALIFIED.values() if v.kind == venues.SANDBOX)
    listed = sorted(re.findall(r"'(\w+)'", re.search(r"venue in \(([^)]*)\)", strip_comments(HARDENING.read_text())).group(1)))
    assert sandbox and listed == sorted(set(sandbox)), (sandbox, listed)
    assert "mainnet" not in listed and "eu" not in listed
    assert venues._QUALIFIED["mainnet"].kind == venues.LIVE and "eu" in venues._UNQUALIFIED


def test_server_owns_id_and_created_at_and_a_forged_pair_is_ignored(pg):
    owner = str(uuid.uuid4())
    forged = {**hyp(owner, "H-forge"), "created_at": "2000-01-01T00:00:00Z", "id": "00000000-0000-0000-0000-000000000001"}
    assert put(pg, "strategy_hypotheses", forged) == "t"
    row = pg.scalar(f"select id::text || '|' || created_at::text from carry.strategy_hypotheses where owner_id='{owner}';")
    rid, created = row.split("|")
    assert rid != "00000000-0000-0000-0000-000000000001"
    assert pg.scalar(f"select created_at > now() - interval '5 minutes' and created_at <= now() "
                     f"from carry.strategy_hypotheses where owner_id='{owner}';") == "t"
    assert not created.startswith("2000")
    # an identical resend with a different forged stamp is still a quiet no-op
    assert put(pg, "strategy_hypotheses", {**forged, "id": str(uuid.uuid4())}) == "f"
    assert pg.scalar(f"select count(*) from carry.strategy_hypotheses where owner_id='{owner}';") == "1"
    # every carry table carries the trigger
    assert pg.scalar("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='carry' and c.relkind='r' "
                     "and not exists (select 1 from pg_trigger g where g.tgrelid=c.oid and g.tgname='carry_stamp_insert');") == "0"


def test_future_dated_event_times_are_refused_but_the_present_and_the_past_are_not(pg):
    owner = str(uuid.uuid4())
    assert put(pg, "strategy_hypotheses", hyp(owner)) == "t"
    imp = {"owner_id": owner, "import_id": sha("fi"), "source_sha256": sha("fsrc"), "kind": "statement", "storage_ref": "s3://x",
           "status": "VERIFIED", "imported_utc": "2026-01-01T00:00:00Z", "source": "t", "content_sha256": sha("fi")}
    assert put(pg, "owner_account_imports", imp) == "t"
    ev = {"owner_id": owner, "import_id": sha("fi"), "status": "VERIFIED", "evidence_ref": sha("fi"), "source": "t"}
    far = {**ev, "recorded_utc": "9999-01-01T00:00:00Z", "content_sha256": sha("fe1")}
    pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps(far), "in the future", role="service_role")
    soon = pg.scalar("select (now() + interval '1 hour')::text;")
    pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps({**far, "recorded_utc": soon}), "in the future", role="service_role")
    near = pg.scalar("select to_char(now() + interval '1 minute', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');")
    assert put(pg, "owner_account_import_status_events", {**ev, "recorded_utc": near, "content_sha256": sha("fe2")}) == "t"   # within the 5-minute skew
    assert put(pg, "owner_account_import_status_events", {**ev, "recorded_utc": "2026-01-02T00:00:00Z", "content_sha256": sha("fe3")}) == "t"
    # other event-time columns are covered too, and forward-looking ones are not
    pg.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps(
        {"owner_id": owner, "approval_id": "APF", "subject_kind": "data_batch", "subject_ref": "b", "approved_by_owner": False,
         "decided_utc": "9999-01-01T00:00:00Z", "source": "t", "content_sha256": sha("apf")}), "in the future", role="service_role")
    sess = {"owner_id": owner, "session_id": "FUT", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
            "started_utc": "9999-01-01T00:00:00Z", "source": "t", "content_sha256": sha("fut")}
    pg.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps(sess), "in the future", role="service_role")
    assert put(pg, "execution_sessions", {**sess, "started_utc": "2026-11-07T00:00:00Z"}) == "t"                  # positive control
    pol = {"owner_id": owner, "policy_name": "p", "version": 1, "policy": {"a": 1}, "effective_from_utc": "9999-01-01T00:00:00Z",
           "source": "t", "content_sha256": sha("pol")}
    assert put(pg, "risk_policy_versions", pol) == "t"


def test_a_conflicting_resend_is_reported_false_and_never_overwrites(pg):
    """Documented, NOT a DB-level error (see the migration header): put_row keeps returning false so the writer can classify
    the conflict as CONFLICT. What the database does guarantee is that the stored row is untouched."""
    owner = str(uuid.uuid4())
    h = hyp(owner, "H-conf")
    assert put(pg, "strategy_hypotheses", h) == "t"
    assert put(pg, "strategy_hypotheses", h) == "f"
    changed = {**h, "mechanism": "CHANGED", "content_sha256": sha("changed")}         # same (owner, hypothesis_id), other content
    assert put(pg, "strategy_hypotheses", changed) == "f"
    assert pg.scalar(f"select mechanism from carry.strategy_hypotheses where owner_id='{owner}' and hypothesis_id='H-conf';") == "funding"
    assert pg.scalar(f"select count(*) from carry.strategy_hypotheses where owner_id='{owner}';") == "1"


def test_execution_sessions_pair_only_with_a_registered_sandbox_venue(pg):
    base = {"owner_id": A, "strategy": "x", "code_sha": "abcdef1", "started_utc": "2026-11-07T00:00:00Z", "source": "t"}
    n = 0
    for mode in ("PAPER", "TESTNET"):
        for venue in ("mainnet", "eu", "Testnet", "bybit", "", "main net"):
            n += 1
            row = {**base, "mode": mode, "venue": venue, "session_id": f"V{n}", "content_sha256": sha(f"v{n}")}
            pg.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps(row), "violates", role="service_role")
        for venue in ("testnet", "demo"):                                              # positive control
            n += 1
            row = {**base, "mode": mode, "venue": venue, "session_id": f"V{n}", "content_sha256": sha(f"v{n}")}
            assert put(pg, "execution_sessions", row) == "t", (mode, venue)


def test_order_fill_and_balance_tables_accept_no_writes_from_anyone(pg):
    owner = A
    sess = {"owner_id": owner, "session_id": "S-ro", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
            "started_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("s-ro")}
    assert put(pg, "execution_sessions", sess) == "t"
    order = {"owner_id": owner, "session_id": "S-ro", "venue_order_id": "o1", "symbol": "BTCUSDT", "side": "BUY", "reduce_only": False,
             "venue_payload_sha256": sha("p"), "evidence_ref": "x", "observed_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("o1")}
    snap = {"owner_id": owner, "snapshot_id": "z", "snapshot_kind": "VENUE_READ", "status": "VERIFIED", "taken_utc": "2026-11-07T00:00:00Z",
            "payload_sha256": sha("pl"), "payload_ref": "r", "summary": {"balance_usd": 123456.78, "email": "a@b.co"}, "source": "t",
            "content_sha256": sha("z")}
    fill = {"owner_id": owner, "session_id": "S-ro", "venue_order_id": "o1", "venue_fill_id": "f1", "venue_payload_sha256": sha("p"),
            "evidence_ref": "x", "observed_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("f1")}
    for table, row in (("order_refs", order), ("account_snapshots", snap), ("fill_refs", fill)):
        pg.fails("select carry.put_row('%s', $j$%s$j$::jsonb);" % (table, json.dumps(row)), "permission denied", role="service_role")
        # the table owner / superuser path is closed by the trigger even if a later migration re-grants INSERT
        pg.fails(f"grant insert on carry.{table} to service_role; set role service_role; "
                 f"select carry.put_row('{table}', $j${json.dumps(row)}$j$::jsonb);", "accepts no writes")
        pg.run(f"revoke insert on carry.{table} from service_role;")
        pg.fails(f"insert into carry.{table} default values;", "accepts no writes")
        assert pg.scalar(f"select has_table_privilege('service_role','carry.{table}','insert');") == "f"
        assert pg.scalar(f"select count(*) from carry.{table};", role="service_role") == "0"             # still readable
    # positive control: an ordinary table still takes the same session's other writes
    assert put(pg, "execution_session_events", {"owner_id": owner, "session_id": "S-ro", "state": "STARTED",
                                                 "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("ev-ro")}) == "t"


def test_verified_and_reconciled_evidence_must_be_the_import_row_hash(pg):
    owner = str(uuid.uuid4())
    imp = {"owner_id": owner, "import_id": sha("ei"), "source_sha256": sha("esrc"), "kind": "statement", "storage_ref": "s3://x",
           "status": "PROVISIONAL", "imported_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("ei")}
    assert put(pg, "owner_account_imports", imp) == "t"
    ev = {"owner_id": owner, "import_id": sha("ei"), "recorded_utc": "2026-11-07T00:00:00Z", "source": "t"}
    n = 0
    for status in ("VERIFIED", "RECONCILED"):
        for bad in (None, "", "   ", "recon-1", sha("some-other-hash")):
            n += 1
            row = {**ev, "status": status, "content_sha256": sha(f"b{n}")}
            if bad is not None:
                row["evidence_ref"] = bad
            pg.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps(row), "violates", role="service_role")
        n += 1
        assert put(pg, "owner_account_import_status_events", {**ev, "status": status, "evidence_ref": sha("ei"),
                                                              "content_sha256": sha(f"g{n}")}) == "t", status
    # the negative states need a reason, not the hash
    assert put(pg, "owner_account_import_status_events", {**ev, "status": "DISPUTED", "reason": "mismatch",
                                                          "evidence_ref": sha("anything"), "content_sha256": sha("disp")}) == "t"


def test_the_service_role_still_cannot_tamper_with_the_guards(pg):
    pg.fails("alter table carry.strategy_hypotheses disable trigger all;", "must be owner", role="service_role")
    pg.fails("select pg_catalog.set_config('session_replication_role','replica',false);", "permission denied", role="service_role")
    pg.fails("drop trigger carry_stamp_insert on carry.strategy_hypotheses;", "must be owner", role="service_role")
    pg.fails("update carry.strategy_hypotheses set mechanism='x';", "permission denied", role="service_role")
    pg.fails("delete from carry.strategy_hypotheses;", "permission denied", role="service_role")
    pg.fails("truncate carry.strategy_hypotheses;", "permission denied", role="service_role")
    pg.fails("select carry.put_row('strategy_hypotheses','{}'::jsonb);", "permission denied", role="authenticated")
    pg.fails("select count(*) from carry.strategy_hypotheses;", "permission denied", role="anon")
