####
## Airflow Incident Revalidation DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.sdk import DAG, Param

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)


# --- Defining DAG ID And Documentation
logger = logging.getLogger(__name__)
DAG_ID = "90_03_dag_dq_incident_revalidation"
REQUEST_ID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

DOC_MD = """
# 90.03 - Incident Revalidation

Manual, exact-scope verification for a durable incident resolution request.
The request owner assigns a deterministic DagRun ID and a 15-minute deadline,
including queue time. This DAG never repairs data or closes an incident.

Supply only `resolution_request_id` (a UUID). Scope, permissions, and contract
are loaded from durable control state. The runner reads the warehouse and
writes observability evidence, audit, and request progress only.

No generator, ingestion, dbt, backfill, schema changes, or external model calls
are performed. Retries are disabled. A successful DagRun may contain a business
result of `blocked`; only fresh explicit `pass` evidence permits human closure.
The existing runner helper uses the platform timezone, Asia/Bangkok.
"""


# --- Defining DAG Parameters
def incident_revalidation_params() -> dict[str, Param]:
    """Require one UUID; do not accept a user-selected scope or command."""
    return {
        "resolution_request_id": Param(
            type="string", minLength=36, maxLength=36,
            pattern=REQUEST_ID_PATTERN,
            description="UUID of a durable incident revalidation request; scope is loaded server-side.",
        ),
    }


def request_stage_command(stage: str) -> str:
    """Build a fixed runner CLI command with a schema-validated UUID.

    Raises:
        ValueError: If a developer supplies an unsupported execution stage.
    """
    if stage not in {"load", "checks", "persist", "notify"}:
        raise ValueError("Unsupported incident revalidation stage.")
    return (
        "python -m agent.tools.incident_resolution "
        "--request-id '{{ params.resolution_request_id }}' "
        f"--stage {stage}"
    )


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Recheck one incident scope and retain proof for human-confirmed closure.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    is_paused_upon_creation=False,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=15),
    default_args=default_dag_args(retries=0),
    params=incident_revalidation_params(),
    tags=["dq-platform", "incident", "revalidation", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    t00_start = start_task()
    t10_load_request = runner_plain_bash_task(
        task_id="t10_load_request", project_command=request_stage_command("load"),
        execution_timeout=timedelta(minutes=1),
    )
    t20_run_scoped_checks = runner_plain_bash_task(
        task_id="t20_run_scoped_checks", project_command=request_stage_command("checks"),
        execution_timeout=timedelta(minutes=8),
    )
    t30_persist_validation_result = runner_plain_bash_task(
        task_id="t30_persist_validation_result", project_command=request_stage_command("persist"),
        execution_timeout=timedelta(minutes=2),
    )
    t40_notify_validation_result = runner_plain_bash_task(
        task_id="t40_notify_validation_result", project_command=request_stage_command("notify"),
        execution_timeout=timedelta(minutes=2),
    )
    t40_notify_validation_result.trigger_rule = "all_done"
    t90_finish = finish_task()
    (
        t00_start >> t10_load_request >> t20_run_scoped_checks
        >> t30_persist_validation_result >> t40_notify_validation_result >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
