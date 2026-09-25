####
## Safe Provider Error Diagnostics for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import json
import os
import re
import socket
import ssl
from typing import Any

import httpx

from pipelines.common.logging import logger


# --- Defining Diagnostic Fields
DIAGNOSTIC_FIELDS = (
    "http_status", "provider_status", "provider_reason", "error_hint",
    "quota_violations", "retry_delay",
    "transport_reason",
)


# --- Sanitizing Provider Metadata
def summarize_transport_failure(exc: BaseException) -> str:
    """
    Classify bounded exception causes without exposing free-text connection details.

    Args:
        exc: SDK or transport exception with optional chained causes.

    Returns:
        Fixed diagnostic code, or an empty string when the cause is unknown.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    reason = ""
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))

        if isinstance(current, socket.gaierror):
            return "dns_resolution_failed"
        if isinstance(current, ssl.SSLCertVerificationError):
            return "tls_certificate_verification_failed"
        if isinstance(current, ssl.SSLError):
            reason = "tls_connection_failed"
        elif isinstance(current, (httpx.TimeoutException, TimeoutError)):
            reason = "transport_timeout"
        elif isinstance(current, ConnectionRefusedError):
            reason = "connection_refused"
        elif isinstance(current, ConnectionResetError):
            reason = "connection_reset"
        elif not reason and isinstance(current, httpx.ConnectError):
            reason = "connection_failed"

        current = current.__cause__ or current.__context__

    return reason


def safe_identifier(value: Any) -> str:
    """
    Keep bounded machine identifiers while excluding credentials and free text.

    Args:
        value: Untrusted provider metadata value.

    Returns:
        Safe identifier, or an empty string when the value is not suitable.
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""

    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,180}", text):
        return ""
    if any(marker in text for marker in ("AQ.", "AIza", "sk-", "://")):
        return ""

    # Some providers echo credentials in unexpected fields; redact known secrets too.
    secrets = (
        secret for name, secret in os.environ.items()
        if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        and len(secret) >= 8
    )
    return "" if any(secret in text for secret in secrets) else text


def summarize_provider_error(exc: Exception) -> dict[str, str]:
    """
    Extract diagnostic codes from an SDK error without retaining its raw body.

    Args:
        exc: Provider exception, optionally with status_code and JSON body fields.

    Returns:
        String-valued status, quota, and retry details safe for task logs and audit.
        Message-derived hints are diagnostic clues, not confirmed root causes.
    """
    result: dict[str, str] = {}
    if transport_reason := summarize_transport_failure(exc):
        result["transport_reason"] = transport_reason
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        result["http_status"] = str(status)

    body = getattr(exc, "body", None)
    body = body if isinstance(body, dict) else {}
    body = body.get("error", body)
    body = body if isinstance(body, dict) else {}

    for target, value in (
        ("provider_status", body.get("status")),
        ("provider_reason", body.get("code") or body.get("type")),
    ):
        if safe := safe_identifier(value):
            result[target] = safe

    # Inspect the message only to assign fixed hints; never log or persist it.
    message = body.get("message", "")
    message = message.lower() if isinstance(message, str) else ""
    for phrase, hint in (
        ("reported as leaked", "key_reported_leaked"),
        ("billing", "billing_mentioned"),
        ("balance", "balance_mentioned"),
        ("quota", "quota_mentioned"),
        ("rate limit", "rate_limit_mentioned"),
        ("overloaded", "capacity_mentioned"),
    ):
        if phrase in message:
            result["error_hint"] = hint
            break

    details = body.get("details", [])
    details = details if isinstance(details, list) else []
    violations = []
    for detail in details[:10]:
        if not isinstance(detail, dict):
            continue
        kind = detail.get("@type")
        if kind == "type.googleapis.com/google.rpc.ErrorInfo":
            if reason := safe_identifier(detail.get("reason")):
                result["provider_reason"] = reason
        elif kind == "type.googleapis.com/google.rpc.RetryInfo":
            delay = detail.get("retryDelay")
            if isinstance(delay, str) and re.fullmatch(r"\d{1,8}(?:\.\d{1,9})?s", delay):
                result["retry_delay"] = delay
        elif kind == "type.googleapis.com/google.rpc.QuotaFailure":
            items = detail.get("violations", [])
            for item in (items[:10] if isinstance(items, list) else []):
                if isinstance(item, dict):
                    safe = {
                        key: value for key in ("quotaMetric", "quotaId", "quotaValue")
                        if (value := safe_identifier(item.get(key)))
                    }
                    if safe:
                        violations.append(safe)

    if violations:
        result["quota_violations"] = json.dumps(violations[:10], sort_keys=True)

    logger.info("Collected safe provider diagnostics | fields=%s", sorted(result))
    return result
