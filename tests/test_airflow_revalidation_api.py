import requests
from apps.common.airflow_api import AirflowRevalidationClient


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body
    def json(self):
        return self.body


class Session:
    def __init__(self):
        self.posts = 0
        self.gets = 0
    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        if url.endswith("/auth/token"):
            return Response(200, {"access_token": "test"})
        self.posts += 1
        raise requests.Timeout()
    def get(self, url, **kwargs):
        self.gets += 1
        return Response(404 if self.gets == 1 else 200, {"state": "queued"})


def test_trigger_timeout_reconciles_same_run_without_second_post(monkeypatch):
    for key, value in {"AIRFLOW_API_BASE_URL": "http://airflow:8080", "AIRFLOW_API_USERNAME": "a", "AIRFLOW_API_PASSWORD": "b"}.items():
        monkeypatch.setenv(key, value)
    session = Session()
    result = AirflowRevalidationClient(session).trigger("00000000-0000-0000-0000-000000000001")
    assert result["state"] == "queued" and session.posts == 1 and session.gets == 2
    assert result["dag_run_id"].endswith("00000000-0000-0000-0000-000000000001")


def setup_config(monkeypatch):
    for key, value in {"AIRFLOW_API_BASE_URL": "http://airflow:8080", "AIRFLOW_API_USERNAME": "a", "AIRFLOW_API_PASSWORD": "b"}.items():
        monkeypatch.setenv(key, value)


def test_trigger_and_reconciliation_timeout_remain_unknown(monkeypatch):
    setup_config(monkeypatch)
    class FailedReconciliation(Session):
        def get(self, url, **kwargs):
            self.gets += 1
            if self.gets > 1:
                raise requests.Timeout()
            return Response(404, {})
    session = FailedReconciliation()
    result = AirflowRevalidationClient(session).trigger("00000000-0000-0000-0000-000000000001")
    assert result["state"] == "unknown" and session.posts == 1


def test_reconcile_only_never_posts_even_when_run_is_absent(monkeypatch):
    setup_config(monkeypatch)
    session = Session()
    result = AirflowRevalidationClient(session).trigger("00000000-0000-0000-0000-000000000001", reconcile_only=True)
    assert result["state"] == "not_found" and session.posts == 0
