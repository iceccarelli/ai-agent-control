-- 20261107000100_carry_foundation.sql
-- Structured application data for the BTC funding-carry research programme (paper/testnet only).
--
-- OPERATOR NOTES (read before applying anywhere):
--  * Nothing here is a trading ledger. The venue's own reads and the reconciled owner accounting stay the
--    source of truth. These tables hold registrations, evidence, manifests/hashes/refs, decisions and receipts.
--  * Large raw market data never goes in Postgres: store only sha256, byte counts, segment ids and a storage ref.
--  * Schema `carry` is deliberately NOT in the PostgREST exposed-schemas list. Do not add it in the dashboard
--    or config without a separate owner review; RLS below is defence in depth, not the only gate.
--  * Writers: the service role, server-side only. `authenticated` is read-only and sees only rows whose
--    owner_id = auth.uid(). `anon` has no privileges and an explicit restrictive deny policy.
--  * Every table is append-only (statement-level trigger blocks UPDATE/DELETE/TRUNCATE for every role, and
--    UPDATE/DELETE/TRUNCATE privileges are granted to nobody). State changes are new rows (events).
--  * No live-trading concept exists in this schema: execution modes are PAPER/TESTNET only and no approval
--    kind can represent the promotion gate. Nothing here signs or weakens that gate.
--  * Applying: owner reviews the exact diff, then `supabase db push` against the owner's own project.
--    This file set must never be applied by an agent. It is idempotent (re-applying is safe).

create schema if not exists carry;
create schema if not exists carry_private;

revoke all on schema carry from public;
revoke all on schema carry_private from public;
grant usage on schema carry to authenticated, service_role;
grant usage on schema carry_private to service_role;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'carry' and t.typname = 'sha256_hex') then
    create domain carry.sha256_hex as text check (value ~ '^[0-9a-f]{64}$');
  end if;
end $$;

-- Non-empty JSON object/array (the registry rejects None, "", [] and {} for required fields).
create or replace function carry_private.nonempty_json(j jsonb) returns boolean
language sql immutable parallel safe set search_path = pg_catalog
as $$ select j is not null and jsonb_typeof(j) in ('object', 'array') and j <> '{}'::jsonb and j <> '[]'::jsonb $$;

create or replace function carry_private.nonblank(t text) returns boolean
language sql immutable parallel safe set search_path = pg_catalog
as $$ select t is not null and length(btrim(t)) > 0 $$;

create or replace function carry_private.forbid_mutation() returns trigger
language plpgsql set search_path = ''
as $$
begin
  raise exception 'carry.% is append-only: % is not permitted (retention is an owner-run offline procedure)',
    tg_table_name, tg_op using errcode = 'insufficient_privilege';
end $$;

-- Apply the full security baseline to one carry table. Every table MUST be passed through this.
create or replace function carry_private.secure_table(p_table regclass) returns void
language plpgsql set search_path = pg_catalog
as $$
declare
  v_schema text; v_name text; v_short text;
begin
  select n.nspname, c.relname into v_schema, v_name
    from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.oid = p_table;
  if v_schema <> 'carry' then raise exception 'secure_table only handles schema carry, got %', v_schema; end if;
  if not exists (select 1 from pg_attribute where attrelid = p_table and attname = 'owner_id' and attnotnull) then
    raise exception 'carry.% needs a NOT NULL owner_id column', v_name;
  end if;
  v_short := left(v_name, 40);

  execute format('alter table %s enable row level security', p_table);
  execute format('alter table %s force row level security', p_table);

  execute format('revoke all on table %s from public, anon, authenticated, service_role', p_table);
  execute format('grant select on table %s to authenticated', p_table);
  execute format('grant select, insert on table %s to service_role', p_table);

  if not exists (select 1 from pg_policies where schemaname = 'carry' and tablename = v_name and policyname = v_short || '_owner_read') then
    execute format('create policy %I on %s for select to authenticated using (owner_id = (select auth.uid()))',
                   v_short || '_owner_read', p_table);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'carry' and tablename = v_name and policyname = v_short || '_anon_deny') then
    execute format('create policy %I on %s as restrictive for all to anon using (false) with check (false)',
                   v_short || '_anon_deny', p_table);
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'carry' and tablename = v_name and policyname = v_short || '_service_rw') then
    execute format('create policy %I on %s for all to service_role using (true) with check (true)',
                   v_short || '_service_rw', p_table);
  end if;

  execute format('create or replace trigger %I before update or delete or truncate on %s for each statement execute function carry_private.forbid_mutation()',
                 v_short || '_append_only', p_table);
end $$;

-- Idempotent writer: `insert ... on conflict do nothing` for any carry table, from a JSON object.
-- Only the keys supplied are inserted (table defaults still apply); unknown keys are rejected.
-- Returns true when a row was inserted, false when an equal natural/content key already existed.
create or replace function carry.put_row(p_table text, p_row jsonb) returns boolean
language plpgsql set search_path = pg_catalog
as $$
declare
  v_oid regclass; v_cols text; v_n bigint;
begin
  if p_table is null or p_table !~ '^[a-z][a-z0-9_]*$' then raise exception 'bad table name'; end if;
  if p_row is null or jsonb_typeof(p_row) <> 'object' then raise exception 'row must be a JSON object'; end if;
  select c.oid into v_oid from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'carry' and c.relname = p_table and c.relkind = 'r';
  if v_oid is null then raise exception 'unknown carry table %', p_table; end if;
  if exists (select 1 from jsonb_object_keys(p_row) k
             where not exists (select 1 from pg_attribute a where a.attrelid = v_oid and a.attnum > 0
                                 and not a.attisdropped and a.attname::text = k)) then
    raise exception 'unknown column(s) for carry.%', p_table;
  end if;
  select string_agg(quote_ident(a.attname::text), ', ' order by a.attnum) into v_cols
    from pg_attribute a where a.attrelid = v_oid and a.attnum > 0 and not a.attisdropped and p_row ? a.attname::text;
  execute format('insert into carry.%I (%s) select %s from jsonb_populate_record(null::carry.%I, $1) on conflict do nothing',
                 p_table, v_cols, v_cols, p_table) using p_row;
  get diagnostics v_n = row_count;
  return v_n = 1;
end $$;

-- Functions: no PUBLIC execute; only the service role may call the writer.
revoke all on function carry_private.nonempty_json(jsonb) from public;
revoke all on function carry_private.nonblank(text) from public;
revoke all on function carry_private.forbid_mutation() from public;
revoke all on function carry_private.secure_table(regclass) from public;
revoke all on function carry.put_row(text, jsonb) from public;
grant execute on function carry_private.nonempty_json(jsonb) to service_role, authenticated;
grant execute on function carry_private.nonblank(text) to service_role, authenticated;
grant execute on function carry.put_row(text, jsonb) to service_role;
