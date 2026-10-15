-- Canonical public incident workspaces; metadata only, never provider/request bodies.
BEGIN;
CREATE SCHEMA IF NOT EXISTS dq_control;
CREATE TABLE IF NOT EXISTS dq_control.discord_incident_workspaces (
    guild_id TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    alert_ref TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    starter_nonce TEXT NOT NULL,
    starter_message_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    starter_attempted BOOLEAN NOT NULL DEFAULT false,
    thread_attempted BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, alert_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_workspace_thread_unique
    ON dq_control.discord_incident_workspaces (guild_id, thread_id) WHERE thread_id <> '';
CREATE TABLE IF NOT EXISTS dq_control.discord_workspace_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN ('missing_404_replacement')),
    generation INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dq_control.discord_investigation_claims (
    guild_id TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'complete', 'uncertain', 'cancelled_before_start')),
    report_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, alert_key, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_investigation_single_active
    ON dq_control.discord_investigation_claims (guild_id, alert_key)
    WHERE status IN ('claimed', 'uncertain');
CREATE INDEX IF NOT EXISTS discord_investigation_lookup
    ON dq_control.discord_investigation_claims (guild_id, alert_key, created_at DESC);
CREATE INDEX IF NOT EXISTS discord_copilot_actor_incident_history
    ON dq_control.discord_copilot_requests (thread_id, actor_id, alert_ref, created_at DESC)
    WHERE status = 'answered';
COMMIT;
