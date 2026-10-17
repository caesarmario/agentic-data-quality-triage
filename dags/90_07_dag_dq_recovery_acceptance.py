"""Explicit isolated synthetic fault fixtures; never callable by a model or recovery plan."""
from datetime import timedelta
from airflow.sdk import DAG, Param
from dq_platform.helpers import DEFAULT_START_DATE, default_dag_args, start_task, finish_task, runner_plain_bash_task

DAG_ID = "90_07_dag_dq_recovery_acceptance"
with DAG(dag_id=DAG_ID, start_date=DEFAULT_START_DATE, schedule=None, catchup=False,
         is_paused_upon_creation=False, max_active_runs=1, default_args=default_dag_args(retries=0),
         params={"case": Param("source_invalid", type="string", enum=["raw", "derived", "both", "source_invalid"])},
         tags=["dq-platform", "acceptance", "synthetic", "administrative"],
         doc_md="""Prepare only fixed owned acceptance dates after empty-scope preflight. This is
         explicit simulation, not recovery. Source-invalid is rejected without warehouse writes.
         No approval, recovery execution, LLM call, or incident closure is performed here.""") as dag:
    t00_start = start_task()
    t20_prepare_owned_fixture = runner_plain_bash_task(task_id="t20_prepare_owned_fixture",
        project_command="python -m scripts.recovery_acceptance --case '{{ params.case }}'",
        execution_timeout=timedelta(minutes=10))
    t90_finish = finish_task()
    t00_start >> t20_prepare_owned_fixture >> t90_finish
