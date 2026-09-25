####
## Web Operator Approval Acceptance for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Read-only evidence gate for one browser-operated synthetic approval."""

# --- Importing Libraries
from __future__ import annotations

import re
from typing import Any, Callable

from pipelines.common.logging import logger
from scripts.smoke_web import WebAcceptanceCheck


# --- Defining Constants
REQUEST_ID_PATTERN = re.compile(r"APR-[0-9]{8}-[A-F0-9]{8}\Z")
REQUIRED_AUDIT = {
    ("approval_requested", "pending"),
    ("approval_decision", "approved"),
    ("approval_cancelled", "cancelled"),
}


# --- Defining Functions
def validate_web_operator_request_id(request_id: str) -> str:
    """Accept only the project's bounded, canonical approval ID syntax."""
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("Invalid web operator request ID")
    return request_id


def build_approval_audit_query(request_id: str) -> str:
    """Aggregate every audit event correlated to exactly this approval ID."""
    literal = f"'{validate_web_operator_request_id(request_id)}'"
    return f"""
        SELECT action, status, count()
        FROM dq.agent_audit_log
        WHERE JSONExtractString(input_json, 'request_id') = {literal}
           OR JSONExtractString(output_json, 'request_id') = {literal}
           OR JSONExtractString(input_json, 'approval_request_id') = {literal}
           OR JSONExtractString(output_json, 'approval_request_id') = {literal}
        GROUP BY action, status
    """


def check_web_operator_approval(
    request_id: str,
    client_factory: Callable[[], Any] | None = None,
    approval_reader: Callable[[Any, str], Any] | None = None,
) -> WebAcceptanceCheck:
    """Verify cancelled synthetic state and complete non-dispatch audit evidence."""
    name = "web_operator:approval_interaction"
    try:
        request_id = validate_web_operator_request_id(request_id)
    except ValueError:
        return WebAcceptanceCheck(name, "fail", {"error_type": "InvalidRequestId"})

    try:
        if client_factory is None:
            from pipelines.common.clickhouse import build_clickhouse_client

            client_factory = build_clickhouse_client
        if approval_reader is None:
            from agent.tools.approval_queue import get_approval_request

            approval_reader = get_approval_request
        client = client_factory()
        approval = approval_reader(client, request_id)
        if approval is None:
            raise LookupError("Approval not found")

        rows = client.query(build_approval_audit_query(request_id)).result_rows
        actions = sorted({str(row[0]) if re.fullmatch(r"[a-z_]{1,64}", str(row[0])) else "invalid_action" for row in rows})
        observed = {(str(action), str(status)) for action, status, count in rows if int(count) > 0}
        details = {
            "request_id": request_id,
            "status": str(approval.status) if str(approval.status) in {"pending", "approved", "rejected", "cancelled"} else "invalid",
            "execution_status": str(approval.execution_status) if str(approval.execution_status) in {"not_started", "running", "succeeded", "failed"} else "invalid",
            "execution_dag_run_id_empty": approval.execution_dag_run_id == "",
            "audit_actions": actions,
        }
        valid = (
            approval.request_id == request_id
            and isinstance(approval.requested_by, str)
            and approval.requested_by.startswith("web-acceptance-")
            and approval.status == "cancelled"
            and approval.execution_status == "not_started"
            and approval.execution_dag_run_id == ""
            and REQUIRED_AUDIT <= observed
            and "approval_execution_transition" not in actions
        )
        if not valid:
            logger.warning("Web operator approval acceptance failed | request_id=%s", request_id)
        else:
            logger.info("Web operator approval acceptance passed | request_id=%s", request_id)
        return WebAcceptanceCheck(name, "pass" if valid else "fail", details)
    except Exception as exc:
        logger.warning("Web operator approval acceptance failed | request_id=%s error_type=%s", request_id, type(exc).__name__)
        return WebAcceptanceCheck(name, "fail", {"request_id": request_id, "error_type": type(exc).__name__})
