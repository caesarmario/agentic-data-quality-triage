"""Exercise bounded exact-pair evidence without warehouse access."""

# --- Importing Libraries
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from pipelines.dq import segments
from pipelines.dq.config import load_orders_dq_contract
from pipelines.dq.run_checks import check_segment_coverage


# --- Defining Test Helpers
BUSINESS_DATE = date(2026, 10, 5)


class SegmentWarehouse:
    """Record one distinct-pair query and return bounded synthetic evidence."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return SimpleNamespace(result_rows=self.rows)


def compare(client, countries=("ID",), channels=("direct",)):
    """Use a fixed, safe business-date scope for the shared comparison."""
    return segments.compare_country_channel_segments(
        client, "dq.fct_orders_daily", BUSINESS_DATE, countries, channels,
    )


# --- Checking Exact Pair Semantics
@pytest.mark.parametrize("rows,exact,ratio,missing,unexpected", [
    ([("ID", "direct")], True, 1.0, [], []),
    ([], False, 0.0, [["ID", "direct"]], []),
    ([("ZZ", "other")], False, 0.0, [["ID", "direct"]], [["ZZ", "other"]]),
    ([("ID", "direct"), ("ZZ", "other")], False, 1.0, [], [["ZZ", "other"]]),
])
def test_exact_comparison_rejects_missing_unexpected_and_empty(rows, exact, ratio, missing, unexpected):
    client = SegmentWarehouse(rows)
    result = compare(client)
    assert result.exact_match is exact
    assert result.coverage_ratio == ratio
    assert result.details() == {
        "observed_segment_count": int(ratio), "actual_segment_count": len(rows),
        "expected_segment_count": 1, "missing_segments": missing, "unexpected_segments": unexpected,
    }
    assert len(client.queries) == 1
    assert "GROUP BY country, channel ORDER BY country, channel LIMIT 1001" in client.queries[0]
    assert "WHERE dt = toDate('2026-10-05')" in client.queries[0]
    assert "concat" not in client.queries[0]


@pytest.mark.parametrize("countries,channels", [
    ([], ["direct"]), (["ID"], []), ([""] , ["direct"]),
    ([None], ["direct"]), (["ID"], [" "]),
    ([str(index) for index in range(1001)], ["direct"]),
])
def test_invalid_expected_contract_fails_before_any_query(countries, channels):
    client = SegmentWarehouse([])
    with pytest.raises(ValueError):
        compare(client, countries, channels)
    assert client.queries == []


@pytest.mark.parametrize("rows", [[(None, "direct")], [("ID",)], [("ID", "direct", "extra")], ["ID"], [(1, "direct")]])
def test_malformed_pair_evidence_is_never_coerced_to_valid_strings(rows):
    with pytest.raises(ValueError, match="string pairs"):
        compare(SegmentWarehouse(rows))


def test_country_channel_identity_has_no_delimiter_collisions():
    rows = [(country, channel) for country in ("A|B", "A") for channel in ("C", "B|C")]
    result = compare(SegmentWarehouse(rows), ("A|B", "A"), ("C", "B|C"))
    assert result.exact_match
    assert len(result.expected) == 4


def test_evidence_limit_boundary_is_complete_but_overflow_is_rejected():
    countries = [str(index) for index in range(segments.MAX_COVERAGE_SEGMENTS)]
    rows = [(country, "direct") for country in countries]
    assert compare(SegmentWarehouse(rows), countries).exact_match
    with pytest.raises(ValueError, match="evidence exceeded"):
        compare(SegmentWarehouse(rows + [("overflow", "direct")]), countries)


def test_unsafe_table_is_rejected_before_query():
    client = SegmentWarehouse([])
    with pytest.raises(ValueError):
        segments.compare_country_channel_segments(client, "dq.orders; SELECT 1", BUSINESS_DATE, ["ID"], ["direct"])
    assert client.queries == []


@pytest.mark.parametrize("change", ["missing", "unexpected", "replacement", "empty", "none"])
@pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0])
def test_daily_checks_use_exact_comparison_even_with_lower_legacy_threshold(change, threshold):
    contract = load_orders_dq_contract()
    contract.quality.segment_coverage.min_coverage_ratio = threshold
    rows = [(country, channel) for country in contract.expected_segments.countries
            for channel in contract.expected_segments.channels]
    if change == "missing":
        rows.pop()
    elif change == "unexpected":
        rows.append(("ZZ", "other"))
    elif change == "replacement":
        rows[-1] = ("ZZ", "other")
    elif change == "empty":
        rows = []
    client = SegmentWarehouse(rows)
    result = check_segment_coverage(client, contract, BUSINESS_DATE, datetime.now(timezone.utc))
    assert (result.status == "pass") is (change == "none")
    assert result.threshold_value == threshold
    assert result.observed_value <= 1.0
    assert len(client.queries) == 1
    assert result.details["expected_segment_count"] == 25
