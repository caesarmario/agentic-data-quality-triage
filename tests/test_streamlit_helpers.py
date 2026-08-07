####
## Streamlit Helper Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError

from apps.streamlit import app as streamlit_app
from apps.streamlit.app import (
    answer_ui_copilot_question,
    build_blast_radius_display_rows,
    build_ui_backfill_approval_payload,
    build_llm_runtime_summary,
    build_ui_copilot_context,
    classify_reliability_state,
    create_ui_backfill_approval_request,
    decide_ui_approval_request,
    load_approval_queue_rows,
    load_life_evaluation_rows,
    matching_blast_radius_result,
    matching_triage_result,
    request_ui_dbt_blast_radius,
    request_ui_copilot_api,
    summarize_alert_rows,
    summarize_approval_queue_rows,
    summarize_life_evaluation_rows,
)


# --- Defining Tests
def test_build_llm_runtime_summary_returns_operator_friendly_metrics() -> None:
    """
    Validate Streamlit AI runtime cards use normalized audit metadata.

    Returns:
        None.
    """
    summary = build_llm_runtime_summary(
        audit_rows=[
            {
                "action": "llm_route_completed",
                "llm_route": {
                    "runtime_mode": "heuristic_fallback",
                    "provider": "heuristic",
                    "model": "heuristic-v1",
                    "requested_route": "triage_reasoning",
                    "executed_route": "evidence_summary",
                    "input_tokens": 720,
                    "output_tokens": 407,
                    "estimated_cost_display": "$0.000000",
                    "duration_ms": 4583,
                    "fallback_summary": "OpenAI quota was unavailable.",
                },
            }
        ]
    )

    assert summary is not None
    assert summary["mode_label"] == "Heuristic fallback"
    assert summary["provider_model"] == "heuristic / heuristic-v1"
    assert summary["route_label"] == "triage_reasoning -> evidence_summary"
    assert summary["token_label"] == "720 in / 407 out"


def test_summarize_alert_rows_counts_status_severity_and_reports() -> None:
    """
    Validate alert summary counts used by the Streamlit reliability overview.

    Returns:
        None.
    """
    alerts = [
        {
            "severity": "critical",
            "status": "open",
            "dt": "2026-06-10",
            "table_name": "dq.stg_orders",
            "report_s3_uri": "s3://dq-artifacts/report.md",
        },
        {
            "severity": "warning",
            "status": "triaged",
            "dt": "2026-06-10",
            "table_name": "dq.fct_orders_daily",
            "report_s3_uri": "",
        },
    ]

    summary = summarize_alert_rows(alerts)

    assert summary["total_alerts"] == 2
    assert summary["critical_count"] == 1
    assert summary["warning_count"] == 1
    assert summary["open_count"] == 1
    assert summary["affected_table_count"] == 2
    assert summary["report_count"] == 1
    assert summary["affected_dates"] == ["2026-06-10"]


def test_classify_reliability_state_marks_critical_attention() -> None:
    """
    Validate critical alert classification for the reliability overview.

    Returns:
        None.
    """
    state = classify_reliability_state(
        {
            "critical_count": 1,
            "warning_count": 0,
            "open_count": 1,
        }
    )

    assert state["label"] == "Critical attention required"
    assert state["css_class"] == "dq-health-critical"


def test_classify_reliability_state_marks_warning_watchlist() -> None:
    """
    Validate warning/open alert classification for the reliability overview.

    Returns:
        None.
    """
    state = classify_reliability_state(
        {
            "critical_count": 0,
            "warning_count": 1,
            "open_count": 1,
        }
    )

    assert state["label"] == "Warning watchlist"
    assert state["css_class"] == "dq-health-warning"


def test_classify_reliability_state_marks_stable_filters() -> None:
    """
    Validate stable classification when no alert requires attention.

    Returns:
        None.
    """
    summary = summarize_alert_rows([])
    state   = classify_reliability_state(summary)

    assert state["label"] == "Stable for selected filters"
    assert state["css_class"] == "dq-health-stable"

def sample_copilot_triage_result(alert_key: str) -> dict[str, object]:
    """
    Build a compact matching triage result for Streamlit Copilot tests.

    Args:
        alert_key: Stable alert key stored in the result summary.

    Returns:
        Triage result dictionary with report-like test doubles.
    """
    hypothesis = SimpleNamespace(
        title="Missing partition",
        recommended_action="Prepare an approval-gated backfill preview.",
    )
    evidence = SimpleNamespace(
        tool_name="clickhouse_sql",
        evidence_type="sql_result",
        summary="The selected partition contains zero rows.",
        row_count=1,
        s3_uri="",
    )
    report = SimpleNamespace(
        summary="The selected partition is missing.",
        impact="Daily reporting may be incomplete.",
        top_hypothesis=hypothesis,
        confidence=0.91,
        recommended_actions=["Validate upstream landing data."],
        approval_gated_actions=[{"action_type": "backfill"}],
        report_id="RPT-TEST01",
        json_report_s3_uri="s3://dq-artifacts/agent-reports/report.json",
        evidence=[evidence],
    )

    return {
        "summary": {"alert_key": alert_key},
        "report": report,
    }


def test_matching_triage_result_rejects_stale_alert_context() -> None:
    """
    Validate that session-state triage context cannot leak between selected alerts.

    Returns:
        None.
    """
    latest = sample_copilot_triage_result("orders|old-alert")

    assert matching_triage_result("orders|new-alert", latest) is None


def test_build_ui_copilot_context_uses_matching_report_and_evidence() -> None:
    """
    Validate report, evidence, and audit context for the selected alert.

    Returns:
        None.
    """
    alert_key = "orders|matching-alert"
    context   = build_ui_copilot_context(
        alert={"alert_key": alert_key},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
    )

    assert context["has_report"] is True
    assert context["report_context"]["report_id"] == "RPT-TEST01"
    assert context["report_context"]["approval_required"] is True
    assert context["evidence_count"] == 1
    assert context["audit_count"] == 1


def test_answer_ui_copilot_question_delegates_to_shared_service(monkeypatch) -> None:
    """
    Validate that Streamlit reuses the shared Copilot narrative service.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_build_operator_answer(**kwargs) -> str:
        """
        Capture shared service arguments without calling an external provider.

        Args:
            kwargs: Copilot context keyword arguments.

        Returns:
            Fixed operator answer.
        """
        captured.update(kwargs)

        return "Grounded operator answer."

    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        fake_build_operator_answer,
    )

    alert_key = "orders|matching-alert"
    result    = answer_ui_copilot_question(
        question="Summarize evidence.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
    )

    assert result["answer"] == "Grounded operator answer."
    assert captured["question"] == "Summarize evidence."
    assert len(captured["evidence_rows"]) == 1
    assert len(captured["audit_rows"]) == 1
    assert result["transport"] == "local"

def test_request_ui_copilot_api_sends_only_alert_key_question_and_report_uri(monkeypatch) -> None:
    """
    Validate Streamlit sends references through the reusable control-plane client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_answer_copilot(self, **kwargs) -> dict[str, object]:
        """
        Capture the client request without network access.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Returns:
            Typed Copilot response payload.
        """
        captured.update(
            {
                "base_url": self.base_url,
                "timeout": self.timeout_seconds,
                **kwargs,
            }
        )

        return {
            "agent_run_id": "11111111-1111-1111-1111-111111111111",
            "alert_key": "orders|matching-alert",
            "answer": "API-grounded answer.",
            "context_source": "alert_report_audit",
            "evidence_count": 2,
            "audit_count": 3,
        }

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        fake_answer_copilot,
    )

    alert_key = "orders|matching-alert"
    result    = request_ui_copilot_api(
        question="Explain the evidence.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        api_base_url="http://api:8000",
        timeout_seconds=10,
    )

    assert result["transport"] == "api"
    assert result["has_report"] is True
    assert captured == {
        "base_url": "http://api:8000",
        "timeout": 10,
        "question": "Explain the evidence.",
        "alert_key": alert_key,
        "report_json_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
        "audit_limit": 10,
    }


def test_answer_ui_copilot_question_falls_back_when_api_transport_is_unavailable(monkeypatch) -> None:
    """
    Validate explicit local fallback without weakening evidence boundaries.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def raise_transport_error(self, **kwargs) -> dict[str, object]:
        """
        Simulate an unavailable control-plane API.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Raises:
            ControlPlaneTransportError: Always.
        """
        raise ControlPlaneTransportError("api unavailable")

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        raise_transport_error,
    )
    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        lambda **kwargs: "Shared local fallback answer.",
    )

    alert_key = "orders|matching-alert"
    result    = answer_ui_copilot_question(
        question="Explain this alert.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
        api_base_url="http://api:8000",
        api_timeout=1,
    )

    assert result["transport"] == "local"
    assert result["answer"] == "Shared local fallback answer."
    assert "ControlPlaneTransportError" in result["fallback_reason"]
    assert result["has_report"] is True


def test_answer_ui_copilot_question_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure contract failures remain visible instead of returning local content.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def raise_response_error(self, **kwargs) -> dict[str, object]:
        """
        Simulate a malformed or rejected API response.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Raises:
            ControlPlaneResponseError: Always.
        """
        raise ControlPlaneResponseError("invalid API response")

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        raise_response_error,
    )
    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        lambda **kwargs: pytest.fail("Response errors must not trigger local fallback."),
    )

    with pytest.raises(ControlPlaneResponseError, match="invalid API response"):
        answer_ui_copilot_question(
            question="Explain this alert.",
            alert={
                "alert_key": "orders|matching-alert",
                "alert_display_id": "DQ-TEST01",
            },
            latest_triage_result=None,
            audit_rows=[],
            api_base_url="http://api:8000",
        )


# --- Defining Blast Radius Tests
def test_request_ui_dbt_blast_radius_uses_shared_bounded_client(monkeypatch) -> None:
    """
    Ensure Streamlit requests impact through the shared API client without local traversal.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal blast-radius control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def get_dbt_blast_radius(self, **kwargs) -> dict[str, object]:
            """
            Capture bounded request values and return sanitized impact.

            Args:
                **kwargs: Blast-radius request values.

            Returns:
                Deterministic impact response.
            """
            captured["request"] = kwargs

            return {
                "table_name": kwargs["table_name"],
                "matched": True,
                "node": {"unique_id": "model.project.fct_orders_daily"},
                "manifest_source": kwargs["manifest_s3_uri"],
                "max_depth": kwargs["max_depth"],
                "max_nodes": kwargs["max_nodes"],
                "max_depth_reached": 1,
                "truncated": False,
                "total_impacted_nodes": 1,
                "impacted_asset_count": 1,
                "impacted_test_count": 0,
                "unresolved_node_count": 0,
                "resource_type_counts": {"model": 1},
                "impacted_assets": [
                    {
                        "unique_id": "model.project.weekly_orders",
                        "resource_type": "model",
                        "name": "weekly_orders",
                        "depth": 1,
                        "lineage_path": [
                            "model.project.fct_orders_daily",
                            "model.project.weekly_orders",
                        ],
                    }
                ],
                "impacted_tests": [],
                "unresolved_nodes": [],
                "summary": "One downstream asset is affected.",
            }

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = request_ui_dbt_blast_radius(
        table_name="dq.fct_orders_daily",
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        max_depth=4,
        max_nodes=80,
        api_base_url="http://api:8000/",
        api_timeout=9,
    )

    assert result["transport"] == "api"
    assert result["table_name"] == "dq.fct_orders_daily"
    assert captured["client"] == {
        "base_url": "http://api:8000",
        "timeout_seconds": 9,
    }
    assert captured["request"] == {
        "table_name": "dq.fct_orders_daily",
        "manifest_s3_uri": "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        "max_depth": 4,
        "max_nodes": 80,
    }


def test_request_ui_dbt_blast_radius_requires_control_plane_api() -> None:
    """
    Ensure the UI does not bypass the shared API boundary when it is unavailable.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="CONTROL_PLANE_API_URL"):
        request_ui_dbt_blast_radius(
            table_name="dq.fct_orders_daily",
            api_base_url="",
        )


def test_matching_blast_radius_result_rejects_stale_table_context() -> None:
    """
    Ensure impact state from a previously selected table is not rendered as current evidence.

    Returns:
        None.
    """
    result = {
        "table_name": "dq.raw_orders",
        "matched": True,
    }

    assert matching_blast_radius_result("dq.fct_orders_daily", result) is None
    assert matching_blast_radius_result("dq.raw_orders", result) == result


def test_build_blast_radius_display_rows_uses_human_readable_path() -> None:
    """
    Ensure dbt unique identifiers become concise operator-facing lineage paths.

    Returns:
        None.
    """
    rows = build_blast_radius_display_rows(
        [
            {
                "unique_id": "model.project.fct_orders_daily",
                "resource_type": "model",
                "name": "fct_orders_daily",
                "relation_name": "dq.fct_orders_daily",
                "depth": 2,
                "lineage_path": [
                    "source.project.raw_orders",
                    "model.project.stg_orders",
                    "model.project.fct_orders_daily",
                ],
            }
        ]
    )

    assert rows == [
        {
            "Depth": 2,
            "Asset": "fct_orders_daily",
            "Type": "model",
            "Relation": "dq.fct_orders_daily",
            "Lineage Path": "raw_orders -> stg_orders -> fct_orders_daily",
        }
    ]


# --- Defining Durable Approval Tests
def test_build_ui_backfill_approval_payload_uses_dispatcher_and_strips_control_fields() -> None:
    """
    Ensure triage actions become bounded approval API payloads without duplicate control fields.

    Returns:
        None.
    """
    payload = build_ui_backfill_approval_payload(
        alert={
            "alert_id": "11111111-1111-1111-1111-111111111111",
            "alert_key": "orders|matching-alert",
        },
        action={
            "action_type": "backfill",
            "reason": "Evidence indicates a missing partition.",
            "target_dag_id": "90_dag_dq_platform_backfill_dispatcher",
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "parameters": {
                "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
                "run_mode": "backfill",
                "run_triage": False,
                "requested_by": "agentic_triage",
                "reason": "Missing partition",
            },
        },
        requested_by="streamlit_operator",
        agent_run_id="22222222-2222-2222-2222-222222222222",
    )

    assert payload["target_dag_id"] == "00_dag_dq_platform_daily_orchestrator"
    assert payload["requested_by"] == "streamlit_operator"
    assert payload["parameters"] == {"run_mode": "backfill", "run_triage": False}
    assert "target_dag_id" not in payload["parameters"]
    assert "requested_by" not in payload["parameters"]


def test_create_ui_backfill_approval_uses_shared_client_without_local_mutation(monkeypatch) -> None:
    """
    Ensure Streamlit creates approval state through FastAPI rather than ClickHouse directly.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal approval-aware control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def create_approval_request(self, **kwargs) -> dict[str, object]:
            """
            Capture approval creation arguments.

            Args:
                **kwargs: Approval request values.

            Returns:
                Pending approval response.
            """
            captured["request"] = kwargs
            return {"request_id": "APR-20260610-A1B2C3D4", "status": "pending"}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = create_ui_backfill_approval_request(
        alert={"alert_key": "orders|matching-alert", "alert_display_id": "DQ-TEST01"},
        action={
            "action_type": "backfill",
            "reason": "Evidence indicates a missing partition.",
            "target_dag_id": "90_dag_dq_platform_backfill_dispatcher",
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "parameters": {"target_dag_id": "00_dag_dq_platform_daily_orchestrator"},
        },
        requested_by="streamlit_operator",
        api_base_url="http://api:8000",
        approval_token="approval-token",
    )

    assert result["status"] == "pending"
    assert captured["client"] == {
        "base_url": "http://api:8000",
        "timeout_seconds": streamlit_app.COPILOT_API_TIMEOUT,
        "approval_token": "approval-token",
    }
    assert captured["request"]["alert_key"] == "orders|matching-alert"


def test_decide_ui_approval_uses_shared_client(monkeypatch) -> None:
    """
    Ensure Streamlit decisions remain API-bound and non-executing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal decision-capable control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """Accept constructor values without network setup."""

        def decide_approval_request(self, **kwargs) -> dict[str, object]:
            """
            Capture one decision call.

            Args:
                **kwargs: Decision values.

            Returns:
                Approved state.
            """
            captured.update(kwargs)
            return {"request_id": kwargs["request_id"], "status": "approved"}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = decide_ui_approval_request(
        request_id="APR-20260610-A1B2C3D4",
        decision="approve",
        decided_by="streamlit_operator",
        comment="Reviewed scope.",
        api_base_url="http://api:8000",
        approval_token="approval-token",
    )

    assert result["status"] == "approved"
    assert captured["decision"] == "approve"
    assert captured["decided_by"] == "streamlit_operator"

def test_summarize_approval_queue_rows_reports_decision_and_execution_counts() -> None:
    """
    Ensure Approval Queue metrics separate human decisions from execution lifecycle.

    Returns:
        None.
    """
    summary = summarize_approval_queue_rows(
        [
            {"status": "pending", "execution_status": "not_started"},
            {"status": "approved", "execution_status": "dispatching"},
            {"status": "approved", "execution_status": "dispatched"},
            {"status": "approved", "execution_status": "failed"},
            {"status": "rejected", "execution_status": "not_started"},
        ]
    )

    assert summary == {
        "pending": 1,
        "approved": 3,
        "active_executions": 2,
        "failed_executions": 1,
    }


def test_load_approval_queue_rows_uses_read_only_shared_api(monkeypatch) -> None:
    """
    Ensure Streamlit Approval Queue reads latest states through ControlPlaneClient.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    rows = [
        {
            "request_id": "APR-20260610-A1B2C3D4",
            "status": "approved",
            "execution_status": "dispatched",
        }
    ]

    class FakeClient:
        """Minimal approval queue client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture read-only client configuration.

            Args:
                **kwargs: Constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def list_approval_requests(self, status, limit: int) -> dict[str, object]:
            """
            Return deterministic approval rows.

            Args:
                status: Optional approval filter.
                limit: Maximum rows.

            Returns:
                Queue response.
            """
            captured["status"] = status
            captured["limit"]  = limit
            return {"status": "success", "row_count": len(rows), "rows": rows}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = load_approval_queue_rows(
        status="approved",
        limit=10,
        api_base_url="http://api:8000",
    )

    assert result == rows
    assert captured["status"] == "approved"
    assert captured["limit"] == 10
    assert "approval_token" not in captured["client"]


def test_summarize_life_evaluation_rows_reports_reliability_states() -> None:
    """
    Ensure Streamlit LIFE cards separate pass, review, fail, and malformed results.

    Returns:
        None.
    """
    summary = summarize_life_evaluation_rows(
        [
            {"eval_status": "pass", "payload_valid": True, "requires_human_approval": False},
            {"eval_status": "review", "payload_valid": True, "requires_human_approval": True},
            {"eval_status": "fail", "payload_valid": True, "requires_human_approval": True},
            {"eval_status": "unknown", "payload_valid": False, "requires_human_approval": False},
        ]
    )

    assert summary == {
        "total": 4,
        "pass": 1,
        "review": 1,
        "fail": 1,
        "malformed": 1,
        "approval_required": 2,
    }


def test_load_life_evaluation_rows_uses_read_only_shared_api(monkeypatch) -> None:
    """
    Ensure Streamlit reads LIFE history through the shared control-plane client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    rows = [
        {
            "run_id": "life-eval-20260807T010203",
            "scenario_id": "missing_latest_day",
            "eval_status": "review",
            "payload_valid": True,
        }
    ]

    class FakeClient:
        """
        Minimal read-only LIFE history client double.
        """

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def list_life_evaluations(self, **kwargs) -> dict[str, object]:
            """
            Capture history filters and return deterministic rows.

            Args:
                **kwargs: LIFE history filter values.

            Returns:
                Sanitized history response.
            """
            captured["request"] = kwargs

            return {"status": "success", "row_count": len(rows), "rows": rows}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)
    load_life_evaluation_rows.clear()

    result = load_life_evaluation_rows(
        eval_status="review",
        lookback_days=14,
        limit=5,
        api_base_url="http://api-life-test:8000",
    )

    assert result == rows
    assert captured["client"] == {
        "base_url": "http://api-life-test:8000",
        "timeout_seconds": streamlit_app.COPILOT_API_TIMEOUT,
    }
    assert captured["request"] == {
        "eval_status": "review",
        "lookback_days": 14,
        "limit": 5,
    }
