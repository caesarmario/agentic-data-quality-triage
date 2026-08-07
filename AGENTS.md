####
## Agent Development Instructions for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# Project Working Rules

- Build one small, production-flavored slice at a time.
- Preserve the architecture boundary: deterministic pipelines and guarded tools remain the source of truth; AI explains, investigates, and recommends.
- Keep code modular, documented, logged, and readable according to the conventions in `todo/list.todo`.
- Do not revert unrelated work in the dirty worktree.


# Development And Testing Workflow

- Add or update focused unit and static tests for each implementation.
- Direct pytest or local smoke commands may be used for fast developer feedback while editing.
- Treat direct pytest and local smoke output as non-authoritative inner-loop feedback only; they never satisfy final acceptance by themselves.
- Whenever the project owner asks to test an implementation, trigger the appropriate Airflow validation or operational DAG even when the same checks already passed directly.
- Final acceptance evidence for every completed development slice must come from an Airflow DagRun so task state, retries, duration, and logs are retained.
- Use `91_dag_dq_platform_validation` for code-focused validation suites and platform readiness checks.
- Use the relevant operational DAG for end-to-end pipeline behavior, such as `00_dag_dq_platform_daily_orchestrator` for the daily data flow.
- Do not claim an implementation is tested from direct pytest output alone when an Airflow validation path exists.
- After triggering validation, inspect the DagRun state and relevant Airflow task logs before reporting the result.
- If an Airflow DagRun cannot be created or inspected, report the implementation as not yet acceptance-tested rather than substituting local output.
- Keep unit tests independent from Airflow internals; Airflow orchestrates the test command but does not replace pytest or CI.
- Never use production mutation, remediation, or destructive cleanup merely to validate a code change.


# Acceptance Evidence

A completed slice should report:

- Airflow DAG ID and run ID.
- Final DagRun state.
- Relevant task states.
- Test count or smoke-check result from Airflow logs.
- Any warning, skipped validation, or residual risk.
