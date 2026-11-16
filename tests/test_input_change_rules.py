from copy import deepcopy

import pytest

from apps.api.input_changes import rules_changed


@pytest.mark.parametrize('change,expected', [
    ('activation', True), ('retirement', True), ('definition', True), ('starts_on', True),
    ('name', False), ('revision', True), ('proposal', False), ('availability', False),
])
def test_live_rule_changes_use_the_audited_scheduling_fields(change, expected):
    policy = {'id': 'rule', 'status': 'active', 'active': True, 'revision': 1,
              'definition': {'rule': {'kind': 'max_duties', 'maximum': 5}}}
    before, after = {'policies': [policy]}, {'policies': [deepcopy(policy)]}
    if change == 'activation':
        before['policies'] = []
    elif change == 'retirement':
        after['policies'][0].update(status='retired', active=False)
    elif change == 'definition':
        after['policies'][0]['definition']['rule']['maximum'] = 6
    elif change == 'starts_on':
        after['policies'][0]['starts_on'] = '2026-11-01'
    elif change == 'proposal':
        after['policies'].append({**policy, 'id': 'proposal', 'status': 'proposed'})
    elif change == 'availability':
        after['availability'] = [{'id': 'leave'}]
    else:
        after['policies'][0][change] = 2
    assert rules_changed([{'kind': 'live_safety', 'before': before, 'after': after}]) is expected


def test_generated_person_lines_do_not_count_as_governed_policy_changes():
    # The resolver labels generated protected-time lines as policy sources too.
    assert not rules_changed([{'kind': 'rule_lines', 'source_kind': 'policies'}])
    assert not rules_changed([{'kind': 'policies', 'source_kind': 'event_occurrences'}])
