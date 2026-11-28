"""A department's times of day (Configuration › Times of day) are a display setting: defaults until saved, validated,
kept in order of start, versioned with history and undone by the served restore."""
import json

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s

MINE = [{'name': 'Late', 'code': 'L', 'start': '19:00', 'colour': 'blue'},
        {'name': 'Early', 'code': 'E', 'start': '07:30', 'colour': 'teal'}]


def test_defaults_cover_the_day_and_each_row_is_checked():
    times = s.DisplaySettingsInput(reason='Defaults').model_dump(mode='json')['times_of_day']
    assert times == s.TIMES_OF_DAY_DEFAULTS
    assert [t['start'] for t in times] == sorted(t['start'] for t in times)
    for bad in ({'code': 'MM'}, {'code': 'm'}, {'start': '24:00'}, {'start': '7:00'}, {'name': ' '}, {'colour': 'red'}):
        with pytest.raises(ValidationError):
            s.TimeOfDay(**{**MINE[0], **bad})
    with pytest.raises(ValidationError):
        s.DisplaySettingsInput(times_of_day=[], reason='None')
    with pytest.raises(ValidationError):
        s.DisplaySettingsInput(times_of_day=[{**MINE[0], 'code': chr(65 + i), 'name': f'T{i}', 'start': f'{i:02d}:00'}
                                             for i in range(s.TIMES_OF_DAY_LIMIT + 1)], reason='Too many')


@pytest.mark.parametrize('field,value,said', [('name', 'early', 'Early already uses early'),
                                               ('code', 'E', 'Early already uses E'),
                                               ('start', '07:30', 'Early already uses 07:30')])
def test_a_name_code_or_start_used_twice_is_refused_in_words(field, value, said):
    with pytest.raises(ValidationError, match=said):
        s.DisplaySettingsInput(times_of_day=[MINE[1], {**MINE[0], field: value}], reason='Twice')


def test_saving_keeps_start_order_versions_and_undo(h):
    department = h.get('admin', '/api/me')['department']
    assert 'times_of_day' not in (department.get('display_settings') or {})
    saved = h.command('admin', 'department.display', {'times_of_day': MINE, 'reason': 'Our times'},
                      department['id'], department['revision'])
    assert [t['name'] for t in saved['department']['display_settings']['times_of_day']] == ['Early', 'Late']
    # Saving another display setting keeps the department's own times.
    other = h.command('admin', 'department.display', {'names_default': 'code', 'reason': 'Names'},
                      saved['department']['id'], saved['department']['revision'])
    assert [t['code'] for t in other['department']['display_settings']['times_of_day']] == ['E', 'L']
    versions = h.get('admin', '/api/department/display/versions')['versions']
    assert any('times_of_day' in v['changed_fields'] and v['reason'] == 'Our times' for v in versions)
    # Undo the names change, then the times: the defaults come back.
    for reply in (other, saved):
        undo, current = reply['undo'], h.get('admin', '/api/me')['department']
        h.command('admin', undo['kind'], undo['payload'], undo['target_id'], current['revision'])
    assert h.get('admin', '/api/me')['department']['display_settings']['times_of_day'] == s.TIMES_OF_DAY_DEFAULTS


def test_only_an_admin_changes_them_and_a_refusal_says_which_row(h):
    department = h.get('admin', '/api/me')['department']
    h.command('staff', 'department.display', {'times_of_day': MINE, 'reason': 'Not mine'},
              department['id'], department['revision'], status=403)
    refused = h.command('admin', 'department.display', {'times_of_day': [MINE[1], {**MINE[0], 'code': 'E'}], 'reason': 'Twice'},
                        department['id'], department['revision'], status=422)
    assert 'Early already uses E' in json.dumps(refused)
