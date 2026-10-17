"""Host-only, manifest-confirmed reset of this local demo. Default is read-only.

No API, model, MCP or Discord command exposes this destructive maintenance tool.
The journal contains identities/counts only, never data backups or credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'agentic-dq-triage'
VOLUME_SUFFIXES = {'ch_data','ch_logs','ch_ui_data','sw_master','sw_volume','sw_filer','airflow_pgdata','agent_checkpoints'}
SERVICES = {'clickhouse','clickhouse-init','ch-ui','seaweed-master','seaweed-volume','seaweed-filer','seaweed-s3',
    's3-init','airflow-postgres','airflow-redis','airflow-init','airflow-api-server','airflow-scheduler',
    'airflow-dag-processor','airflow-triggerer','airflow-worker','flower','dq-runner','streamlit',
    'api','web','approval-control-init','discord-bot'}
FIXED_OUTPUTS = ('data','logs','output','warehouse/dbt/target','warehouse/dbt/logs','.pytest_cache',
    'apps/web/.next','tools/demo-video/.cache','tools/demo-video/out')
PRUNE = {'.git','.vscode','.venv','venv','node_modules','dbt_packages'}
JOURNAL_DIR = Path(tempfile.gettempdir()) / 'datasentry-maintenance'


class ResetError(RuntimeError):
    """A fixed safe message; subprocess/provider bodies must not be displayed."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def container_name(service):
    return {'dq-runner':'dq_runner','flower':'dq_airflow_flower'}.get(service,'dq_'+str(service).replace('-','_'))


def command(args, *, body=None, timeout=180):
    try:
        result = subprocess.run(args, cwd=ROOT, input=body, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ResetError('Local maintenance command timed out or was unavailable; inspect its phase before retrying.') from None
    if result.returncode:
        raise ResetError('Local maintenance dependency rejected the operation; no raw error body is displayed.')
    return result.stdout.strip()


def docker(*args, body=None, timeout=180):
    return command(['docker', *args], body=body, timeout=timeout)


def runner(operation, body=None):
    if operation not in {'probe','pause','validation'}:
        raise ResetError('Internal maintenance operation is not allowlisted.')
    return json.loads(docker('exec','-i','dq_runner','python','-m','scripts.demo_reset_runtime', operation,
                             body=json.dumps(body or {}), timeout=120))


def discord_discover():
    return json.loads(docker('exec','dq_runner','python','-m','scripts.demo_reset_discord','--discover', timeout=180))


def compose(*args):
    return docker('compose','--env-file',str(ROOT/'infra/.env'),'-f',str(ROOT/'infra/docker-compose.yml'),
        '--profile','api','--profile','web','--profile','discord',*args, timeout=300)


def safe_path(root, relative):
    """Check resolved absolute targets and every existing ancestor for reparse links."""
    root = root.resolve()
    part = Path(relative)
    if part.is_absolute() or '..' in part.parts or not part.parts:
        raise ResetError('Deletion path must be a workspace-relative allowlisted output.')
    candidate = root / part
    if not candidate.resolve().is_relative_to(root) or candidate.resolve() == root:
        raise ResetError('Deletion target escapes the workspace.')
    current = root
    for name in part.parts:
        current /= name
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ResetError('Symlink/reparse deletion requires separate operator review.')
    return candidate


def output_allowed(relative):
    p = Path(relative)
    text = p.as_posix()
    if text in FIXED_OUTPUTS:
        return True
    if any(name in PRUNE for name in p.parts):
        return False
    if p.parts[:3] == ('infra','airflow','auth'):
        return False
    return (p.name == '__pycache__' or p.suffix in {'.pyc','.pyo'}
            or p.suffix == '.tsbuildinfo' and p.parent.as_posix() in {'apps/web','tools/demo-video'})


def validate_output_tree(path):
    links = []
    if not path.exists():
        return links
    if path.is_file():
        return links
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs[:] + files:
            child = Path(base)/name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                if not child.resolve().is_relative_to(path.resolve()):
                    raise ResetError('An output link points outside its approved output tree.')
                links.append(child.relative_to(path).as_posix())
                if name in dirs:
                    dirs.remove(name)
    return sorted(links)


def output_stats(path, links):
    count, size = 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    if path.exists():
        for base, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if (Path(base)/d).relative_to(path).as_posix() not in links]
            for name in files:
                item=Path(base)/name
                if item.relative_to(path).as_posix() not in links:
                    count+=1; size+=item.stat().st_size
    return count,size


def output_inventory(root=ROOT):
    candidates = set(FIXED_OUTPUTS)
    for base, dirs, files in os.walk(root, followlinks=False):
        relative = Path(base).relative_to(root)
        dirs[:] = [d for d in dirs if d not in PRUNE and (relative/d).as_posix() not in FIXED_OUTPUTS]
        for name in list(dirs):
            if name == '__pycache__':
                candidates.add((relative/name).as_posix()); dirs.remove(name)
        for name in files:
            p = relative/name
            if output_allowed(p.as_posix()):
                candidates.add(p.as_posix())
    tracked = command(['git','ls-files','-z']).split('\0')
    result = []
    for relative in sorted(candidates):
        if not output_allowed(relative):
            raise ResetError('Output inventory contains a non-allowlisted target.')
        path = safe_path(root, relative)
        if any(t == relative or t.startswith(relative+'/') for t in tracked if t):
            raise ResetError('Generated-output target contains tracked source; refusing removal.')
        links=validate_output_tree(path)
        count,size=output_stats(path,links)
        result.append({'relative': relative, 'files': count, 'bytes': size, 'internal_links':links})
    return result


def docker_inventory():
    ids = docker('ps','-aq','--no-trunc','--filter','label=com.docker.compose.project='+PROJECT).split()
    if not ids:
        raise ResetError('A running local stack is required to prepare the initial manifest.')
    containers, volumes = [], set()
    for item in json.loads(docker('inspect',*ids)):
        labels = item['Config'].get('Labels') or {}
        service = labels.get('com.docker.compose.service')
        expected = container_name(service)
        if labels.get('com.docker.compose.project') != PROJECT or service not in SERVICES or item['Name'] != '/'+expected:
            raise ResetError('Unexpected project container requires review.')
        containers.append({'id': item['Id'], 'name': expected, 'service': service,
                           'was_running': item['State']['Running']})
        for mount in item.get('Mounts', []):
            if mount['Type'] == 'bind':
                source = mount['Source']
                if source == '/var/run/docker.sock' and mount['Destination'] == '/var/run/docker.sock':
                    continue
                if os.name == 'nt' and source.startswith('/run/desktop/mnt/host/'):
                    drive, rest = source.removeprefix('/run/desktop/mnt/host/').split('/',1)
                    source = drive.upper()+':/'+rest
                resolved = Path(source).resolve()
                if not resolved.is_relative_to(ROOT.resolve()) or mount['Destination'] in {'/app','/opt/airflow/project'} and resolved != ROOT.resolve():
                    raise ResetError('Container bind mount belongs to another workspace.')
            if mount['Type'] == 'volume':
                volumes.add(mount['Name'])
    named = {PROJECT+'_'+suffix for suffix in VOLUME_SUFFIXES}
    if not named.issubset(volumes):
        raise ResetError('Expected project volumes are not all attached.')
    for name in volumes:
        if name not in named and not re.fullmatch('[a-f0-9]{64}', name):
            raise ResetError('Unknown named volume is outside reset scope.')
        info = json.loads(docker('volume','inspect',name))[0]
        if name in named and (info.get('Labels') or {}).get('com.docker.compose.project') != PROJECT:
            raise ResetError('Named volume project ownership does not match.')
        users = set(docker('ps','-aq','--filter','volume='+name,'--no-trunc').split())
        if not users.issubset(set(ids)):
            raise ResetError('A volume is shared outside the approved project.')
    return {'containers': sorted(containers,key=lambda c:c['service']), 'volumes': sorted(volumes)}


def create_plan():
    if Path(command(['git','rev-parse','--show-toplevel'])).resolve() != ROOT.resolve():
        raise ResetError('Repository root did not match the reset workspace.')
    plan = {'version': 1, 'id': str(uuid4()), 'root': str(ROOT.resolve()), 'project': PROJECT,
            'outputs': output_inventory(), 'docker': docker_inventory(), 'discord': discord_discover(), 'runtime': runner('probe')}
    plan['hash'] = digest(plan)
    return plan


def validate_plan(plan, confirmation):
    if plan.get('version') != 1 or plan.get('project') != PROJECT or Path(plan.get('root','')).resolve() != ROOT.resolve():
        raise ResetError('Manifest workspace or version is invalid.')
    UUID(plan['id'])
    if confirmation != plan.get('hash') or digest({k:v for k,v in plan.items() if k!='hash'}) != confirmation:
        raise ResetError('Explicit matching manifest hash is required; scope changes need a new preview.')
    if not set(FIXED_OUTPUTS).issubset({p['relative'] for p in plan['outputs']}):
        raise ResetError('Manifest omits a required generated-output root.')
    for output in plan['outputs']:
        if not output_allowed(output['relative']):
            raise ResetError('Manifest contains a non-output path.')
        safe_path(ROOT, output['relative'])
    expected = {PROJECT+'_'+suffix for suffix in VOLUME_SUFFIXES}
    actual = set(plan['docker']['volumes'])
    if not expected.issubset(actual) or any(v not in expected and not re.fullmatch('[a-f0-9]{64}',v) for v in actual):
        raise ResetError('Manifest volume scope is invalid.')
    for c in plan['docker']['containers']:
        service=c['service']; name=container_name(service)
        if service not in SERVICES or c['name']!=name or not re.fullmatch('[a-f0-9]{64}',c['id']):
            raise ResetError('Manifest container scope is invalid.')


def require_quiescent(probe, *, allow_reconciler=False):
    if any(probe[name] for name in ('recovery_active','publication_uncertain','approval_inflight','copilot_claimed','investigation_uncertain')):
        raise ResetError('Active or uncertain application outcome blocks reset; reconcile it first.')
    active = probe['airflow_active']
    if allow_reconciler:
        active = [r for r in active if r['dag_id'] != '90_06_dag_dq_recovery_reconciliation']
    if active:
        raise ResetError('Active Airflow work blocks reset; wait for a known terminal state.')


def save_journal(path, journal):
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(journal,indent=2),encoding='utf-8')
    os.replace(temp,path)


def wait_for(callback, timeout=180):
    deadline = time.monotonic()+timeout
    while True:
        try:
            result = callback()
            if result:
                return result
        except ResetError:
            pass
        if time.monotonic() >= deadline:
            raise ResetError('Maintenance readiness deadline reached; inspect the existing phase.')
        time.sleep(2)


def wait_init(service):
    def ready():
        name='dq_'+service.replace('-','_')
        state=json.loads(docker('inspect',name))[0]['State']
        if state['Status']=='exited' and state['ExitCode'] != 0:
            raise RuntimeError('Bootstrap init failed; inspect sanitized service diagnostics.')
        return state['Status']=='exited' and state['ExitCode']==0
    return wait_for(ready)


@contextlib.contextmanager
def reset_lock():
    JOURNAL_DIR.mkdir(parents=True,exist_ok=True)
    path=JOURNAL_DIR/('lock-'+hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16])
    with path.open('a+b') as handle:
        if handle.tell()==0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise ResetError('Another reset controller holds the maintenance lock.') from None
        yield


def apply_plan(plan, journal_path, confirmation):
    validate_plan(plan,confirmation)
    journal=json.loads(journal_path.read_text()) if journal_path.exists() else {'hash': confirmation, 'phases': []}
    if journal['hash'] != confirmation:
        raise ResetError('Resume journal belongs to another manifest.')
    def phase(name, work):
        if name in journal['phases']:
            return
        journal['current_phase']=name; save_journal(journal_path,journal)
        work()
        journal['phases'].append(name); save_journal(journal_path,journal)
        print(json.dumps({'phase_completed': name}),flush=True)

    def quiesce():
        now=docker_inventory()
        def stable(value):
            return {'volumes':value['volumes'],'containers':[{k:c[k] for k in ('id','name','service')} for c in value['containers']]}
        if stable(now)!=stable(plan['docker']):
            raise ResetError('Docker target identities changed; generate a new manifest.')
        require_quiescent(runner('probe'),allow_reconciler=True)
        runner('pause')
        # Uvicorn drains in-flight API/model work; never SIGKILL an unknown provider outcome.
        # Gateway cannot initiate a new paid request after the shared API listener closes.
        docker('stop','--time','-1','dq_api',timeout=900)
        def idle():
            p=runner('probe')
            require_quiescent(p,allow_reconciler=True)
            return not p['airflow_active']
        wait_for(idle,180)
        keep={'dq-runner','clickhouse','airflow-postgres','airflow-redis','seaweed-master','seaweed-volume','seaweed-filer','seaweed-s3'}
        names=[c['name'] for c in now['containers'] if c['was_running'] and c['service'] not in keep]
        if names:
            docker('stop','--time','30',*names)
        require_quiescent(runner('probe'))
        if discord_discover()!=plan['discord']:
            raise ResetError('Discord inventory changed; no deletion started. Generate a new manifest.')
    phase('quiesced',quiesce)
    phase('discord_deleted',lambda: docker('exec','-i','dq_runner','python','-m','scripts.demo_reset_discord','--apply',
                                         body=json.dumps(plan['discord']),timeout=300))
    def remove_containers():
        for c in plan['docker']['containers']:
            found=docker('ps','-aq','--no-trunc','--filter','id='+c['id']).split()
            if found:
                item=json.loads(docker('inspect',c['id']))[0]
                if ((item['Config'].get('Labels') or {}).get('com.docker.compose.project')!=PROJECT
                        or item['Name']!='/'+c['name']):
                    raise ResetError('Container ownership changed before removal.')
                docker('stop','--time','30',c['id']); docker('rm',c['id'])
    phase('containers_removed',remove_containers)
    def remove_volumes():
        for name in plan['docker']['volumes']:
            existing=docker('volume','ls','-q','--filter','name=^'+re.escape(name)+'$').split()
            if name not in existing:
                continue
            info=json.loads(docker('volume','inspect',name))[0]
            owner=(info.get('Labels') or {}).get('com.docker.compose.project')
            if owner not in {None,PROJECT} or name.startswith(PROJECT+'_') and owner!=PROJECT:
                raise ResetError('Volume ownership changed before removal.')
            if docker('ps','-aq','--filter','volume='+name):
                raise ResetError('A volume gained a new user; refusing removal.')
            docker('volume','rm',name)
    phase('volumes_removed',remove_volumes)
    def remove_outputs():
        tracked=command(['git','ls-files','-z']).split('\0')
        for output in plan['outputs']:
            relative=output['relative']; path=safe_path(ROOT,relative)
            if any(t==relative or t.startswith(relative+'/') for t in tracked if t):
                raise ResetError('A deletion target now contains tracked source.')
            links=validate_output_tree(path)
            if set(links)-set(output.get('internal_links',[])):
                raise ResetError('Output link identities changed; review before deleting.')
            for name in links:
                entry=path/name
                # The target stays untouched: remove the checked internal link entry first.
                if stat.S_ISDIR(entry.lstat().st_mode):
                    os.rmdir(entry)
                else:
                    entry.unlink()
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    phase('outputs_removed',remove_outputs)
    def bootstrap():
        compose('up','-d','airflow-postgres','airflow-redis','clickhouse','seaweed-master','seaweed-volume','seaweed-filer','seaweed-s3')
        compose('up','-d','clickhouse-init','s3-init','approval-control-init','airflow-init')
        for service in ('clickhouse-init','s3-init','approval-control-init','airflow-init'):
            wait_init(service)
        compose('up','-d','--no-deps','dq-runner','api','streamlit','ch-ui','airflow-api-server','airflow-dag-processor')
        wait_for(lambda: runner('pause'))
        old={c['service'] for c in plan['docker']['containers'] if c['was_running']}
        resume=['airflow-scheduler','airflow-worker','airflow-triggerer']
        resume += sorted(old.intersection({'web','flower'}))
        compose('up','-d','--no-deps',*resume)
    phase('bootstrapped',bootstrap)
    rid='manual__fresh_demo_validation_'+UUID(plan['id']).hex[:12]
    def validate():
        allow=not journal.get('validation_attempted',False)
        journal['validation_attempted']=True; save_journal(journal_path,journal)
        result=runner('validation',{'run_id':rid,'allow_create':allow})
        def terminal():
            current=runner('validation',{'run_id':rid,'allow_create':False})
            if current['state']=='failed':
                raise RuntimeError('Final validation failed; do not reset or blindly trigger another run.')
            return current if current['state']=='success' else None
        result=wait_for(terminal,1500)
        if not result['tasks'] or any(t['state']!='success' for t in result['tasks']):
            raise ResetError('Final validation task states are incomplete.')
        journal['validation']=result
    phase('validated',validate)
    def start_onboarding():
        # Keep public bot interactions offline until empty-platform acceptance has finished.
        compose('up','-d','--no-deps','discord-bot')
        wait_for(lambda: runner('probe')['control_rows'].get('discord_service_messages')==1)
    phase('onboarding_started',start_onboarding)
    def verify():
        p=runner('probe',{'audit_since':journal['validation']['logical_date']}); require_quiescent(p)
        business_rows={t:n for t,n in p['warehouse_rows'].items() if t!='agent_audit_log'}
        if (p['airflow_run_count']!=1 or any(business_rows.values())
                or p['audit_check']['status']!='pass' or any(b['objects'] for b in p['buckets'].values())):
            raise ResetError('Fresh data, buckets or DagRun count did not match the approved result.')
        if p['warehouse_databases']!=['dq']:
            raise ResetError('Scratch or acceptance warehouse databases remain.')
        if p['checkpoint_rows'] or len(p['scheduled_paused'])!=2 or not all(p['scheduled_paused'].values()):
            raise ResetError('Checkpoint history or scheduled work is not reset and paused.')
        if any(n for t,n in p['control_rows'].items() if t!='discord_service_messages') or p['control_rows'].get('discord_service_messages')!=1:
            raise ResetError('Control records are not empty apart from the welcome registry.')
        current=discord_discover()
        new_ids={m for c in current['channels'] for m in c['message_ids']}
        if len(new_ids)!=1 or len(p['welcome'])!=1 or p['welcome'][0]['message_id'] not in new_ids:
            raise ResetError('Discord does not contain exactly one new bot welcome.')
        humans={m for c in current['channels'] for m in c['human_message_ids']}
        if not {m for c in plan['discord']['channels'] for m in c['human_message_ids']}.issubset(humans):
            raise ResetError('Human message preservation could not be confirmed.')
        journal['verification']={'business_empty':True,'discord_bot_messages':1,'no_backup':True,
                                 'inspection_audit':p['audit_check']['details']}
    phase('verified',verify)
    receipt=ROOT/'logs/maintenance/reset-summary.json'; receipt.parent.mkdir(parents=True,exist_ok=True)
    receipt.write_text(json.dumps({'manifest_hash':confirmation,'reset_id':plan['id'],'validation':journal['validation'],
        'verification':journal['verification'],'removed_volumes':plan['docker']['volumes'],
        'removed_output_roots':[p['relative'] for p in plan['outputs']]},indent=2),encoding='utf-8')
    journal_path.unlink(missing_ok=True)
    print(json.dumps({'status':'complete','run_id':rid,'receipt':str(receipt)}))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true',help='Read-only inventory; this is the default.')
    parser.add_argument('--write-plan',action='store_true',help='Save an approval manifest in the OS temporary directory.')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--confirm-plan',default='')
    args=parser.parse_args(argv)
    try:
        with reset_lock():
            if args.apply:
                if not args.plan or args.dry_run or args.write_plan:
                    raise ResetError('Apply needs an existing manifest and explicit matching hash.')
                if args.plan.is_symlink() or args.plan.resolve().parent!=JOURNAL_DIR.resolve():
                    raise ResetError('Only an OS-temporary maintenance manifest can be applied.')
                plan=json.loads(args.plan.read_text(encoding='utf-8'))
                if args.plan.name!='reset-'+str(UUID(plan['id']))+'.json':
                    raise ResetError('Manifest filename does not match its maintenance identity.')
                apply_plan(plan,args.plan.with_suffix('.journal.json'),args.confirm_plan)
                args.plan.unlink(missing_ok=True)
            else:
                plan=create_plan()
                if args.write_plan:
                    path=JOURNAL_DIR/('reset-'+plan['id']+'.json')
                    save_journal(path,plan)
                    print(json.dumps({'plan_path':str(path)}))
                print(json.dumps(plan,indent=2))
        return 0
    except ResetError as exc:
        print(json.dumps({'status':'blocked','message':str(exc)}))
        return 1
    except (ValueError,KeyError,RuntimeError):
        print(json.dumps({'status':'blocked','message':'Reset stopped safely. Inspect the approved manifest and completed phase; do not widen scope or retry unknown work.'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
