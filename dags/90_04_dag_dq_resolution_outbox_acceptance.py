####
## Airflow Resolution Outbox Acceptance DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.sdk import DAG, Param

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)


# --- Defining DAG ID And Documentation
logger = logging.getLogger(__name__)
DAG_ID = "90_04_dag_dq_resolution_outbox_acceptance"
ACCEPTANCE_RUN_ID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
BUSINESS_DATE_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
ACCEPTANCE_STAGES = (
    "prepare", "capture", "restore", "request", "wait", "close_fault", "replay", "verify",
)

DOC_MD = """
# 90.04 - Resolution Outbox Acceptance

Manual-only, bounded acceptance against real PostgreSQL, ClickHouse, and Discord.
Supply a required `acceptance_run_id` UUID and `business_date` in YYYY-MM-DD format.
The platform start date uses the shared Asia/Bangkok timezone. Retries are disabled;
one active run is allowed and the DagRun has a 25-minute timeout.

This is not a read-only demo: it explicitly mutates columns of the owned
`dq_resolution_acceptance.schema_fixture` for setup/restore only. Restoration
runs with `all_done`, including after capture failure. Capture must also succeed
before requesting revalidation; successful cleanup cannot bypass that gate.
The deadline or worker loss can interrupt cleanup, so inspect restoration evidence
after any interrupted run rather than assuming the fixture was restored.

The deterministic read-only check runs through child
`90_03_dag_dq_incident_revalidation`. Fresh explicit pass evidence is required.
`confirm_fixture_closure` defaults to false. Setting it to true explicitly
authorizes owner-delegated demo closure with the exact same evidence gate as
normal incident closure; it does not waive permissions, freshness, or pass checks.

The delivery failure is a synthetic application fault BEFORE the Discord call,
not an actual Discord outage. Replay then verifies pending-outbox recovery using
the real delivery path. Control state, evidence, audit, and Discord messages are
written by the fixed acceptance workflow. No LLM calls, raw orders mutation, or
remediation are performed. No caller-selected hosts, tables, keys, SQL, or models
are accepted. All stages delegate to the fixed acceptance script in `/app`.
"""


# --- Defining DAG Parameters
def resolution_outbox_acceptance_params() -> dict[str, Param]:
    """Require validated fixture identity/date and explicit demo closure consent."""
    return {
        "acceptance_run_id": Param(
            type="string", minLength=36, maxLength=36,
            pattern=ACCEPTANCE_RUN_ID_PATTERN, format="uuid",
            description="Required UUID identifying this owned fixture acceptance run.",
        ),
        "business_date": Param(
            type="string", minLength=10, maxLength=10,
            pattern=BUSINESS_DATE_PATTERN, format="date",
            description="Required fixture business date in YYYY-MM-DD format.",
        ),
        "confirm_fixture_closure": Param(
            False,
            type="boolean",
            description="Explicitly authorize owner-delegated demo closure after fresh pass evidence.",
        ),
    }


def acceptance_stage_command(stage: str) -> str:
    """Build one fixed acceptance command; reject unsupported developer stages.

    Raises:
        ValueError: If the execution stage is outside the fixed allowlist.
    """
    if stage not in ACCEPTANCE_STAGES:
        raise ValueError("Unsupported resolution outbox acceptance stage.")
    return (
        "python scripts/accept_resolution_outbox.py "
        "--run-id '{{ params.acceptance_run_id }}' "
        "--dt '{{ params.business_date }}' "
        f"--stage {stage} "
        "{% if params.confirm_fixture_closure %}--confirm-demo-closure{% endif %}"
    )


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Prove owned fixture revalidation, gated closure, and outbox delivery recovery.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=25),
    default_args=default_dag_args(retries=0),
    params=resolution_outbox_acceptance_params(),
    tags=["dq-platform", "incident", "outbox", "acceptance", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    t00_start = start_task()
    t10_prepare_fixture = runner_plain_bash_task(
        task_id="t10_prepare_fixture", project_command=acceptance_stage_command("prepare"),
        execution_timeout=timedelta(minutes=2),
    )
    t20_detect_fixture_alert = runner_plain_bash_task(
        task_id="t20_detect_fixture_alert", project_command=acceptance_stage_command("capture"),
        execution_timeout=timedelta(minutes=2),
    )
    t30_restore_fixture_contract = runner_plain_bash_task(
        task_id="t30_restore_fixture_contract", project_command=acceptance_stage_command("restore"),
        execution_timeout=timedelta(minutes=2),
    )
    t30_restore_fixture_contract.trigger_rule = "all_done"
    t40_request_readonly_check = runner_plain_bash_task(
        task_id="t40_request_readonly_check", project_command=acceptance_stage_command("request"),
        execution_timeout=timedelta(minutes=2),
    )
    t50_wait_for_fresh_pass = runner_plain_bash_task(
        task_id="t50_wait_for_fresh_pass", project_command=acceptance_stage_command("wait"),
        execution_timeout=timedelta(minutes=15),
    )
    t60_confirm_with_delivery_fault = runner_plain_bash_task(
        task_id="t60_confirm_with_delivery_fault", project_command=acceptance_stage_command("close_fault"),
        execution_timeout=timedelta(minutes=2),
    )
    t70_replay_pending_outbox = runner_plain_bash_task(
        task_id="t70_replay_pending_outbox", project_command=acceptance_stage_command("replay"),
        execution_timeout=timedelta(minutes=2),
    )
    t80_verify_recovery = runner_plain_bash_task(
        task_id="t80_verify_recovery", project_command=acceptance_stage_command("verify"),
        execution_timeout=timedelta(minutes=2),
    )
    t90_finish = finish_task()
    (
        t00_start >> t10_prepare_fixture >> t20_detect_fixture_alert
        >> t30_restore_fixture_contract >> t40_request_readonly_check
        >> t50_wait_for_fresh_pass >> t60_confirm_with_delivery_fault
        >> t70_replay_pending_outbox >> t80_verify_recovery >> t90_finish
    )
    # Cleanup success must not hide a failed capture and allow incident closure.
    t20_detect_fixture_alert >> t40_request_readonly_check


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
