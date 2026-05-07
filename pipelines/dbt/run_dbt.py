####
## dbt Runner and Artifact Uploader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client, drop_date_partition_if_exists
from pipelines.common.logging import logger
from pipelines.common.pipeline_runs import write_pipeline_run
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.upload_to_s3 import upload_file_to_s3


# --- Defining Constants
DEFAULT_DBT_PROJECT_DIR  = PROJECT_ROOT / "warehouse" / "dbt"
DEFAULT_DBT_PROFILES_DIR = PROJECT_ROOT / "warehouse" / "dbt"
DEFAULT_ARTIFACTS_BUCKET = "dq-artifacts"
DEFAULT_ARTIFACTS_PREFIX = "dbt-artifacts/orders"

DBT_ARTIFACT_FILES = [
    "manifest.json",
    "run_results.json",
    "catalog.json",
    "sources.json",
]

DBT_STEPS = {
    "debug",
    "run",
    "test",
    "upload-artifacts",
}


DBT_PARTITION_REPLACE_TABLES = [
    "dq.stg_orders",
    "dq.fct_orders_daily",
]


# --- Defining Functions
def resolve_date_window(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[date, date, list[date]]:
    """
    Resolve CLI date arguments into a dbt var window and partition list.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive start date for backfill.
        end: Optional inclusive end date for backfill.

    Returns:
        Tuple of start date, end date, and all inclusive partition dates.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date dbt window | dt=%s", run_dt)

        return run_dt, run_dt, [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    start_dt   = parse_date(start)
    end_dt     = parse_date(end)
    partitions = iter_dates(start_date=start_dt, end_date=end_dt)

    logger.info("Resolved range dbt window | start=%s end=%s partitions=%d", start_dt, end_dt, len(partitions))

    return start_dt, end_dt, partitions


def resolve_artifacts_bucket(bucket: str | None = None) -> str:
    """
    Resolve the bucket used for dbt artifact uploads.

    Args:
        bucket: Optional explicit bucket override.

    Returns:
        S3 bucket name for dbt artifacts.
    """
    resolved_bucket = (
        bucket
        or os.getenv("DBT_ARTIFACTS_BUCKET")
        or os.getenv("ARTIFACTS_BUCKET")
        or DEFAULT_ARTIFACTS_BUCKET
    )

    logger.info("Resolved dbt artifacts bucket | bucket=%s", resolved_bucket)

    return resolved_bucket


def build_dbt_vars(start_dt: date, end_dt: date) -> str:
    """
    Build the JSON dbt vars payload used to bound models by business date.

    Args:
        start_dt: Inclusive start date.
        end_dt: Inclusive end date.

    Returns:
        JSON string passed to dbt `--vars`.
    """
    payload = {
        "start_dt": start_dt.isoformat(),
        "end_dt": end_dt.isoformat(),
    }

    vars_json = json.dumps(payload)
    logger.info("Built dbt vars | vars=%s", vars_json)

    return vars_json


def build_dbt_command(
    step: str,
    project_dir: Path,
    profiles_dir: Path,
    start_dt: date,
    end_dt: date,
    select: str = "",
    full_refresh: bool = False,
) -> list[str]:
    """
    Build a dbt CLI command for one execution step.

    Args:
        step: dbt step to run: debug, run, or test.
        project_dir: dbt project directory.
        profiles_dir: dbt profiles directory.
        start_dt: Inclusive start date for dbt vars.
        end_dt: Inclusive end date for dbt vars.
        select: Optional dbt selector string.
        full_refresh: Whether to pass --full-refresh for supported commands.

    Returns:
        Argument list suitable for subprocess.run.

    Raises:
        ValueError: If the step is not a runnable dbt command.
    """
    if step not in {"debug", "run", "test"}:
        raise ValueError(f"Unsupported dbt CLI step: {step}")

    command = [
        "dbt",
        step,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles_dir),
    ]

    if step in {"run", "test"}:
        command.extend(["--vars", build_dbt_vars(start_dt=start_dt, end_dt=end_dt)])

    if select:
        command.extend(["--select", select])

    if full_refresh and step == "run":
        command.append("--full-refresh")

    logger.info("Built dbt command | step=%s command=%s", step, " ".join(command))

    return command


def tail_text(value: str, max_chars: int = 4000) -> str:
    """
    Keep a bounded tail of command output for metadata logging.

    Args:
        value: Raw command output.
        max_chars: Maximum number of characters to retain.

    Returns:
        Tail of the provided text.
    """
    if len(value) <= max_chars:
        return value

    return value[-max_chars:]


def run_dbt_subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    """
    Execute a dbt command and stream bounded output back to the caller.

    Args:
        command: dbt command argument list.

    Returns:
        CompletedProcess returned by subprocess.run.

    Raises:
        subprocess.CalledProcessError: If dbt exits with a non-zero code.
    """
    logger.info("Executing dbt command | command=%s", " ".join(command))

    # Capture first so Airflow logs stay readable; then print stdout/stderr once per task.
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    if completed.stdout:
        print(completed.stdout)

    if completed.stderr:
        print(completed.stderr, file=sys.stderr)

    if completed.returncode != 0:
        logger.error("dbt command failed | return_code=%s command=%s", completed.returncode, " ".join(command))
        raise subprocess.CalledProcessError(
            returncode=completed.returncode,
            cmd=command,
            output=completed.stdout,
            stderr=completed.stderr,
        )

    logger.info("dbt command completed | return_code=%s command=%s", completed.returncode, " ".join(command))

    return completed


def drop_dbt_output_partitions(
    partitions: list[date],
    table_names: list[str] | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> dict[str, Any]:
    """
    Drop transformed table partitions before an idempotent dbt run.

    Args:
        partitions: Business date partitions to replace.
        table_names: Optional fully qualified tables. Defaults to staging and mart tables.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        Summary dictionary with attempted and dropped partition counts.
    """
    target_tables = table_names or DBT_PARTITION_REPLACE_TABLES
    client        = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    dropped_count = 0
    attempted     = 0

    logger.info(
        "Dropping dbt output partitions before run | tables=%s partitions=%s",
        target_tables,
        [item.isoformat() for item in partitions],
    )

    for table_name in target_tables:
        for partition_dt in partitions:
            attempted += 1

            if drop_date_partition_if_exists(client=client, table_name=table_name, partition_dt=partition_dt):
                dropped_count += 1

    summary = {
        "status": "success",
        "tables": target_tables,
        "partition_count": len(partitions),
        "attempted": attempted,
        "dropped": dropped_count,
    }

    logger.info("dbt output partitions prepared | summary=%s", summary)

    return summary


def write_dbt_pipeline_runs(
    job_name: str,
    partitions: list[date],
    status: str,
    started_at: datetime,
    ended_at: datetime,
    rows_read: int | None = None,
    rows_written: int | None = None,
    source_uri: str = "",
    target_table: str = "",
    error_message: str = "",
    metadata: dict[str, Any] | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    log_pipeline_run: bool = True,
) -> None:
    """
    Write one pipeline run record per partition touched by a dbt step.

    Args:
        job_name: Logical job name for the dbt step.
        partitions: Business dates affected by the dbt step.
        status: Run status such as success or failed.
        started_at: UTC timestamp when the dbt step started.
        ended_at: UTC timestamp when the dbt step ended.
        rows_read: Optional rows read count.
        rows_written: Optional rows written count.
        source_uri: Source project or artifact URI.
        target_table: Target table or artifact namespace.
        error_message: Optional failure message.
        metadata: Optional structured metadata stored as JSON.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        log_pipeline_run: Whether to write dq.pipeline_runs records.

    Returns:
        None.
    """
    if not log_pipeline_run:
        logger.info("Skipping dbt pipeline run logging | job=%s", job_name)
        return

    client = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)

    for partition_dt in partitions:
        write_pipeline_run(
            client=client,
            job_name=job_name,
            partition_dt=partition_dt,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            rows_read=rows_read,
            rows_written=rows_written,
            source_uri=source_uri,
            target_table=target_table,
            error_message=error_message,
            metadata=metadata,
        )


def run_dbt_step(
    step: str,
    start_dt: date,
    end_dt: date,
    partitions: list[date],
    project_dir: Path = DEFAULT_DBT_PROJECT_DIR,
    profiles_dir: Path = DEFAULT_DBT_PROFILES_DIR,
    select: str = "",
    full_refresh: bool = False,
    allow_failure: bool = False,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    log_pipeline_run: bool = True,
) -> dict[str, Any]:
    """
    Run one dbt CLI step and log observability rows to ClickHouse.

    Args:
        step: dbt step to run: debug, run, or test.
        start_dt: Inclusive start date for dbt vars.
        end_dt: Inclusive end date for dbt vars.
        partitions: Business dates affected by the step.
        project_dir: dbt project directory.
        profiles_dir: dbt profiles directory.
        select: Optional dbt selector string.
        full_refresh: Whether to pass --full-refresh for dbt run.
        allow_failure: Whether a non-zero dbt exit should be logged as a warning
            instead of raising. Intended for Airflow dbt test observability.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.
        log_pipeline_run: Whether to write dq.pipeline_runs records.

    Returns:
        Summary dictionary for the dbt step.
    """
    started_at = datetime.now(timezone.utc)
    command    = build_dbt_command(
        step=step,
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        start_dt=start_dt,
        end_dt=end_dt,
        select=select,
        full_refresh=full_refresh,
    )

    try:
        if step == "run" and not full_refresh:
            # dbt incremental insert_overwrite cannot remove a partition when the new data is empty.
            drop_dbt_output_partitions(
                partitions=partitions,
                clickhouse_host=clickhouse_host,
                clickhouse_port=clickhouse_port,
            )

        completed = run_dbt_subprocess(command)
        ended_at  = datetime.now(timezone.utc)
        metadata  = {
            "runner": "pipelines.dbt.run_dbt",
            "step": step,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "select": select,
            "stdout_tail": tail_text(completed.stdout),
            "stderr_tail": tail_text(completed.stderr),
        }

        write_dbt_pipeline_runs(
            job_name=f"dbt_{step}",
            partitions=partitions,
            status="success",
            started_at=started_at,
            ended_at=ended_at,
            source_uri=str(project_dir),
            target_table="warehouse/dbt",
            metadata=metadata,
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            log_pipeline_run=log_pipeline_run,
        )

        summary = {
            "status": "success",
            "step": step,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "partition_count": len(partitions),
            "return_code": completed.returncode,
        }

        logger.info("dbt step completed | summary=%s", summary)

        return summary

    except subprocess.CalledProcessError as exc:
        ended_at = datetime.now(timezone.utc)
        status   = "warning" if allow_failure else "failed"

        if allow_failure:
            logger.warning(
                "dbt step returned non-zero but will continue | step=%s start=%s end=%s return_code=%s",
                step,
                start_dt,
                end_dt,
                exc.returncode,
            )

        else:
            logger.exception(
                "dbt step returned non-zero | step=%s start=%s end=%s return_code=%s",
                step,
                start_dt,
                end_dt,
                exc.returncode,
            )

        write_dbt_pipeline_runs(
            job_name=f"dbt_{step}",
            partitions=partitions,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            source_uri=str(project_dir),
            target_table="warehouse/dbt",
            error_message=str(exc)[:1000],
            metadata={
                "runner": "pipelines.dbt.run_dbt",
                "step": step,
                "start_dt": start_dt.isoformat(),
                "end_dt": end_dt.isoformat(),
                "select": select,
                "allow_failure": allow_failure,
                "stdout_tail": tail_text(exc.output or ""),
                "stderr_tail": tail_text(exc.stderr or ""),
            },
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            log_pipeline_run=log_pipeline_run,
        )

        if allow_failure:
            summary = {
                "status": "warning",
                "step": step,
                "start_dt": start_dt.isoformat(),
                "end_dt": end_dt.isoformat(),
                "partition_count": len(partitions),
                "return_code": exc.returncode,
                "message": "dbt returned non-zero and was allowed to continue for observability.",
            }

            logger.info("dbt step completed with allowed failure | summary=%s", summary)

            return summary

        raise

    except Exception as exc:
        ended_at = datetime.now(timezone.utc)
        logger.exception("dbt step failed | step=%s start=%s end=%s", step, start_dt, end_dt)

        write_dbt_pipeline_runs(
            job_name=f"dbt_{step}",
            partitions=partitions,
            status="failed",
            started_at=started_at,
            ended_at=ended_at,
            source_uri=str(project_dir),
            target_table="warehouse/dbt",
            error_message=str(exc)[:1000],
            metadata={
                "runner": "pipelines.dbt.run_dbt",
                "step": step,
                "start_dt": start_dt.isoformat(),
                "end_dt": end_dt.isoformat(),
                "select": select,
            },
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            log_pipeline_run=log_pipeline_run,
        )

        raise


def build_artifact_keys(
    filename: str,
    prefix: str,
    run_id: str,
    start_dt: date,
    end_dt: date,
) -> tuple[str, str]:
    """
    Build versioned and latest S3 object keys for one dbt artifact.

    Args:
        filename: dbt artifact filename.
        prefix: S3 key prefix.
        run_id: dbt artifact upload run id.
        start_dt: Inclusive dbt start date.
        end_dt: Inclusive dbt end date.

    Returns:
        Tuple of versioned key and latest pointer key.
    """
    normalized_prefix = prefix.strip("/")
    versioned_key     = (
        f"{normalized_prefix}/runs/"
        f"run_id={run_id}/"
        f"start_dt={start_dt.isoformat()}/"
        f"end_dt={end_dt.isoformat()}/"
        f"{filename}"
    )
    latest_key        = f"{normalized_prefix}/latest/{filename}"

    return versioned_key, latest_key


def upload_dbt_artifacts(
    start_dt: date,
    end_dt: date,
    partitions: list[date],
    project_dir: Path = DEFAULT_DBT_PROJECT_DIR,
    bucket: str | None = None,
    prefix: str = DEFAULT_ARTIFACTS_PREFIX,
    endpoint_url: str | None = None,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
    log_pipeline_run: bool = True,
) -> dict[str, Any]:
    """
    Upload selected dbt target artifacts to SeaweedFS S3.

    Args:
        start_dt: Inclusive dbt start date.
        end_dt: Inclusive dbt end date.
        partitions: Business dates associated with the artifact upload.
        project_dir: dbt project directory containing target artifacts.
        bucket: Optional artifacts bucket override.
        prefix: S3 key prefix for dbt artifacts.
        endpoint_url: Optional S3 endpoint URL override.
        clickhouse_host: Optional ClickHouse host override for pipeline logging.
        clickhouse_port: Optional ClickHouse HTTP port override.
        log_pipeline_run: Whether to write dq.pipeline_runs records.

    Returns:
        Summary dictionary containing uploaded artifact URIs.
    """
    started_at      = datetime.now(timezone.utc)
    target_dir      = project_dir / "target"
    resolved_bucket = resolve_artifacts_bucket(bucket)
    run_id          = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"_{uuid4().hex[:8]}"
    uploaded        = []
    skipped         = []

    logger.info("Uploading dbt artifacts | target_dir=%s bucket=%s prefix=%s", target_dir, resolved_bucket, prefix)

    try:
        for filename in DBT_ARTIFACT_FILES:
            artifact_path = target_dir / filename

            if not artifact_path.exists():
                logger.info("dbt artifact missing; skipping upload | path=%s", artifact_path)
                skipped.append(filename)

                continue

            versioned_key, latest_key = build_artifact_keys(
                filename=filename,
                prefix=prefix,
                run_id=run_id,
                start_dt=start_dt,
                end_dt=end_dt,
            )

            versioned_uri = upload_file_to_s3(
                local_path=artifact_path,
                bucket=resolved_bucket,
                key=versioned_key,
                endpoint_url=endpoint_url,
            )
            latest_uri = upload_file_to_s3(
                local_path=artifact_path,
                bucket=resolved_bucket,
                key=latest_key,
                endpoint_url=endpoint_url,
            )

            uploaded.append(
                {
                    "filename": filename,
                    "versioned_uri": versioned_uri,
                    "latest_uri": latest_uri,
                }
            )

        ended_at = datetime.now(timezone.utc)
        summary  = {
            "status": "success",
            "bucket": resolved_bucket,
            "prefix": prefix,
            "run_id": run_id,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "uploaded_count": len(uploaded),
            "skipped": skipped,
            "artifacts": uploaded,
        }

        write_dbt_pipeline_runs(
            job_name="dbt_upload_artifacts",
            partitions=partitions,
            status="success",
            started_at=started_at,
            ended_at=ended_at,
            rows_written=len(uploaded),
            source_uri=str(target_dir),
            target_table=f"s3://{resolved_bucket}/{prefix.strip('/')}",
            metadata=summary,
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            log_pipeline_run=log_pipeline_run,
        )

        logger.info("dbt artifacts uploaded | summary=%s", summary)

        return summary

    except Exception as exc:
        ended_at = datetime.now(timezone.utc)
        logger.exception("dbt artifact upload failed | bucket=%s prefix=%s", resolved_bucket, prefix)

        write_dbt_pipeline_runs(
            job_name="dbt_upload_artifacts",
            partitions=partitions,
            status="failed",
            started_at=started_at,
            ended_at=ended_at,
            rows_written=len(uploaded),
            source_uri=str(target_dir),
            target_table=f"s3://{resolved_bucket}/{prefix.strip('/')}",
            error_message=str(exc)[:1000],
            metadata={
                "runner": "pipelines.dbt.run_dbt",
                "uploaded_count": len(uploaded),
                "skipped": skipped,
            },
            clickhouse_host=clickhouse_host,
            clickhouse_port=clickhouse_port,
            log_pipeline_run=log_pipeline_run,
        )

        raise


def run_step_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """
    Execute one dbt runner step from parsed CLI arguments.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Summary dictionary for the requested step.
    """
    start_dt, end_dt, partitions = resolve_date_window(dt=args.dt, start=args.start, end=args.end)
    project_dir                 = Path(args.project_dir)
    profiles_dir                = Path(args.profiles_dir)

    if args.step == "upload-artifacts":
        return upload_dbt_artifacts(
            start_dt=start_dt,
            end_dt=end_dt,
            partitions=partitions,
            project_dir=project_dir,
            bucket=args.artifacts_bucket,
            prefix=args.artifacts_prefix,
            endpoint_url=args.endpoint_url,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
            log_pipeline_run=not args.skip_pipeline_log,
        )

    return run_dbt_step(
        step=args.step,
        start_dt=start_dt,
        end_dt=end_dt,
        partitions=partitions,
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        select=args.select,
        full_refresh=args.full_refresh,
        allow_failure=args.allow_failure,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
        log_pipeline_run=not args.skip_pipeline_log,
    )


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for dbt execution and artifact upload.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run dbt commands and upload dbt artifacts for the DQ platform.")

    parser.add_argument("--step", required=True, choices=sorted(DBT_STEPS), help="dbt runner step to execute.")
    parser.add_argument("--dt", default=None, help="Single business date in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for dbt vars.")
    parser.add_argument("--end", default=None, help="Inclusive end date for dbt vars.")
    parser.add_argument("--project-dir", default=str(DEFAULT_DBT_PROJECT_DIR), help="dbt project directory.")
    parser.add_argument("--profiles-dir", default=str(DEFAULT_DBT_PROFILES_DIR), help="dbt profiles directory.")
    parser.add_argument("--select", default="", help="Optional dbt selector.")
    parser.add_argument("--full-refresh", action="store_true", help="Pass --full-refresh to dbt run.")
    parser.add_argument("--allow-failure", action="store_true", help="Log non-zero dbt exit as warning instead of failing.")
    parser.add_argument("--artifacts-bucket", default=None, help="Optional S3 bucket for dbt artifacts.")
    parser.add_argument("--artifacts-prefix", default=DEFAULT_ARTIFACTS_PREFIX, help="S3 prefix for dbt artifacts.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument("--skip-pipeline-log", action="store_true", help="Skip dq.pipeline_runs writes.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run the requested dbt step.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        summary = run_step_from_args(args)

    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
