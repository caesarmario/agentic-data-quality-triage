####
## Optional Web Acceptance Tests for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

from io import BytesIO
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from scripts.smoke_web import (
    DENIED_APPROVAL_PATH,
    MAX_REQUEST_BYTES,
    WEB_PAGE_PATHS,
    WebAcceptanceCheck,
    normalize_http_origin,
    open_http_request,
    run_web_acceptance,
)


# --- Defining Test Fakes
class FakeResponse:
    """Provide a context-managed urllib response with bounded bytes."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        """Initialize the response status and body."""
        self.status = status
        self.body = body

    def __enter__(self) -> "FakeResponse":
        """Enter the response context."""
        return self

    def __exit__(self, *args: object) -> None:
        """Exit the response context."""

    def read(self, size: int = -1) -> bytes:
        """Return at most the requested response bytes."""
        return self.body if size < 0 else self.body[:size]


class RecordingWebOpener:
    """Emulate the web pages, BFF proxy, and proxy-policy rejections."""

    def __init__(self) -> None:
        """Initialize the captured request list."""
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float) -> FakeResponse:
        """Return deterministic responses based on method, path, and headers."""
        self.requests.append((request, timeout))
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)

        if request.get_method() == "POST":
            origin = request.get_header("Origin")
            content_type = request.get_header("Content-type")

            if origin != "http://localhost:3000":
                return FakeResponse(403, b'{"detail":"denied"}')
            if content_type != "application/json":
                return FakeResponse(415, b'{"detail":"denied"}')
            if len(request.data or b"") > MAX_REQUEST_BYTES:
                return FakeResponse(413, b'{"detail":"denied"}')
            return FakeResponse(400, b'{"detail":"denied"}')

        if parsed.path == "/api/control-plane/api/v1/alerts":
            status = query.get("status", [""])[0]
            alerts: list[dict[str, Any]] = []

            return FakeResponse(
                200,
                json.dumps(
                    {
                        "status": "success",
                        "row_count": len(alerts),
                        "alerts": alerts,
                    }
                ).encode(),
            )

        if parsed.path == "/api/control-plane/api/v1/not-real":
            return FakeResponse(405, b'{"detail":"denied"}')

        if parsed.path in WEB_PAGE_PATHS:
            return FakeResponse(200, b"<html>control plane</html>")

        raise AssertionError(f"Unexpected test request path: {parsed.path}")


# --- Defining Contract Tests
def test_web_acceptance_covers_pages_proxy_shape_and_denied_posts() -> None:
    """Run the complete offline acceptance flow without valid mutations or LLM calls."""
    opener = RecordingWebOpener()

    checks = run_web_acceptance(timeout_seconds=2.0, opener=opener)

    assert len(checks) == 12
    assert all(isinstance(check, WebAcceptanceCheck) for check in checks)
    assert all(check.status == "pass" for check in checks)
    assert all(timeout == 2.0 for _, timeout in opener.requests)

    post_requests = [request for request, _ in opener.requests if request.get_method() == "POST"]
    assert len(post_requests) == 5
    assert all(urlsplit(request.full_url).path == DENIED_APPROVAL_PATH for request in post_requests)
    assert all("/triage/run" not in request.full_url for request in post_requests)
    assert all("/cancel" not in request.full_url for request in post_requests)


def test_web_acceptance_sends_proxy_host_and_configured_origin() -> None:
    """Ensure internal transport requests preserve the public browser policy headers."""
    opener = RecordingWebOpener()

    run_web_acceptance(opener=opener)

    assert opener.requests
    assert all(request.get_header("Host") == "localhost:3000" for request, _ in opener.requests)
    assert all(
        request.get_header("Origin") == "http://localhost:3000"
        for request, _ in opener.requests
        if request.get_method() == "GET"
    )
    post_origins = [
        request.get_header("Origin")
        for request, _ in opener.requests
        if request.get_method() == "POST"
    ]
    assert post_origins.count("http://localhost:3000") == 3
    assert post_origins.count("http://cross-origin.invalid") == 1
    assert post_origins.count(None) == 1


def test_web_configuration_rejects_credentials_without_echoing_values() -> None:
    """Reject credential-bearing endpoint configuration before network access."""
    calls = 0

    def opener(request: Request, timeout: float) -> FakeResponse:
        """Record an unexpected transport attempt."""
        nonlocal calls
        calls += 1
        return FakeResponse(200)

    checks = run_web_acceptance(
        web_url="http://user:secret@web:3000",
        opener=opener,
    )

    assert calls == 0
    assert checks == [
        WebAcceptanceCheck(
            name="web:configuration",
            status="fail",
            details={"error_type": "ValueError"},
        )
    ]
    assert normalize_http_origin("http://web:3000/") == "http://web:3000"


def test_open_http_request_retains_expected_http_error_status() -> None:
    """Treat urllib HTTPError as an assertion response rather than transport failure."""
    request = Request("http://web:3000/denied")

    def opener(request: Request, timeout: float) -> Any:
        """Raise the behavior used by urllib for a policy rejection."""
        raise HTTPError(
            request.full_url,
            403,
            "forbidden body must not be logged",
            hdrs=None,
            fp=BytesIO(b'{"detail":"denied"}'),
        )

    result = open_http_request(request, timeout_seconds=2.0, opener=opener)

    assert result.status_code == 403
    assert result.body == b'{"detail":"denied"}'
