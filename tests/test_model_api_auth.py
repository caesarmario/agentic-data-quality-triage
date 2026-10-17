"""Billable API operations fail closed before any platform or provider work."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
import yaml

from apps.api import main as api
from apps.common.control_plane import ControlPlaneClient, ControlPlaneResponseError


CASES = [
    ('/api/v1/copilot/answer', {'question':'Explain current data quality'}, 'list_alerts'),
    ('/api/v1/triage/run', {'alert_key':'DQ-20261016-ABC123'}, 'run_triage'),
]


@pytest.mark.parametrize('route,body,entry', CASES)
@pytest.mark.parametrize('configured,supplied,status', [(None,None,503),('server-secret',None,401),('server-secret','wrong-secret',401)])
def test_rejected_model_request_never_accesses_platform(monkeypatch,caplog,route,body,entry,configured,supplied,status):
    if configured is None:
        monkeypatch.delenv('CONTROL_PLANE_APPROVAL_TOKEN', raising=False)
    else:
        monkeypatch.setenv('CONTROL_PLANE_APPROVAL_TOKEN', configured)
    work=Mock(side_effect=AssertionError('No platform work permitted'))
    for name in ('list_alerts','run_triage','build_clickhouse_client','build_operator_answer','write_copilot_api_audit_event'):
        monkeypatch.setattr(api,name,work)
    response=TestClient(api.app).post(route,json=body,headers={'X-Control-Plane-Token':supplied} if supplied else {})
    assert response.status_code == status
    work.assert_not_called()
    assert 'server-secret' not in response.text + caplog.text
    assert 'wrong-secret' not in response.text + caplog.text


@pytest.mark.parametrize('route,body,entry', CASES)
def test_authorized_model_request_reaches_only_mocked_work(monkeypatch,route,body,entry):
    monkeypatch.setenv('CONTROL_PLANE_APPROVAL_TOKEN','server-secret')
    work=Mock(side_effect=LookupError('Owned fixture not found'))
    monkeypatch.setattr(api,entry,work)
    response=TestClient(api.app).post(route,json=body,headers={'X-Control-Plane-Token':'server-secret'})
    assert response.status_code == 404
    work.assert_called_once()


@pytest.mark.parametrize('route', [case[0] for case in CASES])
def test_client_requires_own_configured_token_before_http(monkeypatch,route):
    http=Mock(side_effect=AssertionError('No HTTP permitted'))
    monkeypatch.setattr('apps.common.control_plane.requests.request',http)
    with pytest.raises(ControlPlaneResponseError,match='require'):
        ControlPlaneClient('http://api:8000',approval_token='').request_json('POST',route,json_body={})
    http.assert_not_called()


@pytest.mark.parametrize('route', [case[0] for case in CASES])
def test_redirect_does_not_forward_server_credentials(monkeypatch,route):
    response=SimpleNamespace(status_code=307,raise_for_status=lambda:None)
    http=Mock(return_value=response)
    monkeypatch.setattr('apps.common.control_plane.requests.request',http)
    with pytest.raises(ControlPlaneResponseError,match='redirect'):
        ControlPlaneClient('http://api:8000',approval_token='server-secret').request_json(
            'POST',route,json_body={},headers={'X-Control-Plane-Token':'caller-secret'})
    http.assert_called_once()
    assert http.call_args.kwargs['allow_redirects'] is False
    assert http.call_args.kwargs['headers'] == {'X-Control-Plane-Token':'server-secret'}


def test_all_published_ports_are_loopback_and_internal_hosts_unchanged():
    root=Path(__file__).resolve().parents[1]
    config=yaml.safe_load((root/'infra/docker-compose.yml').read_text(encoding='utf-8'))
    for service in config['services'].values():
        for port in service.get('ports',[]):
            assert port.startswith('127.0.0.1:'), port
    assert config['x-app-env']['CLICKHOUSE_HOST'] == 'clickhouse'
    assert config['services']['web']['environment']['CONTROL_PLANE_API_URL'] == 'http://api:8000'


def test_health_is_not_a_privileged_endpoint(monkeypatch):
    monkeypatch.delenv('CONTROL_PLANE_APPROVAL_TOKEN',raising=False)
    assert TestClient(api.app).get('/health').status_code == 200


def test_object_store_has_capacity_for_five_bucket_collections():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / 'infra/docker-compose.yml').read_text(encoding='utf-8'))
    command = config['services']['seaweed-volume']['command']
    maximum = int(next(item.split('=', 1)[1] for item in command if item.startswith('-max=')))
    assert maximum >= 5 * 7, 'Fresh buckets must not exhaust physical volume slots'


@pytest.mark.parametrize('route,body,entry', CASES)
def test_non_ascii_authorization_header_is_denied_not_server_error(monkeypatch,route,body,entry):
    monkeypatch.setenv('CONTROL_PLANE_APPROVAL_TOKEN','server-secret')
    work=Mock(side_effect=AssertionError('Unauthorized work'))
    monkeypatch.setattr(api,entry,work)
    result=TestClient(api.app).post(route,json=body,headers=[(b'X-Control-Plane-Token',b'\xff')])
    assert result.status_code == 401
    work.assert_not_called()


def test_web_empty_summary_is_neutral_and_proxy_uses_server_token():
    root=Path(__file__).resolve().parents[1]
    overview=(root/'apps/web/app/page.tsx').read_text(encoding='utf-8')
    assert '<DailySummaryGate summary={summary.data}>' in overview
    gate=(root/'apps/web/components/daily-summary-gate.tsx').read_text(encoding='utf-8')
    assert 'if (summary.availability === "empty")' in gate
    assert 'No quality data yet' in gate
    proxy=(root/'apps/web/app/api/control-plane/[...path]/route.ts').read_text(encoding='utf-8')
    assert 'requiresControlPlaneToken(capability)' in proxy
    assert 'headers.set("X-Control-Plane-Token", approvalToken!)' in proxy
    assert 'redirect: "error"' in proxy
