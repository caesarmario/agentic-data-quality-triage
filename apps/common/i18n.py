"""Explicit two-language presentation context; never translates stored evidence."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from typing import Iterator, Literal

Language = Literal["en", "id"]
_language: ContextVar[Language] = ContextVar("operator_language", default="en")


def normalize_language(value: str) -> Language:
    """Accept only supported locale identifiers, not caller-provided instructions."""
    if value not in {"en", "id"}:
        raise ValueError("Language must be en or id.")
    return value  # type: ignore[return-value]


def current_language() -> Language:
    return _language.get()


@contextmanager
def language_context(language: str) -> Iterator[None]:
    token = _language.set(normalize_language(language))
    try:
        yield
    finally:
        _language.reset(token)


def translate(text: str, language: str | None = None, **values: object) -> str:
    """Translate catalog entries only; unknown text and artifacts remain original."""
    locale = normalize_language(language) if language is not None else current_language()
    translated = text
    for name in ("discord", "streamlit", "common", "help", "recovery"):
        module_name = f"apps.common.locales.{name}"
        try:
            catalog = import_module(module_name).MESSAGES
        except ModuleNotFoundError as exc:
            if exc.name not in {module_name, "apps.common.locales"}:
                raise
            continue
        if text in catalog:
            translated = catalog[text] if locale == "id" else text
            break
        # Legacy application-owned Indonesian strings have an English default.
        english = next((key for key, value in catalog.items() if value == text), None)
        if english is not None:
            translated = text if locale == "id" else english
            break
    return translated.format(**values) if values else translated


tr = translate
