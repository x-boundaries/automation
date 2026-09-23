-- Member vertical slice: exact Google source identity, receipt-driven
-- admission, epoch-scoped page protocol, and the durable welcome_v1 outbox.
--
-- Additive and transactional. Migrations 0001-0004 are not modified, no data
-- is deleted, and no production cutover, form ID or cursor is seeded here.
-- The deprecated 0004 admitted tuple, resume token and global page receipts
-- remain physically present as audit-only history; no code path reads them as
-- admission, filtering, checkpoint or skip authority.

BEGIN;

-- Exact Google createTime: 0, 3, 6 or 9 fractional digits, UTC 'Z' only.
-- The existing create_time timestamptz columns stay as business derivatives.
ALTER TABLE xb_member_gateway.source_responses
    ADD COLUMN IF NOT EXISTS create_time_exact text
        CHECK (create_time_exact IS NULL OR create_time_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$');
ALTER TABLE xb_member_gateway.source_observations
    ADD COLUMN IF NOT EXISTS create_time_exact text
        CHECK (create_time_exact IS NULL OR create_time_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$');

-- Rows admitted before 0005 cannot have their exact Google string proven, so
-- the column stays nullable and is never guessed. Bootstrap readiness fails
-- closed while any source response lacks create_time_exact.
CREATE OR REPLACE FUNCTION xb_member_gateway.protect_source_response_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source_response_delete_forbidden';
    END IF;
    IF NEW.response_id IS DISTINCT FROM OLD.response_id
       OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
       OR NEW.create_time IS DISTINCT FROM OLD.create_time
       OR NEW.mapping_version IS DISTINCT FROM OLD.mapping_version
       OR (OLD.create_time_exact IS NOT NULL AND NEW.create_time_exact IS DISTINCT FROM OLD.create_time_exact) THEN
        RAISE EXCEPTION 'source_response_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_response_identity_immutable ON xb_member_gateway.source_responses;
CREATE TRIGGER source_response_identity_immutable
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_responses
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_source_response_identity();

-- Fixed production cutover and private form binding on the cursor row. Both
-- are set once by the separately authorised initialisation and never change.
ALTER TABLE xb_member_gateway.source_ingest_cursors
    ADD COLUMN IF NOT EXISTS production_cutover_exact text
        CHECK (production_cutover_exact IS NULL OR production_cutover_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$'),
    ADD COLUMN IF NOT EXISTS form_id text
        CHECK (form_id IS NULL OR form_id ~ '^[A-Za-z0-9._:-]{1,200}$');

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_source_cutover_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.production_cutover_exact IS NOT NULL AND NEW.production_cutover_exact IS DISTINCT FROM OLD.production_cutover_exact)
       OR (OLD.form_id IS NOT NULL AND NEW.form_id IS DISTINCT FROM OLD.form_id) THEN
        RAISE EXCEPTION 'source_production_cutover_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_production_cutover_immutable ON xb_member_gateway.source_ingest_cursors;
CREATE TRIGGER source_production_cutover_immutable
BEFORE UPDATE ON xb_member_gateway.source_ingest_cursors
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_source_cutover_identity();

CREATE OR REPLACE FUNCTION xb_member_gateway.reject_append_only_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%_append_only', TG_TABLE_NAME;
END;
$$;

-- PII-free customer-validation rejections. No customer field is stored.
CREATE TABLE IF NOT EXISTS xb_member_gateway.source_rejections (
    response_id text PRIMARY KEY CHECK (response_id ~ '^[A-Za-z0-9._:-]{1,200}$'),
    rejection_id text NOT NULL UNIQUE CHECK (rejection_id ~ '^rejection-[0-9a-f]{32}$'),
    source_response_ref text NOT NULL CHECK (source_response_ref ~ '^hmac-v1:[0-9a-f]{64}$'),
    form_alias text NOT NULL,
    form_id text NOT NULL,
    mapping_version text NOT NULL,
    create_time_exact text NOT NULL
        CHECK (create_time_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    error_code text NOT NULL CHECK (error_code IN (
        'name_invalid', 'email_invalid', 'phone_required',
        'phone_contains_letters_or_unsupported_characters', 'phone_shape_invalid',
        'birthday_month_invalid', 'marketing_consent_must_be_yes_or_no'
    )),
    request_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS source_rejections_append_only ON xb_member_gateway.source_rejections;
CREATE TRIGGER source_rejections_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_rejections
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- One unified durable handling receipt per responseId. ACCEPTED binds the
-- member job; REJECTED binds the PII-free rejection. Exactly one is present.
CREATE TABLE IF NOT EXISTS xb_member_gateway.source_handling_receipts (
    response_id text PRIMARY KEY,
    source_response_ref text NOT NULL CHECK (source_response_ref ~ '^hmac-v1:[0-9a-f]{64}$'),
    form_alias text NOT NULL,
    form_id text NOT NULL,
    mapping_version text NOT NULL,
    create_time_exact text NOT NULL
        CHECK (create_time_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$'),
    payload_fingerprint text NOT NULL CHECK (payload_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK (outcome IN ('ACCEPTED', 'REJECTED')),
    job_id text UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    rejection_id text UNIQUE REFERENCES xb_member_gateway.source_rejections(rejection_id) ON DELETE RESTRICT,
    receipt_version integer NOT NULL DEFAULT 1 CHECK (receipt_version = 1),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (outcome = 'ACCEPTED' AND job_id IS NOT NULL AND rejection_id IS NULL)
        OR (outcome = 'REJECTED' AND rejection_id IS NOT NULL AND job_id IS NULL)
    )
);

DROP TRIGGER IF EXISTS source_handling_receipts_append_only ON xb_member_gateway.source_handling_receipts;
CREATE TRIGGER source_handling_receipts_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_handling_receipts
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- Scan epochs. Every epoch repeats the same inclusive fixed-cutover filter.
CREATE TABLE IF NOT EXISTS xb_member_gateway.source_scan_epochs (
    epoch_id text PRIMARY KEY CHECK (epoch_id ~ '^epoch-[0-9a-f]{32}$'),
    source_system text NOT NULL,
    form_alias text NOT NULL,
    form_id text NOT NULL CHECK (form_id ~ '^[A-Za-z0-9._:-]{1,200}$'),
    mapping_version text NOT NULL,
    admission_mode text NOT NULL CHECK (admission_mode IN ('first_member', 'continuous')),
    production_cutover_exact text NOT NULL
        CHECK (production_cutover_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$'),
    filter_exact text NOT NULL,
    page_size integer NOT NULL CHECK (page_size = 1),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED', 'ABANDONED')),
    epoch_state_version bigint NOT NULL DEFAULT 0 CHECK (epoch_state_version >= 0),
    current_page_token text CHECK (current_page_token IS NULL OR (
        length(current_page_token) BETWEEN 1 AND 1024 AND current_page_token ~ '^[ -~]+$'
    )),
    page_ordinal integer NOT NULL DEFAULT 0 CHECK (page_ordinal >= 0),
    predecessor_epoch_id text REFERENCES xb_member_gateway.source_scan_epochs(epoch_id) ON DELETE RESTRICT,
    restart_reason text CHECK (restart_reason IS NULL OR restart_reason IN ('token_invalidated', 'ambiguous_crashed_attempt')),
    abandon_reason text CHECK (abandon_reason IS NULL OR abandon_reason IN ('token_invalidated', 'ambiguous_crashed_attempt', 'admission_mode_changed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    abandoned_at timestamptz,
    FOREIGN KEY (source_system, form_alias, mapping_version)
        REFERENCES xb_member_gateway.source_ingest_cursors(source_system, form_alias, mapping_version)
        ON DELETE RESTRICT,
    CHECK (filter_exact = 'timestamp >= ' || production_cutover_exact),
    CHECK ((status = 'ABANDONED') = (abandon_reason IS NOT NULL)),
    CHECK ((status = 'COMPLETED') = (completed_at IS NOT NULL)),
    CHECK (restart_reason IS NULL OR predecessor_epoch_id IS NOT NULL)
);

-- At most one ACTIVE epoch per source binding (and therefore per immutable
-- epoch binding).
CREATE UNIQUE INDEX IF NOT EXISTS source_scan_epochs_one_active_idx
    ON xb_member_gateway.source_scan_epochs(source_system, form_alias, mapping_version)
    WHERE status = 'ACTIVE';

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_source_scan_epoch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source_scan_epoch_delete_forbidden';
    END IF;
    IF NEW.epoch_id IS DISTINCT FROM OLD.epoch_id
       OR NEW.source_system IS DISTINCT FROM OLD.source_system
       OR NEW.form_alias IS DISTINCT FROM OLD.form_alias
       OR NEW.form_id IS DISTINCT FROM OLD.form_id
       OR NEW.mapping_version IS DISTINCT FROM OLD.mapping_version
       OR NEW.admission_mode IS DISTINCT FROM OLD.admission_mode
       OR NEW.production_cutover_exact IS DISTINCT FROM OLD.production_cutover_exact
       OR NEW.filter_exact IS DISTINCT FROM OLD.filter_exact
       OR NEW.page_size IS DISTINCT FROM OLD.page_size
       OR NEW.predecessor_epoch_id IS DISTINCT FROM OLD.predecessor_epoch_id
       OR NEW.restart_reason IS DISTINCT FROM OLD.restart_reason
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'source_scan_epoch_binding_immutable';
    END IF;
    IF OLD.status <> 'ACTIVE' THEN
        RAISE EXCEPTION 'source_scan_epoch_terminal_immutable';
    END IF;
    IF NEW.epoch_state_version <> OLD.epoch_state_version + 1 THEN
        RAISE EXCEPTION 'source_scan_epoch_state_version_must_increment';
    END IF;
    IF NEW.page_ordinal < OLD.page_ordinal THEN
        RAISE EXCEPTION 'source_scan_epoch_page_ordinal_regressed';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_scan_epoch_protected ON xb_member_gateway.source_scan_epochs;
CREATE TRIGGER source_scan_epoch_protected
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_scan_epochs
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_source_scan_epoch();

-- Two-phase pages: OPEN persists the request/next token and item identities
-- before the epoch token can move; COMMIT verifies every item's receipt.
CREATE TABLE IF NOT EXISTS xb_member_gateway.source_scan_pages (
    page_id text PRIMARY KEY CHECK (page_id ~ '^page-[0-9a-f]{32}$'),
    epoch_id text NOT NULL REFERENCES xb_member_gateway.source_scan_epochs(epoch_id) ON DELETE RESTRICT,
    page_ordinal integer NOT NULL CHECK (page_ordinal >= 0),
    page_state text NOT NULL CHECK (page_state IN ('OPEN', 'COMMITTED', 'ABANDONED')),
    request_page_token text CHECK (request_page_token IS NULL OR (
        length(request_page_token) BETWEEN 1 AND 1024 AND request_page_token ~ '^[ -~]+$'
    )),
    next_page_token text CHECK (next_page_token IS NULL OR (
        length(next_page_token) BETWEEN 1 AND 1024 AND next_page_token ~ '^[ -~]+$'
    )),
    terminal boolean NOT NULL,
    items jsonb NOT NULL CHECK (jsonb_typeof(items) = 'array' AND jsonb_array_length(items) <= 1),
    opened_state_version bigint NOT NULL CHECK (opened_state_version >= 1),
    committed_state_version bigint CHECK (committed_state_version IS NULL OR committed_state_version = opened_state_version + 1),
    opened_at timestamptz NOT NULL DEFAULT now(),
    committed_at timestamptz,
    abandoned_at timestamptz,
    UNIQUE (epoch_id, page_ordinal),
    CHECK (terminal = (next_page_token IS NULL)),
    CHECK (next_page_token IS NULL OR next_page_token IS DISTINCT FROM request_page_token),
    CHECK ((page_state = 'COMMITTED') = (committed_state_version IS NOT NULL AND committed_at IS NOT NULL))
);

-- Token repetition is prohibited only within one epoch. The same opaque token
-- in a successor epoch is not a global conflict.
CREATE UNIQUE INDEX IF NOT EXISTS source_scan_pages_next_token_per_epoch_idx
    ON xb_member_gateway.source_scan_pages(epoch_id, next_page_token)
    WHERE next_page_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS source_scan_pages_request_token_per_epoch_idx
    ON xb_member_gateway.source_scan_pages(epoch_id, request_page_token)
    WHERE request_page_token IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS source_scan_pages_one_open_idx
    ON xb_member_gateway.source_scan_pages(epoch_id)
    WHERE page_state = 'OPEN';

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_source_scan_page()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source_scan_page_delete_forbidden';
    END IF;
    IF OLD.page_state <> 'OPEN' OR NEW.page_state NOT IN ('COMMITTED', 'ABANDONED') THEN
        RAISE EXCEPTION 'source_scan_page_terminal_immutable';
    END IF;
    IF NEW.page_id IS DISTINCT FROM OLD.page_id
       OR NEW.epoch_id IS DISTINCT FROM OLD.epoch_id
       OR NEW.page_ordinal IS DISTINCT FROM OLD.page_ordinal
       OR NEW.request_page_token IS DISTINCT FROM OLD.request_page_token
       OR NEW.next_page_token IS DISTINCT FROM OLD.next_page_token
       OR NEW.terminal IS DISTINCT FROM OLD.terminal
       OR NEW.items IS DISTINCT FROM OLD.items
       OR NEW.opened_state_version IS DISTINCT FROM OLD.opened_state_version
       OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
        RAISE EXCEPTION 'source_scan_page_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_scan_page_protected ON xb_member_gateway.source_scan_pages;
CREATE TRIGGER source_scan_page_protected
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_scan_pages
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_source_scan_page();

-- The FK to the handling receipt makes "commit only with a matching receipt"
-- structural rather than procedural.
CREATE TABLE IF NOT EXISTS xb_member_gateway.source_scan_page_items (
    page_id text NOT NULL REFERENCES xb_member_gateway.source_scan_pages(page_id) ON DELETE RESTRICT,
    response_id text NOT NULL REFERENCES xb_member_gateway.source_handling_receipts(response_id) ON DELETE RESTRICT,
    create_time_exact text NOT NULL
        CHECK (create_time_exact ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6}|\.\d{9})?Z$'),
    payload_fingerprint text NOT NULL CHECK (payload_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (page_id, response_id)
);

DROP TRIGGER IF EXISTS source_scan_page_items_append_only ON xb_member_gateway.source_scan_page_items;
CREATE TRIGGER source_scan_page_items_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_scan_page_items
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

-- Durable welcome_v1 outbox. Inserted only inside the result acknowledgement
-- transaction that records CREATED_VERIFIED.
CREATE TABLE IF NOT EXISTS xb_member_gateway.welcome_email_outbox (
    outbox_id text PRIMARY KEY CHECK (outbox_id ~ '^welcome-[0-9a-f]{32}$'),
    job_id text NOT NULL UNIQUE REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    response_id text NOT NULL REFERENCES xb_member_gateway.source_responses(response_id) ON DELETE RESTRICT,
    source_response_ref text NOT NULL CHECK (source_response_ref ~ '^hmac-v1:[0-9a-f]{64}$'),
    template_id text NOT NULL CHECK (template_id = 'welcome_v1'),
    recipient text NOT NULL CHECK (length(recipient) BETWEEN 3 AND 254),
    message_hash text NOT NULL CHECK (message_hash ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN (
        'PENDING', 'LEASED', 'RETRY_WAIT', 'SEND_INTENT_RECORDED',
        'SENT', 'DELIVERY_OUTCOME_UNCERTAIN', 'DEAD_LETTER'
    )),
    state_version bigint NOT NULL CHECK (state_version >= 0),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND max_attempts),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts = 3),
    lease_id text CHECK (lease_id IS NULL OR lease_id ~ '^lease-[0-9a-f]{32}$'),
    lease_expires_at timestamptz,
    next_attempt_at timestamptz,
    send_intent_at timestamptz,
    last_error_code text CHECK (last_error_code IS NULL OR last_error_code ~ '^[a-z0-9_.:-]{1,80}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (response_id, template_id),
    -- A live state always carries its lease. The last lease identity is kept
    -- after completion so an idempotent result replay can be verified.
    CHECK (state NOT IN ('LEASED', 'SEND_INTENT_RECORDED') OR (lease_id IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK (state <> 'PENDING' OR (lease_id IS NULL AND attempt = 0)),
    CHECK ((state IN ('SEND_INTENT_RECORDED', 'SENT', 'DELIVERY_OUTCOME_UNCERTAIN')) = (send_intent_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS welcome_email_outbox_claim_idx
    ON xb_member_gateway.welcome_email_outbox(state, next_attempt_at, created_at);

CREATE OR REPLACE FUNCTION xb_member_gateway.require_created_verified_for_welcome()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
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

DROP TRIGGER IF EXISTS welcome_email_requires_created_verified ON xb_member_gateway.welcome_email_outbox;
CREATE TRIGGER welcome_email_requires_created_verified
BEFORE INSERT ON xb_member_gateway.welcome_email_outbox
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.require_created_verified_for_welcome();

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_welcome_email_outbox()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'welcome_email_outbox_delete_forbidden';
    END IF;
    IF NEW.outbox_id IS DISTINCT FROM OLD.outbox_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.response_id IS DISTINCT FROM OLD.response_id
       OR NEW.source_response_ref IS DISTINCT FROM OLD.source_response_ref
       OR NEW.template_id IS DISTINCT FROM OLD.template_id
       OR NEW.recipient IS DISTINCT FROM OLD.recipient
       OR NEW.message_hash IS DISTINCT FROM OLD.message_hash
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'welcome_email_identity_immutable';
    END IF;
    IF NEW.state_version <> OLD.state_version + 1 THEN
        RAISE EXCEPTION 'welcome_email_state_version_must_increment';
    END IF;
    IF NOT (
        (OLD.state = 'PENDING' AND NEW.state = 'LEASED')
        OR (OLD.state = 'LEASED' AND NEW.state IN ('RETRY_WAIT', 'DEAD_LETTER', 'SEND_INTENT_RECORDED'))
        OR (OLD.state = 'RETRY_WAIT' AND NEW.state IN ('LEASED', 'DEAD_LETTER'))
        OR (OLD.state = 'SEND_INTENT_RECORDED' AND NEW.state IN ('SENT', 'DELIVERY_OUTCOME_UNCERTAIN'))
    ) THEN
        RAISE EXCEPTION 'welcome_email_transition_forbidden';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS welcome_email_outbox_protected ON xb_member_gateway.welcome_email_outbox;
CREATE TRIGGER welcome_email_outbox_protected
BEFORE UPDATE OR DELETE ON xb_member_gateway.welcome_email_outbox
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_welcome_email_outbox();

CREATE TABLE IF NOT EXISTS xb_member_gateway.welcome_email_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    outbox_id text NOT NULL REFERENCES xb_member_gateway.welcome_email_outbox(outbox_id) ON DELETE RESTRICT,
    event_type text NOT NULL CHECK (event_type IN (
        'created', 'claimed', 'send_intent_recorded', 'sent',
        'delivery_outcome_uncertain', 'retry_scheduled', 'dead_lettered'
    )),
    from_state text,
    to_state text NOT NULL,
    state_version bigint NOT NULL CHECK (state_version >= 0),
    attempt integer NOT NULL CHECK (attempt >= 0),
    error_code text CHECK (error_code IS NULL OR error_code ~ '^[a-z0-9_.:-]{1,80}$'),
    recorded_at timestamptz NOT NULL
);

DROP TRIGGER IF EXISTS welcome_email_events_append_only ON xb_member_gateway.welcome_email_events;
CREATE TRIGGER welcome_email_events_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.welcome_email_events
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_append_only_mutation();

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0005_member_vertical_slice')
ON CONFLICT (version) DO NOTHING;

COMMIT;
