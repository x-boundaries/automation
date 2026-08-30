-- Preserve every result observation while keeping results as the current
-- projection used by the gateway.  A reconciliation update never erases the
-- original uncertain result.

BEGIN;

CREATE TABLE IF NOT EXISTS xb_member_gateway.result_events (
    result_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES xb_member_gateway.jobs(job_id) ON DELETE RESTRICT,
    result_hash text NOT NULL CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN (
        'CREATED_VERIFIED', 'WRITE_OUTCOME_UNCERTAIN',
        'CONFIRMED_NOT_CREATED', 'CREATED_READBACK_MISMATCH'
    )),
    member_no text NOT NULL,
    dispatch_fence_id uuid NOT NULL REFERENCES xb_member_gateway.dispatch_fences(fence_id) ON DELETE RESTRICT,
    save_invocation_count integer NOT NULL CHECK (save_invocation_count = 1),
    readback_found boolean NOT NULL,
    readback_match boolean NOT NULL,
    reconciliation_required boolean NOT NULL,
    error_code text,
    acknowledged_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE xb_member_gateway.results
    ADD CONSTRAINT results_save_invocation_once CHECK (save_invocation_count = 1);

INSERT INTO xb_member_gateway.schema_migrations(version)
VALUES ('0002_result_event_history')
ON CONFLICT (version) DO NOTHING;

COMMIT;
