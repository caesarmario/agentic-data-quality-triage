-- Durable, metadata-only identity for bot-owned service messages.
BEGIN;
CREATE SCHEMA IF NOT EXISTS dq_control;
CREATE TABLE IF NOT EXISTS dq_control.discord_service_messages (
    guild_id BIGINT NOT NULL CHECK (guild_id > 0),
    channel_id BIGINT NOT NULL CHECK (channel_id > 0),
    kind TEXT NOT NULL CHECK (kind = 'welcome'),
    message_id TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    creation_pending BOOLEAN NOT NULL DEFAULT false,
    edit_pending BOOLEAN NOT NULL DEFAULT false,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    nonce TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id, kind)
);
COMMIT;
