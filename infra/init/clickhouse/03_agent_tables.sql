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


-- --- Creating dq.approval_requests Table
CREATE TABLE IF NOT EXISTS dq.approval_requests
(
    request_id             String,
    created_at             DateTime64(3, 'UTC') DEFAULT now64(3),
    updated_at             DateTime64(3, 'UTC') DEFAULT now64(3),
    alert_id               Nullable(UUID),
    alert_key              String DEFAULT '',
    agent_run_id           Nullable(UUID),
    action_type            LowCardinality(String),
    risk_level             LowCardinality(String),
    status                 LowCardinality(String),
    requested_by           String,
    reason                 String,
    dispatcher_dag_id      String,
    target_dag_id          String,
    start_date             Nullable(Date),
    end_date               Nullable(Date),
    parameters_json        String DEFAULT '{}',
    dry_run                UInt8 DEFAULT 0,
    idempotency_key        String,
    decided_by             String DEFAULT '',
    decided_at             Nullable(DateTime64(3, 'UTC')),
    decision_comment       String DEFAULT '',
    execution_dag_run_id   String DEFAULT '',
    execution_status       LowCardinality(String) DEFAULT 'not_started',
    execution_error        String DEFAULT ''
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY request_id;
