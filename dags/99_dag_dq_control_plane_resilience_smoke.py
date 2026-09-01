####
## Airflow Control Plane Resilience Smoke DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Manually verify single-handoff and fan-out failure containment controls."""

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.control_plane_resilience import (
    CONTROL_PLANE_RESILIENCE_SCENARIOS,
    emit_control_plane_resilience_summary,
)
from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "99_dag_dq_control_plane_resilience_smoke"

DOC_MD = """
# 99 - Control Plane Resilience Smoke

Manual administrative DAG for controlled failure acceptance. It verifies that the supervisor:

- retries exactly once only for an explicitly retry-safe read-only specialist;
- interrupts a hung specialist using a hard process signal deadline;
- blocks execution when the persistent circuit is open; and
- accepts a typed partial result without starting a second handoff; and
- contains a terminal specialist failure with complete parent audit evidence.
- isolates optional and required parallel worker failures;
- rejects invalid worker contracts and exhausted cost admission before execution;
- resumes completed parallel waves without repeating worker side effects; and
- proves ten-worker capacity with default concurrency capped at three.

The scenarios never execute SQL, remediation, DDL, or backfill actions. Failure injection lives
outside the production specialist code and is selected through an allowlisted Airflow parameter.
Gemini timeout and rate-limit scenarios are simulations and make zero external provider requests.

Example:

```json
{
  "scenario": "hard_timeout"
}
```
"""


# --- Defining DAG Parameters
def control_plane_resilience_params() -> dict[str, Param]:
    """
    Build the allowlisted resilience scenario parameter.

    Returns:
        Dictionary containing one controlled scenario selector.
    """
    return {
        "scenario": Param(
            "transient_once",
            type="string",
            enum=list(CONTROL_PLANE_RESILIENCE_SCENARIOS),
            description="Controlled failure mode verified by this administrative DAG.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Verify bounded supervisor failure containment through Airflow.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=control_plane_resilience_params(),
    tags=["dq-platform", "agent", "resilience", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for controlled supervisor resilience acceptance.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_run_resilience_scenario = runner_plain_bash_task(
        task_id="t10_run_resilience_scenario",
        project_command=(
            "python scripts/run_control_plane_resilience_smoke.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--scenario '{{ dag_run.conf.get(\"scenario\", \"transient_once\") }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_verify_resilience_audit = runner_plain_bash_task(
        task_id="t20_verify_resilience_audit",
        project_command=(
            "python scripts/verify_control_plane_resilience.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--scenario '{{ dag_run.conf.get(\"scenario\", \"transient_once\") }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_resilience_summary = PythonOperator(
        task_id="t30_emit_resilience_summary",
        python_callable=emit_control_plane_resilience_summary,
        execution_timeout=timedelta(minutes=2),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_run_resilience_scenario
        >> t20_verify_resilience_audit
        >> t30_emit_resilience_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
