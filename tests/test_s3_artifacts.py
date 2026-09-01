####
## Agent S3 Artifact Idempotency Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Contract tests for immutable report keys and replay-safe report audit events."""

# --- Importing Libraries
from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from agent.state import Alert, TriageReport
from agent.tools import s3 as s3_tools
from agent.tools.audit_log import AGENT_AUDIT_LOG_COLUMNS
from scripts import smoke_agent_side_effect_replay as replay_smoke


# --- Defining Constants
AGENT_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
ALERT_ID     = UUID("33333333-3333-4333-8333-333333333333")


# --- Defining Test Doubles
class FakeS3Client:
    """
    In-memory S3 client supporting immutable artifact operations.

    Attributes:
        objects: Stored object bodies and metadata keyed by bucket/key.
        put_calls: Captured put_object requests.
    """

    def __init__(self) -> None:
        """
        Initialize empty object storage.

        Returns:
            None.
        """
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]]                = []

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        """
        Return object metadata or raise the standard S3 missing-key error.

        Args:
            Bucket: S3 bucket name.
            Key: S3 object key.

        Returns:
            Object ContentLength and user metadata.

        Raises:
            ClientError: If the object does not exist.
        """
        stored = self.objects.get((Bucket, Key))

        if stored is None:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )

        return {
            "ContentLength": len(stored["Body"]),
            "Metadata": dict(stored.get("Metadata") or {}),
        }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        """
        Return one legacy object body for digest fallback verification.

        Args:
            Bucket: S3 bucket name.
            Key: S3 object key.

        Returns:
            Dictionary containing a readable Body stream.
        """
        return {"Body": BytesIO(self.objects[(Bucket, Key)]["Body"])}

    def put_object(self, **kwargs: Any) -> None:
        """
        Persist one object and capture its complete request.

        Args:
            kwargs: boto3-compatible put_object keyword arguments.

        Returns:
            None.
        """
        self.put_calls.append(dict(kwargs))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": bytes(kwargs["Body"]),
            "Metadata": dict(kwargs.get("Metadata") or {}),
            "ContentType": kwargs.get("ContentType", ""),
        }


class FakeQueryResult:
    """Minimal scalar response for deterministic audit existence checks."""

    def __init__(self, result_rows: list[tuple[Any, ...]]) -> None:
        """
        Store bounded ClickHouse result rows.

        Args:
            result_rows: Rows returned to the caller.

        Returns:
            None.
        """
        self.result_rows = result_rows


class FakeAuditClient:
    """
    In-memory ClickHouse client for report audit idempotency.

    Attributes:
        rows_by_id: Persisted audit rows keyed by deterministic UUID.
        inserts: Captured ClickHouse insert calls.
    """

    def __init__(self) -> None:
        """
        Initialize empty audit storage.

        Returns:
            None.
        """
        self.rows_by_id: dict[UUID, dict[str, Any]] = {}
        self.inserts: list[dict[str, Any]]           = []

    def query(self, sql: str, parameters: dict[str, Any]) -> FakeQueryResult:
        """
        Return whether one deterministic audit UUID exists.

        Args:
            sql: Bounded audit existence SQL.
            parameters: Query parameters containing audit_id.

        Returns:
            Scalar count result.
        """
        audit_id = UUID(str(parameters["audit_id"]))
        row      = self.rows_by_id.get(audit_id)

        if "any(action)" in sql:
            if row is None:
                return FakeQueryResult([(0, "", "", "")])

            return FakeQueryResult(
                [
                    (
                        1,
                        row["action"],
                        row["status"],
                        row["report_s3_uri"],
                    )
                ]
            )

        return FakeQueryResult([(int(row is not None),)])

    def insert(
        self,
        table: str,
        data: list[list[Any]],
        column_names: list[str],
    ) -> None:
        """
        Store one typed audit row.

        Args:
            table: Fully qualified audit table name.
            data: Row-oriented audit values.
            column_names: Explicit audit column order.

        Returns:
            None.
        """
        row = dict(zip(column_names, data[0], strict=True))
        self.inserts.append({"table": table, "data": data, "column_names": column_names})
        self.rows_by_id[row["audit_id"]] = row


# --- Defining Test Helpers
def build_report() -> TriageReport:
    """
    Build one deterministic report for replay tests.

    Returns:
        TriageReport with stable IDs, timestamps, and Markdown content.
    """
    alert = Alert(
        alert_id=ALERT_ID,
        alert_key="checkpoint|replay|2026-08-27|dq.raw_orders|row_count|table",
        alert_display_id="DQ-20260827-REPLAY",
        status="open",
        severity="critical",
        table_name="dq.raw_orders",
        metric="row_count",
        dt=date(2026, 8, 27),
        dimension="table",
    )

    return TriageReport(
        agent_run_id=AGENT_RUN_ID,
        alert=alert,
        summary="Deterministic checkpoint replay report.",
        impact="No production data impact; this report validates side-effect replay safety.",
        hypotheses=[],
        confidence=1.0,
        report_id="RPT-REPLAY01",
        markdown_report=(
            "# Checkpoint Replay Report\n\n"
            "Markdown: {{MARKDOWN_REPORT_S3_URI}}\n\n"
            "JSON: {{JSON_REPORT_S3_URI}}\n"
        ),
        created_at=datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
    )


# --- Defining Tests
def test_put_text_artifact_writes_digest_metadata_and_verifies_readback(monkeypatch) -> None:
    """
    Validate first-write metadata and readback integrity.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeS3Client()
    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: client)

    uri = s3_tools.put_text_artifact("dq-artifacts", "reports/report.md", "stable report")

    assert uri == "s3://dq-artifacts/reports/report.md"
    assert len(client.put_calls) == 1
    assert client.put_calls[0]["Metadata"][s3_tools.CONTENT_SHA256_METADATA]
    assert client.put_calls[0]["Metadata"][s3_tools.WRITE_POLICY_METADATA] == s3_tools.IMMUTABLE_KEY_POLICY


def test_put_text_artifact_reuses_identical_content_without_rewrite(monkeypatch) -> None:
    """
    Validate identical immutable content is a no-op on replay.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeS3Client()
    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: client)

    s3_tools.put_text_artifact("dq-artifacts", "reports/report.md", "stable report")
    s3_tools.put_text_artifact("dq-artifacts", "reports/report.md", "stable report")

    assert len(client.put_calls) == 1


def test_put_text_artifact_rejects_conflicting_content(monkeypatch) -> None:
    """
    Validate a deterministic key cannot silently overwrite different content.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeS3Client()
    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: client)

    s3_tools.put_text_artifact("dq-artifacts", "reports/report.md", "first report")

    with pytest.raises(s3_tools.ArtifactConflictError, match="different content"):
        s3_tools.put_text_artifact("dq-artifacts", "reports/report.md", "changed report")

    assert len(client.put_calls) == 1
    assert client.objects[("dq-artifacts", "reports/report.md")]["Body"] == b"first report"


def test_put_text_artifact_accepts_matching_legacy_object_without_metadata(monkeypatch) -> None:
    """
    Validate pre-contract objects can be verified by bounded content download.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    client = FakeS3Client()
    client.objects[("dq-artifacts", "reports/legacy.md")] = {
        "Body": b"legacy report",
        "Metadata": {},
    }
    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: client)

    uri = s3_tools.put_text_artifact("dq-artifacts", "reports/legacy.md", "legacy report")

    assert uri == "s3://dq-artifacts/reports/legacy.md"
    assert client.put_calls == []


def test_store_triage_report_is_replay_safe_across_s3_and_audit(monkeypatch) -> None:
    """
    Validate replay reuses two immutable artifacts and one deterministic audit event.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    s3_client    = FakeS3Client()
    audit_client = FakeAuditClient()

    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: s3_client)
    monkeypatch.setattr(s3_tools, "build_clickhouse_client", lambda **_: audit_client)

    report       = build_report()
    first_result = s3_tools.store_triage_report(report=report)
    replay_result = s3_tools.store_triage_report(report=report)

    assert first_result == replay_result
    assert len(s3_client.put_calls) == 2
    assert len(audit_client.inserts) == 1
    assert first_result["idempotency_contract"] == s3_tools.IMMUTABLE_KEY_POLICY
    assert len(first_result["markdown_sha256"]) == 64
    assert len(first_result["json_sha256"]) == 64

    audit_row = dict(
        zip(
            AGENT_AUDIT_LOG_COLUMNS,
            audit_client.inserts[0]["data"][0],
            strict=True,
        )
    )

    assert audit_row["action"] == "store_triage_report"
    assert audit_row["report_s3_uri"] == first_result["markdown_report_s3_uri"]


def test_cross_process_smoke_contract_reuses_artifacts_and_audit(monkeypatch) -> None:
    """
    Validate the Airflow smoke phases share one deterministic side-effect identity.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    s3_client    = FakeS3Client()
    audit_client = FakeAuditClient()

    monkeypatch.setattr(s3_tools, "build_s3_client", lambda **_: s3_client)
    monkeypatch.setattr(s3_tools, "build_clickhouse_client", lambda **_: audit_client)
    monkeypatch.setattr(replay_smoke, "build_s3_client", lambda **_: s3_client)
    monkeypatch.setattr(replay_smoke, "build_clickhouse_client", lambda **_: audit_client)

    thread_id = "checkpoint-smoke-side-effect-contract"
    written   = replay_smoke.run_side_effect_phase("write", thread_id)
    replayed  = replay_smoke.run_side_effect_phase("replay", thread_id)
    verified  = replay_smoke.run_side_effect_phase("verify", thread_id)

    assert written["agent_run_id"] == replayed["agent_run_id"]
    assert written["report_id"] == replayed["report_id"] == verified["report_id"]
    assert written["markdown_sha256"] == replayed["markdown_sha256"]
    assert written["json_sha256"] == replayed["json_sha256"]
    assert verified["idempotency_contract"] == "sequential-replay-safe"
    assert verified["audit"]["event_count"] == 1
    assert len(s3_client.put_calls) == 2
    assert len(audit_client.inserts) == 1


def test_side_effect_smoke_rejects_unknown_phase() -> None:
    """
    Validate arbitrary phase values cannot select an unbounded operation.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="Unknown agent side-effect phase"):
        replay_smoke.run_side_effect_phase(
            phase="delete",
            thread_id="checkpoint-smoke-invalid-phase",
        )
