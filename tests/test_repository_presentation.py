"""Public documentation must be navigable without local acceptance files."""
from pathlib import Path
import hashlib
import json
import re
import xml.etree.ElementTree as ET

from scripts.validate_repository_presentation import anchors, local_link_errors, validate


def test_catalog_matches_all_repository_owned_ddl_and_migrations():
    from scripts.build_repository_diagrams import build_catalog

    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "docs/diagrams/schema-catalog.json").read_text(encoding="utf-8"))
    assert catalog == build_catalog(root)
    names = {obj["name"] for obj in catalog["objects"]}
    declared = set()
    for directory in ("infra/init/clickhouse", "infra/init/postgres"):
        for path in (root / directory).glob("*.sql"):
            declared.update(re.findall(
                r"CREATE\s+(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w]+\.[\w]+)",
                path.read_text(encoding="utf-8"), re.I,
            ))
    assert names == declared
    recovery_objects = {
        "dq_control.source_snapshots",
        "dq_control.recovery_plans",
        "dq_control.recovery_executions",
        "dq_control.recovery_outbox",
        "dq_control.partition_revisions",
        "dq_control.partition_publications",
        "dq_control.approval_legacy_imports",
        "dq_control.approval_projection_outbox",
    }
    assert recovery_objects <= names
    source_hashes = {item["path"]: item["sha256"] for item in catalog["inputs"]}
    for source in (
        "infra/init/postgres/09_evidence_driven_recovery.sql",
        "infra/init/postgres/10_partition_publication.sql",
        "infra/init/postgres/11_legacy_approval_control.sql",
    ):
        assert len(source_hashes[source]) == 64
        assert source_hashes[source] == hashlib.sha256((root / source).read_bytes()).hexdigest()
    for obj in catalog["objects"]:
        assert obj["columns"] and obj["sources"]
    assert all(r["from"].startswith("dq_control.") and r["to"].startswith("dq_control.")
               for r in catalog["foreign_keys"])
    assert all(r["enforced"] is False for r in catalog["logical_relationships"])


def test_generated_catalog_and_diagrams_are_current_without_rewriting():
    from scripts.build_repository_diagrams import build_artifacts

    root = Path(__file__).resolve().parents[1]
    for name, content in build_artifacts(root).items():
        assert (root / name).read_bytes() == content.encode("utf-8"), name


def test_all_diagrams_are_static_accessible_svg():
    for path in Path("docs/diagrams").glob("*.svg"):
        text = path.read_text(encoding="utf-8")
        root = ET.fromstring(text)
        assert root.attrib.get("viewBox")
        assert "<title" in text and "<desc" in text
        assert not re.search(r"<script\b|\bonload=|javascript:", text, re.I)


def test_recorded_media_checkout_preserves_hashes_with_windows_autocrlf():
    attributes = Path(".gitattributes").read_text(encoding="utf-8")
    for pattern in (
        "tools/demo-video/recorded-diagrams/*.svg",
        "tools/demo-video/*.vtt",
        "tools/demo-video/*.json",
    ):
        assert any(line.split() == [pattern, "text", "eol=lf"] for line in attributes.splitlines())


def test_public_video_assets_are_local_and_match_recorded_hashes():
    import hashlib

    manifest = json.loads(Path("tools/demo-video/asset-provenance.json").read_text(encoding="utf-8"))
    for item in manifest["assets"] + manifest["diagrams"]:
        path = Path(item["path"])
        assert path.is_file() and not path.is_absolute()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    assert all(Path(name).is_file() for name in manifest["documentarySources"])
    verification = json.loads(Path("tools/demo-video/media-verification.json").read_text(encoding="utf-8"))
    assert all(verification["checks"].values())
    assert verification["durationSeconds"] == 180
    assert verification["bytes"] <= 9_500_000 and verification["audioStreams"] == 0


def test_public_documentation_bundle_is_self_contained():
    result = validate()
    assert result["errors"] == [], result["errors"]


def test_heading_links_ignore_fenced_examples_and_keep_duplicate_ids():
    assert anchors("## Run Locally\n```text\n## not a heading\n```\n## Run Locally") == {
        "run-locally", "run-locally-1",
    }


def test_broken_relative_link_and_anchor_are_reported(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("# Example\n[bad](missing.md)\n[wrong](#absent)\n", encoding="utf-8")
    errors = local_link_errors(page, tmp_path)
    assert len(errors) == 2 and "missing.md" in errors[0] and "#absent" in errors[1]


def test_external_links_are_not_network_requests(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("[API](http://localhost:8000/docs) [mail](mailto:example@example.com)", encoding="utf-8")
    assert local_link_errors(page, tmp_path) == []


def test_overview_documents_safety_and_no_key_entrypoints():
    readme = Path("README.md").read_text(encoding="utf-8")
    for required in ("Resolve", "does not fix", "Approval", "baseline", "synthetic", "/help",
                     "without an API key", "EXTERNAL_LLM_ENABLED=false", "MCP", "LangGraph"):
        assert required in readme
    assert "## Limitations and roadmap" not in readme
