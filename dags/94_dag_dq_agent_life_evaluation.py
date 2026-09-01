####
## Airflow LIFE Agent Reliability Evaluation DAG for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import logging
from datetime import timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, Param

from dq_platform.life_evaluation import (
    DEFAULT_LIFE_ARTIFACT_PREFIX,
    DEFAULT_LIFE_REPLAY_PREFIX,
    DEFAULT_MIN_CONFIDENCE,
    LIFE_SCENARIO_NAMES,
    LIFE_SOURCE_MODES,
    SAFE_ARTIFACT_PREFIX,
    SAFE_EVALUATION_RUN_ID,
    SAFE_REPORT_S3_URI,
    emit_life_evaluation_summary,
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
DAG_ID = "94_dag_dq_agent_life_evaluation"

DOC_MD = """
# 94 - LIFE Agent Reliability Evaluation

Manual administrative DAG that evaluates one stored triage report against an
allowlisted incident ground-truth scenario. Evaluation-only replays are marked
explicitly and never mutate the physical ClickHouse schema.

The evaluator classifies report reliability failures, writes JSON and Markdown
artifacts to SeaweedFS, and records one ClickHouse audit event. It only proposes
improvements for human review. It never changes prompts, code, tools, SQL
guardrails, DQ rules, Airflow DAGs, model routing, or remediation behavior.

```json
{
  "scenario": "missing_latest_day",
  "source_mode": "stored_report",
  "report_s3_uri": "s3://dq-artifacts/agent-reports/.../report.json",
  "evaluation_run_id": "life-eval-20260716T100000000000",
  "minimum_confidence": 0.70,
  "enable_critic": true,
  "fail_on_eval_failure": false
}
```
"""


# --- Defining Functions
def life_evaluation_dag_params() -> dict[str, Param]:
    """
    Build bounded parameters for one manual LIFE evaluation.

    Returns:
        Dictionary containing scenario, report, run, threshold, and failure policy.
    """
    return {
        "scenario": Param(
            "missing_latest_day",
            type="string",
            enum=list(LIFE_SCENARIO_NAMES),
            description="Allowlisted incident ground-truth scenario.",
        ),
        "source_mode": Param(
            "stored_report",
            type="string",
            enum=list(LIFE_SOURCE_MODES),
            description="Use a stored production-style report or an explicit deterministic replay.",
        ),
        "report_s3_uri": Param(
            "",
            type="string",
            pattern=rf"^(?:|{SAFE_REPORT_S3_URI.pattern.removeprefix('^').removesuffix('$')})$",
            description="SeaweedFS URI ending in report.json. Trigger helper derives this for replay mode.",
        ),
        "evaluation_run_id": Param(
            "",
            type="string",
            pattern=rf"^(?:|{SAFE_EVALUATION_RUN_ID.pattern.removeprefix('^').removesuffix('$')})$",
            description="Optional path-safe correlation id. Blank uses the Airflow run id.",
        ),
        "minimum_confidence": Param(
            DEFAULT_MIN_CONFIDENCE,
            type="number",
            minimum=0.0,
            maximum=1.0,
            description="Reports below this confidence require human review.",
        ),
        "artifact_prefix": Param(
            DEFAULT_LIFE_ARTIFACT_PREFIX,
            type="string",
            pattern=SAFE_ARTIFACT_PREFIX.pattern,
            description="Path-safe prefix inside dq-artifacts.",
        ),
        "fail_on_eval_failure": Param(
            False,
            type="boolean",
            description="Fail the DAG after persisting artifacts when reliability status is fail.",
        ),
        "enable_critic": Param(
            False,
            type="boolean",
            description="Run an opt-in deterministic critic for review or fail outcomes.",
        ),
    }


# --- Defining DAG Structure
with DAG(
    dag_id=DAG_ID,
    description="Evaluate stored triage reports and persist human-reviewed reliability proposals.",
    start_date=DEFAULT_START_DATE,
    schedule=None,
    catchup=False,
    render_template_as_native_obj=True,
    max_active_runs=1,
    default_args=default_dag_args(retries=0),
    params=life_evaluation_dag_params(),
    tags=["dq-platform", "agent", "life", "evaluation", "manual", "administrative"],
    doc_md=DOC_MD,
) as dag:
    """
    Airflow DAG definition for bounded LIFE-inspired report evaluation.

    Returns:
        DAG object registered by Airflow.
    """
    t00_start = start_task()

    t05_prepare_source_report = runner_plain_bash_task(
        task_id="t05_prepare_source_report",
        project_command=(
            "python scripts/prepare_life_source_report.py "
            "--source-mode '{{ dag_run.conf.get(\"source_mode\", \"stored_report\") }}' "
            "--scenario '{{ dag_run.conf.get(\"scenario\", \"missing_latest_day\") }}' "
            "--report-s3-uri '{{ dag_run.conf.get(\"report_s3_uri\", \"\") }}' "
            "--evaluation-run-id '{{ dag_run.conf.get(\"evaluation_run_id\") or dag_run.run_id }}' "
            f"--replay-prefix '{DEFAULT_LIFE_REPLAY_PREFIX}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t10_evaluate_life_report = runner_plain_bash_task(
        task_id="t10_evaluate_life_report",
        project_command=(
            "python scripts/run_life_evaluation.py "
            "--scenario '{{ dag_run.conf.get(\"scenario\", \"missing_latest_day\") }}' "
            "--report-s3-uri '{{ dag_run.conf.get(\"report_s3_uri\", \"\") }}' "
            "--evaluation-run-id '{{ dag_run.conf.get(\"evaluation_run_id\") or dag_run.run_id }}' "
            "--minimum-confidence '{{ dag_run.conf.get(\"minimum_confidence\", 0.70) }}' "
            "--artifact-prefix '{{ dag_run.conf.get(\"artifact_prefix\", \"agent-life\") }}' "
            "{% if dag_run.conf.get(\"enable_critic\", false) %}--enable-critic {% endif %}"
            "{% if dag_run.conf.get(\"fail_on_eval_failure\", false) %}--fail-on-eval-failure{% endif %}"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t20_verify_life_artifacts = runner_plain_bash_task(
        task_id="t20_verify_life_artifacts",
        project_command=(
            "python scripts/verify_life_evaluation.py "
            "--evaluation-run-id '{{ dag_run.conf.get(\"evaluation_run_id\") or dag_run.run_id }}' "
            "--scenario '{{ dag_run.conf.get(\"scenario\", \"missing_latest_day\") }}' "
            "--source-mode '{{ dag_run.conf.get(\"source_mode\", \"stored_report\") }}' "
            "--source-report-s3-uri '{{ dag_run.conf.get(\"report_s3_uri\", \"\") }}' "
            "--artifact-prefix '{{ dag_run.conf.get(\"artifact_prefix\", \"agent-life\") }}'"
        ),
        execution_timeout=timedelta(minutes=5),
    )

    t30_emit_life_summary = PythonOperator(
        task_id="t30_emit_life_summary",
        python_callable=emit_life_evaluation_summary,
        execution_timeout=timedelta(minutes=2),
    )

    t90_finish = finish_task()

    (
        t00_start
        >> t05_prepare_source_report
        >> t10_evaluate_life_report
        >> t20_verify_life_artifacts
        >> t30_emit_life_summary
        >> t90_finish
    )


logger.info("Loaded DAG | dag_id=%s", DAG_ID)
