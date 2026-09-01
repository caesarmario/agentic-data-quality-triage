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
    CheckpointSnapshotSummary,
    build_checkpoint_config,
    build_checkpoint_replay_thread_id,
    build_checkpoint_thread_id,
    load_checkpoint_settings,
    open_checkpoint_saver,
    select_checkpoint_for_replay,
    validate_checkpoint_thread_id,
)
from agent.checkpoint_operations import build_checkpoint_replay_preview
from agent.graph import (
    GraphState,
    TriageRuntimeConfig,
    build_runtime_contract_hash,
    build_triage_graph,
    run_triage,
    validate_historical_replay_state,
)
from agent.state import TriageState
from dags.dq_platform.checkpoint_smoke import emit_checkpoint_smoke_summary
from scripts.smoke_agent_checkpoint import run_smoke_phase
from scripts.inspect_agent_checkpoints import (
    inspect_checkpoint_history,
    resolve_checkpoint_correlation,
)
from scripts import trigger_airflow_triage
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


def test_historical_replay_thread_is_stable_and_hides_source_identifiers() -> None:
    """
    Ensure replay branches are deterministic without exposing source identifiers.

    Returns:
        None.
    """
    source_thread_id     = "triage-source-thread"
    source_checkpoint_id = "1f0c9c44-31c2-6c5a-8004-3f7d91b8d11a"
    replay_request_id    = "airflow-replay-request-001"
    replay_thread_id     = build_checkpoint_replay_thread_id(
        source_thread_id=source_thread_id,
        source_checkpoint_id=source_checkpoint_id,
        replay_request_id=replay_request_id,
    )

    assert replay_thread_id == build_checkpoint_replay_thread_id(
        source_thread_id=source_thread_id,
        source_checkpoint_id=source_checkpoint_id,
        replay_request_id=replay_request_id,
    )
    assert source_checkpoint_id not in replay_thread_id
    assert replay_request_id not in replay_thread_id
    assert len(replay_thread_id) <= 160


def test_runtime_contract_hash_is_stable_and_target_sensitive() -> None:
    """
    Ensure checkpoint replay rejects changes to evidence or side-effect targets.

    Returns:
        None.
    """
    base_config = TriageRuntimeConfig(
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        artifacts_prefix="agent-reports",
        clickhouse_host="clickhouse",
        clickhouse_port=8123,
    )
    changed_config = TriageRuntimeConfig(
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        artifacts_prefix="agent-reports-replay",
        clickhouse_host="clickhouse",
        clickhouse_port=8123,
    )

    assert build_runtime_contract_hash(base_config) == build_runtime_contract_hash(base_config)
    assert build_runtime_contract_hash(base_config) != build_runtime_contract_hash(changed_config)


def test_historical_replay_state_requires_matching_alert_and_runtime_contract() -> None:
    """
    Ensure historical state cannot be replayed for another alert or runtime target.

    Returns:
        None.
    """
    contract_hash = "a" * 64
    state         = TriageState(
        alert_key="orders|dq_failure|2026-07-16|dq.raw_orders|row_count_positive|table",
        runtime_contract_hash=contract_hash,
    )

    validate_historical_replay_state(
        state=state,
        requested_alert_id=None,
        requested_alert_key=state.alert_key,
        runtime_contract_hash=contract_hash,
    )

    with pytest.raises(ValueError, match="alert_key"):
        validate_historical_replay_state(
            state=state,
            requested_alert_id=None,
            requested_alert_key="another-alert",
            runtime_contract_hash=contract_hash,
        )

    with pytest.raises(ValueError, match="runtime contract"):
        validate_historical_replay_state(
            state=state,
            requested_alert_id=None,
            requested_alert_key=state.alert_key,
            runtime_contract_hash="b" * 64,
        )


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
    source_verified = run_smoke_phase(
        phase="verify",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )
    replayed = run_smoke_phase(
        phase="historical-replay",
        thread_id=thread_id,
        sqlite_path=str(sqlite_path),
        marker_directory=str(marker_directory),
    )
    replayed_again = run_smoke_phase(
        phase="historical-replay-repeat",
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
    assert source_verified["external_effect_count"] == 1
    assert replayed["executed_pending_nodes"] is True
    assert replayed_again["executed_pending_nodes"] is False
    assert replayed["source_checkpoint_id"] == replayed_again["source_checkpoint_id"]
    assert replayed["replay_thread_id"] == replayed_again["replay_thread_id"]
    assert replayed_again["external_effect_count"] == 1
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


def test_run_triage_rejects_incomplete_or_unsafe_historical_replay_contract(tmp_path: Path) -> None:
    """
    Ensure historical replay cannot bypass backend, pairing, or resume guards.

    Args:
        tmp_path: Pytest temporary directory fixture.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="must be provided together"):
        run_triage(
            alert_key="synthetic-alert-key",
            checkpoint_replay_id="checkpoint-001",
        )

    with pytest.raises(ValueError, match="enabled checkpoint backend"):
        run_triage(
            alert_key="synthetic-alert-key",
            checkpoint_thread_id="triage-source-thread",
            checkpoint_replay_id="checkpoint-001",
            checkpoint_replay_request_id="replay-request-001",
        )

    with pytest.raises(ValueError, match="not both"):
        run_triage(
            alert_key="synthetic-alert-key",
            checkpoint_mode=CHECKPOINT_MODE_SQLITE,
            checkpoint_sqlite_path=str(tmp_path / "triage.sqlite3"),
            checkpoint_thread_id="triage-source-thread",
            checkpoint_resume=True,
            checkpoint_replay_id="checkpoint-001",
            checkpoint_replay_request_id="replay-request-001",
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
    assert len(summary["task_states"]) == 9


def test_checkpoint_history_selection_returns_newest_matching_candidate() -> None:
    """
    Ensure replay selection uses sanitized newest-first checkpoint metadata only.

    Returns:
        None.
    """
    history = [
        CheckpointSnapshotSummary(
            checkpoint_id="checkpoint-newest",
            created_at="2026-08-28T00:00:03+00:00",
            step=3,
            source="loop",
            next_nodes=("store_report",),
            is_complete=False,
        ),
        CheckpointSnapshotSummary(
            checkpoint_id="checkpoint-middle",
            created_at="2026-08-28T00:00:02+00:00",
            step=2,
            source="loop",
            next_nodes=("rank_hypotheses",),
            is_complete=False,
        ),
        CheckpointSnapshotSummary(
            checkpoint_id="checkpoint-older",
            created_at="2026-08-28T00:00:01+00:00",
            step=1,
            source="loop",
            next_nodes=("store_report",),
            is_complete=False,
        ),
    ]

    selected, matching_count = select_checkpoint_for_replay(history=history, next_node="store_report")

    assert selected.checkpoint_id == "checkpoint-newest"
    assert matching_count == 2


def test_checkpoint_history_selection_rejects_unsafe_or_missing_node() -> None:
    """
    Ensure operators cannot select arbitrary node text or a nonexistent replay boundary.

    Returns:
        None.
    """
    history = [
        CheckpointSnapshotSummary(
            checkpoint_id="checkpoint-safe",
            created_at="2026-08-28T00:00:01+00:00",
            step=1,
            source="loop",
            next_nodes=("store_report",),
            is_complete=False,
        )
    ]

    with pytest.raises(ValueError, match="must start with a letter"):
        select_checkpoint_for_replay(history=history, next_node="store-report;rm")

    with pytest.raises(ValueError, match="No checkpoint waits"):
        select_checkpoint_for_replay(history=history, next_node="write_audit_log")


def test_checkpoint_inspector_is_noop_when_disabled_and_requires_exact_identity() -> None:
    """
    Ensure DAG triage runs can pass through the inspector without opening checkpoint storage.

    Returns:
        None.
    """
    result = inspect_checkpoint_history(
        enabled=False,
        alert_id=None,
        alert_key=None,
        checkpoint_mode="off",
        checkpoint_namespace=None,
    )

    assert result == {
        "status": "skipped",
        "reason": "checkpoint_inspection_disabled",
        "history": [],
        "selected_checkpoint": None,
    }

    with pytest.raises(ValueError, match="exactly one"):
        resolve_checkpoint_correlation(alert_id=None, alert_key=None)

    with pytest.raises(ValueError, match="exactly one"):
        resolve_checkpoint_correlation(alert_id="a", alert_key="b")


def test_airflow_triage_trigger_builds_bounded_source_inspect_and_replay_conf() -> None:
    """
    Ensure all operator actions preserve one namespace and use structured Airflow configuration.

    Returns:
        None.
    """
    common = {
        "alert_id": "",
        "alert_key": "orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        "checkpoint_namespace": "triage-contract-test",
        "history_limit": 25,
        "history_next_node": "store_report",
    }
    source = trigger_airflow_triage.validate_triage_inputs(
        action="triage",
        checkpoint_id="",
        replay_request_id="",
        **common,
    )
    inspect = trigger_airflow_triage.validate_triage_inputs(
        action="inspect",
        checkpoint_id="",
        replay_request_id="",
        **common,
    )
    replay = trigger_airflow_triage.validate_triage_inputs(
        action="replay",
        checkpoint_id="checkpoint-001",
        replay_request_id="replay-request-001",
        **common,
    )

    assert trigger_airflow_triage.build_trigger_conf(source)["checkpoint_action"] == "triage"
    assert trigger_airflow_triage.build_trigger_conf(inspect) == {
        "checkpoint_action": "inspect",
        "run_triage": False,
        "alert_id": "",
        "alert_key": common["alert_key"],
        "checkpoint_mode": "sqlite",
        "checkpoint_namespace": "triage-contract-test",
        "checkpoint_resume": False,
        "checkpoint_history_limit": 25,
        "checkpoint_history_next_node": "store_report",
    }
    assert trigger_airflow_triage.build_trigger_conf(replay)["checkpoint_replay_id"] == "checkpoint-001"

    command    = trigger_airflow_triage.build_trigger_command(replay, "manual__triage_replay_contract")
    conf_index = command.index("-c") + 1

    assert json.loads(command[conf_index])["checkpoint_replay_request_id"] == "replay-request-001"
    assert command[-1] == trigger_airflow_triage.TRIAGE_DAG_ID
    assert ";" not in "".join(command)


def test_airflow_triage_trigger_rejects_ambiguous_or_injected_operator_inputs() -> None:
    """
    Ensure unsafe identities, nodes, and incomplete replay requests fail before Airflow.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Exactly one"):
        trigger_airflow_triage.validate_alert_identity(alert_id="", alert_key="")

    with pytest.raises(ValueError, match="unsupported"):
        trigger_airflow_triage.validate_alert_identity(
            alert_id="",
            alert_key="orders|dq_failure'; rm -rf /tmp/example",
        )

    with pytest.raises(ValueError, match="Replay requires"):
        trigger_airflow_triage.validate_triage_inputs(
            action="replay",
            alert_id="",
            alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
            checkpoint_namespace="triage-contract-test",
            checkpoint_id="checkpoint-001",
            replay_request_id="",
            history_limit=50,
            history_next_node="store_report",
        )


def test_checkpoint_replay_preview_is_stable_and_non_executing() -> None:
    """
    Ensure UI/API previews reuse DAG 40 contracts without executing Airflow.

    Returns:
        None.
    """
    preview = build_checkpoint_replay_preview(
        alert_id="",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        checkpoint_namespace="manual__triage_triage_20260828T042324242187",
        checkpoint_id="checkpoint-001",
        replay_request_id="",
        selected_checkpoint_id="checkpoint-001",
        selected_next_nodes=["store_report"],
    )
    repeated = build_checkpoint_replay_preview(
        alert_id="",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        checkpoint_namespace="manual__triage_triage_20260828T042324242187",
        checkpoint_id="checkpoint-001",
        replay_request_id="",
        selected_checkpoint_id="checkpoint-001",
        selected_next_nodes=["store_report"],
    )

    assert preview["replay_request_id"] == repeated["replay_request_id"]
    assert preview["replay_thread_id"] == repeated["replay_thread_id"]
    assert preview["airflow_triggered"] is False
    assert preview["side_effects_executed"] is False
    assert preview["raw_state_exposed"] is False
    assert preview["operator_confirmation_required"] is True
    assert preview["dag_run_conf"]["checkpoint_replay_id"] == "checkpoint-001"


def test_checkpoint_replay_preview_rejects_stale_or_wrong_node_selection() -> None:
    """
    Ensure a preview cannot silently replay another or no-longer-pending checkpoint.

    Returns:
        None.
    """
    common = {
        "alert_id": "",
        "alert_key": "orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        "checkpoint_namespace": "manual__triage_triage_20260828T042324242187",
        "checkpoint_id": "checkpoint-001",
        "replay_request_id": "replay-contract-001",
        "history_next_node": "store_report",
    }

    with pytest.raises(ValueError, match="stale"):
        build_checkpoint_replay_preview(
            selected_checkpoint_id="checkpoint-002",
            selected_next_nodes=["store_report"],
            **common,
        )

    with pytest.raises(ValueError, match="no longer waits"):
        build_checkpoint_replay_preview(
            selected_checkpoint_id="checkpoint-001",
            selected_next_nodes=["finalize_report"],
            **common,
        )
