####
## Airflow DAG Design Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
import re
from pathlib import Path


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
    "91_dag_dq_platform_validation.py",
    "92_dag_dq_llm_provider_smoke.py",
    "93_dag_dq_agent_checkpoint_smoke.py",
    "94_dag_dq_agent_life_evaluation.py",
    "95_dag_dq_metadata_registry_sync.py",
]

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

    return path.read_text(encoding="utf-8")


# --- Defining Tests
def test_only_platform_daily_orchestrator_has_cron_schedule() -> None:
    """
    Ensure only the platform entrypoint DAG owns the daily cron schedule.

    Returns:
        None.
    """
    daily_content = read_dag_file(SCHEDULED_DAG_FILE)

    # Only the platform-level orchestrator should be scheduled directly.
    assert "schedule=DAILY_PLATFORM_SCHEDULE" in daily_content

    for dag_file in TRIGGERED_DAG_FILES:
        child_content = read_dag_file(dag_file)

        assert "schedule=None" in child_content
        assert "schedule=DAILY_PLATFORM_SCHEDULE" not in child_content
        assert "5 0 * * *" not in child_content


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
        "91_dag_dq_platform_validation",
        "92_dag_dq_llm_provider_smoke",
        "93_dag_dq_agent_checkpoint_smoke",
        "94_dag_dq_agent_life_evaluation",
        "95_dag_dq_metadata_registry_sync",
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
    assert "arbitrary" not in content.lower()

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
    assert "enum=list(LLM_SMOKE_ROUTE_NAMES)" in content
    assert "--force-heuristic" in content
    assert "--require-provider" in content
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
    assert 'task_id="t50_emit_checkpoint_summary"' in content
    assert "smoke_agent_checkpoint.py" in content


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
    assert 'task_id="t10_evaluate_life_report"' in content
    assert 'task_id="t20_verify_life_artifacts"' in content
    assert 'task_id="t30_emit_life_summary"' in content
    assert "run_life_evaluation.py" in content
    assert "verify_life_evaluation.py" in content
    assert "fail_on_eval_failure" in content
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


def test_makefile_build_runner_is_windows_compatible() -> None:
    """
    Ensure the runner build target does not rely on POSIX inline environment syntax.

    Returns:
        None.
    """
    content = MAKEFILE_PATH.read_text(encoding="utf-8")

    assert "COMPOSE_ANSI=never COMPOSE_PROGRESS=plain" not in content
    assert "$(DC) --ansi never --progress plain build $(RUNNER_SERVICE)" in content


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
