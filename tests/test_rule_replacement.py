"""Activating a rebuilt rule retires the older active copies it replaces, in one decision."""


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
