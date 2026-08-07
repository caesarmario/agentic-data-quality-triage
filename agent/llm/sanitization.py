####
## LLM Output Sanitization for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from pipelines.common.logging import logger


# --- Defining Private Reasoning Patterns
PRIVATE_REASONING_TAGS = ("think", "thinking", "analysis", "reasoning")
PRIVATE_TAG_PATTERN    = "|".join(PRIVATE_REASONING_TAGS)

CLOSED_REASONING_BLOCK_PATTERN = re.compile(
    rf"<(?P<tag>{PRIVATE_TAG_PATTERN})\b[^>]*>.*?</(?P=tag)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
OPEN_REASONING_TAG_PATTERN = re.compile(
    rf"<(?:{PRIVATE_TAG_PATTERN})\b[^>]*>",
    flags=re.IGNORECASE,
)
CLOSE_REASONING_TAG_PATTERN = re.compile(
    rf"</(?:{PRIVATE_TAG_PATTERN})\s*>",
    flags=re.IGNORECASE,
)
EXCESSIVE_BLANK_LINES_PATTERN = re.compile(r"\n{3,}")


# --- Defining Models
class SanitizedLlmContent(BaseModel):
    """
    Represent cleaned LLM content and non-sensitive sanitization metadata.

    Attributes:
        content: User-facing model response after private reasoning removal.
        removed_closed_blocks: Number of complete private reasoning blocks removed.
        removed_unclosed_segments: Number of unclosed reasoning segments removed through end-of-response.
        removed_stray_tags: Number of remaining private closing tags removed.
    """

    content: str
    removed_closed_blocks: int      = Field(default=0, ge=0)
    removed_unclosed_segments: int  = Field(default=0, ge=0)
    removed_stray_tags: int         = Field(default=0, ge=0)

    @property
    def removed_item_count(self) -> int:
        """
        Return the total number of private reasoning artifacts removed.

        Returns:
            Sum of removed reasoning blocks, segments, and stray tags.
        """
        return self.removed_closed_blocks + self.removed_unclosed_segments + self.removed_stray_tags

    def audit_metadata(self) -> dict[str, int]:
        """
        Build non-sensitive metadata for route audit and debugging.

        Returns:
            Counts describing sanitization without exposing removed reasoning text.
        """
        return {
            "removed_closed_blocks": self.removed_closed_blocks,
            "removed_unclosed_segments": self.removed_unclosed_segments,
            "removed_stray_tags": self.removed_stray_tags,
        }


# --- Defining Sanitization Helpers
def normalize_user_facing_whitespace(content: str) -> str:
    """
    Normalize trailing whitespace and excessive blank lines in model output.

    Args:
        content: LLM response after private reasoning blocks are removed.

    Returns:
        Trimmed user-facing text with at most one blank line between sections.
    """
    normalized_lines = [
        line.rstrip()
        for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    normalized       = "\n".join(normalized_lines)

    return EXCESSIVE_BLANK_LINES_PATTERN.sub("\n\n", normalized).strip()


def sanitize_llm_content(content: str | None) -> SanitizedLlmContent:
    """
    Remove private reasoning tags before LLM output reaches users or artifacts.

    Complete private blocks such as ``<think>...</think>`` are removed while
    preserving the final answer. An unclosed private block is removed from its
    opening tag through the end of the response so hidden reasoning cannot leak.

    Args:
        content: Raw provider response text.

    Returns:
        Sanitized content plus non-sensitive removal counts.
    """
    sanitized, removed_closed_blocks = CLOSED_REASONING_BLOCK_PATTERN.subn("", str(content or ""))
    removed_unclosed_segments        = 0

    # Fail closed when a provider emits an opening reasoning tag without a close.
    unclosed_match = OPEN_REASONING_TAG_PATTERN.search(sanitized)

    if unclosed_match:
        sanitized                  = sanitized[: unclosed_match.start()]
        removed_unclosed_segments = 1

    sanitized, removed_stray_tags = CLOSE_REASONING_TAG_PATTERN.subn("", sanitized)
    normalized                    = normalize_user_facing_whitespace(sanitized)
    result                        = SanitizedLlmContent(
        content=normalized,
        removed_closed_blocks=removed_closed_blocks,
        removed_unclosed_segments=removed_unclosed_segments,
        removed_stray_tags=removed_stray_tags,
    )

    if result.removed_item_count:
        logger.info(
            "Sanitized private LLM reasoning artifacts | removed_closed_blocks=%d removed_unclosed_segments=%d removed_stray_tags=%d",
            result.removed_closed_blocks,
            result.removed_unclosed_segments,
            result.removed_stray_tags,
        )

    return result
