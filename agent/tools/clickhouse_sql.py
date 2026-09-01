####
## Guarded ClickHouse SQL Tool for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Importing Libraries
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


# --- Configuring Project Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Add repo root before importing project packages when this file is executed by path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tools.audit_log import hash_sql, write_agent_audit_event
from pipelines.common.clickhouse import build_clickhouse_client
from pipelines.common.logging import logger


# --- Defining Constants
TOOL_NAME          = "clickhouse_sql"
DEFAULT_HARD_LIMIT = 100
MAX_HARD_LIMIT     = 1000

ALLOWED_QUERY_PREFIXES = (
    "select",
    "with",
    "show",
    "describe",
    "desc",
    "explain",
)

DENYLISTED_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "truncate",
    "create",
    "replace",
    "optimize",
    "attach",
    "detach",
    "rename",
    "grant",
    "revoke",
    "set",
    "use",
    "system",
    "kill",
    "backup",
    "restore",
    "call",
)

DENYLISTED_READ_PATTERNS = {
    "external_table_function": (
        r"\b(?:s3|url|file|remote|remoteSecure|mysql|postgresql|jdbc|odbc|hdfs|"
        r"azureBlobStorage|gcs|deltaLake|iceberg|cluster|clusterAllReplicas)\s*\("
    ),
    "outfile_clause": r"\binto\s+outfile\b",
}

LARGE_TABLE_DATE_COLUMNS = {
    "dq.raw_orders": ("dt", "order_date"),
    "dq.stg_orders": ("dt", "order_date"),
    "dq.fct_orders_daily": ("dt",),
    "dq.dq_check_results": ("dt", "run_at", "created_at"),
    "dq.data_profile_results": ("dt", "run_at", "created_at"),
    "dq.alerts": ("dt", "created_at", "updated_at"),
    "dq.pipeline_runs": ("logical_date", "partition_dt", "started_at", "created_at"),
}


# --- Defining Classes
class GuardrailViolation(ValueError):
    """
    Raised when a SQL statement violates agent SQL guardrails.

    Args:
        message: Human-readable guardrail failure reason.
    """


class SqlGuardrailConfig(BaseModel):
    """
    Runtime settings for guarded SQL execution.

    Attributes:
        hard_limit: Maximum allowed rows returned by SELECT/WITH statements.
        require_date_filter: Whether large table queries require a date-like predicate.
        allow_describe: Whether DESCRIBE/DESC statements are allowed.
        allow_show: Whether SHOW statements are allowed.
        allow_explain: Whether EXPLAIN statements are allowed.
    """

    hard_limit: int            = Field(default=DEFAULT_HARD_LIMIT, ge=1, le=MAX_HARD_LIMIT)
    require_date_filter: bool  = True
    allow_describe: bool       = True
    allow_show: bool           = True
    allow_explain: bool        = True


class SqlExecutionResult(BaseModel):
    """
    Result returned by the guarded ClickHouse SQL tool.

    Attributes:
        status: Tool execution status.
        original_sql: SQL submitted by the caller.
        executed_sql: SQL after guardrail normalization.
        columns: Result column names.
        rows: JSON-serializable result rows.
        row_count: Number of rows returned.
        duration_ms: Query execution duration in milliseconds.
        sql_hash: Hash written to audit logs.
        guardrails_applied: Guardrails applied during validation.
    """

    status: str
    original_sql: str
    executed_sql: str
    columns: list[str]                 = Field(default_factory=list)
    rows: list[dict[str, Any]]         = Field(default_factory=list)
    row_count: int                     = 0
    duration_ms: int                   = 0
    sql_hash: str                      = ""
    guardrails_applied: list[str]      = Field(default_factory=list)

    @model_validator(mode="after")
    def sync_row_count(self) -> "SqlExecutionResult":
        """
        Keep row_count aligned with result rows when needed.

        Returns:
            Current SqlExecutionResult instance.
        """
        if self.row_count == 0 and self.rows:
            self.row_count = len(self.rows)

        return self


# --- Defining Functions
def remove_sql_comments(sql: str) -> str:
    """
    Remove SQL comments before guardrail analysis.

    Args:
        sql: Raw SQL statement.

    Returns:
        SQL without line or block comments.
    """
    without_block_comments = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    without_line_comments  = re.sub(r"--.*?$", " ", without_block_comments, flags=re.MULTILINE)

    return without_line_comments


def normalize_sql(sql: str) -> str:
    """
    Normalize whitespace and trailing semicolon in a SQL statement.

    Args:
        sql: Raw SQL statement.

    Returns:
        Normalized SQL statement.

    Raises:
        GuardrailViolation: If the statement is empty or contains multiple statements.
    """
    cleaned = remove_sql_comments(sql).strip()

    if not cleaned:
        raise GuardrailViolation("SQL statement is empty after removing comments.")

    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    # Single-statement enforcement prevents sneaking mutation statements after a read query.
    if ";" in cleaned:
        raise GuardrailViolation("Only one SQL statement is allowed.")

    return re.sub(r"\s+", " ", cleaned).strip()


def first_keyword(sql: str) -> str:
    """
    Extract the first SQL keyword from a normalized statement.

    Args:
        sql: Normalized SQL statement.

    Returns:
        Lowercase first keyword.
    """
    return sql.split(maxsplit=1)[0].lower()


def assert_allowed_query_prefix(sql: str, config: SqlGuardrailConfig) -> None:
    """
    Ensure the SQL statement starts with an allowed read-only prefix.

    Args:
        sql: Normalized SQL statement.
        config: Guardrail configuration.

    Returns:
        None.

    Raises:
        GuardrailViolation: If the query prefix is not allowed.
    """
    keyword = first_keyword(sql)

    if keyword not in ALLOWED_QUERY_PREFIXES:
        raise GuardrailViolation(f"Only read-only SQL is allowed. First keyword was: {keyword}")

    if keyword in {"describe", "desc"} and not config.allow_describe:
        raise GuardrailViolation("DESCRIBE statements are disabled by guardrail config.")

    if keyword == "show" and not config.allow_show:
        raise GuardrailViolation("SHOW statements are disabled by guardrail config.")

    if keyword == "explain" and not config.allow_explain:
        raise GuardrailViolation("EXPLAIN statements are disabled by guardrail config.")


def assert_no_denylisted_keywords(sql: str) -> None:
    """
    Reject SQL containing mutation, admin, or session-control keywords.

    Args:
        sql: Normalized SQL statement.

    Returns:
        None.

    Raises:
        GuardrailViolation: If a denied keyword is present.
    """
    lower_sql = sql.lower()

    for keyword in DENYLISTED_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", lower_sql):
            raise GuardrailViolation(f"Denied SQL keyword detected: {keyword}")


def assert_no_unsafe_read_patterns(sql: str) -> None:
    """
    Reject read-looking SQL that can access external systems or write files.

    Args:
        sql: Normalized SQL statement.

    Returns:
        None.

    Raises:
        GuardrailViolation: If a denied table function or output clause is present.
    """
    for pattern_name, pattern in DENYLISTED_READ_PATTERNS.items():
        if re.search(pattern, sql, flags=re.IGNORECASE):
            raise GuardrailViolation(f"Denied SQL read pattern detected: {pattern_name}")


def mentioned_large_tables(sql: str) -> list[str]:
    """
    Identify guarded large tables referenced by a SQL statement.

    Args:
        sql: Normalized SQL statement.

    Returns:
        List of fully qualified table names detected in the SQL.
    """
    lower_sql = sql.lower()
    mentioned = []

    for table_name in LARGE_TABLE_DATE_COLUMNS:
        database, table = table_name.split(".", 1)
        full_pattern    = rf"\b{re.escape(database)}\s*\.\s*{re.escape(table)}\b"
        bare_pattern    = rf"\b{re.escape(table)}\b"

        if re.search(full_pattern, lower_sql) or re.search(bare_pattern, lower_sql):
            mentioned.append(table_name)

    return mentioned


def extract_where_like_text(sql: str) -> str:
    """
    Extract the SQL text after WHERE for simple guardrail predicate inspection.

    Args:
        sql: Normalized SQL statement.

    Returns:
        Lowercase text after the first WHERE clause, or an empty string when absent.
    """
    lower_sql = sql.lower()

    if " where " not in lower_sql:
        return ""

    where_text = lower_sql.split(" where ", 1)[1]

    for boundary in (" group by ", " order by ", " limit ", " having ", " union ", " settings ", " format "):
        if boundary in where_text:
            where_text = where_text.split(boundary, 1)[0]

    return where_text


def has_date_filter(where_text: str, date_columns: tuple[str, ...]) -> bool:
    """
    Check whether a WHERE clause contains a date-like filter predicate.

    Args:
        where_text: Lowercase SQL WHERE text.
        date_columns: Accepted date/date-time columns for the referenced table.

    Returns:
        True when a supported date column predicate is found.
    """
    if not where_text:
        return False

    for column in date_columns:
        pattern = rf"\b{re.escape(column.lower())}\b\s*(=|!=|<>|>=|>|<=|<|between\b|in\b)"

        if re.search(pattern, where_text):
            return True

    return False


def assert_required_date_filters(sql: str, config: SqlGuardrailConfig) -> None:
    """
    Ensure large table queries include a dt/date-like filter.

    Args:
        sql: Normalized SQL statement.
        config: Guardrail configuration.

    Returns:
        None.

    Raises:
        GuardrailViolation: If a large table is queried without an accepted date predicate.
    """
    if not config.require_date_filter:
        return

    tables     = mentioned_large_tables(sql)
    where_text = extract_where_like_text(sql)

    for table_name in tables:
        date_columns = LARGE_TABLE_DATE_COLUMNS[table_name]

        if not has_date_filter(where_text=where_text, date_columns=date_columns):
            accepted = ", ".join(date_columns)
            raise GuardrailViolation(f"Query against {table_name} requires a date filter on one of: {accepted}")


def is_result_query(sql: str) -> bool:
    """
    Decide whether a query should receive a hard LIMIT.

    Args:
        sql: Normalized SQL statement.

    Returns:
        True for SELECT and WITH statements, otherwise False.
    """
    return first_keyword(sql) in {"select", "with"}


def enforce_limit(sql: str, hard_limit: int) -> tuple[str, list[str]]:
    """
    Add or cap a LIMIT clause for result-returning queries.

    Args:
        sql: Normalized SQL statement.
        hard_limit: Maximum allowed row count.

    Returns:
        Tuple of SQL after LIMIT enforcement and applied guardrail labels.
    """
    applied = []

    if not is_result_query(sql):
        return sql, applied

    limit_match = re.search(r"\blimit\s+(\d+)\b", sql, flags=re.IGNORECASE)

    if not limit_match:
        applied.append(f"limit_added_{hard_limit}")
        return f"{sql} LIMIT {hard_limit}", applied

    requested_limit = int(limit_match.group(1))

    if requested_limit <= hard_limit:
        applied.append(f"limit_ok_{requested_limit}")
        return sql, applied

    capped_sql = re.sub(
        r"\blimit\s+\d+\b",
        f"LIMIT {hard_limit}",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )

    applied.append(f"limit_capped_{requested_limit}_to_{hard_limit}")

    return capped_sql, applied


def guard_sql(sql: str, config: SqlGuardrailConfig | None = None) -> tuple[str, list[str]]:
    """
    Validate and normalize a SQL statement before execution.

    Args:
        sql: Raw SQL statement submitted by the agent.
        config: Optional guardrail config.

    Returns:
        Tuple of guarded SQL and applied guardrail labels.

    Raises:
        GuardrailViolation: If the SQL violates read-only, denylist, date-filter, or statement-count rules.
    """
    resolved_config = config or SqlGuardrailConfig()
    normalized_sql  = normalize_sql(sql)

    assert_allowed_query_prefix(normalized_sql, resolved_config)
    assert_no_denylisted_keywords(normalized_sql)
    assert_no_unsafe_read_patterns(normalized_sql)
    assert_required_date_filters(normalized_sql, resolved_config)

    guarded_sql, applied = enforce_limit(normalized_sql, resolved_config.hard_limit)
    applied.insert(0, "read_only_checked")

    if resolved_config.require_date_filter:
        applied.append("date_filter_checked")

    logger.info("SQL guardrails passed | applied=%s", applied)

    return guarded_sql, applied


def normalize_cell(value: Any) -> Any:
    """
    Convert ClickHouse cell values into JSON-serializable values.

    Args:
        value: Raw ClickHouse cell value.

    Returns:
        JSON-serializable cell value.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return float(value)

    return value


def rows_to_dicts(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """
    Convert ClickHouse row tuples into dictionaries.

    Args:
        columns: Result column names.
        rows: Result row tuples.

    Returns:
        List of row dictionaries.
    """
    return [
        {column: normalize_cell(value) for column, value in zip(columns, row)}
        for row in rows
    ]


def run_guarded_sql(
    sql: str,
    agent_run_id: UUID | str | None = None,
    alert_id: UUID | str | None = None,
    alert_key: str = "",
    hard_limit: int = DEFAULT_HARD_LIMIT,
    require_date_filter: bool = True,
    clickhouse_host: str | None = None,
    clickhouse_port: int | None = None,
) -> SqlExecutionResult:
    """
    Execute a guarded read-only SQL query against ClickHouse and audit the tool call.

    Args:
        sql: Raw SQL statement submitted by the agent.
        agent_run_id: Optional agent run UUID.
        alert_id: Optional alert UUID.
        alert_key: Optional stable alert key.
        hard_limit: Maximum allowed rows for SELECT/WITH statements.
        require_date_filter: Whether large tables require a date-like filter.
        clickhouse_host: Optional ClickHouse host override.
        clickhouse_port: Optional ClickHouse HTTP port override.

    Returns:
        SqlExecutionResult with columns, rows, row count, and guardrail metadata.

    Raises:
        GuardrailViolation: If guardrails block the query.
        clickhouse_connect.driver.exceptions.ClickHouseError: If ClickHouse execution fails.
    """
    client                = build_clickhouse_client(host=clickhouse_host, port=clickhouse_port)
    resolved_agent_run_id = UUID(str(agent_run_id)) if agent_run_id else uuid4()
    config                = SqlGuardrailConfig(hard_limit=hard_limit, require_date_filter=require_date_filter)
    started_monotonic     = time.monotonic()

    logger.info("Running guarded SQL | agent_run_id=%s alert_key=%s", resolved_agent_run_id, alert_key)

    try:
        guarded_sql, guardrails_applied = guard_sql(sql=sql, config=config)
        query_result                    = client.query(guarded_sql)
        duration_ms                     = int((time.monotonic() - started_monotonic) * 1000)
        columns                         = list(query_result.column_names or [])
        rows                            = rows_to_dicts(columns=columns, rows=query_result.result_rows)
        result                          = SqlExecutionResult(
            status="success",
            original_sql=sql,
            executed_sql=guarded_sql,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            duration_ms=duration_ms,
            sql_hash=hash_sql(guarded_sql),
            guardrails_applied=guardrails_applied,
        )

        write_agent_audit_event(
            client=client,
            action="run_guarded_sql",
            status="success",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "original_sql": sql,
                "executed_sql": guarded_sql,
                "hard_limit": hard_limit,
                "require_date_filter": require_date_filter,
            },
            output_payload={
                "columns": columns,
                "row_count": len(rows),
                "guardrails_applied": guardrails_applied,
            },
            sql=guarded_sql,
            row_count=len(rows),
        )

        logger.info("Guarded SQL completed | agent_run_id=%s rows=%d duration_ms=%d", resolved_agent_run_id, len(rows), duration_ms)

        return result

    except GuardrailViolation as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.warning("Guarded SQL blocked | agent_run_id=%s reason=%s", resolved_agent_run_id, exc)

        write_agent_audit_event(
            client=client,
            action="run_guarded_sql",
            status="blocked",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "original_sql": sql,
                "hard_limit": hard_limit,
                "require_date_filter": require_date_filter,
            },
            output_payload={"blocked_reason": str(exc)},
            error_message=str(exc),
            sql=sql,
        )

        raise

    except Exception as exc:
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        logger.exception("Guarded SQL failed | agent_run_id=%s", resolved_agent_run_id)

        write_agent_audit_event(
            client=client,
            action="run_guarded_sql",
            status="failed",
            agent_run_id=resolved_agent_run_id,
            alert_id=alert_id,
            alert_key=alert_key,
            tool_name=TOOL_NAME,
            duration_ms=duration_ms,
            input_payload={
                "original_sql": sql,
                "hard_limit": hard_limit,
                "require_date_filter": require_date_filter,
            },
            output_payload={"error_type": type(exc).__name__},
            error_message=str(exc),
            sql=sql,
        )

        raise


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser for guarded SQL execution.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(description="Run a guarded read-only ClickHouse SQL query.")

    parser.add_argument("--sql", required=True, help="Read-only SQL statement to execute.")
    parser.add_argument("--alert-key", default="", help="Optional alert key for audit context.")
    parser.add_argument("--agent-run-id", default=None, help="Optional agent run UUID.")
    parser.add_argument("--hard-limit", type=int, default=DEFAULT_HARD_LIMIT, help="Maximum rows returned.")
    parser.add_argument("--allow-no-date-filter", action="store_true", help="Disable date-filter requirement for large tables.")
    parser.add_argument("--clickhouse-host", default=None, help="Optional ClickHouse host override.")
    parser.add_argument("--clickhouse-port", type=int, default=None, help="Optional ClickHouse HTTP port override.")

    return parser


def main() -> None:
    """
    Parse CLI arguments and execute one guarded SQL query.

    Returns:
        None.
    """
    parser = build_parser()
    args   = parser.parse_args()

    result = run_guarded_sql(
        sql=args.sql,
        agent_run_id=args.agent_run_id,
        alert_key=args.alert_key,
        hard_limit=args.hard_limit,
        require_date_filter=not args.allow_no_date_filter,
        clickhouse_host=args.clickhouse_host,
        clickhouse_port=args.clickhouse_port,
    )

    print(result.model_dump_json(indent=2))


# --- Running CLI Entrypoint
if __name__ == "__main__":
    main()
