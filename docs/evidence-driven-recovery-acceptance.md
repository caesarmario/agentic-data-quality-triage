# Evidence-Driven Recovery Acceptance

> Historical acceptance: this record predates the owner-authorized [fresh demo reset](demo-reset.md) on 16 October 2026. The reset does not retain the runtime rows, messages, or logs cited below. This document is not proof that those records remain in the current local stack.

Local synthetic-orders acceptance, 15-16 October 2026 (Asia/Bangkok). These results describe the current worktree, not a published commit or production rollout. No history was regenerated, no arbitrary repair SQL was offered, and no commit/push was performed.

## Operational Recovery

Each `90_05_dag_dq_incident_recovery` run below finished **success** with `t00_start`, `t20_execute_approved_plan`, and `t90_finish` successful. Stored business validation was independently **PASS**.

| Runbook | Incident / date | Execution and deterministic Airflow run suffix |
| --- | --- | --- |
| `replay_raw_partition` | `DQ-20260111-93B34C` / 2026-01-11 | `81b25252-738d-5e1d-b23d-aae7922a1754` |
| `rebuild_derived_partition` | `DQ-20260112-5236BA` / 2026-01-12 | `1199082e-2d56-52a3-9e4f-b327c7a2284e` |
| `replay_and_rebuild_partition` | `DQ-20260110-5E1900` / 2026-01-10 | `58a35af2-1d66-532b-9877-10c975707938` |

The full run ID is `manual__incident_recovery_<execution UUID>`. Recovery loaded an already stored, checksum-verified upstream version. It did not invoke the generator, choose a cleaner scenario, or call an LLM.

For the combined case, source and restored raw contained 2,534 business rows with identical normalized multiset fingerprints. Backups contained 2,479 rows in raw and staging; all remained unchanged, with 55 missing rows restored. Exact membership verification found all 25 contracted country/channel pairs, including the missing `SG / paid` pair, and no unexpected pair. Repeating the execution API request returned the same execution identity; PostgreSQL retained exactly one execution for that plan.

The `90_07_dag_dq_recovery_acceptance` runs `manual__recovery_fixture_source_invalid_20261015`, `manual__recovery_fixture_raw_20261015`, and `manual__recovery_fixture_derived_20261015` succeeded. The invalid-source case returned business **blocked** with zero warehouse writes. Fixture creation is explicitly separate from recovery. These unused January scopes were not pre-existing operational partitions.

## Live Operator Boundaries

- **Discord approval, Streamlit execution:** plan `76b5f5b6-035c-5d62-b938-9070e5e20f19`, approval `APR-0F820A937ABC4F53D708`. A real browser button/modal sequence approved the immutable plan in the private ops channel. Read-back showed approved / not started, zero executions, and the mart still at 24 segments. A separate Streamlit scope checkbox and Run Approved Fix then created execution `58a35af2-1d66-532b-9877-10c975707938`.
- **Streamlit closure:** raw incident `DQ-20260111-93B34C` closed after request `d11d4067-95c4-45e3-8122-8d9e18ab7645` produced a new positive-volume check, followed by an operator reason, checkbox, and final confirmation. The trusted-local-console name is an audit label, not SSO identity proof.
- **Discord closure:** derived incident `DQ-20260112-5236BA` used Check Again, Check Status, Close Incident, a reason modal, and Confirm Close. Request `1e4d314b-767d-48a4-b640-0c3ee6aac488` produced fresh 25/25 PASS. The incident became RESOLVED at `2026-10-15T22:04:37Z`; its durable message stayed `1560412302904398017`, authored by DataSentry bot `1476800539747553463` with transport `discord_bot_api`.
- **No automatic closure:** combined incident remained TRIAGED after successful recovery; raw and derived became RESOLVED only after their separate closure decisions.
- **Delegated demo:** browser interactions used the owner's authorized Discord session. This is not two-human isolation or separation-of-duties certification.

## Supporting Airflow Evidence

| DAG | Run | Inspected result |
| --- | --- | --- |
| `10_dag_dq_orders_landing_orchestrator` | `manual__recovery_landing_20261015_0110` | Success after a publication syntax fix; retained earlier failed attempt. |
| `10_dag_dq_orders_landing_orchestrator` | `manual__recovery_reload_no_seed_20261015_0110` | Success; seed disabled while reload still ran. |
| `20_dag_dq_orders_dbt_transform` | `manual__recovery_dbt_20261015_0110` | Success after isolated-artifact CLI corrections; earlier retries retained. |
| `20_dag_dq_orders_dbt_transform` | `manual__recovery_lineage_compatibility_20261016` | All six tasks success on attempt one. Latest lineage view published from verified versioned manifest; historical-date freshness remained an explicit dbt warning. |
| `30_dag_dq_orders_quality_alerts` | `manual__recovery_quality_20261015_0110` | Success; deterministic quality/evidence generation. |
| `30_dag_dq_orders_quality_alerts` | `manual__recovery_closure_delivery_20261016_0112` | Success; derived incident card delivered before live closure. |
| `90_03_dag_dq_incident_revalidation` | `manual__incident_revalidation_1e4d314b-767d-48a4-b640-0c3ee6aac488` | Success; fresh 25/25 business PASS, not automatic closure. |
| `90_03_dag_dq_incident_revalidation` | `manual__incident_revalidation_d11d4067-95c4-45e3-8122-8d9e18ab7645` | Success; fresh positive-volume PASS before Streamlit closure. |
| `90_06_dag_dq_recovery_reconciliation` | `manual__recovery_outbox_replay_20261015` | Success; pending projections drained without rerunning recovery. Scheduled reconciliation enabled after migration. |

An old dispatched dbt approval was reconciled from its existing successful child run without re-execution. Legacy import preserved five requests and available snapshots; imported requests remain `legacy_review_required`, not eligible to run a newly privileged recovery plan. After replay, recovery outbox pending count and unknown/publishing/compensating publication counts were zero.

Daily DAG `00` stayed paused as found. Its deliberately queued, unstarted acceptance run was administratively marked failed to preserve that state; **daily-orchestrator end-to-end success is not claimed**. Stage DAG acceptance and deterministic flag tests are separate evidence.

The lineage-compatibility run read already restored raw for the owned January fixture; no generator or new recovery execution ran. Its 7,211-byte latest manifest is a lineage-only projection, with verified original SHA-256 `d7124a041055d314d044895b58f5b03cd19ad674ff676fce7b314419d5feefa2`. Existing consumers matched `dq.fct_orders_daily` and parent `dq.stg_orders`. Raw/staging/source business fingerprints remained equal at 2,534 rows and the mart retained 25 segments. dbt reported **22 passing tests and one freshness failure** because January is intentionally historical; the task's existing allow-failure policy preserved that as observability warning. Neither successful delivery nor scoped recovery is presented as whole-warehouse quality PASS.

## Regression

Named DAG 91 suites `dq`, `pipelines`, `airflow`, `api`, `agent`, and `ui` succeeded in `manual__recovery_acceptance_<suite>_20261015`. The initial Discord suite failed; `manual__recovery_discord_corrected_20261015` passed after localization/assertion corrections. Logs and failed attempts were retained.

| Named suite | Passing tests | Readiness |
| --- | ---: | ---: |
| dq | 61 | 27/27 |
| pipelines | 318 | 27/27 |
| airflow | 135 | 27/27 |
| api | 326 | 27/27 |
| agent | 543 | 27/27 |
| ui | 276 | 27/27 |
| discord, corrected run | 699 | 27/27 |

`manual__recovery_prepaid_gate_all_20261015` completed all five tasks on attempt one: **2,443 passed, 12 skipped, 10 warnings; readiness 27/27**. This was the gate before paid-model testing, not the final post-documentation regression. Final post-change acceptance is recorded below only after terminal inspection.

`manual__recovery_final_discord_20261016` completed all five tasks on attempt one: **702 passed, 10 warnings; readiness 27/27**. This includes the third Help page for recovery buttons and English-first closure copy. The welcome message retained ID `1559709631083839519` after the controlled bot restart; no startup broadcast was added.

### Final Post-Change Gate

DAG `91_dag_dq_platform_validation`, run `manual__recovery_final_all_20261016`: **success**, **2,472 passed, 12 skipped, 10 warnings in 46.40 seconds; readiness 27/27**.

| Task | State | Attempt |
| --- | --- | ---: |
| `t00_start` | success | 1 |
| `t10_run_named_pytest_suite` | success | 1 |
| `t20_run_platform_readiness` | success | 1 |
| `t30_emit_validation_summary` | success | 1 |
| `t90_finish` | success | 1 |

Retained logs were inspected under `logs/airflow/dag_id=91_dag_dq_platform_validation/run_id=manual__recovery_final_all_20261016/`. The 12 skips are Node-dependent screenshot-helper tests in the Python-only runner, not skipped recovery checks. Warnings concern Discord/Python deprecations (`audioop` and modal labels), not provider failures. Host media tooling separately passed TypeScript plus ten deterministic documentary tests; this is inner-loop tooling feedback, not new video playback or mobile acceptance.

## Live Help Follow-Up

After the owner renewed the Discord login on 16 October, real browser clicks opened the welcome card's Button Guide and navigated Investigation -> Approval & Resolve -> Recovery. The guide remained ephemeral (`Only you can see this`) and navigation edited the same personal message, `1560423038531412038`, rather than posting a new channel message for each page. These are explanatory entries, not executable recovery controls.

The actual `/dq language` command selected Indonesia; the existing panel then displayed the Indonesian recovery guide. One untranslated label, Track Execution, was corrected to **Pantau Eksekusi**, covered by a regression assertion, and verified live after a controlled bot restart. Preference was restored through `/dq language` to English and read back from PostgreSQL. Shared welcome stayed English with the same message ID `1559709631083839519`; restart logged `unchanged` and did not broadcast startup messages.

Live-window read-back found zero additional model events and zero additional recovery executions. Recorded screenshots are retained locally under `data/acceptance/recovery/visual/discord-help-recovery-en.png` and `discord-help-recovery-id.png`; they are real cropped UI, not generated mockups. Private Discord responses may disappear when the client reloads; message continuity here refers to navigation within one open guide, not permanent storage of ephemeral messages.

The follow-up also found two synthetic `discord:101` recovery-interaction audit events emitted by a unit test with a mocked execution client. They were not real recovery requests: the actual execution count stayed unchanged. The unit fixture now replaces the live audit writer with an in-memory recorder and asserts the emitted context. Existing audit rows were retained, not deleted or rewritten. A separate post-fix Airflow regression and audit-count read-back verify isolation.

The project containers were later found stopped together after an interruption; no OOM flag was recorded, and the cause was not determined. Existing containers were started without recreate, volume reset, migration replay, or changes to secrets/schedules. Before starting workers, durable state showed zero active/unknown recovery executions and zero unresolved publications. Saved English preference survived the restart.

### Post-Isolation Acceptance

Both runs below used DAG `91_dag_dq_platform_validation`. Every task (`t00_start`, `t10_run_named_pytest_suite`, `t20_run_platform_readiness`, `t30_emit_validation_summary`, `t90_finish`) finished **success on attempt one**, and retained pytest/readiness logs were inspected.

| Run ID | Tests | Readiness |
| --- | --- | --- |
| `manual__recovery_help_isolated_discord_20261016` | 703 passed, 10 warnings; 14.59 seconds | 27/27 |
| `manual__recovery_help_isolated_all_20261016` | 2,473 passed, 12 skipped, 10 warnings; 57.58 seconds | 27/27 |

The skip/deprecation categories are unchanged from the earlier gate. The additional test asserts every Indonesian recovery label and navigation-only action IDs. Final read-back at 10:04 Asia/Bangkok showed **zero new synthetic recovery audit events during post-isolation regression, zero additional provider events, and zero new recovery executions**. English remained selected; welcome identity remained unchanged. The API's personal Gemini setting stayed enabled and the routine runner stayed disabled. Daily DAG `00` remained paused as found.

This completes the previously pending desktop Help recovery click-through. Mobile, two-human isolation, and the live crash/fault campaign remain deferred; they are not implied by these results. No commit or push was made.

## Bounded Gemini Evidence

Pricing was checked against the [official Gemini pricing page](https://ai.google.dev/gemini-api/docs/pricing) before the campaign: Gemini 3.5 Flash-Lite, USD 0.30 input / USD 2.50 output per million tokens. Dashboard billing remains authoritative; all costs below are application estimates.

| DAG / run ID | Calls | Total tokens | Estimated USD |
| --- | ---: | ---: | ---: |
| 92 / `manual__recovery_gemini_scoped_smoke_20261015` | 1 | 176 | 0.0001606 |
| 40 / `manual__recovery_gemini_langgraph_20261015` | 3 | 27,635 | 0.0102595 |
| 92 / `manual__recovery_gemini_answer_en_20261015` | 1 | 10,343 | 0.0034813 |
| 92 / `manual__recovery_gemini_answer_id_20261015` | 1 | 8,798 | 0.0029716 |
| **Campaign** | **6** | **46,952** | **0.0168730** |

These four runs succeeded with actual Gemini provenance, no heuristic fallback, and no paid retries. The first smoke attempt, `manual__recovery_gemini_smoke_20261015`, failed route preflight before any external request; scoped enablement was fixed rather than changing the routine runner kill switch. The campaign stayed below 10 calls / 64,000 tokens / USD 0.20.

The LangGraph report `RPT-E0CA6E5A` and English answer identified the actual SG/paid evidence gap rather than merely restating row counts. The Indonesian answer distinguished current verified recovery from old incident evidence and did not claim automatic closure. One recorded model approval-rationale sentence overstated missing prerequisites; a deterministic policy override now owns that field. The recorded report was not rewritten to hide the limitation. This small sample is functional evidence, not a statistical accuracy or quality-improvement claim.

Routine runner external LLM remains disabled; API personal-testing enablement is preserved. Help, revalidation, execution, reconciliation, and closure do not call Gemini.

## Limits And Pending Evidence

- Desktop evidence covers the authorized browser session. Mobile/narrow and two-human tests remain deferred.
- Concurrent decisions, timeout/unknown outcomes, compensation, and projection replay have deterministic tests. A complete live crash/fault campaign and simultaneous two-human Discord/Streamlit confirmation are not claimed.
- Partition replacement is atomic per table, not across raw/staging/mart. Direct external writers can bypass platform locks. Unknown publication is fail-closed, not automatically resumed.
- Historical source is not reconstructed. Incidents without a verified registered snapshot remain blocked.
- The published demo video predates this recovery slice; frozen recorded diagrams are not presented as current recovery evidence.
- The shared control-plane token authorizes trusted local adapters. Streamlit/API operator names are audit assertions, not cryptographically verified individual identities; do not expose this trusted-local API as an enterprise authenticated service. Discord applies its own guild/channel/operator checks.
