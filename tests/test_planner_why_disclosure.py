"""Why and fill options share leave disclosure without changing the source snapshot."""
import copy
import json

import pytest

from apps.api.services import staffing
from packages.contracts import schemas as s
from .test_fill_options import options
from .test_leave_disclosure import setting
from .test_planner_why import why


@pytest.mark.parametrize('mode', ['type', 'details', 'none'])
def test_why_readers_share_fill_options_leave_disclosure_without_mutating_snapshot(h, mode):
    away = h.seed['people'][0]
    note = 'Private synthetic leave note'
    workspace = h.shift(h.workspace('Why leave readers'), people=[])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-03T00:00:00+03:00',
        'end': '2026-10-06T00:00:00+03:00', 'private_reason': note})
    h.command('admin', 'role.grant', {'identity_id': h.seed['identities']['staff'], 'role': 'programme_lead',
        'starts_on': '2026-10-01', 'reason': 'Synthetic Why reader'})
    if mode != 'type':
        setting(h, mode)
    workspace = h.detail(workspace)['workspace']
    source = h.snapshot(workspace)
    original = copy.deepcopy(source)
    shift_id = source['shifts'][0]['id']
    expected = {'type': 'Annual', 'starts_on': '2026-10-03', 'ends_on': '2026-10-05'}
    if mode == 'details':
        expected['note'] = note

    def disclosed(row):
        findings = [finding for finding in row['findings'] if finding['code'] == 'UNAVAILABLE']
        assert findings
        if mode == 'none':
            assert all('leave' not in finding for finding in findings)
            assert 'Annual' not in json.dumps(row)
        else:
            assert all(finding['leave'] == expected for finding in findings)
        assert (note in json.dumps(row)) == (mode == 'details')
        return [finding.get('leave') for finding in findings]

    fill = options(h, workspace, shift_id)
    shared = disclosed(next(row for row in fill['candidates'] if row['person_id'] == away))
    for person_id in (None, away):
        value = s.WhyInput(workspace_id=workspace['id'], expected_revision=workspace['revision'],
                           shift_ids=[shift_id], person_id=person_id)
        staffing.why(source, {}, value)
        assert source == original
        for role in s.TEAM_READER_ROLES:
            result = why(h, workspace, [shift_id], role='staff' if role == 'programme_lead' else role,
                         person_id=person_id)
            row = next(row for row in result['sessions'][0]['rows'] if row['person_id'] == away)
            assert row['group'] == 'unavailable'
            assert disclosed(row) == shared
    assert h.snapshot(workspace) == original
