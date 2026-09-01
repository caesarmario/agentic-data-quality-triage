-- ##############################################
-- SQL Initialization Script for ClickHouse Schema Drift Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

-- --- Creating dq.schema_snapshots Table
CREATE TABLE IF NOT EXISTS dq.schema_snapshots
(
    snapshot_id          UUID,
    run_id               String,
    observed_at          DateTime64(3, 'UTC'),
    contract_name        LowCardinality(String),
    contract_version     UInt32,
    contract_sha256      FixedString(64),
    qualified_name       String,
    database_name        LowCardinality(String),
    table_name           String,
    schema_sha256        FixedString(64),
    status               LowCardinality(String),
    highest_severity     LowCardinality(String),
    comparison_count     UInt32,
    finding_count        UInt32,
    columns_json         String,
    created_at           DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(observed_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (run_id, qualified_name);

-- --- Creating dq.schema_drift_results Table
CREATE TABLE IF NOT EXISTS dq.schema_drift_results
(
    result_id            UUID,
    snapshot_id          UUID,
    run_id               String,
    observed_at          DateTime64(3, 'UTC'),
    contract_name        LowCardinality(String),
    contract_version     UInt32,
    contract_sha256      FixedString(64),
    qualified_name       String,
    column_name          String,
    check_type           LowCardinality(String),
    status               LowCardinality(String),
    severity             LowCardinality(String),
    expected_value       String,
    actual_value         String,
    details_json         String DEFAULT '{}',
    created_at           DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(observed_at)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (run_id, qualified_name, check_type, column_name);
