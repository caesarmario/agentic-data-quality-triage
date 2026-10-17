####
## Airflow Helper Utilities for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
import os
import re
import textwrap
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.providers.standard.operators.python import BranchPythonOperator
from airflow.sdk import Param

from dq_platform.run_identity import trigger_logical_date, trigger_run_suffix


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
DEFAULT_OWNER            = "data-engineering"
DEFAULT_RETRIES          = 1
DEFAULT_RUNNER_CONTAINER = "dq_runner"
DEFAULT_DOCKER_EXEC      = "/usr/bin/docker"
DEFAULT_TIMEZONE         = "Asia/Bangkok"
DEFAULT_START_DATE       = pendulum.datetime(2026, 5, 1, tz=DEFAULT_TIMEZONE)
DAILY_PLATFORM_SCHEDULE      = "5 0 * * *"
DEFAULT_TRIGGER_RESET_DAG_RUN = True
EMPTY_DEMO_AUDIT_SINCE_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])(?![\s\S])"
)
STAGE_FLAG_DEFAULTS = {
    "run_seed": True,
    "run_load": True,
    "run_dbt": True,
    "run_dq": True,
    "run_triage": False,
}


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


def bangkok_ds(logical_date: Any) -> str:
    """
    Convert an Airflow logical date into the platform business date.

    Args:
        logical_date: Airflow logical date from the template context.

    Returns:
        Date string in YYYY-MM-DD format using Asia/Bangkok timezone.
    """
    if logical_date is None:
        return ""

    if logical_date.tzinfo is None:
        logical_date = logical_date.replace(tzinfo=timezone.utc)

    return logical_date.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()


def default_user_defined_macros() -> dict[str, Any]:
    """
    Build user-defined Jinja macros shared by all platform DAGs.

    Returns:
        Dictionary of macro names to Python callables.
    """
    return {
        "bangkok_ds": bangkok_ds,
        "default_incident_scenario": default_incident_scenario,
        "trigger_run_suffix": trigger_run_suffix,
        "trigger_logical_date": trigger_logical_date,
        "parse_stage_flag": parse_stage_flag,
        "safe_empty_demo_audit_since": safe_empty_demo_audit_since,
        "empty_demo_readiness_args": empty_demo_readiness_args,
    }


def default_incident_scenario(dag_run: Any) -> str:
    """
    Resolve default incident scenario for Airflow scheduled versus manual runs.

    Args:
        dag_run: Airflow DagRun object from the template context.

    Returns:
        "auto" for scheduled daily runs, explicit dag_run.conf value when provided,
        otherwise "baseline" for manual/testing runs.
    """
    if dag_run and dag_run.conf and dag_run.conf.get("incident_scenario"):
        return dag_run.conf["incident_scenario"]

    run_type = str(getattr(dag_run, "run_type", "")).lower()

    # Scheduled daily runs should feel production-like; manual runs stay clean unless overridden.
    if "scheduled" in run_type:
        return "auto"

    return "baseline"


def parse_stage_flag(value: Any, name: str) -> bool:
    """Parse only booleans or case-insensitive true/false strings.

    Args:
        value: Explicit flag value; missing keys use defaults at the caller.
        name: Flag name for a safe error message without echoing conf values.

    Returns:
        Strictly parsed boolean.

    Raises:
        ValueError: Nulls, numbers, blanks, or other boolean-like values.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ValueError(f"{name} must be a boolean or a true/false string.")


def acceptance_manual_only() -> bool:
    """Disable only selected scheduled DAGs when the strict server flag is true."""
    return parse_stage_flag(
        os.environ.get("DQ_ACCEPTANCE_MANUAL_ONLY", "false"), "DQ_ACCEPTANCE_MANUAL_ONLY"
    )


def safe_empty_demo_audit_since(value: Any, logical_date: datetime | None) -> str:
    """Return an aware, shell-safe UTC boundary; default to the DagRun logical date."""
    if value is None:
        stamp = logical_date
    elif isinstance(value, str) and re.fullmatch(EMPTY_DEMO_AUDIT_SINCE_PATTERN, value):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("empty_demo_audit_since must be an aware ISO timestamp") from exc
    else:
        raise ValueError("empty_demo_audit_since must be an aware ISO timestamp")
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("empty_demo_audit_since must be an aware ISO timestamp")
    try:
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValueError("empty_demo_audit_since must be an aware ISO timestamp") from exc


def empty_demo_readiness_args(enabled: Any, value: Any, logical_date: datetime | None) -> str:
    """Validate the opt-in/boundary pair even when the empty-demo check is disabled."""
    if not parse_stage_flag(enabled, "expect_empty_demo"):
        if value is not None:
            raise ValueError("empty_demo_audit_since requires expect_empty_demo")
        return ""
    boundary = safe_empty_demo_audit_since(value, logical_date)
    return f"--expect-empty-demo --empty-demo-audit-since '{boundary}' "


def resolve_stage_flags(conf: dict[str, Any] | None) -> dict[str, bool]:
    """Validate all stage flags before selecting any work, including skipped work.

    Args:
        conf: Optional DagRun conf; unrelated date/scenario fields are preserved.

    Returns:
        Five normalized flags with defaults applied only to missing keys.

    Raises:
        ValueError: Malformed conf or any invalid explicit stage flag.
    """
    if conf is None:
        conf = {}
    if not isinstance(conf, dict):
        raise ValueError("Stage flags require a dag_run.conf dictionary.")
    return {name: parse_stage_flag(conf.get(name, default), name)
            for name, default in STAGE_FLAG_DEFAULTS.items()}


def select_stage_branch(
    flags: tuple[str, ...],
    run_task_id: str,
    skip_task_id: str,
    dag_run: Any = None,
) -> str:
    """Choose a trigger or bypass after validating the complete flag payload.

    Args:
        flags: Allowlisted flags; any enabled flag selects the stage.
        run_task_id: Direct downstream trigger task.
        skip_task_id: Direct downstream join used to bypass disabled work.
        dag_run: Airflow context DagRun, injected at task execution.

    Returns:
        Selected downstream task ID.

    Raises:
        ValueError: Unknown flag names or invalid explicit flag values.
    """
    if not flags or any(name not in STAGE_FLAG_DEFAULTS for name in flags):
        raise ValueError("Stage branch requires known stage flags.")
    resolved = resolve_stage_flags(getattr(dag_run, "conf", None))
    enabled = any(resolved[name] for name in flags)
    logger.info("Selected optional stage | flags=%s enabled=%s", flags, enabled)
    return run_task_id if enabled else skip_task_id


def optional_stage(
    task_id: str,
    flags: tuple[str, ...],
    task: Any,
) -> tuple[BranchPythonOperator, EmptyOperator]:
    """Wrap one trigger with a branch and failure-preserving, skip-safe join.

    Args:
        task_id: Unique branch task ID; its join receives a _join suffix.
        flags: Flags controlling the trigger (OR semantics for seed/load).
        task: Existing child-DAG trigger to guard.

    Returns:
        Entry branch and exit join for sequential composition of independent work.
    """
    join = EmptyOperator(task_id=f"{task_id}_join", trigger_rule="none_failed_min_one_success")
    branch = BranchPythonOperator(
        task_id=task_id,
        python_callable=select_stage_branch,
        op_kwargs={"flags": flags, "run_task_id": task.task_id, "skip_task_id": join.task_id},
        retries=0,
    )
    # The direct branch edge makes bypass succeed without masking trigger failures.
    branch >> task >> join
    branch >> join
    return branch, join


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
            description="Single business date to process. Defaults to the Airflow logical date in Asia/Bangkok when blank.",
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
            description="Synthetic scenario label. Scheduled runs default to auto; manual runs default to baseline.",
        ),
        "run_mode": Param(
            "daily",
            type="string",
            description="Logical run mode such as daily, manual, or backfill.",
        ),
        **{
            name: Param(
                default,
                type=["boolean", "string"],
                pattern=r"^([Tt][Rr][Uu][Ee]|[Ff][Aa][Ll][Ss][Ee])(?![\s\S])",
                description=f"Enable {name.removeprefix('run_')} stage. Accepts boolean or true/false string only.",
            )
            for name, default in STAGE_FLAG_DEFAULTS.items()
        },
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
        RUN_DT="{{ dag_run.conf.get("dt") or bangkok_ds(dag_run.logical_date) }}"
        START_DATE="{{ dag_run.conf.get("start_date", "") }}"
        END_DATE="{{ dag_run.conf.get("end_date", "") }}"

        if [ -n "$START_DATE" ] && [ -n "$END_DATE" ]; then
          DATE_ARGS="--start $START_DATE --end $END_DATE"
        elif [ -n "$RUN_DT" ]; then
          DATE_ARGS="--dt $RUN_DT"
        else
          DATE_ARGS=""
        fi

        INCIDENT_SCENARIO="{{ default_incident_scenario(dag_run) }}"
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

        {docker} exec --env AIRFLOW_CTX_DAG_RUN_ID="${{AIRFLOW_CTX_DAG_RUN_ID:-}}" --env AIRFLOW_CTX_DAG_ID="${{AIRFLOW_CTX_DAG_ID:-}}" --env AIRFLOW_CTX_TASK_ID="${{AIRFLOW_CTX_TASK_ID:-}}" {container} /bin/sh -lc "cd /app && {project_command}"
        """
    ).strip()

    logger.info("Built runner bash command | container=%s command=%s", container, project_command)

    return command


def build_runner_plain_bash_command(project_command: str) -> str:
    """
    Build a runner command without pipeline date or incident context.

    Args:
        project_command: Command to run from /app inside the Python runner container.

    Returns:
        Bash command string for code validation and administrative tasks.
    """
    container = runner_container_name()
    docker    = docker_exec_binary()

    command = textwrap.dedent(
        f"""
        set -euo pipefail

        {docker} exec --env AIRFLOW_CTX_DAG_RUN_ID="${{AIRFLOW_CTX_DAG_RUN_ID:-}}" --env AIRFLOW_CTX_DAG_ID="${{AIRFLOW_CTX_DAG_ID:-}}" --env AIRFLOW_CTX_TASK_ID="${{AIRFLOW_CTX_TASK_ID:-}}" {container} /bin/sh -lc "cd /app && {project_command}"
        """
    ).strip()

    logger.info("Built plain runner command | container=%s command=%s", container, project_command)

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


def runner_plain_bash_task(
    task_id: str,
    project_command: str,
    execution_timeout: Any | None = None,
) -> BashOperator:
    """
    Create a BashOperator for non-pipeline commands inside dq_runner.

    Args:
        task_id: Airflow task id.
        project_command: Command to execute from /app in dq_runner.
        execution_timeout: Optional Airflow task timeout.

    Returns:
        Configured BashOperator without date-context shell variables.
    """
    logger.info("Creating plain runner task | task_id=%s", task_id)

    return BashOperator(
        task_id=task_id,
        bash_command=build_runner_plain_bash_command(project_command),
        execution_timeout=execution_timeout,
    )


def single_date_conf() -> dict[str, str]:
    """
    Build standard conf payload for TriggerDagRunOperator child DAGs.

    Returns:
        Conf dictionary forwarding date, scenario, and strictly parsed stage flags.
    """
    return {
        "dt": "{{ dag_run.conf.get(\"dt\") or bangkok_ds(dag_run.logical_date) }}",
        "start_date": "{{ dag_run.conf.get(\"start_date\", \"\") }}",
        "end_date": "{{ dag_run.conf.get(\"end_date\", \"\") }}",
        "incident_scenario": "{{ default_incident_scenario(dag_run) }}",
        "run_mode": "{{ dag_run.conf.get(\"run_mode\", \"daily\") }}",
        **{
            name: f"{{{{ parse_stage_flag(dag_run.conf.get('{name}', {str(default).lower()}), '{name}') }}}}"
            for name, default in STAGE_FLAG_DEFAULTS.items()
        },
    }


logger.info("Airflow helper utilities loaded for Agentic DQ platform")
