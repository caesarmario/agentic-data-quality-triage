####
## S3 Uploader for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from pipelines.common.logging import logger
from pipelines.seeding.config import OrdersSeedConfig, load_orders_config
from pipelines.seeding.helpers import parse_date
from pipelines.seeding.snapshots import (
    MAX_MANIFEST_BYTES,
    SNAPSHOT_BUCKET,
    SNAPSHOT_PREFIX,
    SnapshotValidationError,
    capture_receipt_path,
    publish_landing_snapshot,
    publish_snapshot,
    read_local_snapshot,
)


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

    if key == SNAPSHOT_PREFIX or key.startswith(SNAPSHOT_PREFIX + "/"):
        raise SnapshotValidationError("Use the immutable snapshot publisher for snapshot keys")

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
    *,
    s3_client: Any = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """
    Upload one generated orders partition to the configured landing bucket.

    Args:
        dt: Business date represented by the partition.
        config: Validated orders seeding config.
        local_path: Optional explicit local Parquet path.
        endpoint_url: Optional explicit S3 endpoint URL.
        s3_client: Optional injected S3-compatible client.
        correlation_id: Optional opaque pipeline correlation override.

    Returns:
        Summary dictionary with local path, bucket, key, and S3 URI.
    """
    source_path = Path(local_path) if local_path else config.output.local_path(dt)
    object_key  = config.output.object_key(dt)
    if config.output.bucket != SNAPSHOT_BUCKET or config.output.prefix != "orders":
        raise SnapshotValidationError("Orders snapshots require the allowlisted landing scope")
    if (re.fullmatch(rf"orders/dt={re.escape(dt.isoformat())}/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.parquet", object_key) is None
            or ".." in object_key):
        raise SnapshotValidationError("Legacy landing key outside allowlisted partition")
    landing = read_local_snapshot(source_path)
    receipt_path = capture_receipt_path(source_path)
    manifests = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(read_local_snapshot(receipt_path, MAX_MANIFEST_BYTES))
            if (set(receipt) != {"dt", "source_sha256", "landing_sha256", "correlation_id"}
                    or receipt["dt"] != dt.isoformat()
                    or receipt["landing_sha256"] != hashlib.sha256(landing).hexdigest()
                    or not isinstance(receipt["source_sha256"], str)
                    or len(receipt["source_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in receipt["source_sha256"])):
                raise ValueError("Invalid receipt")
        except (ValueError, TypeError, KeyError):
            raise SnapshotValidationError("Snapshot capture receipt mismatch") from None
        captured_source = read_local_snapshot(source_path.with_name(f"source-{receipt['source_sha256']}.parquet"))
        if hashlib.sha256(captured_source).hexdigest() != receipt["source_sha256"]:
            raise SnapshotValidationError("Captured source checksum mismatch")
        run_id = correlation_id or receipt["correlation_id"]
    else:
        # Existing standalone developer uploads remain possible, but never pretend
        # an old faulted file is an upstream source. Configured runtime fails closed.
        if os.getenv("DQ_CONTROL_DATABASE_URL", "").strip():
            raise SnapshotValidationError("Runtime upload requires a pre-fault source capture")
        captured_source = None
        run_id = correlation_id or f"orders-{dt.isoformat()}-v1"

    client = s3_client if s3_client is not None else build_s3_client(endpoint_url)
    if captured_source is not None:
        upstream = publish_snapshot(captured_source, dt=dt, kind="upstream", correlation_id=run_id, s3_client=client)
        manifests["upstream"] = upstream.model_dump(mode="json")
    faulted = publish_landing_snapshot(landing, dt=dt, correlation_id=run_id, s3_client=client)
    manifests["landing"] = faulted.model_dump(mode="json")
    # Publish snapshots/registry first. The legacy key remains mutable for the
    # current daily loader, but receives precisely the faulted bytes we pinned.
    client.put_object(Bucket=config.output.bucket, Key=object_key, Body=landing,
                      ContentType="application/vnd.apache.parquet")
    s3_uri = f"s3://{config.output.bucket}/{object_key}"

    summary = {
        "dt": dt.isoformat(),
        "local_path": str(source_path),
        "bucket": config.output.bucket,
        "key": object_key,
        "s3_uri": s3_uri,
        "source_manifests": manifests,
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
