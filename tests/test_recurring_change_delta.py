"""Draft replies compare derived recurrence state without resending unchanged sessions."""
import pytest

from apps.api.services import planning
from tests.test_backend_scale import scale_department
from tests.test_series_overlap_api import candidate, reviewed
from tests.test_resources_calendars import update


@pytest.mark.parametrize('absent_peer', [False, True])
def test_recurring_delta_preview_save_and_undo_leave_untouched_projection(h, monkeypatch, absent_peer):
    first = reviewed(h, candidate(h, 'Synthetic edited series'))
    value = candidate(h, 'Synthetic unchanged series')
    value['definition'].update(default_person_ids=[h.seed['people'][1]], close_when_all_usual_unavailable=True)
    peer = reviewed(h, value)
    if absent_peer:
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1],
            'kind': 'recorded_leave', 'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00',
            'private_reason': 'Synthetic unavailable usual assignee'})
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    workspace = h.command('coordinator', 'series.expand', {'workspace_id': workspace['id'],
        'series_ids': [first['id'], peer['id']]}, revision=workspace['revision'])['workspace']
    before = h.detail(workspace)['shifts']
    edited = next(row for row in before if row['series_id'] == first['id'])
    assert not any(row.get('closed') for row in before)
    counts, changed = [], planning.changed

    def observed(*args, **kwargs):
        result = changed(*args, **kwargs)
        counts.append(len(result['shifts']))
        return result

    monkeypatch.setattr(planning, 'changed', observed)
    payload = {'workspace_id': workspace['id'], 'shift_ids': [edited['id']],
               'action': 'cancel', 'reason': 'Synthetic single occurrence cancellation'}
    response = h.client('coordinator').post('/api/planner/preview', json={
        'kind': 'shift.action', 'payload': payload, 'expected_revision': workspace['revision']})
    assert response.status_code == 200, response.text
    assert counts == [1]
    assert h.detail(workspace)['shifts'] == before
    saved = h.command('coordinator', 'shift.action', payload, revision=workspace['revision'])
    assert [row['id'] for row in saved['shifts']] == [edited['id']]
    assert saved['removed_shift_ids'] == [] and saved['shifts'][0]['cancelled']
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the synthetic occurrence'},
        workspace['id'], saved['workspace']['revision'])
    assert [row['id'] for row in restored['shifts']] == [edited['id']]
    assert len(counts) >= 3 and set(counts) == {1}  # preparation may replay the same edit
    assert h.detail(workspace)['shifts'] == before


def test_recurring_delta_keeps_unchanged_peer_when_overlap_flags_clear(h):
    source = reviewed(h, candidate(h))
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    workspace = h.command('coordinator', 'series.expand', {'workspace_id': workspace['id'],
        'series_id': source['id']}, revision=workspace['revision'])['workspace']
    created = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', service=1)
    workspace = created['workspace']
    before = h.detail(workspace)['shifts']
    occurrence = next(row for row in before if row['series_id'] and row['occurrence_on'] == '2026-10-05')
    assert occurrence['double_booking_conflicts']
    saved = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [occurrence['id']], 'action': 'cancel', 'reason': 'Clear the synthetic overlap'},
        revision=workspace['revision'])
    assert {row['id'] for row in saved['shifts']} == {occurrence['id'], created['shift_id']}
    assert all(row['double_booking_conflicts'] == [] for row in saved['shifts'])
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the synthetic overlap'},
        workspace['id'], saved['workspace']['revision'])
    assert {row['id'] for row in restored['shifts']} == {occurrence['id'], created['shift_id']}
    assert all(row['double_booking_conflicts'] for row in restored['shifts'])
    assert h.detail(workspace)['shifts'] == before


def test_reconciliation_removals_return_only_removed_ids_and_undo_created_rows(h):
    source = reviewed(h, candidate(h))
    value = candidate(h, 'Synthetic preserved peer')
    value['definition']['default_person_ids'] = [h.seed['people'][1]]
    peer = reviewed(h, value)
    workspace = h.workspace(start='2026-10-01', end='2026-11-01')
    workspace = h.command('coordinator', 'series.expand', {'workspace_id': workspace['id'],
        'series_ids': [source['id'], peer['id']]}, revision=workspace['revision'])['workspace']
    before = h.detail(workspace)['shifts']
    removed = {row['id'] for row in before if row['series_id'] == source['id']}
    source = update(h, 'recurring_series', source, active=False)
    reply = h.command('coordinator', 'series.reconcile', {'workspace_id': workspace['id'],
        'series_id': source['id'], 'expected_series_revision': source['revision'], 'from_on': '2026-10-01',
        'replace_modified': True, 'participant_policy': 'preserve_offsets',
        'reason': 'Remove synthetic retired occurrences'}, revision=workspace['revision'])
    assert reply['removed_count'] == len(removed) == 4
    assert reply['shifts'] == [] and set(reply['removed_shift_ids']) == removed
    restored = h.command('coordinator', 'workspace.undo', {'reason': 'Restore the synthetic removed occurrences'},
        workspace['id'], reply['workspace']['revision'])
    assert {row['id'] for row in restored['shifts']} == removed
    assert restored['removed_shift_ids'] == []
    assert h.detail(workspace)['shifts'] == before


def test_changed_delta_keeps_old_content_when_validation_focus_narrows(h, monkeypatch):
    first = h.shift(h.workspace())
    second = h.shift(first['workspace'], '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
    from apps.api.services import published_changes
    publication_id = h.publish(second['workspace'])['publication_id']
    workspace = h.workspace(base=publication_id)
    before = h.detail(workspace)['shifts']
    first, second = ({'shift_id': row['id']} for row in before)
    scope = published_changes.validation_scope

    def narrowed(db, row, data):
        assert set(data['focus_ids']) == {first['shift_id'], second['shift_id']}
        assert scope(db, row, data)
        data['focus_ids'] = [first['shift_id']]
        return True

    monkeypatch.setattr(published_changes, 'validation_scope', narrowed)
    payload = {'workspace_id': workspace['id'], 'shift_ids': [second['shift_id']],
               'action': 'cancel', 'reason': 'Synthetic edit outside the narrowed validation focus'}
    preview = h.client('coordinator').post('/api/planner/preview', json={
        'kind': 'shift.action', 'payload': payload, 'expected_revision': workspace['revision']})
    assert preview.status_code == 200, preview.text
    assert preview.json()['impact']['counts']['changed'] == 1
    assert h.detail(workspace)['shifts'] == before
    saved = h.command('coordinator', 'shift.action', payload, revision=workspace['revision'])
    assert [row['id'] for row in saved['shifts']] == [second['shift_id']]
    assert saved['shifts'][0]['cancelled'] and saved['removed_shift_ids'] == []


def test_department_reconciliation_response_row_budget(scale_harness):
    # Exact row counts are a hardware-independent budget on every CI runner.
    h = scale_harness('h')
    _, _, _, series_ids = scale_department(h)
    workspace = h.workspace('Synthetic delta budget', '2026-11-01', '2026-11-29')
    expanded = h.command('coordinator', 'series.expand', {'workspace_id': workspace['id'],
        'series_ids': series_ids}, revision=workspace['revision'])
    before = {row['id']: row for row in expanded['shifts']}
    assert len(before) == 768
    selected = {row['id'] for row in before.values() if row['series_id'] == series_ids[0]}
    read_before = {row['id']: row for row in h.detail(workspace)['shifts']}
    reply = h.command('coordinator', 'series.reconcile', {'workspace_id': workspace['id'],
        'series_id': series_ids[0], 'expected_series_revision': 1, 'from_on': '2026-11-01',
        'replace_modified': True, 'participant_policy': 'preserve_offsets',
        'reason': 'Synthetic department reconciliation'}, revision=expanded['workspace']['revision'])
    assert len(selected) == reply['updated_count'] == len(reply['shifts']) == 28
    assert {row['id'] for row in reply['shifts']} == selected
    assert reply['removed_shift_ids'] == []
    after = {row['id']: row for row in h.detail(workspace)['shifts']}
    assert {row_id: row for row_id, row in after.items() if row_id not in selected} == {
        row_id: row for row_id, row in read_before.items() if row_id not in selected}
