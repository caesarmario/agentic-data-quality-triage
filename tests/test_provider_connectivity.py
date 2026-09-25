####
## Provider Connectivity Preflight Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Verify zero-inference preflight failure, secret isolation, and bounded execution."""

# --- Importing Libraries
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent.llm import connectivity


# --- Defining Fake Transport Fixtures
@pytest.fixture
def route(monkeypatch):
    """Provide enabled Gemini configuration without loading real credentials."""
    value = SimpleNamespace(use_heuristic=False, provider_name="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setattr(connectivity, "resolve_route", lambda **kwargs: value)
    return value


def test_preflight_uses_no_credentials_and_bounds_entire_subprocess(monkeypatch, route) -> None:
    """The probe cannot inherit provider keys and has a hard timeout covering DNS."""
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "private-second-key")

    def run(command, **kwargs):
        """Inspect the safe fixed command without creating any process or request."""
        assert command[-1] == "--probe-gemini"
        assert "GEMINI_API_KEY" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["timeout"] == 12
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0, stdout=json.dumps({"reachable": True, "http_status": 404}))

    monkeypatch.setattr(connectivity.subprocess, "run", run)
    connectivity.require_gemini_connectivity("synthetic-test")


@pytest.mark.parametrize("body", [
    {"reachable": False, "reason": "dns_resolution_failed"},
    {"reachable": False, "reason": "private-secret-message"},
    {"reachable": False, "reason": {"untrusted": "value"}},
    {"reachable": True, "http_status": True},
    {"reachable": True, "http_status": "private-secret-message"},
    [],
])
def test_failed_or_malformed_probe_cannot_start_paid_work(monkeypatch, route, body) -> None:
    """Failures expose only fixed diagnostics and never turn into paid-provider readiness."""
    monkeypatch.setattr(connectivity.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(body)))
    with pytest.raises(RuntimeError) as caught:
        connectivity.require_gemini_connectivity("synthetic-test")
    assert "private-secret-message" not in str(caught.value)


def test_stalled_dns_process_is_bounded(monkeypatch, route) -> None:
    """A stuck resolver becomes a timeout instead of hanging the Airflow task."""
    def run(*args, **kwargs):
        """Simulate an OS subprocess timeout without network IO."""
        raise subprocess.TimeoutExpired("fixed-probe", 12)
    monkeypatch.setattr(connectivity.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="probe_timeout"):
        connectivity.require_gemini_connectivity("synthetic-test")


@pytest.mark.parametrize("url", ["http://generativelanguage.googleapis.com", "https://example.com", "https://key@generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/?key=private"])
def test_nonofficial_or_credential_bearing_urls_are_rejected(monkeypatch, route, url) -> None:
    """Configuration cannot redirect the strict acceptance probe to another endpoint."""
    route.base_url = url
    def forbidden(*args, **kwargs):
        """Fail if rejected endpoint configuration reaches the transport subprocess."""
        raise AssertionError("Transport should not run.")
    monkeypatch.setattr(connectivity.subprocess, "run", forbidden)
    with pytest.raises(connectivity.GeminiConnectivityError, match="invalid_gemini_route"):
        connectivity.require_gemini_connectivity("synthetic-test")


def test_preflight_uses_supplied_route_without_resolving_default(monkeypatch) -> None:
    """A custom-config smoke route is checked rather than the repository default."""
    route = SimpleNamespace(use_heuristic=False, provider_name="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setattr(connectivity, "resolve_route", lambda **kwargs: pytest.fail("Default route was resolved"))
    monkeypatch.setattr(
        connectivity.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({"reachable": True, "http_status": 404})),
    )
    connectivity.require_gemini_connectivity("synthetic-test", route=route)


def test_airflow_runner_stops_before_fanout_when_preflight_fails(monkeypatch) -> None:
    """The operational entrypoint must not admit workers after transport rejection."""
    from scripts import run_control_plane_supervisor as runner
    from agent.supervisor.models import SupervisorRequest
    request = SupervisorRequest(
        intent="interpret_incident_evidence", alert_key="sample-alert",
        execution_mode="fanout", max_workers=3, max_handoffs=3, max_concurrency=3,
        token_budget=32_000, allow_external_llm=True,
    )
    monkeypatch.setattr(runner, "build_request", lambda args: request)
    def preflight(run_id):
        """Simulate the bounded DNS failure retained in Airflow logs."""
        raise RuntimeError("dns_resolution_failed")
    def forbidden(**kwargs):
        """No worker allocator or model runtime may run after failed preflight."""
        raise AssertionError("Workers must not start.")
    monkeypatch.setattr(runner, "require_gemini_connectivity", preflight)
    monkeypatch.setattr(runner, "run_control_plane_fanout", forbidden)
    with pytest.raises(RuntimeError, match="dns_resolution_failed"):
        runner.run_from_args(SimpleNamespace(run_id="synthetic-airflow-run"))
