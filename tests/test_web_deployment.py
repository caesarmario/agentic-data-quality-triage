####
## Premium Web Deployment Contracts for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from pathlib import Path

import yaml


# --- Defining Deployment Tests
def test_triage_success_refreshes_persisted_server_report() -> None:
    """Require refresh after success, not a second POST or automatic triage loop."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/web/components/triage-runner.tsx").read_text()
    assert 'import { useRouter } from "next/navigation";' in source
    assert source.index("if (!response.ok)") < source.index("setResult(payload as TriageRun);")
    assert "setResult(payload as TriageRun);\n      router.refresh();" in source
    assert source.count('fetch("/api/control-plane/api/v1/triage/run"') == 1
    assert 'role="status" aria-live="polite"' in source


def test_web_is_optional_local_only_and_uses_existing_api() -> None:
    """
    Validate deployment isolation without building or starting containers.

    Returns:
        None. The web profile must reuse the API and preserve Streamlit.
    """
    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "infra/docker-compose.yml").read_text())["services"]
    web = services["web"]

    assert web["profiles"] == ["web"]
    assert web["ports"] == ["127.0.0.1:${WEB_PORT:-3000}:3000"]
    assert web["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert "web" in services["api"]["profiles"]
    assert "streamlit" in services
    assert "env_file" not in web
    assert "volumes" not in web
    assert set(web["environment"]) == {
        "NODE_ENV", "NEXT_TELEMETRY_DISABLED", "CONTROL_PLANE_API_URL",
        "CONTROL_PLANE_APPROVAL_TOKEN", "WEB_ORIGIN",
    }
    assert web["environment"]["CONTROL_PLANE_API_URL"] == "http://api:8000"
    assert web["environment"]["WEB_ORIGIN"] == "http://localhost:${WEB_PORT:-3000}"


def test_web_commands_do_not_replace_existing_operator_console() -> None:
    """
    Verify optional launch and stop commands leave API and Streamlit available.

    Returns:
        None. Stopping the web UI must not stop or remove the entire stack.
    """
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text()
    assert "web-up:\n\t$(DC) --profile web up -d --build --wait web" in makefile
    assert "web-down:\n\t$(DC) --profile web stop web" in makefile
    assert "logs-streamlit:" in makefile
    assert "REQUIRE_WEB ?= false" in makefile
    assert "$(if $(filter true 1 yes,$(REQUIRE_WEB)),--require-web,)" in makefile


def test_web_build_checks_security_and_omits_local_secrets() -> None:
    """Require policy tests at image build and exclude developer secrets from context."""
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "apps/web/Dockerfile").read_text()
    dockerignore = (root / "apps/web/.dockerignore").read_text()

    assert "RUN node --experimental-strip-types --test tests/proxy-policy.test.mjs" in dockerfile
    assert "RUN npm test" in dockerfile
    assert "USER nextjs" in dockerfile
    assert ".env*" in dockerignore
    assert "NEXT_TELEMETRY_DISABLED=1" in dockerfile
