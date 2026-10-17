"""Empty daily data is a valid result, not a healthy-state or dependency fallback."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from agent.tools.daily_summary import build_daily_summary_payload
from apps.api import main as api
from apps.common.control_plane import ControlPlaneClient, ControlPlaneResponseError


@pytest.mark.parametrize('dt',[None,'2026-10-16'])
def test_api_returns_empty_success_for_missing_quality_data(monkeypatch,dt):
    monkeypatch.setattr(api,'fetch_daily_quality_summary',lambda **_kw:build_daily_summary_payload(dt,[]))
    response=TestClient(api.app).get('/api/v1/summaries/daily',params={'dt':dt} if dt else {})
    assert response.status_code == 200
    result=response.json()
    assert result['dt'] == dt
    assert result['availability'] == 'empty'
    assert result['status'] == 'success'
    assert result['total_checks'] == result['total_open_alerts'] == 0
    assert result['check_counts'] == result['alert_counts'] == []
    assert result['summary'] == 'No quality data yet'


def test_dependency_failure_is_not_empty(monkeypatch):
    def fail(**_kwargs):
        raise ConnectionError('Synthetic dependency unavailable')
    monkeypatch.setattr(api,'fetch_daily_quality_summary',fail)
    response=TestClient(api.app).get('/api/v1/summaries/daily')
    assert response.status_code >= 500
    assert response.json().get('availability') != 'empty'


@pytest.mark.parametrize('dt',[None,'2026-10-16'])
def test_client_accepts_empty_result_without_fabricating_date(monkeypatch,dt):
    client=ControlPlaneClient('http://api:8000')
    payload=build_daily_summary_payload(dt,[])
    payload.pop('sql',None)
    monkeypatch.setattr(client,'request_json',lambda *_a,**_kw:deepcopy(payload))
    assert client.get_daily_summary(dt)['dt'] == dt


@pytest.mark.parametrize('change',[
    {'dt':''}, {'availability':'healthy'}, {'status':'failed'}, {'total_checks':False},
    {'check_counts':[{'status':'pass','count':0}]},
    {'availability':'available'},
])
def test_client_rejects_invalid_empty_contract(monkeypatch,change):
    client=ControlPlaneClient('http://api:8000')
    payload=build_daily_summary_payload(None,[]); payload.pop('sql',None); payload.update(change)
    monkeypatch.setattr(client,'request_json',lambda *_a,**_kw:payload)
    with pytest.raises(ControlPlaneResponseError):
        client.get_daily_summary()


def test_client_rejects_null_for_explicit_date(monkeypatch):
    client=ControlPlaneClient('http://api:8000')
    payload=build_daily_summary_payload(None,[]); payload.pop('sql',None)
    monkeypatch.setattr(client,'request_json',lambda *_a,**_kw:payload)
    with pytest.raises(ControlPlaneResponseError):
        client.get_daily_summary('2026-10-16')
