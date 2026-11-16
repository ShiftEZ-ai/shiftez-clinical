"""Sparse issued snapshots retain scheduled totals without live name lookups."""
import copy
import csv
import io

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import publication_snapshot
from packages.domain.reports import METRIC_VERSION, workload


KNOWN_PERSON = '00000000-0000-0000-0000-000000000001'
MISSING_PERSON = '00000000-0000-0000-0000-000000000002'
HIDDEN_PERSON = '00000000-0000-0000-0000-000000000003'


@pytest.fixture
def sparse_snapshot():
    return {'timezone': 'UTC', 'input_revision': 1, 'availability_revision': 1,
            'workspace': {'status': 'published', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'},
            'people': {KNOWN_PERSON: {'display_name': 'Issued name'}},
            'overtime_offered_intervals': {HIDDEN_PERSON: [('2026-10-04T08:00:00Z', '2026-10-04T12:00:00Z')]},
            'shifts': [{'id': 'duty', 'type_snapshot': {'definition': {}},
                        'participants': [{'person_id': pid, 'start': '2026-10-04T08:00:00Z',
                                          'end': '2026-10-04T12:00:00Z'} for pid in (KNOWN_PERSON, MISSING_PERSON)]}]}


@pytest.mark.parametrize('version', ['scheduled-v1', 'scheduled-v2', METRIC_VERSION])
@pytest.mark.parametrize('people', ['absent', 'empty', 'partial'])
def test_sparse_snapshot_names_do_not_discard_workload(sparse_snapshot, version, people):
    sparse_snapshot['metric_version'] = version
    if people == 'absent':
        sparse_snapshot.pop('people')
    elif people == 'empty':
        sparse_snapshot['people'] = {}
    before = copy.deepcopy(sparse_snapshot)
    result = workload(sparse_snapshot)
    rows = {row['person_id']: row for row in result['rows']}
    assert set(rows) == {KNOWN_PERSON, MISSING_PERSON}
    assert rows[KNOWN_PERSON]['display_name'] == ('Issued name' if people == 'partial' else 'Unknown person')
    assert rows[MISSING_PERSON]['display_name'] == 'Unknown person'
    assert all(row['assignments'] == 1 and row['scheduled_hours'] == 4 for row in rows.values())
    assert result['metric_version'] == version
    assert ('lead_turns' in rows[KNOWN_PERSON]) == (version != 'scheduled-v1')
    assert sparse_snapshot == before


@pytest.mark.parametrize('version', ['scheduled-v1', 'scheduled-v2', METRIC_VERSION])
def test_snapshot_without_people_or_assignments_has_empty_workload(sparse_snapshot, version):
    sparse_snapshot['metric_version'] = version
    sparse_snapshot.pop('people')
    sparse_snapshot['shifts'] = []
    assert workload(sparse_snapshot)['rows'] == []


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_team_report_formats_accept_missing_snapshot_people(database, request):
    h = request.getfixturevalue(database)
    workspace = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        snapshot = publication_snapshot(db, stored)
        snapshot.pop('people')
        snapshot['workspace']['status'] = 'published'
        snapshot['shifts'][0]['participants'][0]['supervisor_person_id'] = h.seed['people'][1]
        publication = m.Publication(department_id=h.seed['department_id'], workspace_id=stored.id,
            sequence=1, starts_on=stored.starts_on, ends_on=stored.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], validation=stored.validation,
            fingerprint='sparse-synthetic-fixture', snapshot=snapshot)
        db.add(publication)
        db.flush()
        publication_id = publication.id
    before = copy.deepcopy(snapshot)
    url = '/api/reports/' + publication_id
    for grouping in ('', '?grouping=week', '?grouping=month'):
        report = h.get('coordinator', url + grouping)
        [row] = report['rows']
        assert row['person_id'] == h.seed['people'][0]
        assert row['display_name'] == 'Unknown person'
        assert row['assignments'] == 1 and row['scheduled_hours'] == 4
        csv_response = h.client('coordinator').get(url + (grouping + '&' if grouping else '?') + 'format=csv')
        assert csv_response.status_code == 200, csv_response.text
        csv_rows = list(csv.reader(io.StringIO(csv_response.text)))
        values = dict(zip(csv_rows[1], csv_rows[2]))
        assert values['person'] == 'Unknown person' and float(values['scheduled_hours']) == 4
    for format in ('assignments', 'print'):
        response = h.client('coordinator').get(url + '?format=' + format)
        assert response.status_code == 200, response.text
        assert 'Unknown person' in response.text and 'Assigned' in response.text
        assert 'Alex Morgan' not in response.text
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, publication_id).snapshot == before
