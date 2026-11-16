"""A one-sided absence must never acquire an unrelated cancellation target."""
from types import SimpleNamespace
import uuid

import pytest

from apps.api.db import transaction
from apps.api.services.series_conflicts import cancel_actions
from .test_alpha_calendar import programme
from .test_series_overlap_schedule import DAY


@pytest.mark.parametrize('kind', ['unavailable', 'external_rotation'])
def test_one_sided_conflict_cancels_only_its_duty_and_undo_preserves_other(h, kind):
    own, other = h.seed['people'][:2]
    first = h.shift(h.workspace(), *DAY, people=[own])
    made = h.shift(first['workspace'], *DAY, people=[other], service=1, location=1)
    w = made['workspace']
    if kind == 'unavailable':
        h.command('coordinator', 'availability.record', {'person_id': own, 'kind': 'recorded_leave',
            'start': '2026-10-05T09:00:00+03:00', 'end': '2026-10-05T10:00:00+03:00',
            'private_reason': 'Private fictional absence'})
    else:
        _, _, cohort = programme(h)
        h.configure('rotations', {'person_id': own, 'cohort_id': cohort['id'],
            'service_id': h.seed['services'][0], 'department_available': False,
            'starts_on': '2026-10-05', 'ends_on': '2026-10-06'}, role='coordinator')
    h.command('coordinator', 'workspace.validate', {'reason': 'Check fictional absence'}, w['id'], w['revision'])
    before = h.detail(w)
    shifts = {s['id']: s for s in before['shifts']}
    code = 'UNAVAILABLE' if kind == 'unavailable' else 'EXTERNAL_ROTATION'
    conflicts = [c for c in shifts[first['shift_id']]['double_booking_conflicts'] if c['code'] == code]
    assert len(conflicts) == 1
    assert any(f['code'] == code and f['shift_id'] == first['shift_id']
               for f in before['workspace']['validation']['findings'])
    for conflict in conflicts:
        assert conflict['other_shift_id'] is None and conflict['other_series_id'] is None
        [action] = conflict['cancel_actions']
        assert action['side'] == 'this' and action['kind'] == 'shift.action'
        assert action['payload']['workspace_id'] == w['id']
        assert action['payload']['shift_ids'] == [first['shift_id']]
    cancelled = h.command('coordinator', action['kind'], action['payload'], action.get('target_id'), w['revision'])
    current = {s['id']: s for s in h.detail(w)['shifts']}
    assert current[first['shift_id']]['cancelled']
    assert current[made['shift_id']] == shifts[made['shift_id']]
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore fictional own-duty cancellation'},
              w['id'], cancelled['workspace']['revision'])
    restored = {s['id']: s for s in h.detail(w)['shifts']}
    assert not restored[first['shift_id']]['cancelled']
    assert restored[first['shift_id']]['participants'] == shifts[first['shift_id']]['participants']
    assert restored[made['shift_id']] == shifts[made['shift_id']]


@pytest.mark.parametrize('other', [{}, {'other_shift_id': None}, {'other_shift_id': str(uuid.uuid4())},
    {'other_occurrence_on': '2026-10-05'}, {'other_series_id': str(uuid.uuid4())}])
def test_incomplete_other_identity_never_resolves_manual_duties(h, other):
    first = h.shift(h.workspace(), *DAY, people=[])
    made = h.shift(first['workspace'], *DAY, people=[], service=1, location=1)
    conflict = {'code': 'UNAVAILABLE', 'shift_id': first['shift_id'], **other}
    actor = SimpleNamespace(department_id=h.seed['department_id'], permits=lambda role: role == 'coordinator')
    with transaction(h.app.state.engine) as db:
        result = cancel_actions(db, actor, [conflict, conflict])
    for row in result:
        [action] = row['cancel_actions']
        assert action['side'] == 'this' and action['payload']['shift_ids'] == [first['shift_id']]
    result[0]['cancel_actions'][0]['payload']['shift_ids'].append(made['shift_id'])
    assert result[1]['cancel_actions'][0]['payload']['shift_ids'] == [first['shift_id']]


@pytest.mark.parametrize('code', ['UNAVAILABLE', 'EXTERNAL_ROTATION'])
def test_absence_conflict_in_a_published_change_offers_no_cancellation(h, code):
    """A published-change draft has its own approval path (c821853af); the ordinary-draft cancel above stays."""
    from apps.api.auth import authenticate
    published = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    w = h.workspace(base=published)
    sh = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('coordinator').cookies['rota_session'])
        conflict = {'code': code, 'shift_id': sh['id'], 'person_id': h.seed['people'][0]}
        assert cancel_actions(db, actor, [conflict]) == [{**conflict, 'cancel_actions': []}]
