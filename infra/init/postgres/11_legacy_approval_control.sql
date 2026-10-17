-- Legacy approval cutover support. Apply only through the owner-controlled migration path.
-- Migration 09 owns the recover_partition action CHECK; this migration grants no execution rights.
BEGIN;

CREATE TABLE IF NOT EXISTS dq_control.approval_legacy_imports (
    request_id TEXT PRIMARY KEY REFERENCES dq_control.approval_requests(request_id),
    source_hash CHAR(64) NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    history_json JSONB NOT NULL CHECK (jsonb_typeof(history_json) = 'array'),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dq_control.approval_projection_outbox (
    request_id TEXT NOT NULL REFERENCES dq_control.approval_requests(request_id),
    version INTEGER NOT NULL CHECK (version > 0),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    audit_done BOOLEAN NOT NULL DEFAULT false,
    projection_done BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, version)
);

CREATE INDEX IF NOT EXISTS approval_projection_pending_idx
    ON dq_control.approval_projection_outbox (request_id, version)
    WHERE NOT (audit_done AND projection_done);

-- Capture direct recovery-service writes as well as store-mediated mutations.
CREATE OR REPLACE FUNCTION dq_control.enqueue_approval_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    event_name TEXT;
    actor_name TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        event_name := 'approval_requested';
        actor_name := NEW.requested_by;
    ELSE
        IF (NEW.request_id, NEW.idempotency_hash, NEW.request_generation, NEW.action_type,
            NEW.scope_json, NEW.requested_by, NEW.reason, NEW.created_at)
           IS DISTINCT FROM
           (OLD.request_id, OLD.idempotency_hash, OLD.request_generation, OLD.action_type,
            OLD.scope_json, OLD.requested_by, OLD.reason, OLD.created_at) THEN
            RAISE EXCEPTION 'Immutable approval scope cannot be changed';
        END IF;
        IF NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'Approval mutation requires exactly one version increment';
        END IF;
        IF NEW.status = 'cancelled' AND OLD.status <> NEW.status THEN
            event_name := 'approval_cancelled';
            actor_name := NEW.cancelled_by;
        ELSIF NEW.status <> OLD.status THEN
            event_name := 'approval_decision';
            actor_name := NEW.decided_by;
        ELSE
            event_name := 'approval_execution_transition';
            actor_name := 'airflow';
        END IF;
    END IF;
    IF NEW.action_type = 'recover_partition' AND NEW.requested_by <> 'datasentry_recovery' THEN
        RAISE EXCEPTION 'Recovery approvals require the server-system requester';
    END IF;
    INSERT INTO dq_control.approval_projection_outbox(request_id, version, payload_json)
    VALUES (NEW.request_id, NEW.version,
            jsonb_build_object('record', to_jsonb(NEW), 'event', event_name, 'actor', coalesce(actor_name, 'system')))
    ON CONFLICT (request_id, version) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS approval_projection_intent ON dq_control.approval_requests;
CREATE TRIGGER approval_projection_intent AFTER INSERT OR UPDATE ON dq_control.approval_requests
    FOR EACH ROW EXECUTE FUNCTION dq_control.enqueue_approval_projection();

COMMENT ON TABLE dq_control.approval_legacy_imports IS
    'Immutable receipt and all available CH snapshots; never a source of new recovery execution rights.';
COMMENT ON TABLE dq_control.approval_projection_outbox IS
    'Transactional pending audit/projection intent; ClickHouse is an at-least-once projection, not approval authority.';

COMMIT;
