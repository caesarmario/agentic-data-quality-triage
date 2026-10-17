"""FastAPI recovery routes: authorization, validation, and safe conflicts."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent.tools.incident_resolution_store import ResolutionConflict
from apps.api import main as api_main


def test_execution_responses_read_actual_outbox_instead_of_retained_pending_flag():
    from types import SimpleNamespace
    from agent.recovery.service import execution_view
    execution = SimpleNamespace(execution_id="execution-1", plan_id="plan-1", synchronization_pending=True)
    execution.model_dump = lambda **_: {"synchronization_pending": execution.synchronization_pending}
    class Cursor:
        pending = 0
        def execute(self, query, params):
            assert "NOT (audit_done AND delivery_done)" in query
            assert params == ("execution-1", "plan-1")
        def fetchone(self):
            return (self.pending,)
    cursor = Cursor()
    assert execution_view(cursor, execution) == {"synchronization_pending": False}
    cursor.pending = 1
    assert execution_view(cursor, execution) == {"synchronization_pending": True}


def test_recovery_plan_mutation_is_disabled_without_server_token(monkeypatch):
    monkeypatch.delenv(api_main.APPROVAL_TOKEN_ENV_NAME, raising=False)
    response = TestClient(api_main.app).post(
        "/api/v1/alerts/DQ-20261014-ABC123/recovery-plans",
        json={"reason": "Repair the scoped partition.", "expected_incident_revision": 1},
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_recovery_plan_mutation_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setenv(api_main.APPROVAL_TOKEN_ENV_NAME, "correct-secret")
    client = TestClient(api_main.app)
    url = "/api/v1/alerts/DQ-20261014-ABC123/recovery-plans"
    payload = {"reason": "Repair the scoped partition.", "expected_incident_revision": 1}
    assert client.post(url, json=payload).status_code == 401
    response = client.post(url, json=payload, headers={"X-Control-Plane-Token": "wrong-secret"})
    assert response.status_code == 401


def test_recovery_plan_route_returns_422_for_client_supplied_provider_scope(monkeypatch):
    monkeypatch.setenv(api_main.APPROVAL_TOKEN_ENV_NAME, "test-token")
    response = TestClient(api_main.app).post(
        "/api/v1/alerts/DQ-20261014-ABC123/recovery-plans",
        headers={"X-Control-Plane-Token": "test-token"},
        json={"reason": "Repair the scoped partition.", "expected_incident_revision": 1,
              "provider": "gemini", "source_manifest": {"bucket": "override"}},
    )
    assert response.status_code == 422


def test_recovery_plan_route_maps_stale_service_conflict_to_409(monkeypatch):
    monkeypatch.setenv(api_main.APPROVAL_TOKEN_ENV_NAME, "test-token")

    def stale_plan(*_args, **_kwargs):
        raise ResolutionConflict("incident revision changed")

    monkeypatch.setattr(api_main.recovery_service, "create_plan", stale_plan)
    response = TestClient(api_main.app).post(
        "/api/v1/alerts/DQ-20261014-ABC123/recovery-plans",
        headers={"X-Control-Plane-Token": "test-token"},
        json={"reason": "Repair the scoped partition.", "expected_incident_revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "incident revision changed"


def test_recovery_preview_is_read_only_and_delegates_to_service(monkeypatch):
    calls = []
    preview = {"alert_ref": "DQ-20261014-ABC123", "incident_revision": 2, "eligible": False,
               "available": True, "scope": {"dt": "2026-10-14"}, "targets": [], "blockers": ["stale"],
               "observations": {}, "source": None, "latest_plan_id": None}
    monkeypatch.setattr(api_main.recovery_service, "recovery_preview", lambda ref: calls.append(ref) or preview)

    response = TestClient(api_main.app).get("/api/v1/alerts/DQ-20261014-ABC123/recovery-preview")

    assert response.status_code == 200
    assert response.json() == {**preview, "runbook": None}
    assert calls == ["DQ-20261014-ABC123"]


def test_recovery_api_sanitizes_unexpected_service_errors(monkeypatch):
    monkeypatch.setattr(api_main.recovery_service, "recovery_preview",
                        lambda _ref: (_ for _ in ()).throw(RuntimeError("password=never-return-this")))

    response = TestClient(api_main.app).get("/api/v1/alerts/DQ-20261014-ABC123/recovery-preview")

    assert response.status_code == 503
    assert "password" not in response.text
    assert "never-return-this" not in response.text
