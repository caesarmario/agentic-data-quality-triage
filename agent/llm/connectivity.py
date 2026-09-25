####
## Bounded Gemini Connectivity Preflight
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

"""Reject unavailable transport before paid evidence workers reserve model calls."""

# --- Importing Libraries
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from agent.llm.config import ResolvedRoute, resolve_route
from agent.llm.provider_errors import summarize_transport_failure
from pipelines.common.logging import logger


# --- Defining Fixed Probe Policy
GEMINI_HOST = "generativelanguage.googleapis.com"
PROBE_TIMEOUT_SECONDS = 12
PROBE_ENVIRONMENT = {
    "PATH", "SYSTEMROOT", "WINDIR", "HOME", "LANG", "LC_ALL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}
TRANSPORT_REASONS = {
    "dns_resolution_failed", "tls_certificate_verification_failed", "tls_connection_failed",
    "transport_timeout", "connection_refused", "connection_reset", "connection_failed",
    "transport_unknown", "probe_timeout", "invalid_probe_output",
}
PREFLIGHT_REASONS = TRANSPORT_REASONS | {"invalid_gemini_route", "invalid_http_status"}


class GeminiConnectivityError(RuntimeError):
    """Carry only a fixed preflight reason into logs and failure audits."""

    def __init__(self, reason: str) -> None:
        """Initialize a sanitized failure without retaining transport exceptions."""
        self.reason = reason if reason in PREFLIGHT_REASONS else "transport_unknown"
        super().__init__(f"Gemini transport preflight failed: {self.reason}; no model workers started.")


# --- Defining Credential-Free Child Probe
def probe_gemini_transport() -> dict[str, object]:
    """
    Resolve the official host and perform one unauthenticated HTTPS GET.

    Returns:
        Safe transport status only. HTTP reachability does not establish API quota,
        credentials, billing, model availability, or successful inference.
    """
    try:
        socket.getaddrinfo(GEMINI_HOST, 443)
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            response = client.get(f"https://{GEMINI_HOST}/")
        return {"reachable": True, "http_status": response.status_code}
    except Exception as exc:
        return {"reachable": False, "reason": summarize_transport_failure(exc) or "transport_unknown"}


# --- Enforcing Preflight Before Paid Fan-Out
def require_gemini_connectivity(run_id: str, route: ResolvedRoute | None = None) -> None:
    """
    Run a wall-clock-bounded, secret-free transport check for strict Gemini acceptance.

    Args:
        run_id: Airflow correlation identifier used in the retained task log.
        route: Already-resolved route for a custom smoke config; defaults to the
            configured cheap_summary route for existing fanout callers.

    Returns:
        None when the configured official Gemini route has reachable HTTPS transport.

    Raises:
        RuntimeError: If policy is disabled, the endpoint differs, or transport fails.
    """
    route = route if route is not None else resolve_route(route_name="cheap_summary")
    try:
        endpoint = urlsplit(route.base_url)
        endpoint_port = endpoint.port
    except ValueError:
        raise GeminiConnectivityError("invalid_gemini_route") from None
    if (
        route.use_heuristic or route.provider_name != "gemini"
        or endpoint.scheme != "https" or endpoint.hostname != GEMINI_HOST
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or endpoint_port not in {None, 443}
    ):
        raise GeminiConnectivityError("invalid_gemini_route")

    logger.info("Starting Gemini transport preflight | run_id=%s model_calls=0", run_id)
    try:
        # Isolate DNS too: HTTP client timeouts alone do not bound every resolver call.
        completed = subprocess.run(
            [sys.executable, "-m", "agent.llm.connectivity", "--probe-gemini"],
            cwd=Path(__file__).resolve().parents[2],
            env={key: value for key, value in os.environ.items() if key in PROBE_ENVIRONMENT},
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        if not isinstance(payload, dict) or completed.returncode != 0:
            raise ValueError("invalid probe output")
    except subprocess.TimeoutExpired:
        payload = {"reachable": False, "reason": "probe_timeout"}
    except (OSError, ValueError, IndexError):
        payload = {"reachable": False, "reason": "invalid_probe_output"}

    if payload.get("reachable") is not True:
        reason = payload.get("reason", "transport_unknown")
        reason = reason if isinstance(reason, str) and reason in TRANSPORT_REASONS else "transport_unknown"
        logger.error("Gemini transport preflight failed | run_id=%s reason=%s model_calls=0 workers_started=0", run_id, reason)
        raise GeminiConnectivityError(reason)

    status = payload.get("http_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise GeminiConnectivityError("invalid_http_status")
    logger.info("Gemini transport reachable | run_id=%s http_status=%d model_calls=0", run_id, status)


# --- Running Only The Fixed Probe
if __name__ == "__main__":
    if sys.argv[1:] != ["--probe-gemini"]:
        raise SystemExit("Only --probe-gemini is supported.")
    print(json.dumps(probe_gemini_transport()))
