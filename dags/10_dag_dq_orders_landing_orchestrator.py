####
## Airflow Landing Orchestrator DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging

from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import DAG

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    DEFAULT_TRIGGER_RESET_DAG_RUN,
    common_dag_params,
    default_dag_args,
    default_user_defined_macros,
    finish_task,
    single_date_conf,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID       = "10_dag_dq_orders_landing_orchestrator"
SEED_DAG_ID  = "11_dag_dq_orders_seed_to_s3"
LOAD_DAG_ID  = "12_dag_dq_orders_load_raw_clickhouse"

DOC_MD = """
# 10 - Orders Landing Orchestrator

Trigger the daily orders landing path in sequence:

Schedule: none. This DAG is triggered by the platform daily orchestrator or manual runs.

1. Generate synthetic orders and upload Parquet to SeaweedFS S3.
2. Load the S3 landing partition(s) into ClickHouse `dq.raw_orders`.

This DAG keeps Airflow as the scheduler/orchestrator. Data movement remains in the
modular Python pipeline code executed by `dq_runner`.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04", "incident_scenario": "baseline"}
```

```json
{"start_date": "2026-05-01", "end_date": "2026-05-04", "incident_scenario": "missing_latest_day", "run_mode": "backfill"}
```
"""



# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Orchestrate orders seed-to-S3 and S3-to-ClickHouse raw loading.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=common_dag_params(),
    tags=["dq-platform", "orders", "landing", "orchestrator", "triggered"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for the orders landing orchestration layer.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_trigger_seed_to_s3 = TriggerDagRunOperator(
        task_id="t10_trigger_seed_to_s3",
        trigger_dag_id=SEED_DAG_ID,
        trigger_run_id="landing_seed__{{ ts_nodash }}",
        conf=single_date_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t20_trigger_load_raw_clickhouse = TriggerDagRunOperator(
        task_id="t20_trigger_load_raw_clickhouse",
        trigger_dag_id=LOAD_DAG_ID,
        trigger_run_id="landing_load__{{ ts_nodash }}",
        conf=single_date_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t90_finish = finish_task()

    t00_start >> t10_trigger_seed_to_s3 >> t20_trigger_load_raw_clickhouse >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
