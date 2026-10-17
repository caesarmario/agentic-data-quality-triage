####
## dbt Runner and Artifact Uploader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger
from pipelines.common.pipeline_runs import write_pipeline_run
from pipelines.common.publication import (
    PostgresPublicationStore, Preconditions, PreparedPartition, PublicationConflict, PublicationLock,
    capture_preconditions, capture_recovery_preconditions, clone_live_table, copy_partition, partition_fingerprint,
    prepare_partition, publication_lock, publish_group, scratch_database, table_structure,
    validate_partition_scope,
)
from pipelines.seeding.helpers import iter_dates, parse_date
from pipelines.seeding.upload_to_s3 import build_s3_client, upload_file_to_s3


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
MAX_LINEAGE_SOURCE_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_LINEAGE_VIEW_BYTES = 1024 * 1024
MAX_LINEAGE_VIEW_NODES = 128
MAX_PUBLICATION_MARKER_BYTES = 16 * 1024
LINEAGE_TEST_TYPES = frozenset({"accepted_values", "freshness_by_date", "not_null", "rowcount_anomaly", "unique"})
LINEAGE_MODELS = {
    "stg_orders": "models/staging/stg_orders.sql",
    "fct_orders_daily": "models/marts/fct_orders_daily.sql",
}
LINEAGE_TEST_PATHS = frozenset({"models/staging/schema.yml", "models/marts/schema.yml"})
LINEAGE_POINTER_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"datasentry:dbt-lineage-latest-manifest").digest()[:8], "big", signed=True,
)

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
    if step not in {"debug", "run", "test", "compile"}:
        raise ValueError(f"Unsupported dbt CLI step: {step}")

    command = [
        "dbt",
        step,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles_dir),
    ]

    if step in {"run", "test", "compile"}:
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


def run_dbt_subprocess(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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
        env=env,
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


@dataclass(frozen=True)
class ScopedDbtBuild:
    """Immutable input and isolated outputs exposed to the recovery prerequisite gate."""
    prepared: list[PreparedPartition]
    input_table: str
    input_fingerprint: str
    artifact_dir: Path
    model_hash: str


def dbt_execution_identity() -> str:
    """Share artifacts across dbt tasks in one DagRun, without mutable latest pointers."""
    run_id = os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
    if run_id:
        return f"dbt:{os.getenv('AIRFLOW_CTX_DAG_ID', '')}:{run_id}"
    # Separate CLI steps must supply the same --execution-id explicitly.
    return f"dbt:{uuid4().hex}"


def model_contract_hash(project_dir: Path = DEFAULT_DBT_PROJECT_DIR,
                        profiles_dir: Path = DEFAULT_DBT_PROFILES_DIR) -> str:
    """Pin trusted model SQL, macros, tests, project config, and exact profile bytes."""
    files = {project_dir / name for name in ("dbt_project.yml", "packages.yml", "selectors.yml")
             if (project_dir / name).is_file()}
    for directory in ("models", "macros", "tests"):
        files.update(path for path in (project_dir / directory).rglob("*")
                     if path.is_file() and path.suffix in {".sql", ".yml", ".yaml"})
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(project_dir).as_posix()):
        digest.update(path.relative_to(project_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    digest.update(b"profiles.yml\0" + (profiles_dir / "profiles.yml").read_bytes())
    return digest.hexdigest()


def execution_artifact_dir(project_dir: Path, execution_id: str, partition_dt: date) -> Path:
    """Return a traversal-safe path unique to the execution and partition."""
    digest = hashlib.sha256(execution_id.encode()).hexdigest()
    return project_dir / "target" / "publication" / digest / partition_dt.isoformat()


def _scoped_env(database: str) -> dict[str, str]:
    return {**os.environ, "DBT_PUBLICATION_SCHEMA": database, "DBT_INPUT_SCHEMA": database,
            "DBT_RAW_ORDERS_TABLE": "raw_orders_input"}


def _artifact_command(command: list[str], artifact_dir: Path) -> list[str]:
    return [*command, "--target-path", str(artifact_dir / "target"),
            "--log-path", str(artifact_dir / "logs"), "--no-partial-parse"]


def build_scoped_dbt_outputs(
    client: Any, locked: PublicationLock, *, raw_source: str = "dq.raw_orders",
    project_dir: Path = DEFAULT_DBT_PROJECT_DIR,
    profiles_dir: Path = DEFAULT_DBT_PROFILES_DIR,
    intent: str = "daily", subprocess_runner: Callable[..., Any] | None = None,
) -> ScopedDbtBuild:
    """Snapshot raw once, compile trusted SQL, and fill exact live-DDL clones.

    dbt never executes DDL against live tables. Compiled SQL reads only the isolated
    raw input and isolated stg relation. Keeping INSERT under platform ownership also
    avoids dbt's configured MergeTree replacing a live ReplacingMergeTree engine.
    """
    dt = locked.partition_dt
    database = scratch_database(locked.execution_id, dt)
    if raw_source not in {"dq.raw_orders", f"{database}.raw_orders"}:
        raise PublicationConflict("Only live raw or this execution's pinned raw stage is a trusted input")
    expected = capture_preconditions(locked)
    input_table = f"{database}.raw_orders_input"
    model_hash = model_contract_hash(project_dir, profiles_dir)
    artifact_dir = execution_artifact_dir(project_dir, locked.execution_id, dt)
    if (artifact_dir / "build.intent.json").exists() or (artifact_dir / "target").exists():
        raise PublicationConflict("Execution artifacts already exist; do not reuse an earlier build")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with (artifact_dir / "build.intent.json").open("x", encoding="utf-8") as intent_file:
        json.dump({"execution_id": locked.execution_id, "model_hash": model_hash,
                   "partition_dt": dt.isoformat()}, intent_file)
    before = partition_fingerprint(client, raw_source, dt)
    clone_live_table(client, "dq.raw_orders", input_table)
    copy_partition(client, raw_source, input_table, dt)
    input_fingerprint = partition_fingerprint(client, input_table, dt)
    if input_fingerprint != before or partition_fingerprint(client, raw_source, dt) != before:
        raise PublicationConflict("Raw input changed while capturing the immutable build input")
    for target in DBT_PARTITION_REPLACE_TABLES:
        clone_live_table(client, target, f"{database}.{target.split('.')[1]}")
    command = build_dbt_command("compile", project_dir, profiles_dir, dt, dt)
    command.extend(["--select", "stg_orders", "fct_orders_daily"])
    (subprocess_runner or run_dbt_subprocess)(_artifact_command(command, artifact_dir), env=_scoped_env(database))
    if model_contract_hash(project_dir, profiles_dir) != model_hash:
        raise PublicationConflict("Trusted model/profile files changed during compile")
    manifest = json.loads((artifact_dir / "target" / "manifest.json").read_text(encoding="utf-8"))
    prepared = []
    for name in ("stg_orders", "fct_orders_daily"):
        nodes = [node for node in manifest.get("nodes", {}).values()
                 if node.get("resource_type") == "model" and node.get("name") == name]
        if len(nodes) != 1 or nodes[0].get("schema") != database or nodes[0].get("alias") != name:
            raise PublicationConflict("Compiled model is not bound to the isolated output relation")
        sql = nodes[0].get("compiled_code", "").strip().rstrip(";")
        # Only repository-compiled SELECTs are accepted; no client SQL, hooks or materialization SQL.
        # Comments may precede the SELECT; reject statement separators in any position.
        if not sql or ";" in sql or not sql.lower().lstrip().startswith(("select", "with", "--")):
            raise PublicationConflict("Expected one trusted compiled model SELECT")
        live = f"dq.{name}"
        stage = f"{database}.{name}"
        names = [str(column[0]) for column in table_structure(client, stage).columns
                 if column[2] not in {"MATERIALIZED", "ALIAS"}]
        projection = ", ".join(names)
        # BY NAME, not SELECT *: dbt model output order differs from the bootstrap DDL.
        client.command(f"INSERT INTO {stage} ({projection}) SELECT {projection} FROM ({sql})")
        prepared.append(prepare_partition(client, locked, live, stage, intent=intent, expected=expected))
    locked.require(expected)
    if partition_fingerprint(client, input_table, dt) != input_fingerprint:
        raise PublicationConflict("Isolated raw input changed during build")
    (artifact_dir / "publication.json").write_text(json.dumps({
        "execution_id": locked.execution_id, "partition_dt": dt.isoformat(),
        "database": database, "model_hash": model_hash, "input_table": input_table,
        "input_fingerprint": input_fingerprint,
        "publication_ids": [item.publication_id for item in prepared],
    }, sort_keys=True), encoding="utf-8")
    return ScopedDbtBuild(prepared, input_table, input_fingerprint, artifact_dir, model_hash)


def execute_recovery_pipeline(
    plan: dict[str, Any], execution_id: str, *, validate_recovery: Callable[[dict[str, Any]], None],
    client: Any = None, store: PostgresPublicationStore | None = None, s3_client: Any = None,
    project_dir: Path = DEFAULT_DBT_PROJECT_DIR, profiles_dir: Path = DEFAULT_DBT_PROFILES_DIR,
    subprocess_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute an already-approved recovery under one dataset/date lock.

    Required plan fields: runbook, partition_dt, snapshot_manifest, expected_revision,
    expected_watermark, expected_model_hash, compensation_preapproved (strict bool).
    Main owns approval/incident/snapshot eligibility and the mandatory full built
    prerequisite callback. That callback raises on failure; it is not a client flag.
    Empty recovery is always rejected. No migration is ever run here.
    """
    from pipelines.loading.load_clickhouse import prepare_raw_partition
    from pipelines.seeding.snapshots import read_pinned_snapshot

    if not callable(validate_recovery):
        raise ValueError("Recovery requires a trusted full-prerequisite validator")
    dt = plan["partition_dt"]
    dt = date.fromisoformat(dt) if isinstance(dt, str) else dt
    if type(dt) is not date or type(plan["expected_revision"]) is not int:
        raise ValueError("Invalid recovery date/revision")
    if type(plan["compensation_preapproved"]) is not bool:
        raise ValueError("Compensation approval must be an explicit boolean")
    if plan["runbook"] not in {"replay_raw_partition", "rebuild_derived_partition", "replay_and_rebuild_partition"}:
        raise ValueError("Unsupported recovery runbook")
    if plan["expected_revision"] < 0 or not isinstance(plan["expected_watermark"], str):
        raise ValueError("Invalid approved revision/watermark")
    expected = Preconditions(plan["expected_revision"], plan["expected_watermark"])
    client = client or build_clickhouse_client()
    with publication_lock("orders", dt, execution_id, store=store) as locked:
        locked.require(expected)
        if model_contract_hash(project_dir, profiles_dir) != plan["expected_model_hash"]:
            raise PublicationConflict("Recovery model/profile hash no longer matches approval")
        frame = read_pinned_snapshot(plan["snapshot_manifest"], s3_client=s3_client)
        # Upstream manifests intentionally exclude fault/evaluation labels.
        frame = frame.copy()
        frame["incident_scenario"] = ""
        raw = None
        build = None
        prepared = []
        if plan["runbook"] in {"replay_raw_partition", "replay_and_rebuild_partition"}:
            raw = prepare_raw_partition(client, locked, frame, intent="recovery")
            prepared.append(raw)
        if plan["runbook"] in {"rebuild_derived_partition", "replay_and_rebuild_partition"}:
            build = build_scoped_dbt_outputs(client, locked, raw_source=raw.stage_table if raw else "dq.raw_orders",
                                            project_dir=project_dir, profiles_dir=profiles_dir,
                                            intent="recovery", subprocess_runner=subprocess_runner)
            prepared.extend(build.prepared)
        context = {"plan": plan, "execution_id": execution_id, "client": client,
                   "lock": locked, "raw": raw, "build": build, "source_frame": frame,
                   "prepared": prepared}
        if validate_recovery(context) is False:
            raise PublicationConflict("Full recovery prerequisite validator rejected the built stages")
        locked.require(expected)
        if model_contract_hash(project_dir, profiles_dir) != plan["expected_model_hash"]:
            raise PublicationConflict("Recovery model/profile hash changed before publication")
        publications = publish_group(client, locked, prepared,
                                     compensation_preapproved=plan["compensation_preapproved"])
        return {"status": "published", "execution_id": execution_id, "partition_dt": dt.isoformat(),
                "atomicity": "single_table_partition_only", "publications": publications,
                "artifact_dir": str(build.artifact_dir) if build else None,
                "model_hash": plan["expected_model_hash"]}


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
    *,
    execution_id: str | None = None,
    client: Any = None,
    store: PostgresPublicationStore | None = None,
    allow_empty_simulation: bool = False,
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
    if full_refresh:
        raise ValueError("Live full-refresh is disabled; use bounded partition publication")
    if step == "run" and select:
        raise ValueError("Publication builds the complete trusted stg/fct dependency pair; selectors are not supported")
    execution_id = execution_id or dbt_execution_identity()
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
        publications = []
        artifact_dirs = []
        if step in {"run", "test"}:
            client = client or build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
            for partition_dt in partitions:
                with publication_lock("orders", partition_dt, execution_id, store=store) as locked:
                    if step == "run":
                        simulation_empty = allow_empty_simulation
                        if not simulation_empty:
                            receipt = locked.sql("SELECT intent FROM dq_control.partition_publications WHERE dataset=%s "
                                "AND partition_dt=%s AND target_table='dq.raw_orders' AND status='published' "
                                "ORDER BY updated_at DESC LIMIT 1", ("orders", partition_dt), fetch=True)
                            simulation_empty = bool(receipt and receipt[0] == "simulation_empty")
                        intent = "simulation_empty" if simulation_empty else "daily"
                        build = build_scoped_dbt_outputs(client, locked, project_dir=project_dir,
                                                        profiles_dir=profiles_dir, intent=intent)
                        publications.extend(publish_group(client, locked, build.prepared))
                        artifact_dirs.append(str(build.artifact_dir))
                    else:
                        artifact_dir = execution_artifact_dir(project_dir, execution_id, partition_dt)
                        marker = json.loads((artifact_dir / "publication.json").read_text(encoding="utf-8"))
                        database = scratch_database(execution_id, partition_dt)
                        if (marker["execution_id"] != execution_id or marker["database"] != database
                                or marker["model_hash"] != model_contract_hash(project_dir, profiles_dir)):
                            raise PublicationConflict("dbt test artifacts do not match this execution/model")
                        if partition_fingerprint(client, marker["input_table"], partition_dt) != marker["input_fingerprint"]:
                            raise PublicationConflict("dbt test input snapshot changed")
                        test_command = build_dbt_command("test", project_dir, profiles_dir,
                                                         partition_dt, partition_dt, select=select)
                        run_dbt_subprocess(_artifact_command(test_command, artifact_dir / "tests"),
                                           env=_scoped_env(database))
                        artifact_dirs.append(str(artifact_dir))
            completed = subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        else:
            debug_dir = execution_artifact_dir(project_dir, execution_id, start_dt) / "debug"
            completed = run_dbt_subprocess(command, env={**os.environ,
                "DBT_LOG_PATH": str(debug_dir / "logs"), "DBT_TARGET_PATH": str(debug_dir / "target")})
        ended_at  = datetime.now(timezone.utc)
        metadata  = {
            "runner": "pipelines.dbt.run_dbt",
            "step": step,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "select": select,
            "stdout_tail": tail_text(completed.stdout),
            "stderr_tail": tail_text(completed.stderr),
            "execution_id": execution_id,
            "artifact_dirs": artifact_dirs,
            "publications": publications,
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
            "execution_id": execution_id,
            "artifact_dirs": artifact_dirs,
            "publications": publications,
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


def _read_bounded_file(path: Path, limit: int, label: str) -> bytes:
    """Read one artifact only after checking its size and recheck after the read."""
    size = path.stat().st_size
    if size < 1 or size > limit:
        raise PublicationConflict(f"{label} exceeds its configured size bound")
    with path.open("rb") as source:
        payload = source.read(limit + 1)
    if len(payload) != size or len(payload) > limit:
        raise PublicationConflict(f"{label} changed or exceeded its configured size bound")
    return payload


def _relation_parts(value: Any) -> tuple[str, str] | None:
    """Parse a two-part dbt relation without accepting arbitrary SQL or paths."""
    if not isinstance(value, str) or len(value) > 256:
        return None
    parts = [item.strip().strip('`"') for item in value.split(".")]
    if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in parts):
        return None
    return parts[0], parts[1]


def build_lineage_manifest_view(
    source_manifest: bytes,
    *,
    source_manifest_uri: str,
    source_manifest_sha256: str,
    execution_id: str,
    upload_run_id: str,
    model_hash: str,
    partition_dt: date,
    scratch_schema: str,
) -> bytes:
    """Build a bounded, sanitized lineage projection from a verified dbt manifest.

    The source manifest is immutable and retained separately. This view carries
    only the known orders source, two models, and schema-defined test edges.
    """
    if not isinstance(source_manifest, bytes) or not 1 <= len(source_manifest) <= MAX_LINEAGE_SOURCE_MANIFEST_BYTES:
        raise PublicationConflict("Source dbt manifest exceeds its configured size bound")
    if hashlib.sha256(source_manifest).hexdigest() != source_manifest_sha256:
        raise PublicationConflict("Source dbt manifest SHA-256 does not match its versioned artifact")
    if not re.fullmatch(r"[a-f0-9]{64}", source_manifest_sha256):
        raise PublicationConflict("Source dbt manifest SHA-256 is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", execution_id):
        raise PublicationConflict("dbt execution identifier is not an allowlisted public identifier")
    if not re.fullmatch(r"\d{8}T\d{12}Z_[a-f0-9]{8}", upload_run_id):
        raise PublicationConflict("dbt artifact upload identifier is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", model_hash):
        raise PublicationConflict("dbt model hash is invalid")
    if type(partition_dt) is not date or not re.fullmatch(r"dq_pub_[a-f0-9]{24}", scratch_schema):
        raise PublicationConflict("dbt lineage scope is invalid")
    if not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9._=/+-]{1,768}",
                        source_manifest_uri):
        raise PublicationConflict("Versioned manifest URI is not a safe S3 locator")
    if any(part in {".", ".."} for part in source_manifest_uri.removeprefix("s3://").split("/")[1:]):
        raise PublicationConflict("Versioned manifest URI contains an unsafe path segment")

    try:
        original = json.loads(source_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicationConflict("Versioned dbt manifest is not valid JSON") from None
    if not isinstance(original, dict) or not all(
        isinstance(original.get(name), dict) for name in ("nodes", "sources", "parent_map", "child_map")
    ):
        raise PublicationConflict("Versioned dbt manifest is missing lineage graph collections")

    original_nodes = original["nodes"]
    original_sources = original["sources"]
    id_map: dict[str, str] = {}
    view_nodes: dict[str, dict[str, Any]] = {}
    view_sources: dict[str, dict[str, Any]] = {}
    model_original_ids: dict[str, str] = {}

    for name, path in LINEAGE_MODELS.items():
        matches = [
            (uid, node) for uid, node in original_nodes.items()
            if isinstance(node, dict) and node.get("resource_type") == "model"
            and node.get("name") == name and node.get("original_file_path", "").replace("\\", "/") == path
        ]
        if len(matches) != 1:
            raise PublicationConflict(f"Expected exactly one repository-owned dbt model: {name}")
        original_id, node = matches[0]
        alias = node.get("alias")
        relation = _relation_parts(node.get("relation_name"))
        if (node.get("schema") != scratch_schema or alias != name or relation != (scratch_schema, name)):
            raise PublicationConflict(f"Compiled dbt relation is not isolated for model: {name}")
        logical_id = f"model.orders.{name}"
        model_original_ids[name] = original_id
        id_map[original_id] = logical_id
        view_nodes[logical_id] = {
            "resource_type": "model", "name": name, "alias": name,
            "schema": "dq", "relation_name": f"dq.{name}",
            "path": path.removeprefix("models/"), "original_file_path": path,
        }

    source_matches = [
        (uid, node) for uid, node in original_sources.items()
        if isinstance(node, dict) and node.get("resource_type") == "source"
        and node.get("name") == "raw_orders"
        and node.get("original_file_path", "").replace("\\", "/") == "models/sources.yml"
        and uid.startswith("source.") and uid.rsplit(".", 2)[-2:] == ["raw", "raw_orders"]
    ]
    if len(source_matches) != 1:
        raise PublicationConflict("Expected exactly one repository-owned raw_orders source")
    source_id, source_node = source_matches[0]
    source_name = source_node.get("source_name")
    source_relation = _relation_parts(source_node.get("relation_name"))
    if (source_name not in (None, "raw") or source_node.get("schema") != scratch_schema
            or source_node.get("identifier") != "raw_orders_input"
            or source_relation != (scratch_schema, "raw_orders_input")):
        raise PublicationConflict("Compiled raw_orders source is not bound to the isolated input")
    logical_source_id = "source.orders.raw.raw_orders"
    id_map[source_id] = logical_source_id
    view_sources[logical_source_id] = {
        "resource_type": "source", "source_name": "raw", "name": "raw_orders",
        "identifier": "raw_orders", "alias": "raw_orders", "schema": "dq",
        "relation_name": "dq.raw_orders", "path": "models/sources.yml",
        "original_file_path": "models/sources.yml",
    }

    # Preserve only known generic test labels and only tests declared in the
    # repository's two schema files and attached directly to an included model.
    tests = []
    for uid, node in original_nodes.items():
        if (not isinstance(node, dict) or node.get("resource_type") != "test"
                or node.get("original_file_path", "").replace("\\", "/") not in LINEAGE_TEST_PATHS):
            continue
        parents = [parent for parent in original["parent_map"].get(uid, [])
                   if parent in model_original_ids.values()]
        if len(parents) != 1:
            continue
        metadata = node.get("test_metadata")
        test_type = metadata.get("name") if isinstance(metadata, dict) else None
        if test_type not in LINEAGE_TEST_TYPES:
            raw_name = node.get("name")
            test_type = next((kind for kind in sorted(LINEAGE_TEST_TYPES, key=len, reverse=True)
                              if isinstance(raw_name, str) and
                              (raw_name == kind or raw_name.startswith(kind + "_"))), None)
        if test_type not in LINEAGE_TEST_TYPES:
            continue
        model_name = next(name for name, model_id in model_original_ids.items() if model_id == parents[0])
        tests.append((model_name, test_type, uid))
    tests.sort()
    for ordinal, (model_name, test_type, original_id) in enumerate(tests, start=1):
        logical_id = f"test.orders.{model_name}.{test_type}.{ordinal:03d}"
        id_map[original_id] = logical_id
        view_nodes[logical_id] = {"resource_type": "test", "name": test_type}

    if len(id_map) > MAX_LINEAGE_VIEW_NODES:
        raise PublicationConflict("Filtered dbt lineage graph exceeds its node bound")

    def mapped_edges(graph: dict[str, Any]) -> dict[str, list[str]]:
        result = {}
        for original_id, logical_id in id_map.items():
            edges = graph.get(original_id, [])
            if not isinstance(edges, list):
                raise PublicationConflict("dbt lineage graph contains a malformed edge list")
            result[logical_id] = sorted({id_map[edge] for edge in edges if edge in id_map})
        return result

    view = {
        "metadata": {"lineage_view": "datasentry-orders-v1"},
        "nodes": view_nodes,
        "sources": view_sources,
        "exposures": {}, "metrics": {}, "semantic_models": {}, "saved_queries": {},
        "parent_map": mapped_edges(original["parent_map"]),
        "child_map": mapped_edges(original["child_map"]),
        "lineage_provenance": {
            "source_manifest_uri": source_manifest_uri,
            "source_manifest_sha256": source_manifest_sha256,
            "execution_id": execution_id,
            "upload_run_id": upload_run_id,
            "model_hash": model_hash,
            "partition_dt": partition_dt.isoformat(),
            "view_version": 1,
            "normalization": {"source.orders.raw.raw_orders": "dq.raw_orders",
                              "model.orders.stg_orders": "dq.stg_orders",
                              "model.orders.fct_orders_daily": "dq.fct_orders_daily"},
        },
    }
    payload = json.dumps(view, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_LINEAGE_VIEW_BYTES:
        raise PublicationConflict("Sanitized dbt lineage view exceeds its write bound")
    return payload


@contextmanager
def _lineage_pointer_lock() -> Iterator[None]:
    """Serialize latest-pointer publication with a dedicated PostgreSQL session lock."""
    store = PostgresPublicationStore.from_env()
    connection = store.connection_factory()
    cursor = connection.cursor()
    locked = False
    try:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (LINEAGE_POINTER_LOCK_KEY,))
        row = cursor.fetchone()
        connection.commit()
        if not row or not row[0]:
            raise PublicationConflict("Another uploader is publishing the latest lineage manifest")
        locked = True
        yield
    finally:
        try:
            if locked:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LINEAGE_POINTER_LOCK_KEY,))
                connection.commit()
        finally:
            cursor.close()
            connection.close()


def _read_s3_object(client: Any, bucket: str, key: str, limit: int) -> bytes:
    """Read an S3 object with both advertised and streamed size bounds."""
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        if int(response.get("ContentLength", 0)) > limit:
            raise PublicationConflict("Versioned dbt manifest exceeds its read bound")
        payload = body.read(limit + 1)
    finally:
        close = getattr(body, "close", None)
        if close:
            close()
    if not 1 <= len(payload) <= limit:
        raise PublicationConflict("Versioned dbt manifest is empty or exceeds its read bound")
    return payload


def _latest_pointer_is_newer(client: Any, bucket: str, key: str, upload_run_id: str) -> bool:
    """Keep an older concurrently finishing upload from rolling back discovery."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    current = head.get("Metadata", {}).get("lineage-run-id", "")
    if not re.fullmatch(r"\d{8}T\d{12}Z_[a-f0-9]{8}", current):
        return False
    return current > upload_run_id


def publish_lineage_manifest_view(
    *, source_manifest: bytes, source_manifest_uri: str, source_manifest_sha256: str,
    execution_id: str, upload_run_id: str, model_hash: str, partition_dt: date,
    scratch_schema: str, bucket: str, latest_key: str, endpoint_url: str | None = None,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Verify the immutable source object, then atomically publish its lineage-only view."""
    client = s3_client or build_s3_client(endpoint_url=endpoint_url)
    versioned_uri_parts = source_manifest_uri.removeprefix("s3://").split("/", 1)
    if len(versioned_uri_parts) != 2 or versioned_uri_parts[0] != bucket:
        raise PublicationConflict("Versioned manifest URI does not match the configured bucket")
    original_key = versioned_uri_parts[1]
    remote_bytes = _read_s3_object(client, bucket, original_key, MAX_LINEAGE_SOURCE_MANIFEST_BYTES)
    if hashlib.sha256(remote_bytes).hexdigest() != source_manifest_sha256 or remote_bytes != source_manifest:
        raise PublicationConflict("Versioned dbt manifest failed byte/hash verification")
    view_bytes = build_lineage_manifest_view(
        source_manifest, source_manifest_uri=source_manifest_uri,
        source_manifest_sha256=source_manifest_sha256, execution_id=execution_id,
        upload_run_id=upload_run_id, model_hash=model_hash, partition_dt=partition_dt,
        scratch_schema=scratch_schema,
    )
    with _lineage_pointer_lock():
        if _latest_pointer_is_newer(client, bucket, latest_key, upload_run_id):
            return {"status": "superseded", "uri": f"s3://{bucket}/{latest_key}",
                    "source_manifest_uri": source_manifest_uri,
                    "source_manifest_sha256": source_manifest_sha256,
                    "view_bytes": len(view_bytes)}
        client.put_object(
            Bucket=bucket, Key=latest_key, Body=view_bytes, ContentType="application/json",
            Metadata={"lineage-run-id": upload_run_id,
                      "source-manifest-sha256": source_manifest_sha256,
                      "model-hash": model_hash},
        )
    return {"status": "published", "uri": f"s3://{bucket}/{latest_key}",
            "source_manifest_uri": source_manifest_uri,
            "source_manifest_sha256": source_manifest_sha256,
            "view_bytes": len(view_bytes)}


def _publish_latest_lineage_manifest(
    *, project_dir: Path, execution_id: str, partition_dt: date, upload_run_id: str,
    bucket: str, prefix: str, start_dt: date, end_dt: date, endpoint_url: str | None = None,
    s3_client: Any = None,
) -> dict[str, Any]:
    """Bind the latest view to one run-scoped, execution-verified build manifest."""
    artifact_dir = execution_artifact_dir(project_dir, execution_id, partition_dt)
    manifest_path = artifact_dir / "target" / "manifest.json"
    marker_path = artifact_dir / "publication.json"
    intent_path = artifact_dir / "build.intent.json"
    manifest_bytes = _read_bounded_file(manifest_path, MAX_LINEAGE_SOURCE_MANIFEST_BYTES,
                                        "Source dbt manifest")
    marker = json.loads(_read_bounded_file(marker_path, MAX_PUBLICATION_MARKER_BYTES,
                                           "dbt publication marker"))
    intent = json.loads(_read_bounded_file(intent_path, MAX_PUBLICATION_MARKER_BYTES,
                                           "dbt build intent"))
    scratch = scratch_database(execution_id, partition_dt)
    model_hash = marker.get("model_hash")
    if (marker.get("execution_id") != execution_id or intent.get("execution_id") != execution_id
            or marker.get("partition_dt") != partition_dt.isoformat()
            or intent.get("partition_dt") != partition_dt.isoformat()
            or marker.get("database") != scratch or intent.get("model_hash") != model_hash
            or not re.fullmatch(r"[a-f0-9]{64}", str(model_hash or ""))):
        raise PublicationConflict("dbt manifest does not match its isolated execution marker")
    source_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    versioned_key, _ = build_artifact_keys(
        f"{partition_dt.isoformat()}/build/manifest.json", prefix, upload_run_id, start_dt, end_dt,
    )
    source_uri = f"s3://{bucket}/{versioned_key}"
    _, latest_key = build_artifact_keys("manifest.json", prefix, upload_run_id, start_dt, end_dt)
    return publish_lineage_manifest_view(
        source_manifest=manifest_bytes, source_manifest_uri=source_uri,
        source_manifest_sha256=source_sha256, execution_id=execution_id,
        upload_run_id=upload_run_id, model_hash=model_hash, partition_dt=partition_dt,
        scratch_schema=scratch, bucket=bucket, latest_key=latest_key,
        endpoint_url=endpoint_url, s3_client=s3_client,
    )


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
    *,
    execution_id: str | None = None,
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
    execution_id    = execution_id or dbt_execution_identity()
    target_dir      = execution_artifact_dir(project_dir, execution_id, start_dt)
    resolved_bucket = resolve_artifacts_bucket(bucket)
    run_id          = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"_{uuid4().hex[:8]}"
    uploaded        = []
    skipped         = []

    logger.info("Uploading dbt artifacts | target_dir=%s bucket=%s prefix=%s", target_dir, resolved_bucket, prefix)

    try:
        candidates = []
        for partition_dt in partitions:
            root = execution_artifact_dir(project_dir, execution_id, partition_dt)
            if not (root / "publication.json").is_file():
                raise PublicationConflict("No execution-scoped dbt artifacts; refusing shared target fallback")
            for stage_name, stage_dir in (("build", root / "target"), ("tests", root / "tests" / "target")):
                for filename in DBT_ARTIFACT_FILES:
                    candidates.append((f"{partition_dt.isoformat()}/{stage_name}/{filename}", stage_dir / filename))
            candidates.append((f"{partition_dt.isoformat()}/publication.json", root / "publication.json"))
        for filename, artifact_path in candidates:

            if not artifact_path.exists():
                logger.info("dbt artifact missing; skipping upload | path=%s", artifact_path)
                skipped.append(filename)

                continue

            versioned_key, _ = build_artifact_keys(
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
            uploaded.append(
                {
                    "filename": filename,
                    "versioned_uri": versioned_uri,
                }
            )

        if not partitions:
            raise PublicationConflict("A dbt artifact upload requires at least one partition")
        lineage_manifest = _publish_latest_lineage_manifest(
            project_dir=project_dir, execution_id=execution_id, partition_dt=max(partitions),
            upload_run_id=run_id, bucket=resolved_bucket, prefix=prefix,
            start_dt=start_dt, end_dt=end_dt, endpoint_url=endpoint_url,
        )

        ended_at = datetime.now(timezone.utc)
        summary  = {
            "status": "success",
            "bucket": resolved_bucket,
            "prefix": prefix,
            "run_id": run_id,
            "execution_id": execution_id,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "uploaded_count": len(uploaded),
            "skipped": skipped,
            "artifacts": uploaded,
            "lineage_manifest": lineage_manifest,
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
            execution_id=args.execution_id,
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
        execution_id=args.execution_id,
        allow_empty_simulation=args.allow_empty_simulation,
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
    parser.add_argument("--execution-id", default=None,
                        help="Stable identity shared by separate CLI run/test/upload steps. Airflow uses its DagRun.")
    parser.add_argument("--allow-empty-simulation", action="store_true",
                        help="Explicit backed-up empty incident simulation, never recovery.")

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
