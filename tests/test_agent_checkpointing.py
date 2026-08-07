####
## Agent Checkpointing Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Contract tests for optional, persistent, and bounded LangGraph checkpoints."""

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, get_type_hints

import pytest

from agent.checkpointing import (
    CHECKPOINT_ALLOWED_MSGPACK_MODULES,
    CHECKPOINT_MODE_OFF,
    CHECKPOINT_MODE_SQLITE,
    build_checkpoint_config,
    build_checkpoint_thread_id,
    load_checkpoint_settings,
    open_checkpoint_saver,
    validate_checkpoint_thread_id,
)
from agent.graph import GraphState, TriageRuntimeConfig, build_triage_graph, run_triage
from agent.state import TriageState
from dags.dq_platform.checkpoint_smoke import emit_checkpoint_smoke_summary
from scripts.smoke_agent_checkpoint import run_smoke_phase
from scripts.trigger_airflow_checkpoint_smoke import (
    CHECKPOINT_SMOKE_DAG_ID,
    build_checkpoint_smoke_identifiers,
    build_trigger_command,
)


# --- Defining Test State
class PersistedTriageState(TypedDict):
    """
    Minimal graph wrapper used to validate strict TriageState deserialization.

    Attributes:
        state: Project Pydantic state stored inside the checkpoint.
    """

    state: TriageState


class FakeDagRun:
    """
    Provide minimal Airflow 3 context for checkpoint summary tests.

    Attributes:
        dag_id: Checkpoint smoke DAG identifier.
        run_id: Synthetic DagRun identifier.
        conf: Checkpoint thread correlation.
    """

    dag_id = CHECKPOINT_SMOKE_DAG_ID
    run_id = "manual__checkpoint_smoke_summary_test"
    conf   = {"thread_id": "checkpoint-smoke-summary-test"}


# --- Defining Tests
def test_checkpoint_settings_default_to_disabled(monkeypatch) -> None:
    """
    Ensure checkpointing does not change default local behavior.

    Args:
        monkeypatch: Pytest environment fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("AGENT_CHECKPOINT_MODE", raising=False)

    settings = load_checkpoint_settings()

    assert settings.mode == CHECKPOINT_MODE_OFF
    assert settings.enabled is False


@pytest.mark.parametrize("mode", ["memory", "postgres", "s3", "unknown"])
def test_checkpoint_mode_is_allowlisted(mode: str) -> None:
    """
    Ensure unsupported persistence backends fail closed.

    Args:
        mode: Unsupported backend name.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unsupported checkpoint mode"):
        load_checkpoint_settings(mode=mode)


def test_sqlite_checkpoint_path_must_be_absolute() -> None:
    """
    Ensure an Airflow parameter cannot redirect checkpoints through a relative path.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="must be absolute"):
        load_checkpoint_settings(mode=CHECKPOINT_MODE_SQLITE, sqlite_path="relative/checkpoints.sqlite3")


def test_checkpoint_thread_id_is_stable_safe_and_hides_alert_key() -> None:
    """
    Ensure thread correlation remains stable without exposing long internal keys.

    Returns:
        None.
    """
    alert_key = "orders|dq_failure|2026-07-16|dq.raw_orders|row_count_positive|table"
    thread_id = build_checkpoint_thread_id(
        namespace="40_dag_dq_orders_triage_agent:manual__2026-07-16T09:00:00+07:00",
        correlation_value=alert_key,
    )

    assert thread_id == build_checkpoint_thread_id(
        namespace="40_dag_dq_orders_triage_agent:manual__2026-07-16T09:00:00+07:00",
        correlation_value=alert_key,
    )
    assert "row_count_positive" not in thread_id
    assert len(thread_id) <= 160
    assert validate_checkpoint_thread_id(thread_id) == thread_id


def test_checkpoint_config_rejects_path_control_characters() -> None:
    """
    Ensure thread identifiers cannot escape the checkpoint namespace contract.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Checkpoint thread id"):
        build_checkpoint_config("../../unsafe")


def test_serializer_allowlist_is_explicit_and_project_bounded() -> None:
    """
    Ensure checkpoint deserialization never enables a wildcard module policy.

    Returns:
        None.
    """
    assert CHECKPOINT_ALLOWED_MSGPACK_MODULES
    assert all(module == "agent.state" for module, _ in CHECKPOINT_ALLOWED_MSGPACK_MODULES)
    assert ("agent.state", "TriageState") in CHECKPOINT_ALLOWED_MSGPACK_MODULES
    assert ("agent.state", "TriageReport") in CHECKPOINT_ALLOWED_MSGPACK_MODULES


def test_sqlite_saver_reconstructs_allowlisted_triage_state(tmp_path: Path) -> None:
    """
    Ensure strict checkpoint deserialization restores TriageState across connections.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        None.
    """
    from langgraph.graph import END, StateGraph

    sqlite_path = tmp_path / "typed-state.sqlite3"
    settings    = load_checkpoint_settings(
        mode=CHECKPOINT_MODE_SQLITE,
        sqlite_path=str(sqlite_path),
    )
    config = build_checkpoint_config("typed-state-test")

    def preserve_state(state: PersistedTriageState) -> PersistedTriageState:
        """
        Return the typed state unchanged for serialization validation.

        Args:
            state: Persisted graph state.

        Returns:
            Unchanged graph state.
        """
        return state

    with open_checkpoint_saver(settings) as saver:
        workflow = StateGraph(PersistedTriageState)
        workflow.add_node("preserve", preserve_state)
        workflow.set_entry_point("preserve")
        workflow.add_edge("preserve", END)
        graph = workflow.compile(checkpointer=saver)
        graph.invoke({"state": TriageState(alert_key="typed-state-alert")}, config=config)

    with open_checkpoint_saver(settings) as saver:
        workflow = StateGraph(PersistedTriageState)
        workflow.add_node("preserve", preserve_state)
        workflow.set_entry_point("preserve")
        workflow.add_edge("preserve", END)
        graph    = workflow.compile(checkpointer=saver)
        snapshot = graph.get_state(config)

    assert isinstance(snapshot.values["state"], TriageState)
    assert snapshot.values["state"].alert_key == "typed-state-alert"


def test_checkpoint_smoke_phases_persist_and_do_not_repeat_effect(tmp_path: Path) -> None:
    """
    Ensure initialize and resume phases survive closed SQLite connections.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        None.
    """
    sqlite_path      = tmp_path / "smoke.sqlite3"
    marker_directory = tmp_path / "markers"
    thread_id        = "checkpoint-smoke-contract-test"

    initialized = run_smoke_phase(
        phase="initialize",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )
    resumed = run_smoke_phase(
        phase="resume",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )
    completed_resume = run_smoke_phase(
        phase="resume-complete",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )
    verified = run_smoke_phase(
        phase="verify",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )

    assert initialized["next_nodes"] == ["effect"]
    assert initialized["external_effect_count"] == 0
    assert resumed["executed_pending_nodes"] is True
    assert completed_resume["executed_pending_nodes"] is False
    assert verified["prepare_count"] == 1
    assert verified["effect_count"] == 1
    assert verified["external_effect_count"] == 1
    assert verified["checkpoint_count"] >= 4


def test_checkpoint_smoke_rejects_reused_initialize_thread(tmp_path: Path) -> None:
    """
    Ensure accidental thread reuse does not overwrite prior investigations.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        None.
    """
    arguments = {
        "phase": "initialize",
        "thread_id": "checkpoint-smoke-reuse-test",
        "sqlite_path": str(tmp_path / "reuse.sqlite3"),
        "marker_directory": str(tmp_path / "markers"),
    }

    run_smoke_phase(**arguments)

    with pytest.raises(ValueError, match="already exists"):
        run_smoke_phase(**arguments)


def test_run_triage_requires_thread_id_when_sqlite_is_enabled(tmp_path: Path) -> None:
    """
    Ensure production triage cannot create an uncorrelated persistent thread.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="checkpoint_thread_id is required"):
        run_triage(
            alert_key="synthetic-alert-key",
            checkpoint_mode=CHECKPOINT_MODE_SQLITE,
            checkpoint_sqlite_path=str(tmp_path / "triage.sqlite3"),
        )


def test_triage_graph_accepts_optional_checkpointer_argument() -> None:
    """
    Ensure default graph compilation remains backward compatible without persistence.

    Returns:
        None.
    """
    graph = build_triage_graph(TriageRuntimeConfig(), checkpointer=None)

    assert graph is not None
    assert get_type_hints(GraphState)["state"] is TriageState


def test_checkpoint_trigger_identifiers_are_unique_and_safe() -> None:
    """
    Ensure Airflow trigger correlation is deterministic for a supplied timestamp.

    Returns:
        None.
    """
    now = datetime(2026, 7, 16, 2, 3, 4, 567890, tzinfo=timezone.utc)

    run_id, thread_id = build_checkpoint_smoke_identifiers(now=now)
    command           = build_trigger_command(run_id=run_id, thread_id=thread_id)
    conf_index        = command.index("-c") + 1

    assert run_id == "manual__checkpoint_smoke_20260716T020304567890"
    assert thread_id == "checkpoint-smoke-20260716T020304567890"
    assert json.loads(command[conf_index]) == {"thread_id": thread_id}
    assert command[-1] == CHECKPOINT_SMOKE_DAG_ID


def test_checkpoint_smoke_summary_uses_airflow_context() -> None:
    """
    Ensure summary output records the DagRun and all cross-process task states.

    Returns:
        None.
    """
    summary = emit_checkpoint_smoke_summary(dag_run=FakeDagRun())

    assert summary["result"] == "success"
    assert summary["thread_id"] == "checkpoint-smoke-summary-test"
    assert len(summary["task_states"]) == 4
