"""Slow email backlogs yield to jobs and worker health, within global and department bounds."""
from collections import Counter
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.settings import Settings
from apps.worker import email_delivery as email
from apps.worker import main as worker
from .test_email_delivery import configuration, queued, source


BACKENDS = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def backlog(h, *, department_count=2, per_department=4, batch_size=2):
    values = configuration(h, batch_size=batch_size)
    departments = [h.seed['department_id']]
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(department_count - 1):
            department = m.Department(organisation_id=h.seed['organisation_id'], name=f'Email fairness department {index}',
                email_settings=values)
            db.add(department)
            db.flush()
            departments.append(department.id)
            db.add(m.RoleBinding(identity_id=h.seed['identities']['staff'], department_id=department.id,
                role='staff', starts_on=date(2020, 1, 1)))
    for department_id in departments:
        for _ in range(per_department):
            content_id, parent_id = source(h)
            if department_id != h.seed['department_id']:
                with transaction(h.app.state.engine, write=True) as db:
                    db.get(m.DepartmentContent, content_id).department_id = department_id
                    db.get(m.Outbox, parent_id).department_id = department_id
            assert queued(h, parent_id) == 1
    return departments


def remaining(h):
    with transaction(h.app.state.engine) as db:
        return db.scalar(select(func.count()).select_from(m.Outbox).where(m.Outbox.kind == 'email',
            m.Outbox.state.in_(['queued', 'retry'])))


@pytest.mark.parametrize('backend', BACKENDS)
def test_jobs_finish_and_heartbeat_precedes_email_with_global_and_department_count_caps(request, backend, monkeypatch):
    h = request.getfixturevalue(backend)
    departments = backlog(h)
    workspace = h.workspace()
    job_id = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
        revision=workspace['revision'])['job']['id']
    settings = replace(h.settings, worker_email_max_per_pass=3, worker_email_budget_seconds=60)
    clock, order, sends = [0.0], [], []
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_beat', {})
    monkeypatch.delenv('FMUC_HEARTBEAT_APP_URL', raising=False)
    heartbeat = worker.heartbeat

    def healthy(*args, **kwargs):
        order.append('heartbeat')
        heartbeat(*args, **kwargs)

    def send(db, delivery, message, config, supplied_settings):
        assert supplied_settings is settings
        with transaction(h.app.state.engine) as current:
            assert current.get(m.Job, job_id).status == 'succeeded'
            assert current.get(m.WorkerHeartbeat, worker.WORKER_ID) is not None
        sends.append(delivery.department_id)
        order.append('send')
        clock[0] += 11

    def solve(snapshot, *, progress, cancelled):
        order.append('job')
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    monkeypatch.setattr(worker, 'heartbeat', healthy)
    monkeypatch.setattr(email, 'send', send)
    assert worker.process_one(h.app.state.engine, scheduler=solve, settings=settings)
    assert order.index('job') < order.index('heartbeat') < order.index('send')
    assert len(sends) == 3 and set(sends) == set(departments)
    assert sorted(Counter(sends).values()) == [1, 2]
    assert remaining(h) == 5
    with transaction(h.app.state.engine) as db:
        assert db.get(m.WorkerHeartbeat, worker.WORKER_ID).passes >= 4


@pytest.mark.parametrize('backend', BACKENDS)
def test_slow_provider_exhausts_global_elapsed_budget_and_leaves_other_mail_pending(request, backend, monkeypatch):
    h = request.getfixturevalue(backend)
    backlog(h, batch_size=50)
    settings = replace(h.settings, worker_email_max_per_pass=100, worker_email_budget_seconds=5)
    clock, sends = [0.0], []
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker, '_beat', {})
    monkeypatch.delenv('FMUC_HEARTBEAT_APP_URL', raising=False)

    def send(db, delivery, message, config, supplied_settings):
        assert supplied_settings is settings
        sends.append(delivery.id)
        clock[0] += settings.worker_email_budget_seconds

    monkeypatch.setattr(email, 'send', send)
    assert worker.process_one(h.app.state.engine, settings=settings)
    assert len(sends) == 1 and remaining(h) == 7
    assert worker.process_one(h.app.state.engine, settings=settings)
    assert len(sends) == 2 and len(set(sends)) == 2 and remaining(h) == 6


@pytest.mark.parametrize('name,environment,maximum', [
    ('worker_email_max_per_pass', 'FMUC_WORKER_EMAIL_MAX_PER_PASS', 10000),
    ('worker_email_budget_seconds', 'FMUC_WORKER_EMAIL_BUDGET_SECONDS', 60),
])
def test_email_worker_bounds_are_environment_backed_and_validate(name, environment, maximum, monkeypatch):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv(environment, '7')
    settings = Settings.from_env()
    assert getattr(settings, name) == 7
    settings.check()
    for value in (0, -1, maximum + 1):
        with pytest.raises(RuntimeError, match=environment):
            replace(settings, **{name: value}).check()
