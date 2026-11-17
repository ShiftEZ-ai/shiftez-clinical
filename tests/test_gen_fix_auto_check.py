"""Opening a draft reuses current evidence or one shared worker job."""
import pytest
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one

pytestmark = pytest.mark.postgres


def automatic(h, w):
    return h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation',
        'automatic': True}, revision=w['revision'])


def test_current_check_is_reused_and_evaluator_drift_queues_one_job(pg):
    w = pg.shift(pg.workspace())['workspace']
    assert pg.detail(w)['workspace']['validation_current']
    assert automatic(pg, w)['job'] is None
    with transaction(pg.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        row.validation = {**row.validation, 'engine_version': 'outdated'}
    assert not pg.detail(w)['workspace']['validation_current']
    first = automatic(pg, w)['job']
    assert first['id'] == automatic(pg, w)['job']['id']
    assert process_one(pg.app.state.engine)
    assert automatic(pg, w)['job'] is None
    assert pg.detail(w)['workspace']['validation_current']
    with transaction(pg.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Job).where(m.Job.workspace_id == w['id'])))) == 1


def test_validation_result_keeps_input_currency_and_old_revision_editable(pg):
    w = pg.shift(pg.workspace())['workspace']
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    before_detail = pg.detail(w)
    before = before_detail['workspace']
    before_fingerprint = before_detail['current_fingerprint']
    job = automatic(pg, before)['job']
    assert job['status'] == 'queued'
    assert process_one(pg.app.state.engine)
    after_detail = pg.detail(w)
    after = after_detail['workspace']
    assert after['revision'] == before['revision']
    assert after_detail['current_fingerprint'] == before_fingerprint
    assert after['validation']['fingerprint'] == before_fingerprint
    assert automatic(pg, after)['job'] is None
    edited = pg.shift({**w, 'revision': before['revision']}, start='2026-10-05T08:00:00+03:00',
                       end='2026-10-05T12:00:00+03:00')
    assert edited['workspace']['revision'] > before['revision']


def test_missing_check_and_changed_fingerprint_are_stale(pg):
    w = pg.workspace()
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    job = automatic(pg, w)['job']
    assert job['status'] == 'queued'
    assert process_one(pg.app.state.engine)
    pg.command('coordinator', 'availability.record', {'person_id': pg.seed['people'][0],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T09:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Synthetic availability change'})
    assert not pg.detail(w)['workspace']['validation_current']
    assert automatic(pg, w)['job']['id'] != job['id']


def test_simultaneous_viewers_share_the_pending_check(pg):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    w = pg.workspace()
    pg.client('coordinator')
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    barrier = Barrier(2)
    def open_view():
        barrier.wait(timeout=10)
        return automatic(pg, w)['job']['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [pool.submit(open_view) for _ in range(2)]
        assert first.result(timeout=30) == second.result(timeout=30)


def test_current_published_change_check_is_reused(pg):
    pg.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
               'metric': 'duties', 'comparison': 'maximum', 'amount': 100})
    published = pg.publish(pg.shift(pg.workspace())['workspace'])
    w = pg.workspace(base=published['publication_id'])
    w = pg.lifecycle(w, 'validate')['workspace']
    assert pg.detail(w)['workspace']['validation_current']
    assert automatic(pg, w)['job'] is None


def test_input_changes_do_not_join_an_obsolete_running_check(pg):
    w = pg.workspace()
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    first = automatic(pg, w)['job']
    from apps.worker.main import claim_job
    with transaction(pg.app.state.engine, write=True) as db:
        assert claim_job(db)['id'] == first['id']
    pg.command('coordinator', 'availability.record', {'person_id': pg.seed['people'][0],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T09:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Changed while checking'})
    second = automatic(pg, w)['job']
    assert second['id'] != first['id']
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Job, first['id']).error_code == 'STALE_INPUTS'


def test_simultaneous_workerless_viewers_share_one_evaluation(pg, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from apps.api.services import planning
    w = pg.workspace()
    pg.client('coordinator')
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    entered, release, second_started = Event(), Event(), Event()
    calls, evaluate = [], planning.evaluate
    def held(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', held)
    def check(second=False):
        if second:
            second_started.set()
        return pg.command('coordinator', 'workspace.validate', {'automatic': True, 'reason': 'Automatic rota check'},
                          w['id'], w['revision'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(check)
        try:
            assert entered.wait(10)
            second = pool.submit(check, True)
            assert second_started.wait(10)
        finally:
            release.set()
        assert first.result(timeout=30)['validation']['fingerprint'] == second.result(timeout=30)['validation']['fingerprint']
    assert calls == [True]


def test_amendment_check_expires_when_issued_window_changes(pg):
    from packages.domain.time import aware
    published = pg.publish(pg.shift(pg.workspace())['workspace'])
    w = pg.workspace(base=published['publication_id'])
    assert pg.detail(w)['workspace']['validation_current']
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Publication, published['publication_id']).effective_until = aware('2026-10-06T00:00:00+03:00')
    assert not pg.detail(w)['workspace']['validation_current']
    assert automatic(pg, w)['job']['status'] == 'queued'
