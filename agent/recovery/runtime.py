"""Airflow-owned recovery execution. No model or generator is imported here."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from agent.recovery.contracts import canonical_hash
from agent.recovery.evidence import BUSINESS_COLUMNS, MAX_ROWS, business_fingerprint, require_source_contract, partition_fingerprint
from agent.recovery.service import verify_plan
from agent.recovery.store import outbox_event, read_execution, read_plan, save_execution
from agent.tools.incident_resolution_store import ResolutionConflict, control_transaction, read_incident
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal
from pipelines.common.logging import logger


def require_raw_only_scope(client: Any, dt: Any) -> None:
    """Do not replace raw alone while existing dependents would become stale."""
    for table in ("dq.stg_orders", "dq.fct_orders_daily"):
        rows = client.query(f"SELECT count() FROM {table} WHERE dt={format_date_literal(dt)}").result_rows
        if not rows or int(rows[0][0]) != 0:
            raise ResolutionConflict("Existing derived data requires a reviewed replay-and-rebuild plan.")


def validate_built_recovery(context: dict[str, Any]) -> None:
    """Reject incomplete staged output before any live partition is replaced."""
    import pandas as pd
    from pipelines.dq.config import OrdersDqContract, load_orders_dq_contract
    from pipelines.dq import run_checks
    from pipelines.seeding.snapshots import validate_recovery_source
    client, dt = context["client"], context["lock"].partition_dt
    if partition_fingerprint(client, dt) != context["plan"]["expected_partitions_fingerprint"]:
        raise ResolutionConflict("Live inputs changed while building staged recovery output.")
    source = require_source_contract(context["source_frame"], dt)
    source_hash = business_fingerprint(source)
    targets = {item.target_table: item.stage_table for item in context["prepared"]}
    for live in ("dq.raw_orders", "dq.stg_orders"):
        stage = targets.get(live)
        if stage:
            rows = client.query(f"SELECT {','.join(BUSINESS_COLUMNS)} FROM {stage} "
                                f"WHERE dt={format_date_literal(dt)} LIMIT {MAX_ROWS + 1}").result_rows
            frame = pd.DataFrame(rows, columns=BUSINESS_COLUMNS)
            require_source_contract(frame, dt)
            if business_fingerprint(frame) != source_hash:
                raise ResolutionConflict("Staged business data does not match the approved source.")
    if context.get("build") is None:
        require_raw_only_scope(client, dt)
        return
    payload = load_orders_dq_contract().model_dump(mode="json")
    def replace_tables(value):
        if isinstance(value, dict):
            return {key: replace_tables(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_tables(item) for item in value]
        return targets.get(value, value) if isinstance(value, str) else value
    contract = OrdersDqContract.model_validate(replace_tables(payload))
    timestamp = datetime.now(timezone.utc)
    checks = [*run_checks.check_not_null(client, contract, dt, timestamp),
              *run_checks.check_accepted_values(client, contract, dt, timestamp),
              run_checks.check_segment_coverage(client, contract, dt, timestamp),
              run_checks.check_revenue_non_negative(client, contract, dt, timestamp)]
    if any(check.status != "pass" for check in checks):
        raise ResolutionConflict("Built recovery output failed the contracted prerequisites.")
    source_table = targets.get("dq.raw_orders", context["build"].input_table)
    mart = targets["dq.fct_orders_daily"]
    sums = client.query("SELECT count(),uniqExact(order_id),CAST(sum(recognized_revenue_usd) AS Decimal(18,2)) "
                        f"FROM {source_table} WHERE dt={format_date_literal(dt)} AND is_test=0").result_rows[0]
    observed = client.query("SELECT sum(row_count),sum(distinct_order_count),sum(recognized_revenue_usd) "
                            f"FROM {mart} WHERE dt={format_date_literal(dt)}").result_rows[0]
    if tuple(sums) != tuple(observed) or not sums[0]:
        raise ResolutionConflict("Mart totals do not reconcile to the pinned source.")


def run_execution(execution_id: str, *, client=None, work=None, verify=None) -> dict:
    """Single-use mutation boundary. A crash never grants permission to replay writes."""
    from pipelines.common.publication import PublicationUnknown
    from pipelines.dbt.run_dbt import execute_recovery_pipeline
    from pipelines.dq.incident_revalidation import capture_revalidation
    client = client or build_clickhouse_client()
    with control_transaction() as cursor:
        execution = read_execution(cursor, execution_id)
        plan = read_plan(cursor, str(execution.plan_id))
        if execution.status in {"succeeded", "failed"}:
            return execution.model_dump(mode="json")
        if execution.status == "running":
            raise ResolutionConflict("Execution was already started; inspect its existing publication journal.")
        if execution.status == "unknown" and execution.finished_at is not None:
            raise ResolutionConflict("Publication outcome is uncertain; execution cannot be replayed.")
        core = plan.model_dump(mode="json", exclude={"plan_id", "plan_hash", "created_at", "approval_request_id"})
        if canonical_hash(core) != plan.plan_hash:
            raise ResolutionConflict("Immutable recovery plan hash does not match its payload.")
        incident = read_incident(cursor, plan.alert_key)
        verify_plan(plan, incident, client)
        cursor.execute("SELECT status,execution_status,execution_run_id,scope_json FROM dq_control.approval_requests "
                       "WHERE request_id=%s FOR UPDATE", (plan.approval_request_id,))
        approval = cursor.fetchone()
        from agent.tools.incident_resolution_store import decoded
        if (not approval or approval[0] != "approved" or approval[1] not in {"dispatching", "dispatched", "unknown"}
                or approval[2] != execution.dag_run_id or decoded(approval[3]).get("plan_hash") != plan.plan_hash):
            raise ResolutionConflict("Execution does not own this approved plan.")
        execution.status, execution.trigger_state = "running", "accepted"
        save_execution(cursor, execution)
        outbox_event(cursor, execution_id, "recovery_execution_started", {"execution_id": execution_id,
            "alert_key": plan.alert_key, "alert_ref": plan.alert_ref, "runbook": plan.runbook, "actor": execution.actor})
    try:
        started_at = datetime.now(timezone.utc)
        pipeline_plan = {"runbook": plan.runbook, "partition_dt": plan.dt.isoformat(),
            "snapshot_manifest": plan.source_manifest, "expected_revision": plan.publication_revision,
            "expected_watermark": plan.publication_watermark, "expected_model_hash": plan.pipeline_model_hash,
            "expected_partitions_fingerprint": plan.partition_fingerprint,
            "compensation_preapproved": True}
        publication = (work or execute_recovery_pipeline)(pipeline_plan, execution_id,
            client=client, validate_recovery=validate_built_recovery)
        from pipelines.common.pipeline_runs import write_pipeline_run
        for target in plan.targets:
            write_pipeline_run(client, job_name="recovery_publication_" + plan.runbook, partition_dt=plan.dt,
                status="success", started_at=started_at, ended_at=datetime.now(timezone.utc), target_table=target,
                metadata={"execution_id": execution_id, "plan_hash": plan.plan_hash,
                          "source_snapshot_id": plan.source_manifest["snapshot_id"], "generator_executed": False,
                          "phase": "publication", "business_validation": "not_checked"})
        result = (verify or capture_revalidation)(client, plan.verification_scope)
        write_pipeline_run(client, job_name="recovery_verification_" + plan.runbook, partition_dt=plan.dt,
            status="success" if result["status"] == "pass" else "warning", started_at=started_at,
            ended_at=datetime.now(timezone.utc), target_table=plan.scope["table_name"],
            metadata={"execution_id": execution_id, "phase": "verification", "business_validation": result["status"]})
        status, validation_status, error_code = "succeeded", result["status"], ""
    except Exception as exc:
        publication, result = None, None
        status = "unknown" if isinstance(exc, PublicationUnknown) else "failed"
        validation_status, error_code = "error", type(exc).__name__
        _finish(execution_id, status, validation_status, error_code, result, publication)
        logger.error("Recovery execution did not complete | execution_id=%s error_code=%s", execution_id, error_code)
        raise
    return _finish(execution_id, status, validation_status, error_code, result, publication)


def _finish(execution_id: str, status: str, validation_status: str, error_code: str,
            result: dict | None, publication: dict | None) -> dict:
    with control_transaction() as cursor:
        execution = read_execution(cursor, execution_id)
        execution.status = status
        execution.validation_status = validation_status
        execution.validation = result
        execution.error_code = error_code
        execution.finished_at = datetime.now(timezone.utc)
        save_execution(cursor, execution)
        cursor.execute("UPDATE dq_control.approval_requests SET execution_status=%s,execution_error=%s,version=version+1,updated_at=now() "
                       "WHERE request_id=%s AND execution_run_id=%s AND execution_status IN ('dispatching','dispatched','unknown')",
                       (status, error_code, execution.approval_request_id, execution.dag_run_id))
        plan = read_plan(cursor, str(execution.plan_id))
        outbox_event(cursor, execution_id, "recovery_execution_completed", {"execution_id": execution_id,
            "alert_key": plan.alert_key, "alert_ref": plan.alert_ref, "actor": execution.actor,
            "status": status, "validation_status": validation_status, "error_code": error_code,
            "publications": (publication or {}).get("publications", []),
            "evidence_references": (result or {}).get("evidence_references", [])})
    return execution.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one already-approved immutable recovery plan.")
    parser.add_argument("--execution-id", required=True, type=UUID)
    args = parser.parse_args()
    print(run_execution(str(args.execution_id)))


if __name__ == "__main__":
    main()
