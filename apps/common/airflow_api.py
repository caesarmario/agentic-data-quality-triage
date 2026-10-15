"""Allowlisted Airflow v2 trigger with JWT and uncertain-trigger reconciliation."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import UUID
from urllib.parse import urlparse

import requests

from agent.tools.incident_resolution_store import ResolutionUnavailable

DAG_ID = "90_03_dag_dq_incident_revalidation"


def configured() -> bool:
    return all(os.getenv(key, "").strip() for key in
               ("AIRFLOW_API_BASE_URL", "AIRFLOW_API_USERNAME", "AIRFLOW_API_PASSWORD"))


class AirflowRevalidationClient:
    def __init__(self, session=None):
        if not configured():
            raise ResolutionUnavailable("Pemeriksaan belum tersedia: koneksi Airflow belum diatur.")
        self.base = os.environ["AIRFLOW_API_BASE_URL"].rstrip("/")
        parsed = urlparse(self.base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query:
            raise ResolutionUnavailable("Konfigurasi Airflow tidak valid.")
        self.session = session or requests.Session()

    def trigger(self, request_id: str, *, reconcile_only: bool = False) -> dict:
        request_id = str(UUID(request_id))
        run_id = f"manual__incident_revalidation_{request_id}"
        post_attempted = False
        try:
            auth = self.session.post(self.base + "/auth/token", json={
                "username": os.environ["AIRFLOW_API_USERNAME"],
                "password": os.environ["AIRFLOW_API_PASSWORD"],
            }, timeout=5, allow_redirects=False)
            if auth.status_code != 201 and auth.status_code != 200:
                raise ResolutionUnavailable("Autentikasi Airflow belum berhasil.")
            token = auth.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ResolutionUnavailable("Autentikasi Airflow belum berhasil.")
            headers = {"Authorization": "Bearer " + token}
            path = f"{self.base}/api/v2/dags/{DAG_ID}/dagRuns"
            existing = self.session.get(f"{path}/{run_id}", headers=headers, timeout=5, allow_redirects=False)
            if existing.status_code == 200:
                return {"dag_run_id": run_id, "state": existing.json().get("state", "queued")}
            if existing.status_code != 404:
                raise ResolutionUnavailable("Status pemeriksaan Airflow belum dapat dipastikan.")
            if reconcile_only:
                return {"dag_run_id": run_id, "state": "not_found"}
            try:
                post_attempted = True
                response = self.session.post(path, headers=headers, json={
                    "dag_run_id": run_id, "logical_date": datetime.now(timezone.utc).isoformat(),
                    "conf": {"resolution_request_id": request_id},
                }, timeout=5, allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError):
                try:
                    response = self.session.get(f"{path}/{run_id}", headers=headers, timeout=5, allow_redirects=False)
                except requests.RequestException:
                    return {"dag_run_id": run_id, "state": "unknown"}
                if response.status_code != 200:
                    # Never issue a second POST after an uncertain delivery.
                    return {"dag_run_id": run_id, "state": "unknown"}
            if response.status_code == 409:
                try:
                    response = self.session.get(f"{path}/{run_id}", headers=headers, timeout=5, allow_redirects=False)
                except requests.RequestException:
                    return {"dag_run_id": run_id, "state": "unknown"}
                if response.status_code != 200:
                    return {"dag_run_id": run_id, "state": "unknown"}
            if response.status_code >= 500:
                return {"dag_run_id": run_id, "state": "unknown"}
            if response.status_code not in {200, 201}:
                raise ResolutionUnavailable("Airflow belum menerima pemeriksaan.")
            return {"dag_run_id": run_id, "state": response.json().get("state", "queued")}
        except ResolutionUnavailable:
            raise
        except (requests.RequestException, ValueError, KeyError):
            if post_attempted:
                return {"dag_run_id": run_id, "state": "unknown"}
            raise ResolutionUnavailable("Koneksi Airflow belum tersedia.") from None
