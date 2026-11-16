"""Every staff-facing overlap consumer hides recurring-template labels."""
import copy
import json
from datetime import date
from types import SimpleNamespace

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import availability, swaps, timeline, timeline_commands
from packages.contracts import schemas as s
from tests.test_timeline_data import actor_for
from tests.test_timeline_layers import stored_publication


DAY = ('2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
PRIVATE_NAME = 'Synthetic confidential recurring template'


def overlapping_data(h):
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    data = h.snapshot(workspace)
    original = data['shifts'][0]
    data['shifts'] = []
    for index in range(3):
        shift = copy.deepcopy(original)
        shift['id'] = m.uid()
        shift['series_snapshot'] = {'name': f'{PRIVATE_NAME} {index}', 'definition': {}}
        for participant in shift['participants']:
            participant.update(id=m.uid(), shift_id=shift['id'])
        data['shifts'].append(shift)
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    return data


def test_timeline_staff_hides_template_labels_in_nested_conflicts_but_coordinator_keeps_them(h, monkeypatch):
    data = overlapping_data(h)
    before = copy.deepcopy(data)
    stored_publication(h, findings=timeline.evaluate(copy.deepcopy(data))['findings'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Timeline reads must use stored findings without a live check')
    monkeypatch.setattr(timeline, '_check_data', forbidden)
    monkeypatch.setattr(timeline, 'evaluate', forbidden)
    path = '/api/timeline?start=2026-10-01&end=2026-11-01&layers=conflict&person_ids=' + h.seed['people'][0]
    planned = h.get('coordinator', path)
    conflicts = planned['people'][0]['conflicts']
    assert any(row['code'] == 'TIME_OVERLAP' and row['detail'].get('derived_findings') for row in conflicts)
    assert PRIVATE_NAME in json.dumps(planned)
    staff = h.get('staff', path)
    assert any(row['code'] == 'TIME_OVERLAP' for row in staff['people'][0]['conflicts'])
    assert PRIVATE_NAME not in json.dumps(staff)
    assert data == before


def test_staff_rotation_preview_reports_only_new_overlaps_then_hides_template_labels(h, monkeypatch):
    data = overlapping_data(h)
    own = h.seed['people'][0]
    for shift in data['shifts']:
        # An absence never closes a session any more (b0fb0ad37): the legacy closure setting is informational.
        shift['series_snapshot']['definition'] = {
            'default_person_ids': [own], 'close_when_all_usual_unavailable': True}
    source = SimpleNamespace(id=m.uid(), person_id=own, starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11))
    rotation = s.RotationInput(person_id=own, cohort_id=m.uid(), service_id=h.seed['services'][0],
        department_available=False, starts_on=date(2026, 10, 11), ends_on=date(2026, 10, 18))
    data['rotations'].append({**rotation.model_dump(mode='json'), 'id': source.id,
        'department_id': h.seed['department_id'], 'starts_on': source.starts_on.isoformat(),
        'ends_on': source.ends_on.isoformat()})
    monkeypatch.setattr(timeline, '_check_data', lambda *_: copy.deepcopy(data))
    monkeypatch.setattr(timeline_commands.catalogue, 'validate_references', lambda *_: None)
    monkeypatch.setattr(availability, 'rotation_leave_conflicts', lambda *_, **__: [])

    def preview(role):
        with transaction(h.app.state.engine) as db:
            return timeline_commands._conflicts(db, actor_for(h, db, role), SimpleNamespace(action='move'), [rotation], source)
    # The sessions overlap before and after the move: the preview lists only what the move changes.
    assert not any(row['code'] == 'TIME_OVERLAP' for row in preview('coordinator'))
    # An overlap the move introduces is listed; staff never see template labels in it.
    real, calls = timeline_commands.evaluate, []

    def overlap_is_new(checked):
        result = real(checked)
        calls.append(None)
        return result if len(calls) % 2 == 0 else {**result, 'findings': [
            f for f in result['findings'] if f['code'] != 'TIME_OVERLAP']}
    monkeypatch.setattr(timeline_commands, 'evaluate', overlap_is_new)
    for role in ('coordinator', 'staff'):
        conflicts = preview(role)
        assert any(row['code'] == 'TIME_OVERLAP' for row in conflicts)
        assert (PRIVATE_NAME in json.dumps(conflicts)) is (role == 'coordinator')


def test_swap_eligibility_hides_template_labels_before_reducing_findings(h, monkeypatch):
    own, recipient = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own])['workspace']
    workspace = h.shift(workspace, *DAY, people=[recipient], service=1)['workspace']
    draft = h.detail(workspace)['shifts']
    with transaction(h.app.state.engine, write=True) as db:
        for index, shift in enumerate(draft):
            db.get(m.Shift, shift['id']).series_snapshot = {
                'name': f'{PRIVATE_NAME} {index}', 'definition': {}}
    publication_id = h.publish(workspace)['publication_id']
    first = next(participant['id'] for shift in draft for participant in shift['participants']
                 if participant['person_id'] == own)
    checked, evaluate = [], swaps.evaluate

    def full_check(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        checked.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(swaps, 'evaluate', full_check)
    with transaction(h.app.state.engine) as db:
        result = swaps.Eligibility(db, db.get(m.Publication, publication_id)).pair(first, None, recipient_id=recipient)
    assert not result['eligible'] and 'TIME_OVERLAP' in {row['code'] for row in result['findings']}
    assert PRIVATE_NAME in json.dumps(checked)
    assert PRIVATE_NAME not in json.dumps(result)


def test_staff_rotation_preview_conflicts_use_full_closure_inputs_then_hide_template_labels(h, monkeypatch):
    data = overlapping_data(h)
    own = h.seed['people'][0]
    for shift in data['shifts']:
        shift['series_snapshot']['definition'] = {
            'default_person_ids': [own], 'close_when_all_usual_unavailable': True}
    source = SimpleNamespace(id=m.uid(), person_id=own, starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11))
    rotation = s.RotationInput(person_id=own, cohort_id=m.uid(), service_id=h.seed['services'][0],
        department_available=False, starts_on=date(2026, 10, 11), ends_on=date(2026, 10, 18))
    data['rotations'].append({**rotation.model_dump(mode='json'), 'id': source.id,
        'department_id': h.seed['department_id'], 'starts_on': source.starts_on.isoformat(),
        'ends_on': source.ends_on.isoformat()})
    monkeypatch.setattr(timeline, '_check_data', lambda *_: copy.deepcopy(data))
    monkeypatch.setattr(timeline_commands.catalogue, 'validate_references', lambda *_: None)
    monkeypatch.setattr(availability, 'rotation_leave_conflicts', lambda *_, **__: [])
    for role in ('coordinator', 'staff'):
        with transaction(h.app.state.engine) as db:
            conflicts = timeline_commands._conflicts(db, actor_for(h, db, role),
                SimpleNamespace(action='move'), [rotation], source)
        assert any(row['code'] == 'TIME_OVERLAP' for row in conflicts)
        assert (PRIVATE_NAME in json.dumps(conflicts)) is (role == 'coordinator')
