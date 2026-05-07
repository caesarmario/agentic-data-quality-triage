-- ##############################################
-- SQL Initialization Script for ClickHouse Orders Tables
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Defining SQL Objects

-- --- Creating dq.raw_orders Table
CREATE TABLE IF NOT EXISTS dq.raw_orders
(
    dt                         Date,
    order_id                   String,
    order_date                 Date,
    order_ts                   DateTime64(3, 'UTC'),
    ingestion_ts               DateTime64(3, 'UTC'),
    customer_id                String,
    country                    LowCardinality(String),
    channel                    LowCardinality(String),
    status                     LowCardinality(String),
    currency                   LowCardinality(String),
    gross_amount_local         Decimal(18, 2),
    fx_rate_to_usd             Decimal(18, 6),
    gross_amount_usd           Decimal(18, 2),
    discount_usd               Decimal(18, 2),
    refund_amount_usd          Decimal(18, 2),
    recognized_revenue_usd     Decimal(18, 2),
    source_system              LowCardinality(String),
    is_test                    UInt8 DEFAULT 0,
    business_date_version      UInt32 DEFAULT 1,
    incident_scenario          String DEFAULT '',
    generated_at               DateTime64(3, 'UTC') DEFAULT now64(3),
    loaded_at                  DateTime64(3, 'UTC') DEFAULT now64(3),
    load_id                    UUID DEFAULT generateUUIDv4()
)
ENGINE = MergeTree
PARTITION BY dt
ORDER BY (dt, country, channel, order_id, order_ts)
SETTINGS index_granularity = 8192;

-- --- Creating dq.stg_orders Table
CREATE TABLE IF NOT EXISTS dq.stg_orders
(
    dt                         Date,
    order_id                   String,
    order_date                 Date,
    order_ts                   DateTime64(3, 'UTC'),
    ingestion_ts               DateTime64(3, 'UTC'),
    customer_id                String,
    country                    LowCardinality(String),
    channel                    LowCardinality(String),
    status                     LowCardinality(String),
    currency                   LowCardinality(String),
    gross_amount_local         Decimal(18, 2),
    fx_rate_to_usd             Decimal(18, 6),
    gross_amount_usd           Decimal(18, 2),
    discount_usd               Decimal(18, 2),
    refund_amount_usd          Decimal(18, 2),
    recognized_revenue_usd     Decimal(18, 2),
    net_amount_usd             Decimal(18, 2),
    source_system              LowCardinality(String),
    is_test                    UInt8,
    business_date_version      UInt32,
    incident_scenario          String,
    transformed_at             DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY dt
ORDER BY (dt, country, channel, order_id);

-- --- Creating dq.fct_orders_daily Table
CREATE TABLE IF NOT EXISTS dq.fct_orders_daily
(
    dt                         Date,
    country                    LowCardinality(String),
    channel                    LowCardinality(String),
    row_count                  UInt64,
    order_count                UInt64,
    distinct_order_count       UInt64,
    paid_order_count           UInt64,
    cancelled_order_count      UInt64,
    refunded_order_count       UInt64,
    pending_order_count        UInt64,
    gross_amount_usd           Decimal(18, 2),
    discount_usd               Decimal(18, 2),
    refund_amount_usd          Decimal(18, 2),
    recognized_revenue_usd     Decimal(18, 2),
    aov_usd                    Decimal(18, 2),
    duplicate_order_count      UInt64 DEFAULT 0,
    late_arriving_count        UInt64 DEFAULT 0,
    updated_at                 DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY dt
ORDER BY (dt, country, channel);
