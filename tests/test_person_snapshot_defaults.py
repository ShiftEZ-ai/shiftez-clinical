"""Historical publication projections fill optional defaults without rewriting issued evidence."""
import copy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.snapshots import publication_snapshot
from packages.contracts.schemas import TypeDefinition
from packages.domain.reports import workload
from packages.domain.score import schedule_score
from tests.test_schedule_score import coverage, data, shift


def test_legacy_workload_credits_use_contract_defaults():
    person = str(uuid.uuid4())
    duty = shift('old-duty', 4, [person])
    duty['participants'][0].pop('overtime')
    duty['type_snapshot']['definition'] = {}
    snapshot = {'shifts': [duty], 'people': {person: {'display_name': 'Synthetic person'}},
                'workspace': {'status': 'published'}, 'timezone': 'UTC', 'metric_version': 'scheduled-v2',
                'input_revision': 1, 'availability_revision': 1}
    original = copy.deepcopy(snapshot)
    [row] = workload(snapshot)['rows']
    defaults = TypeDefinition()
    assert row['scheduled_hours'] == 8
    assert row['workload_credit'] == 8 * defaults.workload_credit_per_hour
    assert row['training_credit'] == 8 * defaults.training_credit_per_hour
    assert snapshot == original


@pytest.mark.parametrize('definition', [None, {}])
def test_legacy_score_participants_need_no_redundant_shift_id(definition):
    source = data([shift('old-duty', 4, ['a'])])
    if definition is None:
        source['shifts'][0]['type_snapshot'].pop('definition')
    else:
        source['shifts'][0]['type_snapshot']['definition'] = definition
    original = copy.deepcopy(source)
    result = schedule_score(source, {'coverage': [coverage('old-duty', 4, 1, 1)], 'findings': []})
    assert result['score'] == result['people']['a']['score'] == 100
    assert source == original


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
@pytest.mark.parametrize('missing', ['requirements', 'active'])
def test_legacy_workload_keeps_both_overload_and_underload_person_scores(measure, missing):
    source = data([shift('s1', 4, ['a']), shift('s2', 5, ['a']),
                   shift('s3', 6, ['a']), shift('s4', 7, ['b'])])
    if missing == 'requirements':
        for duty in source['shifts']:
            duty['type_snapshot']['definition'] = {}
    else:
        for person in source['people'].values():
            person.pop('active')
    original = copy.deepcopy(source)
    result = schedule_score(source, {'coverage': [], 'findings': []}, {'balance_measure': measure})
    assert result['components']['fairness']['value'] == 0.5625
    assert result['people']['a']['score'] == result['people']['b']['score'] == 50
    from packages.domain.fairness import goal_reports
    [report] = goal_reports({**source, 'generation_options': {'balance_measure': measure}})
    rows = {row['person_id']: row for row in report['people']}
    assert rows['a']['status'] == 'above' and rows['b']['status'] == 'below'
    assert all(row['low'] == row['expected'] == row['high'] for row in rows.values())
    assert source == original


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_person_reads_accept_optional_legacy_snapshot_fields(database, request):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    for metric, amount in (('workload_credit', 4), ('training_credit', 0)):
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
                  'metric': metric, 'comparison': 'target', 'amount': amount,
                  'participation': ['staff'], 'overtime': 'exclude'}, severity='advisory')
    h.policy({'kind': 'max_hours', 'hours': 40, 'include_overtime': False})
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, workspace['id'])
        snapshot = publication_snapshot(db, stored)
        for duty in snapshot['shifts']:
            for participant in duty['participants']:
                for key in ('shift_id', 'fixed', 'participation', 'overtime', 'supervisor_person_id',
                            'team_lead', 'segments'):
                    participant.pop(key, None)
            duty['type_snapshot']['definition'] = {}
        db.add(m.Publication(department_id=h.seed['department_id'], workspace_id=stored.id,
            sequence=1, starts_on=stored.starts_on, ends_on=stored.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], validation=stored.validation,
            fingerprint='historical-fixture', snapshot=snapshot))
    original = copy.deepcopy(snapshot)
    score = h.get('staff', f'/api/people/{person}/score')['published']
    assert score['person_score']['score'] == 100
    work = h.get('staff', f'/api/people/{person}/workload?on=2026-10-06')
    rota = next(row for row in work['periods'] if row['key'] == 'this_rota')
    assert rota['totals']['duties'] == 1 and rota['totals']['hours'] == 4
    assert {row['metric']: row['actual'] for row in rota['quotas']} == {'workload_credit': 4, 'training_credit': 0}
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Publication)).snapshot == original
