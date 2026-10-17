"""Offline contract tests for the release-validation workflow and JUnit gate."""

from __future__ import annotations

import ast
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from scripts.check_ci_test_results import (
    EXPECTED_DATABASE_SKIPS,
    NODE_CAPTURE_CLASS,
    REQUIRED_NODE_CAPTURE_TESTS,
    main,
    summarize_junit,
    summarize_warnings,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release-validation.yml"
NODE_CLASS = NODE_CAPTURE_CLASS


def _node_cases() -> list[ET.Element]:
    return [_case(NODE_CLASS, name) for name in sorted(REQUIRED_NODE_CAPTURE_TESTS)]


def _write_report(path: Path, cases: list[ET.Element]) -> None:
    suite = ET.Element("testsuite", tests=str(len(cases)))
    suite.extend(cases)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _case(classname: str, name: str, outcome: str = "passed", detail: str = "") -> ET.Element:
    case = ET.Element("testcase", classname=classname, name=name, time="0.01")
    if outcome == "skipped":
        ET.SubElement(case, "skipped", message=detail)
    elif outcome == "failed":
        failure = ET.SubElement(case, "failure", message=detail)
        failure.text = detail
    return case


def test_database_only_skips_are_allowed_but_node_capture_must_run(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_report(
        report,
        _node_cases() + [
            _case(classname, name, "skipped", reason)
            for (classname, name), reason in EXPECTED_DATABASE_SKIPS.items()
        ],
    )

    result = summarize_junit(report)

    assert result["status"] == "passed"
    assert result["required_node_capture_tests_passed"] is True
    assert result["counts"] == {
        "total": 23,
        "passed": 12,
        "expected_skipped": 11,
        "unexpected_skipped": 0,
        "failed": 0,
    }
    assert result["node_capture_counts"] == {"required": 12, "passed": 12, "duplicated": 0}
    assert len(result['expected_skip_tests']) == 11
    assert all(name.startswith('tests.test_') for name in result['expected_skip_tests'])


def test_warning_report_keeps_categories_without_diagnostic_content(tmp_path):
    log = tmp_path / 'pytest.log'
    log.write_text("privateDiagnosticToken123\nDeprecationWarning: audioop is deprecated\n"
                   "discord/ui/text_input.py: DeprecationWarning: label parameter deprecated\n"
                   "10 passed, 2 warnings in 1.0s\n", encoding='utf-8')
    result = summarize_warnings(log)
    assert result['count'] == 2
    assert result['known_categories'] == ['python_audioop_deprecation', 'discord_component_label_deprecation']
    assert result['warning_types'] == ['DeprecationWarning']
    assert 'privateDiagnosticToken123' not in json.dumps(result)
    assert 'text_input.py' not in json.dumps(result)


def test_warning_report_rejects_unbounded_logs(tmp_path):
    log = tmp_path / 'pytest.log'
    log.write_bytes(b'x' * 2_000_001)
    with pytest.raises(ValueError):
        summarize_warnings(log)


def test_node_missing_and_unknown_skips_fail_the_gate(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_report(
        report,
        [
            _case(NODE_CLASS, next(iter(REQUIRED_NODE_CAPTURE_TESTS)), "skipped", "Node.js is required for capture helper tests"),
            _case("tests.test_other", "test_unknown", "skipped", "new skip condition"),
        ],
    )

    result = summarize_junit(report)

    assert result["status"] == "failed"
    assert result["required_node_capture_tests_missing"] is True
    assert result["counts"]["unexpected_skipped"] == 2


def test_failures_gate_and_report_omits_failure_details(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_report(
        report,
        _node_cases() + [
            _case("tests.test_other", "test_sensitive_case", "failed", "fake-secret-value"),
        ],
    )

    result = summarize_junit(report)
    serialized = repr(result)

    assert result["status"] == "failed"
    assert len(result["failure_tests"]) == 1
    assert re.fullmatch(r"test-sha256:[0-9a-f]{16}", result["failure_tests"][0])
    assert "fake-secret-value" not in serialized


def test_required_node_inventory_matches_all_twelve_real_helpers() -> None:
    tree = ast.parse((ROOT / "tests/test_demo_screenshot_capture.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "DemoScreenshotCaptureTests")
    names = {node.name for node in cls.body if isinstance(node, ast.FunctionDef)
             and node.name.startswith("test_")}
    assert names == REQUIRED_NODE_CAPTURE_TESTS
    assert len(names) == 12


@pytest.mark.parametrize("missing", sorted(REQUIRED_NODE_CAPTURE_TESTS))
def test_each_required_node_helper_must_pass(tmp_path: Path, missing: str) -> None:
    report = tmp_path / "junit.xml"
    _write_report(report, [case for case in _node_cases() if case.attrib["name"] != missing])
    assert summarize_junit(report)["status"] == "failed"


def test_duplicate_or_impostor_node_helpers_cannot_satisfy_gate(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    cases = _node_cases()
    _write_report(report, cases + [cases[0]])
    assert summarize_junit(report)["status"] == "failed"
    for case in cases:
        case.set("classname", "impostor." + NODE_CLASS)
    _write_report(report, cases)
    assert summarize_junit(report)["status"] == "failed"


@pytest.mark.parametrize("classname,name,reason", [
    ("tests.test_resolution_store", "test_new_regression", "Local control store unavailable"),
    ("impostor.tests.test_resolution_store", "test_two_operator_confirmations_have_one_durable_decision", "Local control store unavailable"),
    ("tests.test_discord_welcome", "test_real_postgres_welcome_fixture_only", "No scoped control DB\n"),
    ("tests.test_discord_welcome", "test_real_postgres_welcome_fixture_only[unexpected]", "No scoped control DB"),
    ("tests.test_discord_welcome", "test_real_postgres_welcome_fixture_only", "Welcome migration not installed; do not apply from this test"),
])
def test_skip_allowlist_is_exact(tmp_path: Path, classname: str, name: str, reason: str) -> None:
    report = tmp_path / "junit.xml"
    _write_report(report, _node_cases() + [_case(classname, name, "skipped", reason)])
    result = summarize_junit(report)
    assert result["status"] == "failed"
    assert result["counts"]["unexpected_skipped"] == 1


def test_expected_failure_is_not_an_allowed_database_skip(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    (classname, name), reason = next(iter(EXPECTED_DATABASE_SKIPS.items()))
    case = _case(classname, name, "skipped", reason)
    case.find("skipped").set("type", "pytest.xfail")
    _write_report(report, _node_cases() + [case])
    assert summarize_junit(report)["status"] == "failed"


def test_report_never_echoes_identifiers_parameters_or_diagnostics(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    secret = "syntheticPrivateToken123"
    cases = _node_cases() + [
        _case(secret, secret + "[" + secret + "]", "failed", secret),
        _case(secret, secret, "skipped", secret),
    ]
    ET.SubElement(cases[0], "system-out").text = secret
    ET.SubElement(cases[0], "properties", token=secret)
    _write_report(report, cases)
    assert secret not in json.dumps(summarize_junit(report))


def test_nonzero_pytest_exit_fails_even_with_all_helpers_passed(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_report(report, _node_cases())
    assert summarize_junit(report, pytest_exit_code=5)["status"] == "failed"


@pytest.mark.parametrize("content", [None, "<invalid", "<wrong-root/>", "<testsuites/>"])
def test_cli_fails_closed_with_safe_json_on_unavailable_or_invalid_junit(tmp_path: Path, capsys, content) -> None:
    report = tmp_path / "synthetic-private-path.xml"
    output = tmp_path / "report.json"
    if content is not None:
        report.write_text(content, encoding="utf-8")
    assert main([str(report), "--output", str(output)]) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "failed"
    assert "synthetic-private-path" not in capsys.readouterr().out


def test_workflow_pins_official_actions_and_keeps_runtime_flags_offline() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches:\n      - main" in workflow
    assert "workflow_dispatch:" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert 'python-version: "3.11"' in workflow
    assert 'node-version: "22"' in workflow
    assert 'EXTERNAL_LLM_ENABLED: "false"' in workflow
    assert 'COPILOT_EXTERNAL_LLM_ENABLED: "false"' in workflow
    assert 'INCIDENT_CONTROL_ENABLED: "false"' in workflow
    assert "secrets." not in workflow
    assert "persist-credentials: false" in workflow
    assert 'DQ_CONTROL_DATABASE_URL: ""' in workflow
    assert 'AWS_EC2_METADATA_DISABLED: "true"' in workflow
    assert "pytest -ra tests --junitxml=" in workflow
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    uses = re.findall(r"uses:\s*(\S+)", workflow)
    assert len(uses) == 4
    assert all(re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", action) for action in uses)


def test_workflow_uses_read_only_documentation_and_manifest_defined_node_checks() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "python scripts/build_repository_diagrams.py --check" in workflow
    assert "python scripts/validate_repository_presentation.py" in workflow
    assert "npm test" in workflow
    assert "npm run typecheck" in workflow
    assert "npm run check" in workflow
    assert "prepare:assets" not in workflow
    assert "--captures" not in workflow
    assert "render.mjs" not in workflow
    assert "if: always()" in workflow


def test_workflow_uploads_only_sanitized_json_not_raw_logs_or_junit() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["validate"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "services" not in job
    steps = job["steps"]
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/release-validation/run.json",
        "${{ runner.temp }}/release-validation/test-results.json",
    ]
    gate = next(step["run"] for step in steps if "check_ci_test_results.py" in step.get("run", ""))
    assert '--pytest-exit-code "$pytest_exit_code"' in gate
    assert '--junitxml="$RUNNER_TEMP/release-validation-junit.xml"' in gate
    assert '> "$RUNNER_TEMP/release-validation-pytest.log" 2>&1' in gate
    assert not any("continue-on-error" in step for step in steps)
