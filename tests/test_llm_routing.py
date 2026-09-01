####
## LLM Routing Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from agent.llm import client as llm_client
from agent.graph import (
    build_llm_report_narrative,
    build_llm_runtime_summary,
    build_report_from_state,
    llm_response_to_evidence,
)
from agent.state import Alert, EvidenceItem, EvidenceType, Hypothesis, TriageState
from agent.llm.client import (
    ExternalLlmExecutionDisabled,
    LlmRequest,
    build_response,
    run_llm_task,
    run_openai_compatible_route,
)
from agent.llm.config import (
    load_model_routing_config,
    resolve_executable_route,
    resolve_external_llm_enabled,
    resolve_route,
)
from agent.llm.costing import estimate_cost_usd, estimate_tokens
from agent.llm.sanitization import sanitize_llm_content
from agent.llm.structured_output import (
    build_json_schema_response_format,
    is_structured_output_unsupported_error,
    parse_structured_output,
)
from agent.supervisor.budgets import (
    SupervisorLlmBudgetExceeded,
    supervisor_llm_budget_scope,
)
from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS, write_llm_route_audit_event


# --- Defining Constants
CONFIG_PATH = Path("configs/agent/model_routing.yml")
ENV_EXAMPLE_PATH = Path(".env.example")


# --- Defining Test Doubles
class StructuredIncidentSummary(BaseModel):
    """
    Define a small structured RCA response contract for provider tests.

    Attributes:
        summary: Human-readable incident summary.
        confidence: Bounded confidence score.
    """

    summary: str
    confidence: float = Field(ge=0.0, le=1.0)


class FakeProviderError(RuntimeError):
    """
    Represent a provider error with an HTTP-like status code.

    Attributes:
        status_code: HTTP-like provider response status.
    """

    def __init__(self, message: str, status_code: int) -> None:
        """
        Initialize a fake provider failure.

        Args:
            message: Error text returned by the provider.
            status_code: HTTP-like response status.

        Returns:
            None.
        """
        super().__init__(message)
        self.status_code = status_code


class FakeChatCompletions:
    """
    Capture completion requests and optionally fail the first call.

    Attributes:
        calls: Completion keyword arguments received by the fake.
        first_error: Optional exception raised on the first call.
        content: Provider content returned after any first-call failure.
    """

    def __init__(self, content: str, first_error: Exception | None = None) -> None:
        """
        Initialize the completion test double.

        Args:
            content: Completion message returned by successful calls.
            first_error: Optional exception raised by the first call.

        Returns:
            None.
        """
        self.calls = []
        self.first_error = first_error
        self.content = content

    def create(self, **kwargs) -> SimpleNamespace:
        """
        Record one completion call and return a minimal SDK-compatible response.

        Args:
            kwargs: Completion arguments sent by the provider client.

        Returns:
            Simple namespace containing choices and usage.

        Raises:
            Exception: Configured first-call provider error.
        """
        self.calls.append(kwargs)

        if len(self.calls) == 1 and self.first_error:
            raise self.first_error

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        )


class FakeOpenAiClient:
    """
    Expose fake chat completions through the OpenAI-compatible client shape.

    Attributes:
        completions: Captured fake completion endpoint.
        chat: Namespace matching the SDK chat interface.
    """

    def __init__(self, completions: FakeChatCompletions) -> None:
        """
        Initialize the fake provider client.

        Args:
            completions: Fake completion endpoint.

        Returns:
            None.
        """
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)


class FakeAuditClient:
    """
    Capture ClickHouse insert calls made by audit helpers.

    Attributes:
        inserts: Recorded insert keyword arguments.
    """

    def __init__(self) -> None:
        """
        Initialize an empty insert collection.

        Returns:
            None.
        """
        self.inserts = []

    def insert(self, **kwargs) -> None:
        """
        Capture one ClickHouse-compatible insert call.

        Args:
            kwargs: Insert arguments supplied by the audit helper.

        Returns:
            None.
        """
        self.inserts.append(kwargs)


# --- Defining Test Helpers
def build_triage_state() -> TriageState:
    """
    Build minimal triage state for LLM narrative tests.

    Returns:
        TriageState with alert, hypothesis, and one evidence item.
    """
    state = TriageState(
        alert_key="orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table",
    )
    state.alert = Alert(
        alert_key=state.alert_key,
        status="open",
        alert_type="dq_failure",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count_positive",
        dt="2026-06-10",
        observed_value=0,
        expected_value=1,
    )
    state.hypotheses = [
        Hypothesis(
            title="Missing or empty ClickHouse partition",
            description="The raw partition has zero rows for the alert date.",
            likelihood=0.84,
            confidence=0.84,
            root_cause_category="missing_partition",
            recommended_action="Backfill the affected date after confirming landing data.",
        )
    ]
    state.evidence = [
        EvidenceItem(
            evidence_type=EvidenceType.SQL_RESULT,
            tool_name="clickhouse_sql",
            description="Current partition row count evidence.",
            summary="Current partition row count is 0.",
            row_count=1,
        )
    ]

    return state


# --- Defining Tests
def test_model_routing_config_loads_default_routes() -> None:
    """
    Validate that the model routing config loads with expected provider routes.

    Returns:
        None.
    """
    config = load_model_routing_config(config_path=CONFIG_PATH)

    assert config.default_route == "evidence_summary"
    assert "heuristic" in config.providers
    assert "gemini" in config.providers
    assert "groq" in config.providers
    assert "openai" in config.providers
    assert "qwen" not in config.providers
    assert "triage_reasoning" in config.routes
    assert config.routes["cheap_summary"].provider == "gemini"
    assert config.routes["cheap_summary"].model == "gemini-3.5-flash-lite"
    assert config.routes["cheap_summary"].input_cost_per_1m_tokens == 0.30
    assert config.routes["cheap_summary"].output_cost_per_1m_tokens == 2.50
    assert config.routes["groq_summary"].provider == "groq"
    assert config.routes["groq_summary"].model == "openai/gpt-oss-20b"
    assert config.routes["groq_summary"].input_cost_per_1m_tokens == 0.075
    assert config.routes["groq_summary"].output_cost_per_1m_tokens == 0.30
    assert config.routes["cheap_summary"].fallback_route == "openai_summary"
    assert config.routes["evidence_planning"].provider == "gemini"
    assert config.routes["evidence_planning"].fallback_route == "triage_reasoning"
    assert config.routes["evidence_planning"].structured_output_mode == "preferred"
    assert config.routes["evidence_planning"].max_output_tokens == 4096
    assert config.routes["hypothesis_framing"].provider == "gemini"
    assert config.routes["hypothesis_framing"].fallback_route == "triage_reasoning"
    assert config.routes["hypothesis_framing"].structured_output_mode == "preferred"
    assert config.routes["hypothesis_framing"].max_output_tokens == 4096
    assert config.routes["catalog_qa"].provider == "gemini"
    assert config.routes["catalog_qa"].fallback_route == "openai_summary"
    assert config.routes["triage_reasoning"].structured_output_mode == "preferred"
    assert config.routes["low_confidence_rca"].structured_output_mode == "preferred"
    assert config.routes["cheap_summary"].structured_output_mode == "off"
    assert config.routes["low_confidence_rca"].fallback_route == "triage_reasoning"


def test_build_json_schema_response_format_uses_pydantic_contract() -> None:
    """
    Validate provider response_format contains a normalized schema for client-side enforcement.

    Returns:
        None.
    """
    response_format = build_json_schema_response_format(
        response_model=StructuredIncidentSummary,
        schema_name="triage result/v1",
    )

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "triage_result_v1"
    assert response_format["json_schema"]["strict"] is False
    assert set(response_format["json_schema"]["schema"]["properties"]) == {"summary", "confidence"}


def test_parse_structured_output_accepts_json_code_fence() -> None:
    """
    Validate Pydantic parsing canonicalizes provider JSON wrapped in a code fence.

    Returns:
        None.
    """
    result = parse_structured_output(
        content='```json\n{"summary":"Missing partition","confidence":0.91}\n```',
        response_model=StructuredIncidentSummary,
    )

    assert result.data == {"summary": "Missing partition", "confidence": 0.91}
    assert json.loads(result.content) == result.data


def test_structured_output_error_classifier_excludes_quota_failures() -> None:
    """
    Validate only explicit schema compatibility errors permit same-provider retry.

    Returns:
        None.
    """
    unsupported = FakeProviderError("response_format json_schema is unsupported", status_code=400)
    bad_model   = FakeProviderError("requested model is invalid", status_code=400)
    quota       = FakeProviderError("quota exceeded", status_code=429)

    assert is_structured_output_unsupported_error(unsupported) is True
    assert is_structured_output_unsupported_error(bad_model) is False
    assert is_structured_output_unsupported_error(quota) is False


def test_openai_compatible_route_retries_unsupported_schema_once(monkeypatch) -> None:
    """
    Validate unsupported json_schema falls back once to a plain JSON instruction.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    completions = FakeChatCompletions(
        content='{"summary":"The partition has zero rows.","confidence":0.88}',
        first_error=FakeProviderError(
            "response_format json_schema is unsupported",
            status_code=400,
        ),
    )
    fake_client = FakeOpenAiClient(completions=completions)
    monkeypatch.setattr(
        llm_client,
        "create_openai_compatible_client",
        lambda resolved_route: fake_client,
    )

    request = LlmRequest(
        route_name="triage_reasoning",
        prompt="Explain the failed partition.",
        response_model=StructuredIncidentSummary,
        response_schema_name="triage_result",
    )
    resolved_route = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)
    with supervisor_llm_budget_scope(
        max_model_calls=2,
        token_budget=10_000,
        estimated_cost_budget_usd=10.0,
        deadline_monotonic=time.monotonic() + 30,
    ) as ledger:
        response = run_openai_compatible_route(
            request=request,
            resolved_route=resolved_route,
            started_monotonic=time.monotonic(),
        )
        budget_usage = ledger.snapshot(latency_ms=25)

    assert len(completions.calls) == 2
    assert budget_usage.model_calls == 2
    assert budget_usage.tokens > 0
    assert completions.calls[0]["response_format"]["type"] == "json_schema"
    assert "Return only valid JSON" in completions.calls[0]["messages"][-1]["content"]
    assert "response_format" not in completions.calls[1]
    assert "Return only valid JSON" in completions.calls[1]["messages"][-1]["content"]
    assert response.structured_output == {
        "summary": "The partition has zero rows.",
        "confidence": 0.88,
    }
    assert response.metadata["structured_output_status"] == "validated"
    assert response.metadata["structured_output_provider_fallback"] is True


def test_supervised_schema_retry_stops_before_unbudgeted_provider_call(monkeypatch) -> None:
    """
    Ensure schema fallback cannot make a second network call after call exhaustion.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    completions = FakeChatCompletions(
        content='{"summary":"unused","confidence":0.50}',
        first_error=FakeProviderError(
            "response_format json_schema is unsupported",
            status_code=400,
        ),
    )
    fake_client = FakeOpenAiClient(completions=completions)
    monkeypatch.setattr(
        llm_client,
        "create_openai_compatible_client",
        lambda resolved_route: fake_client,
    )
    request = LlmRequest(
        route_name="triage_reasoning",
        prompt="Explain the failed partition.",
        response_model=StructuredIncidentSummary,
    )
    resolved_route = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    with supervisor_llm_budget_scope(
        max_model_calls=1,
        token_budget=10_000,
        estimated_cost_budget_usd=10.0,
        deadline_monotonic=time.monotonic() + 30,
    ) as ledger:
        with pytest.raises(
            SupervisorLlmBudgetExceeded,
            match="model_calls_budget_exceeded",
        ):
            run_openai_compatible_route(
                request=request,
                resolved_route=resolved_route,
                started_monotonic=time.monotonic(),
            )

        budget_usage = ledger.snapshot(latency_ms=25)

    assert len(completions.calls) == 1
    assert budget_usage.model_calls == 1


def test_supervised_client_disables_hidden_sdk_retries(monkeypatch) -> None:
    """
    Ensure supervised provider calls cannot hide retries outside the shared ledger.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    import openai

    captured: dict[str, object] = {}

    def capture_client(**kwargs):
        """Capture OpenAI client keyword arguments without creating a network client."""
        captured.update(kwargs)

        return SimpleNamespace()

    monkeypatch.setattr(openai, "OpenAI", capture_client)
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("LLM_PROVIDER_MAX_RETRIES", "4")
    resolved_route = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    with supervisor_llm_budget_scope(
        max_model_calls=1,
        token_budget=10_000,
        estimated_cost_budget_usd=10.0,
        deadline_monotonic=time.monotonic() + 30,
    ):
        llm_client.create_openai_compatible_client(resolved_route)

    assert captured["max_retries"] == 0
    assert 0 < float(captured["timeout"]) <= 30


def test_openai_compatible_route_does_not_retry_quota_failure(monkeypatch) -> None:
    """
    Validate quota errors immediately enter normal provider fallback handling.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    quota_error = FakeProviderError("quota exceeded", status_code=429)
    completions = FakeChatCompletions(content="", first_error=quota_error)
    fake_client = FakeOpenAiClient(completions=completions)
    monkeypatch.setattr(
        llm_client,
        "create_openai_compatible_client",
        lambda resolved_route: fake_client,
    )

    request = LlmRequest(
        route_name="triage_reasoning",
        prompt="Explain the failed partition.",
        response_model=StructuredIncidentSummary,
    )
    resolved_route = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    with pytest.raises(FakeProviderError, match="quota exceeded"):
        run_openai_compatible_route(
            request=request,
            resolved_route=resolved_route,
            started_monotonic=time.monotonic(),
        )

    assert len(completions.calls) == 1


def test_preferred_structured_output_preserves_plain_text_when_validation_fails() -> None:
    """
    Validate preferred mode degrades to sanitized prose when JSON is invalid.

    Returns:
        None.
    """
    request = LlmRequest(
        route_name="triage_reasoning",
        prompt="Explain the failed partition.",
        response_model=StructuredIncidentSummary,
    )
    resolved_route = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)
    response       = build_response(
        request=request,
        resolved_route=resolved_route,
        content="The partition has zero rows.",
        input_tokens=10,
        output_tokens=8,
        used_heuristic=False,
        fallback_reason="",
        duration_ms=5,
    )

    assert response.content == "The partition has zero rows."
    assert response.structured_output is None
    assert response.metadata["structured_output_status"] == "validation_failed_plain_text"
    assert response.metadata["structured_output_error_type"] == "ValidationError"
    validation_errors = response.metadata["structured_output_validation_errors"]
    assert validation_errors[0]["type"] == "json_invalid"
    assert "The partition has zero rows" not in json.dumps(validation_errors)


def test_structured_request_uses_safe_heuristic_fallback_without_provider() -> None:
    """
    Validate structured requests remain usable when external providers are unavailable.

    Returns:
        None.
    """
    response = run_llm_task(
        route_name="triage_reasoning",
        prompt="Explain the failed partition.",
        context={"table_name": "dq.raw_orders", "dt": "2026-06-10"},
        config_path=CONFIG_PATH,
        force_heuristic=True,
        response_model=StructuredIncidentSummary,
    )

    assert response.used_heuristic is True
    assert response.structured_output is None
    assert response.metadata["structured_output_requested"] is True
    assert response.metadata["structured_output_status"] == "heuristic_fallback"


def test_sanitize_llm_content_removes_private_reasoning_and_preserves_answer() -> None:
    """
    Validate complete private reasoning blocks never reach user-facing content.

    Returns:
        None.
    """
    result = sanitize_llm_content(
        "<think>Inspect every internal possibility.</think>\n\n"
        "The raw orders partition is missing.\n\n\nReview the landing file first."
    )

    assert result.content == "The raw orders partition is missing.\n\nReview the landing file first."
    assert result.removed_closed_blocks == 1
    assert result.removed_unclosed_segments == 0
    assert result.removed_stray_tags == 0
    assert "think" not in result.content.lower()


def test_sanitize_llm_content_fails_closed_for_unclosed_reasoning() -> None:
    """
    Validate an unclosed private block is removed through end-of-response.

    Returns:
        None.
    """
    result = sanitize_llm_content(
        "The observed evidence is incomplete.\n<thinking>Private unfinished reasoning"
    )

    assert result.content == "The observed evidence is incomplete."
    assert result.removed_closed_blocks == 0
    assert result.removed_unclosed_segments == 1


def test_build_response_sanitizes_content_and_records_safe_metadata() -> None:
    """
    Validate normalized responses retain only removal counts, not private text.

    Returns:
        None.
    """
    request        = LlmRequest(prompt="Explain the alert.")
    resolved_route = resolve_route(route_name="evidence_summary", config_path=CONFIG_PATH)
    response       = build_response(
        request=request,
        resolved_route=resolved_route,
        content="<analysis>Private chain of thought.</analysis>\nThe partition has zero rows.",
        input_tokens=10,
        output_tokens=8,
        used_heuristic=False,
        fallback_reason="",
        duration_ms=5,
        metadata={"tool_name": "llm_router"},
    )

    assert response.content == "The partition has zero rows."
    assert response.metadata["content_sanitization"] == {
        "removed_closed_blocks": 1,
        "removed_unclosed_segments": 0,
        "removed_stray_tags": 0,
    }
    assert "Private chain of thought" not in json.dumps(response.metadata)


def test_build_response_rejects_reasoning_only_provider_output() -> None:
    """
    Validate reasoning-only provider output enters the configured fallback path.

    Returns:
        None.
    """
    request        = LlmRequest(prompt="Explain the alert.")
    resolved_route = resolve_route(route_name="evidence_summary", config_path=CONFIG_PATH)

    with pytest.raises(ValueError, match="no user-facing content"):
        build_response(
            request=request,
            resolved_route=resolved_route,
            content="<reasoning>Private text only.</reasoning>",
            input_tokens=10,
            output_tokens=8,
            used_heuristic=False,
            fallback_reason="",
            duration_ms=5,
        )


def test_gemini_route_falls_back_when_provider_keys_are_missing(monkeypatch) -> None:
    """
    Validate Gemini routes reach deterministic fallback when no funded provider is available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resolved = resolve_executable_route(route_name="cheap_summary", config_path=CONFIG_PATH)

    assert resolved.route_name == "evidence_summary"
    assert resolved.provider_name == "heuristic"
    assert resolved.use_heuristic is True


def test_external_llm_kill_switch_defaults_off_even_when_key_exists(monkeypatch) -> None:
    """
    Ensure provider credentials cannot activate billable calls by themselves.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("EXTERNAL_LLM_ENABLED", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    resolved = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    assert resolve_external_llm_enabled() is False
    assert resolved.use_heuristic is True
    assert resolved.fallback_reason == "external_llm_disabled"


def test_external_llm_kill_switch_blocks_direct_provider_execution(monkeypatch) -> None:
    """
    Ensure direct provider calls cannot bypass the route-level kill switch decision.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    resolved = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)
    request  = LlmRequest(
        route_name="triage_reasoning",
        prompt="Explain the synthetic alert.",
    )

    with pytest.raises(ExternalLlmExecutionDisabled, match="disabled by routing policy"):
        run_openai_compatible_route(
            request=request,
            resolved_route=resolved,
            started_monotonic=time.monotonic(),
        )


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_external_llm_kill_switch_accepts_explicit_true_values(monkeypatch, value: str) -> None:
    """
    Validate only explicit accepted values enable external-provider routing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        value: Accepted environment value under test.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", value)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    resolved = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    assert resolve_external_llm_enabled() is True
    assert resolved.use_heuristic is False


def test_external_llm_kill_switch_rejects_ambiguous_value(monkeypatch) -> None:
    """
    Fail closed when the external-provider switch is misspelled or ambiguous.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "enabled")

    with pytest.raises(ValueError, match="EXTERNAL_LLM_ENABLED must be one of"):
        resolve_external_llm_enabled()


def test_resolve_route_uses_heuristic_when_openai_key_is_missing(monkeypatch) -> None:
    """
    Validate OpenAI-compatible routes fall back when API keys are not configured.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resolved = resolve_route(route_name="triage_reasoning", config_path=CONFIG_PATH)

    assert resolved.provider_name == "openai"
    assert resolved.use_heuristic is True
    assert resolved.fallback_reason == "missing_api_key:OPENAI_API_KEY"


def test_resolve_executable_route_follows_fallback_to_heuristic(monkeypatch) -> None:
    """
    Validate fallback routing eventually resolves to the deterministic heuristic route.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    resolved = resolve_executable_route(route_name="low_confidence_rca", config_path=CONFIG_PATH)

    assert resolved.route_name == "evidence_summary"
    assert resolved.provider_name == "heuristic"
    assert resolved.use_heuristic is True


def test_run_llm_task_uses_no_llm_fallback_without_api_key(monkeypatch) -> None:
    """
    Validate routed LLM execution remains local when provider keys are missing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with supervisor_llm_budget_scope(
        max_model_calls=2,
        token_budget=10_000,
        estimated_cost_budget_usd=10.0,
        deadline_monotonic=time.monotonic() + 30,
    ) as ledger:
        response = run_llm_task(
            route_name="triage_reasoning",
            prompt="Summarize why row_count_positive failed.",
            context={
                "table_name": "dq.raw_orders",
                "dt": "2026-06-10",
                "observed_value": 0,
            },
            config_path=CONFIG_PATH,
        )
        budget_usage = ledger.snapshot(latency_ms=10)

    assert response.used_heuristic is True
    assert response.provider == "heuristic"
    assert "No external LLM was called" in response.content
    assert response.input_tokens > 0
    assert response.output_tokens > 0
    assert response.estimated_cost_usd == 0.0
    assert budget_usage.model_calls == 0
    assert budget_usage.tokens == 0
    assert budget_usage.estimated_cost_usd == 0.0


def test_runtime_provider_failures_follow_configured_fallbacks(monkeypatch) -> None:
    """
    Validate quota-like provider failures degrade through secondary and heuristic routes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    def raise_provider_error(*args, **kwargs):
        """
        Simulate a provider quota failure without making an external API request.

        Args:
            args: Positional provider call arguments.
            kwargs: Keyword provider call arguments.

        Raises:
            RuntimeError: Always raised to exercise runtime fallback behavior.
        """
        raise RuntimeError("simulated_provider_quota_failure")

    monkeypatch.setattr(llm_client, "run_openai_compatible_route", raise_provider_error)

    response = run_llm_task(
        route_name="cheap_summary",
        prompt="Explain the selected alert in plain language.",
        context={"alert_ref": "ALT-TEST01"},
        config_path=CONFIG_PATH,
    )

    failures = response.metadata["provider_failures"]

    assert response.used_heuristic is True
    assert response.provider == "heuristic"
    assert [failure["provider"] for failure in failures] == ["gemini", "openai"]
    assert response.fallback_reason == "provider_error:openai:RuntimeError"
    assert response.metadata["requested_route"] == "cheap_summary"
    assert response.metadata["executed_route"] == "evidence_summary"
    assert response.metadata["attempted_routes"] == ["cheap_summary", "openai_summary", "evidence_summary"]
    assert "No external LLM was called" in response.content


def test_supervised_provider_fallback_attempts_consume_model_call_budget(monkeypatch) -> None:
    """
    Ensure every failed external provider route retains one conservative reservation.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    completion_clients: list[FakeChatCompletions] = []

    def build_failing_client(resolved_route):
        """Create one independently failing provider client for each fallback route."""
        completions = FakeChatCompletions(
            content="",
            first_error=RuntimeError(
                f"simulated_{resolved_route.provider_name}_provider_failure"
            ),
        )
        completion_clients.append(completions)

        return FakeOpenAiClient(completions=completions)

    monkeypatch.setattr(
        llm_client,
        "create_openai_compatible_client",
        build_failing_client,
    )

    with supervisor_llm_budget_scope(
        max_model_calls=2,
        token_budget=10_000,
        estimated_cost_budget_usd=10.0,
        deadline_monotonic=time.monotonic() + 30,
    ) as ledger:
        response = run_llm_task(
            route_name="cheap_summary",
            prompt="Explain the selected alert in plain language.",
            context={"alert_ref": "ALT-TEST01"},
            config_path=CONFIG_PATH,
        )
        budget_usage = ledger.snapshot(latency_ms=20)

    assert response.used_heuristic is True
    assert sum(len(item.calls) for item in completion_clients) == 2
    assert budget_usage.model_calls == 2
    assert budget_usage.tokens > 0


def test_catalog_qa_provider_failure_does_not_retry_gemini(monkeypatch) -> None:
    """
    Validate catalog Q&A skips redundant Gemini retries after quota failure.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    def raise_provider_error(*args, **kwargs):
        """
        Simulate an unavailable provider without making an external API request.

        Args:
            args: Positional provider call arguments.
            kwargs: Keyword provider call arguments.

        Raises:
            RuntimeError: Always raised to exercise fallback routing.
        """
        raise RuntimeError("simulated_provider_quota_failure")

    monkeypatch.setattr(llm_client, "run_openai_compatible_route", raise_provider_error)

    response = run_llm_task(
        route_name="catalog_qa",
        prompt="Explain which trusted table should be used.",
        context={"table_name": "dq.fct_orders_daily"},
        config_path=CONFIG_PATH,
    )

    failures = response.metadata["provider_failures"]

    assert response.used_heuristic is True
    assert [failure["provider"] for failure in failures] == ["gemini", "openai"]
    assert response.metadata["attempted_routes"] == ["catalog_qa", "openai_summary", "evidence_summary"]


def test_write_llm_route_audit_event_persists_required_metadata() -> None:
    """
    Validate LLM route audit rows include provider, model, route, token, cost, and fallback metadata.

    Returns:
        None.
    """
    state    = build_triage_state()
    client   = FakeAuditClient()
    response = run_llm_task(
        route_name="triage_reasoning",
        prompt="Explain the failed order partition.",
        context={"alert_key": state.alert_key},
        agent_run_id=state.agent_run_id,
        config_path=CONFIG_PATH,
        force_heuristic=True,
    )

    write_llm_route_audit_event(
        client=client,
        response=response,
        agent_run_id=state.agent_run_id,
        alert_id=state.alert.alert_id,
        alert_key=state.alert.alert_key,
    )

    insert_call = client.inserts[0]
    audit_row   = dict(zip(AGENT_AUDIT_LOG_COLUMNS, insert_call["data"][0], strict=True))
    input_json  = json.loads(audit_row["input_json"])
    output_json = json.loads(audit_row["output_json"])

    assert insert_call["table"] == "dq.agent_audit_log"
    assert audit_row["action"] == "llm_route_completed"
    assert audit_row["tool_name"] == "llm_router"
    assert audit_row["status"] == "success"
    assert input_json == {"requested_route": "triage_reasoning", "force_heuristic": True}
    assert output_json["provider"] == "heuristic"
    assert output_json["model"] == "heuristic-v1"
    assert output_json["executed_route"] == "evidence_summary"
    assert output_json["input_tokens"] > 0
    assert output_json["output_tokens"] > 0
    assert output_json["estimated_cost_usd"] == 0.0
    assert output_json["fallback_reason"] == "forced_heuristic"
    assert output_json["structured_output_requested"] is False
    assert output_json["structured_output_status"] == ""
    assert output_json["structured_output_provider_fallback"] is False
    assert output_json["duration_ms"] >= 0


def test_env_example_contains_no_provider_secrets_or_qwen_config() -> None:
    """
    Validate tracked environment examples remain secret-free and exclude Qwen production config.

    Returns:
        None.
    """
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8-sig")
    values  = {}

    for line in content.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue

        name, value = line.split("=", 1)
        values[name] = value

    assert values["OPENAI_API_KEY"] == ""
    assert values["XAI_API_KEY"] == ""
    assert values["GEMINI_API_KEY"] == ""
    assert values["GROQ_API_KEY"] == ""
    assert values["EXTERNAL_LLM_ENABLED"] == "false"
    assert values["LLM_PROVIDER_MAX_RETRIES"] == "0"
    assert values["GEMINI_BASE_URL"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert values["OPENAI_MODEL"] == "gpt-5.6-luna"
    assert values["XAI_MODEL"] == "grok-4.6"
    assert values["GEMINI_MODEL"] == "gemini-3.5-flash-lite"
    assert values["GROQ_BASE_URL"] == "https://api.groq.com/openai/v1"
    assert values["GROQ_MODEL"] == "openai/gpt-oss-20b"
    assert "QWEN" not in content.upper()


def test_groq_route_is_optional_and_unavailable_safe(monkeypatch) -> None:
    """
    Validate Groq remains an explicit route and falls back without credentials.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "true")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    selected = resolve_route(route_name="groq_summary", config_path=CONFIG_PATH)
    fallback = resolve_executable_route(route_name="groq_summary", config_path=CONFIG_PATH)

    assert selected.provider_name == "groq"
    assert selected.model == "openai/gpt-oss-20b"
    assert selected.use_heuristic is True
    assert selected.fallback_reason == "missing_api_key:GROQ_API_KEY"
    assert fallback.route_name == "evidence_summary"
    assert fallback.provider_name == "heuristic"


def test_estimate_tokens_and_cost_are_deterministic() -> None:
    """
    Validate token and cost estimates are deterministic for local logging.

    Returns:
        None.
    """
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2

    cost = estimate_cost_usd(
        input_tokens=1000,
        output_tokens=500,
        input_cost_per_1m_tokens=0.40,
        output_cost_per_1m_tokens=1.60,
    )

    assert cost == 0.0012


def test_build_llm_report_narrative_uses_heuristic_without_api_key(monkeypatch) -> None:
    """
    Validate graph report narrative enrichment remains local without provider keys.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = build_llm_report_narrative(state=build_triage_state())

    assert response.used_heuristic is True
    assert response.provider == "heuristic"
    assert "No external LLM was called" in response.content


def test_build_llm_report_narrative_requests_strong_route_for_low_confidence(
    monkeypatch,
) -> None:
    """
    Validate low-confidence evidence selects the strong RCA route before provider fallback.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    state = build_triage_state()
    state.hypotheses = [
        hypothesis.model_copy(update={"confidence": 0.55})
        for hypothesis in state.hypotheses
    ]
    state.confidence_threshold = 0.70

    def capture_route(**kwargs):
        """
        Capture the graph-selected provider route without calling an external model.

        Args:
            **kwargs: Routed LLM task arguments.

        Returns:
            Lightweight sentinel containing the selected route.
        """
        captured.update(kwargs)

        return SimpleNamespace(route_name=kwargs["route_name"])

    monkeypatch.setattr("agent.graph.run_llm_task", capture_route)

    response = build_llm_report_narrative(state=state)

    assert response.route_name == "low_confidence_rca"
    assert captured["route_name"] == "low_confidence_rca"


def test_build_llm_report_narrative_requests_strong_route_for_high_complexity(
    monkeypatch,
) -> None:
    """
    Validate deterministic blast-radius facts select strong reasoning at high confidence.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    state = build_triage_state()
    state.evidence.append(
        EvidenceItem(
            evidence_type=EvidenceType.LINEAGE,
            tool_name="dbt_lineage",
            description="Direct downstream blast radius.",
            rows=[
                {
                    "parents": ["source.orders", "source.customers"],
                    "children": ["model.stg_orders", "model.fct_orders_daily"],
                }
            ],
            summary="Four directly related assets were found.",
        )
    )

    def capture_route(**kwargs):
        """
        Capture the selected route without calling an external provider.

        Args:
            **kwargs: Routed LLM task arguments.

        Returns:
            Lightweight sentinel containing the selected route.
        """
        captured.update(kwargs)

        return SimpleNamespace(route_name=kwargs["route_name"])

    monkeypatch.setattr("agent.graph.run_llm_task", capture_route)

    response = build_llm_report_narrative(state=state)

    assert state.top_hypothesis is not None
    assert state.top_hypothesis.confidence > state.confidence_threshold
    assert response.route_name == "low_confidence_rca"
    assert captured["route_name"] == "low_confidence_rca"


def test_llm_response_to_evidence_contains_cost_metadata(monkeypatch) -> None:
    """
    Validate LLM response evidence captures route, provider, and cost metadata.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    response = build_llm_report_narrative(state=build_triage_state())
    evidence = llm_response_to_evidence(response=response)

    assert evidence.tool_name == "llm_router"
    assert evidence.evidence_type == "note"
    assert evidence.rows[0]["provider"] == "heuristic"
    assert evidence.rows[0]["requested_route"] == "triage_reasoning"
    assert evidence.rows[0]["executed_route"] == "evidence_summary"
    assert evidence.rows[0]["duration_ms"] >= 0
    assert evidence.rows[0]["estimated_cost_usd"] == 0.0


def test_triage_report_retains_sanitized_llm_runtime_summary() -> None:
    """
    Validate report JSON and Markdown expose bounded route usage without raw prompts.

    Returns:
        None.
    """
    state = build_triage_state()
    state.evidence.extend(
        [
            EvidenceItem(
                evidence_type=EvidenceType.NOTE,
                tool_name="llm_router",
                description="Provider route metadata.",
                rows=[
                    {
                        "requested_route": "cheap_summary",
                        "executed_route": "cheap_summary",
                        "provider": "gemini",
                        "model": "gemini-3.5-flash-lite",
                        "used_heuristic": False,
                        "fallback_reason": "",
                        "input_tokens": 127,
                        "output_tokens": 37,
                        "estimated_cost_usd": 0.0001306,
                        "duration_ms": 1668,
                    }
                ],
                row_count=1,
                summary="Gemini route completed.",
            ),
            EvidenceItem(
                evidence_type=EvidenceType.NOTE,
                tool_name="llm_router",
                description="Fallback route metadata.",
                rows=[
                    {
                        "requested_route": "low_confidence_rca",
                        "executed_route": "evidence_summary",
                        "provider": "heuristic",
                        "model": "heuristic-v1",
                        "used_heuristic": True,
                        "fallback_reason": "external_llm_disabled",
                        "input_tokens": 40,
                        "output_tokens": 20,
                        "estimated_cost_usd": 0.0,
                        "duration_ms": 4,
                    }
                ],
                row_count=1,
                summary="Strong route used deterministic fallback.",
            ),
        ]
    )

    summary = build_llm_runtime_summary(state.evidence)
    report  = build_report_from_state(state=state)
    payload = report.model_dump(mode="json")

    assert summary.route_event_count == 2
    assert summary.requested_routes == ["cheap_summary", "low_confidence_rca"]
    assert summary.executed_routes == ["cheap_summary", "evidence_summary"]
    assert summary.providers == ["gemini", "heuristic"]
    assert summary.models == ["gemini-3.5-flash-lite", "heuristic-v1"]
    assert summary.external_model_used is True
    assert summary.heuristic_fallback_used is True
    assert summary.fallback_reasons == ["external_llm_disabled"]
    assert summary.input_tokens == 167
    assert summary.output_tokens == 57
    assert summary.estimated_cost_usd == 0.0001306
    assert summary.duration_ms == 1672
    assert payload["llm_runtime"]["providers"] == ["gemini", "heuristic"]
    assert "## LLM Runtime" in report.markdown_report
    assert "gemini-3.5-flash-lite" in report.markdown_report
    assert "prompt" not in payload["llm_runtime"]
