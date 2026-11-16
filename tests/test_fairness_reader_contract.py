"""One exported grant represents existing team workload, fairness and quota readers."""
import pytest

from apps.api.auth import Actor, _effective, require_permission
from apps.api.errors import Problem
from apps.api.services import fairness, scheduling_facts
from packages.contracts import schemas as s
from tests.governance_support import identity


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_fairness_reader_grant_matches_domain_read_authority(role):
    assert fairness.READERS == scheduling_facts.READERS == s.PERMISSIONS['fairness.read_team']
    actor = Actor('identity', 'department', 'organisation', 'Synthetic reader', _effective([role]), None, 'session')
    expected = role in {'admin', 'coordinator', 'approver', 'programme_lead', 'auditor'}
    assert fairness.sees_everyone(actor) is expected
    if expected:
        require_permission(actor, 'fairness.read_team')
    else:
        with pytest.raises(Problem):
            require_permission(actor, 'fairness.read_team')


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_fairness_reader_grant_matches_person_workload_and_quotas(database, request, role):
    h = request.getfixturevalue(database)
    if role == 'programme_lead':
        identity(h, role, [role])
    me = h.get(role, '/api/me')
    team = role in s.PERMISSIONS['fairness.read_team']
    assert ('fairness.read_team' in me['permissions']) is team
    own, other = h.seed['people'][:2]
    for person in (own, other):
        allowed = team or role == 'staff' and person == own
        for source in ('fairness', 'workload', 'score'):
            h.get(role, f'/api/people/{person}/{source}', status=200 if allowed else 403)
    h.get(role, f'/api/people/{other}/scheduling-facts', status=200 if team else 403)
