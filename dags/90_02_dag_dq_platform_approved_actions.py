####
## Airflow Approved Action Dispatcher DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.approved_actions import run_approved_action_dispatcher
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
DAG_ID = "90_02_dag_dq_platform_approved_actions"

DOC_MD = """
# 90.02 - Approved Action Dispatcher

Manual dispatcher for transactional, exact-scope approval actions.

Schedule: none. Default behavior is `dry_run=true`, which reads the request and
validates provider readiness without claiming or executing it. Set
`dry_run=false` only after the request has been approved.

Supported action contracts are `rerun_dbt`, `create_ticket`, and
`post_notification`. The caller supplies only an approval request ID; action
scope, destination, and permissions are loaded from durable policy state.

Outbound ticket and notification providers are disabled by default. Network
ambiguity is recorded as `unknown` and is never retried automatically.

```json
{
  "approval_request_id": "APR-ABCDEF0123456789ABCD",
  "dry_run": true
}
```
"""


# --- Defining DAG Parameters
def approved_action_params() -> dict[str, Param]:
    """Build the manual exact-scope action parameters."""
    return {
        "approval_request_id": Param(
            "",
            type="string",
            pattern="^$|^APR-[A-F0-9]{20}$",
            description="Transactional approval request identifier.",
        ),
        "dry_run": Param(
            True,
            type="boolean",
            description="Preview request and provider readiness without claiming or executing it.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Preview or execute one transactional approval-gated action.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    user_defined_macros=default_user_defined_macros(),
    params=approved_action_params(),
    tags=["dq-platform", "approval-gated", "actions", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """Airflow DAG definition for approved bounded action execution."""
    t00_start = start_task()

    t10_preview_or_execute = PythonOperator(
        task_id="t10_preview_or_execute",
        python_callable=run_approved_action_dispatcher,
        execution_timeout=timedelta(minutes=10),
    )

    t90_finish = finish_task()

    t00_start >> t10_preview_or_execute >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
