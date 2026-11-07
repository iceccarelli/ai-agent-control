-- 20261107000200_carry_research_registry.sql
-- Strategy hypotheses and experiment registrations. Mirrors bot/tools/hypothesis_registry.py:
-- REQUIRED_REGISTRATION fields, the 7 EVIDENCE_LEVELS, append-only evidence/amendments/abandonment/supersession.
-- Provenance on every table: owner_id, source, schema_version, content_sha256, created_at.
-- content_sha256 is the writer's canonical hash of the row content; (owner_id, content_sha256) is the
-- idempotency key. For registrations it is the registry's entry_digest (the sealed registration digest).

create table if not exists carry.strategy_hypotheses (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  hypothesis_id text not null check (carry_private.nonblank(hypothesis_id)),
  mechanism text not null check (carry_private.nonblank(mechanism)),
  family text not null check (carry_private.nonblank(family)),
  statement text not null check (carry_private.nonblank(statement)),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, hypothesis_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.strategy_hypotheses');

create table if not exists carry.experiment_registrations (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  trial_id text not null check (carry_private.nonblank(trial_id)),
  hypothesis_id text not null,
  -- REQUIRED_REGISTRATION (hypothesis_id above), all non-empty:
  mechanism text not null check (carry_private.nonblank(mechanism)),
  family text not null check (carry_private.nonblank(family)),
  strategy text not null check (carry_private.nonblank(strategy)),
  universe jsonb not null check (carry_private.nonempty_json(universe)),
  input_data_sha256 carry.sha256_hex not null,
  code_sha text not null check (code_sha ~ '^[0-9a-f]{7,64}$'),
  data_window jsonb not null check (carry_private.nonempty_json(data_window)),
  parameter_grid jsonb not null check (carry_private.nonempty_json(parameter_grid)),
  costs jsonb not null check (carry_private.nonempty_json(costs)),
  statistical_design jsonb not null check (carry_private.nonempty_json(statistical_design)),
  train_test jsonb not null check (carry_private.nonempty_json(train_test)),
  multiple_testing_family text not null check (carry_private.nonblank(multiple_testing_family)),
  prospective_period text not null check (carry_private.nonblank(prospective_period)), -- explicit 'none' allowed
  registered_utc timestamptz not null,
  note text,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null, -- registration digest (entry_digest)
  created_at timestamptz not null default now(),
  unique (owner_id, trial_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, hypothesis_id) references carry.strategy_hypotheses (owner_id, hypothesis_id)
);
select carry_private.secure_table('carry.experiment_registrations');

create table if not exists carry.experiment_evidence (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  trial_id text not null,
  level text not null check (level in ('RESEARCH_CANDIDATE', 'BACKTEST', 'OUT_OF_SAMPLE', 'PROSPECTIVE',
                                       'SIMULATED_EXECUTION', 'TESTNET_MECHANICS', 'OWNER_ACCOUNT_VERIFIED')),
  result text not null check (carry_private.nonblank(result)),
  provenance text not null check (carry_private.nonblank(provenance)), -- file/run/commit it came from
  details jsonb not null default '{}'::jsonb check (jsonb_typeof(details) = 'object'),
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, content_sha256),
  foreign key (owner_id, trial_id) references carry.experiment_registrations (owner_id, trial_id)
);
select carry_private.secure_table('carry.experiment_evidence');

create table if not exists carry.experiment_amendments (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  trial_id text not null,
  what text not null check (carry_private.nonblank(what)),
  why text not null check (carry_private.nonblank(why)),
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, content_sha256),
  foreign key (owner_id, trial_id) references carry.experiment_registrations (owner_id, trial_id)
);
select carry_private.secure_table('carry.experiment_amendments');

create table if not exists carry.experiment_abandonments (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  trial_id text not null,
  reason text not null check (carry_private.nonblank(reason)),
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, trial_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, trial_id) references carry.experiment_registrations (owner_id, trial_id)
);
select carry_private.secure_table('carry.experiment_abandonments');

create table if not exists carry.experiment_supersessions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  trial_id text not null,
  superseded_by text not null,
  reason text not null check (carry_private.nonblank(reason)),
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (trial_id <> superseded_by),
  unique (owner_id, trial_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, trial_id) references carry.experiment_registrations (owner_id, trial_id),
  foreign key (owner_id, superseded_by) references carry.experiment_registrations (owner_id, trial_id)
);
select carry_private.secure_table('carry.experiment_supersessions');

-- Registry rule: evidence cannot be added to an abandoned or superseded trial.
create or replace function carry_private.guard_evidence_target() returns trigger
language plpgsql set search_path = pg_catalog, carry
as $$
begin
  if exists (select 1 from carry.experiment_abandonments a where a.owner_id = new.owner_id and a.trial_id = new.trial_id) then
    raise exception 'trial % is abandoned; evidence cannot be added', new.trial_id using errcode = 'check_violation';
  end if;
  if exists (select 1 from carry.experiment_supersessions s where s.owner_id = new.owner_id and s.trial_id = new.trial_id) then
    raise exception 'trial % is superseded; add the evidence to the replacement trial', new.trial_id using errcode = 'check_violation';
  end if;
  return new;
end $$;
revoke all on function carry_private.guard_evidence_target() from public;
create or replace trigger experiment_evidence_target_guard before insert on carry.experiment_evidence
  for each row execute function carry_private.guard_evidence_target();
