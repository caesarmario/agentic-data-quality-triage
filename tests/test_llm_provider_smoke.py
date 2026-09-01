####
## LLM Provider Smoke Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import pytest

from agent.llm import client as llm_client
from agent.llm.client import LlmResponse
from agent.llm.config import load_model_routing_config
from agent.supervisor.budgets import active_supervisor_llm_budget
from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from dags.dq_platform.llm_smoke import emit_llm_smoke_summary
from scripts.smoke_llm_provider import (
    MAX_EXTERNAL_SMOKE_ESTIMATED_COST_USD,
    MAX_EXTERNAL_SMOKE_LATENCY_MS,
    MAX_EXTERNAL_SMOKE_MODEL_CALLS,
    MAX_EXTERNAL_SMOKE_TOTAL_TOKENS,
    ProviderSmokeBudgetError,
    ProviderSmokeExecutionError,
    ProviderSmokeRequirementError,
    SMOKE_ROUTE_NAMES,
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
    conf   = {"route_name": "cheap_summary", "run_external_provider": False}


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
def test_smoke_route_allowlist_is_static_and_compatible_with_routing_config() -> None:
    """
    Ensure smoke execution cannot expand when routing YAML gains new routes.

    Returns:
        None.
    """
    config = load_model_routing_config()

    assert SMOKE_ROUTE_NAMES == ("evidence_summary", "cheap_summary")
    assert set(SMOKE_ROUTE_NAMES).issubset(config.routes)
    assert MAX_EXTERNAL_SMOKE_MODEL_CALLS == 1


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
    assert result.estimated_cost_usd == 0.0
    assert result.external_provider_smoke is False


@pytest.mark.parametrize(
    ("route_name", "provider", "model"),
    [
        ("cheap_summary", "gemini", "gemini-3.5-flash-lite"),
    ],
)
def test_external_provider_routes_satisfy_strict_smoke_contract(
    route_name: str,
    provider: str,
    model: str,
) -> None:
    """
    Ensure the one allowlisted low-risk provider route uses a strict contract.

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
        external_provider_smoke=True,
        client=client,
        llm_runner=build_response_runner(response),
    )

    assert result.outcome == "external_provider"
    assert result.requested_provider == provider
    assert result.executed_provider == provider
    assert build_audit_row(client)["status"] == "success"
    assert result.projected_tokens <= MAX_EXTERNAL_SMOKE_TOTAL_TOKENS
    assert result.projected_cost_usd <= MAX_EXTERNAL_SMOKE_ESTIMATED_COST_USD


def test_smoke_audit_uses_environment_model_override(monkeypatch) -> None:
    """
    Ensure requested-model audit metadata matches the actual environment override.

    Args:
        monkeypatch: Pytest fixture used to set a synthetic model override.

    Returns:
        None.
    """
    overridden_model = "gemini-test-model-override"
    client           = FakeAuditClient()

    monkeypatch.setenv("GEMINI_MODEL", overridden_model)

    result = run_provider_smoke(
        route_name="cheap_summary",
        require_provider=True,
        external_provider_smoke=True,
        client=client,
        llm_runner=build_response_runner(
            build_response(
                provider="gemini",
                route_name="cheap_summary",
                model=overridden_model,
            )
        ),
    )

    assert result.requested_model == overridden_model
    assert result.executed_model == overridden_model


def test_default_selected_route_stays_zero_cost_heuristic_execution() -> None:
    """
    Ensure the selected route cannot use an external provider without strict opt-in.

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
        attempted_routes=["cheap_summary"],
    )

    result = run_provider_smoke(
        route_name="cheap_summary",
        client=client,
        llm_runner=build_response_runner(response),
    )
    audit_output = json.loads(build_audit_row(client)["output_json"])

    assert result.status == "success"
    assert result.outcome == "heuristic"
    assert result.requested_provider == "gemini"
    assert result.executed_provider == "heuristic"
    assert audit_output["attempted_routes"] == ["cheap_summary"]
    assert audit_output["external_provider_smoke"] is False


def test_external_provider_smoke_requires_explicit_strict_opt_in() -> None:
    """
    Ensure direct smoke execution remains zero-cost unless strict mode is selected.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="requires strict provider execution"):
        run_provider_smoke(
            route_name="cheap_summary",
            external_provider_smoke=True,
            client=FakeAuditClient(),
            llm_runner=build_response_runner(
                build_response(provider="gemini", route_name="cheap_summary", model="gemini-3.5-flash-lite")
            ),
        )


def test_external_provider_smoke_rejects_actual_usage_above_budget() -> None:
    """
    Ensure a non-conforming provider response is audited and rejected after execution.

    Returns:
        None.
    """
    client   = FakeAuditClient()
    response = build_response(provider="gemini", route_name="cheap_summary", model="gemini-3.5-flash-lite")
    response.input_tokens       = MAX_EXTERNAL_SMOKE_TOTAL_TOKENS
    response.output_tokens      = 1
    response.estimated_cost_usd = MAX_EXTERNAL_SMOKE_ESTIMATED_COST_USD + 0.00001

    with pytest.raises(ProviderSmokeBudgetError, match="exceeded its bounded budget"):
        run_provider_smoke(
            route_name="cheap_summary",
            require_provider=True,
            external_provider_smoke=True,
            client=client,
            llm_runner=build_response_runner(response),
        )

    assert build_audit_row(client)["status"] == "failed"


def test_strict_external_smoke_uses_temporary_heuristic_fallback_config() -> None:
    """
    Ensure one failed external call cannot cascade to another paid provider route.

    Returns:
        None.
    """
    captured_config_path: Path | None = None
    client               = FakeAuditClient()

    def capture_runner(**kwargs: Any) -> LlmResponse:
        """Capture the temporary router config without executing a provider call."""
        nonlocal captured_config_path
        captured_config_path = Path(kwargs["config_path"])
        payload              = json.loads(captured_config_path.read_text(encoding="utf-8"))

        assert payload["routes"]["cheap_summary"]["fallback_route"] == "evidence_summary"

        return build_response(provider="gemini", route_name="cheap_summary", model="gemini-3.5-flash-lite")

    run_provider_smoke(
        route_name="cheap_summary",
        require_provider=True,
        external_provider_smoke=True,
        client=client,
        llm_runner=capture_runner,
    )

    assert captured_config_path is not None


def test_strict_external_smoke_installs_one_call_budget_scope() -> None:
    """
    Ensure strict smoke execution installs the exact bounded provider ledger.

    Returns:
        None.
    """
    client = FakeAuditClient()

    def inspect_budget_runner(**kwargs: Any) -> LlmResponse:
        """
        Inspect the active ledger without making provider network requests.

        Args:
            kwargs: Routed request arguments supplied by the smoke runner.

        Returns:
            Synthetic successful Gemini response.
        """
        ledger = active_supervisor_llm_budget()

        assert ledger is not None
        assert ledger.max_model_calls == MAX_EXTERNAL_SMOKE_MODEL_CALLS
        assert ledger.token_budget == MAX_EXTERNAL_SMOKE_TOTAL_TOKENS
        assert ledger.estimated_cost_budget_usd == MAX_EXTERNAL_SMOKE_ESTIMATED_COST_USD
        assert 0 < ledger.remaining_latency_ms() <= MAX_EXTERNAL_SMOKE_LATENCY_MS

        return build_response(
            provider="gemini",
            route_name="cheap_summary",
            model="gemini-3.5-flash-lite",
        )

    result = run_provider_smoke(
        route_name="cheap_summary",
        require_provider=True,
        external_provider_smoke=True,
        client=client,
        llm_runner=inspect_budget_runner,
    )

    assert result.status == "success"


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
            external_provider_smoke=True,
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
            require_provider=True,
            external_provider_smoke=True,
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
    assert summary["run_external_provider"] is False
    assert summary["task_states"] == {
        "t10_smoke_heuristic_baseline": "success",
        "t20_smoke_selected_route": "success",
    }
