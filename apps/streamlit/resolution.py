####
## Evidence-Gated Incident Resolution for the Streamlit Operator Console
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, MutableMapping
from urllib.parse import quote
from uuid import uuid4

import streamlit as st
from apps.streamlit.localization import tr

from apps.common.control_plane import (
    ControlPlaneClient,
    ControlPlaneClientError,
    ControlPlaneResponseError,
    ControlPlaneTransportError,
)


# --- Defining Contracts And Operator Copy
REQUEST_STATUSES = {"queued", "running", "pass", "blocked", "error", "expired"}
ACTIVE_REQUEST_STATUSES = {"queued", "running"}
INCIDENT_STATUSES = {"open", "acknowledged", "triaged", "resolved"}
SUPPORTED_KINDS = {"dq", "data_quality", "dq_failure", "schema_drift"}
ACTIVE_PANEL_KEY = "resolution_active_alert_ref"
RESOLUTION_NOTICE_KEY = "resolution_last_decision_notice"
MAX_EVIDENCE_AGE = timedelta(minutes=10)

STATUS_COPY = {
    "queued": ('Check queued.', 'The incident is not closed. No data has been changed.'),
    "running": ('Check running.', 'Only data or schema conditions are checked. The incident is not closed.'),
    "pass": ('Check passed.', 'The issue was not found in the latest check for this incident.'),
    "blocked": ('An issue remains.', 'The incident cannot be closed yet. Review the evidence and continue investigating or fixing the issue.'),
    "error": ('Check incomplete.', 'There is not enough evidence to close the incident. It remains active.'),
    "expired": ('Check evidence has expired.', 'Check again before closing this incident.'),
}


# --- Defining Pure Session And Validation Helpers
def resolution_session_key(alert_ref: str) -> str:
    """Return an incident-scoped key without exposing system keys in widget IDs."""
    return "resolution_" + sha256(alert_ref.encode("utf-8")).hexdigest()[:20]


def resolution_session(state: MutableMapping[str, Any], alert_ref: str) -> dict[str, Any]:
    """Initialize UI state only; rendering or rerunning never creates a request."""
    key = resolution_session_key(alert_ref)
    if key not in state:
        state[key] = {"open": False, "actor": "", "reason": "", "preview": None,
                      "request": None, "create_attempt": None, "create_previous_request_id": None,
                      "confirmation": None}
    return state[key]


def resolution_panel_is_active(state: MutableMapping[str, Any], alert_ref: str) -> bool:
    """Guard timer reruns after a tab switch, incident switch, or panel closure."""
    panel = state.get(resolution_session_key(alert_ref), {})
    return bool(
        panel.get("open")
        and state.get(ACTIVE_PANEL_KEY) == alert_ref
        and state.get("primary_workspace_tabs") == "Incidents"
        and state.get("incident_detail_tabs") == "Overview"
    )


def _revision(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def validate_preview(payload: Any, alert_ref: str) -> dict[str, Any]:
    """Reject missing or mismatched authoritative state instead of inventing defaults."""
    required = {"alert_ref", "incident_revision", "status", "scope", "blockers",
                "available", "request", "resolution", "synchronization_pending"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ControlPlaneResponseError(tr('Resolution preview is incomplete.'))
    if payload["alert_ref"] != alert_ref or not _revision(payload["incident_revision"]):
        raise ControlPlaneResponseError(tr('Resolution preview identity or revision is invalid.'))
    if payload["status"] not in INCIDENT_STATUSES:
        raise ControlPlaneResponseError(tr('Resolution preview status is invalid.'))
    scope = payload["scope"]
    if not isinstance(scope, dict) or not {"kind", "table_name", "dt", "metric", "dimension"}.issubset(scope):
        raise ControlPlaneResponseError(tr('Resolution scope is incomplete.'))
    if scope["kind"] not in SUPPORTED_KINDS or not isinstance(scope["table_name"], str) or not scope["table_name"]:
        raise ControlPlaneResponseError(tr('Resolution scope is unsupported.'))
    if not isinstance(payload["blockers"], list) or any(not isinstance(item, str) for item in payload["blockers"]):
        raise ControlPlaneResponseError(tr('Resolution blockers must be text entries.'))
    if not all(isinstance(payload[name], bool) for name in ("available", "synchronization_pending")):
        raise ControlPlaneResponseError(tr('Resolution availability is invalid.'))
    if payload["resolution"] is not None and not isinstance(payload["resolution"], dict):
        raise ControlPlaneResponseError(tr('Resolution record is invalid.'))
    if payload["request"] is not None:
        validate_revalidation(payload["request"], alert_ref=alert_ref)
    return payload


def validate_revalidation(
    payload: Any, *, alert_ref: str, request_id: str | None = None,
) -> dict[str, Any]:
    """Validate request identity and status without treating a successful DAG as PASS."""
    required = {"request_id", "alert_ref", "incident_revision", "status", "created_at",
                "finished_at", "expires_at", "dag_run_id", "result", "error_code"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ControlPlaneResponseError(tr('Incident revalidation response is incomplete.'))
    if not isinstance(payload["request_id"], str) or not payload["request_id"]:
        raise ControlPlaneResponseError(tr('Incident revalidation request identity is invalid.'))
    if payload["alert_ref"] != alert_ref or (request_id is not None and payload["request_id"] != request_id):
        raise ControlPlaneResponseError(tr('Incident revalidation belongs to another request or incident.'))
    if not _revision(payload["incident_revision"]) or payload["status"] not in REQUEST_STATUSES:
        raise ControlPlaneResponseError(tr('Incident revalidation revision or status is invalid.'))
    result = payload["result"]
    if result is not None and not isinstance(result, dict):
        raise ControlPlaneResponseError(tr('Incident revalidation result must be an object.'))
    if payload["status"] == "pass":
        if not isinstance(result, dict) or not {"expected", "observed", "summary", "evidence_references"}.issubset(result):
            raise ControlPlaneResponseError(tr('Passing revalidation evidence is incomplete.'))
        if not isinstance(result["summary"], str) or not result["summary"].strip():
            raise ControlPlaneResponseError(tr('Passing revalidation summary is missing.'))
        if result["expected"] is None or result["observed"] is None:
            raise ControlPlaneResponseError(tr('Passing revalidation observations are missing.'))
        references = result["evidence_references"]
        if not isinstance(references, list) or not references or any(not isinstance(ref, str) or not ref.strip() for ref in references):
            raise ControlPlaneResponseError(tr('Passing revalidation evidence references are missing.'))
        if _timestamp(payload["finished_at"]) is None or _timestamp(payload["expires_at"]) is None:
            raise ControlPlaneResponseError(tr('Passing revalidation timestamps are missing.'))
    return payload


def evidence_is_current(request: dict[str, Any], *, now: datetime | None = None) -> bool:
    """Conservatively gate UI controls; the backend performs the final freshness check."""
    finished = _timestamp(request.get("finished_at"))
    expires = _timestamp(request.get("expires_at"))
    current = now or datetime.now(timezone.utc)
    return bool(
        request.get("status") == "pass" and finished and expires
        and current.tzinfo and finished <= current < min(expires, finished + MAX_EVIDENCE_AGE)
    )


def build_resolution_view(
    preview: dict[str, Any], request: dict[str, Any] | None = None,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """Translate backend state into simple operator text and fail-closed controls."""
    validate_preview(preview, str(preview.get("alert_ref") or ""))
    current = request if request is not None else preview["request"]
    if current is not None:
        validate_revalidation(current, alert_ref=preview["alert_ref"])
    resolved = preview["status"] == "resolved"
    same_revision = current is not None and current["incident_revision"] == preview["incident_revision"]
    status = current["status"] if current is not None else "none"
    if status == "pass" and not evidence_is_current(current, now=now):
        status = "expired"
    heading, explanation = STATUS_COPY.get(status, (
        tr('Check before closing the incident.'), tr('This check does not fix or change data.'),
    ))
    if resolved:
        heading, explanation = tr('The incident is already closed.'), tr('Closing the incident does not run a data fix.')
    elif not preview["available"]:
        heading, explanation = tr('Check unavailable.'), tr('Contact the platform administrator. The incident is not closed.')
    elif current is not None and not same_revision:
        heading, explanation = tr('The incident state has changed.'), tr('Previous evidence cannot be used. Check again.')
    result = (current or {}).get("result") or {}
    if resolved:
        resolution = preview["resolution"] or {}
        result = resolution.get("result") or (
            result if current is not None and current["status"] == "pass"
            and current["request_id"] == resolution.get("resolution_request_id") else {
                "expected": resolution.get("expected"), "observed": resolution.get("observed"),
                "summary": resolution.get("summary", ""),
            }
        )
        if not isinstance(result, dict):
            raise ControlPlaneResponseError(tr('Resolution result must be an object.'))
    return {
        "heading": tr(heading), "explanation": tr(explanation), "status": status,
        "resolved": resolved, "scope": preview["scope"], "blockers": preview["blockers"],
        "expected": result.get("expected"), "observed": result.get("observed"),
        "summary": result.get("summary") or "", "resolution": preview["resolution"],
        "synchronization_pending": preview["synchronization_pending"],
        "can_revalidate": preview["available"] and not resolved and not preview["blockers"] and status not in ACTIVE_REQUEST_STATUSES,
        "can_resolve": bool(preview["available"] and not resolved and not preview["blockers"]
                            and same_revision and status == "pass"),
    }


def validate_operator_input(actor: str, reason: str | None = None) -> tuple[str, str | None]:
    """Require a human-entered console identity and a bounded closure reason."""
    normalized_actor = actor.strip()
    normalized_reason = None if reason is None else reason.strip()
    if not normalized_actor or len(normalized_actor) > 120 or any(ord(char) < 32 for char in normalized_actor):
        raise ValueError(tr('Enter an operator name (1-120 characters, no newlines).'))
    if normalized_reason is not None and not 5 <= len(normalized_reason) <= 1000:
        raise ValueError(tr('The closure reason must contain 5-1,000 characters.'))
    return normalized_actor, normalized_reason


def prepare_revalidation_attempt(state: dict[str, Any], actor: str) -> dict[str, str]:
    """Retain the exact identity/key after an ambiguous timeout until explicitly retried."""
    normalized_actor, _ = validate_operator_input(actor)
    attempt = state.get("create_attempt")
    if attempt is None:
        attempt = {"actor": normalized_actor, "idempotency_key": "ui-check-" + uuid4().hex}
        state["create_attempt"] = attempt
        state["create_previous_request_id"] = (state.get("request") or {}).get("request_id")
    elif attempt["actor"] != normalized_actor:
        raise ValueError(tr('The previous request outcome is unknown. Use the same operator name to retry the check.'))
    return dict(attempt)


def reconcile_revalidation_attempt(state: dict[str, Any], request: dict[str, Any] | None) -> None:
    """Clear an uncertain create attempt only after a new durable request is observed."""
    if state.get("create_attempt") is not None and request is not None:
        if request["request_id"] != state.get("create_previous_request_id"):
            state["create_attempt"] = None
            state["create_previous_request_id"] = None


def prepare_resolution_confirmation(
    state: dict[str, Any], preview: dict[str, Any], request: dict[str, Any],
    *, actor: str, reason: str, now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze a decision draft; calling this helper does not close the incident."""
    normalized_actor, normalized_reason = validate_operator_input(actor, reason)
    if not build_resolution_view(preview, request, now=now)["can_resolve"]:
        raise ValueError(tr('Cannot close yet. Check the latest status and evidence.'))
    draft = {"resolution_request_id": request["request_id"],
             "expected_incident_revision": preview["incident_revision"],
             "actor": normalized_actor, "reason": normalized_reason}
    existing = state.get("confirmation")
    if existing is None or any(existing.get(key) != value for key, value in draft.items()):
        state["confirmation"] = {**draft, "idempotency_key": "ui-resolve-" + uuid4().hex}
    return dict(state["confirmation"])


def resolution_error_message(exc: Exception) -> str:
    """Explain API failures without exposing response bodies, headers, or credentials."""
    if isinstance(exc, ControlPlaneTransportError):
        return (tr('The API cannot be reached. The request outcome is unknown. Check Status or retry the same button; do not create another request.'))
    response = getattr(exc.__cause__, "response", None)
    status = getattr(response, "status_code", None)
    return {
        401: tr('Closure access is not authorized. Contact the platform administrator.'),
        403: tr('You do not have access to close this incident.'),
        404: tr('The incident or check was not found. Refresh the incident list.'),
        409: tr('Cannot close yet: state changed, evidence expired, or blockers remain. Check Status, then check again.'),
        422: tr('Invalid input. Check the operator name and closure reason.'),
        503: tr('Check unavailable. The incident remains active; contact the platform administrator.'),
    }.get(status, tr('The API status is uncertain. Check Status before continuing; there is no local closure.'))


# --- Defining Shared API Adapters (No Local Mutation Fallback)
def _client(client: ControlPlaneClient | None) -> ControlPlaneClient:
    if client is None:
        raise ControlPlaneResponseError(tr('Resolution requires the shared control-plane API.'))
    return client


def _alert_path(alert_ref: str) -> str:
    if not alert_ref or len(alert_ref) > 100:
        raise ValueError(tr('Incident reference is missing or invalid.'))
    return "/api/v1/alerts/" + quote(alert_ref, safe="")


def fetch_resolution_preview(client: ControlPlaneClient | None, alert_ref: str) -> dict[str, Any]:
    """Read authoritative incident scope and blockers through the shared API."""
    payload = _client(client).request_json("GET", _alert_path(alert_ref) + "/resolution-preview")
    return validate_preview(payload, alert_ref)


def request_revalidation(
    client: ControlPlaneClient | None, alert_ref: str, *, actor: str, idempotency_key: str,
) -> dict[str, Any]:
    """Submit only identity and idempotency, never caller-supplied SQL or check scope."""
    actor, _ = validate_operator_input(actor)
    api = _client(client)
    payload = api.request_json(
        "POST", _alert_path(alert_ref) + "/revalidations", headers=api.approval_headers(),
        json_body={"actor": actor, "idempotency_key": idempotency_key},
    )
    return validate_revalidation(payload, alert_ref=alert_ref)


def fetch_revalidation(
    client: ControlPlaneClient | None, request_id: str, *, alert_ref: str,
) -> dict[str, Any]:
    """Read the same durable request after reruns, tab switches, or reconnects."""
    if not request_id or len(request_id) > 250:
        raise ValueError(tr('Revalidation request identity is invalid.'))
    payload = _client(client).request_json("GET", "/api/v1/incident-revalidations/" + quote(request_id, safe=""))
    return validate_revalidation(payload, alert_ref=alert_ref, request_id=request_id)


def confirm_resolution(
    client: ControlPlaneClient | None, alert_ref: str, confirmation: dict[str, Any],
    *, confirmed: bool,
) -> dict[str, Any]:
    """Send a human-confirmed draft; only the backend can authorize the transition."""
    required = {"resolution_request_id", "expected_incident_revision", "idempotency_key", "actor", "reason"}
    if not confirmed or set(confirmation) != required:
        raise ValueError(tr('Closure confirmation is required first.'))
    validate_operator_input(confirmation["actor"], confirmation["reason"])
    if not _revision(confirmation["expected_incident_revision"]):
        raise ValueError(tr('Incident revision is invalid.'))
    api = _client(client)
    # Response shape is backend-owned. Refetch the preview before claiming RESOLVED.
    return api.request_json("POST", _alert_path(alert_ref) + "/resolve",
                            headers=api.approval_headers(), json_body=dict(confirmation))


# --- Rendering The Operator Panel
def record_resolution_success(state: MutableMapping[str, Any], alert_ref: str, decision: dict[str, Any]) -> None:
    """Keep the closed identity visible when the active queue selects another row."""
    if decision.get("status") != "resolved" or decision.get("alert_ref") != alert_ref:
        raise ControlPlaneResponseError(tr('API did not confirm closure for this incident.'))
    state[RESOLUTION_NOTICE_KEY] = {"alert_ref": alert_ref,
                                  "synchronization_pending": bool(decision.get("synchronization_pending"))}
    state[ACTIVE_PANEL_KEY] = None
    resolution_session(state, alert_ref).update(open=False, confirmation=None)


def render_resolution_notice() -> None:
    notice = st.session_state.get(RESOLUTION_NOTICE_KEY)
    if not notice:
        return
    st.success(tr("Incident {alert_ref} closed successfully (RESOLVED). No data fix was run.", alert_ref=notice['alert_ref']))
    st.caption(tr('The incident left the active list. Its history remains under the resolved filter; other incidents may still have issues.'))
    if notice.get("synchronization_pending"):
        st.warning(tr('Decision saved. ClickHouse or Discord synchronization is pending.'))
    if st.button(tr('Dismiss closure notice'), key="resolution_dismiss_notice"):
        st.session_state.pop(RESOLUTION_NOTICE_KEY, None)
        st.rerun()


def _show_result(view: dict[str, Any]) -> None:
    status = view["status"]
    if view["resolved"] or view["heading"] == tr('Check passed.'):
        display = st.success
    elif status in {"blocked", "expired", "error"} or (status == "pass" and not view["can_resolve"]):
        display = st.warning
    else:
        display = st.info
    display(view["heading"])
    st.write(view["explanation"])
    if view["synchronization_pending"]:
        st.warning(tr('Synchronization is incomplete. The decision is saved, but ClickHouse or Discord status may still differ.'))
    if view["expected"] is not None or view["observed"] is not None:
        left, right = st.columns(2)
        left.write(tr('**Expected**'))
        left.write(view["expected"] if view["expected"] is not None else tr('Not available'))
        right.write(tr('**Observed**'))
        right.write(view["observed"] if view["observed"] is not None else tr('Not available'))
    if view["summary"] and not view["resolved"]:
        st.write(view["summary"])
    if view["resolved"]:
        resolution = view["resolution"] or {}
        st.write(tr('**Closed by:** ') + str(resolution.get("resolved_by") or resolution.get("actor") or tr('Not available')))
        st.write(tr('**Closed at:** ') + str(resolution.get("resolved_at") or tr('Not available')))
        st.write(tr('**Closure reason**'))
        st.write(str(resolution.get("resolution_reason") or resolution.get("reason") or tr('Not available')))
        st.caption(tr('Closure is based on system checks. No Gemini or LLM call decides Resolve.'))
    for blocker in view["blockers"]:
        st.warning(tr('Cannot close yet: ') + blocker)
    st.caption(tr('Evidence is valid at check time, not a permanent guarantee. This closure does not declare the entire table or warehouse healthy.'))


@st.fragment(run_every="5s")
def _render_resolution_fragment(
    client: ControlPlaneClient | None, alert_ref: str, on_resolved: Callable[[], None] | None = None,
) -> None:
    """Poll only while this incident's Overview panel remains actively open."""
    if not resolution_panel_is_active(st.session_state, alert_ref):
        return
    state = resolution_session(st.session_state, alert_ref)
    prefix = resolution_session_key(alert_ref)
    try:
        preview = fetch_resolution_preview(client, alert_ref)
        state["preview"] = preview
        request = preview["request"]
        if request is not None:
            request = fetch_revalidation(client, request["request_id"], alert_ref=alert_ref)
        reconcile_revalidation_attempt(state, request)
        state["request"] = request
        view = build_resolution_view(preview, request)
    except (ControlPlaneClientError, ValueError) as exc:
        st.warning(resolution_error_message(exc))
        if client is None:
            st.info(tr('Check unavailable. API configuration is required; there is no local closure.'))
        return

    scope = view["scope"]
    st.write(tr('**Issue to close:** ') + (tr('Schema change') if scope["kind"] == "schema_drift" else tr('Data quality')))
    st.write(tr('**Table:** ') + scope["table_name"])
    if scope["dt"]:
        st.write((tr('**Detection date:** ') if scope["kind"] == "schema_drift" else tr('**Data date:** ')) + str(scope["dt"]))
    _show_result(view)
    if st.button(tr('Check Status'), key=prefix + "_refresh"):
        st.rerun(scope="fragment")
    if view["resolved"]:
        if st.button(tr('Refresh incident list'), key=prefix + "_refresh_queue"):
            if on_resolved is not None:
                on_resolved()
            st.rerun()
        return

    state["actor"] = st.text_input(tr('Operator name'), value=state["actor"], max_chars=120,
                                      key=prefix + "_actor", help=tr('This name is recorded. The local console does not use SSO yet.'),
                                  )
    st.caption(tr('This console trusts the name you enter; individual identity is not verified yet.'))
    try:
        validate_operator_input(state["actor"])
        valid_actor = True
    except ValueError:
        valid_actor = False
    authorized = client is not None and bool(client.approval_token)
    if not authorized:
        st.info(tr('Closure access is not configured. Contact the platform administrator.'))
    if st.button(tr('Check Again'), key=prefix + "_check",
                 disabled=not (view["can_revalidate"] and valid_actor and authorized)):
        try:
            attempt = prepare_revalidation_attempt(state, state["actor"])
            state["request"] = request_revalidation(client, alert_ref, **attempt)
            state["create_attempt"] = None
            state["create_previous_request_id"] = None
            state["confirmation"] = None
            st.rerun(scope="fragment")
        except (ControlPlaneClientError, ValueError) as exc:
            st.warning(str(exc) if isinstance(exc, ValueError) else resolution_error_message(exc))
    if request is not None:
        st.caption(tr('Check result: ') + str(view["status"]).upper() + tr('. DagRun status alone is not PASS evidence.'))
    if not view["can_resolve"]:
        state["confirmation"] = None
        return

    state["reason"] = st.text_area(tr('Closure reason'), value=state["reason"], max_chars=1000,
                                      key=prefix + "_reason", placeholder=tr('What was checked and why can the incident be closed?'),
                                  )
    try:
        actor, reason = validate_operator_input(state["actor"], state["reason"])
        valid_reason = True
    except ValueError:
        actor, reason, valid_reason = "", "", False
    confirmation = state["confirmation"]
    if confirmation and (confirmation["actor"] != actor or confirmation["reason"] != reason
                         or confirmation["resolution_request_id"] != request["request_id"]
                         or confirmation["expected_incident_revision"] != preview["incident_revision"]):
        state["confirmation"] = confirmation = None
    if st.button(tr('Continue to confirmation'), key=prefix + "_prepare",
                 disabled=not (valid_reason and authorized)):
        state["confirmation"] = prepare_resolution_confirmation(state, preview, request, actor=actor, reason=reason)
        st.rerun(scope="fragment")
    if state["confirmation"] is not None:
        st.write(tr('**Confirm closure**'))
        st.write(tr('Closed by: ') + actor)
        st.write(reason)
        accepted = st.checkbox(tr('I agree to close this incident based on a passing check, not to fix data.'),
                                  key=prefix + "_confirm_" + state["confirmation"]["idempotency_key"],
                              )
        if st.button(tr('Confirm Closure'), key=prefix + "_close", type="primary",
                     disabled=not (accepted and authorized)):
            try:
                decision = confirm_resolution(client, alert_ref, state["confirmation"], confirmed=accepted)
                record_resolution_success(st.session_state, alert_ref, decision)
                if on_resolved is not None:
                    on_resolved()
                st.rerun()
            except (ControlPlaneClientError, ValueError) as exc:
                st.warning(resolution_error_message(exc))


def render_resolution_panel(
    alert: dict[str, Any], client: ControlPlaneClient | None,
    *, on_resolved: Callable[[], None] | None = None,
) -> None:
    """Add an explicit panel toggle to Overview without changing the existing theme."""
    alert_ref = str(alert.get("alert_display_id") or "").strip()
    if not alert_ref:
        st.info(tr('Check unavailable. This incident has no Alert Ref yet.'))
        return
    state = resolution_session(st.session_state, alert_ref)
    st.session_state[ACTIVE_PANEL_KEY] = alert_ref
    st.subheader(tr('Evidence-gated Resolve'))
    st.caption(tr('Resolve means the latest check passed and the operator agrees to close the incident. Resolve is not a data fix.'))
    prefix = resolution_session_key(alert_ref)
    if st.button(tr('Close panel') if state["open"] else tr('View closure') if alert.get("status") == "resolved" else tr('Resolve'),
                 key=prefix + "_toggle"):
        state["open"] = not state["open"]
        st.rerun()
    if state["open"]:
        _render_resolution_fragment(client, alert_ref, on_resolved)


def render_resolution_technical_details(alert: dict[str, Any], client: ControlPlaneClient | None) -> None:
    """Keep request/run identifiers and evidence references inside Audit & Recovery."""
    alert_ref = str(alert.get("alert_display_id") or "").strip()
    if not alert_ref:
        return
    with st.expander(tr('Incident closure - technical references'), expanded=False,
                     key=resolution_session_key(alert_ref) + "_technical", on_change="rerun") as details:
        if not details.open:
            return
        try:
            preview = fetch_resolution_preview(client, alert_ref)
        except (ControlPlaneClientError, ValueError) as exc:
            st.info(resolution_error_message(exc))
            return
        request = preview["request"] or {}
        st.json({"Alert Ref": alert_ref, "incident_revision": preview["incident_revision"],
                 "resolution_request_id": request.get("request_id"), "validation_dag_run_id": request.get("dag_run_id"),
                 "validation_status": request.get("status"), "finished_at": request.get("finished_at"),
                 "expires_at": request.get("expires_at"), "evidence": (request.get("result") or {}).get("evidence_references", []),
                 "error_code": request.get("error_code"), "resolution": preview["resolution"],
                 "synchronization_pending": preview["synchronization_pending"]})
