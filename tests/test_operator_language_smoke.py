from unittest.mock import Mock
import pytest
from scripts.smoke_operator_language import run


def response(**changes):
    payload = {"language": "en", "alert_display_id": "DQ-20261011-3E3C14", "agent_run_id": "synthetic-run",
               "answer": "One required segment is missing. Review the evidence first.",
               "llm_runtime": {"provider": "gemini", "model": "gemini-test", "used_heuristic": False,
                               "fallback_reason": "", "input_tokens": 100, "output_tokens": 20, "estimated_cost_usd": .00008}}
    payload.update(changes)
    result = Mock()
    result.json.return_value = payload
    return result


def test_smoke_makes_one_api_call_and_requires_real_provider(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_URL", "http://api:8000")
    request = Mock(return_value=response())
    result = run("en", "DQ-20261011-3E3C14", request)
    assert result["used_heuristic"] is False and result["total_tokens"] == 120
    request.assert_called_once()
    assert request.call_args.kwargs["json"]["language"] == "en"
    assert "provider" not in request.call_args.kwargs["json"]


@pytest.mark.parametrize("changes", [{"language": "id"}, {"alert_display_id": "DQ-20261011-000000"}, {"llm_runtime": {"provider": "heuristic", "used_heuristic": True}}])
def test_smoke_rejects_fallback_or_mismatched_context(changes):
    with pytest.raises(ValueError):
        run("en", "DQ-20261011-3E3C14", Mock(return_value=response(**changes)))


def test_invalid_scope_rejected_before_api_call():
    request = Mock()
    with pytest.raises(ValueError):
        run("en", "'; arbitrary shell", request)
    request.assert_not_called()
