####
## Heuristic LLM Fallback for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
MAX_CONTEXT_ITEMS = 5
MAX_TEXT_CHARS    = 1800


# --- Defining Functions
def compact_json(value: Any, max_chars: int = MAX_TEXT_CHARS) -> str:
    """
    Serialize context into a compact bounded JSON string.

    Args:
        value: JSON-like context value.
        max_chars: Maximum characters to return.

    Returns:
        Bounded JSON string.
    """
    text = json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)

    if len(text) <= max_chars:
        return text

    return text[: max_chars - 3] + "..."


def extract_context_lines(context: dict[str, Any] | None) -> list[str]:
    """
    Build readable lines from structured context for local fallback output.

    Args:
        context: Optional structured context dictionary.

    Returns:
        List of compact context lines.
    """
    if not context:
        return []

    lines = []

    for key, value in list(context.items())[:MAX_CONTEXT_ITEMS]:
        lines.append(f"- {key}: {compact_json(value, max_chars=320)}")

    return lines


def build_heuristic_response(
    route_name: str,
    prompt: str,
    context: dict[str, Any] | None = None,
) -> str:
    """
    Build a deterministic no-LLM response for local demos and missing API keys.

    Args:
        route_name: Model routing task name.
        prompt: User or system prompt text.
        context: Optional structured context.

    Returns:
        Deterministic fallback response.
    """
    logger.info("Building heuristic LLM fallback response | route=%s", route_name)

    prompt_summary = " ".join(prompt.strip().split())[:MAX_TEXT_CHARS]
    context_lines  = extract_context_lines(context=context)
    lines          = [
        "Heuristic fallback response",
        "",
        f"Route: {route_name}",
        "",
        "Summary:",
        prompt_summary or "No prompt was provided.",
    ]

    if context_lines:
        lines.extend(["", "Context reviewed:", *context_lines])

    lines.extend(
        [
            "",
            "Limitations:",
            "- No external LLM was called.",
            "- Use this output for local demo continuity, not as final RCA wording.",
            "- Deterministic tools and stored evidence remain the source of truth.",
        ]
    )

    return "\n".join(lines).strip()
