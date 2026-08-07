####
## Discord Bot Boundary Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError
from apps.discord_bot import bot, service


# --- Defining Constants
EXPECTED_COMMANDS = {
    "alerts",
    "approve",
    "ask",
    "backfill_preview",
    "daily_summary",
    "reject",
    "triage",
}


# --- Defining Test Helpers
def build_environment() -> dict[str, str]:
    """
    Build representative Discord settings without real credentials.

    Returns:
        Environment mapping for diagnostics tests.
    """
    return {
        "DISCORD_BOT_TOKEN": "test-only-token",
        "DISCORD_GUILD_ID": "123",
        "DISCORD_ALERTS_CHANNEL_ID": "456",
        "DISCORD_TRIAGE_CHANNEL_ID": "789",
        "DISCORD_OPS_CHANNEL_ID": "987",
        "CONTROL_PLANE_API_URL": "http://api:8000",
        "CONTROL_PLANE_APPROVAL_TOKEN": "approval-token",
    }


# --- Defining Diagnostics Tests
def test_startup_diagnostics_are_secret_safe_and_commands_are_registered() -> None:
    """
    Ensure diagnostics expose readiness without credential values.

    Returns:
        None.
    """
    environment = build_environment()
    diagnostics = bot.build_startup_diagnostics(environment=environment)
    serialized  = json.dumps(diagnostics)

    assert diagnostics["status"] == "ready"
    assert diagnostics["missing_settings"] == []
    assert set(diagnostics["registered_commands"]) == EXPECTED_COMMANDS
    assert diagnostics["registered_command_count"] == len(EXPECTED_COMMANDS)
    assert diagnostics["control_plane_api_configured"] is True
    assert diagnostics["approval_mutations_configured"] is True
    assert diagnostics["message_content_intent_enabled"] is False
    assert diagnostics["command_scope"] == "guild"
    assert environment["DISCORD_BOT_TOKEN"] not in serialized
    assert environment["CONTROL_PLANE_APPROVAL_TOKEN"] not in serialized


def test_smoke_cli_reports_configuration_without_connecting(monkeypatch, capsys) -> None:
    """
    Ensure smoke mode never starts Discord network IO.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest output capture fixture.

    Returns:
        None.
    """
    environment = build_environment()

    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(
        bot.client,
        "run",
        lambda *args, **kwargs: pytest.fail("Smoke mode must not connect to Discord."),
    )

    return_code = bot.main(["--smoke", "--require-settings"])
    output      = capsys.readouterr().out

    assert return_code == 0
    assert '"status": "ready"' in output
    assert '"registered_command_count": 7' in output
    assert '"message_content_intent_enabled": false' in output
    assert environment["DISCORD_BOT_TOKEN"] not in output
    assert environment["CONTROL_PLANE_APPROVAL_TOKEN"] not in output


def test_smoke_cli_fails_when_required_settings_are_missing(monkeypatch) -> None:
    """
    Ensure strict smoke mode reports incomplete Discord configuration.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    for name in bot.REQUIRED_DISCORD_SETTINGS:
        monkeypatch.delenv(name, raising=False)

    assert bot.main(["--smoke", "--require-settings"]) == 2


def test_tree_registers_expected_slash_commands() -> None:
    """
    Ensure the command tree exposes the supported operator actions.

    Returns:
        None.
    """
    commands = {
        command.name
        for command in bot.tree.get_commands(guild=bot.guild_object())
    }

    assert commands == EXPECTED_COMMANDS


# --- Defining API Readiness Tests
def test_control_plane_health_probe_is_secret_safe(monkeypatch) -> None:
    """
    Ensure readiness returns bounded service metadata.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class HealthyClient:
        """Healthy control-plane test double."""

        def health(self) -> dict[str, Any]:
            """Return a healthy API response."""
            return {
                "status": "ok",
                "service": "agentic-dq-api",
                "version": "0.1.0",
            }

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: HealthyClient())

    assert service.probe_control_plane_health() == {
        "status": "ready",
        "service": "agentic-dq-api",
        "version": "0.1.0",
    }


def test_control_plane_health_probe_sanitizes_transport_failure(monkeypatch) -> None:
    """
    Ensure readiness diagnostics never expose transport details.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class FailedClient:
        """Unavailable control-plane test double."""

        def health(self) -> dict[str, Any]:
            """Raise a controlled transport error."""
            raise ControlPlaneTransportError("sensitive endpoint details")

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: FailedClient())

    assert service.probe_control_plane_health() == {
        "status": "unavailable",
        "error_type": "ControlPlaneTransportError",
    }


# --- Defining Shared Service Tests
def test_fetch_discord_alerts_prefers_control_plane_api(monkeypatch) -> None:
    """
    Ensure Discord reads alerts through the shared API when available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = {"alerts": [{"alert_key": "key"}], "count": 1}

    class ApiClient:
        """Alert API test double."""

        def list_alerts(self, **kwargs) -> dict[str, Any]:
            """Return one stable alert payload."""
            return expected

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())
    monkeypatch.setattr(
        service,
        "list_alerts",
        lambda **kwargs: pytest.fail("Local alert tool should not run."),
    )

    payload, transport = service.fetch_discord_alerts("open", "2026-06-10", 5)

    assert payload == expected
    assert transport == "api"


def test_fetch_discord_alerts_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure transport outages may use the local deterministic alert tool.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class UnavailableClient:
        """Transport-failing API test double."""

        def list_alerts(self, **kwargs) -> dict[str, Any]:
            """Raise a transport-only failure."""
            raise ControlPlaneTransportError("network unavailable")

    expected = {"alerts": [], "count": 0}
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: UnavailableClient())
    monkeypatch.setattr(service, "list_alerts", lambda **kwargs: expected)

    payload, transport = service.fetch_discord_alerts("open", None, 5)

    assert payload == expected
    assert transport == "local"


def test_fetch_discord_alerts_does_not_hide_contract_failure(monkeypatch) -> None:
    """
    Ensure HTTP and contract failures remain visible.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting API test double."""

        def list_alerts(self, **kwargs) -> dict[str, Any]:
            """Raise a response contract failure."""
            raise ControlPlaneResponseError("invalid response")

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: RejectedClient())

    with pytest.raises(ControlPlaneResponseError):
        service.fetch_discord_alerts("open", None, 5)


def test_answer_discord_question_uses_api_correlation(monkeypatch) -> None:
    """
    Ensure grounded Copilot answers retain API audit correlation.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class ApiClient:
        """Copilot API test double."""

        def answer_copilot(self, **kwargs) -> dict[str, Any]:
            """Return one correlated answer."""
            return {
                "answer": "The partition is empty.",
                "agent_run_id": "run-123",
                "alert_key": "system-key",
            }

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())

    result = service.answer_discord_question("What happened?", "DQ-20260610-A1B2C3")

    assert result == {
        "answer": "The partition is empty.",
        "transport": "api",
        "agent_run_id": "run-123",
        "alert_key": "system-key",
    }


def test_approval_mutations_require_control_plane_api(monkeypatch) -> None:
    """
    Ensure Discord never records approvals through an unaudited fallback.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: None)

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_API_URL"):
        service.create_discord_backfill_approval_request(
            "2026-06-10",
            "2026-06-10",
            "00_dag_dq_platform_daily_orchestrator",
            "Missing data",
            "discord:7:mario",
        )

    with pytest.raises(RuntimeError, match="CONTROL_PLANE_API_URL"):
        service.decide_discord_approval_request(
            "APR-20260610-A1B2C3D4",
            "approve",
            "discord:7:mario",
            "Reviewed",
        )


# --- Defining Runtime Surface Tests
def test_runtime_surfaces_are_configured_for_discord() -> None:
    """
    Ensure Discord is the only configured chat-operations runtime surface.

    Returns:
        None.
    """
    requirements = Path("infra/requirements.txt").read_text(encoding="utf-8")
    compose      = Path("infra/docker-compose.yml").read_text(encoding="utf-8")
    env_example  = Path(".env.example").read_text(encoding="utf-8")
    makefile     = Path("Makefile").read_text(encoding="utf-8")

    assert "discord.py" in requirements
    assert "discord-bot:" in compose
    assert "apps.discord_bot.bot" in compose
    assert "DISCORD_BOT_TOKEN=" in env_example
    assert "discord-smoke" in makefile

    # Keep the retired Telegram adapter from silently returning to runtime configuration.
    assert "python-telegram-bot" not in requirements.casefold()
    assert "telegram-bot:" not in compose.casefold()
    assert "apps.telegram_bot" not in compose.casefold()
    assert "TELEGRAM_" not in env_example
    assert "telegram-" not in makefile.casefold()
    assert not Path("apps/telegram_bot").exists()
