####
## LLM Provider Smoke Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

import pytest

from agent.llm import client as llm_client
from agent.llm.client import LlmResponse
from agent.llm.config import load_model_routing_config
from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from dags.dq_platform.llm_smoke import LLM_SMOKE_ROUTE_NAMES, emit_llm_smoke_summary
from scripts.smoke_llm_provider import (
    ProviderSmokeExecutionError,
    ProviderSmokeRequirementError,
    build_content_preview,
    run_provider_smoke,
)
from scripts.trigger_airflow_llm_smoke import build_llm_smoke_run_id


# --- Defining Constants
AGENT_RUN_ID = UUID("11111111-2222-3333-4444-555555555555")


# --- Defining Test Doubles
class FakeAuditClient:
    """
    Capture ClickHouse insert calls made by the provider smoke audit boundary.

    Attributes:
        inserts: Recorded ClickHouse-compatible insert keyword arguments.
    """

    def __init__(self) -> None:
        """
        Initialize an empty insert collection.

        Returns:
            None.
        """
        self.inserts: list[dict[str, Any]] = []

    def insert(self, **kwargs: Any) -> None:
        """
        Capture one ClickHouse-compatible insert call.

        Args:
            kwargs: Insert arguments supplied by the audit helper.

        Returns:
            None.
        """
        self.inserts.append(kwargs)


class FakeDagRun:
    """
    Provide minimal Airflow 3 DagRun context for summary tests.

    Attributes:
        dag_id: Manual provider smoke DAG identifier.
        run_id: Synthetic Airflow run identifier.
        conf: Selected provider route and strictness.
    """

    dag_id = "92_dag_dq_llm_provider_smoke"
    run_id = "manual__llm_smoke_summary_test"
    conf   = {"route_name": "cheap_summary", "require_provider": False}


# --- Defining Test Helpers
def build_response(
    provider: str,
    route_name: str,
    model: str,
    used_heuristic: bool = False,
    fallback_reason: str = "",
    attempted_routes: list[str] | None = None,
) -> LlmResponse:
    """
    Build a normalized routed response without external provider IO.

    Args:
        provider: Provider that produced the final response.
        route_name: Route that produced the final response.
        model: Model name reported by the router.
        used_heuristic: Whether local deterministic fallback produced the response.
        fallback_reason: Sanitized fallback reason.
        attempted_routes: Optional ordered route attempts.

    Returns:
        LlmResponse accepted by the provider smoke runner.
    """
    return LlmResponse(
        agent_run_id=AGENT_RUN_ID,
        route_name=route_name,
        provider=provider,
        model=model,
        content="The latest raw orders partition is empty. Verify ingestion before trusting downstream metrics.",
        input_tokens=24,
        output_tokens=18,
        estimated_cost_usd=0.00001 if not used_heuristic else 0.0,
        used_heuristic=used_heuristic,
        fallback_reason=fallback_reason,
        duration_ms=25,
        metadata={
            "attempted_routes": attempted_routes or [route_name],
            "structured_output_status": "not_requested",
            "provider_failures": [],
        },
    )


def build_response_runner(response: LlmResponse) -> Callable[..., LlmResponse]:
    """
    Build a routed LLM callable that returns one controlled response.

    Args:
        response: Normalized response returned by the callable.

    Returns:
        Callable compatible with run_provider_smoke.
    """
    def run_response(**kwargs: Any) -> LlmResponse:
        """
        Return the configured response without provider network IO.

        Args:
            kwargs: Routed request arguments supplied by the smoke runner.

        Returns:
            Configured LlmResponse.
        """
        assert kwargs["prompt"]
        assert kwargs["context"]["data_classification"] == "synthetic_non_sensitive"

        return response

    return run_response


def build_audit_row(client: FakeAuditClient) -> dict[str, Any]:
    """
    Convert the latest fake insert into a named audit row.

    Args:
        client: Fake audit client containing at least one insert.

    Returns:
        Dictionary keyed by dq.agent_audit_log column names.
    """
    insert_call = client.inserts[-1]

    return dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0], strict=True))


# --- Defining Tests
def test_smoke_route_allowlist_matches_model_routing_config() -> None:
    """
    Ensure Airflow route parameters cannot drift from the routing YAML.

    Returns:
        None.
    """
    config = load_model_routing_config()

    assert LLM_SMOKE_ROUTE_NAMES == tuple(config.routes)


def test_heuristic_baseline_writes_sanitized_success_audit() -> None:
    """
    Ensure deterministic baseline execution is non-empty and audit-backed.

    Returns:
        None.
    """
    client   = FakeAuditClient()
    response = build_response(
        provider="heuristic",
        route_name="evidence_summary",
        model="heuristic-v1",
        used_heuristic=True,
        fallback_reason="forced_heuristic",
    )

    result = run_provider_smoke(
        route_name="evidence_summary",
        force_heuristic=True,
        client=client,
        llm_runner=build_response_runner(response),
    )
    audit_row    = build_audit_row(client)
    input_json   = json.loads(audit_row["input_json"])
    output_json  = json.loads(audit_row["output_json"])
    serialized   = json.dumps({"input": input_json, "output": output_json})

    assert result.outcome == "heuristic"
    assert result.status == "success"
    assert result.content_length > 0
    assert result.content_sha256
    assert audit_row["action"] == "llm_provider_smoke"
    assert audit_row["status"] == "success"
    assert input_json["context_classification"] == "synthetic_non_sensitive"
    assert "api_key" not in serialized.lower()
    assert "system_prompt" not in serialized.lower()


@pytest.mark.parametrize(
    ("route_name", "provider", "model"),
    [
        ("cheap_summary", "gemini", "gemini-2.5-flash"),
        ("openai_summary", "openai", "gpt-4.1-nano"),
        ("low_confidence_rca", "xai", "grok-4.3"),
    ],
)
def test_external_provider_routes_satisfy_strict_smoke_contract(
    route_name: str,
    provider: str,
    model: str,
) -> None:
    """
    Ensure Gemini, OpenAI, and xAI route outcomes share one strict contract.

    Args:
        route_name: Route selected by the parameterized case.
        provider: Expected provider configured for the route.
        model: Expected provider model.

    Returns:
        None.
    """
    client   = FakeAuditClient()
    response = build_response(provider=provider, route_name=route_name, model=model)

    result = run_provider_smoke(
        route_name=route_name,
        require_provider=True,
        client=client,
        llm_runner=build_response_runner(response),
    )

    assert result.outcome == "external_provider"
    assert result.requested_provider == provider
    assert result.executed_provider == provider
    assert build_audit_row(client)["status"] == "success"


def test_fallback_safe_smoke_records_actual_heuristic_execution() -> None:
    """
    Ensure provider failure can degrade safely while remaining explicit.

    Returns:
        None.
    """
    client   = FakeAuditClient()
    response = build_response(
        provider="heuristic",
        route_name="evidence_summary",
        model="heuristic-v1",
        used_heuristic=True,
        fallback_reason="provider_failures:gemini,openai;final=heuristic_provider",
        attempted_routes=["cheap_summary", "openai_summary", "evidence_summary"],
    )

    result = run_provider_smoke(
        route_name="cheap_summary",
        require_provider=False,
        client=client,
        llm_runner=build_response_runner(response),
    )
    audit_output = json.loads(build_audit_row(client)["output_json"])

    assert result.status == "success"
    assert result.outcome == "fallback"
    assert result.requested_provider == "gemini"
    assert result.executed_provider == "heuristic"
    assert audit_output["attempted_routes"] == [
        "cheap_summary",
        "openai_summary",
        "evidence_summary",
    ]


def test_strict_smoke_fails_and_audits_provider_fallback() -> None:
    """
    Ensure strict mode rejects a usable response from the wrong provider.

    Returns:
        None.
    """
    client   = FakeAuditClient()
    response = build_response(
        provider="heuristic",
        route_name="evidence_summary",
        model="heuristic-v1",
        used_heuristic=True,
        fallback_reason="provider_unavailable",
        attempted_routes=["cheap_summary", "evidence_summary"],
    )

    with pytest.raises(ProviderSmokeRequirementError, match="Required provider was not used"):
        run_provider_smoke(
            route_name="cheap_summary",
            require_provider=True,
            client=client,
            llm_runner=build_response_runner(response),
        )

    audit_row = build_audit_row(client)

    assert audit_row["status"] == "failed"
    assert json.loads(audit_row["output_json"])["executed_provider"] == "heuristic"


def test_provider_execution_failure_does_not_persist_raw_error_text() -> None:
    """
    Ensure provider exceptions are reduced to a safe error type in logs and audit.

    Returns:
        None.
    """
    client = FakeAuditClient()

    def fail_provider(**kwargs: Any) -> LlmResponse:
        """
        Simulate a provider error containing text that must not be persisted.

        Args:
            kwargs: Routed request arguments supplied by the smoke runner.

        Raises:
            RuntimeError: Always raised with synthetic sensitive-looking text.
        """
        raise RuntimeError("synthetic-secret-token-should-not-be-retained")

    with pytest.raises(ProviderSmokeExecutionError) as exc_info:
        run_provider_smoke(
            route_name="cheap_summary",
            client=client,
            llm_runner=fail_provider,
        )

    audit_row  = build_audit_row(client)
    serialized = json.dumps(audit_row, default=str)

    assert "RuntimeError" in str(exc_info.value)
    assert "synthetic-secret-token" not in str(exc_info.value)
    assert "synthetic-secret-token" not in serialized
    assert json.loads(audit_row["output_json"]) == {"error_type": "RuntimeError"}


def test_provider_timeout_and_retry_settings_are_bounded(monkeypatch) -> None:
    """
    Ensure external provider calls cannot receive unbounded timeout or retry values.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("LLM_PROVIDER_MAX_RETRIES", "2")

    assert llm_client.resolve_provider_timeout_seconds() == 12.5
    assert llm_client.resolve_provider_max_retries() == 2

    monkeypatch.setenv("LLM_PROVIDER_TIMEOUT_SECONDS", "121")

    with pytest.raises(ValueError, match="must be between 1 and 120"):
        llm_client.resolve_provider_timeout_seconds()

    monkeypatch.setenv("LLM_PROVIDER_MAX_RETRIES", "6")

    with pytest.raises(ValueError, match="must be between 0 and 5"):
        llm_client.resolve_provider_max_retries()


def test_content_preview_is_single_line_and_bounded() -> None:
    """
    Ensure Airflow logs receive only a short readable content preview.

    Returns:
        None.
    """
    preview = build_content_preview("first line\nsecond line " + ("x" * 300), limit=80)

    assert "\n" not in preview
    assert len(preview) == 80
    assert preview.endswith("...")


def test_llm_smoke_run_id_is_unique_and_shell_safe() -> None:
    """
    Ensure generated provider smoke run ids remain readable and path-safe.

    Returns:
        None.
    """
    now    = datetime(2026, 7, 16, 1, 2, 3, 456789, tzinfo=timezone.utc)
    run_id = build_llm_smoke_run_id("cheap_summary", now=now)

    assert run_id == "manual__llm_smoke_cheap_summary_20260716T010203456789"


def test_llm_smoke_summary_uses_airflow3_compatible_context() -> None:
    """
    Ensure the DAG summary relies only on Airflow SDK context fields.

    Returns:
        None.
    """
    summary = emit_llm_smoke_summary(dag_run=FakeDagRun())

    assert summary["result"] == "success"
    assert summary["route_name"] == "cheap_summary"
    assert summary["require_provider"] is False
    assert summary["task_states"] == {
        "t10_smoke_heuristic_baseline": "success",
        "t20_smoke_selected_route": "success",
    }
