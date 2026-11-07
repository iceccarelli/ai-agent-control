-- 20261107000600_carry_operations.sql
-- Incidents (open + events), system health observations, backup and restore receipts.

create table if not exists carry.incidents (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  incident_id text not null check (carry_private.nonblank(incident_id)),
  severity text not null check (severity in ('INFO', 'WARN', 'HIGH', 'CRITICAL')),
  title text not null check (carry_private.nonblank(title)),
  runbook_ref text,
  opened_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, incident_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.incidents');

create table if not exists carry.incident_events (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  incident_id text not null,
  state text not null check (state in ('OPENED', 'ACKNOWLEDGED', 'MITIGATED', 'RESOLVED', 'REOPENED')),
  note text,
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (state not in ('MITIGATED', 'RESOLVED') or carry_private.nonblank(note)),
  unique (owner_id, content_sha256),
  foreign key (owner_id, incident_id) references carry.incidents (owner_id, incident_id)
);
select carry_private.secure_table('carry.incident_events');

create table if not exists carry.system_health (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  component text not null check (carry_private.nonblank(component)),
  state text not null check (state in ('OK', 'DEGRADED', 'DOWN', 'UNKNOWN')),
  observed_utc timestamptz not null,
  detail jsonb not null default '{}'::jsonb check (jsonb_typeof(detail) = 'object'),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, component, observed_utc),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.system_health');

create table if not exists carry.backup_receipts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  backup_id text not null check (carry_private.nonblank(backup_id)),
  target text not null check (carry_private.nonblank(target)),   -- e.g. pg_dump, managed backup, evidence archive
  artifact_sha256 carry.sha256_hex not null,
  byte_count bigint not null check (byte_count >= 0),
  storage_ref text not null check (carry_private.nonblank(storage_ref)),
  taken_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, backup_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.backup_receipts');

create table if not exists carry.restore_receipts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  restore_id text not null check (carry_private.nonblank(restore_id)),
  backup_id text not null,
  outcome text not null check (outcome in ('PASSED', 'FAILED')),
  verification_sha256 carry.sha256_hex not null,
  checks jsonb not null default '{}'::jsonb check (jsonb_typeof(checks) = 'object'),
  restored_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, restore_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, backup_id) references carry.backup_receipts (owner_id, backup_id)
);
select carry_private.secure_table('carry.restore_receipts');
