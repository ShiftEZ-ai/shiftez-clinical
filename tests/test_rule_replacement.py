"""Activation replaces compatible copies and defers any weaker optional safety retirement."""
import pytest

from .governance_support import identity


def test_activation_retires_the_copies_it_replaces(h):
    old = [h.policy({'kind': 'min_rest', 'hours': 10}) for _ in range(2)]
    other = h.policy({'kind': 'max_consecutive_days', 'maximum': 5})
    new = h.command('coordinator', 'policy.create', {'name': 'Rest · rebuilt', 'owner': 'Dept', 'source': 'Rebuilt rule', 'starts_on': '2020-01-01',
        'definition': {'severity': 'mandatory', 'exception_allowed': False, 'scope': {}, 'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    # A different rule type cannot be swept away by mistake.
    h.command('approver', 'policy.activate', {'reason': 'Replace', 'replaces_ids': [other['id']]}, new['id'], new['revision'], status=422)
    h.command('approver', 'policy.activate', {'reason': 'Replace weekly copies', 'replaces_ids': [o['id'] for o in old]}, new['id'], new['revision'])
    status = {p['id']: p['status'] for p in [p for state in ('active', 'proposed', 'retired')
        for p in h.get('coordinator', '/api/policies?status=' + state)['policies']]}
    assert status[new['id']] == 'active' and all(status[o['id']] == 'retired' for o in old) and status[other['id']] == 'active'


@pytest.mark.parametrize('already_waiting', [False, True])
def test_narrower_activation_keeps_wider_safety_until_retirement_is_approved(h, already_waiting):
    wide = h.policy({'kind': 'min_rest', 'hours': 11})
    narrow = h.command('coordinator', 'policy.create', {
        'name': 'Scoped rest standard', 'owner': 'Rule coordinator', 'source': 'Review rest requirements',
        'starts_on': '2020-01-01',
        'definition': {'scope': {'person_ids': [h.seed['people'][0]]},
                       'rule': {'kind': 'min_rest', 'hours': 12}}})['record']
    reviewer = 'second_retirement_reviewer'
    reviewer_id = identity(h, reviewer, ['approver'])
    reason = {'reason': 'Independent rest review'}
    if already_waiting:
        first = h.command(reviewer, 'policy.retire', reason, wide['id'], wide['revision'])
        assert first['approval']['approved_by'] == [reviewer_id]

    activated = h.command('approver', 'policy.activate', {**reason, 'replaces_ids': [wide['id']]},
                          narrow['id'], narrow['revision'])
    assert activated['record']['status'] == 'active' and not activated['replaced']
    [waiting] = activated['pending_retirements']
    assert waiting['record'] == wide
    assert waiting['approval']['approvals_required'] == 2 and waiting['approval']['approvals_received'] == 1
    assert not waiting['approval']['complete']
    first_reviewer = reviewer if already_waiting else 'approver'
    second_reviewer = 'approver' if already_waiting else reviewer
    assert waiting['approval']['approved_by'] == [h.seed['identities'][first_reviewer]]
    active = h.get('approver', '/api/policies?status=active')['policies']
    assert {wide['id'], narrow['id']} <= {p['id'] for p in active}
    duplicate = h.command(first_reviewer, 'policy.retire', reason, wide['id'], wide['revision'], status=409)
    assert duplicate['error']['code'] == 'DUPLICATE_REVIEWER'
    retired = h.command(second_reviewer, 'policy.retire', reason, wide['id'], wide['revision'])
    assert retired['record']['status'] == 'retired' and retired['approval']['complete']
    assert set(retired['approval']['approved_by']) == {reviewer_id, h.seed['identities']['approver']}
