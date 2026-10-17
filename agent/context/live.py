"""Server-only adapters for public context. No caller-supplied facts or oracle data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.context.bundle import TrustedSourceMetadata
from agent.recovery.contracts import canonical_hash
from agent.recovery.store import latest_source_snapshot
from pipelines.common.clickhouse import build_clickhouse_client, format_date_literal


def runbook_from_current_facts(facts: dict, snapshot: dict) -> dict:
    """Use the same minimum-action policy as execution, not source existence alone."""
    from agent.recovery.contracts import choose_runbook
    selected = choose_runbook(
        table=snapshot["table_name"], kind=snapshot["alert_type"],
        source_valid=facts.get("source_valid", False), raw_matches=facts.get("raw_matches", False),
        raw_valid=facts.get("raw_valid", False), derived_matches=facts.get("derived_matches", False),
        derived_present=facts.get("derived_present", False),
    )
    unchanged = (snapshot["alert_type"] == "dq_failure" and facts.get("source_valid")
                 and facts.get("raw_valid") and facts.get("raw_matches") and facts.get("derived_matches"))
    eligibility = "eligible" if selected else "not_indicated" if unchanged else "blocked"
    actions = {
        "replay_raw_partition": ["reload_verified_partition", "revalidate_quality"],
        "rebuild_derived_partition": ["rebuild_derived_partition", "revalidate_quality"],
        "replay_and_rebuild_partition": ["restore_verified_source", "rebuild_derived_partition", "revalidate_quality"],
    }.get(selected, ["revalidate_quality"])
    manifest = facts.get("source_manifest") or {}
    blockers = list(facts.get("blockers", []))
    if eligibility == "blocked" and not blockers:
        blockers.append("No supported data-changing runbook is justified for this incident.")
    return {
        "runbook_id": selected or "scoped_read_only_revalidation", "eligibility": eligibility,
        "actions": actions,
        "prerequisites": [{"name": "Verified upstream business contract", "satisfied": facts.get("source_valid", False),
                           "evidence_ref": "snapshot:" + manifest["snapshot_id"] if manifest.get("snapshot_id") else None},
                          {"name": "Current warehouse scope inspected", "satisfied": bool(facts.get("partition_fingerprint")),
                           "evidence_ref": "warehouse:" + facts["partition_fingerprint"] if facts.get("partition_fingerprint") else None}],
        "required_permissions": ["read_platform_evidence"], "granted_permissions": ["read_platform_evidence"],
        "blockers": blockers,
    }


def load_trusted_context(alert: Any, *, client=None) -> TrustedSourceMetadata:
    """Read bounded non-sensitive aggregates and versioned repository contracts."""
    from agent.recovery.service import _incident
    from pipelines.dq.config import load_orders_dq_contract
    from pipelines.metadata.config import load_named_metadata_registry
    from pipelines.schema_drift.config import load_named_schema_contract
    from pipelines.seeding.snapshots import read_observed_snapshot
    client = client or build_clickhouse_client()
    ref = getattr(alert, "alert_display_id", "") or getattr(alert, "alert_key", "")
    incident = _incident(client, ref)
    snapshot = incident["snapshot"]
    from datetime import date
    dt = date.fromisoformat(str(snapshot["dt"]))
    metadata, _ = load_named_metadata_registry("orders")
    schema, _ = load_named_schema_contract("orders")
    contract = load_orders_dq_contract()
    asset = next((item for item in metadata.assets if item.qualified_name == snapshot["table_name"]), None)
    observed_at = datetime.now(timezone.utc)
    expected = [(country, channel) for country in contract.expected_segments.countries for channel in contract.expected_segments.channels]
    snapshots = []
    for kind in ("upstream", "landing"):
        manifest = latest_source_snapshot(dt.isoformat(), kind)
        if manifest:
            frame = read_observed_snapshot(manifest, kind=kind)
            pairs = sorted({(str(c), str(ch)) for c, ch in frame[["country", "channel"]].itertuples(index=False, name=None)})
            if len(pairs) > 128:
                raise ValueError("Source context segments exceed the bounded limit.")
            snapshots.append(dict(layer=kind, partition_dt=dt, observed_at=observed_at,
                evidence_ref="snapshot:" + manifest["snapshot_id"], source_version=manifest["sha256"],
                row_count=len(frame), metric_value=float(len(pairs)),
                segments=[{"country": c, "channel": ch} for c, ch in pairs]))
    for layer, table in (("raw", "dq.raw_orders"), ("mart", "dq.fct_orders_daily")):
        rows = client.query(f"SELECT country,channel,count() FROM {table} WHERE dt={format_date_literal(dt)} "
                            "GROUP BY country,channel ORDER BY country,channel LIMIT 129").result_rows
        if len(rows) > 128:
            raise ValueError("Warehouse context segments exceed the bounded limit.")
        public_pairs = [{"country": str(c), "channel": str(ch)} for c, ch, _ in rows]
        version = canonical_hash(rows)
        snapshots.append(dict(layer=layer, table_name=table, partition_dt=dt, observed_at=observed_at,
            evidence_ref=layer + ":" + version, source_version=version,
            row_count=sum(int(row[2]) for row in rows), metric_value=float(len(rows)), segments=public_pairs))
    model_version = canonical_hash({"schema": schema.model_dump(mode="json"), "metadata": metadata.model_dump(mode="json")})
    lineage = []
    runs = client.query("SELECT run_id,job_name FROM dq.pipeline_runs WHERE partition_dt=" + format_date_literal(dt) +
                        " ORDER BY started_at DESC LIMIT 8").result_rows
    if runs:
        rid = str(runs[0][0])
        for upstream, downstream in (("orders.landing", "dq.raw_orders"), ("dq.raw_orders", "dq.stg_orders"),
                                     ("dq.stg_orders", "dq.fct_orders_daily")):
            lineage.append(dict(upstream=upstream, downstream=downstream, pipeline_id="orders_pipeline",
                version=model_version, observed_at=observed_at, evidence_ref="pipeline:" + rid))
    keys = ["dt", "country", "channel"] if snapshot["table_name"] == "dq.fct_orders_daily" else ["order_id"]
    definitions = {"segment_coverage__country_channel": "Share of contracted country/channel pairs present; unknown pairs also block PASS.",
                   "row_count_positive": "The affected business-date partition must contain at least one row.",
                   "duplicate_rate__daily_total": "Duplicate order count divided by event row count; an empty denominator is not healthy."}
    from agent.recovery.evidence import collect_facts
    runbook = runbook_from_current_facts(collect_facts(client, incident), snapshot)
    return TrustedSourceMetadata(incident_id=str(getattr(alert, "alert_id", None) or incident["alert_ref"]),
        revision=str(incident["revision"]), table_name=snapshot["table_name"], partition_dt=dt,
        metadata={"grain": asset.grain if asset else None, "keys": keys,
                  "metric_definition": definitions.get(snapshot["metric"], "Contracted check: " + snapshot["metric"]),
                  "owner": asset.technical_owner if asset else None},
        contracts=[{"kind": kind, "fingerprint": canonical_hash(value.model_dump(mode="json")), "version": "1"}
                   for kind, value in (("dq", contract), ("schema", schema), ("metadata", metadata))],
        expected_segments=[{"country": c, "channel": ch} for c, ch in expected], snapshots=snapshots,
        pipeline_lineage=lineage,
        runbook=runbook)
