-- Immutable recovery plans and transactional execution ownership. No data reset.
BEGIN;
ALTER TABLE dq_control.approval_requests
    DROP CONSTRAINT IF EXISTS approval_requests_action_type_check;
ALTER TABLE dq_control.approval_requests
    ADD CONSTRAINT approval_requests_action_type_check
    CHECK (action_type IN ('backfill','rerun_dbt','create_ticket','post_notification','recover_partition'));

CREATE TABLE IF NOT EXISTS dq_control.source_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL CHECK (dataset = 'orders'),
    dt DATE NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('upstream','landing')),
    checksum CHAR(64) NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS source_snapshot_scope_idx
    ON dq_control.source_snapshots(dataset, dt, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS dq_control.recovery_plans (
    plan_id UUID PRIMARY KEY,
    alert_key TEXT NOT NULL REFERENCES dq_control.incident_controls(alert_key),
    incident_revision BIGINT NOT NULL CHECK (incident_revision > 0),
    plan_hash CHAR(64) NOT NULL UNIQUE,
    approval_request_id TEXT NOT NULL UNIQUE REFERENCES dq_control.approval_requests(request_id),
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dq_control.recovery_executions (
    execution_id UUID PRIMARY KEY,
    plan_id UUID NOT NULL UNIQUE REFERENCES dq_control.recovery_plans(plan_id),
    status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','unknown')),
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dq_control.recovery_outbox (
    event_id UUID PRIMARY KEY,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    audit_done BOOLEAN NOT NULL DEFAULT false,
    delivery_done BOOLEAN NOT NULL DEFAULT false,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (aggregate_id, event_type)
);
ALTER TABLE dq_control.recovery_outbox ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE dq_control.recovery_outbox ADD COLUMN IF NOT EXISTS message_id TEXT NOT NULL DEFAULT '';
ALTER TABLE dq_control.recovery_outbox ADD COLUMN IF NOT EXISTS author_id TEXT NOT NULL DEFAULT '';
COMMIT;
