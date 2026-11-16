"""Stored coverage findings need not have an anchor session."""
import copy

import pytest

from apps.api import models as m
from apps.api.db import transaction


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('affected', [False, True])
def test_person_scores_read_unanchored_gap_without_changing_issued_check(request, database, affected):
    h = request.getfixturevalue(database)
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             scope={'service_ids': [h.seed['services'][0]]})
    typ = h.configure('assignment_types', {'name': 'Unanchored gap fixture', 'starts_on': '2020-01-01',
                      'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[h.seed['people'][1]], typ=typ)['workspace']
    shift_id = h.detail(workspace)['shifts'][0]['id']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace['id'])
        check = copy.deepcopy(row.validation)
        # Isolate the finding's association from the per-session coverage index.
        check['coverage'] = []
        gap, = [finding for finding in check['findings'] if finding['code'] == 'SCOPED_COVERAGE_GAP']
        gap.pop('shift_id', None)
        if affected:
            gap['detail']['affected_shift_ids'] = [shift_id]
        else:
            gap['detail'].pop('affected_shift_ids', None)
        row.validation = check

    for person_id in h.seed['people']:
        result = h.get('coordinator', f'/api/people/{person_id}/score?open_limit=0')['draft']
        assert result['workspace_id'] == workspace['id'] and result['fresh']
    result = h.get('coordinator', f'/api/people/{h.seed["people"][0]}/score?open_limit=20')['draft']
    assert result['open_places_total'] == int(affected)
    assert [place['shift_id'] for place in result['open_places']] == ([shift_id] if affected else [])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, workspace['id']).validation == check
