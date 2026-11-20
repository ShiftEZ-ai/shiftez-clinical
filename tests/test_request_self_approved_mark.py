"""A decision a person makes on their own request under a self-approval permission is marked on the request itself:
its row and the decision in its history say Self-approved, as the audit log already does. An independent decision is not."""
from .governance_support import identity
from .test_availability_patterns import pattern

GRANT = {'actions': ['request.decide'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01',
         'reason': 'Synthetic bounded self-decision permission'}


def listed(h, role, row_id):
    return next(i for i in h.get(role, '/api/requests/all?status=approved')['items'] if i['id'] == row_id)


def test_a_self_approved_decision_marks_the_row_and_its_history(h):
    person = h.seed['people'][0]
    owner = identity(h, 'pattern_owner', ['staff', 'coordinator'], person=person)
    h.command('admin', 'self_approval.grant', {'identity_id': owner, **GRANT})
    row = h.command('pattern_owner', 'availability_pattern.propose', pattern(person))['pattern']
    h.command('pattern_owner', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Reviewed with a colleague by phone'},
              row['id'], row['revision'])
    detail = h.get('coordinator', '/api/requests/all?id=' + row['id'])
    assert detail['item']['self_approved'] is True
    decided = [e for e in detail['history'] if e['action'].endswith('.decide')]
    assert decided and decided[-1].get('self_approved') is True
    assert all(not e.get('self_approved') for e in detail['history'] if not e['action'].endswith('.decide'))
    assert listed(h, 'coordinator', row['id'])['self_approved'] is True


def test_an_independent_decision_is_not_marked(h):
    person = h.seed['people'][0]
    row = h.command('staff', 'availability_pattern.propose', pattern(person))['pattern']
    h.command('coordinator', 'availability_pattern.decide', {'decision': 'approve', 'reason': 'Independent review'},
              row['id'], row['revision'])
    detail = h.get('coordinator', '/api/requests/all?id=' + row['id'])
    assert detail['item']['self_approved'] is False
    assert not any(e.get('self_approved') for e in detail['history'])
    assert listed(h, 'coordinator', row['id'])['self_approved'] is False
