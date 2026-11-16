"""Other departments' absences still block duties without disclosing their private details."""
import copy
import json
from types import SimpleNamespace

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.leave_disclosure import PLANNING_ROLES, disclose_leave
from packages.domain.time import aware
from .test_planner_why import why

FOREIGN_TYPE = 'Private leave category from another department'
FOREIGN_NOTE = 'Confidential note owned by another department'
LOCAL_NOTE = 'Note recorded in the viewing department'
MODES = ('none', 'type', 'details')


def foreign_absence(h, viewer_mode, source_mode, *, local=False):
    """A shared person, with an earlier foreign absence and optionally an overlapping local one."""
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.leave_reason_display = viewer_mode
        org = db.get(m.Organisation, department.organisation_id)
        other = m.Department(organisation_id=org.id, name='Other clinical department', leave_reason_display=source_mode)
        db.add(other)
        db.flush()
        absence = m.Availability(department_id=other.id, person_id=h.seed['people'][0], kind='recorded_leave',
            start=aware('2026-10-03T00:00:00+03:00'), end=aware('2026-10-06T00:00:00+03:00'),
            leave_type=FOREIGN_TYPE, private_reason=FOREIGN_NOTE, recorded_by=h.seed['identities']['coordinator'])
        db.add(absence)
        if local:
            db.add(m.Availability(department_id=department.id, person_id=h.seed['people'][0], kind='recorded_leave',
                start=aware('2026-10-04T00:00:00+03:00'), end=aware('2026-10-05T00:00:00+03:00'),
                leave_type='Annual leave', private_reason=LOCAL_NOTE, recorded_by=h.seed['identities']['coordinator']))
        org.input_revision += 1
        org.availability_revision += 1
        db.flush()
        return absence.id, org.id


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('viewer_mode', MODES)
@pytest.mark.parametrize('source_mode', MODES)
def test_foreign_department_absence_blocks_duties_without_disclosing_details(request, database, viewer_mode, source_mode):
    h = request.getfixturevalue(database)
    w = h.shift(h.workspace('Shared person absence privacy'))['workspace']
    # Why evaluates unassigned candidates; keep the original assigned duty to verify its safety finding too.
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    absence_id, _ = foreign_absence(h, viewer_mode, source_mode)
    w = h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']

    # The institution-wide safety input is retained; only its browser enrichment is restricted.
    snapshot = h.snapshot(w)
    absence = next(r for r in snapshot['availability'] if r['id'] == absence_id)
    assert 'private_reason' not in absence
    for role in ('coordinator', 'approver', 'auditor'):
        data = h.get(role, '/api/workspaces/' + w['id'])
        check = data['workspace']['validation']
        findings = [f for f in check['findings'] if f['code'] == 'UNAVAILABLE']
        assert findings and not check['valid']
        assert all('leave' not in f for f in findings)
        assert FOREIGN_TYPE not in json.dumps(data) and FOREIGN_NOTE not in json.dumps(data)

    # The planner's nested Why projection uses the same disclosure boundary.
    shift_id = next(s['id'] for s in h.detail(w)['shifts'] if not s['participants'])
    explanation = why(h, w, [shift_id])
    row = next(row for row in explanation['sessions'][0]['rows'] if row['person_id'] == h.seed['people'][0])
    assert row['group'] == 'unavailable'
    findings = [f for f in row['findings'] if f['code'] == 'UNAVAILABLE']
    assert findings and all('leave' not in f for f in findings)
    assert FOREIGN_TYPE not in json.dumps(explanation) and FOREIGN_NOTE not in json.dumps(explanation)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('viewer_mode', MODES)
def test_local_absence_enrichment_keeps_its_own_mode_and_never_selects_a_foreign_note(request, database, viewer_mode):
    h = request.getfixturevalue(database)
    _, organisation_id = foreign_absence(h, viewer_mode, 'details', local=True)
    body = {'sessions': [{'start': '2026-10-04T05:00:00+00:00', 'end': '2026-10-04T09:00:00+00:00',
                         'rows': [{'person_id': h.seed['people'][0], 'findings': [{'code': 'UNAVAILABLE'}]}]}]}
    for role in (*PLANNING_ROLES, 'staff'):
        actor = SimpleNamespace(department_id=h.seed['department_id'], organisation_id=organisation_id,
                                permits=lambda *allowed: role in allowed)
        with transaction(h.app.state.engine) as db:
            result = disclose_leave(db, actor, copy.deepcopy(body))
        finding = result['sessions'][0]['rows'][0]['findings'][0]
        expected = {'type': 'Annual leave', 'starts_on': '2026-10-04', 'ends_on': '2026-10-04'}
        if viewer_mode == 'none' or role == 'staff':
            assert 'leave' not in finding
        else:
            if viewer_mode == 'details':
                expected['note'] = LOCAL_NOTE
            assert finding['leave'] == expected
        assert FOREIGN_TYPE not in json.dumps(result) and FOREIGN_NOTE not in json.dumps(result)
