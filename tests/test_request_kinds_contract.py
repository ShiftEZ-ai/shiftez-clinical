"""Stored request kinds are exported without admitting rotation changes as ordinary requests."""
from typing import get_args

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from tests.test_rotation_leave_policy import resident


def test_stored_request_kinds_include_rotation_changes_but_keep_input_contracts():
    ordinary = get_args(s.LeaveInput.model_fields['kind'].annotation)
    overtime = get_args(s.OvertimeAvailabilityInput.model_fields['kind'].annotation)
    absence = get_args(s.AbsenceReportInput.model_fields['kind'].annotation)
    assert set(s.REQUEST_KINDS) == {*ordinary, *overtime, *absence, 'rotation_change'}
    with pytest.raises(ValidationError):
        s.LeaveInput(person_id='00000000-0000-0000-0000-000000000001', kind='rotation_change',
            start='2026-10-04T08:00:00+03:00', end='2026-10-04T12:00:00+03:00', private_reason='Synthetic invalid request')


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_rotation_change_is_exported_and_still_uses_its_governed_command(database, request):
    h = request.getfixturevalue(database)
    typ, value, rotation = resident(h, policy='allowed')
    asked = h.command('staff', 'rotation.request_change', {'rotation_id': rotation['id'],
        'rotation_type_id': typ['id'], 'service_id': h.seed['services'][0],
        'starts_on': value['starts_on'], 'ends_on': value['ends_on'],
        'private_reason': 'Synthetic rotation service change'})['record']
    assert asked['kind'] == 'rotation_change' and asked['kind'] in s.REQUEST_KINDS
    [listed] = h.get('staff', '/api/requests/all')['items']
    assert listed['id'] == asked['id'] and listed['kind'] in s.REQUEST_KINDS
    h.command('staff', 'request.create', {'person_id': rotation['person_id'], 'kind': 'rotation_change',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'private_reason': 'Synthetic attempt to bypass rotation approval'}, status=422)
