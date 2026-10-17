<!--
Gemini Triage Acceptance Evidence
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# Gemini Triage Acceptance

> Historical acceptance: this record predates the owner-authorized [fresh demo reset](demo-reset.md) on 16 October 2026. The reset does not retain the runtime rows, messages, or logs cited below. This document is not proof that those records remain in the current local stack.

## Revalidation On 2026-09-30

The current checked-in routing policy was revalidated through the bounded
supervisor using the existing synthetic missing-partition alert. Unlike the
historical September 7 run below, this acceptance did not require a temporary
routing file: the reviewed repository routes now use Gemini for evidence
planning, hypothesis framing, and the normal triage narrative whenever external
LLM execution is explicitly enabled.

| Check | Evidence |
| --- | --- |
| DAG | `98_dag_dq_control_plane_supervisor_smoke` |
| Run | `manual__gemini_ai_investigation_20260930T000500` |
| Result | DagRun and all five tasks succeeded |
| Parent run | `6c230462-7f0a-51b9-a4ae-f3313827bf75` |
| Child run | `2d910f74-47af-4f45-b8e1-9402add33885` |
| Alert Ref | `DQ-20260610-9C37F7` |
| Report | `RPT-314BBFBD` |
| Provider/model | `gemini / gemini-3.5-flash-lite` |
| Calls | 3, with validated structured planning/framing and no fallback |
| Usage | 7,270 input + 1,340 output = 8,610 tokens |
| Estimated cost | USD 0.00553100 |
| Route duration | 7,187 ms aggregate |
| Investigation | Confidence 0.84, seven evidence items |
| Approval | Required and still pending; no remediation executed |

The route breakdown was `evidence_planning` (788 input, 300 output),
`hypothesis_framing` (4,153 input, 638 output), and `triage_reasoning` (2,329
input, 402 output). The report is stored at:

```text
s3://dq-artifacts/agent-reports/dt=2026-06-10/report_id=RPT-314BBFBD/alert_key_hash=f3ae2e08c11c/agent_run_id=2d910f74-47af-4f45-b8e1-9402add33885/report.md
```

The generated operator brief identified the zero-row partition, described the
possible downstream impact, explicitly retained uncertainty between a landing
failure and a completed zero-write pipeline, and recommended checking upstream
landing evidence before requesting a backfill. The recommendation did not
execute an action.

The same report was rendered through Discord formatting and Streamlit. A
report-aware FastAPI Copilot request also returned a natural-language answer
grounded in five evidence rows, five audit rows, and two prior incident records.
Provider route telemetry for Copilot answers is now persisted separately from
the bounded interaction event so provider, model, usage, cost estimate, and
fallback state remain auditable without storing raw question or answer text.

The post-fix live Copilot request used agent run
`5612edf9-97e6-4602-bd6a-1c4937148365`. Gemini generated the answer with 2,216
input tokens, 171 output tokens, 4,185 ms route duration, and estimated cost USD
0.00109230. ClickHouse retained one `llm_route_completed` row and one
`copilot_answer` row under that same run ID. The route used
`gemini-3.5-flash-lite`, did not use heuristic fallback, and explicitly stated
that no remediation had executed.

## Direct Discord Answer Acceptance On 2026-10-05

The Discord service path was exercised with the direct-answer presentation that
was exposed through `/dq ask` at the time. The current command no longer exposes
`response_mode` as an operator option. The bot called the shared FastAPI endpoint,
the API called Gemini once, and the Discord formatter preserved the sanitized
model answer instead of splitting it into the normal triage-report sections.

| Check | Evidence |
| --- | --- |
| Agent run | `028971fa-15a6-497e-9921-eda3ea6bb176` |
| Transport | Discord service to shared FastAPI API |
| Provider/model | `gemini / gemini-3.5-flash-lite` |
| Route | `cheap_summary` |
| Usage | 2,225 input + 185 output = 2,410 tokens |
| Estimated cost | USD 0.00113000 |
| Route duration | 3,389 ms |
| Fallback | None |
| Audit | Correlated `llm_route_completed` and `copilot_answer` rows |
| Side effects | No SQL mutation, backfill, or remediation |

The model answer stated that the `dq.raw_orders` partition was empty, cited the
zero-row query and pipeline failure evidence, retained uncertainty because the
exact upstream failure log was absent, and recommended upstream review followed
by an approval-gated backfill request. Runtime proof was rendered separately so
operators can distinguish Gemini output from local heuristic fallback.

Post-change deterministic regression was accepted through Airflow DAG
`91_dag_dq_platform_validation`, run
`manual__validation_all_20261005T123500355530`. The DagRun and all five tasks
succeeded. Airflow logs retained `1,070 passed, 12 skipped, 1 warning` and
`20 passed, 0 failed` for platform readiness. External LLM execution remained
disabled throughout this regression run and was verified as `false` in the
runner, API, Streamlit, and Discord bot after the paid acceptance call.

## Incident-First Discord And Streamlit Acceptance On 2026-10-06

The incident-first operator slice was accepted against one controlled
`missing_segment` incident for business date `2026-10-05`. Deterministic facts
continued to come from ClickHouse and Airflow; Gemini supplied only the
investigation narrative and recommendation.

| Check | Evidence |
| --- | --- |
| Alert Ref | `DQ-20261005-3B55A6` |
| Discord message | `1557017880854396939` |
| Lifecycle continuity | Same message for `OPEN`, `TRIAGED`, `WAITING APPROVAL`, and `APPROVED` |
| Gemini agent run | `516dfe30-5c9d-4617-a191-3fd3f57d40f0` |
| Provider/model | `gemini / gemini-3.5-flash-lite` |
| Usage | 7,201 input + 1,018 output = 8,219 tokens |
| Estimated cost | USD 0.00470530 |
| Fallback | None across three model calls |
| Report | `RPT-966426C2` |
| Approval | `APR-20261005-8E61C59F`, approved but execution remained `not_started` |
| Regression DAG | `91_dag_dq_platform_validation` |
| Regression run | `manual__validation_incident_ux_documented_20261006T134100` |
| Regression result | All five tasks succeeded; 1,106 passed, 12 skipped, one dependency warning |
| Readiness | 20 passed, 0 failed; no DAG import errors |

The card title, severity, table, date, Alert Ref, lifecycle, and approval state
were deterministic. The persisted provider-backed report supplied the compact
explanation. At the time, the pre-migration webhook transport was read back to
confirm one stored embed after each update; that statement is historical and is
not evidence for the current Bot REST transport. The
approval decision did not dispatch DAG 90 or any target DAG, and no remediation
was executed. `EXTERNAL_LLM_ENABLED` was restored to `false` before final
regression.

Streamlit was inspected in desktop light and dark modes plus a 390-pixel narrow
viewport. The incident queue remained ahead of administrative panels, the
explicit light and dark palettes retained readable contrast, and the narrow
layout had no page-level horizontal overflow. This is a focused local visual
check, not a formal accessibility certification.

## Discord Incident Decision UX Acceptance On 2026-10-08

The current Discord transport uses the Discord Bot REST API with the same green
DataSentry application identity used by slash commands. Fresh acceptance covered
the latest channel routing, PostgreSQL approval-decision lock, HIGH-risk recovery
wording, same-message incident lifecycle, strict Gemini provenance, and the
standalone DAG 40 T3 budget.

| Check | Evidence |
| --- | --- |
| Discord regression | DAG 91 `manual__discord_incident_decision_ux_20261008T060000`; all five tasks succeeded; 154 passed; 20/20 readiness |
| Full regression | DAG 91 `manual__all_incident_decision_ux_20261008T060100`; all five tasks succeeded; 1,160 passed, 12 skipped; 20/20 readiness |
| Final regression | DAG 91 `manual__final_incident_decision_ux_20261008T060400`; all five tasks succeeded; 1,160 passed, 12 skipped; 20/20 readiness |
| Daily E2E | `manual__discord_incident_e2e_20261008T060300`; parent and all landing/dbt/quality/triage children succeeded |
| Scenario / Alert Ref | `duplicates_spike` / `DQ-20261008-72DF3B` |
| Discord message | `1557526654618706023`; created `OPEN`, then updated in place to `TRIAGED` |
| Bot identity | Author `1476800539747553463`, `discord_bot_api`, no `webhook_id` |
| Strict provider smoke | DAG 92 `manual__llm_smoke_discord_ux_20261008T055800`; one Gemini call; no fallback |
| Smoke usage | 127 input + 37 output = 164 tokens; USD 0.00013060 estimated cost |
| Paid triage | DAG 40 `manual__gemini_incident_ux_20261008T060000`; all five tasks succeeded |
| Paid triage usage | Three calls; 8,727 aggregate tokens; USD 0.00540770 estimated cost |
| Paid triage report | `RPT-05F651A8`; agent run `68e34965-bc24-4ced-8d6a-b448d193dfdc` |
| Provenance | Live card rendered `AI analysis | Gemini 3.5 Flash Lite | Grounded in platform evidence` |
| Disabled-provider fallback | DAG 40 `manual__fallback_incident_ux_20261008T060100`; zero external calls and zero provider cost |
| Fallback provenance | Same message changed to `Heuristic analysis | No external model call` |
| Restored defaults | `EXTERNAL_LLM_ENABLED=false`; scheduled daily DAG paused |

The three paid calls were `evidence_planning`, `hypothesis_framing`, and
`triage_reasoning`. All executed through Gemini 3.5 Flash-Lite with validated
structured outputs where requested and no heuristic execution. The shared T3
ledger retained 3/3 calls, 8,727/16,384 tokens, USD 0.00540770/USD 0.05, and
9,085/300,000 ms. This fixed the earlier over-budget acceptance pattern in which
a full investigation could make more than three calls.

The live Bot REST read-back showed the compact incident fields and the
`Investigate`, `Ask AI`, `Evidence`, and optional `Open Workspace` actions. The
external-provider run truthfully named Gemini. After the kill switch was
restored, the next run removed the Gemini label and kept the deterministic
severity, lifecycle, table, date, and Alert Ref unchanged.

Approval remains separate from execution. The current HIGH-risk card states that
there is no automatic rollback, approval decisions are serialized through the
PostgreSQL control store, and no button can dispatch remediation directly. Live
component acceptance on 2026-10-08 exercised incident, triage, report, draft,
approval, and rejection controls for `DQ-20261008-72DF3B`. Request
`APR-20261006-A89DDA5E` was confirmed `APPROVED`; request
`APR-20261008-1CAA3926` was reason-required `REJECTED`. Both remained
`execution_status=not_started` with no execution DagRun ID, and the Airflow
metadata database recorded no new DagRun during the decision window.

## Verified Scope

On 2026-09-07, the bounded supervisor executed one existing synthetic orders
incident through the Incident Triage Agent using Gemini for evidence planning,
hypothesis framing, and the final narrative. This is a three-call single-agent
workflow, not three parallel workers and not an unrestricted autonomous agent.

The test used the existing alert
`orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table` and the dbt
manifest at `s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json`.
It did not regenerate warehouse data, trigger backfill, or execute remediation.

## Airflow Evidence

| Check | Evidence |
| --- | --- |
| DAG | `98_dag_dq_control_plane_supervisor_smoke` |
| Run | `manual__gemini_triage_accounting_20260907T212700` |
| Start | `2026-09-07T21:26:10+07:00` |
| End | `2026-09-07T21:26:29+07:00` |
| Result | DagRun and all five tasks succeeded |
| Parent run | `c4b18ae9-b142-5656-8283-7041458da597` |
| Child run | `34f8f70a-0c8c-42f6-b980-b00ce77f83d7` |
| Report | `RPT-90F9F926` |
| Provider/model | `gemini / gemini-3.5-flash-lite` |
| Calls | 3, without external retries or fallback |
| Usage | 6,470 input + 1,078 output = 7,548 tokens |
| Estimated cost | USD 0.00463600 |
| Structured outputs | Planning and framing validated |
| Investigation | Confidence 0.84, seven retained evidence items, no investigation errors |
| Actions | Backfill recommendation requires human approval |

The report lives under
`s3://dq-artifacts/agent-reports/dt=2026-06-10/report_id=RPT-90F9F926/alert_key_hash=f3ae2e08c11c/agent_run_id=34f8f70a-0c8c-42f6-b980-b00ce77f83d7/`
as `report.json` and `report.md`. Both artifacts were read after the Airflow run.
Their route count, input/output tokens, and estimated cost were reconciled against
the three `llm_route_completed` rows in `dq.agent_audit_log` for the exact child UUID.

## Accounting Defect Found And Fixed

The first full test, `manual__gemini_full_triage_20260907T212000`, completed three
Gemini calls with 7,222 tokens and estimated cost USD 0.00467460. Its supervisor
audit totals were correct, but report `RPT-A8E77090` counted only the last narrative
call. That historical report must not be used as evidence of complete cost reporting.

The fix retains sanitized `llm_route_events` in triage state and report JSON.
Planning/framing telemetry does not become deterministic DQ evidence. New report
totals use the complete event collection rather than also counting the legacy
narrative evidence a second time. Reports without event collections keep the
legacy behavior; historical reports are not silently rewritten.

Airflow regression evidence:

- DAG `91_dag_dq_platform_validation`.
- Run `manual__triage_accounting_20260907T212500`.
- All five tasks succeeded; task logs show 366 agent-suite tests passed and 20/20
  platform readiness checks passed.
- External LLM disabled throughout deterministic regression.

## Historical 2026-09-07 Bounded Test Configuration

Both full triage runs used an isolated, temporary routing profile inside the
runner, derived from `configs/agent/model_routing.yml`:

1. Keep only the Gemini and heuristic providers enabled.
2. Map `triage_reasoning` to the same provider/model and pricing as `cheap_summary`.
3. Point external-route fallback directly to `evidence_summary`, never another
   external provider. Acceptance requires no fallback in the retained evidence.
4. Keep the strong-review route disabled; do not label Flash-Lite as a stronger
   model merely to make a high-risk test pass.
5. Use one specialist handoff, zero retries, at most three model calls, 16,384
   aggregate tokens, USD 0.05, and a 300,000 ms deadline.

The temporary YAML SHA256 was
`07959e323464e1eec5b29f7c2863846ebfb4c099c9c98726adb5517a0489ef26`.
`LLM_ROUTING_CONFIG_PATH` selected it only in the recreated test runner.
Disk configuration was restored immediately after runner setup. After each
terminal test, the runner was recreated from the disabled disk configuration;
`EXTERNAL_LLM_ENABLED=false` was verified and the temporary profile was discarded.

At the time of this historical run, default application routing was not replaced.
That statement no longer describes the current checked-in policy; see the
September 30 revalidation above.

## Remaining Limits

- Three LLM-backed parallel workers have separate strict acceptance with distinct
  evidence purposes and a shared parent budget in `gemini_fanout_acceptance.md`.
  That proves bounded integration, not that fan-out is diagnostically superior.
- This single synthetic missing-partition scenario does not establish accuracy
  across every incident type or production workload.
- Provider usage dashboards remain authoritative for billing. Application costs
  are estimates, not guarantees of invoice totals.
- Generated prose still needs human review; deterministic rules, confidence policy,
  guarded tools, and approval gates remain authoritative.
