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
from dq_platform.llm_smoke import emit_llm_smoke_summary


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "92_dag_dq_llm_provider_smoke"

# Only this low-risk route may make one explicitly approved external request.
EXTERNAL_SMOKE_ROUTE_NAMES = ("cheap_summary",)
DEFAULT_EXTERNAL_SMOKE_ROUTE = EXTERNAL_SMOKE_ROUTE_NAMES[0]

DOC_MD = """
# 92 - LLM Provider Smoke

Manual administrative DAG for validating the provider-agnostic LLM router without
running a full incident triage workflow.

The DAG always runs a deterministic heuristic baseline first. The selected
route also runs in heuristic mode by default, so triggering this DAG does not
spend provider credit unless the operator explicitly sets
`run_external_provider` to `true`.

Default zero-cost example:

```json
{"route_name": "cheap_summary", "run_external_provider": false}
```

Strict external-provider example:

```json
{"route_name": "cheap_summary", "run_external_provider": true}
```

External mode permits one configured provider call only. Its fallback chain is
bounded to the local heuristic route, and the runner rejects a projected total
above 4,000 tokens or USD 0.01. External mode fails unless the configured
provider produces the final response.

All prompts and context are synthetic. API keys, raw reasoning, and raw provider
errors are excluded from Airflow output and ClickHouse audit payloads.
"""


# --- Defining Functions
def llm_smoke_dag_params() -> dict[str, Param]:
    """
    Build allowlisted parameters for manual provider smoke runs.

    Returns:
        Dictionary containing route and explicit external-provider parameters.
    """
    return {
        "route_name": Param(
            DEFAULT_EXTERNAL_SMOKE_ROUTE,
            type="string",
            enum=list(EXTERNAL_SMOKE_ROUTE_NAMES),
            description="Allowlisted low-risk route used when external smoke is explicitly enabled.",
        ),
        "run_external_provider": Param(
            False,
            type="boolean",
            description="Explicitly run one strict external provider call; false keeps this task zero-cost.",
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
            "{% if dag_run.conf.get(\"run_external_provider\", false) %}"
            "--strict-external-provider"
            "{% else %}--force-heuristic{% endif %}"
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
