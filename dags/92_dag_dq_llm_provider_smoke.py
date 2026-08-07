####
## Airflow LLM Provider Smoke DAG for Agentic Data Quality Triage
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
from dq_platform.llm_smoke import LLM_SMOKE_ROUTE_NAMES, emit_llm_smoke_summary


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "92_dag_dq_llm_provider_smoke"

DOC_MD = """
# 92 - LLM Provider Smoke

Manual administrative DAG for validating the provider-agnostic LLM router without
running a full incident triage workflow.

The DAG always runs a deterministic heuristic baseline first. It then runs one
allowlisted model route using the configured fallback chain.

Default fallback-safe example:

```json
{"route_name": "cheap_summary", "require_provider": false}
```

Strict provider example:

```json
{"route_name": "cheap_summary", "require_provider": true}
```

Fallback-safe mode succeeds when Gemini, OpenAI, or xAI is unavailable but the
router returns a usable heuristic response. Strict mode fails unless the provider
configured for the selected route produces the final response.

All prompts and context are synthetic. API keys, raw reasoning, and raw provider
errors are excluded from Airflow output and ClickHouse audit payloads.
"""


# --- Defining Functions
def llm_smoke_dag_params() -> dict[str, Param]:
    """
    Build allowlisted parameters for manual provider smoke runs.

    Returns:
        Dictionary containing route and strict-provider parameters.
    """
    return {
        "route_name": Param(
            "cheap_summary",
            type="string",
            enum=list(LLM_SMOKE_ROUTE_NAMES),
            description="Model routing entry tested after the heuristic baseline.",
        ),
        "require_provider": Param(
            False,
            type="boolean",
            description="Fail if the selected route falls back instead of using its configured provider.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Validate heuristic and selected external LLM routes with auditable Airflow logs.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=llm_smoke_dag_params(),
    tags=["dq-platform", "llm", "provider", "smoke", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for bounded LLM provider connectivity checks.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_smoke_heuristic_baseline = runner_plain_bash_task(
        task_id="t10_smoke_heuristic_baseline",
        project_command=(
            "python scripts/smoke_llm_provider.py "
            "--route evidence_summary "
            "--force-heuristic"
        ),
        execution_timeout=timedelta(minutes=2),
    )

    t20_smoke_selected_route = runner_plain_bash_task(
        task_id="t20_smoke_selected_route",
        project_command=(
            "python scripts/smoke_llm_provider.py "
            "--route '{{ dag_run.conf.get(\"route_name\", \"cheap_summary\") }}' "
            "{% if dag_run.conf.get(\"require_provider\", false) %}--require-provider{% endif %}"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_llm_smoke_summary = PythonOperator(
        task_id="t30_emit_llm_smoke_summary",
        python_callable=emit_llm_smoke_summary,
        execution_timeout=timedelta(minutes=2),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_smoke_heuristic_baseline
        >> t20_smoke_selected_route
        >> t30_emit_llm_smoke_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
