"""Preferences writes all four bulk controls through the existing settings command."""
import pytest


def settings_command(h, bulk_settings):
    me = h.get('admin', '/api/me')
    department, organisation = me['department'], me['organisation']
    return department, organisation, {'department_name': department['name'],
        'department_revision': department['revision'], 'organisation_name': organisation['name'],
        'timezone': organisation['timezone'], 'week_start': organisation['week_start'],
        'bulk_settings': bulk_settings, 'reason': 'Configure the department bulk controls'}


def test_department_settings_writes_all_four_bulk_limits_and_undoes_them(h):
    original = h.get('admin', '/api/bulk/settings')['settings']
    values = {'bulk_max_items': 120, 'bulk_direct_max': 12, 'bulk_staged_days': 9,
              'rate_bulk_preview_per_minute': 17}
    department, organisation, payload = settings_command(h, values)
    result = h.command('admin', 'department.settings', payload, department['id'], organisation['revision'])
    assert result['department']['bulk_settings'] == values
    assert h.get('admin', '/api/bulk/settings')['settings'] == values
    assert result['previous']['bulk_settings'] == original
    h.command('admin', 'department.settings', {**result['previous'], 'reason': 'Undo all four bulk controls'},
              department['id'], result['organisation']['revision'])
    assert h.get('admin', '/api/bulk/settings')['settings'] == original


@pytest.mark.parametrize('invalid', [
    {'bulk_max_items': 0}, {'bulk_direct_max': -1}, {'bulk_staged_days': 0},
    {'rate_bulk_preview_per_minute': -1}, {'bulk_max_items': 40, 'bulk_direct_max': 41},
])
def test_department_settings_validates_each_bulk_control_before_writing(h, invalid):
    original = h.get('admin', '/api/bulk/settings')['settings']
    department, organisation, payload = settings_command(h, {**original, **invalid})
    refused = h.command('admin', 'department.settings', payload, department['id'], organisation['revision'], status=422)
    assert refused['error']['code'] == 'SCHEMA_INVALID'
    assert h.get('admin', '/api/bulk/settings')['settings'] == original
    assert h.get('admin', '/api/me')['department']['revision'] == department['revision']
