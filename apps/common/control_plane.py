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
MAX_BLAST_RADIUS_DEPTH                = 10
MAX_BLAST_RADIUS_NODES                = 250
FORBIDDEN_LINEAGE_FIELDS              = {"raw_code", "compiled_code", "compiled_sql"}
FORBIDDEN_METADATA_FIELDS             = {"config_sha256", "source_config_path", "version", "is_active"}
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
        payload = self.request_json(
            "GET",
            "/api/v1/alerts",
            params={
                "status": status,
                "dt": dt,
                "limit": max(1, min(limit, 100)),
            },
        )

        if not isinstance(payload.get("alerts"), list):
            raise ControlPlaneResponseError(
                "Alert API response must contain an alerts list."
            )

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

        return payload

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
        artifact = self.request_json(
            "GET",
            "/api/v1/reports/read",
            params={
                "s3_uri": s3_uri,
                "max_bytes": MAX_REPORT_BYTES,
            },
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

