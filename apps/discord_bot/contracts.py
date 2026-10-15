####
## Discord Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Typed contracts shared by Discord rendering and delivery boundaries."""

# --- Importing Libraries
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


# --- Defining Contract Literals
DiscordLifecycle = Literal[
    "OPEN",
    "TRIAGED",
    "WAITING APPROVAL",
    "APPROVED",
    "REJECTED",
    "RESOLVED",
]
DiscordNarrativeSource = Literal[
    "external_copilot",
    "heuristic_copilot",
    "deterministic_fallback",
]
DiscordButtonStyle = Literal["primary", "secondary", "success", "danger", "link"]
DiscordInteractionStatus = Literal["success", "rejected", "failed", "cancelled"]


# --- Defining Interactive Action Contracts
@dataclass(frozen=True)
class DiscordActionSpec:
    """One bounded Discord button rendered by both REST and gateway transports."""

    action_id: str
    label: str
    style: DiscordButtonStyle
    custom_id: str = ""
    url: str = ""
    disabled: bool = False
    emoji: str = ""


@dataclass(frozen=True)
class DiscordActionRow:
    """One Discord component row containing no more than five actions."""

    actions: tuple[DiscordActionSpec, ...]


@dataclass(frozen=True)
class DiscordInteractionContext:
    """Sanitized correlation data captured for one button or modal interaction."""

    action: str
    target_ref: str
    actor: str
    guild_id: str
    channel_id: str
    message_id: str = ""


@dataclass(frozen=True)
class DiscordInteractionResult:
    """Bounded interaction outcome suitable for operator feedback and audit."""

    status: DiscordInteractionStatus
    action: str
    target_ref: str
    message: str
    alert_key: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# --- Defining Incident Facts
@dataclass(frozen=True)
class DiscordIncidentFacts:
    """Deterministic incident facts that an LLM is not allowed to override."""

    alert_key: str
    alert_ref: str
    severity: str
    lifecycle: DiscordLifecycle
    issue_title: str
    what_changed: str
    table_name: str
    affected_date: str
    impact: str
    recommended_action: str
    command_hint: str
    approval_state: str = "not_required"


# --- Defining Narrative Content
@dataclass(frozen=True)
class DiscordNarrative:
    """Bounded operator narrative kept separate from deterministic facts."""

    text: str
    source: DiscordNarrativeSource


# --- Defining Incident Payloads
@dataclass(frozen=True)
class DiscordIncidentPayload:
    """Complete incident card input with explicit fact and narrative boundaries."""

    facts: DiscordIncidentFacts
    narrative: DiscordNarrative
    report_id: str = ""
    agent_run_id: str = ""
    report_uri: str = ""
    provider: str = ""
    model: str = ""
    transport: str = ""


# --- Defining Durable Message State
@dataclass(frozen=True)
class DiscordIncidentMessageState:
    """Latest durable Discord message state stored in ClickHouse."""

    alert_key: str
    alert_ref: str
    channel_id: str
    message_id: str
    rendered_lifecycle: DiscordLifecycle
    content_hash: str
    narrative_source: DiscordNarrativeSource
    transport: str = "discord_bot_api"
    author_id: str = ""
    last_http_status: int | None = None
    last_error_state: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --- Defining Render Results
@dataclass(frozen=True)
class DiscordRenderResult:
    """Sanitized Discord payload plus deterministic delivery metadata."""

    payload: dict[str, Any]
    narrative_source: DiscordNarrativeSource
    content_hash: str
    fallback_markdown: str
    lifecycle: DiscordLifecycle | None = None
    provider: str = ""
    model: str = ""
    action_rows: tuple[DiscordActionRow, ...] = field(default_factory=tuple)
