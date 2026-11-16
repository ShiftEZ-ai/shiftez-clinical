"""HTTP recurrence conflicts, exact acknowledgement, occurrence commands and evidence."""
import copy
import json
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_alpha_calendar import programme
from tests.test_series_reconciliation import materialise


REASON = 'Review every synthetic recurring conflict'
PRIVATE = 'Confidential synthetic reason never disclosed in recurrence evidence'
MONDAYS = {'2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26'}


def candidate(h, name='Synthetic recurring clinic', weekday=0, service=0):
    return s.SeriesInput(name=name, starts_on='2026-10-01', ends_on='2026-11-01',
        assignment_type_id=h.seed['type'], location_id=h.seed['locations'][0], definition={
            'anchor': '2026-10-04', 'days': [{'weekday': weekday, 'start_time': '08:00', 'end_time': '12:00'}],
            'service_ids': [h.seed['services'][service]], 'default_person_ids': [h.seed['people'][0]],
            'usual_assignees_fixed': True}).model_dump(mode='json')


def value_of(row):
    return s.SeriesInput.model_validate({key: row[key] for key in s.SeriesInput.model_fields}).model_dump(mode='json')


def preview(h, value, row=None, role='admin', status=200):
    response = h.client(role).post('/api/series/preview', json={'value': value,
        **({'series_id': row['id'], 'expected_revision': row['revision']} if row else {})})
    assert response.status_code == status, response.text
    return response.json()


def save(h, value, row=None, token=None, role='admin', status=200):
    return h.command(role, 'catalogue.update' if row else 'catalogue.create', {
        'catalogue': 'recurring_series', 'value': value, 'reason': REASON,
        **({'overlap_acknowledgement': token} if token is not None else {})},
        row['id'] if row else None, row['revision'] if row else None, status=status)


def reviewed(h, value):
    return save(h, value, token=preview(h, value)['conflict_token'])['record']


def apply_action(h, action, role='admin', status=200):
    return h.command(role, action['kind'], action['payload'], action.get('target_id'),
        action.get('expected_revision'), status=status)


def absence(h, day='2026-10-12', kind='recorded_leave'):
    return h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': kind,
        'start': day + 'T08:00:00+03:00', 'end': day + 'T12:00:00+03:00', 'private_reason': PRIVATE})['record']


def state(h):
    with transaction(h.app.state.engine) as db:
        return [db.scalar(select(func.count()).select_from(cls)) for cls in (
            m.RecurringSeries, m.Shift, m.Assignment, m.DefinitionHistory, m.AuditEvent, m.CommandReceipt)]


def test_unsaved_and_saved_previews_list_every_series_oneoff_and_leave_without_writes(h):
    peer = reviewed(h, candidate(h, 'Synthetic peer clinic', service=1))
    oneoff = h.shift(h.workspace(), '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['shift_id']
    leave = absence(h)
    before = state(h)
    result = preview(h, candidate(h))
    assert state(h) == before
    assert len(result['occurrences']) == 4 and len(result['conflicts']) == 6
    peer_conflicts = [row for row in result['conflicts'] if row['other_series_id'] == peer['id']]
    assert {row['date'] for row in peer_conflicts} == MONDAYS
    assert all(row['other_session_name'] == peer['name'] and row['other_clinic_ids'] == [h.seed['services'][1]]
               and row['other_clinic_names'] for row in peer_conflicts)
    assert len([row for row in result['conflicts'] if row['other_shift_id'] == oneoff]) == 1
    [unavailable] = [row for row in result['conflicts'] if row['code'] == 'UNAVAILABLE']
    assert unavailable['date'] == '2026-10-12' and unavailable['availability_id'] == leave['id']
    assert all(row['person_id'] == h.seed['people'][0] and row['person_name'] != row['person_id']
               and row['start'] < row['end'] for row in result['conflicts'])
    assert PRIVATE not in json.dumps(result)
    saved = h.get('admin', '/api/series/' + peer['id'] + '/preview')
    assert len(saved['conflicts']) == 2 and state(h) == before
    assert preview(h, value_of(peer), peer) == saved


def test_create_requires_exact_acknowledgement_and_audits_every_conflict_without_private_reasons(h):
    reviewed(h, candidate(h, 'Synthetic peer clinic'))
    absence(h)
    value = candidate(h, service=1)
    checked = preview(h, value)
    before = state(h)
    refused = save(h, value, status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    assert len(refused['details']['conflicts']) == 5 and state(h) == before
    result = save(h, value, token=checked['conflict_token'])
    assert result['overlap_conflicts'] == result['conflicts']
    assert not result.get('rotation_leave_conflicts') and not result.get('warnings')
    s.CatalogueConflictDetails.model_validate({key: result[key] for key in
        ('overlap_conflicts', 'conflicts', 'conflict_token')})
    row = result['record']
    versions = h.get('admin', f"/api/catalogues/recurring_series/{row['id']}/history")['versions']
    [version] = versions
    assert version['snapshot']['overlap_acknowledged']
    assert version['snapshot']['overlap_conflict_token'] == checked['conflict_token']
    for stored, shown in zip(version['snapshot']['overlap_conflicts'], result['conflicts'], strict=True):
        assert stored == {**{key: value for key, value in shown.items() if key not in {'label', 'leave'}},
                          **({'label': 'Unavailable'} if shown['code'] != 'TIME_OVERLAP' else {})}
    assert len(version['snapshot']['overlap_conflicts']) == 5
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == row['id'],
            m.AuditEvent.action == 'catalogue.create'))
        receipt = db.get(m.CommandReceipt, result['receipt_id'])
        assert audit.detail['receipt_id'] == receipt.id
        assert receipt.result['conflicts'] == version['snapshot']['overlap_conflicts']
    assert PRIVATE not in json.dumps([versions, result, refused])


def test_changed_conflicts_invalidate_a_create_acknowledgement(h):
    reviewed(h, candidate(h, 'Synthetic peer clinic'))
    value = candidate(h, service=1)
    checked = preview(h, value)
    absence(h)
    refused = save(h, value, token=checked['conflict_token'], status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    assert refused['details']['conflict_token'] != checked['conflict_token']
    assert len(refused['details']['conflicts']) == 5
    assert save(h, value, token=refused['details']['conflict_token'])['record']['name'] == value['name']


def test_preview_reads_compact_sessions_but_preserves_the_issued_duty_name(h, monkeypatch):
    from apps.api import snapshots
    from tests.test_resources_calendars import update
    workspace = h.shift(h.workspace(), '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    session, = h.detail(workspace)['shifts']
    duty = next(row for row in h.get('admin', '/api/catalogues')['assignment_types'] if row['id'] == h.seed['type'])
    update(h, 'assignment_types', duty, name='Renamed current duty')
    original = snapshots.record

    def without_full_session(row, *args, **kwargs):
        assert not isinstance(row, m.Shift), 'Preview must not copy unused session definitions and attributes'
        return original(row, *args, **kwargs)

    monkeypatch.setattr(snapshots, 'record', without_full_session)
    conflict, = preview(h, candidate(h))['conflicts']
    assert conflict['other_shift_id'] == session['id']
    assert conflict['other_session_name'].startswith(session['type_snapshot']['name'])
    assert 'Renamed current duty' not in conflict['other_session_name']


def test_edit_acknowledgement_binds_exact_proposed_values_and_revision(h):
    source = reviewed(h, candidate(h, 'Synthetic original clinic', weekday=1))
    peer = reviewed(h, candidate(h, 'Synthetic Monday peer'))
    value = value_of(source)
    value['definition']['days'][0]['weekday'] = 0
    assert save(h, value, source, status=409)['error']['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    checked = preview(h, value, source)
    assert {row['other_series_id'] for row in checked['conflicts']} == {peer['id']}
    altered = copy.deepcopy(value)
    altered['definition']['days'][0]['start_time'] = '09:00'
    refused = save(h, altered, source, checked['conflict_token'], status=409)['error']
    assert refused['code'] == 'SERIES_OVERLAP_ACKNOWLEDGEMENT'
    updated = save(h, altered, source, refused['details']['conflict_token'])['record']
    assert updated['revision'] == source['revision'] + 1
    assert preview(h, altered, source, status=409)['error']['code'] == 'STALE_REVISION'
    assert h.get('admin', '/api/series/' + updated['id'] + '/preview')['conflicts']


def test_catalogue_impact_preview_returns_same_conflicts_and_rolls_back(h):
    reviewed(h, candidate(h, 'Synthetic peer clinic'))
    value = candidate(h, service=1)
    direct = preview(h, value)
    before = state(h)
    response = h.client('admin').post('/api/catalogue/preview', json={'kind': 'catalogue.create',
        'payload': {'catalogue': 'recurring_series', 'value': value, 'reason': REASON}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result['committed'] and result['inventory_only']
    assert [s.SeriesOverlapConflict.model_validate(row).model_dump(mode='json') for row in result['conflicts']] == direct['conflicts']
    assert result['conflict_token'] == direct['conflict_token']
    assert state(h) == before
    applied = h.client('admin').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        'kind': 'catalogue.create', 'expected_fingerprint': result['source_fingerprint'], 'payload': {
            'catalogue': 'recurring_series', 'value': value, 'reason': REASON,
            'overlap_acknowledgement': result['conflict_token']}})
    assert applied.status_code == 200, applied.text
    assert applied.json()['record']['name'] == value['name']


@pytest.mark.parametrize('kind', ['recorded_leave', 'external_commitment', 'protected_time', 'off_duty'])
def test_time_away_uses_same_conflict_contract_and_retraction_clears_it(h, kind):
    away = absence(h, kind=kind)
    value = candidate(h)
    [conflict] = preview(h, value)['conflicts']
    assert conflict['code'] == 'UNAVAILABLE' and conflict['date'] == '2026-10-12'
    assert conflict['label']
    assert conflict['availability_id'] == away['id'] and PRIVATE not in json.dumps(conflict)
    h.command('coordinator', 'availability.retract', {'reason': 'Retract synthetic time away'}, away['id'], away['revision'])
    assert preview(h, value)['conflicts'] == []


def test_external_rotation_is_returned_as_structured_time_away(h):
    _, _, cohort = programme(h)
    rotation = h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
        'service_id': h.seed['services'][0], 'department_available': False,
        'starts_on': '2026-10-12', 'ends_on': '2026-10-13'}, role='coordinator')['record']
    [conflict] = preview(h, candidate(h))['conflicts']
    assert conflict['code'] == 'EXTERNAL_ROTATION' and conflict['date'] == '2026-10-12'
    assert conflict['label'] == 'Unavailable'
    assert conflict['availability_id'] == rotation['id'] and conflict['other_shift_id'] is None


def test_virtual_cancel_is_one_date_with_audited_undo_and_both_resolution_choices(h):
    first = reviewed(h, candidate(h, 'Synthetic first clinic'))
    second = reviewed(h, candidate(h, 'Synthetic second clinic', service=1))
    checked = h.get('admin', '/api/series/' + first['id'] + '/preview')
    conflict = next(row for row in checked['conflicts'] if row['date'] == '2026-10-05')
    actions = {row['side']: row for row in conflict['cancel_actions']}
    assert set(actions) == {'this', 'other'}
    assert actions['this']['label'] == 'Cancel this occurrence' and actions['this']['target_id'] == first['id']
    action = actions['other']
    assert action['label'] == 'Cancel the other occurrence'
    assert action['target_id'] == second['id'] and action['payload']['occurrence_on'] == '2026-10-05'
    cancelled = apply_action(h, action)
    assert cancelled['record']['definition']['excluded_dates'] == ['2026-10-05']
    assert cancelled['cancelled_occurrence_on'] == '2026-10-05'
    remaining = h.get('admin', '/api/series/' + first['id'] + '/preview')['conflicts']
    assert {row['date'] for row in remaining} == MONDAYS - {'2026-10-05'}
    restored = apply_action(h, cancelled['undo_command'])
    assert restored['record']['definition']['excluded_dates'] == []
    assert {row['date'] for row in h.get('admin', '/api/series/' + first['id'] + '/preview')['conflicts']} == MONDAYS
    versions = h.get('admin', f"/api/catalogues/recurring_series/{second['id']}/history")['versions']
    assert len(versions) == 3 and versions[1]['snapshot']['resolved_occurrence'] == '2026-10-05'
    assert versions[2]['snapshot']['overlap_acknowledged']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.action == 'series.occurrence.cancel',
            m.AuditEvent.target_id == second['id'])) is not None
    assert apply_action(h, action, status=409)['error']['code'] == 'STALE_REVISION'


def test_saved_occurrence_actions_use_draft_cancel_and_do_not_recreate_cancelled_conflicts(h):
    source = reviewed(h, candidate(h, 'Synthetic materialised clinic'))
    workspace = materialise(h, source, h.workspace(start='2026-10-01', end='2026-11-01'))
    first = next(row for row in h.detail(workspace)['shifts'] if row['occurrence_on'] == '2026-10-05')
    created = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00', service=1)
    workspace = created['workspace']
    [conflict] = h.get('admin', '/api/series/' + source['id'] + '/preview')['conflicts']
    actions = {row['side']: row for row in conflict['cancel_actions']}
    assert set(actions) == {'this', 'other'}
    action = actions['this']
    assert action['kind'] == 'shift.action' and action['payload']['shift_ids'] == [first['id']]
    assert actions['other']['payload']['shift_ids'] == [created['shift_id']]
    cancelled = apply_action(h, action, role='coordinator')
    assert sum(row['cancelled'] for row in h.detail(workspace)['shifts']) == 1
    assert h.get('admin', '/api/series/' + source['id'] + '/preview')['conflicts'] == []
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore synthetic occurrence'},
              workspace['id'], cancelled['workspace']['revision'])
    assert len(h.get('admin', '/api/series/' + source['id'] + '/preview')['conflicts']) == 1
    refused = h.command('admin', 'series.occurrence.cancel', {'occurrence_on': '2026-10-05', 'reason': REASON},
        source['id'], source['revision'], status=409)['error']
    assert refused['code'] == 'OCCURRENCE_MATERIALISED'


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_preview_save_and_virtual_cancel_preserve_existing_role_authority(h, role):
    source = reviewed(h, candidate(h))
    username = 'overlap_' + role
    identity(h, username, [role], h.seed['people'][0])
    allowed_preview = role in {'admin', 'coordinator', 'approver'}
    result = preview(h, candidate(h, 'Synthetic another clinic'), role=username,
                     status=200 if allowed_preview else 403)
    h.get(username, '/api/series/' + source['id'] + '/preview', status=200 if allowed_preview else 403)
    save(h, candidate(h, 'Synthetic another clinic'), token=result.get('conflict_token'),
         role=username, status=200 if role == 'admin' else 403)
    h.command(username, 'series.occurrence.cancel', {'occurrence_on': '2026-10-05', 'reason': REASON},
              source['id'], source['revision'], status=200 if role == 'admin' else 403)


def test_foreign_series_and_catalogue_references_are_not_addressable(h):
    source = reviewed(h, candidate(h, 'Synthetic foreign clinic'))
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Synthetic foreign department')
        db.add(other)
        db.flush()
        db.get(m.RecurringSeries, source['id']).department_id = other.id
        foreign = m.Service(department_id=other.id, name='Synthetic foreign service',
                            starts_on=date.fromisoformat(source['starts_on']))
        db.add(foreign)
        db.flush()
        foreign_service_id = foreign.id
    h.get('admin', '/api/series/' + source['id'] + '/preview', status=404)
    preview(h, value_of(source), source, status=404)
    h.command('admin', 'series.occurrence.cancel', {'occurrence_on': '2026-10-05', 'reason': REASON},
              source['id'], source['revision'], status=404)
    value = candidate(h)
    value['definition']['service_ids'] = [foreign_service_id]
    assert preview(h, value, status=404)['error']['message'] == 'Record not found'
