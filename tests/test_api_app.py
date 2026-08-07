####
## FastAPI App Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.common.llm_observability import enrich_audit_rows
from agent.tools.approval_queue import ApprovalRequest, normalize_backfill_parameters


# --- Creating Test Client
client = TestClient(api_main.app)


# --- Defining Test Fakes
class DummyAlert:
    """
    Minimal alert object used by API tests.

    Attributes:
        payload: Serialized alert payload.
    """

    def __init__(self, payload: dict) -> None:
        """
        Store a serialized alert payload.

        Args:
            payload: Alert dictionary returned by model_dump.

        Returns:
            None.
        """
        self.payload = payload

        for field_name, value in payload.items():
            setattr(self, field_name, value)

        self.alert_id          = getattr(self, "alert_id", None)
        self.alert_key         = getattr(self, "alert_key", "")
        self.alert_display_id  = getattr(self, "alert_display_id", "DQ-00000000-ABC123")
        self.report_s3_uri     = getattr(self, "report_s3_uri", "")

    def model_dump(self, mode: str = "json") -> dict:
        """
        Return the stored payload in Pydantic-compatible shape.

        Args:
            mode: Serialization mode requested by the caller.

        Returns:
            Alert payload dictionary.
        """
        return self.payload


class DummyAction:
    """
    Minimal approval action object used by API tests.
    """

    def model_dump(self, mode: str = "json") -> dict:
        """
        Return a compact approval action payload.

        Args:
            mode: Serialization mode requested by the caller.

        Returns:
            Approval action dictionary.
        """
        return {"action_type": "trigger_backfill", "requires_approval": True}


class DummyReport:
    """
    Minimal triage report object used by API tests.
    """

    def __init__(self) -> None:
        """
        Initialize report fields consumed by compact_triage_response.

        Returns:
            None.
        """
        self.agent_run_id             = "11111111-1111-1111-1111-111111111111"
        self.alert                    = type(
            "Alert",
            (),
            {
                "alert_key": "orders|test",
                "alert_display_id": "DQ-00000000-ABC123",
                "severity": "critical",
            },
        )()
        self.confidence               = 0.84
        self.top_hypothesis           = type("Hypothesis", (), {"title": "Missing partition"})()
        self.markdown_report_s3_uri   = "s3://dq-artifacts/report.md"
        self.json_report_s3_uri       = "s3://dq-artifacts/report.json"
        self.approval_gated_actions   = [DummyAction()]


def build_api_approval(status: str = "pending") -> ApprovalRequest:
    """
    Build one approval state returned by mocked lifecycle services.

    Args:
        status: Approval lifecycle status.

    Returns:
        ApprovalRequest model compatible with API serialization.
    """
    now = datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc)

    return ApprovalRequest(
        request_id="APR-20260610-A1B2C3D4",
        created_at=now,
        updated_at=now,
        alert_key="orders|dq_failure|2026-06-10",
        action_type="backfill",
        risk_level="high",
        status=status,
        requested_by="mario",
        reason="Backfill the missing orders partition after triage review.",
        dispatcher_dag_id="90_dag_dq_platform_backfill_dispatcher",
        target_dag_id="00_dag_dq_platform_daily_orchestrator",
        start_date=date(2026, 6, 10),
        end_date=date(2026, 6, 10),
        parameters=normalize_backfill_parameters(),
        dry_run=False,
        idempotency_key="a1b2c3d4",
    )


def build_api_metadata_asset(qualified_name: str = "dq.fct_orders_daily") -> dict:
    """
    Build one valid public metadata asset response.

    Args:
        qualified_name: Fully qualified warehouse asset identity.

    Returns:
        Metadata payload compatible with MetadataAssetResponse.
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


# --- Defining Tests
def test_health_endpoint_returns_api_metadata() -> None:
    """
    Validate the API health endpoint.

    Returns:
        None.
    """
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "agentic-dq-api"


def test_alert_list_endpoint_uses_existing_alert_tool(monkeypatch) -> None:
    """
    Validate alert listing endpoint delegates to the alert tool.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fake_list_alerts(status: str, dt: str | None, limit: int) -> dict:
        """
        Return a deterministic alert list payload.

        Args:
            status: Alert status filter.
            dt: Optional business date.
            limit: Maximum rows.

        Returns:
            Alert lookup payload.
        """
        return {
            "status": "success",
            "alerts": [{"alert_key": "orders|test", "status": status, "dt": dt}],
            "row_count": 1,
            "limit": limit,
        }

    monkeypatch.setattr(api_main, "list_alerts", fake_list_alerts)

    response = client.get("/api/v1/alerts", params={"status": "open", "dt": "2026-06-10", "limit": 5})

    assert response.status_code == 200
    assert response.json()["row_count"] == 1
    assert response.json()["alerts"][0]["alert_key"] == "orders|test"


def test_alert_detail_endpoint_returns_serialized_alert(monkeypatch) -> None:
    """
    Validate alert detail endpoint serializes the loaded alert model.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fake_load_alert(alert_id: str | None = None, alert_key: str | None = None) -> DummyAlert:
        """
        Return a deterministic alert object.

        Args:
            alert_id: Optional alert UUID.
            alert_key: Optional stable alert key.

        Returns:
            DummyAlert object.
        """
        return DummyAlert({"alert_id": alert_id, "alert_key": alert_key, "severity": "critical"})

    monkeypatch.setattr(api_main, "load_alert", fake_load_alert)

    response = client.get("/api/v1/alerts/detail", params={"alert_key": "orders|test"})

    assert response.status_code == 200
    assert response.json()["alert_key"] == "orders|test"
    assert response.json()["severity"] == "critical"


def test_metadata_asset_search_endpoint_is_typed_and_filters_internal_fields(monkeypatch) -> None:
    """
    Validate metadata discovery delegates bounded filters and strips registry internals.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    asset = build_api_metadata_asset()
    asset["config_sha256"] = "internal-hash"
    asset["source_config_path"] = "configs/metadata/orders.yml"

    def fake_search_metadata_assets(**kwargs) -> dict:
        """
        Capture API tool arguments and return one metadata result.

        Args:
            **kwargs: Metadata search filters.

        Returns:
            Metadata list payload with internal fields for schema filtering.
        """
        captured.update(kwargs)

        return {
            "status": "success",
            "query": "orders",
            "filters": {"data_layer": "mart"},
            "limit": 5,
            "row_count": 1,
            "assets": [asset],
            "summary": "Found 1 trusted metadata asset(s).",
        }

    monkeypatch.setattr(
        api_main,
        "search_metadata_assets",
        fake_search_metadata_assets,
    )

    response = client.get(
        "/api/v1/metadata/assets",
        params={
            "query": "orders",
            "data_layer": "mart",
            "certification_status": "certified",
            "limit": 5,
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["row_count"] == 1
    assert payload["assets"][0]["qualified_name"] == "dq.fct_orders_daily"
    assert "config_sha256" not in payload["assets"][0]
    assert "source_config_path" not in payload["assets"][0]
    assert captured == {
        "query": "orders",
        "domain": None,
        "data_layer": "mart",
        "certification_status": "certified",
        "lifecycle_status": None,
        "limit": 5,
    }


def test_metadata_asset_detail_endpoint_returns_404_for_unknown_asset(monkeypatch) -> None:
    """
    Ensure exact metadata lookup preserves a clear not-found API contract.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(
        api_main,
        "get_metadata_asset",
        lambda **_: (_ for _ in ()).throw(LookupError("Metadata asset not found: dq.missing")),
    )

    response = client.get("/api/v1/metadata/assets/dq.missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Metadata asset not found: dq.missing"


def test_dbt_blast_radius_endpoint_returns_typed_bounded_impact(monkeypatch) -> None:
    """
    Validate the API delegates bounded lineage traversal and filters its schema.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_fetch_dbt_blast_radius(**kwargs) -> dict:
        """
        Return deterministic transitive impact while capturing endpoint arguments.

        Args:
            kwargs: Blast-radius tool keyword arguments.

        Returns:
            Typed blast-radius compatible payload.
        """
        captured.update(kwargs)

        return {
            "table_name": kwargs["table_name"],
            "matched": True,
            "node": {
                "unique_id": "source.project.raw.raw_orders",
                "resource_type": "source",
                "name": "raw_orders",
                "schema": "dq",
            },
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
                    "unique_id": "model.project.stg_orders",
                    "resource_type": "model",
                    "name": "stg_orders",
                    "schema": "dq",
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

    monkeypatch.setattr(
        api_main,
        "fetch_dbt_blast_radius",
        fake_fetch_dbt_blast_radius,
    )

    response = client.get(
        "/api/v1/lineage/dbt/blast-radius",
        params={
            "table_name": "dq.raw_orders",
            "manifest_s3_uri": "s3://dq-artifacts/dbt/manifest.json",
            "max_depth": 4,
            "max_nodes": 25,
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["impacted_asset_count"] == 1
    assert payload["impacted_assets"][0]["depth"] == 1
    assert payload["impacted_assets"][0]["schema"] == "dq"
    assert "raw_code" not in payload["impacted_assets"][0]
    assert captured["max_depth"] == 4
    assert captured["max_nodes"] == 25


def test_dbt_blast_radius_endpoint_rejects_unbounded_query() -> None:
    """
    Validate FastAPI rejects traversal settings above the public contract.

    Returns:
        None.
    """
    response = client.get(
        "/api/v1/lineage/dbt/blast-radius",
        params={
            "table_name": "dq.raw_orders",
            "max_depth": 11,
            "max_nodes": 251,
        },
    )

    assert response.status_code == 422


def test_report_read_endpoint_uses_bounded_reader(monkeypatch) -> None:
    """
    Validate report read endpoint delegates to the bounded artifact reader.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fake_read_report_artifact(s3_uri: str, max_bytes: int) -> dict:
        """
        Return a deterministic bounded artifact payload.

        Args:
            s3_uri: Report artifact URI.
            max_bytes: Maximum bytes requested.

        Returns:
            Report read payload.
        """
        return {"status": "success", "s3_uri": s3_uri, "max_bytes": max_bytes, "text": "# Report"}

    monkeypatch.setattr(api_main, "read_report_artifact", fake_read_report_artifact)

    response = client.get("/api/v1/reports/read", params={"s3_uri": "s3://dq-artifacts/report.md", "max_bytes": 50})

    assert response.status_code == 200
    assert response.json()["text"] == "# Report"
    assert response.json()["max_bytes"] == 50


def test_triage_run_endpoint_returns_report_uris(monkeypatch) -> None:
    """
    Validate triage endpoint returns a compact response from run_triage.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def fake_run_triage(**kwargs) -> DummyReport:
        """
        Return a deterministic triage report.

        Args:
            kwargs: Triage keyword arguments.

        Returns:
            DummyReport object.
        """
        return DummyReport()

    monkeypatch.setattr(api_main, "run_triage", fake_run_triage)

    response = client.post(
        "/api/v1/triage/run",
        json={"alert_key": "orders|test", "confidence_threshold": 0.7, "max_evidence_iterations": 1},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["markdown_report_s3_uri"] == "s3://dq-artifacts/report.md"
    assert response.json()["approval_gated_actions"][0]["action_type"] == "trigger_backfill"


def test_triage_run_endpoint_requires_alert_identifier() -> None:
    """
    Validate triage endpoint rejects empty alert identifiers.

    Returns:
        None.
    """
    response = client.post("/api/v1/triage/run", json={"confidence_threshold": 0.7})

    assert response.status_code == 422


def test_create_approval_request_endpoint_returns_idempotency_state(monkeypatch) -> None:
    """
    Validate approval creation delegates to the lifecycle service and exposes created_new.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approval = build_api_approval()

    monkeypatch.setenv("CONTROL_PLANE_APPROVAL_TOKEN", "test-approval-token")
    monkeypatch.setattr(api_main, "create_approval_request", lambda request: (approval, True))

    response = client.post(
        "/api/v1/approvals/requests",
        json={
            "alert_key": approval.alert_key,
            "requested_by": "mario",
            "reason": approval.reason,
            "target_dag_id": approval.target_dag_id,
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "parameters": {"run_triage": False},
        },
        headers={"X-Control-Plane-Token": "test-approval-token"},
    )

    assert response.status_code == 200
    assert response.json()["request_id"] == approval.request_id
    assert response.json()["status"] == "pending"
    assert response.json()["created_new"] is True


def test_list_and_get_approval_request_endpoints(monkeypatch) -> None:
    """
    Validate approval queue list and detail endpoints use latest-state service methods.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approval = build_api_approval()

    monkeypatch.setattr(api_main, "build_clickhouse_client", lambda: object())
    monkeypatch.setattr(
        api_main,
        "list_approval_requests",
        lambda client, status, limit: [approval],
    )
    monkeypatch.setattr(
        api_main,
        "get_approval_request",
        lambda client, request_id: approval if request_id == approval.request_id else None,
    )

    list_response = client.get("/api/v1/approvals/requests", params={"status": "pending", "limit": 10})
    get_response  = client.get(f"/api/v1/approvals/requests/{approval.request_id}")
    missing       = client.get("/api/v1/approvals/requests/APR-MISSING")

    assert list_response.status_code == 200
    assert list_response.json()["row_count"] == 1
    assert list_response.json()["rows"][0]["request_id"] == approval.request_id
    assert get_response.status_code == 200
    assert missing.status_code == 404


def test_decide_approval_request_endpoint_returns_transition_state(monkeypatch) -> None:
    """
    Validate explicit human decisions return the latest state and transition indicator.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approved = build_api_approval(status="approved").model_copy(
        update={
            "decided_by": "reviewer",
            "decided_at": datetime(2026, 6, 23, 10, 5, tzinfo=timezone.utc),
        }
    )

    monkeypatch.setenv("CONTROL_PLANE_APPROVAL_TOKEN", "test-approval-token")
    monkeypatch.setattr(
        api_main,
        "decide_approval_request",
        lambda **kwargs: (approved, True),
    )

    response = client.post(
        f"/api/v1/approvals/requests/{approved.request_id}/decision",
        json={
            "decision": "approve",
            "decided_by": "reviewer",
            "comment": "Exact action scope reviewed.",
        },
        headers={"X-Control-Plane-Token": "test-approval-token"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["state_changed"] is True
    assert response.json()["decided_by"] == "reviewer"


def test_approval_mutations_fail_closed_without_valid_token(monkeypatch) -> None:
    """
    Ensure callers cannot create or decide approvals without server-side authorization.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    approval = build_api_approval()
    payload  = {
        "alert_key": approval.alert_key,
        "requested_by": "mario",
        "reason": approval.reason,
        "target_dag_id": approval.target_dag_id,
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
    }

    monkeypatch.delenv("CONTROL_PLANE_APPROVAL_TOKEN", raising=False)

    disabled = client.post("/api/v1/approvals/requests", json=payload)

    monkeypatch.setenv("CONTROL_PLANE_APPROVAL_TOKEN", "expected-token")

    unauthorized = client.post(
        "/api/v1/approvals/requests",
        json=payload,
        headers={"X-Control-Plane-Token": "wrong-token"},
    )

    assert disabled.status_code == 503
    assert unauthorized.status_code == 401

def test_normalize_report_json_uri_converts_markdown_sibling() -> None:
    """
    Validate deterministic report.md to report.json normalization.

    Returns:
        None.
    """
    uri = api_main.normalize_report_json_s3_uri(
        "s3://dq-artifacts/agent-reports/report.md"
    )

    assert uri == "s3://dq-artifacts/agent-reports/report.json"


def test_load_copilot_report_context_rejects_other_alert(monkeypatch) -> None:
    """
    Validate that report evidence cannot cross alert boundaries.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    payload = {
        "alert": {"alert_key": "orders|other-alert"},
        "summary": "Wrong incident.",
        "evidence": [],
    }

    monkeypatch.setattr(
        api_main,
        "read_report_artifact",
        lambda s3_uri, max_bytes: {
            "truncated": False,
            "text": json.dumps(payload),
        },
    )

    with pytest.raises(ValueError, match="does not match"):
        api_main.load_copilot_report_context(
            report_json_s3_uri="s3://dq-artifacts/report.json",
            expected_alert_key="orders|selected-alert",
        )


def test_copilot_answer_endpoint_uses_shared_context_and_audit(monkeypatch) -> None:
    """
    Validate the Copilot endpoint delegates to shared routing and writes audit metadata.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    alert = DummyAlert(
        {
            "alert_id": None,
            "alert_key": "orders|test",
            "alert_display_id": "DQ-00000000-ABC123",
            "severity": "critical",
            "table_name": "dq.raw_orders",
            "metric": "row_count_positive",
            "report_s3_uri": "",
        }
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(api_main, "load_alert", lambda **kwargs: alert)
    monkeypatch.setattr(
        api_main,
        "fetch_audit_log_rows",
        lambda alert_key, limit: {
            "rows": [{"action": "triage_completed", "status": "success"}],
            "row_count": 1,
        },
    )

    def fake_build_operator_answer(**kwargs) -> str:
        """
        Capture shared Copilot arguments.

        Args:
            kwargs: Shared Copilot keyword arguments.

        Returns:
            Fixed operator answer.
        """
        captured["copilot"] = kwargs

        return "The selected partition is missing."

    def fake_write_copilot_api_audit_event(**kwargs) -> None:
        """
        Capture the API audit event arguments.

        Args:
            kwargs: Audit helper keyword arguments.

        Returns:
            None.
        """
        captured["audit"] = kwargs

    monkeypatch.setattr(api_main, "build_operator_answer", fake_build_operator_answer)
    monkeypatch.setattr(api_main, "write_copilot_api_audit_event", fake_write_copilot_api_audit_event)

    response = client.post(
        "/api/v1/copilot/answer",
        json={
            "alert_key": "orders|test",
            "question": "What happened to this partition?",
            "audit_limit": 10,
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "The selected partition is missing."
    assert response.json()["context_source"] == "alert_audit"
    assert response.json()["audit_count"] == 1
    assert captured["copilot"]["alert"] is alert
    assert str(captured["copilot"]["agent_run_id"]) == response.json()["agent_run_id"]
    assert captured["audit"]["response"].agent_run_id == response.json()["agent_run_id"]


def test_copilot_answer_endpoint_rejects_mismatched_report(monkeypatch) -> None:
    """
    Validate HTTP rejection when a report artifact belongs to another alert.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    alert = DummyAlert(
        {
            "alert_id": None,
            "alert_key": "orders|selected-alert",
            "alert_display_id": "DQ-00000000-ABC123",
            "severity": "critical",
            "table_name": "dq.raw_orders",
            "metric": "row_count_positive",
            "report_s3_uri": "",
        }
    )
    payload = {
        "alert": {"alert_key": "orders|other-alert"},
        "summary": "Wrong incident.",
        "evidence": [],
    }

    monkeypatch.setattr(api_main, "load_alert", lambda **kwargs: alert)
    monkeypatch.setattr(
        api_main,
        "fetch_audit_log_rows",
        lambda alert_key, limit: {"rows": [], "row_count": 0},
    )
    monkeypatch.setattr(
        api_main,
        "read_report_artifact",
        lambda s3_uri, max_bytes: {
            "truncated": False,
            "text": json.dumps(payload),
        },
    )

    response = client.post(
        "/api/v1/copilot/answer",
        json={
            "alert_key": "orders|selected-alert",
            "question": "Explain the evidence.",
            "report_json_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
        },
    )

    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]

def test_audit_log_sql_exposes_correlation_identifiers() -> None:
    """
    Validate that API audit reads can correlate user answers with agent runs.

    Returns:
        None.
    """
    sql = api_main.build_audit_log_sql(
        alert_key="orders|test",
        limit=10,
    )

    assert "audit_id" in sql
    assert "alert_id" in sql
    assert "agent_run_id" in sql
    assert "input_json" in sql
    assert "output_json" in sql
    assert "LIMIT 10" in sql


def test_llm_audit_normalization_exposes_metadata_without_raw_payloads() -> None:
    """
    Validate API-safe audit normalization exposes only allowlisted LLM metadata.

    Returns:
        None.
    """
    rows, observations = enrich_audit_rows(
        rows=[
            {
                "audit_id": "audit-1",
                "ts": "2026-06-23 10:48:21.504",
                "agent_run_id": "agent-run-1",
                "action": "llm_route_completed",
                "status": "success",
                "duration_ms": 4583,
                "input_json": json.dumps({"requested_route": "triage_reasoning"}),
                "output_json": json.dumps(
                    {
                        "executed_route": "evidence_summary",
                        "provider": "heuristic",
                        "model": "heuristic-v1",
                        "input_tokens": 720,
                        "output_tokens": 407,
                        "estimated_cost_usd": 0.0,
                        "used_heuristic": True,
                        "fallback_reason": "provider_error:openai:RateLimitError",
                        "attempted_routes": ["triage_reasoning", "evidence_summary"],
                    }
                ),
            }
        ]
    )

    route = rows[0]["llm_route"]

    assert "input_json" not in rows[0]
    assert "output_json" not in rows[0]
    assert len(observations) == 1
    assert route["provider"] == "heuristic"
    assert route["runtime_mode"] == "heuristic_fallback"
    assert route["total_tokens"] == 1127
    assert "quota or credit limit" in route["fallback_summary"]


def test_life_evaluation_history_endpoint_returns_sanitized_contract(monkeypatch) -> None:
    """
    Validate the API exposes bounded LIFE summaries without raw audit payloads.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_list_life_evaluation_history(**kwargs) -> dict[str, object]:
        """
        Capture endpoint filters and return one sanitized evaluation.

        Args:
            **kwargs: LIFE history tool arguments.

        Returns:
            API-compatible LIFE history payload.
        """
        captured.update(kwargs)

        return {
            "status": "success",
            "row_count": 1,
            "lookback_days": 30,
            "eval_status_filter": "review",
            "scenario_id_filter": "missing_latest_day",
            "duration_ms": 5,
            "rows": [
                {
                    "audit_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "evaluated_at": "2026-08-07T01:02:04+00:00",
                    "run_id": "life-eval-20260807T010203",
                    "scenario_id": "missing_latest_day",
                    "eval_status": "review",
                    "failed_checks": ["confidence"],
                    "failure_category": "low_confidence",
                    "failure_categories": ["low_confidence"],
                    "life_stage": "find_faults",
                    "suggested_change_type": "evidence_plan_review",
                    "suggested_change_summary": "Review evidence coverage.",
                    "requires_human_approval": True,
                    "summary": "Confidence remains below target.",
                    "source_report_sha256": "a" * 64,
                    "json_report_s3_uri": "s3://dq-artifacts/agent-life/report.json",
                    "markdown_report_s3_uri": "s3://dq-artifacts/agent-life/report.md",
                    "payload_valid": True,
                }
            ],
        }

    monkeypatch.setattr(
        api_main,
        "list_life_evaluation_history",
        fake_list_life_evaluation_history,
    )

    response = client.get(
        "/api/v1/evaluations/life",
        params={
            "eval_status": "review",
            "scenario_id": "missing_latest_day",
            "lookback_days": 30,
            "limit": 10,
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["row_count"] == 1
    assert payload["rows"][0]["eval_status"] == "review"
    assert payload["rows"][0]["requires_human_approval"] is True
    assert "input_json" not in payload["rows"][0]
    assert "output_json" not in payload["rows"][0]
    assert captured["eval_status"] == "review"
    assert captured["scenario_id"] == "missing_latest_day"
    assert captured["lookback_days"] == 30
    assert captured["limit"] == 10
