"""Coverage authorization depends on issued duties, not optional report metadata."""
import copy
from datetime import date
from types import SimpleNamespace

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import publication_snapshot
from apps.api.services.schedule import in_force_snapshot
from .governance_support import identity


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('snapshot_kind', ['complete', 'without_metadata', 'empty'])
@pytest.mark.parametrize('replaced', [False, True])
def test_coverage_accepts_sparse_snapshots_without_broadening_staff_access(database, request, snapshot_kind, replaced):
    h = request.getfixturevalue(database)
    own, other, outside = h.seed['people']
    workspace = h.shift(h.workspace(), people=[own])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[other])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        snapshot = publication_snapshot(db, stored, evaluation=stored.validation)
        own_shift = next(sh['id'] for sh in snapshot['shifts']
                         if any(p['person_id'] == own for p in sh['participants']))
        all_shifts = {own_shift} if replaced else {sh['id'] for sh in snapshot['shifts']}
        if snapshot_kind == 'without_metadata':
            for key in ('workspace', 'people', 'locations', 'services'):
                snapshot.pop(key)
        elif snapshot_kind == 'empty':
            snapshot = {}
        stored.status = 'published'
        publication = m.Publication(workspace_id=stored.id, department_id=stored.department_id,
            sequence=1, starts_on=stored.starts_on, ends_on=stored.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], snapshot=snapshot,
            effective_until=date(2026, 10, 5) if replaced else None,
            validation=stored.validation, fingerprint='0' * 64)
        db.add(publication)
        db.flush()
        publication_id = publication.id
    frozen = copy.deepcopy(snapshot)
    h.command('coordinator', 'availability.record', {'person_id': own,
        'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Synthetic confidential reason'})
    path = f'/api/publications/{publication_id}/coverage'
    operator = h.get('coordinator', path)['evaluation']
    assert {row['shift_id'] for row in operator['coverage']} == all_shifts
    assert operator['findings'] and operator['error_count'] > 0
    if snapshot_kind == 'empty':
        h.get('staff', path, status=404)
    else:
        personal = h.get('staff', path)['evaluation']
        assert {row['shift_id'] for row in personal['coverage']} == {own_shift}
        assert personal['findings'] == []
        assert personal['error_count'] == personal['warning_count'] == 0
    assert h.get('coordinator', path)['evaluation'] == operator
    identity(h, 'other_staff', ['staff'], other)
    h.get('other_staff', path, status=404 if replaced or snapshot_kind == 'empty' else 200)
    identity(h, 'outside_staff', ['staff'], outside)
    h.get('outside_staff', path, status=404)
    h.get('credentialer', path, status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == frozen


@pytest.mark.parametrize('frozen_work', [False, True])
def test_sparse_replaced_snapshot_keeps_wish_outcomes_and_frozen_work(frozen_work):
    snapshot = {'timezone': 'Asia/Riyadh', 'shifts': [
        {'id': f'duty-{day}', 'assignment_type_id': 'type', 'start': f'2026-10-{day:02}T08:00:00+03:00',
         'participants': [{'person_id': 'person', 'start': f'2026-10-{day:02}T08:00:00+03:00'}]}
        for day in (4, 5)], 'wishes': [
            {'id': 'off', 'kind': 'prefer_off', 'person_id': 'person', 'first_day': '2026-10-04',
             'last_day': '2026-10-06', 'outcome': 'partly',
             'outcome_reasons': [{'day': f'2026-10-{day:02}', 'code': 'assigned'} for day in (4, 5)]},
            {'id': 'future', 'kind': 'prefer_on', 'person_id': 'person', 'first_day': '2026-10-05',
             'last_day': '2026-10-05', 'outcome': 'granted'}]}
    if frozen_work:
        snapshot['wish_worked'] = {'person': {f'2026-10-{day:02}': ['type'] for day in (4, 5)}}
    original = copy.deepcopy(snapshot)
    publication = SimpleNamespace(snapshot=snapshot, starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11),
                                  effective_until=date(2026, 10, 5))
    projected = in_force_snapshot(None, publication)
    assert [shift['id'] for shift in projected['shifts']] == ['duty-4']
    wish, = projected['wishes']
    assert wish['id'] == 'off' and wish['outcome'] == 'declined'
    assert wish['outcome_reasons'] == [{'day': '2026-10-04', 'code': 'assigned'}]
    if frozen_work:
        assert projected['wish_worked'] == {'person': {'2026-10-04': ['type']}}
    assert 'workspace' not in projected and publication.snapshot == original
