-- Durable claims prevent replaying paid calls after reconnect or interruption.
BEGIN;
CREATE SCHEMA IF NOT EXISTS dq_control;
CREATE TABLE IF NOT EXISTS dq_control.discord_copilot_requests (
    source_message_id TEXT PRIMARY KEY,
    root_message_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    answer_message_id TEXT NOT NULL DEFAULT '',
    alert_ref TEXT NOT NULL DEFAULT '',
    agent_run_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('claimed', 'answered', 'failed')),
    error_type TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS discord_copilot_thread_lookup
    ON dq_control.discord_copilot_requests (thread_id, created_at DESC);
ALTER TABLE dq_control.discord_copilot_requests
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'message'
    CHECK (source_kind IN ('message', 'modal'));
COMMIT;
