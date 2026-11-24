"""Fill-panel currency, writer admission and the authoritative background check."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import planning
from apps.worker.main import process_one
from tests.clock_support import freeze_clock
from tests.test_be18_command_latency import edit, optional_type, worker_up
from tests.test_events import event, event_type, repeat
from tests.test_fill_options import options
from tests.test_fill_options_async import enqueue


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_fill_panel_assign_runs_once_under_writer_then_worker_checks_and_undo_restores(h, monkeypatch):
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[], typ=optional_type(h))['workspace']
    shift, = h.detail(w)['shifts']
    job = enqueue(h, w, shift['id'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('coordinator', f"/api/jobs/{job['id']}")['job']['result']
    candidate = next(row for row in result['candidates'] if row['status'] == 'ok')
    stamp = result.get('assignment_stamp', result['source_fingerprint'])
    calls = []
    save = planning.save_shift

    def counted(db, *args, **kwargs):
        calls.append((bool(db.connection().info.get('rota_write')), bool(db.info.get('_assignment_reply'))))
        return save(db, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(planning, 'save_shift', counted)
        saved = edit(h, w, shift, [{**result['slot'], 'person_id': candidate['person_id']}], expected_fingerprint=stamp)
    assert calls == [(True, True)]
    w = saved['workspace']
    assert saved['validation']['scope'] == 'command_changes'
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        job_id = db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id'], m.Job.kind == 'validation'))
        assert db.get(m.Job, job_id).status == 'queued'
        expected = planning.revalidate(db, workspace, store=False, include_internal=True)
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        assert workspace.validation == expected
        assert workspace.revision == w['revision']
        assert db.get(m.Job, job_id).status == 'succeeded'
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo assignment'}, w['id'], w['revision'])
    assert h.detail(undone['workspace'])['shifts'][0]['participants'] == []


@pytest.mark.parametrize('changed', ['person', 'membership', 'rule', 'availability', 'session', 'settings'])
def test_candidate_input_changes_replace_the_stamp_and_refuse_the_old_one(h, changed):
    person_id = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[])['workspace']
    shift, = h.detail(w)['shifts']
    result = options(h, w, shift['id'])
    stamp = result['assignment_stamp']
    if changed == 'session':
        w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', people=[])['workspace']
    elif changed == 'rule':
        h.policy({'kind': 'min_rest', 'hours': 12})
    elif changed == 'availability':
        h.command('coordinator', 'availability.record', {'person_id': person_id,
            'kind': 'actual_absence', 'start': shift['start'], 'end': shift['end'],
            'private_reason': 'Synthetic scheduling input'})
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if changed == 'person':
                db.get(m.Person, person_id).active = False
            elif changed == 'membership':
                row = db.scalar(select(m.Membership).where(m.Membership.person_id == person_id,
                                m.Membership.department_id == h.seed['department_id']).limit(1))
                row.ends_on = date(2026, 10, 4)
            else:
                db.get(m.Organisation, h.seed['organisation_id']).week_start = 0
    assert options(h, w, shift['id'])['assignment_stamp'] != stamp
    refused = edit(h, w, shift, [{**result['slot'], 'person_id': person_id}],
                   expected_fingerprint=stamp, status=409)
    assert refused['error']['code'] == 'STALE_PREVIEW'
    assert h.detail(w)['shifts'][0]['participants'] == []


def test_bound_assign_checks_revision_and_refuses_current_safety_violation_under_writer(h, monkeypatch):
    freeze_clock(monkeypatch)
    person_id = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[])['workspace']
    shift, = h.detail(w)['shifts']
    h.policy({'kind': 'restriction'}, {'person_id': person_id})
    worker_up(h)
    result = options(h, w, shift['id'])
    stamp = result['assignment_stamp']
    participants = [{**result['slot'], 'person_id': person_id}]
    stale = edit(h, {**w, 'revision': w['revision'] - 1}, shift, participants,
                 expected_fingerprint=stamp, status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    checks = []
    original = snapshots._domain_snapshot

    def checked(db, *args, **kwargs):
        checks.append(bool(db.connection().info.get('rota_write')))
        return original(db, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(snapshots, '_domain_snapshot', checked)
        refused = edit(h, w, shift, participants, expected_fingerprint=stamp, status=422)
    assert refused['error']['code'] == 'EDIT_INVALID'
    assert checks and all(checks)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, w['id']).revision == w['revision']
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == w['id'], m.Job.kind == 'validation'))


def test_recurring_protected_time_keeps_the_full_fingerprint(h, monkeypatch):
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[])['workspace']
    shift, = h.detail(w)['shifts']
    assert options(h, w, shift['id']).get('assignment_stamp')
    typ = event_type(h, blocks_duties=True)
    event(h, typ, repeat=repeat())
    result = options(h, w, shift['id'])
    assert not result.get('assignment_stamp')
    assert result['source_fingerprint']
