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

## Acceptance not yet established

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

Historical application/provider successes remain in their dedicated acceptance
documents. They do not satisfy acceptance of this new documentation pass.

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
