-- ##############################################
-- SQL Initialization Script for Agent Context Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

-- --- Creating Temporary Run Context Events
CREATE TABLE IF NOT EXISTS dq.agent_run_context_events
(
    context_event_id        UUID,
    parent_run_id           UUID,
    external_run_id         String,
    event_sequence          UInt8,
    phase                   LowCardinality(String),
    occurred_at             DateTime64(3, 'UTC'),
    expires_at              DateTime('UTC'),
    requester               LowCardinality(String),
    status                  LowCardinality(String),
    selected_specialist     LowCardinality(String) DEFAULT '',
    task_type               LowCardinality(String) DEFAULT '',
    task_id                 Nullable(UUID),
    alert_id                Nullable(UUID),
    alert_key               String DEFAULT '',
    alert_display_id        String DEFAULT '',
    context_references_json String DEFAULT '[]',
    evidence_references_json String DEFAULT '[]',
    decision_json           String DEFAULT '{}',
    report_s3_uri           String DEFAULT '',
    approval_state          LowCardinality(String) DEFAULT 'not_required',
    content_sha256          FixedString(64)
)
ENGINE = ReplacingMergeTree(occurred_at)
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (parent_run_id, context_event_id)
TTL expires_at DELETE;


-- --- Creating Durable Incident Memory
CREATE TABLE IF NOT EXISTS dq.incident_memory
(
    memory_id                UUID,
    memory_key               FixedString(64),
    parent_run_id            UUID,
    recorded_at              DateTime64(3, 'UTC'),
    memory_type              LowCardinality(String),
    alert_id                 Nullable(UUID),
    alert_key                String DEFAULT '',
    alert_display_id         String DEFAULT '',
    outcome_status           LowCardinality(String),
    specialist_name          LowCardinality(String) DEFAULT '',
    task_type                LowCardinality(String) DEFAULT '',
    summary                  String DEFAULT '',
    evidence_references_json String DEFAULT '[]',
    decision_json            String DEFAULT '{}',
    report_s3_uri            String DEFAULT '',
    approval_state           LowCardinality(String) DEFAULT 'not_required',
    resolution_reference     String DEFAULT '',
    content_sha256           FixedString(64)
)
ENGINE = ReplacingMergeTree(recorded_at)
PARTITION BY toYYYYMM(recorded_at)
ORDER BY (alert_key, memory_id);
