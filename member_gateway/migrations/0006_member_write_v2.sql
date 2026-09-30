-- Member write v2 (W-G2-149 section 2.9): XB-MN-1 identity, lease tokens,
-- retry budgets, the append-only member_outcomes and job_resolutions tables,
-- and the welcome trigger that accepts CREATED_VERIFIED from either table.
--
-- Additive and transactional. Migrations 0001-0005 are not modified. No
-- table, column, row, index, trigger or function is dropped. The single
-- DROP CONSTRAINT replaces jobs_state_check with a strict superset (every
-- old value kept, LINKED_EXISTING and RESOLVED added); PostgreSQL cannot
-- widen a CHECK constraint in place. The v1 allocation, fence, writer,
-- hold and result tables stay physically present, unused and read-only;
-- retiring them needs a separately approved migration.

BEGIN;

-- 1. Safety guard. Runs once: refuses while any job is live or in a legacy
--    non-terminal state, or while any writer execution hold is not cleared.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM xb_member_gateway.schema_migrations WHERE version = '0006_member_write_v2'
    ) THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM xb_member_gateway.jobs
        WHERE state NOT IN (
            'CREATED_VERIFIED', 'REJECTED_VALIDATION', 'CONFIRMED_NOT_CREATED',
            'CREATED_READBACK_MISMATCH', 'MANUAL_REVIEW', 'DEAD_LETTER'
        )
    ) THEN
        RAISE EXCEPTION 'member_write_v2_guard_nonterminal_jobs_present';
    END IF;
    IF EXISTS (
        SELECT 1 FROM xb_member_gateway.writer_execution_holds WHERE lifecycle <> 'CLEARED'
    ) THEN
        RAISE EXCEPTION 'member_write_v2_guard_writer_hold_not_cleared';
    END IF;
END;
$$;

-- 2. jobs: widen the state check (superset) and add the v2 columns.
ALTER TABLE xb_member_gateway.jobs
    DROP CONSTRAINT IF EXISTS jobs_state_check;

ALTER TABLE xb_member_gateway.jobs
    ADD CONSTRAINT jobs_state_check CHECK (state IN (
        'RECEIVED', 'VALIDATED', 'QUEUED', 'LEASED', 'PRECHECKING',
        'ALLOCATION_BOUND', 'WRITE_INTENT_RECORDED', 'WRITING', 'READBACK',
        'CREATED_VERIFIED', 'REJECTED_VALIDATION', 'RETRY_WAIT',
        'AMBIGUOUS_LOOKUP', 'WRITE_OUTCOME_UNCERTAIN',
        'WRITER_TERMINATION_UNCONFIRMED', 'CONFIRMED_NOT_CREATED',
        'CREATED_READBACK_MISMATCH', 'MANUAL_REVIEW', 'DEAD_LETTER',
        'LINKED_EXISTING', 'RESOLVED'
    ));

ALTER TABLE xb_member_gateway.jobs
    ADD COLUMN IF NOT EXISTS member_no_rule text
        CHECK (member_no_rule IS NULL OR member_no_rule = 'XB-MN-1'),
    ADD COLUMN IF NOT EXISTS base_member_no text
        CHECK (base_member_no IS NULL OR base_member_no ~ '^[0-9]{6,15}$'),
    ADD COLUMN IF NOT EXISTS name_component text
        CHECK (name_component IS NULL OR name_component ~ '^[A-Z]{0,14}$'),
    ADD COLUMN IF NOT EXISTS first_claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS write_attempts integer NOT NULL DEFAULT 0
        CHECK (write_attempts BETWEEN 0 AND 12),
    ADD COLUMN IF NOT EXISTS busy_attempts integer NOT NULL DEFAULT 0
        CHECK (busy_attempts BETWEEN 0 AND 12),
    ADD COLUMN IF NOT EXISTS outcome_reason text
        CHECK (outcome_reason IS NULL OR outcome_reason ~ '^[a-z0-9_.:-]{1,80}$');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_member_identity_complete'
          AND conrelid = 'xb_member_gateway.jobs'::regclass
    ) THEN
        ALTER TABLE xb_member_gateway.jobs
            ADD CONSTRAINT jobs_member_identity_complete CHECK (
                (member_no_rule IS NULL AND base_member_no IS NULL AND name_component IS NULL)
                OR (member_no_rule IS NOT NULL AND base_member_no IS NOT NULL AND name_component IS NOT NULL
                    AND length(base_member_no || name_component) <= 20)
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_write_budget_cap'
          AND conrelid = 'xb_member_gateway.jobs'::regclass
    ) THEN
        ALTER TABLE xb_member_gateway.jobs
            ADD CONSTRAINT jobs_write_budget_cap CHECK (max_attempts BETWEEN 1 AND 12);
    END IF;
END;
$$;

-- The XB-MN-1 identity is computed once at VALIDATED and never changes.
CREATE OR REPLACE FUNCTION xb_member_gateway.protect_job_member_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.member_no_rule IS NOT NULL AND NEW.member_no_rule IS DISTINCT FROM OLD.member_no_rule)
       OR (OLD.base_member_no IS NOT NULL AND NEW.base_member_no IS DISTINCT FROM OLD.base_member_no)
       OR (OLD.name_component IS NOT NULL AND NEW.name_component IS DISTINCT FROM OLD.name_component)
       OR (OLD.first_claimed_at IS NOT NULL AND NEW.first_claimed_at IS DISTINCT FROM OLD.first_claimed_at) THEN
        RAISE EXCEPTION 'job_member_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

-- 3. attempts: lease token, primitive outcome, rule, save flag, result hash.
ALTER TABLE xb_member_gateway.attempts
    ADD COLUMN IF NOT EXISTS lease_token text
        CHECK (lease_token IS NULL OR lease_token ~ '^lease-[0-9a-f]{32}$'),
    ADD COLUMN IF NOT EXISTS outcome text
        CHECK (outcome IS NULL OR outcome IN (
            'CREATED_VERIFIED', 'CREATED_VERIFIED_PRIOR_ATTEMPT', 'LINKED_EXISTING', 'MANUAL_REVIEW',
            'REJECTED_VALIDATION', 'FAILED_BEFORE_WRITE', 'NOT_CREATED', 'NOT_CREATED_CONFLICT',
            'OUTCOME_UNCERTAIN', 'CREATED_READBACK_MISMATCH', 'MUTEX_BUSY', 'LEASE_EXPIRED'
        )),
    ADD COLUMN IF NOT EXISTS rule text
        CHECK (rule IS NULL OR rule IN ('R0', 'R1', 'R2a', 'R2b', 'R2c', 'R3', 'R3b', 'R4', 'NONE')),
    ADD COLUMN IF NOT EXISTS save_invoked boolean,
    ADD COLUMN IF NOT EXISTS result_hash text
        CHECK (result_hash IS NULL OR result_hash ~ '^sha256:[0-9a-f]{64}$');

-- 4. leases: the opaque v2 lease token, unique across all leases.
ALTER TABLE xb_member_gateway.leases
    ADD COLUMN IF NOT EXISTS lease_token text
        CHECK (lease_token IS NULL OR lease_token ~ '^lease-[0-9a-f]{32}$');

CREATE UNIQUE INDEX IF NOT EXISTS leases_lease_token_idx
    ON xb_member_gateway.leases(lease_token)
    WHERE lease_token IS NOT NULL;

-- 5. member_outcomes: append-only credit of an AutoCount member to a job.
--    member_no and member_guid are private: never logged, never shown.
CREATE TABLE IF NOT EXISTS xb_member_gateway.member_outcomes (
    job_id text PRIMARY KEY REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    outcome text NOT NULL CHECK (outcome IN ('CREATED_VERIFIED', 'LINKED_EXISTING')),
    rule text NOT NULL CHECK (rule IN ('R0', 'R1', 'R2c', 'R4')),
    member_no text NOT NULL CHECK (length(member_no) BETWEEN 1 AND 20),
    member_guid uuid NOT NULL,
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    result_hash text NOT NULL CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL
);

-- An AutoCount member is credited as CREATED_VERIFIED to at most one job.
CREATE UNIQUE INDEX IF NOT EXISTS member_outcomes_created_guid_idx
    ON xb_member_gateway.member_outcomes(member_guid)
    WHERE outcome = 'CREATED_VERIFIED';

-- 6. job_resolutions: append-only operator CLOSE / REQUEUE decisions.
CREATE TABLE IF NOT EXISTS xb_member_gateway.job_resolutions (
    resolution_id text PRIMARY KEY CHECK (resolution_id ~ '^resolution-[0-9a-f]{32}$'),
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    action text NOT NULL CHECK (action IN ('CLOSE', 'REQUEUE')),
    resolution_code text NOT NULL CHECK (resolution_code ~ '^[a-z0-9_.:-]{1,80}$'),
    member_no text CHECK (member_no IS NULL OR length(member_no) BETWEEN 1 AND 20),
    member_guid uuid,
    prior_state_version bigint NOT NULL CHECK (prior_state_version >= 0),
    resulting_state text NOT NULL CHECK (resulting_state IN ('RESOLVED', 'QUEUED')),
    write_budget integer NOT NULL CHECK (write_budget BETWEEN 1 AND 12),
    resolved_by text NOT NULL CHECK (resolved_by ~ '^[A-Za-z0-9._:-]{1,100}$'),
    resolved_at timestamptz NOT NULL,
    CHECK ((action = 'CLOSE') = (resulting_state = 'RESOLVED'))
);

CREATE INDEX IF NOT EXISTS job_resolutions_job_idx
    ON xb_member_gateway.job_resolutions(job_id, resolved_at);

-- Triggers are created once; nothing is dropped on a re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'job_member_identity_immutable'
          AND tgrelid = 'xb_member_gateway.jobs'::regclass
    ) THEN
        CREATE TRIGGER job_member_identity_immutable
        BEFORE UPDATE ON xb_member_gateway.jobs
        FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_job_member_identity();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'member_outcomes_append_only'
          AND tgrelid = 'xb_member_gateway.member_outcomes'::regclass
    ) THEN
        CREATE TRIGGER member_outcomes_append_only
        BEFORE UPDATE OR DELETE ON xb_member_gateway.member_outcomes
        FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'job_resolutions_append_only'
          AND tgrelid = 'xb_member_gateway.job_resolutions'::regclass
    ) THEN
        CREATE TRIGGER job_resolutions_append_only
        BEFORE UPDATE OR DELETE ON xb_member_gateway.job_resolutions
        FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();
    END IF;
END;
$$;

-- 7. Welcome trigger: an outbox row requires CREATED_VERIFIED in the legacy
--    results table or in member_outcomes. The existing trigger keeps calling
--    this function by name, so only the body is replaced.
CREATE OR REPLACE FUNCTION xb_member_gateway.require_created_verified_for_welcome()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.results
        WHERE job_id = NEW.job_id AND status = 'CREATED_VERIFIED'
    ) AND NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.member_outcomes
        WHERE job_id = NEW.job_id AND outcome = 'CREATED_VERIFIED'
    ) THEN
        RAISE EXCEPTION 'welcome_email_requires_created_verified';
    END IF;
    IF NEW.state <> 'PENDING' OR NEW.state_version <> 0 OR NEW.attempt <> 0 THEN
        RAISE EXCEPTION 'welcome_email_insert_state_invalid';
    END IF;
    RETURN NEW;
END;
$$;

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0006_member_write_v2')
ON CONFLICT (version) DO NOTHING;

COMMIT;
