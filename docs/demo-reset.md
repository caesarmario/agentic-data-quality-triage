# Destructive Local Demo Reset

This maintenance workflow intentionally removes old synthetic data and runtime history. It is not a recovery runbook, model tool, Discord command, or production repair. **No data backup is made.** Keep a separate backup yourself if you need the old data.

## Scope

- Only Docker project `agentic-dq-triage`: eight named volumes and anonymous volumes attached exclusively to its verified containers. Other projects, images, and global Docker caches are not purged.
- Generated workspace data, logs, dbt artifacts, temporary output, and Python/test/build caches. Tracked source, installed dependencies, migrations, core tests, configuration, Airflow auth files, and documentary media are retained.
- Only DataSentry bot `1476800539747553463` messages in guild `1557744199745540137`, channels `1557750137672179732`, `1557943799072694282`, and `1557943838067261560`. Human messages, channels, permissions, and thread containers are preserved. Accessible public threads and joined private threads are inspected; inaccessible private conversations are not claimed as scanned. A bot-owned thread starter is blocked rather than orphaned.

The host controller checks volume ownership/shared users, workspace identity, tracked files, absolute paths, and reparse points. An internal generated Airflow log `latest` link is unlinked without following it; links outside the approved output tree block removal. Unknown recovery, publication, approval, investigation, or model-request outcomes block reset.

## Preview First

From the repository root on a host with Python 3.11+, Docker CLI, and the existing stack running:

```powershell
py -3 scripts/reset_local_demo.py --dry-run
py -3 scripts/reset_local_demo.py --write-plan
```

Bash uses `python3` instead of `py -3`. Default execution only inventories; it does not pause services or delete data. `--write-plan` places an identities/counts-only manifest in the operating system temporary directory, outside the folders being erased. It contains no token, password, message text, report body, or source-data backup.

Review the exact manifest and its SHA-256. Obtain explicit permanent-deletion confirmation before applying:

```text
python scripts/reset_local_demo.py --apply --plan <temporary-plan-path> --confirm-plan <exact-manifest-hash>
```

Apply is host-side: it must not run inside a container it will remove. It drains the shared API, pauses scheduled DAGs, checks work is terminal, verifies Docker and Discord targets again, deletes only approved bot messages, removes approved containers/volumes/generated output, and bootstraps from repository definitions. Authentication files and provider settings remain unchanged. Source generation and external LLM calls do not run.

If any step fails, inspect the phase journal beside the temporary manifest. A completed phase is not repeated; resume uses the same manifest and hash. Never widen targets, reset unknown executions, or blindly trigger another validation. A changed Discord/Docker target set needs a new preview before destructive work. There is no data rollback after deletion.

## Expected Result

- Business tables, source snapshots, reports, checkpoints, approvals, recovery records, and incidents are empty.
- New read-only inspection audit is retained, not deleted to make a check green. Empty-demo acceptance rejects every audit event older than the final run's logical date and every action outside the explicit alert-list/daily-summary read allowlist. Provider, mutation, and unknown audit events are not allowed.
- Five empty S3 buckets exist and schema/migrations are initialized; no orders or historical source is generated.
- Daily orchestrator and recovery reconciler are paused before workers/scheduler start.
- Exactly one English welcome exists, with a new registry/message identity; human history remains.
- Exactly one final DAG 91 run remains, with `validation_suite=all`, `require_api=true`, and `expect_empty_demo=true`. Readiness reports services present and data **empty**, not a fabricated healthy dataset.
- New startup/validation logs and a minimal `logs/maintenance/reset-summary.json` receipt remain. Old acceptance logs/captures are gone; version-controlled ledgers and media are historical evidence, not live data backups.

The final receipt records actual run/task states and scope metadata. Inspect retained pytest/readiness logs before calling the reset acceptance-tested. No automatic paid provider test, remediation, commit, or push is part of reset.

In this mode DAG 91 supplies its own logical date as `--empty-demo-audit-since`; without that explicit boundary the standalone CLI still requires an empty audit table. Retrying a failed validation task retains its earlier log and reuses the same DagRun; it does not reset data or create another run.

## Source Cleanup Review

The current dependency audit found references for every existing Python helper through imports, DAG commands, Makefile, CI, docs, registries, media tooling, or tests. No helper was removed merely because its name contains `test`, `smoke`, or `acceptance`. Seventeen redundant section-marker comments were removed from three administrative scripts with identical pre/post ASTs; public docstrings, safety comments, and attribution were preserved.

## Verified Reset - 16 October 2026

Permanent deletion completed against approved manifest `7686a077f8bb729bd0d4bb80c321280fd0f95ee431ddc813446324a945fd9d1f`. No backup was made.

Removed: 23 project containers and their 12 approved volumes (eight named, four exclusive anonymous); 262 S3 objects; 49 checkpoints; old Airflow/control/warehouse history; and 41 generated-output roots (preview: 17,505 files, approximately 1.49 GiB). The last quiescent probe counted 752 old DagRuns. Twenty-two DataSentry messages were deleted. One human message, all channels/permissions, the old server, credentials, dirty source, installed dependencies, tests, migrations, and tracked media were preserved. No Python source was proved orphaned, so none was deleted.

All four init containers exited `0`. API, Streamlit, CH-UI, and the optional web service were healthy. The daily orchestrator and minute reconciler remained paused. Empty checks found only the application `dq` database, no publication/acceptance scratch databases, zero business/control rows, zero checkpoints, and five empty buckets. The sole control-store exception is the new welcome registry. New read-only audit evidence remains intentionally retained.

### Airflow Evidence

Pre-reset regression `manual__demo_reset_preflight_all_20261016` passed all five tasks: 2,514 passed, 12 skipped, 10 warnings, readiness 27/27. Its raw logs were then intentionally erased with the old runtime; this paragraph is a historical summary.

The fresh runtime contains exactly one DagRun:

- DAG: `91_dag_dq_platform_validation`
- Run: `manual__fresh_demo_validation_fa3fe4a204bb`
- Final state: **success**
- Final pytest result: **2,521 passed, 12 skipped, 10 warnings** in 38.84 seconds.
- Empty-demo readiness: **24/24**, with zero unexpected audit events.

| Task | Final state | Attempt |
| --- | --- | ---: |
| `t00_start` | success | 1 |
| `t10_run_named_pytest_suite` | success | 2 |
| `t20_run_platform_readiness` | success | 2 |
| `t30_emit_validation_summary` | success | 1 |
| `t90_finish` | success | 1 |

The first readiness attempt failed because the original empty check also rejected new read-only inspection audits. No audit was deleted. After adding an explicit timestamp boundary and action/tool allowlist, pytest and readiness were cleared through Airflow's public API and rerun in the **same** DagRun. Both attempts' logs remain under `logs/airflow/dag_id=91_dag_dq_platform_validation/run_id=manual__fresh_demo_validation_fa3fe4a204bb/`.

The 12 skips require Node helpers unavailable in the Python runner; ten deprecation warnings concern `audioop` and Discord modal labels. They are not paid-provider, recovery, or production acceptance. The API shell launcher was corrected to `exec` Uvicorn after a graceful-stop signal was found not reaching the child process; no unknown provider request was killed.

### Operator Read-Back

- Discord REST and the real desktop client showed welcome `1560502391277617183` from the existing green bot. The human message remained; alerts and ops had no DataSentry messages. Stale ephemeral help disappeared on client navigation.
- Streamlit defaulted to English, showed zero active/critical incidents, and offered no old incident/report. With no dataset, the daily snapshot remains unavailable; this is not a warehouse-health PASS. Its existing optional error expander can show HTTP 400 for the missing latest business date.
- Runner external LLM remained `false`; API personal testing remained `true`, with the configured key still present. All retained inspection events matched the read-only allowlist; no model call, dataset generation, recovery, or remediation ran.
- The manifest and phase journal were removed after success. The minimal receipt and sanitized desktop screenshots are in ignored `logs/maintenance/`.

Mobile/narrow, two-human isolation, and full crash/fault campaigns remain deferred as before. Inaccessible private Discord threads were not claimed as inspected. No commit or push was performed.
