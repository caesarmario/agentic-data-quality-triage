####
## DQ Evidence Export Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from pipelines.dq.evidence import build_evidence_key, export_failure_evidence_for_results
from pipelines.dq.run_checks import DqCheckResult


# --- Defining Test Helpers
class FakeQueryResponse:
    """
    Minimal ClickHouse query response used by evidence exporter tests.

    Attributes:
        column_names: Ordered response column names.
        result_rows: Query result rows.
    """

    def __init__(self, column_names: list[str], result_rows: list[tuple[object, ...]]) -> None:
        """
        Store fake query response data.

        Args:
            column_names: Ordered response column names.
            result_rows: Query result rows.

        Returns:
            None.
        """
        self.column_names = column_names
        self.result_rows  = result_rows


class FakeClickHouseClient:
    """
    Minimal ClickHouse client that returns deterministic evidence rows.

    Attributes:
        queries: SQL queries executed by the exporter.
    """

    def __init__(self) -> None:
        """
        Initialize the fake client with an empty query log.

        Returns:
            None.
        """
        self.queries: list[str] = []

    def query(self, query: str) -> FakeQueryResponse:
        """
        Return deterministic rows based on the incoming SQL shape.

        Args:
            query: SQL query executed by the evidence exporter.

        Returns:
            Fake ClickHouse response.
        """
        self.queries.append(query)

        if "GROUP BY dt" in query:
            return FakeQueryResponse(
                column_names=["dt", "row_count"],
                result_rows=[(date(2026, 5, 9), 0)],
            )

        return FakeQueryResponse(
            column_names=["dt", "order_id", "country"],
            result_rows=[(date(2026, 5, 9), "order_001", "ID")],
        )


def build_result(status: str) -> DqCheckResult:
    """
    Build one DQ result for evidence exporter tests.

    Args:
        status: DQ result status.

    Returns:
        DqCheckResult test object.
    """
    return DqCheckResult(
        check_run_id=uuid4(),
        run_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
        dt=date(2026, 5, 9),
        table_name="dq.raw_orders",
        check_name="row_count_positive",
        check_type="volume",
        status=status,
        severity="critical",
        observed_value=0.0,
        expected_value=1.0,
        threshold_value=1.0,
        details={},
    )


# --- Defining Tests
def test_build_evidence_key_is_path_safe() -> None:
    """
    Validate that evidence keys are deterministic and path-safe.

    Returns:
        None.
    """
    result = build_result(status="fail")
    key    = build_evidence_key(result=result)

    assert key.startswith("dq-failures/orders/dt=2026-05-09/table=dq_raw_orders/check=row_count_positive/")
    assert key.endswith("/evidence.json")


def test_export_failure_evidence_only_exports_bad_results(monkeypatch) -> None:
    """
    Validate that failed results get evidence URIs while passing results are skipped.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    exported_payloads = []

    def fake_put_json_evidence(bucket, key, payload, endpoint_url=None):
        """
        Capture evidence uploads without touching S3.

        Args:
            bucket: Target evidence bucket.
            key: Target object key.
            payload: Evidence payload.
            endpoint_url: Optional S3 endpoint URL.

        Returns:
            Fake S3 URI.
        """
        exported_payloads.append(
            {
                "bucket": bucket,
                "key": key,
                "payload": payload,
                "endpoint_url": endpoint_url,
            }
        )

        return f"s3://{bucket}/{key}"

    monkeypatch.setattr("pipelines.dq.evidence.put_json_evidence", fake_put_json_evidence)

    client             = FakeClickHouseClient()
    fail_result        = build_result(status="fail")
    pass_result        = build_result(status="pass")
    updated_results, summary = export_failure_evidence_for_results(
        client=client,
        results=[fail_result, pass_result],
        bucket="dq-dqfailures",
    )

    assert summary["exported"] == 1
    assert summary["skipped"] == 1
    assert updated_results[0].evidence_s3_uri.startswith("s3://dq-dqfailures/")
    assert updated_results[1].evidence_s3_uri == ""
    assert exported_payloads[0]["payload"]["check_name"] == "row_count_positive"
    assert len(client.queries) == 2
