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
    "96_dag_dq_schema_drift_detection.py",
    "97_dag_dq_metadata_lineage_agent_smoke.py",
    "98_dag_dq_control_plane_supervisor_smoke.py",
    "99_dag_dq_control_plane_resilience_smoke.py",
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
    assert "--checkpoint-replay-id" in content
    assert "--checkpoint-replay-request-id" in content
    assert "and not dag_run.conf.get" in content
    assert "checkpoint_replay_id" in content
    assert "Historical replay branches from an exact checkpoint" in content
    assert '"checkpoint_action": Param(' in content
    assert 'task_id="t05_inspect_checkpoint_history"' in content
    assert "scripts/inspect_agent_checkpoints.py" in content
    assert "checkpoint_history_next_node" in content
    assert "t00_start >> t05_inspect_checkpoint_history >> t10_run_agentic_triage >> t90_finish" in content


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
    registry = (
        PROJECT_ROOT / "agent" / "supervisor" / "scenario_registry.py"
    ).read_text(encoding="utf-8")

    assert 'DAG_ID = "99_dag_dq_control_plane_resilience_smoke"' in content
    assert "schedule=None" in content
    assert "max_active_runs=1" in content
    assert "default_dag_args(retries=0)" in content
    assert 'task_id="t10_run_resilience_scenario"' in content
    assert 'task_id="t20_verify_resilience_audit"' in content
    assert 'task_id="t30_emit_resilience_summary"' in content
    assert '"scenario": Param(' in content
    assert "enum=list(CONTROL_PLANE_RESILIENCE_SCENARIOS)" in content
    assert "from agent.supervisor.scenario_registry import" in helper
    assert '"terminal_failure"' in registry
    assert '"concurrent_budget_reservation"' in registry
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
