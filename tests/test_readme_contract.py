"""Check the local README contract without network, services, or provider calls."""

# --- Importing Libraries
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


# --- Defining Document Locations
PROJECT_ROOT = Path(__file__).resolve().parents[1]
README_PATH  = PROJECT_ROOT / "README.md"

# --- Getting Logger
logger = logging.getLogger(__name__)


# --- Checking Documentation Contracts
def test_readme_final_queries_use_replacing_engines() -> None:
    """Reject FINAL examples for tables whose bootstrap engine cannot merge versions."""
    document = README_PATH.read_text(encoding="utf-8")
    ddl = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "infra/init/clickhouse").glob("*.sql")
    )
    engines = dict(re.findall(
        r"CREATE TABLE IF NOT EXISTS\s+(dq\.\w+)\s*\(.*?ENGINE\s*=\s*(\w+)",
        ddl, flags=re.IGNORECASE | re.DOTALL,
    ))
    references = re.findall(r"FROM\s+(dq\.\w+)\s+FINAL\b", document, re.IGNORECASE)
    assert references, "Expected latest-state inspection examples."
    for table in references:
        assert engines.get(table) == "ReplacingMergeTree", (
            f"README FINAL example does not match bootstrap engine for {table}"
        )


def test_readme_local_links_exist() -> None:
    """Check Markdown and HTML file links. Receives nothing; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    targets  = re.findall(r"\]\(([^\s)]+)\)", document)
    targets += re.findall(r'(?:href|src)="([^"]+)"', document)
    checked  = 0

    for target in targets:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue

        # Resolve only repository-relative links; no external link is requested.
        path = (PROJECT_ROOT / unquote(parsed.path)).resolve()
        assert path.is_relative_to(PROJECT_ROOT), target
        assert path.exists(), f"Missing README target: {target}"
        checked += 1

    assert checked > 0
    logger.info("README local links verified | links=%d", checked)


def test_readme_navigation_anchors_exist() -> None:
    """Validate the table of contents. Receives nothing; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    prose    = re.sub(r"```[^\n]*\n.*?```", "", document, flags=re.DOTALL)
    headings = re.findall(r"^#{1,6} (.+)$", prose, flags=re.MULTILINE)
    anchors  = {
        re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        for heading in headings
    }
    links = re.findall(r"\]\(#([^)]*)\)", prose)

    assert links
    assert all(link in anchors for link in links), set(links) - anchors
    logger.info("README navigation verified | anchors=%d", len(links))


def test_readme_dag_ids_match_explicit_files() -> None:
    """Match documented DAG IDs to source files. Receives nothing; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    dag_ids  = set(re.findall(r"\b\d{2}_dag_dq_[a-z0-9_]+\b", document))

    assert "00_dag_dq_platform_daily_orchestrator" in dag_ids
    assert "91_dag_dq_platform_validation" in dag_ids
    for dag_id in dag_ids:
        assert (PROJECT_ROOT / "dags" / f"{dag_id}.py").is_file(), dag_id

    logger.info("README DAG references verified | dags=%d", len(dag_ids))


def test_readme_json_examples_parse_and_remain_safe() -> None:
    """Parse example payloads and check demo defaults. No args; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    blocks   = re.findall(r"```json\n(.*?)\n```", document, flags=re.DOTALL)

    assert blocks
    for block in blocks:
        payload = json.loads(block)
        if "incident_scenario" in payload:
            scenario = payload["incident_scenario"]
            assert (PROJECT_ROOT / "configs" / "incidents" / f"{scenario}.yml").is_file()
            assert payload["run_triage"] is False
        if "approval_request_id" in payload:
            assert payload["dry_run"] is True
            assert payload["reset_dag_run"] is False

    logger.info("README JSON examples verified | examples=%d", len(blocks))


def test_readme_fences_and_local_acceptance_boundary() -> None:
    """Keep formatting and zero-cost onboarding explicit. No args; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    fences   = re.findall(r"^```.*$", document, flags=re.MULTILINE)

    assert len(fences) % 2 == 0
    assert "```mermaid" in document
    assert "EXTERNAL_LLM_ENABLED=false" in document
    assert "final acceptance comes from an Airflow DagRun" in document
    assert not re.search(r"^TBD\s*$", document, flags=re.MULTILINE)
    assert not re.search(r"[A-Za-z]:\\(?:Users|05\.)", document)
    logger.info("README formatting and acceptance boundary verified")


def test_readme_trigger_scripts_exist() -> None:
    """Resolve documented Airflow trigger scripts. No args; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    scripts  = set(re.findall(r"/opt/airflow/project/(scripts/[a-z_]+\.py)", document))

    assert scripts
    for script in scripts:
        assert (PROJECT_ROOT / script).is_file(), script

    logger.info("README trigger paths verified | scripts=%d", len(scripts))


def test_readme_api_routes_exist() -> None:
    """Compare documented endpoints with API decorators. No args; returns None."""
    document = README_PATH.read_text(encoding="utf-8")
    module   = ast.parse((PROJECT_ROOT / "apps/api/main.py").read_text(encoding="utf-8-sig"))
    routes   = set()

    # AST inspection avoids importing the API or initializing service clients.
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue
            path = decorator.args[0]
            if isinstance(path, ast.Constant) and isinstance(path.value, str):
                routes.add((decorator.func.attr.upper(), path.value))

    documented = re.findall(r"`(GET|POST) (/[^` ]+)`", document)
    assert documented
    assert set(documented).issubset(routes), set(documented) - routes
    logger.info("README API routes verified | routes=%d", len(documented))
