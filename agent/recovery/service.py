"""Recovery planning and dispatch. This service never runs warehouse mutations."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5

from agent.recovery.contracts import (
    RECOVERY_DAG_ID, RECOVERY_POLICY_VERSION, SYSTEM_REQUESTER, RecoveryExecution,
    RecoveryExecutionCreate, RecoveryPlan, RecoveryPreview, RecoveryProposal, canonical_hash, choose_runbook,
)
from agent.recovery.evidence import collect_facts, model_fingerprint
from agent.recovery.store import outbox_event, read_execution, read_plan, save_execution
from agent.tools.incident_resolution import _source, initialize, remediation_blockers, scope_for
from agent.tools.incident_resolution_store import (
    ResolutionConflict, ResolutionUnavailable, control_transaction, decoded, encoded, read_incident,
)
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.dq.incident_revalidation import current_contract_hash


def now() -> datetime:
    return datetime.now(timezone.utc)


def _incident(client: Any, reference: str) -> dict:
    snapshot = _source(client, reference)
    with control_transaction() as cursor:
        return initialize(cursor, snapshot)


def facts_hash(facts: dict) -> str:
    """Bind only authoritative inputs, not explanatory language or operator identity."""
    keys = ("source_fingerprint", "input_fingerprint", "derived_fingerprint", "partition_fingerprint")
    return canonical_hash({key: facts.get(key) for key in keys})


def preview_with_facts(reference: str, *, client=None) -> tuple[RecoveryPreview, dict, dict]:
    """Collect current evidence without selecting a provider or performing repair."""
    from apps.common.airflow_api import configured
    client = client or build_clickhouse_client()
    incident = _incident(client, reference)
    scope = scope_for(incident["snapshot"])
    blockers = []
    facts: dict = {}
    available = configured()
    if not available:
        blockers.append("Airflow recovery is not configured.")
    if incident["status"] == "resolved":
        blockers.append("This incident is already resolved.")
    if scope["kind"] != "dq_failure" or scope["table_name"] not in {"dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily"}:
        blockers.append("This incident needs a manual runbook; automatic schema changes are not supported.")
    if not blockers:
        facts = collect_facts(client, incident)
        blockers.extend(facts.get("blockers", []))
        if facts.get("source_valid"):
            from agent.state import Alert
            from agent.context.live import load_trusted_context
            from agent.context.bundle import build_incident_context_bundle
            alert = Alert.from_clickhouse_dict(incident["snapshot"])
            trusted = load_trusted_context(alert, client=client)
            bundle = build_incident_context_bundle(alert, incident_id=trusted.incident_id,
                revision=trusted.revision, trusted_source_metadata=trusted)
            facts["context_complete"] = bundle.context_complete
            if not bundle.context_complete:
                blockers.append("Trusted incident context is incomplete. Collect the missing evidence before planning a repair.")
    with control_transaction() as cursor:
        current = read_incident(cursor, incident["alert_key"])
        if current["revision"] != incident["revision"]:
            raise ResolutionConflict("Incident changed while collecting recovery evidence. Refresh the preview.")
        blockers.extend(remediation_blockers(cursor, client, incident))
        cursor.execute("SELECT p.plan_id FROM dq_control.recovery_plans p JOIN dq_control.approval_requests a "
                       "ON a.request_id=p.approval_request_id WHERE p.alert_key=%s AND a.status IN ('pending','approved') AND a.execution_status<>'failed' "
                       "ORDER BY p.created_at DESC LIMIT 1", (incident["alert_key"],))
        row = cursor.fetchone()
    runbook = choose_runbook(table=scope["table_name"], kind=scope["kind"],
        source_valid=facts.get("source_valid", False), raw_matches=facts.get("raw_matches", False),
        raw_valid=facts.get("raw_valid", False), derived_matches=facts.get("derived_matches", False),
        derived_present=facts.get("derived_present", False))
    if not runbook and not blockers:
        blockers.append("No repair is justified by the current evidence. Check this incident again instead.")
    targets = (["dq.raw_orders"] if runbook == "replay_raw_partition" else
               ["dq.stg_orders", "dq.fct_orders_daily"] if runbook == "rebuild_derived_partition" else
               ["dq.raw_orders", "dq.stg_orders", "dq.fct_orders_daily"] if runbook else [])
    public_source = {key: value for key, value in (facts.get("source_manifest") or {}).items()
                     if key in {"snapshot_id", "version", "sha256", "schema_sha256", "row_count", "dt", "kind"}}
    preview = RecoveryPreview(alert_ref=incident["alert_ref"], incident_revision=incident["revision"],
        eligible=bool(runbook and not blockers), available=available, runbook=runbook, scope=scope,
        targets=targets, blockers=sorted(set(blockers)), source=public_source or None,
        observations={key: facts[key] for key in ("source_valid", "raw_valid", "raw_matches", "derived_matches", "derived_present", "row_counts", "context_complete") if key in facts},
        latest_plan_id=str(row[0]) if row else None)
    return preview, incident, facts


def recovery_preview(reference: str) -> dict:
    return preview_with_facts(reference)[0].model_dump(mode="json")


def create_plan(reference: str, body: RecoveryProposal) -> dict:
    """Create one immutable system proposal and its approval in the same transaction."""
    preview, incident, facts = preview_with_facts(reference)
    if preview.latest_plan_id:
        existing = get_plan(preview.latest_plan_id)
        plan = RecoveryPlan.model_validate(existing["plan"])
        if (plan.incident_revision == body.expected_incident_revision and plan.reason == body.reason
                and plan.facts_hash == facts_hash(facts)):
            return existing
    if body.expected_incident_revision != incident["revision"] or not preview.eligible:
        raise ResolutionConflict("Recovery is blocked: " + "; ".join(preview.blockers or ["Incident revision changed."]))
    scope = preview.scope
    from pipelines.dbt.run_dbt import model_contract_hash
    with control_transaction() as cursor:
        cursor.execute("SELECT revision,watermark FROM dq_control.partition_revisions WHERE dataset='orders' AND partition_dt=%s", (scope["dt"],))
        partition_state = cursor.fetchone() or (0, "")
    core = dict(policy_version=RECOVERY_POLICY_VERSION, runbook=preview.runbook, runbook_version=1,
        alert_ref=incident["alert_ref"], alert_key=incident["alert_key"], incident_revision=incident["revision"],
        incident_episode=incident["episode"], dt=scope["dt"], scope=scope, targets=preview.targets,
        source_manifest=facts["source_manifest"], contract_hash=current_contract_hash(scope),
        model_hash=model_fingerprint(), pipeline_model_hash=model_contract_hash(),
        publication_revision=partition_state[0], publication_watermark=partition_state[1],
        input_fingerprint=facts["input_fingerprint"], partition_fingerprint=facts["partition_fingerprint"], facts_hash=facts_hash(facts),
        verification_scope=scope, validation=["Source checksum and approved input version", "Scoped rule and positive-volume prerequisites", "No missing or unexpected contracted segments"],
        rollback=["Retain previous partitions before publication", "Compensate partial publication using only this execution's backup", "Do not retry an unknown publication outcome"],
        reason=body.reason)
    plan_hash = canonical_hash(core)
    plan_id = str(uuid5(NAMESPACE_URL, "datasentry:recovery:" + plan_hash))
    approval_scope = {"plan_id": plan_id, "plan_hash": plan_hash, "alert_ref": incident["alert_ref"],
                      "alert_key": incident["alert_key"], "dt": scope["dt"], "runbook": preview.runbook}
    from agent.tools.approval_control_store import _canonical_scope
    scope_json, approval_hash = _canonical_scope("recover_partition", approval_scope)
    request_id = "APR-" + approval_hash[:20].upper()
    plan = RecoveryPlan(**core, plan_id=plan_id, plan_hash=plan_hash, created_at=now(), approval_request_id=request_id)
    with control_transaction() as cursor:
        current = read_incident(cursor, incident["alert_key"])
        if current["revision"] != incident["revision"] or current["status"] == "resolved":
            raise ResolutionConflict("Incident changed. Review a new recovery plan.")
        cursor.execute("SELECT record FROM dq_control.recovery_plans WHERE plan_hash=%s", (plan_hash,))
        existing = cursor.fetchone()
        if existing:
            plan = RecoveryPlan.model_validate(decoded(existing[0]))
        else:
            cursor.execute("INSERT INTO dq_control.approval_requests(request_id,idempotency_hash,action_type,scope_json,requested_by,reason) "
                           "VALUES (%s,%s,'recover_partition',%s::jsonb,%s,%s)",
                           (request_id, approval_hash, scope_json, SYSTEM_REQUESTER, body.reason))
            cursor.execute("INSERT INTO dq_control.recovery_plans(plan_id,alert_key,incident_revision,plan_hash,approval_request_id,record) "
                           "VALUES (%s,%s,%s,%s,%s,%s::jsonb)", (plan_id, incident["alert_key"], incident["revision"], plan_hash, request_id, plan.model_dump_json()))
            outbox_event(cursor, plan_id, "recovery_plan_created", {"plan_id": plan_id, "plan_hash": plan_hash,
                         "approval_request_id": request_id, "alert_ref": plan.alert_ref, "alert_key": plan.alert_key,
                         "runbook": plan.runbook, "actor": SYSTEM_REQUESTER})
    return get_plan(str(plan.plan_id))


def get_plan(plan_id: str) -> dict:
    """Return immutable scope alongside current transactional approval state."""
    with control_transaction() as cursor:
        plan = read_plan(cursor, plan_id)
        from agent.tools.approval_control_store import SELECT_COLUMNS, _record
        cursor.execute(f"SELECT {SELECT_COLUMNS} FROM dq_control.approval_requests WHERE request_id=%s", (plan.approval_request_id,))
        approval = asdict(_record(cursor.fetchone()))
        cursor.execute("SELECT record FROM dq_control.recovery_executions WHERE plan_id=%s", (str(plan.plan_id),))
        row = cursor.fetchone()
    return {"plan": plan.model_dump(mode="json"), "approval": decoded(encoded(approval)),
            "execution": get_execution(str(decoded(row[0])["execution_id"])) if row else None}


def verify_plan(plan: RecoveryPlan, incident: dict, client: Any) -> None:
    """Reject stale inputs; never substitute a newer source or ask Gemini to replan."""
    if incident["revision"] != plan.incident_revision or incident["episode"] != plan.incident_episode or incident["status"] == "resolved":
        raise ResolutionConflict("Incident revision changed. A new reviewed plan is required.")
    if current_contract_hash(plan.scope) != plan.contract_hash or model_fingerprint() != plan.model_hash:
        raise ResolutionConflict("Contract or implementation changed. Review and approve a new plan.")
    facts = collect_facts(client, incident, manifest=plan.source_manifest)
    if not facts.get("source_valid") or facts_hash(facts) != plan.facts_hash:
        raise ResolutionConflict("Source or warehouse inputs changed. Review and approve a new plan.")


def create_execution(plan_id: str, body: RecoveryExecutionCreate, *, airflow=None) -> dict:
    """Claim a plan once, then submit one deterministic Airflow run outside the lock."""
    from apps.common.recovery_airflow import RecoveryAirflowClient
    airflow = airflow or RecoveryAirflowClient()
    client = build_clickhouse_client()
    with control_transaction() as cursor:
        plan = read_plan(cursor, plan_id)
        if body.expected_plan_hash != plan.plan_hash:
            raise ResolutionConflict("The confirmed plan hash is stale.")
        cursor.execute("SELECT record FROM dq_control.recovery_executions WHERE plan_id=%s", (str(plan.plan_id),))
        row = cursor.fetchone()
        if row:
            return execution_view(cursor, RecoveryExecution.model_validate(decoded(row[0])))
        incident = read_incident(cursor, plan.alert_key)
        verify_plan(plan, incident, client)
        cursor.execute("SELECT status,version,requested_by,scope_json FROM dq_control.approval_requests WHERE request_id=%s FOR UPDATE",
                       (plan.approval_request_id,))
        approval = cursor.fetchone()
        if (not approval or approval[0] != "approved" or approval[1] != body.expected_approval_version
                or approval[2] != SYSTEM_REQUESTER or decoded(approval[3]).get("plan_hash") != plan.plan_hash):
            raise ResolutionConflict("This exact recovery plan requires a current human approval.")
        eid = str(uuid5(NAMESPACE_URL, "datasentry:execution:" + str(plan.plan_id)))
        execution = RecoveryExecution(execution_id=eid, plan_id=plan.plan_id, plan_hash=plan.plan_hash,
            approval_request_id=plan.approval_request_id, alert_ref=plan.alert_ref, actor=body.actor, status="queued",
            dag_run_id=f"manual__incident_recovery_{eid}", created_at=now())
        cursor.execute("UPDATE dq_control.approval_requests SET execution_status='dispatching',execution_run_id=%s,version=version+1,updated_at=now() "
                       "WHERE request_id=%s AND execution_status='not_started' AND version=%s RETURNING request_id",
                       (execution.dag_run_id, plan.approval_request_id, body.expected_approval_version))
        if not cursor.fetchone():
            raise ResolutionConflict("This approval was already claimed or cancelled.")
        save_execution(cursor, execution)
        outbox_event(cursor, eid, "recovery_execution_requested", {"execution_id": eid, "plan_id": str(plan.plan_id),
                     "alert_ref": plan.alert_ref, "alert_key": plan.alert_key, "actor": body.actor})
    return dispatch_execution(str(execution.execution_id), airflow=airflow)


def dispatch_execution(execution_id: str, *, airflow=None) -> dict:
    """Persist dispatch intent before POST so reconnects can only reconcile it."""
    from apps.common.recovery_airflow import RecoveryAirflowClient
    airflow = airflow or RecoveryAirflowClient()
    with control_transaction() as cursor:
        execution = read_execution(cursor, execution_id)
        if execution.status in {"succeeded", "failed"}:
            return execution.model_dump(mode="json")
        reconcile_only = execution.trigger_state != "pending"
        execution.trigger_state = "claimed"
        save_execution(cursor, execution)
    try:
        outcome = airflow.trigger(execution_id, reconcile_only=reconcile_only)
        uncertain = outcome.get("state") in {"unknown", "not_found"}
        trigger_state, status = ("unknown", "unknown") if uncertain else ("accepted", "queued")
    except ResolutionUnavailable:
        trigger_state, status = "unknown", "unknown"
    with control_transaction() as cursor:
        execution = read_execution(cursor, execution_id)
        if execution.status not in {"running", "succeeded", "failed"}:
            execution.trigger_state, execution.status = trigger_state, status
            save_execution(cursor, execution)
            cursor.execute("UPDATE dq_control.approval_requests SET execution_status=%s,version=version+1,updated_at=now() "
                           "WHERE request_id=%s AND execution_run_id=%s AND execution_status='dispatching'",
                           ("unknown" if status == "unknown" else "dispatched", execution.approval_request_id, execution.dag_run_id))
    return execution.model_dump(mode="json")


def execution_view(cursor: Any, execution: RecoveryExecution) -> dict:
    """Read pending sinks at response time, including idempotent POST responses."""
    cursor.execute("SELECT count(*) FROM dq_control.recovery_outbox WHERE aggregate_id IN (%s,%s) AND NOT (audit_done AND delivery_done)",
                   (str(execution.execution_id), str(execution.plan_id)))
    execution.synchronization_pending = cursor.fetchone()[0] != 0
    return execution.model_dump(mode="json")


def get_execution(execution_id: str) -> dict:
    with control_transaction() as cursor:
        return execution_view(cursor, read_execution(cursor, execution_id))
