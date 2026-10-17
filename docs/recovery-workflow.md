# Evidence-Driven Recovery Workflow

**Status:** Implemented local recovery workflow. See [fresh v1 runtime acceptance](datasentry-v1-acceptance.md) separately from the [historical recovery ledger](evidence-driven-recovery-acceptance.md). Neither local acceptance nor this guide certifies production readiness. The reconciler is restored paused after acceptance; do not assume scheduled reconciliation is enabled.

Recovery is a bounded, operator-approved path for one incident and one business date. The API selects a runbook from current evidence, pins its source and implementation facts into an immutable plan, and requires a separate human decision before it dispatches the recovery DAG. Models explain and recommend; they do not select source objects, targets, commands, SQL, or execution parameters.

The ordinary daily pipeline remains separate. Recovery does not regenerate a baseline, seed replacement data, invoke the daily orchestrator, or close an incident. A successful Airflow run, a passing scoped data check, and a human-confirmed incident closure are distinct states.

## Scope And Evidence

Automatic recovery currently applies only to supported `dq_failure` incidents on `dq.raw_orders`, `dq.stg_orders`, or `dq.fct_orders_daily`. Schema changes and other incident kinds require a manual runbook and are blocked from these automatic runbooks.

Before a plan is eligible, the API reads the current incident revision and bounded warehouse facts, checks the trusted incident context, and compares source, raw, staging, and mart state. Source replay requires a validated, immutable upstream snapshot registered for the same business date. A file someone can access manually, an existing legacy raw partition, an unregistered landing object, or a historical approval is not a pinned recovery input. If no usable snapshot exists, or the source fails the active schema/business/segment contract, preview remains blocked.

For an explicitly supplied correction, `scripts/register_recovery_source.py` is the source-registration path, not a recovery command. It accepts a fixed-bucket key under the selected orders/date prefix plus an expected SHA-256 and correlation ID, verifies the actual object bytes, reads Parquet, applies the active source/business/segment contract, and publishes an immutable source snapshot. It does not generate replacement rows. A manually available file that has not passed this registration path, or a legacy source/approval record without the required immutable facts, remains blocked from automated recovery.

The selected plan binds the incident revision and episode, source manifest, source and partition fingerprints, contract and model hashes, publication revision/watermark, targets, validation requirements, rollback steps, and operator reason. The plan is immutable and hashed. If its incident, evidence, source, contracts, model, or publication preconditions change before execution, the API rejects it and requires a newly reviewed plan.

## Server-Selected Runbooks

Runbooks are selected by deterministic code in `agent/recovery/contracts.py`, not caller input or model output:

| Runbook | Evidence condition | Scope |
|---|---|---|
| `replay_raw_partition` | The incident is on `dq.raw_orders`, a valid pinned source exists, raw differs, and no derived partition exists for that date. | Replay that date's pinned upstream input to raw. |
| `rebuild_derived_partition` | The incident is on staging or mart, raw matches the pinned source and passes its contract, but derived outputs do not match. | Rebuild staging and the daily mart for that date. |
| `replay_and_rebuild_partition` | Raw differs from the pinned source and the incident affects derived data, or an existing derived partition would become stale after raw replay. | Replay raw, then rebuild staging and mart for that date. |

If the facts do not justify one of these bounded paths, preview returns blockers and no runbook. The source must satisfy the configured orders schema, date, business-domain, and expected country/channel segment contract. Legacy records do not bypass these requirements.

The shared AI context uses the same minimum-action eligibility policy. Source existence alone is not a reason to repair data. When current source/warehouse business values agree, `not_indicated` suppresses another replay/rebuild recommendation. Earlier landing discrepancies remain historical evidence; a fresh scoped check, not an AI opinion, determines whether closure is possible.

## Operator API

The shared API exposes these recovery operations. Plan creation and execution dispatch use the configured approval-authorization dependency; contracts reject extra fields and do not accept source paths, SQL, target overrides, or arbitrary commands.

| Method and path | Purpose |
|---|---|
| `GET /api/v1/alerts/{alert_ref}/recovery-preview` | Read current eligibility, scope, selected runbook, source summary, observations, and blockers. Does not repair data. |
| `POST /api/v1/alerts/{alert_ref}/recovery-plans` | Create or retrieve the immutable proposal for the selected incident revision and operator reason. |
| `GET /api/v1/recovery-plans/{plan_id}` | Read the plan with its current approval and any existing execution. |
| `POST /api/v1/recovery-plans/{plan_id}/executions` | Dispatch the already approved exact plan after explicit scope confirmation and version/hash checks. |
| `GET /api/v1/recovery-executions/{execution_id}` | Read execution, validation, and synchronization state. |

Discord and Streamlit are operator interfaces to these shared API operations. They do not have a local mutation fallback. The Streamlit operator name is recorded but is not SSO identity proof; deployment authorization must be supplied by the shared API/control-plane boundary. Discord recovery actions require the configured operator authorization.

Protected POSTs use `X-Control-Plane-Token` matching the server's shared `CONTROL_PLANE_APPROVAL_TOKEN`. This also applies to `POST /api/v1/copilot/answer` and `POST /api/v1/triage/run`, even in heuristic/no-LLM mode: missing server configuration returns `503`, and missing/wrong caller credentials return `401` before platform or provider work. Keep the token in server-side configuration, never `NEXT_PUBLIC_*` or browser requests. The shared Python client and Next.js proxy reject redirects; do not route credentials through a redirect. Compose publishes host ports on `127.0.0.1` only, without changing internal service addresses. See the [API and availability contracts](local-operations.md#fastapi).

## Lifecycle

1. **Review:** Open recovery for an unresolved incident and fetch a fresh preview. The operator reviews the scope, source snapshot, runbook, targets, blockers, validation, and rollback details.
2. **Propose:** Submit the preview's incident revision and a reason. The service re-reads evidence and creates a deterministic immutable plan plus a system-requested `recover_partition` approval in the control store. A repeated matching request returns the existing plan rather than creating another scope.
3. **Decide:** A human operator approves or rejects the exact request. The UI prevents the requester from approving their own request. Approval applies to a specific plan hash and is versioned; it does not execute the plan.
4. **Confirm and dispatch:** The operator explicitly confirms the same scope. The API checks the plan hash, approval version/status, actor, incident revision, contracts, model, source manifest, and current warehouse facts. It claims the plan once and persists execution/dispatch intent before contacting Airflow.
5. **Execute:** The service submits one deterministic manual run to `90_05_dag_dq_incident_recovery`. That DAG accepts only an execution UUID and calls `agent.recovery.runtime`; it does not accept caller-supplied commands or targets. Runtime revalidates the immutable plan and approval ownership, reads the exact pinned source, builds only the approved date/table scope, validates staged output, and publishes through the partition publication protocol.
6. **Verify:** Runtime captures scoped quality/revalidation evidence and stores execution and publication outcomes. Execution success alone is not a business-data PASS. A failed or uncertain outcome does not grant permission to replay the write; inspect the existing execution and publication journal.
7. **Close separately:** A recovery validation PASS does not close the incident. Use the existing Resolve workflow for a fresh closure check and explicit human closure decision. Recovery itself never changes lifecycle status to resolved.

## Airflow Roles

| DAG | Schedule and purpose |
|---|---|
| `90_05_dag_dq_incident_recovery` | Manual, one run per approved execution, retry-free. Runs only the existing immutable plan. |
| `90_06_dag_dq_recovery_reconciliation` | One-minute schedule by default, `catchup=False`, one active run, and paused on creation. It remains paused, not currently enabled. The acceptance flag changes its schedule to `None`. When deliberately run, it reads existing Airflow run state and reconciles control/outbox records; it never triggers, clears, retries, or repairs a warehouse job and never calls a model. |
| `90_07_dag_dq_recovery_acceptance` | Manual synthetic-fixture preparation, not a recovery DAG. Its default case is `source_invalid`, which checks that invalid source is rejected before warehouse writes. Other fixed cases prepare isolated, owned synthetic dates and may write fixture data; they are not an operator repair path. Existing non-empty scopes are refused. |

An unknown dispatch response is reconciled against the deterministic existing DAG run ID; the dispatcher does not issue a second POST merely because the first response timed out. Reconciliation confirms transport/run state only. A terminal Airflow success without stored validation remains unknown rather than being promoted to a data PASS.

`DQ_ACCEPTANCE_MANUAL_ONLY=false` is the normal default. Temporary `true` makes only DAG `00` and reconciler `90_06` manual (`schedule=None`); it neither pauses DAGs nor cancels existing work. For a controlled session, pause both, account for active runs, recreate Airflow with the temporary flag, and verify both effective schedules before any manual enablement. After work finishes, pause both **before** restoring `false`, recreate/reparse, and verify normal schedules with both still paused. Do not use a brief cron enable/disable window. Follow the [full temporary override and restore procedure](local-operations.md#temporary-manual-only-acceptance).

## Publication And Recovery Outbox

`pipelines/common/publication.py` stages the scoped partition work under a publication lock and checks the approved revision, watermark, and model hash before publication. Recovery runs provide a validator that checks pinned-source equivalence, active DQ contracts, required segment coverage, and mart aggregates before live publication. Publication records distinguish prepared, publishing, published, unknown, compensating, compensated, and aborted outcomes. Compensation, where performed by the publication protocol, is scoped to backups owned by that execution; an unknown publication outcome is not an automatic retry signal.

Recovery plan/execution events and delivery projections use the PostgreSQL outbox. Projection/audit/delivery status is separate from the warehouse publication and incident closure. Outbox delivery completion does not prove data quality, and successful data publication does not prove that all observability sinks have synchronized.

dbt artifacts remain execution-versioned. For older lineage consumers, `latest/manifest.json` is a bounded **lineage-only view**, not a mutable source snapshot or the original compile artifact. The uploader verifies the original versioned bytes/SHA-256, normalizes only the known isolated orders relations back to logical `dq.*` names, removes compiled SQL/environment metadata, and records the original URI, checksum, execution, model, and partition provenance. A PostgreSQL lock serializes discovery updates and prevents an older upload from replacing a newer one.

Pipeline observability distinguishes `recovery_publication_*` from `recovery_verification_*`. A successful publication records business validation as `not_checked`; a separate verification record carries PASS or a blocked result. Neither is an incident-closure decision.

## Reconciliation And Observability

The `90_06` reconciler is paused and not currently enabled; do not assume background reconciliation or projection delivery is running. Its implementation inspects only allowlisted DAG/run identities through the Airflow API, updates durable execution/approval state from existing runs, and flushes pending approval/recovery projections. Missing runs, mismatched execution IDs, missing verification evidence, or incomplete child-run outcomes stay unknown or pending for operator inspection. Reconciliation does not clear or retry runs and does not infer business validation from scheduler state.

The execution record separates `status`, `trigger_state`, and `validation_status`; it also retains the deterministic DAG run ID, actor, plan hash, validation result, error code, finish time, and synchronization-pending state. PostgreSQL publication records retain per-table publication and compensation state. These are control/observability records, not a claim that a live environment has completed a recovery.

Daily summary `availability=available|empty` is separate from those execution states. An empty HTTP `200` response means the request succeeded without check/open-alert data, not that recovery passed or the warehouse is healthy. An explicit date remains in `dt`; only an omitted-date request with no latest DQ date returns `dt=null`. Dependency failures remain errors.

## Schema Diagram And Sources

The generated [recovery/publication ERD](diagrams/erd-recovery-publication.svg) shows only declared PostgreSQL foreign keys among its eight displayed entities. It draws the execution-to-plan FK and the composite publication-to-revision FK `(dataset, partition_dt)`. In-scope declared FKs omitted because their target entities are outside this eight-entity view are `recovery_plans.alert_key` to `incident_controls`, `recovery_plans.approval_request_id` to `approval_requests`, and the `request_id` FKs from `approval_legacy_imports` and `approval_projection_outbox` to `approval_requests`. `source_snapshots` and `recovery_outbox` have no declared foreign key in these migrations; the diagram invents none.

The catalog is generated from the checked-in init SQL. At this revision it contains 36 database objects (20 PostgreSQL tables, 15 ClickHouse tables, and one ClickHouse view), 460 columns, and 11 declared PostgreSQL foreign keys. Migration 09 adds source snapshots, recovery control records, and recovery-outbox delivery fields; migration 10 defines partition publication state including `aborted`; migration 11 adds legacy approval import/projection support. These counts describe the source catalog, not a live database inspection.

Relevant implementation sources:

- `agent/recovery/contracts.py`, `agent/recovery/evidence.py`, and `agent/recovery/service.py` define eligibility, pinned facts, plan creation, approval checks, and dispatch.
- `agent/recovery/runtime.py` owns approved execution and scoped verification.
- `agent/recovery/reconciliation.py` and `apps/common/recovery_airflow.py` inspect existing Airflow runs without retrying writes.
- `pipelines/seeding/snapshots.py` validates and reads immutable source snapshots.
- `pipelines/common/publication.py` owns staged partition publication and compensation records.
- `infra/init/postgres/09_evidence_driven_recovery.sql`, `10_partition_publication.sql`, and `11_legacy_approval_control.sql` define recovery storage contracts.

## Upgrade And Operation

For a fresh stack, `approval-control-init` applies the ordered idempotent SQL files, including migrations 09-11. An existing stack requires a deliberate control-store upgrade:

1. Stop old approval writers only after checking that no dispatch, model call, or publication has an uncertain active outcome. Do not interrupt an unknown write and replay it.
2. Back up the control store, then run `docker compose --env-file infra/.env -f infra/docker-compose.yml run --rm --no-deps approval-control-init`. Do not reset volumes.
3. Inspect `docker exec dq_runner python scripts/migrate_legacy_approvals.py` in dry-run mode. Only after all old writers are quiesced and blockers are cleared, use `--apply --writers-quiesced`. This preserves available history, not versions already removed by historical ClickHouse merges.
4. Recreate only the changed application services. Preserve environment secrets, configured Discord guild/channels, and the owner's separate runner/API Gemini settings.
5. Inspect the new DAG imports and keep `90_06_dag_dq_recovery_reconciliation` paused. It is not currently enabled; any manual reconciliation or later scheduled activation requires a separate operational decision and inspection of existing runs. Use the manual-only procedure above for a controlled session, then restore both `00` and `90_06` paused.
6. Open an incident, choose **Review Fix**, review the pinned source and scope, approve the plan, then separately confirm **Run Approved Fix**. Track the existing execution rather than submitting a replacement after an uncertain response.

Server-side Airflow configuration reuses `AIRFLOW_API_BASE_URL`, `AIRFLOW_API_USERNAME`, and `AIRFLOW_API_PASSWORD`. Protected writes and both investigation POST routes require the shared token described above. Keep credentials in local server-side configuration, never in plans, reports, browser variables, or source control. Resolve/recovery are not exposed as model or MCP mutation tools.

No data is regenerated during migration. An older incident without a registered, valid saved source remains **Source unavailable**. `90_07` owns isolated synthetic fixture dates; it is not a source backfiller for old incidents.

Storage readiness must cover physical-volume slots as well as free bytes. The pre-fix SeaweedFS PUT `500` exhausted 20 slots despite free disk; Compose now specifies `-max=64`. The [v1 ledger](datasentry-v1-acceptance.md) records subsequent successful evidence writes and fresh schema PASS after UTC/default contract alignment, with failed runs and false alerts retained. See [volume capacity](local-operations.md#seaweedfs-volume-capacity).

## Verification Status

The [historical ledger](evidence-driven-recovery-acceptance.md) and [fresh v1 ledger](datasentry-v1-acceptance.md) are separate evidence sets. Fresh v1 work exercises the full daily flow, all three runbooks, cross-interface decisions, scoped closure, reconciliation, and a bounded Gemini campaign. Inspected Airflow DagRuns and business results are recorded separately; local runtime success does not substitute for final candidate bootstrap or remote CI.

Deterministic publication/compensation and concurrency tests are not live fault-injection or two-human evidence. Those checks remain pending wherever the ledger says so. Direct writes outside the platform remain a coordination limit. The `90_07` fixture DAG is a separate simulation path and is never called by the recovery runtime.

For a future real demo, follow the [operator recording runbook](local-operations.md#future-live-demo-recording). Capture one traceable incident through review, approval, separate execution, fresh check, and explicit closure, or show the actual blocker. The current video is historical and does not demonstrate the new v1 workflow; no new recording is claimed.
