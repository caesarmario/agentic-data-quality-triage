from datetime import date
from pathlib import Path
from pipelines.dq.generate_alerts import fetch_latest_bad_results, open_alert_exists
from pipelines.dq.config import load_orders_dq_contract
from agent.tools.alert_lifecycle import build_alert_lifecycle_version_row, ALERT_VERSION_COLUMNS
from datetime import datetime, timezone


class CaptureClient:
    def __init__(self):
        self.sql = ""
    def query(self, sql):
        self.sql = sql
        class Result:
            result_rows = []
        return Result()


def test_all_statuses_ranked_before_fail_filter():
    client = CaptureClient()
    fetch_latest_bad_results(client, load_orders_dq_contract(), date(2026,10,11))
    where, having = client.sql.split("WHERE",1)[1].split("HAVING",1)
    assert "status IN" not in where and "result_status IN" in having


def test_latest_active_lifecycle_not_historical_open():
    client = CaptureClient()
    assert open_alert_exists(client, "fixture", "open") is False
    assert "dq.alerts FINAL" in client.sql and "'triaged'" in client.sql


def test_late_triage_preserves_resolved_status():
    source = {key: None for key in ALERT_VERSION_COLUMNS}
    source.update(updated_at=datetime.now(timezone.utc), status="resolved")
    row = build_alert_lifecycle_version_row(source, "s3://reports/history.md")
    assert row[ALERT_VERSION_COLUMNS.index("status")] == "resolved"
