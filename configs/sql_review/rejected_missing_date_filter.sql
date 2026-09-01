-- ##############################################
-- Rejected SQL Review Fixture for Agentic Data Quality Triage
-- Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-- ##############################################

-- --- Selecting A Large Table Without The Required Date Filter

SELECT
    country,
    count() AS order_count
FROM dq.raw_orders
GROUP BY country
LIMIT 10

