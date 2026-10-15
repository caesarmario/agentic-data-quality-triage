from uuid import uuid4
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from apps.api import main
from agent.tools.incident_resolution_store import ResolutionConflict


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_APPROVAL_TOKEN", "fixture-only")
    return TestClient(main.app)


def test_resolution_preview_read_only(client, monkeypatch):
    monkeypatch.setattr(main, "resolution_preview", lambda ref: dict(alert_ref=ref, incident_revision=1,
        status="triaged", scope={}, blockers=[], available=False))
    assert client.get("/api/v1/alerts/DQ-20261005-3B55A6/resolution-preview").status_code == 200


def test_revalidation_requires_authorization(client):
    assert client.post("/api/v1/alerts/ref/revalidations", json={"actor": "a", "idempotency_key": "key"}).status_code == 401


def test_conflict_and_no_client_pass(client, monkeypatch):
    def blocked(*args):
        raise ResolutionConflict("Belum bisa ditutup.")
    monkeypatch.setattr(main, "confirm_resolution", blocked)
    body = dict(actor="operator", idempotency_key="one", resolution_request_id=str(uuid4()),
                expected_incident_revision=1, reason="Pemeriksaan lolos.")
    headers = {"X-Control-Plane-Token": "fixture-only"}
    assert client.post("/api/v1/alerts/ref/resolve", json=body, headers=headers).status_code == 409
    assert client.post("/api/v1/alerts/ref/resolve", json=body | {"status": "pass"}, headers=headers).status_code == 422


def test_dependency_errors_sanitized(client, monkeypatch):
    def failure(ref):
        raise RuntimeError("postgresql://user:password@host SQL SECRET")
    monkeypatch.setattr(main, "resolution_preview", failure)
    response = client.get("/api/v1/alerts/ref/resolution-preview")
    assert response.status_code == 503
    assert "SECRET" not in response.text and "password" not in response.text
