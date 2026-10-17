####
## Bounded Segment Evidence for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Share exact country/channel semantics between detection and revalidation."""

# --- Importing Libraries
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pipelines.common.clickhouse import format_date_literal, validate_qualified_table_name
from pipelines.common.logging import logger


# --- Defining Constants
MAX_COVERAGE_SEGMENTS = 1000


# --- Defining Evidence Contracts
@dataclass(frozen=True)
class SegmentComparison:
    """Bounded distinct pairs; only exact, non-empty equality counts as PASS."""

    expected: frozenset[tuple[str, str]]
    observed: frozenset[tuple[str, str]]

    @property
    def exact_match(self) -> bool:
        """Reject both missing and unexpected pairs, including empty evidence."""
        return bool(self.expected) and self.observed == self.expected

    @property
    def coverage_ratio(self) -> float:
        """Report expected-pair coverage without credit for unknown pairs."""
        return len(self.expected & self.observed) / len(self.expected)

    def details(self) -> dict[str, Any]:
        """Return deterministic JSON-safe counts and exact set differences."""
        return {
            "observed_segment_count": len(self.expected & self.observed),
            "actual_segment_count": len(self.observed),
            "expected_segment_count": len(self.expected),
            "missing_segments": [list(pair) for pair in sorted(self.expected - self.observed)],
            "unexpected_segments": [list(pair) for pair in sorted(self.observed - self.expected)],
        }


# --- Defining Comparison Helpers
def compare_country_channel_segments(
    client: Any,
    table_name: str,
    dt: date,
    countries: Sequence[str],
    channels: Sequence[str],
) -> SegmentComparison:
    """Read distinct pairs for one date and compare with a bounded contract.

    Args:
        client: Read-only ClickHouse query client.
        table_name: Contracted, qualified table identifier.
        dt: Business date to inspect.
        countries: Expected country values.
        channels: Expected channel values.

    Returns:
        Exact comparison, with empty observed data represented as a mismatch.

    Raises:
        ValueError: Empty/oversized contract, malformed pairs, or evidence overflow.
    """
    validate_qualified_table_name(table_name)
    # Check the product before allocating it, and read one sentinel past the cap.
    if not countries or not channels or len(countries) * len(channels) > MAX_COVERAGE_SEGMENTS:
        raise ValueError("Expected segment contract is empty or exceeds the bounded limit.")
    for values in (countries, channels):
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Expected segment values must be non-empty strings.")
    expected = frozenset((country, channel) for country in countries for channel in channels)
    rows = client.query(
        f"SELECT country, channel FROM {table_name} WHERE dt = {format_date_literal(dt)} "
        f"GROUP BY country, channel ORDER BY country, channel LIMIT {MAX_COVERAGE_SEGMENTS + 1}"
    ).result_rows
    if len(rows) > MAX_COVERAGE_SEGMENTS:
        raise ValueError("Segment evidence exceeded the bounded limit.")
    if any(not isinstance(row, (tuple, list)) or len(row) != 2
           or any(not isinstance(value, str) for value in row) for row in rows):
        raise ValueError("Segment evidence must contain country/channel string pairs.")
    comparison = SegmentComparison(expected, frozenset(tuple(row) for row in rows))
    logger.info(
        "Compared exact country/channel segments | table=%s dt=%s expected=%d observed=%d exact_match=%s",
        table_name, dt, len(expected), len(comparison.observed), comparison.exact_match,
    )
    return comparison
