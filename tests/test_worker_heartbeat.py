"""External app heartbeats follow successful database/worker passes, never a blind timer."""
import logging
import threading

import httpx
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import main as worker


@pytest.fixture
def monitor(monkeypatch):
    requests, options = [], []
    client = httpx.Client

    def receive(request):
        requests.append(request)
        return httpx.Response(200)

    def connect(**kwargs):
        options.append(kwargs)
        return client(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setenv('FMUC_HEARTBEAT_APP_URL', 'https://heartbeat.example.test/secret-check-id')
    monkeypatch.setattr(worker.httpx, 'Client', connect)
    monkeypatch.setattr(worker, '_beat', {})
    monkeypatch.setattr(worker, '_app_beat', {})
    monkeypatch.setattr(worker, 'ALIVE_FILE', '')
    return requests, options


def test_successful_pass_pings_once_a_minute_after_database_heartbeat(h, monkeypatch, monitor):
    requests, options = monitor
    engine = h.app.state.engine
    moment = [0.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: moment[0])
    assert not worker.process_one(engine)
    with transaction(engine) as db:
        assert db.get(m.WorkerHeartbeat, worker.WORKER_ID).last_seen_at
    assert len(requests) == 1
    moment[0] = 50.0
    assert not worker.process_one(engine)
    assert len(requests) == 1
    moment[0] = 60.0
    assert not worker.process_one(engine)
    assert len(requests) == 2
    assert all(request.method == 'GET' for request in requests)
    assert all(option == {'timeout': 3, 'follow_redirects': False, 'trust_env': False} for option in options)


def test_unset_monitor_does_not_ping(h, monkeypatch, monitor):
    monkeypatch.delenv('FMUC_HEARTBEAT_APP_URL')
    assert not worker.process_one(h.app.state.engine)
    assert monitor == ([], [])


@pytest.mark.parametrize('stage', ['dispatch_pending', 'claim_pending'])
def test_failed_loop_does_not_record_a_healthy_heartbeat(h, monkeypatch, monitor, stage):
    def fail(*args, **kwargs):
        raise RuntimeError('Database unavailable')

    monkeypatch.setattr(worker, stage, fail)
    with pytest.raises(RuntimeError, match='Database unavailable'):
        worker.process_one(h.app.state.engine)
    assert monitor == ([], [])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.WorkerHeartbeat.worker_id)) is None


def test_failed_database_heartbeat_never_pings(h, monkeypatch, monitor):
    def fail(*args, **kwargs):
        raise RuntimeError('Database unavailable')

    monkeypatch.setattr(worker, 'retry_write', fail)
    worker.heartbeat(h.app.state.engine)
    assert monitor == ([], [])
    assert worker._beat == {}


def test_monitor_failure_is_bounded_and_logs_no_url(h, monkeypatch, monitor, caplog):
    def fail(request):
        raise httpx.ConnectTimeout(str(request.url))

    transport = httpx.MockTransport(fail)
    monkeypatch.setattr(worker.httpx, 'MockTransport', lambda receive: transport)
    with caplog.at_level(logging.WARNING):
        assert not worker.process_one(h.app.state.engine)
    assert any(record.message == 'app_heartbeat_failed' and record.error_code == 'ConnectTimeout'
               for record in caplog.records)
    assert 'secret-check-id' not in caplog.text and 'heartbeat.example.test' not in caplog.text


def test_monitor_does_not_follow_redirects(h, monkeypatch, monitor):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://other.example.test/secret-check-id'})

    transport = httpx.MockTransport(redirect)
    monkeypatch.setattr(worker.httpx, 'MockTransport', lambda receive: transport)
    assert not worker.process_one(h.app.state.engine)
    assert len(requests) == 1 and requests[0].url.host == 'heartbeat.example.test'


def test_long_running_job_pings_from_its_valid_database_lease(h, monkeypatch, monitor):
    workspace = h.workspace()
    h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
              revision=workspace['revision'])
    pinged = threading.Event()
    heartbeat = worker.ping_app_heartbeat

    def observed(engine):
        heartbeat(engine)
        pinged.set()

    monkeypatch.setattr(worker, 'ping_app_heartbeat', observed)

    def scheduler(snapshot, *, progress, cancelled):
        assert pinged.wait(8), 'Active job did not report a healthy database lease'
        assert not cancelled()
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    assert worker.process_one(h.app.state.engine, scheduler=scheduler)
    assert len(monitor[0]) == 1
