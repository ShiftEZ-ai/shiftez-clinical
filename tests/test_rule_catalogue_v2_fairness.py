"""Current fairness goals and prospective checks share one bound scheduling context."""
import copy
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.snapshots import domain_snapshot
from packages.domain import fairness as f
from packages.domain.conditions import active_rules
from tests.test_rule_catalogue_v2_api import proposed


def convert(h):
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent department conversion review'},
              change['id'], change['revision'])


def test_current_fairness_reads_updated_person_target_and_keeps_publication_aim(h):
    person = h.seed['people'][0]
    source = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'period': 'rota',
                       'amount': 10, 'person_amounts': {person: 7}}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    publication = h.publish(workspace)['publication_id']
    convert(h)
    with transaction(h.app.state.engine) as db:
        fact = record(db.scalar(select(m.SchedulingFact).where(m.SchedulingFact.kind == 'target',
                      m.SchedulingFact.person_id == person, m.SchedulingFact.status == 'active')))
    assert fact['data']['goal_ref']['line_id'] is None
    data = fact['data']
    line = copy.deepcopy(data['line'])
    line['numbers']['target']['value'] = 9
    waiting = h.command('coordinator', 'person.workload.propose', {
        'kind': 'target', 'person_id': person, 'catalogue_id': data['catalogue_id'],
        'catalogue_version': data['catalogue_version'], 'line': line, 'goal_ref': data['goal_ref'],
        'replaces_id': fact['id'], 'expected_revision': fact['revision'],
        'reason': 'Reviewed new personal amount'})['fact']
    h.command('approver', 'person.workload.activate', {'reason': 'Independent personal target review'},
              waiting['id'], waiting['revision'])
    for suffix in ('', '?workspace_id=' + workspace['id']):
        goals = h.get('coordinator', '/api/fairness/goals' + suffix)['goals']
        [goal] = [value for value in goals if value['policy_id'] == source['id']]
        assert next(value for value in goal['people'] if value['person_id'] == person)['expected'] == 9
        goals = h.get('coordinator', '/api/people/' + person + '/fairness' + suffix)['goals']
        [goal] = [value for value in goals if value['policy_id'] == source['id']]
        assert goal['people'][0]['expected'] == 9
        [trend] = goal['trend']
        assert (trend['publication_id'], trend['expected']) == (publication, 7)
    with transaction(h.app.state.engine) as db:
        historical = domain_snapshot(db, db.get(m.Workspace, workspace['id']))
    [goal] = [value for value in f.goal_reports(historical) if value['policy_id'] == source['id']]
    assert next(value for value in goal['people'] if value['person_id'] == person)['expected'] == 7


def test_bound_replacement_preview_reassembles_once_and_blocks_missing_publication_context(h):
    source = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    published = h.shift(h.workspace('Published before conversion'), people=[h.seed['people'][0]])['workspace']
    h.publish(published)
    draft = h.workspace('Current prospective check')
    convert(h)
    current = next(entry['rule'] for entry in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                   if entry['id'] == 'workload_limit')
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers']['max']['value'] = 3
    waiting = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': lines,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'reason': 'Preview a reviewed replacement limit'})['record']
    candidate = {**waiting, 'status': 'active', 'active': True}
    with transaction(h.app.state.engine) as db:
        before = domain_snapshot(db, db.get(m.Workspace, draft['id']))
        after = domain_snapshot(db, db.get(m.Workspace, draft['id']), policy_overrides=[candidate])
        with pytest.raises(Problem) as refused:
            domain_snapshot(db, db.get(m.Workspace, published['id']), policy_overrides=[candidate])
        assert refused.value.status == 409 and refused.value.code == 'BOUND_PREVIEW_CONTEXT'
        assert 'current draft' in refused.value.message
        historical = domain_snapshot(db, db.get(m.Workspace, published['id']))
    [old] = [value for value in active_rules(before) if value['id'] == source['id']]
    [changed] = [value for value in active_rules(after) if value['id'] == source['id']]
    assert old['definition']['rule']['maximum'] == 5
    assert changed['definition']['rule']['maximum'] == 3 and changed['revision'] > old['revision']
    assert len(active_rules(after)) == len(active_rules(before)) == 2  # The visible workload goal also remains.
    [saved] = [value for value in active_rules(historical) if value['id'] == source['id']]
    assert saved['definition']['rule']['maximum'] == 5
    # Previewing has no lifecycle side effects.
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current['id']).status == 'active'
        assert db.get(m.Policy, waiting['id']).status == 'proposed'


@pytest.mark.parametrize('capture_after_conversion', [False, True])
def test_bound_replacement_preview_preserves_safety_and_unrelated_captured_rules(h, capture_after_conversion):
    from tests.test_rule_lines import holds

    limit = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.policy({'kind': 'min_rest', 'hours': 12})
    draft = None if capture_after_conversion else h.workspace('Captured before conversion')
    convert(h)
    if capture_after_conversion:
        draft = h.workspace('Captured after conversion')
    entries = h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
    current = next(entry['rule'] for entry in entries if entry['id'] == 'duty_spacing')
    workload = next(entry['rule'] for entry in entries if entry['id'] == 'workload_limit')
    # A later independent change must not replace this draft's unrelated captured limit.
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.get(m.Policy, workload['id'])
        definition = copy.deepcopy(policy.definition)
        definition['rule']['lines'][0]['numbers']['max']['value'] = 9
        policy.definition = definition
        policy.revision += 1
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines.append({'id': str(uuid4()), 'who': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [h.seed['people'][0]]}]},
        'numbers': {'min': {'value': 1, 'level': 'mandatory'}},
        'settings': copy.deepcopy(lines[0].get('settings') or {})})
    waiting = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duty_spacing', 'catalogue_version': 2, 'lines': lines,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'reason': 'Preview a personal rest line without weakening the default'})['record']
    with transaction(h.app.state.engine) as db:
        after = domain_snapshot(db, db.get(m.Workspace, draft['id']),
            policy_overrides=[{**waiting, 'status': 'active', 'active': True}])
    resolved = active_rules(after)
    [captured] = [policy for policy in resolved if policy['id'] == limit['id']]
    assert captured['definition']['rule']['maximum'] == 5
    rest = [policy for policy in resolved if policy['kind'] == 'min_rest']
    assert rest and min(policy['definition']['rule']['hours'] for policy in rest) == 1
    assert max(policy['definition']['rule']['hours'] for policy in rest) == 12
    assert all(holds(after, policy, h.seed['people'][0], draft['starts_on']) for policy in rest)


@pytest.mark.parametrize('entry,source_rule,key,proposed_value,people,expected_kind,expected_value', [
    ('duty_spacing', {'kind': 'min_rest', 'hours': 12}, 'min', 8, [0], 'min_rest', 12),
    ('duty_spacing', {'kind': 'min_rest', 'hours': 12}, 'min', 14, [0], 'min_rest', 14),
    ('people_per_session', {'kind': 'maximum_participants', 'maximum': 2}, 'max', 3, [0, 1, 2],
     'maximum_participants', 2),
])
@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True,
                         ids=['sqlite', 'postgres'])
def test_bound_preview_keeps_captured_safety_stricter_than_proposed(
        h, entry, source_rule, key, proposed_value, people, expected_kind, expected_value):
    from apps.api.snapshots import domain_snapshot
    from tests.test_rule_catalogue_v2_api import proposed

    source = h.policy(source_rule)
    draft = h.shift(h.workspace('Captured safety preview'), people=[h.seed['people'][i] for i in people])['workspace']
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              change['id'], change['revision'])
    current = next(entry_data['rule'] for entry_data in h.get(
        'coordinator', '/api/rules/catalogue?version=2')['entries'] if entry_data['id'] == entry)
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    lines[0]['numbers'][key]['value'] = proposed_value
    waiting = h.command('coordinator', 'rule.propose', {
        'catalogue_id': entry, 'catalogue_version': 2, 'lines': lines,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'reason': 'Preview a proposed safety relaxation'})['record']
    with transaction(h.app.state.engine) as db:
        after = domain_snapshot(db, db.get(m.Workspace, draft['id']),
            policy_overrides=[{**waiting, 'status': 'active', 'active': True}])
    resolved = active_rules(after)
    safety = [policy for policy in resolved if policy['kind'] == expected_kind]
    assert safety and ((max if key == 'min' else min)(
        policy['definition']['rule'][{'min_rest': 'hours', 'maximum_participants': 'maximum'}[expected_kind]]
        for policy in safety) == expected_value)


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True,
                         ids=['sqlite', 'postgres'])
def test_bound_preview_keeps_only_captured_safety_lines_in_mixed_catalogue(h):
    from apps.api.snapshots import domain_snapshot
    from tests.test_rule_lines import holds
    from tests.test_rule_catalogue_v2_api import proposed

    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.policy({'kind': 'restriction', 'weekdays': [0]})
    draft = h.shift(h.workspace('Captured mixed workload preview'), people=[h.seed['people'][0]])['workspace']
    change = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              change['id'], change['revision'])
    current = next(entry['rule'] for entry in h.get(
        'coordinator', '/api/rules/catalogue?version=2')['entries'] if entry['id'] == 'workload_limit')
    lines = copy.deepcopy(current['definition']['rule']['lines'])
    max_line = next(line for line in lines if line.get('numbers', {}).get('max', {}).get('value') == 5)
    max_line['numbers']['max']['value'] = 6
    waiting = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'workload_limit', 'catalogue_version': 2, 'lines': lines,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'reason': 'Preview a proposed workload increase'})['record']
    with transaction(h.app.state.engine) as db:
        after = domain_snapshot(db, db.get(m.Workspace, draft['id']),
            policy_overrides=[{**waiting, 'status': 'active', 'active': True}])
    resolved = active_rules(after)
    maximums = [policy for policy in resolved if policy['kind'] == 'max_duties']
    assert [policy['definition']['rule']['maximum'] for policy in maximums] == [6]
    restrictions = [policy for policy in resolved if policy['kind'] == 'restriction']
    assert restrictions and all(holds(after, policy, h.seed['people'][0], draft['starts_on']) for policy in restrictions)
