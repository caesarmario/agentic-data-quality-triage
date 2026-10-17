"""Register an explicitly supplied corrected source; never generate replacement rows."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from datetime import date


def register(*, dt: date, key: str, expected_sha256: str, correlation_id: str) -> dict:
    """Verify a fixed-bucket correction file before publishing its immutable version."""
    import pandas as pd
    from pipelines.seeding.snapshots import _read_object, _client, MAX_SNAPSHOT_BYTES, publish_source_snapshot
    from agent.recovery.evidence import require_source_contract
    if not re.fullmatch(r"orders/dt=" + re.escape(dt.isoformat()) + r"/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.parquet", key) or ".." in key:
        raise ValueError("Corrected input must be in the allowlisted orders/date prefix.")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("An explicit SHA-256 is required.")
    payload = _read_object(_client(), "dq-landing", key, MAX_SNAPSHOT_BYTES)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Corrected input checksum mismatch.")
    frame = pd.read_parquet(io.BytesIO(payload))
    frame = require_source_contract(frame, dt)
    manifest = publish_source_snapshot(frame, dt=dt, correlation_id=correlation_id)
    return manifest.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dt", required=True, type=date.fromisoformat)
    parser.add_argument("--key", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(register(**vars(args)), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_code": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
