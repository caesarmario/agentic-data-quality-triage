####
## LangGraph Checkpointing for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Bounded persistent checkpoint support for local LangGraph triage runs."""

# --- Importing Libraries
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

from pipelines.common.logging import logger


# --- Defining Constants
CHECKPOINT_MODE_OFF       = "off"
CHECKPOINT_MODE_SQLITE    = "sqlite"
DEFAULT_CHECKPOINT_PATH   = "/var/lib/agent-checkpoints/triage.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS   = 5_000
MAX_BUSY_TIMEOUT_MS       = 60_000
MAX_CHECKPOINT_HISTORY    = 100
MAX_CHECKPOINT_THREAD_LEN = 160

SUPPORTED_CHECKPOINT_MODES = (
    CHECKPOINT_MODE_OFF,
    CHECKPOINT_MODE_SQLITE,
)

SAFE_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
SAFE_NODE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
SAFE_CHECKPOINT_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

# Checkpoint deserialization is fail-closed. Only project state models required by
# TriageState may be reconstructed in addition to LangGraph's built-in safe types.
CHECKPOINT_ALLOWED_MSGPACK_MODULES = (
    ("agent.state", "AlertStatus"),
    ("agent.state", "Severity"),
    ("agent.state", "EvidenceType"),
    ("agent.state", "EvidenceCategory"),
    ("agent.state", "ToolStatus"),
    ("agent.state", "ApprovalActionType"),
    ("agent.state", "Alert"),
    ("agent.state", "EvidenceRequest"),
    ("agent.state", "EvidencePlan"),
    ("agent.state", "EvidenceItem"),
    ("agent.state", "Hypothesis"),
    ("agent.state", "HypothesisFraming"),
    ("agent.state", "ApprovalGatedAction"),
    ("agent.state", "ToolAuditEvent"),
    ("agent.state", "TriageReport"),
    ("agent.state", "TriageState"),
)


# --- Defining Classes
@dataclass(frozen=True)
class CheckpointSettings:
    """
    Runtime settings for one LangGraph checkpoint session.

    Attributes:
        mode: Checkpoint backend mode. Supported values are off and sqlite.
        sqlite_path: Absolute SQLite database path used when mode is sqlite.
        busy_timeout_ms: Maximum SQLite lock wait in milliseconds.
    """

    mode: str                  = CHECKPOINT_MODE_OFF
    sqlite_path: str           = DEFAULT_CHECKPOINT_PATH
    busy_timeout_ms: int       = DEFAULT_BUSY_TIMEOUT_MS

    @property
    def enabled(self) -> bool:
        """
        Return whether persistent checkpointing is enabled.

        Returns:
            True only when the configured mode is not off.
        """
        return self.mode != CHECKPOINT_MODE_OFF


@dataclass(frozen=True)
class CheckpointSnapshotSummary:
    """
    Sanitized metadata for one persisted LangGraph checkpoint.

    Attributes:
        checkpoint_id: LangGraph-generated checkpoint identifier.
        created_at: ISO timestamp recorded by LangGraph.
        step: Graph super-step number.
        source: Checkpoint source such as input, loop, or update.
        next_nodes: Nodes scheduled after this checkpoint.
        is_complete: Whether the checkpoint has no remaining nodes.
    """

    checkpoint_id: str
    created_at: str
    step: int
    source: str
    next_nodes: tuple[str, ...]
    is_complete: bool


@dataclass(frozen=True)
class HistoricalCheckpointReplay:
    """
    Sanitized execution metadata for one branched historical replay.

    Attributes:
        source_thread_id: Original checkpoint thread retained without modification.
        source_checkpoint_id: Exact historical checkpoint selected by the operator.
        replay_thread_id: Deterministic child thread used for replay execution.
        source_writer_node: Node that produced the selected historical checkpoint.
        source_next_nodes: Nodes pending at the selected historical checkpoint.
        executed_pending_nodes: Whether this invocation executed pending replay nodes.
    """

    source_thread_id: str
    source_checkpoint_id: str
    replay_thread_id: str
    source_writer_node: str
    source_next_nodes: tuple[str, ...]
    executed_pending_nodes: bool


# --- Defining Configuration Helpers
def normalize_checkpoint_mode(value: str | None) -> str:
    """
    Normalize and validate a checkpoint backend mode.

    Args:
        value: Raw mode value from code, environment, or an Airflow parameter.

    Returns:
        Normalized checkpoint mode.

    Raises:
        ValueError: If the mode is not in the supported allowlist.
    """
    normalized = (value or CHECKPOINT_MODE_OFF).strip().lower()

    if normalized not in SUPPORTED_CHECKPOINT_MODES:
        allowed = ", ".join(SUPPORTED_CHECKPOINT_MODES)
        raise ValueError(f"Unsupported checkpoint mode: {value}. Allowed modes: {allowed}.")

    return normalized


def load_checkpoint_settings(
    mode: str | None = None,
    sqlite_path: str | None = None,
    busy_timeout_ms: int | None = None,
) -> CheckpointSettings:
    """
    Load checkpoint settings with explicit arguments taking precedence over environment values.

    Args:
        mode: Optional checkpoint mode override.
        sqlite_path: Optional SQLite path override.
        busy_timeout_ms: Optional SQLite lock timeout override.

    Returns:
        Validated immutable checkpoint settings.

    Raises:
        ValueError: If the mode, path, or timeout is invalid.
    """
    resolved_mode = normalize_checkpoint_mode(mode or os.getenv("AGENT_CHECKPOINT_MODE", CHECKPOINT_MODE_OFF))
    resolved_path = (
        sqlite_path
        or os.getenv("AGENT_CHECKPOINT_SQLITE_PATH", DEFAULT_CHECKPOINT_PATH)
    ).strip()
    timeout_raw = busy_timeout_ms

    if timeout_raw is None:
        timeout_raw = int(os.getenv("AGENT_CHECKPOINT_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS)))

    if timeout_raw < 1 or timeout_raw > MAX_BUSY_TIMEOUT_MS:
        raise ValueError(f"Checkpoint busy timeout must be between 1 and {MAX_BUSY_TIMEOUT_MS} milliseconds.")

    if resolved_mode == CHECKPOINT_MODE_SQLITE:
        checkpoint_path = Path(resolved_path).expanduser()

        if not checkpoint_path.is_absolute():
            raise ValueError("Checkpoint SQLite path must be absolute.")

        resolved_path = str(checkpoint_path)

    settings = CheckpointSettings(
        mode=resolved_mode,
        sqlite_path=resolved_path,
        busy_timeout_ms=timeout_raw,
    )

    logger.info(
        "Resolved checkpoint settings | mode=%s enabled=%s sqlite_path=%s busy_timeout_ms=%d",
        settings.mode,
        settings.enabled,
        settings.sqlite_path if settings.enabled else "disabled",
        settings.busy_timeout_ms,
    )

    return settings


def validate_checkpoint_thread_id(thread_id: str) -> str:
    """
    Validate an operator-visible checkpoint thread identifier.

    Args:
        thread_id: Raw LangGraph thread identifier.

    Returns:
        Validated thread identifier.

    Raises:
        ValueError: If the identifier is blank, too long, or contains unsafe characters.
    """
    normalized = thread_id.strip()

    if not SAFE_THREAD_ID.fullmatch(normalized):
        raise ValueError(
            "Checkpoint thread id must start with an alphanumeric character and contain only "
            "letters, numbers, dot, underscore, colon, or hyphen."
        )

    return normalized


def build_checkpoint_thread_id(namespace: str, correlation_value: str) -> str:
    """
    Build a stable path-safe thread id without exposing a full alert key.

    Args:
        namespace: Run-scoped namespace such as an Airflow DagRun identifier.
        correlation_value: Alert id or internal alert key used only for hashing.

    Returns:
        Stable checkpoint thread identifier for one alert inside one run namespace.

    Raises:
        ValueError: If namespace or correlation value is blank.
    """
    raw_namespace = namespace.strip()
    raw_value     = correlation_value.strip()

    if not raw_namespace:
        raise ValueError("Checkpoint namespace must not be blank.")

    if not raw_value:
        raise ValueError("Checkpoint correlation value must not be blank.")

    safe_namespace = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw_namespace).strip("-._:")
    safe_namespace = safe_namespace[:120] or "triage"
    digest         = hashlib.sha256(f"{raw_namespace}|{raw_value}".encode("utf-8")).hexdigest()[:16]
    thread_id      = f"{safe_namespace}:{digest}"

    if len(thread_id) > MAX_CHECKPOINT_THREAD_LEN:
        thread_id = f"{safe_namespace[: MAX_CHECKPOINT_THREAD_LEN - 17]}:{digest}"

    return validate_checkpoint_thread_id(thread_id)


def build_checkpoint_config(
    thread_id: str,
    checkpoint_id: str | None = None,
    checkpoint_namespace: str | None = None,
) -> dict[str, dict[str, str]]:
    """
    Build the LangGraph configurable payload for checkpoint access.

    Args:
        thread_id: Validated checkpoint thread identifier.
        checkpoint_id: Optional historical checkpoint identifier for read-only inspection.
        checkpoint_namespace: Optional supervisor or child-subgraph namespace.

    Returns:
        Runnable config dictionary accepted by LangGraph.

    Raises:
        ValueError: If thread or checkpoint identifiers are unsafe.
    """
    configurable = {"thread_id": validate_checkpoint_thread_id(thread_id)}

    if checkpoint_id:
        normalized_checkpoint_id = checkpoint_id.strip()

        if not SAFE_THREAD_ID.fullmatch(normalized_checkpoint_id):
            raise ValueError("Checkpoint id contains unsupported characters.")

        configurable["checkpoint_id"] = normalized_checkpoint_id

    if checkpoint_namespace:
        normalized_namespace = checkpoint_namespace.strip()

        if not SAFE_CHECKPOINT_NAMESPACE.fullmatch(normalized_namespace):
            raise ValueError("Checkpoint namespace contains unsupported characters.")

        configurable["checkpoint_ns"] = normalized_namespace

    return {"configurable": configurable}


def build_supervisor_checkpoint_namespace(parent_run_id: str) -> str:
    """
    Build one stable checkpoint namespace for the parent supervisor graph.

    Args:
        parent_run_id: Stable parent supervisor UUID or correlation identifier.

    Returns:
        Validated namespace isolated from child specialist checkpoints.
    """
    normalized = parent_run_id.strip()

    if not normalized:
        raise ValueError("Supervisor checkpoint parent run id must not be blank.")

    digest    = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    namespace = f"supervisor:{digest}"

    if not SAFE_CHECKPOINT_NAMESPACE.fullmatch(namespace):
        raise ValueError("Generated supervisor checkpoint namespace is unsafe.")

    return namespace


def build_specialist_checkpoint_namespace(
    parent_run_id: str,
    task_id: str,
    specialist_name: str,
) -> str:
    """
    Build one immutable per-invocation namespace for a specialist worker.

    Args:
        parent_run_id: Stable parent supervisor correlation identifier.
        task_id: Immutable worker task identifier.
        specialist_name: Registry specialist name used for operator diagnostics.

    Returns:
        Validated child namespace unique to the parent, task, and specialist.
    """
    parent     = parent_run_id.strip()
    task       = task_id.strip()
    specialist = specialist_name.strip().lower()

    if not parent or not task or not specialist:
        raise ValueError("Specialist checkpoint namespace requires parent, task, and specialist identifiers.")

    safe_specialist = re.sub(r"[^a-z0-9_]+", "_", specialist).strip("_")[:48]
    digest          = hashlib.sha256(
        f"{parent}|{task}|{specialist}".encode("utf-8")
    ).hexdigest()[:20]
    namespace       = f"child:{safe_specialist}:{digest}"

    if not SAFE_CHECKPOINT_NAMESPACE.fullmatch(namespace):
        raise ValueError("Generated specialist checkpoint namespace is unsafe.")

    return namespace


def build_checkpoint_replay_thread_id(
    source_thread_id: str,
    source_checkpoint_id: str,
    replay_request_id: str,
) -> str:
    """
    Build a deterministic child thread for one historical replay request.

    Args:
        source_thread_id: Existing checkpoint thread selected for replay.
        source_checkpoint_id: Exact historical checkpoint identifier.
        replay_request_id: Stable operator or Airflow replay request identifier.

    Returns:
        Path-safe replay thread id derived from all source identifiers.

    Raises:
        ValueError: If any replay identifier is blank or unsafe.
    """
    validated_source  = validate_checkpoint_thread_id(source_thread_id)
    validated_config  = build_checkpoint_config(validated_source, source_checkpoint_id)
    validated_checkpoint = validated_config["configurable"]["checkpoint_id"]
    validated_request = validate_checkpoint_thread_id(replay_request_id)

    return build_checkpoint_thread_id(
        namespace=f"{validated_source}:historical-replay",
        correlation_value=f"{validated_checkpoint}|{validated_request}",
    )


# --- Defining Saver Lifecycle
@contextmanager
def open_checkpoint_saver(settings: CheckpointSettings) -> Iterator[Any | None]:
    """
    Open an optional SQLite LangGraph saver for one bounded execution.

    Args:
        settings: Validated checkpoint runtime settings.

    Yields:
        SqliteSaver when enabled, otherwise None.

    Raises:
        ImportError: If the optional SQLite checkpoint package is unavailable.
        sqlite3.Error: If the database cannot be opened or initialized.
    """
    if not settings.enabled:
        yield None
        return

    # Lazy import preserves the existing default-off runtime when the optional
    # checkpoint dependency has not yet been installed in a development shell.
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    checkpoint_path = Path(settings.sqlite_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        checkpoint_path,
        timeout=settings.busy_timeout_ms / 1_000,
        check_same_thread=False,
    )

    try:
        connection.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")

        serializer = JsonPlusSerializer(
            allowed_msgpack_modules=CHECKPOINT_ALLOWED_MSGPACK_MODULES,
        )
        saver = SqliteSaver(connection, serde=serializer)
        saver.setup()

        logger.info(
            "Opened SQLite checkpoint saver | sqlite_path=%s busy_timeout_ms=%d",
            checkpoint_path,
            settings.busy_timeout_ms,
        )

        yield saver

    finally:
        connection.close()
        logger.info("Closed SQLite checkpoint saver | sqlite_path=%s", checkpoint_path)


# --- Defining Checkpoint Runtime Helpers
def checkpoint_exists(checkpointer: Any, config: dict[str, Any]) -> bool:
    """
    Check whether a checkpoint thread already has persisted state.

    Args:
        checkpointer: LangGraph checkpoint saver.
        config: Runnable config containing a validated thread id.

    Returns:
        True when the saver returns a checkpoint tuple.
    """
    return checkpointer.get_tuple(config) is not None


def resume_checkpointed_graph(
    graph: Any,
    checkpointer: Any,
    config: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """
    Resume an incomplete graph or return an already-complete snapshot without rerunning nodes.

    Args:
        graph: Compiled LangGraph application using the supplied checkpointer.
        checkpointer: Saver used to prove the checkpoint thread exists.
        config: Runnable config containing the checkpoint thread id.

    Returns:
        Tuple of graph values and whether any pending node was executed.

    Raises:
        ValueError: If the thread does not exist or contains no state values.
    """
    if not checkpoint_exists(checkpointer=checkpointer, config=config):
        raise ValueError("Checkpoint thread does not exist; start a new run before requesting resume.")

    snapshot = graph.get_state(config)

    if not snapshot.values:
        raise ValueError("Checkpoint thread exists but contains no graph state.")

    if not snapshot.next:
        logger.info(
            "Checkpoint thread already complete; skipping node execution | thread_id=%s",
            config["configurable"]["thread_id"],
        )

        return dict(snapshot.values), False

    logger.info(
        "Resuming checkpoint thread | thread_id=%s next_nodes=%s",
        config["configurable"]["thread_id"],
        tuple(snapshot.next),
    )

    result = graph.invoke(None, config=config)

    return dict(result), True


def resolve_checkpoint_writer_node(graph: Any, snapshot: Any) -> str:
    """
    Resolve the single graph node that produced a historical checkpoint.

    Args:
        graph: Compiled LangGraph application containing static topology metadata.
        snapshot: LangGraph StateSnapshot selected from checkpoint history.

    Returns:
        Graph node name safe to pass to update_state as `as_node`.

    Raises:
        ValueError: If metadata and graph topology cannot identify one writer node.
    """
    metadata = snapshot.metadata or {}
    writes   = metadata.get("writes")

    if isinstance(writes, dict):
        writer_nodes = [
            str(node_name)
            for node_name in writes
            if str(node_name) not in {"", "__start__"}
        ]

        if len(writer_nodes) == 1:
            return writer_nodes[0]

        if writer_nodes:
            raise ValueError("Historical replay metadata identifies multiple writer nodes.")

    pending_nodes = {str(node_name) for node_name in snapshot.next if str(node_name)}

    if not pending_nodes:
        raise ValueError("Historical replay cannot resolve a writer for a complete checkpoint.")

    graph_view = graph.get_graph()
    outgoing_targets: dict[str, set[str]] = {}

    for edge in graph_view.edges:
        source = str(edge.source)
        target = str(edge.target)

        if source in {"", "__start__", "__end__"}:
            continue

        outgoing_targets.setdefault(source, set()).add(target)

    topology_candidates = [
        source
        for source, targets in outgoing_targets.items()
        if pending_nodes.issubset(targets)
    ]

    if len(topology_candidates) != 1:
        raise ValueError(
            "Historical replay requires one unambiguous writer from checkpoint metadata or graph topology."
        )

    resolved_writer = topology_candidates[0]

    logger.info(
        "Resolved historical checkpoint writer from graph topology | writer_node=%s pending_nodes=%s",
        resolved_writer,
        tuple(sorted(pending_nodes)),
    )

    return resolved_writer


def replay_historical_checkpoint_branch(
    graph: Any,
    checkpointer: Any,
    source_thread_id: str,
    source_checkpoint_id: str,
    replay_request_id: str,
) -> tuple[dict[str, Any], HistoricalCheckpointReplay]:
    """
    Replay pending nodes from an exact checkpoint inside a deterministic child thread.

    The source thread is read-only. A repeated call with the same request resumes or
    reuses the child thread, allowing Airflow retries without mutating the original
    investigation history.

    Args:
        graph: Compiled LangGraph application using the supplied checkpointer.
        checkpointer: Saver containing both source and replay threads.
        source_thread_id: Existing source checkpoint thread.
        source_checkpoint_id: Exact historical checkpoint selected for replay.
        replay_request_id: Stable replay request used to derive the child thread.

    Returns:
        Tuple containing replay graph values and sanitized replay metadata.

    Raises:
        ValueError: If the source checkpoint is missing, complete, ambiguous, or invalid.
    """
    validated_source = validate_checkpoint_thread_id(source_thread_id)
    source_config     = build_checkpoint_config(
        thread_id=validated_source,
        checkpoint_id=source_checkpoint_id,
    )

    if not checkpoint_exists(checkpointer=checkpointer, config=source_config):
        raise ValueError("Historical checkpoint does not exist in the selected source thread.")

    source_snapshot = graph.get_state(source_config)

    if not source_snapshot.values:
        raise ValueError("Historical checkpoint contains no graph state values.")

    if not source_snapshot.next:
        raise ValueError("Historical checkpoint is already complete and has no pending nodes to replay.")

    source_writer_node = resolve_checkpoint_writer_node(graph=graph, snapshot=source_snapshot)
    replay_thread_id   = build_checkpoint_replay_thread_id(
        source_thread_id=validated_source,
        source_checkpoint_id=source_checkpoint_id,
        replay_request_id=replay_request_id,
    )
    replay_config = build_checkpoint_config(replay_thread_id)

    if checkpoint_exists(checkpointer=checkpointer, config=replay_config):
        replay_values, executed_pending_nodes = resume_checkpointed_graph(
            graph=graph,
            checkpointer=checkpointer,
            config=replay_config,
        )

    else:
        # Copy state into a separate thread as if the original writer node produced it.
        # This preserves source history while allowing LangGraph to schedule the same next nodes.
        graph.update_state(
            replay_config,
            dict(source_snapshot.values),
            as_node=source_writer_node,
        )
        replay_snapshot = graph.get_state(replay_config)

        if tuple(replay_snapshot.next) != tuple(source_snapshot.next):
            raise ValueError("Historical replay branch does not preserve the selected pending-node contract.")

        replay_values         = dict(graph.invoke(None, config=replay_config))
        executed_pending_nodes = True

    replay = HistoricalCheckpointReplay(
        source_thread_id=validated_source,
        source_checkpoint_id=source_config["configurable"]["checkpoint_id"],
        replay_thread_id=replay_thread_id,
        source_writer_node=source_writer_node,
        source_next_nodes=tuple(source_snapshot.next),
        executed_pending_nodes=executed_pending_nodes,
    )

    logger.info(
        "Historical checkpoint branch resolved | source_thread_id=%s source_checkpoint_id=%s "
        "replay_thread_id=%s source_writer_node=%s next_nodes=%s executed=%s",
        replay.source_thread_id,
        replay.source_checkpoint_id,
        replay.replay_thread_id,
        replay.source_writer_node,
        replay.source_next_nodes,
        replay.executed_pending_nodes,
    )

    return replay_values, replay


def summarize_checkpoint_history(
    graph: Any,
    config: dict[str, Any],
    limit: int = 25,
) -> list[CheckpointSnapshotSummary]:
    """
    Return bounded, sanitized history metadata without exposing checkpoint state payloads.

    Args:
        graph: Compiled LangGraph application using a checkpointer.
        config: Runnable config containing a validated thread id.
        limit: Maximum history rows to return.

    Returns:
        Newest-first checkpoint metadata summaries.

    Raises:
        ValueError: If limit falls outside the bounded range.
    """
    if limit < 1 or limit > MAX_CHECKPOINT_HISTORY:
        raise ValueError(f"Checkpoint history limit must be between 1 and {MAX_CHECKPOINT_HISTORY}.")

    summaries = []

    for snapshot in islice(graph.get_state_history(config), limit):
        configurable = snapshot.config.get("configurable", {})
        metadata     = snapshot.metadata or {}
        summaries.append(
            CheckpointSnapshotSummary(
                checkpoint_id=str(configurable.get("checkpoint_id", "")),
                created_at=str(snapshot.created_at or ""),
                step=int(metadata.get("step", -1)),
                source=str(metadata.get("source", "unknown")),
                next_nodes=tuple(snapshot.next),
                is_complete=not bool(snapshot.next),
            )
        )

    logger.info(
        "Summarized checkpoint history | thread_id=%s checkpoints=%d",
        config["configurable"]["thread_id"],
        len(summaries),
    )

    return summaries


def select_checkpoint_for_replay(
    history: list[CheckpointSnapshotSummary],
    next_node: str,
) -> tuple[CheckpointSnapshotSummary, int]:
    """
    Select the newest checkpoint waiting for one exact graph node.

    Args:
        history: Newest-first sanitized checkpoint summaries.
        next_node: Exact pending node requested by the operator.

    Returns:
        Tuple containing the newest matching checkpoint and total match count.

    Raises:
        ValueError: If the node name is unsafe or no checkpoint waits for that node.
    """
    normalized_node = next_node.strip()

    if not SAFE_NODE_NAME.fullmatch(normalized_node):
        raise ValueError(
            "Checkpoint next node must start with a letter and contain only letters, numbers, or underscore."
        )

    matches = [
        summary
        for summary in history
        if normalized_node in summary.next_nodes
    ]

    if not matches:
        raise ValueError(f"No checkpoint waits for the requested next node: {normalized_node}.")

    selected = matches[0]

    logger.info(
        "Selected checkpoint for replay | checkpoint_id=%s next_node=%s matching_checkpoints=%d",
        selected.checkpoint_id,
        normalized_node,
        len(matches),
    )

    return selected, len(matches)
