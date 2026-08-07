####
## Airflow Validation Suite Runner Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dags.dq_platform import validation as airflow_validation
from scripts import (
    read_airflow_validation_logs,
    run_validation_suite,
    trigger_airflow_llm_smoke,
    trigger_airflow_metadata_sync,
    trigger_airflow_validation,
)


# --- Defining Tests
def test_validation_suite_registry_contains_expected_named_suites() -> None:
    """
    Ensure the validation registry exposes the bounded public suite contract.

    Returns:
        None.
    """
    expected = {
        "all",
        "airflow",
        "agent",
        "api",
        "checkpoint",
        "discord",
        "dq",
        "llm",
        "life",
        "mcp",
        "metadata",
        "pipelines",
        "ui",
    }

    assert set(run_validation_suite.list_validation_suites()) == expected


def test_airflow_first_acceptance_policy_is_documented() -> None:
    """
    Ensure development agents cannot silently downgrade Airflow acceptance to local pytest.

    Returns:
        None.
    """
    agent_instructions = Path("AGENTS.md").read_text(encoding="utf-8")
    todo                = Path("todo/list.todo").read_text(encoding="utf-8")

    assert "Whenever the project owner asks to test" in agent_instructions
    assert "trigger the appropriate Airflow validation or operational DAG" in agent_instructions
    assert "non-authoritative inner-loop feedback only" in agent_instructions
    assert "Every project-owner testing request must produce an Airflow DagRun" in todo
    assert "DAG ID, run ID, final DagRun state" in todo


def test_build_pytest_command_uses_allowlisted_paths_without_shell() -> None:
    """
    Ensure suite commands use an argument list and repository-owned test paths.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("discord")

    assert command[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert command[4:] == [
        "tests/test_control_plane_client.py",
        "tests/test_copilot_narratives.py",
        "tests/test_discord_bot.py",
        "tests/test_discord_formatters.py",
        "tests/test_discord_webhook.py",
    ]
    assert all(";" not in argument for argument in command)


def test_api_validation_suite_covers_server_and_shared_client_contracts() -> None:
    """
    Ensure API acceptance includes both endpoint and adapter-side contract tests.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("api")

    assert "tests/test_api_app.py" in command
    assert "tests/test_control_plane_client.py" in command
    assert "tests/test_smoke_readiness.py" in command


def test_all_suite_targets_the_repository_test_directory() -> None:
    """
    Ensure the all suite discovers every test under the configured test directory.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("all")

    assert command[-1] == "tests"


def test_llm_suite_uses_provider_router_and_smoke_contract_tests() -> None:
    """
    Ensure the named LLM suite covers routing and the Airflow smoke boundary.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("llm")

    assert command[4:] == [
        "tests/test_airflow_dag_design.py",
        "tests/test_llm_routing.py",
        "tests/test_llm_provider_smoke.py",
        "tests/test_validation_suite.py",
    ]


def test_checkpoint_suite_uses_persistence_and_airflow_contract_tests() -> None:
    """
    Ensure the checkpoint suite covers persistence and Airflow acceptance contracts.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("checkpoint")

    assert command[4:] == [
        "tests/test_agent_checkpointing.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_validation_suite.py",
    ]


def test_life_suite_uses_evaluator_and_airflow_contract_tests() -> None:
    """
    Ensure the LIFE suite covers evaluation, history, and Airflow execution boundaries.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("life")

    assert command[4:] == [
        "tests/test_life_evaluation.py",
        "tests/test_life_history.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_validation_suite.py",
    ]


def test_metadata_suite_covers_registry_and_public_consumers() -> None:
    """
    Ensure metadata acceptance covers storage, tools, API, MCP, DAG policy, and readiness.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("metadata")

    assert command[4:] == [
        "tests/test_metadata_catalog.py",
        "tests/test_metadata_registry.py",
        "tests/test_api_app.py",
        "tests/test_control_plane_client.py",
        "tests/test_mcp_server.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_smoke_readiness.py",
        "tests/test_validation_suite.py",
    ]


def test_unknown_or_injected_suite_is_rejected() -> None:
    """
    Ensure arbitrary paths and shell fragments cannot reach subprocess execution.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unknown validation suite"):
        run_validation_suite.build_pytest_command("tests/test_api_app.py; rm -rf /tmp/example")


def test_run_validation_suite_returns_pytest_exit_code(monkeypatch) -> None:
    """
    Ensure Airflow receives the underlying pytest failure code.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_run(command, cwd, check):
        """
        Capture subprocess inputs without executing pytest.

        Args:
            command: Subprocess argument list.
            cwd: Subprocess working directory.
            check: Whether subprocess should raise automatically.

        Returns:
            CompletedProcess with a controlled failure code.
        """
        captured.update({"command": command, "cwd": cwd, "check": check})

        return subprocess.CompletedProcess(command, returncode=3)

    monkeypatch.setattr(run_validation_suite.subprocess, "run", fake_run)

    return_code = run_validation_suite.run_validation_suite("api")

    assert return_code == 3
    assert captured["check"] is False
    assert isinstance(captured["command"], list)

def test_trigger_command_contains_valid_json_without_shell_interpolation() -> None:
    """
    Ensure the Windows-safe trigger helper passes JSON as one subprocess argument.

    Returns:
        None.
    """
    command = trigger_airflow_validation.build_trigger_command(
        suite="all",
        run_id="manual__validation_test",
    )
    conf_index = command.index("-c") + 1

    assert json.loads(command[conf_index]) == {"validation_suite": "all"}
    assert command[0:3] == ["airflow", "dags", "trigger"]
    assert ";" not in "".join(command)


def test_trigger_command_can_require_optional_api_readiness() -> None:
    """
    Ensure API-dependent slices can request stronger readiness evidence through Airflow.

    Returns:
        None.
    """
    command = trigger_airflow_validation.build_trigger_command(
        suite="all",
        run_id="manual__validation_api_required",
        require_api=True,
    )
    conf_index = command.index("-c") + 1

    assert json.loads(command[conf_index]) == {
        "validation_suite": "all",
        "require_api": True,
    }


def test_validation_run_id_is_unique_and_shell_safe() -> None:
    """
    Ensure generated run ids remain readable and path-safe.

    Returns:
        None.
    """
    now    = datetime(2026, 6, 20, 1, 2, 3, 456789, tzinfo=timezone.utc)
    run_id = trigger_airflow_validation.build_validation_run_id("discord", now=now)

    assert run_id == "manual__validation_discord_20260620T010203456789"
    assert read_airflow_validation_logs.SAFE_RUN_ID.fullmatch(run_id)


def test_trigger_validation_runs_unpause_before_trigger(monkeypatch) -> None:
    """
    Ensure the helper unpauses the DAG before creating the requested run.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_validation, "run_command", commands.append)

    run_id = trigger_airflow_validation.trigger_validation(
        suite="api",
        run_id="manual__validation_api_test",
    )

    assert run_id == "manual__validation_api_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_validation.VALIDATION_DAG_ID,
    ]
    assert commands[1][0:3] == ["airflow", "dags", "trigger"]


def test_metadata_sync_trigger_uses_allowlisted_json_and_unpauses_first(monkeypatch) -> None:
    """
    Ensure metadata trigger configuration cannot become an arbitrary shell command.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_metadata_sync, "run_command", commands.append)

    run_id = trigger_airflow_metadata_sync.trigger_metadata_sync(
        registry_name="orders",
        run_id="manual__metadata_sync_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__metadata_sync_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_metadata_sync.METADATA_SYNC_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {"registry_name": "orders"}
    assert ";" not in "".join(trigger_command)


def test_metadata_sync_trigger_rejects_unknown_or_injected_registry() -> None:
    """
    Ensure an untrusted registry name cannot reach Airflow configuration.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unknown metadata registry"):
        trigger_airflow_metadata_sync.build_trigger_command(
            registry_name="orders'; rm -rf /tmp/example",
            run_id="manual__metadata_sync_injected",
        )


def test_llm_smoke_trigger_uses_safe_json_and_unpauses_first(monkeypatch) -> None:
    """
    Ensure the LLM helper validates routes and triggers DAG 92 without shell parsing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_llm_smoke, "run_command", commands.append)

    run_id = trigger_airflow_llm_smoke.trigger_llm_smoke(
        route_name="cheap_summary",
        require_provider=True,
        run_id="manual__llm_smoke_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__llm_smoke_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_llm_smoke.LLM_SMOKE_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {
        "route_name": "cheap_summary",
        "require_provider": True,
    }
    assert ";" not in "".join(trigger_command)


def test_llm_smoke_trigger_rejects_unknown_or_injected_route() -> None:
    """
    Ensure arbitrary route values cannot reach Airflow or a shell command.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unknown LLM smoke route"):
        trigger_airflow_llm_smoke.build_trigger_command(
            route_name="cheap_summary'; rm -rf /tmp/example",
            run_id="manual__llm_smoke_injected",
        )


def test_log_reader_rejects_path_traversal() -> None:
    """
    Ensure a validation run id cannot escape the Airflow log root.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="unsupported characters"):
        read_airflow_validation_logs.validation_log_directory("../../etc/passwd")


def test_log_reader_supports_allowlisted_llm_smoke_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure the shared administrative log reader can inspect retained DAG 92 logs.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id   = read_airflow_validation_logs.LLM_SMOKE_DAG_ID
    run_id   = "manual__llm_smoke_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_smoke_selected_route" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("executed_provider=heuristic\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_smoke_selected_route" in output
    assert "executed_provider=heuristic" in output


def test_log_reader_supports_allowlisted_checkpoint_smoke_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure the shared administrative log reader can inspect retained DAG 93 logs.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.CHECKPOINT_SMOKE_DAG_ID
    run_id    = "manual__checkpoint_smoke_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_resume_checkpoint" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("checkpoint resume log\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_resume_checkpoint" in output
    assert "checkpoint resume log" in output


def test_log_reader_prints_retained_task_logs(tmp_path: Path, capsys) -> None:
    """
    Ensure retained task logs are discoverable by run id.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    run_id    = "manual__validation_log_test"
    directory = read_airflow_validation_logs.validation_log_directory(run_id, log_root=tmp_path)
    log_path  = directory / "task_id=t10_run_named_pytest_suite" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("10 passed in 0.50s\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_validation_logs(run_id, log_root=tmp_path)
    output      = capsys.readouterr().out

    assert return_code == 0
    assert "task_id=t10_run_named_pytest_suite" in output
    assert "10 passed" in output

def test_validation_summary_uses_airflow3_compatible_context() -> None:
    """
    Ensure the summary does not depend on Airflow ORM-only DagRun methods.

    Returns:
        None.
    """
    class FakeDagRun:
        """Minimal Airflow 3 SDK DagRun context used by the summary task."""

        dag_id = "91_dag_dq_platform_validation"
        run_id = "manual__validation_summary_test"
        conf   = {"validation_suite": "all"}

    summary = airflow_validation.emit_validation_summary(dag_run=FakeDagRun())

    assert summary["result"] == "success"
    assert summary["validation_suite"] == "all"
    assert summary["task_states"] == {
        "t10_run_named_pytest_suite": "success",
        "t20_run_platform_readiness": "success",
    }
    assert "all_success" in summary["state_evidence"]

