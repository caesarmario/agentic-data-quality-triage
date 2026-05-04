-- ##############################################
-- SQL Initialization Script for ClickHouse Views
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

CREATE VIEW IF NOT EXISTS dq.open_alerts AS
SELECT *
FROM dq.alerts
WHERE status IN ('open', 'triaged')
ORDER BY created_at DESC;

-- Idempotent load convention:
-- 1. Generate deterministic Parquet files per dt under s3://dq-landing/orders/dt=YYYY-MM-DD/.
-- 2. Before loading a dt, run: ALTER TABLE dq.raw_orders DROP PARTITION 'YYYY-MM-DD'.
-- 3. Insert the full replacement partition from the landing Parquet file.
-- 4. Transform layers should use the same partition-replace pattern for dt-scoped reruns.
