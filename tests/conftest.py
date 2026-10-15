"""Unit tests default to isolated control paths and zero paid provider calls."""
import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_defaults(monkeypatch):
    # Tests of an enabled path must opt in explicitly and provide their doubles.
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "false")
    monkeypatch.setenv("INCIDENT_CONTROL_ENABLED", "false")
