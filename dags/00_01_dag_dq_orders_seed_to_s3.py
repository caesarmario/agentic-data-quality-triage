####
## Airflow Seed-to-S3 DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.sdk import DAG

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    common_dag_params,
    default_dag_args,
    finish_task,
    runner_bash_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "00_01_dag_dq_orders_seed_to_s3"

DOC_MD = """
# 00.01 - Orders Seed To S3

Generate synthetic orders data for one business date or a manual date range, then upload
the resulting Parquet partition(s) to the local SeaweedFS S3 landing bucket.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04", "incident_scenario": "baseline"}
```

```json
{"start_date": "2026-05-01", "end_date": "2026-05-04", "incident_scenario": "missing_latest_day"}
```
"""



# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Generate daily orders data and upload landing Parquet to SeaweedFS S3.",
    start_date=DEFAULT_START_DATE,
    schedule="@daily",
    catchup=False,
    max_active_runs=1,
    default_args=default_dag_args(),
    params=common_dag_params(),
    tags=["dq-platform", "orders", "landing", "s3", "daily"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for synthetic orders landing.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_generate_and_upload_orders = runner_bash_task(
        task_id="t10_generate_and_upload_orders",
        project_command="python -m pipelines.seeding.run_daily $DATE_ARGS --incident-scenario $INCIDENT_SCENARIO",
        execution_timeout=timedelta(minutes=15),
    )

    t90_finish = finish_task()

    t00_start >> t10_generate_and_upload_orders >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
