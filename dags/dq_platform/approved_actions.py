####
## Approved Action Dispatcher Runtime for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


# --- Configuring Project Path
LOCAL_PROJECT_ROOT = Path(__file__).resolve().parents[2]
AIRFLOW_PROJECT_ROOT = Path(os.getenv("DQ_PROJECT_ROOT", "/opt/airflow/project"))
PROJECT_ROOT = (
    AIRFLOW_PROJECT_ROOT if AIRFLOW_PROJECT_ROOT.is_dir() else LOCAL_PROJECT_ROOT
)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.approved_actions import (
    ApprovedActionDeliveryUnknown,
    action_readiness,
    build_approval_control_store,
    execute_approved_action,
)
from agent.tools.audit_log import write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Reading Airflow Configuration
def _conf(context: dict[str, Any]) -> dict[str, Any]:
    """Return a plain DagRun configuration dictionary."""
    dag_run = context.get("dag_run")
    payload = getattr(dag_run, "conf", None)
    return dict(payload) if isinstance(payload, dict) else {}


def _bool(value: Any, default: bool = False) -> bool:
    """Parse one bounded boolean from native or string Airflow parameters."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _execution_actor(context: dict[str, Any]) -> str:
    """Return the Airflow principal that triggered this execution when available."""
    dag_run = context.get("dag_run")
    triggering_user_name = getattr(dag_run, "triggering_user_name", None)
    if triggering_user_name is None:
        triggering_user_name = context.get("triggering_user_name")

    normalized_principal = str(triggering_user_name or "").strip()
    return f"airflow:{normalized_principal}" if normalized_principal else "airflow"


def _audit_input(
    *,
    request_id: str,
    action_type: str,
    execution_run_id: str,
    requested_by: str,
    approved_by: str | None,
) -> dict[str, str]:
    """Build common action audit context without conflating approval and execution actors."""
    return {
        "approval_request_id": request_id,
        "action_type": action_type,
        "execution_run_id": execution_run_id,
        "requested_by": requested_by,
        "approved_by": approved_by or "",
    }


# --- Executing The Airflow Boundary
def run_approved_action_dispatcher(**context: Any) -> dict[str, Any]:
    """Preview or execute one exact-scope transactional approval request."""
    conf = _conf(context)
    request_id = str(conf.get("approval_request_id", "")).strip()
    dry_run = _bool(conf.get("dry_run", True), default=True)
    dag_run = context.get("dag_run")
    execution_run_id = str(getattr(dag_run, "run_id", "")).strip()

    if not request_id:
        raise ValueError("approval_request_id is required.")
    if not execution_run_id:
        raise ValueError("Airflow run_id is required for action execution correlation.")

    store = build_approval_control_store()
    record = store.read_latest(request_id)
    if record is None:
        raise LookupError(f"Approval request was not found: {request_id}")

    clickhouse = build_clickhouse_client()
    actor = _execution_actor(context)
    audit_input = _audit_input(
        request_id=request_id,
        action_type=record.action_type,
        execution_run_id=execution_run_id,
        requested_by=record.requested_by,
        approved_by=record.decided_by,
    )

    if dry_run:
        preview = action_readiness(record)
        write_agent_audit_event(
            client=clickhouse,
            action="approved_action_previewed",
            status="preview",
            actor=actor,
            tool_name="approved_action_dispatcher",
            input_payload=audit_input,
            output_payload=preview,
        )
        logger.info(
            "Approved action preview completed | request_id=%s action=%s ready=%s",
            request_id,
            record.action_type,
            preview["provider_ready"],
        )
        return preview

    try:
        result = execute_approved_action(
            request_id=request_id,
            execution_run_id=execution_run_id,
            store=store,
        )
    except ApprovedActionDeliveryUnknown:
        write_agent_audit_event(
            client=clickhouse,
            action="approved_action_execution_unknown",
            status="unknown",
            actor=actor,
            tool_name="approved_action_dispatcher",
            input_payload=audit_input,
            output_payload={
                "error_type": "ApprovedActionDeliveryUnknown",
                "message": (
                    "Delivery outcome is unknown; inspect the target system before "
                    "considering a retry."
                ),
            },
        )
        raise
    except Exception as exc:
        write_agent_audit_event(
            client=clickhouse,
            action="approved_action_execution_failed",
            status="failed",
            actor=actor,
            tool_name="approved_action_dispatcher",
            input_payload=audit_input,
            output_payload={
                "error_type": type(exc).__name__,
                "message": str(exc)[:500],
            },
        )
        raise

    payload = result.model_dump(mode="json")
    write_agent_audit_event(
        client=clickhouse,
        action="approved_action_executed",
        status=result.status,
        actor=actor,
        tool_name="approved_action_dispatcher",
        input_payload=audit_input,
        output_payload=payload,
    )
    logger.info(
        "Approved action execution completed | request_id=%s action=%s status=%s reference=%s",
        request_id,
        record.action_type,
        result.status,
        result.execution_reference,
    )
    return payload
