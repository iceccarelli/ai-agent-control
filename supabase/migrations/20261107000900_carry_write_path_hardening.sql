-- 20261107000900_carry_write_path_hardening.sql
-- Closes the write-path gaps found by the second red-team pass (probes replayed as executed tests in
-- bot/tests/test_supabase_migrations.py). ADDITIVE and idempotent: nothing already numbered is edited, no table is
-- created, nothing is granted, and no RLS / append-only guarantee is relaxed. Re-applying is safe.
--
-- CLOSED HERE, in the database
--  1. Server-owned bookkeeping. A BEFORE INSERT row trigger (carry_private.stamp_insert) on EVERY carry table sets
--     created_at := now() and id := gen_random_uuid() and ignores whatever the client sent. A writer could previously
--     forge both (backdated created_at, a chosen id).
--  2. No future-dated events. The same trigger refuses an event-time column (recorded_utc, decided_utc, observed_utc,
--     imported_utc, started_utc, taken_utc, ...) later than now() + 5 minutes. "Latest recorded_utc wins" semantics
--     (owner import status events) could otherwise be captured forever by a row dated year 9999. Forward-looking
--     columns (effective_from_utc, period_*, window_*) are deliberately not checked.
--  3. Execution sessions are sandbox-only. execution_sessions.venue was free text, so PAPER could pair with
--     'mainnet'. It is now restricted to the registered SANDBOX names of bot/venues.py ('testnet', 'demo'): 'mainnet'
--     (LIVE) and 'eu' (registered UNQUALIFIED, refused everywhere) and any unknown name are rejected, in both modes.
--     bot/tests/test_supabase_migrations.py pins this list to venues.py so the two cannot drift.
--  4. Supabase is not a second financial truth. order_refs, fill_refs and account_snapshots are written by no code
--     in this repository (bot/tools/supabase_sync.py maps none of them) yet accepted order rows and arbitrary
--     balances / personal data in `summary`. INSERT is revoked from service_role AND a BEFORE INSERT statement
--     trigger refuses inserts for every role (including the table owner), so re-running an earlier migration
--     (secure_table re-grants INSERT) cannot silently re-open them. Reading stays as before. Re-enabling one needs a
--     reviewed migration that removes the trigger, an owner decision, and a writer in the repository.
--  5. VERIFIED / RECONCILED status events must carry evidence_ref = the import's own row hash (import_id), in the
--     database as well as in bot/tools/supabase_sync.py (previously any non-blank string was accepted here).
--
-- The new CHECK constraints are NOT VALID: they are enforced for every new row from now on but do not rescan rows
-- that were written before this migration (an existing deployment may hold one; the owner can inspect and then
-- `alter table ... validate constraint ...`). On a fresh database there is nothing to rescan.
--
-- NOT CLOSABLE AT DATABASE LEVEL (documented, not hidden)
--  * A forged owner approval. approval_records requires approver_id = owner_id and a statement hash, but the
--    service role IS the trusted writer; Postgres cannot tell a genuine owner decision from a writer that states
--    one. The promotion gate is not representable here at all (no approval kind), and the owner's own signature
--    stays outside this database.
--  * content_sha256 correctness. The canonical-JSON hash is application-defined; SQL cannot recompute it, so a
--    writer can store a row under a hash that does not match its body. (Mitigated only by bot-side verification.)
--  * The table owner / a database superuser can disable triggers, change session_replication_role or delete rows.
--    That is the documented owner-run offline retention procedure (see 20261107000700); the service role cannot
--    (it holds no ownership, no superuser and no UPDATE/DELETE/TRUNCATE privilege), and this migration adds none.
--  * A conflicting resend under the same natural key. carry.put_row returns false both for "this exact row
--    already exists" and for "a row with the same natural key but DIFFERENT content exists" (the new row is
--    discarded, the stored one stays; nothing is overwritten). Making put_row raise for the second case was built
--    and works, but it turns the writer's CONFLICT verdict into a generic FAILED: bot/tools/supabase_sync.py
--    deliberately reads put_row = false as "ask the database which of the two it was" and reports CONFLICT (pinned by
--    tests/test_supabase_sync.py::test_conflicting_duplicate_is_reported_against_real_postgres). So the conflict is
--    surfaced by the writer, not by SQL, and put_row keeps its contract. Owner decision if a DB-level error is
--    wanted instead: change both together.
--  * A service-role writer can still invent a row that is internally consistent (a plausible but false hypothesis
--    or evidence record). This schema stores records, not proof.

-- 1 + 2: server-owned id / created_at and a ceiling on event times -----------------------------------------------
create or replace function carry_private.stamp_insert() returns trigger
language plpgsql set search_path = pg_catalog
as $$
declare
  k text;
  v timestamptz;
  j jsonb := to_jsonb(new);
begin
  foreach k in array array['recorded_utc', 'decided_utc', 'detected_utc', 'observed_utc', 'imported_utc', 'registered_utc',
                           'started_utc', 'taken_utc', 'captured_utc', 'opened_utc', 'restored_utc', 'sealed_utc',
                           'last_record_utc'] loop
    if j ? k and j ->> k is not null then
      v := (j ->> k)::timestamptz;
      if v > now() + interval '5 minutes' then
        raise exception 'carry.%: % is in the future (% > now() + 5 minutes); event times are not accepted ahead of the server clock',
          tg_table_name, k, v using errcode = 'check_violation';
      end if;
    end if;
  end loop;
  -- server-owned bookkeeping: whatever the client supplied is discarded
  new := jsonb_populate_record(new, jsonb_build_object('id', gen_random_uuid(), 'created_at', now()));
  return new;
end $$;
revoke all on function carry_private.stamp_insert() from public;

do $$
declare
  r record;
begin
  for r in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'carry' and c.relkind = 'r' loop
    if not exists (select 1 from pg_attribute a where a.attrelid = format('carry.%I', r.relname)::regclass
                   and a.attname in ('id', 'created_at') and a.attnum > 0 and not a.attisdropped having count(*) = 2) then
      raise exception 'carry.% lacks id/created_at', r.relname;
    end if;
    execute format('create or replace trigger carry_stamp_insert before insert on carry.%I for each row execute function carry_private.stamp_insert()',
                   r.relname);
  end loop;
end $$;

-- 3: execution sessions pair PAPER/TESTNET only with a registered sandbox venue --------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint where conrelid = 'carry.execution_sessions'::regclass
                 and conname = 'execution_sessions_mode_sandbox_venue') then
    alter table carry.execution_sessions add constraint execution_sessions_mode_sandbox_venue
      check ((mode = 'PAPER' and venue in ('testnet', 'demo')) or (mode = 'TESTNET' and venue in ('testnet', 'demo')))
      not valid;
  end if;
end $$;

-- 4: no order / fill / balance tables that anything could write -----------------------------------------------------
create or replace function carry_private.forbid_insert() returns trigger
language plpgsql set search_path = ''
as $$
begin
  raise exception 'carry.% accepts no writes: Supabase is not a trading or account ledger (no writer exists for it in this repository)',
    tg_table_name using errcode = 'insufficient_privilege';
end $$;
revoke all on function carry_private.forbid_insert() from public;

do $$
declare
  t text;
begin
  foreach t in array array['order_refs', 'fill_refs', 'account_snapshots'] loop
    execute format('revoke insert on table carry.%I from service_role', t);
    execute format('create or replace trigger carry_no_insert before insert on carry.%I for each statement execute function carry_private.forbid_insert()', t);
  end loop;
end $$;

-- 5: VERIFIED / RECONCILED evidence is the import's own row hash ---------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint where conrelid = 'carry.owner_account_import_status_events'::regclass
                 and conname = 'owner_import_status_evidence_is_import_hash') then
    alter table carry.owner_account_import_status_events add constraint owner_import_status_evidence_is_import_hash
      check (status not in ('VERIFIED', 'RECONCILED') or (evidence_ref is not null and evidence_ref = import_id))
      not valid;
  end if;
end $$;

-- Fail the migration (rolling it back) if any carry table escaped the new baseline.
do $$
declare
  r record;
begin
  for r in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'carry' and c.relkind = 'r' loop
    if not exists (select 1 from pg_trigger g where g.tgrelid = format('carry.%I', r.relname)::regclass
                   and g.tgname = 'carry_stamp_insert' and not g.tgisinternal) then
      raise exception 'carry.% has no stamp trigger', r.relname;
    end if;
  end loop;
  if has_table_privilege('service_role', 'carry.order_refs', 'insert')
     or has_table_privilege('service_role', 'carry.fill_refs', 'insert')
     or has_table_privilege('service_role', 'carry.account_snapshots', 'insert') then
    raise exception 'service_role can still insert into a table that must stay write-closed';
  end if;
end $$;
