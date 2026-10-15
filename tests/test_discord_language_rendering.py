"""Local renderer feedback only; no Discord, Airflow, warehouse or model calls."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from apps.common.control_plane import ControlPlaneTransportError
from apps.common.i18n import current_language, language_context, translate
from apps.common.locales.discord import MESSAGES
from apps.discord_bot import formatters as f
from apps.discord_bot import interactions, preferences
from apps.discord_bot import resolution as r
from apps.discord_bot import workspace


REF = "DQ-20261005-3B55A6"
REQUEST_ID = "12345678-1234-1234-1234-123456789012"
WORKSPACE = "https://example.test/workspace"
RAW = "Pemeriksaan lolos. Masalah pada incident ini tidak ditemukan dalam pemeriksaan terbaru."


@pytest.fixture(autouse=True)
def isolated_presentation_environment(monkeypatch):
    """Never read the live preference store or inherit a configured workspace URL."""
    monkeypatch.setattr(preferences, "get_user_language", Mock(return_value="en"))
    monkeypatch.delenv("DISCORD_OPERATOR_UI_URL", raising=False)


def alert(status="open"):
    """Return original technical facts, not localized or persisted content."""
    return {
        "alert_key": "orders|dq_failure|2026-10-05|dq.raw_orders|row_count_positive|table",
        "alert_display_id": REF, "status": status, "severity": "critical",
        "table_name": "dq.raw_orders", "dt": "2026-10-05", "metric": "row_count_positive",
        "observed_value": 0, "expected_value": 1, "details": {},
    }


def report():
    return {
        "alert": alert(), "summary": RAW, "confidence": 0.75,
        "top_hypothesis": {"title": RAW, "description": RAW, "recommended_action": RAW},
        "evidence": [{"summary": RAW}], "impact": RAW,
        "report_id": "RPT-ABC123", "agent_run_id": "run-original",
        "markdown_report_s3_uri": "s3://reports/original.md",
    }


def request(status="pass", summary=RAW):
    now = datetime.now(timezone.utc)
    return {
        "request_id": REQUEST_ID, "alert_ref": REF, "incident_revision": 4, "status": status,
        "finished_at": (now - timedelta(seconds=10)).isoformat(),
        "expires_at": (now + timedelta(minutes=9)).isoformat(),
        "scope": {"kind": "dq", "table_name": "dq.raw_orders", "dt": "2026-10-05", "metric": "row_count_positive"},
        "result": {"status": status, "summary": summary, "observed": 1, "expected": 1,
                   "evidence_references": ["clickhouse://dq.dq_check_results/original-id"]},
    }


def preview(check=None):
    return {"alert_ref": REF, "incident_revision": 4, "status": "open", "scope": request()["scope"],
            "available": True, "blockers": [], "request": check}


def buttons(render):
    return [button for row in render.payload.get("components", []) for button in row["components"]]


def serialized(render):
    return json.dumps(render.payload, ensure_ascii=False)


def assert_limits(render):
    embed = render.payload["embeds"][0]
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096
    assert len(embed["fields"]) <= 25
    assert all(len(field["name"]) <= 256 and len(field["value"]) <= 1024 for field in embed["fields"])
    size = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
    assert size + sum(len(field["name"]) + len(field["value"]) for field in embed["fields"]) <= 6000
    assert len(render.action_rows) <= 5
    assert all(len(row.actions) <= 5 for row in render.action_rows)
    assert all(len(button["label"]) <= 80 and len(button.get("custom_id", "")) <= 100 for button in buttons(render))
    assert render.payload["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("renderer,english,indonesian", [
    (lambda: f.format_alert_summary(alert()), "### Quick Read", "### Ringkasan"),
    (lambda: f.format_alert_list([alert()], "open"), "### Health Summary", "### Ringkasan Kondisi"),
    (lambda: f.format_alert_list([], "open"), "No alerts matched", "Tidak ada alert yang cocok"),
    (lambda: f.format_triage_result(report()), "### Recommended Next Step", "### Langkah Berikutnya"),
    (lambda: f.format_daily_summary("2026-10-05", [], []), "### Day Status", "### Kondisi Harian"),
    (lambda: f.format_quality_summary({"scope": "weekly"}), "DQ Weekly Summary", "Ringkasan DQ Mingguan"),
    (lambda: f.format_operator_answer("original question", RAW), "### Guardrail", "### Batas Pengamanan"),
    (lambda: f.format_operator_answer("", RAW, response_mode="direct"), "### Runtime Proof", "### Bukti Runtime"),
    (lambda: f.format_backfill_preview("APR-ABC123", "2026-10-05", "2026-10-05", "dag-original", RAW), "### Safety Check", "### Pemeriksaan Keamanan"),
    (lambda: f.format_approval_recorded("APR-ABC123", "operator-original"), "### Approval Details", "### Detail Persetujuan"),
    (f.format_bot_online, "DQ Bot Online", "Bot DQ Aktif"),
    (lambda: f.trim_message("x" * 5000), "Message trimmed for Discord.", "Pesan dipotong agar sesuai batas Discord."),
])
def test_markdown_fixed_copy_uses_current_context(language, renderer, english, indonesian):
    with language_context(language):
        text = renderer()
    assert (english if language == "en" else indonesian) in text
    assert f.DISCORD_SEPARATOR in text


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("renderer,english,indonesian", [
    (lambda: f.render_incident_embed(f.build_discord_incident_payload(alert())), "Next best action", "Langkah berikutnya"),
    (lambda: f.render_alert_list_embed([], "open", None), "Data Quality Incident Inbox", "Daftar Incident Kualitas Data"),
    (lambda: f.render_triage_embed(report()), "Investigation", "Investigasi"),
    (lambda: f.render_daily_summary_embed("2026-10-05", [], []), "Daily Data Quality", "Kualitas Data Harian"),
    (lambda: f.render_quality_summary_embed({"scope": "weekly"}), "Weekly data reliability", "Keandalan data mingguan"),
    (lambda: f.render_evidence_embed({"alert_ref": REF}), "Evidence summary", "Ringkasan bukti"),
    (lambda: f.render_report_reference_embed({"alert_ref": REF}), "Investigation Report", "Laporan Investigasi"),
    (lambda: f.render_approval_plan_embed({"request_id": "APR-ABC123"}), "Execution boundary", "Batas eksekusi"),
    (lambda: f.render_operator_answer_embed(answer=RAW, alert_key=REF), "Data reliability answer", "Jawaban keandalan data"),
    (lambda: f.render_approval_embed(request_id="APR-ABC123", status="pending", summary=RAW, next_step=RAW), "Recovery boundary", "Batas pemulihan"),
    (lambda: f.render_debug_embed({}), "DQ Debug Context", "Konteks Debug DQ"),
    (lambda: f.render_resolution_preview_embed(preview()), "Close this incident?", "Tutup incident ini?"),
    (lambda: f.render_resolution_evidence_embed(preview(), request()), "Stored evidence", "Bukti tersimpan"),
    (lambda: f.render_resolution_confirmation_embed(preview(), request(), RAW), "Your reason", "Alasan Anda"),
    (lambda: f.render_resolved_incident_embed(REF, {}, {}), "closure history", "riwayat penutupan"),
])
def test_embeds_fixed_copy_and_bounds_in_both_languages(language, renderer, english, indonesian):
    with language_context(language):
        render = renderer()
    assert (english if language == "en" else indonesian) in serialized(render)
    assert_limits(render)


@pytest.mark.parametrize("language", ["en", "id"])
def test_reports_answers_reasons_and_evidence_are_not_translated(language):
    stored = report()
    original = copy.deepcopy(stored)
    with language_context(language):
        renders = [
            f.render_triage_embed(stored),
            f.render_operator_answer_embed(answer=RAW, alert_key=REF),
            f.render_evidence_embed({"alert_ref": REF, "status": "open", "summary": RAW}),
            f.render_approval_plan_embed({"request_id": "APR-ABC123", "reason": RAW}),
            f.render_approval_embed(request_id="APR-ABC123", status="pending", summary=RAW, next_step=RAW,
                                    validation_plan=RAW, rollback_plan=RAW),
            f.render_resolution_confirmation_embed(preview(), request(), RAW),
            f.render_resolved_incident_embed(REF, {}, {"resolution_reason": RAW}),
        ]
    assert stored == original
    for render in renders:
        assert RAW in serialized(render)
        assert REF in serialized(render) or "APR-ABC123" in serialized(render)
    assert "s3://reports/original.md" in renders[0].fallback_markdown


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("english", [
    "Check passed. The issue was not found in the latest check.",
    "Closure blocked. Problems remain or check evidence is incomplete.",
])
@pytest.mark.parametrize("legacy", [False, True])
def test_only_catalogued_backend_resolution_results_are_mapped(language, english, legacy):
    text = MESSAGES[english] if legacy else english
    check = request(summary=text)
    original = copy.deepcopy(check)
    with language_context(language):
        renders = [
            f.render_resolution_preview_embed(preview(check), check),
            f.render_resolution_evidence_embed(preview(check), check),
            f.render_resolution_confirmation_embed(preview(check), check, "operator reason unchanged"),
            f.render_resolved_incident_embed(REF, {}, {"result": check["result"], "resolution_request_id": REQUEST_ID}),
            f.render_evidence_embed({"alert_ref": REF, "status": "resolved", "summary": text}),
        ]
    assert check == original
    for render in renders:
        assert (english if language == "en" else MESSAGES[english]) in serialized(render)
        assert render.provider == render.model == ""
        assert render.narrative_source == "deterministic_fallback"


@pytest.mark.parametrize("language", ["en", "id"])
def test_uncatalogued_resolution_summary_and_evidence_values_remain_original(language):
    original_summary = "25 dari 25 segmen tersedia. {operator_unchanged}"
    check = request(summary=original_summary)
    check["result"]["expected"] = {"metric-original": "Pemeriksaan lolos"}
    check["result"]["observed"] = ["key-original", "No evidence returned."]
    with language_context(language):
        render = f.render_resolution_evidence_embed(preview(check), check)
    assert original_summary in serialized(render)
    assert "metric-original: Pemeriksaan lolos" in serialized(render)
    assert "key-original; No evidence returned." in serialized(render)


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("workspace", ["", WORKSPACE])
@pytest.mark.parametrize("has_report", [False, True])
@pytest.mark.parametrize("resolved", [False, True])
def test_incident_action_grouping_and_readonly_resolution(language, workspace, has_report, resolved):
    with language_context(language):
        rows = f.incident_action_rows(REF, "RESOLVED" if resolved else "OPEN", workspace, has_report)
        payload = f.serialize_action_rows(rows)
    first = [item.action_id for item in rows[0].actions]
    assert first == (["evidence"] + (["report"] if has_report else []) if resolved else ["investigate", "evidence", "ask"])
    second = (["workspace"] if workspace else []) + ([] if resolved else ["resolve"])
    assert [item.action_id for row in rows[1:] for item in row.actions] == second
    assert all(item.style != "success" for row in rows for item in row.actions)
    assert all(item.custom_id.endswith(REF) for row in rows for item in row.actions if item.custom_id)
    if not resolved:
        assert [item.emoji for item in rows[0].actions] == ["\U0001F50D", "\U0001F4CE", "\U0001F4AC"]
        assert [item.style for item in rows[0].actions] == ["primary", "secondary", "secondary"]
    assert all(len(row["components"]) <= 5 for row in payload)


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("workspace", ["", WORKSPACE])
@pytest.mark.parametrize("has_report", [False, True])
def test_triage_has_explicit_reinvestigation_and_grouped_followups(language, workspace, has_report):
    with language_context(language):
        rows = f.triage_action_rows(REF, has_report, operator_ui_url=workspace)
    assert [item.action_id for item in rows[0].actions] == ["reinvestigate", "evidence", "ask"] + (["report"] if has_report else [])
    assert rows[0].actions[0].custom_id == f"dq:v1:reinvestigate:{REF}"
    assert [item.action_id for item in rows[1].actions] == (["workspace"] if workspace else []) + ["resolve", "draft"]
    assert all(len(row.actions) <= 5 for row in rows)
    assert all(item.style != "success" for row in rows for item in row.actions)


def test_resolved_report_metadata_enables_only_readonly_actions():
    stored = alert("resolved")
    incident = f.build_discord_incident_payload(stored, report_uri="s3://reports/original.md")
    render = f.render_incident_embed(incident, WORKSPACE)
    assert [item.action_id for row in render.action_rows for item in row.actions] == ["evidence", "report", "workspace"]
    triage = report()
    triage["alert"]["status"] = "resolved"
    render = f.render_triage_embed(triage, operator_ui_url=WORKSPACE)
    assert [item.action_id for row in render.action_rows for item in row.actions] == ["evidence", "report", "workspace"]


def test_configured_workspace_and_persisted_report_metadata_are_rendered(monkeypatch):
    monkeypatch.setenv("DISCORD_OPERATOR_UI_URL", WORKSPACE)
    stored = alert("resolved")
    stored["report_s3_uri"] = "s3://reports/unchanged.md"
    render = f.render_incident_embed(f.build_discord_incident_payload(stored))
    assert [item.action_id for row in render.action_rows for item in row.actions] == ["evidence", "report", "workspace"]
    render = f.render_triage_embed(report())
    assert [item.action_id for row in render.action_rows for item in row.actions][-3:] == ["workspace", "resolve", "draft"]


@pytest.mark.parametrize("language", ["en", "id"])
def test_gateway_view_matches_renderer_rows_labels_styles_and_emoji(language):
    async def create():
        with language_context(language):
            render = f.render_incident_embed(f.build_discord_incident_payload(alert()), WORKSPACE)
            return render, interactions.build_discord_view(render)
    render, view = asyncio.run(create())
    specs = [(row_index, item) for row_index, row in enumerate(render.action_rows) for item in row.actions]
    assert len(view.children) == len(specs)
    for gateway_item, (row_index, spec) in zip(view.children, specs):
        button = getattr(gateway_item, "item", gateway_item)
        assert gateway_item.row == row_index
        assert button.label == spec.label
        assert button.style.value == f.BUTTON_STYLE_VALUES[spec.style]
        assert str(button.emoji or "") == spec.emoji


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("status", ["queued", "running", "blocked", "error", "expired", "pass"])
def test_resolution_has_no_green_shortcut_and_no_close_without_pass(language, status):
    check = request(status)
    with language_context(language):
        render = f.render_resolution_preview_embed(preview(check), check, can_close=status == "pass")
    assert all(button["style"] != 3 for button in buttons(render))
    assert ("resolve_close" in {item.action_id for row in render.action_rows for item in row.actions}) is (status == "pass")
    assert_limits(render)


@pytest.mark.parametrize("language", ["en", "id"])
def test_modal_and_confirmation_are_localized_at_instance_creation(language):
    async def create():
        origin = r.ResolutionOrigin(REF, REQUEST_ID, 200, 10, 300, 700, 100)
        with language_context(language):
            modal = r.ResolutionReasonModal(origin)
            view = r.ResolutionConfirmationView(origin, 4, RAW)
        return modal, view
    modal, view = asyncio.run(create())
    assert modal.title == ("Incident closure reason" if language == "en" else "Alasan penutupan incident")
    assert modal.reason.label == ("Why can this incident be closed?" if language == "en" else "Mengapa incident ini boleh ditutup?")
    assert view.confirm.label == ("Confirm Close" if language == "en" else "Konfirmasi Penutupan")
    assert view.confirm.style == discord.ButtonStyle.success
    assert view.cancel.label == ("Cancel" if language == "en" else "Batal")
    assert view.reason == RAW


@pytest.mark.parametrize("language", ["en", "id"])
@pytest.mark.parametrize("error,english", [
    (ValueError("Alasan penutupan wajib berisi 5-1.000 karakter."), "A closure reason must contain 5-1,000 characters."),
    (PermissionError("secret-private-host"), "You cannot use this control. Verify that the message is from DataSentry and that you have the required access."),
    (ControlPlaneTransportError("secret-private-host"), "The system has not confirmed this request. Check Resolve or Workspace; do not assume the incident is closed."),
    (RuntimeError("secret-private-host"), "The check is unavailable. Review Workspace or contact an operator."),
])
def test_resolution_error_copy_is_localized_and_ephemeral(monkeypatch, language, error, english):
    value = SimpleNamespace(response=SimpleNamespace(is_done=lambda: True), followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(r.interactions, "interaction_context", Mock(return_value=None))
    monkeypatch.setattr(r.interactions, "write_interaction_audit", Mock())
    with language_context(language):
        asyncio.run(r._resolution_error(value, "resolve", REF, error))
    args = value.followup.send.call_args
    assert args.args[0] == (english if language == "en" else MESSAGES[english])
    assert args.kwargs["ephemeral"] is True
    assert args.kwargs["allowed_mentions"].to_dict()["parse"] == []
    assert "secret-private-host" not in args.args[0]


def test_scheduled_result_always_english_and_restores_callers_context():
    with language_context("id"):
        render = r.render_revalidation_result(request())
        assert current_language() == "id"
    assert render.payload["embeds"][0]["title"] == "\u2705 Check passed"
    assert "Check passed. The issue was not found" in serialized(render)
    assert current_language() == "en"


@pytest.mark.parametrize("language", ["en", "id"])
def test_provenance_remains_truthful_and_mentions_bounds_are_preserved(language):
    raw = "@everyone <@123> " + "evidence " * 2000
    with language_context(language):
        heuristic = f.render_incident_embed(f.build_discord_incident_payload(
            alert(), narrative=raw, llm_runtime={"provider": "heuristic", "used_heuristic": True}))
        external = f.render_incident_embed(f.build_discord_incident_payload(
            alert(), narrative=raw, llm_runtime={"provider": "gemini", "model": "gemini-example", "used_heuristic": False}))
        deterministic = f.render_incident_embed(f.build_discord_incident_payload(alert()))
    assert heuristic.narrative_source == "heuristic_copilot"
    assert "Gemini" not in serialized(heuristic)
    assert external.narrative_source == "external_copilot"
    assert "Gemini" in external.payload["embeds"][0]["footer"]["text"]
    assert deterministic.narrative_source == "deterministic_fallback"
    for render in (heuristic, external, deterministic):
        assert "@everyone" not in serialized(render) and "<@123>" not in serialized(render)
        assert_limits(render)


def test_catalog_templates_keep_identical_placeholders_and_valid_unicode():
    def placeholders(text):
        return Counter((name, spec, conversion) for _, name, spec, conversion in Formatter().parse(text)
                       if name is not None)
    for english, indonesian in MESSAGES.items():
        assert placeholders(english) == placeholders(indonesian), english
        english.encode("utf-8")
        indonesian.encode("utf-8")
    assert translate(RAW) == "Check passed. The issue was not found in the latest check."
    assert translate("noncatalogued evidence {raw}") == "noncatalogued evidence {raw}"


def _translation_template(value, *, positional=False):
    """Extract fixed lookup keys without evaluating source code or stored content."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if not isinstance(value, ast.JoinedStr):
        return None
    parts = []
    index = 0
    for item in value.values:
        if isinstance(item, ast.Constant):
            parts.append(item.value.replace("{", "{{").replace("}", "}}"))
        else:
            conversion = "" if item.conversion == -1 else "!" + chr(item.conversion)
            spec = _translation_template(item.format_spec) if item.format_spec else ""
            if spec is None:
                return None
            name = f"v{index}" if positional else ast.unparse(item.value)
            parts.append("{" + name + conversion + (":" + spec if spec else "") + "}")
            index += 1
    return "".join(parts)


def _fixed_discord_lookups(filename):
    """Include the Mention Ask feedback helper's fixed callers as indirect lookups."""
    path = Path(__file__).resolve().parents[1] / "apps" / "discord_bot" / filename
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    aliases = {"tr", "translate"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "apps.common.i18n":
            aliases.update(item.asname or item.name for item in node.names if item.name in {"tr", "translate"})
    lookups = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        value = None
        if (isinstance(node.func, ast.Name) and node.func.id in aliases
                or isinstance(node.func, ast.Attribute) and node.func.attr in {"tr", "translate"}):
            value = node.args[0] if node.args else next((item.value for item in node.keywords if item.arg == "text"), None)
        elif filename == "mentions.py" and isinstance(node.func, ast.Attribute) and node.func.attr == "feedback":
            value = node.args[1] if len(node.args) > 1 else next((item.value for item in node.keywords if item.arg == "text"), None)
        key = _translation_template(value)
        if key:
            lookups.append((node.lineno, key))
    return lookups


@pytest.mark.parametrize("filename", [
    "formatters.py", "resolution.py", "interactions.py", "workspace.py", "mentions.py", "bot.py",
])
def test_fixed_discord_translation_lookups_have_catalog_entries(filename):
    missing = [f"{filename}:{line}: {key!r}" for line, key in _fixed_discord_lookups(filename) if key not in MESSAGES]
    assert not missing, "Missing fixed Discord copy:\n" + "\n".join(missing)


@pytest.mark.parametrize("source,expected", [
    ("'Incident workspace: {url}'", "Incident workspace: {url}"),
    ("f'Incident {alert_ref}: {count!r}, {duration:.2f}'", "Incident {alert_ref}: {count!r}, {duration:.2f}"),
    ("f'{{literal}} {alert_ref}'", "{{literal}} {alert_ref}"),
    ("stored_report", None),
])
def test_lookup_scanner_handles_templates_and_ignores_raw_values(source, expected):
    assert _translation_template(ast.parse(source, mode="eval").body) == expected


def _indirect_presentation_copy(tree):
    """Inventory fixed presentation arguments, not runtime translation wiring.

    Raw f-strings use vN fields so expressions are never evaluated or translated.
    Only known presentation sinks are inspected; stored values and logs are excluded.
    """
    lookups = []

    def record(value):
        if isinstance(value, ast.IfExp):
            record(value.body)
            record(value.orelse)
            return
        key = _translation_template(value, positional=True)
        if key:
            lookups.append((value.lineno, key))

    sinks = {
        "publish_render": {"acknowledgement"},
        "publish_extended_quality_summary": {"acknowledgement"},
        "respond_with_message": {"content"},
        "command_error": {"operation"},
        "render_approval_embed": {"summary", "next_step"},
        "Embed": {"title", "description"},
        "_embed_result": {"title", "description", "footer", "fallback_markdown"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for item in node.keywords:
                if item.arg in sinks.get(name, set()):
                    record(item.value)
            if name == "respond_with_message" and len(node.args) > 1:
                record(node.args[1])
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "name":
                    record(value)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ValueError" and node.exc.args:
                record(node.exc.args[0])
    return lookups


@pytest.mark.parametrize("filename", ["bot.py", "workspace.py"])
def test_indirect_command_and_workspace_copy_has_discord_catalog_entries(filename):
    path = Path(__file__).resolve().parents[1] / "apps" / "discord_bot" / filename
    keys = _indirect_presentation_copy(ast.parse(path.read_text(encoding="utf-8-sig")))
    assert keys
    missing = [f"{filename}:{line}: {key!r}" for line, key in keys if key not in MESSAGES]
    assert not missing, "Uncatalogued presentation copy (not runtime wiring):\n" + "\n".join(missing)


def test_indirect_copy_scanner_ignores_stored_values_and_logs():
    tree = ast.parse("""
publish_render(result=stored_report, acknowledgement='Report posted.')
respond_with_message(interaction, f'Request {payload["request_id"]} recorded.')
render_approval_embed(summary=stored_summary, next_step='Confirm.' if pending else 'Read only.')
logger.info('Not presentation copy.')
raise RuntimeError('Not operator copy.')
""")
    assert [key for _, key in _indirect_presentation_copy(tree)] == [
        "Report posted.", "Request {v0} recorded.", "Confirm.", "Read only.",
    ]


@pytest.mark.parametrize("language", [None, "en", "id"])
def test_command_catalog_copy_preserves_urls_ids_and_operator_notes(language):
    path = Path(__file__).resolve().parents[1] / "apps" / "discord_bot" / "bot.py"
    keys = {key for _, key in _indirect_presentation_copy(ast.parse(path.read_text(encoding="utf-8-sig")))}
    assert current_language() == "en"
    for english in keys:
        fields = [name for _, name, _, _ in Formatter().parse(english) if name is not None]
        values = {name: f"ORIGINAL-{name}" for name in fields}
        text = translate(english, language=language, **values)
        expected = MESSAGES[english] if language == "id" else english
        assert text == expected.format(**values)
        assert all(value in text for value in values.values())
    note = "No evidence returned. {original_note} https://example.test/report?id=APR-ABC123"
    template = "Backfill request for `{v0}` covering `{v1}` to `{v2}`. {v3}"
    text = translate(template, language=language, v0="dag_original", v1="2026-10-05", v2="2026-10-06", v3=note)
    assert note in text
    assert all(value in text for value in ("dag_original", "2026-10-05", "2026-10-06"))


@pytest.mark.parametrize("language", ["en", "id"])
def test_shared_workspace_starter_stays_english_and_restores_personal_context(language):
    state = SimpleNamespace(alert_ref=REF, generation=4)
    facts = {"table_name": "dq.original_table"}
    with language_context(language):
        render = workspace.workspace_starter(state, facts)
        assert current_language() == language
    embed = render.payload["embeds"][0]
    assert embed["title"] == "Investigation Workspace"
    assert embed["description"].startswith("Use this thread for investigation, evidence, reports, and follow-up questions.")
    assert embed["footer"]["text"] == "Workspace revision 5 | No model call"
    assert embed["fields"] == [
        {"name": "Alert Ref", "value": REF, "inline": True},
        {"name": "Table", "value": "dq.original_table", "inline": True},
    ]
    assert facts == {"table_name": "dq.original_table"}
    assert_limits(render)


def test_personal_thread_copy_is_indonesian_with_exact_placeholders():
    def fields(text):
        return Counter((name, spec, conversion) for _, name, spec, conversion in Formatter().parse(text) if name is not None)
    keys = {key for filename in ("interactions.py", "mentions.py") for _, key in _fixed_discord_lookups(filename)}
    assert keys
    for english in keys:
        indonesian = MESSAGES[english]
        assert indonesian != english, english
        assert fields(english) == fields(indonesian), english
        values = {name: f"ORIGINAL-{name}" for name, _, _ in fields(english)}
        with language_context("id"):
            text = translate(english, **values)
        assert text == indonesian.format(**values)
        assert all(value in text for value in values.values())


@pytest.mark.parametrize("english", sorted({key for _, key in _fixed_discord_lookups("mentions.py")}))
def test_actual_mention_feedback_uses_indonesian_preference(monkeypatch, english):
    from apps.discord_bot.mentions import MentionAskHandler
    monkeypatch.setattr(preferences, "get_user_language", Mock(return_value="id"))
    handler = SimpleNamespace(guild_id=10)
    message = SimpleNamespace(id=900, author=SimpleNamespace(id=200), reply=AsyncMock())
    asyncio.run(MentionAskHandler.feedback(handler, message, english))
    sent = message.reply.call_args
    assert sent.args[0] == MESSAGES[english]
    assert sent.args[0] != english
    assert sent.kwargs["mention_author"] is False
    assert sent.kwargs["allowed_mentions"].to_dict()["parse"] == []
    preferences.get_user_language.assert_called_once_with(10, 200)
    assert current_language() == "en"
