"""Combined recurrence checks retain saved peer duties without counting templates twice."""
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from tests.test_series_overlap_api import REASON, state, value_of
from tests.test_series_overlap_combined import bulk_payload, combined, pair, series_harness, stored
from tests.test_series_reconciliation import edit, materialise


def saved_duties(h, workspace):
    with transaction(h.app.state.engine) as db:
        shifts = [dict(row) for row in db.execute(select(m.Shift.__table__).where(
            m.Shift.workspace_id == workspace['id']).order_by(m.Shift.id)).mappings()]
        assignments = [dict(row) for row in db.execute(select(m.Assignment.__table__).where(
            m.Assignment.shift_id.in_([row['id'] for row in shifts])).order_by(m.Assignment.id)).mappings()]
    return shifts, assignments


@pytest.mark.parametrize('peer_state', ['materialised', 'reassigned', 'moved', 'cancelled'])
def test_combined_preview_and_save_retain_saved_peer_duties(series_harness, peer_state):
    h = series_harness
    rows = pair(h)
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    for row in rows:
        workspace = materialise(h, row, workspace)
    shifts = h.detail(workspace)['shifts']
    peer_shifts = [row for row in shifts if row['series_id'] == rows[1]['id']]
    first = next(row for row in peer_shifts if row['occurrence_on'] == '2026-10-05')
    if peer_state == 'reassigned':
        workspace = edit(h, workspace, first, participants=[{'person_id': h.seed['people'][2],
            'start': first['start'], 'end': first['end']}])
    elif peer_state == 'moved':
        workspace = edit(h, workspace, first, start='2026-10-06T08:00:00+03:00',
            end='2026-10-06T12:00:00+03:00', participants=[{'person_id': h.seed['people'][1],
                'start': '2026-10-06T08:00:00+03:00', 'end': '2026-10-06T12:00:00+03:00'}])
    elif peer_state == 'cancelled':
        workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
            'shift_ids': [first['id']], 'action': 'cancel', 'reason': REASON},
            revision=workspace['revision'])['workspace']
    values = [value_of(row) for row in rows]
    person_id = h.seed['people'][2 if peer_state == 'reassigned' else 1]
    values[0]['definition']['default_person_ids'] = [person_id]
    values[1]['name'] += ' timing-neutral rename'
    if peer_state == 'materialised':
        values[1]['definition']['default_person_ids'] = [h.seed['people'][2]]
    expected_ids = {row['id'] for row in peer_shifts}
    if peer_state == 'reassigned':
        expected_ids = {first['id']}
    elif peer_state in {'moved', 'cancelled'}:
        expected_ids.remove(first['id'])
    before, original, duties = state(h), stored(h, rows), saved_duties(h, workspace)
    checked = combined(h, rows, values)
    conflicts = checked['items'][0]['conflicts']
    assert len(conflicts) == len(expected_ids)
    assert {row['other_shift_id'] for row in conflicts} == expected_ids
    assert all(row['code'] == 'TIME_OVERLAP' and row['person_id'] == person_id
               and row['other_series_id'] == rows[1]['id'] for row in conflicts)
    assert checked['items'][1]['conflicts'] == []
    assert not {row['other_shift_id'] for item in checked['items'] for row in item['conflicts']} & {
        row['id'] for row in shifts if row['series_id'] == rows[0]['id']}
    assert state(h) == before and saved_duties(h, workspace) == duties
    refused = h.command('admin', 'bulk.apply', bulk_payload(rows, values), status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT' and refused['details'] == checked
    assert state(h) == before and stored(h, rows) == original and saved_duties(h, workspace) == duties
    applied = h.command('admin', 'bulk.apply', bulk_payload(rows, values, checked['conflict_token']))
    assert applied['counts']['changed'] == 2
    assert saved_duties(h, workspace) == duties
    after = stored(h, rows)
    assert [value_of(after[row['id']]) for row in rows] == values
    with transaction(h.app.state.engine) as db:
        for row, item in zip(rows, checked['items'], strict=True):
            version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'],
                m.DefinitionHistory.object_revision == after[row['id']]['revision']))
            assert [s.SeriesOverlapConflict.model_validate(conflict)
                    for conflict in version.snapshot['overlap_conflicts']] == [
                s.SeriesOverlapConflict.model_validate(conflict) for conflict in item['conflicts']]
            assert version.snapshot['overlap_conflict_token'] == checked['conflict_token']
            assert version.snapshot['overlap_acknowledged'] is bool(item['conflicts'])


@pytest.mark.parametrize('cancelled', [False, True])
def test_preview_does_not_resurrect_saved_peer_moved_outside_window(series_harness, cancelled):
    h = series_harness
    rows = pair(h)
    workspace = materialise(h, rows[1], h.workspace(start='2026-10-01', end='2026-11-01'))
    first = next(row for row in h.detail(workspace)['shifts'] if row['occurrence_on'] == '2026-10-05')
    workspace = edit(h, workspace, first, start='2026-10-08T08:00:00+03:00',
        end='2026-10-08T12:00:00+03:00', participants=[{'person_id': h.seed['people'][1],
            'start': '2026-10-08T08:00:00+03:00', 'end': '2026-10-08T12:00:00+03:00'}])
    if cancelled:
        h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
            'shift_ids': [first['id']], 'action': 'cancel', 'reason': REASON},
            revision=workspace['revision'])
    value = value_of(rows[0])
    value.update(starts_on='2026-10-05', ends_on='2026-10-06')
    value['definition']['default_person_ids'] = [h.seed['people'][1]]
    before = saved_duties(h, workspace)
    checked = combined(h, rows[:1], [value])
    assert len(checked['items'][0]['occurrences']) == 1
    assert checked['items'][0]['conflicts'] == []
    assert saved_duties(h, workspace) == before


@pytest.mark.parametrize('effective_until,conflicts', [('2026-10-09', 0), ('2026-10-08', 1)])
def test_moved_published_lineage_uses_actual_start_effective_boundary(series_harness, effective_until, conflicts):
    from tests.test_series_overlap_publications import imported_publication
    h = series_harness
    rows = pair(h)
    workspace = materialise(h, rows[1], h.workspace(start='2026-10-01', end='2026-11-01'))
    first = next(row for row in h.detail(workspace)['shifts'] if row['occurrence_on'] == '2026-10-05')
    workspace = edit(h, workspace, first, start='2026-10-08T08:00:00+03:00',
        end='2026-10-08T12:00:00+03:00', participants=[{'person_id': h.seed['people'][1],
            'start': '2026-10-08T08:00:00+03:00', 'end': '2026-10-08T12:00:00+03:00'}])
    publication = imported_publication(h, workspace)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Publication, publication).effective_until = date.fromisoformat(effective_until)
    value = value_of(rows[0])
    value.update(starts_on='2026-10-05', ends_on='2026-10-06')
    value['definition']['default_person_ids'] = [h.seed['people'][1]]
    before = saved_duties(h, workspace)
    checked = combined(h, rows[:1], [value])
    assert len(checked['items'][0]['conflicts']) == conflicts
    assert saved_duties(h, workspace) == before


def test_preview_bounds_sql_for_one_thousand_relevant_publications(h):
    from tests.test_series_overlap_publications import imported_publication
    rows = pair(h)
    workspace = materialise(h, rows[1], h.workspace(start='2026-10-01', end='2026-11-01'))
    publication = imported_publication(h, workspace)
    with transaction(h.app.state.engine, write=True) as db:
        source = db.get(m.Workspace, workspace['id'])
        pub = db.get(m.Publication, publication)
        shift = db.scalar(select(m.Shift).where(m.Shift.workspace_id == source.id))
        workspaces, publications, shifts = [], [], []
        for index in range(999):
            workspace_id = str(uuid4())
            start = date(2020, 1, 1) + timedelta(days=index)
            workspaces.append({**{col.name: getattr(source, col.name) for col in m.Workspace.__table__.columns},
                'id': workspace_id, 'status': 'draft', 'starts_on': start, 'ends_on': start + timedelta(days=1)})
            publications.append({**{col.name: getattr(pub, col.name) for col in m.Publication.__table__.columns},
                'id': str(uuid4()), 'workspace_id': workspace_id, 'sequence': pub.sequence + index + 1,
                'snapshot': {}, 'starts_on': start, 'ends_on': start + timedelta(days=1)})
            shifts.append({**{col.name: getattr(shift, col.name) for col in m.Shift.__table__.columns},
                'id': str(uuid4()), 'workspace_id': workspace_id, 'series_id': None,
                'series_revision': None, 'series_snapshot': None, 'occurrence_on': None,
                'series_modified': False, 'template_skipped': False})
        db.execute(insert(m.Workspace), workspaces)
        db.execute(insert(m.Shift), shifts)
        db.execute(update(m.Workspace).where(m.Workspace.id.in_([row['id'] for row in workspaces])).values(status='published'))
        db.execute(insert(m.Publication), publications)
    value = value_of(rows[0])
    value['definition']['default_person_ids'] = [h.seed['people'][1]]
    checked = combined(h, rows[:1], [value])
    assert len(checked['items'][0]['conflicts']) == 4
