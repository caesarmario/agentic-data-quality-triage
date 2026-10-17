"""Bounded source/raw/derived comparison used to choose recovery, not fault labels."""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from agent.recovery.contracts import canonical_hash
from agent.recovery.store import latest_source_snapshot
from pipelines.common.clickhouse import format_date_literal

MAX_ROWS = 100_000
ROOT = Path(__file__).resolve().parents[2]
BUSINESS_COLUMNS = (
    "dt", "order_id", "order_date", "order_ts", "ingestion_ts", "customer_id", "country",
    "channel", "status", "currency", "gross_amount_local", "fx_rate_to_usd", "gross_amount_usd",
    "discount_usd", "refund_amount_usd", "recognized_revenue_usd", "source_system", "is_test",
    "business_date_version",
)
MONEY_COLUMNS = {"gross_amount_local", "gross_amount_usd", "discount_usd", "refund_amount_usd",
                 "recognized_revenue_usd", "net_amount_usd", "aov_usd"}


def model_fingerprint() -> str:
    """Bind execution to the actual transform/publication code reviewed in the plan."""
    paths = [ROOT / "warehouse/dbt/dbt_project.yml", ROOT / "warehouse/dbt/profiles.yml"]
    paths += sorted((ROOT / "warehouse/dbt/models").rglob("*.sql"))
    paths += sorted((ROOT / "warehouse/dbt/models").rglob("*.yml"))
    paths += sorted((ROOT / "warehouse/dbt/macros").rglob("*.sql"))
    paths += [ROOT / "pipelines/loading/load_clickhouse.py", ROOT / "pipelines/dbt/run_dbt.py",
              ROOT / "pipelines/common/publication.py", ROOT / "agent/recovery/runtime.py",
              ROOT / "agent/context/live.py", ROOT / "configs/metadata/orders.yml",
              ROOT / "pipelines/seeding/snapshots.py", ROOT / "pipelines/dq/segments.py"]
    return canonical_hash({path.relative_to(ROOT).as_posix(): path.read_bytes().hex() for path in paths})


def _cell(column: str, value: Any) -> str:
    import pandas as pd
    if pd.isna(value):
        return "<null>"
    if column in MONEY_COLUMNS or column == "fx_rate_to_usd":
        scale = Decimal("0.000001") if column == "fx_rate_to_usd" else Decimal("0.01")
        return str(Decimal(str(value)).quantize(scale, rounding=ROUND_HALF_EVEN))
    if column in {"order_ts", "ingestion_ts"}:
        return pd.to_datetime(value, utc=True).floor("ms").isoformat()
    if column in {"dt", "order_date"}:
        return pd.to_datetime(value).date().isoformat()
    if column in {"is_test", "business_date_version"}:
        return str(int(value))
    return str(value)


def business_fingerprint(frame: Any) -> str:
    """Order-independent multiset hash; duplicate rows are not silently discarded."""
    if len(frame) > MAX_ROWS or set(BUSINESS_COLUMNS) - set(frame.columns):
        raise ValueError("Business evidence is incomplete or exceeds the bounded limit.")
    rows = [tuple(_cell(column, row[column]) for column in BUSINESS_COLUMNS) for row in frame.to_dict("records")]
    return canonical_hash(sorted(Counter(rows).items()))


def require_source_contract(frame: Any, dt: date) -> Any:
    """A structurally valid file is not enough: validate the active business domain."""
    from pipelines.seeding.snapshots import validate_recovery_source
    from pipelines.dq.config import load_orders_dq_contract
    from pipelines.dq.segments import SegmentComparison, MAX_COVERAGE_SEGMENTS
    if set(BUSINESS_COLUMNS) - set(frame.columns) or len(frame) > MAX_ROWS:
        raise ValueError("Source contract evidence is incomplete or exceeds the row bound.")
    normalized = frame.loc[:, list(BUSINESS_COLUMNS)].copy()
    if not normalized["is_test"].isin([0, 1, False, True]).all():
        raise ValueError("Source has invalid test flags.")
    normalized["is_test"] = normalized["is_test"].astype(bool)
    for name in MONEY_COLUMNS | {"fx_rate_to_usd"}:
        if name in normalized:
            normalized[name] = normalized[name].map(float)
    clean = validate_recovery_source(normalized, dt)
    contract = load_orders_dq_contract()
    expected = contract.expected_segments
    observed = frozenset((str(c), str(ch)) for c, ch in clean[["country", "channel"]].drop_duplicates().itertuples(index=False, name=None))
    if len(observed) > MAX_COVERAGE_SEGMENTS:
        raise ValueError("Source segment evidence exceeds the bounded limit.")
    comparison = SegmentComparison(frozenset((c, ch) for c in expected.countries for ch in expected.channels), observed)
    if not comparison.exact_match:
        raise ValueError("Source does not satisfy the approved country/channel contract.")
    return clean


def _read_frame(client: Any, table: str, dt: date):
    import pandas as pd
    if table not in {"dq.raw_orders", "dq.stg_orders"}:
        raise ValueError("Recovery table is not allowlisted.")
    result = client.query(f"SELECT {','.join(BUSINESS_COLUMNS)} FROM {table} "
                          f"WHERE dt={format_date_literal(dt)} LIMIT {MAX_ROWS + 1}")
    frame = pd.DataFrame(result.result_rows, columns=BUSINESS_COLUMNS)
    if len(frame) > MAX_ROWS:
        raise ValueError("Recovery evidence exceeds the bounded row limit.")
    return frame


def _mart_matches(client: Any, dt: date) -> bool:
    """Compare key business aggregates, not just a matching number of segments."""
    fields = ("country", "channel", "row_count", "distinct_order_count", "recognized_revenue_usd",
              "duplicate_order_count", "late_arriving_count")
    where = f"dt={format_date_literal(dt)}"
    expected = client.query("SELECT country,channel,count(),uniqExact(order_id),"
        "CAST(sum(recognized_revenue_usd) AS Decimal(18,2)),count()-uniqExact(order_id),"
        "countIf(dateDiff('day',order_ts,ingestion_ts)>0) FROM dq.raw_orders "
        f"WHERE {where} AND is_test=0 GROUP BY country,channel ORDER BY country,channel LIMIT 1001").result_rows
    actual = client.query(f"SELECT {','.join(fields)} FROM dq.fct_orders_daily WHERE {where} "
                           "ORDER BY country,channel LIMIT 1001").result_rows
    if len(expected) > 1000 or len(actual) > 1000:
        raise ValueError("Derived segment evidence exceeds the bounded limit.")
    return [[_cell(col, val) for col, val in zip(fields, row)] for row in expected] == [
        [_cell(col, val) for col, val in zip(fields, row)] for row in actual]


def partition_fingerprint(client: Any, dt: date) -> str:
    """Conservatively invalidate approval after any scoped part publication or merge."""
    rows = client.query("SELECT table,name,rows,data_version,modification_time FROM system.parts "
        "WHERE database='dq' AND table IN ('raw_orders','stg_orders','fct_orders_daily') AND active "
        f"AND partition='{dt.isoformat()}' ORDER BY table,name LIMIT 10001").result_rows
    if len(rows) > 10000:
        raise ValueError("Partition preconditions exceeded the bounded part limit.")
    return canonical_hash(rows)


def collect_facts(client: Any, incident: dict, *, manifest: dict | None = None) -> dict:
    """Read pinned source and current platform data; never consult scenario ground truth."""
    from pipelines.seeding.snapshots import read_pinned_snapshot, validate_recovery_source
    dt = date.fromisoformat(str(incident["snapshot"]["dt"]))
    manifest = manifest or latest_source_snapshot(dt.isoformat())
    facts = {"source_valid": False, "raw_valid": False, "raw_matches": False,
             "derived_matches": False, "source_manifest": manifest, "blockers": []}
    if manifest is None:
        facts["blockers"].append("Source unavailable: this date has no immutable upstream snapshot.")
        return facts
    source = read_pinned_snapshot(manifest)
    try:
        require_source_contract(source, dt)
    except ValueError:
        facts["blockers"].append("Upstream source is invalid; publish an explicitly corrected source version first.")
        return facts
    facts["source_valid"] = True
    raw = _read_frame(client, "dq.raw_orders", dt)
    facts["source_fingerprint"] = business_fingerprint(source)
    facts["input_fingerprint"] = business_fingerprint(raw)
    facts["raw_matches"] = facts["source_fingerprint"] == facts["input_fingerprint"]
    try:
        require_source_contract(raw, dt)
        facts["raw_valid"] = True
    except ValueError:
        pass
    staged = _read_frame(client, "dq.stg_orders", dt)
    mart_count = int(client.query("SELECT count() FROM dq.fct_orders_daily WHERE dt=" + format_date_literal(dt)).result_rows[0][0])
    facts["derived_present"] = bool(len(staged) or mart_count)
    facts["derived_matches"] = business_fingerprint(raw) == business_fingerprint(staged) and _mart_matches(client, dt)
    facts["row_counts"] = {"upstream": len(source), "raw": len(raw), "staging": len(staged), "mart": mart_count}
    facts["derived_fingerprint"] = business_fingerprint(staged)
    facts["partition_fingerprint"] = partition_fingerprint(client, dt)
    return facts
