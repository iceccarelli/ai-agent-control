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
    assert all(re.fullmatch(r"(2026110[78]|20261110)\d{6}_[a-z0-9_]+\.sql", n) for n in names), names
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


class _PG:
    def __init__(self, psql, uri):
        self.psql, self.uri = psql, uri

    def run(self, sql, role=None, uid=None, check=True):
        pre = ""
        if role:
            pre += f"set role {role};\n"
        if uid:
            pre += f"select set_config('request.jwt.claim.sub', '{uid}', false);\n"
        p = subprocess.run([str(self.psql), self.uri, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"], input=pre + sql,
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


@pytest.fixture(scope="module")
def server(tmp_path_factory):
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
    return _PG(psql, srv.get_uri())


def _fresh(server, name, files):
    server.run(f"create database {name};")
    h = _PG(server.psql, server.uri.replace("@/postgres?", f"@/{name}?", 1))
    h.run(SUPABASE_STUB)
    for f in files:
        h.run(f.read_text())
    return h


@pytest.fixture(scope="module")
def pg(server):
    """Every migration, in order: the schema as shipped."""
    return _fresh(server, "mig_all", FILES)


@pytest.fixture(scope="module")
def pg_pre(server):
    """The schema as of 20261107000900 (before the rt4 migration closed the no-writer tables): the tests of that migration's CHECKs insert into tables the later one write-closes."""
    return _fresh(server, "mig_pre", [f for f in FILES if f not in (RT4, RT5)])


@pytest.fixture(scope="module")
def pg_rt4(server):
    """The schema as of the rt4 migration (before rt5 refused TESTNET_MECHANICS and bound owner evidence to an import row)."""
    return _fresh(server, "mig_rt4", [f for f in FILES if f != RT5])


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


def test_execution_session_modes_and_flat_requires_venue_read(pg_pre):
    s = {"owner_id": A, "session_id": "S1", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
         "started_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("s1")}
    for bad_mode in ("LIVE", "MAINNET", "paper"):
        pg_pre.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps({**s, "mode": bad_mode, "session_id": "SX", "content_sha256": sha("sx" + bad_mode)}),
                 "violates", role="service_role")
    assert put(pg_pre, "execution_sessions", s) == "t"
    e = {"owner_id": A, "session_id": "S1", "state": "FLAT_CONFIRMED", "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("e")}
    pg_pre.fails("select carry.put_row('execution_session_events', $j$%s$j$::jsonb);" % json.dumps(e), "violates", role="service_role")
    pg_pre.fails("select carry.put_row('execution_session_events', $j$%s$j$::jsonb);" % json.dumps(
        {**e, "state": "HALTED", "venue_position_read_sha256": sha("r"), "content_sha256": sha("e3")}), "violates", role="service_role")
    assert put(pg_pre, "execution_session_events", {**e, "venue_position_read_sha256": sha("r")}) == "t"


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
RT4 = MIG_DIR / "20261108000100_carry_rt4_sync_hardening.sql"
RT5 = MIG_DIR / "20261108000200_carry_rt5_sync_hardening.sql"


def test_hardening_migration_is_last_additive_and_documents_what_it_cannot_close():
    assert FILES[-4:-1] == [HARDENING, RT4, RT5] and FILES[-1].name.startswith("20261110")          # the rt5 migration closed the old set; the execution-evidence migration is ADDITIVE after it; nothing numbered was edited to make room
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


def test_future_dated_event_times_are_refused_but_the_present_and_the_past_are_not(pg_pre):
    owner = str(uuid.uuid4())
    assert put(pg_pre, "strategy_hypotheses", hyp(owner)) == "t"
    imp = {"owner_id": owner, "import_id": sha("fi"), "source_sha256": sha("fsrc"), "kind": "statement", "storage_ref": "s3://x",
           "status": "VERIFIED", "imported_utc": "2026-01-01T00:00:00Z", "source": "t", "content_sha256": sha("fi")}
    assert put(pg_pre, "owner_account_imports", imp) == "t"
    ev = {"owner_id": owner, "import_id": sha("fi"), "status": "VERIFIED", "evidence_ref": sha("fi"), "source": "t"}
    far = {**ev, "recorded_utc": "9999-01-01T00:00:00Z", "content_sha256": sha("fe1")}
    pg_pre.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps(far), "in the future", role="service_role")
    soon = pg_pre.scalar("select (now() + interval '1 hour')::text;")
    pg_pre.fails("select carry.put_row('owner_account_import_status_events', $j$%s$j$::jsonb);" % json.dumps({**far, "recorded_utc": soon}), "in the future", role="service_role")
    near = pg_pre.scalar("select to_char(now() + interval '1 minute', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');")
    assert put(pg_pre, "owner_account_import_status_events", {**ev, "recorded_utc": near, "content_sha256": sha("fe2")}) == "t"   # within the 5-minute skew
    assert put(pg_pre, "owner_account_import_status_events", {**ev, "recorded_utc": "2026-01-02T00:00:00Z", "content_sha256": sha("fe3")}) == "t"
    # other event-time columns are covered too, and forward-looking ones are not
    pg_pre.fails("select carry.put_row('approval_records', $j$%s$j$::jsonb);" % json.dumps(
        {"owner_id": owner, "approval_id": "APF", "subject_kind": "data_batch", "subject_ref": "b", "approved_by_owner": False,
         "decided_utc": "9999-01-01T00:00:00Z", "source": "t", "content_sha256": sha("apf")}), "in the future", role="service_role")
    sess = {"owner_id": owner, "session_id": "FUT", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
            "started_utc": "9999-01-01T00:00:00Z", "source": "t", "content_sha256": sha("fut")}
    pg_pre.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps(sess), "in the future", role="service_role")
    assert put(pg_pre, "execution_sessions", {**sess, "started_utc": "2026-11-07T00:00:00Z"}) == "t"                  # positive control
    pol = {"owner_id": owner, "policy_name": "p", "version": 1, "policy": {"a": 1}, "effective_from_utc": "9999-01-01T00:00:00Z",
           "source": "t", "content_sha256": sha("pol")}
    assert put(pg_pre, "risk_policy_versions", pol) == "t"


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


def test_execution_sessions_pair_only_with_a_registered_sandbox_venue(pg_pre):
    base = {"owner_id": A, "strategy": "x", "code_sha": "abcdef1", "started_utc": "2026-11-07T00:00:00Z", "source": "t"}
    n = 0
    for mode in ("PAPER", "TESTNET"):
        for venue in ("mainnet", "eu", "Testnet", "bybit", "", "main net"):
            n += 1
            row = {**base, "mode": mode, "venue": venue, "session_id": f"V{n}", "content_sha256": sha(f"v{n}")}
            pg_pre.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps(row), "violates", role="service_role")
        for venue in ("testnet", "demo"):                                              # positive control
            n += 1
            row = {**base, "mode": mode, "venue": venue, "session_id": f"V{n}", "content_sha256": sha(f"v{n}")}
            assert put(pg_pre, "execution_sessions", row) == "t", (mode, venue)


def test_order_fill_and_balance_tables_accept_no_writes_from_anyone(pg_pre):
    owner = A
    sess = {"owner_id": owner, "session_id": "S-ro", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
            "started_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("s-ro")}
    assert put(pg_pre, "execution_sessions", sess) == "t"
    order = {"owner_id": owner, "session_id": "S-ro", "venue_order_id": "o1", "symbol": "BTCUSDT", "side": "BUY", "reduce_only": False,
             "venue_payload_sha256": sha("p"), "evidence_ref": "x", "observed_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("o1")}
    snap = {"owner_id": owner, "snapshot_id": "z", "snapshot_kind": "VENUE_READ", "status": "VERIFIED", "taken_utc": "2026-11-07T00:00:00Z",
            "payload_sha256": sha("pl"), "payload_ref": "r", "summary": {"balance_usd": 123456.78, "email": "a@b.co"}, "source": "t",
            "content_sha256": sha("z")}
    fill = {"owner_id": owner, "session_id": "S-ro", "venue_order_id": "o1", "venue_fill_id": "f1", "venue_payload_sha256": sha("p"),
            "evidence_ref": "x", "observed_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("f1")}
    for table, row in (("order_refs", order), ("account_snapshots", snap), ("fill_refs", fill)):
        pg_pre.fails("select carry.put_row('%s', $j$%s$j$::jsonb);" % (table, json.dumps(row)), "permission denied", role="service_role")
        # the table owner / superuser path is closed by the trigger even if a later migration re-grants INSERT
        pg_pre.fails(f"grant insert on carry.{table} to service_role; set role service_role; "
                 f"select carry.put_row('{table}', $j${json.dumps(row)}$j$::jsonb);", "accepts no writes")
        pg_pre.run(f"revoke insert on carry.{table} from service_role;")
        pg_pre.fails(f"insert into carry.{table} default values;", "accepts no writes")
        assert pg_pre.scalar(f"select has_table_privilege('service_role','carry.{table}','insert');") == "f"
        assert pg_pre.scalar(f"select count(*) from carry.{table};", role="service_role") == "0"             # still readable
    # positive control: an ordinary table still takes the same session's other writes
    assert put(pg_pre, "execution_session_events", {"owner_id": owner, "session_id": "S-ro", "state": "STARTED",
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


# --------------------------------------------------------------------------- 20261108000100 rt4 hardening

NO_WRITER_TABLES = ("execution_sessions", "execution_session_events", "risk_decisions", "reconciliation_exceptions", "reconciliation_exception_resolutions")


def test_rt4_migration_is_additive_documents_client_supplied_registered_utc_and_never_edits_a_numbered_file():
    raw = RT4.read_text()
    code = strip_comments(raw).lower()
    assert not re.search(r"\bcreate table\b|\bdrop\b|\bgrant\b[^;]*\b(insert|update|delete|truncate|all)\b|disable row level|security definer", code)
    assert code.count("not valid") >= 4                                           # existing rows are never rescanned
    for phrase in ("registered_utc IS CLIENT-SUPPLIED", "created_at", "free-form"):
        assert phrase in raw, phrase
    assert "comment on column carry.experiment_registrations.registered_utc" in code and "client-supplied" in code
    # nothing numbered before it was changed: 000900 still ends with its own baseline check and no later object appears in it
    assert "forbid_insert_untyped" not in HARDENING.read_text() and "report_sha256" not in HARDENING.read_text()


def test_rt4_the_write_closed_tables_are_exactly_the_tables_the_sync_writer_does_not_map():
    """Every carry table is either written by tools/supabase_sync.py (TABLE_ORDER) or write-closed in SQL: no third state. If a writer is added for one, this test forces the
    migration that re-opens it (with constrained columns) to be written too."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import supabase_sync as SS
    sql = all_sql()
    created = set(re.findall(r"create table if not exists carry\.(\w+) \(", sql))
    closed = set(NO_WRITER_TABLES) | {"order_refs", "fill_refs", "account_snapshots"}
    mapped = set(SS.TABLE_ORDER)
    exec_owned = {"exec_events", "exec_heartbeats", "exec_lease_log"}                       # written by tools/exec_evidence.py (its own migration, RPC-only writes), not by the sync writer
    assert created == mapped | closed | exec_owned and not (mapped & closed) and not (exec_owned & (mapped | closed)), (sorted(created - mapped - closed - exec_owned), sorted(mapped & closed))
    body = strip_comments(RT4.read_text())
    listed = set(re.findall(r"'(\w+)'", re.search(r"foreach t in array array\[(.*?)\] loop", body, re.S).group(1)))
    assert listed == set(NO_WRITER_TABLES)


def test_rt4_no_writer_tables_accept_no_writes_from_anyone_and_stay_readable(pg):
    owner = A
    seed = {"execution_sessions": {"owner_id": owner, "session_id": "S-x", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1",
                                   "started_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("rt4-s")},
            "execution_session_events": {"owner_id": owner, "session_id": "S-x", "state": "STARTED", "detail": {"balance_usd": 123456.78, "email": "a@b.co"},
                                         "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("rt4-e")},
            "risk_decisions": {"owner_id": owner, "decision_id": "D1", "policy_name": "p", "policy_version": 1, "subject": "s", "decision": "DENY",
                               "reasons": {"note": "free text with a balance 123456.78"}, "inputs_sha256": sha("in"), "decided_utc": "2026-11-07T00:00:00Z",
                               "source": "t", "content_sha256": sha("rt4-d")},
            "reconciliation_exceptions": {"owner_id": owner, "exception_id": "X1", "import_id": sha("imp"), "kind": "k", "severity": "WARN", "description": "acct 12345678 off by 1",
                                          "amount": "12.5", "currency": "USD", "detected_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("rt4-x")},
            "reconciliation_exception_resolutions": {"owner_id": owner, "exception_id": "X1", "resolution": "RESOLVED", "explanation": "paid to account 12345678",
                                                     "recorded_utc": "2026-11-07T00:00:00Z", "source": "t", "content_sha256": sha("rt4-r")}}
    assert set(seed) == set(NO_WRITER_TABLES)
    for table, row in seed.items():
        pg.fails("select carry.put_row('%s', $j$%s$j$::jsonb);" % (table, json.dumps(row)), "permission denied", role="service_role")
        pg.fails(f"grant insert on carry.{table} to service_role; set role service_role; select carry.put_row('{table}', $j${json.dumps(row)}$j$::jsonb);", "accepts no writes")
        pg.run(f"revoke insert on carry.{table} from service_role;")
        pg.fails(f"insert into carry.{table} default values;", "accepts no writes")                    # the table owner too
        assert pg.scalar(f"select has_table_privilege('service_role','carry.{table}','insert');") == "f"
        assert pg.scalar(f"select count(*) from carry.{table};", role="service_role") == "0"             # still readable
    # positive control: a table the writer maps still takes its rows in the same database
    assert put(pg, "strategy_hypotheses", hyp(owner, "H-rt4")) == "t"


def test_rt4_the_checks_of_the_closed_tables_survive_in_the_schema_as_defence_in_depth(pg):
    for name in ("execution_sessions_mode_sandbox_venue",):
        assert pg.scalar(f"select count(*) from pg_constraint where conname='{name}';") == "1"
    assert pg.scalar("select count(*) from pg_trigger where tgname='carry_no_insert' and not tgisinternal;") == str(3 + len(NO_WRITER_TABLES))


def _imp(owner, status, **over):
    r = {"owner_id": owner, "import_id": sha("rt4-i" + status + str(sorted(over))), "source_sha256": sha("src"), "kind": "owner_import", "storage_ref": "private://x",
         "status": status, "imported_utc": "2026-11-07T00:00:00Z", "source": "t"}
    r["content_sha256"] = r["import_id"]
    r.update(over)
    return r


def test_rt4_a_verified_or_reconciled_import_row_must_name_its_sealed_report_hash(pg):
    owner = str(uuid.uuid4())
    for status in ("VERIFIED", "RECONCILED"):
        row = _imp(owner, status)
        pg.fails("select carry.put_row('owner_account_imports', $j$%s$j$::jsonb);" % json.dumps(row), "violates", role="service_role")
        pg.fails("select carry.put_row('owner_account_imports', $j$%s$j$::jsonb);" % json.dumps({**row, "report_sha256": "not-a-hash"}), "violates|invalid", role="service_role")
        ok = _imp(owner, status, report_sha256=sha("report-" + status))
        assert put(pg, "owner_account_imports", ok) == "t"                          # positive control
        assert pg.scalar(f"select report_sha256 from carry.owner_account_imports where owner_id='{owner}' and import_id='{ok['import_id']}';") == sha("report-" + status)
    for status in ("PROVISIONAL", "ESTIMATED", "STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE"):
        assert put(pg, "owner_account_imports", _imp(owner, status)) == "t"          # weaker statuses make no claim and need no report hash
    # the constraint is the load-bearing line: dropping it lets the unbound claim in (so the test above can fail)
    pg.run("alter table carry.owner_account_imports drop constraint owner_import_verified_status_names_its_report;")
    assert put(pg, "owner_account_imports", _imp(owner, "VERIFIED", source_sha256=sha("other"))) == "t"
    pg.run("alter table carry.owner_account_imports add constraint owner_import_verified_status_names_its_report "
           "check (status not in ('VERIFIED', 'RECONCILED') or report_sha256 is not null) not valid;")
    pg.fails("select carry.put_row('owner_account_imports', $j$%s$j$::jsonb);" % json.dumps(_imp(owner, "RECONCILED", source_sha256=sha("other2"))), "violates", role="service_role")


def _evrow(owner, level, details, n):
    return {"owner_id": owner, "trial_id": "T1", "level": level, "result": "r:PASS", "provenance": "run-1", "details": details, "recorded_utc": "2026-11-07T00:00:00Z",
            "source": "t", "content_sha256": sha(f"rt4-ev{n}")}


def test_rt4_financial_evidence_is_bound_to_a_hash_and_carries_no_owner_figure(pg_rt4):
    pg = pg_rt4
    owner = str(uuid.uuid4())
    assert put(pg, "strategy_hypotheses", hyp(owner)) == "t"
    assert put(pg, "experiment_registrations", reg(owner)) == "t"
    h1, h2 = sha("report"), sha("log")
    n = 0
    for level, field in (("OWNER_ACCOUNT_VERIFIED", "import_report_sha256"), ("TESTNET_MECHANICS", "execution_log_sha256")):
        for bad in ({}, {"status": "RECONCILED"}, {field: "x" * 64}, {field: h1[:63]}, {field: h1.upper()}, {"metrics": {field: "nope"}}, {"metrics": "scalar"}):
            n += 1
            pg.fails("select carry.put_row('experiment_evidence', $j$%s$j$::jsonb);" % json.dumps(_evrow(owner, level, bad, n)), "violates", role="service_role")
        for good in ({field: h1}, {"metrics": {field: h2}}):
            n += 1
            assert put(pg, "experiment_evidence", _evrow(owner, level, good, n)) == "t"
    for level in ("BACKTEST", "PROSPECTIVE"):                                      # other levels make no such claim
        n += 1
        assert put(pg, "experiment_evidence", _evrow(owner, level, {}, n)) == "t"
    for bad in ({"net_usd_after_all_costs": "1000000"}, {"metrics": {"net_usd_after_all_costs": "1000000"}},
                {"import_report_sha256": h1, "metrics": {"net_usd_after_all_costs": 5}}):
        n += 1
        pg.fails("select carry.put_row('experiment_evidence', $j$%s$j$::jsonb);" % json.dumps(_evrow(owner, "OWNER_ACCOUNT_VERIFIED", bad, n)), "violates", role="service_role")
    n += 1
    assert put(pg, "experiment_evidence", _evrow(owner, "BACKTEST", {"metrics": {"sharpe": "1.25"}}, n)) == "t"     # an ordinary metric is fine


def test_rt4_registered_utc_is_documented_client_supplied_and_created_at_is_the_server_clock(pg):
    owner = str(uuid.uuid4())
    assert pg.scalar("select col_description('carry.experiment_registrations'::regclass, (select attnum from pg_attribute where attrelid='carry.experiment_registrations'::regclass and attname='registered_utc'));").startswith("CLIENT-SUPPLIED")
    assert put(pg, "strategy_hypotheses", hyp(owner)) == "t"
    assert put(pg, "experiment_registrations", reg(owner, registered_utc="2020-01-01T00:00:00Z")) == "t"           # a backdated claim is accepted (documented) ...
    row = pg.scalar(f"select registered_utc < '2021-01-01' and created_at > now() - interval '1 minute' from carry.experiment_registrations where owner_id='{owner}';")
    assert row == "t"                                                                                            # ... and is visible: created_at is the server's, not the client's


def test_rt4_reapplying_the_migration_changes_nothing(pg):
    before = pg.scalar("select count(*) from pg_constraint where connamespace='carry'::regnamespace;")
    pg.run(RT4.read_text())
    assert pg.scalar("select count(*) from pg_constraint where connamespace='carry'::regnamespace;") == before
    assert pg.scalar("select has_table_privilege('service_role','carry.risk_decisions','insert');") == "f"


def test_rt4_mutation_the_write_closing_trigger_and_revoke_are_load_bearing(server):
    """On a scratch copy: without the trigger and the revoke the free-form insert goes through (so the refusals above are the migration's doing, not an accident)."""
    h = _fresh(server, "mig_mut", FILES)
    row = {"owner_id": A, "session_id": "S-mut", "mode": "PAPER", "venue": "testnet", "strategy": "x", "code_sha": "abcdef1", "started_utc": "2026-11-07T00:00:00Z",
           "source": "t", "content_sha256": sha("mut")}
    h.fails("select carry.put_row('execution_sessions', $j$%s$j$::jsonb);" % json.dumps(row), "permission denied", role="service_role")
    h.run("drop trigger carry_no_insert on carry.execution_sessions; grant insert on carry.execution_sessions to service_role;")
    assert put(h, "execution_sessions", row) == "t"


# =========================================================================== rt5 (red-team round 4)

def _ins(pg, row, table="experiment_evidence", **kw):
    return pg.run("select carry.put_row('%s', $j$%s$j$::jsonb);" % (table, json.dumps(row)), role="service_role", **kw)


def _refused(pg, row, match="violates|must cite|refused", table="experiment_evidence"):
    return pg.fails("select carry.put_row('%s', $j$%s$j$::jsonb);" % (table, json.dumps(row)), match, role="service_role")


_N = [1000]


def _ev5(owner, level, details, **over):
    _N[0] += 1
    r = _evrow(owner, level, details, _N[0])
    r["content_sha256"] = sha(f"rt5-ev{_N[0]}")
    r.update(over)
    return r


def _seed_reg(pg, owner):
    assert put(pg, "strategy_hypotheses", hyp(owner)) == "t"
    assert put(pg, "experiment_registrations", reg(owner)) == "t"


def _sql_arrays():
    code = strip_comments(RT5.read_text())
    m = re.search(r"evidence_details_keys_ok.*?array\[(.*?)\]::text\[\].*?array\[(.*?)\]::text\[\]", code, re.S)
    return [re.findall(r"'([^']+)'", g) for g in m.groups()]


def test_rt5_the_migration_is_additive_and_says_exactly_what_is_and_is_not_bound():
    raw = RT5.read_text()
    code = strip_comments(raw).lower()
    assert not re.search(r"\bcreate table\b|\bdrop\b|\bgrant\b[^;]*\b(insert|update|delete|truncate|all)\b|disable row level|security definer", code)
    assert code.count("not valid") >= 3
    for phrase in ("NOT bound", "FORMAT check only", "client-supplied", "TESTNET_MECHANICS evidence: REFUSED", "NOT closable"):
        assert phrase in raw, phrase


def test_rt5_the_sql_whitelists_are_the_python_whitelists():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import supabase_sync as SS
    top, met = _sql_arrays()
    schema = SS.OBJ_SCHEMAS[("experiment_evidence", "details")]
    assert sorted(top) == sorted(schema) and sorted(met) == sorted(schema["metrics"])
    assert re.search(r"~ '%s'" % re.escape(SS.PROVENANCE_SQL_REGEX), strip_comments(RT5.read_text()))


def test_rt5_renamed_or_nested_financial_keys_are_refused_by_the_whitelist_and_every_listed_key_is_accepted(pg):
    owner = str(uuid.uuid4())
    _seed_reg(pg, owner)
    top, met = _sql_arrays()
    for bad in ({"net_pnl_usd": "1234.5"}, {"pnl": "5"}, {"metrics": {"net_pnl": "5"}}, {"metrics": {"balance": "9"}}, {"owner": {"net": 1}}, {"metrics": {"nested": {"net_usd_after_all_costs": 1}}},
                {"attestation": "ab" * 32}, {"metrics": "scalar"}, {"Sharpe": 1}, {"metrics": {"SHARPE": 1}}, {"net_usd_after_all_costs": "1"}):
        _refused(pg, _ev5(owner, "BACKTEST", bad))
    _refused(pg, _ev5(owner, "BACKTEST", [1, 2]))                                      # not an object
    for k in top:
        if k in ("metrics",):
            continue
        assert _ins(pg, _ev5(owner, "BACKTEST", {k: "1"})).returncode == 0, k              # positive control: every whitelisted top-level key is accepted
    for k in met:
        assert _ins(pg, _ev5(owner, "BACKTEST", {"metrics": {k: "1"}})).returncode == 0, k
    assert _ins(pg, _ev5(owner, "BACKTEST", {})).returncode == 0


def _imp5(owner, status, rsha, tag):
    r = _imp(owner, status, report_sha256=rsha, source_sha256=sha("src5" + tag))
    r["import_id"] = r["content_sha256"] = sha("imp5" + tag)
    return r


def test_rt5_owner_evidence_must_cite_an_existing_verified_or_reconciled_import_row_of_the_same_owner(pg):
    owner, other = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_reg(pg, owner)
    fake = sha("no import has this")
    for det in ({"import_report_sha256": fake}, {"metrics": {"import_report_sha256": fake}}):
        _refused(pg, _ev5(owner, "OWNER_ACCOUNT_VERIFIED", det), "must cite")                 # the PoC: a random sha256 with no import row
    weak = sha("weak report")
    assert put(pg, "owner_account_imports", _imp5(owner, "ESTIMATED", weak, "w")) == "t"
    _refused(pg, _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"import_report_sha256": weak}), "must cite")     # an import with a weaker status backs nothing
    theirs = sha("someone elses report")
    assert put(pg, "owner_account_imports", _imp5(other, "RECONCILED", theirs, "o")) == "t"
    _refused(pg, _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"import_report_sha256": theirs}), "must cite")  # another owner's import backs nothing
    for status in ("VERIFIED", "RECONCILED"):                                                   # positive controls, hash at top level and in metrics
        good = sha("good " + status)
        assert put(pg, "owner_account_imports", _imp5(owner, status, good, status)) == "t"
        assert _ins(pg, _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"import_report_sha256": good})).returncode == 0
        row = _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"metrics": {"import_report_sha256": good}})
        assert put(pg, "experiment_evidence", row) == "t" and put(pg, "experiment_evidence", row) == "f"      # an identical re-send stays idempotent
        _refused(pg, _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"import_report_sha256": good, "metrics": {"import_report_sha256": fake}}), "two different|must cite")


def test_rt5_testnet_mechanics_evidence_is_refused_outright(pg):
    owner = str(uuid.uuid4())
    _seed_reg(pg, owner)
    for det in ({"execution_log_sha256": sha("log")}, {"metrics": {"execution_log_sha256": sha("log")}}):
        _refused(pg, _ev5(owner, "TESTNET_MECHANICS", det), "no writer exists")
    assert _ins(pg, _ev5(owner, "SIMULATED_EXECUTION", {})).returncode == 0


@pytest.mark.parametrize("bad", ["run by alice@example.org", "+34 612 345 678", "balance=12345.67", "a, b", "x" * 121, "", "run\n", "café", "run;drop", "run (1)", "k'v"])
def test_rt5_provenance_and_source_are_restricted_to_a_safe_short_code(pg, bad):
    owner = str(uuid.uuid4())
    _seed_reg(pg, owner)
    _refused(pg, _ev5(owner, "BACKTEST", {}, provenance=bad), "violates|check")
    _refused(pg, hyp(owner, "Hbad") | {"source": bad}, "violates|check", table="strategy_hypotheses")


def test_rt5_provenance_and_source_quote_newline_and_the_writers_own_values(pg):
    owner = str(uuid.uuid4())
    _seed_reg(pg, owner)
    for bad in ("run\n", "run\nnext", "a​b"):
        r = _ev5(owner, "BACKTEST", {}, provenance=bad)
        p = pg.run("select carry.put_row('experiment_evidence', %s::jsonb);" % ("'" + json.dumps(r).replace("'", "''") + "'"), role="service_role", check=False)
        assert p.returncode != 0 and re.search("violates", p.stderr), bad
    for good in ("run#1 offline harness", "run-1", "testnet drill 7", "imp_ref", "tools/strategy_verdict.py", "carry:tools/md_seal.py v1.2", "r" * 120):
        assert _ins(pg, _ev5(owner, "BACKTEST", {}, provenance=good, source=good)).returncode == 0, good


def test_rt5_reapplying_the_migration_changes_nothing(pg):
    before = pg.scalar("select count(*) from pg_constraint where connamespace='carry'::regnamespace;")
    pg.run(RT5.read_text())
    assert pg.scalar("select count(*) from pg_constraint where connamespace='carry'::regnamespace;") == before
    assert pg.scalar("select count(*) from pg_trigger where tgname='carry_evidence_binding' and not tgisinternal;") == "1"


def test_rt5_mutation_the_trigger_the_whitelist_and_the_charset_are_each_load_bearing(server):
    h = _fresh(server, "mig_mut5", FILES)
    owner = str(uuid.uuid4())
    _seed_reg(h, owner)
    fake = _ev5(owner, "OWNER_ACCOUNT_VERIFIED", {"import_report_sha256": sha("nothing")})
    renamed = _ev5(owner, "BACKTEST", {"metrics": {"net_pnl": "5"}})
    prov = _ev5(owner, "BACKTEST", {}, provenance="run, by, alice")
    for r in (fake, renamed, prov):
        _refused(h, r)
    h.run("drop trigger carry_evidence_binding on carry.experiment_evidence;")
    assert put(h, "experiment_evidence", fake) == "t"
    h.run("alter table carry.experiment_evidence drop constraint experiment_evidence_details_key_whitelist;")
    assert put(h, "experiment_evidence", renamed) == "t"
    h.run("alter table carry.experiment_evidence drop constraint experiment_evidence_provenance_short_code;")
    assert put(h, "experiment_evidence", prov) == "t"
