# End-to-End Human Operator Simulation

## Test Record

| Field | Value |
|---|---|
| Test date | 2026-09-30 Asia/Bangkok |
| Business date | `2026-09-30` |
| Scenario | `missing_segment` |
| Main DAG | `00_dag_dq_platform_daily_orchestrator` |
| Main DagRun | `manual__e2e_missing_segment_clean_20260930T213310` |
| Alert Ref | `DQ-20260930-E5C2D0` |
| Report ID | `RPT-B5A091A6` |
| Agent run ID | `8547d86d-acb2-4591-ba06-bd569725944b` |
| Result | Pipeline, detection, triage, Streamlit explanation, and Discord delivery succeeded |

This document records one human-style walkthrough of the complete reliability
flow. The test intentionally used an isolated business date and did not execute
any remediation or destructive cleanup.

## What The Operator Is Testing

The expected journey is:

```text
Controlled data incident
  -> Airflow daily pipeline
  -> SeaweedFS landing object
  -> ClickHouse raw and dbt models
  -> deterministic DQ checks
  -> alert with a human-facing Alert Ref
  -> LangGraph evidence collection
  -> bounded LLM reasoning and fallback
  -> report artifacts and audit trail
  -> Streamlit explanation
  -> Discord operator notification
  -> approval-gated remediation only
```

The deterministic pipeline and guarded tools remain the source of truth. The
LLM helps plan evidence collection, frame hypotheses, and explain the result. It
does not execute SQL mutation, backfill, rerun, or remediation.

## Human Walkthrough

### 1. Confirm The Platform Is Ready

From the project root, start the stack without resetting volumes:

```powershell
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps
```

Open these operator surfaces:

| Surface | URL | Purpose |
|---|---|---|
| Airflow | `http://localhost:8080` | Trigger and inspect orchestration |
| Streamlit | `http://localhost:8501` | Review alerts, reports, and Copilot explanations |
| FastAPI | `http://localhost:8000/docs` | Inspect the shared control-plane API |
| CH-UI | `http://localhost:5521` | Run read-only warehouse inspection |
| SeaweedFS filer | `http://localhost:8888` | Inspect landing, evidence, report, and audit artifacts |

Before proceeding, confirm that there are no DAG import errors and that core
services are healthy. External LLM use should normally remain disabled. It was
enabled only for the bounded Airflow triage acceptance that produced the report
used in this walkthrough, and it is now disabled again.

### 2. Trigger One Controlled Incident

In Airflow, open `00_dag_dq_platform_daily_orchestrator`, choose **Trigger DAG**,
and provide:

```json
{
  "dt": "2026-09-30",
  "run_mode": "manual",
  "incident_scenario": "missing_segment",
  "run_triage": true,
  "max_alerts": 1
}
```

Use a business date that is not being processed by another manual or scheduled
run. Do not pause and unpause the scheduled DAG merely to run this test; see
`E2E-001` in the anomaly register.

### 3. Follow The Airflow Run

The main run completed successfully:

```text
manual__e2e_missing_segment_clean_20260930T213310
```

All main tasks reached `success`:

```text
t00_start
t10_trigger_landing
t20_trigger_dbt_transform
t30_trigger_quality_alerts
t40_trigger_agentic_triage
t90_finish
```

The triggered child runs also completed successfully:

| Stage | DagRun |
|---|---|
| Landing orchestrator | `daily_landing__manual_454f2b75b8820e7f55104c45` |
| Seed to S3 | `landing_seed__manual_181c17882c51a3e9167d167e` |
| Load raw ClickHouse | `landing_load__manual_181c17882c51a3e9167d167e` |
| dbt transform | `daily_dbt__manual_454f2b75b8820e7f55104c45` |
| DQ and alerts | `daily_quality__manual_454f2b75b8820e7f55104c45` |
| Agent triage | `daily_triage__manual_454f2b75b8820e7f55104c45` |

### 4. Verify The Injected Data Problem

The synthetic generator deliberately removed the `SG` and `direct` segment:

| Fact | Observed value |
|---|---:|
| Removed orders | 55 |
| Removed recognized revenue | USD 1,362.26 |
| Final source rows | 2,987 |
| Expected country-channel combinations | 25 |
| Observed country-channel combinations | 24 |

The evidence is retained in:

```text
s3://dq-landing/orders/dt=2026-09-30/orders_2026-09-30.parquet
s3://dq-artifacts/ground-truth/orders/dt=2026-09-30/incident_resolution.json
```

The final partition was consistent across layers after the clean run:

| Layer | Rows | Segments |
|---|---:|---:|
| `dq.raw_orders` | 2,987 | 24 |
| `dq.stg_orders` | 2,987 | 24 |
| `dq.fct_orders_daily` | 2,987 aggregated orders | 24 |

### 5. Verify Detection And Alerting

The DQ stage produced 18 checks:

| Status | Count |
|---|---:|
| Passed | 17 |
| Warning | 1 |
| Failed | 0 |

The warning was:

```text
Check: segment_coverage__country_channel
Observed: 0.96
Expected: 1.00
```

The operator-facing identifier is `DQ-20260930-E5C2D0`. The longer system alert
key remains available only for internal joins and debugging.

The DQ evidence artifact is:

```text
s3://dq-dqfailures/dq-failures/orders/dt=2026-09-30/table=dq_fct_orders_daily/check=segment_coverage__country_channel/check_run_id=15cf7f06-1416-4a71-a841-ae45c08ae74a/evidence.json
```

### 6. Review The Agent Triage

The resulting report identified this leading hypothesis:

```text
Missing country or channel segment in partition
```

| Triage fact | Value |
|---|---|
| Confidence | 0.72 |
| Evidence items retained | 6 |
| Recommended actions | 3 |
| Approval-gated actions created | 0 |
| Report ID | `RPT-B5A091A6` |

Artifacts:

```text
s3://dq-artifacts/agent-reports/dt=2026-09-30/report_id=RPT-B5A091A6/alert_key_hash=49f2b367f3fa/agent_run_id=8547d86d-acb2-4591-ba06-bd569725944b/report.md
s3://dq-artifacts/agent-reports/dt=2026-09-30/report_id=RPT-B5A091A6/alert_key_hash=49f2b367f3fa/agent_run_id=8547d86d-acb2-4591-ba06-bd569725944b/report.json
```

### 7. Understand The LLM Result

The triage run made three bounded reasoning calls:

| Requested route | Executed provider/model | Input | Output | Estimated cost | Result |
|---|---|---:|---:|---:|---|
| `evidence_planning` | Gemini `gemini-3.5-flash-lite` | 798 | 296 | USD 0.0009794 | Valid structured output |
| `hypothesis_framing` | Gemini `gemini-3.5-flash-lite` | 3,826 | 380 | USD 0.0020978 | Valid structured output |
| `low_confidence_rca` | Heuristic `heuristic-v1` | 1,011 | 519 | USD 0 | OpenAI attempt returned 429, then safe fallback |

Aggregate model evidence:

| Metric | Value |
|---|---:|
| Input tokens | 5,635 |
| Output tokens | 1,195 |
| Estimated application cost | USD 0.0030772 |
| Model-route duration | 14,752 ms |
| External model used | Yes |
| Heuristic fallback used | Yes |

The estimated application cost is not the authoritative provider invoice. The
provider dashboard remains the billing source of truth.

### 8. Review The Result In Streamlit

Open Streamlit and select:

```text
Date: 2026-09-30
Alert Ref: DQ-20260930-E5C2D0
```

The operator console correctly displayed 18 checks, 17 passes, one warning, the
triaged alert, report availability, confidence 0.72, and evidence context.

Use **Explain this alert** in the Copilot panel. The observed response:

- explained the missing segment in natural language;
- showed the leading hypothesis and confidence;
- recommended reviewing segment configuration before requesting a backfill;
- explicitly stated that rerun, backfill, notification, ticket, and data-changing
  actions require the controlled approval boundary.

No mutation was executed from Streamlit.

### 9. Review The Result In Discord

For a human slash-command check, run:

```text
/dq triage alert_key:DQ-20260930-E5C2D0
/dq ask question:What happened and what should I do next? alert_key:DQ-20260930-E5C2D0
```

The automated acceptance used the same persisted report, control-plane Copilot
endpoint, formatter, and configured triage channel. Discord accepted two message
chunks with HTTP 200. The stored messages were read back successfully and
contained the E2E label, Alert Ref, Report ID, and approval guardrail.

This proves real channel delivery and readable content. It does not replace the
final human slash-command interaction, because Discord does not allow the bot to
impersonate a human interaction safely.

### 10. Stop At The Approval Boundary

The incident is a warning and the report did not create an executable approval
request. The safe next step is to inspect the missing segment and, only if a
backfill is justified, create a preview or approval request through the existing
Discord, UI, or API flow.

Do not interpret a recommendation as executed remediation.

## Acceptance Evidence

| Area | Evidence | Result |
|---|---|---|
| Main orchestration | Main DagRun state and six task states | Passed |
| Child orchestration | Six child DagRuns and all task states | Passed |
| Incident injection | Immutable ground-truth artifact | Passed |
| Warehouse consistency | Raw, staging, and mart partition queries | Passed |
| DQ detection | 17 pass and one warning | Passed |
| Alert identity | Human Alert Ref and stable system key | Passed |
| Agent triage | Report, evidence, confidence, and audit references | Passed |
| Gemini use | Two successful Gemini routes with token and cost evidence | Passed |
| Safe fallback | OpenAI 429 fell back to deterministic heuristic | Passed with anomaly |
| Streamlit | Selected alert and Copilot explanation | Passed with UX anomalies |
| Discord | Two real messages accepted and read back | Passed |
| Remediation safety | No mutation or remediation executed | Passed |

## Anomaly Register

### E2E-001: Unpausing The Scheduled DAG Created A Catch-Up Run

**Priority:** High

During the first attempt, unpausing the daily orchestrator created
`scheduled__2026-09-28T17:05:00+00:00` while the manual incident run was active.
Both runs succeeded, but the scheduled run could write the same logical partition
and change the data after the incident snapshot was captured.

**Risk:** The final warehouse partition may no longer match the alert, report, or
ground-truth artifact used during the investigation.

**Recommendation:**

1. Do not use pause/unpause as a manual-testing control.
2. Keep production scheduling state stable and use a dedicated manual test date.
3. Add a partition-level concurrency or write-conflict guard.
4. Add a preflight check that rejects a manual run when another run can write the
   same dataset and business date.
5. Make catch-up behavior explicit in the DAG documentation and tests.

### E2E-002: Discord Webhook Task Was Green Although Delivery Was Skipped

**Priority:** High

Airflow task `t70_push_discord_alerts` reached `success`, while its log reported:

```text
status=skipped
reason=webhook_url_not_configured
alerts_discovered=0
```

The interactive Discord bot was configured, but the independent scheduled-alert
webhook was not.

**Risk:** A green quality DAG can be mistaken for successful incident delivery.

**Recommendation:** Surface notification delivery as a separate observable state.
Use an Airflow `skipped` state for an intentionally disabled integration, or fail
a required-notification gate when delivery is mandatory. Include discovered,
attempted, delivered, and failed counts in the task summary.

### E2E-003: Heuristic Fallback Leaked Raw Prompt Context Into The Report

**Priority:** High

The Markdown report's recommended action contains the raw narrative request and a
large serialized context block. It also states `No external LLM was called`, even
though Gemini successfully handled two earlier routes.

**Risk:** The operator report is difficult to read, can expose internal prompt
structure, and gives a misleading aggregate statement about model use.

**Recommendation:**

1. Make fallback output return only an operator-facing narrative.
2. Never append raw prompt or serialized context to recommended actions.
3. Build report-level model disclosure from the aggregate `llm_runtime` summary,
   not from the last route alone.
4. Add regression tests for prompt leakage and contradictory provider statements.

### E2E-004: Stronger Reasoning Route Tried An Unavailable OpenAI Provider

**Priority:** Medium

The `low_confidence_rca` route attempted OpenAI `gpt-5.6-luna`, received HTTP 429
with `credit_balance_exhausted`, and safely fell back to the heuristic route.

**Risk:** A portfolio demo configured around Gemini can lose narrative quality or
latency budget because deep reasoning silently attempts a provider without usable
billing.

**Recommendation:** Make provider fallback order explicit per environment. While
OpenAI billing is unavailable, map the stronger route to an approved Gemini model
or skip the external attempt and use the deterministic fallback immediately.

### E2E-005: Streamlit AI Runtime Card Showed Only The Last Route

**Priority:** Medium

The UI emphasized `heuristic / heuristic-v1` and zero cost, while the report
aggregate showed two successful Gemini calls and USD 0.0030772 estimated cost.

**Risk:** Operators receive an incomplete model-use and cost picture.

**Recommendation:** Render aggregate providers, external-model use, fallback use,
total tokens, total estimated cost, and per-route details from `llm_runtime` and
`llm_route_events`.

### E2E-006: Streamlit Investigation Copy Conflicted With Report State

**Priority:** Medium

The selected alert was `triaged` and had a matching report, but the UI also showed
copy such as `Run triage to create the first evidence-backed record` and
`Run triage to generate report`.

**Risk:** The operator may rerun triage unnecessarily and create duplicate audit
or report artifacts.

**Recommendation:** Derive call-to-action copy from alert lifecycle state and
report availability. When a matching report exists, show **View report** or
**Re-run triage with new evidence**, not a first-run prompt.

### E2E-007: Global Alert Filters Could Conflict With The Selected Daily Snapshot

**Priority:** Low

The overview could show affected dates from a global triaged-alert list while the
daily check snapshot was scoped to `2026-09-30`.

**Risk:** Date-level metrics and alert-level metrics can appear to describe the
same scope when they do not.

**Recommendation:** Display an explicit scope badge on every card and default the
alert list to the selected business date during incident investigation.

### E2E-008: Airflow Trigger Dialog Did Not Create A Run Under Browser Automation

**Priority:** Investigation required

The trigger dialog closed during browser automation without producing a DagRun or
a visible API POST. The CLI/Make trigger path worked.

**Risk:** This may be an automation/hydration limitation rather than a product
bug. It is not yet reproducible as a normal human interaction.

**Recommendation:** Reproduce once manually in Airflow before changing code. If a
human click also fails, capture browser network errors and Airflow API logs.

### E2E-009: Discord Bot Needed DNS Recovery During Startup

**Priority:** Low

The bot initially logged a temporary DNS resolution failure for `discord.com`,
then reconnected and registered 20 commands successfully.

**Risk:** A transient network failure delays operator availability.

**Recommendation:** Keep reconnect/backoff enabled, expose bot readiness through
container health, and alert only if the bot remains unavailable after a bounded
grace period.

### E2E-010: Docker Desktop Restart Interrupted The Local Session

**Priority:** Environment note

Local endpoints were briefly unavailable when Docker Desktop restarted. Services
recovered and persisted data remained intact.

**Recommendation:** Do not classify this as an application defect. For demos,
run a readiness check before presenting and avoid Docker Desktop maintenance
during the session.

## Recommended Fix Order

1. Remove raw prompt/context leakage and correct aggregate model disclosure.
2. Prevent same-partition concurrent writers and document catch-up behavior.
3. Make notification delivery state explicit instead of green-on-skip.
4. Fix Streamlit report-state and aggregate LLM-runtime presentation.
5. Make environment-specific provider fallback policy explicit.
6. Add readiness and recovery visibility for Discord.
7. Manually reproduce the Airflow trigger-dialog issue before treating it as a
   code defect.

## Safe State After The Test

- `EXTERNAL_LLM_ENABLED=false` in the runtime.
- The daily orchestrator is paused after the isolated test to avoid another
  automatic catch-up while findings are being reviewed.
- No backfill, rerun, approval decision, data mutation, or cleanup was executed.
- The Discord test message remains in the configured triage channel as operator
  evidence.

## Documentation Acceptance

The final project validation was executed through Airflow after this document
was added:

| Field | Value |
|---|---|
| Validation DAG | `91_dag_dq_platform_validation` |
| Validation run | `manual__validation_all_20260929T231028161608` |
| DagRun state | `success` |
| Task states | Five of five `success` |
| Test suite | `1064 passed`, `12 skipped`, `1 warning` |
| Platform readiness | 20 passed, 0 failed |

The warning is the upstream `discord.py` use of Python's deprecated `audioop`
module. It did not affect this acceptance run, but it should be tracked before a
future Python 3.13 runtime upgrade.
