####
## Shared LLM Observability Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from pipelines.common.logging import logger


# --- Defining Models
class LlmRouteObservation(BaseModel):
    """
    Sanitized model-routing observation safe for operator-facing surfaces.

    Attributes:
        agent_run_id: Agent run correlation identifier.
        timestamp: Audit event timestamp serialized as text.
        event_action: Audit action such as llm_route_completed or llm_route_failed.
        status: Audit event status.
        requested_route: Route requested by the calling workflow.
        executed_route: Route that produced the final response.
        attempted_routes: Ordered routes attempted during fallback.
        provider: Final provider name.
        model: Final model name.
        input_tokens: Input token count or estimate.
        output_tokens: Output token count or estimate.
        estimated_cost_usd: Estimated route cost in USD.
        duration_ms: End-to-end route duration in milliseconds.
        used_heuristic: Whether deterministic fallback produced the response.
        fallback_reason: Machine-readable fallback reason.
        provider_failures: Sanitized provider failure metadata.
    """

    agent_run_id: str                         = ""
    timestamp: str                            = ""
    event_action: str                         = "llm_route_completed"
    status: str                               = "success"
    requested_route: str                      = ""
    executed_route: str                       = ""
    attempted_routes: list[str]               = Field(default_factory=list)
    provider: str                             = ""
    model: str                                = ""
    input_tokens: int                         = 0
    output_tokens: int                        = 0
    estimated_cost_usd: float                 = 0.0
    duration_ms: int                          = 0
    used_heuristic: bool                      = False
    fallback_reason: str                      = ""
    provider_failures: list[dict[str, Any]]   = Field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        """
        Serialize the observation with derived operator-facing fields.

        Returns:
            Dictionary safe for API, Streamlit, and Discord presentation.
        """
        payload = self.model_dump(mode="json")
        payload.update(
            {
                "runtime_mode": resolve_runtime_mode(self),
                "fallback_summary": humanize_fallback_reason(self.fallback_reason),
                "total_tokens": self.input_tokens + self.output_tokens,
                "estimated_cost_display": format_estimated_cost(self.estimated_cost_usd),
            }
        )

        return payload


# --- Defining Generic Helpers
def get_value(value: Any, field_name: str, default: Any = None) -> Any:
    """
    Read a field from a dictionary or model-like object.

    Args:
        value: Dictionary, Pydantic model, or arbitrary object.
        field_name: Field name to read.
        default: Value returned when the field is unavailable.

    Returns:
        Resolved field value or default.
    """
    if isinstance(value, dict):
        return value.get(field_name, default)

    return getattr(value, field_name, default)


def parse_json_object(value: Any) -> dict[str, Any]:
    """
    Parse a JSON object while treating malformed or non-object values as empty.

    Args:
        value: JSON string, dictionary, or unsupported value.

    Returns:
        Parsed dictionary or an empty dictionary.
    """
    if isinstance(value, dict):
        return value

    if not isinstance(value, str) or not value.strip():
        return {}

    try:
        parsed = json.loads(value)

    except (TypeError, ValueError):
        logger.warning("Ignoring malformed LLM audit JSON | value_type=%s", type(value).__name__)
        return {}

    return parsed if isinstance(parsed, dict) else {}


def normalize_string_list(value: Any) -> list[str]:
    """
    Normalize a list-like value into non-empty strings.

    Args:
        value: Candidate list value.

    Returns:
        List containing only non-empty string representations.
    """
    if not isinstance(value, (list, tuple)):
        return []

    return [str(item).strip() for item in value if str(item).strip()]


# --- Defining Observation Builders
def build_llm_route_observation(
    payload: dict[str, Any],
    *,
    agent_run_id: Any = "",
    timestamp: Any = "",
    event_action: Any = "llm_route_completed",
    status: Any = "success",
    duration_ms: Any = None,
) -> LlmRouteObservation:
    """
    Build a typed observation from an allowlisted metadata payload.

    Args:
        payload: LLM routing metadata dictionary.
        agent_run_id: Optional audit correlation identifier.
        timestamp: Optional audit timestamp.
        event_action: Audit event action.
        status: Audit event status.
        duration_ms: Optional audit duration override.

    Returns:
        Sanitized LlmRouteObservation.
    """
    provider_failures = payload.get("provider_failures")

    if not isinstance(provider_failures, list):
        provider_failures = []

    return LlmRouteObservation(
        agent_run_id=str(agent_run_id or ""),
        timestamp=str(timestamp or ""),
        event_action=str(event_action or "llm_route_completed"),
        status=str(status or "success"),
        requested_route=str(payload.get("requested_route") or payload.get("route_name") or ""),
        executed_route=str(payload.get("executed_route") or payload.get("route_name") or ""),
        attempted_routes=normalize_string_list(payload.get("attempted_routes")),
        provider=str(payload.get("provider") or ""),
        model=str(payload.get("model") or ""),
        input_tokens=int(payload.get("input_tokens") or 0),
        output_tokens=int(payload.get("output_tokens") or 0),
        estimated_cost_usd=float(payload.get("estimated_cost_usd") or 0.0),
        duration_ms=int(duration_ms if duration_ms is not None else payload.get("duration_ms") or 0),
        used_heuristic=bool(payload.get("used_heuristic", False)),
        fallback_reason=str(payload.get("fallback_reason") or payload.get("error_type") or ""),
        provider_failures=[item for item in provider_failures if isinstance(item, dict)],
    )


def llm_route_from_audit_row(row: dict[str, Any]) -> LlmRouteObservation | None:
    """
    Extract one LLM route observation from an audit row.

    Args:
        row: Audit row containing action plus optional input/output JSON.

    Returns:
        Sanitized observation for LLM events, otherwise None.
    """
    action = str(row.get("action") or "")

    if action not in {"llm_route_completed", "llm_route_failed"}:
        return None

    input_payload  = parse_json_object(row.get("input_json"))
    output_payload = parse_json_object(row.get("output_json"))
    payload        = {**input_payload, **output_payload}

    return build_llm_route_observation(
        payload,
        agent_run_id=row.get("agent_run_id"),
        timestamp=row.get("ts"),
        event_action=action,
        status=row.get("status"),
        duration_ms=row.get("duration_ms"),
    )


def llm_route_from_report(report: Any) -> LlmRouteObservation | None:
    """
    Extract LLM route metadata from one persisted triage report.

    Args:
        report: TriageReport-like model or dictionary.

    Returns:
        Sanitized observation when llm_router evidence exists, otherwise None.
    """
    for evidence in list(get_value(report, "evidence", []) or []):
        if str(get_value(evidence, "tool_name", "")) != "llm_router":
            continue

        rows = list(get_value(evidence, "rows", []) or [])

        if rows and isinstance(rows[0], dict):
            return build_llm_route_observation(
                rows[0],
                agent_run_id=get_value(report, "agent_run_id", ""),
            )

    return None


def enrich_audit_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[LlmRouteObservation]]:
    """
    Remove raw JSON payloads and attach sanitized LLM metadata to audit rows.

    Args:
        rows: Raw ClickHouse audit rows.

    Returns:
        Public audit rows and extracted LLM route observations.
    """
    public_rows  = []
    observations = []

    for row in rows:
        observation = llm_route_from_audit_row(row)
        public_row  = {
            key: value
            for key, value in row.items()
            if key not in {"input_json", "output_json"}
        }

        if observation:
            public_row["llm_route"] = observation.to_public_dict()
            observations.append(observation)

        public_rows.append(public_row)

    return public_rows, observations


def latest_llm_route_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Find the latest enriched LLM route observation from ordered audit rows.

    Args:
        rows: Audit rows ordered newest first.

    Returns:
        Public LLM route dictionary or None.
    """
    for row in rows:
        route = row.get("llm_route")

        if isinstance(route, dict):
            return route

        observation = llm_route_from_audit_row(row)

        if observation:
            return observation.to_public_dict()

    return None


# --- Defining Presentation Helpers
def resolve_runtime_mode(observation: LlmRouteObservation) -> str:
    """
    Resolve a concise runtime mode label.

    Args:
        observation: Sanitized LLM route observation.

    Returns:
        external_model, heuristic_fallback, or failed.
    """
    if observation.status == "failed" or observation.event_action == "llm_route_failed":
        return "failed"

    if observation.used_heuristic:
        return "heuristic_fallback"

    return "external_model"


def humanize_fallback_reason(reason: str) -> str:
    """
    Convert a machine fallback reason into operator-friendly language.

    Args:
        reason: Machine-readable fallback reason.

    Returns:
        Human-readable fallback summary.
    """
    normalized = reason.strip()

    if not normalized:
        return ""

    if normalized.startswith("provider_error:"):
        _, provider, error_type = (normalized.split(":", 2) + ["unknown", "unknown"])[:3]

        if error_type == "RateLimitError":
            return f"{provider} was unavailable because its quota or credit limit was reached; deterministic fallback completed the response."

        return f"{provider} returned {error_type}; the configured fallback route completed the response."

    if normalized.startswith("missing_api_key:"):
        return "No provider key was available, so the deterministic fallback completed the response."

    if normalized in {"forced_heuristic", "heuristic_provider"}:
        return "The workflow intentionally used the deterministic no-LLM route."

    return normalized.replace("_", " ").strip().capitalize()


def format_estimated_cost(value: float) -> str:
    """
    Format estimated LLM cost for compact operator displays.

    Args:
        value: Estimated cost in USD.

    Returns:
        USD amount with practical precision.
    """
    return f"${max(0.0, float(value)):.6f}"

