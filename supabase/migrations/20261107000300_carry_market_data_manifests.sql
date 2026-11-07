-- 20261107000300_carry_market_data_manifests.sql
-- Instrument universe snapshots, data-batch manifests, md_seal receipts, collector status.
-- ONLY hashes, counts, ids and storage refs: raw market data stays in object storage / on the collector volume.

create table if not exists carry.instrument_universe_snapshots (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  snapshot_id text not null check (carry_private.nonblank(snapshot_id)),
  venue text not null check (carry_private.nonblank(venue)),
  captured_utc timestamptz not null,
  manifest_sha256 carry.sha256_hex not null,        -- universe_capture manifest digest
  manifest_ref text not null check (carry_private.nonblank(manifest_ref)),
  instrument_count integer not null check (instrument_count >= 0),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, snapshot_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.instrument_universe_snapshots');

create table if not exists carry.data_batches (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  batch_id text not null check (carry_private.nonblank(batch_id)),
  dataset text not null check (carry_private.nonblank(dataset)),
  venue text not null check (carry_private.nonblank(venue)),
  batch_sha256 carry.sha256_hex not null,
  byte_count bigint not null check (byte_count >= 0),
  row_count bigint check (row_count is null or row_count >= 0),
  segment_ids text[] not null check (cardinality(segment_ids) > 0),
  window_start_utc timestamptz not null,
  window_end_utc timestamptz not null,
  storage_ref text not null check (carry_private.nonblank(storage_ref)),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (window_end_utc >= window_start_utc),
  unique (owner_id, batch_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.data_batches');

create table if not exists carry.md_seal_receipts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  seal_id text not null check (carry_private.nonblank(seal_id)),
  batch_id text not null,
  manifest_sha256 carry.sha256_hex not null,
  manifest_rows bigint not null check (manifest_rows >= 0),
  receipt_sha256 carry.sha256_hex not null,
  sealed_utc timestamptz not null,
  receipt jsonb not null default '{}'::jsonb check (jsonb_typeof(receipt) = 'object'),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, seal_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, batch_id) references carry.data_batches (owner_id, batch_id)
);
select carry_private.secure_table('carry.md_seal_receipts');

-- Append-only observations (one row per poll); the latest observed_utc per collector is "current".
create table if not exists carry.collector_status (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  collector_name text not null check (carry_private.nonblank(collector_name)),
  state text not null check (state in ('RUNNING', 'STALLED', 'STOPPED', 'ERROR', 'UNKNOWN')),
  observed_utc timestamptz not null,
  last_record_utc timestamptz,
  lag_seconds numeric check (lag_seconds is null or lag_seconds >= 0),
  detail jsonb not null default '{}'::jsonb check (jsonb_typeof(detail) = 'object'),
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, collector_name, observed_utc),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.collector_status');
