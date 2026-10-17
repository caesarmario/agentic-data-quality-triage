"""Guarded, journaled single-table partition publication.

Migration: infra/init/postgres/10_partition_publication.sql (never applied here).
Every platform writer must use publication_lock('orders', dt). PostgreSQL session
advisory locks are cooperative, not fencing tokens for external ClickHouse writers.
Staging and backups are retained deliberately; cleanup/reconciliation is operational
work, never an automatic response to an uncertain ClickHouse command.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterator
from uuid import uuid4

from pipelines.common.clickhouse import format_date_literal, quote_sql_literal, split_table_name
from pipelines.common.logging import logger


# --- Defining Contracts
LIVE_TABLES = frozenset({'dq.raw_orders', 'dq.stg_orders', 'dq.fct_orders_daily'})
UNRESOLVED = ('prepared', 'publishing', 'unknown', 'compensating')
INTENTS = frozenset({'daily', 'recovery', 'simulation_empty'})


class PublicationConflict(RuntimeError):
    """Scope, prerequisites, structure, or revision no longer matches approval."""


class PublicationUnknown(RuntimeError):
    """A durable in-flight record requires reconciliation, not an automatic retry."""


def advisory_lock_key(dataset: str, partition_dt: date) -> int:
    """Return the shared signed bigint lock key, independent of writer/table."""
    if dataset != 'orders' or type(partition_dt) is not date:
        raise ValueError('Only the orders dataset and a concrete business date are supported')
    digest = hashlib.sha256(f'datasentry:publication:{dataset}:{partition_dt.isoformat()}'.encode()).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def execution_identity(writer: str) -> str:
    """Reuse Airflow task identity across retries; CLI executions get a fresh ID."""
    run_id = os.getenv('AIRFLOW_CTX_DAG_RUN_ID')
    if run_id:
        return ':'.join((writer, os.getenv('AIRFLOW_CTX_DAG_ID', ''), run_id,
                         os.getenv('AIRFLOW_CTX_TASK_ID', '')))
    return f'{writer}:{uuid4().hex}'


def scratch_database(execution_id: str, partition_dt: date) -> str:
    """Return a non-client-controlled isolated database identifier."""
    digest = hashlib.sha256(f'{execution_id}:{partition_dt.isoformat()}'.encode()).hexdigest()[:24]
    return f'dq_pub_{digest}'


def _qualified(table: str) -> str:
    split_table_name(table)
    return table


def _live(table: str) -> str:
    if table not in LIVE_TABLES:
        raise ValueError('Publication target must be an allowlisted orders table')
    return table


@dataclass(frozen=True)
class Preconditions:
    revision: int
    watermark: str


@dataclass(frozen=True)
class Structure:
    properties: tuple[Any, ...]
    columns: tuple[tuple[Any, ...], ...]
    ddl_body: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(repr(self).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedPartition:
    publication_id: str
    execution_id: str
    target_table: str
    stage_table: str
    backup_table: str
    partition_dt: date
    preconditions: Preconditions
    before_fingerprint: str
    staged_fingerprint: str
    structure: Structure
    rows: int
    intent: str


@dataclass
class PublicationLock:
    """A live PostgreSQL session lock; never serialize this across Airflow tasks."""
    connection: Any
    dataset: str
    partition_dt: date
    execution_id: str
    active: bool = True
    owned_revisions: set[Preconditions] = field(default_factory=set)

    def sql(self, statement: str, parameters: tuple = (), *, fetch: bool = False) -> Any:
        """Execute a bounded control query and commit without releasing session lock."""
        if not self.active:
            raise PublicationConflict('Publication lock is no longer active')
        cursor = self.connection.cursor()
        try:
            cursor.execute(statement, parameters)
            result = cursor.fetchone() if fetch else None
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def state(self) -> Preconditions:
        row = self.sql(
            'SELECT revision, watermark FROM dq_control.partition_revisions '
            'WHERE dataset = %s AND partition_dt = %s',
            (self.dataset, self.partition_dt), fetch=True,
        )
        if row is None:
            raise PublicationConflict('Partition revision row is missing')
        return Preconditions(int(row[0]), str(row[1]))

    def require(self, expected: Preconditions) -> None:
        if self.state() != expected:
            raise PublicationConflict('Partition revision or watermark changed')

    def journal_status(self, publication_id: str) -> str:
        row = self.sql(
            'SELECT status FROM dq_control.partition_publications '
            'WHERE publication_id = %s AND execution_id = %s AND dataset = %s AND partition_dt = %s',
            (publication_id, self.execution_id, self.dataset, self.partition_dt), fetch=True,
        )
        if row is None:
            raise PublicationConflict('Publication does not belong to this locked execution')
        return str(row[0])

    def transition(self, publication_id: str, before: str, after: str) -> None:
        row = self.sql(
            'UPDATE dq_control.partition_publications SET status = %s, updated_at = now() '
            'WHERE publication_id = %s AND execution_id = %s AND status = %s RETURNING publication_id',
            (after, publication_id, self.execution_id, before), fetch=True,
        )
        if row is None:
            raise PublicationConflict('Publication state changed or is not retryable')

    def finish(self, prepared: PreparedPartition, expected: Preconditions, *, compensation: bool = False) -> Preconditions:
        """Commit the revision and terminal journal state in one PostgreSQL transaction."""
        status = 'compensated' if compensation else 'published'
        inflight = 'compensating' if compensation else 'publishing'
        watermark = f'{prepared.publication_id}:{status}'
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                'UPDATE dq_control.partition_revisions SET revision = revision + 1, watermark = %s '
                'WHERE dataset = %s AND partition_dt = %s AND revision = %s AND watermark = %s '
                'RETURNING revision, watermark',
                (watermark, self.dataset, self.partition_dt, expected.revision, expected.watermark),
            )
            row = cursor.fetchone()
            if row is None:
                raise PublicationConflict('Revision changed during publication')
            cursor.execute(
                'UPDATE dq_control.partition_publications SET status = %s, updated_at = now() '
                'WHERE publication_id = %s AND execution_id = %s AND status = %s RETURNING publication_id',
                (status, prepared.publication_id, self.execution_id, inflight),
            )
            if cursor.fetchone() is None:
                raise PublicationConflict('Journal changed during publication')
            self.connection.commit()
            result = Preconditions(int(row[0]), str(row[1]))
            self.owned_revisions.add(result)
            return result
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cursor.close()


class PostgresPublicationStore:
    """Own control connections only; callers inject a DB-API factory in tests."""
    def __init__(self, connection_factory: Callable[[], Any]):
        self.connection_factory = connection_factory

    @classmethod
    def from_env(cls) -> 'PostgresPublicationStore':
        url = os.getenv('DQ_CONTROL_DATABASE_URL', '').strip()
        if not url:
            raise PublicationConflict('DQ_CONTROL_DATABASE_URL is required for all partition writers')
        try:
            import psycopg
            connect = psycopg.connect
        except ImportError:
            import psycopg2
            connect = psycopg2.connect
        return cls(lambda: connect(url))

    @contextmanager
    def lock(self, dataset: str, partition_dt: date, execution_id: str) -> Iterator[PublicationLock]:
        key = advisory_lock_key(dataset, partition_dt)
        if not execution_id or len(execution_id) > 1000:
            raise ValueError('A bounded execution_id is required')
        connection = self.connection_factory()
        locked = PublicationLock(connection, dataset, partition_dt, execution_id)
        try:
            # Fail fast rather than queueing a stale approval behind another writer.
            row = locked.sql('SELECT pg_try_advisory_lock(%s)', (key,), fetch=True)
            if not row or not row[0]:
                raise PublicationConflict('Another writer holds the dataset/date publication lock')
            locked.sql(
                'INSERT INTO dq_control.partition_revisions (dataset, partition_dt) VALUES (%s, %s) '
                'ON CONFLICT (dataset, partition_dt) DO NOTHING', (dataset, partition_dt),
            )
            pending = locked.sql(
                'SELECT publication_id, status FROM dq_control.partition_publications '
                'WHERE dataset = %s AND partition_dt = %s '
                "AND status IN ('prepared', 'publishing', 'unknown', 'compensating') LIMIT 1",
                (dataset, partition_dt), fetch=True,
            )
            if pending:
                raise PublicationUnknown(f'Unresolved publication {pending[0]} ({pending[1]}); reconcile before retry')
            locked.owned_revisions.add(locked.state())
            try:
                yield locked
            except PublicationUnknown:
                raise
            except Exception:
                # A definite pre-publication failure may release prepared work,
                # never an in-flight or uncertain warehouse operation.
                locked.sql("UPDATE dq_control.partition_publications SET status='aborted',updated_at=now() "
                           "WHERE execution_id=%s AND dataset=%s AND partition_dt=%s AND status='prepared'",
                           (execution_id, dataset, partition_dt))
                raise
        finally:
            locked.active = False
            # Closing the dedicated connection releases even a lock acquired before an error.
            connection.close()


@contextmanager
def publication_lock(dataset: str, partition_dt: date, execution_id: str,
                     *, store: PostgresPublicationStore | None = None) -> Iterator[PublicationLock]:
    """Hold the common writer lock for an entire build/publish/compensate lifecycle."""
    with (store or PostgresPublicationStore.from_env()).lock(dataset, partition_dt, execution_id) as locked:
        yield locked


# --- Inspecting And Building Trusted Stages
def table_structure(client: Any, table: str) -> Structure:
    """Inspect actual engine, keys, storage, complete DDL, defaults and codecs."""
    database, name = split_table_name(table)
    where = f'database = {quote_sql_literal(database)} AND table = {quote_sql_literal(name)}'
    rows = client.query(
        'SELECT engine, engine_full, partition_key, sorting_key, primary_key, sampling_key, '
        'storage_policy, create_table_query FROM system.tables WHERE '
        f'database = {quote_sql_literal(database)} AND name = {quote_sql_literal(name)}'
    ).result_rows
    if len(rows) != 1:
        raise PublicationConflict(f'Missing or ambiguous table: {table}')
    row = rows[0]
    if row[0] not in {'MergeTree', 'ReplacingMergeTree'} or str(row[2]).replace('`', '') != 'dt':
        raise PublicationConflict('Only local MergeTree/ ReplaceMergeTree tables partitioned by dt are supported')
    columns = tuple(tuple(item) for item in client.query(
        'SELECT name, type, default_kind, default_expression, compression_codec '
        f'FROM system.columns WHERE {where} ORDER BY position'
    ).result_rows)
    if not columns or ('dt', 'Date') != columns[0][:2]:
        raise PublicationConflict('Expected an explicit Date dt partition column')
    ddl = str(row[7])
    if '(' not in ddl:
        raise PublicationConflict('Complete live CREATE TABLE DDL is unavailable')
    return Structure(tuple(row[:7]), columns, ddl[ddl.index('('):].strip())


def partition_fingerprint(client: Any, table: str, partition_dt: date) -> str:
    """Order-independent full-row fingerprint, with count and two hash accumulators."""
    _qualified(table)
    rows = client.query(
        'SELECT count(), groupBitXor(cityHash64(tuple(*))), '
        f'sumWithOverflow(cityHash64(tuple(*))) FROM {table} '
        f'WHERE dt = {format_date_literal(partition_dt)} SETTINGS use_query_cache = 0'
    ).result_rows
    if len(rows) != 1:
        raise PublicationConflict('Could not inspect partition fingerprint')
    return json.dumps([int(value or 0) for value in rows[0]], separators=(',', ':'))


def validate_partition_scope(client: Any, table: str, partition_dt: date) -> None:
    """Reject an input/stage containing any date other than the approved partition."""
    _qualified(table)
    row = client.query(
        f'SELECT countIf(dt != {format_date_literal(partition_dt)} OR isNull(dt)) FROM {table}'
    ).result_rows
    if not row or int(row[0][0]) != 0:
        raise PublicationConflict('Stage contains rows outside the approved date')


def clone_live_table(client: Any, target_table: str, destination: str) -> Structure:
    """Clone the actual live definition, never recreate engines from repository DDL."""
    _live(target_table)
    database, name = split_table_name(destination)
    if not re.fullmatch(r'dq_pub_[a-f0-9]{24}', database):
        raise ValueError('Stage/backup must be in a generated scratch database')
    if name not in {'raw_orders', 'stg_orders', 'fct_orders_daily',
                    'raw_orders_backup', 'stg_orders_backup', 'fct_orders_daily_backup',
                    'raw_orders_input'}:
        raise ValueError('Unsupported scratch relation')
    expected = table_structure(client, target_table)
    client.command(f'CREATE DATABASE IF NOT EXISTS {database}')
    client.command(f'CREATE TABLE {destination} AS {target_table}')
    if table_structure(client, destination) != expected:
        raise PublicationConflict('Cloned table differs from actual live DDL/engine/keys/storage')
    return expected


def copy_partition(client: Any, source: str, destination: str, partition_dt: date,
                   *, columns: list[str] | None = None) -> None:
    """Copy from a trusted relation using generated SQL, not a caller SQL fragment."""
    _qualified(source)
    database, _ = split_table_name(destination)
    if not re.fullmatch(r'dq_pub_[a-f0-9]{24}', database):
        raise ValueError('Copy destination must be isolated scratch')
    names = columns or [str(row[0]) for row in table_structure(client, destination).columns
                        if row[2] not in {'MATERIALIZED', 'ALIAS'}]
    if not names or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in names):
        raise ValueError('Unsafe copy columns')
    projection = ', '.join(names)
    client.command(f'INSERT INTO {destination} ({projection}) SELECT {projection} FROM {source} '
                   f'WHERE dt = {format_date_literal(partition_dt)}')


def capture_preconditions(locked: PublicationLock) -> Preconditions:
    """Capture the dataset revision and opaque publication watermark under lock."""
    return locked.state()


def capture_recovery_preconditions(partition_dt: date, store: PostgresPublicationStore | None = None) -> dict[str, Any]:
    """Briefly acquire the same partition lock to pin a new recovery plan's state.

    This is a short initial lease, not a lock held while awaiting human approval.
    Existing unresolved publication states block new plans as well as writers.
    """
    with publication_lock('orders', partition_dt, f'plan:{uuid4().hex}', store=store) as locked:
        current = locked.state()
        return {'expected_revision': current.revision, 'expected_watermark': current.watermark}


def prepare_partition(client: Any, locked: PublicationLock, target_table: str,
                      stage_table: str, *, intent: str = 'daily',
                      expected: Preconditions | None = None) -> PreparedPartition:
    """Validate a filled clone, back up live data, then persist its prepared journal.

    A recovery caller must validate all business/model/snapshot prerequisites after
    preparing the full group and before calling publish_group. This function checks
    structural/date prerequisites, not business DQ rules.
    """
    _live(target_table)
    if intent not in INTENTS:
        raise ValueError('Unsupported publication intent')
    expected = expected or locked.state()
    locked.require(expected)
    database = scratch_database(locked.execution_id, locked.partition_dt)
    name = target_table.split('.')[1]
    if stage_table != f'{database}.{name}':
        raise PublicationConflict('Stage is not bound to this execution/target/date')
    structure = table_structure(client, target_table)
    if table_structure(client, stage_table) != structure:
        raise PublicationConflict('Stage structure does not match live engine/DDL/keys/storage')
    validate_partition_scope(client, stage_table, locked.partition_dt)
    staged = partition_fingerprint(client, stage_table, locked.partition_dt)
    rows = json.loads(staged)[0]
    if rows == 0 and intent != 'simulation_empty':
        raise PublicationConflict('Empty recovery/daily partition rejected; explicit simulation intent required')
    before = partition_fingerprint(client, target_table, locked.partition_dt)
    backup = f'{database}.{name}_backup'
    clone_live_table(client, target_table, backup)
    if json.loads(before)[0]:
        client.command(f'ALTER TABLE {backup} REPLACE PARTITION '
                       f'{quote_sql_literal(locked.partition_dt.isoformat())} FROM {target_table}')
    if partition_fingerprint(client, backup, locked.partition_dt) != before:
        raise PublicationConflict('Backup does not match live partition')
    if partition_fingerprint(client, target_table, locked.partition_dt) != before:
        raise PublicationConflict('Live partition changed while preparing its backup')
    prepared = PreparedPartition(str(uuid4()), locked.execution_id, target_table, stage_table,
                                 backup, locked.partition_dt, expected, before, staged, structure, rows, intent)
    metadata = json.dumps({'structure_hash': structure.digest, 'rows': rows})
    locked.sql(
        'INSERT INTO dq_control.partition_publications '
        '(publication_id, execution_id, dataset, partition_dt, target_table, stage_table, backup_table, '
        'expected_revision, expected_watermark, before_fingerprint, staged_fingerprint, status, intent, metadata) '
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'prepared', %s, %s::jsonb)",
        (prepared.publication_id, locked.execution_id, locked.dataset, locked.partition_dt,
         target_table, stage_table, backup, expected.revision, expected.watermark, before, staged, intent, metadata),
    )
    logger.info('Partition prepared | publication_id=%s table=%s dt=%s rows=%s backup=%s',
                prepared.publication_id, target_table, locked.partition_dt, rows, backup)
    return prepared


stage_partition = prepare_partition


def _check_prepared(client: Any, locked: PublicationLock, prepared: PreparedPartition) -> None:
    if (prepared.execution_id != locked.execution_id or prepared.partition_dt != locked.partition_dt
            or prepared.preconditions not in locked.owned_revisions):
        raise PublicationConflict('Prepared partition is not owned by this locked execution')
    _live(prepared.target_table)
    for table in (prepared.target_table, prepared.stage_table, prepared.backup_table):
        if table_structure(client, table) != prepared.structure:
            raise PublicationConflict('Publication table DDL changed since preparation')
    validate_partition_scope(client, prepared.stage_table, prepared.partition_dt)
    if partition_fingerprint(client, prepared.stage_table, prepared.partition_dt) != prepared.staged_fingerprint:
        raise PublicationConflict('Stage changed since validation')
    if partition_fingerprint(client, prepared.backup_table, prepared.partition_dt) != prepared.before_fingerprint:
        raise PublicationConflict('Backup changed since preparation')


def _mark_unknown(locked: PublicationLock, prepared: PreparedPartition, inflight: str) -> None:
    try:
        locked.transition(prepared.publication_id, inflight, 'unknown')
    except BaseException:
        # The earlier committed in-flight state still blocks retries if PostgreSQL is unavailable.
        logger.exception('Could not record unknown; committed in-flight journal must be reconciled | id=%s',
                         prepared.publication_id)


def publish_partition(client: Any, locked: PublicationLock, prepared: PreparedPartition) -> dict[str, Any]:
    """Replace one validated partition; no DROP-before-insert and no automatic retry."""
    _check_prepared(client, locked, prepared)
    current = locked.state()
    if current not in locked.owned_revisions:
        raise PublicationConflict('An unowned revision appeared during the locked execution')
    if locked.journal_status(prepared.publication_id) != 'prepared':
        raise PublicationUnknown('Publication is no longer prepared; do not retry it')
    if partition_fingerprint(client, prepared.target_table, prepared.partition_dt) != prepared.before_fingerprint:
        raise PublicationConflict('Live partition changed since preparation')
    locked.require(current)
    locked.transition(prepared.publication_id, 'prepared', 'publishing')
    try:
        if prepared.rows:
            command = (f'ALTER TABLE {prepared.target_table} REPLACE PARTITION '
                       f'{quote_sql_literal(prepared.partition_dt.isoformat())} FROM {prepared.stage_table}')
        elif prepared.intent == 'simulation_empty':
            # Empty REPLACE semantics differ by server version. Explicit deletion is
            # allowed only after a verified backup and a durable simulation journal.
            command = f'ALTER TABLE {prepared.target_table} DROP PARTITION {quote_sql_literal(prepared.partition_dt.isoformat())}'
        else:
            raise PublicationConflict('Empty publication has no approved simulation intent')
        client.command(command)
        if partition_fingerprint(client, prepared.target_table, prepared.partition_dt) != prepared.staged_fingerprint:
            raise PublicationUnknown('Published partition did not match the prepared fingerprint')
        result = locked.finish(prepared, current)
    except BaseException as exc:
        _mark_unknown(locked, prepared, 'publishing')
        raise PublicationUnknown(f'Publication {prepared.publication_id} requires reconciliation; no blind retry') from exc
    logger.info('Partition published | id=%s table=%s revision=%s',
                prepared.publication_id, prepared.target_table, result.revision)
    return {'publication_id': prepared.publication_id, 'table': prepared.target_table,
            'status': 'published', 'revision': result.revision, 'watermark': result.watermark,
            'rows': prepared.rows, 'backup_table': prepared.backup_table}


def compensate_partition(client: Any, locked: PublicationLock, prepared: PreparedPartition,
                         *, preapproved: bool = False) -> dict[str, Any]:
    """Restore only a known-published partition from its verified retained backup."""
    if preapproved is not True:
        raise PublicationConflict('Compensation requires explicit prior approval')
    _check_prepared(client, locked, prepared)
    current = locked.state()
    if current not in locked.owned_revisions or locked.journal_status(prepared.publication_id) != 'published':
        raise PublicationUnknown('Compensation requires an owned, known-published state')
    if partition_fingerprint(client, prepared.target_table, prepared.partition_dt) != prepared.staged_fingerprint:
        raise PublicationConflict('Live partition changed; compensation refused')
    locked.transition(prepared.publication_id, 'published', 'compensating')
    try:
        if json.loads(prepared.before_fingerprint)[0]:
            command = (f'ALTER TABLE {prepared.target_table} REPLACE PARTITION '
                       f'{quote_sql_literal(prepared.partition_dt.isoformat())} FROM {prepared.backup_table}')
        else:
            command = f'ALTER TABLE {prepared.target_table} DROP PARTITION {quote_sql_literal(prepared.partition_dt.isoformat())}'
        client.command(command)
        if partition_fingerprint(client, prepared.target_table, prepared.partition_dt) != prepared.before_fingerprint:
            raise PublicationUnknown('Compensation verification failed')
        result = locked.finish(prepared, current, compensation=True)
    except BaseException as exc:
        _mark_unknown(locked, prepared, 'compensating')
        raise PublicationUnknown(f'Compensation {prepared.publication_id} requires reconciliation') from exc
    return {'publication_id': prepared.publication_id, 'status': 'compensated', 'revision': result.revision}


def publish_group(client: Any, locked: PublicationLock, prepared: list[PreparedPartition],
                  *, compensation_preapproved: bool = False) -> list[dict[str, Any]]:
    """Preflight all members, then publish sequentially, never as a transaction.

    Unknown outcomes are never compensated automatically. A known completed prefix
    may be compensated only after a definite pre-mutation failure in a later member.
    """
    if not prepared or len({item.target_table for item in prepared}) != len(prepared):
        raise ValueError('Group must contain unique prepared targets')
    for item in prepared:
        _check_prepared(client, locked, item)
        if locked.journal_status(item.publication_id) != 'prepared':
            raise PublicationUnknown('Group contains an unresolved/already-published member')
        if partition_fingerprint(client, item.target_table, item.partition_dt) != item.before_fingerprint:
            raise PublicationConflict('Group live precondition changed')
    completed: list[PreparedPartition] = []
    results: list[dict[str, Any]] = []
    try:
        for item in prepared:
            results.append(publish_partition(client, locked, item))
            completed.append(item)
        return results
    except PublicationUnknown:
        raise
    except Exception:
        if compensation_preapproved is True:
            for item in reversed(completed):
                compensate_partition(client, locked, item, preapproved=True)
        raise
