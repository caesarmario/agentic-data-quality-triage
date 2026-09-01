####
## Agent Checkpoint Inspection for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read sanitized LangGraph checkpoint history without exposing persisted graph state."""

# --- Importing Libraries
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent.checkpointing import (
    CHECKPOINT_MODE_OFF,
    build_checkpoint_config,
    build_checkpoint_thread_id,
    checkpoint_exists,
    load_checkpoint_settings,
    open_checkpoint_saver,
    select_checkpoint_for_replay,
    summarize_checkpoint_history,
)
from agent.graph import TriageRuntimeConfig, build_triage_graph
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_REPLAY_NEXT_NODE = "store_report"


# --- Defining Input Helpers
def parse_bool_flag(value: str | bool | None, default: bool = False) -> bool:
    """
    Parse one boolean-like runtime value.

    Args:
        value: Boolean or text such as true, false, 1, 0, yes, or no.
        default: Value returned when the input is blank.

    Returns:
        Parsed boolean value.

    Raises:
        ValueError: If the value is not a supported boolean representation.
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


def clean_optional(value: str | None) -> str | None:
    """
    Normalize one optional string.

    Args:
        value: Raw optional value.

    Returns:
        Stripped value, or None when blank.
    """
    if value is None:
        return None

    normalized = value.strip()

    return normalized or None


def resolve_checkpoint_correlation(alert_id: str | None, alert_key: str | None) -> str:
    """
    Resolve the same checkpoint correlation value used by the triage runner.

    Args:
        alert_id: Optional alert UUID string.
        alert_key: Optional stable system alert key.

    Returns:
        Alert key when present, otherwise alert id.

    Raises:
        ValueError: If both or neither alert identities are supplied.
    """
    normalized_id  = clean_optional(alert_id)
    normalized_key = clean_optional(alert_key)

    if bool(normalized_id) == bool(normalized_key):
        raise ValueError("Checkpoint inspection requires exactly one explicit alert_id or alert_key.")

    return normalized_key or normalized_id or ""


# --- Defining Inspection Runtime
def inspect_checkpoint_history(
    enabled: str | bool | None,
    alert_id: str | None,
    alert_key: str | None,
    checkpoint_mode: str = CHECKPOINT_MODE_OFF,
    checkpoint_namespace: str | None = None,
    checkpoint_sqlite_path: str | None = None,
    checkpoint_busy_timeout_ms: int | None = None,
    history_limit: int = 50,
    select_next_node: str = DEFAULT_REPLAY_NEXT_NODE,
    runtime_config: TriageRuntimeConfig | None = None,
) -> dict[str, Any]:
    """
    Read sanitized checkpoint history and select one replay candidate.

    Args:
        enabled: Whether this invocation should inspect checkpoint history.
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        checkpoint_mode: Checkpoint backend mode. Inspection requires SQLite.
        checkpoint_namespace: Namespace used by the source triage run.
        checkpoint_sqlite_path: Optional absolute SQLite path override.
        checkpoint_busy_timeout_ms: Optional SQLite lock timeout.
        history_limit: Maximum number of newest checkpoints to inspect.
        select_next_node: Exact pending node used to select the replay candidate.
        runtime_config: Optional triage graph runtime configuration.

    Returns:
        JSON-serializable sanitized history and selected checkpoint metadata.

    Raises:
        ValueError: If inputs, source history, or replay candidate are invalid.
    """
    if not parse_bool_flag(enabled, default=False):
        logger.info("Checkpoint inspection disabled; returning a no-op result")

        return {
            "status": "skipped",
            "reason": "checkpoint_inspection_disabled",
            "history": [],
            "selected_checkpoint": None,
        }

    namespace = clean_optional(checkpoint_namespace)

    if not namespace:
        raise ValueError("checkpoint_namespace is required for checkpoint inspection.")

    correlation = resolve_checkpoint_correlation(alert_id=alert_id, alert_key=alert_key)
    settings    = load_checkpoint_settings(
        mode=checkpoint_mode,
        sqlite_path=clean_optional(checkpoint_sqlite_path),
        busy_timeout_ms=checkpoint_busy_timeout_ms,
    )

    if not settings.enabled:
        raise ValueError("Checkpoint inspection requires an enabled checkpoint backend.")

    thread_id    = build_checkpoint_thread_id(namespace=namespace, correlation_value=correlation)
    graph_config = build_checkpoint_config(thread_id)
    config       = runtime_config or TriageRuntimeConfig()

    logger.info(
        "Inspecting checkpoint history | thread_id=%s history_limit=%d select_next_node=%s",
        thread_id,
        history_limit,
        select_next_node,
    )

    with open_checkpoint_saver(settings) as checkpointer:
        if checkpointer is None:
            raise ValueError("Checkpoint inspection requires the SQLite saver.")

        graph = build_triage_graph(config=config, checkpointer=checkpointer)

        if not checkpoint_exists(checkpointer=checkpointer, config=graph_config):
            raise ValueError("Checkpoint source thread does not exist for the supplied alert and namespace.")

        history = summarize_checkpoint_history(
            graph=graph,
            config=graph_config,
            limit=history_limit,
        )

    selected, matching_count = select_checkpoint_for_replay(
        history=history,
        next_node=select_next_node,
    )
    result = {
        "status": "success",
        "thread_id": thread_id,
        "checkpoint_namespace": namespace,
        "history_count": len(history),
        "matching_checkpoint_count": matching_count,
        "selected_checkpoint": asdict(selected),
        "history": [asdict(summary) for summary in history],
        "raw_state_exposed": False,
        "read_only": True,
    }

    logger.info(
        "Checkpoint inspection completed | thread_id=%s selected_checkpoint_id=%s history_count=%d",
        thread_id,
        selected.checkpoint_id,
        len(history),
    )

    return result
