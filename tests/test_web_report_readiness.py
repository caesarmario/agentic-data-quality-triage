####
## Persisted Web Report Acceptance Tests
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
import html
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import smoke_readiness, trigger_airflow_validation
from scripts.web_report_readiness import check_persisted_web_report, paired_json_uri


# --- Defining Offline Transport
class ReportTransport:
    """Supply isolated alert, artifact, and HTML fixtures while recording GET requests."""

    def __init__(self, failure: str = "") -> None:
        """Initialize a single controlled failure or a valid stored report."""
        self.failure = failure
        self.paths: list[str] = []
        self.report = {
            "report_id": "RPT-123ABC",
            "alert": {"alert_key": "orders|dq_failure|2026-09-08"},
            "summary": "Orders are missing & downstream totals are incomplete.",
            "hypotheses": [{"title": "The raw partition is absent"}],
            "evidence": [{"summary": "The persisted row count is zero."}],
        }

    def __call__(self, request, timeout: float):
        """Return context-managed bounded HTTP bytes without making a network request."""
        assert request.get_method() == "GET"
        assert request.get_header("Host") == "localhost:3000"
        assert 0 < timeout <= 20
        self.paths.append(request.full_url)
        path = urlsplit(request.full_url).path
        if path.endswith("/alerts"):
            payload = {"alerts": [] if self.failure == "missing" else [{
                "alert_key": self.report["alert"]["alert_key"],
                "report_s3_uri": "s3://dq-artifacts/agent-reports/id=123/report.md",
            }]}
        elif path.endswith("/reports/read"):
            assert parse_qs(urlsplit(request.full_url).query)["s3_uri"][0].endswith("/report.json")
            report = dict(self.report)
            if self.failure == "identity":
                report["alert"] = {"alert_key": "another-alert"}
            payload = {"media_type": "application/json", "truncated": self.failure == "truncated", "text": json.dumps(report)}
        else:
            assert path == "/triage"
            assert parse_qs(urlsplit(request.full_url).query)["alert"] == [self.report["alert"]["alert_key"]]
            parts = ["Persisted triage report", "Ranked hypotheses", "Evidence reviewed",
                     self.report["report_id"], self.report["summary"],
                     self.report["hypotheses"][0]["title"], self.report["evidence"][0]["summary"]]
            content = " ".join(html.escape(value) for value in parts)
            if self.failure == "script_only":
                content = "<script>" + content + "</script>"
            elif self.failure == "hidden_only":
                content = '<div hidden="">' + content + "</div>"
            elif self.failure == "id_only":
                content = self.report["report_id"]
            payload = content
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        response = BytesIO(body)
        response.status = 200
        return response


# --- Verifying Read-Only Content Acceptance
def test_stored_report_is_matched_to_actual_document_text() -> None:
    """Require seven matching fields and three bounded reads, with no model call."""
    transport = ReportTransport()
    result = check_persisted_web_report(opener=transport)
    assert result.status == "pass"
    assert result.details["report_id"] == "RPT-123ABC"
    assert result.details["checked_text_fields"] == 7
    assert result.details["external_calls"] == 0
    assert len(transport.paths) == 3


@pytest.mark.parametrize("failure", ["missing", "identity", "truncated", "script_only", "hidden_only", "id_only"])
def test_incomplete_or_unrelated_report_cannot_pass(failure: str) -> None:
    """Reject missing fixtures, identity conflicts, and JSON-only hydration content."""
    result = check_persisted_web_report(opener=ReportTransport(failure))
    assert result.status == "fail"
    assert set(result.details) == {"error_type"}


def test_report_gate_is_explicit_in_cli_and_airflow_contract() -> None:
    """Ensure the extra report requirement is opt-in and carried through DAG conf."""
    assert not smoke_readiness.build_parser().parse_args([]).require_web_report
    assert smoke_readiness.build_parser().parse_args(["--require-web-report"]).require_web_report
    command = trigger_airflow_validation.build_trigger_command("ui", "manual__report", require_web_report=True)
    assert json.loads(command[command.index("-c") + 1])["require_web_report"] is True
    dag = (Path(__file__).resolve().parents[1] / "dags/91_dag_dq_platform_validation.py").read_text()
    assert '"require_web_report": Param(' in dag
    assert "--require-web-report" in dag


@pytest.mark.parametrize("uri", ["https://example.com/report.md", "s3://dq-artifacts/a/../report.md", "s3://dq-artifacts/a/report.md?key=x"])
def test_unsafe_report_references_are_not_resolved(uri: str) -> None:
    """Refuse storage traversal and caller-style external URLs."""
    assert paired_json_uri(uri) is None
