"""Allowlisted Airflow Public API client; never retry an uncertain trigger."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

import requests

from agent.recovery.contracts import RECOVERY_DAG_ID
from agent.tools.incident_resolution_store import ResolutionConflict, ResolutionUnavailable

OBSERVABLE_DAGS = frozenset({
    RECOVERY_DAG_ID,
    "00_dag_dq_platform_daily_orchestrator",
    "10_dag_dq_orders_landing_orchestrator",
    "11_dag_dq_orders_seed_to_s3",
    "12_dag_dq_orders_load_raw_clickhouse",
    "20_dag_dq_orders_dbt_transform",
    "30_dag_dq_orders_quality_alerts",
    "40_dag_dq_orders_triage_agent",
    "90_dag_dq_platform_backfill_dispatcher",
})


class RecoveryAirflowClient:
    """Trigger only the recovery DAG; observe only known execution boundaries."""
    def __init__(self, session=None):
        self.base = os.getenv("AIRFLOW_API_BASE_URL", "").rstrip("/")
        parsed = urlparse(self.base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or not all(os.getenv(key) for key in ("AIRFLOW_API_USERNAME", "AIRFLOW_API_PASSWORD"))):
            raise ResolutionUnavailable("Recovery checks are unavailable: Airflow is not configured.")
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        try:
            result = self.session.post(self.base + "/auth/token", json={
                "username": os.environ["AIRFLOW_API_USERNAME"],
                "password": os.environ["AIRFLOW_API_PASSWORD"],
            }, timeout=5, allow_redirects=False)
            if result.status_code not in {200, 201}:
                raise ResolutionUnavailable("Airflow authentication is unavailable.")
            token = result.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ResolutionUnavailable("Airflow authentication is unavailable.")
            return {"Authorization": "Bearer " + token}
        except (requests.RequestException, ValueError):
            raise ResolutionUnavailable("Airflow authentication could not be confirmed.") from None

    def inspect(self, dag_id: str, run_id: str) -> dict | None:
        """Read an authoritative terminal state, without clearing or retrying tasks."""
        if dag_id not in OBSERVABLE_DAGS or not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,250}", run_id):
            raise ValueError("Airflow execution reference is not allowlisted.")
        try:
            response = self.session.get(f"{self.base}/api/v2/dags/{dag_id}/dagRuns/{run_id}",
                                        headers=self._headers(), timeout=5, allow_redirects=False)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ResolutionUnavailable("The existing Airflow execution could not be inspected.")
            return response.json()
        except (requests.RequestException, ValueError):
            raise ResolutionUnavailable("The existing Airflow execution could not be inspected.") from None

    @staticmethod
    def _verified(payload: dict, execution_id: str) -> dict:
        if payload.get("conf", {}).get("execution_id") != execution_id:
            raise ResolutionConflict("An existing Airflow run does not match this execution.")
        return payload

    def trigger(self, execution_id: str, *, reconcile_only: bool = False) -> dict:
        """At most one POST. Existing/uncertain outcomes require read-back."""
        execution_id = str(UUID(execution_id))
        run_id = f"manual__incident_recovery_{execution_id}"
        existing = self.inspect(RECOVERY_DAG_ID, run_id)
        if existing:
            return self._verified(existing, execution_id)
        if reconcile_only:
            return {"dag_run_id": run_id, "state": "unknown"}
        try:
            response = self.session.post(f"{self.base}/api/v2/dags/{RECOVERY_DAG_ID}/dagRuns",
                headers=self._headers(), timeout=5, allow_redirects=False,
                json={"dag_run_id": run_id, "logical_date": datetime.now(timezone.utc).isoformat(),
                      "conf": {"execution_id": execution_id}})
        except (requests.Timeout, requests.ConnectionError):
            try:
                existing = self.inspect(RECOVERY_DAG_ID, run_id)
            except ResolutionUnavailable:
                existing = None
            return self._verified(existing, execution_id) if existing else {"dag_run_id": run_id, "state": "unknown"}
        except requests.RequestException:
            return {"dag_run_id": run_id, "state": "unknown"}
        if response.status_code in {200, 201}:
            return self._verified(response.json(), execution_id)
        if response.status_code == 409 or response.status_code >= 500:
            try:
                existing = self.inspect(RECOVERY_DAG_ID, run_id)
            except ResolutionUnavailable:
                existing = None
            return self._verified(existing, execution_id) if existing else {"dag_run_id": run_id, "state": "unknown"}
        raise ResolutionUnavailable("Airflow did not accept the recovery execution.")
