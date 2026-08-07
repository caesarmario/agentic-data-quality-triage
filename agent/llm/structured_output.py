####
## Structured LLM Output Helpers for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from pipelines.common.logging import logger


# --- Defining Structured Output Constants
MAX_SCHEMA_NAME_LENGTH = 64
SCHEMA_ERROR_STATUS_CODES = {400, 404, 422}
SCHEMA_ERROR_MARKERS = (
    "response_format",
    "json_schema",
    "json schema",
    "structured output",
)
SCHEMA_UNSUPPORTED_MARKERS = (
    "invalid",
    "not allowed",
    "not support",
    "unsupported",
    "unknown",
)
JSON_CODE_FENCE_PATTERN = re.compile(
    r"^\s*```(?:json)?\s*(?P<payload>.*?)\s*```\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)
SCHEMA_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


# --- Defining Models
class ParsedStructuredOutput(BaseModel):
    """
    Represent validated structured output and its canonical JSON text.

    Attributes:
        content: Canonical JSON string safe for persistence and display.
        data: JSON-compatible dictionary validated by the requested Pydantic model.
    """

    content: str
    data: dict[str, Any]


# --- Defining Schema Helpers
def normalize_schema_name(name: str) -> str:
    """
    Normalize a provider-safe JSON schema name.

    Args:
        name: Requested schema or Pydantic model name.

    Returns:
        Non-empty schema identifier limited to provider-safe characters and length.
    """
    normalized = SCHEMA_NAME_PATTERN.sub("_", name.strip()).strip("_-")

    return (normalized or "agent_response")[:MAX_SCHEMA_NAME_LENGTH]


def build_json_schema_response_format(
    response_model: type[BaseModel],
    schema_name: str = "",
) -> dict[str, Any]:
    """
    Build an OpenAI-compatible JSON schema response format.

    Args:
        response_model: Pydantic model used as the output contract.
        schema_name: Optional provider-facing schema identifier.

    Returns:
        Chat completion response_format payload.
    """
    normalized_name = normalize_schema_name(schema_name or response_model.__name__)
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": normalized_name,
            "strict": False,
            "schema": response_model.model_json_schema(),
        },
    }

    logger.info(
        "Built structured output response format | schema_name=%s model=%s",
        normalized_name,
        response_model.__name__,
    )

    return response_format


def build_plain_json_instruction(
    response_model: type[BaseModel],
    schema_name: str = "",
) -> str:
    """
    Build a bounded JSON-only instruction for providers without json_schema support.

    Args:
        response_model: Pydantic model used as the output contract.
        schema_name: Optional logical schema identifier.

    Returns:
        Plain-text instruction containing the required JSON schema.
    """
    normalized_name = normalize_schema_name(schema_name or response_model.__name__)
    schema_json     = json.dumps(response_model.model_json_schema(), ensure_ascii=True, separators=(",", ":"))

    return (
        f"Return only valid JSON for schema `{normalized_name}`. "
        "Do not add Markdown fences, commentary, or private reasoning. "
        f"The JSON must match this schema: {schema_json}"
    )


def is_structured_output_unsupported_error(exc: Exception) -> bool:
    """
    Identify provider errors that justify one plain-text JSON retry.

    Authentication, quota, timeout, and server failures intentionally return
    False so the normal provider fallback handles them without duplicate calls.

    Args:
        exc: Provider exception raised by the OpenAI-compatible client.

    Returns:
        True only for 400/404/422 errors that explicitly reference an unsupported schema feature.
    """
    status_code = getattr(exc, "status_code", None)
    message     = str(exc).lower()

    if status_code not in SCHEMA_ERROR_STATUS_CODES:
        return False

    references_schema = any(marker in message for marker in SCHEMA_ERROR_MARKERS)
    is_unsupported    = any(marker in message for marker in SCHEMA_UNSUPPORTED_MARKERS)

    return references_schema and is_unsupported


def extract_json_payload(content: str) -> str:
    """
    Extract JSON from an optional Markdown code fence.

    Args:
        content: Sanitized model response text.

    Returns:
        Raw JSON payload ready for Pydantic validation.
    """
    match = JSON_CODE_FENCE_PATTERN.match(content)

    return match.group("payload").strip() if match else content.strip()


def summarize_structured_output_error(exc: Exception) -> list[dict[str, object]]:
    """
    Build safe validation diagnostics without retaining model response values.

    Args:
        exc: Pydantic validation or JSON parsing exception.

    Returns:
        Bounded error dictionaries containing only location, type, and message.
    """
    errors_method = getattr(exc, "errors", None)

    if not callable(errors_method):
        return [
            {
                "location": [],
                "type": type(exc).__name__,
                "message": "Structured output could not be validated.",
            }
        ]

    diagnostics = []

    for item in errors_method(include_input=False, include_url=False)[:10]:
        diagnostics.append(
            {
                "location": [str(part) for part in item.get("loc", ())],
                "type": str(item.get("type") or type(exc).__name__),
                "message": str(item.get("msg") or "Validation failed."),
            }
        )

    return diagnostics


def parse_structured_output(
    content: str,
    response_model: type[BaseModel],
) -> ParsedStructuredOutput:
    """
    Parse and validate provider output with a Pydantic contract.

    Args:
        content: Sanitized provider response text.
        response_model: Pydantic model defining the expected JSON structure.

    Returns:
        Canonical JSON content and validated JSON-compatible data.

    Raises:
        pydantic.ValidationError: If the JSON does not match the requested model.
        ValueError: If content is not valid JSON.
    """
    payload   = extract_json_payload(content=content)
    validated = response_model.model_validate_json(payload)
    data      = validated.model_dump(mode="json")
    canonical = json.dumps(data, ensure_ascii=True, indent=2)

    logger.info("Validated structured LLM output | model=%s", response_model.__name__)

    return ParsedStructuredOutput(content=canonical, data=data)
