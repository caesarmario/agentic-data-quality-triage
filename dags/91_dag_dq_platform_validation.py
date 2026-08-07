####
## Airflow Platform Validation DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)
from dq_platform.validation import VALIDATION_SUITE_NAMES, emit_validation_summary


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "91_dag_dq_platform_validation"

DOC_MD = """
# 91 - Platform Validation

Manual Airflow acceptance harness for code-focused test suites and read-only platform readiness checks.

Schedule: none. Trigger this DAG after a development slice so pytest output, task states,
duration, retries, and readiness logs remain available in Airflow.

This DAG does not mutate analytical data, execute remediation, or clean platform state.

Manual `dag_run.conf` example:

```json
{"validation_suite": "all", "require_api": true}
```

Allowed suites: `all`, `airflow`, `agent`, `api`, `checkpoint`, `discord`, `dq`, `life`, `llm`, `mcp`, `metadata`, `pipelines`, and `ui`.
"""


# --- Defining Functions
def validation_dag_params() -> dict[str, Param]:
    """
    Build parameters for manual Airflow validation runs.

    Returns:
        Dictionary containing the allowlisted validation suite parameter.
    """
    return {
        "validation_suite": Param(
            "all",
            type="string",
            enum=list(VALIDATION_SUITE_NAMES),
            description="Named pytest suite executed inside dq_runner before platform readiness checks.",
        ),
        "require_api": Param(
            False,
            type="boolean",
            description="Require the optional FastAPI control-plane profile during readiness validation.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Run named pytest suites and read-only platform readiness checks through Airflow.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=validation_dag_params(),
    tags=["dq-platform", "validation", "testing", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for development acceptance validation.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_run_named_pytest_suite = runner_plain_bash_task(
        task_id="t10_run_named_pytest_suite",
        project_command=(
            "python scripts/run_validation_suite.py "
            "--suite '{{ dag_run.conf.get(\"validation_suite\", \"all\") }}'"
        ),
        execution_timeout=timedelta(minutes=20),
    )

    t20_run_platform_readiness = runner_plain_bash_task(
        task_id="t20_run_platform_readiness",
        project_command=(
            "python scripts/smoke_readiness.py "
            "{% if dag_run.conf.get(\"require_api\", false) %}--require-api{% endif %}"
        ),
        execution_timeout=timedelta(minutes=10),
    )

    t30_emit_validation_summary = PythonOperator(
        task_id="t30_emit_validation_summary",
        python_callable=emit_validation_summary,
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_run_named_pytest_suite
        >> t20_run_platform_readiness
        >> t30_emit_validation_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)

