-- Shopify-authoritative Milestone 1 (#155 G3): new canonical member-mg create only.
--
-- Additive and transactional. Migrations 0001-0005 are not modified and no
-- Forms row, receipt, result or outbox history is changed. Shopify protected
-- profile data never enters source_responses, source_observations,
-- jobs.canonical_payload or any other immutable history: the only protected
-- values are the transient AES-GCM envelope in shopify_protected_payloads,
-- which is scrubbed at commit once its job is terminally resolved with no
-- uncertain write left to reconcile. No baseline, shop or admission state is
-- seeded; admission starts disabled.

BEGIN;

-- Source-aware jobs. Existing rows are Forms rows by definition.
ALTER TABLE xb_member_gateway.jobs
    ADD COLUMN IF NOT EXISTS source_system text NOT NULL DEFAULT 'google_forms';
ALTER TABLE xb_member_gateway.jobs
    DROP CONSTRAINT IF EXISTS jobs_source_system_check;
ALTER TABLE xb_member_gateway.jobs
    ADD CONSTRAINT jobs_source_system_check CHECK (source_system IN ('google_forms', 'shopify'));
ALTER TABLE xb_member_gateway.jobs ALTER COLUMN response_id DROP NOT NULL;
ALTER TABLE xb_member_gateway.jobs
    DROP CONSTRAINT IF EXISTS jobs_source_identity_check;
-- Forms keeps its mandatory responseId; a Shopify job has none and its
-- canonical_payload is structurally PII-free.
ALTER TABLE xb_member_gateway.jobs
    ADD CONSTRAINT jobs_source_identity_check CHECK (
        (source_system = 'google_forms' AND response_id IS NOT NULL)
        OR (source_system = 'shopify' AND response_id IS NULL AND canonical_payload = '{}'::jsonb)
    );

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_job_source_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.source_system IS DISTINCT FROM OLD.source_system
       OR NEW.response_id IS DISTINCT FROM OLD.response_id
       OR NEW.canonical_payload IS DISTINCT FROM OLD.canonical_payload
       OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash THEN
        RAISE EXCEPTION 'job_source_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS job_source_identity_immutable ON xb_member_gateway.jobs;
CREATE TRIGGER job_source_identity_immutable
BEFORE UPDATE ON xb_member_gateway.jobs
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_job_source_identity();

-- Allocation identity follows the job's source identity exactly.
ALTER TABLE xb_member_gateway.member_allocations ALTER COLUMN response_id DROP NOT NULL;

CREATE OR REPLACE FUNCTION xb_member_gateway.require_allocation_source_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.jobs j
        WHERE j.job_id = NEW.job_id AND j.response_id IS NOT DISTINCT FROM NEW.response_id
    ) THEN
        RAISE EXCEPTION 'allocation_source_identity_mismatch';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS member_allocation_source_identity ON xb_member_gateway.member_allocations;
CREATE TRIGGER member_allocation_source_identity
BEFORE INSERT OR UPDATE ON xb_member_gateway.member_allocations
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.require_allocation_source_identity();

-- The historical Forms welcome_v1 outbox stays Forms-only.
CREATE OR REPLACE FUNCTION xb_member_gateway.require_created_verified_for_welcome()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.jobs
        WHERE job_id = NEW.job_id AND source_system = 'google_forms'
    ) THEN
        RAISE EXCEPTION 'welcome_email_requires_forms_source';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.results
        WHERE job_id = NEW.job_id AND status = 'CREATED_VERIFIED'
    ) THEN
        RAISE EXCEPTION 'welcome_email_requires_created_verified';
    END IF;
    IF NEW.state <> 'PENDING' OR NEW.state_version <> 0 OR NEW.attempt <> 0 THEN
        RAISE EXCEPTION 'welcome_email_insert_state_invalid';
    END IF;
    RETURN NEW;
END;
$$;

-- Bounded delivery metadata + GID only. No body, digest of body or header dump.
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_webhook_receipts (
    webhook_id text PRIMARY KEY CHECK (webhook_id ~ '^[A-Za-z0-9-]{8,100}$'),
    topic text NOT NULL CHECK (topic IN ('customers/create', 'customers/update', 'customer.tags_added')),
    shop_domain text NOT NULL CHECK (shop_domain ~ '^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$'),
    api_version text NOT NULL CHECK (api_version ~ '^20[2-9][0-9]-(01|04|07|10)$'),
    event_id text CHECK (event_id IS NULL OR event_id ~ '^[A-Za-z0-9-]{1,100}$'),
    triggered_at text CHECK (triggered_at IS NULL OR length(triggered_at) BETWEEN 1 AND 40),
    customer_gid text NOT NULL CHECK (customer_gid ~ '^gid://shopify/Customer/[1-9][0-9]{0,19}$'),
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS shopify_webhook_receipts_gid_idx
    ON xb_member_gateway.shopify_webhook_receipts(customer_gid, topic, received_at);

DROP TRIGGER IF EXISTS shopify_webhook_receipts_append_only ON xb_member_gateway.shopify_webhook_receipts;
CREATE TRIGGER shopify_webhook_receipts_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.shopify_webhook_receipts
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- Exhaustive member-mg cutover baseline: historical GIDs only (Shopify
-- createdAt < capture_started_at = cutover C), sealed by count + digest that
-- the database itself recomputes from the member rows. C is the gateway
-- database clock, UTC, whole second; scanned post-C member-mg GIDs become
-- GID-only PENDING admissions in the same seal transaction.
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_cutover_baselines (
    baseline_id text PRIMARY KEY CHECK (baseline_id ~ '^baseline-[0-9a-f]{32}$'),
    state text NOT NULL CHECK (state IN ('CAPTURING', 'SEALED')),
    shop_domain text NOT NULL CHECK (shop_domain ~ '^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$'),
    api_version text NOT NULL CHECK (api_version ~ '^20[2-9][0-9]-(01|04|07|10)$'),
    capture_started_at timestamptz NOT NULL,
    sealed_at timestamptz,
    member_count integer NOT NULL CHECK (member_count >= 0),
    member_digest text NOT NULL CHECK (member_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK ((state = 'SEALED') = (sealed_at IS NOT NULL)),
    CHECK (sealed_at IS NULL OR sealed_at >= capture_started_at),
    CHECK (date_trunc('second', capture_started_at AT TIME ZONE 'UTC') = capture_started_at AT TIME ZONE 'UTC')
);
CREATE UNIQUE INDEX IF NOT EXISTS shopify_cutover_baselines_one_sealed_idx
    ON xb_member_gateway.shopify_cutover_baselines((true)) WHERE state = 'SEALED';

CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_baseline_members (
    baseline_id text NOT NULL REFERENCES xb_member_gateway.shopify_cutover_baselines(baseline_id) ON DELETE RESTRICT,
    customer_gid text NOT NULL CHECK (customer_gid ~ '^gid://shopify/Customer/[1-9][0-9]{0,19}$'),
    PRIMARY KEY (baseline_id, customer_gid)
);

CREATE OR REPLACE FUNCTION xb_member_gateway.shopify_baseline_recomputed(p_baseline_id text)
RETURNS TABLE(member_count integer, member_digest text) LANGUAGE sql STABLE AS $$
    SELECT count(*)::integer,
           'sha256:' || encode(sha256(convert_to(COALESCE(string_agg(customer_gid, E'\n' ORDER BY customer_gid COLLATE "C"), ''), 'UTF8')), 'hex')
    FROM xb_member_gateway.shopify_baseline_members WHERE baseline_id = p_baseline_id
$$;

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_shopify_baseline()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    recomputed record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shopify_baseline_delete_forbidden';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'CAPTURING' THEN
            RAISE EXCEPTION 'shopify_baseline_must_start_capturing';
        END IF;
        -- Receiver-before-C: an HMAC-verified delivery for this exact shop and
        -- API version was durably received strictly before the cutover.
        IF NOT EXISTS (
            SELECT 1 FROM xb_member_gateway.shopify_webhook_receipts r
            WHERE r.shop_domain = NEW.shop_domain AND r.api_version = NEW.api_version
              AND r.received_at < NEW.capture_started_at
        ) THEN
            RAISE EXCEPTION 'shopify_receiver_not_verified_before_cutover';
        END IF;
        IF NEW.capture_started_at > clock_timestamp() THEN
            RAISE EXCEPTION 'shopify_cutover_in_future';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state <> 'CAPTURING' OR NEW.state <> 'SEALED'
       OR NEW.baseline_id IS DISTINCT FROM OLD.baseline_id
       OR NEW.shop_domain IS DISTINCT FROM OLD.shop_domain
       OR NEW.api_version IS DISTINCT FROM OLD.api_version
       OR NEW.capture_started_at IS DISTINCT FROM OLD.capture_started_at
       OR NEW.member_count IS DISTINCT FROM OLD.member_count
       OR NEW.member_digest IS DISTINCT FROM OLD.member_digest THEN
        RAISE EXCEPTION 'shopify_baseline_immutable';
    END IF;
    SELECT * INTO recomputed FROM xb_member_gateway.shopify_baseline_recomputed(NEW.baseline_id);
    IF recomputed.member_count <> NEW.member_count OR recomputed.member_digest <> NEW.member_digest THEN
        RAISE EXCEPTION 'shopify_baseline_seal_mismatch';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shopify_baseline_protected ON xb_member_gateway.shopify_cutover_baselines;
CREATE TRIGGER shopify_baseline_protected
BEFORE INSERT OR UPDATE OR DELETE ON xb_member_gateway.shopify_cutover_baselines
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_shopify_baseline();

-- A capture that does not seal in its own transaction cannot commit.
CREATE OR REPLACE FUNCTION xb_member_gateway.require_shopify_baseline_sealed()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM xb_member_gateway.shopify_cutover_baselines
        WHERE baseline_id = NEW.baseline_id AND state <> 'SEALED'
    ) THEN
        RAISE EXCEPTION 'shopify_baseline_partial_capture';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS shopify_baseline_sealed_at_commit ON xb_member_gateway.shopify_cutover_baselines;
CREATE CONSTRAINT TRIGGER shopify_baseline_sealed_at_commit
AFTER INSERT ON xb_member_gateway.shopify_cutover_baselines
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.require_shopify_baseline_sealed();

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_shopify_baseline_members()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'shopify_baseline_members_append_only';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.shopify_cutover_baselines
        WHERE baseline_id = NEW.baseline_id AND state = 'CAPTURING'
    ) THEN
        RAISE EXCEPTION 'shopify_baseline_sealed_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shopify_baseline_members_protected ON xb_member_gateway.shopify_baseline_members;
CREATE TRIGGER shopify_baseline_members_protected
BEFORE INSERT OR UPDATE OR DELETE ON xb_member_gateway.shopify_baseline_members
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_shopify_baseline_members();

-- Singleton admission control; starts disabled and can only be enabled
-- against the sealed baseline whose recomputed count/digest still match.
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_admission_control (
    control_id smallint PRIMARY KEY CHECK (control_id = 1),
    admission_enabled boolean NOT NULL DEFAULT false,
    baseline_id text REFERENCES xb_member_gateway.shopify_cutover_baselines(baseline_id) ON DELETE RESTRICT,
    approval_reference text CHECK (approval_reference IS NULL OR approval_reference ~ '^[A-Za-z0-9._:/#-]{1,160}$'),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (NOT admission_enabled OR (baseline_id IS NOT NULL AND approval_reference IS NOT NULL))
);

INSERT INTO xb_member_gateway.shopify_admission_control(control_id, admission_enabled)
VALUES (1, false)
ON CONFLICT (control_id) DO NOTHING;

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_shopify_admission_control()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    sealed record;
    recomputed record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shopify_admission_control_delete_forbidden';
    END IF;
    IF NEW.state_version <> OLD.state_version + 1 THEN
        RAISE EXCEPTION 'shopify_admission_control_state_version_must_increment';
    END IF;
    IF OLD.baseline_id IS NOT NULL AND NEW.baseline_id IS DISTINCT FROM OLD.baseline_id THEN
        RAISE EXCEPTION 'shopify_admission_baseline_immutable';
    END IF;
    IF NEW.admission_enabled THEN
        SELECT * INTO sealed FROM xb_member_gateway.shopify_cutover_baselines
        WHERE baseline_id = NEW.baseline_id AND state = 'SEALED';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'shopify_admission_requires_sealed_baseline';
        END IF;
        SELECT * INTO recomputed FROM xb_member_gateway.shopify_baseline_recomputed(NEW.baseline_id);
        IF recomputed.member_count <> sealed.member_count OR recomputed.member_digest <> sealed.member_digest THEN
            RAISE EXCEPTION 'shopify_admission_baseline_verification_failed';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shopify_admission_control_protected ON xb_member_gateway.shopify_admission_control;
CREATE TRIGGER shopify_admission_control_protected
BEFORE UPDATE OR DELETE ON xb_member_gateway.shopify_admission_control
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_shopify_admission_control();

-- Per-GID admission state and the durable GID <-> job mapping. The job's
-- allocation row carries the bound MemberNo, so GID -> job -> MemberNo is
-- unique in both directions. No profile value is stored here.
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_member_admissions (
    customer_gid text PRIMARY KEY CHECK (customer_gid ~ '^gid://shopify/Customer/[1-9][0-9]{0,19}$'),
    gid_ref text NOT NULL UNIQUE CHECK (gid_ref ~ '^hmac-v1:[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('PENDING', 'NOT_ELIGIBLE', 'EXCLUDED_BASELINE', 'MANUAL_REVIEW', 'ADMITTED')),
    reason_code text CHECK (reason_code IS NULL OR reason_code ~ '^[a-z0-9_]{1,60}$'),
    check_count integer NOT NULL DEFAULT 0 CHECK (check_count >= 0),
    next_check_at timestamptz,
    job_id text UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    baseline_id text REFERENCES xb_member_gateway.shopify_cutover_baselines(baseline_id) ON DELETE RESTRICT,
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'ADMITTED') = (job_id IS NOT NULL)),
    CHECK (state <> 'MANUAL_REVIEW' OR reason_code IS NOT NULL),
    CHECK (state <> 'PENDING' OR next_check_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS shopify_member_admissions_due_idx
    ON xb_member_gateway.shopify_member_admissions(state, next_check_at);

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_shopify_member_admission()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'shopify_member_admission_delete_forbidden';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'PENDING' OR NEW.job_id IS NOT NULL OR NEW.state_version <> 0 THEN
            RAISE EXCEPTION 'shopify_member_admission_insert_invalid';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.customer_gid IS DISTINCT FROM OLD.customer_gid OR NEW.gid_ref IS DISTINCT FROM OLD.gid_ref
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'shopify_member_admission_identity_immutable';
    END IF;
    IF NEW.state_version <> OLD.state_version + 1 THEN
        RAISE EXCEPTION 'shopify_member_admission_state_version_must_increment';
    END IF;
    IF OLD.state IN ('EXCLUDED_BASELINE', 'MANUAL_REVIEW', 'ADMITTED') THEN
        RAISE EXCEPTION 'shopify_member_admission_terminal_immutable';
    END IF;
    IF OLD.state = 'NOT_ELIGIBLE' AND NEW.state <> 'PENDING' THEN
        RAISE EXCEPTION 'shopify_member_admission_transition_forbidden';
    END IF;
    IF NEW.state = 'ADMITTED' AND NOT EXISTS (
        SELECT 1 FROM xb_member_gateway.shopify_admission_control c
        WHERE c.control_id = 1 AND c.admission_enabled AND c.baseline_id = NEW.baseline_id
    ) THEN
        RAISE EXCEPTION 'shopify_admission_disabled';
    END IF;
    IF NEW.state IN ('ADMITTED', 'EXCLUDED_BASELINE') AND NEW.baseline_id IS NULL THEN
        RAISE EXCEPTION 'shopify_member_admission_baseline_required';
    END IF;
    IF NEW.state = 'ADMITTED' AND EXISTS (
        SELECT 1 FROM xb_member_gateway.shopify_baseline_members m
        WHERE m.baseline_id = NEW.baseline_id AND m.customer_gid = NEW.customer_gid
    ) THEN
        RAISE EXCEPTION 'shopify_baseline_member_not_admissible';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS shopify_member_admission_protected ON xb_member_gateway.shopify_member_admissions;
CREATE TRIGGER shopify_member_admission_protected
BEFORE INSERT OR UPDATE OR DELETE ON xb_member_gateway.shopify_member_admissions
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_shopify_member_admission();

-- PII-free legacy duplicate-precheck evidence (booleans only, never values).
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_legacy_prechecks (
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    attempt_count integer NOT NULL CHECK (attempt_count > 0),
    outcome text NOT NULL CHECK (outcome IN ('NO_CANDIDATE', 'CANDIDATE_FOUND', 'LOOKUP_FAILED')),
    phone_member_no_hit boolean NOT NULL,
    mobile_phone_hit boolean NOT NULL,
    email_hit boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, attempt_count),
    CHECK ((outcome = 'CANDIDATE_FOUND') = (phone_member_no_hit OR mobile_phone_hit OR email_hit))
);

DROP TRIGGER IF EXISTS shopify_legacy_prechecks_append_only ON xb_member_gateway.shopify_legacy_prechecks;
CREATE TRIGGER shopify_legacy_prechecks_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.shopify_legacy_prechecks
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- Purpose-bound transient protected create payload: AEAD envelope only.
CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_protected_payloads (
    job_id text PRIMARY KEY REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    key_id text NOT NULL CHECK (key_id ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    -- PostgreSQL regex repetition is bounded at 255, so the length bound is
    -- a separate predicate.
    envelope text NOT NULL CHECK (
        length(envelope) BETWEEN 50 AND 8300
        AND envelope ~ '^xbpp1\.[a-z0-9][a-z0-9-]{0,31}\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{24,}$'
    ),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.shopify_payload_scrubs (
    job_id text PRIMARY KEY REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    key_id text NOT NULL,
    job_state text NOT NULL,
    scrubbed_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS shopify_payload_scrubs_append_only ON xb_member_gateway.shopify_payload_scrubs;
CREATE TRIGGER shopify_payload_scrubs_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.shopify_payload_scrubs
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- Terminal, resolved and with no live/uncertain writer: the only state in
-- which protected values may (and must) be removed.
CREATE OR REPLACE FUNCTION xb_member_gateway.shopify_payload_scrub_eligible(p_job_id text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM xb_member_gateway.jobs j
        WHERE j.job_id = p_job_id
          AND j.source_system = 'shopify'
          AND j.state IN ('CREATED_VERIFIED', 'REJECTED_VALIDATION', 'CONFIRMED_NOT_CREATED',
                          'CREATED_READBACK_MISMATCH', 'MANUAL_REVIEW', 'DEAD_LETTER')
          AND NOT EXISTS (
              SELECT 1 FROM xb_member_gateway.writer_execution_holds h
              WHERE h.job_id = j.job_id AND h.lifecycle IN ('PENDING', 'REGISTERED', 'QUARANTINED')
          )
          AND (
              j.dispatch_fence_id IS NULL
              OR EXISTS (
                  SELECT 1 FROM xb_member_gateway.results r
                  WHERE r.job_id = j.job_id AND r.status <> 'WRITE_OUTCOME_UNCERTAIN'
              )
          )
    )
$$;

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_shopify_protected_payload()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1 FROM xb_member_gateway.jobs
            WHERE job_id = NEW.job_id AND source_system = 'shopify' AND payload_hash = NEW.payload_digest
        ) THEN
            RAISE EXCEPTION 'shopify_protected_payload_binding_invalid';
        END IF;
        IF EXISTS (SELECT 1 FROM xb_member_gateway.shopify_payload_scrubs WHERE job_id = NEW.job_id) THEN
            RAISE EXCEPTION 'shopify_protected_payload_already_scrubbed';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'shopify_protected_payload_immutable';
    END IF;
    IF NOT xb_member_gateway.shopify_payload_scrub_eligible(OLD.job_id) THEN
        RAISE EXCEPTION 'shopify_protected_payload_retention_required';
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS shopify_protected_payload_protected ON xb_member_gateway.shopify_protected_payloads;
CREATE TRIGGER shopify_protected_payload_protected
BEFORE INSERT OR UPDATE OR DELETE ON xb_member_gateway.shopify_protected_payloads
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_shopify_protected_payload();

-- Transactional scrub: evaluated at commit, after the same transaction has
-- written the result row and cleared the writer hold.
CREATE OR REPLACE FUNCTION xb_member_gateway.scrub_resolved_shopify_payload()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    removed record;
BEGIN
    IF xb_member_gateway.shopify_payload_scrub_eligible(NEW.job_id) THEN
        DELETE FROM xb_member_gateway.shopify_protected_payloads WHERE job_id = NEW.job_id
        RETURNING key_id INTO removed;
        IF FOUND THEN
            INSERT INTO xb_member_gateway.shopify_payload_scrubs(job_id, key_id, job_state)
            SELECT NEW.job_id, removed.key_id, j.state FROM xb_member_gateway.jobs j WHERE j.job_id = NEW.job_id;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS jobs_shopify_payload_scrub ON xb_member_gateway.jobs;
CREATE CONSTRAINT TRIGGER jobs_shopify_payload_scrub
AFTER UPDATE ON xb_member_gateway.jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW WHEN (NEW.source_system = 'shopify')
EXECUTE FUNCTION xb_member_gateway.scrub_resolved_shopify_payload();

-- Operator/audit join: GID -> job -> bound MemberNo. Not exposed by any
-- generic operator endpoint.
CREATE OR REPLACE VIEW xb_member_gateway.shopify_member_mappings AS
SELECT a.customer_gid, a.gid_ref, a.job_id, m.member_no, j.state AS job_state
FROM xb_member_gateway.shopify_member_admissions a
JOIN xb_member_gateway.jobs j ON j.job_id = a.job_id
LEFT JOIN xb_member_gateway.member_allocations m ON m.job_id = a.job_id;

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0006_shopify_member_m1')
ON CONFLICT (version) DO NOTHING;

COMMIT;
