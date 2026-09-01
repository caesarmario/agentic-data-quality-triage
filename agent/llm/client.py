####
## Provider-Agnostic LLM Client for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm.config import ResolvedRoute, load_model_routing_config, resolve_executable_route
from agent.llm.costing import estimate_cost_usd, estimate_tokens
from agent.llm.fallback import build_heuristic_response
from agent.llm.sanitization import sanitize_llm_content
from agent.llm.structured_output import (
    build_json_schema_response_format,
    build_plain_json_instruction,
    is_structured_output_unsupported_error,
    parse_structured_output,
    summarize_structured_output_error,
)
from agent.supervisor.budgets import (
    SupervisorLlmBudgetExceeded,
    active_supervisor_llm_budget,
)
from pipelines.common.logging import logger


# --- Defining Constants
TOOL_NAME = "llm_router"

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 30.0
DEFAULT_PROVIDER_MAX_RETRIES     = 0


# --- Defining Exceptions
class ExternalLlmExecutionDisabled(RuntimeError):
    """
    Reject direct external-provider execution when routing policy selected fallback.

    This fail-closed boundary prevents callers from bypassing the global kill switch
    by invoking the provider implementation directly with a non-executable route.
    """


# --- Defining Classes
class LlmRequest(BaseModel):
    """
    Provider-agnostic LLM request payload.

    Attributes:
        route_name: Model route name from configs/agent/model_routing.yml.
        prompt: User/task prompt.
        system_prompt: Optional system prompt.
        context: Optional structured evidence/context payload.
        agent_run_id: Optional agent run UUID for logging correlation.
        force_heuristic: Force deterministic no-LLM fallback.
        response_model: Optional Pydantic contract for preferred structured output.
        response_schema_name: Optional provider-facing JSON schema name.
    """

    route_name: str                    = "evidence_summary"
    prompt: str
    system_prompt: str                 = ""
    context: dict[str, Any]            = Field(default_factory=dict)
    agent_run_id: UUID                 = Field(default_factory=uuid4)
    force_heuristic: bool                  = False
    response_model: type[BaseModel] | None = Field(default=None, exclude=True)
    response_schema_name: str              = ""


class LlmResponse(BaseModel):
    """
    Provider-agnostic LLM response payload.

    Attributes:
        agent_run_id: Agent run UUID used for correlation.
        route_name: Executed route name.
        provider: Provider key such as heuristic, gemini, openai, or xai.
        model: Model name used by the route.
        content: Sanitized response text or canonical validated JSON.
        structured_output: Optional JSON-compatible payload validated by the requested model.
        input_tokens: Prompt token count or estimate.
        output_tokens: Completion token count or estimate.
        estimated_cost_usd: Estimated cost in USD.
        used_heuristic: Whether local fallback was used.
        fallback_reason: Reason fallback was used.
        duration_ms: Execution duration in milliseconds.
        metadata: Additional provider metadata.
    """

    agent_run_id: UUID
    route_name: str
    provider: str
    model: str
    content: str
    structured_output: dict[str, Any] | None = None
    input_tokens: int                  = 0
    output_tokens: int                 = 0
    estimated_cost_usd: float          = 0.0
    used_heuristic: bool               = False
    fallback_reason: str               = ""
    duration_ms: int                   = 0
    metadata: dict[str, Any]           = Field(default_factory=dict)


# --- Defining Functions
def build_messages(request: LlmRequest) -> list[dict[str, str]]:
    """
    Build OpenAI-compatible chat messages from a request.

    Args:
        request: LLM request payload.

    Returns:
        List of chat messages.
    """
    messages = []

    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})

    context_text = ""

    if request.context:
        context_text = "\n\nContext JSON:\n" + json.dumps(request.context, ensure_ascii=True, default=str, indent=2)

    messages.append({"role": "user", "content": request.prompt + context_text})

    return messages


def estimate_messages_input_tokens(messages: list[dict[str, str]]) -> int:
    """
    Estimate input tokens from the exact messages sent to a provider.

    Args:
        messages: OpenAI-compatible chat messages.

    Returns:
        Estimated token count across message roles and content.
    """
    serialized = json.dumps(messages, ensure_ascii=True, separators=(",", ":"))

    return estimate_tokens(serialized)


def estimate_request_input_tokens(request: LlmRequest) -> int:
    """
    Estimate input tokens for a request before or after provider execution.

    Args:
        request: LLM request payload.

    Returns:
        Estimated prompt token count.
    """
    text = request.system_prompt + "\n" + request.prompt + "\n" + json.dumps(request.context, ensure_ascii=True, default=str)

    return estimate_tokens(text)


def build_response(
    request: LlmRequest,
    resolved_route: ResolvedRoute,
    content: str,
    input_tokens: int,
    output_tokens: int,
    used_heuristic: bool,
    fallback_reason: str,
    duration_ms: int,
    metadata: dict[str, Any] | None = None,
) -> LlmResponse:
    """
    Build a normalized LLM response with token and cost estimates.

    Args:
        request: Original LLM request.
        resolved_route: Resolved model route.
        content: Response text.
        input_tokens: Input token count or estimate.
        output_tokens: Output token count or estimate.
        used_heuristic: Whether local fallback was used.
        fallback_reason: Reason fallback was used.
        duration_ms: Execution duration in milliseconds.
        metadata: Optional provider metadata.

    Returns:
        LlmResponse object with sanitized user-facing content.

    Raises:
        ValueError: If private reasoning removal leaves no user-facing answer.
    """
    sanitized_content = sanitize_llm_content(content=content)

    if not sanitized_content.content:
        raise ValueError("LLM response has no user-facing content after private reasoning sanitization.")

    response_metadata  = dict(metadata or {})
    normalized_content = sanitized_content.content
    structured_output  = None

    if sanitized_content.removed_item_count:
        response_metadata["content_sanitization"] = sanitized_content.audit_metadata()

    if request.response_model:
        structured_mode = resolved_route.route.structured_output_mode
        response_metadata["structured_output_mode"] = structured_mode

        if used_heuristic:
            response_metadata["structured_output_status"] = "heuristic_fallback"

        elif structured_mode == "preferred":
            try:
                parsed_output      = parse_structured_output(
                    content=normalized_content,
                    response_model=request.response_model,
                )
                normalized_content = parsed_output.content
                structured_output  = parsed_output.data
                response_metadata["structured_output_status"] = "validated"

            except Exception as exc:
                # Preferred mode keeps usable sanitized prose when provider JSON is invalid.
                validation_errors = summarize_structured_output_error(exc)
                response_metadata["structured_output_status"] = "validation_failed_plain_text"
                response_metadata["structured_output_error_type"] = type(exc).__name__
                response_metadata["structured_output_validation_errors"] = validation_errors
                logger.warning(
                    "Structured LLM output validation failed; preserving sanitized plain text | route=%s provider=%s model=%s error_type=%s validation_errors=%s",
                    resolved_route.route_name,
                    resolved_route.provider_name,
                    resolved_route.model,
                    type(exc).__name__,
                    validation_errors,
                )

        else:
            response_metadata["structured_output_status"] = "disabled_by_route"

    estimated_cost = estimate_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost_per_1m_tokens=resolved_route.route.input_cost_per_1m_tokens,
        output_cost_per_1m_tokens=resolved_route.route.output_cost_per_1m_tokens,
    )

    response = LlmResponse(
        agent_run_id=request.agent_run_id,
        route_name=resolved_route.route_name,
        provider=resolved_route.provider_name,
        model=resolved_route.model,
        content=normalized_content,
        structured_output=structured_output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimated_cost,
        used_heuristic=used_heuristic,
        fallback_reason=fallback_reason,
        duration_ms=duration_ms,
        metadata=response_metadata,
    )

    logger.info(
        "LLM route completed | agent_run_id=%s route=%s provider=%s model=%s heuristic=%s input_tokens=%d output_tokens=%d cost_usd=%.8f",
        response.agent_run_id,
        response.route_name,
        response.provider,
        response.model,
        response.used_heuristic,
        response.input_tokens,
        response.output_tokens,
        response.estimated_cost_usd,
    )

    return response


def run_heuristic_route(request: LlmRequest, resolved_route: ResolvedRoute, started_monotonic: float) -> LlmResponse:
    """
    Execute a deterministic no-LLM fallback route.

    Args:
        request: LLM request payload.
        resolved_route: Resolved model route.
        started_monotonic: Monotonic start time.

    Returns:
        Normalized LlmResponse.
    """
    content       = build_heuristic_response(route_name=resolved_route.route_name, prompt=request.prompt, context=request.context)
    input_tokens  = estimate_request_input_tokens(request=request)
    output_tokens = estimate_tokens(content)
    duration_ms   = int((time.monotonic() - started_monotonic) * 1000)

    return build_response(
        request=request,
        resolved_route=resolved_route,
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        used_heuristic=True,
        fallback_reason=resolved_route.fallback_reason or "heuristic_route",
        duration_ms=duration_ms,
        metadata={"tool_name": TOOL_NAME},
    )


def resolve_provider_timeout_seconds() -> float:
    """
    Resolve the bounded HTTP timeout used by external LLM providers.

    Returns:
        Provider request timeout in seconds.

    Raises:
        ValueError: If LLM_PROVIDER_TIMEOUT_SECONDS is outside the safe range.
    """
    timeout_seconds = float(
        os.getenv("LLM_PROVIDER_TIMEOUT_SECONDS", str(DEFAULT_PROVIDER_TIMEOUT_SECONDS))
    )

    if not 1.0 <= timeout_seconds <= 120.0:
        raise ValueError("LLM_PROVIDER_TIMEOUT_SECONDS must be between 1 and 120 seconds.")

    return timeout_seconds


def resolve_provider_max_retries() -> int:
    """
    Resolve the bounded SDK retry count used by external LLM providers.

    Returns:
        Maximum provider SDK retry attempts.

    Raises:
        ValueError: If LLM_PROVIDER_MAX_RETRIES is outside the safe range.
    """
    max_retries = int(os.getenv("LLM_PROVIDER_MAX_RETRIES", str(DEFAULT_PROVIDER_MAX_RETRIES)))

    if not 0 <= max_retries <= 5:
        raise ValueError("LLM_PROVIDER_MAX_RETRIES must be between 0 and 5.")

    return max_retries


def create_openai_compatible_client(resolved_route: ResolvedRoute) -> Any:
    """
    Create an OpenAI-compatible client without exposing provider credentials.

    Args:
        resolved_route: Route containing provider API key and optional base URL.

    Returns:
        OpenAI client configured for the resolved provider.
    """
    # Import lazily so local no-LLM demos do not initialize provider clients.
    from openai import OpenAI

    supervised_budget = active_supervisor_llm_budget()
    timeout_seconds   = resolve_provider_timeout_seconds()

    if supervised_budget:
        remaining_ms = supervised_budget.remaining_latency_ms()

        if remaining_ms < 1_000:
            raise SupervisorLlmBudgetExceeded("latency_ms_budget_exceeded")

        timeout_seconds = min(timeout_seconds, remaining_ms / 1_000)

    client_kwargs = {
        "api_key": resolved_route.api_key,
        "timeout": timeout_seconds,
        # Hidden SDK retries cannot be counted reliably by the supervisor ledger.
        "max_retries": 0 if supervised_budget else resolve_provider_max_retries(),
    }

    if resolved_route.base_url:
        client_kwargs["base_url"] = resolved_route.base_url

    return OpenAI(**client_kwargs)


def reserve_supervised_provider_call(
    messages: list[dict[str, str]],
    resolved_route: ResolvedRoute,
) -> UUID | None:
    """
    Reserve one explicit external provider call under supervisor policy.

    Args:
        messages: Exact messages sent to the provider.
        resolved_route: Provider route containing output and costing limits.

    Returns:
        Reservation UUID when supervised, otherwise None.

    Raises:
        SupervisorLlmBudgetExceeded: If a supervised call exceeds its budget.
    """
    ledger = active_supervisor_llm_budget()

    if ledger is None:
        return None

    projected_input_tokens = estimate_messages_input_tokens(messages=messages)
    projected_tokens       = (
        projected_input_tokens + resolved_route.route.max_output_tokens
    )
    projected_cost = estimate_cost_usd(
        input_tokens=projected_input_tokens,
        output_tokens=resolved_route.route.max_output_tokens,
        input_cost_per_1m_tokens=resolved_route.route.input_cost_per_1m_tokens,
        output_cost_per_1m_tokens=resolved_route.route.output_cost_per_1m_tokens,
    )

    return ledger.reserve_model_call(
        projected_tokens=projected_tokens,
        projected_cost_usd=projected_cost,
    )


def reconcile_supervised_provider_call(
    reservation_id: UUID | None,
    response: LlmResponse,
) -> None:
    """
    Replace one supervised reservation with actual normalized provider usage.

    Args:
        reservation_id: Optional active reservation UUID.
        response: Normalized provider response containing usage and cost.

    Returns:
        None.
    """
    if reservation_id is None:
        return

    ledger = active_supervisor_llm_budget()

    if ledger is None:
        raise RuntimeError("Supervised LLM reservation lost its active budget context.")

    ledger.reconcile_model_call(
        reservation_id=reservation_id,
        actual_tokens=response.input_tokens + response.output_tokens,
        actual_cost_usd=response.estimated_cost_usd,
    )


def build_chat_completion_kwargs(
    request: LlmRequest,
    resolved_route: ResolvedRoute,
    messages: list[dict[str, str]],
    include_response_format: bool,
) -> dict[str, Any]:
    """
    Build provider chat completion arguments for plain or structured output.

    Args:
        request: Routed LLM request.
        resolved_route: Resolved provider and route policy.
        messages: Messages sent to the provider.
        include_response_format: Whether to include a strict JSON schema contract.

    Returns:
        Keyword arguments accepted by an OpenAI-compatible chat completion client.
    """
    completion_kwargs: dict[str, Any] = {
        "model": resolved_route.model,
        "messages": messages,
        "temperature": resolved_route.route.temperature,
        "max_tokens": resolved_route.route.max_output_tokens,
    }

    if include_response_format and request.response_model:
        completion_kwargs["response_format"] = build_json_schema_response_format(
            response_model=request.response_model,
            schema_name=request.response_schema_name,
        )

    return completion_kwargs


def add_structured_output_instruction(
    messages: list[dict[str, str]],
    request: LlmRequest,
) -> list[dict[str, str]]:
    """
    Append a JSON-only contract to the final user message for provider portability.

    Args:
        messages: Base chat messages built from prompt and evidence context.
        request: LLM request containing the optional Pydantic response model.

    Returns:
        Copied messages with a bounded JSON instruction when structured output is requested.
    """
    if not request.response_model or not messages:
        return list(messages)

    instruction = build_plain_json_instruction(
        response_model=request.response_model,
        schema_name=request.response_schema_name,
    )
    final_message = dict(messages[-1])
    final_message["content"] = f"{final_message['content']}\n\n{instruction}"

    return [*messages[:-1], final_message]


def run_openai_compatible_route(
    request: LlmRequest,
    resolved_route: ResolvedRoute,
    started_monotonic: float,
) -> LlmResponse:
    """
    Execute an OpenAI-compatible route with optional structured-output fallback.

    Args:
        request: LLM request payload.
        resolved_route: Resolved route with API key and optional base URL.
        started_monotonic: Monotonic start time.

    Returns:
        Normalized LlmResponse.

    Raises:
        ExternalLlmExecutionDisabled: If routing policy selected heuristic fallback.
        Exception: If provider execution fails outside the bounded schema compatibility retry.
    """
    if resolved_route.use_heuristic or resolved_route.provider.provider_type == "heuristic":
        raise ExternalLlmExecutionDisabled(
            "External LLM execution is disabled by routing policy. "
            "Use run_llm_task so the configured heuristic fallback remains auditable."
        )

    client               = create_openai_compatible_client(resolved_route=resolved_route)
    base_messages        = build_messages(request=request)
    structured_requested = bool(
        request.response_model
        and resolved_route.route.structured_output_mode == "preferred"
    )
    provider_schema_fallback = False
    executed_messages        = (
        add_structured_output_instruction(messages=base_messages, request=request)
        if structured_requested
        else base_messages
    )
    completion_kwargs = build_chat_completion_kwargs(
        request=request,
        resolved_route=resolved_route,
        messages=executed_messages,
        include_response_format=structured_requested,
    )

    logger.info(
        "Executing OpenAI-compatible LLM route | agent_run_id=%s route=%s provider=%s model=%s base_url_set=%s structured_requested=%s",
        request.agent_run_id,
        resolved_route.route_name,
        resolved_route.provider_name,
        resolved_route.model,
        bool(resolved_route.base_url),
        structured_requested,
    )

    reservation_id = reserve_supervised_provider_call(
        messages=executed_messages,
        resolved_route=resolved_route,
    )

    try:
        completion = client.chat.completions.create(**completion_kwargs)

    except Exception as exc:
        if not structured_requested or not is_structured_output_unsupported_error(exc):
            raise

        # Retry once without response_format only when the provider explicitly
        # rejects that feature. Quota, auth, timeout, and server errors are not retried.
        provider_schema_fallback = True
        completion_kwargs        = build_chat_completion_kwargs(
            request=request,
            resolved_route=resolved_route,
            messages=executed_messages,
            include_response_format=False,
        )

        logger.warning(
            "Provider rejected json_schema; retrying once with plain JSON instruction | agent_run_id=%s route=%s provider=%s model=%s error_type=%s",
            request.agent_run_id,
            resolved_route.route_name,
            resolved_route.provider_name,
            resolved_route.model,
            type(exc).__name__,
        )
        reservation_id = reserve_supervised_provider_call(
            messages=executed_messages,
            resolved_route=resolved_route,
        )
        completion = client.chat.completions.create(**completion_kwargs)

    choice  = completion.choices[0]
    content = choice.message.content or ""
    usage   = getattr(completion, "usage", None)

    input_tokens       = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    output_tokens      = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    completion_details = getattr(usage, "completion_tokens_details", None) if usage else None
    reasoning_tokens   = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    finish_reason      = str(getattr(choice, "finish_reason", "") or "")

    if input_tokens == 0:
        input_tokens = estimate_messages_input_tokens(messages=executed_messages)

    if output_tokens == 0:
        output_tokens = estimate_tokens(content)

    duration_ms = int((time.monotonic() - started_monotonic) * 1000)

    response = build_response(
        request=request,
        resolved_route=resolved_route,
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        used_heuristic=False,
        fallback_reason="",
        duration_ms=duration_ms,
        metadata={
            "tool_name": TOOL_NAME,
            "structured_output_requested": structured_requested,
            "structured_output_provider_fallback": provider_schema_fallback,
            "finish_reason": finish_reason,
            "reasoning_tokens": reasoning_tokens,
        },
    )

    reconcile_supervised_provider_call(
        reservation_id=reservation_id,
        response=response,
    )

    return response


def finalize_routing_metadata(
    response: LlmResponse,
    request: LlmRequest,
    attempted_routes: list[str],
    provider_failures: list[dict[str, str]],
) -> LlmResponse:
    """
    Attach non-sensitive routing context to a normalized LLM response.

    Args:
        response: Executed LLM or heuristic response.
        request: Original routed request.
        attempted_routes: Ordered routes attempted during execution.
        provider_failures: Sanitized provider failure metadata.

    Returns:
        Response enriched with auditable routing metadata.
    """
    response.metadata.update(
        {
            "requested_route": request.route_name,
            "executed_route": response.route_name,
            "attempted_routes": attempted_routes,
            "force_heuristic": request.force_heuristic,
            "structured_output_requested": bool(request.response_model),
        }
    )

    if provider_failures:
        response.fallback_reason = provider_failures[-1]["fallback_reason"]
        response.metadata["provider_failures"] = provider_failures

    return response


def run_llm_task(
    route_name: str,
    prompt: str,
    system_prompt: str = "",
    context: dict[str, Any] | None = None,
    agent_run_id: UUID | str | None = None,
    config_path: str | Path | None = None,
    force_heuristic: bool = False,
    response_model: type[BaseModel] | None = None,
    response_schema_name: str = "",
) -> LlmResponse:
    """
    Execute a routed LLM task with provider fallback and cost logging.

    Args:
        route_name: Model route name from the routing config.
        prompt: User/task prompt.
        system_prompt: Optional system prompt.
        context: Optional structured context.
        agent_run_id: Optional agent run UUID for correlation.
        config_path: Optional routing config path.
        force_heuristic: Force deterministic no-LLM fallback.
        response_model: Optional Pydantic model for preferred structured output.
        response_schema_name: Optional provider-facing JSON schema name.

    Returns:
        Normalized LlmResponse.
    """
    request = LlmRequest(
        route_name=route_name,
        prompt=prompt,
        system_prompt=system_prompt,
        context=context or {},
        agent_run_id=UUID(str(agent_run_id)) if agent_run_id else uuid4(),
        force_heuristic=force_heuristic,
        response_model=response_model,
        response_schema_name=response_schema_name,
    )
    config            = load_model_routing_config(config_path=config_path)
    started_monotonic = time.monotonic()
    selected_route    = request.route_name
    attempted_routes  = []
    provider_failures = []

    for _ in range(len(config.routes) + 1):
        resolved_route = resolve_executable_route(
            route_name=selected_route,
            config=config,
            force_heuristic=request.force_heuristic,
        )

        if resolved_route.route_name in attempted_routes:
            raise RuntimeError(f"Runtime model fallback loop detected: {resolved_route.route_name}")

        attempted_routes.append(resolved_route.route_name)

        if resolved_route.use_heuristic or resolved_route.provider.provider_type == "heuristic":
            response = run_heuristic_route(
                request=request,
                resolved_route=resolved_route,
                started_monotonic=started_monotonic,
            )

            return finalize_routing_metadata(
                response=response,
                request=request,
                attempted_routes=attempted_routes,
                provider_failures=provider_failures,
            )

        try:
            response = run_openai_compatible_route(
                request=request,
                resolved_route=resolved_route,
                started_monotonic=started_monotonic,
            )

            return finalize_routing_metadata(
                response=response,
                request=request,
                attempted_routes=attempted_routes,
                provider_failures=provider_failures,
            )

        except Exception as exc:
            # Provider quota, billing, authentication, or availability failures must
            # degrade safely instead of breaking Discord and UI copilot requests.
            error_type      = type(exc).__name__
            fallback_reason = f"provider_error:{resolved_route.provider_name}:{error_type}"
            fallback_route  = resolved_route.route.fallback_route

            provider_failures.append(
                {
                    "route_name": resolved_route.route_name,
                    "provider": resolved_route.provider_name,
                    "model": resolved_route.model,
                    "error_type": error_type,
                    "fallback_reason": fallback_reason,
                }
            )

            logger.warning(
                "LLM provider route failed; applying configured fallback | agent_run_id=%s route=%s provider=%s model=%s error_type=%s fallback_route=%s",
                request.agent_run_id,
                resolved_route.route_name,
                resolved_route.provider_name,
                resolved_route.model,
                error_type,
                fallback_route or "none",
            )

            if not fallback_route or fallback_route == resolved_route.route_name:
                raise RuntimeError(
                    f"No safe runtime fallback is configured for model route: {resolved_route.route_name}"
                ) from exc

            selected_route = fallback_route

    raise RuntimeError(f"Runtime model fallback exceeded configured routes: {request.route_name}")


def build_parser() -> argparse.ArgumentParser:
    """
    Build CLI parser for routed LLM smoke tests.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run one provider-agnostic LLM route.")

    parser.add_argument("--route", default="evidence_summary", help="Model route name.")
    parser.add_argument("--prompt", required=True, help="Prompt text.")
    parser.add_argument("--system-prompt", default="", help="Optional system prompt.")
    parser.add_argument("--config-path", default=None, help="Optional model routing config path.")
    parser.add_argument("--force-heuristic", action="store_true", help="Force local heuristic fallback.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and execute one routed LLM task.

    Returns:
        None.
    """
    parser   = build_parser()
    args     = parser.parse_args()
    response = run_llm_task(
        route_name=args.route,
        prompt=args.prompt,
        system_prompt=args.system_prompt,
        config_path=args.config_path,
        force_heuristic=args.force_heuristic,
    )

    print(response.model_dump_json(indent=2))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()

