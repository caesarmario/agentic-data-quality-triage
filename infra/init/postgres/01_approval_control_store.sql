-- ##############################################
-- Transactional approval control store migration
-- Applied by the idempotent approval-control-init Docker Compose service.
-- ##############################################

-- --- Defining Approval Control Objects
BEGIN;

CREATE SCHEMA IF NOT EXISTS dq_control;

CREATE TABLE IF NOT EXISTS dq_control.approval_requests (
    request_id TEXT PRIMARY KEY,
    idempotency_hash CHAR(64) NOT NULL UNIQUE,
    request_generation INTEGER NOT NULL DEFAULT 1,
    action_type TEXT NOT NULL
        CHECK (action_type IN ('backfill', 'rerun_dbt', 'create_ticket', 'post_notification')),
    scope_json JSONB NOT NULL CHECK (jsonb_typeof(scope_json) = 'object'),
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 200),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 5 AND 2000),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision_comment TEXT NOT NULL DEFAULT '',
    cancelled_by TEXT,
    cancelled_at TIMESTAMPTZ,
    execution_run_id TEXT,
    execution_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (execution_status IN ('not_started', 'dispatching', 'dispatched', 'succeeded', 'failed', 'unknown')),
    execution_error TEXT NOT NULL DEFAULT '',
    CONSTRAINT approval_hash_format CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT approval_decision_fields CHECK (
        (status = 'pending' AND decided_at IS NULL)
        OR (status IN ('approved', 'rejected') AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
        OR (status = 'cancelled' AND (decided_at IS NULL OR decided_by IS NOT NULL))
    ),
    CONSTRAINT approval_claim_fields CHECK (
        (execution_status = 'not_started' AND execution_run_id IS NULL)
        OR (status = 'approved' AND execution_run_id IS NOT NULL)
    )
);

ALTER TABLE dq_control.approval_requests
    ADD COLUMN IF NOT EXISTS request_generation INTEGER;

UPDATE dq_control.approval_requests
SET request_generation = 1
WHERE request_generation IS NULL;

ALTER TABLE dq_control.approval_requests
    ALTER COLUMN request_generation SET DEFAULT 1,
    ALTER COLUMN request_generation SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'approval_request_generation_positive'
          AND conrelid = 'dq_control.approval_requests'::regclass
    ) THEN
        ALTER TABLE dq_control.approval_requests
            ADD CONSTRAINT approval_request_generation_positive
            CHECK (request_generation BETWEEN 1 AND 1000000);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS approval_requests_status_idx
    ON dq_control.approval_requests (status, created_at DESC);

COMMIT;
