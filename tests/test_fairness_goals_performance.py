"""Department-scale lifecycle and fairness measurements with rolling leadership history."""
from copy import deepcopy
import json
import uuid

import pytest
from sqlalchemy import select
from apps.api import main as api, models as m
from apps.api.db import transaction
from apps.api.services import fairness
from apps.api import snapshots
from tests.test_backend_scale import measure
from tests.test_fix_list_performance import backend
from tests.test_lead_history_activation_performance import lead_history_case
from tests.test_performance_budget import python_budget


def lifecycle_case(h, supervision=False):
    supervision_rule = {'trainee_category_ids': [], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1, 'require_pairing': True}
    def historical_shift(shift, people):
        shift['type_snapshot'] = deepcopy(shift['type_snapshot'])
        shift['type_snapshot']['definition']['supervision'] = supervision_rule
        supervisor = shift['participants'][0]
        supervisor['participation'] = 'supervisor'
        trainee = people[(people.index(supervisor['person_id']) + 1) % len(people)]
        shift['participants'].append({**supervisor, 'id': m.uid(), 'person_id': trainee,
            'participation': 'trainee', 'supervisor_person_id': supervisor['person_id'], 'team_lead': False})
    workspace, target, _, predecessor = lead_history_case(h, historical_shift=historical_shift if supervision else None)
    with transaction(h.app.state.engine, write=True) as db:
        old, goal = db.get(m.Policy, predecessor), db.get(m.Policy, target)
        old.status, old.active = 'retired', False
        db.flush()
        goal.status, goal.active = 'active', True
        if supervision:
            definition = deepcopy(goal.definition)
            definition['rule']['lines'][-1]['settings']['balance_goal'] = {
                'metric': 'supervision', 'period': 'rolling', 'period_months': 12,
                'share_by': 'supervisable_sessions'}
            goal.definition = definition
            people = list(db.scalars(select(m.Person.id).where(m.Person.organisation_id == h.seed['organisation_id'])))
            known = set(db.scalars(select(m.CapabilityGrant.person_id).where(
                m.CapabilityGrant.capability_id == h.seed['supervisor_capability'])))
            from datetime import date
            db.add_all([m.CapabilityGrant(person_id=pid, department_id=h.seed['department_id'],
                capability_id=h.seed['supervisor_capability'], starts_on=date(2020, 1, 1),
                granted_by=h.seed['identities']['credentialer'], evidence='Synthetic scale qualification')
                for pid in people if pid not in known])
        # All three assigned consultants are leads in this synthetic fixture.
        for shift in db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace['id'])):
            typ = deepcopy(shift.type_snapshot)
            typ['definition']['lead']['maximum'] = 3
            if supervision:
                typ['definition']['supervision'] = supervision_rule
                rows = list(db.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id).order_by(m.Assignment.id)))
                for index, row in enumerate(rows):
                    row.participation = 'trainee' if index == 2 else 'supervisor'
                    row.supervisor_person_id = rows[0].person_id if index == 2 else None
            shift.type_snapshot = typ
        db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
    h.client('coordinator'); h.client('approver')
    return workspace


def command(h, workspace, kind, role='coordinator'):
    response = h.client(role).post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={'kind': 'workspace.' + kind, 'payload': {'reason': 'Synthetic scale lifecycle'},
              'target_id': workspace['id'], 'expected_revision': workspace['revision']})
    assert response.status_code == 200, response.text[:2000]
    return response.json()


def test_supervision_fairness_goals_at_department_scale(backend, monkeypatch, request):
    pg = backend
    workspace = lifecycle_case(pg, supervision=True)
    original, acquired = snapshots._domain_snapshot, []
    def once(*args, **kwargs):
        acquired.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshots, '_domain_snapshot', once)
    result, timing = measure(pg, 'supervision-fairness-goals', lambda: pg.get('coordinator',
        '/api/fairness/goals?workspace_id=' + workspace['id']), monkeypatch, (fairness, 'goals'))
    goal = next(r for r in result['goals'] if r['metric'] == 'supervision')
    assert sum(row['prior'] for row in goal['people']) == 6600
    assert sum(row['planned'] for row in goal['people']) == 768
    assert sum(row['expected'] for row in goal['people']) > 7300
    assert acquired == [1]
    assert sum(timing['sql'].values()) <= 35
    request.node.user_properties.append(('supervision_fairness_goals', json.dumps(timing)))
    return locals()


@pytest.mark.cpu_budget
@pytest.mark.postgres
def test_supervision_fairness_goals_cpu_budget(pg, monkeypatch, request):
    state = test_supervision_fairness_goals_at_department_scale(pg, monkeypatch, request)
    assert state['timing']['cpu_seconds'] < python_budget(4), state['timing']


@pytest.mark.wall_benchmark
@pytest.mark.postgres
def test_supervision_fairness_goals_wall_budget(pg, monkeypatch, request):
    state = test_supervision_fairness_goals_at_department_scale(pg, monkeypatch, request)
    assert state['timing']['wall_seconds'] < python_budget(6), state['timing']


@pytest.mark.postgres
def test_fairness_goals_projection_reuses_across_api_apps(pg, monkeypatch):
    """A cold goals calculation is durable; another worker must reuse that revision."""
    from fastapi.testclient import TestClient

    workspace = pg.shift(pg.workspace('Fairness projection'))['workspace']
    path = '/api/fairness/goals?workspace_id=' + workspace['id']
    calls, original = [], fairness._goals_uncached

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(fairness, '_goals_uncached', counted)
    first = pg.get('coordinator', path)
    assert len(calls) == 1

    other_app = api.create_app(pg.settings)
    try:
        with TestClient(other_app, base_url=pg.settings.public_origin) as client:
            client.cookies.update(pg.client('coordinator').cookies)
            second = client.get(path)
            assert second.status_code == 200, second.text
            assert second.json() == first
            assert len(calls) == 1
    finally:
        other_app.state.engine.dispose()
