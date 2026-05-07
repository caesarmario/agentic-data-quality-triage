-- ##############################################
-- SQL Initialization Script for ClickHouse Agent Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

-- --- Creating dq.agent_audit_log Table
CREATE TABLE IF NOT EXISTS dq.agent_audit_log
(
    audit_id         UUID DEFAULT generateUUIDv4(),
    ts               DateTime64(3, 'UTC') DEFAULT now64(3),
    alert_id         Nullable(UUID),
    alert_key        String DEFAULT '',
    agent_run_id     UUID DEFAULT generateUUIDv4(),
    actor            LowCardinality(String) DEFAULT 'agent',
    action           LowCardinality(String),
    tool_name        LowCardinality(String) DEFAULT '',
    status           LowCardinality(String),
    duration_ms      Nullable(UInt64),
    input_json       String DEFAULT '{}',
    output_json      String DEFAULT '{}',
    error_message    String DEFAULT '',
    sql_hash         String DEFAULT '',
    row_count        Nullable(UInt64),
    report_s3_uri    String DEFAULT ''
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, alert_key, agent_run_id, tool_name);
