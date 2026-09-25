####
## Persisted Web Report Acceptance for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Compare one real stored report with server-emitted HTML without invoking an LLM."""

# --- Importing Libraries
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen

from pipelines.common.logging import logger
from scripts.smoke_web import (
    DEFAULT_WEB_ORIGIN, DEFAULT_WEB_URL, WebAcceptanceCheck, WebTransportError,
    build_request, normalize_http_origin, open_http_request,
)


# --- Extracting Server-Emitted Report Text
class ReportTextParser(HTMLParser):
    """Collect document text while excluding scripts and explicitly hidden markup."""

    def __init__(self) -> None:
        """Initialize tag ancestry and text fragments; no browser is launched."""
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track suppression for scripts, templates, styles, and hidden subtrees."""
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        attributes = dict(attrs)
        hidden = tag in {"script", "style", "template"} or "hidden" in attributes
        self.stack.append((tag, hidden))

    def handle_endtag(self, tag: str) -> None:
        """Close the matching ancestry without interpreting CSS or JavaScript."""
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        """Retain text only outside suppressed subtrees."""
        if not any(hidden for _, hidden in self.stack):
            self.fragments.append(data)


# --- Validating Persisted Artifact References
def paired_json_uri(reference: str) -> str | None:
    """Return the stored report's JSON sibling; reject traversal and non-S3 URLs."""
    if not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9_/=.-]+/report\.(md|json)", reference):
        return None
    if any(part in {".", ".."} for part in reference.split("/")):
        return None
    return re.sub(r"/report\.md$", "/report.json", reference)


def check_persisted_web_report(
    web_url: str = DEFAULT_WEB_URL,
    web_origin: str = DEFAULT_WEB_ORIGIN,
    opener: Callable[..., Any] = urlopen,
) -> WebAcceptanceCheck:
    """
    Compare API-discovered alert/report facts with the selected server HTML.

    Args:
        web_url: Internal web service origin.
        web_origin: Trusted public browser origin.
        opener: Injectable read-only HTTP transport for offline tests.

    Returns:
        Sanitized pass/fail evidence. Missing fixture data fails this explicit gate.
        This proves server-emitted report text, not browser layout or hydration.
    """
    name = "web_report:stored_identity_and_content"
    logger.info("Starting persisted web report acceptance | check=%s", name)

    def read(path: str) -> bytes:
        """Perform one bounded GET; never issue a mutation or return raw errors."""
        request = build_request(web_url, web_origin, path)
        response = open_http_request(request, timeout_seconds=20, opener=opener)
        if response.status_code != 200:
            raise ValueError("Expected successful bounded read")
        return response.body

    try:
        web_url = normalize_http_origin(web_url)
        web_origin = normalize_http_origin(web_origin)
        payload = json.loads(read("/api/control-plane/api/v1/alerts?status=triaged&limit=20"))
        candidates = payload["alerts"]
        selected = next((row for row in candidates if isinstance(row, dict)
                         and isinstance(row.get("alert_key"), str)
                         and isinstance(row.get("report_s3_uri"), str)
                         and paired_json_uri(row["report_s3_uri"])), None)
        if selected is None:
            raise LookupError("No stored triaged report available")
        artifact = json.loads(read("/api/control-plane/api/v1/reports/read?" + urlencode({
            "s3_uri": paired_json_uri(selected["report_s3_uri"]), "max_bytes": 160_000,
        })))
        if artifact.get("truncated") or artifact.get("media_type") != "application/json":
            raise ValueError("A complete JSON artifact is required")
        report = json.loads(artifact["text"])
        report_id = report["report_id"]
        if not isinstance(report_id, str) or not re.fullmatch(r"RPT-[A-Za-z0-9-]{1,100}", report_id):
            raise ValueError("Invalid report identifier")
        if report["alert"]["alert_key"] != selected["alert_key"]:
            raise ValueError("Report belongs to a different alert")
        required = ["Persisted triage report", "Ranked hypotheses", "Evidence reviewed",
                    report_id, report["summary"], report["hypotheses"][0]["title"],
                    report["evidence"][0]["summary"]]
        parser = ReportTextParser()
        parser.feed(read("/triage?" + urlencode({"alert": selected["alert_key"]})).decode("utf-8"))
        content = " ".join(" ".join(parser.fragments).split())
        if not all(isinstance(value, str) and value.strip()
                   and " ".join(value.split()) in content for value in required):
            raise ValueError("Selected report text not emitted in document")
        details = {"report_id": report_id, "checked_text_fields": len(required),
                   "identity_matched": True, "external_calls": 0,
                   "evidence_scope": "server_html_not_visual_browser_acceptance"}
        logger.info("Persisted web report acceptance passed | report_id=%s", report_id)
        return WebAcceptanceCheck(name=name, status="pass", details=details)
    except (ValueError, LookupError, TypeError, AttributeError, WebTransportError) as exc:
        logger.warning("Persisted web report acceptance failed | error_type=%s", type(exc).__name__)
        return WebAcceptanceCheck(name=name, status="fail", details={"error_type": type(exc).__name__})
