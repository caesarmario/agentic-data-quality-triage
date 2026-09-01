####
## Shared Alert Records for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Define the shared ClickHouse alert row contract used by deterministic producers."""

# --- Importing Libraries
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from pipelines.common.logging import logger


# --- Defining Constants
ALERTS_TABLE = "dq.alerts"

ALERT_COLUMNS = (
    "alert_key",
    "alert_display_id",
    "status",
    "alert_type",
    "severity",
    "table_name",
    "metric",
    "dt",
    "dimension",
    "observed_value",
    "expected_value",
    "threshold_value",
    "source_check_run_id",
    "details_json",
)


# --- Defining Data Models
@dataclass(frozen=True)
class AlertCandidate:
    """
    Represent one deterministic alert ready for ClickHouse insertion.

    Attributes:
        alert_key: Stable system identity used for idempotency and joins.
        alert_display_id: Short human-facing identifier used by operators.
        status: Alert lifecycle state, usually open at creation.
        alert_type: Alert category such as dq_failure or schema_drift.
        severity: Operational severity.
        table_name: Fully qualified affected table.
        metric: Deterministic check or signal name.
        dt: Optional business or observation date.
        dimension: Optional affected column, segment, or other dimension.
        observed_value: Optional observed numeric value.
        expected_value: Optional expected numeric value.
        threshold_value: Optional threshold used by the producer.
        source_check_run_id: Optional source DQ result UUID.
        details: Structured producer-specific evidence.
    """

    alert_key: str
    alert_display_id: str
    status: str
    alert_type: str
    severity: str
    table_name: str
    metric: str
    dt: date | None
    dimension: str
    observed_value: float | None
    expected_value: float | None
    threshold_value: float | None
    source_check_run_id: UUID | None
    details: dict[str, Any]

    def as_insert_row(self) -> tuple[Any, ...]:
        """
        Convert this candidate into the shared ClickHouse column order.

        Returns:
            Tuple aligned with ALERT_COLUMNS.
        """
        return (
            self.alert_key,
            self.alert_display_id,
            self.status,
            self.alert_type,
            self.severity,
            self.table_name,
            self.metric,
            self.dt,
            self.dimension,
            self.observed_value,
            self.expected_value,
            self.threshold_value,
            self.source_check_run_id,
            json.dumps(self.details, default=str, ensure_ascii=True, sort_keys=True),
        )


# --- Defining Persistence Functions
def insert_alert_rows(client: Any, candidates: list[AlertCandidate]) -> int:
    """
    Insert pre-deduplicated alert candidates with an explicit column contract.

    Args:
        client: clickhouse-connect client instance.
        candidates: Alert candidates already approved by producer-specific idempotency logic.

    Returns:
        Number of inserted alert rows.
    """
    if not candidates:
        logger.info("No alert rows selected for insertion")

        return 0

    rows = [candidate.as_insert_row() for candidate in candidates]

    logger.info("Inserting shared alert rows | table=%s rows=%d", ALERTS_TABLE, len(rows))
    client.insert(
        table=ALERTS_TABLE,
        data=rows,
        column_names=ALERT_COLUMNS,
    )
    logger.info("Shared alert rows inserted | table=%s rows=%d", ALERTS_TABLE, len(rows))

    return len(rows)
