"""Recovery policy, immutable transport, and source-fingerprint contracts."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest
from pydantic import ValidationError

from agent.recovery import contracts, evidence


def test_all_three_runbooks_are_selected_only_for_minimal_proven_conditions():
    assert contracts.choose_runbook(table="dq.raw_orders", source_valid=True, raw_matches=False,
                                    raw_valid=False, derived_matches=False) == "replay_raw_partition"
    assert contracts.choose_runbook(table="dq.stg_orders", source_valid=True, raw_matches=True,
                                    raw_valid=True, derived_matches=False) == "rebuild_derived_partition"
    assert contracts.choose_runbook(table="dq.fct_orders_daily", source_valid=True, raw_matches=False,
                                    raw_valid=False, derived_matches=False) == "replay_and_rebuild_partition"


def test_raw_incident_rebuilds_existing_dependents_instead_of_leaving_them_stale():
    assert contracts.choose_runbook(table="dq.raw_orders", source_valid=True, raw_matches=False,
        raw_valid=False, derived_matches=True, derived_present=True) == "replay_and_rebuild_partition"
    assert contracts.choose_runbook(table="dq.raw_orders", source_valid=True, raw_matches=False,
        raw_valid=False, derived_matches=False, derived_present=False) == "replay_raw_partition"


@pytest.mark.parametrize("facts", [
    {"table": "dq.raw_orders", "source_valid": False, "raw_matches": False, "raw_valid": False, "derived_matches": False},
    {"table": "dq.raw_orders", "source_valid": True, "raw_matches": True, "raw_valid": True, "derived_matches": False},
    {"table": "dq.stg_orders", "source_valid": True, "raw_matches": True, "raw_valid": False, "derived_matches": False},
    {"table": "dq.fct_orders_daily", "source_valid": True, "raw_matches": True, "raw_valid": True, "derived_matches": True},
    {"table": "dq.other", "source_valid": True, "raw_matches": False, "raw_valid": False, "derived_matches": False},
])
def test_runbook_refuses_unproven_or_out_of_scope_remediation(facts):
    assert contracts.choose_runbook(**facts) is None
    assert contracts.choose_runbook(**facts, kind="schema_drift") is None


@pytest.mark.parametrize("field,value", [
    ("source_manifest", {"bucket": "attacker"}), ("targets", ["dq.other"]),
    ("provider", "gemini"), ("generator", "generate_orders"), ("model_hash", "override"),
])
def test_proposal_rejects_client_scope_provider_and_generator_overrides(field, value):
    payload = {"reason": "Evidence reviewed for one date.", "expected_incident_revision": 3, field: value}
    with pytest.raises(ValidationError):
        contracts.RecoveryProposal.model_validate(payload)


@pytest.mark.parametrize("actor", ["system", "airflow", "agentic_triage", "datasentry_recovery"])
def test_execution_contract_rejects_service_as_human_operator(actor):
    with pytest.raises(ValidationError, match="operator identity"):
        contracts.RecoveryExecutionCreate(actor=actor, expected_plan_hash="a" * 64,
                                          expected_approval_version=4, confirm_scope=True)


def test_execution_confirmation_requires_literal_true_and_forbids_extra_fields():
    base = {"actor": "operator:mario", "expected_plan_hash": "a" * 64,
            "expected_approval_version": 4, "confirm_scope": True}
    assert contracts.RecoveryExecutionCreate.model_validate(base).confirm_scope is True
    for changed in ({**base, "confirm_scope": False}, {**base, "runbook": "replay_raw_partition"}):
        with pytest.raises(ValidationError):
            contracts.RecoveryExecutionCreate.model_validate(changed)


def test_canonical_plan_hash_is_order_stable_and_binds_new_execution_fields():
    contract = {"runbook": "rebuild_derived_partition", "dt": "2026-10-14",
                "targets": ["dq.stg_orders", "dq.fct_orders_daily"],
                "source_manifest": {"snapshot_id": "snap-1", "version": "v9"},
                "expected_approval_version": 12}
    reordered = {"expected_approval_version": 12, "source_manifest": {"version": "v9", "snapshot_id": "snap-1"},
                 "targets": ["dq.stg_orders", "dq.fct_orders_daily"], "dt": "2026-10-14",
                 "runbook": "rebuild_derived_partition"}
    assert contracts.canonical_hash(contract) == contracts.canonical_hash(reordered)
    assert contracts.canonical_hash({**contract, "expected_approval_version": 13}) != contracts.canonical_hash(contract)
    assert contracts.canonical_hash({**contract, "targets": ["dq.fct_orders_daily"]}) != contracts.canonical_hash(contract)


def _business_row(**overrides):
    row = {column: "value" for column in evidence.BUSINESS_COLUMNS}
    row.update({"dt": date(2026, 10, 14), "order_id": "order-1", "order_date": date(2026, 10, 14),
                "order_ts": datetime(2026, 10, 14, 10, tzinfo=timezone.utc),
                "ingestion_ts": datetime(2026, 10, 14, 10, 1, tzinfo=timezone.utc),
                "gross_amount_local": 10.004, "fx_rate_to_usd": 1.2345674,
                "gross_amount_usd": 10.0, "discount_usd": 0.0, "refund_amount_usd": 0.0,
                "recognized_revenue_usd": 10.0, "is_test": 0, "business_date_version": 1})
    row.update(overrides)
    return row


def test_business_fingerprint_is_order_independent_but_preserves_duplicate_multiplicity():
    first, second = _business_row(order_id="one"), _business_row(order_id="two")
    assert evidence.business_fingerprint(pd.DataFrame([first, second])) == evidence.business_fingerprint(pd.DataFrame([second, first]))
    assert evidence.business_fingerprint(pd.DataFrame([first, first])) != evidence.business_fingerprint(pd.DataFrame([first]))


def test_business_fingerprint_rounds_money_to_cents_and_fx_to_six_places():
    original = _business_row(gross_amount_local=10.004, fx_rate_to_usd=1.2345674)
    same_rounded = _business_row(gross_amount_local=10.003, fx_rate_to_usd=1.2345673)
    changed_cents = _business_row(gross_amount_local=10.006, fx_rate_to_usd=1.2345673)
    assert evidence.business_fingerprint(pd.DataFrame([original])) == evidence.business_fingerprint(pd.DataFrame([same_rounded]))
    assert evidence.business_fingerprint(pd.DataFrame([original])) != evidence.business_fingerprint(pd.DataFrame([changed_cents]))


def test_business_fingerprint_fails_closed_for_missing_business_columns():
    with pytest.raises(ValueError, match="incomplete"):
        evidence.business_fingerprint(pd.DataFrame([{"dt": date(2026, 10, 14)}]))


def test_source_must_match_full_configured_country_channel_domain(monkeypatch):
    class Expected:
        countries = ["CA", "GB"]
        channels = ["direct", "marketplace"]

    class Contract:
        expected_segments = Expected()

    from pipelines.dq import config
    from pipelines.seeding import snapshots

    monkeypatch.setattr(config, "load_orders_dq_contract", lambda: Contract())
    monkeypatch.setattr(snapshots, "validate_recovery_source", lambda frame, dt: frame)
    dt = date(2026, 10, 14)
    complete = pd.DataFrame([
        _business_row(country=country, channel=channel, order_id=f"{country}-{channel}")
        for country in Expected.countries for channel in Expected.channels
    ])
    assert len(evidence.require_source_contract(complete, dt)) == 4
    missing = complete.iloc[:-1]
    with pytest.raises(ValueError, match="country/channel contract"):
        evidence.require_source_contract(missing, dt)
    extra = pd.concat([complete, pd.DataFrame([_business_row(country="ZZ", channel="direct", order_id="ZZ-direct")])],
                      ignore_index=True)
    with pytest.raises(ValueError, match="country/channel contract"):
        evidence.require_source_contract(extra, dt)


def test_raw_uint8_decimal_frame_normalizes_to_matching_source_business_fingerprint(monkeypatch):
    import numpy as np

    class Expected:
        countries = ["CA", "GB"]
        channels = ["direct", "marketplace"]

    class Contract:
        expected_segments = Expected()

    from pipelines.dq import config
    from pipelines.seeding import snapshots

    monkeypatch.setattr(config, "load_orders_dq_contract", lambda: Contract())
    monkeypatch.setattr(snapshots, "validate_recovery_source", lambda frame, dt: frame)
    dt = date(2026, 10, 14)
    source = pd.DataFrame([
        _business_row(country=country, channel=channel, order_id=f"{country}-{channel}",
                      is_test=False, gross_amount_local=10.0, fx_rate_to_usd=1.234567,
                      gross_amount_usd=12.35, discount_usd=0.25, refund_amount_usd=0.0,
                      recognized_revenue_usd=12.10)
        for country in Expected.countries for channel in Expected.channels
    ])
    raw = source.copy()
    raw["is_test"] = pd.Series(np.zeros(len(raw), dtype=np.uint8), index=raw.index)
    numeric_columns = (evidence.MONEY_COLUMNS | {"fx_rate_to_usd"}) & set(evidence.BUSINESS_COLUMNS)
    for column in numeric_columns:
        raw[column] = raw[column].map(lambda value: Decimal(str(value)))

    normalized = evidence.require_source_contract(raw, dt)

    assert raw["is_test"].dtype == np.dtype("uint8")
    assert isinstance(raw.loc[0, "gross_amount_local"], Decimal)
    assert normalized["is_test"].dtype == bool
    assert all(pd.api.types.is_float_dtype(normalized[column]) for column in numeric_columns)
    assert evidence.business_fingerprint(normalized) == evidence.business_fingerprint(source)
