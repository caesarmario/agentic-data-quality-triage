-- ####
-- ## Discord Incident Message Registry for Agentic Data Quality Triage
-- ## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ####

-- --- Creating dq.discord_incident_messages Table
CREATE TABLE IF NOT EXISTS dq.discord_incident_messages
(
    alert_key              String,
    alert_display_id       String,
    channel_id             String,
    message_id             String,
    rendered_lifecycle     LowCardinality(String),
    content_hash           FixedString(64),
    narrative_source       LowCardinality(String),
    transport              LowCardinality(String) DEFAULT 'legacy_webhook',
    author_id              String DEFAULT '',
    last_http_status       Nullable(UInt16),
    last_error_state       String DEFAULT '',
    created_at             DateTime64(3, 'UTC'),
    updated_at             DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY alert_key;
