####
## Control Plane API Client Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import requests

from apps.common.control_plane import (
    ControlPlaneClient,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)


# --- Defining Test Fixtures
def sample_report_payload(
    alert_key: str = "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table",
    agent_run_id: str = "11111111-1111-1111-1111-111111111111",
) -> dict[str, Any]:
    """
    Build one minimal persisted triage report payload.

    Args:
        alert_key: Stable system alert key.
        agent_run_id: Triage run UUID.

    Returns:
        JSON-serializable TriageReport payload.
    """
    return {
        "agent_run_id": agent_run_id,
        "alert": {
            "alert_key": alert_key,
            "severity": "critical",
            "table_name": "dq.raw_orders",
            "metric": "row_count_positive",
            "dt": "2026-06-10",
        },
        "summary": "The raw partition is empty.",
        "impact": "Downstream reporting may be incomplete.",
        "hypotheses": [],
        "confidence": 0.80,
        "json_report_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
        "markdown_report_s3_uri": "s3://dq-artifacts/agent-reports/report.md",
    }


def sample_public_alert_payload(
    alert_key: str = "orders|matching-alert",
    alert_display_id: str = "DQ-20260610-ABC123",
) -> dict[str, Any]:
    """
    Build one valid public alert response.

    Args:
        alert_key: Stable system alert key.
        alert_display_id: Human-facing Alert Ref.

    Returns:
        JSON-serializable public alert payload.
    """
    return {
        "alert_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "alert_key": alert_key,
        "alert_display_id": alert_display_id,
        "status": "open",
        "alert_type": "dq_failure",
        "severity": "critical",
        "table_name": "dq.raw_orders",
        "metric": "row_count_positive",
        "dt": "2026-06-10",
        "dimension": "table",
        "observed_value": 0.0,
        "expected_value": 1.0,
        "threshold_value": 1.0,
        "details": {"source": "deterministic_check"},
        "report_s3_uri": "s3://dq-artifacts/agent-reports/report.md",
    }


def sample_report_artifact_payload(
    text: str,
    s3_uri: str = "s3://dq-artifacts/agent-reports/report.json",
    max_bytes: int = 200_000,
) -> dict[str, Any]:
    """
    Build one valid bounded report-artifact API response.

    Args:
        text: Artifact text returned by the API.
        s3_uri: Exact report artifact URI.
        max_bytes: Applied response byte bound.

    Returns:
        JSON-serializable artifact response with consistent byte metadata.
    """
    encoded_size = len(text.encode("utf-8"))
    object_key   = s3_uri.split("dq-artifacts/", maxsplit=1)[-1]

    return {
        "status": "success",
        "s3_uri": s3_uri,
        "bucket": "dq-artifacts",
        "key": object_key,
        "media_type": "application/json" if object_key.endswith(".json") else "text/markdown",
        "bytes_read": encoded_size,
        "returned_bytes": encoded_size,
        "max_bytes": max_bytes,
        "truncated": False,
        "text": text,
    }


def sample_approval_payload(status: str = "pending") -> dict[str, Any]:
    """
    Build one valid approval API response payload.

    Args:
        status: Durable approval lifecycle status.

    Returns:
        JSON-serializable approval response.
    """
    return {
        "request_id": "APR-20260610-A1B2C3D4",
        "created_at": "2026-06-23T10:00:00Z",
        "updated_at": "2026-06-23T10:00:00Z",
        "alert_id": None,
        "alert_key": "orders|matching-alert",
        "agent_run_id": None,
        "action_type": "backfill",
        "risk_level": "high",
        "status": status,
        "requested_by": "mario",
        "reason": "Backfill the missing orders partition.",
        "dispatcher_dag_id": "90_dag_dq_platform_backfill_dispatcher",
        "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
        "parameters": {"run_mode": "backfill"},
        "dry_run": False,
        "idempotency_key": "a1b2c3d4",
        "execution_dag_run_id": "",
        "execution_status": "not_started",
        "execution_error": "",
    }


def sample_metadata_asset_payload(
    qualified_name: str = "dq.fct_orders_daily",
) -> dict[str, Any]:
    """
    Build one valid public metadata asset API payload.

    Args:
        qualified_name: Fully qualified warehouse asset identity.

    Returns:
        JSON-serializable public metadata asset.
    """
    database_name, table_name = qualified_name.split(".", maxsplit=1)

    return {
        "qualified_name": qualified_name,
        "database_name": database_name,
        "table_name": table_name,
        "display_name": "Daily Orders Fact",
        "description": "Curated daily order metrics grouped by date, country, and channel.",
        "dataset": "orders",
        "domain": "commerce",
        "data_layer": "mart",
        "technical_owner": "Analytics Engineering",
        "business_owner": "Commerce Analytics",
        "grain": "One row per business date, country, and channel.",
        "refresh_frequency": "daily",
        "sla_time": "01:15",
        "sla_timezone": "Asia/Bangkok",
        "criticality": "critical",
        "sensitivity": "internal",
        "contains_pii": False,
        "certification_status": "certified",
        "lifecycle_status": "active",
        "tags": ["analytics-ready", "orders"],
        "synced_at": "2026-08-07T08:00:00Z",
    }


def sample_blast_radius_payload() -> dict[str, Any]:
    """
    Build one valid bounded dbt blast-radius API response.

    Returns:
        JSON-serializable blast-radius payload.
    """
    return {
        "table_name": "dq.raw_orders",
        "matched": True,
        "node": {
            "unique_id": "source.project.raw.raw_orders",
            "resource_type": "source",
            "name": "raw_orders",
        },
        "manifest_source": "s3://dq-artifacts/dbt/manifest.json",
        "max_depth": 10,
        "max_nodes": 250,
        "max_depth_reached": 1,
        "truncated": False,
        "total_impacted_nodes": 1,
        "impacted_asset_count": 1,
        "impacted_test_count": 0,
        "unresolved_node_count": 0,
        "resource_type_counts": {"model": 1},
        "impacted_assets": [
            {
                "unique_id": "model.project.stg_orders",
                "resource_type": "model",
                "name": "stg_orders",
                "depth": 1,
                "parent_unique_id": "source.project.raw.raw_orders",
                "lineage_path": [
                    "source.project.raw.raw_orders",
                    "model.project.stg_orders",
                ],
            }
        ],
        "impacted_tests": [],
        "unresolved_nodes": [],
        "summary": "dq.raw_orders impacts 1 downstream data asset.",
    }


def sample_incident_history_payload(
    alert_reference: str = "DQ-20260513-764959",
) -> dict[str, Any]:
    """
    Build one valid sanitized incident-history API response.

    Args:
        alert_reference: Exact human-facing Alert Ref used for lookup.

    Returns:
        JSON-serializable bounded incident-history response.
    """
    return {
        "status": "success",
        "alert_reference": alert_reference,
        "lookback_days": 365,
        "limit": 50,
        "row_count": 1,
        "rows": [
            {
                "memory_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "parent_run_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "recorded_at": "2026-08-20T13:37:00Z",
                "memory_type": "investigation_outcome",
                "alert_id": None,
                "alert_key": "orders|dq_failure|2026-05-13|segment_coverage",
                "alert_display_id": alert_reference,
                "outcome_status": "success",
                "specialist_name": "incident_triage_agent",
                "task_type": "triage_alert",
                "summary": "One expected country and channel segment is missing.",
                "confidence": 0.72,
                "top_hypothesis_category": "missing_segment",
                "report_id": "RPT-27BDC120",
                "requires_human_approval": False,
                "evidence_reference_count": 1,
                "evidence_references": [
                    {
                        "evidence_type": "dq_history",
                        "source_tool": "dq_history",
                        "reference": "dq-check:segment_coverage",
                        "summary": "The expected segment is absent.",
                    }
                ],
                "report_s3_uri": "s3://dq-artifacts/agent-reports/report.md",
                "approval_state": "not_required",
                "resolution_reference": "",
            }
        ],
        "summary": f"Found 1 previous investigation(s) for {alert_reference}.",
    }


# --- Defining Request Tests
def test_request_json_classifies_transport_failure(monkeypatch) -> None:
    """
    Ensure timeout and connectivity errors are safe fallback candidates.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        requests,
        "request",
        lambda **kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")),
    )

    client = ControlPlaneClient("http://api:8000")

    with pytest.raises(ControlPlaneTransportError, match="transport failed"):
        client.health()


def test_request_json_classifies_invalid_payload_as_response_failure(monkeypatch) -> None:
    """
    Ensure malformed API payloads do not silently trigger local fallback.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class FakeResponse:
        """Minimal invalid-JSON response double."""

        status_code = 200

        def raise_for_status(self) -> None:
            """Simulate a successful HTTP status."""

        def json(self) -> dict[str, Any]:
            """
            Raise the same error produced by malformed JSON.

            Raises:
                ValueError: Always.
            """
            raise ValueError("invalid json")

    monkeypatch.setattr(requests, "request", lambda **kwargs: FakeResponse())

    client = ControlPlaneClient("http://api:8000")

    with pytest.raises(ControlPlaneResponseError, match="invalid JSON"):
        client.health()


def test_answer_copilot_sends_bounded_reference_payload(monkeypatch) -> None:
    """
    Ensure Copilot requests send identifiers rather than raw evidence blobs.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, Any] = {}

    class FakeResponse:
        """Minimal successful Copilot response double."""

        status_code = 200

        def raise_for_status(self) -> None:
            """Simulate a successful HTTP status."""

        def json(self) -> dict[str, Any]:
            """Return one valid Copilot response."""
            return {
                "agent_run_id": "22222222-2222-2222-2222-222222222222",
                "alert_key": "orders|matching-alert",
                "answer": "Inspect the failed partition first.",
                "context_source": "alert_report_audit",
                "incident_history_count": 2,
            }

    def fake_request(**kwargs) -> FakeResponse:
        """
        Capture the API request.

        Args:
            kwargs: requests.request keyword arguments.

        Returns:
            Successful response double.
        """
        captured.update(kwargs)

        return FakeResponse()

    monkeypatch.setattr(requests, "request", fake_request)

    client  = ControlPlaneClient("http://api:8000", timeout_seconds=7)
    payload = client.answer_copilot(
        question="What should I do?",
        alert_key="orders|matching-alert",
        report_json_s3_uri="s3://dq-artifacts/agent-reports/report.json",
        audit_limit=10,
    )

    assert payload["answer"] == "Inspect the failed partition first."
    assert payload["incident_history_count"] == 2
    assert captured["url"] == "http://api:8000/api/v1/copilot/answer"
    assert captured["timeout"] == 7
    assert captured["json"] == {
        "alert_key": "orders|matching-alert",
        "question": "What should I do?",
        "report_json_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
        "audit_limit": 10,
    }
    assert "evidence" not in captured["json"]


@pytest.mark.parametrize("invalid_count", [-1, 6, True, "2"])
def test_answer_copilot_rejects_invalid_incident_history_count(
    monkeypatch,
    invalid_count: object,
) -> None:
    """
    Ensure clients fail closed when Copilot history metadata violates bounds.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        invalid_count: Invalid response value returned by the API double.

    Returns:
        None.
    """
    client = ControlPlaneClient("http://api:8000")

    monkeypatch.setattr(
        client,
        "request_json",
        lambda *args, **kwargs: {
            "answer": "Bounded answer.",
            "incident_history_count": invalid_count,
        },
    )

    with pytest.raises(
        ControlPlaneResponseError,
        match="invalid incident_history_count",
    ):
        client.answer_copilot(
            question="Was this investigated before?",
            alert_key="DQ-20260504-ABC123",
        )


def test_list_alerts_rejects_missing_alert_collection(monkeypatch) -> None:
    """
    Ensure alert response contract failures remain non-retryable.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = ControlPlaneClient("http://api:8000")
    monkeypatch.setattr(
        client,
        "request_json",
        lambda *args, **kwargs: {"status": "success"},
    )

    with pytest.raises(ControlPlaneResponseError, match="alerts list"):
        client.list_alerts()


def test_list_alerts_validates_bounds_and_rejects_internal_sql(monkeypatch) -> None:
    """
    Ensure alert list clients enforce public counts and internal-field filtering.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    payload = {
        "status": "success",
        "alert_status": "open",
        "dt": None,
        "limit": 5,
        "row_count": 1,
        "alerts": [sample_public_alert_payload()],
        "summary": "Found 1 open alert.",
    }
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)

    result = api_client.list_alerts(status="open", limit=5)

    assert result["row_count"] == 1

    payload["sql"] = "SELECT * FROM dq.alerts"

    with pytest.raises(ControlPlaneResponseError, match="internal query metadata"):
        api_client.list_alerts(status="open", limit=5)


def test_get_daily_summary_validates_identity_totals_and_internal_fields(monkeypatch) -> None:
    """
    Ensure daily-summary clients accept only consistent public aggregates.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    payload = {
        "status": "success",
        "dt": "2026-06-10",
        "check_counts": [{"status": "pass", "count": 9}],
        "alert_counts": [{"severity": "warning", "count": 2}],
        "total_checks": 9,
        "total_open_alerts": 2,
        "duration_ms": 5,
        "summary": "Nine checks and two alerts.",
    }
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)

    result = api_client.get_daily_summary("2026-06-10")

    assert result["total_checks"] == 9

    payload["total_checks"] = 8

    with pytest.raises(ControlPlaneResponseError, match="inconsistent check total"):
        api_client.get_daily_summary("2026-06-10")

    payload["total_checks"] = 9
    payload["sql"] = "SELECT * FROM dq.dq_check_results"

    with pytest.raises(ControlPlaneResponseError, match="internal query metadata"):
        api_client.get_daily_summary("2026-06-10")


def test_get_daily_summary_rejects_invalid_or_mismatched_dates(monkeypatch) -> None:
    """
    Ensure the client validates calendar dates and exact response identity.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        api_client.get_daily_summary("2026-02-30")

    monkeypatch.setattr(
        api_client,
        "request_json",
        lambda *args, **kwargs: {
            "status": "success",
            "dt": "2026-06-11",
            "check_counts": [],
            "alert_counts": [],
            "total_checks": 0,
            "total_open_alerts": 0,
            "duration_ms": 1,
            "summary": "No data.",
        },
    )

    with pytest.raises(ControlPlaneResponseError, match="different target date"):
        api_client.get_daily_summary("2026-06-10")


def test_get_alert_accepts_alert_ref_and_rejects_identity_mismatch(monkeypatch) -> None:
    """
    Ensure alert detail lookup remains bound to the requested Alert Ref.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    payload    = sample_public_alert_payload()
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)

    alert = api_client.get_alert(alert_key="DQ-20260610-ABC123")

    assert alert["alert_key"] == "orders|matching-alert"

    payload["alert_display_id"] = "DQ-20260610-DIFFERENT"

    with pytest.raises(ControlPlaneResponseError, match="different alert key"):
        api_client.get_alert(alert_key="DQ-20260610-ABC123")


def test_get_audit_logs_rejects_raw_prompt_payloads(monkeypatch) -> None:
    """
    Ensure shared clients fail closed when audit internals leak into a response.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    payload = {
        "status": "success",
        "alert_key": "orders|matching-alert",
        "limit": 10,
        "row_count": 1,
        "rows": [
            {
                "action": "collect_dq_history",
                "status": "success",
                "tool_name": "dq_history",
            }
        ],
        "llm_routes": [],
        "latest_llm_route": None,
        "duration_ms": 5,
        "summary": "Found 1 audit event.",
    }
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)

    result = api_client.get_audit_logs("orders|matching-alert", limit=10)

    assert result["row_count"] == 1

    payload["rows"][0]["input_json"] = '{"prompt": "private"}'

    with pytest.raises(ControlPlaneResponseError, match="internal event fields"):
        api_client.get_audit_logs("orders|matching-alert", limit=10)


def test_evidence_clients_validate_identity_and_parsed_metadata(monkeypatch) -> None:
    """
    Ensure DQ and pipeline evidence stay bound to requested context and public objects.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    responses: Iterator[dict[str, Any]] = iter(
        [
            {
                "status": "success",
                "table_name": "dq.raw_orders",
                "dt": "2026-06-10",
                "check_name": None,
                "lookback_days": 14,
                "limit": 20,
                "row_count": 1,
                "rows": [
                    {
                        "table_name": "dq.raw_orders",
                        "dt": "2026-06-10",
                        "check_name": "row_count_positive",
                        "status": "failed",
                        "details": {"partition": "2026-06-10"},
                    }
                ],
                "status_counts": {"failed": 1},
                "summary": "Found 1 DQ result.",
            },
            {
                "status": "success",
                "dt": "2026-06-10",
                "lookback_days": 7,
                "job_name": None,
                "limit": 20,
                "row_count": 1,
                "rows": [
                    {
                        "run_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                        "job_name": "seed_orders",
                        "status": "success",
                        "metadata": {"run_mode": "daily"},
                    }
                ],
                "status_counts": {"success": 1},
                "summary": "Found 1 pipeline run.",
            },
        ]
    )
    api_client = ControlPlaneClient("http://api:8000")
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: next(responses))

    dq_history = api_client.get_dq_history(
        table_name="dq.raw_orders",
        dt="2026-06-10",
        limit=20,
    )
    pipeline_runs = api_client.get_pipeline_runs(dt="2026-06-10", limit=20)

    assert dq_history["rows"][0]["details"]["partition"] == "2026-06-10"
    assert pipeline_runs["rows"][0]["metadata"]["run_mode"] == "daily"


def test_read_report_artifact_rejects_inconsistent_byte_metadata(monkeypatch) -> None:
    """
    Ensure report clients validate exact artifact identity and UTF-8 byte bounds.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    uri        = "s3://dq-artifacts/agent-reports/report.md"
    api_client = ControlPlaneClient("http://api:8000")
    payload    = sample_report_artifact_payload(
        text="# Report",
        s3_uri=uri,
        max_bytes=100,
    )
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)

    artifact = api_client.read_report_artifact(uri, max_bytes=100)

    assert artifact["media_type"] == "text/markdown"

    payload["returned_bytes"] = 99

    with pytest.raises(ControlPlaneResponseError, match="inconsistent UTF-8"):
        api_client.read_report_artifact(uri, max_bytes=100)


# --- Defining Approval Client Tests
def test_create_approval_request_sends_token_and_bounded_payload(monkeypatch) -> None:
    """
    Ensure approval creation sends authorization without exposing it in the JSON body.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, Any] = {}
    client = ControlPlaneClient(
        "http://api:8000",
        approval_token="local-approval-token",
    )

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture one approval API call and return a valid response.

        Args:
            method: HTTP method.
            path: API path.
            **kwargs: Request options.

        Returns:
            Valid approval response.
        """
        captured.update({"method": method, "path": path, **kwargs})
        return sample_approval_payload()

    monkeypatch.setattr(client, "request_json", fake_request_json)

    payload = client.create_approval_request(
        requested_by="mario",
        reason="Backfill the missing orders partition.",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        start_date="2026-06-10",
        end_date="2026-06-10",
        parameters={"run_mode": "backfill"},
        alert_key="orders|matching-alert",
    )

    assert payload["status"] == "pending"
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/approvals/requests"
    assert captured["headers"] == {"X-Control-Plane-Token": "local-approval-token"}
    assert "token" not in json.dumps(captured["json_body"]).lower()


def test_approval_mutation_requires_local_token_before_http_call(monkeypatch) -> None:
    """
    Ensure missing local authorization fails before any mutation request is sent.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = ControlPlaneClient("http://api:8000", approval_token="")
    monkeypatch.setattr(
        client,
        "request_json",
        lambda *args, **kwargs: pytest.fail("HTTP must not run without an approval token."),
    )

    with pytest.raises(ControlPlaneResponseError, match="CONTROL_PLANE_APPROVAL_TOKEN"):
        client.decide_approval_request(
            request_id="APR-20260610-A1B2C3D4",
            decision="approve",
            decided_by="mario",
        )


def test_decide_and_list_approval_requests_validate_contracts(monkeypatch) -> None:
    """
    Ensure decision and queue methods enforce status and row response contracts.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    responses: Iterator[dict[str, Any]] = iter(
        [
            sample_approval_payload(status="approved"),
            {"status": "success", "row_count": 1, "rows": [sample_approval_payload()]},
        ]
    )
    client = ControlPlaneClient("http://api:8000", approval_token="token")
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: next(responses))

    approved = client.decide_approval_request(
        request_id="APR-20260610-A1B2C3D4",
        decision="approve",
        decided_by="mario",
        comment="Reviewed exact scope.",
    )
    queue = client.list_approval_requests(status="pending", limit=10)

    assert approved["status"] == "approved"
    assert queue["row_count"] == 1

    with pytest.raises(ControlPlaneResponseError, match="invalid status"):
        client.validate_approval_payload(sample_approval_payload(status="executed"))


# --- Defining Triage Tests
def test_run_triage_report_reconstructs_persisted_report(monkeypatch) -> None:
    """
    Ensure Discord can reconstruct a typed report through the shared API boundary.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    alert_key   = "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table"
    agent_run_id = "11111111-1111-1111-1111-111111111111"
    responses: Iterator[dict[str, Any]] = iter(
        [
            {
                "status": "success",
                "agent_run_id": agent_run_id,
                "alert_key": alert_key,
                "json_report_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
            },
            {
                **sample_report_artifact_payload(
                    text=json.dumps(sample_report_payload(alert_key, agent_run_id)),
                )
            },
        ]
    )

    client = ControlPlaneClient("http://api:8000")
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: next(responses))

    report = client.run_triage_report(
        alert_key=alert_key,
        confidence_threshold=0.70,
        max_evidence_iterations=2,
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
    )

    assert report.alert.alert_key == alert_key
    assert str(report.agent_run_id) == agent_run_id
    assert report.confidence == 0.80


def test_run_triage_report_rejects_identity_mismatch(monkeypatch) -> None:
    """
    Ensure a report from another alert cannot contaminate the requested triage.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    requested_key = "orders|requested-alert"
    responses: Iterator[dict[str, Any]] = iter(
        [
            {
                "status": "success",
                "agent_run_id": "11111111-1111-1111-1111-111111111111",
                "alert_key": requested_key,
                "json_report_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
            },
            {
                **sample_report_artifact_payload(
                    text=json.dumps(sample_report_payload(alert_key="orders|different-alert")),
                )
            },
        ]
    )

    client = ControlPlaneClient("http://api:8000")
    monkeypatch.setattr(client, "request_json", lambda *args, **kwargs: next(responses))

    with pytest.raises(ControlPlaneResponseError, match="alert_key does not match"):
        client.run_triage_report(
            alert_key=requested_key,
            confidence_threshold=0.70,
            max_evidence_iterations=2,
            manifest_s3_uri="",
        )

def test_get_approval_request_validates_execution_lifecycle(monkeypatch) -> None:
    """
    Ensure approval detail reads include a valid execution state contract.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = ControlPlaneClient("http://api:8000")
    monkeypatch.setattr(
        client,
        "request_json",
        lambda *args, **kwargs: sample_approval_payload(status="approved"),
    )

    payload = client.get_approval_request("APR-20260610-A1B2C3D4")

    assert payload["execution_status"] == "not_started"

    invalid = sample_approval_payload(status="approved")
    invalid["execution_status"] = "unknown"

    with pytest.raises(ControlPlaneResponseError, match="invalid execution_status"):
        client.validate_approval_payload(invalid)


def test_list_life_evaluations_uses_bounded_filters_and_validates_rows(monkeypatch) -> None:
    """
    Ensure LIFE history reads use the shared API and reject raw audit payloads.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    api_client = ControlPlaneClient("http://api:8000")

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture the request and return one sanitized LIFE summary.

        Args:
            method: HTTP method.
            path: API route path.
            **kwargs: Request parameters.

        Returns:
            LIFE history response payload.
        """
        captured.update({"method": method, "path": path, **kwargs})

        return {
            "status": "success",
            "row_count": 1,
            "rows": [
                {
                    "run_id": "life-eval-20260807T010203",
                    "scenario_id": "missing_latest_day",
                    "eval_status": "review",
                    "payload_valid": True,
                }
            ],
        }

    monkeypatch.setattr(api_client, "request_json", fake_request_json)

    payload = api_client.list_life_evaluations(
        eval_status="REVIEW",
        scenario_id="MISSING_LATEST_DAY",
        lookback_days=999,
        limit=999,
    )

    assert payload["row_count"] == 1
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/evaluations/life"
    assert captured["params"] == {
        "eval_status": "review",
        "scenario_id": "missing_latest_day",
        "lookback_days": 365,
        "limit": 100,
    }

    monkeypatch.setattr(
        api_client,
        "request_json",
        lambda *args, **kwargs: {
            "rows": [
                {
                    "eval_status": "pass",
                    "output_json": '{"private":"payload"}',
                }
            ]
        },
    )

    with pytest.raises(ControlPlaneResponseError, match="must not expose raw audit payloads"):
        api_client.list_life_evaluations()


def test_get_incident_history_uses_exact_bounded_read_contract(monkeypatch) -> None:
    """
    Ensure incident-history reads preserve identity and enforce hard request bounds.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    api_client                   = ControlPlaneClient("http://api:8000")

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture the request and return one sanitized previous investigation.

        Args:
            method: HTTP method.
            path: API route path.
            **kwargs: Request query parameters.

        Returns:
            Valid bounded incident-history response.
        """
        captured.update({"method": method, "path": path, **kwargs})

        return sample_incident_history_payload()

    monkeypatch.setattr(api_client, "request_json", fake_request_json)

    payload = api_client.get_incident_history(
        alert_reference=" DQ-20260513-764959 ",
        lookback_days=999,
        limit=999,
    )

    assert payload["row_count"] == 1
    assert payload["rows"][0]["report_id"] == "RPT-27BDC120"
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/incidents/history"
    assert captured["params"] == {
        "alert_reference": "DQ-20260513-764959",
        "lookback_days": 365,
        "limit": 50,
    }

    with pytest.raises(ValueError, match="single-line alert reference"):
        api_client.get_incident_history("DQ-ONE\nDQ-TWO")


@pytest.mark.parametrize(
    ("payload_mutation", "row_mutation", "expected_message"),
    [
        ({"alert_reference": "DQ-WRONG"}, {}, "different alert reference"),
        ({"lookback_days": 30}, {}, "different lookback window"),
        ({"limit": 20}, {}, "different hard limit"),
        ({"row_count": 2}, {}, "inconsistent or unbounded row_count"),
        ({}, {"alert_display_id": "DQ-WRONG"}, "record for a different alert"),
        ({}, {"decision_facts": {"raw": True}}, "forbidden internal memory fields"),
        ({}, {"unexpected_context": "private"}, "outside the public contract"),
        ({}, {"evidence_reference_count": 2}, "inconsistent evidence references"),
    ],
)
def test_get_incident_history_rejects_mismatched_or_private_payloads(
    monkeypatch,
    payload_mutation: dict[str, Any],
    row_mutation: dict[str, Any],
    expected_message: str,
) -> None:
    """
    Ensure clients fail closed when history identity, bounds, or privacy drift.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        payload_mutation: Invalid response-level values.
        row_mutation: Invalid record-level values.
        expected_message: Expected contract failure fragment.

    Returns:
        None.
    """
    payload = sample_incident_history_payload()
    payload.update(payload_mutation)
    payload["rows"][0].update(row_mutation)
    api_client = ControlPlaneClient("http://api:8000")

    monkeypatch.setattr(
        api_client,
        "request_json",
        lambda *args, **kwargs: payload,
    )

    with pytest.raises(ControlPlaneResponseError, match=expected_message):
        api_client.get_incident_history(
            alert_reference="DQ-20260513-764959",
            lookback_days=365,
            limit=50,
        )


def test_list_metadata_assets_preserves_filters_bounds_and_public_contract(monkeypatch) -> None:
    """
    Ensure shared clients request bounded metadata and reject internal registry fields.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    api_client = ControlPlaneClient("http://api:8000")

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture metadata discovery request arguments.

        Args:
            method: HTTP method.
            path: API route path.
            **kwargs: Request query parameters.

        Returns:
            Valid bounded metadata response.
        """
        captured.update({"method": method, "path": path, **kwargs})

        return {
            "status": "success",
            "query": "orders",
            "filters": {"data_layer": "mart"},
            "limit": 100,
            "row_count": 1,
            "assets": [sample_metadata_asset_payload()],
            "summary": "Found 1 trusted metadata asset(s).",
        }

    monkeypatch.setattr(api_client, "request_json", fake_request_json)

    payload = api_client.list_metadata_assets(
        query=" orders ",
        domain=" COMMERCE ",
        data_layer=" MART ",
        certification_status=" CERTIFIED ",
        lifecycle_status=" ACTIVE ",
        limit=999,
    )

    assert payload["row_count"] == 1
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/metadata/assets"
    assert captured["params"] == {
        "query": "orders",
        "domain": "commerce",
        "data_layer": "mart",
        "certification_status": "certified",
        "lifecycle_status": "active",
        "limit": 100,
    }

    invalid_payload = dict(payload)
    invalid_payload["assets"] = [
        {
            **sample_metadata_asset_payload(),
            "config_sha256": "must-not-leak",
        }
    ]
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: invalid_payload)

    with pytest.raises(ControlPlaneResponseError, match="internal registry fields"):
        api_client.list_metadata_assets()


def test_get_metadata_asset_requires_matching_complete_public_asset(monkeypatch) -> None:
    """
    Ensure exact metadata lookup cannot silently return another or incomplete asset.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture exact metadata asset requests.

        Args:
            method: HTTP method.
            path: API route path.
            **kwargs: Optional request arguments.

        Returns:
            Valid exact metadata asset.
        """
        captured.update({"method": method, "path": path, **kwargs})

        return sample_metadata_asset_payload("dq.raw_orders")

    monkeypatch.setattr(api_client, "request_json", fake_request_json)

    asset = api_client.get_metadata_asset("dq.raw_orders")

    assert asset["qualified_name"] == "dq.raw_orders"
    assert captured["path"] == "/api/v1/metadata/assets/dq.raw_orders"

    monkeypatch.setattr(
        api_client,
        "request_json",
        lambda *args, **kwargs: sample_metadata_asset_payload("dq.stg_orders"),
    )

    with pytest.raises(ControlPlaneResponseError, match="different qualified asset"):
        api_client.get_metadata_asset("dq.raw_orders")

    with pytest.raises(ValueError, match="database.table format"):
        api_client.get_metadata_asset("dq/raw_orders")


def test_get_dbt_blast_radius_enforces_request_and_response_bounds(monkeypatch) -> None:
    """
    Ensure shared clients preserve table identity and hard traversal limits.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    api_client = ControlPlaneClient("http://api:8000")

    def fake_request_json(method: str, path: str, **kwargs) -> dict[str, Any]:
        """
        Capture the bounded blast-radius request.

        Args:
            method: HTTP method.
            path: API route path.
            **kwargs: Request parameters.

        Returns:
            Valid blast-radius payload.
        """
        captured.update({"method": method, "path": path, **kwargs})

        return sample_blast_radius_payload()

    monkeypatch.setattr(api_client, "request_json", fake_request_json)

    payload = api_client.get_dbt_blast_radius(
        table_name="dq.raw_orders",
        manifest_s3_uri="s3://dq-artifacts/dbt/manifest.json",
        max_depth=999,
        max_nodes=999,
    )

    assert payload["impacted_asset_count"] == 1
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/lineage/dbt/blast-radius"
    assert captured["params"] == {
        "table_name": "dq.raw_orders",
        "manifest_s3_uri": "s3://dq-artifacts/dbt/manifest.json",
        "max_depth": 10,
        "max_nodes": 250,
    }


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ({"table_name": "dq.other_table"}, "different table"),
        ({"total_impacted_nodes": 2}, "inconsistent total"),
        (
            {
                "impacted_assets": [
                    {
                        "unique_id": "model.project.stg_orders",
                        "resource_type": "model",
                        "name": "stg_orders",
                        "depth": 1,
                        "compiled_code": "SELECT * FROM secret_table",
                    }
                ]
            },
            "must not expose raw or compiled",
        ),
    ],
)
def test_get_dbt_blast_radius_rejects_contract_violations(
    monkeypatch,
    mutation: dict[str, Any],
    expected_message: str,
) -> None:
    """
    Ensure UI adapters cannot consume mismatched, inconsistent, or raw dbt data.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        mutation: Invalid fields merged into a valid API payload.
        expected_message: Expected contract failure fragment.

    Returns:
        None.
    """
    payload = sample_blast_radius_payload()
    payload.update(mutation)
    api_client = ControlPlaneClient("http://api:8000")

    monkeypatch.setattr(
        api_client,
        "request_json",
        lambda *args, **kwargs: payload,
    )

    with pytest.raises(ControlPlaneResponseError, match=expected_message):
        api_client.get_dbt_blast_radius(
            table_name="dq.raw_orders",
            max_depth=10,
            max_nodes=250,
        )


# --- Defining Checkpoint Client Contract Tests
def sample_checkpoint_history_payload() -> dict[str, Any]:
    """
    Build one valid sanitized checkpoint-history API payload.

    Returns:
        Public checkpoint history without persisted graph values.
    """
    selected = {
        "checkpoint_id": "checkpoint-001",
        "created_at": "2026-08-28T04:23:30Z",
        "step": 8,
        "source": "loop",
        "next_nodes": ["store_report"],
        "is_complete": False,
    }

    return {
        "status": "success",
        "checkpoint_namespace": "manual__triage_source",
        "thread_id": "manual__triage_source:abcdef0123456789",
        "history_count": 1,
        "matching_checkpoint_count": 1,
        "selected_checkpoint": dict(selected),
        "history": [dict(selected)],
        "raw_state_exposed": False,
        "read_only": True,
        "summary": "One replay candidate is available.",
    }


def sample_checkpoint_replay_preview_payload() -> dict[str, Any]:
    """
    Build one valid non-executing replay-preview API payload.

    Returns:
        Public Airflow-only replay preview.
    """
    return {
        "status": "preview",
        "dag_id": "40_dag_dq_orders_triage_agent",
        "action": "replay",
        "alert_reference": "orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        "checkpoint_namespace": "manual__triage_source",
        "source_thread_id": "manual__triage_source:abcdef0123456789",
        "source_checkpoint_id": "checkpoint-001",
        "source_next_nodes": ["store_report"],
        "replay_request_id": "replay-0123456789abcdef",
        "replay_thread_id": "manual__triage_source:historical-replay:fedcba9876543210",
        "dag_run_conf": {
            "checkpoint_action": "triage",
            "run_triage": True,
            "alert_id": "",
            "alert_key": "orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
            "checkpoint_mode": "sqlite",
            "checkpoint_namespace": "manual__triage_source",
            "checkpoint_resume": False,
            "checkpoint_replay_id": "checkpoint-001",
            "checkpoint_replay_request_id": "replay-0123456789abcdef",
        },
        "execution_boundary": "airflow_dag_40",
        "operator_confirmation_required": True,
        "airflow_triggered": False,
        "side_effects_executed": False,
        "raw_state_exposed": False,
        "summary": "Preview only. No Airflow DagRun was triggered.",
    }


def test_checkpoint_history_client_preserves_bounds_and_rejects_raw_state(monkeypatch) -> None:
    """
    Ensure operator clients accept sanitized history and reject graph-state leakage.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, Any] = {}
    api_client = ControlPlaneClient("http://api:8000")

    def fake_request(method, path, **kwargs):
        """Capture request details and return valid checkpoint history."""
        captured.update({"method": method, "path": path, **kwargs})
        return sample_checkpoint_history_payload()

    monkeypatch.setattr(api_client, "request_json", fake_request)
    payload = api_client.get_checkpoint_history(
        checkpoint_namespace="manual__triage_source",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        history_limit=500,
        history_next_node="store_report",
    )

    assert payload["selected_checkpoint"]["checkpoint_id"] == "checkpoint-001"
    assert captured["params"]["history_limit"] == 100

    leaked = sample_checkpoint_history_payload()
    leaked["history"][0]["values"] = {"messages": ["hidden"]}
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: leaked)

    with pytest.raises(ControlPlaneResponseError, match="sanitized snapshot contract"):
        api_client.get_checkpoint_history(
            checkpoint_namespace="manual__triage_source",
            alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        )


def test_checkpoint_replay_preview_client_rejects_execution_claims(monkeypatch) -> None:
    """
    Ensure UI adapters cannot consume a preview that claims Airflow already ran.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    api_client = ControlPlaneClient("http://api:8000")
    payload    = sample_checkpoint_replay_preview_payload()

    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: payload)
    result = api_client.preview_checkpoint_replay(
        checkpoint_namespace="manual__triage_source",
        checkpoint_id="checkpoint-001",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
    )

    assert result["airflow_triggered"] is False
    assert result["dag_run_conf"]["checkpoint_replay_id"] == "checkpoint-001"

    executed = sample_checkpoint_replay_preview_payload()
    executed["airflow_triggered"] = True
    monkeypatch.setattr(api_client, "request_json", lambda *args, **kwargs: executed)

    with pytest.raises(ControlPlaneResponseError, match="incorrectly reports execution"):
        api_client.preview_checkpoint_replay(
            checkpoint_namespace="manual__triage_source",
            checkpoint_id="checkpoint-001",
            alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        )

