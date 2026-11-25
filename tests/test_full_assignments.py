"""Whole duty booking is enforced at both command and publication boundaries."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from packages.domain.validation import evaluate
from packages.domain.time import configured_duration_matches, duration_hours
from .test_participation_segments import at


def test_whole_assignment_and_configured_shorter_duty(h):
    typ = h.configure('assignment_types', {'name': 'Eight hour duty', 'starts_on': '2020-01-01',
        'definition': {'default_timing': {'start_time': '08:00', 'end_time': '16:00'}, 'session_units': 2}})['record']
    w = h.workspace()
    partial = [{'person_id': h.seed['people'][0], 'start': at('08:00'), 'end': at('12:00')}]
    assert 'FULL_ASSIGNMENT_REQUIRED' in str(h.shift(w, end=at('16:00'), people=partial, typ=typ['id'], status=422))
    assert 'CONFIGURED_DURATION_REQUIRED' in str(h.shift(w, typ=typ['id'], status=422))
    result = h.shift(w, end=at('16:00'), typ=typ['id'])
    snapshot = h.snapshot(result['workspace'])
    snapshot['shifts'][0]['participants'][0]['end'] = at('12:00')
    assert 'FULL_ASSIGNMENT_REQUIRED' in {f['code'] for f in evaluate(snapshot)['findings']}
    short = h.configure('assignment_types', {'name': 'Four hour duty', 'starts_on': '2020-01-01',
        'definition': {'default_timing': {'start_time': '08:00', 'end_time': '12:00'}, 'session_units': 1}})['record']
    assert h.shift(h.workspace(), typ=short['id'])['workspace']
    assert 'FIXED_SESSION_UNITS_REQUIRED' in str(h.configure('assignment_types',
        {'name': 'Fractional duty', 'starts_on': '2020-01-01', 'definition': {'session_unit_basis': 'proportional'}}, status=422))


def test_configured_duration_handles_overnight_and_dst():
    definition = {'default_timing': {'start_time': '20:00', 'end_time': '04:00'}}
    assert configured_duration_matches(definition, '2026-10-04T20:00:00+03:00', '2026-10-05T04:00:00+03:00', 'Asia/Riyadh')
    assert not configured_duration_matches(definition, '2026-10-04T20:00:00+03:00', '2026-10-05T00:00:00+03:00', 'Asia/Riyadh')


@pytest.mark.parametrize('start,end,elapsed', [
    ('2026-03-28T20:00:00+01:00', '2026-03-29T04:00:00+02:00', 7),
    ('2026-10-24T20:00:00+02:00', '2026-10-25T04:00:00+01:00', 9),
], ids=['spring', 'autumn'])
def test_configured_overnight_duty_uses_elapsed_hours_across_clock_change(start, end, elapsed):
    zone = ZoneInfo('Europe/Berlin')
    definition = {'default_timing': {'start_time': '20:00', 'end_time': '04:00'}}
    assert configured_duration_matches(definition, start, end, zone.key)
    local_start = datetime.fromisoformat(start).astimezone(zone)
    local_end = datetime.fromisoformat(end).astimezone(zone)
    assert duration_hours(local_start, local_end) == elapsed
