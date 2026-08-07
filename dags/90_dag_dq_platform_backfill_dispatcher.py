####
## Airflow Backfill Dispatcher DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.backfill_dispatcher import DEFAULT_MAX_DATES, DEFAULT_TARGET_DAG_ID, run_backfill_dispatcher
from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    default_user_defined_macros,
    finish_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "90_dag_dq_platform_backfill_dispatcher"

DOC_MD = """
# 90 - Backfill Dispatcher

Approval-gated/manual dispatcher for Airflow backfills.

Schedule: none. This DAG is always manual/approval-gated.

This DAG accepts an inclusive date range and triggers the selected target DAG once per date.
That design keeps each business date isolated in its own child DAG run, making Airflow logs,
pipeline observability, and agent recommendations easier to audit.

Default behavior is `dry_run=true`, so the first run previews what would be triggered.
Set `dry_run=false` only with an approved, exact-scope `approval_request_id`.

Manual `dag_run.conf` example:

```json
{
  "start_date": "2026-05-06",
  "end_date": "2026-05-09",
  "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
  "requested_by": "mario",
  "reason": "agent recommended backfill for missing latest-day orders data",
  "approval_request_id": "",
  "dry_run": true,
  "reset_dag_run": false,
  "wait_for_completion": false,
  "fail_fast": true,
  "max_dates": 7,
  "incident_scenario": "baseline",
  "run_mode": "backfill",
  "run_triage": false
}
```
"""


# --- Defining Functions
def backfill_dispatcher_params() -> dict[str, Param]:
    """
    Build manual-run parameters for approval-gated backfill dispatch.

    Returns:
        Dictionary of Airflow Param objects exposed in the UI.
    """
    return {
        "start_date": Param(
            "",
            type="string",
            description="Inclusive backfill start date in YYYY-MM-DD format.",
        ),
        "end_date": Param(
            "",
            type="string",
            description="Inclusive backfill end date in YYYY-MM-DD format.",
        ),
        "target_dag_id": Param(
            DEFAULT_TARGET_DAG_ID,
            type="string",
            description="Target DAG to trigger once per business date.",
        ),
        "requested_by": Param(
            "manual",
            type="string",
            description="Human, UI, or agent identity requesting this backfill.",
        ),
        "reason": Param(
            "manual_backfill",
            type="string",
            description="Business reason for audit and approval review.",
        ),
        "approval_request_id": Param(
            "",
            type="string",
            description=(
                "Durable approval reference required when dry_run=false. "
                "The approved DAG, date range, and execution flags must match exactly."
            ),
        ),
        "dry_run": Param(
            True,
            type="boolean",
            description="Preview triggers without creating child DAG runs. Set false only after approval.",
        ),
        "reset_dag_run": Param(
            False,
            type="boolean",
            description="Accepted for approval payload compatibility. Child run ids are unique by default.",
        ),
        "wait_for_completion": Param(
            False,
            type="boolean",
            description="When true, poll each child DAG run until success or failure.",
        ),
        "fail_fast": Param(
            True,
            type="boolean",
            description="When waiting, fail dispatcher immediately if a child run fails.",
        ),
        "max_dates": Param(
            DEFAULT_MAX_DATES,
            type="integer",
            minimum=1,
            maximum=90,
            description="Safety cap for the number of dates to dispatch.",
        ),
        "poll_interval_sec": Param(
            15,
            type="integer",
            minimum=5,
            maximum=300,
            description="Polling interval when wait_for_completion=true.",
        ),
        "timeout_sec": Param(
            3600,
            type="integer",
            minimum=60,
            maximum=86400,
            description="Per-child DAG wait timeout when wait_for_completion=true.",
        ),
        "incident_scenario": Param(
            "baseline",
            type="string",
            description="Synthetic incident scenario passed to target DAGs.",
        ),
        "run_mode": Param(
            "backfill",
            type="string",
            description="Logical run mode passed to target DAGs.",
        ),
        "run_seed": Param(
            True,
            type="boolean",
            description="Pass-through flag for target DAGs that support seed toggles.",
        ),
        "run_load": Param(
            True,
            type="boolean",
            description="Pass-through flag for target DAGs that support load toggles.",
        ),
        "run_dbt": Param(
            True,
            type="boolean",
            description="Pass-through flag for target DAGs that support dbt toggles.",
        ),
        "run_dq": Param(
            True,
            type="boolean",
            description="Pass-through flag for target DAGs that support DQ toggles.",
        ),
        "run_triage": Param(
            False,
            type="boolean",
            description="Whether child DAGs should run agent triage after DQ alerts.",
        ),
        "max_alerts": Param(
            5,
            type="integer",
            minimum=1,
            maximum=20,
            description="Maximum alerts to triage when run_triage=true.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Approval-gated dispatcher that triggers one target DAG run per backfill date.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=backfill_dispatcher_params(),
    tags=["dq-platform", "backfill", "dispatcher", "manual", "approval-gated"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for approval-gated backfill dispatch.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_validate_and_dispatch = PythonOperator(
        task_id="t10_validate_and_dispatch",
        python_callable=run_backfill_dispatcher,
        execution_timeout=timedelta(hours=2),
    )

    t90_finish = finish_task()

    t00_start >> t10_validate_and_dispatch >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
