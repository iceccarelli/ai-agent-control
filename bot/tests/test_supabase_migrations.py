"""Supabase/Postgres migrations for the carry programme's structured application data.

Two layers:
  * STATIC tests (always run): parse supabase/migrations/*.sql.
  * EXECUTED tests: apply every migration in order into a DISPOSABLE local Postgres (the `pgserver` wheel,
    a private data dir under pytest's tmp path, unix-socket only) and exercise RLS, grants, triggers and
    constraints. They SKIP cleanly when `pgserver` is not importable. They never contact any network service,
    any Supabase project, or any existing database. Supabase's roles and auth.uid() are stubbed locally.
"""
import hashlib
import json
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

@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver", reason="no disposable Postgres (pip install pgserver): RLS not executed")
    d = tmp_path_factory.mktemp("pgdata")
    try:
        srv = pgserver.get_server(d, cleanup_mode="stop")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"cannot start disposable postgres: {e}")
    psql = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
    if not psql.exists():
        pytest.skip("pgserver psql binary missing")
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
    assert put(pg, "owner_account_import_status_events", {**ev, "evidence_ref": "recon-1"}) == "t"
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
