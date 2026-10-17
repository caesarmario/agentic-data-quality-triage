####
## Airflow DAG Design Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import ast
import logging
import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining Constants
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAGS_ROOT    = PROJECT_ROOT / "dags"
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"
HELPERS_PATH  = DAGS_ROOT / "dq_platform" / "helpers.py"

SCHEDULED_DAG_FILE = "00_dag_dq_platform_daily_orchestrator.py"
TRIGGERED_DAG_FILES = [
    "10_dag_dq_orders_landing_orchestrator.py",
    "11_dag_dq_orders_seed_to_s3.py",
    "12_dag_dq_orders_load_raw_clickhouse.py",
    "20_dag_dq_orders_dbt_transform.py",
    "30_dag_dq_orders_quality_alerts.py",
    "40_dag_dq_orders_triage_agent.py",
    "90_dag_dq_platform_backfill_dispatcher.py",
    "90_02_dag_dq_platform_approved_actions.py",
    "90_03_dag_dq_incident_revalidation.py",
    "90_04_dag_dq_resolution_outbox_acceptance.py",
    "90_05_dag_dq_incident_recovery.py",
    "90_07_dag_dq_recovery_acceptance.py",
    "91_dag_dq_platform_validation.py",
    "92_dag_dq_llm_provider_smoke.py",
    "93_dag_dq_agent_checkpoint_smoke.py",
    "94_dag_dq_agent_life_evaluation.py",
    "95_dag_dq_metadata_registry_sync.py",
    "96_dag_dq_schema_drift_detection.py",
    "97_dag_dq_metadata_lineage_agent_smoke.py",
    "98_dag_dq_control_plane_supervisor_smoke.py",
    "99_dag_dq_control_plane_resilience_smoke.py",
]

SCHEDULED_ADMIN_DAG_FILES = ["90_06_dag_dq_recovery_reconciliation.py"]

ORCHESTRATOR_DAG_FILES = [
    "00_dag_dq_platform_daily_orchestrator.py",
    "10_dag_dq_orders_landing_orchestrator.py",
]


# --- Defining Helper Functions
def read_dag_file(file_name: str) -> str:
    """
    Read one DAG file as plain text for lightweight design-policy checks.

    Args:
        file_name: DAG file name relative to the dags directory.

    Returns:
        Raw DAG file content.

    Raises:
        FileNotFoundError: If the expected DAG file is missing.
    """
    path = DAGS_ROOT / file_name

    logger.info("Reading DAG file for design test | path=%s", path)

    return path.read_text(encoding="utf-8-sig")


def acceptance_helpers() -> dict[str, Any]:
    """Execute only pure acceptance helpers, without importing Airflow internals."""
    tree = ast.parse(HELPERS_PATH.read_text(encoding="utf-8"))
    names = {"parse_stage_flag", "acceptance_manual_only", "safe_empty_demo_audit_since",
             "empty_demo_readiness_args"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
             or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                 and target.id == "EMPTY_DEMO_AUDIT_SINCE_PATTERN" for target in node.targets)]
    namespace = dict(os=os, re=re, datetime=datetime, timezone=timezone, Any=Any)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(HELPERS_PATH), "exec"), namespace)
    return namespace


def validation_readiness_command() -> str:
    """Read the actual command template, not a test-owned approximation."""
    tree = ast.parse(read_dag_file("91_dag_dq_platform_validation.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "runner_plain_bash_task":
            options = {item.arg: item.value for item in node.keywords}
            if ast.literal_eval(options["task_id"]) == "t20_run_platform_readiness":
                return ast.literal_eval(options["project_command"])
    raise AssertionError("Readiness task missing")


# --- Defining Tests
@pytest.mark.parametrize("raw,expected", [(None, False), ("false", False), ("FALSE", False),
                                        ("true", True), ("TrUe", True)])
def test_manual_only_flag_changes_only_the_two_schedules(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("DQ_ACCEPTANCE_MANUAL_ONLY", raising=False)
    else:
        monkeypatch.setenv("DQ_ACCEPTANCE_MANUAL_ONLY", raw)
    namespace = acceptance_helpers()
    assert namespace["acceptance_manual_only"]() is expected
    namespace.update(DAILY_PLATFORM_SCHEDULE="5 0 * * *", timedelta=timedelta)
    expected_defaults = {SCHEDULED_DAG_FILE: "5 0 * * *",
                         "90_06_dag_dq_recovery_reconciliation.py": timedelta(minutes=1)}
    consumers = set()
    for path in DAGS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "acceptance_manual_only" for node in ast.walk(tree)):
            consumers.add(path.name)
            dag_call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == "DAG")
            schedule = next(item.value for item in dag_call.keywords if item.arg == "schedule")
            result = eval(compile(ast.Expression(schedule), str(path), "eval"), namespace)
            assert result == (None if expected else expected_defaults[path.name])
    assert consumers == set(expected_defaults)


@pytest.mark.parametrize("raw", ["", "0", "1", "yes", "no", "on", "off", " true", "false\n", "null"])
def test_manual_only_flag_rejects_non_boolean_env_without_echo(monkeypatch, raw) -> None:
    monkeypatch.setenv("DQ_ACCEPTANCE_MANUAL_ONLY", raw)
    with pytest.raises(ValueError, match="DQ_ACCEPTANCE_MANUAL_ONLY"):
        acceptance_helpers()["acceptance_manual_only"]()


@pytest.mark.parametrize("raw,expected", [
    (None, "2026-10-16T03:00:00+00:00"),
    ("2026-10-16T10:00:00+07:00", "2026-10-16T03:00:00+00:00"),
    ("2026-10-16T03:00:00.123456Z", "2026-10-16T03:00:00.123456+00:00"),
    ("2026-10-15T22:00:00-05:00", "2026-10-16T03:00:00+00:00"),
])
def test_empty_demo_boundary_is_aware_utc_and_one_shell_argument(raw, expected) -> None:
    from scripts.trigger_airflow_validation import validate_empty_demo_audit_since

    helpers = acceptance_helpers()
    logical_date = datetime(2026, 10, 16, 3, tzinfo=timezone.utc)
    assert helpers["safe_empty_demo_audit_since"](raw, logical_date) == expected
    if raw is not None:
        assert validate_empty_demo_audit_since(raw) == expected
    args = helpers["empty_demo_readiness_args"](True, raw, logical_date)
    assert shlex.split(args) == ["--expect-empty-demo", "--empty-demo-audit-since", expected]


@pytest.mark.parametrize("raw", [
    "", "2026-10-16", "2026-10-16T03:00:00", "2026-10-16 03:00:00Z",
    "2026-02-30T03:00:00Z", "2026-10-16T25:00:00Z", "2026-10-16T03:00:00+07:60",
    "2026-10-16T03:00:00+24:00", "2026-10-16T03:00:00Z\n",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
    "2026-10-16T03:00:00.1234567Z", "2026-10-16T03:00:00Z' ; echo unsafe",
    '2026-10-16T03:00:00Z$(echo unsafe)', True, 123,
])
def test_empty_demo_boundary_rejects_invalid_ambiguous_and_injected_values(raw) -> None:
    from scripts.trigger_airflow_validation import validate_empty_demo_audit_since

    with pytest.raises(ValueError, match="aware ISO timestamp"):
        acceptance_helpers()["safe_empty_demo_audit_since"](raw, None)
    with pytest.raises(ValueError, match="aware ISO timestamp"):
        validate_empty_demo_audit_since(raw)


@pytest.mark.parametrize("logical_date", [None, datetime(2026, 10, 16)])
def test_default_audit_boundary_never_guesses_a_timezone(logical_date) -> None:
    with pytest.raises(ValueError, match="aware ISO timestamp"):
        acceptance_helpers()["safe_empty_demo_audit_since"](None, logical_date)


def test_dag91_renders_optional_empty_demo_boundary_and_checks_dependency() -> None:
    helpers = acceptance_helpers()
    env = NativeEnvironment(undefined=StrictUndefined)
    env.globals["empty_demo_readiness_args"] = helpers["empty_demo_readiness_args"]
    template = env.from_string(validation_readiness_command())
    run = SimpleNamespace(conf={}, logical_date=datetime(2026, 10, 16, 3, tzinfo=timezone.utc))
    context = dict(dag_run=run, params=SimpleNamespace(web_operator_request_id=""))
    assert shlex.split(template.render(**context)) == ["python", "scripts/smoke_readiness.py"]
    run.conf = {"expect_empty_demo": True}
    assert shlex.split(template.render(**context))[-3:] == [
        "--expect-empty-demo", "--empty-demo-audit-since", "2026-10-16T03:00:00+00:00"]
    run.conf["empty_demo_audit_since"] = "2026-10-16T11:00:00+07:00"
    assert shlex.split(template.render(**context))[-1] == "2026-10-16T04:00:00+00:00"
    run.conf["expect_empty_demo"] = False
    with pytest.raises(ValueError, match="requires expect_empty_demo"):
        template.render(**context)
    run.conf = {"expect_empty_demo": "false"}
    assert "--expect-empty-demo" not in template.render(**context)
    run.conf = {"expect_empty_demo": 1}
    with pytest.raises(ValueError, match="expect_empty_demo"):
        template.render(**context)


def test_dag91_boundary_schema_and_cli_grammar_stay_aligned() -> None:
    from jsonschema import ValidationError, validate
    from scripts.trigger_airflow_validation import EMPTY_DEMO_AUDIT_SINCE_PATTERN

    helpers = acceptance_helpers()
    assert helpers["EMPTY_DEMO_AUDIT_SINCE_PATTERN"] == EMPTY_DEMO_AUDIT_SINCE_PATTERN
    tree = ast.parse(read_dag_file("91_dag_dq_platform_validation.py"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "validation_dag_params")
    namespace = dict(helpers, Param=lambda default, **schema: schema, VALIDATION_SUITE_NAMES=("all",))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<dag91-params>", "exec"), namespace)
    schema = namespace["validation_dag_params"]()["empty_demo_audit_since"]
    for value in (None, "2026-10-16T03:00:00Z", "2026-10-16T10:00:00.123456+07:00"):
        validate(value, schema)
    for value in ("", "2026-10-16T03:00:00Z\n", "2026-10-16T03:00:00+07:60"):
        with pytest.raises(ValidationError):
            validate(value, schema)


def test_only_platform_daily_orchestrator_has_cron_schedule() -> None:
    """
    Ensure only the platform entrypoint DAG owns the daily cron schedule.

    Returns:
        None.
    """
    daily_content = read_dag_file(SCHEDULED_DAG_FILE)

    # Only the platform-level orchestrator should be scheduled directly.
    assert "schedule=None if acceptance_manual_only() else DAILY_PLATFORM_SCHEDULE" in daily_content

    for dag_file in TRIGGERED_DAG_FILES:
        child_content = read_dag_file(dag_file)

        assert "schedule=None" in child_content
        assert "schedule=DAILY_PLATFORM_SCHEDULE" not in child_content
        assert "5 0 * * *" not in child_content

    for dag_file in SCHEDULED_ADMIN_DAG_FILES:
        content = read_dag_file(dag_file)
        assert "schedule=None if acceptance_manual_only() else timedelta(minutes=1)" in content
        assert "schedule=DAILY_PLATFORM_SCHEDULE" not in content


def test_orchestrator_triggers_reset_existing_child_runs() -> None:
    """
    Ensure orchestrator child triggers are repeatable for local Airflow testing.

    Returns:
        None.
    """
    for dag_file in ORCHESTRATOR_DAG_FILES:
        content = read_dag_file(dag_file)

        # Deterministic child run ids are useful, but retries need reset_dag_run=True.
        assert "reset_dag_run=False" not in content
        assert "reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN" in content


def test_platform_dag_numbering_matches_operational_flow() -> None:
    """
    Ensure DAG numbering keeps the demo flow easy to scan in Airflow UI.

    Returns:
        None.
    """
    expected_dag_ids = {
        "00_dag_dq_platform_daily_orchestrator",
        "10_dag_dq_orders_landing_orchestrator",
        "11_dag_dq_orders_seed_to_s3",
        "12_dag_dq_orders_load_raw_clickhouse",
        "20_dag_dq_orders_dbt_transform",
        "30_dag_dq_orders_quality_alerts",
        "40_dag_dq_orders_triage_agent",
        "90_dag_dq_platform_backfill_dispatcher",
        "90_02_dag_dq_platform_approved_actions",
        "90_03_dag_dq_incident_revalidation",
        "90_04_dag_dq_resolution_outbox_acceptance",
        "90_05_dag_dq_incident_recovery",
        "90_06_dag_dq_recovery_reconciliation",
        "90_07_dag_dq_recovery_acceptance",
        "91_dag_dq_platform_validation",
        "92_dag_dq_llm_provider_smoke",
        "93_dag_dq_agent_checkpoint_smoke",
        "94_dag_dq_agent_life_evaluation",
        "95_dag_dq_metadata_registry_sync",
        "96_dag_dq_schema_drift_detection",
        "97_dag_dq_metadata_lineage_agent_smoke",
        "98_dag_dq_control_plane_supervisor_smoke",
        "99_dag_dq_control_plane_resilience_smoke",
    }

    discovered_dag_ids = set()

    for path in DAGS_ROOT.glob("*.py"):
        content = path.read_text(encoding="utf-8")

        for dag_id in expected_dag_ids:
            dag_id_pattern = rf'DAG_ID\s*=\s*"{re.escape(dag_id)}"'

            if re.search(dag_id_pattern, content):
                discovered_dag_ids.add(dag_id)

    logger.info("Validated platform DAG numbering | discovered=%s", sorted(discovered_dag_ids))

    assert discovered_dag_ids == expected_dag_ids


def test_recovery_dags_keep_execution_manual_and_reconciliation_readonly() -> None:
    """Recovery execution/acceptance are manual; only read-only reconciliation is scheduled."""
    recovery = read_dag_file("90_05_dag_dq_incident_recovery.py")
    reconciliation = read_dag_file("90_06_dag_dq_recovery_reconciliation.py")
    acceptance = read_dag_file("90_07_dag_dq_recovery_acceptance.py")

    assert 'DAG_ID = "90_05_dag_dq_incident_recovery"' in recovery
    assert "schedule=None" in recovery
    assert 'DAG_ID = "90_06_dag_dq_recovery_reconciliation"' in reconciliation
    assert "schedule=None if acceptance_manual_only() else timedelta(minutes=1)" in reconciliation
    assert "is_paused_upon_creation=True" in reconciliation
    assert "reconcile_existing_runs" in reconciliation
    assert "runtime" not in reconciliation.lower()
    assert 'DAG_ID = "90_07_dag_dq_recovery_acceptance"' in acceptance
    assert "schedule=None" in acceptance

def test_validation_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure the validation DAG uses named suites and explicit Airflow task anchors.

    Returns:
        None.
    """
    content = read_dag_file("91_dag_dq_platform_validation.py")

    assert 'DAG_ID = "91_dag_dq_platform_validation"' in content
    assert "schedule=None" in content
    assert 'task_id="t10_run_named_pytest_suite"' in content
    assert 'task_id="t20_run_platform_readiness"' in content
    assert 'task_id="t30_emit_validation_summary"' in content
    assert "t00_start" in content
    assert "t90_finish" in content
    assert "runner_plain_bash_task" in content
    assert "python scripts/run_validation_suite.py" in content
    assert "python scripts/smoke_readiness.py" in content
    assert '"require_api": Param(' in content
    assert "--require-api" in content
    assert '"expect_empty_demo": Param(' in content
    assert '"expect_empty_demo": Param(\n            False,\n            type="boolean"' in content
    assert 'dag_run.conf.get(\\"expect_empty_demo\\", false)' in content
    assert "empty_demo_readiness_args" in content
    assert '"require_web": Param(' in content
    assert "--require-web" in content
    assert "arbitrary" not in content.lower()


def test_approved_action_dag_is_manual_exact_scope_and_retry_free() -> None:
    """Ensure side effects require one durable request and never auto-retry."""
    content = read_dag_file("90_02_dag_dq_platform_approved_actions.py")

    assert 'DAG_ID = "90_02_dag_dq_platform_approved_actions"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert "default_dag_args(retries=0)" in content
    assert 'task_id="t10_preview_or_execute"' in content
    assert '"approval_request_id": Param(' in content
    assert '"dry_run": Param(' in content
    assert "target_dag_id" not in approved_action_params_source(content)


def approved_action_params_source(content: str) -> str:
    """Return only the approved-action parameter function for scope checks."""
    start = content.index("def approved_action_params")
    end = content.index("# --- Defining DAG Structure")
    return content[start:end]


def test_quality_dag_observes_schema_drift_before_data_checks_and_alert_delivery() -> None:
    """
    Ensure daily schema findings become alerts before later DQ tasks can fail.

    Returns:
        None.
    """
    content = read_dag_file("30_dag_dq_orders_quality_alerts.py")
    ordered_task_ids = [
        'task_id="t10_detect_orders_schema_drift"',
        'task_id="t20_generate_schema_drift_alerts"',
        'task_id="t30_push_schema_drift_alerts"',
        'task_id="t40_profile_orders_tables"',
        'task_id="t50_run_orders_dq_checks"',
        'task_id="t60_generate_orders_alerts"',
        'task_id="t70_push_discord_alerts"',
    ]

    assert "--gate-mode observe" in content
    assert "pipelines.schema_drift.generate_alerts" in content
    assert [content.index(task_id) for task_id in ordered_task_ids] == sorted(
        content.index(task_id) for task_id in ordered_task_ids
    )

def test_makefile_exposes_airflow_validation_operator_commands() -> None:
    """
    Ensure operators can trigger and inspect validation runs without ad hoc commands.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-validate:" in content
    assert "airflow-validation-runs:" in content
    assert "airflow-validation-tasks:" in content
    assert "airflow-validation-logs:" in content
    assert "scripts/trigger_airflow_validation.py" in content
    assert "airflow tasks states-for-dag-run" in content
    assert "scripts/read_airflow_validation_logs.py" in content


def test_llm_provider_smoke_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure provider smoke execution stays manual, allowlisted, and Airflow-owned.

    Returns:
        None.
    """
    content = read_dag_file("92_dag_dq_llm_provider_smoke.py")

    assert 'DAG_ID = "92_dag_dq_llm_provider_smoke"' in content
    assert "schedule=None" in content
    assert 'task_id="t10_smoke_heuristic_baseline"' in content
    assert 'task_id="t20_smoke_selected_route"' in content
    assert 'task_id="t30_emit_llm_smoke_summary"' in content
    assert '"route_name": Param(' in content
    assert 'EXTERNAL_SMOKE_ROUTE_NAMES = ("cheap_summary",)' in content
    assert "enum=list(EXTERNAL_SMOKE_ROUTE_NAMES)" in content
    assert "--force-heuristic" in content
    assert "--strict-external-provider" in content
    assert '"run_external_provider": Param(' in content
    assert "max_output_tokens" not in content
    assert "SMOKE_PROMPT" not in content
    assert "API_KEY" not in content


def test_agent_checkpoint_smoke_dag_is_manual_and_cross_process() -> None:
    """
    Ensure DAG 93 proves checkpoint persistence through separate bounded tasks.

    Returns:
        None.
    """
    content = read_dag_file("93_dag_dq_agent_checkpoint_smoke.py")

    assert 'DAG_ID = "93_dag_dq_agent_checkpoint_smoke"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t10_initialize_checkpoint"' in content
    assert 'task_id="t20_resume_checkpoint"' in content
    assert 'task_id="t30_resume_completed_checkpoint"' in content
    assert 'task_id="t40_verify_checkpoint"' in content
    assert 'task_id="t42_replay_historical_checkpoint"' in content
    assert 'task_id="t43_repeat_historical_checkpoint_replay"' in content
    assert 'task_id="t45_store_report_side_effect"' in content
    assert 'task_id="t46_replay_report_side_effect"' in content
    assert 'task_id="t47_verify_report_side_effect"' in content
    assert 'task_id="t50_emit_checkpoint_summary"' in content
    assert "smoke_agent_checkpoint.py" in content
    assert "smoke_agent_side_effect_replay.py" in content
    assert "--phase historical-replay" in content
    assert "--phase historical-replay-repeat" in content


def test_triage_dag_exposes_bounded_historical_checkpoint_replay() -> None:
    """
    Ensure DAG 40 accepts exact replay identifiers without retrying the source thread.

    Returns:
        None.
    """
    content = read_dag_file("40_dag_dq_orders_triage_agent.py")

    assert '"checkpoint_replay_id": Param(' in content
    assert '"checkpoint_replay_request_id": Param(' in content
    assert '"confidence_threshold": Param(' in content
    assert '"max_evidence_iterations": Param(' in content
    assert '"max_model_calls": Param(' in content
    assert '"token_budget": Param(' in content
    assert '"estimated_cost_budget_usd": Param(' in content
    assert '"latency_budget_ms": Param(' in content
    assert "--checkpoint-replay-id" in content
    assert "--checkpoint-replay-request-id" in content
    assert "--confidence-threshold" in content
    assert "--max-evidence-iterations" in content
    assert "--max-model-calls" in content
    assert "--token-budget" in content
    assert "--estimated-cost-budget-usd" in content
    assert "--latency-budget-ms" in content
    assert "and not dag_run.conf.get" in content
    assert "checkpoint_replay_id" in content
    assert "Historical replay branches from an exact checkpoint" in content
    assert '"checkpoint_action": Param(' in content
    assert 'task_id="t05_inspect_checkpoint_history"' in content
    assert "scripts/inspect_agent_checkpoints.py" in content
    assert "checkpoint_history_next_node" in content
    assert 'task_id="t20_sync_discord_incidents"' in content
    assert "python -m apps.discord_bot.delivery" in content
    assert "apps.discord_bot.webhook" not in content
    assert "t10_run_agentic_triage\n        >> t20_sync_discord_incidents\n        >> t90_finish" in content


def test_alert_delivery_dags_and_environment_use_bot_delivery_contract() -> None:
    """Keep Airflow delivery and documented settings aligned with the bot API transport."""
    quality_dag = read_dag_file("30_dag_dq_orders_quality_alerts.py")
    triage_dag  = read_dag_file("40_dag_dq_orders_triage_agent.py")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "python -m apps.discord_bot.delivery" in quality_dag
    assert "python -m apps.discord_bot.delivery" in triage_dag
    assert "apps.discord_bot.webhook" not in quality_dag + triage_dag
    assert "DISCORD_DELIVERY_TIMEOUT_SECONDS=" in env_example
    assert "DISCORD_DELIVERY_MAX_ALERTS=" in env_example
    assert "DISCORD_ALERT_WEBHOOK_URL=" not in env_example
    assert "DISCORD_WEBHOOK_" not in env_example
    assert "APPROVED_ACTION_DISCORD_WEBHOOK_URL=" not in env_example


def test_makefile_routes_checkpoint_smoke_through_airflow() -> None:
    """
    Ensure checkpoint validation cannot bypass the Airflow administrative DAG.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-checkpoint-smoke:" in content
    assert "airflow-checkpoint-runs:" in content
    assert "airflow-checkpoint-tasks:" in content
    assert "airflow-checkpoint-logs:" in content
    assert "scripts/trigger_airflow_checkpoint_smoke.py" in content
    assert "airflow-triage:" in content
    assert "airflow-triage-runs:" in content
    assert "airflow-triage-tasks:" in content
    assert "airflow-triage-logs:" in content
    assert "scripts/trigger_airflow_triage.py" in content


def test_makefile_reads_retained_airflow_logs_from_stable_api_service() -> None:
    """
    Ensure operator log inspection does not depend on a transient Celery worker process.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")
    log_reader_command = "python /opt/airflow/project/scripts/read_airflow_validation_logs.py"

    assert f"$(AIRFLOW_WEB) {log_reader_command}" in content
    assert f"$(AIRFLOW_WORKER) {log_reader_command}" not in content


def test_life_evaluation_dag_is_manual_bounded_and_non_mutating() -> None:
    """
    Ensure DAG 94 evaluates stored reports without autonomous project mutation.

    Returns:
        None.
    """
    content = read_dag_file("94_dag_dq_agent_life_evaluation.py")

    assert 'DAG_ID = "94_dag_dq_agent_life_evaluation"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t05_prepare_source_report"' in content
    assert 'task_id="t10_evaluate_life_report"' in content
    assert 'task_id="t20_verify_life_artifacts"' in content
    assert 'task_id="t30_emit_life_summary"' in content
    assert "run_life_evaluation.py" in content
    assert "prepare_life_source_report.py" in content
    assert "verify_life_evaluation.py" in content
    assert "run_life_supervisor_comparison.py" in content
    assert "prepare_life_supervisor_comparison.py" in content
    assert "verify_life_supervisor_comparison.py" in content
    assert "supervisor_comparison" in content
    assert "single_supervisor_run_id" in content
    assert "fanout_supervisor_run_id" in content
    assert "fail_on_eval_failure" in content
    assert "enable_critic" in content
    assert "subprocess" not in content


def test_makefile_routes_life_evaluation_through_airflow() -> None:
    """
    Ensure operator LIFE evaluation targets retain Airflow acceptance logs.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-life-eval:" in content
    assert "airflow-life-runs:" in content
    assert "airflow-life-tasks:" in content
    assert "airflow-life-logs:" in content
    assert "life-eval: airflow-life-eval" in content
    assert "scripts/trigger_airflow_life_evaluation.py" in content
    assert "LIFE_SOURCE_MODE" in content
    assert "LIFE_SINGLE_SUPERVISOR_RUN_ID" in content
    assert "LIFE_FANOUT_SUPERVISOR_RUN_ID" in content


def test_metadata_registry_sync_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure metadata synchronization uses an allowlisted manual Airflow boundary.

    Returns:
        None.
    """
    content = read_dag_file("95_dag_dq_metadata_registry_sync.py")

    assert 'DAG_ID = "95_dag_dq_metadata_registry_sync"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t10_sync_metadata_registry"' in content
    assert 'task_id="t20_verify_metadata_registry"' in content
    assert 'task_id="t30_emit_metadata_sync_summary"' in content
    assert '"registry_name": Param(' in content
    assert "enum=list(METADATA_REGISTRY_NAMES)" in content
    assert "--mode sync" in content
    assert "--mode verify" in content


def test_makefile_routes_metadata_registry_operations_through_airflow() -> None:
    """
    Ensure operator metadata sync helpers preserve Airflow state and logs.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-metadata-sync:" in content
    assert "airflow-metadata-runs:" in content
    assert "airflow-metadata-tasks:" in content
    assert "airflow-metadata-logs:" in content
    assert "scripts/trigger_airflow_metadata_sync.py" in content


def test_schema_drift_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure schema drift detection uses an allowlisted Airflow operational boundary.

    Returns:
        None.
    """
    content = read_dag_file("96_dag_dq_schema_drift_detection.py")

    assert 'DAG_ID = "96_dag_dq_schema_drift_detection"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t10_detect_schema_drift"' in content
    assert 'task_id="t20_verify_schema_drift_evidence"' in content
    assert 'task_id="t30_emit_schema_drift_summary"' in content
    assert '"contract_name": Param(' in content
    assert "enum=list(SCHEMA_CONTRACT_NAMES)" in content
    assert "--mode detect" in content
    assert "--mode verify" in content
    assert "ALTER TABLE" not in content


def test_makefile_routes_schema_drift_operations_through_airflow() -> None:
    """
    Ensure operator schema drift helpers preserve Airflow state and logs.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-schema-drift:" in content
    assert "airflow-schema-drift-runs:" in content
    assert "airflow-schema-drift-tasks:" in content
    assert "airflow-schema-drift-logs:" in content
    assert "scripts/trigger_airflow_schema_drift.py" in content


def test_metadata_lineage_agent_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure the first bounded specialist uses a manual Airflow smoke boundary.

    Returns:
        None.
    """
    content = read_dag_file("97_dag_dq_metadata_lineage_agent_smoke.py")

    assert 'DAG_ID = "97_dag_dq_metadata_lineage_agent_smoke"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t10_run_metadata_lineage_agent"' in content
    assert 'task_id="t20_verify_metadata_lineage_audit"' in content
    assert 'task_id="t30_emit_metadata_lineage_summary"' in content
    assert "run_metadata_lineage_agent.py" in content
    assert "verify_metadata_lineage_agent.py" in content
    assert "no_llm_fallback" in content
    assert "ALTER TABLE" not in content
    assert "INSERT INTO" not in content


def test_makefile_routes_metadata_lineage_agent_through_airflow() -> None:
    """
    Ensure specialist smoke execution and log inspection remain Airflow-owned.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-metadata-lineage-agent:" in content
    assert "airflow-metadata-lineage-runs:" in content
    assert "airflow-metadata-lineage-tasks:" in content
    assert "airflow-metadata-lineage-logs:" in content
    assert "scripts/trigger_airflow_metadata_lineage_agent.py" in content


def test_control_plane_supervisor_dag_is_manual_bounded_and_auditable() -> None:
    """
    Ensure DAG 98 accepts one policy-routed handoff and verifies retained audit evidence.

    Returns:
        None.
    """
    content = read_dag_file("98_dag_dq_control_plane_supervisor_smoke.py")

    assert 'DAG_ID = "98_dag_dq_control_plane_supervisor_smoke"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert 'task_id="t10_run_control_plane_supervisor"' in content
    assert 'task_id="t20_verify_supervisor_audit"' in content
    assert 'task_id="t30_emit_supervisor_summary"' in content
    assert '"max_handoffs": Param(1' in content
    assert '"max_retries": Param(0' in content
    assert '"max_model_calls": Param(3' in content
    assert '"token_budget": Param(16_384' in content
    assert '"estimated_cost_budget_usd": Param(' in content
    assert '"latency_budget_ms": Param(' in content
    assert "--max-model-calls" in content
    assert "run_control_plane_supervisor.py" in content
    assert "verify_control_plane_execution.py" in content
    assert '"execution_mode": Param(' in content
    assert '"max_workers": Param(1' in content
    assert '"max_concurrency": Param(1' in content
    assert '"allow_external_llm": Param(' in content
    assert "--execution-mode" in content
    assert "--max-workers" in content
    assert "--max-concurrency" in content
    assert "--allow-external-llm" in content
    assert '"sql_proposal_base64": Param(' in content
    assert '"expected_sql_decision": Param(' in content
    assert '"schema_run_id": Param(' in content
    assert '"schema_finding_limit": Param(' in content
    assert '"expected_schema_assessment": Param(' in content
    assert '"domain": Param(' in content
    assert '"data_layer": Param(' in content
    assert '"certification_status": Param(' in content
    assert '"lifecycle_status": Param(' in content
    assert '"result_limit": Param(' in content
    assert '"max_depth": Param(' in content
    assert '"max_nodes": Param(' in content
    assert '"confidence_threshold": Param(' in content
    assert '"max_evidence_iterations": Param(' in content
    assert '"manifest_s3_uri": Param(' in content
    assert '"artifacts_bucket": Param(' in content
    assert '"artifacts_prefix": Param(' in content
    assert "--sql-proposal-base64" in content
    assert "--expected-sql-decision" in content
    assert "--schema-run-id" in content
    assert "--schema-finding-limit" in content
    assert "--expected-schema-assessment" in content
    assert "--expected-schema-run-id" in content
    assert "--domain" in content
    assert "--data-layer" in content
    assert "--certification-status" in content
    assert "--lifecycle-status" in content
    assert "--result-limit" in content
    assert "--max-depth" in content
    assert "--max-nodes" in content
    assert "--confidence-threshold" in content
    assert "--max-evidence-iterations" in content
    assert "--manifest-s3-uri" in content
    assert "--artifacts-bucket" in content
    assert "--artifacts-prefix" in content
    assert "ALTER TABLE" not in content
    assert "execute_remediation" not in content
    assert "run_guarded_sql" not in content


def test_makefile_routes_control_plane_supervisor_through_airflow() -> None:
    """
    Ensure supervisor execution and log inspection remain Airflow-owned.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-control-plane-supervisor:" in content
    assert "airflow-control-plane-runs:" in content
    assert "airflow-control-plane-tasks:" in content
    assert "airflow-control-plane-logs:" in content
    assert "scripts/trigger_airflow_control_plane_supervisor.py" in content
    assert "CONTROL_PLANE_SQL_FILE" in content
    assert "CONTROL_PLANE_MAX_HANDOFFS" in content
    assert "CONTROL_PLANE_MAX_RETRIES" in content
    assert "CONTROL_PLANE_MAX_MODEL_CALLS" in content
    assert "CONTROL_PLANE_ESTIMATED_COST_BUDGET_USD" in content
    assert "CONTROL_PLANE_EXPECTED_SQL_DECISION" in content
    assert "CONTROL_PLANE_SCHEMA_RUN_ID" in content
    assert "CONTROL_PLANE_SCHEMA_FINDING_LIMIT" in content
    assert "CONTROL_PLANE_EXPECTED_SCHEMA_ASSESSMENT" in content
    assert "CONTROL_PLANE_DOMAIN" in content
    assert "CONTROL_PLANE_DATA_LAYER" in content
    assert "CONTROL_PLANE_CERTIFICATION_STATUS" in content
    assert "CONTROL_PLANE_LIFECYCLE_STATUS" in content
    assert "CONTROL_PLANE_RESULT_LIMIT" in content
    assert "CONTROL_PLANE_MAX_DEPTH" in content
    assert "CONTROL_PLANE_MAX_NODES" in content
    assert "CONTROL_PLANE_CONFIDENCE_THRESHOLD" in content
    assert "CONTROL_PLANE_MAX_EVIDENCE_ITERATIONS" in content
    assert "CONTROL_PLANE_MANIFEST_S3_URI" in content
    assert "CONTROL_PLANE_ARTIFACTS_BUCKET" in content
    assert "CONTROL_PLANE_ARTIFACTS_PREFIX" in content


def test_control_plane_resilience_dag_is_manual_bounded_and_non_mutating() -> None:
    """
    Ensure DAG 99 validates controlled failure modes without production mutation.

    Returns:
        None.
    """
    content = read_dag_file("99_dag_dq_control_plane_resilience_smoke.py")
    helper  = (DAGS_ROOT / "dq_platform" / "control_plane_resilience.py").read_text(
        encoding="utf-8"
    )
    assert 'DAG_ID = "99_dag_dq_control_plane_resilience_smoke"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert "default_dag_args(retries=0)" in content
    assert 'task_id="t10_run_resilience_scenario"' in content
    assert 'task_id="t20_verify_resilience_audit"' in content
    assert 'task_id="t30_emit_resilience_summary"' in content
    assert '"scenario": Param(' in content
    assert "enum=list(CONTROL_PLANE_RESILIENCE_SCENARIOS)" in content
    assert '"terminal_failure"' in helper
    assert '"concurrent_budget_reservation"' in helper
    assert "run_control_plane_resilience_smoke.py" in content
    assert "verify_control_plane_resilience.py" in content
    assert "ALTER TABLE" not in content
    assert "execute_remediation" not in content
    assert "run_guarded_sql" not in content
    assert "trigger_backfill" not in content


def test_makefile_routes_control_plane_resilience_through_airflow() -> None:
    """
    Ensure resilience execution and retained-log inspection remain Airflow-owned.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-control-plane-resilience:" in content
    assert "airflow-control-plane-resilience-runs:" in content
    assert "airflow-control-plane-resilience-tasks:" in content
    assert "airflow-control-plane-resilience-logs:" in content
    assert "scripts/trigger_airflow_control_plane_resilience.py" in content
    assert "CONTROL_PLANE_RESILIENCE_SCENARIO" in content
    assert "CONTROL_PLANE_RESILIENCE_RUN_ID" in content


def test_makefile_build_runner_is_windows_compatible() -> None:
    """
    Ensure the runner build target does not rely on POSIX inline environment syntax.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "COMPOSE_ANSI=never COMPOSE_PROGRESS=plain" not in content
    assert "$(DC) --ansi never --progress plain build $(RUNNER_SERVICE)" in content


def test_runner_helpers_forward_airflow_task_identity_to_dq_runner() -> None:
    """The inner runner must receive task identity for per-task artifact namespaces."""
    source = (DAGS_ROOT / "dq_platform" / "helpers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: ast.get_source_segment(source, node) for node in tree.body
                 if isinstance(node, ast.FunctionDef)}

    for name in ("build_runner_bash_command", "build_runner_plain_bash_command"):
        command_builder = functions[name]
        assert "exec --env AIRFLOW_CTX_DAG_RUN_ID=\"${{AIRFLOW_CTX_DAG_RUN_ID:-}}\"" in command_builder
        assert "--env AIRFLOW_CTX_DAG_ID=\"${{AIRFLOW_CTX_DAG_ID:-}}\"" in command_builder
        assert "--env AIRFLOW_CTX_TASK_ID=\"${{AIRFLOW_CTX_TASK_ID:-}}\"" in command_builder
        assert "{container} /bin/sh -lc \"cd /app && {project_command}\"" in command_builder
        assert "exec {container} /bin/sh" not in command_builder


def test_makefile_generic_service_recreate_is_windows_compatible() -> None:
    """
    Ensure the generic recreate target validates SVC without POSIX shell syntax.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert 'if [ -z "$(SVC)" ]' not in content
    assert "$(if $(strip $(SVC)),,$(error SVC is required." in content
    assert "$(DC) up -d --force-recreate $(SVC)" in content


def test_makefile_routes_llm_smoke_through_airflow() -> None:
    """
    Ensure operator smoke targets trigger DAG 92 instead of bypassing Airflow.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "airflow-llm-smoke:" in content
    assert "airflow-llm-runs:" in content
    assert "airflow-llm-tasks:" in content
    assert "airflow-llm-logs:" in content
    assert "scripts/trigger_airflow_llm_smoke.py" in content
    assert "agent-llm-smoke: airflow-llm-smoke" in content
    assert "python -m agent.llm.client --route" not in content


def test_incident_revalidation_dag_is_scoped_manual_and_read_only() -> None:
    content = read_dag_file("90_03_dag_dq_incident_revalidation.py")
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert "default_dag_args(retries=0)" in content
    assert "dagrun_timeout=timedelta(minutes=15)" in content
    assert '"resolution_request_id": Param(' in content
    assert 'pattern=REQUEST_ID_PATTERN' in content
    for task in ("t10_load_request", "t20_run_scoped_checks",
                 "t30_persist_validation_result", "t40_notify_validation_result"):
        assert f'task_id="{task}"' in content
    assert 't40_notify_validation_result.trigger_rule = "all_done"' in content
    assert "python -m agent.tools.incident_resolution" in content
    assert "ALTER TABLE" not in content
    assert "trigger_backfill" not in content
    assert "--sql" not in content


def resolution_outbox_design_tree() -> ast.Module:
    """Parse the sidecar without importing Airflow or running acceptance stages."""
    return ast.parse(read_dag_file("90_04_dag_dq_resolution_outbox_acceptance.py"))


def test_resolution_outbox_dag_is_manual_administrative_and_bounded() -> None:
    tree = resolution_outbox_design_tree()
    dag_call = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "DAG")
    keywords = {item.arg: item.value for item in dag_call.keywords}
    assert ast.literal_eval(keywords["schedule"]) is None
    assert ast.literal_eval(keywords["catchup"]) is False
    assert ast.literal_eval(keywords["max_active_runs"]) == 1
    assert ast.unparse(keywords["start_date"]) == "DEFAULT_START_DATE"
    assert ast.unparse(keywords["default_args"]) == "default_dag_args(retries=0)"
    assert ast.unparse(keywords["dagrun_timeout"]) == "timedelta(minutes=25)"
    assert ast.unparse(keywords["params"]) == "resolution_outbox_acceptance_params()"
    assert {"manual", "administrative"} <= set(ast.literal_eval(keywords["tags"]))
    helper_import = next(node for node in tree.body if isinstance(node, ast.ImportFrom)
                         and node.module == "dq_platform.helpers")
    assert "DEFAULT_START_DATE" in {item.name for item in helper_import.names}


def test_resolution_outbox_params_require_safe_identity_date_and_explicit_consent() -> None:
    tree = resolution_outbox_design_tree()
    constants = {node.targets[0].id: ast.literal_eval(node.value)
                 for node in tree.body if isinstance(node, ast.Assign)
                 and node.targets[0].id in {"ACCEPTANCE_RUN_ID_PATTERN", "BUSINESS_DATE_PATTERN"}}
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "resolution_outbox_acceptance_params")
    params = next(node.value for node in function.body if isinstance(node, ast.Return))
    calls = {ast.literal_eval(key): value for key, value in zip(params.keys, params.values)}
    assert set(calls) == {"acceptance_run_id", "business_date", "confirm_fixture_closure"}
    for name, length, format_name in (
        ("acceptance_run_id", 36, "uuid"), ("business_date", 10, "date"),
    ):
        call = calls[name]
        assert isinstance(call.func, ast.Name) and call.func.id == "Param"
        assert not call.args  # No default means the manual trigger must supply it.
        schema = {item.arg: item.value for item in call.keywords}
        assert "default" not in schema
        assert ast.literal_eval(schema["type"]) == "string"
        assert ast.literal_eval(schema["minLength"]) == length
        assert ast.literal_eval(schema["maxLength"]) == length
        assert ast.literal_eval(schema["format"]) == format_name
        pattern = constants[schema["pattern"].id]
        valid = "019c8af3-36cd-78d2-a81b-6ab3986ccd75" if length == 36 else "2026-10-13"
        assert re.search(pattern, valid)
        for unsafe in ("", "x" + valid, valid + "x", valid + "'; echo injected; '",
                       valid.replace("-", "/"), valid.replace("0", "g", 1)):
            assert re.search(pattern, unsafe) is None
        assert len(valid + "\n") > length  # Length bound also rejects '$' newline matching.
    consent = calls["confirm_fixture_closure"]
    assert [ast.literal_eval(arg) for arg in consent.args] == [False]
    assert ast.literal_eval(next(item.value for item in consent.keywords
                                 if item.arg == "type")) == "boolean"


def test_resolution_outbox_stage_commands_are_fixed_and_reject_unbounded_inputs() -> None:
    tree = resolution_outbox_design_tree()
    stage_constant = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and node.targets[0].id == "ACCEPTANCE_STAGES")
    stages = ast.literal_eval(stage_constant.value)
    assert stages == ("prepare", "capture", "restore", "request", "wait", "close_fault",
                      "replay", "verify")
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "acceptance_stage_command")
    namespace = {"ACCEPTANCE_STAGES": stages}
    # Execute only the pure command builder, never the DAG or acceptance CLI.
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<stage-command>", "exec"), namespace)
    command_builder = namespace["acceptance_stage_command"]
    for stage in stages:
        command = command_builder(stage)
        assert command == (
            "python scripts/accept_resolution_outbox.py "
            "--run-id '{{ params.acceptance_run_id }}' "
            "--dt '{{ params.business_date }}' "
            f"--stage {stage} "
            "{% if params.confirm_fixture_closure %}--confirm-demo-closure{% endif %}"
        )
        assert set(re.findall(r"--[a-z][a-z-]*", command)) == {
            "--run-id", "--dt", "--stage", "--confirm-demo-closure",
        }
        for forbidden in ("--host", "--table", "--key", "--sql", "--model", "dag_run.conf"):
            assert forbidden not in command
    for stage in ("", "prepare; echo injected", "--sql", "remediate", "unknown"):
        with pytest.raises(ValueError, match="Unsupported resolution outbox acceptance stage"):
            command_builder(stage)


def test_resolution_outbox_tasks_use_fixed_stages_and_bounded_timeouts() -> None:
    tree = resolution_outbox_design_tree()
    tasks = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "runner_plain_bash_task"):
            keywords = {item.arg: item.value for item in node.keywords}
            task_id = ast.literal_eval(keywords["task_id"])
            assert task_id not in tasks
            command = keywords["project_command"]
            assert isinstance(command, ast.Call) and command.func.id == "acceptance_stage_command"
            assert len(command.args) == 1 and not command.keywords
            timeout = keywords["execution_timeout"]
            assert timeout.func.id == "timedelta" and not timeout.args
            assert len(timeout.keywords) == 1 and timeout.keywords[0].arg == "minutes"
            tasks[task_id] = (ast.literal_eval(command.args[0]),
                              ast.literal_eval(timeout.keywords[0].value))
    assert tasks == {
        "t10_prepare_fixture": ("prepare", 2),
        "t20_detect_fixture_alert": ("capture", 2),
        "t30_restore_fixture_contract": ("restore", 2),
        "t40_request_readonly_check": ("request", 2),
        "t50_wait_for_fresh_pass": ("wait", 15),
        "t60_confirm_with_delivery_fault": ("close_fault", 2),
        "t70_replay_pending_outbox": ("replay", 2),
        "t80_verify_recovery": ("verify", 2),
    }


def test_resolution_outbox_cleanup_cannot_bypass_capture_failure() -> None:
    tree = resolution_outbox_design_tree()
    rules = {ast.unparse(node.targets[0]): ast.literal_eval(node.value)
             for node in ast.walk(tree) if isinstance(node, ast.Assign)
             and isinstance(node.targets[0], ast.Attribute)
             and node.targets[0].attr == "trigger_rule"}
    assert rules == {"t30_restore_fixture_contract.trigger_rule": "all_done"}
    assert "trigger_rule" not in {
        item.arg for node in ast.walk(tree) if isinstance(node, ast.Call)
        for item in node.keywords
    }

    def chain_names(node: ast.expr) -> list[str]:
        """Flatten a static task chain to inspect its actual dependency edges."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift):
            return chain_names(node.left) + chain_names(node.right)
        assert isinstance(node, ast.Name)
        return [node.id]

    edges = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.BinOp):
            names = chain_names(node.value)
            edges.update(zip(names, names[1:]))
    chain = ["t00_start", "t10_prepare_fixture", "t20_detect_fixture_alert",
             "t30_restore_fixture_contract", "t40_request_readonly_check",
             "t50_wait_for_fresh_pass", "t60_confirm_with_delivery_fault",
             "t70_replay_pending_outbox", "t80_verify_recovery", "t90_finish"]
    assert edges == set(zip(chain, chain[1:])) | {
        ("t20_detect_fixture_alert", "t40_request_readonly_check"),
    }


def test_resolution_outbox_docs_disclose_owned_mutation_and_synthetic_fault_boundary() -> None:
    tree = resolution_outbox_design_tree()
    docs = next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and node.targets[0].id == "DOC_MD")
    normalized = " ".join(docs.split())
    for boundary in (
        "real PostgreSQL, ClickHouse, and Discord",
        "dq_resolution_acceptance.schema_fixture", "setup/restore only",
        "deterministic read-only check", "90_03_dag_dq_incident_revalidation",
        "defaults to false", "owner-delegated demo closure", "exact same evidence gate",
        "synthetic application fault BEFORE the Discord call", "not an actual Discord outage",
        "No LLM calls, raw orders mutation, or remediation",
        "deadline or worker loss can interrupt cleanup",
    ):
        assert boundary in normalized


def test_date_argument_helper_allows_explicit_alert_runs_without_dates() -> None:
    """
    Ensure explicit-alert triage does not receive an invalid empty --dt argument.

    Returns:
        None.
    """
    content = HELPERS_PATH.read_text(encoding="utf-8")

    assert 'elif [ -n "$RUN_DT" ]; then' in content
    assert 'DATE_ARGS="--dt $RUN_DT"' in content
    assert 'DATE_ARGS=""' in content
