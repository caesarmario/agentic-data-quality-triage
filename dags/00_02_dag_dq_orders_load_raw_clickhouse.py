####
## Airflow S3-to-ClickHouse DAG for Agentic Data Quality Triage
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
DAG_ID = "00_02_dag_dq_orders_load_raw_clickhouse"

DOC_MD = """
# 00.02 - Orders S3 To ClickHouse Raw

Load orders Parquet partition(s) from SeaweedFS S3 into `dq.raw_orders`.
The loader is idempotent by date: it drops/replaces the target `dt` partition before insert.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04"}
```

```json
{"start_date": "2026-05-01", "end_date": "2026-05-04", "run_mode": "backfill"}
```
"""



# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Load orders landing partitions from SeaweedFS S3 into ClickHouse raw_orders.",
    start_date=DEFAULT_START_DATE,
    schedule="@daily",
    catchup=False,
    max_active_runs=1,
    default_args=default_dag_args(),
    params=common_dag_params(),
    tags=["dq-platform", "orders", "clickhouse", "raw", "daily"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for raw orders loading.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_load_raw_orders = runner_bash_task(
        task_id="t10_load_raw_orders",
        project_command="python -m pipelines.loading.load_clickhouse $DATE_ARGS",
        execution_timeout=timedelta(minutes=15),
    )

    t90_finish = finish_task()

    t00_start >> t10_load_raw_orders >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
