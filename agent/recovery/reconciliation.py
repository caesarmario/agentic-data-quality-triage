"""Read-only Airflow reconciliation. Never dispatch, retry, clear, or repair data."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent.recovery.contracts import RECOVERY_DAG_ID
from agent.recovery.store import outbox_event, read_execution, save_execution
from agent.tools.incident_resolution_store import control_transaction, decoded
from apps.common.recovery_airflow import RecoveryAirflowClient


def flush_incident_outbox(limit: int = 50) -> int:
    """Replay existing projection/delivery intents, never create a lifecycle decision."""
    from agent.tools.incident_resolution import synchronize_incident
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Incident replay limit must be between one and fifty')
    with control_transaction() as cursor:
        cursor.execute("SELECT alert_key FROM dq_control.incident_outbox "
                       "WHERE NOT (projection_done AND audit_done AND delivery_done) "
                       "GROUP BY alert_key ORDER BY min(created_at) LIMIT %s", (limit,))
        keys = [row[0] for row in cursor.fetchall()]
    for key in keys:
        synchronize_incident(key)
    with control_transaction() as cursor:
        cursor.execute("SELECT count(*) FROM dq_control.incident_outbox "
                       "WHERE NOT (projection_done AND audit_done AND delivery_done)")
        return cursor.fetchone()[0]


def inspect_dispatched_backfill(
    *, airflow: RecoveryAirflowClient, approval_id: str, parent_run_id: str,
    parent_run: dict[str, Any], approved_scope: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate approved scope and inspect its bounded deterministic child runs.

    ``None`` means the observed Airflow state is incomplete or does not match the
    durable approval. This helper is read-only and never dispatches child runs.
    """
    from agent.tools.approval_queue import (
        BACKFILL_DISPATCHER_DAG_ID,
        backfill_parameters_from_conf,
        normalize_backfill_parameters,
    )
    from dags.dq_platform.backfill_dispatcher import (
        ALLOWED_TARGET_DAG_IDS,
        build_child_run_id,
        iter_backfill_dates,
        parse_business_date,
        validate_target_dag_id,
    )

    conf = parent_run.get("conf")
    request = approved_scope.get("_approval_request", approved_scope)
    if (not isinstance(conf, dict) or not isinstance(request, dict)
            or parent_run.get("dag_run_id") != parent_run_id
            or conf.get("approval_request_id") != approval_id
            or request.get("dispatcher_dag_id") != BACKFILL_DISPATCHER_DAG_ID
            or request.get("dry_run") is not False):
        return None
    try:
        approved_target = validate_target_dag_id(str(request["target_dag_id"]))
        run_target = validate_target_dag_id(str(conf["target_dag_id"]))
        start = parse_business_date(str(request["start_date"]))
        end = parse_business_date(str(request["end_date"]))
        run_start = parse_business_date(str(conf["start_date"]))
        run_end = parse_business_date(str(conf["end_date"]))
        approved_parameters = normalize_backfill_parameters(request["parameters"])
        run_parameters = backfill_parameters_from_conf(conf)
        dates = iter_backfill_dates(start, end, approved_parameters["max_dates"])
    except (KeyError, TypeError, ValueError):
        return None
    if (approved_target not in ALLOWED_TARGET_DAG_IDS
            or run_target != approved_target or run_start != start or run_end != end
            or run_parameters != approved_parameters or not dates or len(dates) > 90):
        return None

    parent_state = parent_run.get("state")
    if parent_state not in {"success", "failed"}:
        return None
    if run_parameters["wait_for_completion"]:
        return {
            "status": "succeeded" if parent_state == "success" else "failed",
            "target_dag_id": approved_target,
            "child_run_count": len(dates),
            "child_runs_inspected": 0,
            "child_runs_missing": 0,
            "child_failures": 0,
            "wait_for_completion": True,
        }

    failed = 0
    inspected = 0
    missing = 0
    unresolved = False
    for partition_dt in dates:
        child_run_id = build_child_run_id(approved_target, partition_dt, parent_run_id)
        child = airflow.inspect(approved_target, child_run_id)
        if child is None:
            missing += 1
            if parent_state == "success":
                unresolved = True
            continue
        child_conf = child.get("conf")
        if (child.get("dag_run_id") != child_run_id or not isinstance(child_conf, dict)
                or child_conf.get("approval_request_id") != approval_id
                or child_conf.get("target_dag_id") != approved_target
                or child_conf.get("dt") != partition_dt.isoformat()
                or child_conf.get("backfill_dispatcher") != BACKFILL_DISPATCHER_DAG_ID):
            return None
        child_state = child.get("state")
        if child_state not in {"success", "failed"}:
            unresolved = True
            continue
        inspected += 1
        failed += child_state == "failed"

    if unresolved:
        return None

    # A failed parent can stop dispatch early. Still wait for every found child
    # above to become terminal before finalizing the failed approval.
    status = "failed" if parent_state == "failed" or failed else "succeeded"
    return {
        "status": status,
        "target_dag_id": approved_target,
        "child_run_count": len(dates),
        "child_runs_inspected": inspected,
        "child_runs_missing": missing,
        "child_failures": failed,
        "wait_for_completion": False,
    }


def reconcile(*, airflow=None, limit: int = 50) -> dict[str, int]:
    """Derive execution state from the existing run; business PASS needs stored proof."""
    if not 1 <= limit <= 100:
        raise ValueError("Reconciliation limit must be between 1 and 100.")
    airflow = airflow or RecoveryAirflowClient()
    with control_transaction() as cursor:
        cursor.execute("SELECT execution_id,record FROM dq_control.recovery_executions "
                       "WHERE status IN ('queued','running','unknown') ORDER BY created_at LIMIT %s", (limit,))
        executions = cursor.fetchall()
        cursor.execute("SELECT request_id,action_type,execution_run_id,scope_json FROM dq_control.approval_requests "
                       "WHERE execution_status IN ('dispatching','dispatched','unknown') AND action_type IN ('rerun_dbt','backfill') "
                       "ORDER BY created_at LIMIT %s", (limit,))
        approvals = cursor.fetchall()
    counts = {"inspected": 0, "reconciled": 0, "unknown": 0}
    for eid, record in executions:
        record = decoded(record)
        run = airflow.inspect(RECOVERY_DAG_ID, record["dag_run_id"])
        counts["inspected"] += 1
        if not run:
            counts["unknown"] += 1
            continue
        if run.get("conf", {}).get("execution_id") != str(eid):
            counts["unknown"] += 1
            continue
        state = run.get("state")
        with control_transaction() as cursor:
            execution = read_execution(cursor, str(eid))
            if execution.status in {"succeeded", "failed"}:
                continue
            cursor.execute("SELECT count(*) FROM dq_control.partition_publications WHERE execution_id=%s "
                           "AND status IN ('publishing','unknown','compensating')", (str(eid),))
            uncertain_publication = cursor.fetchone()[0] > 0
            if uncertain_publication:
                execution.status, execution.error_code = "unknown", "PublicationUnknown"
                cursor.execute("UPDATE dq_control.approval_requests SET execution_status='unknown',execution_error='PublicationUnknown',"
                               "version=version+1,updated_at=now() WHERE request_id=%s AND execution_status IN ('dispatching','dispatched')",
                               (execution.approval_request_id,))
            elif state == "failed" and execution.error_code != "PublicationUnknown":
                execution.status, execution.error_code = "failed", "AirflowExecutionFailed"
                execution.finished_at = datetime.now(timezone.utc)
            elif state == "success" and execution.validation is None:
                execution.status, execution.error_code = "unknown", "TerminalRunMissingVerification"
            elif state == "running":
                execution.trigger_state = "accepted"
            save_execution(cursor, execution)
            if execution.status == "failed":
                cursor.execute("UPDATE dq_control.approval_requests SET execution_status='failed',execution_error=%s,"
                               "version=version+1,updated_at=now() WHERE request_id=%s AND execution_status IN ('dispatching','dispatched','unknown')",
                               (execution.error_code, execution.approval_request_id))
            counts["reconciled"] += 1
    for rid, action, owner_run_id, scope in approvals:
        scope = decoded(scope)
        if action == "rerun_dbt":
            dag = "20_dag_dq_orders_dbt_transform"
            run_id = "approved_rerun_dbt__" + rid.lower()
        else:
            dag, run_id = "90_dag_dq_platform_backfill_dispatcher", owner_run_id
        if not run_id:
            continue
        run = airflow.inspect(dag, run_id)
        counts["inspected"] += 1
        if not run or run.get("state") not in {"success", "failed"}:
            counts["unknown"] += 1
            continue
        if run.get("conf", {}).get("approval_request_id") != rid:
            counts["unknown"] += 1
            continue
        backfill_observation = None
        if action == "backfill":
            backfill_observation = inspect_dispatched_backfill(
                airflow=airflow, approval_id=rid, parent_run_id=run_id,
                parent_run=run, approved_scope=scope,
            )
            if backfill_observation is None:
                counts["unknown"] += 1
                continue
            terminal = backfill_observation["status"]
        else:
            terminal = "succeeded" if run["state"] == "success" else "failed"
        with control_transaction() as cursor:
            cursor.execute("UPDATE dq_control.approval_requests SET execution_status=%s,version=version+1,updated_at=now() "
                           "WHERE request_id=%s AND execution_status IN ('dispatching','dispatched','unknown') RETURNING request_id",
                           (terminal, rid))
            if cursor.fetchone():
                outbox_event(cursor, rid, "approved_execution_reconciled", {"approval_request_id": rid,
                    "dag_id": dag, "dag_run_id": run_id, "status": terminal, "actor": "airflow_reconciler",
                    "observed_end_date": run.get("end_date"), "business_validation": "not_implied",
                    **({key: value for key, value in backfill_observation.items() if key != "status"}
                       if backfill_observation else {})})
                counts["reconciled"] += 1
    from agent.tools.approval_queue import flush_approval_projections
    flush_approval_projections(limit=limit)
    from agent.recovery.outbox import flush
    counts["outbox_pending"] = flush(limit)["pending"]
    counts["incident_outbox_pending"] = flush_incident_outbox(limit=min(limit, 50))
    return counts


def main() -> None:
    print(json.dumps(reconcile(), sort_keys=True))


if __name__ == "__main__":
    main()
