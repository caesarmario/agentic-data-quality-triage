####
## LLM Provider Smoke Test for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Literal, Sequence
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.llm.client import LlmResponse, run_llm_task
from agent.llm.config import load_model_routing_config
from agent.tools.audit_log import build_llm_route_audit_payload, write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
SMOKE_ACTION    = "llm_provider_smoke"
SMOKE_TOOL_NAME = "llm_router"

DEFAULT_SMOKE_ROUTE = "cheap_summary"
SMOKE_SYSTEM_PROMPT = (
    "You are validating a data reliability copilot route. "
    "Answer briefly using only the supplied synthetic context."
)
SMOKE_PROMPT = (
    "Explain in two short sentences why a zero row count for today's raw orders "
    "partition should be investigated before downstream dashboards are trusted."
)
SMOKE_CONTEXT = {
    "dataset": "orders",
    "table_name": "dq.raw_orders",
    "check_name": "row_count_positive",
    "observed_value": 0,
    "expected_value": ">= 1",
    "data_classification": "synthetic_non_sensitive",
}


# --- Defining Types
SmokeOutcome = Literal["heuristic", "external_provider", "fallback"]
SmokeStatus  = Literal["success", "failed"]


# --- Defining Data Models
class ProviderSmokeResult(BaseModel):
    """
    Sanitized result for one routed provider smoke test.

    Attributes:
        status: Smoke test status.
        outcome: Whether execution used heuristic, requested provider, or fallback.
        agent_run_id: Correlation UUID shared with ClickHouse audit events.
        requested_route: Route requested by the Airflow task.
        requested_provider: Provider configured for the requested route.
        requested_model: Model configured for the requested route.
        executed_route: Route that ultimately produced the response.
        executed_provider: Provider that ultimately produced the response.
        executed_model: Model that ultimately produced the response.
        require_provider: Whether fallback should fail this smoke run.
        force_heuristic: Whether deterministic fallback was explicitly forced.
        used_heuristic: Whether the final response came from local heuristic logic.
        fallback_reason: Sanitized reason for provider fallback.
        attempted_routes: Ordered route attempts recorded by the router.
        provider_failures: Sanitized provider failure metadata without raw responses.
        input_tokens: Input token count or estimate.
        output_tokens: Output token count or estimate.
        estimated_cost_usd: Estimated provider cost for the smoke call.
        duration_ms: Routed call duration in milliseconds.
        content_length: Length of sanitized response content.
        content_sha256: Stable hash proving non-empty response generation.
        content_preview: Short sanitized response preview for Airflow logs.
        structured_output_status: Structured-output status recorded by the router.
    """

    status: SmokeStatus
    outcome: SmokeOutcome
    agent_run_id: UUID
    requested_route: str
    requested_provider: str
    requested_model: str
    executed_route: str
    executed_provider: str
    executed_model: str
    require_provider: bool
    force_heuristic: bool
    used_heuristic: bool
    fallback_reason: str                         = ""
    attempted_routes: list[str]                  = Field(default_factory=list)
    provider_failures: list[dict[str, str]]       = Field(default_factory=list)
    input_tokens: int                            = 0
    output_tokens: int                           = 0
    estimated_cost_usd: float                    = 0.0
    duration_ms: int                             = 0
    content_length: int                          = 0
    content_sha256: str                          = ""
    content_preview: str                         = ""
    structured_output_status: str                = ""


# --- Defining Exceptions
class ProviderSmokeExecutionError(RuntimeError):
    """
    Represent a sanitized provider smoke execution failure.

    Raw provider exceptions are not exposed because they may include request or
    credential context that should not be retained in Airflow logs.
    """


class ProviderSmokeRequirementError(RuntimeError):
    """
    Represent a strict-provider mismatch after a usable fallback response.

    Attributes:
        result: Sanitized failed smoke result written to the audit log.
    """

    def __init__(self, message: str, result: ProviderSmokeResult) -> None:
        """
        Initialize a strict-provider requirement error.

        Args:
            message: Sanitized operator-facing failure message.
            result: Failed smoke result associated with the mismatch.

        Returns:
            None.
        """
        super().__init__(message)
        self.result = result


# --- Building Sanitized Results
def build_content_preview(content: str, limit: int = 240) -> str:
    """
    Build a compact single-line preview from already-sanitized LLM content.

    Args:
        content: Sanitized LLM response content.
        limit: Maximum preview length.

    Returns:
        Compact response preview.
    """
    compact = " ".join(content.split())

    if len(compact) <= limit:
        return compact

    return compact[: limit - 3].rstrip() + "..."


def classify_smoke_outcome(
    requested_provider: str,
    response: LlmResponse,
    force_heuristic: bool,
) -> SmokeOutcome:
    """
    Classify how the routed smoke response was produced.

    Args:
        requested_provider: Provider configured for the requested route.
        response: Normalized routed LLM response.
        force_heuristic: Whether heuristic execution was explicitly requested.

    Returns:
        Heuristic, external_provider, or fallback outcome.
    """
    if response.used_heuristic:
        if requested_provider == "heuristic" or force_heuristic:
            return "heuristic"

        return "fallback"

    if response.provider == requested_provider:
        return "external_provider"

    return "fallback"


def build_smoke_result(
    response: LlmResponse,
    requested_route: str,
    requested_provider: str,
    requested_model: str,
    require_provider: bool,
    force_heuristic: bool,
) -> ProviderSmokeResult:
    """
    Convert a normalized LLM response into a secret-safe smoke result.

    Args:
        response: Normalized routed LLM response.
        requested_route: Route requested by the operator.
        requested_provider: Provider configured for the requested route.
        requested_model: Model configured for the requested route.
        require_provider: Whether fallback is allowed.
        force_heuristic: Whether heuristic execution was forced.

    Returns:
        Sanitized provider smoke result.
    """
    metadata          = dict(response.metadata or {})
    content           = response.content.strip()
    provider_failures = [
        {
            "route_name": str(item.get("route_name") or ""),
            "provider": str(item.get("provider") or ""),
            "model": str(item.get("model") or ""),
            "error_type": str(item.get("error_type") or ""),
            "fallback_reason": str(item.get("fallback_reason") or ""),
        }
        for item in metadata.get("provider_failures", [])
        if isinstance(item, dict)
    ]

    return ProviderSmokeResult(
        status="success",
        outcome=classify_smoke_outcome(
            requested_provider=requested_provider,
            response=response,
            force_heuristic=force_heuristic,
        ),
        agent_run_id=response.agent_run_id,
        requested_route=requested_route,
        requested_provider=requested_provider,
        requested_model=requested_model,
        executed_route=response.route_name,
        executed_provider=response.provider,
        executed_model=response.model,
        require_provider=require_provider,
        force_heuristic=force_heuristic,
        used_heuristic=response.used_heuristic,
        fallback_reason=response.fallback_reason,
        attempted_routes=[str(item) for item in metadata.get("attempted_routes", [])],
        provider_failures=provider_failures,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        estimated_cost_usd=response.estimated_cost_usd,
        duration_ms=response.duration_ms,
        content_length=len(content),
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        content_preview=build_content_preview(content),
        structured_output_status=str(metadata.get("structured_output_status") or ""),
    )


def requested_provider_was_used(result: ProviderSmokeResult) -> bool:
    """
    Determine whether the configured provider satisfied a strict smoke run.

    Args:
        result: Sanitized provider smoke result.

    Returns:
        True when the requested provider produced the final response.
    """
    if result.requested_provider == "heuristic":
        return result.executed_provider == "heuristic" and result.used_heuristic

    return result.executed_provider == result.requested_provider and not result.used_heuristic


# --- Writing Audit Evidence
def write_smoke_audit_event(
    client: Any,
    result: ProviderSmokeResult,
) -> None:
    """
    Persist one sanitized provider smoke result to ClickHouse.

    Args:
        client: clickhouse-connect client instance.
        result: Sanitized smoke result.

    Returns:
        None.
    """
    write_agent_audit_event(
        client=client,
        action=SMOKE_ACTION,
        status=result.status,
        agent_run_id=result.agent_run_id,
        alert_key=f"llm-provider-smoke|{result.requested_route}|{result.requested_provider}",
        actor="airflow",
        tool_name=SMOKE_TOOL_NAME,
        duration_ms=result.duration_ms,
        input_payload={
            "requested_route": result.requested_route,
            "requested_provider": result.requested_provider,
            "require_provider": result.require_provider,
            "force_heuristic": result.force_heuristic,
            "context_classification": SMOKE_CONTEXT["data_classification"],
        },
        output_payload=result.model_dump(mode="json"),
    )


def write_failed_execution_audit_event(
    client: Any,
    agent_run_id: UUID,
    requested_route: str,
    requested_provider: str,
    require_provider: bool,
    force_heuristic: bool,
    error_type: str,
) -> None:
    """
    Persist a sanitized audit event when routing cannot return a usable response.

    Args:
        client: clickhouse-connect client instance.
        agent_run_id: Correlation UUID for the failed attempt.
        requested_route: Requested model route.
        requested_provider: Provider configured for that route.
        require_provider: Whether strict provider execution was required.
        force_heuristic: Whether heuristic execution was forced.
        error_type: Exception class name only, without raw provider text.

    Returns:
        None.
    """
    write_agent_audit_event(
        client=client,
        action=SMOKE_ACTION,
        status="failed",
        agent_run_id=agent_run_id,
        alert_key=f"llm-provider-smoke|{requested_route}|{requested_provider}",
        actor="airflow",
        tool_name=SMOKE_TOOL_NAME,
        input_payload={
            "requested_route": requested_route,
            "requested_provider": requested_provider,
            "require_provider": require_provider,
            "force_heuristic": force_heuristic,
            "context_classification": SMOKE_CONTEXT["data_classification"],
        },
        output_payload={"error_type": error_type},
        error_message=error_type,
    )


# --- Running Provider Smoke Tests
def run_provider_smoke(
    route_name: str,
    require_provider: bool = False,
    force_heuristic: bool = False,
    config_path: str | Path | None = None,
    client: Any | None = None,
    llm_runner: Callable[..., LlmResponse] = run_llm_task,
) -> ProviderSmokeResult:
    """
    Run one routed provider smoke test and persist sanitized audit evidence.

    Args:
        route_name: Route name from model_routing.yml.
        require_provider: Fail when fallback produces the final response.
        force_heuristic: Force deterministic no-LLM execution.
        config_path: Optional routing config path.
        client: Optional ClickHouse client override used by tests.
        llm_runner: Optional routed LLM callable override used by tests.

    Returns:
        Sanitized successful provider smoke result.

    Raises:
        ValueError: If the route is unknown or flags conflict.
        ProviderSmokeExecutionError: If no usable routed response is produced.
        ProviderSmokeRequirementError: If strict provider execution falls back.
    """
    config = load_model_routing_config(config_path=config_path)

    if route_name not in config.routes:
        raise ValueError(f"Unknown model route: {route_name}")

    route              = config.routes[route_name]
    requested_provider = route.provider
    requested_model    = route.model or config.providers[requested_provider].default_model
    agent_run_id       = uuid4()
    runtime_client     = client or build_clickhouse_client()

    if force_heuristic and require_provider and requested_provider != "heuristic":
        raise ValueError("--force-heuristic cannot satisfy --require-provider for an external provider route.")

    logger.info(
        "Starting LLM provider smoke | agent_run_id=%s route=%s requested_provider=%s requested_model=%s require_provider=%s force_heuristic=%s",
        agent_run_id,
        route_name,
        requested_provider,
        requested_model,
        require_provider,
        force_heuristic,
    )

    try:
        response = llm_runner(
            route_name=route_name,
            prompt=SMOKE_PROMPT,
            system_prompt=SMOKE_SYSTEM_PROMPT,
            context=SMOKE_CONTEXT,
            agent_run_id=agent_run_id,
            config_path=config_path,
            force_heuristic=force_heuristic,
        )

    except Exception as exc:
        error_type = type(exc).__name__

        logger.error(
            "LLM provider smoke failed before a usable response | agent_run_id=%s route=%s provider=%s error_type=%s",
            agent_run_id,
            route_name,
            requested_provider,
            error_type,
        )

        write_failed_execution_audit_event(
            client=runtime_client,
            agent_run_id=agent_run_id,
            requested_route=route_name,
            requested_provider=requested_provider,
            require_provider=require_provider,
            force_heuristic=force_heuristic,
            error_type=error_type,
        )

        raise ProviderSmokeExecutionError(
            f"LLM provider smoke failed; route={route_name}; provider={requested_provider}; error_type={error_type}."
        ) from None

    if not response.content.strip():
        write_failed_execution_audit_event(
            client=runtime_client,
            agent_run_id=agent_run_id,
            requested_route=route_name,
            requested_provider=requested_provider,
            require_provider=require_provider,
            force_heuristic=force_heuristic,
            error_type="EmptyProviderResponse",
        )

        raise ProviderSmokeExecutionError(
            f"LLM provider smoke returned empty content; route={route_name}; provider={requested_provider}."
        )

    result = build_smoke_result(
        response=response,
        requested_route=route_name,
        requested_provider=requested_provider,
        requested_model=requested_model,
        require_provider=require_provider,
        force_heuristic=force_heuristic,
    )

    if require_provider and not requested_provider_was_used(result):
        result.status = "failed"
        write_smoke_audit_event(client=runtime_client, result=result)

        logger.error(
            "Strict LLM provider smoke used fallback | agent_run_id=%s route=%s requested_provider=%s executed_provider=%s",
            result.agent_run_id,
            result.requested_route,
            result.requested_provider,
            result.executed_provider,
        )

        raise ProviderSmokeRequirementError(
            (
                "Required provider was not used; "
                f"route={result.requested_route}; requested_provider={result.requested_provider}; "
                f"executed_provider={result.executed_provider}."
            ),
            result=result,
        )

    write_smoke_audit_event(client=runtime_client, result=result)

    route_audit = build_llm_route_audit_payload(response)
    logger.info(
        "LLM provider smoke completed | agent_run_id=%s status=%s outcome=%s requested_provider=%s executed_provider=%s executed_model=%s duration_ms=%d estimated_cost_usd=%.8f fallback_reason=%s",
        result.agent_run_id,
        result.status,
        result.outcome,
        result.requested_provider,
        result.executed_provider,
        result.executed_model,
        result.duration_ms,
        result.estimated_cost_usd,
        route_audit.get("fallback_reason") or "none",
    )

    return result


# --- Building CLI
def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for provider smoke execution.

    Returns:
        Configured ArgumentParser with route and strictness controls.
    """
    route_names = tuple(load_model_routing_config().routes)
    parser      = argparse.ArgumentParser(
        description="Run one sanitized LLM provider route smoke test with ClickHouse audit evidence."
    )

    parser.add_argument("--route", default=DEFAULT_SMOKE_ROUTE, choices=route_names)
    parser.add_argument(
        "--require-provider",
        action="store_true",
        help="Fail when the configured provider is unavailable and routing falls back.",
    )
    parser.add_argument(
        "--force-heuristic",
        action="store_true",
        help="Force deterministic no-LLM execution for the local baseline.",
    )
    parser.add_argument("--config-path", default=None, help="Optional model routing config path.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """
    Parse CLI arguments and execute one provider smoke test.

    Args:
        argv: Optional argument sequence used by tests.

    Returns:
        Zero for success and two for a strict-provider mismatch.
    """
    args = build_parser().parse_args(argv)

    try:
        result = run_provider_smoke(
            route_name=args.route,
            require_provider=args.require_provider,
            force_heuristic=args.force_heuristic,
            config_path=args.config_path,
        )

    except ProviderSmokeRequirementError as exc:
        print(exc.result.model_dump_json(indent=2))

        return 2

    print(result.model_dump_json(indent=2))

    return 0


# --- Running CLI Entrypoint
if __name__ == "__main__":
    raise SystemExit(main())
