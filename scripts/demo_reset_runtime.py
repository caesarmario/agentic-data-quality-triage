"""Internal read-only probes and fixed Airflow controls for the host reset CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

SCHEDULED = ('00_dag_dq_platform_daily_orchestrator', '90_06_dag_dq_recovery_reconciliation')
VALIDATION = '91_dag_dq_platform_validation'


def database():
    import psycopg
    url = os.environ.get('DQ_CONTROL_DATABASE_URL', '')
    parsed = urlparse(url)
    if parsed.hostname != 'airflow-postgres' or parsed.path != '/airflow':
        raise ValueError('Control database is outside the local reset scope')
    return psycopg.connect(url, connect_timeout=5)


def warehouse():
    import clickhouse_connect
    if os.getenv('CLICKHOUSE_HOST', 'clickhouse') != 'clickhouse':
        raise ValueError('Warehouse is outside the local reset scope')
    return clickhouse_connect.get_client(host='clickhouse', port=8123,
        username=os.getenv('CLICKHOUSE_USER', 'default'), password=os.getenv('CLICKHOUSE_PASSWORD', ''),
        connect_timeout=5, send_receive_timeout=10)


def object_store():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url='http://seaweed-s3:8333',
        config=Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 0}))


def probe(body=None):
    """Only count application state; never return row bodies or credentials."""
    with database() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute("SELECT count(*) FROM dq_control.recovery_executions WHERE status IN ('queued','running','unknown')")
        recovery = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dq_control.partition_publications WHERE status IN ('prepared','publishing','unknown','compensating')")
        publications = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dq_control.approval_requests WHERE execution_status IN ('dispatching','dispatched','running','unknown')")
        approvals = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dq_control.discord_copilot_requests WHERE status='claimed'")
        copilot = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM dq_control.discord_investigation_claims WHERE status IN ('claimed','uncertain')")
        investigations = cur.fetchone()[0]
        cur.execute("SELECT dag_id,run_id,state FROM public.dag_run WHERE state IN ('running','queued')")
        active = [dict(dag_id=d, run_id=r, state=s) for d, r, s in cur.fetchall()]
        cur.execute("SELECT count(*) FROM public.dag_run")
        run_count = cur.fetchone()[0]
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='dq_control' AND table_type='BASE TABLE' ORDER BY table_name")
        counts = {}
        for (table,) in cur.fetchall():
            if not re.fullmatch('[a-z_]+', table):
                raise ValueError('Unexpected control table identifier')
            cur.execute(f'SELECT count(*) FROM dq_control.{table}')
            counts[table] = cur.fetchone()[0]
        cur.execute("SELECT dag_id,is_paused FROM public.dag WHERE dag_id IN (%s,%s)", SCHEDULED)
        paused = dict(cur.fetchall())
        cur.execute("SELECT guild_id,channel_id,message_id FROM dq_control.discord_service_messages WHERE kind='welcome'")
        welcome = [dict(guild_id=str(g),channel_id=str(c),message_id=m) for g,c,m in cur.fetchall()]
    ch = warehouse()
    tables = ch.query("SELECT name FROM system.tables WHERE database='dq' AND engine!='View' ORDER BY name").result_rows
    row_counts = {}
    for (table,) in tables:
        if not re.fullmatch('[a-z_][a-z0-9_]*', table):
            raise ValueError('Unexpected warehouse table identifier')
        row_counts[table] = ch.query(f'SELECT count() FROM dq.{table}').result_rows[0][0]
    audit_check = None
    if (body or {}).get('audit_since'):
        from scripts.smoke_readiness import check_empty_demo_audit
        from dataclasses import asdict
        audit_check = asdict(check_empty_demo_audit(ch, body['audit_since']))
    databases = [r[0] for r in ch.query("SELECT name FROM system.databases WHERE name NOT IN ('system','information_schema','INFORMATION_SCHEMA','default')").result_rows]
    foreign = [name for name in databases if name not in {'dq', 'dq_resolution_acceptance'} and not re.fullmatch(r'dq_pub_[a-f0-9]{24}', name)]
    if foreign:
        raise ValueError('Unrecognized warehouse database requires review')
    s3 = object_store()
    expected = {os.getenv(k, v) for k, v in (
        ('LANDING_BUCKET','dq-landing'), ('ARTIFACTS_BUCKET','dq-artifacts'), ('DQREPORTS_BUCKET','dq-dqreports'),
        ('DQFAILURES_BUCKET','dq-dqfailures'), ('AUDIT_BUCKET','dq-audit'))}
    actual = {b['Name'] for b in s3.list_buckets()['Buckets']}
    if actual != expected:
        raise ValueError('Object-store bucket scope differs from the five configured buckets')
    buckets = {}
    for bucket in sorted(expected):
        count, size, token = 0, 0, None
        for _ in range(100):
            page = s3.list_objects_v2(Bucket=bucket, MaxKeys=1000, **({'ContinuationToken': token} if token else {}))
            count += len(page.get('Contents', []))
            size += sum(obj['Size'] for obj in page.get('Contents', []))
            if not page.get('IsTruncated'):
                break
            token = page['NextContinuationToken']
        else:
            raise ValueError('Object inventory exceeded the safe bound')
        buckets[bucket] = {'objects': count, 'bytes': size}
    checkpoints=0
    checkpoint=Path(os.getenv('AGENT_CHECKPOINT_SQLITE_PATH','/var/lib/agent-checkpoints/triage.sqlite3'))
    if not checkpoint.is_relative_to('/var/lib/agent-checkpoints'):
        raise ValueError('Checkpoint is outside the reset volume')
    if checkpoint.exists():
        import sqlite3
        with sqlite3.connect('file:'+str(checkpoint)+'?mode=ro',uri=True) as db:
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone()[0]:
                checkpoints=db.execute('SELECT count(*) FROM checkpoints').fetchone()[0]
    return {'recovery_active': recovery, 'publication_uncertain': publications, 'approval_inflight': approvals,
        'copilot_claimed': copilot, 'investigation_uncertain': investigations,
        'airflow_active': active, 'airflow_run_count': run_count,
        'control_rows': counts, 'warehouse_rows': row_counts, 'warehouse_databases': sorted(databases),
        'buckets': buckets, 'checkpoint_rows':checkpoints, 'scheduled_paused':paused, 'welcome':welcome,
        'audit_check':audit_check}


class Airflow:
    def __init__(self):
        import requests
        self.session = requests.Session()
        self.base = os.environ['AIRFLOW_API_BASE_URL'].rstrip('/')
        url = urlparse(self.base)
        if url.hostname != 'airflow-api-server' or url.port != 8080 or url.scheme != 'http':
            raise ValueError('Airflow is outside the local reset scope')
        response = self.session.post(self.base+'/auth/token', json={
            'username': os.environ['AIRFLOW_API_USERNAME'], 'password': os.environ['AIRFLOW_API_PASSWORD']}, timeout=10, allow_redirects=False)
        response.raise_for_status()
        self.headers = {'Authorization': 'Bearer '+response.json()['access_token']}

    def request(self, method, path, body=None):
        response = self.session.request(method, self.base+'/api/v2/'+path, headers=self.headers,
            json=body, timeout=10, allow_redirects=False)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def paused(self):
        result = {}
        for dag in SCHEDULED:
            current = self.request('GET', 'dags/'+dag)
            if current is None:
                raise ValueError('Scheduled DAG catalog is not ready')
            self.request('PATCH', 'dags/'+dag, {'is_paused': True})
            result[dag] = True
        return result

    def validation(self, body):
        rid = body['run_id']
        if not re.fullmatch(r'manual__fresh_demo_validation_[a-f0-9]{12}', rid):
            raise ValueError('Invalid final validation identity')
        path = f'dags/{VALIDATION}/dagRuns/{rid}'
        run = self.request('GET', path)
        expected = {'validation_suite': 'all', 'require_api': True, 'expect_empty_demo': True}
        if run is None and body.get('allow_create') is True:
            with database() as conn, conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM public.dag_run')
                if cur.fetchone()[0] != 0:
                    raise ValueError('Fresh platform already has a different DagRun')
            self.request('PATCH', 'dags/'+VALIDATION, {'is_paused': False})
            try:
                run = self.request('POST', f'dags/{VALIDATION}/dagRuns', {'dag_run_id': rid,
                    'logical_date': datetime.now(timezone.utc).isoformat(), 'conf': expected})
            except Exception:
                run = self.request('GET', path)
                if run is None:
                    raise ValueError('Validation dispatch outcome is unknown; do not trigger again') from None
        if run is None or run.get('conf') != expected:
            raise ValueError('Existing validation could not be confirmed')
        tasks = self.request('GET', path+'/taskInstances?limit=100')
        return {'run_id': rid, 'state': run['state'], 'logical_date':run['logical_date'], 'tasks': [
            {k: item.get(k) for k in ('task_id', 'state', 'try_number')}
            for item in tasks.get('task_instances', [])]}


def main():
    try:
        body = json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            operation = sys.argv[1]
            if operation == 'probe':
                result = probe(body)
            elif operation == 'pause':
                result = Airflow().paused()
            elif operation == 'validation':
                result = Airflow().validation(body)
            else:
                raise ValueError('Unknown internal maintenance operation')
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print(json.dumps({'error': 'Maintenance runtime operation failed; inspect configured local dependencies.'}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
