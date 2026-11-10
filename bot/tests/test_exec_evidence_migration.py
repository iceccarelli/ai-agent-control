"""20261110000100_exec_evidence_lease.sql: static checks (always run) and EXECUTED checks against a disposable Postgres.

The executed layer mirrors tests/test_supabase_migrations.py exactly: the `pgserver` wheel, a private data dir, unix
socket only, Supabase roles / auth.uid() stubbed locally. It SKIPS when pgserver is not importable UNLESS
REQUIRE_PG=1 (the CI job `supabase-migrations` sets it): then a missing pgserver, a Postgres that will not start or a
missing psql is a FAILURE. Nothing here contacts a network service or any existing database.
"""
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT / "tools"))
MIG_DIR = BOT.parent / "supabase" / "migrations"
FILES = sorted(MIG_DIR.glob("*.sql"))
MINE = MIG_DIR / "20261110000100_exec_evidence_lease.sql"
PREVIOUS = [f for f in FILES if f != MINE]
NEW_TABLES = ("exec_events", "exec_heartbeats", "exec_lease_log")

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


# --------------------------------------------------------------------------- static

def test_migration_exists_is_the_newest_and_is_additive():
    assert MINE.exists() and FILES[-1] == MINE and len(PREVIOUS) >= 11
    sql = strip_comments(MINE.read_text())
    low = sql.lower()
    assert not re.search(r"\bdrop\b|\bdelete\s+from\b|\balter\s+table\b[^;]*\bdisable\b", low)
    assert "security definer" not in low and "bypassrls" not in low and "disable row level security" not in low
    assert not re.search(r"grant\s[^;]*\b(update|delete|truncate|all)\b[^;]*\bto\b", low)
    assert not re.search(r"grant\s[^;]*\bto\b[^;]*\b(anon|public)\b", low)
    assert "testnet_mechanics" not in low and "experiment_evidence" not in low
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}|sb_secret_|service_role_key|-----BEGIN", sql, flags=re.I)
    for t in NEW_TABLES:
        assert f"select carry_private.secure_table('carry.{t}');" in sql
        assert f"create table if not exists carry.{t} (" in sql


def test_sql_vocabularies_are_the_python_contract():
    import exec_evidence as X
    sql = strip_comments(MINE.read_text())
    kinds = re.search(r"kind in \((.*?)\)\)", sql, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", kinds)) == X.KINDS
    states = re.search(r"state in \((.*?)\)\),", sql, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", states)) == X.ENGINE_STATES
    assert "mode in ('PAPER', 'TESTNET')" in sql and "'LIVE'" not in sql
    hb = re.search(r"create table if not exists carry\.exec_heartbeats \((.*?)\n\);", sql, re.S).group(1)
    for f in X.HEARTBEAT_FIELDS:
        assert re.search(rf"^\s*{f} ", hb, re.M), f
    puts = re.search(r"function carry\.exec_heartbeat_put.*?\$\$;", sql, re.S).group(0)
    for f in X.HEARTBEAT_FIELDS:
        assert f"'{f}'" in puts, f


# --------------------------------------------------------------------------- executed (same machinery as test_supabase_migrations)

def _unavailable(reason, require):
    if require:
        pytest.fail(f"REQUIRE_PG=1 but the executed Supabase tests cannot run: {reason}", pytrace=False)
    pytest.skip(reason)


def _pgserver(require, importer=importlib.import_module):
    try:
        return importer("pgserver")
    except ImportError:
        _unavailable("no disposable Postgres (pip install pgserver): exec evidence migration not executed", require)


def test_require_pg_turns_a_missing_pgserver_into_a_failure_not_a_skip():
    def missing(name):
        raise ImportError(name)

    with pytest.raises(pytest.fail.Exception, match="REQUIRE_PG=1"):
        _pgserver(True, missing)
    with pytest.raises(pytest.skip.Exception):
        _pgserver(False, missing)
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
    d = tmp_path_factory.mktemp("pgdata_exec")
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
    """Every migration in order, mine last."""
    return _fresh(server, "exec_all", FILES)


@pytest.fixture(scope="module")
def pg_prev(server):
    """Everything before mine (to prove mine applies cleanly on top, and changes nothing that existed)."""
    return _fresh(server, "exec_prev", PREVIOUS)


A, B = str(uuid.uuid4()), str(uuid.uuid4())


def sha(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def event(owner, n, kind="DECISION", **over):
    r = {"event_id": f"{kind.lower()}:evt-{owner[:8]}-{n}", "owner_id": owner, "kind": kind, "session_id": "s-1", "seq": n,
         "payload": {"n": n}, "occurred_at": "2026-11-10T00:00:00.000Z", "source": "tools/exec_evidence.py",
         "schema_version": 1, "content_sha256": sha(f"{owner}{kind}{n}")}
    r.update(over)
    return r


def put_event(pg, row, **kw):
    return pg.scalar(f"select carry.exec_event_put($j${json.dumps(row)}$j$::jsonb);", role="service_role", **kw)


def hbrow(owner, worker="w1", seq=1, **over):
    r = {"owner_id": owner, "worker_id": worker, "app": "carry-worker", "machine_id": "m1", "region": "fra",
         "code_revision": "abc1234", "runtime_version": "3.11.9", "run_id": "run-1", "mode": "TESTNET", "state": "HEDGED",
         "session_id": None, "last_bybit_read_ms": 5, "last_market_data_ms": None, "last_reconcile_ms": None,
         "last_supabase_sync_ms": None, "outbox_pending": 0, "disk_free_bytes": 1000, "lease_token": 1, "seq": seq,
         "at_ms": 1_700_000_000_000 + seq, "source": "tools/exec_evidence.py", "schema_version": 1,
         "content_sha256": sha(f"{owner}{worker}{seq}")}
    r.update(over)
    return r


def put_hb(pg, row):
    return pg.scalar(f"select carry.exec_heartbeat_put($j${json.dumps(row)}$j$::jsonb);", role="service_role")


def lease(pg, fn, owner, *args):
    a = ", ".join([f"'{owner}'"] + [f"'{x}'" if isinstance(x, str) else str(x) for x in args])
    return pg.scalar(f"select coalesce(carry.{fn}({a})::text, 'NULL');", role="service_role")


def test_previous_migrations_apply_in_order_and_mine_applies_on_top_unchanged_below(pg_prev):
    def snap():
        return pg_prev.run(
            "select 'p', tablename, policyname, cmd, roles::text, qual, with_check from pg_policies where schemaname='carry' and tablename not like 'exec\\_%' "
            "union all select 't', c.relname, t.tgname, t.tgenabled::text, '', '', '' from pg_trigger t join pg_class c on c.oid=t.tgrelid "
            "join pg_namespace n on n.oid=c.relnamespace where n.nspname='carry' and not t.tgisinternal and c.relname not like 'exec\\_%' "
            "union all select 'g', table_name, grantee, privilege_type, '', '', '' from information_schema.role_table_grants "
            "where table_schema='carry' and table_name not like 'exec\\_%' order by 1,2,3,4,5,6,7;").stdout
    before = snap()
    assert before.count("\n") > 100
    pg_prev.run(MINE.read_text())
    assert snap() == before                      # not one pre-existing policy, trigger or grant changed (incl. the rt5 refusals)
    assert pg_prev.scalar("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                          "where n.nspname='carry' and c.relname in ('exec_events','exec_heartbeats','exec_lease_log');") == "3"


def test_reapply_is_idempotent(pg):
    before = pg.scalar("select count(*) from pg_policies where schemaname='carry';")
    trig = pg.scalar("select count(*) from pg_trigger t join pg_namespace n on n.oid=(select relnamespace from pg_class where oid=t.tgrelid) where n.nspname='carry' and not tgisinternal;")
    pg.run(MINE.read_text())
    assert pg.scalar("select count(*) from pg_policies where schemaname='carry';") == before
    assert pg.scalar("select count(*) from pg_trigger t join pg_namespace n on n.oid=(select relnamespace from pg_class where oid=t.tgrelid) where n.nspname='carry' and not tgisinternal;") == trig


def test_rls_enabled_forced_and_documented_and_public_has_nothing(pg):
    for t in NEW_TABLES:
        assert pg.scalar(f"select relrowsecurity::text||relforcerowsecurity::text from pg_class where oid='carry.{t}'::regclass;") == "truetrue"
        assert int(pg.scalar(f"select count(*) from pg_policies where schemaname='carry' and tablename='{t}';")) >= 3
        assert "RETENTION" in pg.scalar(f"select obj_description('carry.{t}'::regclass);")
        assert pg.scalar(f"select has_table_privilege('public','carry.{t}','select')") == "f"
        assert pg.scalar(f"select has_table_privilege('service_role','carry.{t}','insert')") == "t"
        assert pg.scalar(f"select has_table_privilege('service_role','carry.{t}','update')") == "f"


def test_anon_has_no_access_to_tables_views_or_functions(pg):
    for rel in (*NEW_TABLES, "exec_heartbeats_latest", "exec_lease"):
        pg.fails(f"select * from carry.{rel};", "permission denied", role="anon")
    for t in NEW_TABLES:
        pg.fails(f"insert into carry.{t} default values;", "permission denied", role="anon")
    for call in (f"carry.exec_event_put('{{}}'::jsonb)", f"carry.exec_heartbeat_put('{{}}'::jsonb)",
                 f"carry.exec_lease_acquire('{A}','h',5)", f"carry.exec_lease_renew('{A}','h',1,5)", f"carry.exec_lease_release('{A}','h',1)"):
        pg.fails(f"select {call};", "permission denied", role="anon")
        pg.fails(f"select {call};", "permission denied", role="authenticated", uid=A)


def test_append_only_blocks_update_delete_truncate_on_every_new_table(pg):
    assert put_event(pg, event(A, 1)) == "INSERTED"
    assert put_hb(pg, hbrow(A)) == "INSERTED"
    assert lease(pg, "exec_lease_acquire", A, "h-append", 5) != "NULL"
    for t in NEW_TABLES:
        for stmt in (f"update carry.{t} set source = source;", f"delete from carry.{t};", f"truncate carry.{t} cascade;"):
            pg.fails(stmt, "append-only")                                   # superuser: trigger
            pg.fails(stmt, "permission denied", role="service_role")        # privilege
            pg.fails(stmt, "permission denied", role="authenticated", uid=A)


def test_owner_isolation_and_authenticated_is_read_only(pg):
    assert put_event(pg, event(A, 10)) == "INSERTED" and put_event(pg, event(B, 10)) == "INSERTED"
    assert put_hb(pg, hbrow(A, "wiso")) == "INSERTED" and put_hb(pg, hbrow(B, "wiso")) == "INSERTED"
    assert pg.scalar("select count(distinct owner_id) from carry.exec_events;", role="authenticated", uid=A) == "1"
    assert pg.scalar("select distinct owner_id::text from carry.exec_events;", role="authenticated", uid=B) == B
    assert pg.scalar("select count(*) from carry.exec_events;", role="authenticated") == "0"
    assert pg.scalar("select distinct owner_id::text from carry.exec_heartbeats_latest;", role="authenticated", uid=A) == A
    c = str(uuid.uuid4())
    assert lease(pg, "exec_lease_acquire", c, "h-iso", 30) == "1"
    assert pg.scalar("select count(*) from carry.exec_lease;", role="authenticated", uid=B) == "0"
    assert pg.scalar("select holder from carry.exec_lease;", role="authenticated", uid=c) == "h-iso"
    pg.fails(f"insert into carry.exec_events (event_id,owner_id,kind,seq,payload,occurred_at,content_sha256) "
             f"values ('evt-forbidden-1','{A}','DECISION',1,'{{}}','2026-11-10T00:00:00Z','{sha('z')}');",
             "permission denied", role="authenticated", uid=A)


def test_exec_event_put_inserted_unchanged_conflict_and_never_overwrites(pg):
    e = event(A, 20)
    assert put_event(pg, e) == "INSERTED"
    assert put_event(pg, e) == "UNCHANGED"
    assert put_event(pg, {**e, "content_sha256": sha("different"), "payload": {"n": 999}}) == "CONFLICT"     # same id, other content
    assert pg.scalar(f"select payload->>'n' from carry.exec_events where event_id='{e['event_id']}';") == "20"
    other = event(A, 21, content_sha256=e["content_sha256"])
    assert put_event(pg, other) == "CONFLICT"                                      # same content hash under another id
    assert pg.scalar(f"select count(*) from carry.exec_events where event_id='{other['event_id']}';") == "0"
    assert put_event(pg, {**e, "owner_id": B}) == "CONFLICT"                       # another owner cannot claim or probe the id
    assert pg.scalar(f"select count(*) from carry.exec_events where event_id='{e['event_id']}';") == "1"


def test_exec_event_put_validation(pg):
    pg.fails(f"select carry.exec_event_put($j${json.dumps({**event(A, 30), 'bogus': 1})}$j$::jsonb);", "unknown column", role="service_role")
    pg.fails(f"select carry.exec_event_put($j${json.dumps({**event(A, 31), 'received_at': '2020-01-01T00:00:00Z'})}$j$::jsonb);", "unknown column", role="service_role")
    pg.fails("select carry.exec_event_put('[]'::jsonb);", "JSON object", role="service_role")
    for bad, why in (({"kind": "LIVE_ORDER"}, "violates"), ({"event_id": "short"}, "violates"), ({"payload": []}, "violates"),
                     ({"seq": -1}, "violates"), ({"content_sha256": "abc"}, "violates|invalid"),
                     ({"occurred_at": "2999-01-01T00:00:00Z"}, "future")):
        pg.fails(f"select carry.exec_event_put($j${json.dumps({**event(A, 32), **bad})}$j$::jsonb);", why, role="service_role")
    for kind in ("SESSION_STATE", "DECISION", "OPPORTUNITY_EVAL", "PLAN", "RISK_ASSESSMENT", "ORDER", "FILL", "FUNDING", "EXIT",
                 "RECONCILIATION", "COST", "DAILY_PNL", "INCIDENT", "PROVENANCE"):
        assert put_event(pg, event(A, 33, kind=kind)) == "INSERTED", kind


def test_received_at_and_created_at_are_server_owned(pg):
    e = event(A, 40)
    assert put_event(pg, e) == "INSERTED"
    assert pg.scalar(f"select (received_at > now() - interval '1 minute' and created_at > now() - interval '1 minute')::text "
                     f"from carry.exec_events where event_id='{e['event_id']}';") == "true"


def test_heartbeats_validate_dedupe_and_latest_view(pg):
    assert put_hb(pg, hbrow(A, "wl", 1)) == "INSERTED" and put_hb(pg, hbrow(A, "wl", 1)) == "UNCHANGED"
    assert put_hb(pg, {**hbrow(A, "wl", 1), "disk_free_bytes": 5, "content_sha256": sha("other")}) == "CONFLICT"
    assert put_hb(pg, hbrow(A, "wl", 3, state="EXITING")) == "INSERTED"
    assert put_hb(pg, hbrow(A, "wl", 2, state="FLAT")) == "INSERTED"
    assert put_hb(pg, hbrow(A, "w2", 1)) == "INSERTED"
    latest = pg.run(f"select worker_id||':'||seq||':'||state from carry.exec_heartbeats_latest where owner_id='{A}' and worker_id in ('wl','w2') order by 1;").stdout.split()
    assert latest == ["w2:1:HEDGED", "wl:3:EXITING"]
    for bad in ({"mode": "LIVE"}, {"state": "TRADING"}, {"disk_free_bytes": -1}, {"lease_token": 0}, {"at_ms": 0}, {"outbox_pending": -1}):
        pg.fails(f"select carry.exec_heartbeat_put($j${json.dumps(hbrow(A, 'wbad', 9, **bad))}$j$::jsonb);", "violates", role="service_role")
    pg.fails(f"select carry.exec_heartbeat_put($j${json.dumps({**hbrow(A, 'wbad', 9), 'created_at': '2020-01-01T00:00:00Z'})}$j$::jsonb);",
             "unknown column", role="service_role")


# -- lease ---------------------------------------------------------------------------------------------------------------

def test_lease_basics_token_monotonic_refusal_and_stale_renew(pg):
    o = str(uuid.uuid4())
    assert lease(pg, "exec_lease_acquire", o, "holder-a", 30) == "1"
    assert lease(pg, "exec_lease_acquire", o, "holder-b", 30) == "NULL"                 # another holder's live lease
    assert lease(pg, "exec_lease_renew", o, "holder-b", 1, 30) == "false"               # wrong holder
    assert lease(pg, "exec_lease_renew", o, "holder-a", 99, 30) == "false"              # wrong token
    assert lease(pg, "exec_lease_renew", o, "holder-a", 1, 30) == "true"
    assert lease(pg, "exec_lease_acquire", o, "holder-a", 30) == "2"                    # a NEW acquisition (even by the same name) bumps the token
    assert lease(pg, "exec_lease_renew", o, "holder-a", 1, 30) == "false"              # stale token FAILS
    assert lease(pg, "exec_lease_renew", o, "holder-a", 2, 30) == "true"
    assert lease(pg, "exec_lease_release", o, "holder-b", 2) == "false"
    assert lease(pg, "exec_lease_release", o, "holder-a", 1) == "false"
    assert lease(pg, "exec_lease_release", o, "holder-a", 2) == "true"
    assert lease(pg, "exec_lease_renew", o, "holder-a", 2, 30) == "false"               # released
    assert lease(pg, "exec_lease_acquire", o, "holder-b", 30) == "3"                    # strictly increasing after release
    assert pg.scalar(f"select count(*) from carry.exec_lease where owner_id='{o}';") == "1"      # single current row per owner
    assert pg.run(f"select holder||':'||fencing_token||':'||held from carry.exec_lease where owner_id='{o}';").stdout.strip() == "holder-b:3:true"
    toks = pg.run(f"select fencing_token from carry.exec_lease_log where owner_id='{o}' order by log_seq;").stdout.split()
    assert [int(t) for t in toks] == sorted(int(t) for t in toks)                      # never decreases in the log


def test_expired_lease_can_be_taken_over_and_the_old_holder_cannot_renew(pg):
    o = str(uuid.uuid4())
    assert lease(pg, "exec_lease_acquire", o, "holder-a", 1) == "1"
    time.sleep(1.3)
    assert lease(pg, "exec_lease_renew", o, "holder-a", 1, 30) == "false"               # expired: fail closed, re-acquire instead
    assert pg.run(f"select held from carry.exec_lease where owner_id='{o}';").stdout.strip() == "f"
    assert lease(pg, "exec_lease_acquire", o, "holder-b", 30) == "2"
    assert lease(pg, "exec_lease_renew", o, "holder-a", 1, 30) == "false"
    assert lease(pg, "exec_lease_renew", o, "holder-b", 2, 30) == "true"


def test_two_holders_racing_exactly_one_wins(pg):
    for rnd in range(5):
        o = str(uuid.uuid4())
        out = {}
        barrier = threading.Barrier(2)

        def go(name):
            barrier.wait()
            out[name] = lease(pg, "exec_lease_acquire", o, name, 60)

        ts = [threading.Thread(target=go, args=(n,)) for n in ("racer-a", "racer-b")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(out.values()) == ["1", "NULL"], (rnd, out)
        assert pg.scalar(f"select count(*) from carry.exec_lease_log where owner_id='{o}';") == "1"


def test_leases_are_per_owner_and_bad_arguments_are_refused(pg):
    o1, o2 = str(uuid.uuid4()), str(uuid.uuid4())
    assert lease(pg, "exec_lease_acquire", o1, "h", 30) == "1" and lease(pg, "exec_lease_acquire", o2, "h2", 30) == "1"
    pg.fails(f"select carry.exec_lease_acquire('{o1}','h',0);", "ttl_seconds", role="service_role")
    pg.fails(f"select carry.exec_lease_acquire('{o1}','h',99999);", "ttl_seconds", role="service_role")
    pg.fails(f"select carry.exec_lease_acquire('{o1}',' ',30);", "bad owner or holder", role="service_role")
    pg.fails(f"select carry.exec_lease_renew('{o1}','h',1,0);", "ttl_seconds", role="service_role")
    assert pg.scalar(f"select carry.exec_lease_renew(null,'h',1,30)::text;", role="service_role") == "false"


def test_the_lease_functions_cannot_be_used_to_tamper_with_the_log(pg):
    o = str(uuid.uuid4())
    assert lease(pg, "exec_lease_acquire", o, "h", 30) == "1"
    pg.fails(f"insert into carry.exec_lease_log (owner_id,holder,fencing_token,action,expires_at,content_sha256) "
             f"values ('{o}','x',1,'RENEW',now(),'{sha('t')}') ;", "permission denied|violates", role="authenticated", uid=o)
    pg.fails(f"insert into carry.exec_lease_log (owner_id,holder,fencing_token,action,expires_at,content_sha256) "
             f"values ('{o}','x',0,'ACQUIRE',now(),'{sha('t2')}');", "violates", role="service_role")
    pg.fails(f"insert into carry.exec_lease_log (owner_id,holder,fencing_token,action,expires_at,content_sha256) "
             f"values ('{o}','x',5,'STEAL',now(),'{sha('t3')}');", "violates", role="service_role")
    pg.fails("select carry_private.exec_lease_append(gen_random_uuid(),'x',1,'ACQUIRE',now());", "permission denied", role="authenticated", uid=o)


def test_existing_testnet_mechanics_refusal_is_untouched(pg):
    # the rt5 trigger is still installed and my tables did not add a path around it
    assert int(pg.scalar("select count(*) from pg_trigger t join pg_class c on c.oid=t.tgrelid where c.relname='experiment_evidence' and not t.tgisinternal;")) >= 3
    assert pg.scalar("select (string_agg(prosrc, ' ') like '%TESTNET_MECHANICS%')::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='carry_private';") == "true"
