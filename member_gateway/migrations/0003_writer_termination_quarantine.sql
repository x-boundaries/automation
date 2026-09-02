-- Durable writer liveness quarantine.  No process or gateway call is made in
-- this migration; all writer transitions are serialized by the gate row.

BEGIN;

ALTER TABLE xb_member_gateway.jobs
    DROP CONSTRAINT IF EXISTS jobs_state_check;

ALTER TABLE xb_member_gateway.jobs
    ADD CONSTRAINT jobs_state_check CHECK (state IN (
        'RECEIVED', 'VALIDATED', 'QUEUED', 'LEASED', 'PRECHECKING',
        'ALLOCATION_BOUND', 'WRITE_INTENT_RECORDED', 'WRITING', 'READBACK',
        'CREATED_VERIFIED', 'REJECTED_VALIDATION', 'RETRY_WAIT',
        'AMBIGUOUS_LOOKUP', 'WRITE_OUTCOME_UNCERTAIN',
        'WRITER_TERMINATION_UNCONFIRMED', 'CONFIRMED_NOT_CREATED',
        'CREATED_READBACK_MISMATCH', 'MANUAL_REVIEW', 'DEAD_LETTER'
    ));

CREATE TABLE IF NOT EXISTS xb_member_gateway.writer_termination_gate (
    gate_id smallint PRIMARY KEY CHECK (gate_id = 1),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO xb_member_gateway.writer_termination_gate(gate_id)
VALUES (1)
ON CONFLICT (gate_id) DO NOTHING;

-- Persist the existing fresh-bound-MemberNo proof used by the dispatch fence.
-- NULL is retained only for pre-0003 rows until the worker re-records intent.
ALTER TABLE xb_member_gateway.write_intents
    ADD COLUMN IF NOT EXISTS recheck_id text;

CREATE TABLE IF NOT EXISTS xb_member_gateway.writer_execution_holds (
    hold_id uuid PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    fence_id uuid NOT NULL UNIQUE REFERENCES xb_member_gateway.dispatch_fences(fence_id) ON DELETE RESTRICT,
    attempt_count integer NOT NULL CHECK (attempt_count > 0),
    worker_session text,
    host_binding text,
    execution_id text NOT NULL UNIQUE,
    member_no text NOT NULL,
    lifecycle text NOT NULL CHECK (lifecycle IN (
        'PENDING', 'REGISTERED', 'TERMINATION_CONFIRMED', 'QUARANTINED', 'CLEARED'
    )),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    process_pid bigint,
    -- Keep the exact host-reported process-start identity.  Converting it to
    -- timestamptz would round precision and break exact registration/proof
    -- matching across the PostgreSQL boundary.
    process_start_at text,
    evidence_type text,
    evidence_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    registered_at timestamptz,
    termination_confirmed_at timestamptz,
    quarantined_at timestamptz,
    cleared_at timestamptz,
    CHECK ((process_pid IS NULL) = (process_start_at IS NULL)),
    CHECK (process_start_at IS NULL OR length(process_start_at) BETWEEN 1 AND 80),
    CHECK ((worker_session IS NULL) = (host_binding IS NULL)),
    CHECK (lifecycle IN ('PENDING', 'QUARANTINED') OR (process_pid IS NOT NULL AND process_start_at IS NOT NULL)),
    CHECK (lifecycle <> 'REGISTERED' OR (registered_at IS NOT NULL AND process_pid IS NOT NULL AND process_start_at IS NOT NULL)),
    CHECK (lifecycle <> 'TERMINATION_CONFIRMED' OR termination_confirmed_at IS NOT NULL),
    CHECK (lifecycle <> 'TERMINATION_CONFIRMED' OR (evidence_type IN ('process_exit', 'termination_recovery') AND evidence_reference IS NOT NULL)),
    CHECK (lifecycle <> 'QUARANTINED' OR quarantined_at IS NOT NULL),
    CHECK (lifecycle <> 'QUARANTINED' OR (evidence_type IN ('legacy_unproven', 'quarantine') AND evidence_reference IS NOT NULL)),
    CHECK (lifecycle <> 'CLEARED' OR cleared_at IS NOT NULL),
    CHECK (lifecycle <> 'CLEARED' OR (termination_confirmed_at IS NOT NULL AND evidence_type IN ('process_exit', 'termination_recovery') AND evidence_reference IS NOT NULL)),
    CHECK (evidence_type IS NULL OR evidence_type IN ('process_exit', 'legacy_unproven', 'termination_recovery', 'quarantine')),
    CHECK (evidence_reference IS NULL OR evidence_reference ~ '^[A-Za-z0-9._:-]{1,200}$')
);

CREATE INDEX IF NOT EXISTS writer_execution_holds_lifecycle_idx
    ON xb_member_gateway.writer_execution_holds(lifecycle, updated_at);
CREATE INDEX IF NOT EXISTS writer_execution_holds_job_lifecycle_idx
    ON xb_member_gateway.writer_execution_holds(job_id, lifecycle);

-- Existing uncertainty is not termination proof.  Materialize a conservative
-- legacy hold without inventing process identity or clearing any result event.
INSERT INTO xb_member_gateway.writer_execution_holds(
    hold_id, job_id, fence_id, attempt_count, worker_session, host_binding,
    execution_id, member_no, lifecycle, state_version, evidence_type,
    evidence_reference, quarantined_at
)
SELECT
    md5('writer-hold:' || j.job_id)::uuid,
    j.job_id,
    f.fence_id,
    GREATEST(j.attempt_count, 1),
    NULL,
    NULL,
    'legacy-execution-' || md5(j.job_id),
    COALESCE(j.allocation_member_no, f.member_no),
    'QUARANTINED',
    1,
    'legacy_unproven',
    'legacy-' || md5(j.job_id),
    now()
FROM xb_member_gateway.jobs j
JOIN xb_member_gateway.dispatch_fences f ON f.job_id = j.job_id
WHERE j.dispatch_fence_id IS NOT NULL
  AND j.state IN ('WRITING', 'READBACK', 'WRITE_OUTCOME_UNCERTAIN')
  AND NOT EXISTS (
      SELECT 1 FROM xb_member_gateway.writer_execution_holds h WHERE h.job_id = j.job_id
  )
ON CONFLICT (job_id) DO NOTHING;

UPDATE xb_member_gateway.jobs j
SET state = 'WRITER_TERMINATION_UNCONFIRMED',
    last_error_code = 'legacy_writer_termination_unconfirmed',
    state_version = state_version + 1,
    updated_at = now()
WHERE j.dispatch_fence_id IS NOT NULL
  AND j.state IN ('WRITING', 'READBACK', 'WRITE_OUTCOME_UNCERTAIN')
  AND EXISTS (
      SELECT 1 FROM xb_member_gateway.writer_execution_holds h
      WHERE h.job_id = j.job_id AND h.lifecycle = 'QUARANTINED'
  );

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0003_writer_termination_quarantine')
ON CONFLICT (version) DO NOTHING;

COMMIT;
