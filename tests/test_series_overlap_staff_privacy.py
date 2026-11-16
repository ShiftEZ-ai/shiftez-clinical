"""Every staff conflict label stays inside the published-session privacy boundary."""
import copy
import json

import pytest

from apps.api.services.schedule_view import staff_shift
from tests.test_series_overlap_api import absence, candidate, reviewed
from tests.test_series_overlap_publications import imported_publication
from tests.test_series_reconciliation import materialise


PRIVATE = 'Synthetic planner-only template name'
PEER_PRIVATE = 'Synthetic planner-only peer template name'
WINDOW = '/api/schedule/sessions?start=2026-10-05&end=2026-10-06'


@pytest.mark.parametrize('endpoint', ['schedule', 'publication'])
@pytest.mark.parametrize('conflict', ['absence', 'peer'])
def test_staff_conflict_names_hide_templates_but_coordinator_keeps_them(h, endpoint, conflict):
    recurring = reviewed(h, candidate(h, PRIVATE))
    workspace = materialise(h, recurring)
    if conflict == 'peer':
        peer = reviewed(h, candidate(h, PEER_PRIVATE, service=1))
        workspace = materialise(h, peer, workspace)
        publication = imported_publication(h, workspace)
    else:
        publication = h.publish(workspace)['publication_id']
        absence(h, day='2026-10-05')
    path = WINDOW if endpoint == 'schedule' else '/api/publications/' + publication
    planner = h.get('coordinator', path)
    staff = h.get('staff', path)
    planned = planner.get('snapshot', planner)['shifts']
    projected = staff.get('snapshot', staff)['shifts']
    assert any(row['session_name'] == PRIVATE for shift in planned
               for row in shift.get('double_booking_conflicts', []))
    if conflict == 'peer':
        assert any(row['other_session_name'] == PEER_PRIVATE for shift in planned
                   for row in shift.get('double_booking_conflicts', []))
    flags = [row for shift in projected for row in shift.get('double_booking_conflicts', [])]
    assert flags and all(row['person_id'] == h.seed['people'][0] for row in flags)
    assert PRIVATE not in json.dumps(staff) and PEER_PRIVATE not in json.dumps(staff)
    assert all(row['session_name'] for row in flags)
    assert all(bool(row['other_session_name']) == (conflict == 'peer') for row in flags)


def test_staff_projection_contains_no_private_snapshot_values_at_any_depth():
    private = {'name': PRIVATE, 'definition': {'attributes': {'planner_secret': 'Synthetic secret attribute'},
                                             'default_person_ids': ['Synthetic hidden usual person']}}
    conflict = {'person_id': 'mine', 'session_name': private['name'], 'other_session_name': private['name'],
                'shift_id': 'own', 'other_shift_id': 'peer', 'clinic_names': ['Visible clinic'],
                'other_clinic_names': ['Visible peer clinic']}
    shift = {'participants': [{'person_id': 'mine'}], 'attributes': {}, 'type_snapshot': {'definition': {}},
             'series_snapshot': private, 'double_booking_conflicts': [conflict]}
    before = copy.deepcopy(shift)
    projected = staff_shift(shift, 'mine')

    def strings(value):
        if isinstance(value, dict):
            return [part for key, child in value.items() for part in [key, *strings(child)]]
        if isinstance(value, list):
            return [part for child in value for part in strings(child)]
        return [value] if isinstance(value, str) else []

    private_values = {private['name'], *private['definition']['attributes'].values(),
                      *private['definition']['default_person_ids']}
    assert set(strings(projected)).isdisjoint(private_values)
    assert 'series_snapshot' not in strings(projected)
    assert shift == before
