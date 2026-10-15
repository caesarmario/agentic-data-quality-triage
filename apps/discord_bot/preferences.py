"""Durable per-guild/user presentation preferences, separate from agent policy."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Any

from apps.common.i18n import normalize_language
from pipelines.common.logging import logger


@contextmanager
def connection() -> Iterator[Any]:
    import psycopg
    url = os.getenv("DQ_CONTROL_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Discord preferences require the control database.")
    with psycopg.connect(url, connect_timeout=5) as conn:
        yield conn


def get_user_language(guild_id: int | str, user_id: int | str) -> str:
    """Fail safely to English when no preference is set or the store is offline."""
    if not str(guild_id).isdigit() or not str(user_id).isdigit():
        return "en"
    try:
        with connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT language FROM dq_control.discord_user_preferences WHERE guild_id=%s AND user_id=%s",
                           (str(guild_id), str(user_id)))
            row = cursor.fetchone()
            return normalize_language(row[0]) if row else "en"
    except Exception as exc:
        logger.warning("Discord language preference unavailable | error_type=%s", type(exc).__name__)
        return "en"


def set_user_language(guild_id: int | str, user_id: int | str, language: str) -> str:
    """Save only the invoking actor's locale; never alter a shared incident card."""
    locale = normalize_language(language)
    if not str(guild_id).isdigit() or not str(user_id).isdigit():
        raise ValueError("A guild and user identity are required.")
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute("INSERT INTO dq_control.discord_user_preferences(guild_id,user_id,language) VALUES(%s,%s,%s) "
                       "ON CONFLICT(guild_id,user_id) DO UPDATE SET language=EXCLUDED.language,updated_at=now()",
                       (str(guild_id), str(user_id), locale))
    return locale
