####
## Optional Web Acceptance Smoke for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_WEB_URL = os.getenv("WEB_READINESS_URL", "http://web:3000")
DEFAULT_WEB_ORIGIN = os.getenv("WEB_ORIGIN", "http://localhost:3000")
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 65_536

WEB_PAGE_PATHS = (
    "/",
    "/incidents",
    "/triage",
    "/approvals",
    "/lineage",
)
ALERTS_PROXY_PATH = "/api/control-plane/api/v1/alerts?status=open&limit=1"
UNKNOWN_PROXY_PATH = "/api/control-plane/api/v1/not-real"
DENIED_APPROVAL_PATH = "/api/control-plane/api/v1/approvals/requests/APR-NOTREAL/decision"


# --- Defining Data Models
@dataclass(frozen=True)
class WebAcceptanceCheck:
    """
    Represent one credential-safe HTTP acceptance result.

    Attributes:
        name: Stable check identifier retained in Airflow logs.
        status: Pass or fail.
        details: Sanitized status and request metadata without raw bodies.
    """

    name: str
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class HttpResult:
    """
    Store one bounded HTTP response used by acceptance assertions.

    Attributes:
        status_code: HTTP response status.
        body: Bounded response bytes, never written to logs.
    """

    status_code: int
    body: bytes


class WebTransportError(RuntimeError):
    """Represent a sanitized web transport failure."""

    def __init__(self, error_type: str) -> None:
        """Store only the exception class name, not sensitive exception text."""
        super().__init__(error_type)
        self.error_type = error_type


# --- Validating Configuration
def normalize_http_origin(value: str) -> str:
    """
    Validate an HTTP base URL without exposing credentials in an error.

    Args:
        value: Environment-provided URL or public origin.

    Returns:
        Normalized origin without a trailing slash.

    Raises:
        ValueError: If the value is not a credential-free HTTP origin.
    """
    parsed = urlsplit(value.strip())

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Web acceptance requires a credential-free HTTP origin.")

    return f"{parsed.scheme}://{parsed.netloc}"


# --- Executing Bounded HTTP Requests
def open_http_request(
    request: Request,
    timeout_seconds: float,
    opener: Callable[..., Any] = urlopen,
) -> HttpResult:
    """
    Execute one request and retain a bounded response for local assertions.

    Args:
        request: Fully constructed urllib request.
        timeout_seconds: Per-request timeout.
        opener: urllib-compatible opener injected by offline tests.

    Returns:
        HTTP status and bounded body. Expected non-2xx responses are retained.

    Raises:
        WebTransportError: For network failures or oversized responses.
    """
    try:
        response = opener(request, timeout=timeout_seconds)
    except HTTPError as exc:
        response = exc
    except (URLError, TimeoutError, OSError) as exc:
        raise WebTransportError(type(exc).__name__) from None

    try:
        with response:
            status_code = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError) as exc:
        raise WebTransportError(type(exc).__name__) from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise WebTransportError("ResponseTooLarge")

    return HttpResult(status_code=status_code, body=body)


def build_request(
    web_url: str,
    web_origin: str,
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    origin: str | None = None,
    content_type: str | None = None,
) -> Request:
    """
    Build one web request with explicit reverse-proxy security headers.

    Args:
        web_url: Internal container URL used for transport.
        web_origin: Configured public browser origin.
        path: Absolute application path, optionally with a query string.
        method: HTTP method.
        body: Optional request bytes used only by denied mutation checks.
        origin: Origin override; an empty string intentionally omits the header.
        content_type: Optional request media type.

    Returns:
        urllib Request targeting the internal web service.
    """
    origin_host = urlsplit(web_origin).netloc
    headers = {
        "Accept": "application/json, text/html;q=0.9",
        "Host": origin_host,
    }
    resolved_origin = web_origin if origin is None else origin

    if resolved_origin:
        headers["Origin"] = resolved_origin
    if content_type:
        headers["Content-Type"] = content_type

    return Request(
        url=f"{web_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )


# --- Evaluating Acceptance Cases
def check_expected_status(
    name: str,
    request: Request,
    expected_status: int,
    path: str,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> WebAcceptanceCheck:
    """Execute one request and compare its exact status without logging bodies."""
    method = request.get_method()
    logger.info(
        "Running web acceptance request | check=%s method=%s path=%s expected_status=%d",
        name,
        method,
        path,
        expected_status,
    )

    try:
        result = open_http_request(request, timeout_seconds=timeout_seconds, opener=opener)
    except WebTransportError as exc:
        logger.warning(
            "Web acceptance transport failed | check=%s method=%s path=%s error_type=%s",
            name,
            method,
            path,
            exc.error_type,
        )
        return WebAcceptanceCheck(
            name=name,
            status="fail",
            details={
                "method": method,
                "path": path,
                "expected_status": expected_status,
                "error_type": exc.error_type,
            },
        )

    return WebAcceptanceCheck(
        name=name,
        status="pass" if result.status_code == expected_status else "fail",
        details={
            "method": method,
            "path": path,
            "expected_status": expected_status,
            "status_code": result.status_code,
        },
    )


def check_alerts_proxy(
    web_url: str,
    web_origin: str,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> WebAcceptanceCheck:
    """Verify the browser proxy returns the typed real-backend alert envelope."""
    name = "web_proxy:alerts_shape"
    request = build_request(web_url, web_origin, ALERTS_PROXY_PATH)
    logger.info("Running web acceptance request | check=%s method=GET path=%s", name, ALERTS_PROXY_PATH)

    try:
        result = open_http_request(request, timeout_seconds=timeout_seconds, opener=opener)
    except WebTransportError as exc:
        return WebAcceptanceCheck(
            name=name,
            status="fail",
            details={"method": "GET", "path": ALERTS_PROXY_PATH, "error_type": exc.error_type},
        )

    shape = "invalid"
    if result.status_code == 200:
        try:
            payload = json.loads(result.body.decode("utf-8"))
            row_count = payload.get("row_count") if isinstance(payload, dict) else None
            alerts = payload.get("alerts") if isinstance(payload, dict) else None
            valid_count = isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0
            valid_alerts = isinstance(alerts, list) and all(isinstance(alert, dict) for alert in alerts)

            if (
                isinstance(payload, dict)
                and payload.get("status") == "success"
                and valid_count
                and valid_alerts
                and row_count == len(alerts)
            ):
                shape = "alert_list"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    return WebAcceptanceCheck(
        name=name,
        status="pass" if result.status_code == 200 and shape == "alert_list" else "fail",
        details={
            "method": "GET",
            "path": ALERTS_PROXY_PATH,
            "expected_status": 200,
            "status_code": result.status_code,
            "response_shape": shape,
        },
    )


def run_web_acceptance(
    web_url: str = DEFAULT_WEB_URL,
    web_origin: str = DEFAULT_WEB_ORIGIN,
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
) -> list[WebAcceptanceCheck]:
    """
    Run read-only pages/proxy checks and policy-denied mutation requests.

    Args:
        web_url: Internal web transport origin, configured through WEB_READINESS_URL.
        web_origin: Public origin used for Host and Origin policy headers.
        timeout_seconds: Per-request timeout.
        opener: urllib-compatible opener injected by offline tests.

    Returns:
        Ordered acceptance checks. No valid mutation or triage request is sent.
    """
    try:
        normalized_url = normalize_http_origin(web_url)
        normalized_origin = normalize_http_origin(web_origin)
    except ValueError as exc:
        logger.warning("Web acceptance configuration rejected | error_type=%s", type(exc).__name__)
        return [
            WebAcceptanceCheck(
                name="web:configuration",
                status="fail",
                details={"error_type": type(exc).__name__},
            )
        ]

    checks = []

    for path in WEB_PAGE_PATHS:
        checks.append(
            check_expected_status(
                name=f"web_page:{path}",
                request=build_request(normalized_url, normalized_origin, path),
                expected_status=200,
                path=path,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
        )

    checks.append(
        check_alerts_proxy(
            web_url=normalized_url,
            web_origin=normalized_origin,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
    )
    checks.append(
        check_expected_status(
            name="web_proxy:unknown_route_denied",
            request=build_request(normalized_url, normalized_origin, UNKNOWN_PROXY_PATH),
            expected_status=405,
            path=UNKNOWN_PROXY_PATH,
            timeout_seconds=timeout_seconds,
            opener=opener,
        )
    )

    denied_posts = (
        (
            "web_proxy:cross_origin_post_denied",
            build_request(
                normalized_url,
                normalized_origin,
                DENIED_APPROVAL_PATH,
                method="POST",
                body=b"{}",
                origin="http://cross-origin.invalid",
                content_type="application/json",
            ),
            403,
        ),
        (
            "web_proxy:missing_origin_post_denied",
            build_request(
                normalized_url,
                normalized_origin,
                DENIED_APPROVAL_PATH,
                method="POST",
                body=b"{}",
                origin="",
                content_type="application/json",
            ),
            403,
        ),
        (
            "web_proxy:text_post_denied",
            build_request(
                normalized_url,
                normalized_origin,
                DENIED_APPROVAL_PATH,
                method="POST",
                body=b"not-json",
                content_type="text/plain",
            ),
            415,
        ),
        (
            "web_proxy:oversized_json_denied",
            build_request(
                normalized_url,
                normalized_origin,
                DENIED_APPROVAL_PATH,
                method="POST",
                body=b"{" + (b" " * MAX_REQUEST_BYTES) + b"}",
                content_type="application/json",
            ),
            413,
        ),
        (
            "web_proxy:malformed_json_denied",
            build_request(
                normalized_url,
                normalized_origin,
                DENIED_APPROVAL_PATH,
                method="POST",
                body=b"{",
                content_type="application/json",
            ),
            400,
        ),
    )

    for name, request, expected_status in denied_posts:
        checks.append(
            check_expected_status(
                name=name,
                request=request,
                expected_status=expected_status,
                path=DENIED_APPROVAL_PATH,
                timeout_seconds=timeout_seconds,
                opener=opener,
            )
        )

    logger.info(
        "Web acceptance completed | passed=%d failed=%d",
        sum(check.status == "pass" for check in checks),
        sum(check.status != "pass" for check in checks),
    )

    return checks


# --- Running CLI Entrypoint
def main() -> int:
    """Run web acceptance from environment-only endpoint configuration."""
    checks = run_web_acceptance()
    summary = {
        "status": "pass" if all(check.status == "pass" for check in checks) else "fail",
        "passed": sum(check.status == "pass" for check in checks),
        "failed": sum(check.status != "pass" for check in checks),
        "checks": [asdict(check) for check in checks],
    }
    print(json.dumps(summary, indent=2))

    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
