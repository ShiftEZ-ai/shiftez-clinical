"""Complete score attribution names the exact copy and weight without changing the score summary."""
import uuid

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import active_rules
from packages.domain.score import schedule_score
from tests.test_rule_catalogue_guard import catalogue, line
from tests.test_schedule_score import coverage, data, policy, shift, warning

FIELDS = ('line', 'slot', 'rule_severity', 'rule_strength', 'rule_weight')


def assert_complete(result):
    assert round(sum(row['points'] for row in result['contributions']), 1) == round(100 - result['score'], 1)
    assert all(not set(FIELDS).intersection(row) for row in result['why'])
    assert all(not set(FIELDS).intersection(row) for person in result['people'].values() for row in person['issues'])


@pytest.mark.parametrize('entry,slot,value,component', [
    ('hours_per_week', 'max', 1, 'rules'),
    ('preferences', 'priority', -1, 'preferences'),
    ('days_off_together', 'min', 2, 'preferences'),
])
def test_two_clinic_copies_keep_their_exact_strength_and_line(entry, slot, value, component):
    services = [str(uuid.uuid4()), str(uuid.uuid4())]
    rule = catalogue(entry, *[line(where={'conditions': [{'field': 'service_ids', 'values': [service]}]},
                                  numbers={slot: {'value': value, 'level': strength}})
                              for service, strength in zip(services, ('low', 'high'))])
    rule['department_id'] = 'd'
    rule['definition'] = PolicyDefinition.model_validate(rule['definition']).model_dump(mode='json')
    shifts = [dict(shift('s1', 1, ['a']), service_ids=[services[0]]),
              dict(shift('s2', 2, ['a']), service_ids=[services[1]])]
    source = data(shifts, [rule])
    source['people'] = {'a': source['people']['a']}  # isolate source weighting from the audience denominator
    copies = [copy for copy in active_rules(source['policies']) if copy['id'] == rule['id']]
    assert len(copies) == 2
    findings = []
    if entry != 'preferences':
        for copy in copies:
            findings.append({**warning(rule['id'], 'a'), 'detail': {
                **{key: copy['template_line'][key] for key in ('line', 'slot')},
                'rule_severity': 'advisory', 'rule_strength': copy['definition']['strength']}})
    result = schedule_score(source, {'coverage': [], 'findings': findings}, {'scoring': {'weights': {
        'coverage': 0, 'rules': int(component == 'rules'), 'fairness': 0,
        'preferences': int(component == 'preferences'), 'stability': 0}}}, limit=1)
    assert result['score'] == 0
    assert {(row['line'], row['slot'], row['rule_severity'], row['rule_strength'], row['rule_weight'])
            for row in result['contributions']} == {(0, slot, 'advisory', 'low', 1), (1, slot, 'advisory', 'high', 10)}
    assert {row['rule_id'] for row in result['contributions']} == {rule['id']}
    assert [row['points'] for row in result['contributions']] == [90.9, 9.1]
    assert result['why'][-1]['component'] == 'other' and result['why'][-1]['points'] == 9.1
    assert_complete(result)


@pytest.mark.parametrize('comparison,slot,strength,component,expected_score', [
    ('maximum', 'max', 'critical', 'rules', 0),
    ('target', 'target', 'high', 'rules', 50),
])
def test_quota_row_provenance_overrides_the_first_policy_copy(comparison, slot, strength, component, expected_score):
    rule = policy('quota', strength='low')
    rule['definition']['rule'] = {'kind': 'block_quota'}
    source = data([shift('s1', 1, ['a'])], [rule])
    row = {'policy_id': rule['id'], 'policy_name': 'Quota', 'person_id': 'a',
           'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'line_id': str(uuid.uuid4()),
           'line': 4, 'slot': slot, 'severity': 'advisory', 'strength': strength, 'metric': 'duties',
           'comparison': comparison, 'status': 'above' if comparison == 'maximum' else 'below',
           'actual': 2 if comparison == 'maximum' else 1, 'amount': 1 if comparison == 'maximum' else 2,
           'difference': 1 if comparison == 'maximum' else -1}
    result = schedule_score(source, {'coverage': [], 'findings': [], 'quota_report': {'rows': [row]}},
                            {'scoring': {'weights': {'coverage': 0, 'rules': int(component == 'rules'),
                             'fairness': int(component == 'fairness'), 'preferences': 0, 'stability': 0}}})
    [contribution] = result['contributions']
    assert result['score'] == expected_score
    assert contribution['rule_strength'] == strength
    assert contribution['rule_weight'] == result['settings']['strengths'][strength]
    assert (contribution['line'], contribution['slot'], contribution['rule_severity']) == (4, slot, 'advisory')
    assert_complete(result)


def test_fairness_goal_uses_report_strength_without_guessing_a_line():
    rule = policy('balance', strength='low')
    rule['definition']['rule'] = {'kind': 'balance_goal', 'metric': 'duties'}
    report = {'policy_id': 'balance', 'name': 'Balance', 'strength': 'critical', 'score': 50, 'unit': 'duties',
              'people': [{'person_id': 'a', 'actual': 1, 'expected': 2, 'cost': 0.5}], 'pairs': []}
    source = data([shift('s1', 1, ['a'])], [rule])
    source['people'] = {'a': source['people']['a']}  # isolate the configured goal from built-in workload spread
    result = schedule_score(source, {'coverage': [], 'findings': [], 'goals': [report]})
    [contribution] = result['contributions']
    assert (contribution['rule_id'], contribution['rule_severity'], contribution['rule_strength'], contribution['rule_weight']) == (
        'balance', 'advisory', 'critical', 30)
    assert contribution['line'] is None and contribution['slot'] is None
    assert result['score'] == 37.5  # the shared mean-cost and worst-person calculation
    assert_complete(result)


@pytest.mark.parametrize('reported', [False, True])
def test_grouped_quota_losses_keep_each_persons_source_lines_and_strengths(reported):
    from packages.domain.fairness import quota_goal_reports

    rule = policy('quota')
    rule['definition']['rule'] = {'kind': 'block_quota'}
    source = data([shift('s1', 1, ['a'])], [rule])
    rows = [{'policy_id': rule['id'], 'policy_name': 'Quota', 'person_id': pid,
             'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'line_id': str(uuid.uuid4()),
             'line': number, 'slot': 'target', 'severity': 'advisory', 'strength': level, 'metric': 'duties',
             'comparison': 'target', 'status': 'above_target' if actual else 'below_target',
             'actual': actual, 'amount': 2}
            for number, (pid, actual, level) in enumerate((('a', 4, 'high'), ('a', 4, 'low'), ('b', 0, 'critical')))]
    validation = {'coverage': [], 'findings': [], 'quota_report': {'rows': rows}}
    if reported:
        validation['goals'] = quota_goal_reports(source, validation['quota_report'])
    result = schedule_score(source, validation, {'scoring': {'weights': {
        'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}})
    contributions = [row for row in result['contributions'] if row['rule_id'] == rule['id']]
    assert {(row['person_id'], row['line'], row['slot'], row['rule_strength'], row['rule_weight'])
            for row in contributions} == {('a', 0, 'target', 'high', 10), ('a', 1, 'target', 'low', 1),
                                          ('b', 2, 'target', 'critical', 30)}
    assert all(row['rule_severity'] == 'advisory' for row in contributions)
    assert all(row['component'] == 'rules' for row in contributions)
    assert result['score'] == 0 and sum(row['points'] for row in contributions) == 100
    assert_complete(result)


def test_seeded_workload_is_attributed_and_ruleless_components_are_unattributed():
    source = data([shift('s1', 1, ['a']), shift('s2', 2, ['a']), shift('s3', 3, ['b']), shift('s4', 4, [])])
    source['workspace'] = {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-10-08'}
    source['wishes'] = [{'id': 'wish', 'person_id': 'a', 'kind': 'prefer_off',
                         'first_day': '2026-10-01', 'last_day': '2026-10-01', 'weight': 1}]
    result = schedule_score(source, {'coverage': [coverage('s4', 4, 1, 0)], 'findings': []}, baseline={('s1', 'b')})
    assert {row['component'] for row in result['contributions']} == {'coverage', 'fairness', 'preferences', 'stability'}
    for row in result['contributions']:
        if row['component'] == 'fairness':
            assert (row['rule_id'], row['line'], row['slot'], row['rule_weight']) == ('workload', 0, 'apply', 3)
        else:
            assert row['rule_id'] is None and all(row[field] is None for field in FIELDS)
    assert_complete(result)
