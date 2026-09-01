####
## Batch Triage Runner CLI for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.graph import (
    DEFAULT_CONFIDENCE_TARGET,
    DEFAULT_MAX_EVIDENCE_LOOP,
    DEFAULT_REPORT_PREFIX,
    TriageRuntimeConfig,
    run_triage,
)
from agent.checkpointing import (
    CHECKPOINT_MODE_OFF,
    build_checkpoint_replay_thread_id,
    build_checkpoint_thread_id,
    load_checkpoint_settings,
)
from agent.tools.alerts import list_alerts
from pipelines.common.logging import logger
from pipelines.seeding.helpers import iter_dates, parse_date


# --- Defining Constants
DEFAULT_MANIFEST_S3_URI = "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"


# --- Defining Functions
def parse_bool_flag(value: str | bool | None, default: bool = True) -> bool:
    """
    Parse a flexible boolean flag from Airflow/Jinja/CLI values.

    Args:
        value: Boolean-like input such as true, false, 1, 0, yes, or no.
        default: Value returned when input is blank.

    Returns:
        Parsed boolean value.

    Raises:
        ValueError: If the value cannot be interpreted as a boolean.
    """
    if value is None or value == "":
        return default

    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()

    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True

    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False

    raise ValueError(f"Invalid boolean value: {value}")


def clean_optional(value: str | None) -> str | None:
    """
    Normalize optional CLI strings passed by Airflow templating.

    Args:
        value: Raw optional value.

    Returns:
        Stripped string, or None when blank.
    """
    if value is None:
        return None

    cleaned = value.strip()

    return cleaned or None


def resolve_run_dates(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[date]:
    """
    Resolve CLI date arguments into an inclusive alert scan date list.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive start date for backfill.
        end: Optional inclusive end date for backfill.

    Returns:
        List of business dates to scan for open alerts.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date triage scan | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end unless alert_key/alert_id is provided.")

    return iter_dates(start_date=parse_date(start), end_date=parse_date(end))


def build_runtime_config(args: argparse.Namespace) -> TriageRuntimeConfig:
    """
    Build triage runtime config from parsed CLI arguments.

    Args:
        args: Parsed argparse namespace.

    Returns:
        TriageRuntimeConfig used by the LangGraph workflow.
    """
    return TriageRuntimeConfig(
        manifest_path=clean_optional(args.manifest_path),
        manifest_s3_uri=clean_optional(args.manifest_s3_uri),
        s3_endpoint_url=clean_optional(args.endpoint_url),
        artifacts_bucket=clean_optional(args.artifacts_bucket),
        artifacts_prefix=args.artifacts_prefix,
        clickhouse_host=clean_optional(args.clickhouse_host),
        clickhouse_port=args.clickhouse_port,
    )


def run_single_alert_triage(
    alert_id: str | None,
    alert_key: str | None,
    args: argparse.Namespace,
    config: TriageRuntimeConfig,
) -> dict[str, Any]:
    """
    Run triage for one alert id or alert key.

    Args:
        alert_id: Optional ClickHouse alert UUID.
        alert_key: Optional stable alert key.
        args: Parsed argparse namespace containing runtime thresholds.
        config: Triage runtime config.

    Returns:
        Compact triage result dictionary.
    """
    checkpoint_settings  = load_checkpoint_settings(
        mode=args.checkpoint_mode,
        sqlite_path=clean_optional(args.checkpoint_sqlite_path),
        busy_timeout_ms=args.checkpoint_busy_timeout_ms,
    )
    checkpoint_thread_id = None

    if checkpoint_settings.enabled:
        checkpoint_namespace = clean_optional(args.checkpoint_namespace)
        correlation_value    = alert_key or alert_id or ""

        if not checkpoint_namespace:
            raise ValueError("checkpoint_namespace is required when checkpointing is enabled.")

        checkpoint_thread_id = build_checkpoint_thread_id(
            namespace=checkpoint_namespace,
            correlation_value=correlation_value,
        )

    logger.info(
        "Running single alert triage | alert_id=%s alert_key=%s checkpoint_mode=%s "
        "checkpoint_thread_id=%s resume=%s replay_checkpoint_id=%s",
        alert_id,
        alert_key,
        checkpoint_settings.mode,
        checkpoint_thread_id or "disabled",
        args.checkpoint_resume,
        clean_optional(args.checkpoint_replay_id) or "none",
    )

    report = run_triage(
        alert_id=alert_id,
        alert_key=alert_key,
        confidence_threshold=args.confidence_threshold,
        max_evidence_iterations=args.max_evidence_iterations,
        config=config,
        checkpoint_mode=checkpoint_settings.mode,
        checkpoint_sqlite_path=checkpoint_settings.sqlite_path,
        checkpoint_busy_timeout_ms=checkpoint_settings.busy_timeout_ms,
        checkpoint_thread_id=checkpoint_thread_id,
        checkpoint_resume=args.checkpoint_resume,
        checkpoint_replay_id=clean_optional(args.checkpoint_replay_id),
        checkpoint_replay_request_id=clean_optional(args.checkpoint_replay_request_id),
    )

    replay_thread_id = ""

    if checkpoint_thread_id and clean_optional(args.checkpoint_replay_id):
        replay_thread_id = build_checkpoint_replay_thread_id(
            source_thread_id=checkpoint_thread_id,
            source_checkpoint_id=str(args.checkpoint_replay_id),
            replay_request_id=str(args.checkpoint_replay_request_id),
        )

    return {
        "status": "success",
        "agent_run_id": str(report.agent_run_id),
        "alert_id": str(report.alert.alert_id),
        "alert_key": report.alert.alert_key,
        "severity": report.alert.severity,
        "confidence": report.confidence,
        "top_hypothesis": report.top_hypothesis.title if report.top_hypothesis else None,
        "markdown_report_s3_uri": report.markdown_report_s3_uri,
        "json_report_s3_uri": report.json_report_s3_uri,
        "checkpoint_mode": checkpoint_settings.mode,
        "checkpoint_thread_id": checkpoint_thread_id or "",
        "checkpoint_resume_requested": args.checkpoint_resume,
        "checkpoint_replay_id": clean_optional(args.checkpoint_replay_id) or "",
        "checkpoint_replay_request_id": clean_optional(args.checkpoint_replay_request_id) or "",
        "checkpoint_replay_thread_id": replay_thread_id,
    }


def run_alert_list_triage(
    dates: list[date],
    args: argparse.Namespace,
    config: TriageRuntimeConfig,
) -> list[dict[str, Any]]:
    """
    List alerts by date/status and run triage for a bounded number of rows.

    Args:
        dates: Business dates to scan for alerts.
        args: Parsed argparse namespace containing filters and limits.
        config: Triage runtime config.

    Returns:
        List of compact triage result dictionaries.
    """
    results        = []
    remaining      = max(1, min(args.limit, 50))
    alert_status   = args.status

    logger.info(
        "Running alert-list triage | dates=%s status=%s limit=%d",
        [item.isoformat() for item in dates],
        alert_status,
        remaining,
    )

    for run_dt in dates:
        if remaining <= 0:
            break

        lookup = list_alerts(
            status=alert_status,
            dt=run_dt.isoformat(),
            limit=remaining,
            clickhouse_host=args.clickhouse_host,
            clickhouse_port=args.clickhouse_port,
        )
        alerts = lookup.get("alerts", [])

        logger.info("Listed alerts for triage | dt=%s alerts=%d", run_dt, len(alerts))

        for alert_row in alerts:
            if remaining <= 0:
                break

            alert_key = str(alert_row["alert_key"])
            results.append(
                run_single_alert_triage(
                    alert_id=None,
                    alert_key=alert_key,
                    args=args,
                    config=config,
                )
            )
            remaining -= 1

    return results


def run_batch_triage(args: argparse.Namespace) -> dict[str, Any]:
    """
    Run one-alert or date-scanned batch triage from parsed arguments.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Summary dictionary for the batch triage execution.
    """
    enabled   = parse_bool_flag(args.enabled, default=True)
    alert_id  = clean_optional(args.alert_id)
    alert_key = clean_optional(args.alert_key)
    config    = build_runtime_config(args)
    replay_id      = clean_optional(args.checkpoint_replay_id)
    replay_request = clean_optional(args.checkpoint_replay_request_id)

    if not enabled:
        logger.info("Triage disabled by flag; skipping all alert execution")

        return {
            "status": "skipped",
            "reason": "triage_disabled",
            "results": [],
            "triaged_count": 0,
        }

    if replay_id or replay_request:
        if not replay_id or not replay_request:
            raise ValueError("Historical checkpoint replay requires both checkpoint and request identifiers.")

        if bool(alert_id) == bool(alert_key):
            raise ValueError("Historical checkpoint replay requires exactly one explicit alert_id or alert_key.")

    if alert_id or alert_key:
        results = [run_single_alert_triage(alert_id=alert_id, alert_key=alert_key, args=args, config=config)]

    else:
        dates   = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)
        results = run_alert_list_triage(dates=dates, args=args, config=config)

    summary = {
        "status": "success",
        "triaged_count": len(results),
        "results": results,
    }

    logger.info("Batch triage completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for batch alert triage.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run agentic triage for one alert or a bounded list of open alerts.")

    parser.add_argument("--enabled", default="true", help="Boolean flag used by orchestrators to no-op triage.")
    parser.add_argument("--alert-id", default=None, help="Optional ClickHouse alert UUID.")
    parser.add_argument("--alert-key", default=None, help="Optional stable alert key.")
    parser.add_argument("--dt", default=None, help="Single business date to scan, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for alert scan.")
    parser.add_argument("--end", default=None, help="Inclusive end date for alert scan.")
    parser.add_argument("--status", default="open", help="Alert status to scan when alert_key/alert_id is not set.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum alerts to triage across the date window.")
    parser.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_TARGET, help="Confidence threshold.")
    parser.add_argument("--max-evidence-iterations", type=int, default=DEFAULT_MAX_EVIDENCE_LOOP, help="Maximum evidence loops.")
    parser.add_argument("--manifest-path", default=None, help="Optional local dbt manifest.json path.")
    parser.add_argument("--manifest-s3-uri", default=DEFAULT_MANIFEST_S3_URI, help="Optional S3 URI for dbt manifest.json.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--artifacts-bucket", default=None, help="Optional artifacts bucket override.")
    parser.add_argument("--artifacts-prefix", default=DEFAULT_REPORT_PREFIX, help="S3 prefix for report artifacts.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")
    parser.add_argument(
        "--checkpoint-mode",
        default=CHECKPOINT_MODE_OFF,
        help="Checkpoint mode: off or sqlite. Defaults to off.",
    )
    parser.add_argument(
        "--checkpoint-namespace",
        default=None,
        help="Run-scoped namespace used to derive one safe thread id per alert.",
    )
    parser.add_argument(
        "--checkpoint-sqlite-path",
        default=None,
        help="Optional absolute SQLite checkpoint path override.",
    )
    parser.add_argument(
        "--checkpoint-busy-timeout-ms",
        type=int,
        default=None,
        help="Optional SQLite lock timeout in milliseconds.",
    )
    parser.add_argument(
        "--checkpoint-resume",
        action="store_true",
        help="Resume each derived checkpoint thread instead of starting it.",
    )
    parser.add_argument(
        "--checkpoint-replay-id",
        default=None,
        help="Exact historical checkpoint id selected for branched replay.",
    )
    parser.add_argument(
        "--checkpoint-replay-request-id",
        default=None,
        help="Stable request id used to derive the replay child thread.",
    )

    return parser


def main() -> None:
    """
    Parse CLI arguments, run batch triage, and print JSON summary.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        summary = run_batch_triage(args)

    except ValueError as exc:
        parser.error(str(exc))

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
