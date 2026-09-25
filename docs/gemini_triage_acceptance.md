<!--
Gemini Triage Acceptance Evidence
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# Gemini Triage Acceptance

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

## Bounded Test Configuration

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

The default application routing was not replaced: enabling external LLM globally
without this isolated test profile can still select other configured providers.

## Remaining Acceptance

- Three LLM-backed parallel workers still require separate acceptance with distinct
  evidence purposes and a shared parent budget.
  The implemented adapter, deterministic acceptance, and failed 2026-09-11
  connectivity attempt are recorded in `gemini_fanout_acceptance.md`.
- This single synthetic missing-partition scenario does not establish accuracy
  across every incident type or production workload.
- Provider usage dashboards remain authoritative for billing. Application costs
  are estimates, not guarantees of invoice totals.
- Generated prose still needs human review; deterministic rules, confidence policy,
  guarded tools, and approval gates remain authoritative.
