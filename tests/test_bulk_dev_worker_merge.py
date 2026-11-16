"""Bulk retention and email delivery share the same bounded worker pass."""
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import email_delivery
from apps.worker import main as worker
from .test_bulk_staged import staged_change
from .test_worker_email_fairness import backlog


def test_worker_expires_staged_payloads_and_finishes_jobs_before_email(h, monkeypatch):
    _, staged = staged_change(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.BulkStaged, staged['id']).expires_at = m.now() - timedelta(days=1)
    backlog(h, department_count=1, per_department=1)
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
        revision=workspace['revision'])['job']
    settings = replace(h.settings, worker_email_max_per_pass=1)
    monkeypatch.setattr(worker, '_bulk_staged_pruned', {})
    monkeypatch.setattr(worker, '_beat', {})
    monkeypatch.delenv('FMUC_HEARTBEAT_APP_URL', raising=False)
    sent = []

    def send(db, delivery, message, config, supplied_settings):
        assert supplied_settings is settings
        with transaction(h.app.state.engine) as current:
            expired = current.get(m.BulkStaged, staged['id'])
            assert expired.status == 'expired' and expired.payload == {}
            assert current.get(m.Job, job['id']).status == 'succeeded'
        sent.append(delivery.id)

    monkeypatch.setattr(email_delivery, 'send', send)
    assert worker.process_one(h.app.state.engine, settings=settings,
        scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible', 'additions': [], 'removals': []})
    assert len(sent) == 1
    with transaction(h.app.state.engine) as db:
        history = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'bulk_staged', m.DefinitionHistory.object_id == staged['id'])
            .order_by(m.DefinitionHistory.object_revision)))
        assert [row.snapshot['status'] for row in history] == ['active', 'expired']
        assert all('payload' not in row.snapshot for row in history)
