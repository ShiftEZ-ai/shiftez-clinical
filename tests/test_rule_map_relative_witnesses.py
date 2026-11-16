"""Clinic-dependent missing skills remain possible people and are narrowed for each actual duty."""
import pytest

from packages.domain.conditions import active_rules, counting_views
from packages.domain.rule_map import rule_map
from packages.domain.time import aware
from packages.domain.validation import policy_applies
from .test_rule_conditions import data as person_data
from .test_rule_map import HOURS, U, entry, rule


def snapshot(grant='clinic'):
    source, skill, clinics = person_data(), U('presence-skill'), [U('presence-clinic-0'), U('presence-clinic-1')]
    source['organisation']['week_start'] = 6
    source['workspace'] = {'id': U('presence-rota'), 'department_id': 'd', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    source['people'] = {'p': source['people']['p']}
    source['memberships'] = [row for row in source['memberships'] if row['person_id'] == 'p']
    source['capabilities'] = {skill: {'active': True}}
    source['services'] = {clinic: {'id': clinic, 'department_id': 'd'} for clinic in clinics}
    source['grants'] = [{'person_id': 'p', 'department_id': 'foreign' if grant == 'foreign' else 'd',
                         'capability_id': skill, 'service_id': clinics[0] if grant == 'clinic' else None,
                         'starts_on': '2020-01-01'}] if grant != 'none' else []
    source['shifts'] = []
    for day in (4, 6):
        for clinic in clinics:
            start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
            shift = {'id': U(f'{day}-{clinic}'), 'assignment_type_id': 'T', 'location_id': 'room',
                     'service_ids': [clinic], 'start': start, 'end': end,
                     'participants': [{'person_id': 'p', 'start': start, 'end': end}]}
            source['shifts'].append(shift)
    source['focus_ids'] = [shift['id'] for shift in source['shifts']]
    source['policies'] = []
    return source, skill


@pytest.mark.parametrize('grant', ['clinic', 'global', 'foreign', 'none'])
@pytest.mark.parametrize('op', ['empty', 'not_empty', 'none', 'is_not'])
def test_skill_presence_and_exclusion_map_matches_each_duty(grant, op):
    source, skill = snapshot(grant)
    condition = {'field': 'capability_ids', 'op': op}
    if op in {'none', 'is_not'}:
        condition['value'] = {'kind': 'absolute', 'value': [skill]}
    policy = rule(source, 'Skill presence', HOURS, {'conditions': {'conditions': [condition]}})
    source['policies'] = [policy]
    result = entry(rule_map(source), policy['id'])
    expected = [index for index, shift in enumerate(source['shifts'])
                if policy_applies(source, policy, 'p', shift, aware(shift['start']), aware(shift['end']))]
    assert result['sessions'] == expected
    if grant in {'clinic', 'foreign'}:
        assert result['people']['p'] == [[0, 7]]
        assert result['not_here'] == ({'p': [index for index in range(4) if index not in expected]}
                                      if len(expected) < 4 else {})


@pytest.mark.parametrize('op', ['empty', 'not_empty'])
@pytest.mark.parametrize('relative', [False, True])
def test_date_scoped_skill_presence_map_uses_each_duty_local_day(op, relative):
    source, _ = snapshot()
    date_line = {'field': 'date', 'op': 'within' if relative else 'is',
                 'value': {'kind': 'relative', 'token': 'duty_day'} if relative else
                          {'kind': 'absolute', 'value': '2026-10-06'}}
    scope = {'conditions': {'conditions': [{'field': 'capability_ids', 'op': op}, date_line]}}
    policy = rule(source, 'Dated skill presence', HOURS, scope)
    source['policies'] = [policy]
    result = entry(rule_map(source), policy['id'])
    # Session scopes retain their complete session projection; not_here supplies the person-specific exclusion.
    assert result['sessions'] == (list(range(4)) if relative else [2, 3])
    expected = [index for index in result['sessions'] if
                (source['shifts'][index]['service_ids'][0] == list(source['services'])[0]) == (op == 'not_empty')]
    assert result['not_here'] == {'p': [index for index in result['sessions'] if index not in expected]}


@pytest.mark.parametrize('op', ['empty', 'not_empty'])
def test_skill_presence_in_override_sources_cannot_erase_the_other_clinic(op):
    source, _ = snapshot()
    base = rule(source, 'Base hours', HOURS)
    narrower = rule(source, 'Scoped hours', {'kind': 'max_hours', 'hours': 24},
                    {'conditions': {'conditions': [{'field': 'capability_ids', 'op': op}]}}, overrides=[base['id']])
    source['policies'] = [base, narrower]
    result = rule_map(source)
    copies = active_rules(source['policies'])
    for policy in source['policies']:
        copy_policy = next(copy for copy in copies if copy['id'] == policy['id'])
        expected = [index for index, shift in enumerate(source['shifts']) if
                    policy_applies({**source, 'policies': copies}, copy_policy, 'p', shift,
                                   aware(shift['start']), aware(shift['end']))]
        assert entry(result, policy['id'])['sessions'] == expected


def test_nested_count_bucket_skill_presence_keeps_exact_clinic_rechecks():
    source, _ = snapshot()
    policy = rule(source, 'Counted presence', HOURS, {'conditions': {'conditions': [
        {'field': 'capability_ids', 'op': 'empty'},
        {'field': 'service_ids', 'values': list(source['services']), 'count': 'each'}]}})
    source['policies'] = counting_views(source, [policy])
    result = entry(rule_map(source), policy['id'])
    assert result['sessions'] == [0, 1, 2, 3]
    assert result['people']['p'] == [[0, 7]]
    assert result['not_here'] == {'p': [0, 2]}
