"""The compiled session form preserves the participant's own planned presence."""
import json

import pytest

from .test_client_form_answers import run


def interval(start, end):
    return {'start': f'2026-10-04T{start}', 'end': f'2026-10-04T{end}'}


SESSION = interval('08:00:00Z', '12:00:00Z')
SEGMENTS = [
    interval('08:00:00+00:00', '09:00:00Z'),
    interval('09:30:00.000Z', '10:30:00+00:00'),
    interval('11:00:00Z', '12:00:00+00:00'),
]
PARTIAL = {**SESSION, 'segments': SEGMENTS}
LATER_END = interval('08:00:00Z', '12:30:00Z')
EARLIER_START = interval('07:30:00Z', '12:00:00Z')
TRIMMED = interval('08:30:00Z', '12:30:00Z')
MIDDLE_ONLY = interval('09:15:00Z', '11:15:00Z')
MOVED = interval('14:00:00Z', '18:00:00Z')


@pytest.mark.parametrize('own,new,expected', [
    pytest.param(SESSION, LATER_END, {**LATER_END, 'segments': [], 'cut': False}, id='whole-duty-follows-times'),
    pytest.param(PARTIAL, SESSION, {**PARTIAL, 'cut': False}, id='unchanged-keeps-exact-strings'),
    pytest.param(PARTIAL, LATER_END, {**LATER_END, 'segments': [
        interval('08:00:00Z', '09:00:00Z'), SEGMENTS[1], interval('11:00:00Z', '12:30:00Z'),
    ], 'cut': False}, id='all-segments-fit'),
    pytest.param({**SESSION, 'segments': [SEGMENTS[0], SEGMENTS[2]]}, EARLIER_START,
                 {**EARLIER_START, 'segments': [
                     interval('07:30:00Z', '09:00:00Z'), interval('11:00:00Z', '12:00:00Z'),
                 ], 'cut': False}, id='earlier-start-keeps-both-segments'),
    pytest.param(PARTIAL, TRIMMED, {**TRIMMED, 'segments': [
        interval('08:30:00Z', '10:30:00+00:00'), interval('11:00:00Z', '12:30:00Z'),
    ], 'cut': True}, id='cut-keeps-two-with-new-outer-edges'),
    pytest.param(PARTIAL, MOVED, {**MOVED, 'segments': [], 'cut': True}, id='none-fit'),
    pytest.param(PARTIAL, MIDDLE_ONLY, {**MIDDLE_ONLY, 'segments': [], 'cut': True},
                 id='one-segment-becomes-whole-duty'),
    pytest.param(PARTIAL, interval('08:00:00+00:00', '12:00:00+00:00'),
                 {**PARTIAL, 'cut': False}, id='equivalent-instant-spellings'),
])
def test_kept_attendance(client_modules, own, new, expected):
    args = json.dumps([own, SESSION, new['start'], new['end']])
    actual = run(client_modules, f'''
const {{keptAttendance}} = await import(FORMS);
console.log(JSON.stringify(keptAttendance(...{args})));
''')
    assert actual == expected
