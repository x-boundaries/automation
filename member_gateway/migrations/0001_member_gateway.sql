-- XB member gateway durable model, v1.
-- Every statement is migration-local and transactional. AutoCount API calls
-- must happen outside these short database transactions.

BEGIN;

CREATE SCHEMA IF NOT EXISTS xb_member_gateway;

CREATE TABLE IF NOT EXISTS xb_member_gateway.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.control_flags (
    flag_name text PRIMARY KEY,
    enabled boolean NOT NULL,
    environment text NOT NULL DEFAULT 'production',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

INSERT INTO xb_member_gateway.control_flags(flag_name, enabled, environment, updated_by)
VALUES
    ('production_activation_enabled', false, 'production', 'migration'),
    ('kill_switch_enabled', true, 'production', 'migration')
ON CONFLICT (flag_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS xb_member_gateway.source_responses (
    response_id text PRIMARY KEY,
    source_response_ref text NOT NULL,
    create_time timestamptz NOT NULL,
    mapping_version text NOT NULL,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    canonical_payload jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.source_observations (
    observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    response_id text NOT NULL REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    request_id text NOT NULL,
    form_alias text NOT NULL,
    create_time timestamptz NOT NULL,
    mapping_version text NOT NULL,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    canonical_payload jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS source_observations_response_idx
    ON xb_member_gateway.source_observations(response_id, observed_at);

CREATE TABLE IF NOT EXISTS xb_member_gateway.ingest_receipts (
    request_id text PRIMARY KEY,
    response_id text NOT NULL REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    replayed boolean NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.jobs (
    job_id text PRIMARY KEY,
    response_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    operation text NOT NULL CHECK (operation = 'member.create'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    canonical_payload jsonb NOT NULL,
    state text NOT NULL CHECK (state IN (
        'RECEIVED', 'VALIDATED', 'QUEUED', 'LEASED', 'PRECHECKING',
        'ALLOCATION_BOUND', 'WRITE_INTENT_RECORDED', 'WRITING', 'READBACK',
        'CREATED_VERIFIED', 'REJECTED_VALIDATION', 'RETRY_WAIT',
        'AMBIGUOUS_LOOKUP', 'WRITE_OUTCOME_UNCERTAIN', 'CONFIRMED_NOT_CREATED',
        'CREATED_READBACK_MISMATCH', 'MANUAL_REVIEW', 'DEAD_LETTER'
    )),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    next_attempt_at timestamptz,
    attempt_started_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    allocation_member_no text,
    allocation_probe_reference text,
    write_intent_id text,
    dispatch_fence_id text,
    save_invocation_count integer NOT NULL DEFAULT 0 CHECK (save_invocation_count >= 0),
    result_status text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON xb_member_gateway.jobs(state, next_attempt_at, created_at, job_id);

CREATE TABLE IF NOT EXISTS xb_member_gateway.attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    worker_id text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    outcome_code text,
    UNIQUE(job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.leases (
    lease_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    worker_id text NOT NULL,
    state_version bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true,
    claimed_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS leases_one_active_per_job_idx
    ON xb_member_gateway.leases(job_id) WHERE active;

CREATE TABLE IF NOT EXISTS xb_member_gateway.allocation_probes (
    probe_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    candidate text NOT NULL,
    probe_status text NOT NULL CHECK (probe_status IN ('FREE', 'OCCUPIED', 'AMBIGUOUS', 'UNAVAILABLE')),
    probe_reference text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id, candidate, probe_reference)
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.member_allocations (
    allocation_id uuid PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    response_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    member_no text NOT NULL UNIQUE,
    probe_reference text NOT NULL,
    bound_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.write_intents (
    intent_id uuid PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    operation text NOT NULL CHECK (operation = 'member.create'),
    member_no text NOT NULL,
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.dispatch_fences (
    fence_id uuid PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    operation text NOT NULL CHECK (operation = 'member.create'),
    member_no text NOT NULL,
    created_at timestamptz NOT NULL,
    save_invocation_count integer NOT NULL DEFAULT 0 CHECK (save_invocation_count = 0)
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.results (
    result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    result_hash text NOT NULL CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN (
        'CREATED_VERIFIED', 'WRITE_OUTCOME_UNCERTAIN',
        'CONFIRMED_NOT_CREATED', 'CREATED_READBACK_MISMATCH'
    )),
    member_no text NOT NULL,
    dispatch_fence_id uuid NOT NULL REFERENCES xb_member_gateway.dispatch_fences(fence_id) ON DELETE RESTRICT,
    save_invocation_count integer NOT NULL CHECK (save_invocation_count >= 0),
    readback_found boolean NOT NULL,
    readback_match boolean NOT NULL,
    reconciliation_required boolean NOT NULL,
    error_code text,
    acknowledged_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.result_conflicts (
    conflict_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    expected_hash text NOT NULL,
    observed_hash text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.reconciliation_cases (
    case_id uuid PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    member_no text NOT NULL,
    case_state text NOT NULL CHECK (case_state IN ('OPEN', 'EXACT_MATCH', 'ABSENT', 'MISMATCH', 'AMBIGUOUS', 'MANUAL_REVIEW')),
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.reconciliation_checks (
    check_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id uuid NOT NULL REFERENCES xb_member_gateway.reconciliation_cases(case_id) ON DELETE RESTRICT,
    lookup_status text NOT NULL CHECK (lookup_status IN ('exact_match', 'absent', 'mismatch', 'ambiguous')),
    readback_found boolean NOT NULL,
    readback_match boolean NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.dead_letters (
    dead_letter_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    error_code text NOT NULL,
    attempt_count integer NOT NULL,
    lineage jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.rejections (
    rejection_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    response_id text REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    request_id text NOT NULL,
    error_code text NOT NULL,
    payload_hash text,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.audit_events (
    audit_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    job_id text REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    operation text CHECK (operation IS NULL OR operation = 'member.create'),
    state text,
    safe_reference text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0001_member_gateway')
ON CONFLICT (version) DO NOTHING;

COMMIT;
