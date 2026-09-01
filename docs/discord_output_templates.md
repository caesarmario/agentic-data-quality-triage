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
/dq alerts dt:<YYYY-MM-DD> status:open limit:10
/dq daily_summary dt:<YYYY-MM-DD>
/dq triage alert_key:<Alert Ref or system key>
/dq ask question:<question> alert_key:<optional Alert Ref>
/dq backfill_preview start_date:<YYYY-MM-DD> end_date:<YYYY-MM-DD> target_dag_id:<DAG ID> reason:<reason>
/dq approve request_id:<APR ID> comment:<review note>
/dq reject request_id:<APR ID> comment:<review note>
```

Guild-scoped command registration is used for the local demo so command changes synchronize quickly.
Legacy top-level aliases remain available for one compatibility release, but new examples and operator guidance use the canonical `/dq` namespace.


## Bot Permissions And Channel Contract

Use the smallest practical Discord installation scope for this local portfolio bot:

- OAuth scopes: `bot` and `applications.commands`.
- Bot permissions: View Channels and Send Messages.
- Optional permission: Read Message History only when operators need Discord-side context continuity.
- Disabled privileged intent: Message Content.
- Runtime gateway intent: Guilds only.

The command transport remains slash-command based. The bot does not inspect arbitrary server messages and does not need member, presence, moderation, voice, reaction, or message-content access.

Use three explicit channel roles:

- `dq-alerts` receives deterministic alert summaries and scheduled webhook delivery.
- `dq-triage` receives evidence-backed triage reports and Copilot answers.
- `dq-ops-private` receives approval previews and approve/reject decisions when the optional private operations channel is configured.

If `dq-ops-private` is not configured, approval messages fall back to `dq-triage`. Approval messages remain non-executing; Airflow owns the remediation boundary.


## Static Formatting Versus AI Copilot Reasoning

The deterministic formatter and the LLM-assisted Copilot serve different purposes. Portfolio screenshots should show that difference explicitly instead of presenting every formatted sentence as AI output.

### Deterministic Alert Message

This message is assembled from trusted ClickHouse fields. It is stable, testable, and available without an API key.

```text
# 🚨 Critical Data Quality Alert
## Raw Orders Data has missing or unusually low row count

### Quick Read
Raw Orders Data has no rows for 2026-05-04.
Triage this alert before trusting downstream data.

### Key Facts
Alert Ref DQ-20260504-A1B2C3
Observed / Expected 0 / 1

### Recommended Next Step
/dq triage alert_key:DQ-20260504-A1B2C3
```

### LLM-Assisted Copilot Readout

This message is generated only after guarded tools provide bounded alert, evidence, lineage, pipeline, and incident-history context. Provider, model, route, token, cost, duration, and fallback metadata remain auditable.

```text
# 🤖 DQ Copilot
## Why this alert needs investigation

The warehouse partition is empty, but that does not yet prove the landing file is missing. The latest pipeline evidence should be checked first because an unsuccessful ClickHouse load can produce the same symptom even when the S3 object exists.

The safest next action is to run triage and compare the landing artifact, load status, and downstream row counts. A backfill should remain approval-gated until those checks agree.

Confidence 0.82
Suggested command /dq triage alert_key:DQ-20260504-A1B2C3
```

The Copilot may improve explanation and action wording, but it cannot execute SQL, trigger Airflow, mutate data, or approve remediation.


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
/dq triage alert_key:DQ-20260504-A1B2C3

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
/dq alerts dt:2026-05-04 status:open limit:10
/dq triage alert_key:<Alert Ref>

### ----------------------------------------
```


## Copilot Answer Template

```text
# 🤖 DQ Copilot
## Operator Answer

### Direct Answer
I found two earlier investigation records for this Alert Ref. The latest prior report identified a missing segment, but that conclusion must be checked against the current evidence before any action is approved.

### Alert Context
Alert Ref `DQ-20260504-A1B2C3`
Previous Investigation Records `2`
Prior records are comparison context only, not proof of the current root cause.

### Question
Has this alert been investigated before?

### Guardrail
I can explain, summarize, and recommend next steps, but I will not execute remediation without approval.

### Suggested Next Command
/dq triage alert_key:DQ-20260504-A1B2C3

### ----------------------------------------
```

Use the shared bounded Copilot path for history questions:

```text
/dq ask question:Has this alert been investigated before? alert_key:DQ-20260504-A1B2C3
```

The answer may summarize earlier outcomes for the same exact Alert Ref. It must not claim that the issue recurred across other dates unless separate current evidence proves that pattern.


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
/dq approve request_id:APR-20260504-A1B2C3D4 comment:Reviewed exact scope
/dq reject request_id:APR-20260504-A1B2C3D4 comment:Scope is unsafe

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
