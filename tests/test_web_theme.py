"""Check source theme tokens for readable normal-size operator text."""

# --- Importing Libraries
import re
from pathlib import Path

import pytest


# --- Defining Theme Checks
CSS = Path(__file__).resolve().parents[1] / "apps/web/app/globals.css"


def luminance(color: str) -> float:
    """Return WCAG relative luminance for a six-digit sRGB theme token."""
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


@pytest.mark.parametrize("theme", [":root", ':root[data-theme="dark"]'])
@pytest.mark.parametrize("background", ["accent", "success", "danger"])
def test_solid_operator_controls_have_readable_contrast(theme: str, background: str) -> None:
    """Theme changes must preserve at least 4.5:1 normal-text contrast."""
    source = CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(theme) + r"\s*\{([^}]+)\}", source).group(1)
    tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", block))
    foreground, backing = sorted([luminance(tokens["on-solid"]), luminance(tokens[background])])
    assert (backing + 0.05) / (foreground + 0.05) >= 4.5


@pytest.mark.parametrize("selector", [
    ".brand-mark", ".nav-link.active", ".button.primary", ".button.success",
    ".button.danger", ".depth-marker",
])
def test_solid_controls_use_theme_foreground(selector: str) -> None:
    """A fixed white foreground must not override dark-mode contrast tokens."""
    source = CSS.read_text(encoding="utf-8")
    block = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", source).group(1)
    assert "color: var(--on-solid);" in block
