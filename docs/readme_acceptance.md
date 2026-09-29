<!--
README Documentation Acceptance
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# README Documentation Acceptance

## Change scope

Documentation pass dated 2026-09-22, Asia/Bangkok. The project owner requested a
complete English local installation and demonstration guide instead of the old
`TBD` placeholder. This pass changes documentation, adds offline documentation
contract tests, and updates GitHub About metadata. It does not change application
behavior, provider configuration, schedules, permissions, or warehouse data.

The README covers architecture, fresh-clone instructions, PowerShell/Bash setup,
zero-key baseline and incident demos, current DAG numbering, data/artifact lookup,
Discord, optional web/API/MCP interfaces, LLM cost boundaries, approval lifecycles,
multi-agent limitations, testing, troubleshooting, and safe shutdown.

## Local checks

Seven standalone documentation contract checks passed on 2026-09-22:

1. Repository-relative Markdown and HTML links resolve.
2. Navigation anchors have matching headings.
3. Documented full DAG IDs have explicit source files.
4. JSON examples parse, referenced scenarios exist, and demo backfill remains dry-run.
5. Fences are balanced and zero-cost/Airflow acceptance defaults remain explicit.
6. Documented Airflow trigger scripts exist.
7. Documented API method/path pairs match source decorators inspected through AST.

The checks are in `tests/test_readme_contract.py` and will be collected by the
existing DAG 91 `all` suite. Local Python did not have pytest installed, so the
seven test functions were executed directly through the standard-library
`runpy` module. This is development feedback only, not a pytest or Airflow pass.

Docker Compose `config --quiet` and `git diff --check` for the authored files
passed. Git reported the repository's expected LF-to-CRLF conversion warning.
No provider inference call was made for this documentation work.

## GitHub metadata

The repository About editor was used while authenticated as the owner. The
description, website, and twenty approved topics were saved and read back from
the page and public repository metadata. Visibility remained public, the
default branch remained `main`, and Releases/Packages/Deployments inclusion
checkboxes were not changed.

Description:

> Data reliability control plane with Airflow, ClickHouse, dbt, and LangGraph for warehouse quality checks, evidence-based AI triage, lineage impact, and approval-gated remediation workflows.

Website: [Project repository](https://github.com/caesarmario/agentic-data-quality-triage).

Topics: `data-engineering`, `data-quality`, `data-reliability`,
`data-observability`, `data-warehouse`, `apache-airflow`, `clickhouse`, `dbt`,
`seaweedfs`, `langgraph`, `llm`, `multi-agent`, `human-in-the-loop`, `data-lineage`,
`fastapi`, `streamlit`, `nextjs`, `discord-bot`, `model-context-protocol`,
`docker-compose`.

## Initial acceptance status (superseded)

Docker initially reported that the Docker Desktop Linux engine named pipe was
unavailable. Docker Desktop startup was attempted; a subsequent bounded Docker
server-version probe timed out after eight seconds even though Desktop backend
processes were present. An available engine was not confirmed. Consequently,
this documentation pass must not be reported
as Airflow acceptance-tested unless a later result is recorded below.

- DAG: `91_dag_dq_platform_validation`.
- Current-pass DagRun ID: not created.
- Final DagRun state: unavailable.
- Task states/test counts from Airflow: unavailable.
- Operational baseline/incident replay for this pass: not executed.
- Fresh-clone installation on a clean machine: not executed.
- GitHub Markdown/Mermaid rendering and real application browser QA: not established by these source checks.

Historical application/provider successes remained in their dedicated acceptance
documents and did not satisfy this documentation pass at that time. Later
sections record the completed current-checkout and isolated fresh-clone evidence.

## Next verification

Once Docker is available, verify the runner's external-model flag is false,
check DAG imports, and trigger DAG 91 with `validation_suite=all`. Inspect all
five task states, pytest count, readiness output, summary, and warnings. Retain
the resulting run ID here. Use unused synthetic dates and disabled outbound
notifications for any additional operational walkthrough; do not delete
existing partitions or spend LLM credit for a documentation check.

The README is still a local edit. No commit or push was made. Before publishing,
review the complete checkout so referenced optional UI files, helper scripts,
and acceptance documents are included in the same coherent release rather than
pushing the README ahead of its dependencies.

## September 24 Current-Checkout Acceptance

Docker was available on this later pass. Existing data and prior work were
preserved; no fresh-clone installation or destructive reset was performed.

| DAG | Run ID | Verified outcome |
|---|---|---|
| 00 daily orchestrator | `manual__readme_baseline_20260924` | Success; September 21 baseline, raw/staging 3,021 rows and mart 25 rows; 17 DQ pass, one skip |
| 00 daily orchestrator | `manual__readme_incident_20260924` | Success; September 22 missing_segment; 16 pass, one skip, one warning |
| 00 daily orchestrator | `manual__readme_no_logical_date_20260924` | Success after one retry; September 23 baseline; tests manual trigger without logical date |
| 98 supervisor | `manual__readme_zero_llm_triage_20260924` | All five tasks success; zero external calls; stored Markdown/JSON report RPT-87CB9E0E |
| 91 validation | `manual__acceptance_hardening_20260924T184200` | All five tasks success; 937 pytest tests, two warnings, 39 readiness checks |

The undated manual run initially failed during Jinja rendering because Airflow
did not supply `ts_nodash`. The corrected helper preserves legacy dated IDs and
uses a stable parent-run hash for undated triggers; the original task's second
attempt and every downstream stage succeeded. Airflow retained the first failure.
Unpausing the daily DAG also allowed its due scheduled synthetic run to execute;
the daily DAG was returned to its paused state after manual validation.

The triage request uses Alert Ref `DQ-20260922-3218EA`. Its parent UUID is
`8d90fe6f-cb1e-51a0-b177-dbd478f07f02`; child report UUID is
`f5f69925-97e9-4964-a364-701bf8d6f08b`. Markdown and JSON are stored under
`s3://dq-artifacts/agent-reports/dt=2026-09-22/report_id=RPT-87CB9E0E/`.
API/web report readiness read the stored report and matched seven fields.

Final task states and retained pytest/readiness/summary logs were inspected.
The warnings concern Starlette/httpx and Discord/audioop deprecations, not test
failures. Eight documentation contract checks now include rejecting FINAL in
queries against non-ReplacingMergeTree bootstrap tables. The README mart query
was corrected accordingly. Routine validation used disabled external LLMs.

Separately authorized paid Gemini smoke and fan-out acceptance also succeeded;
these were not needed to make documentation tests pass. See
`gemini_fanout_acceptance.md` for bounded inference evidence and estimates.

Browser inspection covered twenty page/size/theme combinations and real
screenshots; see `web_operator_testing.md`. Full mutation interactions,
Markdown/Mermaid renderer validation, clean-machine installation, and publishing
remain separate work. No commit, push, or deployment was performed.

### Final Regression And Theme Evidence

The later DAG 91 run `manual__theme_acceptance_20260924T185000` completed all
five tasks successfully on attempt one, with 950 tests passed, two dependency
warnings, and 39 readiness checks. Its stored DagRun/task states were rechecked
on September 25. This includes the standalone Airflow-helper import regression
and twelve theme-contrast tests added after the earlier 937-test acceptance.
The matching final browser image and screenshot refresh are recorded in
`web_operator_testing.md`. No additional paid inference was required.

### Operator Acceptance After The September 25 Checkpoint

DAG 91 run `manual__operator_acceptance_20260925T112000` succeeded with all five
tasks on their first attempt, 966 tests passed, two dependency warnings, and 40
readiness checks. The read-only approval gate verified the dedicated cancelled
request and its creation/approval/cancellation audit, with no execution run.
Stored report `RPT-286D11F6` matched seven selected web page fields. Separate real
browser interactions covered identity validation, keyboard approve/cancel,
mobile triage with automatic persisted-report refresh, and a simulated error.
The test did not dispatch remediation and external LLM remained disabled.
See `web_operator_testing.md` for fixture IDs and the exact verification scope.

### Local Markdown And Mermaid Rendering

`scripts/check_readme_browser.cjs` renders the current README with Marked and
Mermaid in headless Chromium. It serves only the preview, Mermaid distribution,
and reviewed documentation images on a loopback ephemeral port. External badge
requests are intercepted locally; badge service availability is not tested.
No provider request, warehouse mutation, or operational DAG is invoked.

On September 25, all three Mermaid diagrams rendered as SVG in each combination
of 1440px/390px and light/dark theme. Internal navigation anchors resolved, local
screenshots loaded, and there was no document-level horizontal overflow or
JavaScript page error. The dark investigation diagram and mobile light README
were visually inspected. This uses a local preview stylesheet and does **not**
claim to reproduce GitHub's exact Markdown renderer or certify fresh installation.

The ignored `data/acceptance/readme-browser/results.json` records versions, UTC
completion time, source SHA-256, per-case assertions, and blocked external request
count. Screenshots of every diagram and the page header are written beside it.
The successful run used Marked 16.4.2, Mermaid 12.0.0, and Playwright 1.62.1.
The harness follows the [official Mermaid rendering API](https://mermaid.js.org/config/usage.html).

Optional rerun, separate from the container quickstart, requires Node.js 22.12+
and a Chromium installation managed by Playwright. Dependencies and generated
outputs stay under ignored local acceptance data:

```powershell
npm install --prefix data/acceptance/readme-render --ignore-scripts --no-audit --no-fund mermaid@12.0.0 marked@16.4.2 playwright@1.62.1
$env:NODE_PATH = (Resolve-Path data/acceptance/readme-render/node_modules).Path
node data/acceptance/readme-render/node_modules/playwright/cli.js install chromium
node scripts/check_readme_browser.cjs
```

```bash
npm install --prefix data/acceptance/readme-render --ignore-scripts --no-audit --no-fund mermaid@12.0.0 marked@16.4.2 playwright@1.62.1
export NODE_PATH="$PWD/data/acceptance/readme-render/node_modules"
node data/acceptance/readme-render/node_modules/playwright/cli.js install chromium
node scripts/check_readme_browser.cjs
```

Browser installation may additionally require operating-system libraries on Linux.
The actual acceptance reused the bundled Chromium/Node runtime already present;
the installation commands above were not tested on a clean machine. Keep the
Airflow code/documentation gate and browser observations as separate evidence.

The subsequent Airflow gate `manual__readme_render_contract_20260925T042000Z`
completed on DAG 91 at 11:20:10 Asia/Bangkok with all five tasks successful on
attempt one, 966 tests passed, two known dependency warnings, and 40 readiness
checks passed. Pytest, readiness, summary logs, and stored task/DagRun states were
inspected. The browser harness ran separately; this DagRun did not execute Node
or Playwright. No additional paid inference or remediation was performed.

### Missing-Segment And Fan-Out Safety Regression

The current checkout completed two no-LLM DAG 98 runs and one accepted DAG 94
comparison on September 27:

- single: `manual__segment_safety_single_20260927T161300Z`
- fan-out: `manual__segment_safety_fanout_20260927T161300Z`
- comparison: `manual__life_eval_20260927T163140683251`

The selected alert was a downstream `missing_segment` warning while the exact
raw partition still contained 2,902 rows. Both stored reports therefore retained
zero approval-gated actions. This is the intended fail-closed behavior: segment
coverage alone cannot authorize a whole-partition backfill. All tasks in the
accepted runs succeeded, external LLM execution stayed disabled, and provider
cost remained zero.

Fan-out added three evidence references but produced zero confidence or
report-quality improvement. DAG 94 verified the immutable comparison artifacts
and one replay-safe audit event, then returned `keep_single`. An earlier DAG 94
attempt used child report IDs rather than supervisor parent IDs and failed source
preparation; no comparison artifact was accepted from that invalid correlation.

The subsequent full DAG 91 gate
`manual__validation_all_20260927T162205206087` also succeeded with all five
tasks, 999 tests passed, two dependency warnings, and 20 core platform readiness
checks. The lower readiness count reflects that optional API/web flags were not
requested, not a reduction in core service coverage.

### Extended Quality Summary Acceptance

The API, Discord, MCP, Copilot, and proxy contracts for weekly, per-table, and
warehouse-wide quality summaries completed the full Airflow validation gate on
September 28, 2026. The accepted run was
`manual__validation_all_20260927T223032737511` on
`91_dag_dq_platform_validation`.

All five task instances succeeded on their first attempt. The retained pytest
log recorded 1,018 passed tests and two known dependency warnings in 12.21
seconds. Platform readiness recorded 39 passed checks and zero failures,
including the daily and database summary API probes plus the optional web
acceptance checks. External LLM execution remained disabled, so the acceptance
incurred no provider cost.

The summary contract keeps ClickHouse-backed aggregates deterministic. Any
allowlisted rerun candidate is returned as `advisory_only` with
`requires_approval=true`; the summary path cannot trigger Airflow or perform a
remediation action.

### Transactional Approved Action And Demo Image Documentation

On September 29, 2026, the README was reconciled with the current approval
implementation and the approval boundary was hardened. The exact-scope store now
uses explicit request generations for intentional repeats, rejects self-approval
by the same requester label, preserves an explicitly empty runtime environment,
and separates the Airflow execution actor from requester and approver fields in
the audit event. Operator labels remain local caller-supplied identifiers rather
than authenticated enterprise principals.

The source review confirmed two distinct approval paths:

- legacy daily backfill requests remain append-versioned in
  `dq.approval_requests` in ClickHouse and are dispatched by DAG `90`;
- exact-scope `rerun_dbt`, `create_ticket`, and `post_notification` requests use
  PostgreSQL `dq_control.approval_requests`, request-version compare-and-swap,
  atomic dispatch claims, and DAG `90_02_dag_dq_platform_approved_actions`.

The documented `rerun_dbt` workflow was checked against
`scripts/manage_approved_action.py`,
`scripts/trigger_airflow_approved_action.py`, the Pydantic scope contract, and
the Airflow DAG source. The scope is fixed to DAG `20`, one business date,
optional tests, and `full_refresh=false`. Preview is the trigger script's default;
execution requires the explicit `--execute` flag.

Generation `1` preserves the original idempotency hash and request identifier.
The same canonical scope plus generation is reused, while an operator must pass
an explicit higher `--generation` for an intentional repeat after a terminal
attempt. Changing the scope merely to bypass idempotency is not documented or
accepted behavior.

Runtime state was read back from Airflow and both control stores. Request
`APR-CE62C33DDCB9F8238054` remained `approved` with execution status
`dispatched`. Parent run
`manual__approved_action_execute_apr-ce62c33ddcb9f8238054_20260928T124335713164`
completed successfully with all three DAG `90_02` tasks successful. Child run
`approved_rerun_dbt__apr-ce62c33ddcb9f8238054` completed successfully with all
six DAG `20` tasks successful. ClickHouse retained the matching
`approved_action_executed` / `dispatched` audit event. Earlier failed hardening
runs remain visible and were not rewritten or presented as successful evidence.

Live GitHub issue and Discord notification delivery were not exercised. Both
providers remain disabled by default, use environment-owned destinations, and
disable automatic retry for ambiguous outbound outcomes. The README therefore
documents their safety boundary without claiming external acceptance.

A preview-only operational acceptance created generation-two request
`APR-F5A2F7DCFC33787D0F24`. A self-approval attempt by
`generation_acceptance` was rejected, a distinct `generation_reviewer` approved
version one, and Airflow DAG `90_02_dag_dq_platform_approved_actions` run
`manual__approved_action_preview_apr-f5a2f7dcfc33787d0f24_20260929T133833301843`
completed successfully. All three tasks succeeded. The retained task log reports
`provider_ready=true` and `side_effects_executed=false`; the PostgreSQL record
remains `approved` with `execution_status=not_started`. The matching ClickHouse
audit event attributes execution to `airflow:airflow` while retaining the
requester and approver separately. The log-reader allowlist was also corrected
so the documented `make airflow-approved-action-logs` helper accepts DAG `90_02`.

Eight current images under `docs/images` were captured with strict mode and
opened and reviewed before being
embedded: Next.js overview, Next.js triage, Streamlit alerts, the Airflow daily
orchestrator graph, CH-UI, rendered Discord output, rendered S3 artifact
inventory, and rendered MCP tool inventory. Captions explicitly distinguish the
S3 and MCP read-only listings from native service/client UI screenshots and the
Discord formatter render from a real Discord client screenshot. The Airflow
image comes from the authenticated local Airflow 3 graph view. The Next.js
overview is also labeled as a valid empty current-date summary with retained
historical alerts. Screenshot capture did not send an outbound Discord message,
invoke an external LLM, or execute remediation.

The capture utility now writes temporary files and replaces a screenshot only
after success, fails strict capture when any selected surface is skipped, and
redacts quoted credential fields, bearer tokens, GitHub tokens, Discord webhook
URLs, AWS access-key identifiers, and Google API-key formats. Focused helper
tests prove failed captures preserve the previous file.

The focused README contract suite then passed all eight checks inside
`dq_runner`. The local browser harness rendered the updated README in four
viewport/theme cases, rendered all three Mermaid diagrams, resolved internal
anchors and local images, and reported no page-level horizontal overflow or
JavaScript error. Restoring the harness dependencies changed only ignored local
acceptance data, not the application dependency manifests.

Final acceptance used Airflow DAG `91_dag_dq_platform_validation`, run
`manual__validation_all_20260928T231129403707`. The DagRun reached `success` and
all five tasks succeeded on their first attempt. Retained logs recorded 1,055
passed tests, eight skipped tests, and one Discord `audioop` deprecation warning
in the full suite. Core platform readiness passed 20 checks with zero failures,
covering the required ClickHouse tables, five S3 buckets, CH-UI, and Streamlit.
The Airflow import-error list was empty. This acceptance did not call an external
LLM, send a ticket/notification, or execute another remediation action.

### September 29 Isolated Fresh-Clone Acceptance

A repository-only clone was created in a temporary Windows directory from the
current tracked and non-ignored files. Local `.env` values, generated data,
database volumes, browser dependencies, and prior Airflow metadata were not
copied. The clone used a separate Compose project and new Docker volumes. The
main project's containers were stopped without deleting their volumes while the
isolated stack occupied the fixed local container names.

The first DAG 91 attempt correctly exposed a packaging defect: one validation
test required ignored `todo/list.todo`. That file is a local planning aid and is
not present after cloning. The test was corrected to validate the tracked
Airflow-first policy in `AGENTS.md`; no TODO file is now required by runtime or
release acceptance.

The accepted retry was:

- DAG: `91_dag_dq_platform_validation`
- Run ID: `manual__fresh_clone_validation_retry_20260929T205200`
- DagRun state: `success`
- Task states: all five tasks `success` on attempt one
- Pytest: 1,064 passed, 12 skipped, one Discord `audioop` deprecation warning
- Readiness: 20 passed, zero failed
- Airflow import errors: none

Fresh initialization created all required ClickHouse tables and five S3 buckets;
the empty raw, staging, mart, alert, and metadata tables were accepted as the
correct clean-install state. CH-UI and Streamlit health checks returned HTTP 200.
The optional FastAPI and Next.js profiles also built and started successfully.

README rendering was then executed from the clone with newly installed ignored
Node dependencies. Marked 18.0.14, Mermaid 12.0.0, Playwright 1.63.0, and the
locally available Chromium runtime rendered four viewport/theme cases and all
three Mermaid diagrams. Internal anchors and local images resolved, while
external requests were blocked by the harness. The result reported no broken
image, JavaScript page error, or page-level horizontal overflow.

The isolated stack and all temporary volumes were removed afterward. The main
stack was restarted against its original volumes; retained pipeline/audit data
and approval request `APR-F5A2F7DCFC33787D0F24` remained present. This proves one
Windows/Docker Desktop fresh-clone path, not universal macOS/Linux compatibility
or GitHub's exact Markdown renderer. External LLMs, Discord delivery, ticket
creation, notification delivery, and remediation execution were not invoked.
