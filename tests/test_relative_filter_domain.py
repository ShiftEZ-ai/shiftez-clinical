"""Relative operands use one calendar and evaluator for previews, lists and duties."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from packages.domain import time
from packages.domain.conditions import EvaluationContext, evaluate, parse_date_short_form, resolve_operand, rule_holds


def relative(token, **options):
    return {'kind': 'relative', 'token': token, **options}


def context(day='2026-10-18', **options):
    return EvaluationContext(timezone='Asia/Riyadh', week_start=0, today=date.fromisoformat(day), **options)


def tree(field, op, value=None):
    return {'match': 'all', 'conditions': [{'field': field, 'op': op, **({'value': value} if value is not None else {})}]}


def date_range(token, ctx=None, **options):
    resolved = resolve_operand(relative(token, **options), ctx or context(), 'date')
    assert resolved['label'] and resolved['display']
    assert resolved['value'] == [resolved['from'], resolved['to']]
    return resolved['from'], resolved['to']


@pytest.mark.parametrize('token,expected', [
    ('today', ('2026-12-31', '2026-12-31')),
    ('yesterday', ('2026-12-30', '2026-12-30')),
    ('tomorrow', ('2027-01-01', '2027-01-01')),
    ('this_week', ('2026-12-28', '2027-01-03')),
    ('last_week', ('2026-12-21', '2026-12-27')),
    ('next_week', ('2027-01-04', '2027-01-10')),
    ('this_month', ('2026-12-01', '2026-12-31')),
    ('last_month', ('2026-11-01', '2026-11-30')),
    ('next_month', ('2027-01-01', '2027-01-31')),
    ('this_quarter', ('2026-10-01', '2026-12-31')),
    ('last_quarter', ('2026-07-01', '2026-09-30')),
    ('next_quarter', ('2027-01-01', '2027-03-31')),
    ('this_year', ('2026-01-01', '2026-12-31')),
    ('last_year', ('2025-01-01', '2025-12-31')),
    ('next_year', ('2027-01-01', '2027-12-31')),
])
def test_calendar_tokens_cross_year_end(token, expected):
    assert date_range(token, context('2026-12-31')) == expected


@pytest.mark.parametrize('day,token,expected', [
    ('2028-02-29', 'this_month', ('2028-02-01', '2028-02-29')),
    ('2028-03-01', 'last_month', ('2028-02-01', '2028-02-29')),
    ('2026-03-31', 'next_month', ('2026-04-01', '2026-04-30')),
    ('2026-03-31', 'this_quarter', ('2026-01-01', '2026-03-31')),
    ('2026-04-01', 'last_quarter', ('2026-01-01', '2026-03-31')),
    ('2026-01-01', 'last_quarter', ('2025-10-01', '2025-12-31')),
    ('2028-02-29', 'this_year', ('2028-01-01', '2028-12-31')),
])
def test_calendar_month_and_quarter_boundaries(day, token, expected):
    assert date_range(token, context(day)) == expected


@pytest.mark.parametrize('week_start,expected', [
    (0, ('2026-10-12', '2026-10-18')),
    (6, ('2026-10-18', '2026-10-24')),
])
def test_week_start_is_department_configuration(week_start, expected):
    ctx = EvaluationContext(timezone='Asia/Riyadh', week_start=week_start, today=date(2026, 10, 18))
    assert date_range('this_week', ctx) == expected


@pytest.mark.parametrize('token,n,unit,day,expected', [
    ('next_n', 2, 'weeks', '2026-10-18', ('2026-10-18', '2026-10-31')),
    ('last_n', 2, 'weeks', '2026-10-18', ('2026-10-05', '2026-10-18')),
    ('next_n', 1, 'days', '2026-12-31', ('2026-12-31', '2026-12-31')),
    ('last_n', 1, 'days', '2026-01-01', ('2026-01-01', '2026-01-01')),
    ('next_n', 3, 'days', '2026-12-31', ('2026-12-31', '2027-01-02')),
    ('last_n', 3, 'days', '2026-01-01', ('2025-12-30', '2026-01-01')),
    ('next_n', 1, 'months', '2026-01-31', ('2026-01-31', '2026-02-27')),
    ('last_n', 1, 'months', '2026-03-31', ('2026-03-01', '2026-03-31')),
    ('next_n', 1, 'months', '2028-01-31', ('2028-01-31', '2028-02-28')),
])
def test_rolling_ranges_include_today(token, n, unit, day, expected):
    assert date_range(token, context(day), n=n, unit=unit) == expected


@pytest.mark.parametrize('zone,instant,expected', [
    ('Asia/Riyadh', '2026-10-17T20:59:00+00:00', '2026-10-17'),
    ('Asia/Riyadh', '2026-10-17T21:00:00+00:00', '2026-10-18'),
    ('America/New_York', '2026-03-08T04:59:00+00:00', '2026-03-07'),
    ('America/New_York', '2026-03-08T07:01:00+00:00', '2026-03-08'),
    ('America/New_York', '2026-11-01T05:30:00+00:00', '2026-11-01'),
    ('America/New_York', '2026-11-01T06:30:00+00:00', '2026-11-01'),
])
def test_today_uses_department_timezone_and_shared_clock(monkeypatch, zone, instant, expected):
    monkeypatch.setattr(time, 'now', lambda: datetime.fromisoformat(instant))
    ctx = EvaluationContext(timezone=zone, week_start=0)
    assert date_range('today', ctx) == (expected, expected)


def test_as_of_does_not_replace_today_for_calendar_tokens():
    assert date_range('today', context(as_of=date(2025, 1, 1))) == ('2026-10-18', '2026-10-18')


@pytest.mark.parametrize('token,options,expected', [
    ('duty_day', {}, ('2026-12-31', '2026-12-31')),
    ('duty_week', {}, ('2026-12-28', '2027-01-03')),
    ('duty_month', {}, ('2026-12-01', '2026-12-31')),
    ('days_before_duty', {'n': 3}, ('2026-12-28', '2026-12-31')),
    ('days_after_duty', {'n': 3}, ('2026-12-31', '2027-01-03')),
])
def test_duty_ranges_use_the_duty_day(token, options, expected):
    ctx = context('2020-01-01', duty_day=date(2026, 12, 31), context='rule')
    assert date_range(token, ctx, **options) == expected


@pytest.mark.parametrize('op,day,expected', [
    ('is', '2026-10-18', True), ('is', '2026-10-19', False),
    ('before', '2026-10-17', True), ('before', '2026-10-18', False),
    ('after', '2026-10-19', True), ('after', '2026-10-18', False),
    ('on_or_before', '2026-10-18', True), ('on_or_before', '2026-10-19', False),
    ('on_or_after', '2026-10-18', True), ('on_or_after', '2026-10-17', False),
])
def test_date_comparisons_have_inclusive_and_exclusive_boundaries(op, day, expected):
    assert evaluate(tree('starts_on', op, relative('today')), lambda field: set(), context(), row={'starts_on': day}) == expected


def test_between_mixes_relative_and_absolute_operands_and_normalises_range():
    operand = [{'kind': 'absolute', 'value': '2026-10-01'}, relative('today')]
    predicate = tree('ends_on', 'between', operand)
    before = deepcopy(predicate)
    assert [evaluate(predicate, lambda field: set(), context(), row={'ends_on': stored_end}) for stored_end in
            ('2026-10-01', '2026-10-02', '2026-10-19', '2026-10-20')] == [False, True, True, False]
    reverse = tree('ends_on', 'between', list(reversed(operand)))
    assert evaluate(reverse, lambda field: set(), context(), row={'ends_on': '2026-10-11'})
    assert predicate == before  # Saved operands stay unresolved and unchanged.


@pytest.mark.parametrize('reverse', [False, True])
def test_between_keeps_the_complete_range_when_one_endpoint_is_a_relative_range(reverse):
    endpoints = [relative('this_month'), {'kind': 'absolute', 'value': '2026-10-10'}]
    predicate = tree('starts_on', 'between', list(reversed(endpoints)) if reverse else endpoints)
    assert [evaluate(predicate, lambda field: set(), context(), row={'starts_on': day}) for day in
            ('2026-09-30', '2026-10-01', '2026-10-10', '2026-10-31', '2026-11-01')] == [False, True, True, True, False]


def test_within_accepts_calendar_range_and_keeps_absolute_literals_working():
    predicate = tree('starts_on', 'within', relative('next_n', n=2, unit='weeks'))
    assert [evaluate(predicate, lambda field: set(), context(), row={'starts_on': day}) for day in
            ('2026-10-17', '2026-10-18', '2026-10-31', '2026-11-01')] == [False, True, True, False]
    assert evaluate(tree('name', 'is', 'Example'), lambda field: set(), context(), row={'name': 'Example'})
    assert evaluate(tree('number', 'is', 0), lambda field: set(), context(), row={'number': 0})


@pytest.mark.parametrize('field', ['starts_on', 'ends_on', 'name', 'note', 'number', 'person_ids', 'category_ids',
                                   'service_ids', 'location_ids', 'tags'])
@pytest.mark.parametrize('value', [None, '', []])
def test_empty_works_for_every_kind_including_open_ended_relationships(field, value):
    row = {field: value}
    assert evaluate(tree(field, 'empty'), row.get, context(), row=row)
    assert not evaluate(tree(field, 'not_empty'), row.get, context(), row=row)


@pytest.mark.parametrize('field,value', [
    ('starts_on', '2026-10-18'), ('name', 'Name'), ('note', ' '), ('number', 0),
    ('person_ids', ['p']), ('category_ids', ['rank']), ('service_ids', ['clinic']), ('tags', ['night']),
])
def test_nonempty_does_not_treat_zero_or_whitespace_as_empty(field, value):
    row = {field: value}
    assert not evaluate(tree(field, 'empty'), row.get, context(), row=row)
    assert evaluate(tree(field, 'not_empty'), row.get, context(), row=row)


@pytest.mark.parametrize('token,field,selected', [
    ('me', 'person_ids', ['p']),
    ('people_i_approve', 'person_ids', ['p', 'q']),
    ('my_team', 'person_ids', ['q']),
    ('people_i_supervise', 'person_ids', ['q']),
    ('my_programme', 'programme_ids', ['programme']),
    ('my_cohort', 'cohort_ids', ['cohort']),
    ('my_rank', 'category_ids', ['rank']),
    ('my_clinics', 'service_ids', ['clinic']),
    ('my_clinics', 'location_ids', ['wing', 'room']),
    ('my_department', 'department_ids', ['department']),
    ('current_rota', 'rota_ids', ['current']),
    ('next_rota', 'rota_ids', ['next']),
    ('viewed_rota', 'rota_ids', ['viewed']),
])
def test_viewer_tokens_are_resolved_once_from_supplied_context(token, field, selected):
    values = {token: {field: selected}}
    ctx = context(values=values, allowed_tokens=frozenset({token}))
    predicate = tree(field, 'any', relative(token))
    assert evaluate(predicate, lambda key: {selected[-1]}, ctx)
    assert not evaluate(predicate, lambda key: {'unrelated'}, ctx)
    assert values == {token: {field: selected}}


def test_my_clinics_location_matches_descendants_through_shared_rule_holds_ancestry():
    data = {'organisation': {'timezone': 'Asia/Riyadh', 'week_start': 0},
            'locations': {'wing': {'parent_id': None}, 'room': {'parent_id': 'wing'},
                          'subroom': {'parent_id': 'room'}, 'other-wing': {'parent_id': None}}}
    ctx = context(values={'my_clinics': {'location_ids': ['wing']}},
                  allowed_tokens=frozenset({'my_clinics'}))
    predicate = tree('location_ids', 'any', relative('my_clinics'))
    for location, expected in [('subroom', True), ('other-wing', False)]:
        shift = {'start': '2026-10-18T08:00:00+03:00', 'location_id': location,
                 'service_ids': [], 'assignment_type_id': 'type'}
        assert evaluate(predicate, rule_holds(data, None, shift, None, None), ctx) == expected


def test_same_stored_operand_re_evaluates_for_each_viewer_and_day():
    predicate = {'conditions': [tree('person_ids', 'any', relative('me')),
                                tree('ends_on', 'within', relative('this_month'))]}
    stored = deepcopy(predicate)
    row = {'ends_on': '2026-10-31'}
    first = context(values={'me': ['p']}, allowed_tokens=frozenset({'me', 'this_month'}))
    other = context(values={'me': ['q']}, allowed_tokens=frozenset({'me', 'this_month'}))
    later = context('2026-11-03', values={'me': ['p']}, allowed_tokens=frozenset({'me', 'this_month'}))
    assert evaluate(predicate, lambda field: {'p'}, first, row=row)
    assert not evaluate(predicate, lambda field: {'p'}, other, row=row)
    assert not evaluate(predicate, lambda field: {'p'}, later, row=row)
    assert predicate == stored


@pytest.mark.parametrize('person,tags,expected', [('p', [], False), ('q', ['night'], False), ('p', ['night'], True)])
def test_row_with_many_people_cannot_combine_one_persons_identity_with_anothers_tags(person, tags, expected):
    ctx = context(values={'me': ['p']}, allowed_tokens=frozenset({'me'}))
    predicate = {'conditions': [tree('person_ids', 'any', relative('me')),
                                {'field': 'tags', 'op': 'any', 'values': ['night']}]}
    held = {'person_ids': {person}, 'tags': set(tags)}
    row = {'person_ids': ['p', 'q'], 'tags': ['night']}
    assert evaluate(predicate, held.get, ctx, row=row) == expected


@pytest.mark.parametrize('token,number', [('its_target', 12), ('department_average', 9.5)])
def test_numeric_tokens_use_the_requested_field_metric(token, number):
    ctx = context(values={token: {'number': number}}, allowed_tokens=frozenset({token}))
    assert evaluate(tree('number', 'is', relative(token)), lambda field: set(), ctx, row={'number': number})
    assert not evaluate(tree('number', 'is', relative(token)), lambda field: set(), ctx, row={'number': number + 1})


def test_its_target_is_resolved_for_each_row_without_caching_another_rows_target():
    ctx = context(allowed_tokens=frozenset({'its_target', 'department_average'}),
                  metrics={'number': {'average': 7.5}})
    predicate = tree('number', 'is', relative('its_target'))
    for number in (12, 0, 7.5):
        assert evaluate(predicate, lambda field: set(), ctx, row={'number': number, 'target': number})
    assert not evaluate(predicate, lambda field: set(), ctx, row={'number': 12, 'target': 0})
    assert evaluate(tree('number', 'is', relative('department_average')), lambda field: set(), ctx,
                    row={'number': 7.5})
    with pytest.raises(ValueError):
        evaluate(predicate, lambda field: set(), ctx, row={'number': 12})


@pytest.mark.parametrize('token', ['me', 'people_i_approve', 'my_clinics', 'its_target'])
def test_unavailable_viewer_or_metric_token_is_refused(token):
    with pytest.raises(ValueError):
        resolve_operand(relative(token), context(allowed_tokens=frozenset()), 'person_ids')


@pytest.mark.parametrize('operand', [
    relative('unknown'), relative('next_n', n=0, unit='days'), relative('last_n', n=-1, unit='weeks'),
    relative('next_n', n=2, unit='fortnights'), relative('last_n', unit='months'),
])
def test_invalid_tokens_and_units_are_refused_in_the_shared_resolver(operand):
    with pytest.raises(ValueError):
        resolve_operand(operand, context(), 'date')


@pytest.mark.parametrize('evaluation_context', ['rule', 'audience'])
@pytest.mark.parametrize('token', ['today', 'this_month', 'me', 'my_clinics', 'viewed_rota', 'department_average'])
def test_rule_and_audience_context_refuses_clock_and_viewer_tokens(evaluation_context, token):
    with pytest.raises(ValueError, match='duty|rule|audience'):
        resolve_operand(relative(token), context(context=evaluation_context, values={token: ['p']}))


def test_rule_holds_binds_relative_dates_to_each_duty_start():
    data = {'organisation': {'timezone': 'Asia/Riyadh', 'week_start': 6}, 'people': {'p': {}}}
    predicate = tree('date', 'is', relative('duty_day'))
    for start in ('2026-10-17T20:59:00+00:00', '2026-10-17T21:00:00+00:00'):
        shift = {'start': start, 'service_ids': [], 'assignment_type_id': 'type'}
        # Participant time may differ; the rule is always anchored to the original duty.
        holds = rule_holds(data, 'p', shift, datetime(2026, 10, 20, tzinfo=timezone.utc),
                           datetime(2026, 10, 21, tzinfo=timezone.utc))
        assert evaluate(predicate, holds)
        fixed = tree('date', 'is', {'kind': 'absolute', 'value': '2026-10-18'})
        assert evaluate(fixed, holds) == (start == '2026-10-17T21:00:00+00:00')


@pytest.mark.parametrize('query,operand,expected', [
    ('today', relative('today'), ('2026-10-18', '2026-10-18')),
    (' NEXT 2 WEEKS ', relative('next_n', n=2, unit='weeks'), ('2026-10-18', '2026-10-31')),
    ('-7d', {'kind': 'absolute', 'value': '2026-10-11'}, ('2026-10-11', '2026-10-11')),
    ('in 3 days', {'kind': 'absolute', 'value': '2026-10-21'}, ('2026-10-21', '2026-10-21')),
    ('oct', {'kind': 'absolute', 'value': ['2026-10-01', '2026-10-31']}, ('2026-10-01', '2026-10-31')),
])
def test_date_short_forms_return_ranked_operands_and_resolution(query, operand, expected):
    matches = parse_date_short_form(query, context())
    assert matches and matches[0]['operand'] == operand
    assert (matches[0]['from'], matches[0]['to']) == expected
    assert matches[0]['label'] and matches[0]['display']
    assert [match['rank'] for match in matches] == sorted(match['rank'] for match in matches)


@pytest.mark.parametrize('query', ['', 'not a date', 'next 0 weeks', '-999999999999d'])
def test_bad_short_forms_have_no_suggestion(query):
    assert parse_date_short_form(query, context()) == []


def test_legacy_stored_condition_shapes_keep_their_existing_set_semantics():
    predicate = {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'values': ['p']},
        {'field': 'tags', 'op': 'all', 'values': ['NIGHT', 'qualified']},
        {'field': 'category_ids', 'op': 'none', 'values': ['excluded']},
    ]}
    held = {'person_ids': {'p'}, 'tags': {'night', 'qualified'}, 'category_ids': {'rank'}}
    assert evaluate(predicate, held.get)
    assert evaluate(predicate, held.get, context())
    assert not evaluate(predicate, {**held, 'category_ids': {'excluded'}}.get, context())


@pytest.mark.parametrize('person_condition', ['legacy', 'absolute', 'is'])
def test_date_scoped_catalogue_rule_agrees_in_validator_map_score_and_solver(h, monkeypatch, person_condition):
    from zoneinfo import ZoneInfo
    from packages.contracts.schemas import PolicyDefinition
    from packages.domain.cp_sat import generate
    from packages.domain.rule_map import rule_map
    from packages.domain.score import schedule_score
    from packages.domain.time import aware
    from packages.domain.validation import evaluate as validate
    from .test_rule_catalogue_guard import catalogue, line

    person = h.seed['people'][0]
    workspace = h.workspace(start='2026-10-04', end='2026-10-09')
    for day in range(4, 8):
        workspace = h.shift(workspace, f'2026-10-{day:02}T08:00:00+03:00',
                            f'2026-10-{day:02}T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    where = {'conditions': [
        tree('date', 'between', [{'kind': 'absolute', 'value': '2026-10-05'},
                                 {'kind': 'absolute', 'value': '2026-10-06'}]),
        tree('date', 'within', relative('duty_week')),
    ]}
    who = ({'conditions': [{'field': 'person_ids', 'values': [person]}]} if person_condition == 'legacy' else
           tree('person_ids', 'any' if person_condition == 'absolute' else 'is',
                {'kind': 'absolute', 'value': person}))
    policy = catalogue('hours_per_period', line(
        who=who, where=where,
        numbers={'max': {'value': 1, 'level': 'high'}}, settings={'max_hours': {'window': 'day'}}))
    policy['department_id'] = h.seed['department_id']
    policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    data['policies'] = [policy]
    stored = deepcopy(policy)
    validation = validate(deepcopy(data))
    breaches = [finding for finding in validation['findings'] if finding['rule_id'] == policy['id']]
    assert len(breaches) == 2 and all(finding['severity'] == 'warning' for finding in breaches)
    assert {aware(finding['start']).astimezone(ZoneInfo('Asia/Riyadh')).date().isoformat()
            for finding in breaches} == {'2026-10-05', '2026-10-06'}
    mapped = rule_map(deepcopy(data))
    reached = next(rule for rule in mapped['rules'] if rule['id'] == policy['id'])
    assert {mapped['shifts'][index] for index in reached['sessions']} == {
        shift['id'] for shift in data['shifts'] if shift['start'][:10] in {'2026-10-05', '2026-10-06'}}
    assert reached['session_side'] and reached['person_side']
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    score = schedule_score(data, validation, options)
    assert score['components']['rules']['value'] == 0
    assert {item['rule_id'] for item in score['contributions']} == {policy['id']}
    data['generation_options'] = options
    solved = generate(deepcopy(data), 3, workers=1, deterministic=True)
    assert solved['status'] == 'candidate_feasible', solved
    selected_cost = next(item['value'] for item in solved['objectives'] if item['name'] == 'weighted')
    assert solved['compiled_component_values']['rules'] == solved['objective_components']['rules']['value'] == score['components']['rules']['value']
    assert solved['additions'] == solved['removals'] == []
    assert selected_cost == solved['weighted_tie_multiplier'] * solved['weighted_loss_scale'] + 4
    every_day = deepcopy(data)
    every_day['policies'][0]['definition']['rule']['lines'][0]['where'] = tree('date', 'within', relative('duty_week'))
    all_days = generate(every_day, 3, workers=1, deterministic=True)
    assert all_days['status'] == 'candidate_feasible', all_days
    # Weighted losses are normalized; twice as many affected days doubles findings, not the mean loss.
    assert len([finding for finding in all_days['validation']['findings'] if finding['rule_id'] == policy['id']]) == 2 * len(breaches)
    assert all_days['compiled_component_values']['rules'] == all_days['objective_components']['rules']['value'] == score['components']['rules']['value']
    assert next(item['value'] for item in all_days['objectives'] if item['name'] == 'weighted') == selected_cost
    monkeypatch.setattr(time, 'now', lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert validate(deepcopy(data))['findings'] == validation['findings']
    assert rule_map(deepcopy(data))['rules'] == mapped['rules']
    assert data['policies'] == [stored]


@pytest.mark.parametrize('first,second', [
    (tree('date', 'before', {'kind': 'absolute', 'value': '2026-10-05'}),
     tree('date', 'before', {'kind': 'absolute', 'value': '2026-10-06'})),
    (tree('date', 'is', relative('duty_day')), tree('date', 'is', relative('duty_week'))),
    (tree('service_ids', 'empty'), tree('service_ids', 'not_empty')),
])
def test_grouping_proof_keeps_distinct_scalar_relative_and_empty_conditions(first, second):
    from packages.domain.rule_groups import expanded
    from .test_rule_catalogue_guard import catalogue, line

    before = catalogue('keep_off', line(where=first, numbers={'apply': {'value': None}}))
    after = deepcopy(before)
    after['definition']['rule']['lines'][0]['where'] = second
    assert expanded([before]) != expanded([after])


def test_grouping_absolute_choices_keep_the_same_meaning_as_legacy_lists():
    from packages.domain.rule_groups import expanded
    from .test_rule_catalogue_guard import catalogue, line

    clinic = '00000000-0000-4000-8000-000000000001'
    before = catalogue('keep_off', line(where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
                                         numbers={'apply': {'value': None}}))
    after = deepcopy(before)
    after['definition']['rule']['lines'][0]['where'] = tree('service_ids', 'any', {'kind': 'absolute', 'value': clinic})
    assert expanded([before]) == expanded([after])


def test_rule_helpers_name_absolute_choices_and_keep_nonempty_reach_paths():
    from packages.domain.rule_groups import _listed_people
    from packages.domain.rule_map import _listed, _reach_record, named
    from packages.domain.validation import named_people

    person = tree('person_ids', 'any', {'kind': 'absolute', 'value': 'person-a'})
    clinic = tree('service_ids', 'is', {'kind': 'absolute', 'value': 'clinic-a'})
    scope = {'conditions': {'conditions': [person, clinic]}}
    for helper in (named_people, _listed, _listed_people):
        assert helper(scope) == {'person-a'}
    assert named({'locations': {}}, {'scope': scope})['services'] == ['clinic-a']
    node = clinic['conditions'][0]
    assert _reach_record({'0': node}, {'0'}, 'service_ids', {'clinic-a'}, ()) == {
        'matched_paths': ['0'], 'count': 'together'}
    assert _reach_record({'0': node}, {'0'}, 'service_ids', {'other-clinic'}, ()) is None
    nonempty = {'field': 'service_ids', 'op': 'not_empty'}
    assert _reach_record({'0': nonempty}, {'0'}, 'service_ids', {'clinic-a'}, ()) == {
        'matched_paths': ['0'], 'count': 'together'}


@pytest.mark.parametrize('op', ['any', 'all', 'none'])
def test_absolute_date_set_condition_keeps_named_entry_semantics(op):
    from packages.domain.conditions import conditions_match
    from .test_date_set_conditions import SET, date_sets, selected

    legacy = selected('Eid', op=op)
    absolute = deepcopy(legacy)
    absolute['conditions'][0].update(values=[], value={'kind': 'absolute', 'value': SET})
    stored = deepcopy(absolute)
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': date_sets()}
    shifts = [{'start': f'2026-10-{day:02}T08:00:00+03:00'} for day in (4, 5, 6, 7)]
    old = [conditions_match(data, legacy, None, shift, None, None) for shift in shifts]
    assert old == ([True, False, False, True] if op == 'none' else [False, True, True, False])
    assert [conditions_match(data, absolute, None, shift, None, None) for shift in shifts] == old
    assert absolute == stored


@pytest.mark.parametrize('op', ['any', 'is'])
def test_fact_projection_narrows_absolute_person_operands_without_mutating_the_source(op):
    from packages.domain.rule_catalogue_migration import _fact_line_people

    who = tree('person_ids', op, {'kind': 'absolute', 'value': ['person-a', 'person-b']})
    who['conditions'][0]['count'] = 'each'
    line = {'id': 'source-line', 'who': who}
    stored = deepcopy(line)
    narrowed = _fact_line_people(line, {'person-a'})
    assert narrowed['who']['conditions'][0]['value'] == {'kind': 'absolute', 'value': ['person-a']}
    assert narrowed['who']['conditions'][0]['count'] == 'each'
    assert evaluate(narrowed['who'], lambda field: {'person-a'})
    assert not evaluate(narrowed['who'], lambda field: {'person-b'})
    assert line == stored


@pytest.mark.parametrize('op', ['any', 'is'])
def test_allocation_goal_conversion_keeps_absolute_clinic_choices(op):
    from packages.domain.rule_groups import goal_conversions
    from .test_rule_simplification import SERVICE, rule

    clinics = list(SERVICE.values())[:2]
    before = rule('Allocation balance', {'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties'},
                  {'conditions': {'match': 'all', 'conditions': [{'field': 'service_ids', 'values': clinics}]}},
                  severity='advisory')
    after = deepcopy(before)
    after['definition']['scope']['conditions'] = tree('service_ids', op, {'kind': 'absolute', 'value': clinics})
    old = goal_conversions([before])
    assert len(old) == 1
    assert goal_conversions([after]) == old
