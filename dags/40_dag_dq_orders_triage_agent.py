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
    runner_plain_bash_task,
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

Persistent LangGraph checkpoints are optional and default to `off`. When SQLite mode is enabled,
the DAG derives one stable thread per alert from the DagRun namespace. Airflow task retries resume
that thread instead of creating a second investigation.

Use `checkpoint_action=inspect` to print sanitized checkpoint history and the newest checkpoint
waiting for `checkpoint_history_next_node`. Inspection is read-only and does not expose graph state.
Use `checkpoint_action=triage` for normal execution or historical replay.

Schedule: none. This DAG is triggered by the platform daily orchestrator or manual runs.

Manual `dag_run.conf` examples:

```json
{"dt": "2026-05-04", "run_triage": true, "max_alerts": 3}
```

```json
{"alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table", "run_triage": true}
```

```json
{"alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table", "checkpoint_mode": "sqlite"}
```

```json
{
  "checkpoint_action": "inspect",
  "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
  "checkpoint_mode": "sqlite",
  "checkpoint_namespace": "triage-demo-20260504",
  "checkpoint_history_next_node": "store_report"
}
```

Historical replay branches from an exact checkpoint into a deterministic child thread. It
requires an explicit alert, source namespace, checkpoint id, and replay request id. The source
thread remains unchanged and all completed report/lifecycle/audit side effects remain guarded.

```json
{
  "alert_key": "orders|dq_failure|2026-05-04|dq.raw_orders|row_count_positive|table",
  "checkpoint_mode": "sqlite",
  "checkpoint_namespace": "triage-demo-20260504",
  "checkpoint_replay_id": "source-checkpoint-id",
  "checkpoint_replay_request_id": "replay-request-001"
}
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
                maxLength=512,
                pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,511})?$",
                description="Optional stable alert key. When provided, only this alert is triaged.",
            ),
            "alert_id": Param(
                "",
                type="string",
                pattern=(
                    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})?$"
                ),
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
            "checkpoint_mode": Param(
                "off",
                type="string",
                enum=["off", "sqlite"],
                description="Optional LangGraph persistence backend. Off preserves existing behavior.",
            ),
            "checkpoint_action": Param(
                "triage",
                type="string",
                enum=["triage", "inspect"],
                description="Run triage/replay or inspect sanitized checkpoint history only.",
            ),
            "checkpoint_namespace": Param(
                "",
                type="string",
                maxLength=160,
                pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,159})?$",
                description="Optional run namespace. Blank derives a stable value from DAG id and run id.",
            ),
            "checkpoint_resume": Param(
                False,
                type="boolean",
                description="Resume existing threads. SQLite task retries enable this automatically.",
            ),
            "checkpoint_replay_id": Param(
                "",
                type="string",
                maxLength=160,
                pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,159})?$",
                description="Exact historical checkpoint id. Requires one explicit alert and SQLite mode.",
            ),
            "checkpoint_replay_request_id": Param(
                "",
                type="string",
                maxLength=160,
                pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.:-]{0,159})?$",
                description="Stable idempotency request id used to derive a replay child thread.",
            ),
            "checkpoint_history_limit": Param(
                50,
                type="integer",
                minimum=1,
                maximum=100,
                description="Maximum newest checkpoint summaries printed by inspect action.",
            ),
            "checkpoint_history_next_node": Param(
                "store_report",
                type="string",
                minLength=1,
                maxLength=80,
                pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$",
                description="Exact pending graph node used to select a historical replay candidate.",
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

    t05_inspect_checkpoint_history = runner_plain_bash_task(
        task_id="t05_inspect_checkpoint_history",
        project_command=(
            "python scripts/inspect_agent_checkpoints.py "
            "--enabled '{{ dag_run.conf.get(\"checkpoint_action\", \"triage\") == \"inspect\" }}' "
            "--alert-key '{{ dag_run.conf.get(\"alert_key\", \"\") }}' "
            "--alert-id '{{ dag_run.conf.get(\"alert_id\", \"\") }}' "
            "--checkpoint-mode '{{ dag_run.conf.get(\"checkpoint_mode\", \"off\") }}' "
            "--checkpoint-namespace "
            "'{{ dag_run.conf.get(\"checkpoint_namespace\", \"\") or (dag.dag_id ~ \":\" ~ run_id) }}' "
            "--history-limit '{{ dag_run.conf.get(\"checkpoint_history_limit\", 50) }}' "
            "--select-next-node "
            "'{{ dag_run.conf.get(\"checkpoint_history_next_node\", \"store_report\") }}' "
            "--manifest-s3-uri s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t10_run_agentic_triage = runner_bash_task(
        task_id="t10_run_agentic_triage",
        project_command=(
            "python scripts/run_triage_alerts.py $DATE_ARGS "
            "--enabled "
            "'{{ (dag_run.conf.get(\"checkpoint_action\", \"triage\") == \"triage\") "
            "and dag_run.conf.get(\"run_triage\", true) }}' "
            "--alert-key '{{ dag_run.conf.get(\"alert_key\", \"\") }}' "
            "--alert-id '{{ dag_run.conf.get(\"alert_id\", \"\") }}' "
            "--status '{{ dag_run.conf.get(\"alert_status\", \"open\") }}' "
            "--limit '{{ dag_run.conf.get(\"max_alerts\", 5) }}' "
            "--checkpoint-mode '{{ dag_run.conf.get(\"checkpoint_mode\", \"off\") }}' "
            "--checkpoint-namespace "
            "'{{ dag_run.conf.get(\"checkpoint_namespace\", \"\") or (dag.dag_id ~ \":\" ~ run_id) }}' "
            "--checkpoint-replay-id '{{ dag_run.conf.get(\"checkpoint_replay_id\", \"\") }}' "
            "--checkpoint-replay-request-id "
            "'{{ dag_run.conf.get(\"checkpoint_replay_request_id\", \"\") }}' "
            "{% if dag_run.conf.get(\"checkpoint_mode\", \"off\") == \"sqlite\" "
            "and not dag_run.conf.get(\"checkpoint_replay_id\", \"\") "
            "and (dag_run.conf.get(\"checkpoint_resume\", false) or task_instance.try_number > 1) %}"
            "--checkpoint-resume "
            "{% endif %}"
            "--manifest-s3-uri s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"
        ),
        execution_timeout=timedelta(minutes=30),
    )

    t90_finish = finish_task()

    t00_start >> t05_inspect_checkpoint_history >> t10_run_agentic_triage >> t90_finish


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
