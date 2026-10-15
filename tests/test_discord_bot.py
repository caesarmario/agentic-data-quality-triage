####
## Discord Bot Boundary Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError
from apps.discord_bot import bot, service
from apps.discord_bot.contracts import DiscordRenderResult


# --- Defining Constants
LEGACY_COMMANDS = {
    "help",
    "alerts",
    "approve",
    "ask",
    "backfill_preview",
    "db_summary",
    "daily_summary",
    "reject",
    "table_summary",
    "triage",
    "weekly_summary",
}

GROUPED_ONLY_COMMANDS = {"debug", "language"}
GROUPED_COMMANDS  = {f"dq {command}" for command in LEGACY_COMMANDS | GROUPED_ONLY_COMMANDS}
EXPECTED_COMMANDS = LEGACY_COMMANDS | GROUPED_COMMANDS


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


def test_discord_runtime_uses_only_required_gateway_intents() -> None:
    """
    Validate slash-command operation does not enable broad or privileged intents.

    Returns:
        None.
    """
    assert bot.intents.guilds is True
    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.guild_messages is True
    assert bot.intents.dm_messages is False


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
    assert f'"registered_command_count": {len(EXPECTED_COMMANDS)}' in output
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

    assert commands == LEGACY_COMMANDS | {"dq"}

    dq_command = next(
        command
        for command in bot.tree.get_commands(guild=bot.guild_object())
        if command.name == "dq"
    )

    assert {command.name for command in dq_command.commands} == LEGACY_COMMANDS | GROUPED_ONLY_COMMANDS

    legacy_ask = next(
        command
        for command in bot.tree.get_commands(guild=bot.guild_object())
        if command.name == "ask"
    )
    grouped_ask = next(command for command in dq_command.commands if command.name == "ask")

    assert [parameter.name for parameter in legacy_ask.parameters] == ["question", "alert_key"]
    assert [parameter.name for parameter in grouped_ask.parameters] == ["question", "alert_key"]
    assert "response_mode" not in {parameter.name for parameter in legacy_ask.parameters}
    assert "response_mode" not in {parameter.name for parameter in grouped_ask.parameters}


def test_startup_readiness_does_not_post_to_any_channel(monkeypatch) -> None:
    """Ensure reconnect/readiness diagnostics remain in application logs only."""
    attempted: list[str] = []
    monkeypatch.setenv("DISCORD_WELCOME_ENABLED", "false")

    async def fake_sync(**kwargs) -> None:
        """Avoid Discord command registration network calls."""

    async def unexpected_send_text(*args, **kwargs) -> bool:
        attempted.append("text")
        return pytest.fail("Startup must not post channel messages.")

    async def unexpected_send_render(*args, **kwargs) -> bool:
        attempted.append("embed")
        return pytest.fail("Startup must not post channel embeds.")

    monkeypatch.setattr(bot, "OPS_CHANNEL_ID", 987)
    monkeypatch.setattr(bot, "ALERTS_CHANNEL_ID", 456)
    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    monkeypatch.setattr(bot, "send_text", unexpected_send_text)
    monkeypatch.setattr(bot, "send_render", unexpected_send_render)

    asyncio.run(bot.on_ready())

    assert attempted == []


def test_legacy_approval_command_only_presents_authoritative_controls(monkeypatch) -> None:
    """Legacy slash aliases must not bypass confirmation or rejection-reason controls."""
    calls: dict[str, list[Any]] = {"scope": [], "lookup": [], "render": []}

    class Response:
        """Capture the ephemeral defer used before authoritative lookup."""

        async def defer(self, **kwargs) -> None:
            calls.setdefault("defer", []).append(kwargs)

    class Interaction:
        """Minimal slash-command interaction test double."""

        response = Response()

    monkeypatch.setattr(
        bot,
        "validate_interaction_scope",
        lambda *args, **kwargs: calls["scope"].append(kwargs),
    )
    monkeypatch.setattr(
        bot,
        "fetch_discord_approval_request",
        lambda request_id: calls["lookup"].append(request_id)
        or {
            "request_id": request_id,
            "status": "pending",
            "risk_level": "high",
            "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
            "start_date": "2026-10-07",
            "end_date": "2026-10-07",
        },
    )

    async def capture_render(interaction, result, ephemeral=True) -> None:
        calls["render"].append({"result": result, "ephemeral": ephemeral})

    monkeypatch.setattr(bot, "respond_with_render", capture_render)

    asyncio.run(
        bot.decide_approval(
            interaction=Interaction(),
            request_id="APR-20261007-ABC123",
            decision="approve",
            comment="Review requested.",
        )
    )

    assert calls["lookup"] == ["APR-20261007-ABC123"]
    assert calls["scope"][0]["require_approver"] is True
    assert calls["scope"][0]["require_bot_authored_message"] is False
    render = calls["render"][0]["result"]
    labels = {action.label for row in render.action_rows for action in row.actions}
    fields = {field["name"]: field["value"] for field in render.payload["embeds"][0]["fields"]}
    assert labels == {"View Plan", "Approve", "Reject"}
    assert fields["Risk"] == "HIGH"
    assert "Execute" not in labels
    assert "Run" not in labels
    assert calls["render"][0]["ephemeral"] is True


def test_dq_debug_is_ephemeral_and_does_not_publish(monkeypatch) -> None:
    """Ensure technical context remains private to the requesting operator."""
    calls: list[dict[str, Any]] = []

    class Response:
        """Interaction response test double."""

        async def defer(self, **kwargs) -> None:
            calls.append({"defer": kwargs})

    class Followup:
        """Interaction follow-up test double."""

        async def send(self, **kwargs) -> None:
            calls.append({"send": kwargs})

    class Interaction:
        """Minimal Discord interaction test double."""

        response = Response()
        followup = Followup()

    monkeypatch.setattr(
        bot,
        "fetch_discord_debug_context",
        lambda alert_key: {
            "alert_ref": "DQ-20260610-A1B2C3",
            "alert_key": "system-key",
            "agent_run_id": "run-123",
            "report_id": "RPT-12345678",
            "report_uri": "s3://dq-reports/report.md",
            "provider": "gemini",
            "model": "gemini-flash",
            "transport": "local_read_only",
        },
    )
    monkeypatch.setattr(
        bot,
        "send_render",
        lambda *args, **kwargs: pytest.fail("Debug must not publish to a channel."),
    )

    asyncio.run(bot.dq_debug.callback(Interaction(), "DQ-20260610-A1B2C3"))

    send_call = next(item["send"] for item in calls if "send" in item)
    assert send_call["ephemeral"] is True
    assert send_call["embeds"][0].title == "DQ Debug Context"
    assert "view" not in send_call


def test_command_render_omits_none_view_for_read_only_result() -> None:
    """Read-only command embeds must not pass an invalid explicit None view."""
    calls: list[dict[str, Any]] = []

    class Followup:
        async def send(self, **kwargs) -> None:
            calls.append(kwargs)

    interaction = type("Interaction", (), {"followup": Followup()})()
    render = DiscordRenderResult(
        payload={"embeds": [{"title": "Read-only Result"}]},
        narrative_source="deterministic_fallback",
        content_hash="read-only-hash",
        fallback_markdown="Read-only result.",
    )

    asyncio.run(bot.respond_with_render(interaction, render, ephemeral=True))

    assert calls[0]["ephemeral"] is True
    assert "view" not in calls[0]
    assert calls[0]["embeds"][0].title == "Read-only Result"


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


def test_fetch_discord_daily_summary_prefers_control_plane_api(monkeypatch) -> None:
    """
    Ensure Discord obtains daily aggregates from the shared API when available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = {
        "dt": "2026-06-10",
        "check_counts": [{"status": "pass", "count": 10}],
        "alert_counts": [],
        "total_checks": 10,
        "total_open_alerts": 0,
    }

    class ApiClient:
        """Daily-summary API test double."""

        def get_daily_summary(self, dt: str) -> dict[str, Any]:
            """Return one deterministic summary for the requested date."""
            assert dt == "2026-06-10"
            return expected

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())
    monkeypatch.setattr(
        service,
        "fetch_daily_quality_summary",
        lambda **kwargs: pytest.fail("Local daily summary tool should not run."),
    )

    payload, transport = service.fetch_discord_daily_summary("2026-06-10")

    assert payload == expected
    assert transport == "api"


def test_fetch_discord_daily_summary_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure API connectivity failures use the audited deterministic local tool.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class UnavailableClient:
        """Transport-failing daily-summary API test double."""

        def get_daily_summary(self, dt: str) -> dict[str, Any]:
            """Raise a transport-only failure."""
            raise ControlPlaneTransportError("network unavailable")

    expected = {
        "dt": "2026-06-10",
        "check_counts": [],
        "alert_counts": [],
        "total_checks": 0,
        "total_open_alerts": 0,
    }
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: UnavailableClient())
    monkeypatch.setattr(service, "fetch_daily_quality_summary", lambda **kwargs: expected)

    payload, transport = service.fetch_discord_daily_summary("2026-06-10")

    assert payload == expected
    assert transport == "local"


def test_fetch_discord_daily_summary_does_not_hide_contract_failure(monkeypatch) -> None:
    """
    Ensure malformed or rejected API responses do not silently query ClickHouse locally.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting daily-summary API test double."""

        def get_daily_summary(self, dt: str) -> dict[str, Any]:
            """Raise a non-retryable response failure."""
            raise ControlPlaneResponseError("invalid response")

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: RejectedClient())
    monkeypatch.setattr(
        service,
        "fetch_daily_quality_summary",
        lambda **kwargs: pytest.fail("Contract failures must not use local fallback."),
    )

    with pytest.raises(ControlPlaneResponseError):
        service.fetch_discord_daily_summary("2026-06-10")


@pytest.mark.parametrize(
    ("service_name", "client_name", "local_name", "args"),
    [
        (
            "fetch_discord_weekly_summary",
            "get_weekly_summary",
            "fetch_weekly_quality_summary",
            ("2026-06-10",),
        ),
        (
            "fetch_discord_table_summary",
            "get_table_summary",
            "fetch_table_quality_summary",
            ("dq.fct_orders_daily", "2026-06-04", "2026-06-10"),
        ),
        (
            "fetch_discord_database_summary",
            "get_database_summary",
            "fetch_database_quality_summary",
            ("2026-06-04", "2026-06-10"),
        ),
    ],
)
def test_extended_discord_summaries_use_api_and_only_transport_fallback(
    monkeypatch,
    service_name: str,
    client_name: str,
    local_name: str,
    args: tuple[str, ...],
) -> None:
    """Ensure extended summaries share API-first, contract-safe fallback behavior."""
    expected = {"scope": "bounded", "sql": "internal"}

    class ApiClient:
        """Dynamic summary API test double."""

        def __getattr__(self, name: str):
            """Return the selected API method and reject unexpected names."""
            if name != client_name:
                raise AttributeError(name)

            return lambda *call_args, **kwargs: expected

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())
    monkeypatch.setattr(
        service,
        local_name,
        lambda **kwargs: pytest.fail("Local summary tool should not run when API succeeds."),
    )

    payload, transport = getattr(service, service_name)(*args)

    assert payload == expected
    assert transport == "api"

    class UnavailableClient(ApiClient):
        """Transport-failing summary API test double."""

        def __getattr__(self, name: str):
            """Raise a transport error for the selected API method."""
            if name != client_name:
                raise AttributeError(name)

            return lambda *call_args, **kwargs: (_ for _ in ()).throw(
                ControlPlaneTransportError("network unavailable")
            )

    local_payload = {"scope": "bounded", "sql": "internal"}
    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: UnavailableClient())
    monkeypatch.setattr(service, local_name, lambda **kwargs: dict(local_payload))

    payload, transport = getattr(service, service_name)(*args)

    assert payload == {"scope": "bounded"}
    assert transport == "local"


def test_extended_quality_summary_publishes_embed_not_markdown(monkeypatch) -> None:
    """Weekly, table, and warehouse summaries share the compact embed publisher."""
    published: list[dict[str, Any]] = []
    interaction = object()
    payload = {
        "scope": "weekly",
        "start_date": "2026-06-04",
        "end_date": "2026-06-10",
        "check_counts": [{"status": "fail", "count": 1}],
        "alert_counts": [{"severity": "critical", "count": 1}],
    }

    monkeypatch.setattr(
        bot,
        "build_quality_summary_copilot_note",
        lambda _payload, _responses: "Review the critical failure first.",
    )
    monkeypatch.setattr(bot, "build_llm_route_audit_payload", lambda _response: {})

    async def capture_publish(**kwargs) -> None:
        published.append(kwargs)

    monkeypatch.setattr(bot, "publish_render", capture_publish)

    asyncio.run(
        bot.publish_extended_quality_summary(
            interaction=interaction,
            payload=payload,
            data_transport="api",
            acknowledgement="Summary posted.",
        )
    )

    assert len(published) == 1
    result = published[0]["result"]
    assert published[0]["interaction"] is interaction
    assert published[0]["channel_id"] == bot.TRIAGE_CHANNEL_ID
    assert published[0]["channel_id"] != bot.ALERTS_CHANNEL_ID
    assert len(result.payload["embeds"]) == 1
    assert result.payload["embeds"][0]["title"] == "Weekly data reliability"
    assert result.narrative_source == "heuristic_copilot"
    assert result.payload["embeds"][0]["footer"]["text"] == (
        "Heuristic analysis · No external model call"
    )


def test_daily_summary_publishes_to_triage_channel(monkeypatch) -> None:
    """Daily summaries belong in triage, never in the incident-only alerts channel."""
    published: list[dict[str, Any]] = []

    class Response:
        async def defer(self, **kwargs: Any) -> None:
            return None

    interaction = type("Interaction", (), {"response": Response()})()

    monkeypatch.setattr(bot, "ALERTS_CHANNEL_ID", 456)
    monkeypatch.setattr(bot, "TRIAGE_CHANNEL_ID", 789)
    monkeypatch.setattr(
        bot,
        "fetch_discord_daily_summary",
        lambda _dt: (
            {
                "check_counts": [{"status": "pass", "count": 4}],
                "alert_counts": [],
            },
            "api",
        ),
    )
    monkeypatch.setattr(
        bot,
        "build_daily_summary_copilot_note",
        lambda *_args: "No active reliability issue was found for this date.",
    )

    async def capture_publish(**kwargs: Any) -> None:
        published.append(kwargs)

    monkeypatch.setattr(bot, "publish_render", capture_publish)

    asyncio.run(bot.daily_summary.callback(interaction, "2026-06-10"))

    assert len(published) == 1
    assert published[0]["channel_id"] == 789
    assert published[0]["channel_id"] != 456
    assert published[0]["acknowledgement"] == (
        "Daily summary posted to the configured triage channel."
    )


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
                "llm_runtime": {
                    "provider": "gemini",
                    "model": "gemini-3.5-flash-lite",
                    "runtime_mode": "external_model",
                },
                "incident_history_count": 2,
            }

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())

    result = service.answer_discord_question("What happened?", "DQ-20260610-A1B2C3")

    assert result == {
        "answer": "The partition is empty.",
        "transport": "api",
        "alert_ref": "",
        "agent_run_id": "run-123",
        "alert_key": "system-key",
        "llm_runtime": {
            "provider": "gemini",
            "model": "gemini-3.5-flash-lite",
            "runtime_mode": "external_model",
        },
        "incident_history_count": 2,
    }


def test_answer_discord_question_uses_api_for_global_question(monkeypatch) -> None:
    """Ensure Discord does not bypass the API when Alert Ref is omitted."""
    captured: dict[str, Any] = {}

    class ApiClient:
        """Global Copilot API test double."""

        def answer_copilot(self, **kwargs) -> dict[str, Any]:
            """Capture the request and return one platform-level answer."""
            captured.update(kwargs)
            return {
                "answer": "Ada dua alert aktif.",
                "agent_run_id": "run-global",
                "alert_key": "",
                "llm_runtime": {
                    "provider": "gemini",
                    "model": "gemini-3.5-flash-lite",
                    "runtime_mode": "external_model",
                },
                "incident_history_count": 0,
            }

    monkeypatch.setattr(service, "build_control_plane_client", lambda **kwargs: ApiClient())

    result = service.answer_discord_question(
        "Apakah ada error di data quality? Coba list down."
    )

    assert result["transport"] == "api"
    assert result["answer"] == "Ada dua alert aktif."
    assert result["agent_run_id"] == "run-global"
    assert captured["alert_key"] == ""


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


def test_discord_approval_uses_top_level_alert_correlation(monkeypatch) -> None:
    """Keep alert correlation outside execution parameters so API validation succeeds."""
    captured: dict[str, Any] = {}

    class FakeClient:
        def create_approval_request(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"request_id": "APR-20261007-ABC123", "status": "pending"}

    monkeypatch.setattr(service, "build_control_plane_client", lambda: FakeClient())

    service.create_discord_backfill_approval_request(
        "2026-10-07",
        "2026-10-07",
        "00_dag_dq_platform_daily_orchestrator",
        "Review one bounded recovery plan.",
        "discord:7:mario",
        "orders|dq_failure|2026-10-07|dq.raw_orders|volume|row_count",
    )

    assert captured["alert_key"].startswith("orders|dq_failure|")
    assert captured["parameters"] == {}


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
