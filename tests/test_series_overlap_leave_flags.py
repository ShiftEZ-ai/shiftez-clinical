"""Published schedule absence flags follow the existing planning disclosure boundary."""
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES, SeriesOverlapConflict
from tests.governance_support import identity
from tests.test_leave_disclosure import setting
from tests.test_leave_disclosure_scope import foreign_absence, FOREIGN_TYPE, FOREIGN_NOTE, LOCAL_NOTE
from tests.test_series_overlap_schedule import DAY, publish_synthetic_rows


PATH = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'
NOTE = 'Synthetic confidential leave detail'


def leave_values(value):
    if isinstance(value, dict):
        if 'leave' in value:
            yield value['leave']
        for child in value.values():
            yield from leave_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from leave_values(child)


def absence(h, person=None):
    return h.command('coordinator', 'availability.record', {
        'person_id': person or h.seed['people'][0], 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': DAY[0], 'end': DAY[1], 'private_reason': NOTE})['record']


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_schedule_leave_flags_obey_display_setting_for_every_role(h, mode):
    own, other = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), *DAY, people=[own, other])['workspace']
    publish_synthetic_rows(h, workspace)
    absence(h, own)
    absence(h, other)
    setting(h, mode)
    identity(h, 'programme_lead', ['programme_lead'])
    for role in ROLE_NAMES:
        allowed = role == 'staff' or role in PERMISSIONS['schedule.read_team']
        body = h.get(role, PATH, status=200 if allowed else 403)
        if not allowed:
            continue
        flags = body['shifts'][0]['double_booking_conflicts']
        assert {row['person_id'] for row in flags} == ({own} if role == 'staff' else {own, other})
        disclosed = role in PERMISSIONS['schedule.read_team'] and mode != 'none'
        for flag in flags:
            assert flag['code'] == 'UNAVAILABLE' and flag['other_shift_id'] is None
            SeriesOverlapConflict.model_validate(flag)
            if not disclosed:
                assert flag['label'] == 'Unavailable' and 'leave' not in flag
            else:
                assert flag['leave']['type'] == 'Annual'
                assert flag['label'] == ('Annual: ' + NOTE if mode == 'details' else 'Annual')
                assert flag['leave'].get('note') == (NOTE if mode == 'details' else None)
        if not disclosed or mode != 'details':
            assert NOTE not in json.dumps(body)
        values = list(leave_values(body))
        if not disclosed:
            assert not values
        elif mode == 'type':
            assert values and all(set(value) == {'type', 'starts_on', 'ends_on'} for value in values)
        else:
            assert values and all(value.get('note') == NOTE for value in values)


def test_schedule_leave_flags_refresh_after_availability_and_disclosure_changes(h):
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    publish_synthetic_rows(h, workspace)
    client = h.client('coordinator')
    first = client.get(PATH)
    assert first.status_code == 200 and not first.json()['shifts'][0].get('double_booking_conflicts')
    assert client.get(PATH, headers={'if-none-match': first.headers['etag']}).status_code == 304
    absence(h)
    changed = client.get(PATH, headers={'if-none-match': first.headers['etag']})
    assert changed.status_code == 200
    assert changed.json()['shifts'][0]['double_booking_conflicts'][0]['label'] == 'Annual'
    setting(h, 'none')
    hidden = client.get(PATH, headers={'if-none-match': changed.headers['etag']})
    assert hidden.status_code == 200
    assert hidden.json()['shifts'][0]['double_booking_conflicts'][0]['label'] == 'Unavailable'
    assert NOTE not in hidden.text


def test_schedule_foreign_leave_flag_never_borrows_an_overlapping_local_reason(h):
    workspace = h.shift(h.workspace(), '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00')['workspace']
    publish_synthetic_rows(h, workspace)
    foreign_id, _ = foreign_absence(h, 'details', 'details', local=True)
    path = '/api/schedule/sessions?start=2026-10-04&end=2026-10-05'
    for role in (*PERMISSIONS['schedule.read_team'], 'staff'):
        if role == 'programme_lead':
            identity(h, role, [role])
        body = h.get(role, path)
        flags = body['shifts'][0]['double_booking_conflicts']
        foreign = next(row for row in flags if row['availability_id'] == foreign_id)
        assert foreign['label'] == 'Unavailable' and 'leave' not in foreign
        assert FOREIGN_TYPE not in json.dumps(body) and FOREIGN_NOTE not in json.dumps(body)
        if role == 'staff':
            assert LOCAL_NOTE not in json.dumps(body)
        else:
            local = next(row for row in flags if row['availability_id'] != foreign_id)
            assert local['leave']['note'] == LOCAL_NOTE


def test_schedule_weekly_unavailability_flags_share_disclosure_and_ignore_retracted_leave(h):
    workspace = h.shift(h.workspace(), *DAY)['workspace']
    publish_synthetic_rows(h, workspace)
    row = absence(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Availability, row['id']).retracted = True
    h.command('coordinator', 'availability_pattern.propose', {'person_id': h.seed['people'][0],
        'weekdays': [0], 'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-10-01', 'note': NOTE})
    setting(h, 'details')
    flag, = h.get('coordinator', PATH)['shifts'][0]['double_booking_conflicts']
    assert flag['availability_id'].startswith('pattern:')
    assert flag['label'] == 'Weekly unavailability: ' + NOTE and flag['leave']['note'] == NOTE
    staff_flag, = h.get('staff', PATH)['shifts'][0]['double_booking_conflicts']
    assert staff_flag['label'] == 'Unavailable' and 'leave' not in staff_flag
