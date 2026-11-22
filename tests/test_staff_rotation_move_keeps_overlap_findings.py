"""A rotation move preserves pre-existing overlaps in a full draft check."""
from itertools import combinations

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import copy_snapshot_into
from apps.api.snapshots import publication_snapshot
from packages.domain.validation import check_contracts, finding_observations
from tests.test_residency import setup_residency
from tests.test_staff_overlap_consumers import overlapping_data
from tests.test_timeline_commands import REASON, gesture
from tests.test_timeline_data import actor_for


def overlapping_replacement(h, person_id):
    data = overlapping_data(h)
    for shift in data['shifts']:
        shift['series_snapshot']['definition'] = {
            'default_person_ids': [person_id], 'close_when_all_usual_unavailable': True}
    issued = h.workspace(name='Synthetic issued overlap')
    publication_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, issued['id'])
        actor = actor_for(h, db)
        copy_snapshot_into(db, actor, workspace, data, preserve_ids=True)
        workspace.status = 'published'
        db.add(m.Publication(id=publication_id, department_id=workspace.department_id,
            workspace_id=workspace.id, sequence=1, starts_on=workspace.starts_on, ends_on=workspace.ends_on,
            published_by=actor.identity_id, snapshot=publication_snapshot(db, workspace),
            validation={'contracts': check_contracts()}, fingerprint='0' * 64))
    draft = h.command('coordinator', 'workspace.create', {
        'name': 'Synthetic replacement draft', 'starts_on': '2026-10-05', 'ends_on': '2026-10-11',
        'replaces_publication_id': publication_id,
    })['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        copy_snapshot_into(db, actor_for(h, db), db.get(m.Workspace, draft['id']), data)
    return draft


def draft_overlap_pairs(h, draft, person_id):
    validation = h.lifecycle(draft, 'validate')['validation']
    overlaps = [row['detail']['overlap'] for row in finding_observations(validation)
                if row['code'] == 'TIME_OVERLAP' and row['person_id'] == person_id]
    return {tuple(sorted((row['shift_id'], row['other_shift_id']))) for row in overlaps}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('role', ['staff', 'coordinator'])
def test_rotation_move_keeps_existing_overlap_pairs_in_the_draft_check(h, role):
    _, _, cohort, _, people, _ = setup_residency(h)
    own = people[0]
    draft = overlapping_replacement(h, own)
    source = h.configure('rotations', {
        'person_id': own, 'cohort_id': cohort['id'], 'service_id': h.seed['services'][0],
        'department_available': False, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
    }, role='coordinator')['record']
    move = {'action': 'move', 'person_id': own, 'rotation_id': source['id'],
            'expected_revision': source['revision'], 'starts_on': '2026-10-11', 'ends_on': '2026-10-18',
            'reason': REASON}

    shifts = h.detail(draft)['shifts']
    assert len(shifts) == 3
    assert all(any(p['person_id'] == own for p in row['participants']) for row in shifts)
    before = draft_overlap_pairs(h, draft, own)
    assert before == set(combinations(sorted(row['id'] for row in shifts), 2))

    saved = gesture(h, move, role)
    if role == 'staff':
        assert saved['status'] == 'submitted'
        request = saved['request']
        approved = h.command('programme_lead', 'request.decide', {'decision': 'approve', 'reason': REASON},
                             request['id'], request['revision'])['record']
        assert approved['status'] == 'approved'
    else:
        assert saved['status'] == 'applied'

    rotations = h.get('coordinator', '/api/catalogues')['rotations']
    rotation, = [row for row in rotations if row['id'] == source['id']]
    assert (rotation['starts_on'], rotation['ends_on']) == (move['starts_on'], move['ends_on'])
    assert draft_overlap_pairs(h, draft, own) == before
