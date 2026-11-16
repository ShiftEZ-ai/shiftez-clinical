"""A current rule can be absent from a published rota's frozen scheduling inputs."""
from datetime import date
from uuid import uuid4

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_rule_detail_keeps_current_rule_when_publication_has_no_reach(database, request):
    h = request.getfixturevalue(database)
    workspace = h.shift(h.workspace())['workspace']
    h.publish(workspace)
    source = line(numbers={'max': {'value': 6}})
    policy = approve(h, propose(h, 'duties_per_week', [source])['record'])
    query = (f"day=2026-10-04&workspace_id={workspace['id']}&starts_on=2026-10-04&ends_on=2026-10-11"
             f"&policy_id={policy['id']}&line_id={source['id']}")
    endpoint = '/api/rules/catalogue/duties_per_week/detail?'

    result = h.get('coordinator', endpoint + query)
    assert result['entry']['rule']['id'] == policy['id']
    assert result['check']['issues'] == []
    assert result['reach'] is None
    reached = h.get('auditor', '/api/policies/reach?details=true&' + query)
    assert reached['reach'] == reached['details'] == {}
    assert reached['workspace_id'] == workspace['id']
    assert policy['id'] not in {row['id'] for row in h.snapshot(workspace)['policies']}

    today = h.get('auditor', f"/api/policies/reach?day=2026-10-04&policy_id={policy['id']}")
    assert today['reach'][policy['id']] == len(h.seed['people'])
    for old, replacement in ((policy['id'], str(uuid4())), (source['id'], str(uuid4())),
                             (workspace['id'], str(uuid4()))):
        h.get('auditor', endpoint + query.replace(old, replacement), status=404)
        h.get('auditor', '/api/policies/reach?' + query.replace(old, replacement), status=404)
    h.get('staff', endpoint + query, status=403)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_missing_workspace_reach_does_not_expose_another_departments_rule(database, request):
    h = request.getfixturevalue(database)
    workspace = h.workspace()
    source = line(numbers={'max': {'value': 6}})
    policy = approve(h, propose(h, 'duties_per_week', [source])['record'])
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        foreign = m.Policy(department_id=department.id, name='Other department rule', kind='catalogue', status='active',
                           owner='Synthetic owner', source='Reach isolation fixture', starts_on=date(2026, 1, 1),
                           created_by=h.seed['identities']['coordinator'], definition=policy['definition'])
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
    query = f"workspace_id={workspace['id']}&policy_id={foreign_id}&line_id={source['id']}"
    h.get('auditor', '/api/policies/reach?' + query, status=404)
    h.get('auditor', '/api/rules/catalogue/duties_per_week/detail?' + query, status=404)
