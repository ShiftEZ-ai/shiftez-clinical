"""Server wording on every editable overlap projection, without private stored evidence."""
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_alpha_calendar import programme
from tests.test_leave_disclosure import setting
from tests.test_leave_disclosure_scope import foreign_absence, FOREIGN_TYPE, FOREIGN_NOTE, LOCAL_NOTE
from tests.test_series_overlap_api import candidate, preview, reviewed, save, state, REASON
from tests.test_series_overlap_leave_flags import absence, NOTE
from tests.test_series_overlap_schedule import DAY


def away_flags(body):
    if isinstance(body, list):
        return [flag for row in body for flag in away_flags(row)]
    if not isinstance(body, dict):
        return []
    return ([body] if body.get('code') in {'UNAVAILABLE', 'EXTERNAL_ROTATION'}
            and body.get('availability_id') else []) + [flag for row in body.values() for flag in away_flags(row)]


def expect_label(body, label, note=None):
    flags = away_flags(body)
    assert flags, body
    assert all(flag['label'] == label and (flag.get('leave') or {}).get('note') == note for flag in flags)
    if note is None:
        assert NOTE not in json.dumps(flags)


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_draft_calendar_planner_and_both_series_previews_label_leave_for_effective_roles(h, mode):
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    value = candidate(h)
    series = reviewed(h, value)
    absence(h)
    setting(h, mode)
    identity(h, 'programme_lead', ['programme_lead'])
    for role in s.ROLE_NAMES:
        roles = set(h.get(role, '/api/me')['roles'])
        planning = bool(roles.intersection(s.TEAM_READER_ROLES))
        can_preview = bool(roles.intersection({'admin', 'coordinator', 'approver'}))
        label = {'none': 'Unavailable', 'type': 'Annual', 'details': 'Annual: ' + NOTE}[mode]
        note = NOTE if mode == 'details' else None
        planner = h.get(role, '/api/workspaces/' + workspace['id'], status=200 if planning else 403)
        calendar = h.get(role, '/api/calendar?start=2026-10-05&end=2026-10-06&workspace_id=' + workspace['id'],
                         status=200 if planning else 403)
        before = state(h)
        direct = preview(h, value, role=role, status=200 if can_preview else 403)
        saved = h.get(role, '/api/series/' + series['id'] + '/preview', status=200 if can_preview else 403)
        assert state(h) == before
        if planning:
            expect_label(planner['shifts'], label, note)
            # A programme lead sees only sessions in the programmes they lead.
            if calendar['sessions']:
                expect_label(calendar['sessions'], label, note)
        if can_preview:
            expect_label(direct['conflicts'], label, note)
            expect_label(saved['conflicts'], label, note)


def test_preview_409_impact_save_and_replay_disclose_current_wording_but_store_generic_evidence(h):
    away = absence(h)
    value = candidate(h)
    setting(h, 'none')
    hidden = preview(h, value)
    expect_label(hidden, 'Unavailable')
    setting(h, 'details')
    before = state(h)
    detailed = preview(h, value)
    expect_label(detailed, 'Annual: ' + NOTE, NOTE)
    assert detailed['conflict_token'] == hidden['conflict_token'] and state(h) == before
    changed_note = 'An updated synthetic private note'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Availability, away['id']).private_reason = changed_note
    current = preview(h, value)
    assert current['conflict_token'] == hidden['conflict_token']
    expect_label(current, 'Annual: ' + changed_note, changed_note)
    before = state(h)
    refusal = save(h, value, status=409)['error']
    expect_label(refusal['details'], 'Annual: ' + changed_note, changed_note)
    impact = h.client('admin').post('/api/catalogue/preview', json={'kind': 'catalogue.create', 'payload': {
        'catalogue': 'recurring_series', 'value': value, 'reason': REASON}})
    assert impact.status_code == 200, impact.text
    expect_label(impact.json()['conflicts'], 'Annual: ' + changed_note, changed_note)
    assert impact.json()['conflict_token'] == hidden['conflict_token'] and state(h) == before
    key = str(uuid.uuid4())
    payload = {'catalogue': 'recurring_series', 'value': value, 'reason': REASON,
               'overlap_acknowledgement': hidden['conflict_token']}
    result = h.command('admin', 'catalogue.create', payload, key=key)
    expect_label(result['conflicts'], 'Annual: ' + changed_note, changed_note)
    setting(h, 'none')
    expect_label(h.command('admin', 'catalogue.create', payload, key=key)['conflicts'], 'Unavailable')
    expect_label(h.get('admin', '/api/commands/' + key)['result']['conflicts'], 'Unavailable')
    with transaction(h.app.state.engine) as db:
        history = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == result['record']['id']))
        receipt = db.get(m.CommandReceipt, result['receipt_id'])
        for body in (history.snapshot['overlap_conflicts'], receipt.result['conflicts']):
            expect_label(body, 'Unavailable')
            assert changed_note not in json.dumps(body) and all(not flag.get('leave') for flag in away_flags(body))


def test_changed_session_reply_and_receipt_keep_labels_out_of_stored_checks(h):
    absence(h)
    setting(h, 'details')
    result = h.shift(h.workspace(), *DAY)
    expect_label(result['shifts'], 'Annual: ' + NOTE, NOTE)
    with transaction(h.app.state.engine) as db:
        receipt = db.get(m.CommandReceipt, result['receipt_id'])
        expect_label(receipt.result['shifts'], 'Unavailable')
        stored = db.get(m.Workspace, result['workspace']['id']).validation
        assert NOTE not in json.dumps(stored)
        assert all(not flag.get('leave') for flag in away_flags(receipt.result))


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_bulk_preview_session_flags_use_disclosure_wording_without_saving(h, mode):
    from tests.test_bulk import preview as bulk_preview
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    shift, = h.detail(workspace)['shifts']
    absence(h)
    setting(h, mode)
    payload = {'item_kind': 'shift.update', 'items': [{'expected_revision': workspace['revision'], 'payload': {
        'workspace_id': workspace['id'], 'shift_id': shift['id'], 'assignment_type_id': h.seed['type'],
        'location_id': h.seed['locations'][1], 'service_ids': [h.seed['services'][0]],
        'start': DAY[0], 'end': DAY[1],
        'participants': [{'person_id': h.seed['people'][0], 'start': DAY[0], 'end': DAY[1]}]}}],
        'reason': REASON}
    before = state(h)
    original = h.detail(workspace)['shifts']
    result = bulk_preview(h, 'coordinator', payload)
    assert result['counts']['ready'] == 1, json.dumps(result['items'])
    expect_label(result['items'][0]['results'],
                 {'none': 'Unavailable', 'type': 'Annual', 'details': 'Annual: ' + NOTE}[mode],
                 NOTE if mode == 'details' else None)
    assert state(h) == before and h.detail(workspace)['shifts'] == original


def test_foreign_clash_labels_do_not_borrow_an_overlapping_local_leave_reason(h):
    workspace = h.shift(h.workspace())['workspace']
    foreign, _ = foreign_absence(h, 'details', 'details', local=True)
    value = candidate(h, weekday=6)
    bodies = [preview(h, value)['conflicts'], h.detail(workspace)['shifts'],
              h.get('coordinator', '/api/calendar?start=2026-10-04&end=2026-10-05&workspace_id=' + workspace['id'])['sessions']]
    for body in bodies:
        flags = away_flags(body)
        foreign_flag, = [flag for flag in flags if flag['availability_id'] == foreign]
        assert foreign_flag['label'] == 'Unavailable' and not foreign_flag.get('leave')
        local, = [flag for flag in flags if flag['availability_id'] != foreign]
        assert local['leave']['note'] == LOCAL_NOTE and local['label'].endswith(': ' + LOCAL_NOTE)
        assert FOREIGN_TYPE not in json.dumps(body) and FOREIGN_NOTE not in json.dumps(body)


def test_weekly_and_external_rotation_flags_always_have_server_labels(h):
    h.command('coordinator', 'availability_pattern.propose', {'person_id': h.seed['people'][0],
        'weekdays': [0], 'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-10-01', 'note': NOTE})
    _, _, cohort = programme(h)
    rotation = h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
        'service_id': h.seed['services'][0], 'department_available': False,
        'starts_on': '2026-10-05', 'ends_on': '2026-10-06'}, role='coordinator')['record']
    workspace = h.shift(h.workspace(), *DAY, seed_restricted=True)['workspace']
    for mode in ('details', 'none'):
        setting(h, mode)
        bodies = [preview(h, candidate(h))['conflicts'], h.detail(workspace)['shifts'],
                  h.get('coordinator', '/api/calendar?start=2026-10-05&end=2026-10-06&workspace_id=' + workspace['id'])['sessions']]
        for body in bodies:
            flags = away_flags(body)
            weekly = [flag for flag in flags if flag['availability_id'].startswith('pattern:')]
            expect_label(weekly, 'Weekly unavailability: ' + NOTE if mode == 'details' else 'Unavailable',
                         NOTE if mode == 'details' else None)
            external, = [flag for flag in flags if flag['availability_id'] == rotation['id']]
            assert external['code'] == 'EXTERNAL_ROTATION' and external['label'] == 'Unavailable'
            assert not external.get('leave')
