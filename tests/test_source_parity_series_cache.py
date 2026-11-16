"""Generation rechecks reuse series state while structural input edits still take effect."""
import copy
import json
from datetime import timedelta

import pytest

from packages.domain import recurrence, validation
from packages.domain.time import aware


def source(h):
    workspace = h.shift(h.workspace())['workspace']
    data = h.snapshot(workspace)
    shift = data['shifts'][0]
    person = h.seed['people'][0]
    shift['series_snapshot'] = {'definition': {'default_person_ids': [person], 'usual_assignees_fixed': True,
                                              'close_when_all_usual_unavailable': True}}
    return data, person


def absent(data, person):
    shift = data['shifts'][0]
    data['availability'].append({'person_id': person, 'start': shift['start'], 'end': shift['end'], 'kind': 'recorded_leave'})


def test_evaluate_participant_trials_reuses_series_state_and_person_index(h, monkeypatch):
    data, _ = source(h)
    calls = {'state': 0, 'index': 0}
    original_state, original_index = recurrence.series_state, validation.index_person_rows

    def state(*args):
        calls['state'] += 1
        return original_state(*args)

    def index(*args):
        calls['index'] += 1
        return original_index(*args)

    monkeypatch.setattr(recurrence, 'series_state', state)
    monkeypatch.setattr(validation, 'index_person_rows', index)
    applied = recurrence.with_series_state(data, cache=True)
    before = copy.deepcopy(applied)
    shift = applied['shifts'][0]
    for people in ([], shift['participants'], []):
        checked = validation.evaluate({**applied, 'shifts': [{**shift, 'participants': people}]})
        assert checked['coverage']
    assert calls == {'state': 1, 'index': 1}
    assert applied == before


def test_immutable_swap_trials_reuse_prepared_series_state_and_person_index(h, monkeypatch):
    from packages.domain.conditions import active_rules
    data, _ = source(h)
    shift = data['shifts'][0]
    shift.pop('series_snapshot')
    prepared = {**data, '_active_policies': active_rules(data['policies'], context=data),
                '_person_rows': validation.index_person_rows(data)}

    def unexpected(*args):
        raise AssertionError('Immutable swap trials must reuse prepared series state and person indexes')

    monkeypatch.setattr(validation, 'index_person_rows', unexpected)
    monkeypatch.setattr(validation, 'with_series_state', unexpected)
    for people in ([], shift['participants'], []):
        assert validation.evaluate({**prepared, 'shifts': [{**shift, 'participants': people}]})['coverage']


def test_reduced_assignment_trial_reuses_the_full_snapshot_series_state(h, monkeypatch):
    data, _ = source(h)
    first = data['shifts'][0]
    data['shifts'].append({**first, 'id': 'next-occurrence',
                           'start': (aware(first['start']) + timedelta(days=1)).isoformat(),
                           'end': (aware(first['end']) + timedelta(days=1)).isoformat(), 'participants': []})
    applied = recurrence.with_series_state(data, cache=True)

    def unexpected(*args):
        raise AssertionError('A participant-only reduced check must reuse the applied series state')

    monkeypatch.setattr(recurrence, 'series_state', unexpected)
    monkeypatch.setattr(validation, 'index_person_rows', unexpected)
    reduced = recurrence.with_series_state({**applied, 'shifts': [applied['shifts'][0]]})
    assert reduced['shifts'][0] == applied['shifts'][0]


@pytest.mark.parametrize('change', ['availability', 'rotation', 'provenance', 'timing', 'cancellation', 'flags'])
def test_structural_edits_invalidate_applied_series_state(h, change):
    data, person = source(h)
    absent(data, person)
    applied = recurrence.with_series_state(data, cache=True)
    assert not applied['shifts'][0]['closed']
    assert applied['shifts'][0]['usual_assignee_unavailability'][0]['person_id'] == person
    shift = applied['shifts'][0]
    if change == 'availability':
        row = applied['availability'][-1]
        row.update(start=shift['end'], end=(aware(shift['end']) + timedelta(hours=1)).isoformat())
    elif change == 'rotation':
        applied['availability'].clear()
        applied['rotations'].append({'person_id': person, 'department_available': False,
                                    'starts_on': str(shift['start'])[:10], 'ends_on': None})
        applied = recurrence.with_series_state(applied)
        assert not applied['shifts'][0]['closed']
        assert applied['shifts'][0]['usual_assignee_unavailability'][0]['codes'] == ['EXTERNAL_ROTATION']
        applied['rotations'][-1]['department_available'] = True
    elif change == 'provenance':
        shift['series_snapshot']['definition']['default_person_ids'] = [h.seed['people'][1]]
    elif change == 'timing':
        shift.update(start=(aware(shift['start']) + timedelta(days=1)).isoformat(),
                     end=(aware(shift['end']) + timedelta(days=1)).isoformat())
    elif change == 'cancellation':
        shift['cancelled'] = True
    else:
        shift['series_snapshot']['definition'].update(usual_assignees_fixed=False, close_when_all_usual_unavailable=False)
    refreshed = recurrence.with_series_state(applied)
    assert not refreshed['shifts'][0]['closed']
    assert refreshed['shifts'][0]['usual_assignee_unavailability'] == (
        applied['shifts'][0]['usual_assignee_unavailability'] if change == 'cancellation' else [])


def test_person_row_rekey_and_replacement_refresh_the_cached_index(h):
    data, person = source(h)
    absent(data, person)
    applied = recurrence.with_series_state(data, cache=True)
    assert not applied['shifts'][0]['closed']
    assert applied['shifts'][0]['usual_assignee_unavailability']
    applied['availability'][-1]['person_id'] = h.seed['people'][1]
    refreshed = recurrence.with_series_state(applied)
    assert not refreshed['shifts'][0]['closed']
    assert refreshed['shifts'][0]['usual_assignee_unavailability'] == []
    refreshed['availability'][-1] = {**refreshed['availability'][-1], 'person_id': person}
    assert not recurrence.with_series_state(refreshed)['shifts'][0]['closed']
    assert recurrence.with_series_state(refreshed)['shifts'][0]['usual_assignee_unavailability'][0]['person_id'] == person


@pytest.mark.parametrize('historically_closed', [False, True])
def test_published_frozen_state_stays_frozen_when_availability_changes(h, historically_closed):
    data, person = source(h)
    absent(data, person)
    applied = recurrence.with_series_state(data, cache=True)
    applied['shifts'][0]['series_state_frozen'] = True
    applied['shifts'][0]['closed'] = historically_closed
    applied['availability'].clear()
    assert recurrence.with_series_state(applied)['shifts'][0]['closed'] is historically_closed
    assert recurrence.with_series_state(applied)['shifts'][0]['usual_assignee_unavailability']


def test_default_series_state_remains_serializable_for_job_and_publication_snapshots(h):
    data, person = source(h)
    absent(data, person)
    applied = recurrence.with_series_state(data)
    assert not applied['shifts'][0]['closed']
    # The default call adds no reuse cache; the snapshot's own person index (snapshots.py, adf40abbe) is deliberate.
    assert '_series_state_cache' not in applied
    assert not json.loads(json.dumps(applied))['shifts'][0]['closed']
