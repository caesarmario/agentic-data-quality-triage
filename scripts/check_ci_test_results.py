"""Gate pytest JUnit results and emit a report safe to upload as a CI artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


NODE_CAPTURE_CLASS = "tests.test_demo_screenshot_capture.DemoScreenshotCaptureTests"
REQUIRED_NODE_CAPTURE_TESTS = frozenset({
    "test_required_surfaces_are_explicit",
    "test_nonlocal_and_credentialed_urls_are_rejected",
    "test_explicit_local_http_url_is_accepted",
    "test_invalid_ports_are_rejected",
    "test_inventory_escapes_markup_and_redacts_token_like_values",
    "test_redaction_covers_provider_and_transport_secret_formats",
    "test_discord_markdown_is_rendered_and_sanitized",
    "test_env_parser_can_report_presence_without_printing_value",
    "test_help_is_service_free_and_invalid_selection_fails_closed",
    "test_atomic_capture_replaces_target_only_after_success",
    "test_failed_capture_preserves_existing_target_and_cleans_temp_file",
    "test_strict_mode_fails_after_emitting_skipped_summary",
})
EXPECTED_DATABASE_SKIPS = {
    ("tests.test_discord_welcome", "test_real_postgres_welcome_fixture_only"): "No scoped control DB",
    **{
        ("tests.test_discord_workspaces", f"{name}[{value}]"): "Local PostgreSQL control store unavailable"
        for name in (
            "test_real_postgres_cancellation_reconciles_claim_without_provider_execution",
            "test_real_postgres_cancel_does_not_release_finished_or_uncertain_work",
        )
        for value in ("False", "True")
    },
    **{
        ("tests.test_resolution_store", name): "Local control store unavailable"
        for name in (
            "test_two_operator_confirmations_have_one_durable_decision",
            "test_double_click_is_idempotent_and_different_payload_conflicts",
            "test_reused_active_request_retains_each_idempotency_key_after_completion",
            "test_uncertain_dispatch_replay_reconciles_without_reposting",
            "test_old_failure_stays_closed_but_new_failure_opens_preserved_episode",
            "test_late_triage_result_never_reopens_closed_control_incident",
        )
    },
}


def _test_label(classname: str, name: str) -> str:
    """Fingerprint identifiers; even syntactically valid names can contain secrets."""
    base_name = name.split("[", maxsplit=1)[0]
    digest = hashlib.sha256(f"{classname}::{base_name}".encode("utf-8")).hexdigest()
    return f"test-sha256:{digest[:16]}"


def _expected_skip(classname: str, name: str, reason: str) -> bool:
    return EXPECTED_DATABASE_SKIPS.get((classname, name)) == reason


def summarize_junit(report_path: Path, pytest_exit_code: int = 0) -> dict[str, object]:
    """Summarize outcomes, allowing only known database-backed integration skips."""
    root = ET.parse(report_path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise ValueError("Invalid JUnit root")
    cases = list(root.iter("testcase"))
    failures: list[str] = []
    unexpected_skips: list[str] = []
    expected_skips = 0
    expected_skip_tests: list[str] = []
    passed = 0
    capture_counts = {name: 0 for name in REQUIRED_NODE_CAPTURE_TESTS}
    capture_passed: set[str] = set()

    for case in cases:
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        label = _test_label(classname, name)
        has_failure = case.find("failure") is not None or case.find("error") is not None
        skipped = case.find("skipped")

        if classname == NODE_CAPTURE_CLASS and name in REQUIRED_NODE_CAPTURE_TESTS:
            capture_counts[name] += 1
            if skipped is None and not has_failure:
                capture_passed.add(name)

        if has_failure:
            failures.append(label)
        elif skipped is not None:
            reason = skipped.attrib.get("message", "")
            if (skipped.attrib.get("type", "pytest.skip") == "pytest.skip"
                    and _expected_skip(classname, name, reason)):
                expected_skips += 1
                expected_skip_tests.append(f"{classname}::{name}")
            else:
                unexpected_skips.append(label)
        else:
            passed += 1

    capture_tests_passed = (
        capture_passed == REQUIRED_NODE_CAPTURE_TESTS
        and all(count == 1 for count in capture_counts.values())
    )
    successful = (
        bool(cases)
        and pytest_exit_code == 0
        and not failures
        and not unexpected_skips
        and capture_tests_passed
    )
    return {
        "schema_version": 1,
        "status": "passed" if successful else "failed",
        "pytest_exit_code": pytest_exit_code,
        "counts": {
            "total": len(cases),
            "passed": passed,
            "expected_skipped": expected_skips,
            "unexpected_skipped": len(unexpected_skips),
            "failed": len(failures),
        },
        "required_node_capture_tests_passed": capture_tests_passed,
        "required_node_capture_tests_missing": bool(REQUIRED_NODE_CAPTURE_TESTS - capture_passed),
        "node_capture_counts": {
            "required": len(REQUIRED_NODE_CAPTURE_TESTS),
            "passed": len(capture_passed),
            "duplicated": sum(count > 1 for count in capture_counts.values()),
        },
        "failure_tests": failures,
        "unexpected_skip_tests": unexpected_skips,
        "expected_skip_tests": sorted(expected_skip_tests),
    }


def summarize_warnings(log_path: Path) -> dict[str, object]:
    """Retain fixed warning categories/counts, never diagnostic text or test arguments."""
    with log_path.open('rb') as handle:
        raw = handle.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Pytest log exceeds the report bound')
    text = raw.decode('utf-8', errors='replace')
    totals = re.findall(r'\b(\d+) warnings?\b', text)
    categories = []
    if 'audioop' in text and 'deprecated' in text:
        categories.append('python_audioop_deprecation')
    if 'discord' in text and 'label' in text and 'DeprecationWarning' in text:
        categories.append('discord_component_label_deprecation')
    warning_types = sorted(set(re.findall(r'\b(DeprecationWarning|PendingDeprecationWarning|FutureWarning|UserWarning|RuntimeWarning):', text)))
    return {'available': True, 'count': int(totals[-1]) if totals else 0,
            'known_categories': categories, 'warning_types': warning_types}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pytest-exit-code", type=int, default=0)
    parser.add_argument("--pytest-log", type=Path)
    args = parser.parse_args(argv)

    try:
        summary = summarize_junit(args.junit, args.pytest_exit_code)
        if args.pytest_log is not None:
            summary['warnings'] = summarize_warnings(args.pytest_log)
    except (OSError, ET.ParseError, ValueError):
        summary = {
            "schema_version": 1,
            "status": "failed",
            "pytest_exit_code": args.pytest_exit_code,
            "error": "junit_report_unavailable_or_invalid",
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return int(summary["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
