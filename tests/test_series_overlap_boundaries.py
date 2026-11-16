"""Timezone, historical occurrence and department-scale recurrence boundaries."""
import time
from datetime import date
from random import Random

import pytest
from sqlalchemy import event
from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from tests.test_schedule_window import _big_month
from tests.test_series_overlap_api import candidate, preview, reviewed, apply_action, absence
from tests.test_series_reconciliation import materialise


def test_series_preview_finds_overnight_peer_across_the_date_line(h):
    peer = candidate(h, 'Synthetic date-line peer', weekday=5)
    peer.update(starts_on='2026-10-03', ends_on='2026-10-04')
    peer['definition'].update(timezone='Etc/GMT+12', days=[{'weekday': 5, 'start_time': '23:00', 'end_time': '02:00'}])
    saved = reviewed(h, peer)
    value = candidate(h)
    value.update(starts_on='2026-10-05', ends_on='2026-10-06')
    value['definition'].update(timezone='Pacific/Kiritimati', days=[{'weekday': 0, 'start_time': '00:00', 'end_time': '02:00'}])
    [conflict] = preview(h, value)['conflicts']
    assert conflict['date'] == '2026-10-05' and conflict['other_occurrence_on'] == '2026-10-03'
    assert conflict['other_series_id'] == saved['id']
    assert conflict['start'] == '2026-10-04T11:00:00Z' and conflict['end'] == '2026-10-04T12:00:00Z'


def test_series_archived_occurrences_do_not_block_future_template_cancellation(h):
    peer = reviewed(h, candidate(h, 'Synthetic archived peer'))
    workspace = materialise(h, peer)
    h.command('coordinator', 'workspace.archive', {'reason': 'Archive the synthetic alternative draft'},
              workspace['id'], workspace['revision'])
    conflicts = preview(h, candidate(h))['conflicts']
    conflict = next(row for row in conflicts if row['date'] == '2026-10-05')
    action = next(action for action in conflict['cancel_actions'] if action['label'] == 'Cancel the other occurrence')
    assert action['kind'] == 'series.occurrence.cancel'
    result = apply_action(h, action)
    assert result['cancelled_occurrence_on'] == '2026-10-05'
    assert not any(row['date'] == '2026-10-05' for row in preview(h, candidate(h))['conflicts'])


def test_series_replaced_publication_days_offer_executable_template_cancellation(h):
    peer = reviewed(h, candidate(h, 'Synthetic partly replaced clinic'))
    workspace = materialise(h, peer)
    publication_id = h.publish(workspace)['publication_id']
    replacement = h.command('coordinator', 'workspace.create', {'name': 'Synthetic replacement',
        'starts_on': '2026-10-05', 'ends_on': '2026-10-11', 'replaces_publication_id': publication_id})['workspace']
    replacement = h.shift(replacement, '2026-10-05T13:00:00+03:00', '2026-10-05T17:00:00+03:00',
                          people=[h.seed['people'][1]])['workspace']
    h.publish(replacement)
    conflict = next(row for row in preview(h, candidate(h))['conflicts'] if row['date'] == '2026-10-05')
    action = next(action for action in conflict['cancel_actions'] if action['label'] == 'Cancel the other occurrence')
    assert action['kind'] == 'series.occurrence.cancel'
    result = apply_action(h, action)
    assert result['cancelled_occurrence_on'] == '2026-10-05'


@pytest.mark.parametrize('initially_absent', [False, True])
def test_series_preview_preserves_explicit_issued_closure_when_current_absence_changes(h, initially_absent):
    if initially_absent:
        leave = absence(h, day='2026-10-05')
    value = candidate(h, 'Synthetic issued clinic')
    value['definition']['close_when_all_usual_unavailable'] = True
    peer = reviewed(h, value)
    workspace = materialise(h, peer)
    shift_id = h.detail(workspace)['shifts'][0]['id']
    if initially_absent:
        workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [shift_id],
            'action': 'cancel', 'reason': 'Explicit coordinator closure'}, revision=workspace['revision'])['workspace']
    h.publish(workspace)
    if initially_absent:
        h.command('coordinator', 'availability.retract', {'reason': 'Synthetic availability changed'}, leave['id'], leave['revision'])
    else:
        absence(h, day='2026-10-05')
    conflicts = preview(h, candidate(h))['conflicts']
    issued_overlap = [row for row in conflicts if row['other_shift_id'] == shift_id]
    assert bool(issued_overlap) is not initially_absent


@pytest.fixture
def series_month(h):
    workspace = h.workspace('Synthetic series month', '2026-10-01', '2026-11-01')
    with transaction(h.app.state.engine, write=True) as db:
        extra = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Synthetic series person {n}')
                 for n in range(147)]
        db.add_all(extra)
        db.flush()
        people = list(h.seed['people']) + [row.id for row in extra]
        typ = db.get(m.AssignmentType, h.seed['type'])
        _big_month(db, workspace['id'], people, typ, h.seed['locations'], h.seed['services'],
                   date(2026, 10, 1), Random(23), '2026-09-30T00:00:00Z')
        for n in range(50):
            value = candidate(h, f'Synthetic series {n}', weekday=n % 7, service=n % 2)
            value['definition']['default_person_ids'] = [people[n]]
            typed = s.SeriesInput.model_validate(value)
            db.add(m.RecurringSeries(department_id=h.seed['department_id'],
                **typed.model_dump(exclude={'definition'}), definition=value['definition']))
    value = candidate(h)
    value['definition']['default_person_ids'] = people[:100]
    client, queries = h.client('admin'), []

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', query)
    began = time.process_time()
    try:
        result = client.post('/api/series/preview', json={'value': value})
    finally:
        elapsed = time.process_time() - began
        event.remove(h.app.state.engine, 'before_cursor_execute', query)
    assert result.status_code == 200, result.text
    return result.json(), len(queries), elapsed


def test_series_preview_scale_batches_people_series_and_sessions(series_month):
    result, queries, _ = series_month
    assert len(result['occurrences']) == 4
    peer_conflicts = [row for row in result['conflicts'] if row['other_series_id']]
    assert len(peer_conflicts) == 8 * 4  # Eight Monday series, each with four occurrences.
    assert any(row['other_workspace_id'] for row in result['conflicts'])
    assert {row['code'] for row in result['conflicts']} == {'TIME_OVERLAP'}
    assert queries < 40, queries


@pytest.mark.cpu_budget
def test_series_preview_scale_cpu_budget(series_month):
    from tests.test_scheduler_performance import _budget
    assert series_month[2] < _budget(0.3), series_month[2]


@pytest.mark.parametrize('initially_absent', [False, True])
def test_series_preview_preserves_issued_closure_when_current_absence_changes(h, initially_absent):
    if initially_absent:
        leave = absence(h, day='2026-10-05')
    value = candidate(h, 'Synthetic issued clinic')
    value['definition']['close_when_all_usual_unavailable'] = True
    peer = reviewed(h, value)
    workspace = materialise(h, peer)
    shift_id = h.detail(workspace)['shifts'][0]['id']
    h.publish(workspace)
    if initially_absent:
        h.command('coordinator', 'availability.retract', {'reason': 'Synthetic availability changed'}, leave['id'], leave['revision'])
    else:
        absence(h, day='2026-10-05')
    conflicts = preview(h, candidate(h))['conflicts']
    issued_overlap = [row for row in conflicts if row['other_shift_id'] == shift_id]
    assert bool(issued_overlap) is not initially_absent
