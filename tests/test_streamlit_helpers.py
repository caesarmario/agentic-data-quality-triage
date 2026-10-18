####
## Streamlit Helper Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import inspect
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import Mock

from apps.common.control_plane import ControlPlaneResponseError, ControlPlaneTransportError

from apps.streamlit import app as streamlit_app
from apps.streamlit.app import (
    answer_ui_copilot_question,
    build_blast_radius_display_rows,
    build_checkpoint_history_display_rows,
    build_copilot_presentation,
    build_incident_option_rows,
    build_incident_history_display_rows,
    build_reliability_kpis,
    build_streamlit_control_plane_client,
    build_ui_backfill_approval_payload,
    build_llm_runtime_summary,
    build_ui_copilot_context,
    classify_reliability_state,
    create_ui_backfill_approval_request,
    decide_ui_approval_request,
    fetch_streamlit_alert_rows,
    fetch_streamlit_audit_rows,
    fetch_streamlit_daily_summary,
    load_approval_queue_rows,
    load_incident_history_rows,
    load_life_evaluation_rows,
    matching_blast_radius_result,
    matching_triage_result,
    request_ui_dbt_blast_radius,
    request_ui_checkpoint_history,
    request_ui_checkpoint_replay_preview,
    request_ui_copilot_api,
    read_report_text,
    resolve_alert_date_filter,
    run_selected_alert_triage,
    summarize_alert_rows,
    summarize_approval_queue_rows,
    summarize_daily_quality_payload,
    summarize_incident_history_rows,
    summarize_life_evaluation_rows,
    synchronize_ui_incident_card,
)


# --- Defining Test Constants
PROJECT_ROOT = Path(__file__).resolve().parents[1]
THEME_CONFIG = PROJECT_ROOT / ".streamlit" / "config.toml"


@pytest.mark.parametrize('opened,truncated,has_uri', [(False, False, True), (True, False, True), (True, True, True), (True, False, False)])
def test_saved_triage_read_is_explicit_bounded_and_never_runs_a_model(monkeypatch, opened, truncated, has_uri):
    state = {}
    fake_ui = SimpleNamespace(session_state=state, caption=Mock(), button=Mock(return_value=opened), warning=Mock(), code=Mock())
    api = Mock()
    api.read_report_artifact.return_value = {'text': '# Recorded investigation', 'truncated': truncated}
    run = Mock(side_effect=AssertionError('Reading a saved report must not run triage'))
    document, access = Mock(), Mock()
    monkeypatch.setattr(streamlit_app, 'st', fake_ui)
    monkeypatch.setattr(streamlit_app, 'tr', lambda text, **kwargs: text)
    monkeypatch.setattr(streamlit_app, 'build_streamlit_control_plane_client', lambda: api)
    monkeypatch.setattr(streamlit_app, 'run_selected_alert_triage', run)
    monkeypatch.setattr(streamlit_app, 'render_report_document', document)
    monkeypatch.setattr(streamlit_app, 'render_report_access', access)
    uri = 's3://dq-artifacts/agent-reports/dt=2026-10-16/report.md'
    assert streamlit_app.render_saved_triage_panel({'report_s3_uri': uri if has_uri else ''}) is has_uri
    run.assert_not_called()
    if not has_uri or not opened:
        api.read_report_artifact.assert_not_called()
    else:
        api.read_report_artifact.assert_called_once_with(s3_uri=uri, max_bytes=streamlit_app.MAX_REPORT_BYTES)
    if opened and has_uri and not truncated:
        document.assert_called_once()
        access.assert_called_once()
        fake_ui.button.return_value = False
        streamlit_app.render_saved_triage_panel({'report_s3_uri': uri})
        api.read_report_artifact.assert_called_once()
    else:
        document.assert_not_called()
        access.assert_not_called()
        assert 'saved_triage_read' not in state


# --- Defining Accessibility Helpers
def calculate_relative_luminance(hex_color: str) -> float:
    """
    Calculate WCAG relative luminance for one hexadecimal RGB color.

    Args:
        hex_color: Six-digit hexadecimal color including the leading hash.

    Returns:
        Relative luminance between zero and one.
    """
    channel_values = [
        int(hex_color[index : index + 2], 16) / 255
        for index in (1, 3, 5)
    ]
    linear_channels = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channel_values
    ]

    return (
        0.2126 * linear_channels[0]
        + 0.7152 * linear_channels[1]
        + 0.0722 * linear_channels[2]
    )


def calculate_contrast_ratio(foreground: str, background: str) -> float:
    """
    Calculate the WCAG contrast ratio between two hexadecimal colors.

    Args:
        foreground: Foreground hexadecimal RGB color.
        background: Background hexadecimal RGB color.

    Returns:
        Contrast ratio where 4.5 or higher satisfies normal text AA guidance.
    """
    luminances = sorted(
        [
            calculate_relative_luminance(foreground),
            calculate_relative_luminance(background),
        ],
        reverse=True,
    )

    return (luminances[0] + 0.05) / (luminances[1] + 0.05)


# --- Defining Tests
def test_streamlit_theme_config_defines_complete_light_and_dark_palettes() -> None:
    """
    Validate that Streamlit can expose equivalent light and dark theme choices.

    Returns:
        None.
    """
    with THEME_CONFIG.open("rb") as config_file:
        config = tomllib.load(config_file)

    shared_theme = config["theme"]

    assert shared_theme["showSidebarBorder"] is True
    assert shared_theme["baseFontSize"] == 16
    assert shared_theme["baseFontWeight"] == 400

    for mode in ("light", "dark"):
        theme   = config["theme"][mode]
        sidebar = theme["sidebar"]

        assert theme["showWidgetBorder"] is True
        assert theme["font"] == "Aptos"
        assert theme["headingFont"] == "Bahnschrift"
        assert theme["codeFont"] == "Cascadia Mono"
        assert sidebar["backgroundColor"]
        assert sidebar["textColor"]

        # These options are only accepted in the shared [theme] table by Streamlit 1.59.
        invalid_subtheme_keys = {
            "base",
            "showSidebarBorder",
            "baseFontSize",
            "baseFontWeight",
            "chartCategoricalColors",
        }

        assert invalid_subtheme_keys.isdisjoint(theme)


def test_streamlit_theme_colors_meet_normal_text_contrast_targets() -> None:
    """
    Validate body, control, and severity colors remain readable in both modes.

    Returns:
        None.
    """
    with THEME_CONFIG.open("rb") as config_file:
        config = tomllib.load(config_file)

    semantic_colors = ("red", "orange", "yellow", "green", "blue", "violet", "gray")

    for mode in ("light", "dark"):
        theme = config["theme"][mode]

        assert calculate_contrast_ratio(theme["textColor"], theme["backgroundColor"]) >= 4.5
        assert calculate_contrast_ratio("#FFFFFF", theme["primaryColor"]) >= 4.5

        for color_name in semantic_colors:
            assert calculate_contrast_ratio(
                theme[f"{color_name}TextColor"],
                theme[f"{color_name}BackgroundColor"],
            ) >= 4.5


def test_page_style_uses_semantic_theme_tokens_for_operator_surfaces() -> None:
    """
    Validate custom CSS follows Streamlit theme tokens instead of one fixed mode.

    Returns:
        None.
    """
    light_css = streamlit_app.build_page_style_css("light")
    dark_css = streamlit_app.build_page_style_css("dark")

    assert "var(--st-background-color" in light_css
    assert "var(--st-red-background-color" in light_css
    assert "var(--st-green-text-color" in light_css
    assert "#F6F8F5" in light_css
    assert "#0B1210" in dark_css
    assert "#EBF4EF" in dark_css
    assert light_css != dark_css

    for css in (light_css, dark_css):
        assert ".st-key-triage_report_document" in css
        assert ".st-key-artifact_report_document" in css
        assert 'label[data-testid="stMetricLabel"] *' in css
        assert 'div[data-testid="stTab"]' in css
        assert 'div[data-testid="stTab"][aria-selected="true"]' in css
        assert 'section[data-testid="stMain"] label[data-testid="stWidgetLabel"] *' in css
        assert "@media (max-width: 768px)" in css
        assert "linear-gradient" not in css
        assert "#65758b" not in css.lower()


def test_live_theme_colors_follow_native_scheme_without_overriding_canvas() -> None:
    for mode in ("light", "dark"):
        css = streamlit_app.build_page_style_css(mode)
        assert '@supports (color: light-dark(white, black))' in css
        assert '[data-testid="stApp"] {' in css
        assert '--dq-surface: light-dark(#FFFFFF, #141D19);' in css
        assert '--dq-text: light-dark(#18211B, #EBF4EF);' in css
        assert '--dq-info-text: light-dark(#1F477F, #BED4FA);' in css
        assert 'color-scheme:' not in css
        assert '.stApp {' not in css


def test_streamlit_workspace_defines_incident_first_tracked_tab_hierarchy() -> None:
    """
    Ensure primary and incident tabs remain stable and lazily rendered.

    Returns:
        None.
    """
    main_source     = inspect.getsource(streamlit_app.main)
    incident_source = inspect.getsource(streamlit_app.render_incident_workspace)

    assert streamlit_app.PRIMARY_WORKSPACE_TABS == (
        "Incidents",
        "Approvals",
        "Agent Reliability",
    )
    assert streamlit_app.INCIDENT_DETAIL_TABS == (
        "Overview",
        "Evidence",
        "AI Analysis",
        "Lineage",
        "Approval",
        "Audit & Recovery",
    )
    assert 'key="primary_workspace_tabs"' in main_source
    assert 'key="incident_detail_tabs"' in incident_source
    assert 'on_change="rerun"' in main_source
    assert 'on_change="rerun"' in incident_source
    assert ".open" in main_source
    assert ".open" in incident_source
    assert incident_source.index("render_alert_table") < incident_source.index("workspace_tabs")


def test_streamlit_incident_defaults_use_active_status_and_latest_snapshot() -> None:
    """
    Validate the default scope does not hide triaged incidents or stale dates.

    Returns:
        None.
    """
    sidebar_source = inspect.getsource(streamlit_app.render_sidebar_filters)

    assert streamlit_app.DEFAULT_ALERT_STATUS == "active"
    assert 'options=[DEFAULT_ALERT_STATUS, "open", "acknowledged", "triaged", "resolved"]' in sidebar_source
    assert 'options=["Latest available", "Choose date"]' in sidebar_source
    assert "with st.expander(tr('Advanced triage settings'), expanded=False)" in sidebar_source


def test_streamlit_empty_states_cover_incident_summary_report_and_ai_fallback() -> None:
    """
    Ensure each operator dead-end has a specific recovery-oriented message.

    Returns:
        None.
    """
    messages = streamlit_app.EMPTY_STATE_MESSAGES

    assert set(messages) == {
        "no_active_incidents",
        "no_daily_summary",
        "no_triage_report",
        "ai_fallback",
    }
    assert "active incidents" in messages["no_active_incidents"].lower()
    assert "daily quality snapshot" in messages["no_daily_summary"].lower()
    assert "run triage" in messages["no_triage_report"].lower()
    assert "fallback" in messages["ai_fallback"].lower()


@pytest.mark.parametrize(
    ("severity", "expected_class"),
    [
        ("critical", "dq-severity-critical"),
        ("HIGH", "dq-severity-critical"),
        ("warning", "dq-severity-warning"),
        ("medium", "dq-severity-warning"),
        ("info", "dq-severity-info"),
        ("low", "dq-severity-info"),
        ("unknown", "dq-severity-neutral"),
    ],
)
def test_severity_css_class_uses_an_allowlisted_semantic_mapping(
    severity: str,
    expected_class: str,
) -> None:
    """
    Validate alert severities cannot inject arbitrary CSS class names.

    Args:
        severity: Raw severity supplied by the test case.
        expected_class: Allowlisted class expected from the mapping.

    Returns:
        None.
    """
    assert streamlit_app.severity_css_class(severity) == expected_class


def test_escape_html_text_protects_custom_alert_card_markup() -> None:
    """
    Validate custom HTML alert values are escaped before rendering.

    Returns:
        None.
    """
    raw_value = '<script>alert("unsafe")</script> & raw'

    escaped_value = streamlit_app.escape_html_text(raw_value)

    assert escaped_value == "&lt;script&gt;alert(&quot;unsafe&quot;)&lt;/script&gt; &amp; raw"
    assert "<script>" not in escaped_value


# --- Defining Control Plane Read Tests
def test_build_streamlit_control_plane_client_normalizes_url_and_timeout(monkeypatch) -> None:
    """
    Validate Streamlit builds one bounded shared control-plane client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal client constructor test double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture normalized client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured.update(kwargs)

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    client = build_streamlit_control_plane_client(
        api_base_url="http://api:8000/",
        timeout_seconds=999,
    )

    assert client is not None
    assert captured == {
        "base_url": "http://api:8000",
        "timeout_seconds": 60.0,
    }


def test_fetch_streamlit_alert_rows_prefers_control_plane_api(monkeypatch) -> None:
    """
    Ensure Streamlit reads public alerts through FastAPI when available.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = [{"alert_key": "orders|test", "alert_display_id": "DQ-TEST01"}]

    class ApiClient:
        """Alert API success test double."""

        def list_alerts(self, **kwargs) -> dict[str, object]:
            """
            Return one public alert row.

            Args:
                **kwargs: Bounded alert filters.

            Returns:
                API-style alert collection.
            """
            assert kwargs == {
                "status": "open",
                "dt": "2026-06-10",
                "limit": 5,
            }

            return {"alerts": expected}

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "list_alerts",
        lambda **kwargs: pytest.fail("Local alert tool must not run after API success."),
    )

    rows, transport = fetch_streamlit_alert_rows(
        status="open",
        dt="2026-06-10",
        limit=5,
    )

    assert rows == expected
    assert transport == "api"


def test_fetch_streamlit_alert_rows_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure an API network outage may use the deterministic local alert tool.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = [{"alert_key": "orders|fallback", "alert_display_id": "DQ-LOCAL1"}]

    class UnavailableClient:
        """Transport-failing alert API test double."""

        def list_alerts(self, **kwargs) -> dict[str, object]:
            """Raise a controlled transport error for every request."""
            raise ControlPlaneTransportError("network unavailable")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: UnavailableClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "list_alerts",
        lambda **kwargs: {"alerts": expected},
    )

    rows, transport = fetch_streamlit_alert_rows(
        status="open",
        dt=None,
        limit=5,
    )

    assert rows == expected
    assert transport == "local"


def test_fetch_streamlit_alert_rows_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure malformed or rejected API alert responses remain visible.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting alert API test double."""

        def list_alerts(self, **kwargs) -> dict[str, object]:
            """Raise a public response contract error."""
            raise ControlPlaneResponseError("invalid alert response")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: RejectedClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "list_alerts",
        lambda **kwargs: pytest.fail("Response failures must not trigger local reads."),
    )

    with pytest.raises(ControlPlaneResponseError, match="invalid alert response"):
        fetch_streamlit_alert_rows(status="open", dt=None, limit=5)


def test_fetch_streamlit_daily_summary_prefers_control_plane_api(monkeypatch) -> None:
    """
    Ensure the Reliability Overview reads its daily snapshot through FastAPI.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = {
        "status": "success",
        "dt": "2026-06-10",
        "check_counts": [{"status": "pass", "count": 8}],
        "alert_counts": [],
        "total_checks": 8,
        "total_open_alerts": 0,
        "duration_ms": 4,
        "summary": "Daily quality summary.",
    }

    class ApiClient:
        """Daily-summary API success test double."""

        def get_daily_summary(self, **kwargs) -> dict[str, object]:
            """
            Return one validated public daily summary.

            Args:
                **kwargs: Exact requested business date.

            Returns:
                API-style daily summary payload.
            """
            assert kwargs == {"dt": "2026-06-10"}

            return expected

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "fetch_daily_quality_summary",
        lambda **kwargs: pytest.fail("Local daily-summary tool must not run after API success."),
    )

    payload, transport = fetch_streamlit_daily_summary(dt="2026-06-10")

    assert payload == expected
    assert transport == "api"


def test_fetch_streamlit_daily_summary_requests_latest_when_date_is_omitted(monkeypatch) -> None:
    """
    Ensure latest snapshot mode passes an explicit None through the shared API.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = {
        "status": "success",
        "dt": "2026-06-10",
        "check_counts": [],
        "alert_counts": [],
        "total_checks": 0,
        "total_open_alerts": 0,
        "duration_ms": 1,
        "summary": "Latest daily quality summary.",
    }

    class ApiClient:
        """Latest-summary API test double."""

        def get_daily_summary(self, **kwargs) -> dict[str, object]:
            """Return the latest summary for a None date."""
            assert kwargs == {"dt": None}

            return expected

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )

    payload, transport = fetch_streamlit_daily_summary(dt=None)

    assert payload == expected
    assert transport == "api"


@pytest.mark.parametrize("dt", [None, "2026-10-16"])
def test_fetch_streamlit_daily_summary_accepts_empty_api_success_without_fallback(monkeypatch, dt) -> None:
    expected = {
        "status": "success", "availability": "empty", "dt": dt,
        "check_counts": [], "alert_counts": [], "total_checks": 0, "total_open_alerts": 0,
    }
    monkeypatch.setattr(
        streamlit_app, "build_streamlit_control_plane_client",
        lambda **kwargs: SimpleNamespace(get_daily_summary=lambda **kwargs: expected),
    )
    monkeypatch.setattr(
        streamlit_app, "fetch_daily_quality_summary",
        lambda **kwargs: pytest.fail("Empty API success must not trigger local data access."),
    )

    payload, transport = fetch_streamlit_daily_summary(dt=dt)

    assert payload == expected
    assert transport == "api"


def test_fetch_streamlit_daily_summary_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure a network outage uses the audited local tool without exposing SQL.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class UnavailableClient:
        """Transport-failing daily-summary API test double."""

        def get_daily_summary(self, **kwargs) -> dict[str, object]:
            """Raise a controlled transport error for every request."""
            raise ControlPlaneTransportError("network unavailable")

    def fake_local_summary(**kwargs) -> dict[str, object]:
        """
        Return a local summary containing internal metadata.

        Args:
            **kwargs: Exact business date supplied to the local tool.

        Returns:
            Daily summary payload including SQL that must be removed.
        """
        captured.update(kwargs)

        return {
            "status": "success",
            "dt": "2026-06-10",
            "check_counts": [{"status": "warn", "count": 1}],
            "alert_counts": [{"severity": "warning", "count": 1}],
            "total_checks": 1,
            "total_open_alerts": 1,
            "duration_ms": 2,
            "summary": "Daily quality summary.",
            "sql": "SELECT secret_internal_query",
        }

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: UnavailableClient(),
    )
    monkeypatch.setattr(streamlit_app, "fetch_daily_quality_summary", fake_local_summary)

    payload, transport = fetch_streamlit_daily_summary(dt="2026-06-10")

    assert transport == "local"
    assert captured == {"dt": "2026-06-10"}
    assert "sql" not in payload


def test_fetch_streamlit_daily_summary_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure malformed API summaries remain visible instead of falling back.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting daily-summary API test double."""

        def get_daily_summary(self, **kwargs) -> dict[str, object]:
            """Raise a public response contract error."""
            raise ControlPlaneResponseError("invalid daily summary response")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: RejectedClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "fetch_daily_quality_summary",
        lambda **kwargs: pytest.fail("Response failures must not trigger local reads."),
    )

    with pytest.raises(ControlPlaneResponseError, match="invalid daily summary response"):
        fetch_streamlit_daily_summary(dt="2026-06-10")


def test_fetch_streamlit_audit_rows_prefers_sanitized_api_contract(monkeypatch) -> None:
    """
    Ensure Streamlit reads sanitized audit history through FastAPI first.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = [{"action": "triage_completed", "status": "success"}]

    class ApiClient:
        """Audit API success test double."""

        def get_audit_logs(self, **kwargs) -> dict[str, object]:
            """
            Return sanitized audit rows.

            Args:
                **kwargs: Exact alert identity and result bound.

            Returns:
                API-style audit collection.
            """
            assert kwargs == {"alert_key": "orders|test", "limit": 25}

            return {"rows": expected}

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "load_local_audit_rows",
        lambda **kwargs: pytest.fail("Local audit query must not run after API success."),
    )

    rows, transport = fetch_streamlit_audit_rows(
        alert_key="orders|test",
        limit=25,
    )

    assert rows == expected
    assert transport == "api"


def test_fetch_streamlit_audit_rows_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure audit transport outages retain bounded local operability.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    expected = [{"action": "local_audit", "status": "success"}]
    captured: dict[str, object] = {}

    class UnavailableClient:
        """Transport-failing audit API test double."""

        def get_audit_logs(self, **kwargs) -> dict[str, object]:
            """Raise a controlled transport error for every request."""
            raise ControlPlaneTransportError("api unavailable")

    def fake_local_read(**kwargs) -> list[dict[str, object]]:
        """
        Capture the local fallback bound.

        Args:
            **kwargs: Exact alert key and bounded result limit.

        Returns:
            One deterministic local audit row.
        """
        captured.update(kwargs)

        return expected

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: UnavailableClient(),
    )
    monkeypatch.setattr(streamlit_app, "load_local_audit_rows", fake_local_read)

    rows, transport = fetch_streamlit_audit_rows(
        alert_key="orders|test",
        limit=500,
    )

    assert rows == expected
    assert transport == "local"
    assert captured == {"alert_key": "orders|test", "limit": 100}


def test_fetch_streamlit_audit_rows_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure malformed API audit responses cannot silently use ClickHouse fallback.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting audit API test double."""

        def get_audit_logs(self, **kwargs) -> dict[str, object]:
            """Raise a public response contract error."""
            raise ControlPlaneResponseError("invalid audit response")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: RejectedClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "load_local_audit_rows",
        lambda **kwargs: pytest.fail("Response failures must not trigger local reads."),
    )

    with pytest.raises(ControlPlaneResponseError, match="invalid audit response"):
        fetch_streamlit_audit_rows(alert_key="orders|test", limit=25)


def test_read_report_text_prefers_bounded_control_plane_api(monkeypatch) -> None:
    """
    Ensure Streamlit reads report content through the typed API contract.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    report_uri = "s3://dq-artifacts/agent-reports/report.md"

    class ApiClient:
        """Report API success test double."""

        def read_report_artifact(self, **kwargs) -> dict[str, object]:
            """
            Return one bounded report artifact.

            Args:
                **kwargs: Artifact identity and byte bound.

            Returns:
                API-style report payload.
            """
            assert kwargs == {"s3_uri": report_uri, "max_bytes": 125}

            return {"text": "# Report", "truncated": False, "bytes_read": 8, "returned_bytes": 8}

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "read_s3_text",
        lambda **kwargs: pytest.fail("Local S3 must not run after API success."),
    )

    text, transport = read_report_text(
        s3_uri=report_uri,
        max_bytes=125,
    )

    assert text == "# Report"
    assert transport == "api"


def test_read_report_text_transport_fallback_preserves_hard_byte_bound(monkeypatch) -> None:
    """
    Ensure report fallback remains bounded when the API cannot be reached.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    report_uri = "s3://dq-artifacts/agent-reports/report.md"
    captured: dict[str, object] = {}

    class UnavailableClient:
        """Transport-failing report API test double."""

        def read_report_artifact(self, **kwargs) -> dict[str, object]:
            """Raise a controlled transport error for every request."""
            raise ControlPlaneTransportError("api unavailable")

    def fake_local_read(**kwargs) -> str:
        """
        Capture local artifact identity and safety bound.

        Args:
            **kwargs: S3 URI and hard byte bound.

        Returns:
            Deterministic local report text.
        """
        captured.update(kwargs)

        return "# Local Report"

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: UnavailableClient(),
    )
    monkeypatch.setattr(streamlit_app, "read_s3_text", fake_local_read)

    text, transport = read_report_text(
        s3_uri=report_uri,
        max_bytes=75,
    )

    assert text == "# Local Report"
    assert transport == "local"
    assert captured == {"s3_uri": report_uri, "max_bytes": 75}


def test_read_report_text_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure report identity or byte-contract failures remain visible.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting report API test double."""

        def read_report_artifact(self, **kwargs) -> dict[str, object]:
            """Raise a public response contract error."""
            raise ControlPlaneResponseError("report identity mismatch")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: RejectedClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "read_s3_text",
        lambda **kwargs: pytest.fail("Response failures must not trigger local S3 reads."),
    )

    with pytest.raises(ControlPlaneResponseError, match="report identity mismatch"):
        read_report_text(
            s3_uri="s3://dq-artifacts/agent-reports/report.md",
            max_bytes=125,
        )


# --- Defining Triage Transport Tests
def build_streamlit_triage_report(
    alert_key: str = "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table",
) -> SimpleNamespace:
    """
    Build the minimum report shape consumed by the Streamlit triage adapter.

    Args:
        alert_key: Stable system alert key returned by the triage workflow.

    Returns:
        Report-like namespace with stable identity, confidence, and artifacts.
    """
    return SimpleNamespace(
        agent_run_id="11111111-1111-1111-1111-111111111111",
        alert=SimpleNamespace(
            alert_key=alert_key,
            alert_display_id="DQ-20260610-TEST01",
        ),
        confidence=0.82,
        top_hypothesis=SimpleNamespace(title="Missing raw partition"),
        markdown_report_s3_uri="s3://dq-artifacts/agent-reports/report.md",
        json_report_s3_uri="s3://dq-artifacts/agent-reports/report.json",
        approval_gated_actions=[],
    )


def test_run_selected_alert_triage_prefers_control_plane_api(monkeypatch) -> None:
    """
    Ensure Streamlit triage uses the shared API and retains report identity.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    alert_key = "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table"
    report    = build_streamlit_triage_report(alert_key=alert_key)
    captured: dict[str, object] = {}

    class ApiClient:
        """Triage API success test double."""

        def run_triage_report(self, **kwargs) -> SimpleNamespace:
            """
            Capture the bounded request and return one typed report shape.

            Args:
                **kwargs: Triage identity, evidence-loop, and manifest settings.

            Returns:
                Stable report-like namespace.
            """
            captured.update(kwargs)

            return report

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: ApiClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "run_triage",
        lambda **kwargs: pytest.fail("Local triage must not run after API success."),
    )

    result = run_selected_alert_triage(
        alert_key=alert_key,
        confidence_threshold=0.75,
        max_evidence_iterations=2,
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
    )

    assert result["report"] is report
    assert result["summary"]["transport"] == "api"
    assert result["summary"]["fallback_reason"] == ""
    assert result["summary"]["alert_key"] == alert_key
    assert captured == {
        "alert_key": alert_key,
        "confidence_threshold": 0.75,
        "max_evidence_iterations": 2,
        "manifest_s3_uri": "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
    }


def test_run_selected_alert_triage_falls_back_only_on_transport_failure(monkeypatch) -> None:
    """
    Ensure a network outage uses the same deterministic local triage settings.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    alert_key = "orders|dq_failure|2026-06-10|dq.raw_orders|row_count_positive|table"
    report    = build_streamlit_triage_report(alert_key=alert_key)
    captured: dict[str, object] = {}

    class UnavailableClient:
        """Transport-failing triage API test double."""

        def run_triage_report(self, **kwargs) -> SimpleNamespace:
            """Raise a controlled transport failure for every triage request."""
            raise ControlPlaneTransportError("api unavailable")

    def fake_local_triage(**kwargs) -> SimpleNamespace:
        """
        Capture deterministic local triage arguments.

        Args:
            **kwargs: Local LangGraph triage configuration.

        Returns:
            Stable report-like namespace.
        """
        captured.update(kwargs)

        return report

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: UnavailableClient(),
    )
    monkeypatch.setattr(streamlit_app, "run_triage", fake_local_triage)

    result = run_selected_alert_triage(
        alert_key=alert_key,
        confidence_threshold=0.75,
        max_evidence_iterations=2,
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
    )

    assert result["report"] is report
    assert result["summary"]["transport"] == "local"
    assert result["summary"]["fallback_reason"] == "control_plane_transport_unavailable"
    assert captured["alert_key"] == alert_key
    assert captured["confidence_threshold"] == 0.75
    assert captured["max_evidence_iterations"] == 2
    assert captured["config"].manifest_s3_uri == (
        "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json"
    )


def test_run_selected_alert_triage_records_unconfigured_local_mode(monkeypatch) -> None:
    """
    Ensure an intentionally unconfigured API remains observable in the result.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    report = build_streamlit_triage_report()

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(streamlit_app, "run_triage", lambda **kwargs: report)

    result = run_selected_alert_triage(
        alert_key=report.alert.alert_key,
        confidence_threshold=0.70,
        max_evidence_iterations=1,
        manifest_s3_uri="",
    )

    assert result["summary"]["transport"] == "local"
    assert result["summary"]["fallback_reason"] == "control_plane_api_not_configured"


def test_run_selected_alert_triage_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure malformed or identity-mismatched API reports stop UI triage.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    class RejectedClient:
        """Contract-rejecting triage API test double."""

        def run_triage_report(self, **kwargs) -> SimpleNamespace:
            """Raise a controlled report identity error."""
            raise ControlPlaneResponseError("triage report identity mismatch")

    monkeypatch.setattr(
        streamlit_app,
        "build_streamlit_control_plane_client",
        lambda **kwargs: RejectedClient(),
    )
    monkeypatch.setattr(
        streamlit_app,
        "run_triage",
        lambda **kwargs: pytest.fail("Response failures must not trigger local triage."),
    )

    with pytest.raises(ControlPlaneResponseError, match="triage report identity mismatch"):
        run_selected_alert_triage(
            alert_key="orders|requested-alert",
            confidence_threshold=0.75,
            max_evidence_iterations=2,
            manifest_s3_uri="",
        )


def test_build_llm_runtime_summary_returns_operator_friendly_metrics() -> None:
    """
    Validate Streamlit AI runtime cards use normalized audit metadata.

    Returns:
        None.
    """
    summary = build_llm_runtime_summary(
        audit_rows=[
            {
                "action": "llm_route_completed",
                "agent_run_id": "run-current",
                "llm_route": {
                    "agent_run_id": "run-current",
                    "runtime_mode": "heuristic_fallback",
                    "provider": "heuristic",
                    "model": "heuristic-v1",
                    "requested_route": "triage_reasoning",
                    "executed_route": "evidence_summary",
                    "input_tokens": 720,
                    "output_tokens": 407,
                    "estimated_cost_display": "$0.000000",
                    "duration_ms": 4583,
                    "fallback_summary": "OpenAI quota was unavailable.",
                },
            }
        ]
    )

    assert summary is not None
    assert summary["mode_label"] == "Heuristic fallback"
    assert summary["provider_model"] == "heuristic / heuristic-v1"
    assert summary["route_label"] == "triage_reasoning -> evidence_summary"
    assert summary["token_label"] == "720 in / 407 out"
    assert summary["route_count"] == 1
    assert summary["total_tokens"] == 1127
    assert summary["fallback_summaries"] == ["OpenAI quota was unavailable."]


def test_build_llm_runtime_summary_aggregates_only_latest_agent_run() -> None:
    """
    Ensure the operator summary combines route events without mixing runs.

    Returns:
        None.
    """
    summary = build_llm_runtime_summary(
        audit_rows=[
            {
                "action": "llm_route_completed",
                "agent_run_id": "run-new",
                "llm_route": {
                    "agent_run_id": "run-new",
                    "requested_route": "deep_reasoning",
                    "executed_route": "gemini_reasoning",
                    "provider": "gemini",
                    "model": "gemini-flash",
                    "input_tokens": 30,
                    "output_tokens": 12,
                    "estimated_cost_usd": 0.0002,
                    "duration_ms": 500,
                    "used_heuristic": False,
                },
            },
            {
                "action": "llm_route_completed",
                "agent_run_id": "run-new",
                "llm_route": {
                    "agent_run_id": "run-new",
                    "requested_route": "evidence_planning",
                    "executed_route": "gemini_reasoning",
                    "provider": "gemini",
                    "model": "gemini-flash",
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "estimated_cost_usd": 0.0001,
                    "duration_ms": 300,
                    "used_heuristic": False,
                },
            },
            {
                "action": "llm_route_completed",
                "agent_run_id": "run-old",
                "llm_route": {
                    "agent_run_id": "run-old",
                    "requested_route": "old_route",
                    "executed_route": "old_route",
                    "provider": "openai",
                    "model": "old-model",
                    "input_tokens": 900,
                    "output_tokens": 500,
                    "estimated_cost_usd": 1.0,
                    "duration_ms": 9000,
                    "used_heuristic": False,
                },
            },
        ]
    )

    assert summary is not None
    assert summary["route_count"] == 2
    assert summary["requested_routes"] == ["deep_reasoning", "evidence_planning"]
    assert summary["executed_routes"] == ["gemini_reasoning"]
    assert summary["providers"] == ["gemini"]
    assert summary["models"] == ["gemini-flash"]
    assert summary["has_external_model"] is True
    assert summary["has_heuristic"] is False
    assert summary["total_input_tokens"] == 50
    assert summary["total_output_tokens"] == 20
    assert summary["total_tokens"] == 70
    assert summary["total_estimated_cost_usd"] == pytest.approx(0.0003)
    assert summary["total_duration_ms"] == 800
    assert summary["estimated_cost_display"] == "$0.000300"


def test_summarize_alert_rows_counts_status_severity_and_reports() -> None:
    """
    Validate alert summary counts used by the Streamlit reliability overview.

    Returns:
        None.
    """
    alerts = [
        {
            "severity": "critical",
            "status": "open",
            "dt": "2026-06-10",
            "table_name": "dq.stg_orders",
            "report_s3_uri": "s3://dq-artifacts/report.md",
        },
        {
            "severity": "warning",
            "status": "triaged",
            "dt": "2026-06-10",
            "table_name": "dq.fct_orders_daily",
            "report_s3_uri": "",
        },
    ]

    summary = summarize_alert_rows(alerts)

    assert summary["total_alerts"] == 2
    assert summary["critical_count"] == 1
    assert summary["warning_count"] == 1
    assert summary["open_count"] == 1
    assert summary["affected_table_count"] == 2
    assert summary["report_count"] == 1
    assert summary["affected_dates"] == ["2026-06-10"]


def test_summarize_daily_quality_payload_normalizes_operator_metrics() -> None:
    """
    Validate stable metric names for daily check and alert aggregates.

    Returns:
        None.
    """
    summary = summarize_daily_quality_payload(
        {
            "dt": "2026-06-10",
            "check_counts": [
                {"status": "pass", "count": 8},
                {"status": "warn", "count": 2},
                {"status": "fail", "count": 1},
                {"status": "skip", "count": 1},
            ],
            "alert_counts": [
                {"severity": "critical", "count": 1},
                {"severity": "warning", "count": 2},
            ],
            "total_checks": 12,
            "total_open_alerts": 3,
        }
    )

    assert summary == {
        "total_checks": 12,
        "passed_checks": 8,
        "warning_checks": 2,
        "failed_checks": 1,
        "skipped_checks": 1,
        "total_open_alerts": 3,
        "critical_alerts": 1,
        "warning_alerts": 2,
    }


def test_build_reliability_kpis_returns_exactly_four_incident_first_metrics() -> None:
    """
    Validate the header stays compact and prioritizes failure and incident load.

    Returns:
        None.
    """
    metrics = build_reliability_kpis(
        alerts=[
            {"severity": "critical", "status": "open"},
            {"severity": "warning", "status": "triaged"},
        ],
        daily_summary={
            "check_counts": [
                {"status": "pass", "count": 8},
                {"status": "fail", "count": 2},
            ],
            "alert_counts": [{"severity": "critical", "count": 1}],
            "total_checks": 10,
            "total_open_alerts": 1,
        },
    )

    assert metrics == [
        {"label": "Daily Checks", "value": 10},
        {"label": "Failed Checks", "value": 2},
        {"label": "Active Incidents", "value": 2},
        {"label": "Critical Incidents", "value": 1},
    ]


def test_build_reliability_kpis_marks_missing_snapshot_unavailable() -> None:
    """
    Ensure missing daily data is not rendered as a healthy zero.

    Returns:
        None.
    """
    metrics = build_reliability_kpis(alerts=[], daily_summary=None)

    assert metrics[0]["value"] == "Unavailable"
    assert metrics[1]["value"] == "Unavailable"
    assert len(metrics) == 4


def test_resolve_alert_date_filter_uses_latest_resolved_business_date() -> None:
    """
    Ensure latest-date mode can still constrain incidents after resolution.

    Returns:
        None.
    """
    assert resolve_alert_date_filter(
        restrict_to_summary_date=True,
        requested_summary_dt=None,
        daily_summary={"dt": "2026-06-10"},
    ) == "2026-06-10"
    assert resolve_alert_date_filter(
        restrict_to_summary_date=False,
        requested_summary_dt="2026-06-09",
        daily_summary={"dt": "2026-06-10"},
    ) is None


def test_build_incident_option_rows_hides_system_alert_key() -> None:
    """
    Validate operator selectors prefer Alert Ref over long system identities.

    Returns:
        None.
    """
    alert = {
        "alert_display_id": "DQ-20260610-A1B2C3",
        "alert_key": "orders|dq_failure|long-system-key",
        "severity": "critical",
        "table_name": "dq.raw_orders",
        "metric": "row_count",
        "dt": "2026-06-10",
    }
    options = build_incident_option_rows([alert])
    label   = next(iter(options))

    assert "DQ-20260610-A1B2C3" in label
    assert "CRITICAL" in label
    assert "long-system-key" not in label
    assert options[label] is alert


def test_classify_reliability_state_marks_critical_attention() -> None:
    """
    Validate critical alert classification for the reliability overview.

    Returns:
        None.
    """
    state = classify_reliability_state(
        {
            "critical_count": 1,
            "warning_count": 0,
            "open_count": 1,
        }
    )

    assert state["label"] == "Critical attention required"
    assert state["css_class"] == "dq-health-critical"


def test_classify_reliability_state_uses_failed_daily_checks_without_loaded_alerts() -> None:
    """
    Ensure an empty alert filter cannot hide a failed deterministic DQ check.

    Returns:
        None.
    """
    state = classify_reliability_state(
        summary=summarize_alert_rows([]),
        daily_summary={
            "dt": "2026-06-10",
            "check_counts": [{"status": "fail", "count": 1}],
            "alert_counts": [],
            "total_checks": 1,
            "total_open_alerts": 0,
        },
    )

    assert state["label"] == "Critical attention required"
    assert state["css_class"] == "dq-health-critical"


def test_classify_reliability_state_marks_warning_watchlist() -> None:
    """
    Validate warning/open alert classification for the reliability overview.

    Returns:
        None.
    """
    state = classify_reliability_state(
        {
            "critical_count": 0,
            "warning_count": 1,
            "open_count": 1,
        }
    )

    assert state["label"] == "Warning watchlist"
    assert state["css_class"] == "dq-health-warning"


def test_classify_reliability_state_keeps_triaged_info_incident_visible() -> None:
    """
    Ensure active non-warning incidents do not make the header look stable.

    Returns:
        None.
    """
    state = classify_reliability_state(
        summarize_alert_rows(
            [{"severity": "info", "status": "triaged"}]
        )
    )

    assert state["label"] == "Warning watchlist"
    assert state["css_class"] == "dq-health-warning"


def test_classify_reliability_state_fails_closed_when_daily_summary_is_unavailable() -> None:
    """
    Ensure a missing daily snapshot is never described as a healthy state.

    Returns:
        None.
    """
    state = classify_reliability_state(
        summary=summarize_alert_rows([]),
        summary_error="ControlPlaneResponseError: invalid response",
    )

    assert state["label"] == "Daily quality status unavailable"
    assert state["css_class"] == "dq-health-warning"


def test_classify_reliability_state_does_not_treat_missing_checks_as_stable() -> None:
    """
    Ensure a date without executed DQ checks remains visibly unverified.

    Returns:
        None.
    """
    state = classify_reliability_state(
        summary=summarize_alert_rows([]),
        daily_summary={
            "dt": "2026-06-11",
            "check_counts": [],
            "alert_counts": [],
            "total_checks": 0,
            "total_open_alerts": 0,
        },
    )

    assert state["label"] == "No quality data yet"
    assert state["css_class"] == "dq-health-warning"


def test_classify_reliability_state_marks_stable_filters() -> None:
    """
    Validate stable classification when no alert requires attention.

    Returns:
        None.
    """
    summary = summarize_alert_rows([])
    state   = classify_reliability_state(summary, daily_summary={
        "availability": "available",
        "dt": "2026-10-16",
        "check_counts": [{"status": "pass", "count": 1}],
        "alert_counts": [],
        "total_checks": 1,
        "total_open_alerts": 0,
    })

    assert state["label"] == "Stable for selected date and filters"
    assert state["css_class"] == "dq-health-stable"


@pytest.mark.parametrize("dt", [None, "2026-10-16"])
def test_daily_empty_summary_has_zero_kpis_but_never_stable(dt) -> None:
    payload = {
        "status": "success", "availability": "empty", "dt": dt,
        "check_counts": [], "alert_counts": [], "total_checks": 0, "total_open_alerts": 0,
    }
    state = classify_reliability_state(summarize_alert_rows([]), daily_summary=payload)
    metrics = build_reliability_kpis([], payload)

    assert state["label"] == "No quality data yet"
    assert state["css_class"] != "dq-health-stable"
    assert [item["value"] for item in metrics] == [0, 0, 0, 0]


def test_no_daily_snapshot_cannot_be_classified_stable() -> None:
    state = classify_reliability_state(summarize_alert_rows([]))

    assert state["label"] == "No quality data yet"
    assert state["css_class"] != "dq-health-stable"


@pytest.mark.parametrize("severity,label", [
    ("critical", "Critical attention required"), ("warning", "Warning watchlist"),
])
@pytest.mark.parametrize("incident_source", ["loaded", "daily"])
def test_active_incident_severity_outranks_zero_daily_checks(severity, label, incident_source) -> None:
    payload = {
        "availability": "empty" if incident_source == "loaded" else "available",
        "dt": "2026-10-16", "check_counts": [],
        "alert_counts": [] if incident_source == "loaded" else [{"severity": severity, "count": 1}],
        "total_checks": 0, "total_open_alerts": 0 if incident_source == "loaded" else 1,
    }
    alerts = [{"severity": severity, "status": "triaged"}] if incident_source == "loaded" else []
    state = classify_reliability_state(summarize_alert_rows(alerts), daily_summary=payload)

    assert state["label"] == label


def sample_copilot_triage_result(alert_key: str) -> dict[str, object]:
    """
    Build a compact matching triage result for Streamlit Copilot tests.

    Args:
        alert_key: Stable alert key stored in the result summary.

    Returns:
        Triage result dictionary with report-like test doubles.
    """
    hypothesis = SimpleNamespace(
        title="Missing partition",
        recommended_action="Prepare an approval-gated backfill preview.",
    )
    evidence = SimpleNamespace(
        tool_name="clickhouse_sql",
        evidence_type="sql_result",
        summary="The selected partition contains zero rows.",
        row_count=1,
        s3_uri="",
    )
    report = SimpleNamespace(
        summary="The selected partition is missing.",
        impact="Daily reporting may be incomplete.",
        top_hypothesis=hypothesis,
        confidence=0.91,
        recommended_actions=["Validate upstream landing data."],
        approval_gated_actions=[{"action_type": "backfill"}],
        report_id="RPT-TEST01",
        json_report_s3_uri="s3://dq-artifacts/agent-reports/report.json",
        evidence=[evidence],
    )

    return {
        "summary": {"alert_key": alert_key},
        "report": report,
    }


def test_matching_triage_result_rejects_stale_alert_context() -> None:
    """
    Validate that session-state triage context cannot leak between selected alerts.

    Returns:
        None.
    """
    latest = sample_copilot_triage_result("orders|old-alert")

    assert matching_triage_result("orders|new-alert", latest) is None


def test_build_ui_copilot_context_uses_matching_report_and_evidence() -> None:
    """
    Validate report, evidence, and audit context for the selected alert.

    Returns:
        None.
    """
    alert_key = "orders|matching-alert"
    context   = build_ui_copilot_context(
        alert={"alert_key": alert_key},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
    )

    assert context["has_report"] is True
    assert context["report_context"]["report_id"] == "RPT-TEST01"
    assert context["report_context"]["approval_required"] is True
    assert context["evidence_count"] == 1
    assert context["audit_count"] == 1


def test_answer_ui_copilot_question_delegates_to_shared_service(monkeypatch) -> None:
    """
    Validate that Streamlit reuses the shared Copilot narrative service.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_build_operator_answer(**kwargs) -> str:
        """
        Capture shared service arguments without calling an external provider.

        Args:
            kwargs: Copilot context keyword arguments.

        Returns:
            Fixed operator answer.
        """
        captured.update(kwargs)

        return "Grounded operator answer."

    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        fake_build_operator_answer,
    )

    alert_key = "orders|matching-alert"
    result    = answer_ui_copilot_question(
        question="Summarize evidence.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
        api_base_url="",
    )

    assert result["answer"] == "Grounded operator answer."
    assert captured["question"] == "Summarize evidence."
    assert len(captured["evidence_rows"]) == 1
    assert len(captured["audit_rows"]) == 1
    assert result["transport"] == "local"


def test_build_copilot_presentation_prioritizes_answer_and_compact_provenance() -> None:
    """
    Validate successful external AI output stays concise and warning-free.

    Returns:
        None.
    """
    presentation = build_copilot_presentation(
        {
            "answer": "The raw partition is empty, so the daily mart may be incomplete.",
            "has_report": True,
            "evidence_count": 3,
            "incident_history_count": 1,
            "transport": "api",
            "fallback_reason": "",
            "llm_runtime": {
                "provider": "gemini",
                "model": "gemini-flash",
                "runtime_mode": "external_model",
                "used_heuristic": False,
            },
        }
    )

    assert presentation["answer"].startswith("The raw partition")
    assert presentation["provenance"] == "gemini / gemini-flash | 3 evidence | 1 prior investigations"
    assert presentation["show_fallback"] is False
    assert presentation["fallback_message"] == ""
    assert presentation["has_report"] is True


def test_build_copilot_presentation_warns_only_for_actual_fallback() -> None:
    """
    Validate heuristic or transport fallback is explicit without repeating guardrails.

    Returns:
        None.
    """
    heuristic = build_copilot_presentation(
        {
            "answer": "Bounded fallback answer.",
            "transport": "api",
            "llm_runtime": {
                "provider": "heuristic",
                "model": "heuristic-v1",
                "runtime_mode": "heuristic_fallback",
                "used_heuristic": True,
                "fallback_summary": "Gemini was unavailable.",
            },
        }
    )
    normal_local = build_copilot_presentation(
        {
            "answer": "Local answer.",
            "transport": "local",
            "fallback_reason": "",
            "llm_runtime": {},
        }
    )

    assert heuristic["show_fallback"] is True
    assert heuristic["fallback_message"] == "Gemini was unavailable."
    assert normal_local["show_fallback"] is False
    assert normal_local["fallback_message"] == ""

def test_request_ui_copilot_api_sends_only_alert_key_question_and_report_uri(monkeypatch) -> None:
    """
    Validate Streamlit sends references through the reusable control-plane client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    def fake_answer_copilot(self, **kwargs) -> dict[str, object]:
        """
        Capture the client request without network access.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Returns:
            Typed Copilot response payload.
        """
        captured.update(
            {
                "base_url": self.base_url,
                "timeout": self.timeout_seconds,
                **kwargs,
            }
        )

        return {
            "agent_run_id": "11111111-1111-1111-1111-111111111111",
            "alert_key": "orders|matching-alert",
            "answer": "API-grounded answer.",
            "context_source": "alert_report_audit",
            "evidence_count": 2,
            "audit_count": 3,
            "incident_history_count": 2,
        }

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        fake_answer_copilot,
    )

    alert_key = "orders|matching-alert"
    result    = request_ui_copilot_api(
        question="Explain the evidence.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        api_base_url="http://api:8000",
        timeout_seconds=10,
    )

    assert result["transport"] == "api"
    assert result["has_report"] is True
    assert result["evidence_count"] == 2
    assert result["audit_count"] == 3
    assert result["incident_history_count"] == 2
    assert captured == {
        "base_url": "http://api:8000",
        "timeout": 10,
        "question": "Explain the evidence.",
        "alert_key": alert_key,
        "report_json_s3_uri": "s3://dq-artifacts/agent-reports/report.json",
        "audit_limit": 10,
        "language": "en",
    }


def test_answer_ui_copilot_question_falls_back_when_api_transport_is_unavailable(monkeypatch) -> None:
    """
    Validate explicit local fallback without weakening evidence boundaries.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def raise_transport_error(self, **kwargs) -> dict[str, object]:
        """
        Simulate an unavailable control-plane API.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Raises:
            ControlPlaneTransportError: Always.
        """
        raise ControlPlaneTransportError("api unavailable")

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        raise_transport_error,
    )
    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        lambda **kwargs: "Shared local fallback answer.",
    )

    alert_key = "orders|matching-alert"
    result    = answer_ui_copilot_question(
        question="Explain this alert.",
        alert={"alert_key": alert_key, "alert_display_id": "DQ-TEST01"},
        latest_triage_result=sample_copilot_triage_result(alert_key),
        audit_rows=[{"action": "triage_completed", "status": "success"}],
        api_base_url="http://api:8000",
        api_timeout=1,
    )

    assert result["transport"] == "local"
    assert result["answer"] == "Shared local fallback answer."
    assert "ControlPlaneTransportError" in result["fallback_reason"]
    assert result["has_report"] is True


def test_answer_ui_copilot_question_does_not_hide_response_failure(monkeypatch) -> None:
    """
    Ensure contract failures remain visible instead of returning local content.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    def raise_response_error(self, **kwargs) -> dict[str, object]:
        """
        Simulate a malformed or rejected API response.

        Args:
            self: ControlPlaneClient instance.
            kwargs: Copilot request keyword arguments.

        Raises:
            ControlPlaneResponseError: Always.
        """
        raise ControlPlaneResponseError("invalid API response")

    monkeypatch.setattr(
        streamlit_app.ControlPlaneClient,
        "answer_copilot",
        raise_response_error,
    )
    monkeypatch.setattr(
        streamlit_app.copilot_service,
        "build_operator_answer",
        lambda **kwargs: pytest.fail("Response errors must not trigger local fallback."),
    )

    with pytest.raises(ControlPlaneResponseError, match="invalid API response"):
        answer_ui_copilot_question(
            question="Explain this alert.",
            alert={
                "alert_key": "orders|matching-alert",
                "alert_display_id": "DQ-TEST01",
            },
            latest_triage_result=None,
            audit_rows=[],
            api_base_url="http://api:8000",
        )


# --- Defining Blast Radius Tests
def test_request_ui_dbt_blast_radius_uses_shared_bounded_client(monkeypatch) -> None:
    """
    Ensure Streamlit requests impact through the shared API client without local traversal.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal blast-radius control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def get_dbt_blast_radius(self, **kwargs) -> dict[str, object]:
            """
            Capture bounded request values and return sanitized impact.

            Args:
                **kwargs: Blast-radius request values.

            Returns:
                Deterministic impact response.
            """
            captured["request"] = kwargs

            return {
                "table_name": kwargs["table_name"],
                "matched": True,
                "node": {"unique_id": "model.project.fct_orders_daily"},
                "manifest_source": kwargs["manifest_s3_uri"],
                "max_depth": kwargs["max_depth"],
                "max_nodes": kwargs["max_nodes"],
                "max_depth_reached": 1,
                "truncated": False,
                "total_impacted_nodes": 1,
                "impacted_asset_count": 1,
                "impacted_test_count": 0,
                "unresolved_node_count": 0,
                "resource_type_counts": {"model": 1},
                "impacted_assets": [
                    {
                        "unique_id": "model.project.weekly_orders",
                        "resource_type": "model",
                        "name": "weekly_orders",
                        "depth": 1,
                        "lineage_path": [
                            "model.project.fct_orders_daily",
                            "model.project.weekly_orders",
                        ],
                    }
                ],
                "impacted_tests": [],
                "unresolved_nodes": [],
                "summary": "One downstream asset is affected.",
            }

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = request_ui_dbt_blast_radius(
        table_name="dq.fct_orders_daily",
        manifest_s3_uri="s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        max_depth=4,
        max_nodes=80,
        api_base_url="http://api:8000/",
        api_timeout=9,
    )

    assert result["transport"] == "api"
    assert result["table_name"] == "dq.fct_orders_daily"
    assert captured["client"] == {
        "base_url": "http://api:8000",
        "timeout_seconds": 9,
    }
    assert captured["request"] == {
        "table_name": "dq.fct_orders_daily",
        "manifest_s3_uri": "s3://dq-artifacts/dbt-artifacts/orders/latest/manifest.json",
        "max_depth": 4,
        "max_nodes": 80,
    }


def test_request_ui_dbt_blast_radius_requires_control_plane_api() -> None:
    """
    Ensure the UI does not bypass the shared API boundary when it is unavailable.

    Returns:
        None.
    """
    with pytest.raises(ValueError, match="CONTROL_PLANE_API_URL"):
        request_ui_dbt_blast_radius(
            table_name="dq.fct_orders_daily",
            api_base_url="",
        )


def test_matching_blast_radius_result_rejects_stale_table_context() -> None:
    """
    Ensure impact state from a previously selected table is not rendered as current evidence.

    Returns:
        None.
    """
    result = {
        "table_name": "dq.raw_orders",
        "matched": True,
    }

    assert matching_blast_radius_result("dq.fct_orders_daily", result) is None
    assert matching_blast_radius_result("dq.raw_orders", result) == result


def test_build_blast_radius_display_rows_uses_human_readable_path() -> None:
    """
    Ensure dbt unique identifiers become concise operator-facing lineage paths.

    Returns:
        None.
    """
    rows = build_blast_radius_display_rows(
        [
            {
                "unique_id": "model.project.fct_orders_daily",
                "resource_type": "model",
                "name": "fct_orders_daily",
                "relation_name": "dq.fct_orders_daily",
                "depth": 2,
                "lineage_path": [
                    "source.project.raw_orders",
                    "model.project.stg_orders",
                    "model.project.fct_orders_daily",
                ],
            }
        ]
    )

    assert rows == [
        {
            "Depth": 2,
            "Asset": "fct_orders_daily",
            "Type": "model",
            "Relation": "dq.fct_orders_daily",
            "Lineage Path": "raw_orders -> stg_orders -> fct_orders_daily",
        }
    ]


# --- Defining Checkpoint Recovery UI Tests
def test_checkpoint_ui_helpers_use_shared_api_without_execution_credentials(monkeypatch) -> None:
    """
    Ensure Streamlit history and preview calls stay inside the read-only API boundary.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal checkpoint-aware control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """Capture client construction without approval credentials."""
            captured.setdefault("clients", []).append(kwargs)

        def get_checkpoint_history(self, **kwargs) -> dict[str, object]:
            """Capture history input and return sanitized metadata."""
            captured["history_request"] = kwargs

            return {
                "history_count": 1,
                "matching_checkpoint_count": 1,
                "selected_checkpoint": {"checkpoint_id": "checkpoint-001"},
                "history": [{"checkpoint_id": "checkpoint-001"}],
            }

        def preview_checkpoint_replay(self, **kwargs) -> dict[str, object]:
            """Capture replay input and return a non-executing preview."""
            captured["preview_request"] = kwargs

            return {
                "replay_request_id": "replay-0123456789abcdef",
                "airflow_triggered": False,
                "side_effects_executed": False,
            }

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    history = request_ui_checkpoint_history(
        checkpoint_namespace="manual__triage_source",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        api_base_url="http://api:8000",
        api_timeout=9,
    )
    preview = request_ui_checkpoint_replay_preview(
        checkpoint_namespace="manual__triage_source",
        checkpoint_id="checkpoint-001",
        alert_key="orders|dq_failure|2026-08-28|dq.raw_orders|row_count_positive|table",
        api_base_url="http://api:8000",
        api_timeout=9,
    )

    assert history["history_count"] == 1
    assert preview["airflow_triggered"] is False
    assert captured["history_request"]["history_next_node"] == "store_report"
    assert captured["preview_request"]["checkpoint_id"] == "checkpoint-001"
    assert all("approval_token" not in client for client in captured["clients"])


def test_checkpoint_history_display_rows_hide_raw_graph_state() -> None:
    """
    Ensure operator tables contain only sanitized checkpoint metadata.

    Returns:
        None.
    """
    rows = build_checkpoint_history_display_rows(
        [
            {
                "checkpoint_id": "checkpoint-001",
                "created_at": "2026-08-28T04:23:30Z",
                "step": 8,
                "source": "loop",
                "next_nodes": ["store_report"],
                "is_complete": False,
            }
        ]
    )

    assert rows == [
        {
            "Created At": "2026-08-28T04:23:30Z",
            "Step": 8,
            "Source": "Loop",
            "Pending Nodes": "store_report",
            "Complete": False,
            "Checkpoint ID": "checkpoint-001",
        }
    ]
    assert "values" not in rows[0]


# --- Defining Durable Approval Tests
def test_build_ui_backfill_approval_payload_uses_dispatcher_and_strips_control_fields() -> None:
    """
    Ensure triage actions become bounded approval API payloads without duplicate control fields.

    Returns:
        None.
    """
    payload = build_ui_backfill_approval_payload(
        alert={
            "alert_id": "11111111-1111-1111-1111-111111111111",
            "alert_key": "orders|matching-alert",
        },
        action={
            "action_type": "backfill",
            "reason": "Evidence indicates a missing partition.",
            "target_dag_id": "90_dag_dq_platform_backfill_dispatcher",
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "parameters": {
                "target_dag_id": "00_dag_dq_platform_daily_orchestrator",
                "run_mode": "backfill",
                "run_triage": False,
                "requested_by": "agentic_triage",
                "reason": "Missing partition",
            },
        },
        requested_by="streamlit_operator",
        agent_run_id="22222222-2222-2222-2222-222222222222",
    )

    assert payload["target_dag_id"] == "00_dag_dq_platform_daily_orchestrator"
    assert payload["requested_by"] == "streamlit_operator"
    assert payload["parameters"] == {"run_mode": "backfill", "run_triage": False}
    assert "target_dag_id" not in payload["parameters"]
    assert "requested_by" not in payload["parameters"]


def test_create_ui_backfill_approval_uses_shared_client_without_local_mutation(monkeypatch) -> None:
    """
    Ensure Streamlit creates approval state through FastAPI rather than ClickHouse directly.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal approval-aware control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def create_approval_request(self, **kwargs) -> dict[str, object]:
            """
            Capture approval creation arguments.

            Args:
                **kwargs: Approval request values.

            Returns:
                Pending approval response.
            """
            captured["request"] = kwargs
            return {"request_id": "APR-20260610-A1B2C3D4", "status": "pending"}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = create_ui_backfill_approval_request(
        alert={"alert_key": "orders|matching-alert", "alert_display_id": "DQ-TEST01"},
        action={
            "action_type": "backfill",
            "reason": "Evidence indicates a missing partition.",
            "target_dag_id": "90_dag_dq_platform_backfill_dispatcher",
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "parameters": {"target_dag_id": "00_dag_dq_platform_daily_orchestrator"},
        },
        requested_by="streamlit_operator",
        api_base_url="http://api:8000",
        approval_token="approval-token",
    )

    assert result["status"] == "pending"
    assert captured["client"] == {
        "base_url": "http://api:8000",
        "timeout_seconds": streamlit_app.COPILOT_API_TIMEOUT,
        "approval_token": "approval-token",
    }
    assert captured["request"]["alert_key"] == "orders|matching-alert"


def test_decide_ui_approval_uses_shared_client(monkeypatch) -> None:
    """
    Ensure Streamlit decisions remain API-bound and non-executing.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}

    class FakeClient:
        """Minimal decision-capable control-plane client double."""

        def __init__(self, **kwargs) -> None:
            """Accept constructor values without network setup."""

        def decide_approval_request(self, **kwargs) -> dict[str, object]:
            """
            Capture one decision call.

            Args:
                **kwargs: Decision values.

            Returns:
                Approved state.
            """
            captured.update(kwargs)
            return {"request_id": kwargs["request_id"], "status": "approved"}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = decide_ui_approval_request(
        request_id="APR-20260610-A1B2C3D4",
        decision="approve",
        decided_by="streamlit_operator",
        comment="Reviewed scope.",
        api_base_url="http://api:8000",
        approval_token="approval-token",
    )

    assert result["status"] == "approved"
    assert captured["decision"] == "approve"
    assert captured["decided_by"] == "streamlit_operator"


def test_synchronize_ui_incident_card_is_bounded_and_best_effort(monkeypatch) -> None:
    """Refresh the Discord card from approval state without raising into the UI."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        streamlit_app,
        "synchronize_incident_message",
        lambda **kwargs: captured.update(kwargs),
    )

    synchronize_ui_incident_card(
        {
            "request_id": "APR-20260610-A1B2C3D4",
            "alert_key": "orders|matching-alert",
            "status": "approved",
        }
    )

    assert captured == {
        "alert_key": "orders|matching-alert",
        "approval_state": "approved",
    }


def test_synchronize_ui_incident_card_skips_unlinked_approval(monkeypatch) -> None:
    """A standalone approval must not invent an incident relationship."""
    monkeypatch.setattr(
        streamlit_app,
        "synchronize_incident_message",
        lambda **kwargs: pytest.fail("Unlinked approvals must not call Discord."),
    )

    synchronize_ui_incident_card({"request_id": "APR-STANDALONE", "status": "pending"})

def test_summarize_approval_queue_rows_reports_decision_and_execution_counts() -> None:
    """
    Ensure Approval Queue metrics separate human decisions from execution lifecycle.

    Returns:
        None.
    """
    summary = summarize_approval_queue_rows(
        [
            {"status": "pending", "execution_status": "not_started"},
            {"status": "approved", "execution_status": "dispatching"},
            {"status": "approved", "execution_status": "dispatched"},
            {"status": "approved", "execution_status": "failed"},
            {"status": "rejected", "execution_status": "not_started"},
            {"status": "cancelled", "execution_status": "not_started"},
        ]
    )

    assert summary == {
        "pending": 1,
        "approved": 3,
        "cancelled": 1,
        "active_executions": 2,
        "failed_executions": 1,
    }


def test_load_approval_queue_rows_uses_read_only_shared_api(monkeypatch) -> None:
    """
    Ensure Streamlit Approval Queue reads latest states through ControlPlaneClient.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    rows = [
        {
            "request_id": "APR-20260610-A1B2C3D4",
            "status": "approved",
            "execution_status": "dispatched",
        }
    ]

    class FakeClient:
        """Minimal approval queue client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture read-only client configuration.

            Args:
                **kwargs: Constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def list_approval_requests(self, status, limit: int) -> dict[str, object]:
            """
            Return deterministic approval rows.

            Args:
                status: Optional approval filter.
                limit: Maximum rows.

            Returns:
                Queue response.
            """
            captured["status"] = status
            captured["limit"]  = limit
            return {"status": "success", "row_count": len(rows), "rows": rows}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)

    result = load_approval_queue_rows(
        status="approved",
        limit=10,
        api_base_url="http://api:8000",
    )

    assert result == rows
    assert captured["status"] == "approved"
    assert captured["limit"] == 10
    assert "approval_token" not in captured["client"]


def test_summarize_life_evaluation_rows_reports_reliability_states() -> None:
    """
    Ensure Streamlit LIFE cards separate pass, review, fail, and malformed results.

    Returns:
        None.
    """
    summary = summarize_life_evaluation_rows(
        [
            {"eval_status": "pass", "payload_valid": True, "requires_human_approval": False},
            {"eval_status": "review", "payload_valid": True, "requires_human_approval": True},
            {"eval_status": "fail", "payload_valid": True, "requires_human_approval": True},
            {"eval_status": "unknown", "payload_valid": False, "requires_human_approval": False},
        ]
    )

    assert summary == {
        "total": 4,
        "pass": 1,
        "review": 1,
        "fail": 1,
        "malformed": 1,
        "approval_required": 2,
    }


def test_load_life_evaluation_rows_uses_read_only_shared_api(monkeypatch) -> None:
    """
    Ensure Streamlit reads LIFE history through the shared control-plane client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    rows = [
        {
            "run_id": "life-eval-20260807T010203",
            "scenario_id": "missing_latest_day",
            "eval_status": "review",
            "payload_valid": True,
        }
    ]

    class FakeClient:
        """
        Minimal read-only LIFE history client double.
        """

        def __init__(self, **kwargs) -> None:
            """
            Capture client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def list_life_evaluations(self, **kwargs) -> dict[str, object]:
            """
            Capture history filters and return deterministic rows.

            Args:
                **kwargs: LIFE history filter values.

            Returns:
                Sanitized history response.
            """
            captured["request"] = kwargs

            return {"status": "success", "row_count": len(rows), "rows": rows}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)
    load_life_evaluation_rows.clear()

    result = load_life_evaluation_rows(
        eval_status="review",
        lookback_days=14,
        limit=5,
        api_base_url="http://api-life-test:8000",
    )

    assert result == rows
    assert captured["client"] == {
        "base_url": "http://api-life-test:8000",
        "timeout_seconds": streamlit_app.COPILOT_API_TIMEOUT,
    }
    assert captured["request"] == {
        "eval_status": "review",
        "lookback_days": 14,
        "limit": 5,
    }


def test_summarize_incident_history_rows_reports_operator_metrics() -> None:
    """
    Ensure previous-investigation cards summarize status, evidence, and approvals.

    Returns:
        None.
    """
    summary = summarize_incident_history_rows(
        [
            {
                "outcome_status": "success",
                "requires_human_approval": False,
                "approval_state": "not_required",
                "evidence_reference_count": 3,
                "report_s3_uri": "s3://dq-artifacts/report-one.md",
            },
            {
                "outcome_status": "partial",
                "requires_human_approval": True,
                "approval_state": "pending",
                "evidence_reference_count": 2,
                "report_s3_uri": "",
            },
        ]
    )

    assert summary == {
        "total": 2,
        "successful": 1,
        "approval_required": 1,
        "reports": 1,
        "evidence_references": 5,
    }


def test_load_incident_history_rows_uses_read_only_shared_api(monkeypatch) -> None:
    """
    Ensure Streamlit loads exact incident history without carrying approval credentials.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        None.
    """
    captured: dict[str, object] = {}
    rows = [
        {
            "alert_display_id": "DQ-20260513-764959",
            "outcome_status": "success",
            "evidence_reference_count": 1,
        }
    ]

    class FakeClient:
        """Minimal read-only incident-history client double."""

        def __init__(self, **kwargs) -> None:
            """
            Capture shared client configuration.

            Args:
                **kwargs: ControlPlaneClient constructor values.

            Returns:
                None.
            """
            captured["client"] = kwargs

        def get_incident_history(self, **kwargs) -> dict[str, object]:
            """
            Capture exact history filters and return deterministic rows.

            Args:
                **kwargs: Incident-history request values.

            Returns:
                Sanitized incident-history response.
            """
            captured["request"] = kwargs

            return {"status": "success", "row_count": len(rows), "rows": rows}

    monkeypatch.setattr(streamlit_app, "ControlPlaneClient", FakeClient)
    load_incident_history_rows.clear()

    result = load_incident_history_rows(
        alert_reference="DQ-20260513-764959",
        lookback_days=30,
        limit=5,
        api_base_url="http://api-history-test:8000",
    )

    assert result == rows
    assert captured["client"] == {
        "base_url": "http://api-history-test:8000",
        "timeout_seconds": streamlit_app.COPILOT_API_TIMEOUT,
    }
    assert captured["request"] == {
        "alert_reference": "DQ-20260513-764959",
        "lookback_days": 30,
        "limit": 5,
    }
    assert "approval_token" not in captured["client"]

    load_incident_history_rows.clear()


def test_build_incident_history_display_rows_hides_internal_identity() -> None:
    """
    Ensure the main UI table prioritizes human labels over internal memory keys.

    Returns:
        None.
    """
    display_rows = build_incident_history_display_rows(
        [
            {
                "memory_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "parent_run_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "alert_key": "orders|dq_failure|long-system-key",
                "alert_display_id": "DQ-20260513-764959",
                "recorded_at": "2026-08-20T13:37:00Z",
                "report_id": "RPT-27BDC120",
                "outcome_status": "success",
                "top_hypothesis_category": "missing_segment",
                "confidence": 0.72,
                "evidence_reference_count": 3,
                "approval_state": "not_required",
                "report_s3_uri": "s3://dq-artifacts/report.md",
            }
        ]
    )
    row = display_rows[0]

    assert row["Alert Ref"] == "DQ-20260513-764959"
    assert row["Report Ref"] == "RPT-27BDC120"
    assert row["Status"] == "Success"
    assert row["Likely Cause"] == "Missing Segment"
    assert "memory_id" not in row
    assert "parent_run_id" not in row
    assert "alert_key" not in row


def test_streamlit_guided_prompts_include_prior_investigation_review() -> None:
    """
    Ensure the UI exposes one history-grounded Copilot task.

    Returns:
        None.
    """
    prompt = streamlit_app.COPILOT_GUIDED_PROMPTS["Review prior investigations"]

    assert "investigated before" in prompt.lower()
    assert "current evidence" in prompt.lower()
