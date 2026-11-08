-- 20261108000200_carry_rt5_sync_hardening.sql
-- Closes the database-side findings of the fourth red-team pass (probes replayed as executed tests in bot/tests/test_supabase_migrations.py).
-- ADDITIVE and idempotent: nothing already numbered is edited, no table is created, nothing is granted, no RLS / append-only guarantee is relaxed.
--
-- WHAT IS AND IS NOT BOUND (this corrects the wording of 20261108000100, which said evidence is "bound to the hashed artifact"; there it was a FORMAT check only)
--  * OWNER_ACCOUNT_VERIFIED evidence: BOUND to an existing import ROW. A BEFORE INSERT trigger requires an owner_account_imports row of the same owner whose
--    report_sha256 equals the cited details.import_report_sha256 (top level or details.metrics; if both are set they must agree) and whose status is VERIFIED or RECONCILED.
--    NOT bound: that the report behind that hash is genuine. The row's report_sha256 is itself client-supplied by the service-role writer; the database cannot recompute a
--    sealed report. bot/tools/supabase_sync.py verifies the report (verify_import_report) before it builds the import row, and refuses owner evidence unless that import
--    is in the same plan; a writer that bypasses it can still insert a fabricated import row and cite it. The database proves "an import row exists", not "the owner's
--    account says so".
--  * TESTNET_MECHANICS evidence: REFUSED outright. Such a claim would have to point at an execution session, nothing in this repository writes execution_sessions (it is
--    write-closed since 20261108000100), so no claim at that level can be backed. The older execution_log_sha256 format CHECK stays as defence in depth.
--  * No second financial truth: details (top-level keys) and details.metrics are now a WHITELIST (carry_private.evidence_details_keys_ok), the same lists as
--    bot/tools/supabase_sync.py OBJ_SCHEMAS[("experiment_evidence","details")] (a test asserts they are identical). The single-key blacklist of 20261108000100
--    (net_usd_after_all_costs) stays as well (this repository's tests forbid DROP statements in migrations) but is now subsumed: a renamed or nested figure
--    (net_pnl_usd, pnl, metrics.balance, details.owner.net) has no whitelisted name and is refused. NOT closable here: a whitelisted name (for example metrics.sharpe or
--    capacity_usd) can still carry a wrong number: the database checks names, not truth.
--  * Free-text provenance: experiment_evidence.provenance and every table's source column must match carry_private.safe_short_code (^[A-Za-z0-9_.:/# -]{1,120}$):
--    letters, digits and _ . : / # space - only, 1..120 characters. That cannot carry an e-mail, a phone number, an assignment or accented personal data. NOT closable: a name
--    written in plain words ("Alice Smith") is inside the charset; the application's SecretGuard has cheap wording rules, not a name detector.
--
-- The new CHECK constraints are NOT VALID: enforced for every new row from now on, no rescan of rows written before this migration.

create or replace function carry_private.evidence_details_keys_ok(d jsonb) returns boolean
language sql immutable set search_path = ''
as $$
  select d is null or (
    jsonb_typeof(d) = 'object'
    and not exists (select 1 from jsonb_object_keys(d) k where k <> all (array['capacity_usd', 'complete', 'fees_measured', 'fills_measured', 'independent_observations', 'multiple_testing_adjusted', 'n_variants_tested', 'net_edge_bps_after_costs', 'point_in_time', 'registered_before_observation', 'universe_point_in_time', 'unknown_cost_inputs', 'uses_future_information', 'venue', 'rejection_conditions_fired', 'sharpe', 'n', 'sortino', 'max_drawdown_pct', 'hit_rate', 'trades', 'observations', 'observation_started_ms', 'observation_ended_ms', 'observed_settlements', 'coverage', 'fills_count', 'fee_observations_count', 'metrics', 'data_sha256', 'status', 'import_report_sha256', 'execution_log_sha256']::text[]))
    and (not (d ? 'metrics') or (
      jsonb_typeof(d -> 'metrics') = 'object'
      and not exists (select 1 from jsonb_object_keys(d -> 'metrics') k where k <> all (array['capacity_usd', 'complete', 'fees_measured', 'fills_measured', 'independent_observations', 'multiple_testing_adjusted', 'n_variants_tested', 'net_edge_bps_after_costs', 'point_in_time', 'registered_before_observation', 'universe_point_in_time', 'unknown_cost_inputs', 'uses_future_information', 'venue', 'rejection_conditions_fired', 'sharpe', 'n', 'sortino', 'max_drawdown_pct', 'hit_rate', 'trades', 'observations', 'observation_started_ms', 'observation_ended_ms', 'observed_settlements', 'coverage', 'fills_count', 'fee_observations_count', 'import_report_sha256', 'execution_log_sha256']::text[]))))
  ) $$;
revoke all on function carry_private.evidence_details_keys_ok(jsonb) from public;
grant execute on function carry_private.evidence_details_keys_ok(jsonb) to service_role;

create or replace function carry_private.safe_short_code(t text) returns boolean
language sql immutable set search_path = ''
as $$ select t is not null and t ~ '^[A-Za-z0-9_.:/# -]{1,120}$' $$;
revoke all on function carry_private.safe_short_code(text) from public;
grant execute on function carry_private.safe_short_code(text) to service_role;

do $$
declare
  t text;
begin
  if not exists (select 1 from pg_constraint where conrelid = 'carry.experiment_evidence'::regclass and conname = 'experiment_evidence_details_key_whitelist') then
    alter table carry.experiment_evidence add constraint experiment_evidence_details_key_whitelist
      check (carry_private.evidence_details_keys_ok(details)) not valid;
  end if;
  if not exists (select 1 from pg_constraint where conrelid = 'carry.experiment_evidence'::regclass and conname = 'experiment_evidence_provenance_short_code') then
    alter table carry.experiment_evidence add constraint experiment_evidence_provenance_short_code
      check (carry_private.safe_short_code(provenance)) not valid;
  end if;
  for t in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           join pg_attribute a on a.attrelid = c.oid and a.attname = 'source' and not a.attisdropped
           where n.nspname = 'carry' and c.relkind = 'r' loop
    if not exists (select 1 from pg_constraint where conrelid = format('carry.%I', t)::regclass and conname = t || '_source_short_code') then
      execute format('alter table carry.%I add constraint %I check (carry_private.safe_short_code(source)) not valid', t, t || '_source_short_code');
    end if;
  end loop;
end $$;

create or replace function carry_private.evidence_binding_guard() returns trigger
language plpgsql set search_path = ''
as $$
declare
  h1 text;
  h2 text;
  h text;
begin
  if new.level = 'TESTNET_MECHANICS' then
    raise exception 'TESTNET_MECHANICS evidence is refused: no writer exists for execution_sessions, so the claim cannot be bound to a session row'
      using errcode = 'check_violation';
  end if;
  if new.level = 'OWNER_ACCOUNT_VERIFIED' then
    h1 := new.details ->> 'import_report_sha256';
    h2 := new.details -> 'metrics' ->> 'import_report_sha256';
    if h1 is not null and h2 is not null and h1 <> h2 then
      raise exception 'OWNER_ACCOUNT_VERIFIED evidence cites two different report hashes' using errcode = 'check_violation';
    end if;
    h := coalesce(h1, h2);
    if h is null or not exists (select 1 from carry.owner_account_imports i
                                where i.owner_id = new.owner_id and i.report_sha256 = h and i.status in ('VERIFIED', 'RECONCILED')) then
      raise exception 'OWNER_ACCOUNT_VERIFIED evidence must cite the report_sha256 of an existing VERIFIED or RECONCILED owner_account_imports row of the same owner'
        using errcode = 'check_violation';
    end if;
  end if;
  return new;
end $$;
revoke all on function carry_private.evidence_binding_guard() from public;

create or replace trigger carry_evidence_binding before insert on carry.experiment_evidence
  for each row execute function carry_private.evidence_binding_guard();
