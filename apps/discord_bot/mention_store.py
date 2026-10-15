"""Transactional mention claims and metadata-only thread correlation."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
from typing import Callable, Iterator, Any


@dataclass(frozen=True)
class MentionRequestState:
    source_message_id: str
    root_message_id: str
    guild_id: str
    channel_id: str
    actor_id: str
    thread_id: str = ""
    answer_message_id: str = ""
    alert_ref: str = ""
    agent_run_id: str = ""
    status: str = "claimed"
    error_type: str = ""
    source_kind: str = "message"


COLUMNS = tuple(MentionRequestState.__dataclass_fields__)
TABLE = "dq_control.discord_copilot_requests"


class MentionRequestStore:
    """Use PostgreSQL uniqueness, not an eventually consistent audit, for claims."""

    def __init__(self, connection_factory: Callable | None = None) -> None:
        self.connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[Any]:
        if self.connection_factory is not None:
            with self.connection_factory() as connection:
                yield connection
            return
        import psycopg
        database_url = os.getenv("DQ_CONTROL_DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("Discord mention claims require the control database.")
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            yield connection

    def claim(self, state: MentionRequestState) -> bool:
        """Commit a durable claim before any provider call; duplicates fail closed."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
                "ON CONFLICT (source_message_id) DO NOTHING RETURNING source_message_id",
                tuple(asdict(state).values()),
            )
            return cursor.fetchone() is not None

    def save(self, state: MentionRequestState) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            values = asdict(state)
            mutable = tuple(name for name in COLUMNS if name != "source_message_id")
            cursor.execute(
                f"UPDATE {TABLE} SET " + ', '.join(f"{name} = %s" for name in mutable)
                + ", updated_at = now() WHERE source_message_id = %s",
                tuple(values[name] for name in mutable) + (state.source_message_id,),
            )

    def history(self, thread_id: str, actor_id: str, alert_ref: str,
                guild_id: str, channel_id: str) -> list[MentionRequestState]:
        """Fetch only this actor's two completed turns within the exact incident scope."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                "WHERE thread_id = %s AND actor_id = %s AND alert_ref = %s "
                "AND guild_id = %s AND channel_id = %s AND status = 'answered' "
                "ORDER BY created_at DESC LIMIT 2",
                (thread_id, actor_id, alert_ref, guild_id, channel_id),
            )
            return [MentionRequestState(**dict(zip(COLUMNS, row))) for row in cursor.fetchall()]

    def find_thread(self, thread_id: str) -> MentionRequestState | None:
        """Read root correlation only, independently of actor-scoped dialogue history."""
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                "WHERE thread_id = %s AND status = 'answered' ORDER BY created_at ASC LIMIT 1",
                (thread_id,),
            )
            row = cursor.fetchone()
            return MentionRequestState(**dict(zip(COLUMNS, row))) if row else None
