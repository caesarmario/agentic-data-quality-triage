####
## LIFE Agent Reliability Evaluation Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Contract tests for deterministic, auditable, and non-mutating LIFE evaluation."""

# --- Importing Libraries
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent.evaluation import life
from agent.evaluation.life import (
    LIFE_SCENARIO_NAMES,
    build_life_artifact_keys,
    evaluate_life_report,
    persist_life_evaluation,
    render_life_evaluation,
    validate_report_s3_uri,
)
from agent.evaluation.triage import (
    accepted_categories_for,
    load_scenario_catalog,
    load_yaml_file,
    resolve_scenario_path,
)
from dags.dq_platform import life_evaluation as airflow_life_evaluation
from scripts import (
    read_airflow_validation_logs,
    trigger_airflow_life_evaluation,
    verify_life_evaluation,
)


# --- Defining Constants
SOURCE_REPORT_URI = "s3://dq-artifacts/agent-reports/test-run/report.json"
AGENT_RUN_ID      = "11111111-1111-4111-8111-111111111111"
ALERT_ID          = "22222222-2222-4222-8222-222222222222"


# --- Defining Test Helpers
def load_scenario(scenario_id: str) -> dict:
    """
    Load one repository incident scenario for LIFE tests.

    Args:
        scenario_id: Allowlisted incident scenario identifier.

    Returns:
        Parsed incident scenario dictionary.
    """
    return load_yaml_file(resolve_scenario_path(scenario_id))


def build_reliable_report(scenario: dict) -> dict:
    """
    Build a report that satisfies deterministic LIFE policy for one scenario.

    Args:
        scenario: Parsed incident scenario with ground truth.

    Returns:
        JSON-like triage report payload.
    """
    ground_truth    = scenario["ground_truth"]
    triage_required = bool(scenario["expected_pipeline_behavior"]["triage_required"])
    accepted        = sorted(accepted_categories_for(ground_truth["root_cause_category"]))
    evidence_id     = "evidence-001"
    evidence        = []
    supporting_ids  = []

    if triage_required:
        evidence = [
            {
                "evidence_id": evidence_id,
                "evidence_type": "dq_history",
                "tool_name": "dq_history",
                "description": "Historical DQ evidence for the affected partition.",
                "query": "",
                "rows": [{"status": "fail"}],
                "summary": "The deterministic history confirms the expected incident signal for this scenario.",
            }
        ]
        supporting_ids = [evidence_id]

    alert = {}

    if ground_truth["expected_alert"]:
        signal = ground_truth["expected_dq_signals"][0]
        alert  = {
            "alert_id": ALERT_ID,
            "alert_key": (
                f"orders|dq_failure|2026-07-16|{signal['table_name']}|"
                f"{signal['check_name']}|table"
            ),
            "table_name": signal["table_name"],
            "metric": signal["check_name"],
            "severity": signal["severity"],
        }

    return {
        "agent_run_id": AGENT_RUN_ID,
        "alert": alert,
        "summary": (
            "The reliability investigation matched the expected incident pattern and retained "
            "deterministic evidence for operator review."
        ),
        "impact": (
            "Downstream warehouse models and reporting outputs may be incomplete for the affected "
            "partition until a human reviews and approves the recommended recovery plan."
        ),
        "confidence": 0.90,
        "top_hypothesis": {
            "root_cause_category": accepted[0],
            "supporting_evidence_ids": supporting_ids,
        },
        "evidence": evidence,
        "evidence_plan": {"planner_source": "llm"},
        "hypothesis_framing": {"source": "llm"},
        "recommended_actions": [
            "Review the collected evidence and prepare a human-approved remediation plan."
        ],
        "approval_gated_actions": [],
    }


class FakeS3Body:
    """Provide the minimal streaming-body interface used by the verifier."""

    def __init__(self, text: str) -> None:
        """
        Store UTF-8 text for one fake S3 response.

        Args:
            text: Artifact body returned by read().

        Returns:
            None.
        """
        self.body = text.encode("utf-8")

    def read(self) -> bytes:
        """
        Return the stored artifact bytes.

        Returns:
            UTF-8 encoded artifact body.
        """
        return self.body


class FakeS3Client:
    """Provide in-memory S3 objects for verification tests."""

    def __init__(self, objects: dict[str, str]) -> None:
        """
        Store object bodies keyed by S3 object key.

        Args:
            objects: Mapping of S3 keys to text bodies.

        Returns:
            None.
        """
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3-compatible contract
        """
        Return one boto3-compatible object response.

        Args:
            Bucket: Requested S3 bucket.
            Key: Requested S3 object key.

        Returns:
            Dictionary containing a readable Body object.
        """
        assert Bucket == "dq-artifacts"

        return {"Body": FakeS3Body(self.objects[Key])}


class FakeClickHouseClient:
    """Return one LIFE audit event while retaining query parameters."""

    def __init__(self, row: tuple) -> None:
        """
        Store one result row for the verifier query.

        Args:
            row: Audit row returned from query().

        Returns:
            None.
        """
        self.row        = row
        self.query_text = ""
        self.parameters = {}

    def query(self, query: str, parameters: dict | None = None) -> SimpleNamespace:
        """
        Capture a parameterized query and return the configured row.

        Args:
            query: ClickHouse query text.
            parameters: Bound query parameters.

        Returns:
            Namespace exposing result_rows like clickhouse-connect.
        """
        self.query_text = query
        self.parameters = parameters or {}

        return SimpleNamespace(result_rows=[self.row])


class FakeDagRun:
    """Provide minimal Airflow context for LIFE summary tests."""

    dag_id = "94_dag_dq_agent_life_evaluation"
    run_id = "manual__life_eval_summary_test"
    conf   = {
        "scenario": "missing_latest_day",
        "report_s3_uri": SOURCE_REPORT_URI,
        "evaluation_run_id": "life-eval-summary-test",
        "artifact_prefix": "agent-life",
    }


# --- Defining Catalog And Passing Evaluation Tests
def test_life_scenario_registry_matches_ground_truth_catalog() -> None:
    """
    Ensure Airflow and evaluator allowlists cover every incident ground-truth config.

    Returns:
        None.
    """
    catalog_ids = tuple(sorted(item.scenario_id for item in load_scenario_catalog()))

    assert tuple(sorted(LIFE_SCENARIO_NAMES)) == catalog_ids
    assert airflow_life_evaluation.LIFE_SCENARIO_NAMES == LIFE_SCENARIO_NAMES
    assert airflow_life_evaluation.SAFE_REPORT_S3_URI.pattern == life.SAFE_REPORT_S3_URI.pattern


@pytest.mark.parametrize("scenario_id", LIFE_SCENARIO_NAMES)
def test_life_evaluator_passes_all_supported_ground_truth_scenarios(scenario_id: str) -> None:
    """
    Ensure baseline and all five incident scenarios can produce a passing evaluation.

    Args:
        scenario_id: Parameterized incident scenario identifier.

    Returns:
        None.
    """
    scenario   = load_scenario(scenario_id)
    report     = build_reliable_report(scenario)
    evaluation = evaluate_life_report(
        scenario=scenario,
        report=report,
        report_s3_uri=SOURCE_REPORT_URI,
        evaluation_run_id=f"life-{scenario_id}",
    )

    assert evaluation.eval_status == "pass"
    assert evaluation.failed_checks == []
    assert evaluation.failure_categories == []
    assert evaluation.requires_human_approval is False
    assert evaluation.source_report_sha256 == life.stable_payload_hash(report)


def test_life_output_contains_required_contract_fields() -> None:
    """
    Ensure the persisted model contains every minimum field promised in the roadmap.

    Returns:
        None.
    """
    scenario   = load_scenario("missing_latest_day")
    report     = build_reliable_report(scenario)
    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-contract")
    payload    = evaluation.model_dump(mode="json")

    required_fields = {
        "run_id",
        "scenario_id",
        "agent_run_id",
        "report_s3_uri",
        "eval_status",
        "failed_checks",
        "failure_category",
        "life_stage",
        "suggested_change_type",
        "suggested_change_summary",
        "requires_human_approval",
        "created_at",
    }

    assert required_fields <= set(payload)


# --- Defining Failure Classification Tests
def test_life_classifies_malformed_report_without_secondary_noise() -> None:
    """
    Ensure malformed reports stop before misleading deeper classifications.

    Returns:
        None.
    """
    scenario = load_scenario("missing_latest_day")
    report   = build_reliable_report(scenario)
    report.pop("summary")

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-malformed")

    assert evaluation.eval_status == "fail"
    assert evaluation.failure_categories == ["malformed_report"]
    assert len(evaluation.checks) == 1


def test_life_classifies_wrong_root_cause() -> None:
    """
    Ensure incorrect root-cause ranking is a hard reliability failure.

    Returns:
        None.
    """
    scenario = load_scenario("missing_latest_day")
    report   = build_reliable_report(scenario)
    report["top_hypothesis"]["root_cause_category"] = "unrelated_category"

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-wrong-root")

    assert evaluation.eval_status == "fail"
    assert "wrong_root_cause" in evaluation.failure_categories


def test_life_classifies_missing_evidence() -> None:
    """
    Ensure unsupported hypotheses cannot pass report reliability evaluation.

    Returns:
        None.
    """
    scenario = load_scenario("missing_segment")
    report   = build_reliable_report(scenario)
    report["evidence"] = []

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-missing-evidence")

    assert evaluation.eval_status == "fail"
    assert "missing_evidence" in evaluation.failure_categories


def test_life_classifies_low_confidence_as_review() -> None:
    """
    Ensure low confidence requests review instead of pretending the report is reliable.

    Returns:
        None.
    """
    scenario = load_scenario("late_arriving")
    report   = build_reliable_report(scenario)
    report["confidence"] = 0.40

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-low-confidence")

    assert evaluation.eval_status == "review"
    assert evaluation.failure_categories == ["low_confidence"]
    assert evaluation.requires_human_approval is True


def test_life_classifies_ungated_mutating_recommendation() -> None:
    """
    Ensure mutation recommendations require a matching approval-gated action contract.

    Returns:
        None.
    """
    scenario = load_scenario("missing_latest_day")
    report   = build_reliable_report(scenario)
    report["recommended_actions"] = ["Backfill the missing partition immediately."]

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-ungated-action")

    assert evaluation.eval_status == "fail"
    assert "hallucinated_action" in evaluation.failure_categories


def test_life_accepts_mutating_recommendation_with_explicit_approval_gate() -> None:
    """
    Ensure remediation may be proposed when execution remains human-controlled.

    Returns:
        None.
    """
    scenario = load_scenario("missing_latest_day")
    report   = build_reliable_report(scenario)
    report["recommended_actions"] = ["Backfill the missing partition after operator approval."]
    report["approval_gated_actions"] = [
        {
            "action_type": "backfill",
            "reason": "The source partition is missing.",
            "requires_approval": True,
        }
    ]

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-gated-action")

    assert evaluation.eval_status == "pass"


def test_life_classifies_sql_guardrail_violation() -> None:
    """
    Ensure retained SQL evidence is revalidated through read-only guardrails.

    Returns:
        None.
    """
    scenario = load_scenario("duplicates_spike")
    report   = build_reliable_report(scenario)
    report["evidence"][0]["tool_name"] = "clickhouse_sql"
    report["evidence"][0]["query"]     = "DROP TABLE dq.raw_orders"

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-sql-guardrail")

    assert evaluation.eval_status == "fail"
    assert "sql_guardrail_issue" in evaluation.failure_categories


def test_life_classifies_llm_fallback_as_review() -> None:
    """
    Ensure deterministic provider fallback remains visible without becoming a hard failure.

    Returns:
        None.
    """
    scenario = load_scenario("null_spike")
    report   = build_reliable_report(scenario)
    report["evidence_plan"]["planner_source"] = "provider_fallback"

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-llm-fallback")

    assert evaluation.eval_status == "review"
    assert "llm_fallback" in evaluation.failure_categories


def test_life_classifies_weak_stakeholder_explanation() -> None:
    """
    Ensure terse technical output is routed to human-readable narrative review.

    Returns:
        None.
    """
    scenario = load_scenario("missing_segment")
    report   = build_reliable_report(scenario)
    report["summary"] = "Too short."
    report["impact"]  = "Also too short."

    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-weak-wording")

    assert evaluation.eval_status == "review"
    assert "weak_stakeholder_explanation" in evaluation.failure_categories


# --- Defining Persistence And Verification Tests
def test_life_artifact_keys_are_deterministic_and_path_safe() -> None:
    """
    Ensure run identifiers cannot redirect LIFE writes outside the configured prefix.

    Returns:
        None.
    """
    json_key, markdown_key = build_life_artifact_keys("life-safe-run")

    assert json_key == "agent-life/run_id=life-safe-run/life_report.json"
    assert markdown_key == "agent-life/run_id=life-safe-run/life_report.md"

    with pytest.raises(ValueError, match="path-safe"):
        build_life_artifact_keys("life-safe-run", prefix="../../unsafe")


def test_life_report_s3_uri_rejects_traversal_and_shell_fragments() -> None:
    """
    Ensure Airflow source URI parameters cannot escape the report artifact contract.

    Returns:
        None.
    """
    assert validate_report_s3_uri(SOURCE_REPORT_URI) == SOURCE_REPORT_URI

    with pytest.raises(ValueError, match="path-safe"):
        validate_report_s3_uri("s3://dq-artifacts/agent-reports/../../report.json")

    with pytest.raises(ValueError, match="path-safe"):
        validate_report_s3_uri("s3://dq-artifacts/agent-reports/x/report.json'; rm -rf /tmp/x")


def test_persist_life_evaluation_writes_only_proposal_artifacts_and_audit(monkeypatch) -> None:
    """
    Ensure persistence leaves source input unchanged and performs only approved writes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    scenario       = load_scenario("missing_latest_day")
    source_report  = build_reliable_report(scenario)
    source_snapshot = deepcopy(source_report)
    evaluation     = evaluate_life_report(
        scenario,
        source_report,
        SOURCE_REPORT_URI,
        "life-persist-test",
    )
    writes = []
    audits = []

    def fake_put_text_artifact(**kwargs):
        """Capture text artifact writes and return their S3 URI."""
        writes.append(("text", kwargs))

        return f"s3://{kwargs['bucket']}/{kwargs['key']}"

    def fake_put_json_artifact(**kwargs):
        """Capture JSON artifact writes and return their S3 URI."""
        writes.append(("json", kwargs))

        return f"s3://{kwargs['bucket']}/{kwargs['key']}"

    def fake_write_agent_audit_event(**kwargs):
        """Capture one audit event without touching ClickHouse."""
        audits.append(kwargs)

        return UUID(AGENT_RUN_ID)

    monkeypatch.setattr(life, "put_text_artifact", fake_put_text_artifact)
    monkeypatch.setattr(life, "put_json_artifact", fake_put_json_artifact)
    monkeypatch.setattr(life, "write_agent_audit_event", fake_write_agent_audit_event)

    persisted = persist_life_evaluation(
        evaluation=evaluation,
        source_report=source_report,
        clickhouse_client=object(),
    )

    assert source_report == source_snapshot
    assert [item[0] for item in writes] == ["text", "json"]
    assert len(audits) == 1
    assert audits[0]["action"] == "life_evaluation_completed"
    assert audits[0]["status"] == "success"
    assert persisted.requires_human_approval is False
    assert "does not modify prompts" in persisted.markdown_report


def test_persist_life_evaluation_rejects_changed_source_payload(monkeypatch) -> None:
    """
    Ensure an evaluation cannot be persisted against a source payload changed after scoring.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    scenario      = load_scenario("missing_latest_day")
    source_report = build_reliable_report(scenario)
    evaluation    = evaluate_life_report(scenario, source_report, SOURCE_REPORT_URI, "life-hash-test")
    source_report["summary"] = "Changed after evaluation."

    monkeypatch.setattr(life, "put_text_artifact", lambda **kwargs: pytest.fail("unexpected S3 write"))

    with pytest.raises(ValueError, match="SHA-256"):
        persist_life_evaluation(
            evaluation=evaluation,
            source_report=source_report,
            clickhouse_client=object(),
        )


def test_verify_life_evaluation_checks_s3_and_clickhouse_audit() -> None:
    """
    Ensure the operational verifier requires matching artifacts and audit correlation.

    Returns:
        None.
    """
    scenario   = load_scenario("missing_latest_day")
    report     = build_reliable_report(scenario)
    evaluation = evaluate_life_report(scenario, report, SOURCE_REPORT_URI, "life-verify-test")
    json_key, markdown_key = build_life_artifact_keys(evaluation.run_id)
    json_uri               = f"s3://dq-artifacts/{json_key}"
    markdown_uri           = f"s3://dq-artifacts/{markdown_key}"
    persisted              = evaluation.model_copy(
        update={
            "json_report_s3_uri": json_uri,
            "markdown_report_s3_uri": markdown_uri,
        }
    )
    persisted.markdown_report = render_life_evaluation(persisted)
    objects = {
        json_key: json.dumps(persisted.model_dump(mode="json")),
        markdown_key: persisted.markdown_report,
    }
    audit_payload = {
        "run_id": persisted.run_id,
        "source_report_sha256": persisted.source_report_sha256,
    }
    clickhouse_client = FakeClickHouseClient(
        (
            "life_evaluation_completed",
            "success",
            json.dumps(audit_payload),
            json_uri,
        )
    )

    summary = verify_life_evaluation.verify_life_evaluation(
        evaluation_run_id=persisted.run_id,
        scenario_id="missing_latest_day",
        expected_source_report_s3_uri=SOURCE_REPORT_URI,
        s3_client=FakeS3Client(objects),
        clickhouse_client=clickhouse_client,
    )

    assert summary["status"] == "success"
    assert summary["audit_event_count"] == 1
    assert clickhouse_client.parameters == {"run_id": "life-verify-test"}
    assert "ts >= now() - INTERVAL 7 DAY" in clickhouse_client.query_text
    assert "ORDER BY ts DESC" in clickhouse_client.query_text
    assert "LIMIT 10" in clickhouse_client.query_text


# --- Defining Airflow Boundary Tests
def test_life_trigger_uses_safe_json_and_unique_identifiers() -> None:
    """
    Ensure Windows-safe Airflow triggers carry only validated LIFE configuration.

    Returns:
        None.
    """
    now = datetime(2026, 7, 16, 3, 4, 5, 678901, tzinfo=timezone.utc)
    run_id, evaluation_id = trigger_airflow_life_evaluation.build_life_evaluation_identifiers(now=now)
    command = trigger_airflow_life_evaluation.build_trigger_command(
        run_id=run_id,
        evaluation_run_id=evaluation_id,
        scenario_id="missing_latest_day",
        report_s3_uri=SOURCE_REPORT_URI,
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert run_id == "manual__life_eval_20260716T030405678901"
    assert evaluation_id == "life-eval-20260716T030405678901"
    assert conf["scenario"] == "missing_latest_day"
    assert conf["report_s3_uri"] == SOURCE_REPORT_URI
    assert conf["evaluation_run_id"] == evaluation_id
    assert command[-1] == trigger_airflow_life_evaluation.LIFE_EVALUATION_DAG_ID
    assert ";" not in "".join(command)


def test_life_trigger_unpauses_before_creating_dag_run(monkeypatch) -> None:
    """
    Ensure the operator helper always makes the manual DAG available before trigger.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    commands = []
    monkeypatch.setattr(trigger_airflow_life_evaluation, "run_command", commands.append)

    resolved = trigger_airflow_life_evaluation.trigger_life_evaluation(
        scenario_id="missing_latest_day",
        report_s3_uri=SOURCE_REPORT_URI,
        run_id="manual__life_eval_test",
        evaluation_run_id="life-eval-test",
    )

    assert resolved == ("manual__life_eval_test", "life-eval-test")
    assert commands[0] == [
        "airflow",
        "dags",
        "unpause",
        trigger_airflow_life_evaluation.LIFE_EVALUATION_DAG_ID,
    ]
    assert commands[1][0:3] == ["airflow", "dags", "trigger"]


def test_life_airflow_summary_exposes_artifacts_and_task_state(monkeypatch) -> None:
    """
    Ensure the final Airflow task logs source, artifacts, and upstream state evidence.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("ARTIFACTS_BUCKET", "dq-artifacts")

    summary = airflow_life_evaluation.emit_life_evaluation_summary(dag_run=FakeDagRun())

    assert summary["result"] == "success"
    assert summary["evaluation_run_id"] == "life-eval-summary-test"
    assert summary["source_report_s3_uri"] == SOURCE_REPORT_URI
    assert summary["json_report_s3_uri"].endswith("/life_report.json")
    assert summary["task_states"] == {
        "t10_evaluate_life_report": "success",
        "t20_verify_life_artifacts": "success",
    }


def test_life_dag_is_allowlisted_for_retained_log_inspection(tmp_path, capsys) -> None:
    """
    Ensure operators can read retained DAG 94 task logs through the shared helper.

    Args:
        tmp_path: Pytest temporary directory fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    dag_id    = read_airflow_validation_logs.LIFE_EVALUATION_DAG_ID
    run_id    = "manual__life_eval_log_test"
    directory = read_airflow_validation_logs.airflow_log_directory(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    log_path = directory / "task_id=t20_verify_life_artifacts" / "attempt=1.log"

    log_path.parent.mkdir(parents=True)
    log_path.write_text("LIFE artifacts and audit verified\n", encoding="utf-8")

    return_code = read_airflow_validation_logs.print_airflow_logs(
        dag_id=dag_id,
        run_id=run_id,
        log_root=tmp_path,
    )
    output = capsys.readouterr().out

    assert return_code == 0
    assert "t20_verify_life_artifacts" in output
    assert "LIFE artifacts and audit verified" in output
