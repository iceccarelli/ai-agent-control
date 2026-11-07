-- 20261107000500_carry_risk_execution.sql
-- Risk policy versions, risk decisions, owner approval records, execution sessions, order/fill REFERENCES.
-- Not a trading ledger: orders/fills are references (venue ids + payload hash + evidence ref) only.
-- Live trading is NOT representable here: session mode is PAPER or TESTNET, and no approval kind covers the
-- promotion gate (which stays REFUSE until the owner signs it elsewhere).

create table if not exists carry.risk_policy_versions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  policy_name text not null check (carry_private.nonblank(policy_name)),
  version integer not null check (version >= 1),
  policy jsonb not null check (carry_private.nonempty_json(policy)),
  effective_from_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,       -- hash of the policy document
  created_at timestamptz not null default now(),
  unique (owner_id, policy_name, version),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.risk_policy_versions');

create table if not exists carry.risk_decisions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  decision_id text not null check (carry_private.nonblank(decision_id)),
  policy_name text not null,
  policy_version integer not null,
  subject text not null check (carry_private.nonblank(subject)),
  decision text not null check (decision in ('ALLOW', 'DENY', 'REDUCE_ONLY', 'HALT')),
  reasons jsonb not null check (carry_private.nonempty_json(reasons)),
  inputs_sha256 carry.sha256_hex not null,
  decided_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, decision_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, policy_name, policy_version)
    references carry.risk_policy_versions (owner_id, policy_name, version)
);
select carry_private.secure_table('carry.risk_decisions');

-- Owner approval is its own append-only record. approved_by_owner has NO default and is NOT NULL: a writer
-- must state it. TRUE requires the approver to be the owner and a hash of the statement they approved.
-- A revocation or rejection is a new row with approved_by_owner = false (latest decided_utc wins).
create table if not exists carry.approval_records (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  approval_id text not null check (carry_private.nonblank(approval_id)),
  subject_kind text not null check (subject_kind in ('risk_policy_version', 'execution_session',
                                                     'import_reconciliation', 'data_batch')),
  subject_ref text not null check (carry_private.nonblank(subject_ref)),
  approved_by_owner boolean not null,
  approver_id uuid,
  statement_sha256 carry.sha256_hex,
  note text,
  decided_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check (not approved_by_owner or (approver_id = owner_id and statement_sha256 is not null)),
  unique (owner_id, approval_id),
  unique (owner_id, content_sha256)
);
select carry_private.secure_table('carry.approval_records');

create table if not exists carry.execution_sessions (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  session_id text not null check (carry_private.nonblank(session_id)),
  mode text not null check (mode in ('PAPER', 'TESTNET')),
  venue text not null check (carry_private.nonblank(venue)),   -- meaning/real-money status is bot/venues.py's
  strategy text not null check (carry_private.nonblank(strategy)),
  code_sha text not null check (code_sha ~ '^[0-9a-f]{7,64}$'),
  risk_policy_name text,
  risk_policy_version integer,
  started_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check ((risk_policy_name is null) = (risk_policy_version is null)),
  unique (owner_id, session_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, risk_policy_name, risk_policy_version)
    references carry.risk_policy_versions (owner_id, policy_name, version)
);
select carry_private.secure_table('carry.execution_sessions');

-- FLAT_CONFIRMED means "the venue's own position read says flat": it REQUIRES the hash of that read and
-- may never be derived from fill arithmetic. Other states must not carry one.
create table if not exists carry.execution_session_events (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  session_id text not null,
  state text not null check (state in ('STARTED', 'HALTED', 'FLAT_CONFIRMED', 'ENDED')),
  venue_position_read_sha256 carry.sha256_hex,
  detail jsonb not null default '{}'::jsonb check (jsonb_typeof(detail) = 'object'),
  recorded_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  check ((state = 'FLAT_CONFIRMED') = (venue_position_read_sha256 is not null)),
  unique (owner_id, content_sha256),
  foreign key (owner_id, session_id) references carry.execution_sessions (owner_id, session_id)
);
select carry_private.secure_table('carry.execution_session_events');

create table if not exists carry.order_refs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  session_id text not null,
  venue_order_id text not null check (carry_private.nonblank(venue_order_id)),
  client_order_id text,
  symbol text not null check (carry_private.nonblank(symbol)),
  side text not null check (side in ('BUY', 'SELL')),
  reduce_only boolean not null,
  venue_payload_sha256 carry.sha256_hex not null,
  evidence_ref text not null check (carry_private.nonblank(evidence_ref)),
  observed_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, session_id, venue_order_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, session_id) references carry.execution_sessions (owner_id, session_id)
);
select carry_private.secure_table('carry.order_refs');

create table if not exists carry.fill_refs (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null,
  session_id text not null,
  venue_order_id text not null,
  venue_fill_id text not null check (carry_private.nonblank(venue_fill_id)),
  venue_payload_sha256 carry.sha256_hex not null,
  evidence_ref text not null check (carry_private.nonblank(evidence_ref)),
  observed_utc timestamptz not null,
  source text not null check (carry_private.nonblank(source)),
  schema_version integer not null default 1 check (schema_version >= 1),
  content_sha256 carry.sha256_hex not null,
  created_at timestamptz not null default now(),
  unique (owner_id, session_id, venue_fill_id),
  unique (owner_id, content_sha256),
  foreign key (owner_id, session_id, venue_order_id)
    references carry.order_refs (owner_id, session_id, venue_order_id)
);
select carry_private.secure_table('carry.fill_refs');
