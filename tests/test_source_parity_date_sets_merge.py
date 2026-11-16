"""Date sets and frozen usual providers compose without leaking cache matches between duties."""
import copy
from zoneinfo import ZoneInfo

import pytest

from packages.contracts.schemas import CONDITION_FIELD_DEFINITIONS, PolicyDefinition, SESSION_SCOPE_FIELDS
from packages.domain.conditions import active_rules, conditions_match
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from packages.domain.rule_map import reach_details, rule_map
from packages.domain.time import aware
from packages.domain.validation import evaluate
from .test_date_set_conditions import SET, uid
from .test_rule_catalogue_guard import catalogue, line


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_combined_date_and_usual_scope_reaches_only_the_matching_duty(h, engine):
    person, provider, other_provider = h.seed['people']
    workspace = h.workspace()
    for day in (4, 5):
        for hour in (8, 13):
            workspace = h.shift(workspace, f'2026-10-{day:02}T{hour:02}:00:00+03:00',
                f'2026-10-{day:02}T{hour + 4:02}:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    expected = None
    for shift in data['shifts']:
        local = aware(shift['start']).astimezone(ZoneInfo(data['organisation']['timezone']))
        lead = provider if local.hour == 13 else other_provider
        shift['series_snapshot'] = {'definition': {'default_person_ids': [lead]}}
        if local.date().isoformat() == '2026-10-05' and local.hour == 13:
            expected = shift['id']
    assert expected is not None
    data['date_sets'] = {SET: {'id': SET, 'name': 'Synthetic selected days', 'entries': [{
        'id': uid('combined-entry'), 'name': 'Synthetic selected day', 'repeat': 'once',
        'first_day': '2026-10-05', 'last_day': '2026-10-05'}]}}
    scope = {'match': 'all', 'conditions': [
        {'field': 'date_set_ids', 'op': 'all', 'values': [SET]},
        {'field': 'usual_assignee_ids', 'op': 'all', 'values': [provider]}]}
    policy = catalogue('hours_per_period', line(where=scope, numbers={'max': {'value': 1, 'level': 'high'}},
        settings={'max_hours': {'window': 'day'}}))
    policy['department_id'] = h.seed['department_id']
    policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    data['policies'] = [policy]
    assert {row['key'] for row in CONDITION_FIELD_DEFINITIONS} >= {'date_set_ids', 'usual_assignee_ids'}
    assert {'date_set_ids', 'usual_assignee_ids'} <= SESSION_SCOPE_FIELDS
    assert {s['id'] for s in data['shifts'] if conditions_match(data, scope, person, s,
        aware(s['start']), aware(s['end']))} == {expected}
    mapped = rule_map(copy.deepcopy(data))
    [row] = mapped['rules']
    assert {mapped['shifts'][n] for n in row['sessions']} == {expected}
    a, b = aware('2026-10-04T00:00:00+03:00'), aware('2026-10-11T00:00:00+03:00')
    details = reach_details(copy.deepcopy(data), active_rules([policy], context=data), a, b)
    [detail] = details[policy['id']]['lines']
    assert [row['service_id'] for row in detail['clinics']] == [h.seed['services'][0]]
    person_row = next(row for row in detail['people'] if row['person_id'] == person)
    assert [row['actual'] for row in person_row['counts']] == [4]
    validation = evaluate(copy.deepcopy(data))
    assert len([f for f in validation['findings'] if f['rule_id'] == policy['id']]) == 1
    result = engine(data, 5)
    assert result['status'] == 'candidate_feasible', result
    assert len([f for f in result['validation']['findings'] if f['rule_id'] == policy['id']]) == 1
