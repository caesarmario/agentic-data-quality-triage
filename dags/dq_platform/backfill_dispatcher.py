####
## Airflow Backfill Dispatcher Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


# --- Configuring Project Path
LOCAL_PROJECT_ROOT   = Path(__file__).resolve().parents[2]
AIRFLOW_PROJECT_ROOT = Path(os.getenv("DQ_PROJECT_ROOT", "/opt/airflow/project"))
PROJECT_ROOT         = AIRFLOW_PROJECT_ROOT if AIRFLOW_PROJECT_ROOT.is_dir() else LOCAL_PROJECT_ROOT

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.approval_queue import (
    ApprovalExecutionStatus,
    ApprovalRequest,
    backfill_parameters_from_conf,
    require_approved_backfill_request,
    transition_approval_execution,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
DEFAULT_TARGET_DAG_ID      = "00_dag_dq_platform_daily_orchestrator"
DEFAULT_MAX_DATES          = 14
DEFAULT_POLL_INTERVAL_SEC  = 15
DEFAULT_WAIT_TIMEOUT_SEC   = 60 * 60
BANGKOK_TIMEZONE           = ZoneInfo("Asia/Bangkok")

ALLOWED_TARGET_DAG_IDS = {
    "00_dag_dq_platform_daily_orchestrator",
    "10_dag_dq_orders_landing_orchestrator",
    "11_dag_dq_orders_seed_to_s3",
    "12_dag_dq_orders_load_raw_clickhouse",
    "20_dag_dq_orders_dbt_transform",
    "30_dag_dq_orders_quality_alerts",
    "40_dag_dq_orders_triage_agent",
}


# --- Defining Functions
def parse_bool(value: Any, default: bool = False) -> bool:
    """
    Parse boolean-like values from Airflow dag_run.conf.

    Args:
        value: Raw boolean-like value.
        default: Fallback when the value is blank or None.

    Returns:
        Parsed boolean.

    Raises:
        ValueError: If the value cannot be interpreted as boolean.
    """
    if value is None or value == "":
        return default

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True

    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False

    raise ValueError(f"Invalid boolean value: {value}")


def parse_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """
    Parse and clamp integer-like values from Airflow dag_run.conf.

    Args:
        value: Raw integer-like value.
        default: Fallback when the value is blank or None.
        minimum: Minimum accepted value.
        maximum: Maximum accepted value.

    Returns:
        Parsed integer within the configured bounds.

    Raises:
        ValueError: If the value cannot be parsed as integer.
    """
    if value is None or value == "":
        parsed = default

    else:
        parsed = int(value)

    return max(minimum, min(parsed, maximum))


def parse_business_date(value: str) -> date:
    """
    Parse a YYYY-MM-DD business date.

    Args:
        value: Date string in YYYY-MM-DD format.

    Returns:
        Parsed date object.

    Raises:
        ValueError: If the value is blank or invalid.
    """
    if not value:
        raise ValueError("Business date cannot be blank.")

    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_backfill_dates(start_date: date, end_date: date, max_dates: int) -> list[date]:
    """
    Build an inclusive list of dates to backfill.

    Args:
        start_date: Inclusive start date.
        end_date: Inclusive end date.
        max_dates: Safety cap for date count.

    Returns:
        Inclusive list of dates.

    Raises:
        ValueError: If the date range is invalid or exceeds max_dates.
    """
    if end_date < start_date:
        raise ValueError(f"end_date must be >= start_date: {start_date} to {end_date}")

    dates        = []
    current_date = start_date

    while current_date <= end_date:
        dates.append(current_date)
        current_date = current_date + timedelta(days=1)

    if len(dates) > max_dates:
        raise ValueError(f"Backfill date count {len(dates)} exceeds max_dates={max_dates}.")

    return dates


def validate_target_dag_id(target_dag_id: str) -> str:
    """
    Validate a target DAG id against the dispatcher allowlist.

    Args:
        target_dag_id: Candidate DAG id.

    Returns:
        Validated DAG id.

    Raises:
        ValueError: If target_dag_id is blank or not allowed.
    """
    if not target_dag_id:
        raise ValueError("target_dag_id cannot be blank.")

    if target_dag_id not in ALLOWED_TARGET_DAG_IDS:
        raise ValueError(f"target_dag_id is not allowed: {target_dag_id}")

    return target_dag_id


def conf_value(conf: dict[str, Any], key: str, default: Any = "") -> Any:
    """
    Read one value from dag_run.conf with a fallback.

    Args:
        conf: Airflow dag_run.conf dictionary.
        key: Configuration key to read.
        default: Fallback when the key is missing.

    Returns:
        Raw configuration value.
    """
    return conf.get(key, default)


def build_child_conf(
    parent_conf: dict[str, Any],
    run_dt: date,
    target_dag_id: str,
    requested_by: str,
    reason: str,
    reset_dag_run: bool,
) -> dict[str, Any]:
    """
    Build the child DAG conf payload for one backfilled date.

    Args:
        parent_conf: Parent dispatcher dag_run.conf.
        run_dt: Business date to trigger.
        target_dag_id: Target DAG receiving this conf.
        requested_by: Human or agent that requested the backfill.
        reason: Reason captured for auditability.
        reset_dag_run: Whether the requester intended duplicate child runs to be reset.

    Returns:
        Conf dictionary sent to the target DAG.
    """
    return {
        "dt": run_dt.isoformat(),
        "start_date": "",
        "end_date": "",
        "target_dag_id": target_dag_id,
        "incident_scenario": conf_value(parent_conf, "incident_scenario", "baseline"),
        "run_mode": conf_value(parent_conf, "run_mode", "backfill"),
        "run_seed": parse_bool(conf_value(parent_conf, "run_seed", True), default=True),
        "run_load": parse_bool(conf_value(parent_conf, "run_load", True), default=True),
        "run_dbt": parse_bool(conf_value(parent_conf, "run_dbt", True), default=True),
        "run_dq": parse_bool(conf_value(parent_conf, "run_dq", True), default=True),
        "run_triage": parse_bool(conf_value(parent_conf, "run_triage", False), default=False),
        "max_alerts": parse_int(conf_value(parent_conf, "max_alerts", 5), default=5, minimum=1, maximum=20),
        "requested_by": requested_by,
        "reason": reason,
        "reset_dag_run": reset_dag_run,
        "backfill_dispatcher": "90_dag_dq_platform_backfill_dispatcher",
        "approval_request_id": str(conf_value(parent_conf, "approval_request_id", "")).strip(),
    }


def build_child_run_id(target_dag_id: str, run_dt: date, parent_run_id: str) -> str:
    """
    Build an audit-friendly unique child run id.

    Args:
        target_dag_id: Target DAG id.
        run_dt: Business date being triggered.
        parent_run_id: Parent dispatcher run id.

    Returns:
        Airflow run_id for the child DAG.
    """
    safe_parent = "".join(char if char.isalnum() else "_" for char in parent_run_id)[-64:]

    return f"backfill__{target_dag_id}__dt_{run_dt.isoformat()}__{safe_parent}"


def run_airflow_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    """
    Run an Airflow CLI command and return its completed process.

    Args:
        command: Command argument list.

    Returns:
        CompletedProcess with captured output.

    Raises:
        subprocess.CalledProcessError: If the command exits non-zero.
    """
    logger.info("Running Airflow CLI command | command=%s", " ".join(command))

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    if completed.stdout:
        print(completed.stdout)

    if completed.stderr:
        print(completed.stderr)

    if completed.returncode != 0:
        logger.error("Airflow CLI command failed | return_code=%s", completed.returncode)
        raise subprocess.CalledProcessError(
            returncode=completed.returncode,
            cmd=command,
            output=completed.stdout,
            stderr=completed.stderr,
        )

    return completed


def trigger_child_dag(
    target_dag_id: str,
    run_id: str,
    run_dt: date,
    child_conf: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    """
    Trigger one target DAG run for one business date.

    Args:
        target_dag_id: Target Airflow DAG id.
        run_id: Child run id.
        run_dt: Business date being triggered.
        child_conf: Conf payload sent to the child DAG.
        dry_run: When true, only preview the trigger request.

    Returns:
        Trigger metadata dictionary.
    """
    logical_date = f"{run_dt.isoformat()}T00:05:00+07:00"
    command      = [
        "airflow",
        "dags",
        "trigger",
        target_dag_id,
        "--run-id",
        run_id,
        "--logical-date",
        logical_date,
        "--conf",
        json.dumps(child_conf, ensure_ascii=True),
    ]

    if dry_run:
        logger.info("Dry-run backfill trigger preview | target=%s run_id=%s", target_dag_id, run_id)

        return {
            "status": "dry_run",
            "target_dag_id": target_dag_id,
            "run_id": run_id,
            "dt": run_dt.isoformat(),
            "logical_date": logical_date,
            "conf": child_conf,
            "command": command,
        }

    run_airflow_command(command)

    return {
        "status": "triggered",
        "target_dag_id": target_dag_id,
        "run_id": run_id,
        "dt": run_dt.isoformat(),
        "logical_date": logical_date,
        "conf": child_conf,
    }


def fetch_dag_run_state(target_dag_id: str, run_id: str) -> str:
    """
    Fetch current state for one target DAG run.

    Args:
        target_dag_id: Target DAG id.
        run_id: Child run id.

    Returns:
        Airflow DAG run state string.
    """
    completed = run_airflow_command(["airflow", "dags", "state", target_dag_id, run_id])
    state     = completed.stdout.strip().splitlines()[-1].strip()

    logger.info("Fetched child DAG state | target=%s run_id=%s state=%s", target_dag_id, run_id, state)

    return state


def wait_for_child_completion(
    target_dag_id: str,
    run_id: str,
    poll_interval_sec: int,
    timeout_sec: int,
) -> str:
    """
    Poll a child DAG run until it reaches a terminal state.

    Args:
        target_dag_id: Target DAG id.
        run_id: Child run id.
        poll_interval_sec: Seconds between state checks.
        timeout_sec: Maximum seconds to wait.

    Returns:
        Final DAG run state.

    Raises:
        TimeoutError: If the child run does not finish before timeout.
    """
    started_at = time.monotonic()

    while True:
        state = fetch_dag_run_state(target_dag_id=target_dag_id, run_id=run_id)

        if state in {"success", "failed"}:
            return state

        if time.monotonic() - started_at > timeout_sec:
            raise TimeoutError(f"Timed out waiting for {target_dag_id}:{run_id}")

        time.sleep(poll_interval_sec)


def validate_execution_approval(
    parent_conf: dict[str, Any],
    dry_run: bool,
    target_dag_id: str,
    start_date: date,
    end_date: date,
) -> ApprovalRequest | None:
    """
    Enforce a durable exact-scope approval before creating child DAG runs.

    Args:
        parent_conf: Raw dispatcher dag_run.conf dictionary.
        dry_run: Whether the dispatcher is preview-only.
        target_dag_id: Operational DAG proposed for execution.
        start_date: Inclusive proposed start date.
        end_date: Inclusive proposed end date.

    Returns:
        Matching approved request for real execution, otherwise None for dry-run.

    Raises:
        ValueError: If a real execution lacks a matching approved request.
    """
    if dry_run:
        logger.info("Approval gate bypassed for non-mutating dry-run preview")
        return None

    approval_request_id = str(conf_value(parent_conf, "approval_request_id", "")).strip()
    execution_parameters = backfill_parameters_from_conf(parent_conf)

    approval = require_approved_backfill_request(
        request_id=approval_request_id,
        target_dag_id=target_dag_id,
        start_date=start_date,
        end_date=end_date,
        parameters=execution_parameters,
    )

    logger.info(
        "Backfill execution authorized by durable approval | request_id=%s decided_by=%s",
        approval.request_id,
        approval.decided_by,
    )

    return approval


def mark_approval_execution_failed(
    request_id: str,
    parent_run_id: str,
    requested_by: str,
    error: Exception,
) -> None:
    """
    Persist a failed approval execution without hiding the original dispatcher error.

    Args:
        request_id: Human-facing APR request identifier.
        parent_run_id: Parent Airflow dispatcher DagRun identifier.
        requested_by: Operator identity from dag_run.conf.
        error: Original dispatch or child execution exception.

    Returns:
        None. Persistence failures are logged and remain secondary to the original error.
    """
    try:
        transition_approval_execution(
            request_id=request_id,
            execution_status=ApprovalExecutionStatus.FAILED,
            execution_dag_run_id=parent_run_id,
            error_message=f"{type(error).__name__}: {error}",
            actor=f"airflow:{requested_by}",
        )

    except Exception:
        logger.exception(
            "Failed to persist approval execution failure | request_id=%s parent_run_id=%s",
            request_id,
            parent_run_id,
        )


def run_backfill_dispatcher(**context: Any) -> dict[str, Any]:
    """
    Dispatch one target DAG run per date in the requested backfill window.

    Args:
        **context: Airflow task context containing dag_run and run metadata.

    Returns:
        Summary dictionary with trigger previews/results and execution state.

    Raises:
        RuntimeError: If a child run fails or dispatcher execution cannot complete.
    """
    dag_run     = context["dag_run"]
    parent_conf = dict(dag_run.conf or {})
    parent_id   = str(dag_run.run_id)

    target_dag_id       = validate_target_dag_id(conf_value(parent_conf, "target_dag_id", DEFAULT_TARGET_DAG_ID))
    requested_by        = str(conf_value(parent_conf, "requested_by", "manual")).strip() or "manual"
    reason              = str(conf_value(parent_conf, "reason", "manual_backfill")).strip() or "manual_backfill"
    start_dt            = parse_business_date(str(conf_value(parent_conf, "start_date", "")))
    end_dt              = parse_business_date(str(conf_value(parent_conf, "end_date", "")))
    max_dates           = parse_int(conf_value(parent_conf, "max_dates", DEFAULT_MAX_DATES), DEFAULT_MAX_DATES, 1, 90)
    dry_run             = parse_bool(conf_value(parent_conf, "dry_run", True), default=True)
    reset_dag_run       = parse_bool(conf_value(parent_conf, "reset_dag_run", False), default=False)
    wait_for_completion = parse_bool(conf_value(parent_conf, "wait_for_completion", False), default=False)
    fail_fast           = parse_bool(conf_value(parent_conf, "fail_fast", True), default=True)
    poll_interval_sec   = parse_int(conf_value(parent_conf, "poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC), DEFAULT_POLL_INTERVAL_SEC, 5, 300)
    timeout_sec         = parse_int(conf_value(parent_conf, "timeout_sec", DEFAULT_WAIT_TIMEOUT_SEC), DEFAULT_WAIT_TIMEOUT_SEC, 60, 86400)
    run_dates           = iter_backfill_dates(start_date=start_dt, end_date=end_dt, max_dates=max_dates)
    approval            = validate_execution_approval(
        parent_conf=parent_conf,
        dry_run=dry_run,
        target_dag_id=target_dag_id,
        start_date=start_dt,
        end_date=end_dt,
    )
    approval_request_id = approval.request_id if approval is not None else ""

    logger.info(
        "Starting backfill dispatcher | target=%s start=%s end=%s dates=%d dry_run=%s requested_by=%s approval_request_id=%s",
        target_dag_id,
        start_dt,
        end_dt,
        len(run_dates),
        dry_run,
        requested_by,
        approval_request_id or "not_required_for_dry_run",
    )

    results          = []
    child_failures   = []
    approval_claimed = False
    execution_status = "preview"

    try:
        if approval is not None:
            transition_approval_execution(
                request_id=approval.request_id,
                execution_status=ApprovalExecutionStatus.DISPATCHING,
                execution_dag_run_id=parent_id,
                actor=f"airflow:{requested_by}",
            )
            approval_claimed = True

            logger.info(
                "Claimed approval request for single-use execution | request_id=%s parent_run_id=%s",
                approval.request_id,
                parent_id,
            )

        for run_dt in run_dates:
            child_conf = build_child_conf(
                parent_conf=parent_conf,
                run_dt=run_dt,
                target_dag_id=target_dag_id,
                requested_by=requested_by,
                reason=reason,
                reset_dag_run=reset_dag_run,
            )
            run_id = build_child_run_id(target_dag_id=target_dag_id, run_dt=run_dt, parent_run_id=parent_id)
            result = trigger_child_dag(
                target_dag_id=target_dag_id,
                run_id=run_id,
                run_dt=run_dt,
                child_conf=child_conf,
                dry_run=dry_run,
            )

            if wait_for_completion and not dry_run:
                final_state = wait_for_child_completion(
                    target_dag_id=target_dag_id,
                    run_id=run_id,
                    poll_interval_sec=poll_interval_sec,
                    timeout_sec=timeout_sec,
                )
                result["final_state"] = final_state

                if final_state != "success":
                    child_failures.append(f"{target_dag_id}:{run_id}:{final_state}")

                    if fail_fast:
                        results.append(result)
                        raise RuntimeError(
                            f"Child DAG failed and fail_fast=true: {target_dag_id}:{run_id}"
                        )

            results.append(result)

        if child_failures:
            raise RuntimeError(
                "One or more child DAG runs failed after complete date-range dispatch: "
                + ", ".join(child_failures)
            )

        if approval is not None:
            final_execution_status = (
                ApprovalExecutionStatus.SUCCEEDED
                if wait_for_completion
                else ApprovalExecutionStatus.DISPATCHED
            )
            final_approval, _ = transition_approval_execution(
                request_id=approval.request_id,
                execution_status=final_execution_status,
                execution_dag_run_id=parent_id,
                actor=f"airflow:{requested_by}",
            )
            execution_status = final_execution_status.value

    except Exception as exc:
        if approval_claimed and approval_request_id:
            mark_approval_execution_failed(
                request_id=approval_request_id,
                parent_run_id=parent_id,
                requested_by=requested_by,
                error=exc,
            )

        logger.exception(
            "Backfill dispatcher failed | target=%s approval_request_id=%s parent_run_id=%s",
            target_dag_id,
            approval_request_id,
            parent_id,
        )
        raise

    summary = {
        "status": "success",
        "dry_run": dry_run,
        "reset_dag_run": reset_dag_run,
        "target_dag_id": target_dag_id,
        "requested_by": requested_by,
        "reason": reason,
        "approval_request_id": approval_request_id,
        "execution_status": execution_status,
        "date_count": len(run_dates),
        "results": results,
        "dispatched_at": datetime.now(BANGKOK_TIMEZONE).isoformat(),
    }

    print(json.dumps(summary, indent=2, default=str))
    logger.info("Backfill dispatcher completed | summary=%s", summary)

    return summary
