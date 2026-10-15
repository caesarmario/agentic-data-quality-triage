-- ####
-- ## Discord Bot Delivery Registry Migration
-- ## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ####

-- Rows written before this migration used the retired webhook transport. New
-- bot-authored revisions replace them through ReplacingMergeTree semantics.
ALTER TABLE dq.discord_incident_messages
    ADD COLUMN IF NOT EXISTS transport LowCardinality(String) DEFAULT 'legacy_webhook'
    AFTER narrative_source;

ALTER TABLE dq.discord_incident_messages
    ADD COLUMN IF NOT EXISTS author_id String DEFAULT ''
    AFTER transport;
