####
## Control Plane API Client for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from typing import Any

import requests

from agent.state import TriageReport
from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS = 15.0
MAX_CONTROL_PLANE_TIMEOUT_SECONDS     = 120.0
MAX_ERROR_DETAIL_CHARS                = 500
MAX_REPORT_BYTES                      = 200_000
APPROVAL_TOKEN_ENV_NAME               = "CONTROL_PLANE_APPROVAL_TOKEN"
APPROVAL_TOKEN_HEADER                 = "X-Control-Plane-Token"
APPROVAL_STATUSES                     = {"pending", "approved", "rejected"}
APPROVAL_EXECUTION_STATUSES           = {"not_started", "dispatching", "dispatched", "succeeded", "failed"}
LIFE_EVALUATION_STATUSES              = {"pass", "review", "fail", "unknown"}
INCIDENT_OUTCOME_STATUSES             = {"success", "partial", "failed", "blocked"}
INCIDENT_APPROVAL_STATES              = {"not_required", "pending", "approved", "rejected"}
MAX_BLAST_RADIUS_DEPTH                = 10
MAX_BLAST_RADIUS_NODES                = 250
FORBIDDEN_LINEAGE_FIELDS              = {"raw_code", "compiled_code", "compiled_sql"}
FORBIDDEN_METADATA_FIELDS             = {"config_sha256", "source_config_path", "version", "is_active"}
FORBIDDEN_INCIDENT_HISTORY_FIELDS     = {
    "memory_key",
    "content_sha256",
    "decision_facts",
    "decision_json",
    "evidence_references_json",
    "raw_sql",
    "raw_tool_output",
    "conversation_history",
}
PUBLIC_INCIDENT_HISTORY_FIELDS        = {
    "memory_id",
    "parent_run_id",
    "recorded_at",
    "memory_type",
    "alert_id",
    "alert_key",
    "alert_display_id",
    "outcome_status",
    "specialist_name",
    "task_type",
    "summary",
    "confidence",
    "top_hypothesis_category",
    "report_id",
    "requires_human_approval",
    "evidence_reference_count",
    "evidence_references",
    "report_s3_uri",
    "approval_state",
    "resolution_reference",
}
PUBLIC_INCIDENT_EVIDENCE_FIELDS       = {
    "evidence_type",
    "source_tool",
    "reference",
    "summary",
}
REQUIRED_METADATA_FIELDS              = {
    "qualified_name",
    "display_name",
    "description",
    "domain",
    "data_layer",
    "technical_owner",
    "business_owner",
    "grain",
    "refresh_frequency",
    "sla_time",
    "sla_timezone",
    "criticality",
    "sensitivity",
    "contains_pii",
    "certification_status",
    "lifecycle_status",
    "tags",
    "synced_at",
}
METADATA_QUALIFIED_NAME_PATTERN       = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
CHECKPOINT_SNAPSHOT_FIELDS            = {
    "checkpoint_id",
    "created_at",
    "step",
    "source",
    "next_nodes",
    "is_complete",
}
CHECKPOINT_HISTORY_FIELDS             = {
    "status",
    "checkpoint_namespace",
    "thread_id",
    "history_count",
    "matching_checkpoint_count",
    "selected_checkpoint",
    "history",
    "raw_state_exposed",
    "read_only",
    "summary",
}
CHECKPOINT_REPLAY_PREVIEW_FIELDS       = {
    "status",
    "dag_id",
    "action",
    "alert_reference",
    "checkpoint_namespace",
    "source_thread_id",
    "source_checkpoint_id",
    "source_next_nodes",
    "replay_request_id",
    "replay_thread_id",
    "dag_run_conf",
    "execution_boundary",
    "operator_confirmation_required",
    "airflow_triggered",
    "side_effects_executed",
    "raw_state_exposed",
    "summary",
}
CHECKPOINT_REPLAY_CONF_FIELDS          = {
    "checkpoint_action",
    "run_triage",
    "alert_id",
    "alert_key",
    "checkpoint_mode",
    "checkpoint_namespace",
    "checkpoint_resume",
    "checkpoint_replay_id",
    "checkpoint_replay_request_id",
}
FORBIDDEN_CHECKPOINT_FIELDS            = {
    "values",
    "state",
    "tasks",
    "metadata",
    "parent_config",
    "raw_state",
    "messages",
    "conversation_history",
    "command",
    "shell_command",
}
FORBIDDEN_READ_API_FIELDS              = {
    "sql",
    "input_json",
    "output_json",
    "details_json",
    "metadata_json",
    "raw_sql",
    "compiled_sql",
}
REQUIRED_ALERT_FIELDS                  = {
    "alert_key",
    "alert_display_id",
    "status",
    "severity",
    "table_name",
    "metric",
}


# --- Defining Exceptions
class ControlPlaneClientError(RuntimeError):
    """
    Base error for local control-plane API client failures.
    """


class ControlPlaneTransportError(ControlPlaneClientError):
    """
    Retryable connectivity or timeout failure.
    """


class ControlPlaneResponseError(ControlPlaneClientError):
    """
    Non-retryable HTTP status or response-contract failure.
    """


# --- Defining Client
class ControlPlaneClient:
    """
    Small synchronous client shared by Discord, Streamlit, and future UI adapters.

    Attributes:
        base_url: Control-plane API base URL.
        timeout_seconds: Bounded request timeout.
        approval_token: Optional secret used only for approval mutation endpoints.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = DEFAULT_CONTROL_PLANE_TIMEOUT_SECONDS,
        approval_token: str | None = None,
    ) -> None:
        """
        Initialize one bounded control-plane API client.

        Args:
            base_url: API base URL such as http://api:8000.
            timeout_seconds: Per-request timeout in seconds.
            approval_token: Optional approval mutation token. None reads the environment.

        Returns:
            None.

        Raises:
            ValueError: If the base URL is blank.
        """
        normalized_url = base_url.strip().rstrip("/")

        if not normalized_url:
            raise ValueError("Control-plane API base URL cannot be blank.")

        self.base_url        = normalized_url
        self.timeout_seconds = max(
            1.0,
            min(float(timeout_seconds), MAX_CONTROL_PLANE_TIMEOUT_SECONDS),
        )
        self.approval_token = (
            os.getenv(APPROVAL_TOKEN_ENV_NAME, "").strip()
            if approval_token is None
            else approval_token.strip()
        )

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """
        Execute one bounded API request and require a JSON object response.

        Args:
            method: HTTP method.
            path: API path beginning with a slash.
            params: Optional query parameters.
            json_body: Optional JSON request body.
            headers: Optional bounded request headers.
            timeout_seconds: Optional request-specific timeout override.

        Returns:
            Parsed JSON object.

        Raises:
            ControlPlaneClientError: If transport, status, or payload validation fails.
        """
        if not path.startswith("/"):
            raise ValueError("Control-plane API path must begin with '/'.")

        timeout = self.timeout_seconds if timeout_seconds is None else max(
            1.0,
            min(float(timeout_seconds), MAX_CONTROL_PLANE_TIMEOUT_SECONDS),
        )
        started_at = time.monotonic()
        url        = f"{self.base_url}{path}"

        logger.info(
            "Calling control-plane API | method=%s path=%s timeout=%.1f",
            method.upper(),
            path,
            timeout,
        )

        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()

        except (requests.ConnectionError, requests.Timeout) as exc:
            detail = str(exc)[:MAX_ERROR_DETAIL_CHARS]
            raise ControlPlaneTransportError(
                f"Control-plane API transport failed for {method.upper()} {path}: {detail}"
            ) from exc

        except requests.RequestException as exc:
            detail = str(exc)[:MAX_ERROR_DETAIL_CHARS]
            raise ControlPlaneResponseError(
                f"Control-plane API request was rejected for {method.upper()} {path}: {detail}"
            ) from exc

        try:
            payload = response.json()

        except ValueError as exc:
            raise ControlPlaneResponseError(
                f"Control-plane API returned invalid JSON for {method.upper()} {path}."
            ) from exc

        if not isinstance(payload, dict):
            raise ControlPlaneResponseError(
                f"Control-plane API response must be a JSON object for {method.upper()} {path}."
            )

        duration_ms = int((time.monotonic() - started_at) * 1000)

        logger.info(
            "Control-plane API call completed | method=%s path=%s status=%s duration_ms=%d",
            method.upper(),
            path,
            response.status_code,
            duration_ms,
        )

        return payload

    def approval_headers(self) -> dict[str, str]:
        """
        Build the authorization header for approval mutation endpoints.

        Returns:
            Header dictionary containing the configured token.

        Raises:
            ControlPlaneResponseError: If approval mutation is not configured locally.
        """
        if not self.approval_token:
            raise ControlPlaneResponseError(
                f"Approval mutations require {APPROVAL_TOKEN_ENV_NAME}."
            )

        return {APPROVAL_TOKEN_HEADER: self.approval_token}

    @staticmethod
    def validate_approval_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """
        Validate the minimum durable approval response contract.

        Args:
            payload: JSON object returned by an approval endpoint.

        Returns:
            Original payload when identity, state, and parameter fields are valid.

        Raises:
            ControlPlaneResponseError: If required fields are missing or malformed.
        """
        required_fields = {
            "request_id",
            "action_type",
            "risk_level",
            "status",
            "requested_by",
            "reason",
            "dispatcher_dag_id",
            "target_dag_id",
            "start_date",
            "end_date",
            "parameters",
            "execution_dag_run_id",
            "execution_status",
        }
        missing_fields = sorted(
            field_name
            for field_name in required_fields
            if field_name not in payload
        )

        if missing_fields:
            raise ControlPlaneResponseError(
                f"Approval API response is missing required fields: {', '.join(missing_fields)}"
            )

        if not str(payload.get("request_id") or "").startswith("APR-"):
            raise ControlPlaneResponseError("Approval API response contains an invalid request_id.")

        if str(payload.get("status") or "") not in APPROVAL_STATUSES:
            raise ControlPlaneResponseError("Approval API response contains an invalid status.")

        if not isinstance(payload.get("parameters"), dict):
            raise ControlPlaneResponseError("Approval API response parameters must be an object.")

        if str(payload.get("execution_status") or "") not in APPROVAL_EXECUTION_STATUSES:
            raise ControlPlaneResponseError("Approval API response contains an invalid execution_status.")

        return payload

    def create_approval_request(
        self,
        *,
        requested_by: str,
        reason: str,
        target_dag_id: str,
        start_date: str,
        end_date: str,
        parameters: dict[str, Any] | None = None,
        alert_id: str | None = None,
        alert_key: str = "",
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Create or reuse one durable approval request through the API.

        Args:
            requested_by: Human or client identity requesting approval.
            reason: Human-readable backfill reason.
            target_dag_id: Allowlisted operational DAG to backfill.
            start_date: Inclusive YYYY-MM-DD start date.
            end_date: Inclusive YYYY-MM-DD end date.
            parameters: Optional execution parameters bound to the decision.
            alert_id: Optional source alert UUID.
            alert_key: Optional stable source alert key.
            agent_run_id: Optional source triage run UUID.

        Returns:
            Validated latest approval state.
        """
        payload = self.request_json(
            "POST",
            "/api/v1/approvals/requests",
            headers=self.approval_headers(),
            json_body={
                "action_type": "backfill",
                "alert_id": alert_id or None,
                "alert_key": alert_key.strip(),
                "agent_run_id": agent_run_id or None,
                "requested_by": requested_by.strip(),
                "reason": reason.strip(),
                "target_dag_id": target_dag_id.strip(),
                "start_date": start_date.strip(),
                "end_date": end_date.strip(),
                "parameters": dict(parameters or {}),
            },
        )

        return self.validate_approval_payload(payload)

    def list_approval_requests(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List latest durable approval states through the read-only API.

        Args:
            status: Optional pending, approved, or rejected filter.
            limit: Maximum latest-state rows returned.

        Returns:
            Validated queue response containing approval rows.
        """
        payload = self.request_json(
            "GET",
            "/api/v1/approvals/requests",
            params={
                "status": status,
                "limit": max(1, min(limit, 100)),
            },
        )
        rows = payload.get("rows")

        if not isinstance(rows, list):
            raise ControlPlaneResponseError("Approval queue response must contain a rows list.")

        for row in rows:
            if not isinstance(row, dict):
                raise ControlPlaneResponseError("Approval queue rows must be JSON objects.")

            self.validate_approval_payload(row)

        return payload

    def get_approval_request(self, request_id: str) -> dict[str, Any]:
        """
        Load the latest durable state for one approval request.

        Args:
            request_id: Human-facing APR request identifier.

        Returns:
            Validated latest approval state.

        Raises:
            ValueError: If request_id does not use the APR reference format.
        """
        normalized_id = request_id.strip()

        if not normalized_id.startswith("APR-"):
            raise ValueError("Approval request_id must use the APR- reference format.")

        payload = self.request_json(
            "GET",
            f"/api/v1/approvals/requests/{normalized_id}",
        )

        return self.validate_approval_payload(payload)

    def decide_approval_request(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """
        Approve or reject one pending request without executing remediation.

        Args:
            request_id: Human-facing APR identifier.
            decision: Approve or reject.
            decided_by: Human identity making the decision.
            comment: Optional bounded rationale.

        Returns:
            Validated latest approval state.

        Raises:
            ValueError: If request ID or decision is invalid.
        """
        normalized_id       = request_id.strip()
        normalized_decision = decision.strip().lower()

        if not normalized_id.startswith("APR-"):
            raise ValueError("Approval request_id must use the APR- reference format.")

        if normalized_decision not in {"approve", "reject"}:
            raise ValueError("Approval decision must be approve or reject.")

        payload = self.request_json(
            "POST",
            f"/api/v1/approvals/requests/{normalized_id}/decision",
            headers=self.approval_headers(),
            json_body={
                "decision": normalized_decision,
                "decided_by": decided_by.strip(),
                "comment": comment.strip(),
            },
        )

        return self.validate_approval_payload(payload)

    def health(self) -> dict[str, Any]:
        """
        Fetch API health metadata.

        Returns:
            Health response payload.
        """
        return self.request_json("GET", "/health")

    def list_alerts(
        self,
        status: str = "open",
        dt: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """
        List alerts through the shared API boundary.

        Args:
            status: Alert lifecycle status.
            dt: Optional business date.
            limit: Maximum alert rows.

        Returns:
            Alert list response payload.
        """
        normalized_status = status.strip().lower() or "open"
        bounded_limit     = max(1, min(int(limit), 100))
        payload           = self.request_json(
            "GET",
            "/api/v1/alerts",
            params={
                "status": normalized_status,
                "dt": dt,
                "limit": bounded_limit,
            },
        )
        alerts = payload.get("alerts")

        if not isinstance(alerts, list):
            raise ControlPlaneResponseError(
                "Alert API response must contain an alerts list."
            )

        if FORBIDDEN_READ_API_FIELDS.intersection(payload):
            raise ControlPlaneResponseError("Alert API exposed internal query metadata.")

        row_count = payload.get("row_count")

        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count != len(alerts)
            or row_count > bounded_limit
        ):
            raise ControlPlaneResponseError("Alert API returned inconsistent or unbounded rows.")

        if str(payload.get("alert_status") or "") != normalized_status:
            raise ControlPlaneResponseError("Alert API returned a different lifecycle filter.")

        if int(payload.get("limit") or 0) != bounded_limit:
            raise ControlPlaneResponseError("Alert API returned a different result limit.")

        for alert in alerts:
            self.validate_public_alert_payload(alert)

        return payload

    def get_daily_summary(self, dt: str) -> dict[str, Any]:
        """
        Fetch one deterministic daily DQ summary through the shared API.

        Args:
            dt: Business date in YYYY-MM-DD format.

        Returns:
            Validated check and open-alert aggregate payload.

        Raises:
            ValueError: If dt is blank or not a calendar date.
            ControlPlaneResponseError: If the response identity, totals, or privacy contract fails.
        """
        try:
            normalized_dt = date.fromisoformat(dt.strip()).isoformat()

        except (AttributeError, ValueError) as exc:
            raise ValueError("Daily summary requires dt in YYYY-MM-DD format.") from exc

        payload = self.request_json(
            "GET",
            "/api/v1/summaries/daily",
            params={"dt": normalized_dt},
        )

        if FORBIDDEN_READ_API_FIELDS.intersection(payload):
            raise ControlPlaneResponseError("Daily summary API exposed internal query metadata.")

        if str(payload.get("dt") or "") != normalized_dt:
            raise ControlPlaneResponseError("Daily summary API returned a different target date.")

        check_counts = payload.get("check_counts")
        alert_counts = payload.get("alert_counts")

        if not isinstance(check_counts, list) or not isinstance(alert_counts, list):
            raise ControlPlaneResponseError("Daily summary API must return aggregate lists.")

        if len(check_counts) > 100 or len(alert_counts) > 100:
            raise ControlPlaneResponseError("Daily summary API returned unbounded aggregates.")

        def validate_counts(
            rows: list[Any],
            label_field: str,
            contract_name: str,
        ) -> int:
            """
            Validate one aggregate collection and return its total.

            Args:
                rows: Candidate aggregate dictionaries.
                label_field: Required label field name.
                contract_name: Human-readable collection name for errors.

            Returns:
                Sum of all validated count values.

            Raises:
                ControlPlaneResponseError: If rows contain malformed, duplicate, or internal fields.
            """
            labels = []
            total  = 0

            for row in rows:
                if not isinstance(row, dict) or FORBIDDEN_READ_API_FIELDS.intersection(row):
                    raise ControlPlaneResponseError(f"Daily summary {contract_name} rows are malformed.")

                label = str(row.get(label_field) or "").strip()
                count = row.get("count")

                if not label or isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ControlPlaneResponseError(f"Daily summary {contract_name} rows contain invalid values.")

                labels.append(label)
                total += count

            if len(labels) != len(set(labels)):
                raise ControlPlaneResponseError(f"Daily summary {contract_name} rows contain duplicate labels.")

            return total

        check_total = validate_counts(check_counts, "status", "check")
        alert_total = validate_counts(alert_counts, "severity", "alert")

        if payload.get("total_checks") != check_total:
            raise ControlPlaneResponseError("Daily summary API returned an inconsistent check total.")

        if payload.get("total_open_alerts") != alert_total:
            raise ControlPlaneResponseError("Daily summary API returned an inconsistent alert total.")

        return payload

    @staticmethod
    def validate_public_alert_payload(alert: Any) -> dict[str, Any]:
        """
        Validate one public alert without accepting internal storage fields.

        Args:
            alert: Candidate alert object returned by the API.

        Returns:
            Validated alert dictionary.

        Raises:
            ControlPlaneResponseError: If required identity fields are missing or internal fields leak.
        """
        if not isinstance(alert, dict):
            raise ControlPlaneResponseError("Alert API rows must contain JSON objects.")

        if FORBIDDEN_READ_API_FIELDS.intersection(alert):
            raise ControlPlaneResponseError("Alert API exposed internal storage fields.")

        if not REQUIRED_ALERT_FIELDS.issubset(alert):
            raise ControlPlaneResponseError("Alert API response is missing required public fields.")

        if not all(str(alert.get(field) or "").strip() for field in REQUIRED_ALERT_FIELDS):
            raise ControlPlaneResponseError("Alert API response contains blank required fields.")

        report_uri = str(alert.get("report_s3_uri") or "")

        if report_uri and not report_uri.startswith("s3://dq-artifacts/"):
            raise ControlPlaneResponseError("Alert API returned an unsafe report artifact URI.")

        return alert

    def get_alert(
        self,
        *,
        alert_id: str = "",
        alert_key: str = "",
    ) -> dict[str, Any]:
        """
        Fetch one exact alert through the shared API boundary.

        Args:
            alert_id: Optional durable alert UUID.
            alert_key: Optional stable alert key or human-facing Alert Ref.

        Returns:
            Validated public alert detail.

        Raises:
            ValueError: If the request does not contain exactly one identifier.
            ControlPlaneResponseError: If the API returns another alert.
        """
        normalized_id  = alert_id.strip()
        normalized_key = alert_key.strip()

        if bool(normalized_id) == bool(normalized_key):
            raise ValueError("Alert detail requires exactly one alert_id or alert_key.")

        payload = self.request_json(
            "GET",
            "/api/v1/alerts/detail",
            params={
                "alert_id": normalized_id or None,
                "alert_key": normalized_key or None,
            },
        )
        alert = self.validate_public_alert_payload(payload)

        if normalized_id and str(alert.get("alert_id") or "") != normalized_id:
            raise ControlPlaneResponseError("Alert API returned a different alert_id.")

        if normalized_key and normalized_key not in {
            str(alert.get("alert_key") or ""),
            str(alert.get("alert_display_id") or ""),
        }:
            raise ControlPlaneResponseError("Alert API returned a different alert key or Alert Ref.")

        details = alert.get("details")

        if details is not None and not isinstance(details, dict):
            raise ControlPlaneResponseError("Alert API details must be a JSON object.")

        return alert

    def get_audit_logs(self, alert_key: str, limit: int = 50) -> dict[str, Any]:
        """
        Fetch sanitized audit events for one exact alert key.

        Args:
            alert_key: Stable system alert key.
            limit: Maximum audit events returned.

        Returns:
            Validated audit history without raw payloads or SQL text.

        Raises:
            ValueError: If alert_key is blank.
            ControlPlaneResponseError: If identity, count, or privacy contracts fail.
        """
        normalized_key = alert_key.strip()
        bounded_limit  = max(1, min(int(limit), 100))

        if not normalized_key:
            raise ValueError("Audit lookup requires a stable alert key.")

        payload = self.request_json(
            "GET",
            "/api/v1/audit/logs",
            params={"alert_key": normalized_key, "limit": bounded_limit},
        )
        rows = payload.get("rows")

        self.validate_bounded_rows(
            payload=payload,
            rows=rows,
            expected_limit=bounded_limit,
            contract_name="Audit",
        )

        if str(payload.get("alert_key") or "") != normalized_key:
            raise ControlPlaneResponseError("Audit API returned events for a different alert key.")

        for row in rows:
            if not isinstance(row, dict) or FORBIDDEN_READ_API_FIELDS.intersection(row):
                raise ControlPlaneResponseError("Audit API exposed malformed or internal event fields.")

            if not str(row.get("action") or "") or not str(row.get("status") or ""):
                raise ControlPlaneResponseError("Audit API returned an event without action or status.")

        return payload

    @staticmethod
    def validate_bounded_rows(
        *,
        payload: dict[str, Any],
        rows: Any,
        expected_limit: int,
        contract_name: str,
    ) -> list[dict[str, Any]]:
        """
        Validate a generic bounded rows response from a read-only endpoint.

        Args:
            payload: Full API response dictionary.
            rows: Candidate rows collection from the response.
            expected_limit: Hard caller result bound.
            contract_name: Operator-facing contract name used in errors.

        Returns:
            Validated list of row dictionaries.

        Raises:
            ControlPlaneResponseError: If rows, counts, bounds, or internal fields are invalid.
        """
        if FORBIDDEN_READ_API_FIELDS.intersection(payload):
            raise ControlPlaneResponseError(f"{contract_name} API exposed internal query metadata.")

        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ControlPlaneResponseError(f"{contract_name} API rows must contain JSON objects.")

        row_count = payload.get("row_count")

        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count != len(rows)
            or row_count > expected_limit
        ):
            raise ControlPlaneResponseError(
                f"{contract_name} API returned inconsistent or unbounded rows."
            )

        if int(payload.get("limit") or 0) != expected_limit:
            raise ControlPlaneResponseError(f"{contract_name} API returned a different result limit.")

        return rows

    def get_dq_history(
        self,
        *,
        table_name: str,
        dt: str,
        check_name: str = "",
        lookback_days: int = 14,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Fetch bounded DQ history evidence through the shared API.

        Args:
            table_name: Fully qualified warehouse table.
            dt: Target business date in YYYY-MM-DD format.
            check_name: Optional exact DQ check name.
            lookback_days: Historical lookback window.
            limit: Maximum DQ results returned.

        Returns:
            Validated public DQ evidence without SQL or serialized details.
        """
        normalized_table = table_name.strip()
        normalized_dt    = dt.strip()
        bounded_lookback = max(0, min(int(lookback_days), 90))
        bounded_limit    = max(1, min(int(limit), 500))

        if not normalized_table or not normalized_dt:
            raise ValueError("DQ history requires table_name and dt.")

        payload = self.request_json(
            "GET",
            "/api/v1/evidence/dq-history",
            params={
                "table_name": normalized_table,
                "dt": normalized_dt,
                "check_name": check_name.strip() or None,
                "lookback_days": bounded_lookback,
                "limit": bounded_limit,
            },
        )
        rows = self.validate_bounded_rows(
            payload=payload,
            rows=payload.get("rows"),
            expected_limit=bounded_limit,
            contract_name="DQ history",
        )

        if str(payload.get("table_name") or "") != normalized_table:
            raise ControlPlaneResponseError("DQ history API returned a different table.")

        if str(payload.get("dt") or "") != normalized_dt:
            raise ControlPlaneResponseError("DQ history API returned a different target date.")

        for row in rows:
            if FORBIDDEN_READ_API_FIELDS.intersection(row):
                raise ControlPlaneResponseError("DQ history API exposed internal result fields.")

            if str(row.get("table_name") or "") != normalized_table:
                raise ControlPlaneResponseError("DQ history API returned a row for another table.")

            if not isinstance(row.get("details"), dict):
                raise ControlPlaneResponseError("DQ history details must be a JSON object.")

        return payload

    def get_pipeline_runs(
        self,
        *,
        dt: str,
        lookback_days: int = 7,
        job_name: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Fetch bounded pipeline-run evidence through the shared API.

        Args:
            dt: Target business date in YYYY-MM-DD format.
            lookback_days: Historical lookback window.
            job_name: Optional exact pipeline job name.
            limit: Maximum pipeline runs returned.

        Returns:
            Validated public pipeline evidence without SQL or serialized metadata.
        """
        normalized_dt    = dt.strip()
        bounded_lookback = max(0, min(int(lookback_days), 90))
        bounded_limit    = max(1, min(int(limit), 500))

        if not normalized_dt:
            raise ValueError("Pipeline run evidence requires dt.")

        payload = self.request_json(
            "GET",
            "/api/v1/evidence/pipeline-runs",
            params={
                "dt": normalized_dt,
                "lookback_days": bounded_lookback,
                "job_name": job_name.strip() or None,
                "limit": bounded_limit,
            },
        )
        rows = self.validate_bounded_rows(
            payload=payload,
            rows=payload.get("rows"),
            expected_limit=bounded_limit,
            contract_name="Pipeline runs",
        )

        if str(payload.get("dt") or "") != normalized_dt:
            raise ControlPlaneResponseError("Pipeline run API returned a different target date.")

        for row in rows:
            if FORBIDDEN_READ_API_FIELDS.intersection(row):
                raise ControlPlaneResponseError("Pipeline run API exposed internal result fields.")

            if not isinstance(row.get("metadata"), dict):
                raise ControlPlaneResponseError("Pipeline run metadata must be a JSON object.")

        return payload

    @staticmethod
    def validate_checkpoint_snapshot(snapshot: Any) -> dict[str, Any]:
        """
        Validate one sanitized checkpoint snapshot returned by the API.

        Args:
            snapshot: Candidate checkpoint metadata object.

        Returns:
            Validated snapshot dictionary.

        Raises:
            ControlPlaneResponseError: If raw state leaks or fields are malformed.
        """
        if not isinstance(snapshot, dict):
            raise ControlPlaneResponseError("Checkpoint history rows must contain JSON objects.")

        unexpected_fields = set(snapshot).difference(CHECKPOINT_SNAPSHOT_FIELDS)
        leaked_fields     = FORBIDDEN_CHECKPOINT_FIELDS.intersection(snapshot)

        if leaked_fields or unexpected_fields:
            raise ControlPlaneResponseError(
                "Checkpoint history exposed fields outside the sanitized snapshot contract."
            )

        if not str(snapshot.get("checkpoint_id") or "").strip():
            raise ControlPlaneResponseError("Checkpoint history contains a blank checkpoint_id.")

        next_nodes = snapshot.get("next_nodes")

        if not isinstance(next_nodes, list) or not all(
            isinstance(node, str) and node for node in next_nodes
        ):
            raise ControlPlaneResponseError("Checkpoint history next_nodes must be a string list.")

        if not isinstance(snapshot.get("is_complete"), bool):
            raise ControlPlaneResponseError("Checkpoint history contains an invalid completion state.")

        return snapshot

    def get_checkpoint_history(
        self,
        *,
        checkpoint_namespace: str,
        alert_id: str = "",
        alert_key: str = "",
        history_limit: int = 50,
        history_next_node: str = "store_report",
    ) -> dict[str, Any]:
        """
        Fetch sanitized checkpoint history through the shared API boundary.

        Args:
            checkpoint_namespace: Existing source triage checkpoint namespace.
            alert_id: Optional source alert UUID.
            alert_key: Optional stable source alert key.
            history_limit: Maximum newest-first snapshots returned.
            history_next_node: Pending node used to select a replay candidate.

        Returns:
            Validated read-only checkpoint history.

        Raises:
            ValueError: If identity or namespace input is ambiguous.
            ControlPlaneResponseError: If the API exposes raw or inconsistent state.
        """
        normalized_namespace = checkpoint_namespace.strip()
        normalized_id        = alert_id.strip()
        normalized_key       = alert_key.strip()

        if not normalized_namespace or "\n" in normalized_namespace or "\r" in normalized_namespace:
            raise ValueError("Checkpoint history requires one single-line namespace.")

        if bool(normalized_id) == bool(normalized_key):
            raise ValueError("Checkpoint history requires exactly one alert_id or alert_key.")

        bounded_limit = max(1, min(int(history_limit), 100))
        selected_node = history_next_node.strip()

        if not selected_node:
            raise ValueError("Checkpoint history requires a replay next node.")

        payload = self.request_json(
            "GET",
            "/api/v1/checkpoints/history",
            params={
                "checkpoint_namespace": normalized_namespace,
                "alert_id": normalized_id or None,
                "alert_key": normalized_key or None,
                "history_limit": bounded_limit,
                "history_next_node": selected_node,
            },
        )
        unexpected_fields = set(payload).difference(CHECKPOINT_HISTORY_FIELDS)

        if unexpected_fields or FORBIDDEN_CHECKPOINT_FIELDS.intersection(payload):
            raise ControlPlaneResponseError(
                "Checkpoint history API exposed fields outside the public contract."
            )

        history = payload.get("history")

        if not isinstance(history, list) or not history or len(history) > bounded_limit:
            raise ControlPlaneResponseError(
                "Checkpoint history API returned an empty, malformed, or unbounded history list."
            )

        for snapshot in history:
            self.validate_checkpoint_snapshot(snapshot)

        history_count = payload.get("history_count")

        if isinstance(history_count, bool) or history_count != len(history):
            raise ControlPlaneResponseError("Checkpoint history API returned an inconsistent history_count.")

        selected = self.validate_checkpoint_snapshot(payload.get("selected_checkpoint"))
        matching = payload.get("matching_checkpoint_count")

        if isinstance(matching, bool) or not isinstance(matching, int) or matching < 1:
            raise ControlPlaneResponseError(
                "Checkpoint history API returned an invalid matching checkpoint count."
            )

        if not any(
            row["checkpoint_id"] == selected["checkpoint_id"]
            for row in history
        ):
            raise ControlPlaneResponseError(
                "Checkpoint history selected a checkpoint outside the returned history."
            )

        if selected_node not in selected["next_nodes"]:
            raise ControlPlaneResponseError(
                "Checkpoint history selected a checkpoint that does not wait for the requested node."
            )

        if payload.get("checkpoint_namespace") != normalized_namespace:
            raise ControlPlaneResponseError("Checkpoint history returned another namespace.")

        if payload.get("raw_state_exposed") is not False or payload.get("read_only") is not True:
            raise ControlPlaneResponseError("Checkpoint history violated the read-only privacy contract.")

        return payload

    def preview_checkpoint_replay(
        self,
        *,
        checkpoint_namespace: str,
        checkpoint_id: str,
        alert_id: str = "",
        alert_key: str = "",
        replay_request_id: str = "",
        history_limit: int = 50,
        history_next_node: str = "store_report",
    ) -> dict[str, Any]:
        """
        Build an Airflow-only checkpoint replay preview without triggering execution.

        Args:
            checkpoint_namespace: Existing source triage checkpoint namespace.
            checkpoint_id: Exact candidate selected from current history.
            alert_id: Optional source alert UUID.
            alert_key: Optional stable source alert key.
            replay_request_id: Optional explicit replay idempotency key.
            history_limit: Maximum history rows re-read by the API.
            history_next_node: Required pending node for replay.

        Returns:
            Validated non-executing replay preview.

        Raises:
            ValueError: If required operator input is blank or ambiguous.
            ControlPlaneResponseError: If the response could imply direct execution.
        """
        normalized_namespace = checkpoint_namespace.strip()
        normalized_checkpoint = checkpoint_id.strip()
        normalized_id         = alert_id.strip()
        normalized_key        = alert_key.strip()

        if not normalized_namespace or not normalized_checkpoint:
            raise ValueError("Replay preview requires checkpoint_namespace and checkpoint_id.")

        if bool(normalized_id) == bool(normalized_key):
            raise ValueError("Replay preview requires exactly one alert_id or alert_key.")

        payload = self.request_json(
            "POST",
            "/api/v1/checkpoints/replay-preview",
            json_body={
                "alert_id": normalized_id or None,
                "alert_key": normalized_key or None,
                "checkpoint_namespace": normalized_namespace,
                "checkpoint_id": normalized_checkpoint,
                "replay_request_id": replay_request_id.strip(),
                "history_limit": max(1, min(int(history_limit), 100)),
                "history_next_node": history_next_node.strip(),
            },
        )
        unexpected_fields = set(payload).difference(CHECKPOINT_REPLAY_PREVIEW_FIELDS)

        if unexpected_fields or FORBIDDEN_CHECKPOINT_FIELDS.intersection(payload):
            raise ControlPlaneResponseError(
                "Replay preview API exposed fields outside the public contract."
            )

        if (
            payload.get("status") != "preview"
            or payload.get("dag_id") != "40_dag_dq_orders_triage_agent"
            or payload.get("action") != "replay"
            or payload.get("execution_boundary") != "airflow_dag_40"
        ):
            raise ControlPlaneResponseError("Replay preview returned an invalid execution boundary.")

        if (
            payload.get("operator_confirmation_required") is not True
            or payload.get("airflow_triggered") is not False
            or payload.get("side_effects_executed") is not False
            or payload.get("raw_state_exposed") is not False
        ):
            raise ControlPlaneResponseError("Replay preview incorrectly reports execution or raw state exposure.")

        expected_reference = normalized_key or normalized_id

        if (
            payload.get("alert_reference") != expected_reference
            or payload.get("checkpoint_namespace") != normalized_namespace
            or payload.get("source_checkpoint_id") != normalized_checkpoint
        ):
            raise ControlPlaneResponseError("Replay preview returned different source identifiers.")

        if replay_request_id.strip() and payload.get("replay_request_id") != replay_request_id.strip():
            raise ControlPlaneResponseError("Replay preview returned a different replay request id.")

        source_next_nodes = payload.get("source_next_nodes")

        if not isinstance(source_next_nodes, list) or history_next_node.strip() not in source_next_nodes:
            raise ControlPlaneResponseError("Replay preview does not preserve the selected pending node.")

        conf = payload.get("dag_run_conf")

        if not isinstance(conf, dict) or set(conf) != CHECKPOINT_REPLAY_CONF_FIELDS:
            raise ControlPlaneResponseError("Replay preview returned an unexpected Airflow configuration.")

        if (
            conf.get("checkpoint_action") != "triage"
            or conf.get("run_triage") is not True
            or conf.get("checkpoint_mode") != "sqlite"
            or conf.get("checkpoint_namespace") != normalized_namespace
            or conf.get("checkpoint_replay_id") != normalized_checkpoint
            or conf.get("checkpoint_replay_request_id") != payload.get("replay_request_id")
        ):
            raise ControlPlaneResponseError("Replay preview Airflow configuration violates DAG 40 contracts.")

        return payload

    @staticmethod
    def validate_metadata_asset_payload(asset: Any) -> dict[str, Any]:
        """
        Validate one public metadata asset returned by the API.

        Args:
            asset: Candidate JSON object from the metadata API.

        Returns:
            Validated metadata asset dictionary.

        Raises:
            ControlPlaneResponseError: If required fields are missing or internal fields leak.
        """
        if not isinstance(asset, dict):
            raise ControlPlaneResponseError("Metadata API assets must contain JSON objects.")

        internal_fields = FORBIDDEN_METADATA_FIELDS.intersection(asset)

        if internal_fields:
            raise ControlPlaneResponseError(
                "Metadata API exposed internal registry fields: "
                f"{', '.join(sorted(internal_fields))}."
            )

        missing_fields = REQUIRED_METADATA_FIELDS - set(asset)

        if missing_fields:
            raise ControlPlaneResponseError(
                "Metadata API asset is missing required fields: "
                f"{', '.join(sorted(missing_fields))}."
            )

        if not isinstance(asset.get("tags"), list):
            raise ControlPlaneResponseError("Metadata API asset tags must be a list.")

        return asset

    def list_metadata_assets(
        self,
        query: str | None = None,
        domain: str | None = None,
        data_layer: str | None = None,
        certification_status: str | None = None,
        lifecycle_status: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """
        Search bounded trusted metadata through the shared control-plane API.

        Args:
            query: Optional free-text asset search.
            domain: Optional data domain filter.
            data_layer: Optional warehouse layer filter.
            certification_status: Optional certification filter.
            lifecycle_status: Optional lifecycle filter.
            limit: Maximum metadata assets returned.

        Returns:
            Validated metadata discovery response.

        Raises:
            ControlPlaneResponseError: If the API response violates the public contract.
        """
        bounded_limit = max(1, min(int(limit), 100))
        payload       = self.request_json(
            "GET",
            "/api/v1/metadata/assets",
            params={
                "query": str(query or "").strip() or None,
                "domain": str(domain or "").strip().lower() or None,
                "data_layer": str(data_layer or "").strip().lower() or None,
                "certification_status": str(certification_status or "").strip().lower() or None,
                "lifecycle_status": str(lifecycle_status or "").strip().lower() or None,
                "limit": bounded_limit,
            },
        )
        assets = payload.get("assets")

        if not isinstance(assets, list):
            raise ControlPlaneResponseError(
                "Metadata API response must contain an assets list."
            )

        if len(assets) > bounded_limit:
            raise ControlPlaneResponseError(
                "Metadata API returned more assets than the requested limit."
            )

        for asset in assets:
            self.validate_metadata_asset_payload(asset)

        if int(payload.get("row_count") or 0) != len(assets):
            raise ControlPlaneResponseError(
                "Metadata API returned an inconsistent row_count."
            )

        return payload

    def get_metadata_asset(self, qualified_name: str) -> dict[str, Any]:
        """
        Fetch one exact trusted metadata asset through the shared API.

        Args:
            qualified_name: Fully qualified database.table asset identity.

        Returns:
            Validated public metadata asset.

        Raises:
            ValueError: If qualified_name is blank.
            ControlPlaneResponseError: If the API returns another or malformed asset.
        """
        normalized_name = qualified_name.strip()

        if not normalized_name:
            raise ValueError("Metadata asset qualified_name cannot be blank.")

        if not METADATA_QUALIFIED_NAME_PATTERN.fullmatch(normalized_name):
            raise ValueError("Metadata asset qualified_name must use database.table format.")

        payload = self.request_json(
            "GET",
            f"/api/v1/metadata/assets/{normalized_name}",
        )
        asset = self.validate_metadata_asset_payload(payload)

        if str(asset.get("qualified_name") or "") != normalized_name:
            raise ControlPlaneResponseError(
                "Metadata API returned a different qualified asset."
            )

        return asset

    def list_life_evaluations(
        self,
        eval_status: str | None = None,
        scenario_id: str | None = None,
        lookback_days: int = 30,
        limit: int = 25,
    ) -> dict[str, Any]:
        """
        List sanitized LIFE evaluation summaries through the shared API.

        Args:
            eval_status: Optional pass, review, or fail filter.
            scenario_id: Optional incident scenario identifier.
            lookback_days: Mandatory recent audit window in days.
            limit: Maximum evaluation summaries to return.

        Returns:
            Bounded LIFE evaluation history response.

        Raises:
            ControlPlaneResponseError: If the API payload violates the public contract.
        """
        payload = self.request_json(
            "GET",
            "/api/v1/evaluations/life",
            params={
                "eval_status": (eval_status or "").strip().lower() or None,
                "scenario_id": (scenario_id or "").strip().lower() or None,
                "lookback_days": max(1, min(int(lookback_days), 365)),
                "limit": max(1, min(int(limit), 100)),
            },
        )
        rows = payload.get("rows")

        if not isinstance(rows, list):
            raise ControlPlaneResponseError(
                "LIFE evaluation API response must contain a rows list."
            )

        for row in rows:
            if not isinstance(row, dict):
                raise ControlPlaneResponseError(
                    "LIFE evaluation API rows must contain JSON objects."
                )

            if "input_json" in row or "output_json" in row:
                raise ControlPlaneResponseError(
                    "LIFE evaluation API must not expose raw audit payloads."
                )

            if str(row.get("eval_status") or "unknown") not in LIFE_EVALUATION_STATUSES:
                raise ControlPlaneResponseError(
                    "LIFE evaluation API returned an invalid eval_status."
                )

        return payload

    def get_incident_history(
        self,
        alert_reference: str,
        lookback_days: int = 90,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Fetch sanitized durable investigation history for one exact alert.

        Args:
            alert_reference: Human Alert Ref, canonical system key, or alert UUID.
            lookback_days: Mandatory recent timestamp window.
            limit: Maximum previous investigations returned.

        Returns:
            Validated incident-history response safe for operator interfaces.

        Raises:
            ValueError: If the alert reference is blank or multiline.
            ControlPlaneResponseError: If the API violates identity or privacy contracts.
        """
        normalized_reference = alert_reference.strip()

        if (
            not normalized_reference
            or "\n" in normalized_reference
            or "\r" in normalized_reference
        ):
            raise ValueError("Incident history requires one single-line alert reference.")

        bounded_lookback = max(1, min(int(lookback_days), 365))
        bounded_limit    = max(1, min(int(limit), 50))
        payload          = self.request_json(
            "GET",
            "/api/v1/incidents/history",
            params={
                "alert_reference": normalized_reference,
                "lookback_days": bounded_lookback,
                "limit": bounded_limit,
            },
        )
        rows = payload.get("rows")

        if not isinstance(rows, list):
            raise ControlPlaneResponseError(
                "Incident-history API response must contain a rows list."
            )

        row_count = payload.get("row_count")

        if isinstance(row_count, bool) or not isinstance(row_count, int):
            raise ControlPlaneResponseError(
                "Incident-history API returned an invalid row_count."
            )

        if len(rows) > bounded_limit or row_count != len(rows):
            raise ControlPlaneResponseError(
                "Incident-history API returned an inconsistent or unbounded row_count."
            )

        if str(payload.get("alert_reference") or "") != normalized_reference:
            raise ControlPlaneResponseError(
                "Incident-history API returned context for a different alert reference."
            )

        if payload.get("lookback_days") != bounded_lookback:
            raise ControlPlaneResponseError(
                "Incident-history API returned a different lookback window."
            )

        if payload.get("limit") != bounded_limit:
            raise ControlPlaneResponseError(
                "Incident-history API returned a different hard limit."
            )

        for row in rows:
            if not isinstance(row, dict):
                raise ControlPlaneResponseError(
                    "Incident-history API rows must contain JSON objects."
                )

            leaked_fields = FORBIDDEN_INCIDENT_HISTORY_FIELDS.intersection(row)

            if leaked_fields:
                raise ControlPlaneResponseError(
                    "Incident-history API exposed forbidden internal memory fields."
                )

            unexpected_fields = set(row).difference(PUBLIC_INCIDENT_HISTORY_FIELDS)

            if unexpected_fields:
                raise ControlPlaneResponseError(
                    "Incident-history API exposed fields outside the public contract."
                )

            row_identities = {
                str(row.get("alert_id") or ""),
                str(row.get("alert_key") or ""),
                str(row.get("alert_display_id") or ""),
            }

            if normalized_reference not in row_identities:
                raise ControlPlaneResponseError(
                    "Incident-history API returned a record for a different alert."
                )

            if str(row.get("outcome_status") or "") not in INCIDENT_OUTCOME_STATUSES:
                raise ControlPlaneResponseError(
                    "Incident-history API returned an invalid outcome_status."
                )

            if str(row.get("approval_state") or "") not in INCIDENT_APPROVAL_STATES:
                raise ControlPlaneResponseError(
                    "Incident-history API returned an invalid approval_state."
                )

            evidence = row.get("evidence_references")
            evidence_count = row.get("evidence_reference_count")

            if (
                not isinstance(evidence, list)
                or isinstance(evidence_count, bool)
                or not isinstance(evidence_count, int)
                or evidence_count != len(evidence)
            ):
                raise ControlPlaneResponseError(
                    "Incident-history API returned inconsistent evidence references."
                )

            if any(
                not isinstance(reference, dict)
                or set(reference).difference(PUBLIC_INCIDENT_EVIDENCE_FIELDS)
                for reference in evidence
            ):
                raise ControlPlaneResponseError(
                    "Incident-history API exposed invalid evidence references."
                )

            confidence = row.get("confidence")

            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                raise ControlPlaneResponseError(
                    "Incident-history API returned an invalid confidence score."
                )

            report_uri = str(row.get("report_s3_uri") or "")

            if report_uri and not report_uri.startswith("s3://"):
                raise ControlPlaneResponseError(
                    "Incident-history API returned an unsafe report URI."
                )

        return payload

    def get_dbt_blast_radius(
        self,
        table_name: str,
        manifest_s3_uri: str = "",
        max_depth: int = 5,
        max_nodes: int = 100,
    ) -> dict[str, Any]:
        """
        Fetch bounded transitive dbt impact through the shared API.

        Args:
            table_name: Fully qualified warehouse table name.
            manifest_s3_uri: Optional dbt manifest artifact URI.
            max_depth: Maximum downstream lineage depth.
            max_nodes: Maximum downstream nodes returned, excluding the root.

        Returns:
            Validated blast-radius response safe for Discord, Streamlit, and web clients.

        Raises:
            ValueError: If table_name is blank.
            ControlPlaneResponseError: If the API violates traversal or data contracts.
        """
        normalized_table = table_name.strip()

        if not normalized_table:
            raise ValueError("A dbt blast-radius request requires a table name.")

        bounded_depth = max(1, min(int(max_depth), MAX_BLAST_RADIUS_DEPTH))
        bounded_nodes = max(1, min(int(max_nodes), MAX_BLAST_RADIUS_NODES))
        payload       = self.request_json(
            "GET",
            "/api/v1/lineage/dbt/blast-radius",
            params={
                "table_name": normalized_table,
                "manifest_s3_uri": manifest_s3_uri.strip() or None,
                "max_depth": bounded_depth,
                "max_nodes": bounded_nodes,
            },
        )

        if str(payload.get("table_name") or "") != normalized_table:
            raise ControlPlaneResponseError(
                "Blast-radius API returned context for a different table."
            )

        if int(payload.get("max_depth") or 0) != bounded_depth:
            raise ControlPlaneResponseError(
                "Blast-radius API did not preserve the requested depth bound."
            )

        if int(payload.get("max_nodes") or 0) != bounded_nodes:
            raise ControlPlaneResponseError(
                "Blast-radius API did not preserve the requested node bound."
            )

        root_node = payload.get("node")

        if root_node is not None and not isinstance(root_node, dict):
            raise ControlPlaneResponseError(
                "Blast-radius API root node must be a JSON object or null."
            )

        if isinstance(root_node, dict) and FORBIDDEN_LINEAGE_FIELDS.intersection(root_node):
            raise ControlPlaneResponseError(
                "Blast-radius API must not expose raw or compiled dbt SQL."
            )

        collection_names = (
            "impacted_assets",
            "impacted_tests",
            "unresolved_nodes",
        )
        collections: dict[str, list[dict[str, Any]]] = {}

        for collection_name in collection_names:
            collection = payload.get(collection_name)

            if not isinstance(collection, list) or not all(
                isinstance(item, dict) for item in collection
            ):
                raise ControlPlaneResponseError(
                    f"Blast-radius API field {collection_name} must contain JSON objects."
                )

            for item in collection:
                if FORBIDDEN_LINEAGE_FIELDS.intersection(item):
                    raise ControlPlaneResponseError(
                        "Blast-radius API must not expose raw or compiled dbt SQL."
                    )

                depth = int(item.get("depth") or 0)

                if depth < 1 or depth > bounded_depth:
                    raise ControlPlaneResponseError(
                        "Blast-radius API returned a node outside the requested depth bound."
                    )

            collections[collection_name] = collection

        actual_total = sum(len(collection) for collection in collections.values())

        if actual_total > bounded_nodes:
            raise ControlPlaneResponseError(
                "Blast-radius API returned more nodes than the requested node bound."
            )

        if int(payload.get("total_impacted_nodes") or 0) != actual_total:
            raise ControlPlaneResponseError(
                "Blast-radius API returned inconsistent total node counts."
            )

        if int(payload.get("impacted_asset_count") or 0) != len(
            collections["impacted_assets"]
        ):
            raise ControlPlaneResponseError(
                "Blast-radius API returned inconsistent impacted asset counts."
            )

        if int(payload.get("impacted_test_count") or 0) != len(
            collections["impacted_tests"]
        ):
            raise ControlPlaneResponseError(
                "Blast-radius API returned inconsistent impacted test counts."
            )

        if int(payload.get("unresolved_node_count") or 0) != len(
            collections["unresolved_nodes"]
        ):
            raise ControlPlaneResponseError(
                "Blast-radius API returned inconsistent unresolved node counts."
            )

        return payload

    def answer_copilot(
        self,
        question: str,
        alert_key: str,
        report_json_s3_uri: str = "",
        audit_limit: int = 10,
    ) -> dict[str, Any]:
        """
        Request one grounded Copilot answer through the shared API.

        Args:
            question: Operator question.
            alert_key: Alert Ref or stable system alert key.
            report_json_s3_uri: Optional persisted triage report JSON URI.
            audit_limit: Maximum recent audit events used by the API.

        Returns:
            Typed Copilot response payload.

        Raises:
            ValueError: If alert_key is blank.
            ControlPlaneClientError: If response context does not match the request.
        """
        normalized_alert_key = alert_key.strip()

        if not normalized_alert_key:
            raise ValueError("A grounded Copilot API request requires an Alert Ref or alert key.")

        payload = self.request_json(
            "POST",
            "/api/v1/copilot/answer",
            json_body={
                "alert_key": normalized_alert_key,
                "question": question.strip(),
                "report_json_s3_uri": report_json_s3_uri.strip() or None,
                "audit_limit": max(1, min(audit_limit, 25)),
            },
        )

        if not payload.get("answer"):
            raise ControlPlaneResponseError("Copilot API response does not contain an answer.")

        history_count = payload.get("incident_history_count", 0)

        if (
            isinstance(history_count, bool)
            or not isinstance(history_count, int)
            or history_count < 0
            or history_count > 5
        ):
            raise ControlPlaneResponseError(
                "Copilot API response contains an invalid incident_history_count."
            )

        payload["incident_history_count"] = history_count

        return payload

    def read_report_artifact(
        self,
        s3_uri: str,
        max_bytes: int = MAX_REPORT_BYTES,
    ) -> dict[str, Any]:
        """
        Read one bounded Markdown or JSON report through the shared API.

        Args:
            s3_uri: Approved report artifact S3 URI.
            max_bytes: Maximum UTF-8 bytes returned by the API.

        Returns:
            Validated artifact metadata and bounded text.

        Raises:
            ValueError: If the URI is blank or not an S3 URI.
            ControlPlaneResponseError: If identity or byte-bound contracts fail.
        """
        normalized_uri = s3_uri.strip()
        bounded_bytes  = max(1, min(int(max_bytes), MAX_REPORT_BYTES))

        if not normalized_uri.startswith("s3://"):
            raise ValueError("Report artifact lookup requires an S3 URI.")

        artifact = self.request_json(
            "GET",
            "/api/v1/reports/read",
            params={
                "s3_uri": normalized_uri,
                "max_bytes": bounded_bytes,
            },
        )

        if FORBIDDEN_READ_API_FIELDS.intersection(artifact):
            raise ControlPlaneResponseError("Report API exposed internal query metadata.")

        if str(artifact.get("s3_uri") or "") != normalized_uri:
            raise ControlPlaneResponseError("Report API returned a different artifact URI.")

        if int(artifact.get("max_bytes") or 0) != bounded_bytes:
            raise ControlPlaneResponseError("Report API returned a different byte limit.")

        text           = artifact.get("text")
        bytes_read     = artifact.get("bytes_read")
        returned_bytes = artifact.get("returned_bytes")

        if not isinstance(text, str):
            raise ControlPlaneResponseError("Report API text must be a string.")

        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (bytes_read, returned_bytes)
        ):
            raise ControlPlaneResponseError("Report API returned invalid byte metadata.")

        if returned_bytes != len(text.encode("utf-8")):
            raise ControlPlaneResponseError("Report API returned inconsistent UTF-8 byte metadata.")

        if returned_bytes > bounded_bytes or returned_bytes > bytes_read:
            raise ControlPlaneResponseError("Report API exceeded its artifact byte bounds.")

        if artifact.get("media_type") not in {"application/json", "text/markdown"}:
            raise ControlPlaneResponseError("Report API returned an unsupported media type.")

        if not isinstance(artifact.get("truncated"), bool):
            raise ControlPlaneResponseError("Report API returned an invalid truncation state.")

        return artifact

    def read_report_json(self, s3_uri: str) -> dict[str, Any]:
        """
        Read and parse one bounded report JSON artifact through the API.

        Args:
            s3_uri: Approved triage report JSON S3 URI.

        Returns:
            Parsed report JSON object.

        Raises:
            ControlPlaneClientError: If the report is truncated or malformed.
        """
        artifact = self.read_report_artifact(
            s3_uri=s3_uri,
            max_bytes=MAX_REPORT_BYTES,
        )

        if artifact.get("truncated"):
            raise ControlPlaneResponseError("Triage report JSON was truncated by the API.")

        try:
            payload = json.loads(str(artifact.get("text") or ""))

        except json.JSONDecodeError as exc:
            raise ControlPlaneResponseError("Triage report artifact is not valid JSON.") from exc

        if not isinstance(payload, dict):
            raise ControlPlaneResponseError("Triage report artifact must contain a JSON object.")

        return payload

    def run_triage_report(
        self,
        alert_key: str,
        confidence_threshold: float,
        max_evidence_iterations: int,
        manifest_s3_uri: str,
    ) -> TriageReport:
        """
        Run triage through FastAPI and reconstruct the persisted report model.

        Args:
            alert_key: Alert Ref or stable system alert key.
            confidence_threshold: Evidence-loop confidence target.
            max_evidence_iterations: Maximum bounded extra-evidence loops.
            manifest_s3_uri: dbt manifest artifact URI.

        Returns:
            Validated TriageReport loaded from the API report artifact.

        Raises:
            ControlPlaneClientError: If response and report identities do not match.
        """
        triage_payload = self.request_json(
            "POST",
            "/api/v1/triage/run",
            json_body={
                "alert_key": alert_key.strip(),
                "confidence_threshold": confidence_threshold,
                "max_evidence_iterations": max_evidence_iterations,
                "manifest_s3_uri": manifest_s3_uri,
            },
            timeout_seconds=MAX_CONTROL_PLANE_TIMEOUT_SECONDS,
        )
        report_uri = str(triage_payload.get("json_report_s3_uri") or "")

        if not report_uri:
            raise ControlPlaneResponseError("Triage API response does not contain a JSON report URI.")

        report_payload = self.read_report_json(report_uri)

        try:
            report = TriageReport.model_validate(report_payload)

        except ValueError as exc:
            raise ControlPlaneResponseError(
                "Triage report artifact does not match the TriageReport contract."
            ) from exc

        response_alert_key = str(triage_payload.get("alert_key") or "")

        if report.alert.alert_key != response_alert_key:
            raise ControlPlaneResponseError(
                "Triage report alert_key does not match the triage API response."
            )

        if str(report.agent_run_id) != str(triage_payload.get("agent_run_id") or ""):
            raise ControlPlaneResponseError(
                "Triage report agent_run_id does not match the triage API response."
            )

        return report

