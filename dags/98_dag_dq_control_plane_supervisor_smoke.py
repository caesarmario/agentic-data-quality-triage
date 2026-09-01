####
## Airflow Control Plane Supervisor Smoke DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Manual Airflow acceptance boundary for policy-driven specialist routing."""

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.control_plane_supervisor import (
    CONTROL_PLANE_SUPERVISOR_INTENTS,
    emit_control_plane_supervisor_summary,
)
from dq_platform.helpers import (
    DEFAULT_START_DATE,
    default_dag_args,
    finish_task,
    runner_plain_bash_task,
    start_task,
)


# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Defining DAG ID And Documentation
DAG_ID = "98_dag_dq_control_plane_supervisor_smoke"

DOC_MD = """
# 98 - Control Plane Supervisor Smoke

Manual administrative DAG that classifies one bounded intent and executes either the default
single specialist handoff or an explicitly requested bounded fan-out plan. Both modes enforce
least-privilege tools, parent budgets, isolated workers, and ClickHouse audit evidence.

This pilot supports the Incident Triage Agent, Metadata and Lineage Agent, deterministic SQL
Safety and Review Agent, and deterministic Schema Drift Agent. It does not replace the default
supervisor-lite runtime, execute proposed SQL, alter schemas, execute remediation, or allow
LLM-selected tools. Fan-out remains opt-in, capped at 10 workers with default concurrency 3,
and cannot grant mutation permissions or select a provider/model from DagRun configuration.

Bounded fan-out example:

```json
{
  "execution_mode": "fanout",
  "intent": "asset_context",
  "qualified_name": "dq.raw_orders",
  "max_workers": 2,
  "max_concurrency": 2,
  "max_handoffs": 2,
  "max_model_calls": 0,
  "token_budget": 0,
  "estimated_cost_budget_usd": 0.0,
  "allow_external_llm": false
}
```

Metadata context example:

```json
{
  "intent": "asset_context",
  "qualified_name": "dq.raw_orders"
}
```

Incident triage example:

```json
{
  "intent": "triage_alert",
  "alert_key": "DQ-20260808-ABC123"
}
```

SQL review proposals are transported as Base64 to avoid raw SQL shell interpolation. Use
`scripts/trigger_airflow_control_plane_supervisor.py --intent review_sql` rather than manually
constructing the encoded payload.

Schema assessment example:

```json
{
  "intent": "schema_drift_assessment",
  "schema_run_id": "manual__schema_drift_source_20260820T200000000000",
  "qualified_name": "dq.raw_orders",
  "expected_schema_assessment": "compatible"
}
```
"""


# --- Defining Functions
def control_plane_supervisor_params() -> dict[str, Param]:
    """
    Build bounded supervisor smoke parameters.

    Returns:
        Dictionary containing intent, context, evidence, and budget bounds.
    """
    return {
        "intent": Param(
            "asset_context",
            type="string",
            enum=list(CONTROL_PLANE_SUPERVISOR_INTENTS),
            description="Explicit or deterministic auto supervisor intent.",
        ),
        "question": Param(
            "",
            type="string",
            maxLength=1_000,
            pattern=r"^[A-Za-z0-9 _.-]{0,1000}$",
            description="Optional bounded operator wording for auto routing.",
        ),
        "alert_key": Param(
            "",
            type="string",
            maxLength=500,
            pattern=r"^[A-Za-z0-9_.|:-]{0,500}$",
            description="Optional system alert key or human-facing Alert Ref.",
        ),
        "qualified_name": Param(
            "dq.raw_orders",
            type="string",
            pattern=r"^([A-Za-z0-9_]+\.[A-Za-z0-9_]+)?$",
            description="Exact database.table asset for metadata requests.",
        ),
        "query": Param(
            "orders",
            type="string",
            maxLength=120,
            pattern=r"^[A-Za-z0-9 _.-]{0,120}$",
            description="Bounded trusted metadata search query.",
        ),
        "domain": Param("", type="string", pattern=r"^[A-Za-z0-9_]{0,80}$"),
        "data_layer": Param("", type="string", enum=["", "raw", "staging", "mart"]),
        "certification_status": Param(
            "",
            type="string",
            enum=["", "experimental", "candidate", "certified", "deprecated"],
        ),
        "lifecycle_status": Param("", type="string", enum=["", "active", "deprecated"]),
        "sql_proposal_base64": Param(
            "",
            type="string",
            maxLength=30_000,
            pattern=r"^[A-Za-z0-9+/=]{0,30000}$",
            description="Base64-encoded SQL proposal; decoded only inside the Python runner.",
        ),
        "sql_purpose": Param(
            "",
            type="string",
            maxLength=500,
            pattern=r"^[A-Za-z0-9 _.,()/+-]{0,500}$",
        ),
        "sql_hard_limit": Param(100, type="integer", minimum=1, maximum=1_000),
        "sql_require_date_filter": Param(True, type="boolean"),
        "sql_max_scan_bytes": Param(
            1024 * 1024 * 1024,
            type="integer",
            minimum=1024 * 1024,
            maximum=1024 * 1024 * 1024 * 1024,
        ),
        "expected_sql_decision": Param(
            "",
            type="string",
            enum=["", "approved", "rejected"],
            description="Optional SQL review decision verified from retained audit evidence.",
        ),
        "schema_run_id": Param(
            "",
            type="string",
            maxLength=250,
            pattern=r"^[A-Za-z0-9_.:+-]{0,250}$",
            description="Exact persisted schema detector DagRun identifier.",
        ),
        "schema_finding_limit": Param(50, type="integer", minimum=1, maximum=100),
        "expected_schema_assessment": Param(
            "",
            type="string",
            enum=["", "compatible", "review_required", "breaking_change"],
            description="Optional schema compatibility result verified from audit evidence.",
        ),
        "result_limit": Param(10, type="integer", minimum=1, maximum=25),
        "max_depth": Param(5, type="integer", minimum=1, maximum=10),
        "max_nodes": Param(100, type="integer", minimum=1, maximum=250),
        "confidence_threshold": Param(
            0.70,
            type="number",
            minimum=0.10,
            maximum=0.95,
        ),
        "max_evidence_iterations": Param(2, type="integer", minimum=0, maximum=5),
        "manifest_s3_uri": Param(
            "",
            type="string",
            maxLength=2_048,
            pattern=(
                r"^(s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"
                r"(/[A-Za-z0-9][A-Za-z0-9._/-]{0,2000})?)?$"
            ),
        ),
        "artifacts_bucket": Param(
            "",
            type="string",
            maxLength=63,
            pattern=r"^([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])?$",
        ),
        "artifacts_prefix": Param(
            "agent-reports",
            type="string",
            pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$",
        ),
        "execution_mode": Param(
            "single",
            type="string",
            enum=["single", "fanout"],
            description="Single is default; fanout is manual and opt-in.",
        ),
        "max_workers": Param(1, type="integer", minimum=1, maximum=10),
        "max_concurrency": Param(1, type="integer", minimum=1, maximum=3),
        "expected_worker_count": Param(
            0,
            type="integer",
            minimum=0,
            maximum=10,
            description="Optional exact fan-out worker count; zero accepts the audited plan count.",
        ),
        "allow_external_llm": Param(
            False,
            type="boolean",
            description="Request-level permission; global provider switch must also be enabled.",
        ),
        "max_handoffs": Param(1, type="integer", minimum=1, maximum=10),
        "max_retries": Param(0, type="integer", minimum=0, maximum=0),
        "max_model_calls": Param(3, type="integer", minimum=0, maximum=10),
        "token_budget": Param(16_384, type="integer", minimum=0, maximum=64_000),
        "estimated_cost_budget_usd": Param(
            0.05,
            type="number",
            minimum=0.0,
            maximum=0.15,
        ),
        "latency_budget_ms": Param(
            300_000,
            type="integer",
            minimum=1_000,
            maximum=900_000,
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Route, execute, and verify a single or bounded fan-out supervisor run.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=control_plane_supervisor_params(),
    tags=["dq-platform", "agent", "supervisor", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for the bounded Control Plane Supervisor pilot.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t10_run_control_plane_supervisor = runner_plain_bash_task(
        task_id="t10_run_control_plane_supervisor",
        project_command=(
            "python scripts/run_control_plane_supervisor.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--intent '{{ dag_run.conf.get(\"intent\", \"asset_context\") }}' "
            "--question '{{ dag_run.conf.get(\"question\", \"\") }}' "
            "--alert-key '{{ dag_run.conf.get(\"alert_key\", \"\") }}' "
            "--qualified-name '{{ dag_run.conf.get(\"qualified_name\", \"dq.raw_orders\") }}' "
            "--query '{{ dag_run.conf.get(\"query\", \"orders\") }}' "
            "--domain '{{ dag_run.conf.get(\"domain\", \"\") }}' "
            "--data-layer '{{ dag_run.conf.get(\"data_layer\", \"\") }}' "
            "--certification-status '{{ dag_run.conf.get(\"certification_status\", \"\") }}' "
            "--lifecycle-status '{{ dag_run.conf.get(\"lifecycle_status\", \"\") }}' "
            "--sql-proposal-base64 '{{ dag_run.conf.get(\"sql_proposal_base64\", \"\") }}' "
            "--sql-purpose '{{ dag_run.conf.get(\"sql_purpose\", \"\") }}' "
            "--sql-hard-limit '{{ dag_run.conf.get(\"sql_hard_limit\", 100) }}' "
            "--sql-require-date-filter '{{ dag_run.conf.get(\"sql_require_date_filter\", true) | lower }}' "
            "--sql-max-scan-bytes '{{ dag_run.conf.get(\"sql_max_scan_bytes\", 1073741824) }}' "
            "--schema-run-id '{{ dag_run.conf.get(\"schema_run_id\", \"\") }}' "
            "--schema-finding-limit '{{ dag_run.conf.get(\"schema_finding_limit\", 50) }}' "
            "--result-limit '{{ dag_run.conf.get(\"result_limit\", 10) }}' "
            "--max-depth '{{ dag_run.conf.get(\"max_depth\", 5) }}' "
            "--max-nodes '{{ dag_run.conf.get(\"max_nodes\", 100) }}' "
            "--confidence-threshold '{{ dag_run.conf.get(\"confidence_threshold\", 0.70) }}' "
            "--max-evidence-iterations '{{ dag_run.conf.get(\"max_evidence_iterations\", 2) }}' "
            "--manifest-s3-uri '{{ dag_run.conf.get(\"manifest_s3_uri\", \"\") }}' "
            "--artifacts-bucket '{{ dag_run.conf.get(\"artifacts_bucket\", \"\") }}' "
            "--artifacts-prefix '{{ dag_run.conf.get(\"artifacts_prefix\", \"agent-reports\") }}' "
            "--requester airflow "
            "--execution-mode '{{ dag_run.conf.get(\"execution_mode\", \"single\") }}' "
            "--max-workers '{{ dag_run.conf.get(\"max_workers\", 1) }}' "
            "--max-concurrency '{{ dag_run.conf.get(\"max_concurrency\", 1) }}' "
            "--allow-external-llm "
            "'{{ dag_run.conf.get(\"allow_external_llm\", false) | lower }}' "
            "--max-handoffs '{{ dag_run.conf.get(\"max_handoffs\", 1) }}' "
            "--max-retries '{{ dag_run.conf.get(\"max_retries\", 0) }}' "
            "--max-model-calls '{{ dag_run.conf.get(\"max_model_calls\", 3) }}' "
            "--token-budget '{{ dag_run.conf.get(\"token_budget\", 16384) }}' "
            "--estimated-cost-budget-usd '{{ dag_run.conf.get(\"estimated_cost_budget_usd\", 0.05) }}' "
            "--latency-budget-ms '{{ dag_run.conf.get(\"latency_budget_ms\", 300000) }}'"
        ),
        execution_timeout=timedelta(minutes=20),
    )

    t20_verify_supervisor_audit = runner_plain_bash_task(
        task_id="t20_verify_supervisor_audit",
        project_command=(
            "python scripts/verify_control_plane_execution.py "
            "--run-id '{{ dag_run.run_id }}' "
            "--execution-mode '{{ dag_run.conf.get(\"execution_mode\", \"single\") }}' "
            "--expected-worker-count '{{ dag_run.conf.get(\"expected_worker_count\", 0) }}' "
            "--expected-intent '{{ dag_run.conf.get(\"intent\", \"asset_context\") }}' "
            "--expected-sql-decision '{{ dag_run.conf.get(\"expected_sql_decision\", \"\") }}' "
            "--expected-schema-assessment "
            "'{{ dag_run.conf.get(\"expected_schema_assessment\", \"\") }}' "
            "--expected-schema-run-id '{{ dag_run.conf.get(\"schema_run_id\", \"\") }}'"
        ),
        execution_timeout=timedelta(minutes=10),
    )

    t30_emit_supervisor_summary = PythonOperator(
        task_id="t30_emit_supervisor_summary",
        python_callable=emit_control_plane_supervisor_summary,
        execution_timeout=timedelta(minutes=5),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t10_run_control_plane_supervisor
        >> t20_verify_supervisor_audit
        >> t30_emit_supervisor_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
