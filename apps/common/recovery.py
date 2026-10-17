"""API-only recovery adapter. Warehouse facts and execution authority stay server-side."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote
from uuid import UUID

from pydantic import ValidationError

from agent.recovery.contracts import (
    RecoveryExecution, RecoveryExecutionCreate, RecoveryPlan, RecoveryPreview,
    RecoveryProposal, SYSTEM_REQUESTER,
)
from apps.common.control_plane import ControlPlaneClient, ControlPlaneResponseError


# --- Validating Authoritative Response Boundaries
ALERT_REF = re.compile(r"DQ-\d{8}-[A-F0-9]{6}")
RESERVED_ACTORS = {SYSTEM_REQUESTER, "system", "airflow", "agentic_triage",
                   "datasentry", "datasentrysystem", "datasentry_system"}


def human_actor(actor: str) -> str:
    """Reject service identities; adapters must derive identity from trusted context."""
    if (not isinstance(actor, str) or not actor.strip() or len(actor) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in actor)
            or actor.strip().casefold() in RESERVED_ACTORS):
        raise ValueError("A verified human operator identity is required.")
    return actor.strip()


def _model(model: Any, payload: Any) -> dict[str, Any]:
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError):
        # Pydantic errors can contain raw response values. Never expose those.
        raise ControlPlaneResponseError("Recovery API returned an invalid contract.") from None


def validate_preview(payload: Any, alert_ref: str) -> dict[str, Any]:
    """Require the requested incident, not a caller's suggested scope or source."""
    value = _model(RecoveryPreview, payload)
    if value["alert_ref"] != alert_ref or type(payload.get("incident_revision")) is not int:
        raise ControlPlaneResponseError("Recovery preview identity is invalid.")
    if value["latest_plan_id"] is not None:
        _uuid(value["latest_plan_id"])
    return value


def validate_plan(payload: Any, *, alert_ref: str | None = None,
                  plan_id: str | None = None) -> dict[str, Any]:
    """Validate plan/approval correlation, including the optimistic-lock version."""
    if not isinstance(payload, dict) or not isinstance(payload.get("approval"), dict):
        raise ControlPlaneResponseError("Recovery plan approval is incomplete.")
    plan = _model(RecoveryPlan, payload.get("plan"))
    approval = payload["approval"]
    if (alert_ref is not None and plan["alert_ref"] != alert_ref
            or plan_id is not None and plan["plan_id"] != _uuid(plan_id)
            or approval.get("request_id") != plan["approval_request_id"]
            or not re.fullmatch(r"APR-[A-Za-z0-9-]{3,80}", plan["approval_request_id"])
            or approval.get("status") not in {"pending", "approved", "rejected", "cancelled"}
            or type(approval.get("version")) is not int or approval["version"] < 1
            or not isinstance(approval.get("requested_by"), str) or not approval["requested_by"].strip()):
        raise ControlPlaneResponseError("Recovery plan or approval identity is invalid.")
    value = {"plan": plan, "approval": dict(approval)}
    if payload.get("execution") is not None:
        value["execution"] = validate_execution(payload["execution"], plan=plan)
    return value


def validate_execution(payload: Any, *, plan: dict[str, Any] | None = None,
                       execution_id: str | None = None) -> dict[str, Any]:
    """Execution success, validation PASS, and lifecycle closure are different facts."""
    value = _model(RecoveryExecution, payload)
    if (execution_id is not None and value["execution_id"] != _uuid(execution_id)
            or plan is not None and any(value[name] != plan[name] for name in
                                      ("plan_id", "plan_hash", "alert_ref", "approval_request_id"))):
        raise ControlPlaneResponseError("Recovery execution belongs to another plan.")
    return value


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ControlPlaneResponseError("Recovery identity is invalid.") from None


def plan_is_current(preview: dict[str, Any], bundle: dict[str, Any]) -> bool:
    """UI guard only. The API revalidates pinned facts and policy at dispatch."""
    plan = bundle["plan"]
    return bool(preview["available"] and not preview["blockers"]
                and preview["alert_ref"] == plan["alert_ref"]
                and preview["incident_revision"] == plan["incident_revision"]
                and preview["latest_plan_id"] == plan["plan_id"])


def can_decide(bundle: dict[str, Any], actor: str) -> bool:
    """Never impersonate the system requester or approve one's own request."""
    try:
        actor = human_actor(actor)
    except ValueError:
        return False
    approval = bundle["approval"]
    return approval["status"] == "pending" and actor.casefold() != approval["requested_by"].strip().casefold()


def can_execute(bundle: dict[str, Any]) -> bool:
    """An approved plan is runnable only before its single-use execution begins."""
    return (bundle["approval"]["status"] == "approved" and not bundle.get("execution")
            and bundle["approval"].get("execution_status", "not_started") == "not_started")


def review_token(bundle: dict[str, Any]) -> tuple[str, str, int]:
    """Freeze the exact reviewed scope and approval version for confirmation."""
    return bundle["plan"]["plan_id"], bundle["plan"]["plan_hash"], bundle["approval"]["version"]


def recovery_error_message(exc: Exception) -> str:
    """Return fixed safe copy, never raw transport bodies, inputs or credentials."""
    status = getattr(getattr(exc.__cause__, "response", None), "status_code", None)
    return {
        401: "Recovery access is not authorized. Contact the platform administrator.",
        403: "You do not have recovery operator access.",
        404: "Recovery state was not found. Refresh the selected incident.",
        409: "Recovery state changed. Refresh and review the exact plan again.",
        422: "Recovery input is invalid. Review the reason and current scope.",
        503: "Recovery is unavailable. No local fallback will run.",
    }.get(status, "Recovery request not confirmed. Check Status before trying again; do not assume a fix ran.")


# --- Calling The Trusted Control Plane (No Local Mutation Fallback)
class RecoveryClient:
    """Bounded calls with no automatic mutation retry and no SQL/fact overrides."""

    def __init__(self, client: ControlPlaneClient | None) -> None:
        if client is None:
            raise ControlPlaneResponseError("Recovery requires the shared control-plane API.")
        self.api = client

    @staticmethod
    def _alert(alert_ref: str) -> str:
        if not isinstance(alert_ref, str) or not ALERT_REF.fullmatch(alert_ref):
            raise ValueError("A canonical Alert Ref is required.")
        return "/api/v1/alerts/" + quote(alert_ref, safe="")

    def preview(self, alert_ref: str) -> dict[str, Any]:
        """Read eligibility, blockers, immutable source and server-chosen runbook."""
        return validate_preview(self.api.request_json("GET", self._alert(alert_ref) + "/recovery-preview"), alert_ref)

    def plan(self, plan_id: str, *, alert_ref: str | None = None) -> dict[str, Any]:
        """Read current approval and any execution attached to the immutable plan."""
        return validate_plan(self.api.request_json("GET", "/api/v1/recovery-plans/" + _uuid(plan_id)),
                             plan_id=plan_id, alert_ref=alert_ref)

    def propose(self, alert_ref: str, *, reason: str, expected_incident_revision: int) -> dict[str, Any]:
        """Reuse a durable plan after an uncertain response instead of duplicating it."""
        if type(expected_incident_revision) is not int:
            raise ValueError("Incident revision must be an integer.")
        body = RecoveryProposal(reason=reason, expected_incident_revision=expected_incident_revision).model_dump(mode="json")
        preview = self.preview(alert_ref)
        if preview["incident_revision"] != expected_incident_revision:
            raise ValueError("The incident changed. Review Fix again.")
        if preview["latest_plan_id"]:
            return self.plan(preview["latest_plan_id"], alert_ref=alert_ref)
        if not preview["available"] or not preview["eligible"] or preview["blockers"]:
            raise ValueError("Recovery is blocked. Review the latest evidence.")
        return validate_plan(self.api.request_json("POST", self._alert(alert_ref) + "/recovery-plans",
                                                  headers=self.api.approval_headers(), json_body=body), alert_ref=alert_ref)

    def decide(self, bundle: dict[str, Any], *, actor: str, decision: str, comment: str) -> dict[str, Any]:
        """Send only a human decision with a frozen version; never execute here."""
        actor = human_actor(actor)
        if decision not in {"approve", "reject"} or not can_decide(bundle, actor):
            raise ValueError("A different human operator must decide a pending request.")
        if not isinstance(comment, str) or not 5 <= len(comment.strip()) <= 1000:
            raise ValueError("A recovery reason must contain 5-1,000 characters.")
        approval = bundle["approval"]
        self.api.request_json("POST", "/api/v1/approvals/requests/" + quote(approval["request_id"], safe="") + "/decision",
                              headers=self.api.approval_headers(), json_body={"decided_by": actor, "decision": decision,
                              "comment": comment.strip(), "expected_version": approval["version"]})
        return self.plan(bundle["plan"]["plan_id"], alert_ref=bundle["plan"]["alert_ref"])

    def execute(self, bundle: dict[str, Any], *, actor: str, confirm_scope: bool) -> dict[str, Any]:
        """Explicit dispatch only; an ambiguous response must be reconciled by GET."""
        if confirm_scope is not True or not can_execute(bundle):
            raise ValueError("Confirm the exact approved recovery scope before running it.")
        body = RecoveryExecutionCreate(actor=human_actor(actor), expected_plan_hash=bundle["plan"]["plan_hash"],
                                       expected_approval_version=bundle["approval"]["version"], confirm_scope=True)
        payload = self.api.request_json("POST", "/api/v1/recovery-plans/" + _uuid(bundle["plan"]["plan_id"]) + "/executions",
                                       headers=self.api.approval_headers(), json_body=body.model_dump(mode="json"))
        return validate_execution(payload, plan=bundle["plan"])

    def execution(self, execution_id: str, *, plan: dict[str, Any]) -> dict[str, Any]:
        """Poll one known execution; polling never dispatches or closes incidents."""
        return validate_execution(self.api.request_json("GET", "/api/v1/recovery-executions/" + _uuid(execution_id)),
                                  plan=plan, execution_id=execution_id)
