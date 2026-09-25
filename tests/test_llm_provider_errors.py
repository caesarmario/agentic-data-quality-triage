####
## Provider Diagnostic Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
import json
import socket
import ssl
from uuid import UUID

import httpx
import pytest
from openai import RateLimitError

from agent.llm.client import LlmResponse
from agent.llm.provider_errors import safe_identifier, summarize_provider_error
from scripts.smoke_llm_provider import build_smoke_result


# --- Defining Test Helpers
def quota_error(body: object) -> RateLimitError:
    """
    Build a real SDK exception without sending an HTTP request.

    Args:
        body: Synthetic provider response body.

    Returns:
        SDK rate-limit exception with a synthetic HTTP 429 response.
    """
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
    return RateLimitError("private raw error", response=response, body=body)


# --- Testing Safe Diagnostic Extraction
def test_google_quota_and_retry_details_are_retained() -> None:
    """Verify nested Google quota codes survive without retaining raw messages."""
    result = summarize_provider_error(quota_error({"error": {
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded. Private content must not be retained.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                "quotaValue": "0",
                "description": "private description",
            }]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30.5s"},
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"},
        ],
    }}))

    assert result["http_status"] == "429"
    assert result["provider_status"] == "RESOURCE_EXHAUSTED"
    assert result["provider_reason"] == "RATE_LIMIT_EXCEEDED"
    assert result["error_hint"] == "quota_mentioned"
    assert result["retry_delay"] == "30.5s"
    assert json.loads(result["quota_violations"])[0]["quotaValue"] == "0"
    assert "private" not in json.dumps(result).lower()


@pytest.mark.parametrize("body", [None, [], "raw secret", {"error": []}, {"details": None}])
def test_malformed_provider_bodies_do_not_break_fallback(body: object) -> None:
    """Verify malformed bodies retain only HTTP status and never raise."""
    assert summarize_provider_error(quota_error(body)) == {"http_status": "429"}


@pytest.mark.parametrize("value", ["AQ.fake-key", "AIza-fake-key", "sk-fake", "https://private", "raw\ntext", "x" * 181])
def test_secret_shaped_and_unbounded_identifiers_are_rejected(value: str) -> None:
    """Verify credential patterns, URLs, multiline values, and long fields are dropped."""
    assert safe_identifier(value) == ""


def test_environment_secret_is_not_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify even an identifier-shaped credential cannot enter diagnostics."""
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic_private_value")
    assert safe_identifier("synthetic_private_value") == ""


def test_openai_compatible_error_is_preserved_in_smoke_audit() -> None:
    """Verify safe error fields pass through smoke output while raw fields are dropped."""
    details = summarize_provider_error(quota_error({"code": "insufficient_quota"}))
    response = LlmResponse(
        agent_run_id=UUID("11111111-2222-3333-4444-555555555555"),
        provider="heuristic", model="heuristic-v1", route_name="evidence_summary",
        content="Provider unavailable; no external response was received.",
        used_heuristic=True,
        metadata={"provider_failures": [{**details, "raw_body": "private"}]},
    )
    result = build_smoke_result(
        response=response, requested_route="cheap_summary", requested_provider="gemini",
        requested_model="gemini-3.5-flash-lite", require_provider=True, force_heuristic=False,
    )
    assert result.provider_failures[0]["provider_reason"] == "insufficient_quota"
    assert result.provider_failures[0]["http_status"] == "429"
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(("cause", "reason"), [
    (socket.gaierror(-2, "private-host-and-key"), "dns_resolution_failed"),
    (ssl.SSLCertVerificationError(1, "private certificate"), "tls_certificate_verification_failed"),
    (httpx.ConnectTimeout("private endpoint"), "transport_timeout"),
    (ConnectionRefusedError("private endpoint"), "connection_refused"),
    (ConnectionResetError("private endpoint"), "connection_reset"),
])
def test_chained_transport_diagnostics_are_fixed_and_secret_free(cause, reason) -> None:
    """Classify network causes independently from quota/billing without serializing messages."""
    wrapped = RuntimeError("private SDK request")
    wrapped.__cause__ = httpx.ConnectError("private proxy")
    wrapped.__cause__.__cause__ = cause
    details = summarize_provider_error(wrapped)
    assert details == {"transport_reason": reason}
    assert "private" not in json.dumps(details)


def test_transport_cause_cycles_and_unknown_errors_remain_bounded() -> None:
    """Malformed exception chains cannot hang fallback or invent a billing diagnosis."""
    error = RuntimeError("private")
    error.__cause__ = error
    assert summarize_provider_error(error) == {}
