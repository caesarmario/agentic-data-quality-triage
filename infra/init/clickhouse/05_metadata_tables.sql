-- ##############################################
-- SQL Initialization Script for ClickHouse Metadata Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

-- --- Creating dq.metadata_assets Table
CREATE TABLE IF NOT EXISTS dq.metadata_assets
(
    qualified_name         String,
    database_name          LowCardinality(String),
    table_name             String,
    display_name           String,
    description            String,
    dataset                LowCardinality(String),
    domain                 LowCardinality(String),
    data_layer             LowCardinality(String),
    technical_owner        String,
    business_owner         String,
    grain                  String,
    refresh_frequency      LowCardinality(String),
    sla_time               String,
    sla_timezone           LowCardinality(String),
    criticality            LowCardinality(String),
    sensitivity            LowCardinality(String),
    contains_pii           UInt8,
    certification_status   LowCardinality(String),
    lifecycle_status       LowCardinality(String),
    tags                   Array(String),
    source_config_path     String,
    config_sha256          FixedString(64),
    is_active              UInt8 DEFAULT 1,
    version                UInt64,
    synced_at              DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(version)
ORDER BY qualified_name;
