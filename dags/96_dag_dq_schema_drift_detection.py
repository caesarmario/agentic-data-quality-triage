####
## Airflow Schema Drift Detection DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Manual Airflow entrypoint for deterministic schema contract evaluation."""

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)
from dq_platform.schema_drift import SCHEMA_CONTRACT_NAMES, emit_schema_drift_summary


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "96_dag_dq_schema_drift_detection"

DOC_MD = """
# 96 - Schema Drift Detection

Manual administrative DAG that captures ClickHouse schemas from `system.columns`, compares
them with an allowlisted YAML contract, and persists deterministic evidence in
`dq.schema_snapshots` and `dq.schema_drift_results`.

Schedule: none. This DAG is a schema reliability control, not an autonomous agent. Missing
tables, missing columns, and type changes fail the DAG at critical severity. Lower-severity
position, default, and unexpected-column findings are persisted without bypassing policy.

Manual `dag_run.conf` example:

```json
{"contract_name": "orders"}
```
"""


# --- Defining Functions
def schema_drift_dag_params() -> dict[str, Param]:
    """
    Build allowlisted schema drift parameters.

    Returns:
        Dictionary containing the bounded contract_name parameter.
    """
    return {
        "contract_name": Param(
            "orders",
            type="string",
            enum=list(SCHEMA_CONTRACT_NAMES),
            description="Allowlisted schema contract captured and evaluated by this run.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Capture, compare, persist, and verify deterministic ClickHouse schema drift.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=schema_drift_dag_params(),
    tags=["dq-platform", "schema", "drift", "contract", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for deterministic schema drift detection.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_detect_schema_drift = runner_plain_bash_task(
        task_id="t10_detect_schema_drift",
        project_command=(
            "python -m pipelines.schema_drift.run_schema_drift "
            "--contract '{{ dag_run.conf.get(\"contract_name\", \"orders\") }}' "
            "--mode detect "
            "--run-id '{{ dag_run.run_id }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_verify_schema_drift_evidence = runner_plain_bash_task(
        task_id="t20_verify_schema_drift_evidence",
        project_command=(
            "python -m pipelines.schema_drift.run_schema_drift "
            "--contract '{{ dag_run.conf.get(\"contract_name\", \"orders\") }}' "
            "--mode verify "
            "--run-id '{{ dag_run.run_id }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_schema_drift_summary = PythonOperator(
        task_id="t30_emit_schema_drift_summary",
        python_callable=emit_schema_drift_summary,
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_detect_schema_drift
        >> t20_verify_schema_drift_evidence
        >> t30_emit_schema_drift_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
