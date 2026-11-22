"""The rc.12 department mix must retain the complete score, including its provenance."""
import copy
from collections import Counter

import pytest

from packages.contracts.rule_catalogue import CATALOGUE_BY_ID, RULE_CATALOGUE
from packages.domain.rule_catalogue_migration import after_snapshot, lines_of, plan_v2
from packages.domain.rule_migration_proof import strict_expanded, strict_proof
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate

from .test_rule_catalogue_v2_proof import fixture, source, uid


LINE_COUNTS = {'allocation_order': 7, 'companion_cover': 5, 'duties_per_month': 18,
    'duties_per_period': 148, 'overtime_offers': 22, 'preferences': 6, 'protected_time': 11,
    'session_units_per_week': 1, 'supervision': 6, 'days_in_a_row': 1, 'duties_per_week': 14,
    'fair_share': 5, 'keep_off': 24, 'kind_per_session': 20, 'not_after': 2, 'weekends_in_a_row': 1}
EMPTY_TYPES = [entry['id'] for entry in RULE_CATALOGUE if entry['id'] not in LINE_COUNTS][:9]


def department_mix(h):
    """27 synthetic rules: DEV's 16 used types / 291 lines / two legacy sources, plus nine empty types.

    The nine unused types are synthetic configured inventory, not DEV's active-rule count.

    The weekly target retains an editable default after its operator prerequisite is
    resolved. Blank person amounts still inherit it; conversion must not freeze them.
    """
    data = fixture(h, people=144, sessions=7)
    people, typ = list(data['people']), h.seed['type']
    service, rank = h.seed['services'][0], h.seed['category']
    policies = []
    for group, (entry_id, count) in enumerate(LINE_COUNTS.items()):
        entry = CATALOGUE_BY_ID[entry_id]
        lines = []
        for n in range(count):
            line = {'id': uid(80000 + group * 1000 + n), **copy.deepcopy(entry['default_line']),
                'settings': copy.deepcopy(entry['default_settings']), 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
            if entry_id in {'duties_per_month', 'duties_per_period', 'overtime_offers'}:
                line['who'] = {'conditions': [{'field': 'person_ids', 'values': [people[n % len(people)]]}]}
            elif entry_id not in {'days_in_a_row', 'session_units_per_week', 'not_after', 'weekends_in_a_row'}:
                line['who'] = {'conditions': [{'field': 'category_ids', 'values': [rank]}]}
            if entry_id == 'duties_per_period':
                line['numbers'] = {'max': {'value': 4 + n % 4, 'level': 'mandatory'},
                                   'target': {'value': min(3 + n % 3, 4 + n % 4), 'level': 'high' if n % 2 else 'low'}}
                line['settings'] = {'block_quota': {'window_alignment': 'calendar_week', 'metric': 'duties',
                    'prorate_department_days': n % 3 == 0, 'rounding': 'nearest'}}
                if n % 3 == 0:
                    line['numbers'].pop('max')  # proration belongs to targets, never mandatory limits
                if n >= 122:
                    line.update(starts_on='2026-09-01', ends_on='2026-10-01')
            elif entry_id == 'session_units_per_week':
                line['numbers'] = {'min': {'value': 0, 'level': 'mandatory'}, 'max': {'value': 10, 'level': 'mandatory'}}
            elif entry_id == 'overtime_offers':
                line.update(weekdays=[n % 7], where={'conditions': [{'field': 'assignment_type_ids', 'values': [typ]},
                                                                  {'field': 'service_ids', 'values': [service]}]})
                line['settings'] = {'allocation_overtime': {'required_policy_ids': [uid(10000 + 6002)]}}
            elif entry_id == 'protected_time':
                if n < 10:
                    line['calendar_entry_id'] = uid(500)
                    line.update(starts_on='2026-09-01', ends_on='2026-10-01')
                else:
                    line['who'] = {'conditions': [{'field': 'person_ids', 'values': [people[27]]}]}
                line.update(weekdays=[n % 7], start_time='09:00', end_time='10:00')
            elif entry_id == 'keep_off':
                line['who'] = {'conditions': [{'field': 'person_ids', 'values': [people[n]]}]}
                line.update(weekdays=[n % 7], start_time='09:00', end_time='10:00')
            elif entry_id == 'companion_cover':
                line['settings'] = {'minimum_cover': {'when_person_ids': [people[n]]}}
            elif entry_id == 'not_after':
                line['settings'] = {'forbidden_transition': {'from_type_ids': [typ], 'to_type_ids': [typ]}}
            elif entry_id == 'preferences':
                line['numbers']['priority']['value'] = -1 if n % 2 else 1
            elif entry_id == 'fair_share':
                line['who'] = {'conditions': [{'field': 'person_ids', 'values': people[n * 20:(n + 1) * 20]}]}
                line['numbers'] = {'target': {'value': 4 + n, 'level': 'high' if n % 2 else 'medium'}}
                line['settings'] = {'balance_goal': {'metric': 'duties', 'aim': 'target', 'period': 'month'}}
            elif entry_id == 'duties_per_week' and n:
                line['who'] = {'conditions': [{'field': 'person_ids', 'values': [people[n]]}]}
                line['numbers']['max']['level'] = None  # inherit the broader dated line's strength
            lines.append(line)
        policies.append(source(6000 + group, {'kind': 'catalogue', 'id': entry_id, 'lines': lines},
                               merged_ids=[uid(70000 + group * 2), uid(70001 + group * 2)]))
    target = source(6200, {'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'session_units', 'comparison': 'target', 'amount': 7, 'window_alignment': 'calendar_week'},
        severity='advisory', strength='high', per_person=[{'person_id': people[n], 'amount': 6 if n in (22, 23) else None}
                                                       for n in range(38)])
    policies.append(target)
    # Safety kinds are always mandatory. Keep the legacy rows and their inherited floor.
    policies.append(source(6201, {'kind': 'minimum_cover', 'minimum': 1}, listed_only=True,
        per_person=[{'category_ids': [rank], 'service_ids': [service],
            'amount': 3.0 if n == 0 else None, 'weekdays': [n]} for n in range(3)]))
    policies.extend(source(6300 + n, {'kind': 'catalogue', 'id': entry_id, 'lines': []})
                    for n, entry_id in enumerate(EMPTY_TYPES))
    for policy in policies:
        policy['department_id'] = data['workspace']['department_id']
    # Planning reads ID order; snapshots and their legacy score retain database order.
    data['policies'] = list(reversed(policies))
    return data, target


@pytest.mark.parametrize('balance', [True, False])
def test_realistic_catalogue_conversion_preserves_the_complete_schedule_score(h, balance):
    data, _ = department_mix(h)
    assert set(LINE_COUNTS) | set(EMPTY_TYPES) <= set(CATALOGUE_BY_ID)
    assert len(data['people']) == 144
    assert len(data['policies']) == 27
    catalogue_rules = [p['definition']['rule'] for p in data['policies'] if p['kind'] == 'catalogue']
    assert Counter(rule['id'] for rule in catalogue_rules) == Counter([*LINE_COUNTS, *EMPTY_TYPES])
    assert sum(not rule['lines'] for rule in catalogue_rules) == 9
    assert sum(len(p['definition']['rule'].get('lines') or []) for p in data['policies']) == 291
    for policy in data['policies']:
        policy['definition']['balance_between_people'] = balance
    plan = plan_v2(sorted(data['policies'], key=lambda p: p['id']),
                   week_start=data['organisation']['week_start'], department_id=data['workspace']['department_id'])
    assert not plan['blocked'], plan['blocked']
    after = after_snapshot(data, plan)
    before_score = schedule_score(copy.deepcopy(data), evaluate(copy.deepcopy(data)), data['generation_options'])
    after_score = schedule_score(copy.deepcopy(after), evaluate(copy.deepcopy(after)), after['generation_options'])
    assert before_score['contributions'] and before_score['people']
    assert before_score == after_score
    proof = strict_proof(data, after)
    assert proof['identical'], {key: proof[key]['equal'] for key in
                              ('expanded', 'findings', 'quota', 'fairness', 'coverage', 'validation', 'score', 'model')}


def test_inherited_default_blocker_names_every_dependent_person_and_source_line(h):
    data, target = department_mix(h)
    target['definition']['listed_only'] = True
    target['definition']['scope']['conditions'] = {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': list(data['people'])[:38]}]}
    plan = plan_v2(data['policies'], department_id=data['workspace']['department_id'])
    [blocked] = [item for item in plan['blocked'] if item['rule_id'] == target['id']]
    inheriting = [n for n, row in enumerate(target['definition']['per_person']) if row['amount'] is None]
    assert blocked['reason'].startswith('Not in the catalogue yet: ')
    assert set(blocked['person_ids']) == {target['definition']['per_person'][n]['person_id'] for n in inheriting}
    source_lines = lines_of(target)[1]
    assert blocked['line_ids'] == [source_lines[n]['id'] for n in inheriting]


def test_mixed_error_kinds_have_the_same_gate_after_source_reordering(h):
    data = fixture(h, people=22, sessions=2)
    person = next(iter(data['people']))
    for shift in data['shifts']:
        shift['participants'][0]['person_id'] = person
    data['policies'] = [source(6502, {'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, who=person),
                        source(6501, {'kind': 'max_hours', 'hours': 1, 'window': 'week'}, who=person)]
    for policy in data['policies']:
        policy['department_id'] = data['workspace']['department_id']
    plan = plan_v2(sorted(data['policies'], key=lambda p: p['id']),
                   week_start=data['organisation']['week_start'], department_id=data['workspace']['department_id'])
    after = after_snapshot(data, plan)
    before_validation = evaluate(copy.deepcopy(data))
    assert len({row['code'] for row in before_validation['findings'] if row['severity'] == 'error'}) == 2
    assert schedule_score(data, before_validation) == schedule_score(after, evaluate(copy.deepcopy(after)))
    assert strict_proof(data, after)['identical']


@pytest.mark.parametrize('comparison, line_number', [('minimum', None), ('maximum', None), ('target', None),
    ('range', None), ('maximum', 'amount'), ('target', 'amount')])
def test_legacy_quota_rows_keep_their_primary_and_selected_secondary_amount(comparison, line_number):
    rule = {'kind': 'block_quota', 'metric': 'duties', 'comparison': comparison, 'amount': 2,
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}
    if comparison == 'range':
        rule.update(maximum_amount=8, target_amount=5, target_strength='high')
    rows = [{'person_id': uid(10), 'amount': None}, {'person_id': uid(11), 'amount': 3}]
    if comparison == 'range':
        rows[1]['settings'] = {'maximum_amount': 6, 'target_amount': 4}
    policy = source(6600, rule, severity='advisory' if comparison == 'target' else 'mandatory', line_number=line_number,
                    per_person=rows)
    before = {'policies': [policy]}
    plan = plan_v2(before['policies'], department_id=policy['department_id'])
    assert not plan['blocked'], plan['blocked']
    assert strict_expanded(before, after_snapshot(before, plan))['equal']


@pytest.mark.parametrize('severity', ['mandatory', 'advisory'])
def test_legacy_cover_row_uses_the_resolved_minimum_or_advisory_target_slot(severity):
    policy = source(6700, {'kind': 'minimum_cover', 'minimum': 1}, severity=severity, strength='high',
        listed_only=True, per_person=[{'category_ids': [uid(12)], 'amount': 3.0},
                                      {'category_ids': [uid(13)], 'amount': None}])
    before = {'policies': [policy]}
    plan = plan_v2(before['policies'], department_id=policy['department_id'])
    if severity == 'advisory':
        # Safety kinds are always mandatory. Converting this hard minimum to a target would weaken it.
        [blocked] = plan['blocked']
        assert blocked['rule_id'] == policy['id'] and not plan['targets']
        return
    assert not plan['blocked'], plan['blocked']
    assert strict_expanded(before, after_snapshot(before, plan))['equal']
