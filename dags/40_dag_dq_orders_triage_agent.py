####
## Airflow Agent Triage DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.sdk import DAG, Param

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
DAG_ID = "40_dag_dq_orders_triage_agent"

DOC_MD = """
# 40 - Orders Agentic Triage

Run the LangGraph triage agent for one alert or a bounded list of open alerts.

This DAG does not mutate data or trigger remediation. It collects evidence through guarded tools,
stores Markdown/JSON reports in `dq-artifacts`, and writes audit events to ClickHouse.

Schedule: none. This DAG is triggered by the platform daily orchestrator or manual runs.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04", "run_triage": true, "max_alerts": 3}
```

```json
{"alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table", "run_triage": true}
```
"""


# --- Defining Functions
def triage_dag_params() -> dict[str, Param]:
    """
    Build DAG parameters for agentic triage runs.

    Returns:
        Dictionary of Airflow Param objects exposed in the UI.
    """
    params = common_dag_params()
    params.update(
        {
            "run_triage": Param(
                True,
                type="boolean",
                description="When false, triage DAG exits successfully without running the agent.",
            ),
            "alert_key": Param(
                "",
                type="string",
                description="Optional stable alert key. When provided, only this alert is triaged.",
            ),
            "alert_id": Param(
                "",
                type="string",
                description="Optional ClickHouse alert UUID. Takes precedence only when alert_key is blank.",
            ),
            "alert_status": Param(
                "open",
                type="string",
                description="Alert status to scan when alert_key/alert_id are blank.",
            ),
            "max_alerts": Param(
                5,
                type="integer",
                minimum=1,
                maximum=20,
                description="Maximum alerts to triage for a date/date range.",
            ),
        }
    )

    return params


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Run LangGraph triage for selected or newly generated DQ alerts.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(),
    user_defined_macros=default_user_defined_macros(),
    params=triage_dag_params(),
    tags=["dq-platform", "orders", "agent", "triage", "manual"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for agentic DQ alert triage.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_run_agentic_triage = runner_bash_task(
        task_id="t10_run_agentic_triage",
        project_command=(
            "python scripts/run_triage_alerts.py $DATE_ARGS "
            "--enabled '{{ dag_run.conf.get(\"run_triage\", true) }}' "
            "--alert-key '{{ dag_run.conf.get(\"alert_key\", \"\") }}' "
            "--alert-id '{{ dag_run.conf.get(\"alert_id\", \"\") }}' "
            "--status '{{ dag_run.conf.get(\"alert_status\", \"open\") }}' "
            "--limit '{{ dag_run.conf.get(\"max_alerts\", 5) }}' "
            "--manifest-s3-uri s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"
        ),
        execution_timeout=timedelta(minutes=30),
    )

    t90_finish = finish_task()

    t00_start >> t10_run_agentic_triage >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
