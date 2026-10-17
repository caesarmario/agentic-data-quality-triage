"""Reset controller safety checks use temporary files and fake Docker only."""
import ast
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock
from uuid import uuid4

import pytest

from scripts import reset_local_demo as reset


def manifest(root):
    plan={'version':1,'id':str(uuid4()),'root':str(root.resolve()),'project':reset.PROJECT,
        'outputs':[{'relative':p,'files':0,'bytes':0} for p in reset.FIXED_OUTPUTS],
        'docker':{'containers':[{'id':'a'*64,'name':'dq_runner','service':'dq-runner','was_running':True}],
                  'volumes':sorted(reset.PROJECT+'_'+s for s in reset.VOLUME_SUFFIXES)},
        'discord':{'channels':[]},'runtime':{}}
    plan['hash']=reset.digest(plan)
    return plan


@pytest.mark.parametrize('path',['../outside','/','data/../../outside','.git/__pycache__','infra/airflow/auth/__pycache__','tests'])
def test_output_scope_rejects_source_secrets_and_traversal(tmp_path,path):
    if not reset.output_allowed(path):
        return
    with pytest.raises(reset.ResetError):
        reset.safe_path(tmp_path,path)


def test_manifest_confirmation_required_before_any_command(tmp_path,monkeypatch):
    monkeypatch.setattr(reset,'ROOT',tmp_path)
    run=Mock(side_effect=AssertionError('No Docker command expected'))
    monkeypatch.setattr(reset,'docker',run)
    plan=manifest(tmp_path)
    with pytest.raises(reset.ResetError,match='matching manifest hash'):
        reset.apply_plan(plan,tmp_path/'journal.json','wrong')
    run.assert_not_called()


@pytest.mark.parametrize('change',[lambda p:p['docker']['volumes'].append('another_project_data'),
    lambda p:p['outputs'].append({'relative':'tests','files':1,'bytes':1}),
    lambda p:p['docker']['containers'][0].update(service='another-service')])
def test_rehashed_manifest_still_cannot_widen_scope(tmp_path,monkeypatch,change):
    monkeypatch.setattr(reset,'ROOT',tmp_path)
    plan=manifest(tmp_path); change(plan)
    plan['hash']=reset.digest({k:v for k,v in plan.items() if k!='hash'})
    with pytest.raises(reset.ResetError):
        reset.validate_plan(plan,plan['hash'])


def test_source_is_not_an_output_and_tracked_generated_paths_are_blocked(tmp_path,monkeypatch):
    (tmp_path/'data').mkdir(); (tmp_path/'data'/'important.py').write_text('preserve=True')
    monkeypatch.setattr(reset,'command',lambda *_args,**_kwargs:'data/important.py\0')
    with pytest.raises(reset.ResetError,match='tracked source'):
        reset.output_inventory(tmp_path)
    assert (tmp_path/'data'/'important.py').exists()


def test_symlink_output_is_refused_not_followed(tmp_path):
    outside=tmp_path/'outside'; outside.mkdir(); (outside/'keep').write_text('safe')
    root=tmp_path/'root'; root.mkdir()
    try:
        (root/'data').symlink_to(outside,target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit symlink fixture creation')
    with pytest.raises(reset.ResetError):
        reset.safe_path(root,'data')
    assert (outside/'keep').exists()


def test_default_dry_run_never_applies_or_exposes_transport_errors(monkeypatch,capsys):
    monkeypatch.setattr(reset,'reset_lock',nullcontext)
    monkeypatch.setattr(reset,'create_plan',lambda:{'hash':'public-plan','outputs':[]})
    apply=Mock(); monkeypatch.setattr(reset,'apply_plan',apply)
    assert reset.main([])==0
    apply.assert_not_called()
    assert 'public-plan' in capsys.readouterr().out
    monkeypatch.setattr(reset.subprocess,'run',lambda *_a,**_kw:subprocess.CompletedProcess([],1,'authorization=do-not-display','credential=do-not-display'))
    with pytest.raises(reset.ResetError) as error:
        reset.command(['docker','inspect','known'])
    assert 'do-not-display' not in str(error.value)


def test_uncertain_work_blocks_reset():
    probe={k:0 for k in ('recovery_active','publication_uncertain','approval_inflight','copilot_claimed','investigation_uncertain')}
    probe['airflow_active']=[]
    reset.require_quiescent(probe)
    for key in tuple(probe)[:-1]:
        changed={**probe,key:1}
        with pytest.raises(reset.ResetError):
            reset.require_quiescent(changed)


def test_project_volume_cannot_be_shared_with_foreign_container(monkeypatch):
    own='a'*64; volumes=sorted(reset.PROJECT+'_'+s for s in reset.VOLUME_SUFFIXES)
    item={'Id':own,'Name':'/dq_runner','Config':{'Labels':{'com.docker.compose.project':reset.PROJECT,
           'com.docker.compose.service':'dq-runner'}},'State':{'Running':True},
          'Mounts':[{'Type':'volume','Name':v} for v in volumes]}
    def docker(*args,**kwargs):
        if args[:2]==('ps','-aq') and any(str(a).startswith('volume=') for a in args):
            return own+'\n'+'b'*64
        if args[0]=='ps': return own
        if args[0]=='inspect': return json.dumps([item])
        if args[:2]==('volume','inspect'):
            return json.dumps([{'Labels':{'com.docker.compose.project':reset.PROJECT}}])
        raise AssertionError(args)
    monkeypatch.setattr(reset,'docker',docker)
    with pytest.raises(reset.ResetError,match='shared'):
        reset.docker_inventory()


def test_resume_hash_mismatch_cannot_run_a_phase(tmp_path,monkeypatch):
    monkeypatch.setattr(reset,'ROOT',tmp_path)
    plan=manifest(tmp_path); journal=tmp_path/'journal.json'
    journal.write_text(json.dumps({'hash':'different','phases':['quiesced']}))
    run=Mock(); monkeypatch.setattr(reset,'docker',run)
    with pytest.raises(reset.ResetError,match='another manifest'):
        reset.apply_plan(plan,journal,plan['hash'])
    run.assert_not_called()


def test_host_controller_is_stdlib_and_not_model_or_public_mutation_tool():
    tree=ast.parse(Path(reset.__file__).read_text(encoding='utf-8'))
    imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any((m or '').startswith(('agent.','apps.','requests')) for m in imports)
    source=Path(reset.__file__).read_text(encoding='utf-8')
    assert "docker('system','prune'" not in source and "docker('volume','prune'" not in source
    assert "'--confirm-plan'" in source


def test_airflow_bootstrap_preserves_existing_auth_file(tmp_path,monkeypatch):
    import textwrap
    root=Path(__file__).resolve().parents[1]
    source=(root/'infra/docker-compose.yml').read_text(encoding='utf-8')
    embedded=textwrap.dedent(source.split("python - <<'PY'\n",1)[1].split('\n        PY',1)[0])
    path=tmp_path/'auth.json'; original='{"demo":"existing-secret"}'; path.write_text(original)
    monkeypatch.setenv('AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS','demo:admin')
    monkeypatch.setenv('AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE',str(path))
    monkeypatch.setenv('AIRFLOW_WWW_USER_PASSWORD','must-not-overwrite')
    exec(compile(embedded,'bootstrap-auth','exec'),{})
    assert path.read_text()==original


def test_api_launcher_forwards_shutdown_to_server():
    root=Path(__file__).resolve().parents[1]
    source=(root/'infra/docker-compose.yml').read_text(encoding='utf-8')
    api=source.split('\n  api:',1)[1].split('\n  web:',1)[0]
    assert 'exec python -m uvicorn apps.api.main:app' in api
