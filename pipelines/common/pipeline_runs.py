####
## Pipeline Run Observability Utility for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from pipelines.common.logging import logger


# --- Defining Constants
PIPELINE_RUNS_TABLE = "dq.pipeline_runs"

PIPELINE_RUN_COLUMNS = [
    "job_name",
    "dag_id",
    "task_id",
    "logical_date",
    "partition_dt",
    "status",
    "started_at",
    "ended_at",
    "duration_ms",
    "rows_read",
    "rows_written",
    "source_uri",
    "target_table",
    "error_message",
    "metadata_json",
]


# --- Defining Functions
def calculate_duration_ms(started_at: datetime, ended_at: datetime) -> int:
    """
    Calculate elapsed runtime in milliseconds for a pipeline run.

    Args:
        started_at: UTC timestamp when the pipeline step started.
        ended_at: UTC timestamp when the pipeline step ended.

    Returns:
        Runtime duration in milliseconds.
    """
    duration = int((ended_at - started_at).total_seconds() * 1000)

    return max(duration, 0)


def write_pipeline_run(
    client: Any,
    job_name: str,
    partition_dt: date | None,
    status: str,
    started_at: datetime,
    ended_at: datetime,
    rows_read: int | None = None,
    rows_written: int | None = None,
    source_uri: str = "",
    target_table: str = "",
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
    dag_id: str = "",
    task_id: str = "",
    logical_date: date | None = None,
) -> None:
    """
    Persist one pipeline run record to ClickHouse observability storage.

    Args:
        client: clickhouse-connect client instance.
        job_name: Logical job name.
        partition_dt: Business date partition processed by the job.
        status: Run status such as success, failed, or skipped.
        started_at: UTC timestamp when the job started.
        ended_at: UTC timestamp when the job ended.
        rows_read: Optional number of rows read from source.
        rows_written: Optional number of rows written to target.
        source_uri: Source URI used by the job.
        target_table: Target table or artifact namespace written by the job.
        error_message: Optional failure message.
        metadata: Optional structured metadata stored as JSON.
        dag_id: Optional Airflow DAG id.
        task_id: Optional Airflow task id.
        logical_date: Optional Airflow logical date when different from partition_dt.

    Returns:
        None.
    """
    resolved_logical_date = logical_date or partition_dt
    duration_ms          = calculate_duration_ms(started_at=started_at, ended_at=ended_at)

    row = [
        job_name,
        dag_id,
        task_id,
        resolved_logical_date,
        partition_dt,
        status,
        started_at,
        ended_at,
        duration_ms,
        rows_read,
        rows_written,
        source_uri,
        target_table,
        error_message,
        json.dumps(metadata or {}, ensure_ascii=True, default=str),
    ]

    logger.info(
        "Writing pipeline run | job=%s dt=%s status=%s rows_read=%s rows_written=%s target=%s",
        job_name,
        partition_dt,
        status,
        rows_read,
        rows_written,
        target_table,
    )

    client.insert(
        table=PIPELINE_RUNS_TABLE,
        data=[row],
        column_names=PIPELINE_RUN_COLUMNS,
    )

    logger.info("Pipeline run written | job=%s dt=%s status=%s", job_name, partition_dt, status)
