####
## Airflow Helper Utilities for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
import os
import textwrap
from datetime import datetime
from typing import Any

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import Param


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
DEFAULT_OWNER            = "data-engineering"
DEFAULT_RETRIES          = 1
DEFAULT_RUNNER_CONTAINER = "dq_runner"
DEFAULT_START_DATE       = datetime(2026, 5, 1)
DEFAULT_DOCKER_EXEC      = "/usr/bin/docker"


# --- Defining Functions
def default_dag_args(owner: str = DEFAULT_OWNER, retries: int = DEFAULT_RETRIES) -> dict[str, Any]:
    """
    Build common default_args for local DQ platform DAGs.

    Args:
        owner: Logical DAG owner.
        retries: Number of task retries for transient local failures.

    Returns:
        Airflow default_args dictionary.
    """
    return {
        "owner": owner,
        "retries": retries,
    }


def common_dag_params() -> dict[str, Param]:
    """
    Build shared manual-run parameters for operational DAGs.

    Returns:
        Dictionary of Airflow Param objects exposed in the UI.
    """
    return {
        "dt": Param(
            "",
            type="string",
            description="Single business date to process. Defaults to Airflow ds when blank.",
        ),
        "start_date": Param(
            "",
            type="string",
            description="Inclusive backfill start date in YYYY-MM-DD format. Requires end_date.",
        ),
        "end_date": Param(
            "",
            type="string",
            description="Inclusive backfill end date in YYYY-MM-DD format. Requires start_date.",
        ),
        "incident_scenario": Param(
            "baseline",
            type="string",
            description="Synthetic incident scenario label used by the seeding pipeline.",
        ),
        "run_mode": Param(
            "daily",
            type="string",
            description="Logical run mode such as daily, manual, or backfill.",
        ),
        "run_triage": Param(
            False,
            type="boolean",
            description="Whether downstream orchestrators should run agent triage after alerts are generated.",
        ),
    }


def start_task(task_id: str = "t00_start") -> EmptyOperator:
    """
    Create the standard start anchor task.

    Args:
        task_id: Airflow task id.

    Returns:
        EmptyOperator used as DAG start anchor.
    """
    logger.info("Creating Airflow start task | task_id=%s", task_id)

    return EmptyOperator(task_id=task_id)


def finish_task(task_id: str = "t90_finish") -> EmptyOperator:
    """
    Create the standard finish anchor task.

    Args:
        task_id: Airflow task id.

    Returns:
        EmptyOperator used as DAG finish anchor.
    """
    logger.info("Creating Airflow finish task | task_id=%s", task_id)

    return EmptyOperator(task_id=task_id)


def runner_container_name() -> str:
    """
    Resolve the local Python runner container name.

    Returns:
        Docker container name used for pipeline command execution.
    """
    resolved = os.getenv("DQ_RUNNER_CONTAINER", DEFAULT_RUNNER_CONTAINER)

    logger.info("Resolved runner container | container=%s", resolved)

    return resolved


def docker_exec_binary() -> str:
    """
    Resolve the Docker CLI path available inside the Airflow worker container.

    Returns:
        Docker CLI binary path.
    """
    resolved = os.getenv("AIRFLOW_DOCKER_EXEC", DEFAULT_DOCKER_EXEC)

    logger.info("Resolved Docker exec binary | binary=%s", resolved)

    return resolved


def date_argument_shell_snippet() -> str:
    """
    Build the reusable shell snippet that resolves dt or start/end from dag_run.conf.

    Returns:
        Shell snippet that exports DATE_ARGS for downstream pipeline commands.
    """
    return textwrap.dedent(
        """
        RUN_DT="{{ dag_run.conf.get("dt") or ds }}"
        START_DATE="{{ dag_run.conf.get("start_date", "") }}"
        END_DATE="{{ dag_run.conf.get("end_date", "") }}"

        if [ -n "$START_DATE" ] && [ -n "$END_DATE" ]; then
          DATE_ARGS="--start $START_DATE --end $END_DATE"
        else
          DATE_ARGS="--dt $RUN_DT"
        fi

        INCIDENT_SCENARIO="{{ dag_run.conf.get("incident_scenario", "baseline") }}"
        RUN_MODE="{{ dag_run.conf.get("run_mode", "daily") }}"
        """
    ).strip()


def build_runner_bash_command(project_command: str) -> str:
    """
    Build a BashOperator command that executes project code inside dq_runner.

    Args:
        project_command: Command to run from /app inside the Python runner container.

    Returns:
        Bash command string rendered by Airflow and executed by the worker.
    """
    container = runner_container_name()
    docker    = docker_exec_binary()

    # Airflow orchestrates the work; the project runner owns Python/dbt dependencies.
    command = textwrap.dedent(
        f"""
        set -euo pipefail

        {date_argument_shell_snippet()}

        {docker} exec {container} /bin/sh -lc "cd /app && {project_command}"
        """
    ).strip()

    logger.info("Built runner bash command | container=%s command=%s", container, project_command)

    return command


def runner_bash_task(
    task_id: str,
    project_command: str,
    execution_timeout: Any | None = None,
) -> BashOperator:
    """
    Create a BashOperator that delegates execution to the local Python runner container.

    Args:
        task_id: Airflow task id.
        project_command: Command to execute from /app in dq_runner.
        execution_timeout: Optional Airflow task timeout.

    Returns:
        Configured BashOperator.
    """
    logger.info("Creating runner bash task | task_id=%s", task_id)

    return BashOperator(
        task_id=task_id,
        bash_command=build_runner_bash_command(project_command),
        execution_timeout=execution_timeout,
    )


def single_date_conf() -> dict[str, str]:
    """
    Build standard conf payload for TriggerDagRunOperator child DAGs.

    Returns:
        Conf dictionary forwarding date and scenario parameters.
    """
    return {
        "dt": "{{ dag_run.conf.get(\"dt\") or ds }}",
        "start_date": "{{ dag_run.conf.get(\"start_date\", \"\") }}",
        "end_date": "{{ dag_run.conf.get(\"end_date\", \"\") }}",
        "incident_scenario": "{{ dag_run.conf.get(\"incident_scenario\", \"baseline\") }}",
        "run_mode": "{{ dag_run.conf.get(\"run_mode\", \"daily\") }}",
        "run_triage": "{{ dag_run.conf.get(\"run_triage\", false) }}",
    }


logger.info("Airflow helper utilities loaded for Agentic DQ platform")
