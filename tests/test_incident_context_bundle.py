"""Privacy, boundedness, and fail-closed contract tests for shared incident facts."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agent.context.bundle import (
    MAX_BUNDLE_BYTES,
    build_incident_context_bundle,
    render_incident_context,
)


NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize('source,raw,matched,derived,present,table,expected', [
    (False, False, False, False, False, 'dq.raw_orders', 'blocked'),
    (True, False, False, True, True, 'dq.fct_orders_daily', 'replay_and_rebuild_partition'),
    (True, False, False, False, False, 'dq.raw_orders', 'replay_raw_partition'),
    (True, True, True, False, True, 'dq.fct_orders_daily', 'rebuild_derived_partition'),
    (True, True, True, True, True, 'dq.fct_orders_daily', 'not_indicated'),
])
def test_context_uses_execution_eligibility_not_source_existence(source, raw, matched, derived, present, table, expected):
    from agent.context.live import runbook_from_current_facts
    runbook = runbook_from_current_facts(
        {'source_valid': source, 'raw_valid': raw, 'raw_matches': matched, 'derived_matches': derived,
         'derived_present': present, 'source_manifest': {'snapshot_id': 'a' * 64}, 'partition_fingerprint': 'b' * 64},
        {'table_name': table, 'alert_type': 'dq_failure'},
    )
    if expected in {'blocked', 'not_indicated'}:
        assert runbook['eligibility'] == expected
        assert runbook['actions'] == ['revalidate_quality']
    else:
        assert runbook['eligibility'] == 'eligible'
        assert runbook['runbook_id'] == expected
    if not source:
        assert runbook['prerequisites'][0]['satisfied'] is False


def current_consistent_state():
    from agent.context.bundle import TrustedSourceMetadata
    from agent.context.live import runbook_from_current_facts
    from agent.state import Alert, EvidenceItem, Hypothesis, TriageState
    alert = Alert(alert_id=UUID('12345678-1234-4234-8234-123456789012'), alert_key='orders|dq_failure|2026-10-14|dq.fct_orders_daily|segment_coverage__country_channel|country_channel',
                  table_name='dq.fct_orders_daily', metric='segment_coverage__country_channel', dt='2026-10-14',
                  severity='warning', observed_value=0.5, expected_value=1)
    pairs = [{'country': 'ID', 'channel': 'organic'}, {'country': 'SG', 'channel': 'paid'}]
    source = TrustedSourceMetadata(
        incident_id=str(alert.alert_id), revision='1', table_name=alert.table_name, partition_dt=alert.dt,
        metadata={'grain': 'country/channel/day', 'keys': ['dt', 'country', 'channel'], 'owner': 'orders_team', 'metric_definition': 'Exact segment membership'},
        contracts=[{'kind': k, 'fingerprint': 'a' * 64, 'version': '1'} for k in ['dq', 'schema', 'metadata']],
        expected_segments=pairs,
        snapshots=[{'layer': layer, 'partition_dt': alert.dt, 'observed_at': NOW, 'evidence_ref': layer + ':test',
                    'source_version': 'a' * 64, 'row_count': 1 if layer == 'landing' else 2,
                    'metric_value': 1 if layer == 'landing' else 2, 'segments': pairs[:1] if layer == 'landing' else pairs}
                   for layer in ['upstream', 'landing', 'raw', 'mart']],
        pipeline_lineage=[{'upstream': 'orders.landing', 'downstream': 'dq.raw_orders', 'pipeline_id': 'orders',
                           'version': 'a' * 64, 'observed_at': NOW, 'evidence_ref': 'pipeline:test'}],
        runbook=runbook_from_current_facts(
            {'source_valid': True, 'raw_valid': True, 'raw_matches': True, 'derived_matches': True,
             'source_manifest': {'snapshot_id': 'a' * 64}, 'partition_fingerprint': 'b' * 64},
            {'table_name': alert.table_name, 'alert_type': 'dq_failure'}),
    )
    return TriageState(alert=alert, incident_revision='1', trusted_source_metadata=source,
        evidence=[EvidenceItem(evidence_id='E-CURRENT', evidence_type='sql_result', tool_name='clickhouse_sql',
            description='Current aggregate', query="SELECT count() FROM dq.fct_orders_daily WHERE dt='2026-10-14' LIMIT 1",
            rows=[{'row_count': 2}], created_at=NOW)],
        hypotheses=[Hypothesis(title='Historical missing segment', description='The earlier landing lacked a required segment.',
            likelihood=.7, confidence=.7, root_cause_category='missing_segment', recommended_action='Replay the partition.')])


def test_current_consistency_does_not_authorize_repair_or_claim_whole_table_healthy():
    state = current_consistent_state()
    bundle = build_incident_context_bundle(state.alert, state.evidence, incident_id=str(state.alert.alert_id),
        revision='1', trusted_source_metadata=state.trusted_source_metadata, top_hypothesis=state.top_hypothesis, now=NOW)
    assert bundle.context_complete
    assert bundle.runbook.eligibility == 'not_indicated'
    assert 'Earlier detection' in bundle.likely_explanation
    assert 'Do not replay or rebuild' in bundle.recovery_recommendation
    assert 'read-only' in bundle.recovery_recommendation
    assert any('not proof that every quality rule passes' in s for s in bundle.confirmed_observations)
    assert bundle.executable_plan is None


def test_model_cannot_recommend_another_repair_after_verified_consistency(monkeypatch):
    from agent.context import bundle as context
    from agent.graph import build_ai_investigation_narrative, build_report_from_state
    from agent.llm.client import LlmResponse
    monkeypatch.setattr(context, 'datetime', type('Clock', (), {'now': staticmethod(lambda tz: NOW), 'fromisoformat': datetime.fromisoformat}))
    state = current_consistent_state()
    response = LlmResponse(agent_run_id=state.agent_run_id, provider='gemini', model='gemini-3.5-flash-lite', route_name='triage_reasoning', content='Recorded proposal',
        structured_output={'what_happened': 'A segment was missing at detection.',
            'evidence_reasoning': [{'evidence_reference': 'E1', 'finding': 'Current aggregate has two rows.'}],
            'likely_impact': 'Data is incomplete now.', 'uncertainty': 'The specific source cause is uncertain.',
            'recommended_next_step': 'Request approval to replay the partition.', 'approval_rationale': 'Model opinion is not policy.'})
    narrative = build_ai_investigation_narrative(state, response)
    assert narrative.source == 'external_model'
    assert narrative.provider == 'gemini'
    assert narrative.what_happened.startswith('System verification:')
    assert 'Historical AI explanation:' in narrative.what_happened
    assert 'Do not replay or rebuild' in narrative.recommended_next_step
    assert 'does not certify every quality rule' in narrative.likely_impact
    report = build_report_from_state(state, response)
    assert report.summary.startswith('Historical detection:')
    assert report.approval_gated_actions == []
    assert all('Do not replay or rebuild' in action or action.startswith('Review the stored evidence') for action in report.recommended_actions)


@pytest.mark.parametrize('eligibility,action', [('unknown', 'Request approval to replay the partition.'),
                                               ('eligible', 'Review replay_and_rebuild_partition before approval.'),
                                               ('eligible', 'Review a replay of raw input before seeking approval.'),
                                               ('unknown', 'Review a replay of raw input before seeking approval.')])
def test_model_mutation_advice_cannot_replace_deterministic_minimum_runbook(monkeypatch, eligibility, action):
    from agent.context import bundle as context
    from agent.graph import build_ai_investigation_narrative, build_report_from_state
    from agent.llm.client import LlmResponse
    monkeypatch.setattr(context, 'datetime', type('Clock', (), {'now': staticmethod(lambda tz: NOW), 'fromisoformat': datetime.fromisoformat}))
    state = current_consistent_state()
    runbook = state.trusted_source_metadata.runbook.model_copy(update={'eligibility': eligibility, 'runbook_id': 'rebuild_derived_partition'})
    state.trusted_source_metadata = state.trusted_source_metadata.model_copy(update={'runbook': runbook})
    response = LlmResponse(agent_run_id=state.agent_run_id, provider='gemini', model='gemini-3.5-flash-lite',
        route_name='triage_reasoning', content='Recorded proposal', structured_output={
            'what_happened': 'Earlier detection requires review.',
            'evidence_reasoning': [{'evidence_reference': 'E1', 'finding': 'Scoped aggregate retained.'}],
            'likely_impact': 'Scoped quality is under review.', 'uncertainty': 'Not all checks are complete.',
            'recommended_next_step': action, 'approval_rationale': 'Review before execution.'})
    narrative = build_ai_investigation_narrative(state, response)
    assert narrative.recommended_next_step == state.incident_context_bundle.recovery_recommendation
    fallback = build_ai_investigation_narrative(state, None)
    assert fallback.recommended_next_step == narrative.recommended_next_step
    report = build_report_from_state(state, response)
    assert report.recommended_actions[0] == narrative.recommended_next_step
    assert report.top_hypothesis.recommended_action == narrative.recommended_next_step
    if eligibility == 'unknown':
        assert narrative.recommended_next_step.startswith('No eligible repair is established')
    else:
        assert 'Review rebuild_derived_partition' in narrative.recommended_next_step


def test_serialized_transport_never_exceeds_32kb_with_maximum_evidence_references():
    evidence = [
        {"evidence_id": f"evidence-{index}-" + "x" * 145,
         "tool_name": "bounded_collector", "created_at": NOW.isoformat(),
         "summary": "x" * 5000, "rows": [{"private": "not projected"}]}
        for index in range(30)
    ]
    bundle = build_incident_context_bundle(
        {"alert_id": "incident-1", "table_name": "dq.raw_orders", "dt": "2026-10-14",
         "metric": "row_count", "details": "private alert details are excluded"},
        evidence, incident_id="incident-1", revision="4", now=NOW,
    )

    encoded = bundle.serialized_bytes()
    assert len(encoded) <= MAX_BUNDLE_BYTES == 32_000
    assert len(bundle.evidence) <= 24
    assert bundle.truncated is True
    assert b"private alert details" not in encoded
    assert b"not projected" not in encoded


def test_secrets_and_transport_details_are_not_projected_into_public_bundle():
    bundle = build_incident_context_bundle(
        {"alert_id": "incident-2", "table_name": "dq.raw_orders", "dt": "2026-10-14",
         "details": "password=do-not-leak"},
        [{"evidence_id": "safe-ref", "tool_name": "api_key=hidden-value",
          "summary": "Bearer do-not-leak", "query": "SELECT * FROM sensitive_table"}],
        incident_id="incident-2", revision="1", now=NOW,
    )

    rendered = bundle.serialized_bytes().decode("utf-8")
    for secret in ("do-not-leak", "hidden-value", "sensitive_table", "password="):
        assert secret not in rendered


def test_ground_truth_and_scenario_material_cannot_become_public_facts_or_hypotheses():
    bundle = build_incident_context_bundle(
        {"alert_id": "incident-3", "table_name": "dq.raw_orders", "dt": "2026-10-14",
         "scenario": "hidden_scenario", "ground_truth": {"root_cause": "oracle-value"}},
        [{"evidence_id": "safe-evidence", "tool_name": "collector",
          "scenario": "fault_manifest_value", "ground_truth": "hidden_oracle"}],
        incident_id="incident-3", revision="2",
        top_hypothesis={"title": "ground truth says source corruption", "confidence": 0.92}, now=NOW,
    )

    encoded = bundle.serialized_bytes().decode("utf-8")
    for marker in ("hidden_scenario", "oracle-value", "fault_manifest_value", "hidden_oracle"):
        assert marker not in encoded
    assert "ground truth says" not in bundle.likely_explanation
    assert bundle.context_complete is False


def test_missing_or_invalid_critical_facts_fail_closed_without_an_executable_plan():
    bundle = build_incident_context_bundle(
        {"alert_id": "incident-4", "table_name": "dq.raw_orders", "dt": "2026-10-14"},
        incident_id="incident-4", revision=None, now=NOW,
    )

    assert bundle.context_complete is False
    assert bundle.missing_evidence
    assert bundle.executable_plan is None
    assert bundle.approval_required is True
    assert "before proposing" in bundle.recovery_recommendation


def test_heuristic_score_is_explicitly_not_a_probability_in_contract_and_rendering():
    bundle = build_incident_context_bundle(
        {"alert_id": "incident-5", "table_name": "dq.raw_orders", "dt": "2026-10-14"},
        incident_id="incident-5", revision="1",
        top_hypothesis={"title": "Possible delayed landing", "confidence": 0.83}, now=NOW,
    )

    rendered = render_incident_context(bundle)
    assert bundle.heuristic_score == 0.83
    assert bundle.score_semantics == "heuristic_score_not_statistical_probability"
    assert "not statistical probability" in rendered
    assert "83%" not in rendered
