####
## Checkpoint Operator Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Validate checkpoint operator requests and build non-executing Airflow previews."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

from agent.checkpointing import (
    SAFE_NODE_NAME,
    build_checkpoint_replay_thread_id,
    build_checkpoint_thread_id,
    validate_checkpoint_thread_id,
)
from pipelines.common.logging import logger


# --- Defining Constants
TRIAGE_DAG_ID         = "40_dag_dq_orders_triage_agent"
TRIAGE_ACTIONS        = ("triage", "inspect", "replay")
DEFAULT_HISTORY_NODE  = "store_report"
DEFAULT_HISTORY_LIMIT = 50

SAFE_ALERT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,511}$")
SAFE_ALERT_ID  = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


# --- Defining Validation Helpers
def clean_optional(value: str | None) -> str:
    """
    Normalize an optional operator string.

    Args:
        value: Optional raw string.

    Returns:
        Stripped value or an empty string.
    """
    return value.strip() if value else ""


def validate_alert_identity(alert_id: str, alert_key: str) -> tuple[str, str]:
    """
    Require exactly one bounded alert identity for checkpoint operations.

    Args:
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional stable system alert key.

    Returns:
        Tuple containing normalized alert id and alert key.

    Raises:
        ValueError: If both, neither, or an unsafe identity is supplied.
    """
    normalized_id  = clean_optional(alert_id)
    normalized_key = clean_optional(alert_key)

    if bool(normalized_id) == bool(normalized_key):
        raise ValueError("Exactly one alert_id or alert_key is required.")

    if normalized_id and not SAFE_ALERT_ID.fullmatch(normalized_id):
        raise ValueError("alert_id must be a canonical UUID.")

    if normalized_key and not SAFE_ALERT_KEY.fullmatch(normalized_key):
        raise ValueError("alert_key contains unsupported characters or exceeds 512 characters.")

    return normalized_id, normalized_key


def validate_triage_inputs(
    action: str,
    alert_id: str,
    alert_key: str,
    checkpoint_namespace: str,
    checkpoint_id: str,
    replay_request_id: str,
    history_limit: int,
    history_next_node: str,
) -> dict[str, Any]:
    """
    Validate one source, inspect, or replay request shared by API and Airflow.

    Args:
        action: Requested operator action.
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional stable system alert key.
        checkpoint_namespace: Existing or new checkpoint namespace.
        checkpoint_id: Exact source checkpoint used by replay.
        replay_request_id: Stable idempotency key for replay.
        history_limit: Maximum checkpoint history rows for inspection.
        history_next_node: Pending node used to select a replay candidate.

    Returns:
        Normalized request dictionary safe for Airflow JSON configuration.

    Raises:
        ValueError: If action-specific requirements or safety constraints fail.
    """
    normalized_action = action.strip().lower()

    if normalized_action not in TRIAGE_ACTIONS:
        raise ValueError(f"Unsupported triage action: {action}")

    normalized_id, normalized_key = validate_alert_identity(alert_id=alert_id, alert_key=alert_key)
    normalized_namespace = validate_checkpoint_thread_id(checkpoint_namespace)
    normalized_checkpoint = clean_optional(checkpoint_id)
    normalized_request    = clean_optional(replay_request_id)
    normalized_node       = history_next_node.strip()

    if not 1 <= int(history_limit) <= 100:
        raise ValueError("history_limit must be between 1 and 100.")

    if not SAFE_NODE_NAME.fullmatch(normalized_node):
        raise ValueError("history_next_node contains unsupported characters.")

    if normalized_action == "replay":
        if not normalized_checkpoint or not normalized_request:
            raise ValueError("Replay requires checkpoint_id and replay_request_id.")

        normalized_checkpoint = validate_checkpoint_thread_id(normalized_checkpoint)
        normalized_request    = validate_checkpoint_thread_id(normalized_request)

    elif normalized_checkpoint or normalized_request:
        raise ValueError("checkpoint_id and replay_request_id are accepted only for replay action.")

    return {
        "action": normalized_action,
        "alert_id": normalized_id,
        "alert_key": normalized_key,
        "checkpoint_namespace": normalized_namespace,
        "checkpoint_id": normalized_checkpoint,
        "replay_request_id": normalized_request,
        "history_limit": int(history_limit),
        "history_next_node": normalized_node,
    }


# --- Defining Airflow Configuration Helpers
def build_trigger_conf(request: dict[str, Any]) -> dict[str, Any]:
    """
    Build bounded DAG 40 configuration from one validated request.

    Args:
        request: Normalized output from validate_triage_inputs.

    Returns:
        Airflow DagRun configuration containing allowlisted fields only.
    """
    action = request["action"]
    conf: dict[str, Any] = {
        "checkpoint_action": "inspect" if action == "inspect" else "triage",
        "run_triage": action != "inspect",
        "alert_id": request["alert_id"],
        "alert_key": request["alert_key"],
        "checkpoint_mode": "sqlite",
        "checkpoint_namespace": request["checkpoint_namespace"],
        "checkpoint_resume": False,
    }

    if action == "inspect":
        conf["checkpoint_history_limit"]     = request["history_limit"]
        conf["checkpoint_history_next_node"] = request["history_next_node"]

    if action == "replay":
        conf["checkpoint_replay_id"]         = request["checkpoint_id"]
        conf["checkpoint_replay_request_id"] = request["replay_request_id"]

    return conf


def build_default_replay_request_id(
    alert_reference: str,
    checkpoint_namespace: str,
    checkpoint_id: str,
) -> str:
    """
    Build one stable replay idempotency key without exposing source identifiers.

    Args:
        alert_reference: Internal alert UUID or system key.
        checkpoint_namespace: Source triage checkpoint namespace.
        checkpoint_id: Exact selected checkpoint identifier.

    Returns:
        Path-safe deterministic replay request identifier.
    """
    digest = hashlib.sha256(
        f"{alert_reference}|{checkpoint_namespace}|{checkpoint_id}".encode("utf-8")
    ).hexdigest()[:16]

    return f"replay-{digest}"


def build_checkpoint_replay_preview(
    *,
    alert_id: str,
    alert_key: str,
    checkpoint_namespace: str,
    checkpoint_id: str,
    replay_request_id: str,
    selected_checkpoint_id: str,
    selected_next_nodes: Iterable[str],
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    history_next_node: str = DEFAULT_HISTORY_NODE,
) -> dict[str, Any]:
    """
    Build a deterministic non-executing replay preview bound to inspected history.

    Args:
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional stable system alert key.
        checkpoint_namespace: Namespace used by the source triage run.
        checkpoint_id: Exact checkpoint selected by the operator.
        replay_request_id: Optional explicit idempotency key.
        selected_checkpoint_id: Current replay candidate returned by inspection.
        selected_next_nodes: Pending nodes recorded by the selected checkpoint.
        history_limit: Inspection bound retained in the preview.
        history_next_node: Required pending graph node.

    Returns:
        Sanitized Airflow-only replay preview with no mutation or trigger side effect.

    Raises:
        ValueError: If the preview is stale or violates DAG 40 contracts.
    """
    normalized_id, normalized_key = validate_alert_identity(alert_id=alert_id, alert_key=alert_key)
    alert_reference = normalized_key or normalized_id
    resolved_request = clean_optional(replay_request_id) or build_default_replay_request_id(
        alert_reference=alert_reference,
        checkpoint_namespace=checkpoint_namespace,
        checkpoint_id=checkpoint_id,
    )
    request = validate_triage_inputs(
        action="replay",
        alert_id=normalized_id,
        alert_key=normalized_key,
        checkpoint_namespace=checkpoint_namespace,
        checkpoint_id=checkpoint_id,
        replay_request_id=resolved_request,
        history_limit=history_limit,
        history_next_node=history_next_node,
    )
    current_checkpoint = validate_checkpoint_thread_id(selected_checkpoint_id)
    current_nodes      = tuple(str(node) for node in selected_next_nodes)

    if request["checkpoint_id"] != current_checkpoint:
        raise ValueError("Replay preview is stale because the selected checkpoint has changed.")

    if request["history_next_node"] not in current_nodes:
        raise ValueError("Selected checkpoint no longer waits for the requested replay node.")

    source_thread_id = build_checkpoint_thread_id(
        namespace=request["checkpoint_namespace"],
        correlation_value=alert_reference,
    )
    replay_thread_id = build_checkpoint_replay_thread_id(
        source_thread_id=source_thread_id,
        source_checkpoint_id=request["checkpoint_id"],
        replay_request_id=request["replay_request_id"],
    )
    preview = {
        "status": "preview",
        "dag_id": TRIAGE_DAG_ID,
        "action": "replay",
        "alert_reference": alert_reference,
        "checkpoint_namespace": request["checkpoint_namespace"],
        "source_thread_id": source_thread_id,
        "source_checkpoint_id": request["checkpoint_id"],
        "source_next_nodes": list(current_nodes),
        "replay_request_id": request["replay_request_id"],
        "replay_thread_id": replay_thread_id,
        "dag_run_conf": build_trigger_conf(request),
        "execution_boundary": "airflow_dag_40",
        "operator_confirmation_required": True,
        "airflow_triggered": False,
        "side_effects_executed": False,
        "raw_state_exposed": False,
        "summary": (
            "Replay preview is valid. No Airflow DagRun was triggered and no report, audit, "
            "alert lifecycle, or remediation side effect was executed."
        ),
    }

    logger.info(
        "Built checkpoint replay preview | dag_id=%s source_thread_id=%s checkpoint_id=%s replay_thread_id=%s",
        TRIAGE_DAG_ID,
        source_thread_id,
        request["checkpoint_id"],
        replay_thread_id,
    )

    return preview
