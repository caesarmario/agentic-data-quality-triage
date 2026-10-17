"""Reconcile existing runs only. This scheduled administrative DAG cannot repair data."""
from datetime import timedelta

from airflow.sdk import DAG
from dq_platform.helpers import DEFAULT_START_DATE, acceptance_manual_only, default_dag_args, start_task, finish_task, runner_plain_bash_task

DAG_ID = "90_06_dag_dq_recovery_reconciliation"
with DAG(
    dag_id=DAG_ID, start_date=DEFAULT_START_DATE,
    schedule=None if acceptance_manual_only() else timedelta(minutes=1), catchup=False,
    is_paused_upon_creation=True, max_active_runs=1, dagrun_timeout=timedelta(minutes=2),
    default_args=default_dag_args(retries=0),
    tags=["dq-platform", "reconciliation", "administrative", "read-only-warehouse"],
    doc_md="""Read Airflow execution states through Public API v2/JWT, reconcile control records,
    and replay pending observability projections. Never trigger, clear, retry, or repair a
    warehouse job and never call a model. Enable only after control-store migration/cutover.
    Timezone: Asia/Bangkok.""",
) as dag:
    t00_start = start_task()
    t20_reconcile_existing_runs = runner_plain_bash_task(
        task_id="t20_reconcile_existing_runs", project_command="python -m agent.recovery.reconciliation",
        execution_timeout=timedelta(seconds=90),
    )
    t90_finish = finish_task()
    t00_start >> t20_reconcile_existing_runs >> t90_finish
