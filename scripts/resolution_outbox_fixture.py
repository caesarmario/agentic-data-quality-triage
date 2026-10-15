"""Acceptance-only fault adapter; not part of the runtime agent tool registry."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agent.tools.incident_resolution_store import control_transaction, decoded
from scripts.prepare_resolution_schema_fixture import TABLE, MARKER


ACTOR = "acceptance:delegated_operator"
REASON = "Delegated fixture closure: the fresh Airflow schema check passed without findings. Closure itself performed no schema repair."


class InjectedFixtureDeliveryFault(RuntimeError):
    """A deliberate application fault before network delivery, not a Discord outage."""


def require_owned_fixture(client) -> None:
    """Refuse other tables and fixtures without the established ownership marker."""
    rows = client.query("SELECT comment FROM system.tables WHERE database='dq_resolution_acceptance' "
                        "AND name='schema_fixture'").result_rows
    if rows != [(MARKER,)]:
        raise ValueError("The owned schema fixture is required for this acceptance.")


def require_fixture_incident(incident: dict, alert_key: str) -> None:
    snapshot = incident.get("snapshot") or {}
    if (incident.get("alert_key") != alert_key or snapshot.get("table_name") != TABLE
            or snapshot.get("alert_type") != "schema_drift"
            or snapshot.get("metric") != "schema_contract_drift"):
        raise ValueError("This acceptance may only target its exact schema fixture incident.")


@dataclass
class FixtureDeliveryFault:
    """Fail once for one newly committed fixture closure, before any Discord call."""

    client: object
    alert_key: str
    request_id: str
    used: bool = False
    event_id: str = ""

    def __post_init__(self) -> None:
        self.request_id = str(UUID(self.request_id))
        require_owned_fixture(self.client)

    def __call__(self, incident: dict, event_id: str, event_type: str, payload: dict) -> None:
        if event_type != "incident_resolved" or self.used:
            return
        # Ignore every unrelated delivery; rejecting it here would itself inject a fault.
        try:
            require_fixture_incident(incident, self.alert_key)
            require_owned_fixture(self.client)
        except ValueError:
            return
        if (payload.get("resolution_request_id") != self.request_id
                or payload.get("resolved_by") != ACTOR or payload.get("resolution_reason") != REASON):
            return
        self.event_id = str(UUID(event_id))
        self.used = True
        raise InjectedFixtureDeliveryFault("Controlled application fault before Discord delivery.")


def read_recovery_state(alert_key: str, request_id: str) -> dict:
    """Read exact durable counts and sink flags; do not reset a completed event."""
    request_id = str(UUID(request_id))
    with control_transaction() as cursor:
        cursor.execute("SELECT status,revision FROM dq_control.incident_controls WHERE alert_key=%s", (alert_key,))
        row = cursor.fetchone()
        if not row:
            raise LookupError("Fixture incident is not available.")
        cursor.execute("SELECT count(*) FROM dq_control.incident_resolution_decisions WHERE alert_key=%s", (alert_key,))
        decisions = cursor.fetchone()[0]
        cursor.execute("SELECT event_id,projection_done,audit_done,delivery_done,error_code,payload "
                       "FROM dq_control.incident_outbox WHERE alert_key=%s AND event_type='incident_resolved' "
                       "AND payload->>'resolution_request_id'=%s", (alert_key, request_id))
        records = cursor.fetchall()
    if len(records) != 1:
        raise ValueError("Exactly one closure event for the request is required.")
    eid, projection, audit, delivery, error, payload = records[0]
    decision = decoded(payload)
    if decision.get("resolved_by") != ACTOR or decision.get("resolution_reason") != REASON:
        raise ValueError("The fixture decision does not match this acceptance.")
    return {"status": row[0], "revision": row[1], "decision_count": decisions,
            "event_id": str(eid), "projection_done": projection, "audit_done": audit,
            "delivery_done": delivery, "error_code": error}
