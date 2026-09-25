####
## Supervisor Mode Comparison Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Test conservative single-versus-fan-out evaluation and Airflow boundaries."""

# --- Importing Libraries
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent.evaluation import supervisor_comparison as comparison
from agent.evaluation.supervisor_comparison import (
    SupervisorModeComparisonReport,
    SupervisorModeScore,
    SupervisorModeSnapshot,
    build_comparison_artifact_keys,
    build_supervisor_mode_snapshot,
    decide_comparison,
    persist_supervisor_mode_comparison,
    render_supervisor_mode_comparison,
    validate_comparison_identity,
)
from scripts import trigger_airflow_life_evaluation, verify_life_supervisor_comparison


# --- Defining Constants
SINGLE_PARENT_ID = UUID("11111111-1111-4111-8111-111111111111")
FANOUT_PARENT_ID = UUID("22222222-2222-4222-8222-222222222222")
ALERT_KEY        = "DQ-20260504-DEB2B0"
REPORT_URI       = "s3://dq-artifacts/agent-reports/test/report.md"


# --- Defining Test Helpers
class FakeSupervisorAuditClient:
    """Return parent-specific supervisor audit rows through a ClickHouse-like interface."""

    def __init__(self, rows_by_parent: dict[str, list[tuple]]) -> None:
        """
        Store audit rows by normalized parent UUID.

        Args:
            rows_by_parent: Mapping from parent UUID string to audit rows.

        Returns:
            None.
        """
        self.rows_by_parent = rows_by_parent
        self.queries: list[tuple[str, dict]] = []

    def query(self, query: str, parameters: dict | None = None) -> SimpleNamespace:
        """
        Return rows for the requested parent UUID.

        Args:
            query: Parameterized ClickHouse query.
            parameters: Bound query parameters.

        Returns:
            Namespace exposing result_rows.
        """
        bound = parameters or {}
        self.queries.append((query, bound))

        return SimpleNamespace(
            result_rows=self.rows_by_parent.get(str(bound.get("parent_run_id", "")), [])
        )


class FakeArtifactS3Client:
    """Return in-memory comparison artifacts through a boto3-like interface."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        """
        Store artifact bytes by bucket and key.

        Args:
            objects: Mapping from S3 bucket/key pairs to object bytes.

        Returns:
            None.
        """
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict[str, BytesIO]:
        """
        Read one in-memory object.

        Args:
            Bucket: S3 bucket name.
            Key: S3 object key.

        Returns:
            Dictionary containing a readable response body.
        """
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}


class FakeComparisonAuditClient:
    """Return fixed comparison audit rows through a ClickHouse-like interface."""

    def __init__(self, rows: list[tuple]) -> None:
        """
        Store audit rows returned by the verifier query.

        Args:
            rows: Fixed ClickHouse-shaped result rows.

        Returns:
            None.
        """
        self.rows = rows

    def query(self, query: str, parameters: dict | None = None) -> SimpleNamespace:
        """
        Return the configured rows.

        Args:
            query: Parameterized ClickHouse query.
            parameters: Bound query parameters.

        Returns:
            Namespace exposing result_rows.
        """
        return SimpleNamespace(result_rows=self.rows)


def audit_row(
    action: str,
    status: str,
    input_payload: dict,
    output_payload: dict,
    report_uri: str = "",
) -> tuple:
    """
    Build one fixed-shape supervisor audit row.

    Args:
        action: Supervisor lifecycle action.
        status: Lifecycle status.
        input_payload: Serialized supervisor request facts.
        output_payload: Serialized result or aggregation facts.
        report_uri: Optional report artifact URI.

    Returns:
        Tuple matching the comparison audit query projection.
    """
    return (
        "2026-09-02 00:00:00",
        action,
        status,
        125,
        json.dumps(input_payload),
        json.dumps(output_payload),
        report_uri,
        ALERT_KEY,
    )


def base_input(mode: str) -> dict:
    """
    Build the identity-bearing supervisor audit input.

    Args:
        mode: Single or fan-out execution mode.

    Returns:
        Bounded input payload.
    """
    return {
        "execution_mode": mode,
        "requested_intent": "triage_alert",
        "qualified_name": "dq.fct_orders_daily",
        "max_concurrency": 1 if mode == "single" else 2,
    }


def handoff_output(evidence_count: int, specialist: str = "incident_triage_agent") -> dict:
    """
    Build one terminal handoff payload.

    Args:
        evidence_count: Retained evidence reference count.
        specialist: Specialist name.

    Returns:
        Bounded output payload.
    """
    return {
        "selected_specialist": specialist,
        "task_type": "triage_alert" if specialist == "incident_triage_agent" else "blast_radius",
        "confidence": 0.80,
        "evidence_reference_count": evidence_count,
        "model_call_count": 0,
        "token_usage": 0,
        "estimated_cost_usd": 0.0,
    }


def snapshot(mode: str, evidence_count: int = 5) -> SupervisorModeSnapshot:
    """
    Build one typed comparison snapshot.

    Args:
        mode: Single or fan-out mode.
        evidence_count: Aggregate retained evidence count.

    Returns:
        SupervisorModeSnapshot.
    """
    is_fanout = mode == "fanout"

    return SupervisorModeSnapshot(
        parent_run_id=FANOUT_PARENT_ID if is_fanout else SINGLE_PARENT_ID,
        execution_mode=mode,
        requested_intent="triage_alert",
        alert_key=ALERT_KEY,
        qualified_name="dq.fct_orders_daily",
        terminal_status="success",
        worker_count=2 if is_fanout else 1,
        completed_worker_count=2 if is_fanout else 1,
        optional_failure_count=0,
        required_failure_count=0,
        max_concurrency=2 if is_fanout else 1,
        evidence_reference_count=evidence_count,
        triage_confidence=0.80,
        model_call_count=0,
        token_usage=0,
        estimated_cost_usd=0.0,
        duration_ms=200 if is_fanout else 100,
        report_s3_uri="s3://dq-artifacts/agent-reports/test/report.json",
    )


def persisted_comparison_fixture() -> tuple[
    SupervisorModeComparisonReport,
    FakeArtifactS3Client,
    tuple,
]:
    """
    Build matching persisted artifacts and one replay-safe audit row.

    Returns:
        Typed report, in-memory S3 client, and one ClickHouse-shaped audit row.
    """
    run_id                = "life-mode-verify"
    json_key, markdown_key = build_comparison_artifact_keys(run_id)
    json_uri              = f"s3://dq-artifacts/{json_key}"
    markdown_uri          = f"s3://dq-artifacts/{markdown_key}"
    report = SupervisorModeComparisonReport(
        comparison_run_id=run_id,
        scenario_id="missing_latest_day",
        alert_key=ALERT_KEY,
        single=snapshot("single"),
        fanout=snapshot("fanout"),
        single_score=score(),
        fanout_score=score(),
        decision="keep_single",
        measurable_benefit=False,
        decision_reasons=["Tie keeps single mode."],
        requires_human_review=True,
        summary="No measurable improvement.",
        json_report_s3_uri=json_uri,
        markdown_report_s3_uri=markdown_uri,
    )
    report.markdown_report = render_supervisor_mode_comparison(report)
    s3_client = FakeArtifactS3Client(
        {
            ("dq-artifacts", json_key): json.dumps(
                report.model_dump(mode="json"),
                sort_keys=True,
            ).encode("utf-8"),
            ("dq-artifacts", markdown_key): report.markdown_report.encode("utf-8"),
        }
    )
    audit_row = (
        comparison.SUPERVISOR_COMPARISON_ACTION,
        "success",
        json.dumps(
            {
                "comparison_run_id": run_id,
                "single_parent_run_id": str(SINGLE_PARENT_ID),
                "fanout_parent_run_id": str(FANOUT_PARENT_ID),
            }
        ),
        json.dumps(
            {
                "decision": "keep_single",
                "requires_human_review": True,
            }
        ),
        json_uri,
    )

    return report, s3_client, audit_row


def score(
    eval_status: str = "pass",
    failed_checks: list[str] | None = None,
    confidence: float = 0.80,
    expected_evidence_status: str = "pass",
) -> SupervisorModeScore:
    """
    Build a compact deterministic LIFE score.

    Args:
        eval_status: Overall LIFE status.
        failed_checks: Non-passing check names.
        confidence: Source report confidence.
        expected_evidence_status: Evidence coverage check status.

    Returns:
        SupervisorModeScore.
    """
    failures      = failed_checks or []
    check_statuses = {
        "report_contract": "pass",
        "expected_evidence": expected_evidence_status,
        "confidence": "review" if "confidence" in failures else "pass",
    }

    return SupervisorModeScore(
        eval_status=eval_status,
        passed_check_count=sum(value == "pass" for value in check_statuses.values()),
        total_check_count=len(check_statuses),
        failed_checks=failures,
        failure_categories=["low_confidence"] if failures else [],
        expected_evidence_status=expected_evidence_status,
        check_statuses=check_statuses,
        confidence=confidence,
        source_report_sha256="a" * 64,
    )


# --- Defining Snapshot And Identity Tests
def test_snapshot_reads_single_and_fanout_audit_evidence() -> None:
    """
    Ensure snapshot loading retains execution mode, reports, workers, and usage.

    Returns:
        None.
    """
    single_input = base_input("single")
    fanout_input = base_input("fanout")
    client = FakeSupervisorAuditClient(
        {
            str(SINGLE_PARENT_ID): [
                audit_row(
                    "supervisor_handoff_completed",
                    "success",
                    single_input,
                    handoff_output(5),
                    REPORT_URI,
                ),
                audit_row(
                    "supervisor_final_decision",
                    "success",
                    single_input,
                    {
                        **handoff_output(5),
                        "model_call_count": 1,
                        "token_usage": 200,
                        "estimated_cost_usd": 0.001,
                    },
                ),
            ],
            str(FANOUT_PARENT_ID): [
                audit_row(
                    "supervisor_handoff_completed",
                    "success",
                    fanout_input,
                    handoff_output(5),
                    REPORT_URI,
                ),
                audit_row(
                    "supervisor_handoff_completed",
                    "success",
                    fanout_input,
                    handoff_output(3, specialist="metadata_lineage_agent"),
                ),
                audit_row(
                    "supervisor_aggregation_completed",
                    "success",
                    fanout_input,
                    {
                        "resilience": {
                            "completed_count": 2,
                            "optional_failure_count": 0,
                            "required_failure_count": 0,
                        }
                    },
                ),
                audit_row(
                    "supervisor_final_decision",
                    "success",
                    fanout_input,
                    {
                        "resilience": {
                            "worker_count": 2,
                            "model_call_count": 1,
                            "token_usage": 300,
                            "estimated_cost_usd": 0.002,
                        }
                    },
                ),
            ],
        }
    )

    single = build_supervisor_mode_snapshot(SINGLE_PARENT_ID, client)
    fanout = build_supervisor_mode_snapshot(FANOUT_PARENT_ID, client)

    assert single.execution_mode == "single"
    assert single.worker_count == 1
    assert single.report_s3_uri.endswith("/report.json")
    assert single.model_call_count == 1
    assert fanout.execution_mode == "fanout"
    assert fanout.worker_count == 2
    assert fanout.completed_worker_count == 2
    assert fanout.evidence_reference_count == 8
    assert fanout.max_concurrency == 2


def test_comparison_identity_rejects_different_alerts() -> None:
    """
    Ensure unrelated incidents cannot be presented as one mode comparison.

    Returns:
        None.
    """
    fanout = snapshot("fanout").model_copy(update={"alert_key": "DQ-OTHER"})

    with pytest.raises(ValueError, match="same non-empty alert key"):
        validate_comparison_identity(snapshot("single"), fanout)


# --- Defining Conservative Decision Tests
def test_tie_keeps_single_even_when_fanout_retains_more_aggregate_references() -> None:
    """
    Ensure unintegrated branch references do not masquerade as report improvement.

    Returns:
        None.
    """
    decision, benefit, reasons = decide_comparison(
        single_snapshot=snapshot("single", evidence_count=5),
        fanout_snapshot=snapshot("fanout", evidence_count=9),
        single_score=score(),
        fanout_score=score(),
    )

    assert decision == "keep_single"
    assert benefit is False
    assert any("did not improve" in reason for reason in reasons)
    assert any("ties keep single" in reason for reason in reasons)


def test_fanout_becomes_candidate_only_when_report_quality_improves() -> None:
    """
    Ensure a better deterministic report score creates review eligibility only.

    Returns:
        None.
    """
    single_score = score(
        eval_status="review",
        failed_checks=["confidence"],
        confidence=0.65,
    )
    fanout_score = score(eval_status="pass", confidence=0.80)

    decision, benefit, reasons = decide_comparison(
        single_snapshot=snapshot("single"),
        fanout_snapshot=snapshot("fanout"),
        single_score=single_score,
        fanout_score=fanout_score,
    )

    assert decision == "fanout_candidate"
    assert benefit is True
    assert any("LIFE status improved" in reason for reason in reasons)


def test_missing_report_score_is_insufficient_evidence() -> None:
    """
    Ensure missing report evidence cannot promote fan-out.

    Returns:
        None.
    """
    decision, benefit, reasons = decide_comparison(
        single_snapshot=snapshot("single"),
        fanout_snapshot=snapshot("fanout"),
        single_score=score(),
        fanout_score=None,
    )

    assert decision == "insufficient_evidence"
    assert benefit is False
    assert "Both modes" in reasons[0]


# --- Defining Persistence And Trigger Tests
def test_persistence_retains_human_review_and_replay_safe_audit(monkeypatch) -> None:
    """
    Ensure comparison persistence cannot silently enable fan-out.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    writes: list[tuple[str, dict]] = []
    audits: list[dict]             = []
    report = SupervisorModeComparisonReport(
        comparison_run_id="life-mode-test",
        scenario_id="missing_latest_day",
        alert_key=ALERT_KEY,
        single=snapshot("single"),
        fanout=snapshot("fanout"),
        single_score=score(),
        fanout_score=score(),
        decision="keep_single",
        measurable_benefit=False,
        decision_reasons=["Tie keeps single mode."],
        requires_human_review=True,
        summary="No measurable improvement.",
    )

    monkeypatch.setattr(
        comparison,
        "put_text_artifact",
        lambda **kwargs: writes.append(("text", kwargs)),
    )
    monkeypatch.setattr(
        comparison,
        "put_json_artifact",
        lambda **kwargs: writes.append(("json", kwargs)),
    )
    monkeypatch.setattr(
        comparison,
        "write_agent_audit_event",
        lambda **kwargs: audits.append(kwargs),
    )

    persisted = persist_supervisor_mode_comparison(report, clickhouse_client=object())

    assert [kind for kind, _ in writes] == ["text", "json"]
    assert persisted.requires_human_review is True
    assert "cannot enable fan-out" in persisted.markdown_report
    assert audits[0]["action"] == comparison.SUPERVISOR_COMPARISON_ACTION
    assert audits[0]["idempotency_key"]
    assert audits[0]["output_payload"]["decision"] == "keep_single"


def test_comparison_artifact_keys_are_deterministic() -> None:
    """
    Ensure comparison artifacts use their own stable path.

    Returns:
        None.
    """
    json_key, markdown_key = build_comparison_artifact_keys("life-mode-test")

    assert json_key == "agent-life-comparisons/run_id=life-mode-test/comparison.json"
    assert markdown_key == "agent-life-comparisons/run_id=life-mode-test/comparison.md"


def test_airflow_trigger_accepts_only_two_distinct_parent_uuids_for_comparison() -> None:
    """
    Ensure comparison identifiers enter Airflow through validated JSON only.

    Returns:
        None.
    """
    command = trigger_airflow_life_evaluation.build_trigger_command(
        run_id="manual__life_comparison_test",
        evaluation_run_id="life-comparison-test",
        scenario_id="missing_latest_day",
        source_mode="supervisor_comparison",
        single_supervisor_run_id=str(SINGLE_PARENT_ID),
        fanout_supervisor_run_id=str(FANOUT_PARENT_ID),
    )
    conf = json.loads(command[command.index("-c") + 1])

    assert conf["source_mode"] == "supervisor_comparison"
    assert conf["report_s3_uri"] == ""
    assert conf["single_supervisor_run_id"] == str(SINGLE_PARENT_ID)
    assert conf["fanout_supervisor_run_id"] == str(FANOUT_PARENT_ID)
    assert conf["artifact_prefix"] == comparison.DEFAULT_COMPARISON_ARTIFACT_PREFIX

    with pytest.raises(ValueError, match="must be different"):
        trigger_airflow_life_evaluation.build_trigger_command(
            run_id="manual__life_comparison_invalid",
            evaluation_run_id="life-comparison-invalid",
            scenario_id="missing_latest_day",
            source_mode="supervisor_comparison",
            single_supervisor_run_id=str(SINGLE_PARENT_ID),
            fanout_supervisor_run_id=str(SINGLE_PARENT_ID),
        )


def test_comparison_verifier_requires_matching_artifacts_and_one_audit_event() -> None:
    """
    Ensure the cross-process verifier accepts only fully correlated evidence.

    Returns:
        None.
    """
    report, s3_client, audit = persisted_comparison_fixture()
    summary = verify_life_supervisor_comparison.verify_supervisor_comparison(
        comparison_run_id=report.comparison_run_id,
        scenario_id=report.scenario_id,
        single_parent_run_id=SINGLE_PARENT_ID,
        fanout_parent_run_id=FANOUT_PARENT_ID,
        bucket="dq-artifacts",
        s3_client=s3_client,
        clickhouse_client=FakeComparisonAuditClient([audit]),
    )

    assert summary["status"] == "success"
    assert summary["decision"] == "keep_single"
    assert summary["measurable_benefit"] is False
    assert summary["audit_event_count"] == 1

    with pytest.raises(ValueError, match="exactly one replay-safe comparison audit event"):
        verify_life_supervisor_comparison.verify_supervisor_comparison(
            comparison_run_id=report.comparison_run_id,
            scenario_id=report.scenario_id,
            single_parent_run_id=SINGLE_PARENT_ID,
            fanout_parent_run_id=FANOUT_PARENT_ID,
            bucket="dq-artifacts",
            s3_client=s3_client,
            clickhouse_client=FakeComparisonAuditClient([]),
        )


def test_airflow_trigger_rejects_malformed_comparison_parent_uuid() -> None:
    """
    Ensure malformed parent identifiers never enter Airflow dag_run.conf.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="valid parent UUIDs"):
        trigger_airflow_life_evaluation.build_trigger_command(
            run_id="manual__life_comparison_invalid_uuid",
            evaluation_run_id="life-comparison-invalid-uuid",
            scenario_id="missing_latest_day",
            source_mode="supervisor_comparison",
            single_supervisor_run_id="not-a-uuid",
            fanout_supervisor_run_id=str(FANOUT_PARENT_ID),
        )
