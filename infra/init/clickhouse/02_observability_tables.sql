-- ##############################################
-- SQL Initialization Script for ClickHouse Observability Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

CREATE TABLE IF NOT EXISTS dq.data_profile_results
(
    profile_run_id     UUID DEFAULT generateUUIDv4(),
    run_at             DateTime64(3, 'UTC') DEFAULT now64(3),
    dt                 Nullable(Date),
    table_name         LowCardinality(String),
    column_name        String,
    metric_name        LowCardinality(String),
    metric_value       Float64,
    metric_unit        LowCardinality(String) DEFAULT '',
    details_json       String DEFAULT '{}',
    created_at         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_at)
ORDER BY (table_name, dt, column_name, metric_name, run_at);

CREATE TABLE IF NOT EXISTS dq.dq_check_results
(
    check_run_id       UUID DEFAULT generateUUIDv4(),
    run_at             DateTime64(3, 'UTC') DEFAULT now64(3),
    dt                 Nullable(Date),
    table_name         LowCardinality(String),
    check_name         String,
    check_type         LowCardinality(String),
    status             LowCardinality(String),
    severity           LowCardinality(String),
    observed_value     Nullable(Float64),
    expected_value     Nullable(Float64),
    threshold_value    Nullable(Float64),
    details_json       String DEFAULT '{}',
    evidence_s3_uri    String DEFAULT '',
    created_at         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_at)
ORDER BY (table_name, dt, status, check_name, run_at);

CREATE TABLE IF NOT EXISTS dq.alerts
(
    alert_id              UUID DEFAULT generateUUIDv4(),
    alert_key             String,
    created_at            DateTime64(3, 'UTC') DEFAULT now64(3),
    updated_at            DateTime64(3, 'UTC') DEFAULT now64(3),
    status                LowCardinality(String) DEFAULT 'open',
    alert_type            LowCardinality(String),
    severity              LowCardinality(String),
    table_name            LowCardinality(String),
    metric                String,
    dt                    Nullable(Date),
    dimension             String DEFAULT '',
    observed_value        Nullable(Float64),
    expected_value        Nullable(Float64),
    threshold_value       Nullable(Float64),
    source_check_run_id   Nullable(UUID),
    details_json          String DEFAULT '{}',
    report_s3_uri         String DEFAULT '',
    acknowledged_by       String DEFAULT '',
    resolved_at           Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (alert_key, status, severity, created_at);

CREATE TABLE IF NOT EXISTS dq.pipeline_runs
(
    run_id             UUID DEFAULT generateUUIDv4(),
    job_name           LowCardinality(String),
    dag_id             String DEFAULT '',
    task_id            String DEFAULT '',
    logical_date       Nullable(Date),
    partition_dt       Nullable(Date),
    status             LowCardinality(String),
    started_at         DateTime64(3, 'UTC'),
    ended_at           Nullable(DateTime64(3, 'UTC')),
    duration_ms        Nullable(UInt64),
    rows_read          Nullable(UInt64),
    rows_written       Nullable(UInt64),
    source_uri         String DEFAULT '',
    target_table       String DEFAULT '',
    error_message      String DEFAULT '',
    metadata_json      String DEFAULT '{}',
    created_at         DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (job_name, partition_dt, run_id);
