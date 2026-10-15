-- Idempotent, non-destructive incident control and evidence-gated closure.
BEGIN;
CREATE SCHEMA IF NOT EXISTS dq_control;
CREATE TABLE IF NOT EXISTS dq_control.incident_controls (
    alert_key TEXT PRIMARY KEY,
    alert_ref TEXT NOT NULL UNIQUE,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    episode BIGINT NOT NULL DEFAULT 1 CHECK (episode > 0),
    status TEXT NOT NULL CHECK (status IN ('open','acknowledged','triaged','resolved')),
    snapshot JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dq_control.incident_revalidations (
    request_id UUID PRIMARY KEY,
    alert_key TEXT NOT NULL REFERENCES dq_control.incident_controls(alert_key),
    incident_revision BIGINT NOT NULL,
    idempotency_hash CHAR(64) NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued','running','pass','blocked','error','expired')),
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incident_revalidation_lookup
    ON dq_control.incident_revalidations(alert_key, created_at DESC);
CREATE TABLE IF NOT EXISTS dq_control.incident_revalidation_keys (
    idempotency_hash CHAR(64) PRIMARY KEY,
    request_id UUID NOT NULL REFERENCES dq_control.incident_revalidations(request_id),
    actor TEXT NOT NULL
);
INSERT INTO dq_control.incident_revalidation_keys(idempotency_hash,request_id,actor)
SELECT idempotency_hash,request_id,COALESCE(record->>'actor','system')
FROM dq_control.incident_revalidations ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS dq_control.incident_resolution_decisions (
    idempotency_hash CHAR(64) PRIMARY KEY,
    alert_key TEXT NOT NULL REFERENCES dq_control.incident_controls(alert_key),
    request_id UUID NOT NULL REFERENCES dq_control.incident_revalidations(request_id),
    record JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dq_control.incident_outbox (
    event_id UUID PRIMARY KEY,
    alert_key TEXT NOT NULL REFERENCES dq_control.incident_controls(alert_key),
    revision BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    projection_done BOOLEAN NOT NULL DEFAULT false,
    audit_done BOOLEAN NOT NULL DEFAULT false,
    delivery_done BOOLEAN NOT NULL DEFAULT false,
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(alert_key, revision, event_type)
);
COMMIT;
