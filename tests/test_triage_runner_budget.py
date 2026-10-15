####
## Standalone Triage Budget Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import pytest

from agent.supervisor.budgets import active_supervisor_llm_budget
from scripts import run_triage_alerts


def test_triage_runner_defaults_to_bounded_full_triage_policy() -> None:
    """Keep standalone DAG 40 defaults aligned with the approved T3 ceilings."""
    args = run_triage_alerts.build_parser().parse_args(["--alert-key", "orders|dq_failure|test"])

    assert args.max_model_calls == 3
    assert args.token_budget == 16_384
    assert args.estimated_cost_budget_usd == pytest.approx(0.05)
    assert args.latency_budget_ms == 300_000


@pytest.mark.parametrize(
    ("argument", "value", "expected"),
    [
        ("--max-model-calls", "11", "max_model_calls"),
        ("--token-budget", "64001", "token_budget"),
        ("--estimated-cost-budget-usd", "0.151", "estimated_cost_budget_usd"),
        ("--latency-budget-ms", "999", "latency_budget_ms"),
        ("--max-evidence-iterations", "6", "max_evidence_iterations"),
    ],
)
def test_triage_runner_rejects_out_of_policy_budget_before_execution(
    argument: str,
    value: str,
    expected: str,
) -> None:
    """Reject unsafe run configuration before evidence collection or provider access."""
    args = run_triage_alerts.build_parser().parse_args(
        ["--alert-key", "orders|dq_failure|test", argument, value]
    )

    with pytest.raises(ValueError, match=expected):
        run_triage_alerts.validate_llm_budget_args(args)


def test_batch_triage_installs_one_shared_budget_and_reports_actual_usage(monkeypatch) -> None:
    """Make every alert in one standalone batch share one run-scoped provider ledger."""
    args = run_triage_alerts.build_parser().parse_args(["--alert-key", "orders|dq_failure|test"])

    def fake_run_single_alert_triage(**_: object) -> dict[str, object]:
        ledger = active_supervisor_llm_budget()

        assert ledger is not None
        reservation = ledger.reserve_model_call(projected_tokens=120, projected_cost_usd=0.001)
        ledger.reconcile_model_call(
            reservation_id=reservation,
            actual_tokens=80,
            actual_cost_usd=0.0008,
        )

        return {"status": "success", "alert_key": "orders|dq_failure|test"}

    monkeypatch.setattr(run_triage_alerts, "run_single_alert_triage", fake_run_single_alert_triage)

    summary = run_triage_alerts.run_batch_triage(args)

    assert summary["triaged_count"] == 1
    assert summary["llm_budget"]["limits"] == {
        "model_calls": 3,
        "tokens": 16_384,
        "estimated_cost_usd": 0.05,
        "latency_ms": 300_000,
    }
    assert summary["llm_budget"]["usage"]["model_calls"] == 1
    assert summary["llm_budget"]["usage"]["tokens"] == 80
    assert summary["llm_budget"]["usage"]["estimated_cost_usd"] == pytest.approx(0.0008)
