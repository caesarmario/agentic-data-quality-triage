####
## ClickHouse Utility for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import os
from datetime import date
from typing import Any

from pipelines.common.logging import logger


# --- Defining Constants
DEFAULT_CLICKHOUSE_HOST = "localhost"
DEFAULT_CLICKHOUSE_PORT = 8123
DEFAULT_CLICKHOUSE_DB   = "dq"
DEFAULT_CLICKHOUSE_USER = "default"


SAFE_IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


# --- Defining Functions
def build_clickhouse_client(
    host: str | None = None,
    port: int | None = None,
    database: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> Any:
    """
    Build a ClickHouse HTTP client from explicit values or environment variables.

    Args:
        host: Optional ClickHouse host override.
        port: Optional ClickHouse HTTP port override.
        database: Optional database override.
        username: Optional ClickHouse username override.
        password: Optional ClickHouse password override.

    Returns:
        clickhouse-connect client instance configured for local Docker execution.
    """
    resolved_host     = host or os.getenv("CLICKHOUSE_HOST", DEFAULT_CLICKHOUSE_HOST)
    resolved_port     = int(port or os.getenv("CLICKHOUSE_HTTP_PORT", str(DEFAULT_CLICKHOUSE_PORT)))
    resolved_database = database or os.getenv("CLICKHOUSE_DB", DEFAULT_CLICKHOUSE_DB)
    resolved_username = username or os.getenv("CLICKHOUSE_USER", DEFAULT_CLICKHOUSE_USER)
    resolved_password = password if password is not None else os.getenv("CLICKHOUSE_PASSWORD", "")

    logger.info(
        "Building ClickHouse client | host=%s port=%s database=%s user=%s",
        resolved_host,
        resolved_port,
        resolved_database,
        resolved_username,
    )

    # Import lazily so CLI help and config validation can run before dependencies are installed.
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=resolved_host,
        port=resolved_port,
        database=resolved_database,
        username=resolved_username,
        password=resolved_password,
    )


def validate_clickhouse_identifier(identifier: str) -> None:
    """
    Validate a ClickHouse identifier before using it in bounded SQL interpolation.

    Args:
        identifier: Database, table, or column identifier.

    Returns:
        None.

    Raises:
        ValueError: If the identifier contains unsupported characters.
    """
    if not identifier or any(char not in SAFE_IDENTIFIER_CHARS for char in identifier):
        logger.error("Unsafe ClickHouse identifier detected | identifier=%s", identifier)
        raise ValueError(f"Unsafe ClickHouse identifier: {identifier}")


def split_table_name(table_name: str) -> tuple[str, str]:
    """
    Split a fully qualified ClickHouse table name into database and table parts.

    Args:
        table_name: Fully qualified table name in database.table format.

    Returns:
        Tuple of database name and table name.

    Raises:
        ValueError: If the table name is not fully qualified or uses unsafe identifiers.
    """
    parts = table_name.split(".")

    if len(parts) != 2:
        logger.error("ClickHouse table name is not fully qualified | table=%s", table_name)
        raise ValueError(f"ClickHouse table name must use database.table format: {table_name}")

    database, table = parts

    validate_clickhouse_identifier(database)
    validate_clickhouse_identifier(table)

    return database, table


def validate_qualified_table_name(table_name: str) -> str:
    """
    Validate and return a fully qualified ClickHouse table name.

    Args:
        table_name: Candidate table name in database.table format.

    Returns:
        The original table name after validation.

    Raises:
        ValueError: If the table name is malformed or unsafe.
    """
    split_table_name(table_name)

    return table_name


def validate_column_name(column_name: str) -> str:
    """
    Validate and return a ClickHouse column identifier.

    Args:
        column_name: Candidate column name.

    Returns:
        The original column name after validation.

    Raises:
        ValueError: If the column name is malformed or unsafe.
    """
    validate_clickhouse_identifier(column_name)

    return column_name


def quote_sql_literal(value: str) -> str:
    """
    Quote a string value for bounded ClickHouse SQL snippets.

    Args:
        value: String literal value.

    Returns:
        Single-quoted SQL literal with embedded quotes escaped.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")

    return f"'{escaped}'"


def format_date_literal(value: date) -> str:
    """
    Format a Python date as a ClickHouse toDate literal.

    Args:
        value: Python date value.

    Returns:
        ClickHouse date literal expression.
    """
    return f"toDate('{value.isoformat()}')"


def drop_date_partition_if_exists(
    client: Any,
    table_name: str,
    partition_dt: date,
) -> bool:
    """
    Drop one Date-partition from a ClickHouse table when it exists.

    Args:
        client: clickhouse-connect client instance.
        table_name: Fully qualified ClickHouse table name.
        partition_dt: Business date partition to drop.

    Returns:
        True when an active partition was found and dropped, otherwise False.
    """
    database, table = split_table_name(table_name)
    partition_id    = partition_dt.isoformat()

    partition_count = scalar(
        client=client,
        query=f"""
            SELECT count()
            FROM system.parts
            WHERE database = {quote_sql_literal(database)}
              AND table = {quote_sql_literal(table)}
              AND partition = {quote_sql_literal(partition_id)}
              AND active
        """,
        default=0,
    )

    if int(partition_count or 0) == 0:
        logger.info(
            "ClickHouse partition does not exist; drop skipped | table=%s partition=%s",
            table_name,
            partition_id,
        )

        return False

    logger.info(
        "Dropping ClickHouse partition | table=%s partition=%s active_parts=%s",
        table_name,
        partition_id,
        partition_count,
    )

    # partition_id is derived from a datetime.date object; table identifiers are validated above.
    client.command(f"ALTER TABLE {table_name} DROP PARTITION {quote_sql_literal(partition_id)}")

    logger.info("ClickHouse partition dropped | table=%s partition=%s", table_name, partition_id)

    return True


def result_rows(client: Any, query: str) -> list[tuple[Any, ...]]:
    """
    Execute a ClickHouse query and return result rows.

    Args:
        client: clickhouse-connect client instance.
        query: SQL query expected to return rows.

    Returns:
        List of row tuples returned by ClickHouse.
    """
    logger.debug("Executing ClickHouse query | sql=%s", query)

    return client.query(query).result_rows


def scalar(client: Any, query: str, default: Any = None) -> Any:
    """
    Execute a ClickHouse query and return the first scalar value.

    Args:
        client: clickhouse-connect client instance.
        query: SQL query expected to return a single scalar value.
        default: Value returned when ClickHouse returns no rows.

    Returns:
        First value from the first row, or default when there is no result.
    """
    rows = result_rows(client=client, query=query)

    if not rows:
        return default

    return rows[0][0]
