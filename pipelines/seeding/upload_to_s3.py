####
## S3 Uploader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.helpers import parse_date


# --- Defining Constants
DEFAULT_S3_ENDPOINT_URL = "http://localhost:8333"


# --- Defining Functions
def resolve_s3_endpoint(endpoint_url: str | None = None) -> str:
    """
    Resolve the S3 endpoint URL for local or Docker execution.

    Args:
        endpoint_url: Optional explicit endpoint URL from CLI/caller.

    Returns:
        S3-compatible endpoint URL.
    """
    resolved = (
        endpoint_url
        or os.getenv("S3_ENDPOINT_URL")
        or os.getenv("S3_ENDPOINT_URL_INTERNAL")
        or os.getenv("S3_ENDPOINT_URL_EXTERNAL")
        or DEFAULT_S3_ENDPOINT_URL
    )

    logger.info("Resolved S3 endpoint | endpoint_url=%s", resolved)

    return resolved


def build_s3_client(endpoint_url: str | None = None):
    """
    Build a boto3 client for SeaweedFS S3-compatible storage.

    Args:
        endpoint_url: Optional explicit endpoint URL. Defaults to S3_* environment variables.

    Returns:
        boto3 S3 client configured for path-style local S3 access.
    """
    resolved_endpoint = resolve_s3_endpoint(endpoint_url)
    region_name       = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    logger.info("Building S3 client | endpoint_url=%s region=%s", resolved_endpoint, region_name)

    # Import lazily so local no-upload/debug commands do not require boto3 on the host.
    import boto3
    from botocore.config import Config

    # Path-style addressing is safer for local S3 gateways than virtual-host bucket names.
    return boto3.client(
        "s3",
        endpoint_url=resolved_endpoint,
        region_name=region_name,
        config=Config(s3={"addressing_style": "path"}),
    )


def upload_file_to_s3(
    local_path: str | Path,
    bucket: str,
    key: str,
    endpoint_url: str | None = None,
) -> str:
    """
    Upload a local file to an S3-compatible bucket.

    Args:
        local_path: Local file path to upload.
        bucket: Target S3 bucket name.
        key: Target S3 object key.
        endpoint_url: Optional explicit S3 endpoint URL.

    Returns:
        S3 URI of the uploaded object.

    Raises:
        FileNotFoundError: If local_path does not exist.
        botocore.exceptions.BotoCoreError: If boto3 cannot complete the upload.
    """
    source_path = Path(local_path)

    if not source_path.exists():
        logger.error("Upload source file does not exist | path=%s", source_path)
        raise FileNotFoundError(f"Upload source file does not exist: {source_path}")

    client = build_s3_client(endpoint_url)

    logger.info(
        "Uploading file to S3 | source=%s bucket=%s key=%s bytes=%d",
        source_path,
        bucket,
        key,
        source_path.stat().st_size,
    )

    # Parquet files are binary artifacts; metadata is intentionally minimal for portability.
    client.upload_file(str(source_path), bucket, key)

    s3_uri = f"s3://{bucket}/{key}"
    logger.info("File uploaded to S3 | uri=%s", s3_uri)

    return s3_uri


def upload_orders_partition(
    dt: date,
    config: OrdersSeedConfig,
    local_path: str | Path | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    """
    Upload one generated orders partition to the configured landing bucket.

    Args:
        dt: Business date represented by the partition.
        config: Validated orders seeding config.
        local_path: Optional explicit local Parquet path.
        endpoint_url: Optional explicit S3 endpoint URL.

    Returns:
        Summary dictionary with local path, bucket, key, and S3 URI.
    """
    source_path = Path(local_path) if local_path else config.output.local_path(dt)
    object_key  = config.output.object_key(dt)
    s3_uri      = upload_file_to_s3(
        local_path=source_path,
        bucket=config.output.bucket,
        key=object_key,
        endpoint_url=endpoint_url,
    )

    summary = {
        "dt": dt.isoformat(),
        "local_path": str(source_path),
        "bucket": config.output.bucket,
        "key": object_key,
        "s3_uri": s3_uri,
    }

    logger.info("Orders partition uploaded | summary=%s", summary)

    return summary


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for S3 upload execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Upload one generated orders Parquet partition to SeaweedFS S3.")

    parser.add_argument("--dt", required=True, help="Business date to upload, in YYYY-MM-DD format.")
    parser.add_argument("--config", default=None, help="Optional path to orders.yml.")
    parser.add_argument("--file", default=None, help="Optional explicit local Parquet file path.")
    parser.add_argument("--endpoint-url", default=None, help="Optional S3 endpoint URL override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and upload one orders partition.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    config  = load_orders_config(args.config)
    run_dt  = parse_date(args.dt)
    summary = upload_orders_partition(
        dt=run_dt,
        config=config,
        local_path=args.file,
        endpoint_url=args.endpoint_url,
    )

    print(json.dumps(summary, indent=2, default=str))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
