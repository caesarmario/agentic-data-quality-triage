####
## Daily Orders Landing Runner for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.generate_orders import generate_and_write_orders
from pipelines.seeding.helpers import iter_dates, parse_date


def resolve_run_dates(
    dt: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[date]:
    """
    Resolve CLI date arguments into an inclusive execution date list.

    Args:
        dt: Optional single business date in YYYY-MM-DD format.
        start: Optional inclusive start date for backfill.
        end: Optional inclusive end date for backfill.

    Returns:
        List of business dates to process.

    Raises:
        ValueError: If date arguments are missing or mutually invalid.
    """
    if dt and (start or end):
        raise ValueError("Use either --dt or --start/--end, not both.")

    if dt:
        run_dt = parse_date(dt)
        logger.info("Resolved single-date run | dt=%s", run_dt)

        return [run_dt]

    if not start or not end:
        raise ValueError("Provide --dt or both --start and --end.")

    start_date = parse_date(start)
    end_date   = parse_date(end)

    return iter_dates(start_date=start_date, end_date=end_date)


def run_orders_landing(
    dates: list[date],
    config: OrdersSeedConfig,
    upload: bool = True,
    endpoint_url: str | None = None,
    incident_scenario: str = "baseline",
) -> dict[str, Any]:
    """
    Generate local Parquet files and optionally upload them to S3 landing storage.

    Args:
        dates: Business dates to process.
        config: Validated orders seeding config.
        upload: Whether to upload generated files to SeaweedFS S3.
        endpoint_url: Optional explicit S3 endpoint URL.
        incident_scenario: Scenario label stored with generated rows.

    Returns:
        Summary dictionary for the full run.
    """
    logger.info(
        "Starting orders landing run | dates=%s upload=%s scenario=%s",
        [item.isoformat() for item in dates],
        upload,
        incident_scenario,
    )

    partitions = []

    for run_dt in dates:
        generated = generate_and_write_orders(
            dt=run_dt,
            config=config,
            incident_scenario=incident_scenario,
        )

        uploaded = None

        if upload:
            # Import lazily so local --no-upload smoke tests do not require boto3 on the host.
            from pipelines.seeding.upload_to_s3 import upload_orders_partition

            uploaded = upload_orders_partition(
                dt=run_dt,
                config=config,
                local_path=generated["local_path"],
                endpoint_url=endpoint_url,
            )

        partitions.append(
            {
                "dt": run_dt.isoformat(),
                "rows": generated["rows"],
                "local_path": generated["local_path"],
                "s3_uri": uploaded["s3_uri"] if uploaded else None,
                "incident_scenario": incident_scenario,
            }
        )

    total_rows = sum(item["rows"] for item in partitions)

    summary = {
        "status": "ok",
        "dataset": config.dataset,
        "partition_count": len(partitions),
        "total_rows": total_rows,
        "upload_enabled": upload,
        "partitions": partitions,
    }

    logger.info("Orders landing run completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for daily and backfill seeding runs.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Generate orders Parquet and upload to SeaweedFS S3.")

    parser.add_argument("--dt", default=None, help="Single business date to process, in YYYY-MM-DD format.")
    parser.add_argument("--start", default=None, help="Inclusive start date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--end", default=None, help="Inclusive end date for backfill, in YYYY-MM-DD format.")
    parser.add_argument("--config", default=None, help="Optional path to orders.yml.")
    parser.add_argument("--no-upload", action="store_true", help="Generate local Parquet but skip S3 upload.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")
    parser.add_argument("--incident-scenario", default="baseline", help="Scenario label stored with generated rows.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and run the orders landing pipeline.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    try:
        dates = resolve_run_dates(dt=args.dt, start=args.start, end=args.end)

    except ValueError as exc:
        parser.error(str(exc))

    config  = load_orders_config(args.config)
    summary = run_orders_landing(
        dates=dates,
        config=config,
        upload=not args.no_upload,
        endpoint_url=args.endpoint_url,
        incident_scenario=args.incident_scenario,
    )

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
