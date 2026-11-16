"""Whole duty booking is enforced at both command and publication boundaries."""
from packages.domain.validation import evaluate
from packages.domain.time import configured_duration_matches
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
    assert configured_duration_matches(definition, '2026-03-28T20:00:00+01:00', '2026-03-29T04:00:00+02:00', 'Europe/Berlin')
