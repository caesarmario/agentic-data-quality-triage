####
## Airflow Quality And Alerts DAG for Agentic Data Quality Triage
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
DAG_ID = "30_dag_dq_orders_quality_alerts"

DOC_MD = """
# 30 - Orders Quality And Alerts

Run deterministic quality observability after dbt has produced staging and mart tables:

Schedule: none. This DAG is triggered by the platform daily orchestrator or manual runs.

1. Detect schema drift in observe mode and persist deterministic evidence.
2. Generate one deduplicated schema alert per affected table and notify Discord early.
3. Profile raw, staging, and mart tables.
4. Run deterministic DQ contract checks.
5. Generate stable-key alerts from latest failed/warning DQ results.
6. Push newly discovered DQ alerts through the optional idempotent Discord webhook.

The daily DAG uses schema `observe` mode so a critical contract finding is persisted and
alerted before a downstream profile/check can fail. The manual DAG
`96_dag_dq_schema_drift_detection` remains the strict enforcement gate.

The Discord task succeeds with an explicit `skipped` result when
`DISCORD_ALERT_WEBHOOK_URL` is not configured. When configured, delivery
failures remain visible as Airflow task failures and can use normal task retry.

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
    description="Detect schema drift, profile orders data, run deterministic DQ checks, and generate alerts.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=common_dag_params(),
    tags=["dq-platform", "orders", "dq", "alerts", "triggered"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for deterministic orders quality and alerting.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_detect_orders_schema_drift = runner_bash_task(
        task_id="t10_detect_orders_schema_drift",
        project_command=(
            "python -m pipelines.schema_drift.run_schema_drift "
            "--contract orders --mode detect --gate-mode observe "
            "--run-id '{{ dag_run.run_id }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_generate_schema_drift_alerts = runner_bash_task(
        task_id="t20_generate_schema_drift_alerts",
        project_command=(
            "python -m pipelines.schema_drift.generate_alerts "
            "--contract orders --run-id '{{ dag_run.run_id }}' $DATE_ARGS"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_push_schema_drift_alerts = runner_bash_task(
        task_id="t30_push_schema_drift_alerts",
        project_command="python -m apps.discord_bot.webhook $DATE_ARGS",
        execution_timeout=timedelta(minutes=5),
    )

    t40_profile_orders_tables = runner_bash_task(
        task_id="t40_profile_orders_tables",
        project_command="python -m pipelines.profiling.profile_orders $DATE_ARGS",
        execution_timeout=timedelta(minutes=15),
    )

    t50_run_orders_dq_checks = runner_bash_task(
        task_id="t50_run_orders_dq_checks",
        project_command="python -m pipelines.dq.run_checks $DATE_ARGS",
        execution_timeout=timedelta(minutes=15),
    )

    t60_generate_orders_alerts = runner_bash_task(
        task_id="t60_generate_orders_alerts",
        project_command="python -m pipelines.dq.generate_alerts $DATE_ARGS",
        execution_timeout=timedelta(minutes=10),
    )

    t70_push_discord_alerts = runner_bash_task(
        task_id="t70_push_discord_alerts",
        project_command="python -m apps.discord_bot.webhook $DATE_ARGS",
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_detect_orders_schema_drift
        >> t20_generate_schema_drift_alerts
        >> t30_push_schema_drift_alerts
        >> t40_profile_orders_tables
        >> t50_run_orders_dq_checks
        >> t60_generate_orders_alerts
        >> t70_push_discord_alerts
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
