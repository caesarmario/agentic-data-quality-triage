####
## Agent Checkpoint Smoke Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Cross-process smoke phases for the optional LangGraph SQLite checkpoint backend."""

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TypedDict


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.checkpointing import (
    CHECKPOINT_MODE_SQLITE,
    build_checkpoint_config,
    checkpoint_exists,
    load_checkpoint_settings,
    open_checkpoint_saver,
    resume_checkpointed_graph,
    summarize_checkpoint_history,
    validate_checkpoint_thread_id,
)
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_SMOKE_DB_PATH     = "/var/lib/agent-checkpoints/smoke.sqlite3"
DEFAULT_MARKER_DIRECTORY = "/var/lib/agent-checkpoints/smoke-markers"
SMOKE_PHASES             = ("initialize", "resume", "resume-complete", "verify")


# --- Defining State Contract
class CheckpointSmokeState(TypedDict):
    """
    Minimal state used to prove checkpoint recovery across Airflow tasks.

    Attributes:
        thread_id: Stable smoke thread id.
        prepare_count: Number of times the pre-interrupt node executed.
        effect_count: Number of times the guarded effect node executed.
    """

    thread_id: str
    prepare_count: int
    effect_count: int


# --- Defining Marker Helpers
def marker_path_for_thread(thread_id: str, marker_directory: str) -> Path:
    """
    Build a path-safe marker file for one checkpoint thread.

    Args:
        thread_id: Validated checkpoint thread identifier.
        marker_directory: Absolute directory used for smoke side-effect markers.

    Returns:
        Marker JSON path derived from a SHA-256 digest.

    Raises:
        ValueError: If the marker directory is not absolute.
    """
    validated_thread = validate_checkpoint_thread_id(thread_id)
    directory        = Path(marker_directory).expanduser()

    if not directory.is_absolute():
        raise ValueError("Checkpoint smoke marker directory must be absolute.")

    digest = hashlib.sha256(validated_thread.encode("utf-8")).hexdigest()[:24]

    return directory / f"{digest}.json"


def read_marker_count(marker_path: Path) -> int:
    """
    Read the external side-effect counter for one smoke thread.

    Args:
        marker_path: Marker JSON file path.

    Returns:
        Persisted effect count, or zero when the marker does not exist.

    Raises:
        ValueError: If the marker payload does not contain a non-negative integer count.
    """
    if not marker_path.exists():
        return 0

    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    count   = payload.get("effect_count")

    if not isinstance(count, int) or count < 0:
        raise ValueError("Checkpoint smoke marker contains an invalid effect_count.")

    return count


def increment_marker(marker_path: Path, thread_id: str) -> int:
    """
    Atomically increment the smoke side-effect marker.

    Args:
        marker_path: Marker JSON file path.
        thread_id: Validated checkpoint thread identifier.

    Returns:
        Updated external effect count.
    """
    current_count = read_marker_count(marker_path)
    next_count    = current_count + 1

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = marker_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {
                "thread_id": validate_checkpoint_thread_id(thread_id),
                "effect_count": next_count,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(marker_path)

    logger.info("Checkpoint smoke marker incremented | thread_id=%s effect_count=%d", thread_id, next_count)

    return next_count


# --- Defining Smoke Graph
def build_smoke_graph(
    checkpointer: Any,
    marker_path: Path,
    interrupt_before_effect: bool = False,
) -> Any:
    """
    Build a deterministic two-node graph for checkpoint recovery validation.

    Args:
        checkpointer: Persistent LangGraph saver.
        marker_path: External marker written only by the effect node.
        interrupt_before_effect: Pause after prepare and before the effect node.

    Returns:
        Compiled LangGraph application.
    """
    from langgraph.graph import END, StateGraph

    def prepare_node(state: CheckpointSmokeState) -> CheckpointSmokeState:
        """
        Increment the deterministic preparation counter.

        Args:
            state: Current checkpoint smoke state.

        Returns:
            Updated state proving the node execution count.
        """
        logger.info("Checkpoint smoke prepare node | thread_id=%s", state["thread_id"])

        return {
            **state,
            "prepare_count": state["prepare_count"] + 1,
        }

    def effect_node(state: CheckpointSmokeState) -> CheckpointSmokeState:
        """
        Write one external marker and increment the state effect counter.

        Args:
            state: Current checkpoint smoke state.

        Returns:
            Updated state with the effect count.

        Raises:
            ValueError: If the external marker count diverges from graph state.
        """
        external_count = increment_marker(marker_path=marker_path, thread_id=state["thread_id"])
        state_count    = state["effect_count"] + 1

        if external_count != state_count:
            raise ValueError("Checkpoint smoke detected repeated or divergent external side effects.")

        return {
            **state,
            "effect_count": state_count,
        }

    workflow = StateGraph(CheckpointSmokeState)
    workflow.add_node("prepare", prepare_node)
    workflow.add_node("effect", effect_node)
    workflow.set_entry_point("prepare")
    workflow.add_edge("prepare", "effect")
    workflow.add_edge("effect", END)

    interrupt_before = ["effect"] if interrupt_before_effect else None

    return workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )


# --- Defining Phase Execution
def run_smoke_phase(
    phase: str,
    thread_id: str,
    sqlite_path: str = DEFAULT_SMOKE_DB_PATH,
    marker_directory: str = DEFAULT_MARKER_DIRECTORY,
) -> dict[str, Any]:
    """
    Execute one cross-process checkpoint smoke phase.

    Args:
        phase: One allowlisted phase name.
        thread_id: Stable checkpoint thread shared by all Airflow tasks.
        sqlite_path: Absolute SQLite checkpoint database path.
        marker_directory: Absolute directory for the side-effect marker.

    Returns:
        JSON-serializable phase result.

    Raises:
        ValueError: If the phase contract or persisted state is invalid.
    """
    normalized_phase = phase.strip().lower()

    if normalized_phase not in SMOKE_PHASES:
        raise ValueError(f"Unknown checkpoint smoke phase: {phase}.")

    validated_thread = validate_checkpoint_thread_id(thread_id)
    marker_path      = marker_path_for_thread(validated_thread, marker_directory)
    settings         = load_checkpoint_settings(
        mode=CHECKPOINT_MODE_SQLITE,
        sqlite_path=sqlite_path,
    )
    config = build_checkpoint_config(validated_thread)

    logger.info(
        "Starting checkpoint smoke phase | phase=%s thread_id=%s sqlite_path=%s",
        normalized_phase,
        validated_thread,
        settings.sqlite_path,
    )

    with open_checkpoint_saver(settings) as checkpointer:
        if checkpointer is None:
            raise ValueError("Checkpoint smoke requires the SQLite saver.")

        graph = build_smoke_graph(
            checkpointer=checkpointer,
            marker_path=marker_path,
            interrupt_before_effect=normalized_phase == "initialize",
        )

        if normalized_phase == "initialize":
            if checkpoint_exists(checkpointer=checkpointer, config=config):
                raise ValueError("Checkpoint smoke thread already exists; use a unique thread id.")

            graph.invoke(
                {
                    "thread_id": validated_thread,
                    "prepare_count": 0,
                    "effect_count": 0,
                },
                config=config,
            )
            snapshot = graph.get_state(config)

            if tuple(snapshot.next) != ("effect",):
                raise ValueError("Initialize phase did not stop before the effect node.")

            if snapshot.values.get("prepare_count") != 1 or read_marker_count(marker_path) != 0:
                raise ValueError("Initialize phase violated the pre-effect checkpoint contract.")

            executed_pending_nodes = False

        elif normalized_phase in {"resume", "resume-complete"}:
            _, executed_pending_nodes = resume_checkpointed_graph(
                graph=graph,
                checkpointer=checkpointer,
                config=config,
            )
            snapshot = graph.get_state(config)
            expected_execution = normalized_phase == "resume"

            if executed_pending_nodes is not expected_execution:
                raise ValueError("Checkpoint resume execution did not match the selected phase.")

        else:
            if not checkpoint_exists(checkpointer=checkpointer, config=config):
                raise ValueError("Checkpoint smoke thread does not exist during verification.")

            snapshot               = graph.get_state(config)
            executed_pending_nodes = False

        history = summarize_checkpoint_history(graph=graph, config=config, limit=25)
        state   = dict(snapshot.values)
        marker_count = read_marker_count(marker_path)

    if normalized_phase in {"resume", "resume-complete", "verify"}:
        if snapshot.next:
            raise ValueError("Checkpoint smoke thread is not complete after resume.")

        if state.get("prepare_count") != 1 or state.get("effect_count") != 1 or marker_count != 1:
            raise ValueError("Checkpoint smoke did not preserve exactly-once node counts across processes.")

    result = {
        "status": "success",
        "phase": normalized_phase,
        "thread_id": validated_thread,
        "checkpoint_count": len(history),
        "latest_checkpoint_id": history[0].checkpoint_id if history else "",
        "next_nodes": list(snapshot.next),
        "prepare_count": int(state.get("prepare_count", 0)),
        "effect_count": int(state.get("effect_count", 0)),
        "external_effect_count": marker_count,
        "executed_pending_nodes": executed_pending_nodes,
    }

    logger.info("Checkpoint smoke phase completed | result=%s", result)

    return result


# --- Defining CLI Helpers
def build_parser() -> argparse.ArgumentParser:
    """
    Build the checkpoint smoke command-line parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run one Airflow checkpoint smoke phase.")

    parser.add_argument("--phase", required=True, choices=SMOKE_PHASES)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--sqlite-path", default=DEFAULT_SMOKE_DB_PATH)
    parser.add_argument("--marker-directory", default=DEFAULT_MARKER_DIRECTORY)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse arguments, execute one phase, and print its structured result.

    Args:
        argv: Optional argument sequence used by validation tests.

    Returns:
        Zero when the phase succeeds.
    """
    args   = build_parser().parse_args(argv)
    result = run_smoke_phase(
        phase=args.phase,
        thread_id=args.thread_id,
        sqlite_path=args.sqlite_path,
        marker_directory=args.marker_directory,
    )

    print(json.dumps(result, indent=2, sort_keys=True))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
