"""Fractional advisory count targets keep integer bounds and shared scoring."""
from copy import deepcopy
from uuid import UUID

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from packages.contracts.schemas import BlockQuota, PolicyDefinition
from packages.domain.cp_sat_quotas import add_quotas
from packages.domain.fairness import quota_goal_reports
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from .test_quota_targets import rule


@pytest.mark.parametrize('metric', ['duties', 'lead_turns'])
@pytest.mark.parametrize('value,expected', [(0, 0), (32 / 7, 4.57), (1.005, 1.01), (100000, 100000)])
def test_count_targets_store_two_decimal_places(metric, value, expected):
    target = BlockQuota.model_validate(rule(metric=metric, comparison='target', amount=value))
    assert target.amount == expected
    combined = BlockQuota.model_validate(rule(metric=metric, amount=0, target_amount=value))
    assert combined.target_amount == expected


@pytest.mark.parametrize('metric', ['duties', 'lead_turns'])
@pytest.mark.parametrize('changes', [
    {'comparison': 'minimum', 'amount': 1.5},
    {'comparison': 'maximum', 'amount': 1.5},
    {'comparison': 'range', 'amount': 1, 'maximum_amount': 2.5},
])
def test_fractional_targets_do_not_relax_count_bounds(metric, changes):
    with pytest.raises(ValidationError, match='whole-number'):
        BlockQuota.model_validate(rule(metric=metric, **changes))


@pytest.mark.parametrize('field', ['amount', 'target_amount'])
@pytest.mark.parametrize('value', [-0.001, 100000.001, float('nan'), float('inf')])
def test_fractional_targets_reject_invalid_amounts_before_rounding(field, value):
    changes = {'comparison': 'target', 'amount': value} if field == 'amount' else {'amount': 0, field: value}
    with pytest.raises(ValidationError):
        BlockQuota.model_validate(rule(**changes))


def test_fractional_target_is_advisory_and_cannot_grant_exceptions():
    for changes in ({'severity': 'mandatory'}, {'severity': 'advisory', 'exception_allowed': True}):
        with pytest.raises(ValidationError, match='quota target is advisory'):
            PolicyDefinition.model_validate({'rule': rule(comparison='target', amount=4.57), **changes})


@pytest.mark.parametrize('version', [1, 2])
def test_catalogue_target_storage_and_resolved_count_share_two_decimal_places(version):
    from packages.domain.conditions import active_rules

    definition = {'rule': {'kind': 'catalogue', 'catalogue_version': version,
        'id': 'duties_per_week' if version == 1 else 'workload_limit',
        'settings': {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                                    'window_alignment': 'calendar_week'}},
        'lines': [{'id': str(UUID(int=42)), 'numbers': {'target': {'value': 32 / 7, 'level': 'medium'}}}]}}
    stored = PolicyDefinition.model_validate(definition).model_dump(mode='json')
    assert stored['rule']['lines'][0]['numbers']['target']['value'] == 4.57
    policy = {'id': str(UUID(int=43)), 'department_id': 'd', 'status': 'active', 'active': True,
              'starts_on': '2026-10-04', 'ends_on': None, 'definition': stored}
    assert active_rules([policy])[0]['definition']['rule']['amount'] == 4.57


def test_person_target_lines_store_the_same_normalized_numbers():
    person = str(UUID(int=44))
    target = PolicyDefinition.model_validate({'severity': 'advisory', 'rule': rule(comparison='target', amount=1),
        'per_person': [{'person_id': person, 'amount': 32 / 7}]})
    assert target.per_person[0].amount == 4.57
    combined = PolicyDefinition.model_validate({'rule': rule(amount=0),
        'per_person': [{'person_id': person, 'settings': {'target_amount': 32 / 7}}]})
    assert combined.per_person[0].settings.target_amount == 4.57


def test_v2_typed_target_proposal_reload_approval_and_report_keep_normalized_number(h):
    from .test_rule_catalogue_api import approve, propose
    from .test_rule_catalogue_v2_api import proposed

    source = h.policy(rule(comparison='target', amount=4, starts_on='2026-10-04', ends_on='2026-10-11',
                           window_alignment='calendar_week'), severity='advisory')
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent synthetic conversion review'},
              conversion['id'], conversion['revision'])
    current = next(entry['rule'] for entry in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                   if entry['id'] == 'workload_limit')
    lines = deepcopy(current['definition']['rule']['lines'])
    target = next(line for line in lines if line['source_bindings']['target']['policy_id'] == source['id'])
    bindings, settings = deepcopy(target['source_bindings']), deepcopy(target['settings'])
    target.update(measure='duties', period={'kind': 'calendar_week'},
                  starts_on='2026-10-04', ends_on='2026-10-11')
    target['numbers']['target']['value'] = 32 / 7
    saved = propose(h, 'workload_limit', lines, catalogue_version=2,
                    settings=current['definition']['rule']['settings'],
                    policy_id=current['id'], revision=current['revision'])['record']
    reloaded = next(entry['proposed'] for entry in h.get('approver', '/api/rules/catalogue?version=2')['entries']
                    if entry['id'] == 'workload_limit')
    approved = approve(h, saved)
    for record in (saved, reloaded, approved):
        stored = next(line for line in record['definition']['rule']['lines'] if line['id'] == target['id'])
        assert stored['numbers']['target']['value'] == 4.57
        assert stored['source_bindings'] == bindings and stored['settings'] == settings
        assert (stored['starts_on'], stored['ends_on']) == ('2026-10-04', '2026-10-11')
    workspace = h.shift(h.workspace())['workspace']
    validation = h.detail(workspace)['workspace']['validation']
    row = next(row for row in validation['quota_report']['rows']
               if row['policy_id'] == source['id'] and row['person_id'] == h.seed['people'][0])
    assert (row['amount'], row['actual'], row['severity']) == (4.57, 1, 'advisory')
    assert (row['starts_on'], row['ends_on']) == ('2026-10-04', '2026-10-11')


@pytest.mark.parametrize('kind', ['workload', 'target'])
def test_personal_scheduling_fact_persists_its_normalized_target(h, kind):
    person = h.seed['people'][0]
    payload = {'kind': kind, 'person_id': person, 'catalogue_id': 'workload_limit',
        'reason': 'Synthetic fractional personal target',
        'line': {'id': str(UUID(int=45)), 'numbers': {'target': {'value': 32 / 7, 'level': 'medium'}},
                 'settings': {'block_quota': {'metric': 'duties', 'starts_on': '2026-10-04',
                                             'ends_on': '2026-10-11', 'window_alignment': 'calendar_week'}}}}
    proposed = h.command('coordinator', 'person.workload.propose', payload)['fact']
    assert proposed['data']['line']['numbers']['target']['value'] == 4.57
    approved = h.command('approver', 'person.workload.activate', {'reason': 'Independent synthetic review'},
                         proposed['id'], proposed['revision'])['fact']
    assert approved['data']['line']['numbers']['target']['value'] == 4.57
    validation = evaluate(h.snapshot(h.workspace()))
    row = next(row for row in validation['quota_report']['rows'] if row['policy_id'] == approved['id'])
    assert row['amount'] == 4.57 and row['severity'] == 'advisory'


def test_fractional_target_api_reports_score_and_cp_sat_use_the_same_target(h):
    person = h.seed['people'][0]
    saved = h.policy(rule(comparison='range', amount=1, maximum_amount=5, target_amount=32 / 7,
                          starts_on='2026-10-04', ends_on='2026-10-11'), {'person_id': person})
    assert saved['definition']['rule']['target_amount'] == 4.57
    workspace = h.workspace()
    for day in range(4, 9):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    validation = evaluate(data)
    rows = [row for row in validation['quota_report']['rows'] if row['policy_id'] == saved['id']]
    target = next(row for row in rows if row['comparison'] == 'target')
    assert (target['amount'], target['actual'], target['difference'], target['severity']) == (4.57, 5, 0.43, 'advisory')
    goal = next(goal for goal in validation['goals'] if goal['policy_id'] == saved['id'])
    assert goal['people'][0]['expected'] == 4.57
    fallback = quota_goal_reports(data, {'rows': rows})[0]
    assert fallback['score'] == goal['score']
    score = schedule_score(data, validation)
    assert any('4.57' in item['text'] for item in score['contributions'] if item['rule_id'] == saved['id'])
    assert score['components']['fairness'] == schedule_score(
        data, {**validation, 'goals': [goal for goal in validation['goals'] if not goal.get('quota_target')]})['components']['fairness']
    model = cp_model.CpModel()
    variables = [model.new_bool_var(f'duty_{day}') for day in range(5)]
    by_person = {person: [(shift, shift['participants'][0], None, None, variable)
                         for shift, variable in zip(data['shifts'], variables)]}
    penalties = []
    def penalise(expression, policy, worst=1, bound=1):
        penalties.append(expression)
    assert add_quotas(model, data, by_person, penalise) == []
    model.minimize(sum(penalties))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    assert sum(solver.value(variable) for variable in variables) == 5


def test_fractional_prorated_target_override_round_trips_and_undo_restores_calculation(h):
    person = h.seed['people'][0]
    target = h.policy(rule(comparison='target', amount=16, prorate_department_days=True,
                          starts_on='2026-10-04', ends_on='2026-10-11'),
                      {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    result = h.command('coordinator', 'workspace.target_override', {
        'policy_id': target['id'], 'person_id': person, 'amount': 32 / 7,
        'reason': 'Synthetic fractional allocation'}, workspace['id'], workspace['revision'])
    assert result['workspace']['target_overrides'][0]['amount'] == 4.57
    reloaded = h.detail(result['workspace'])
    assert reloaded['workspace']['target_overrides'][0]['amount'] == 4.57
    row = next(row for row in result['validation']['quota_report']['rows'] if row['policy_id'] == target['id'])
    assert row['amount'] == 4.57 and row['target_calculation']['calculated_amount'] == 4
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Restore configured proration'},
                       workspace['id'], result['workspace']['revision'])
    assert undone['workspace']['target_overrides'] == []
    assert next(row for row in undone['validation']['quota_report']['rows'] if row['policy_id'] == target['id'])['amount'] == 4


def test_quota_report_keeps_retained_policy_rows_renderable():
    """Published quota evidence may outlive the active policy row."""
    from .test_schedule_score import data, shift

    source = data([shift('s1', 1, ['a'])])
    row = {'policy_id': 'retired-policy', 'policy_name': 'Retired target', 'person_id': 'a',
           'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
           'comparison': 'target', 'status': 'below', 'actual': 0, 'amount': 1,
           'strength': 'medium', 'balance_between_people': True}
    result = quota_goal_reports(source, {'rows': [row]})

    assert result[0]['policy_id'] == 'retired-policy'
    assert result[0]['audience_label'] == 'Configured group'
