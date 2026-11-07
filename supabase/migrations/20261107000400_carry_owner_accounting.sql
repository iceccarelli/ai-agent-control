-- 20261107000400_carry_owner_accounting.sql
-- Owner-account import records, status events, reconciliation exceptions, account snapshots.
-- These are RECORDS ABOUT imports (hash, status, refs), not a ledger. The owner's reconciled accounting
-- remains the source of truth. Status vocabulary is exactly:
--   VERIFIED, RECONCILED, ESTIMATED, PROVISIONAL, STALE, DISPUTED, INCOMPLETE, UNAVAILABLE
-- A status change is a NEW row in owner_account_import_status_events (latest recorded_utc wins).

create table if not exists carry.owner_account_imports (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  import_id carry.sha256_hex not null,          -- content hash of the import payload
  source_sha256 carry.sha256_hex not null,      -- sha256 of the original source file
  kind text not null check (carry_private.nonblank(kind)),
  period_start_utc timestamptz,
  period_end_utc timestamptz,
  row_count bigint check (row_count is null or row_count >= 0),
  storage_ref text not null check (carry_private.nonblank(storage_ref)),
  status text not null check (status in ('VERIFIED', 'RECONCILED', 'ESTIMATED', 'PROVISIONAL', 'STALE',
                                         'DISPUTED', 'INCOMPLETE', 'UNAVAILABLE')),
  imported_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (import_id = content_sha256),
  check (period_end_utc is null or period_start_utc is null or period_end_utc >= period_start_utc),
  unique (owner_id, import_id)
);
select carry_private.secure_table('carry.owner_account_imports');

create table if not exists carry.owner_account_import_status_events (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  import_id text not null,
  status text not null check (status in ('VERIFIED', 'RECONCILED', 'ESTIMATED', 'PROVISIONAL', 'STALE',
                                         'DISPUTED', 'INCOMPLETE', 'UNAVAILABLE')),
  reason text,
  evidence_ref text,
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  -- VERIFIED/RECONCILED must point at the evidence; the negative/uncertain states must say why.
  check (status not in ('VERIFIED', 'RECONCILED') or carry_private.nonblank(evidence_ref)),
  check (status not in ('STALE', 'DISPUTED', 'INCOMPLETE', 'UNAVAILABLE') or carry_private.nonblank(reason)),
  unique (owner_id, content_sha256),
  foreign key (owner_id, import_id) references carry.owner_account_imports (owner_id, import_id)
);
select carry_private.secure_table('carry.owner_account_import_status_events');

create table if not exists carry.reconciliation_exceptions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  exception_id text not null check (carry_private.nonblank(exception_id)),
  import_id text not null,
  kind text not null check (carry_private.nonblank(kind)),
  severity text not null check (severity in ('INFO', 'WARN', 'HIGH', 'CRITICAL')),
  description text not null check (carry_private.nonblank(description)),
  amount numeric,                                -- difference as reported; informational only
  currency text,
  detected_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, exception_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, import_id) references carry.owner_account_imports (owner_id, import_id)
);
select carry_private.secure_table('carry.reconciliation_exceptions');

create table if not exists carry.reconciliation_exception_resolutions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  exception_id text not null,
  resolution text not null check (resolution in ('RESOLVED', 'ACCEPTED', 'REOPENED')),
  explanation text not null check (carry_private.nonblank(explanation)),
  evidence_ref text,
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, content_sha256),
  foreign key (owner_id, exception_id) references carry.reconciliation_exceptions (owner_id, exception_id)
);
select carry_private.secure_table('carry.reconciliation_exception_resolutions');

-- Point-in-time account snapshot REFERENCES (venue read or owner import). Summary only; payload lives by ref.
create table if not exists carry.account_snapshots (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  snapshot_id text not null check (carry_private.nonblank(snapshot_id)),
  snapshot_kind text not null check (snapshot_kind in ('VENUE_READ', 'OWNER_IMPORT')),
  status text not null check (status in ('VERIFIED', 'RECONCILED', 'ESTIMATED', 'PROVISIONAL', 'STALE',
                                         'DISPUTED', 'INCOMPLETE', 'UNAVAILABLE')),
  import_id text,
  taken_utc timestamptz not null,
  payload_sha256 carry.sha256_hex not null,
  payload_ref text not null check (carry_private.nonblank(payload_ref)),
  summary jsonb not null default '{}'::jsonb check (jsonb_typeof(summary) = 'object'),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (snapshot_kind <> 'OWNER_IMPORT' or import_id is not null),
  unique (owner_id, snapshot_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, import_id) references carry.owner_account_imports (owner_id, import_id)
);
select carry_private.secure_table('carry.account_snapshots');
