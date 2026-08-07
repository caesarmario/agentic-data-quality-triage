####
## LLM Costing Utilities for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import sys
from pathlib import Path


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
APPROX_CHARS_PER_TOKEN = 4


# --- Defining Functions
def estimate_tokens(text: str) -> int:
    """
    Estimate token count from text without requiring tokenizer dependencies.

    Args:
        text: Text to estimate.

    Returns:
        Approximate token count with a minimum of one token for non-empty text.
    """
    if not text:
        return 0

    token_count = max(1, int((len(text) + APPROX_CHARS_PER_TOKEN - 1) / APPROX_CHARS_PER_TOKEN))

    logger.debug("Estimated tokens | chars=%d tokens=%d", len(text), token_count)

    return token_count


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_1m_tokens: float,
    output_cost_per_1m_tokens: float,
) -> float:
    """
    Estimate LLM cost in USD from token counts and route pricing.

    Args:
        input_tokens: Number of prompt/input tokens.
        output_tokens: Number of completion/output tokens.
        input_cost_per_1m_tokens: Input token price per one million tokens.
        output_cost_per_1m_tokens: Output token price per one million tokens.

    Returns:
        Estimated cost in USD rounded to eight decimal places.
    """
    input_cost  = (max(0, input_tokens) / 1_000_000) * input_cost_per_1m_tokens
    output_cost = (max(0, output_tokens) / 1_000_000) * output_cost_per_1m_tokens
    total_cost  = round(input_cost + output_cost, 8)

    logger.info(
        "Estimated LLM cost | input_tokens=%d output_tokens=%d cost_usd=%.8f",
        input_tokens,
        output_tokens,
        total_cost,
    )

    return total_cost
