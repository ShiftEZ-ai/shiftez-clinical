"""Focused contracts for the bounded assignment delta scope."""
from copy import deepcopy
from types import SimpleNamespace

from apps.api.services import planning
from packages.domain import validation_scope


def snapshot(shift_ids=('s1', 's2'), people=('p1', 'p2')):
    rows = [{'id': sid, 'start': f'2026-10-0{index + 4}T08:00:00+00:00',
             'end': f'2026-10-0{index + 4}T12:00:00+00:00',
             'participants': [{'person_id': pid, 'start': f'2026-10-0{index + 4}T08:00:00+00:00',
                               'end': f'2026-10-0{index + 4}T12:00:00+00:00'}]
             } for index, (sid, pid) in enumerate(zip(shift_ids, people))]
    return {'workspace': {'starts_on': '2026-10-04'}, 'organisation': {'timezone': 'UTC'},
            'shifts': rows, 'focus_ids': list(shift_ids)}


def test_rule_map_closure_bounds_changed_sessions_people_and_dates(monkeypatch):
    data = snapshot()
    monkeypatch.setattr('packages.domain.rule_map.rule_map', lambda _, *, reach_only: {'rules': [
        {'id': 'near', 'sessions': [0], 'people': {'p1': [[0, 3]]}, 'days': [0, 1]},
        {'id': 'far', 'sessions': [1], 'people': {'p2': [[2, 3]]}, 'days': [2]},
    ]})
    before = deepcopy(data['shifts'][:1])
    after = deepcopy(before)
    after[0]['participants'][0]['person_id'] = 'p3'
    scope = validation_scope.bounded_assignment_scope(data, before, after)
    assert scope['shifts'] == {'s1'}
    assert {'p1', 'p3'} <= scope['people']
    assert scope['dates'] == {'2026-10-04', '2026-10-05'}


def test_unscoped_rule_map_fails_closed():
    data = snapshot()
    before = deepcopy(data['shifts'][:1])
    after = deepcopy(before)
    after[0]['participants'] = []
    scope = validation_scope.bounded_assignment_scope(data, before, after)
    # No policy map is available in this deliberately incomplete input.
    assert scope is None


def test_participant_only_undo_redo_snapshot_is_bounded():
    before = snapshot()
    after = deepcopy(before)
    after['shifts'][0]['participants'][0]['person_id'] = 'p9'
    assert planning._participant_only_snapshot(before, after)
    after['shifts'][0]['location_id'] = 'changed'
    assert not planning._participant_only_snapshot(before, after)


def test_untouched_scope_reuse_requires_identical_input_components(monkeypatch):
    data = snapshot()
    data['workspace'].update(ends_on='2026-10-11', rule_adjustments=[], target_overrides=[])
    data['input_manifest'] = {'components': {'mandatory_inputs': 'm', 'objective_inputs': 'o', 'toolchain': 't'}}
    before = {'workspace': deepcopy(data['workspace']), 'shifts': deepcopy(data['shifts'])}
    previous = {'_session_parts': [0], '_person_parts': {}, 'input_manifest': {'components': dict(data['input_manifest']['components'])}}
    monkeypatch.setattr(validation_scope, 'bounded_assignment_scope',
                        lambda *_: {'shifts': {'s1'}, 'people': {'p1'}, 'dates': {'2026-10-04'},
                                    'include_resources': True})
    assert planning.assignment_validation_scope(data, before, previous)['previous'] is previous
    data['input_manifest']['components']['mandatory_inputs'] = 'drift'
    assert planning.assignment_validation_scope(data, before, previous) is None
    data['input_manifest']['components']['mandatory_inputs'] = 'm'
    capacity_source = 'place_capacity:own_duties'
    data['input_manifest']['sources'] = {capacity_source: {'hash': 'changed'}}
    assert planning.assignment_validation_scope(data, before, previous) is None
    before['input_manifest'] = {'token': 'before', 'sources': {capacity_source: {'hash': 'changed'}}}
    previous['input_manifest']['token'] = 'before'
    data['input_manifest']['token'] = 'after'
    assert planning.assignment_validation_scope(data, before, previous)['previous'] is previous
    data['input_manifest']['sources'][capacity_source]['hash'] = 'unmatched'
    assert planning.assignment_validation_scope(data, before, previous) is None
    data['input_manifest']['sources'][capacity_source]['hash'] = 'changed'
    before['input_manifest']['token'] = 'unbound'
    assert planning.assignment_validation_scope(data, before, previous) is None


def test_edit_delta_keeps_revision_and_source_currency_and_falls_back_without_scope():
    before = {'workspace': {'revision': 4}, 'shifts': [{'id': 's1', 'participants': [{'person_id': 'p1'}]}]}
    changed = [{'id': 's1', 'participants': [{'person_id': 'p2'}]}]
    validation = {'findings': [], 'coverage': []}
    delta = planning.edit_delta(before, SimpleNamespace(revision=5), changed, [], {}, validation,
                                current_fingerprint='f' * 64)
    assert delta['base_revision'] == 4 and delta['revision'] == 5
    assert delta['current_fingerprint'] == 'f' * 64 and delta['stale'] is False
    assert planning.edit_delta(before, SimpleNamespace(revision=5), [], [], {}, validation,
                               current_fingerprint='f' * 64) is None
