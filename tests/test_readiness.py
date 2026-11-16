"""The actual current migration must be ready; stale or absent schemas must not."""
from datetime import timedelta
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from apps.api import models as m
from apps.api.main import create_app
from apps.api.settings import Settings
from apps.api.db import transaction
from apps.api.schema import supported_revision
from apps.worker import main as worker


def test_migrated_database_is_ready_and_matches_actual_packaged_head(h):
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get('/health/ready')
        assert response.status_code == 200, response.text
        assert response.json() == {'status':'ready'}
        assert client.get('/health/live').status_code == 200


@pytest.mark.parametrize('path', ['/health/ready', '/health/service'])
@pytest.mark.parametrize('versions', [['0003_integrity'], ['unknown_future'], [], ['0006_planner_views', 'other_branch']])
def test_stale_missing_or_multiple_recorded_heads_are_not_ready(h, versions, path):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.WorkerHeartbeat(worker_id='ready-worker', started_at=m.now(), last_seen_at=m.now(), passes=1))
        db.execute(text('DELETE FROM alembic_version'))
        for version in versions:
            db.execute(text('INSERT INTO alembic_version(version_num) VALUES (:version)'), {'version':version})
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response=client.get(path)
        assert response.status_code==503
        assert response.json()=={'status':'unavailable'}
        assert client.get('/health/live').status_code==200


@pytest.mark.parametrize('path', ['/health/ready', '/health/service'])
def test_uninitialised_database_is_explicitly_unready_without_sql_details(tmp_path, path):
    app = create_app(Settings(environment='test',database_url='sqlite:///'+str(tmp_path/'empty.db')))
    try:
        with TestClient(app,base_url='http://127.0.0.1:8000') as client:
            response=client.get(path)
            assert response.status_code==503
            assert response.json()=={'status':'unavailable'}
            assert 'alembic_version' not in response.text and str(tmp_path) not in response.text
            assert client.get('/health/live').status_code==200
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize('age,threshold,expected', [(None, 180, 503), (181, 180, 503),
    (180, 180, 200), (179, 180, 200), (61, 60, 503), (61, 120, 200)])
def test_only_service_readiness_requires_a_recent_worker_using_the_deployment_threshold(h, monkeypatch, age, threshold, expected):
    object.__setattr__(h.settings, 'worker_heartbeat_max_age_seconds', threshold)
    moment = m.now()
    monkeypatch.setattr(m, 'now', lambda: moment)
    if age is not None:
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.WorkerHeartbeat(worker_id='ready-worker', started_at=moment-timedelta(hours=1),
                                     last_seen_at=moment-timedelta(seconds=age), passes=1))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get('/health/service')
        assert response.status_code == expected
        assert response.json() == {'status': 'ready' if expected == 200 else 'unavailable'}
        response = client.get('/health/ready')
        assert response.status_code == 200 and response.json() == {'status': 'ready'}
        assert client.get('/health/live').status_code == 200


def test_service_readiness_requires_a_job_worker_not_a_push_only_pass(h, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(tmp_path / 'worker-alive'))
    monkeypatch.setattr(worker, '_beat', {})
    worker.process_push_one(h.app.state.engine, h.settings, record_worker_heartbeat=False)
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        assert client.get('/health/service').status_code == 503
    worker.heartbeat(h.app.state.engine)
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get('/health/service')
        assert response.status_code == 200 and response.json() == {'status': 'ready'}


def test_a_stale_retired_worker_does_not_make_a_live_worker_unready(h):
    with transaction(h.app.state.engine, write=True) as db:
        moment = m.now()
        for name, age in [('retired', 1000), ('current', 1)]:
            db.add(m.WorkerHeartbeat(worker_id=name, started_at=moment-timedelta(hours=1),
                                     last_seen_at=moment-timedelta(seconds=age), passes=1))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        response = client.get('/health/service')
        assert response.status_code == 200 and response.json() == {'status': 'ready'}


@pytest.mark.parametrize('heads', [0,2])
def test_packaged_migration_graph_must_have_one_head(tmp_path, heads):
    folder=tmp_path/'migrations';(folder/'versions').mkdir(parents=True)
    for index in range(heads):
        (folder/'versions'/f'{index}.py').write_text(f'revision="branch_{index}"\ndown_revision=None\n')
    with pytest.raises(RuntimeError,match='exactly one'):
        supported_revision(folder)
