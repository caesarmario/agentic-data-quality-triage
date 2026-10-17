"""Offline checks for the public documentation bundle; never contacts providers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    "README.md", "docs/local-operations.md", "docs/architecture.md",
    "docs/data-model.md", "docs/demo-video.md", "docs/repository-refresh-acceptance.md",
    "docs/recovery-workflow.md", "docs/evidence-driven-recovery-acceptance.md",
    "docs/demo-reset.md",
    "docs/discord_output_templates.md",
    "docs/datasentry-v1-acceptance.md",
)


def anchors(text: str) -> set[str]:
    """Match GitHub-style heading IDs, excluding fenced examples."""
    result: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        if fenced or not re.match(r"^#{1,6}\s", line):
            continue
        heading = re.sub(r"^#+\s*", "", line).strip().lower()
        heading = re.sub(r"<[^>]+>", "", heading)
        slug = "".join(c for c in heading if c.isalnum() or c in " _-").replace(" ", "-")
        n = counts.get(slug, 0)
        counts[slug] = n + 1
        result.add(f"{slug}-{n}" if n else slug)
    return result


def local_link_errors(path: Path, root: Path = ROOT) -> list[str]:
    text = path.read_text(encoding="utf-8")
    links = re.findall(r"!?\[[^\]]*\]\(([^)\n]+)\)", text)
    links += re.findall(r'(?:src|href)="([^"]+)"', text)
    errors = []
    for raw in links:
        target = raw.strip("<>").split(' "', 1)[0].replace("&amp;", "&")
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith("//"):
            continue
        resolved = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            errors.append(f"{path.name}: link leaves the repository")
        elif not resolved.exists():
            errors.append(f"{path.name}: missing {target}")
        elif parsed.fragment and resolved.suffix == ".md":
            if unquote(parsed.fragment) not in anchors(resolved.read_text(encoding="utf-8")):
                errors.append(f"{path.name}: missing anchor {target}")
    return errors


def validate(root: Path = ROOT) -> dict:
    errors = []
    for name in PAGES:
        path = root / name
        if not path.exists():
            errors.append(f"Missing public page: {name}")
            continue
        errors.extend(local_link_errors(path, root))
    readme = (root / "README.md").read_text(encoding="utf-8")
    if not 230 <= len(readme.splitlines()) <= 350:
        errors.append("README must remain a concise overview (230-350 lines)")
    for name in ("architecture.svg", "data-model-overview.svg"):
        path = root / "docs/diagrams" / name
        if not path.exists():
            errors.append(f"Missing overview diagram: {name}")
            continue
        try:
            tree = ET.fromstring(path.read_text(encoding="utf-8"))
            if not tree.tag.endswith("svg") or not tree.attrib.get("viewBox"):
                errors.append(f"Invalid diagram canvas: {name}")
        except ET.ParseError:
            errors.append(f"Invalid diagram XML: {name}")
        if re.search(r"<script\b|\bonload=|javascript:", path.read_text(encoding="utf-8"), re.I):
            errors.append(f"Active content in diagram: {name}")
    if not re.search(r"https://github\.com/user-attachments/assets/[\w-]+", readme):
        errors.append("README must reference the uploaded demo video")
    for name in PAGES:
        path = root / name
        if path.exists() and re.search(r"[CD]:[/\\](?:Users|05\. Project DE)[/\\]", path.read_text(encoding="utf-8")):
            errors.append(f"Private host path in public page: {name}")
    return {"status": "failed" if errors else "passed", "pages": len(PAGES), "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = validate()
    print(json.dumps(result, indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
