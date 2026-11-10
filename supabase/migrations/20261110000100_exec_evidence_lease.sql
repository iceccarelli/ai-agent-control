-- 20261110000100_exec_evidence_lease.sql
-- Execution EVIDENCE path (Fly worker -> Supabase) and a GLOBAL single-writer executor lease with a fencing token.
-- ADDITIVE and idempotent: nothing already numbered is edited, no existing policy / trigger / grant is touched or
-- relaxed, and the TESTNET_MECHANICS refusal of the rt5 migration is not bypassed (these tables hold execution
-- telemetry, they are not experiment_evidence).
--
-- WHAT THIS IS NOT
--  * Not a trading authority and not a command channel. The venue's own position read and the reconciled owner
--    accounting stay the financial truth. Nothing here is ever read back to decide to place, size or close an order.
--    The lease only answers "may this worker keep executing", and a worker that cannot prove it holds the lease
--    must stop (fail closed, see bot/tools/exec_evidence.py). It is a coordination fence, not an instruction.
--  * No live-trading concept: heartbeats carry mode PAPER or TESTNET only.
--
-- DESIGN
--  * Every table goes through carry_private.secure_table (RLS enabled + forced, owner-isolated read for
--    `authenticated`, restrictive deny for `anon`, service-role INSERT/SELECT only, statement triggers refuse
--    UPDATE / DELETE / TRUNCATE for every role) and carries the server-owned id / created_at stamp trigger.
--  * The lease is APPEND-ONLY too: carry.exec_lease_log records every ACQUIRE / RENEW / RELEASE as a new row, and the
--    single-row-per-owner view carry.exec_lease is simply the newest row. No function needs elevated privileges and
--    no role gets UPDATE: the functions run as the caller (the service role), serialised per owner by an advisory
--    transaction lock.
--  * Fencing token: bigint, strictly increasing per owner across every NEW acquisition (also by the same holder name,
--    so a restarted process fences out its own predecessor). RENEW keeps the token; a renew with a stale token, a
--    different holder, a released lease or an EXPIRED lease returns false (re-acquire instead, which bumps the token).
--  * Idempotent event writer carry.exec_event_put returns INSERTED / UNCHANGED / CONFLICT:
--      INSERTED   a new event_id was stored.
--      UNCHANGED  event_id exists with the same owner and the same content_sha256 (a safe re-send).
--      CONFLICT   event_id exists with a different hash or owner, or the hash is already stored under another event_id.
--                 Nothing is overwritten; the stored row stays.
--
-- NOT CLOSABLE AT DATABASE LEVEL (documented, not hidden)
--  * content_sha256 correctness: the canonical-JSON hash is application-defined; SQL cannot recompute it.
--  * A service-role writer can state a false event. This schema stores records, not proof.
--  * Server clock only bounds event times (no more than 5 minutes ahead); event_id / seq are the writer's.
--  * PostgREST exposure: schema `carry` is deliberately NOT in the exposed-schemas list (foundation migration). The
--    worker calls the functions below through /rpc with Content-Profile: carry, which needs the owner to expose the
--    schema (or to front these functions) - that is an owner decision this migration does not make.
--
-- RETENTION: events are audit class; heartbeats and the lease log are observation class (400 days online, then the
-- owner-run offline procedure described in 20261107000700). Append-only for every role regardless.

create table if not exists carry.exec_events (
  event_id text primary key check (event_id ~ '^[A-Za-z0-9._:=-]{8,200}$'),
  id uuid not null default gen_random_uuid(),
  owner_id uuid not null,
  kind text not null check (kind in ('SESSION_STATE', 'DECISION', 'OPPORTUNITY_EVAL', 'PLAN', 'RISK_ASSESSMENT', 'ORDER',
                                     'FILL', 'FUNDING', 'EXIT', 'RECONCILIATION', 'COST', 'DAILY_PNL', 'INCIDENT', 'PROVENANCE')),
  session_id text check (session_id is null or (carry_private.nonblank(session_id) and length(session_id) <= 200)),
  seq bigint not null check (seq >= 0),
  payload jsonb not null check (jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 262144),
  occurred_at timestamptz not null,
  received_at timestamptz not null default now(),
  source text not null default 'tools/exec_evidence.py' check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.exec_events');
create index if not exists exec_events_owner_session_idx on carry.exec_events (owner_id, session_id, seq);
create index if not exists exec_events_owner_kind_idx on carry.exec_events (owner_id, kind, occurred_at);

create table if not exists carry.exec_heartbeats (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  worker_id text not null check (carry_private.nonblank(worker_id) and length(worker_id) <= 200),
  app text not null check (carry_private.nonblank(app)),
  machine_id text not null check (carry_private.nonblank(machine_id)),
  region text not null check (carry_private.nonblank(region)),
  code_revision text not null check (carry_private.nonblank(code_revision)),
  runtime_version text not null check (carry_private.nonblank(runtime_version)),
  run_id text not null check (carry_private.nonblank(run_id)),
  mode text not null check (mode in ('PAPER', 'TESTNET')),
  state text not null check (state in ('INITIALISING', 'DATA_CHECK', 'RESEARCHING', 'WAITING_FOR_OPPORTUNITY', 'PLAN_PROPOSED',
                                       'RISK_APPROVED', 'EXECUTING', 'HEDGED', 'EXITING', 'RECONCILING', 'FLAT', 'PAUSED',
                                       'ALERT', 'STOPPED', 'RECOVERING', 'UNRECONCILED', 'DRAINING')),
  session_id text check (session_id is null or carry_private.nonblank(session_id)),
  last_bybit_read_ms bigint check (last_bybit_read_ms is null or last_bybit_read_ms >= 0),
  last_market_data_ms bigint check (last_market_data_ms is null or last_market_data_ms >= 0),
  last_reconcile_ms bigint check (last_reconcile_ms is null or last_reconcile_ms >= 0),
  last_supabase_sync_ms bigint check (last_supabase_sync_ms is null or last_supabase_sync_ms >= 0),
  outbox_pending integer not null check (outbox_pending >= 0),
  disk_free_bytes bigint not null check (disk_free_bytes >= 0),
  lease_token bigint check (lease_token is null or lease_token >= 1),
  seq bigint not null check (seq >= 0),
  at_ms bigint not null check (at_ms > 0),
  source text not null default 'tools/exec_evidence.py' check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, worker_id, run_id, seq),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.exec_heartbeats');
create index if not exists exec_heartbeats_latest_idx on carry.exec_heartbeats (owner_id, worker_id, at_ms desc, seq desc);

create table if not exists carry.exec_lease_log (
  id uuid primary key default gen_random_uuid(),
  log_seq bigint generated always as identity,
  owner_id uuid not null,
  holder text not null check (carry_private.nonblank(holder) and length(holder) <= 200),
  fencing_token bigint not null check (fencing_token >= 1),
  action text not null check (action in ('ACQUIRE', 'RENEW', 'RELEASE')),
  expires_at timestamptz not null,
  source text not null default 'carry.exec_lease' check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, log_seq),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.exec_lease_log');

do $$
declare
  t text;
begin
  foreach t in array array['exec_events', 'exec_heartbeats', 'exec_lease_log'] loop
    execute format('create or replace trigger carry_stamp_insert before insert on carry.%I for each row execute function carry_private.stamp_insert()', t);
  end loop;
end $$;

comment on table carry.exec_events is
  'RETENTION: audit class, programme life + 7 years, append-only; removal only by owner-run offline procedure. Execution evidence/telemetry, NOT a trading ledger: the venue read and owner accounting stay the truth. Writer: service role. Readers: owner_id = auth.uid().';
comment on table carry.exec_heartbeats is
  'RETENTION: observation class, 400 days online, append-only; removal only by owner-run offline procedure. Worker liveness telemetry, never a command channel. Writer: service role. Readers: owner_id = auth.uid().';
comment on table carry.exec_lease_log is
  'RETENTION: observation class, 400 days online, append-only; removal only by owner-run offline procedure. Executor lease history (fencing tokens); a coordination fence, not trading authority. Writer: service role (via carry.exec_lease_*). Readers: owner_id = auth.uid().';

-- latest heartbeat per worker (invoker rights: RLS of the caller applies) ---------------------------------------------
create or replace view carry.exec_heartbeats_latest with (security_invoker = true) as
  select distinct on (owner_id, worker_id) *
    from carry.exec_heartbeats
   order by owner_id, worker_id, at_ms desc, seq desc, created_at desc;

-- the single current lease row per owner: the newest log row ---------------------------------------------------------
create or replace view carry.exec_lease with (security_invoker = true) as
  select distinct on (owner_id) owner_id, holder, fencing_token, action, expires_at,
         (action <> 'RELEASE' and expires_at > clock_timestamp()) as held, created_at as last_change_at
    from carry.exec_lease_log
   order by owner_id, log_seq desc;

revoke all on carry.exec_heartbeats_latest from public, anon, authenticated, service_role;
revoke all on carry.exec_lease from public, anon, authenticated, service_role;
grant select on carry.exec_heartbeats_latest to authenticated, service_role;
grant select on carry.exec_lease to authenticated, service_role;

-- idempotent writers -------------------------------------------------------------------------------------------------
create or replace function carry.exec_event_put(p_row jsonb) returns text
language plpgsql set search_path = pg_catalog
as $$
declare
  v_n bigint;
  v_id text;
  v_owner uuid;
  v_hash text;
  v_at timestamptz;
  s_owner uuid;
  s_hash text;
begin
  if p_row is null or jsonb_typeof(p_row) <> 'object' then raise exception 'row must be a JSON object'; end if;
  if exists (select 1 from jsonb_object_keys(p_row) k
             where k not in ('event_id', 'owner_id', 'kind', 'session_id', 'seq', 'payload', 'occurred_at',
                             'source', 'schema_version', 'content_sha256')) then
    raise exception 'unknown column(s) for carry.exec_events';
  end if;
  v_id := p_row ->> 'event_id';
  v_owner := (p_row ->> 'owner_id')::uuid;
  v_hash := p_row ->> 'content_sha256';
  v_at := (p_row ->> 'occurred_at')::timestamptz;
  if v_at > now() + interval '5 minutes' then
    raise exception 'carry.exec_events: occurred_at is in the future (> now() + 5 minutes)' using errcode = 'check_violation';
  end if;
  insert into carry.exec_events (event_id, owner_id, kind, session_id, seq, payload, occurred_at, received_at, source,
                                 schema_version, content_sha256)
    select r.event_id, r.owner_id, r.kind, r.session_id, r.seq, r.payload, r.occurred_at, now(),
           coalesce(r.source, 'tools/exec_evidence.py'), coalesce(r.schema_version, 1), r.content_sha256
      from jsonb_populate_record(null::carry.exec_events, p_row) r
    on conflict do nothing;
  get diagnostics v_n = row_count;
  if v_n = 1 then return 'INSERTED'; end if;
  select e.owner_id, e.content_sha256 into s_owner, s_hash from carry.exec_events e where e.event_id = v_id;
  if found and s_owner = v_owner and s_hash = v_hash then return 'UNCHANGED'; end if;
  return 'CONFLICT';
end $$;

create or replace function carry.exec_heartbeat_put(p_row jsonb) returns text
language plpgsql set search_path = pg_catalog
as $$
declare
  v_n bigint;
  v_owner uuid;
  v_hash text;
  s_hash text;
begin
  if p_row is null or jsonb_typeof(p_row) <> 'object' then raise exception 'row must be a JSON object'; end if;
  if exists (select 1 from jsonb_object_keys(p_row) k
             where k not in ('owner_id', 'worker_id', 'app', 'machine_id', 'region', 'code_revision', 'runtime_version',
                             'run_id', 'mode', 'state', 'session_id', 'last_bybit_read_ms', 'last_market_data_ms',
                             'last_reconcile_ms', 'last_supabase_sync_ms', 'outbox_pending', 'disk_free_bytes',
                             'lease_token', 'seq', 'at_ms', 'source', 'schema_version', 'content_sha256')) then
    raise exception 'unknown column(s) for carry.exec_heartbeats';
  end if;
  v_owner := (p_row ->> 'owner_id')::uuid;
  v_hash := p_row ->> 'content_sha256';
  insert into carry.exec_heartbeats (owner_id, worker_id, app, machine_id, region, code_revision, runtime_version, run_id,
                                     mode, state, session_id, last_bybit_read_ms, last_market_data_ms, last_reconcile_ms,
                                     last_supabase_sync_ms, outbox_pending, disk_free_bytes, lease_token, seq, at_ms,
                                     source, schema_version, content_sha256)
    select r.owner_id, r.worker_id, r.app, r.machine_id, r.region, r.code_revision, r.runtime_version, r.run_id,
           r.mode, r.state, r.session_id, r.last_bybit_read_ms, r.last_market_data_ms, r.last_reconcile_ms,
           r.last_supabase_sync_ms, r.outbox_pending, r.disk_free_bytes, r.lease_token, r.seq, r.at_ms,
           coalesce(r.source, 'tools/exec_evidence.py'), coalesce(r.schema_version, 1), r.content_sha256
      from jsonb_populate_record(null::carry.exec_heartbeats, p_row) r
    on conflict do nothing;
  get diagnostics v_n = row_count;
  if v_n = 1 then return 'INSERTED'; end if;
  select h.content_sha256 into s_hash from carry.exec_heartbeats h
   where h.owner_id = v_owner and h.worker_id = p_row ->> 'worker_id' and h.run_id = p_row ->> 'run_id'
     and h.seq = (p_row ->> 'seq')::bigint;
  if found and s_hash = v_hash then return 'UNCHANGED'; end if;
  return 'CONFLICT';
end $$;

-- the lease ----------------------------------------------------------------------------------------------------------
create or replace function carry_private.exec_lease_append(p_owner uuid, p_holder text, p_token bigint, p_action text,
                                                           p_expires timestamptz) returns void
language plpgsql set search_path = pg_catalog
as $$
begin
  insert into carry.exec_lease_log (owner_id, holder, fencing_token, action, expires_at, content_sha256)
  values (p_owner, p_holder, p_token, p_action, p_expires,
          encode(sha256(convert_to(format('%s|%s|%s|%s|%s|%s|%s', p_owner, p_holder, p_token, p_action, p_expires,
                                          clock_timestamp(), gen_random_uuid()), 'UTF8')), 'hex'));
end $$;

-- Returns the new fencing token, or NULL when another holder's unexpired lease is in force.
create or replace function carry.exec_lease_acquire(p_owner uuid, p_holder text, p_ttl_seconds integer) returns bigint
language plpgsql set search_path = pg_catalog
as $$
declare
  cur record;
  v_token bigint;
begin
  if p_owner is null or not carry_private.nonblank(p_holder) or length(p_holder) > 200 then raise exception 'bad owner or holder'; end if;
  if p_ttl_seconds is null or p_ttl_seconds < 1 or p_ttl_seconds > 3600 then raise exception 'ttl_seconds must be 1..3600'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_owner::text || ':exec_lease', 0));
  select l.holder, l.action, l.expires_at into cur from carry.exec_lease_log l
   where l.owner_id = p_owner order by l.log_seq desc limit 1;
  if found and cur.action <> 'RELEASE' and cur.expires_at > clock_timestamp() and cur.holder <> p_holder then
    return null;
  end if;
  select coalesce(max(l.fencing_token), 0) + 1 into v_token from carry.exec_lease_log l where l.owner_id = p_owner;
  perform carry_private.exec_lease_append(p_owner, p_holder, v_token, 'ACQUIRE',
                                          clock_timestamp() + make_interval(secs => p_ttl_seconds));
  return v_token;
end $$;

-- true only for the current, unexpired, unreleased lease of exactly this holder and token.
create or replace function carry.exec_lease_renew(p_owner uuid, p_holder text, p_token bigint, p_ttl_seconds integer) returns boolean
language plpgsql set search_path = pg_catalog
as $$
declare
  cur record;
begin
  if p_owner is null or p_holder is null or p_token is null then return false; end if;
  if p_ttl_seconds is null or p_ttl_seconds < 1 or p_ttl_seconds > 3600 then raise exception 'ttl_seconds must be 1..3600'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_owner::text || ':exec_lease', 0));
  select l.holder, l.fencing_token, l.action, l.expires_at into cur from carry.exec_lease_log l
   where l.owner_id = p_owner order by l.log_seq desc limit 1;
  if not found or cur.action = 'RELEASE' or cur.holder <> p_holder or cur.fencing_token <> p_token
     or cur.expires_at <= clock_timestamp() then
    return false;
  end if;
  perform carry_private.exec_lease_append(p_owner, p_holder, p_token, 'RENEW',
                                          clock_timestamp() + make_interval(secs => p_ttl_seconds));
  return true;
end $$;

-- true when this holder's current lease was released; false (nothing written) for any stale or foreign release.
create or replace function carry.exec_lease_release(p_owner uuid, p_holder text, p_token bigint) returns boolean
language plpgsql set search_path = pg_catalog
as $$
declare
  cur record;
begin
  if p_owner is null or p_holder is null or p_token is null then return false; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_owner::text || ':exec_lease', 0));
  select l.holder, l.fencing_token, l.action into cur from carry.exec_lease_log l
   where l.owner_id = p_owner order by l.log_seq desc limit 1;
  if not found or cur.action = 'RELEASE' or cur.holder <> p_holder or cur.fencing_token <> p_token then
    return false;
  end if;
  perform carry_private.exec_lease_append(p_owner, p_holder, p_token, 'RELEASE', clock_timestamp());
  return true;
end $$;

revoke all on function carry.exec_event_put(jsonb) from public;
revoke all on function carry.exec_heartbeat_put(jsonb) from public;
revoke all on function carry_private.exec_lease_append(uuid, text, bigint, text, timestamptz) from public;
revoke all on function carry.exec_lease_acquire(uuid, text, integer) from public;
revoke all on function carry.exec_lease_renew(uuid, text, bigint, integer) from public;
revoke all on function carry.exec_lease_release(uuid, text, bigint) from public;
grant execute on function carry.exec_event_put(jsonb) to service_role;
grant execute on function carry.exec_heartbeat_put(jsonb) to service_role;
grant execute on function carry_private.exec_lease_append(uuid, text, bigint, text, timestamptz) to service_role;
grant execute on function carry.exec_lease_acquire(uuid, text, integer) to service_role;
grant execute on function carry.exec_lease_renew(uuid, text, bigint, integer) to service_role;
grant execute on function carry.exec_lease_release(uuid, text, bigint) to service_role;

-- Fail the migration (rolling it back) if the new tables escaped the baseline.
do $$
declare
  t text;
begin
  foreach t in array array['exec_events', 'exec_heartbeats', 'exec_lease_log'] loop
    if not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                   where n.nspname = 'carry' and c.relname = t and c.relrowsecurity and c.relforcerowsecurity) then
      raise exception 'carry.% lacks enabled+forced RLS', t;
    end if;
    if has_table_privilege('anon', format('carry.%I', t), 'select, insert, update, delete')
       or has_table_privilege('public'::name, format('carry.%I', t), 'select')
       or has_table_privilege('service_role', format('carry.%I', t), 'update, delete, truncate') then
      raise exception 'carry.% has a privilege it must not have', t;
    end if;
    if not exists (select 1 from pg_trigger g where g.tgrelid = format('carry.%I', t)::regclass
                   and g.tgname = 'carry_stamp_insert' and not g.tgisinternal) then
      raise exception 'carry.% has no stamp trigger', t;
    end if;
  end loop;
end $$;
