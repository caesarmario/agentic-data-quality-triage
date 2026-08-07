####
## Airflow Full Daily Orchestrator DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging

from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import DAG, Param

from dq_platform.helpers import (
    DAILY_PLATFORM_SCHEDULE,
    DEFAULT_TRIGGER_RESET_DAG_RUN,
    DEFAULT_START_DATE,
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
DAG_ID             = "00_dag_dq_platform_daily_orchestrator"
LANDING_DAG_ID     = "10_dag_dq_orders_landing_orchestrator"
DBT_DAG_ID         = "20_dag_dq_orders_dbt_transform"
QUALITY_DAG_ID     = "30_dag_dq_orders_quality_alerts"
TRIAGE_DAG_ID      = "40_dag_dq_orders_triage_agent"

DOC_MD = """
# 00 - Full Daily DQ Platform Orchestrator

Run the GitHub/LinkedIn demo path end-to-end:

Schedule: every day at 00:05 Asia/Bangkok.

1. Generate orders data and land Parquet in SeaweedFS S3.
2. Load the S3 landing partition into ClickHouse raw.
3. Run dbt staging/mart transformations and tests.
4. Upload dbt lineage artifacts to S3.
5. Run profiling, deterministic DQ checks, and alert generation.
6. Optionally run LangGraph agentic triage for open alerts.

Scheduled runs use `incident_scenario=auto`, which resolves a deterministic random
daily scenario from `configs/incidents/daily_policy.yml`. Manual runs default to
`baseline` unless explicitly overridden.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04", "incident_scenario": "missing_latest_day", "run_triage": true, "max_alerts": 3}
```

```json
{"start_date": "2026-05-01", "end_date": "2026-05-04", "run_mode": "backfill", "run_triage": false}
```
"""


# --- Defining Functions
def daily_orchestrator_params() -> dict[str, Param]:
    """
    Build manual-run parameters for the full daily orchestrator.

    Returns:
        Dictionary of Airflow Param objects exposed in the UI.
    """
    params = common_dag_params()
    params.update(
        {
            "max_alerts": Param(
                5,
                type="integer",
                minimum=1,
                maximum=20,
                description="Maximum alerts to triage when run_triage is enabled.",
            ),
        }
    )

    return params


def daily_child_conf() -> dict[str, str]:
    """
    Build conf payload forwarded to all child DAGs.

    Returns:
        Conf dictionary including date, scenario, run mode, triage flag, and max alert count.
    """
    conf = single_date_conf()
    conf.update(
        {
            "max_alerts": "{{ dag_run.conf.get(\"max_alerts\", 5) }}",
        }
    )

    return conf


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Run the full local orders DQ platform path from landing through optional agent triage.",
    start_date=DEFAULT_START_DATE,
    schedule=DAILY_PLATFORM_SCHEDULE,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=daily_orchestrator_params(),
    tags=["dq-platform", "orders", "daily", "orchestrator", "demo"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for the full daily demo orchestration path.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_trigger_landing = TriggerDagRunOperator(
        task_id="t10_trigger_landing",
        trigger_dag_id=LANDING_DAG_ID,
        trigger_run_id="daily_landing__{{ ts_nodash }}",
        conf=daily_child_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t20_trigger_dbt_transform = TriggerDagRunOperator(
        task_id="t20_trigger_dbt_transform",
        trigger_dag_id=DBT_DAG_ID,
        trigger_run_id="daily_dbt__{{ ts_nodash }}",
        conf=daily_child_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t30_trigger_quality_alerts = TriggerDagRunOperator(
        task_id="t30_trigger_quality_alerts",
        trigger_dag_id=QUALITY_DAG_ID,
        trigger_run_id="daily_quality__{{ ts_nodash }}",
        conf=daily_child_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t40_trigger_agentic_triage = TriggerDagRunOperator(
        task_id="t40_trigger_agentic_triage",
        trigger_dag_id=TRIAGE_DAG_ID,
        trigger_run_id="daily_triage__{{ ts_nodash }}",
        conf=daily_child_conf(),
        logical_date="{{ dag_run.logical_date.isoformat() }}",
        wait_for_completion=True,
        deferrable=True,
        poke_interval=15,
        allowed_states=["success"],
        failed_states=["failed"],
        reset_dag_run=DEFAULT_TRIGGER_RESET_DAG_RUN,
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_trigger_landing
        >> t20_trigger_dbt_transform
        >> t30_trigger_quality_alerts
        >> t40_trigger_agentic_triage
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
