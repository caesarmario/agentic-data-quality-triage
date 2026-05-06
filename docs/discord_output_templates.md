<!--
####
## Discord Output Templates for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####
-->

# Discord Output Templates

This document defines readable Discord message templates for the optional DQ triage bot. The goal is to keep bot responses easy to scan for business users, data engineers, and reviewers watching the demo.

## Formatting Principles

- Use short headings with Discord Markdown: `#`, `##`, and `###`.
- Use emoji as visual anchors, not decoration overload.
- Keep one message focused on one purpose.
- Put the most important status at the top.
- Use fixed labels for operational fields: `Date`, `Table`, `Metric`, `Observed`, `Expected`, `Confidence`.
- Use bullets for impact, evidence, and next steps.
- End every bot output with `### ----------------------------------------` so adjacent Discord messages are easier to separate visually.
- Keep approval-gated actions explicit and never hide their side effects.

## Emoji Map

- Critical alert: `🚨`
- Warning alert: `⚠️`
- Healthy/pass: `✅`
- Investigation/triage: `🧭`
- Evidence: `🔎`
- Impact: `📉`
- Action: `🛠️`
- Approval required: `🧾`
- Report/artifact: `📄`
- Backfill: `🔁`
- Daily summary: `📊`

## Alert Summary Template

```text
# 🚨 DQ Alert: Critical
## Missing Orders Data

**Date**      : 2026-05-04
**Table**     : dq.raw_orders
**Metric**    : row_count_positive
**Observed**  : 0
**Expected**  : >= 1
**Status**    : Open
**Alert Key** : orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table

### What Happened
No rows were found for the expected orders partition.

### Likely Impact
- Downstream staging and mart tables may be incomplete.
- Daily revenue/order metrics for this date may be missing.
- Dashboards using fct_orders_daily may show zero or stale values.

### Suggested Next Step
Run triage to confirm whether this is a missing landing file, failed load, or upstream generation issue.

### Commands
`!dq triage <alert_id>`
`!dq history orders 2026-05-04`
`!dq backfill-preview 2026-05-04 2026-05-04`

### ----------------------------------------
```

## Triage Result Template

```text
# 🧭 Triage Result
## Missing Latest Day

**Alert**      : row_count_positive
**Date**       : 2026-05-04
**Severity**   : Critical
**Confidence** : 0.87

### Most Likely Root Cause
The raw orders partition for 2026-05-04 was not loaded into ClickHouse.

### Evidence
1. dq.raw_orders row count = 0 for dt=2026-05-04
2. dq.stg_orders row count = 0 for dt=2026-05-04
3. dq.fct_orders_daily row count = 0 for dt=2026-05-04
4. Previous available partition is 2026-05-03

### Recommended Action
Backfill 2026-05-04 through the daily pipeline.

### 🧾 Approval Required
This action will trigger the Airflow backfill dispatcher.

**Approve**
`!dq approve backfill <request_id>`

### 📄 Report
s3://dq-artifacts/agent-reports/...

### ----------------------------------------
```

## Daily Summary Template

```text
# 📊 DQ Daily Summary
## 2026-05-04

### Checks
✅ Passed  : 12
⚠️ Warning : 1
🚨 Failed  : 4
⏭️ Skipped : 1

### Alerts
🚨 Open Critical : 4
⚠️ Open Warning  : 1

### Top Issues
1. dq.raw_orders row_count_positive failed
2. dq.stg_orders freshness failed
3. dq.fct_orders_daily segment coverage warning

### Next Commands
`!dq alerts 2026-05-04`
`!dq triage latest`

### ----------------------------------------
```

## Backfill Recommendation Template

```text
# 🔁 Backfill Recommendation
## Approval Required

**Reason**       : Missing latest orders partition
**Target DAG**   : 99_dag_dq_platform_daily_orchestrator
**Start Date**   : 2026-05-04
**End Date**     : 2026-05-04
**Requested By** : agent

### What Will Run
- Seed/generate landing data: true
- Load ClickHouse raw partition: true
- Run dbt transform/test: true
- Run profiling and DQ checks: true
- Run triage after backfill: false

### Approval Command
`!dq approve backfill <request_id>`

### Cancel Command
`!dq cancel <request_id>`

### ----------------------------------------
```

## Approval Action Preview Template

```text
# 🧾 Approval Action Preview
## Backfill Dispatcher

**Request ID** : bf_20260504_orders_001
**Action**     : Trigger Airflow backfill dispatcher
**Target DAG** : 99_dag_dq_platform_daily_orchestrator
**Date Range** : 2026-05-04 to 2026-05-04
**Risk Level** : Low

### Safety Notes
- The daily DAG is idempotent by dt.
- raw_orders partition will be replaced for the requested date.
- No action will run until approval is submitted.

### Approve
`!dq approve backfill bf_20260504_orders_001`

### Reject
`!dq reject bf_20260504_orders_001`

### ----------------------------------------
```
