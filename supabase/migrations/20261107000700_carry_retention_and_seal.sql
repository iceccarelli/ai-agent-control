-- 20261107000700_carry_retention_and_seal.sql
-- Retention / deletion policy (stated on every table) and a final in-database safety assertion.
--
-- RETENTION POLICY
--  * Audit-class tables (registry, evidence, approvals, decisions, imports, exceptions, receipts, incidents,
--    execution refs, manifests): keep for the life of the programme plus 7 years. Never deleted by the app.
--  * Observation-class tables (collector_status, system_health): keep 400 days online.
--  * No role can UPDATE/DELETE/TRUNCATE (privileges withheld + statement triggers). Any removal is an
--    owner-run OFFLINE procedure: export and hash the rows, record a backup receipt, then the owner (as a
--    database superuser, never the service role) disables the append-only trigger inside one transaction,
--    deletes the exact rows, re-enables the trigger, and records an incident event explaining it.
--  * Owner-account data is personal financial data: hold only hashes/refs here, never statements themselves.

do $$
declare
  r record; v_class text;
begin
  for r in select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'carry' and c.relkind = 'r' loop
    v_class := case when r.relname in ('collector_status', 'system_health')
                    then 'RETENTION: observation class, 400 days online, append-only; removal only by owner-run offline procedure.'
                    else 'RETENTION: audit class, programme life + 7 years, append-only; removal only by owner-run offline procedure.' end;
    execute format('comment on table carry.%I is %L', r.relname,
                   v_class || ' Not a trading ledger: hashes, manifests and refs only. Writer: service role. Readers: owner_id = auth.uid().');
  end loop;
end $$;

-- Fail the migration (rolling it back) if any carry table escaped the baseline.
do $$
declare
  r record;
begin
  for r in select c.relname, c.relrowsecurity, c.relforcerowsecurity, c.oid
           from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'carry' and c.relkind = 'r' loop
    if not (r.relrowsecurity and r.relforcerowsecurity) then
      raise exception 'carry.% lacks enabled+forced RLS', r.relname;
    end if;
    if not exists (select 1 from pg_policies p where p.schemaname = 'carry' and p.tablename = r.relname) then
      raise exception 'carry.% has no policy', r.relname;
    end if;
    if has_table_privilege('anon', r.oid, 'select, insert, update, delete')
       or has_table_privilege('public'::name, r.oid, 'select') then
      raise exception 'carry.% is reachable by anon/public', r.relname;
    end if;
  end loop;
end $$;
