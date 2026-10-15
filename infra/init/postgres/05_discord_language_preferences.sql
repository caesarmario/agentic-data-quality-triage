BEGIN;
CREATE SCHEMA IF NOT EXISTS dq_control;
CREATE TABLE IF NOT EXISTS dq_control.discord_user_preferences (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'id')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);
COMMIT;
