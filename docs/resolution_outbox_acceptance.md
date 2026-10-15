# Resolution Outbox Recovery Acceptance

## What This Proves

An explicit manual fixture run committed one evidence-gated closure, retained a deliberate application failure before Discord delivery, and replayed the same outbox event. PostgreSQL, ClickHouse, the shared API, the child Airflow check, and Discord were real services, not mocked acceptance dependencies.

This is **not a real Discord outage test**. The injected exception occurs before the HTTP call. It proves recovery from that application-delivery failure; it does not certify every transport timeout, lost acknowledgement, provider outage, or concurrent human decision.

The fixture preparation and restoration alter only the reserved column on the owned `dq_resolution_acceptance.schema_fixture`. They are explicit setup operations, separate from read-only revalidation and closure. No Orders schema/data, dbt, backfill, remediation, or external LLM operation was performed.

## Reproduce Safely

Use the API and Discord profiles with the existing approval token, configured bot/channel IDs, and `EXTERNAL_LLM_ENABLED=false` in `dq_runner`. The acceptance runner uses the internal Compose API URL by default; it never falls back to local request creation when that service fails.

On a fresh installation, create the dedicated owned fixture explicitly:

```text
docker exec dq_runner python scripts/prepare_resolution_schema_fixture.py --mode clean
```

This command creates/changes only the named fixture after validating its ownership marker. Do not run it while a fixture acceptance DAG is active. Do not point the demo stack at a production warehouse.

Generate an acceptance UUID in PowerShell:

```powershell
[guid]::NewGuid().ToString()
```

Unpause and manually trigger `90_04_dag_dq_resolution_outbox_acceptance` in Airflow with a new, unused fixture correlation date and this configuration:

```json
{
  "acceptance_run_id": "YOUR_GENERATED_UUID",
  "business_date": "YYYY-MM-DD",
  "confirm_fixture_closure": true
}
```

The confirmation is false by default. Setting it true authorizes the delegated synthetic closure; it does not waive fresh PASS, scope, contract, revision, watermark, or remediation checks. This is a trusted local administrative executor, not verified individual SSO and not a Resolve tool available to an LLM or MCP client.

The fixed stages are prepare, capture, restore, request, wait, close_fault, replay, and verify. The warning column carries its run UUID in ClickHouse metadata. Cleanup can reconcile a completed ALTER whose manifest update was interrupted, but refuses a column owned by another run. The manifest lives under ignored `data/acceptance/resolution_outbox/`; it contains metadata and hashes, not credentials or raw operator reasons.

Retries are disabled. After an interruption, inspect existing task/request/control state. Do not recreate the DagRun or reset completed side effects. An already committed closure can be replayed idempotently even after the original pre-confirmation proof expires; a new closure still requires fresh proof. Never reset completed decisions or outbox flags to manufacture a recovery case.

Read retained logs:

```text
docker exec dq_airflow_api_server python /opt/airflow/project/scripts/read_airflow_validation_logs.py --dag-id 90_04_dag_dq_resolution_outbox_acceptance --run-id YOUR_RUN_ID
```

## Observed Run, 2026-10-13

Parent DAG: `90_04_dag_dq_resolution_outbox_acceptance`.

Parent run: `manual__outbox_fixture_live_20261012T231705`.

Acceptance UUID: `20521b39-f196-4095-846b-35e488a537c9`.

Incident: `DQ-20261013-1C69EA`.

Revalidation request: `aae3a08f-0ebc-4c2e-9b38-657e470d1e92`.

Child run: `manual__incident_revalidation_aae3a08f-0ebc-4c2e-9b38-657e470d1e92`, DAG `90_03_dag_dq_incident_revalidation`. All six child tasks succeeded and the business result was PASS with zero schema findings. This is separate from mere DagRun success.

Every parent task finished `success`:

| Task | Final attempt |
| --- | ---: |
| `t00_start` | 1 |
| `t10_prepare_fixture` | 1 |
| `t20_detect_fixture_alert` | 1 |
| `t30_restore_fixture_contract` | 1 |
| `t40_request_readonly_check` | 1 |
| `t50_wait_for_fresh_pass` | 2 |
| `t60_confirm_with_delivery_fault` | 2 |
| `t70_replay_pending_outbox` | 1 |
| `t80_verify_recovery` | 1 |
| `t90_finish` | 1 |

Two real adapter defects were found and fixed rather than hidden. The first wait attempt incorrectly compared the human-readable `observed` string to numeric zero; it now validates structured PASS checks and evidence. The first close attempt committed and injected the planned fault, then failed audit recording because its idempotency key was not a SHA-256 digest. It now uses the shared audit key builder.

Only the failed task and not-yet-run downstream tasks were explicitly cleared through the Airflow v2 API, with exact DagRun/task IDs and a dry-run scope check first. Preparation, detection, restoration, and request creation were not repeated. Attempt-one logs remain retained. This is an explicitly repaired/resumed acceptance run, not a claim of a clean first attempt.

## Durable Recovery Evidence

Closure event `994ffd4f-751c-4821-9ab1-4efc09d1e9a3` was committed at `2026-10-12T23:22:21.811480Z`, within the fresh PASS window. Before replay:

- Incident status was resolved, revision 2, with exactly one decision.
- Projection and audit flags were true; delivery was false.
- Error code was `InjectedFixtureDeliveryFault`.
- Discord card remained OPEN on message `1559344212544069734`.

The `fixture_delivery_fault_observed` ClickHouse audit records those pending flags and the same event/message IDs. It is durable evidence, not merely a manifest assertion.

After replay:

- The same event had all three sink flags true and an empty error code.
- Exactly one decision and revision 2 remained.
- Shared API preview reported `resolved` and `synchronization_pending=false`.
- Discord message `1559344212544069734` rendered RESOLVED, still authored by bot `1476800539747553463`, with no webhook identity.
- The footer used system-check provenance, not Gemini.
- Previous fixture control, decision, and outbox contents matched their pre-run hashes; evidence bounds fail closed rather than silently truncating.
- No `copilot_answer` or `llm_route_completed` audit exists for this fixture. External model calls and estimated provider cost were zero.

The default-confirmation negative run `manual__outbox_confirmation_rejected_20261012T230011` intentionally failed before fixture writes. No manifest was created and the original two-column fixture remained intact. It is rejection evidence, not a successful positive acceptance.

The acceptance DAG was returned to paused after the run. Personal Gemini configuration in the API was unchanged. Remaining human/mobile acceptance is tracked separately in [the workspace ledger](discord_workspace_acceptance.md).

## Post-Change Regression

Fresh DAG `91_dag_dq_platform_validation` runs after recovery hardening and DQ snapshot isolation were inspected:

| Run | Result | Readiness |
| --- | --- | --- |
| `manual__dq_snapshot_isolation_api_20261012T235420` | 312 passed | 27/27 |
| `manual__dq_snapshot_isolation_ui_20261012T235420` | 276 passed | 27/27 |
| `manual__dq_snapshot_isolation_all_20261012T235420` | 1,975 passed, 12 skipped, 10 warnings | 27/27 |
| `manual__desktop_docs_final_all_20261013T000807` | 1,975 passed, 12 skipped, 10 warnings | 27/27 |

Each DagRun and all five tasks succeeded on attempt one. Retained pytest and platform readiness logs were inspected. The deliberate parent fault and its repaired attempts remain separate evidence, not erased by these passing regressions. Mobile/narrow visual checks were deferred by the owner on 2026-10-13; two-human live context/race checks remain pending.
