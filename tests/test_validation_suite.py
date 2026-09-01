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
    trigger_airflow_metadata_lineage_agent,
    trigger_airflow_control_plane_supervisor,
    trigger_airflow_control_plane_resilience,
    trigger_airflow_schema_drift,
    trigger_airflow_triage,
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
        "schema",
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
        "tests/test_daily_summary_tool.py",
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

    assert "tests/test_daily_summary_tool.py" in command
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
        "tests/test_audit_log.py",
        "tests/test_s3_artifacts.py",
        "tests/test_validation_suite.py",
    ]


def test_agent_suite_covers_context_memory_and_supervisor_contracts() -> None:
    """
    Ensure agent acceptance covers shared context, durable memory, and supervisor runtime.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("agent")

    assert "tests/test_agent_context.py" in command
    assert "tests/test_incident_history_tool.py" in command
    assert "tests/test_control_plane_supervisor.py" in command
    assert "tests/test_specialist_contracts.py" in command
    assert "tests/test_supervisor_routing_policy.py" in command
    assert "tests/test_supervisor_resilience.py" in command
    assert "tests/test_table_health_tool.py" in command
    assert "tests/test_smoke_readiness.py" not in command


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
        "tests/test_life_replay.py",
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
        "tests/test_metadata_lineage_agent.py",
        "tests/test_metadata_registry.py",
        "tests/test_api_app.py",
        "tests/test_control_plane_client.py",
        "tests/test_mcp_server.py",
        "tests/test_airflow_dag_design.py",
        "tests/test_smoke_readiness.py",
        "tests/test_validation_suite.py",
    ]


def test_schema_suite_covers_detector_airflow_readiness_and_runner_contracts() -> None:
    """
    Ensure schema acceptance spans deterministic logic and operational boundaries.

    Returns:
        None.
    """
    command = run_validation_suite.build_pytest_command("schema")

    assert command[4:] == [
        "tests/test_schema_drift.py",
        "tests/test_schema_drift_alerts.py",
        "tests/test_schema_drift_evidence.py",
        "tests/test_schema_drift_agent.py",
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

    def fake_run(command, cwd, check, env):
        """
        Capture subprocess inputs without executing pytest.

        Args:
            command: Subprocess argument list.
            cwd: Subprocess working directory.
            check: Whether subprocess should raise automatically.
            env: Child-process environment with external LLMs disabled.

        Returns:
            CompletedProcess with a controlled failure code.
        """
        captured.update({"command": command, "cwd": cwd, "check": check, "env": env})

        return subprocess.CompletedProcess(command, returncode=3)

    monkeypatch.setattr(run_validation_suite.subprocess, "run", fake_run)

    return_code = run_validation_suite.run_validation_suite("api")

    assert return_code == 3
    assert captured["check"] is False
    assert captured["env"]["EXTERNAL_LLM_ENABLED"] == "false"
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


def test_schema_drift_trigger_uses_allowlisted_json_and_unpauses_first(monkeypatch) -> None:
    """
    Ensure schema drift trigger configuration remains bounded and auditable.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_schema_drift, "run_command", commands.append)

    run_id = trigger_airflow_schema_drift.trigger_schema_drift(
        contract_name="orders",
        run_id="manual__schema_drift_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__schema_drift_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_schema_drift.SCHEMA_DRIFT_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {"contract_name": "orders"}
    assert ";" not in "".join(trigger_command)


def test_schema_drift_trigger_rejects_unknown_or_injected_contract() -> None:
    """
    Ensure untrusted contract aliases cannot reach Airflow configuration.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unknown schema contract"):
        trigger_airflow_schema_drift.build_trigger_command(
            contract_name="orders'; rm -rf /tmp/example",
            run_id="manual__schema_drift_injected",
        )


def test_metadata_lineage_trigger_uses_bounded_json_and_unpauses_first(monkeypatch) -> None:
    """
    Ensure specialist trigger parameters remain allowlisted and Airflow-owned.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_metadata_lineage_agent, "run_command", commands.append)

    run_id = trigger_airflow_metadata_lineage_agent.trigger_metadata_lineage_agent(
        task_type="asset_context",
        qualified_name="dq.raw_orders",
        query="orders",
        max_depth=5,
        max_nodes=100,
        run_id="manual__metadata_lineage_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__metadata_lineage_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_metadata_lineage_agent.METADATA_LINEAGE_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {
        "task_type": "asset_context",
        "qualified_name": "dq.raw_orders",
        "query": "orders",
        "max_depth": 5,
        "max_nodes": 100,
    }
    assert ";" not in "".join(trigger_command)


def test_metadata_lineage_trigger_rejects_injected_search_query() -> None:
    """Shell-sensitive metadata queries must not reach Airflow configuration."""
    with pytest.raises(ValueError, match="unsupported characters"):
        trigger_airflow_metadata_lineage_agent.build_trigger_command(
            task_type="trusted_asset_search",
            qualified_name="",
            query="orders'; rm -rf /tmp/example",
            max_depth=5,
            max_nodes=100,
            run_id="manual__metadata_lineage_injected",
        )


def test_control_plane_trigger_uses_safe_json_and_unpauses_first(monkeypatch) -> None:
    """
    Ensure the supervisor helper triggers DAG 98 through structured configuration.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(
        trigger_airflow_control_plane_supervisor,
        "run_command",
        commands.append,
    )

    run_id = trigger_airflow_control_plane_supervisor.trigger_control_plane_supervisor(
        intent="asset_context",
        question="",
        alert_key="",
        qualified_name="dq.raw_orders",
        query="orders",
        domain="commerce",
        data_layer="raw",
        certification_status="candidate",
        lifecycle_status="active",
        result_limit=25,
        max_depth=10,
        max_nodes=250,
        confidence_threshold=0.95,
        max_evidence_iterations=0,
        manifest_s3_uri="s3://dq-artifacts/dbt/manifest.json",
        artifacts_bucket="dq-artifacts",
        artifacts_prefix="control-plane/reports",
        token_budget=16_384,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_trigger_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__control_plane_trigger_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_control_plane_supervisor.CONTROL_PLANE_SUPERVISOR_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {
        "intent": "asset_context",
        "question": "",
        "alert_key": "",
        "qualified_name": "dq.raw_orders",
        "query": "orders",
        "domain": "commerce",
        "data_layer": "raw",
        "certification_status": "candidate",
        "lifecycle_status": "active",
        "max_handoffs": 1,
        "max_retries": 0,
        "max_model_calls": 3,
        "token_budget": 16_384,
        "estimated_cost_budget_usd": 0.05,
        "latency_budget_ms": 300_000,
        "sql_proposal_base64": "",
        "sql_purpose": "",
        "sql_hard_limit": 100,
        "sql_require_date_filter": True,
        "sql_max_scan_bytes": 1024 * 1024 * 1024,
        "expected_sql_decision": "",
        "schema_run_id": "",
        "schema_finding_limit": 50,
        "expected_schema_assessment": "",
        "result_limit": 25,
        "max_depth": 10,
        "max_nodes": 250,
        "confidence_threshold": 0.95,
        "max_evidence_iterations": 0,
        "manifest_s3_uri": "s3://dq-artifacts/dbt/manifest.json",
        "artifacts_bucket": "dq-artifacts",
        "artifacts_prefix": "control-plane/reports",
    }
    assert ";" not in "".join(trigger_command)
    assert trigger_airflow_control_plane_supervisor.PROJECT_ROOT == Path(
        trigger_airflow_control_plane_supervisor.__file__
    ).resolve().parents[1]


def test_control_plane_sql_review_encodes_proposal_before_airflow() -> None:
    """Raw SQL must be Base64-transported and never become a shell command fragment."""
    sql = (
        "SELECT country FROM dq.raw_orders "
        "WHERE dt = toDate('2026-08-08') LIMIT 10"
    )
    command = trigger_airflow_control_plane_supervisor.build_trigger_command(
        intent="review_sql",
        question="",
        alert_key="",
        qualified_name="",
        query="",
        token_budget=0,
        latency_budget_ms=300_000,
        run_id="manual__control_plane_sql_review_test",
        sql_proposal=sql,
        expected_sql_decision="approved",
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert sql not in json.dumps(conf)
    assert conf["sql_proposal_base64"]
    assert conf["expected_sql_decision"] == "approved"
    assert "SELECT country" not in "".join(command)


def test_control_plane_trigger_rejects_injected_question() -> None:
    """Shell-sensitive supervisor questions must not enter dag_run.conf."""
    with pytest.raises(ValueError, match="unsupported characters"):
        trigger_airflow_control_plane_supervisor.build_trigger_command(
            intent="auto",
            question="Show impact'; rm -rf /tmp/example",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="",
            token_budget=16_384,
            latency_budget_ms=300_000,
            run_id="manual__control_plane_injected",
        )


def test_control_plane_trigger_rejects_injected_run_id_before_airflow(monkeypatch) -> None:
    """
    Ensure an unsafe explicit DagRun ID never reaches an Airflow subprocess.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []
    monkeypatch.setattr(
        trigger_airflow_control_plane_supervisor,
        "run_command",
        commands.append,
    )

    with pytest.raises(ValueError, match="Airflow run ID"):
        trigger_airflow_control_plane_supervisor.trigger_control_plane_supervisor(
            intent="asset_context",
            question="",
            alert_key="",
            qualified_name="dq.raw_orders",
            query="orders",
            token_budget=16_384,
            latency_budget_ms=300_000,
            run_id="manual__safe;touch_tmp",
        )

    assert commands == []


def test_control_plane_resilience_trigger_unpauses_before_allowlisted_scenario(
    monkeypatch,
) -> None:
    """
    Ensure controlled failure scenarios stay structured and Airflow-owned.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(
        trigger_airflow_control_plane_resilience,
        "run_command",
        commands.append,
    )

    run_id = trigger_airflow_control_plane_resilience.trigger_control_plane_resilience(
        scenario="hard_timeout",
        run_id="manual__control_plane_resilience_test",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1

    assert run_id == "manual__control_plane_resilience_test"
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_control_plane_resilience.CONTROL_PLANE_RESILIENCE_DAG_ID,
    ]
    assert json.loads(trigger_command[conf_index]) == {"scenario": "hard_timeout"}
    assert ";" not in "".join(trigger_command)


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
        "run_external_provider": True,
    }
    assert ";" not in "".join(trigger_command)


def test_triage_trigger_unpauses_before_read_only_checkpoint_inspection(monkeypatch) -> None:
    """
    Ensure checkpoint inspection is triggered through DAG 40 with structured, non-mutating configuration.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands: list[list[str]] = []

    monkeypatch.setattr(trigger_airflow_triage, "run_command", commands.append)

    run_id, namespace = trigger_airflow_triage.trigger_airflow_triage(
        action="inspect",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        checkpoint_namespace="triage-inspect-contract",
        history_limit=25,
        history_next_node="store_report",
        run_id="manual__triage_inspect_contract",
    )
    trigger_command = commands[1]
    conf_index      = trigger_command.index("-c") + 1
    conf            = json.loads(trigger_command[conf_index])

    assert run_id == "manual__triage_inspect_contract"
    assert namespace == "triage-inspect-contract"
    assert commands[0] == ["airflow", "dags", "unpause", trigger_airflow_triage.TRIAGE_DAG_ID]
    assert conf["checkpoint_action"] == "inspect"
    assert conf["run_triage"] is False
    assert conf["checkpoint_mode"] == "sqlite"
    assert conf["checkpoint_history_limit"] == 25
    assert "checkpoint_replay_id" not in conf


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


def test_log_reader_supports_allowlisted_operational_triage_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure retained DAG 40 inspect and replay evidence is available to operator tooling.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.TRIAGE_DAG_ID
    run_id    = "manual__triage_inspect_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t05_inspect_checkpoint_history" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("CHECKPOINT_SELECTED_ID=checkpoint-001\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t05_inspect_checkpoint_history" in output
    assert "CHECKPOINT_SELECTED_ID=checkpoint-001" in output


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


def test_log_reader_supports_allowlisted_schema_drift_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure retained DAG 96 detection evidence is available to operator tooling.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.SCHEMA_DRIFT_DAG_ID
    run_id    = "manual__schema_drift_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t10_detect_schema_drift" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("schema evaluation completed status=pass\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t10_detect_schema_drift" in output
    assert "status=pass" in output


def test_log_reader_supports_allowlisted_metadata_lineage_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure retained DAG 97 specialist evidence is available to operator tooling.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.METADATA_LINEAGE_DAG_ID
    run_id    = "manual__metadata_lineage_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_verify_metadata_lineage_audit" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("specialist audit verified status=success\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_verify_metadata_lineage_audit" in output
    assert "status=success" in output


def test_log_reader_supports_allowlisted_control_plane_dag(tmp_path: Path, capsys) -> None:
    """
    Ensure retained DAG 98 supervisor evidence is available to operator tooling.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.CONTROL_PLANE_SUPERVISOR_DAG_ID
    run_id    = "manual__control_plane_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_verify_supervisor_audit" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("supervisor audit verified status=success\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_verify_supervisor_audit" in output
    assert "status=success" in output


def test_log_reader_supports_allowlisted_control_plane_resilience_dag(
    tmp_path: Path,
    capsys,
) -> None:
    """
    Ensure retained DAG 99 failure-containment evidence is operator-accessible.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.CONTROL_PLANE_RESILIENCE_DAG_ID
    run_id    = "manual__control_plane_resilience_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_verify_resilience_audit" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("resilience audit verified status=success\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_verify_resilience_audit" in output
    assert "status=success" in output


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

