"""Execute one immutable approved recovery. No user-selected command or scope."""
from datetime import timedelta

from airflow.sdk import DAG, Param
from dq_platform.helpers import DEFAULT_START_DATE, default_dag_args, start_task, finish_task, runner_plain_bash_task

DAG_ID = "90_05_dag_dq_incident_recovery"
with DAG(
    dag_id=DAG_ID, start_date=DEFAULT_START_DATE, schedule=None, catchup=False,
    is_paused_upon_creation=False, max_active_runs=1, dagrun_timeout=timedelta(minutes=30),
    default_args=default_dag_args(retries=0),
    params={"execution_id": Param(type="string", pattern="^[0-9a-fA-F-]{36}$", minLength=36, maxLength=36)},
    tags=["dq-platform", "recovery", "administrative", "approval-required"],
    doc_md="""Manual recovery from pinned input, never baseline generation. The durable execution
    owns its immutable plan and human approval. A successful run is separate from business
    verification PASS and does not close an incident. Timezone: Asia/Bangkok.""",
) as dag:
    t00_start = start_task()
    t20_execute_approved_plan = runner_plain_bash_task(
        task_id="t20_execute_approved_plan",
        project_command="python -m agent.recovery.runtime --execution-id '{{ params.execution_id }}'",
        execution_timeout=timedelta(minutes=25),
    )
    t90_finish = finish_task()
    t00_start >> t20_execute_approved_plan >> t90_finish
