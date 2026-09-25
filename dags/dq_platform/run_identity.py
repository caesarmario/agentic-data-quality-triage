####
## Stable Child DagRun Identity Helpers
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import hashlib
from datetime import timezone
from typing import Any


# --- Resolving Immutable Parent Identity
def trigger_run_suffix(dag_run: Any) -> str:
    """Return the legacy UTC timestamp, or a stable hash for undated manual runs.

    Args:
        dag_run: Parent run exposing logical_date, dag_id, and run_id.

    Returns:
        Shell-safe child suffix unchanged across parent task retries.

    Raises:
        ValueError: When an undated run has no immutable parent identity.
    """
    logical_date = dag_run.logical_date
    if logical_date is not None:
        if logical_date.tzinfo is None:
            logical_date = logical_date.replace(tzinfo=timezone.utc)
        return logical_date.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")

    dag_id = getattr(dag_run, "dag_id", "")
    run_id = getattr(dag_run, "run_id", "")
    if not isinstance(dag_id, str) or not dag_id or not isinstance(run_id, str) or not run_id:
        raise ValueError("An undated trigger requires a stable parent DAG and run ID.")
    identity = f"{len(dag_id)}:{dag_id}{run_id}"
    return "manual_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def trigger_logical_date(dag_run: Any) -> str | None:
    """Preserve the parent's logical date, including native None for manual runs."""
    return dag_run.logical_date.isoformat() if dag_run.logical_date is not None else None
