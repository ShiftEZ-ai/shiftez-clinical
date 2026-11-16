"""Personal coverage keeps its subject rows without publishing departmental goal data."""
from copy import deepcopy

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.fairness import personal_goals
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES
from tests.governance_support import identity
from tests.test_fairness import goal, session


def test_personal_goal_projection_preserves_own_pairs_and_the_cached_report():
    reports = [{'score': 42, 'trend': [{'score': 42}], 'value': .4,
                'workload_spread': True, 'score_present': True,
                'people': [{'person_id': 'a', 'planned': 2}, {'person_id': 'b', 'planned': 7}],
                'pairs': [{'person_id': 'a', 'partner_id': 'b'}, {'person_id': 'b', 'partner_id': 'a'}]}]
    before = deepcopy(reports)
    projected, = personal_goals(reports, 'a')
    assert projected['people'] == [{'person_id': 'a', 'planned': 2}]
    assert projected['pairs'] == [{'person_id': 'a', 'partner_id': 'b'}]
    assert projected['score'] is None and projected['trend'] == []
    assert not {'value', 'workload_spread', 'score_present'} & projected.keys()
    for person in ('absent', None):
        empty, = personal_goals(reports, person)
        assert empty['people'] == empty['pairs'] == []
        assert empty['score'] is None and empty['trend'] == []
        assert not {'value', 'workload_spread', 'score_present'} & empty.keys()
    assert reports == before


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('first_reader', ['staff', 'coordinator'])
def test_personal_coverage_projects_goals_in_both_cache_read_orders(request, database, first_reader):
    h = request.getfixturevalue(database)
    own = h.get('staff', '/api/me')['person_id']
    other, absent = [p for p in h.seed['people'] if p != own]
    balance = goal(h)
    pair = goal(h, metric='pair_exposure', aim='min_per_pair', amount=1,
                partners={'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [own, other]}]})
    workspace = session(h, h.workspace(), 4, [own])
    workspace = session(h, workspace, 5, [other])
    publication = h.publish(workspace)['publication_id']
    path = '/api/publications/' + publication + '/coverage'
    first = h.get(first_reader, path)['evaluation']
    team = h.get('coordinator', path)['evaluation']
    mine = h.get('staff', path)['evaluation']
    assert first == (mine if first_reader == 'staff' else team)
    team_balance = next(r for r in team['goals'] if r['policy_id'] == balance['id'])
    assert {own, other} <= {r['person_id'] for r in team_balance['people']}
    assert team_balance['score'] is not None
    assert any(r['person_id'] == other for r in next(r for r in team['goals'] if r['policy_id'] == pair['id'])['pairs'])
    assert mine['goals'] and all(r['score'] is None and r['trend'] == [] for r in mine['goals'])
    assert all(r['person_id'] == own for report in mine['goals'] for r in report['people'] + report['pairs'])
    assert all(not {'value', 'workload_spread', 'score_present'} & report.keys() for report in mine['goals'])
    assert any(r['partner_id'] == other for report in mine['goals'] for r in report['pairs'])
    personal_dashboard = h.get('staff', '/api/fairness/goals')['goals']
    assert all(r['score'] is None and r['trend'] == [] for r in personal_dashboard)
    assert all(r['person_id'] == own for report in personal_dashboard for r in report['people'] + report['pairs'])
    # Every role and staff+role combination must respect this endpoint's own team-read contract.
    role_sets = [(role,) for role in ROLE_NAMES] + [('staff', role) for role in ROLE_NAMES if role != 'staff']
    for roles in role_sets:
        reader = 'coverage_' + '_'.join(roles)
        identity(h, reader, roles, own)
        team_reader = bool(set(roles) & {*PERMISSIONS['publications.read_team'], 'admin'})
        if not team_reader and 'staff' not in roles:
            assert h.get(reader, path, status=403)['error']['code'] == 'FORBIDDEN'
        else:
            assert h.get(reader, path)['evaluation'] == (team if team_reader else mine)
    assert h.get('coordinator', path)['evaluation'] == team
    identity(h, 'absent_from_publication', ['staff'], absent)
    assert h.get('absent_from_publication', path, status=404)['error']['code'] == 'NOT_FOUND'
    identity(h, 'unlinked_coverage', ['staff'])
    assert h.get('unlinked_coverage', path, status=404)['error']['code'] == 'NOT_FOUND'
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=db.get(m.Department, h.seed['department_id']).organisation_id,
                                  name='Synthetic foreign coverage department')
        db.add(department)
        db.flush()
        foreign_department = department.id
    identity(h, 'foreign_coverage_reader', ['coordinator'], department=foreign_department)
    assert h.get('foreign_coverage_reader', path, status=404)['error']['code'] == 'NOT_FOUND'
    assert h.get('coordinator', path)['evaluation'] == team
    assert h.get('staff', path)['evaluation'] == mine
