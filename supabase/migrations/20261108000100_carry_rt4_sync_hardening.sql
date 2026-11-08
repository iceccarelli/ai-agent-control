-- 20261108000100_carry_rt4_sync_hardening.sql
-- Closes the database-side findings of the third red-team pass (probes replayed as executed tests in
-- bot/tests/test_supabase_migrations.py). ADDITIVE and idempotent: nothing already numbered is edited, no table is created,
-- nothing is granted, no RLS / append-only guarantee is relaxed. Re-applying is safe.
--
-- CLOSED HERE, in the database
--  1. Free-form tables with NO writer are write-closed. execution_sessions, execution_session_events, risk_decisions,
--     reconciliation_exceptions and reconciliation_exception_resolutions are written by no code in this repository
--     (bot/tools/supabase_sync.py maps none of them: its TABLE_ORDER does not contain them, and a test compares the two lists)
--     yet they accepted free-form columns (detail jsonb, reasons jsonb, description, amount/currency, explanation, ...) from the
--     service role: a place to put balances, order data or personal data. Exactly as 20261107000900 did for order_refs,
--     fill_refs and account_snapshots: INSERT is revoked from service_role AND a BEFORE INSERT statement trigger refuses inserts
--     for every role (including the table owner), so re-running an earlier migration (secure_table re-grants INSERT) cannot
--     silently re-open them. Reading stays as before. Re-enabling one needs a reviewed migration that removes the trigger, an
--     owner decision, a writer in the repository, AND typed columns / CHECKs for its free-form fields (the carry_private.forbid_insert_untyped
--     message says so). The CHECK constraints these tables already carry (sandbox-only venue, FLAT needs a venue read, ...) stay.
--  2. owner_account_imports.status is bound to an evidence hash. A VERIFIED / RECONCILED import row now has to carry
--     report_sha256 (a new nullable column: the sha256 of the sealed owner_import report the status is claimed from). Previously
--     the status word on the import row itself was unconstrained (only the status EVENTS needed evidence).
--  3. Evidence that makes a claim about real money or a real venue is bound to the hashed artifact it speaks about:
--     OWNER_ACCOUNT_VERIFIED needs details.import_report_sha256, TESTNET_MECHANICS needs details.execution_log_sha256 (at the top
--     level or inside details.metrics; 64 lowercase hex). And Supabase carries no second financial truth: the owner's P&L figure
--     (details.metrics.net_usd_after_all_costs, or the same key at the top level) is refused. The figure lives in the hashed report
--     the owner holds.
--
-- registered_utc IS CLIENT-SUPPLIED (documented, not closable here). experiment_registrations.registered_utc is whatever the writer
-- sent; the database cannot know the registration's real time. The registry's own seal (hypothesis_registry.verify, now
-- required by bot/tools/supabase_sync.py before any registration is mapped) proves the value was not edited after sealing, not that
-- the clock was honest. A consumer that needs WHEN a registration reached the database must use created_at, which the server sets
-- (20261107000900 stamps it and ignores the client's). A registration whose registered_utc is far BEFORE its created_at is a
-- backdating signal, not a fact; the comment on the column says so.
--
-- The new CHECK constraints are NOT VALID: enforced for every new row from now on, no rescan of rows written before this
-- migration (the owner can inspect and then `alter table ... validate constraint ...`). On a fresh database there is nothing to rescan.

-- 1: no writers, no writes ------------------------------------------------------------------------------------------
create or replace function carry_private.forbid_insert_untyped() returns trigger
language plpgsql set search_path = ''
as $$
begin
  raise exception 'carry.% accepts no writes: no writer exists for it in this repository, and its free-form columns are not typed (re-enabling needs a reviewed migration, an owner decision, a writer and constrained columns)',
    tg_table_name using errcode = 'insufficient_privilege';
end $$;
revoke all on function carry_private.forbid_insert_untyped() from public;

do $$
declare
  t text;
begin
  foreach t in array array['execution_sessions', 'execution_session_events', 'risk_decisions', 'reconciliation_exceptions',
                           'reconciliation_exception_resolutions'] loop
    execute format('revoke insert on table carry.%I from service_role', t);
    execute format('create or replace trigger carry_no_insert before insert on carry.%I for each statement execute function carry_private.forbid_insert_untyped()', t);
  end loop;
end $$;

-- 2: an import's VERIFIED / RECONCILED status names the sealed report it comes from --------------------------------------
alter table carry.owner_account_imports add column if not exists report_sha256 carry.sha256_hex;

do $$
begin
  if not exists (select 1 from pg_constraint where conrelid = 'carry.owner_account_imports'::regclass
                 and conname = 'owner_import_verified_status_names_its_report') then
    alter table carry.owner_account_imports add constraint owner_import_verified_status_names_its_report
      check (status not in ('VERIFIED', 'RECONCILED') or report_sha256 is not null)
      not valid;
  end if;
end $$;

-- 3: financial claims in evidence are bound to a hash, and carry no figure --------------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint where conrelid = 'carry.experiment_evidence'::regclass
                 and conname = 'experiment_evidence_owner_level_bound_to_report_hash') then
    alter table carry.experiment_evidence add constraint experiment_evidence_owner_level_bound_to_report_hash
      check (level <> 'OWNER_ACCOUNT_VERIFIED'
             or coalesce(coalesce(details ->> 'import_report_sha256', details -> 'metrics' ->> 'import_report_sha256') ~ '^[0-9a-f]{64}$', false))
      not valid;
  end if;
  if not exists (select 1 from pg_constraint where conrelid = 'carry.experiment_evidence'::regclass
                 and conname = 'experiment_evidence_testnet_level_bound_to_log_hash') then
    alter table carry.experiment_evidence add constraint experiment_evidence_testnet_level_bound_to_log_hash
      check (level <> 'TESTNET_MECHANICS'
             or coalesce(coalesce(details ->> 'execution_log_sha256', details -> 'metrics' ->> 'execution_log_sha256') ~ '^[0-9a-f]{64}$', false))
      not valid;
  end if;
  if not exists (select 1 from pg_constraint where conrelid = 'carry.experiment_evidence'::regclass
                 and conname = 'experiment_evidence_carries_no_owner_figure') then
    alter table carry.experiment_evidence add constraint experiment_evidence_carries_no_owner_figure
      check (not (details ? 'net_usd_after_all_costs'
                  or coalesce(jsonb_typeof(details -> 'metrics') = 'object' and (details -> 'metrics') ? 'net_usd_after_all_costs', false)))
      not valid;
  end if;
end $$;

comment on column carry.experiment_registrations.registered_utc is
  'CLIENT-SUPPLIED: the writer states it; the registry seal proves it was not edited afterwards, not that the clock was honest. Rely on created_at (server-set) for when the row reached the database.';

-- Fail the migration (rolling it back) if a write-closed table escaped.
do $$
declare
  t text;
begin
  foreach t in array array['execution_sessions', 'execution_session_events', 'risk_decisions', 'reconciliation_exceptions',
                           'reconciliation_exception_resolutions'] loop
    if has_table_privilege('service_role', format('carry.%I', t), 'insert') then
      raise exception 'service_role can still insert into carry.%', t;
    end if;
    if not exists (select 1 from pg_trigger g where g.tgrelid = format('carry.%I', t)::regclass and g.tgname = 'carry_no_insert' and not g.tgisinternal) then
      raise exception 'carry.% has no write-closing trigger', t;
    end if;
  end loop;
end $$;
