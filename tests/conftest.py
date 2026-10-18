"""Unit tests default to isolated control paths and zero paid provider calls."""
import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_defaults(monkeypatch):
    # Tests of an enabled path must opt in explicitly and provide their doubles.
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "false")
    monkeypatch.setenv("INCIDENT_CONTROL_ENABLED", "false")
    for name in (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
        "DISCORD_BOT_TOKEN", "CONTROL_PLANE_APPROVAL_TOKEN",
    ):
        monkeypatch.setenv(name, "")
