####
## Airflow Metadata Registry Sync DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Manual Airflow entrypoint for idempotent trusted metadata synchronization."""

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
from dq_platform.metadata_registry import METADATA_REGISTRY_NAMES, emit_metadata_sync_summary


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "95_dag_dq_metadata_registry_sync"

DOC_MD = """
# 95 - Metadata Registry Sync

Manual administrative DAG that synchronizes an allowlisted YAML metadata contract into
the append-versioned `dq.metadata_assets` ClickHouse registry.

Schedule: none. The DAG is triggered after metadata contract changes or during explicit
acceptance testing. It is intentionally separate from the daily orders pipeline because
ownership, grain, SLA, and certification metadata change independently from daily data.

The sync is idempotent. Unchanged asset hashes write no rows. Removed assets receive an
inactive tombstone version; historical metadata is never deleted.

Manual `dag_run.conf` example:

```json
{"registry_name": "orders"}
```
"""


# --- Defining Functions
def metadata_sync_dag_params() -> dict[str, Param]:
    """
    Build allowlisted metadata sync parameters.

    Returns:
        Dictionary containing the bounded registry_name parameter.
    """
    return {
        "registry_name": Param(
            "orders",
            type="string",
            enum=list(METADATA_REGISTRY_NAMES),
            description="Allowlisted metadata contract synchronized and verified by this run.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Synchronize and verify trusted warehouse metadata in ClickHouse.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=metadata_sync_dag_params(),
    tags=["dq-platform", "metadata", "trust", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for metadata registry synchronization.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_sync_metadata_registry = runner_plain_bash_task(
        task_id="t10_sync_metadata_registry",
        project_command=(
            "python -m pipelines.metadata.sync_registry "
            "--registry '{{ dag_run.conf.get(\"registry_name\", \"orders\") }}' "
            "--mode sync"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_verify_metadata_registry = runner_plain_bash_task(
        task_id="t20_verify_metadata_registry",
        project_command=(
            "python -m pipelines.metadata.sync_registry "
            "--registry '{{ dag_run.conf.get(\"registry_name\", \"orders\") }}' "
            "--mode verify"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_metadata_sync_summary = PythonOperator(
        task_id="t30_emit_metadata_sync_summary",
        python_callable=emit_metadata_sync_summary,
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_sync_metadata_registry
        >> t20_verify_metadata_registry
        >> t30_emit_metadata_sync_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
