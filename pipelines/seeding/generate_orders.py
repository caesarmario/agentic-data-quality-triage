####
## Orders Parquet Generator for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import random
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.helpers import (
    build_order_count,
    build_order_event,
    parse_date,
    stable_day_index,
)
from pipelines.seeding.incidents import apply_incidents, resolve_incident_names


# --- Defining Constants
ORDER_COLUMNS = [
    "dt",
    "order_id",
    "order_date",
    "order_ts",
    "ingestion_ts",
    "customer_id",
    "country",
    "channel",
    "status",
    "currency",
    "gross_amount_local",
    "fx_rate_to_usd",
    "gross_amount_usd",
    "discount_usd",
    "refund_amount_usd",
    "recognized_revenue_usd",
    "source_system",
    "is_test",
    "business_date_version",
    "incident_scenario",
]


# --- Defining Functions
def generate_orders_for_date(
    dt: date,
    config: OrdersSeedConfig,
    incident_scenario: str = "baseline",
) -> pd.DataFrame:
    """
    Generate one deterministic raw-orders partition as a DataFrame.

    Args:
        dt: Business date to generate.
        config: Validated orders seeding config.
        incident_scenario: Baseline or comma-separated incident scenario label stored
            with each row for later evaluation.

    Returns:
        DataFrame with event-level order rows following the raw_orders contract.
    """
    total_days = config.generation.default_total_days_for_trend
    day_index  = stable_day_index(dt, total_days)
    seed_value = config.seed_for_date(dt, namespace=f"orders.generate.{incident_scenario}")

    logger.info(
        "Generating orders partition | dt=%s seed=%s scenario=%s day_index=%d total_days=%d",
        dt,
        seed_value,
        incident_scenario,
        day_index,
        total_days,
    )

    rng      = random.Random(seed_value)
    rows     = []
    sequence = 1

    for country in config.country_codes:
        for channel in config.channel_names:
            order_count = build_order_count(
                rng=rng,
                order_date=dt,
                country=country,
                channel=channel,
                day_index=day_index,
                total_days=total_days,
                config=config,
            )

            for _ in range(order_count):
                rows.append(
                    build_order_event(
                        rng=rng,
                        order_date=dt,
                        country=country,
                        channel=channel,
                        sequence=sequence,
                        day_index=day_index,
                        total_days=total_days,
                        config=config,
                        incident_scenario=incident_scenario,
                    )
                )
                sequence += 1

    incident_names     = resolve_incident_names(incident_scenario)
    incident_results   = []
    incident_row_count = len(rows)

    if incident_names:
        incident_seed = config.seed_for_date(dt, namespace=f"orders.incidents.{incident_scenario}")
        incident_rng  = random.Random(incident_seed)

        logger.info(
            "Injecting order incidents | dt=%s scenario=%s seed=%s incidents=%s input_rows=%d",
            dt,
            incident_scenario,
            incident_seed,
            incident_names,
            incident_row_count,
        )

        # Incident handlers mutate rows in place so the landing Parquet contains real anomaly evidence.
        incident_results = apply_incidents(
            rows=rows,
            incident_date=dt,
            incidents=incident_names,
            rng=incident_rng,
        )

    frame = pd.DataFrame.from_records(rows, columns=ORDER_COLUMNS)
    frame.attrs["incident_results"] = incident_results

    logger.info(
        "Orders partition generated | dt=%s rows=%d countries=%d channels=%d incidents=%s",
        dt,
        len(frame),
        len(config.country_codes),
        len(config.channel_names),
        incident_results,
    )

    return frame


def write_orders_parquet(
    frame: pd.DataFrame,
    dt: date,
    config: OrdersSeedConfig,
    output_path: str | Path | None = None,
) -> Path:
    """
    Write generated orders to a local partitioned Parquet file.

    Args:
        frame: Generated orders DataFrame.
        dt: Business date represented by the partition.
        config: Validated orders seeding config.
        output_path: Optional explicit Parquet file path. Defaults to config output path.

    Returns:
        Path to the written Parquet file.
    """
    parquet_path = Path(output_path) if output_path else config.output.local_path(dt)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    compression = None if config.output.compression == "none" else config.output.compression

    logger.info(
        "Writing orders Parquet | dt=%s path=%s rows=%d compression=%s",
        dt,
        parquet_path,
        len(frame),
        compression or "none",
    )

    # Index is not part of the landing contract; keep the file schema explicit.
    frame.to_parquet(parquet_path, index=False, engine="pyarrow", compression=compression)

    logger.info("Orders Parquet written | dt=%s path=%s bytes=%d", dt, parquet_path, parquet_path.stat().st_size)

    return parquet_path


def generate_and_write_orders(
    dt: date,
    config: OrdersSeedConfig,
    output_path: str | Path | None = None,
    incident_scenario: str = "baseline",
) -> dict[str, Any]:
    """
    Generate one orders partition and persist it as Parquet.

    Args:
        dt: Business date to generate.
        config: Validated orders seeding config.
        output_path: Optional explicit local Parquet file path.
        incident_scenario: Baseline or incident scenario label stored with each row
            for later evaluation.

    Returns:
        Summary dictionary with partition metadata and local file path.
    """
    frame        = generate_orders_for_date(dt=dt, config=config, incident_scenario=incident_scenario)
    parquet_path = write_orders_parquet(frame=frame, dt=dt, config=config, output_path=output_path)

    summary = {
        "dt": dt.isoformat(),
        "rows": int(len(frame)),
        "local_path": str(parquet_path),
        "incident_scenario": incident_scenario,
        "incident_results": frame.attrs.get("incident_results", []),
        "s3_bucket": config.output.bucket,
        "s3_key": config.output.object_key(dt),
    }

    logger.info("Generate-and-write completed | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for local/Airflow execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Generate one config-driven orders partition as Parquet.")

    parser.add_argument("--dt", required=True, help="Business date to generate, in YYYY-MM-DD format.")
    parser.add_argument("--config", default=None, help="Optional path to orders.yml.")
    parser.add_argument("--output", default=None, help="Optional explicit local Parquet output path.")
    parser.add_argument("--incident-scenario", default="baseline", help="Scenario label stored with generated rows.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and generate one local Parquet partition.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    config  = load_orders_config(args.config)
    run_dt  = parse_date(args.dt)
    summary = generate_and_write_orders(
        dt=run_dt,
        config=config,
        output_path=args.output,
        incident_scenario=args.incident_scenario,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
