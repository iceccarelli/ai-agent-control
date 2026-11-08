-- 20261107000800_carry_evidence_guard_idempotent.sql
-- Writer idempotency fix. carry.put_row is `insert ... on conflict do nothing`, but a BEFORE INSERT row trigger runs
-- BEFORE the conflict check, so re-sending an evidence row that already exists for a trial that was LATER abandoned or
-- superseded raised check_violation instead of being a no-op (found by bot/tests/test_supabase_sync.py).
-- This replaces only the guard function: a row whose (owner_id, content_sha256) already exists is returned untouched
-- (put_row's ON CONFLICT then discards it). A NEW evidence row for an abandoned/superseded trial is still refused.
-- Nothing is granted, no table changes, no privilege changes. Re-applying is safe.
create or replace function carry_private.guard_evidence_target() returns trigger
language plpgsql set search_path = pg_catalog, carry
as $$
begin
  if exists (select 1 from carry.experiment_evidence e
             where e.owner_id = new.owner_id and e.content_sha256 = new.content_sha256) then
    return new;  -- exact duplicate: the insert is a no-op downstream
  end if;
  if exists (select 1 from carry.experiment_abandonments a where a.owner_id = new.owner_id and a.trial_id = new.trial_id) then
    raise exception 'trial % is abandoned; evidence cannot be added', new.trial_id using errcode = 'check_violation';
  end if;
  if exists (select 1 from carry.experiment_supersessions s where s.owner_id = new.owner_id and s.trial_id = new.trial_id) then
    raise exception 'trial % is superseded; add the evidence to the replacement trial', new.trial_id using errcode = 'check_violation';
  end if;
  return new;
end $$;
