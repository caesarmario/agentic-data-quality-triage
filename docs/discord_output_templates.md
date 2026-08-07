####
## Discord Output Templates for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Discord Output Templates

This document defines the operator-facing message structure used by the optional Discord bot. The goal is a readable data reliability copilot, not a raw technical dump.


## Message Anatomy

Use these sections when they apply:

1. Human-readable headline.
2. Quick Read or Day Status.
3. Key Facts or Check Results.
4. Impact or Alert Risk.
5. Evidence or Copilot Analysis.
6. Recommended Next Step.
7. Commands or approval status.
8. Technical Reference.

`Alert Ref` is the primary operator identifier. The long `System Alert Key` belongs only in `Technical Reference` for debugging, joins, and idempotency.


## Formatting Principles

- Use Discord Markdown headings and emoji as scan anchors.
- Put severity, confidence, and the safest next action near the top.
- Split long responses into bounded Discord messages without dropping report sections.
- State clearly whether an action is recommended, approval-gated, stored, or executed.
- Never imply that preview or approval commands triggered Airflow.
- Keep transport and audit correlation under `Technical Reference`.
- End complete messages with `### ----------------------------------------`.


## Emoji Map

- Critical alert: `🚨`
- Warning alert: `⚠️`
- Healthy or resolved: `✅`
- Investigation or triage: `🧭`
- Approval: `🧾`
- Report or artifact: `📄`
- Backfill: `🔁`
- Daily summary: `📊`
- Copilot: `🤖`


## Slash Command Syntax

Discord exposes typed slash command options:

```text
/alerts dt:<YYYY-MM-DD> status:open limit:10
/daily_summary dt:<YYYY-MM-DD>
/triage alert_key:<Alert Ref or system key>
/ask question:<question> alert_key:<optional Alert Ref>
/backfill_preview start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD> target_dag_id:<DAG ID> reason:<reason>
/approve request_id:<APR ID> comment:<review note>
/reject request_id:<APR ID> comment:<review note>
```

Guild-scoped command registration is used for the local demo so command changes synchronize quickly.


## Alert Summary Template

```text
# 🚨 Critical Data Quality Alert
## Raw Orders Data has missing or unusually low row count

### Quick Read
Raw Orders Data has no rows for 2026-05-04.
Triage this alert before trusting downstream data.

### Key Facts
**Alert Ref** `DQ-20260504-A1B2C3`
**Date** `2026-05-04`
**Affected Table** `dq.raw_orders`
**Check** `row_count_positive`
**Observed / Expected** `0 / 1`
**Status** `Open`

### Recommended Next Step
/triage alert_key:DQ-20260504-A1B2C3

### Technical Reference
Alert Data Transport api
**System Alert Key** `orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table`

### ----------------------------------------
```


## Triage Result Template

```text
# 🧭 Triage Result
## Raw Orders Data has missing or unusually low row count

### Quick Read
The leading explanation is Missing or empty ClickHouse partition.
Confidence is 0.88: Good evidence with some uncertainty.

### Why I Think So
1. Current partition row count is zero.
2. DQ history shows the target check failed.
3. No successful load run was found for the affected date.

### Recommended Next Step
Review the landing artifact and prepare an approval-gated backfill.

### 🧾 Approval Status
Approval required. This recommendation has not been executed.

### 📄 Report Links
**Markdown** `s3://dq-artifacts/agent-reports/.../report.md`
**JSON** `s3://dq-artifacts/agent-reports/.../report.json`

### Technical Reference
**Alert Ref** `DQ-20260504-A1B2C3`
**Report ID** `RPT-ABC123EF`
Triage Transport api
Narrative Transport api

### ----------------------------------------
```


## Daily Summary Template

```text
# 📊 DQ Daily Summary
## 2026-05-04

### Day Status
Needs Attention

### Check Results
✅ Passed 12
⚠️ Warning 1
🚨 Failed 4

### Alert Risk
🚨 Open Critical 4
⚠️ Open Warning 1

### Next Commands
/alerts dt:2026-05-04 status:open limit:10
/triage alert_key:<Alert Ref>

### ----------------------------------------
```


## Copilot Answer Template

```text
# 🤖 DQ Copilot
## Operator Answer

### Direct Answer
The affected date should not be trusted yet because the raw partition is empty.

### Guardrail
I can explain, summarize, and recommend next steps, but I will not execute remediation without approval.

### Suggested Next Command
/triage alert_key:DQ-20260504-A1B2C3

### ----------------------------------------
```


## Backfill Approval Template

```text
# 🔁 BACKFILL APPROVAL REQUEST
## Durable Approval Queue

### Request Details
**Request ID** `APR-20260504-A1B2C3D4`
**Status** `pending`
**Target DAG** `00_dag_dq_platform_daily_orchestrator`
**Date Range** `2026-05-04` to `2026-05-04`

### Safety Check
No Airflow DAG was triggered. This command writes approval and audit state only.

### Decision Commands
/approve request_id:APR-20260504-A1B2C3D4 comment:Reviewed exact scope
/reject request_id:APR-20260504-A1B2C3D4 comment:Scope is unsafe

### ----------------------------------------
```


## Alert Webhook Delivery

Scheduled alert push and interactive bot commands are intentionally separate:

- Airflow DAG `30_dag_dq_orders_quality_alerts` runs `apps/discord_bot/webhook.py` after deterministic alert generation.
- `DISCORD_ALERT_WEBHOOK_URL` is optional and remains blank when one-way alert delivery is not required.
- Missing webhook configuration produces an explicit skipped result instead of failing the DQ pipeline.
- Configured delivery uses retry/backoff for rate limits and transient HTTP failures.
- A successful `discord_alert_webhook_sent` event in `dq.agent_audit_log` prevents duplicate delivery when an Airflow task is retried or cleared.
- Discord mentions are disabled in webhook payloads so generated values cannot notify users or roles unexpectedly.
- The secret webhook URL is never written to Airflow logs or ClickHouse audit payloads.

The webhook sends deterministic alert summaries. Interactive investigation, natural-language Copilot explanation, triage, and approval commands remain owned by the Discord bot.


## Implementation Notes

- `DISCORD_GUILD_ID` controls guild-scoped slash command registration.
- Runtime formatting and message chunking live in `apps/discord_bot/formatters.py`.
- Slash command handling lives in `apps/discord_bot/bot.py`.
- One-way Airflow alert delivery lives in `apps/discord_bot/webhook.py`.
- Shared API and deterministic fallback operations live in `apps/discord_bot/service.py`.
- Shared HTTP transport remains in `apps/common/control_plane.py`.
- The bot does not require the privileged Message Content intent.
- Approval decisions are durable but non-executing; Airflow DAG 90 remains the remediation boundary.
