####
## Airflow Agent Checkpoint Smoke DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.checkpoint_smoke import emit_checkpoint_smoke_summary
from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "93_dag_dq_agent_checkpoint_smoke"

DOC_MD = """
# 93 - Agent Checkpoint Smoke

Manual administrative DAG proving that the optional LangGraph SQLite saver persists
state across separate Airflow tasks and runner processes.

The flow pauses before a synthetic effect, resumes it once, requests resume again after
completion, and verifies that both graph state and the external marker remain exactly one.

This DAG does not query production data, mutate ClickHouse, call an LLM, or write S3 artifacts.
Each run requires a unique path-safe `thread_id`.

```json
{"thread_id": "checkpoint-smoke-20260716T090000000000"}
```
"""


# --- Defining Functions
def checkpoint_smoke_params() -> dict[str, Param]:
    """
    Build parameters for one cross-process checkpoint smoke.

    Returns:
        Dictionary containing the required path-safe thread id.
    """
    return {
        "thread_id": Param(
            "checkpoint-smoke-manual",
            type="string",
            minLength=1,
            maxLength=160,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$",
            description="Unique checkpoint thread shared by all smoke tasks.",
        )
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Validate persistent LangGraph checkpoint initialize/resume behavior across Airflow tasks.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=checkpoint_smoke_params(),
    tags=["dq-platform", "agent", "checkpoint", "smoke", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for cross-process checkpoint validation.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_initialize_checkpoint = runner_plain_bash_task(
        task_id="t10_initialize_checkpoint",
        project_command=(
            "python scripts/smoke_agent_checkpoint.py "
            "--phase initialize "
            "--thread-id '{{ dag_run.conf.get(\"thread_id\", \"checkpoint-smoke-manual\") }}'"
        ),
        execution_timeout=timedelta(minutes=3),
    )

    t20_resume_checkpoint = runner_plain_bash_task(
        task_id="t20_resume_checkpoint",
        project_command=(
            "python scripts/smoke_agent_checkpoint.py "
            "--phase resume "
            "--thread-id '{{ dag_run.conf.get(\"thread_id\", \"checkpoint-smoke-manual\") }}'"
        ),
        execution_timeout=timedelta(minutes=3),
    )

    t30_resume_completed_checkpoint = runner_plain_bash_task(
        task_id="t30_resume_completed_checkpoint",
        project_command=(
            "python scripts/smoke_agent_checkpoint.py "
            "--phase resume-complete "
            "--thread-id '{{ dag_run.conf.get(\"thread_id\", \"checkpoint-smoke-manual\") }}'"
        ),
        execution_timeout=timedelta(minutes=3),
    )

    t40_verify_checkpoint = runner_plain_bash_task(
        task_id="t40_verify_checkpoint",
        project_command=(
            "python scripts/smoke_agent_checkpoint.py "
            "--phase verify "
            "--thread-id '{{ dag_run.conf.get(\"thread_id\", \"checkpoint-smoke-manual\") }}'"
        ),
        execution_timeout=timedelta(minutes=3),
    )

    t50_emit_checkpoint_summary = PythonOperator(
        task_id="t50_emit_checkpoint_summary",
        python_callable=emit_checkpoint_smoke_summary,
        execution_timeout=timedelta(minutes=2),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_initialize_checkpoint
        >> t20_resume_checkpoint
        >> t30_resume_completed_checkpoint
        >> t40_verify_checkpoint
        >> t50_emit_checkpoint_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
