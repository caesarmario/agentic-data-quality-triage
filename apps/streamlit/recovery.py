"""Operator recovery panel. Every decision goes through the shared authenticated API."""
from __future__ import annotations

from typing import Any

import streamlit as st

from apps.common.recovery import RecoveryClient, can_decide, can_execute, recovery_error_message
from apps.streamlit.localization import tr


def recovery_session(state, alert_ref: str) -> dict:
    """Keep locale changes and rerenders from changing the reviewed plan identity."""
    return state.setdefault("recovery:" + alert_ref, {"active": False, "bundle": None, "execution": None})


def _render(alert_ref: str, api: Any) -> None:
    state = recovery_session(st.session_state, alert_ref)
    if not state["active"]:
        return
    client = RecoveryClient(api)
    st.subheader(tr("Recover this incident"))
    st.caption(tr("Review Fix → Approve Plan → Run Approved Fix → Check Result → Close Incident"))
    st.info(tr("Recovery replays saved input or rebuilds derived data. It does not generate a clean baseline or close the incident."))
    actor = st.text_input(tr("Operator name"), key="recovery_actor:" + alert_ref)
    st.caption(tr("Trusted local console: this name is recorded, not verified by SSO."))
    try:
        if state["execution"]:
            state["execution"] = client.execution(state["execution"]["execution_id"], plan=state["bundle"]["plan"])
        bundle = state["bundle"]
        if bundle is None:
            preview = client.preview(alert_ref)
            st.write(tr("Business date") + ": " + str(preview["scope"].get("dt", "")))
            if preview.get("latest_plan_id"):
                if st.button(tr("Open reviewed plan"), key="recovery_open:" + alert_ref):
                    state["bundle"] = client.plan(preview["latest_plan_id"], alert_ref=alert_ref)
                    st.rerun()
            elif preview["eligible"]:
                st.write(tr("Runbook") + ": " + str(preview["runbook"]))
                reason = st.text_area(tr("Why should this recovery be reviewed?"), key="recovery_reason:" + alert_ref)
                if st.button(tr("Create recovery plan"), key="recovery_propose:" + alert_ref):
                    state["bundle"] = client.propose(alert_ref, reason=reason, expected_incident_revision=preview["incident_revision"])
                    st.rerun()
            else:
                st.warning(tr("Recovery is blocked"))
                for blocker in preview["blockers"]:
                    st.write("- " + tr(blocker))
        if bundle:
            bundle = client.plan(bundle["plan"]["plan_id"], alert_ref=alert_ref)
            state["bundle"] = bundle
            if bundle.get("execution"):
                state["execution"] = bundle["execution"]
            plan, approval = bundle["plan"], bundle["approval"]
            st.write(tr("Runbook") + ": " + plan["runbook"])
            st.write(tr("Business date") + ": " + plan["dt"])
            st.write(tr("Affected tables") + ": " + ", ".join(plan["targets"]))
            st.write(tr("Approval") + ": " + approval["status"].upper())
            st.write(plan["reason"])
            with st.expander(tr("Validation and rollback")):
                for item in plan["validation"] + plan["rollback"]:
                    st.write("- " + tr(item))
                st.code(plan["source_manifest"]["key"], language=None)
            if can_decide(bundle, actor):
                comment = st.text_input(tr("Decision reason"), key="recovery_decision_reason:" + alert_ref)
                confirmed = st.checkbox(tr("I reviewed this exact scope"), key="recovery_approval_confirm:" + plan["plan_id"])
                approve, reject = st.columns(2)
                if approve.button(tr("Approve Plan"), disabled=not confirmed, key="recovery_approve:" + alert_ref):
                    state["bundle"] = client.decide(bundle, actor=actor, decision="approve", comment=comment)
                    st.rerun()
                if reject.button(tr("Reject"), key="recovery_reject:" + alert_ref):
                    state["bundle"] = client.decide(bundle, actor=actor, decision="reject", comment=comment)
                    st.rerun()
            if can_execute(bundle) and not state["execution"]:
                confirm = st.checkbox(tr("Run only the approved source version, date, and tables"), key="recovery_run_confirm:" + plan["plan_id"])
                if st.button(tr("Run Approved Fix"), disabled=not confirm, key="recovery_run:" + alert_ref, type="primary"):
                    state["execution"] = client.execute(bundle, actor=actor, confirm_scope=confirm)
                    st.rerun()
            execution = state["execution"]
            if execution:
                st.write(tr("Execution") + ": " + execution["status"].upper())
                st.write(tr("Data verification") + ": " + execution["validation_status"].upper())
                if execution["synchronization_pending"]:
                    st.warning(tr("Decision stored; audit or Discord delivery is still pending."))
                if execution["validation_status"] == "pass":
                    st.success(tr("The scoped check passed. Use Resolve for a fresh closure check and final confirmation."))
                elif execution["validation_status"] == "blocked":
                    st.warning(tr("The job finished, but this incident still fails verification. It remains active."))
                elif execution["status"] == "unknown":
                    st.warning(tr("The outcome is uncertain. Inspect the existing execution; do not start another fix."))
                if execution["error_code"]:
                    st.error(tr("Recovery needs operator attention."))
                with st.expander(tr("Technical details")):
                    st.json({key: execution[key] for key in ("execution_id", "dag_run_id", "error_code")})
        if st.button(tr("Close recovery panel"), key="recovery_panel_close:" + alert_ref):
            state["active"] = False
            st.rerun()
    except Exception as exc:
        st.error(tr(recovery_error_message(exc)))
        st.caption(tr("No automatic retry was performed. Reopen the existing plan or execution to inspect its state."))


def render_recovery_panel(alert: dict, api: Any) -> None:
    """Only an opened panel polls; switching language does not dispatch anything."""
    alert_ref = str(alert.get("alert_display_id") or "")
    if not alert_ref or str(alert.get("status", "")).lower() == "resolved":
        return
    state = recovery_session(st.session_state, alert_ref)
    if st.button(tr("Review Fix"), key="recovery_start:" + alert_ref):
        state["active"] = True
    if state["active"]:
        execution = state.get("execution") or {}
        interval = 5 if execution.get("status") in {"queued", "running"} else None
        st.fragment(run_every=interval)(_render)(alert_ref, api)
