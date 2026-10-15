"""Explicit fixture setup, separate from the read-only Resolve workflow."""
from __future__ import annotations

import argparse
import json

from pipelines.common.clickhouse import build_clickhouse_client

TABLE = "dq_resolution_acceptance.schema_fixture"
MARKER = "isolated_incident_resolution_acceptance_fixture"


def prepare(mode: str) -> dict:
    if mode not in {"warn", "clean"}:
        raise ValueError("Only warn and clean fixture modes are supported.")
    client = build_clickhouse_client()
    client.command("CREATE DATABASE IF NOT EXISTS dq_resolution_acceptance")
    rows = client.query("SELECT comment FROM system.tables WHERE database='dq_resolution_acceptance' AND name='schema_fixture'").result_rows
    if rows and rows[0][0] != MARKER:
        raise RuntimeError("Fixture ownership marker mismatch; no table changes performed.")
    if not rows:
        client.command(f"CREATE TABLE {TABLE} (dt Date,id UInt64) ENGINE=MergeTree ORDER BY (dt,id) COMMENT '{MARKER}'")
    if mode == "warn":
        client.command(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS acceptance_extra Nullable(String)")
    else:
        client.command(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS acceptance_extra")
    return {"table": TABLE, "mode": mode, "purpose": "fixture preparation only; not Resolve", "external_llm_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("warn", "clean"))
    print(json.dumps(prepare(parser.parse_args().mode)))
