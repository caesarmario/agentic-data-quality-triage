# DataSentry v1: Fresh Local Acceptance

This record covers the local desktop POC on 16-18 October 2026, after the fresh-demo reset. New data and evidence were retained. Earlier acceptance ledgers remain historical; they are not proof that pre-reset rows still exist.

Job success, quality PASS, approval, execution, and incident closure are separate results. None of the checks below establishes production certification or statistical diagnosis accuracy.

## Local Security And Availability

- All published project ports bind to `127.0.0.1`; Docker-internal service addresses are unchanged.
- Copilot and triage POSTs require the shared server-side token before evidence collection or model execution. Live missing/wrong-token probes returned `401`; missing server configuration and authorized delegation have focused tests.
- Python control-plane requests do not follow redirects. The Next.js server proxy supplies its token server-side and rejects redirects/cross-origin mutations. A scan of all 16 built client assets found no configured token value.
- Empty latest daily summaries return `200`, `status=success`, `availability=empty`, `dt=null`, and zero aggregates. This means no observations, not healthy data. English and Indonesian empty states were inspected before acceptance data was created.
- The owner's environment and credentials were preserved. API personal Gemini remained enabled; routine runner external LLM remained disabled.

The source/history scan covered 586 candidate files and 1,062 reachable Git blobs at the pre-commit scan. Token-shaped findings were synthetic sanitizer fixtures. Other matches were the existing public local-demo JWT default and an authentication-file path, not newly disclosed credentials. Public defaults and the trusted-local operator model are unsuitable for exposed deployment. This is a bounded scan, not a security certification.

## Airflow Regression

All runs in this table use `91_dag_dq_platform_validation`. Successful runs completed these five tasks: `t00_start`, `t10_run_named_pytest_suite`, `t20_run_platform_readiness`, `t30_emit_validation_summary`, and `t90_finish`.

| Run ID | Scope / inspected result |
| --- | --- |
| `manual__release_v1_api_20261016` | Success; 401 passed; readiness 36/36. |
| `manual__release_v1_ui_20261016` | Success; 289 passed; readiness 36/36. |
| `manual__release_v1_discord_20261016` | Success; 757 passed, 10 warnings; readiness 36/36. |
| `manual__release_v1_airflow_20261016` | Success; 215 passed; readiness 36/36. |
| `manual__release_v1_all_20261016` | Success; 2,679 passed, 12 skipped, 10 warnings; readiness 36/36. |
| `manual__release_v1_all_reviewed_20261016` | Retained failure: pytest passed, but a web restart overlapped readiness. This is not recorded as a passing release gate. |
| `manual__release_v1_core_gate_20261016` | Success; stable pre-data core/web gate. Representative API readiness was required again once lineage artifacts existed. |
| `manual__release_v1_safety_corrected_20261016` | Success; 2,692 passed, 12 skipped, 10 warnings; readiness 39/39. |
| `manual__release_v1_current_context_gate_20261016` | Success; 2,698 passed, 12 skipped, 10 warnings; readiness 39/39; every task attempt one. |
| `manual__release_v1_precommit_all_20261016` | Success; 2,713 passed, 12 skipped, 10 warnings; readiness 39/39. |
| `manual__release_v1_reviewed_precommit_all_20261016` | Retained interrupted run; not a completed regression gate. |
| `manual__release_v1_reviewed_precommit_all_20261017` | Retained failure: 2,714 passed and one obsolete assertion still expected a model-selected action instead of the enforced deterministic recommendation. |
| `manual__release_v1_policy_precommit_all_20261017` | Success after updating that assertion; 2,715 passed, 12 skipped, 10 warnings; readiness 39/39; all five tasks on attempt one. |

`manual__release_v1_hermetic_precommit_20261018` subsequently completed all five tasks on attempt one: 2,715 passed, 12 skipped, 10 warnings; readiness 39/39. This verifies credential-erasing test defaults on the main stack; it is not a substitute for the corrected fresh clone.

The 12 main-runner skips are Node-dependent capture-helper tests, not skipped recovery checks. Node-enabled validation separately exercises those helpers. Known warnings concern Python `audioop` and Discord component-label deprecations. CI has an exact allowlist for service-backed skips and requires all 12 Node helpers; unexpected skips and failures fail its gate. Only sanitized summaries, fixed warning categories, and allowlisted skip names are uploaded, not raw diagnostics.

Retained logs are under `logs/airflow/dag_id=<DAG>/run_id=<run>/task_id=<task>/attempt=<n>.log`. Final candidate SHA, clean-clone/bootstrap results, post-commit regression, remote CI URL, and read-back belong to the release handoff; they must not be inferred from an earlier green run in this table.

## Fresh Daily Flow

Empty-scope inventory preceded generation. The older baseline business date was **2026-10-15**; the controlled incident date was **2026-10-16**. Automatic triage was disabled during both deterministic flows.

| DAG / run | Result |
| --- | --- |
| `95_dag_dq_metadata_registry_sync` / `manual__release_v1_metadata_20261016` | Metadata bootstrap succeeded. It populated metadata before the corrected stable gate; no orders or provider call occurred at that point. |
| `00_dag_dq_platform_daily_orchestrator` / `manual__release_v1_case_a_20261016` | Success; 13 successful tasks and intentionally skipped triage. Landing, seed, load, dbt, and quality child DAGs succeeded. |
| `00_dag_dq_platform_daily_orchestrator` / `manual__release_v1_case_b_20261016` | Success; the same intentional triage skip. Quality child retained a failed S3 evidence-write attempt before a successful infrastructure retry. |
| `96_dag_dq_schema_drift_detection` / `manual__release_v1_schema_corrected_20261016` | All five tasks succeeded; current orders schema PASS after contract/DDL alignment. |

Baseline raw/staging contained **2,756 business rows**; mart contained all **25 exact country/channel pairs**, with no unknown pair. The baseline DQ run had 17 PASS and one insufficient-history anomaly SKIP. Both fresh dbt child runs reported **23 PASS, zero WARN/ERROR/SKIP**. Historical January fixture freshness is a separate limitation.

The controlled date had **3,052 trusted upstream rows**, but landing/raw/staging had **2,963**. Exact comparison identified the missing pair **MY / direct**, with no unexpected pair. The incident was `DQ-20261016-4FC07C`. A row count of 24 alone was not treated as proof of which pair was missing.

The upstream object was already stored before fault delivery. Recovery pinned SHA-256 `0f7e8cf4eab6d570d598952194acaf52e183cf4e4c4b6e555bb896f239d0987d`. No historical source was reconstructed and the generator was not called to repair this partition.

### Defects Found Rather Than Hidden

- Bootstrap uses UTC timestamp types/defaults. The owned schema contract omitted those details, producing two false schema incidents. The contract was aligned with all 63 bootstrap columns; checker strictness and DDL were not changed. Fresh scoped schema checks passed before closing those incidents. Their messages/history remain.
- SeaweedFS had exhausted its 20 physical-volume slots despite free disk space. Five bucket collections can each grow seven volumes by default. The local slot limit became 64; existing data volumes were retained. Failed evidence writes remain in Airflow logs. See the [official S3 FAQ](https://github.com/seaweedfs/seaweedfs/wiki/S3-API-FAQ).
- Raw/derived fixture setup stopped during those evidence-write failures. Existing source snapshots were reused; `manual__release_v1_raw_checks_20261016` and `manual__release_v1_derived_checks_20261016` completed quality/notification stages without regenerating the fixtures.
- The local stack later stopped together during an interrupted session. No OOM flag was reported; the cause was not established. Storage/control state was checked before workers restarted, with no active/unknown recovery or unresolved publication.

## Recovery And Human Decisions

Each execution below used `90_05_dag_dq_incident_recovery`, with `t00_start`, `t20_execute_approved_plan`, and `t90_finish` successful on attempt one. Business verification was independently **PASS**.

| Runbook | Incident / date | Execution ID |
| --- | --- | --- |
| `replay_raw_partition` | `DQ-20260111-93B34C` / 2026-01-11 | `a93942a8-912b-5a05-834e-b44a145afb8c` |
| `rebuild_derived_partition` | `DQ-20260112-5236BA` / 2026-01-12 | `599546dc-a94a-5fab-bced-79a36218a4b4` |
| `replay_and_rebuild_partition` | `DQ-20261016-4FC07C` / 2026-10-16 | `58d9e170-0a54-53bb-93f6-e0cbcbf0e46b` |

The deterministic DagRun identity is `manual__incident_recovery_<execution ID>`. Source-invalid fixture run `manual__release_v1_invalid_source_20261016` in DAG `90_07` succeeded with business **blocked** and zero warehouse writes. Fixture creation is simulation, not recovery.

Raw-only restored 2,535 business rows; derived-only retained 2,815 raw rows matching its trusted source. Combined recovery restored **89 missing rows**, preserved every healthy landing business row, and produced raw/staging multiset fingerprints equal to the 3,052-row upstream. The mart then contained 25 expected pairs, with none missing/unexpected. Timestamp/audit ingestion changes are not confused with changes to business values.

### Live, Owner-Delegated Desktop Actions

1. Discord Resolve returned **BLOCKED, 24/25** before recovery. It did not close or repair the incident.
2. A Discord reason modal and final confirmation approved plan `55ccc517-7ce2-5740-be19-ed2179f581ce`, approval `APR-1CB15B93A72166C4F538`, in the private operations workflow. Read-back showed **approved / not_started**, **zero executions**, and **24 mart segments**.
3. A separate Streamlit scope checkbox and **Run Approved Fix** created execution `58d9e170-0a54-53bb-93f6-e0cbcbf0e46b`. Execution succeeded and scoped verification passed; the incident remained active.
4. Streamlit closed the raw-only incident after new request `3c35461c-e65f-4d6a-8680-e1f871380bc1` passed with 2,535 rows, followed by an operator reason, checkbox, and final confirmation. It retained message `1560572763867975722`.
5. Discord closed the daily incident after request `afa9caec-a204-414e-a675-839622d5bc3f` passed **25/25**, followed by a reason modal and **Confirm Close**. Closure was stored at `2026-10-16T15:02:05Z`. Message **`1560572678551769149`** was retained from OPEN through TRIAGED to RESOLVED, with bot author **`1476800539747553463`** and transport `discord_bot_api`.

These are actual UI interactions using the owner's delegated session, not two-human evidence. Resolve did not execute a repair or call Gemini. Closure footers use system-check provenance; historical AI reports remain available.

On 17 October, **Open saved investigation report** displayed the unchanged `RPT-64F74018` from the resolved incident, with its recorded-analysis label, copyable storage references, and download control. This read did not start another investigation. Desktop light and dark rendering were inspected, then English and the original system-theme preference were restored.

## Replay And Reconciliation

`90_04_dag_dq_resolution_outbox_acceptance` / `manual__release_v1_outbox_bootstrapped_20261016` completed all 10 tasks on attempt one. Its isolated schema fixture was restored, one fresh PASS request produced one closure decision, and the same pending event was replayed successfully.

- Fixture incident: `DQ-20261014-F23640`.
- Request: `3bfe0ba7-f238-419c-ae52-e5585a3619c6`.
- Event: `277e260f-0789-4123-bece-0f3e87e58f6c`.
- Message: `1560660315899691151`, same bot author before/after replay.
- Projection, audit, and delivery all completed; decision count remained one.
- Fault was injected **before** the Discord call. A real Discord outage, crash campaign, or human concurrency test is not claimed.

The earlier run `manual__release_v1_outbox_20261016` failed safely because the post-reset fixture table did not yet exist; no owned preparation intent was available for cleanup. Explicit fixture bootstrap preceded the successful run. Orders tables were not altered.

The administrative reconciler also drains bounded existing incident projection/delivery intents. It does not create a new decision, start a recovery, or retry a warehouse mutation. Schedules are restored paused after release acceptance rather than left running to hide pending work.

`90_06` / `manual__release_v1_incident_outbox_replay_20261016` completed all three tasks on attempt one. Five existing detection-delivery intents drained; pending incident and recovery outboxes became zero. The existing three recovery executions and five closure decisions were preserved.

## Bounded Gemini Campaign

Before the first request, the official [model catalog](https://ai.google.dev/gemini-api/docs/models) and [pricing](https://ai.google.dev/gemini-api/docs/pricing) were refreshed: `gemini-3.5-flash-lite`, standard USD 0.30 input / USD 2.50 output per million tokens. Costs below are application estimates, not invoices.

| Path / run or correlation | Calls | Tokens | Estimated USD |
| --- | ---: | ---: | ---: |
| DAG 92 / `manual__release_v1_gemini_smoke_20261016` | 1 | 163 | 0.0001281 |
| DAG 40 / `manual__release_v1_gemini_investigation_20261016` | 3 | 27,735 | 0.0103929 |
| DAG 40 / `manual__release_v1_current_context_retest_20261016` | 3 | 28,264 | 0.0105604 |
| Streamlit -> API English Ask / `c3df0358-ac07-4f3e-82bd-9c0a2aa78d5a` | 1 | 10,577 | 0.0033447 |
| Streamlit -> API Indonesian Ask / `07afec05-7a96-4f31-8ac6-8aadbaafb6f6` | 1 | 10,584 | 0.0034194 |
| **Campaign** | **9** | **77,323** | **0.0278455** |

The first investigation completed technically but failed content review: it proposed recovery again after warehouse consistency had been restored. That report, `RPT-3B716B4E`, was retained unchanged. Paid execution stopped while the policy was corrected. The owner explicitly authorized one retest and increased the campaign token ceiling from **64,000 to 96,000**, retaining **10 calls / USD 0.20** and no automatic paid retry. The original token ceiling was not silently exceeded.

The corrected report, `RPT-64F74018`, separates current system-verified consistency from historical detection and recommends fresh scoped read-only revalidation, not another replay/rebuild. Its three actual Gemini routes had no heuristic/provider fallback. The two live Ask responses made the same distinction; Indonesian preference overrode an English question. English preference was restored afterward without another model call.

Final review removed a phrase-based action filter: both model and heuristic investigation actions now use the deterministic context recommendation unconditionally. This prevents a paraphrased broader repair from bypassing the minimum-runbook decision. Existing model explanations remain separate from the application-owned action. Focused tests and the fresh 17 October Airflow regression cover this strengthening; it did not trigger another paid call.

## Fresh-Clone Findings

The first isolated candidate, `f928710`, built fresh Python/Airflow/web images and initialized all four bootstrap jobs successfully. Before validation it had zero business/control rows, objects, checkpoints or DagRuns, with provider/bot credentials absent. Both scheduled DAGs were verified manual-only and paused before workers started.

`manual__cleanclone_f928710_20261017` exposed nine test failures: mocked Gemini paths had supplied only an unrelated provider key, a Discord routing assertion inherited channel IDs, and a local Streamlit unit test inherited API configuration. These were test isolation defects, not reasons to supply real credentials to CI or weaken runtime authorization. The failed logs were retained. Test defaults now erase outbound credentials, and those cases explicitly supply their own doubles and routing configuration.

The separate frozen-video check also exposed a dependency on an ignored prepared-assets cache. Its diagram-copy code now verifies committed, pinned historical SVGs and is tested in a new temporary directory. Offline checks neither require private captures nor modify the tracked provenance manifest. No new video or AI call was produced.

These findings require a follow-up test/tooling correction commit rather than rewriting the two existing scoped commits. The corrected candidate must pass a new isolated clone and post-commit Airflow validation before publication; the first clone is not a passing release gate.

Runtime usage and provider/model evidence were correlated through ClickHouse audit and report artifacts. The live Ask calls went through the API, **not Airflow orchestration**. Help, language changes, artifact reads, recovery execution, revalidation, reconciliation, and closure add no model call. Scenario labels and evaluator ground truth were not supplied as model context.

This is a small functional sample with a disclosed failed review and explicit retest, not evidence of a statistical accuracy improvement.

## Remaining Boundaries

- Desktop-first, local POC; no production-readiness or enterprise identity claim.
- Mobile/narrow application acceptance, two-human context isolation/decisions, and a complete live crash campaign remain deferred.
- PostgreSQL coordinates platform writers; direct external warehouse writers can bypass those locks.
- Partition replacement is atomic per table, not across raw/staging/mart. Unknown publication outcomes require inspection before any repeat write.
- Historical January fixtures intentionally retain freshness/context limitations. Closing one scoped incident does not certify the warehouse.
- The old video remains historical. A new face-camera/live recording has not been produced; follow the [recording runbook](local-operations.md#future-live-demo-recording).
- Main-stack acceptance, isolated clean-clone bootstrap, and remote CI are separate gates. Publication requires the exact tested candidate and a green remote workflow, not merely this document.
