####
## Airflow dbt Transform DAG for Agentic Data Quality Triage
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
    default_user_defined_macros,
    finish_task,
    runner_bash_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "20_dag_dq_orders_dbt_transform"

DOC_MD = """
# 20 - Orders dbt Transform

Run the dbt transformation layer for the orders dataset:

Schedule: none. This DAG is triggered by the platform daily orchestrator or manual runs.

1. Validate dbt connectivity with `dbt debug`.
2. Build staging and mart models for the requested date window.
3. Run dbt tests as observability signals, allowing data-test failures to continue.
4. Upload dbt artifacts such as `manifest.json` and `run_results.json` to `dq-artifacts`.

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
    description="Run dbt orders transforms/tests and upload dbt artifacts to SeaweedFS S3.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=common_dag_params(),
    tags=["dq-platform", "orders", "dbt", "transform", "triggered"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for the orders dbt transformation layer.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_dbt_debug = runner_bash_task(
        task_id="t10_dbt_debug",
        project_command="python -m pipelines.dbt.run_dbt $DATE_ARGS --step debug",
        execution_timeout=timedelta(minutes=5),
    )

    t20_dbt_run_orders_models = runner_bash_task(
        task_id="t20_dbt_run_orders_models",
        project_command="python -m pipelines.dbt.run_dbt $DATE_ARGS --step run",
        execution_timeout=timedelta(minutes=20),
    )

    t30_dbt_test_orders_models = runner_bash_task(
        task_id="t30_dbt_test_orders_models",
        project_command="python -m pipelines.dbt.run_dbt $DATE_ARGS --step test --allow-failure",
        execution_timeout=timedelta(minutes=20),
    )

    t40_upload_dbt_artifacts = runner_bash_task(
        task_id="t40_upload_dbt_artifacts",
        project_command="python -m pipelines.dbt.run_dbt $DATE_ARGS --step upload-artifacts",
        execution_timeout=timedelta(minutes=10),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_dbt_debug
        >> t20_dbt_run_orders_models
        >> t30_dbt_test_orders_models
        >> t40_upload_dbt_artifacts
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
