-- Durable Google Forms cutover cursor and append-only page evidence.
-- This migration deliberately does not initialize a production watermark.

BEGIN;

CREATE TABLE IF NOT EXISTS xb_member_gateway.source_ingest_cursors (
    source_system text NOT NULL,
    form_alias text NOT NULL,
    mapping_version text NOT NULL,
    watermark timestamptz NOT NULL,
    last_admitted_create_time timestamptz,
    last_admitted_response_id text,
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    scan_lower_bound timestamptz NOT NULL,
    resume_page_token text,
    initial_window_admission_count integer NOT NULL DEFAULT 0
        CHECK (initial_window_admission_count BETWEEN 0 AND 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, form_alias, mapping_version),
    CHECK ((last_admitted_create_time IS NULL) = (last_admitted_response_id IS NULL)),
    CHECK (last_admitted_create_time IS NULL OR last_admitted_create_time >= watermark),
    CHECK (scan_lower_bound >= watermark),
    CHECK (resume_page_token IS NULL OR (
        length(resume_page_token) BETWEEN 1 AND 1024
        AND resume_page_token ~ '^[ -~]+$'
    ))
);

CREATE TABLE IF NOT EXISTS xb_member_gateway.source_ingest_page_receipts (
    page_receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_system text NOT NULL,
    form_alias text NOT NULL,
    mapping_version text NOT NULL,
    expected_state_version bigint NOT NULL CHECK (expected_state_version >= 0),
    committed_state_version bigint NOT NULL CHECK (committed_state_version = expected_state_version + 1),
    current_page_token text,
    next_page_token text,
    scan_lower_bound timestamptz NOT NULL,
    terminal boolean NOT NULL,
    responses jsonb NOT NULL CHECK (jsonb_typeof(responses) = 'array'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_system, form_alias, mapping_version)
        REFERENCES xb_member_gateway.source_ingest_cursors(source_system, form_alias, mapping_version)
        ON DELETE RESTRICT,
    CHECK (current_page_token IS NULL OR (length(current_page_token) BETWEEN 1 AND 1024 AND current_page_token ~ '^[ -~]+$')),
    CHECK (next_page_token IS NULL OR (length(next_page_token) BETWEEN 1 AND 1024 AND next_page_token ~ '^[ -~]+$')),
    CHECK (terminal = (next_page_token IS NULL)),
    CHECK (next_page_token IS NULL OR next_page_token IS DISTINCT FROM current_page_token)
);

CREATE OR REPLACE FUNCTION xb_member_gateway.protect_source_ingest_cursor_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source_ingest_cursor_delete_forbidden';
    END IF;
    IF NEW.source_system IS DISTINCT FROM OLD.source_system
       OR NEW.form_alias IS DISTINCT FROM OLD.form_alias
       OR NEW.mapping_version IS DISTINCT FROM OLD.mapping_version
       OR NEW.watermark IS DISTINCT FROM OLD.watermark THEN
        RAISE EXCEPTION 'source_ingest_cursor_identity_immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_ingest_cursor_identity_immutable
    ON xb_member_gateway.source_ingest_cursors;
CREATE TRIGGER source_ingest_cursor_identity_immutable
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_ingest_cursors
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.protect_source_ingest_cursor_identity();

CREATE UNIQUE INDEX IF NOT EXISTS source_ingest_page_next_token_once_idx
    ON xb_member_gateway.source_ingest_page_receipts(source_system, form_alias, mapping_version, next_page_token)
    WHERE next_page_token IS NOT NULL;

CREATE OR REPLACE FUNCTION xb_member_gateway.reject_source_page_receipt_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'source_ingest_page_receipts_append_only';
END;
$$;

DROP TRIGGER IF EXISTS source_ingest_page_receipts_append_only
    ON xb_member_gateway.source_ingest_page_receipts;
CREATE TRIGGER source_ingest_page_receipts_append_only
BEFORE UPDATE OR DELETE ON xb_member_gateway.source_ingest_page_receipts
FOR EACH ROW EXECUTE FUNCTION xb_member_gateway.reject_source_page_receipt_mutation();

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0004_forms_ingest_cursor')
ON CONFLICT (version) DO NOTHING;

COMMIT;
