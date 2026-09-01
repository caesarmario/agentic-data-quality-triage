####
## Airflow Metadata And Lineage Agent Smoke DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Manual Airflow smoke entrypoint for the bounded Metadata and Lineage Agent."""

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
from dq_platform.metadata_lineage_agent import (
    METADATA_LINEAGE_TASK_TYPES,
    emit_metadata_lineage_summary,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "97_dag_dq_metadata_lineage_agent_smoke"

DOC_MD = """
# 97 - Metadata And Lineage Agent Smoke

Manual administrative DAG that runs one bounded specialist handoff through the trusted
metadata registry and dbt lineage tools, then verifies retained ClickHouse audit evidence.

The specialist is read-only, uses `no_llm_fallback`, has an exact tool allowlist, and cannot
execute SQL or remediation directly. This DAG validates the specialist boundary before a
full control-plane supervisor is enabled.

Manual `dag_run.conf` example:

```json
{
  "task_type": "asset_context",
  "qualified_name": "dq.raw_orders",
  "max_depth": 5,
  "max_nodes": 100
}
```
"""


# --- Defining Functions
def metadata_lineage_dag_params() -> dict[str, Param]:
    """
    Build bounded specialist smoke parameters.

    Returns:
        Dictionary containing allowlisted task, asset, search, and traversal bounds.
    """
    return {
        "task_type": Param(
            "asset_context",
            type="string",
            enum=list(METADATA_LINEAGE_TASK_TYPES),
            description="Allowlisted Metadata and Lineage Agent task.",
        ),
        "qualified_name": Param(
            "dq.raw_orders",
            type="string",
            pattern=r"^([A-Za-z0-9_]+\.[A-Za-z0-9_]+)?$",
            description="Exact database.table identity for asset tasks.",
        ),
        "query": Param(
            "orders",
            type="string",
            maxLength=120,
            pattern=r"^[A-Za-z0-9 _.-]{0,120}$",
            description="Bounded metadata discovery query for trusted_asset_search.",
        ),
        "domain": Param(
            "",
            type="string",
            pattern=r"^[A-Za-z0-9_]{0,80}$",
            description="Optional metadata domain filter.",
        ),
        "data_layer": Param(
            "",
            type="string",
            enum=["", "raw", "staging", "mart"],
            description="Optional warehouse layer filter.",
        ),
        "certification_status": Param(
            "",
            type="string",
            enum=["", "experimental", "candidate", "certified", "deprecated"],
            description="Optional trust certification filter.",
        ),
        "lifecycle_status": Param(
            "",
            type="string",
            enum=["", "active", "deprecated"],
            description="Optional asset lifecycle filter.",
        ),
        "limit": Param(
            10,
            type="integer",
            minimum=1,
            maximum=25,
            description="Maximum metadata search results.",
        ),
        "max_depth": Param(
            5,
            type="integer",
            minimum=1,
            maximum=10,
            description="Maximum downstream dbt traversal depth.",
        ),
        "max_nodes": Param(
            100,
            type="integer",
            minimum=1,
            maximum=250,
            description="Maximum downstream dbt nodes returned.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Run and audit one bounded Metadata and Lineage Agent handoff.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=metadata_lineage_dag_params(),
    tags=["dq-platform", "agent", "metadata", "lineage", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for the Metadata and Lineage Agent smoke boundary.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_run_metadata_lineage_agent = runner_plain_bash_task(
        task_id="t10_run_metadata_lineage_agent",
        project_command=(
            "python scripts/run_metadata_lineage_agent.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--task-type '{{ dag_run.conf.get(\"task_type\", \"asset_context\") }}' "
            "--qualified-name '{{ dag_run.conf.get(\"qualified_name\", \"dq.raw_orders\") }}' "
            "--query '{{ dag_run.conf.get(\"query\", \"orders\") }}' "
            "--domain '{{ dag_run.conf.get(\"domain\", \"\") }}' "
            "--data-layer '{{ dag_run.conf.get(\"data_layer\", \"\") }}' "
            "--certification-status '{{ dag_run.conf.get(\"certification_status\", \"\") }}' "
            "--lifecycle-status '{{ dag_run.conf.get(\"lifecycle_status\", \"\") }}' "
            "--limit '{{ dag_run.conf.get(\"limit\", 10) }}' "
            "--max-depth '{{ dag_run.conf.get(\"max_depth\", 5) }}' "
            "--max-nodes '{{ dag_run.conf.get(\"max_nodes\", 100) }}' "
            "--requester airflow"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_verify_metadata_lineage_audit = runner_plain_bash_task(
        task_id="t20_verify_metadata_lineage_audit",
        project_command=(
            "python scripts/verify_metadata_lineage_agent.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--task-type '{{ dag_run.conf.get(\"task_type\", \"asset_context\") }}' "
            "--qualified-name '{{ dag_run.conf.get(\"qualified_name\", \"dq.raw_orders\") }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_metadata_lineage_summary = PythonOperator(
        task_id="t30_emit_metadata_lineage_summary",
        python_callable=emit_metadata_lineage_summary,
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_run_metadata_lineage_agent
        >> t20_verify_metadata_lineage_audit
        >> t30_emit_metadata_lineage_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)

