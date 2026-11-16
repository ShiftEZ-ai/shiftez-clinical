"""Date-set scopes use the shared condition evaluator in every backend reader."""
import copy
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from packages.contracts.schemas import PolicyDefinition
from packages.domain.conditions import (active_rules, bucket_keys, conditions_match, counting_views, evaluate, has_dated_person_lines,
                                        has_person_lines, has_session_lines, implies, rule_holds)
from packages.domain.cp_sat import generate
from packages.domain.rule_groups import expanded
from packages.domain.rule_lines import outranking_by_number
from packages.domain.rule_map import rule_map
from packages.domain.score import schedule_score
from packages.domain.time import aware
from packages.domain.validation import evaluate as validate, session_matches
from .test_rule_catalogue_guard import catalogue, line


def uid(name):
    return str(uuid5(NAMESPACE_URL, 'date-set-condition:' + name))


SET = uid('public-holidays')


def date_sets():
    return {SET: {'id': SET, 'name': 'Public holidays', 'entries': [
        {'id': uid('eid'), 'name': 'Eid', 'repeat': 'once', 'first_day': '2026-10-05', 'last_day': '2026-10-06'},
        {'id': uid('national'), 'name': 'National Day', 'repeat': 'once', 'first_day': '2026-10-07', 'last_day': '2026-10-07'},
    ]}}


def selected(*names, op='any', count='together'):
    return {'match': 'all', 'conditions': [{'field': 'date_set_ids', 'op': op, 'values': [SET],
                                           'entry_names': list(names), 'count': count}]}


@pytest.mark.parametrize('op,expected', [('any', [False, True, True, False]), ('all', [False, True, True, False]),
                                         ('none', [True, False, False, True])])
def test_date_set_atom_uses_the_duty_start_in_department_timezone(op, expected):
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': date_sets()}
    tree = selected('Eid', op=op)
    starts = ['2026-10-04T20:59:00+00:00', '2026-10-04T21:00:00+00:00',
              '2026-10-06T20:59:00+00:00', '2026-10-06T21:00:00+00:00']
    shifts = [{'start': start, 'service_ids': [], 'assignment_type_id': 't'} for start in starts]
    assert [conditions_match(data, tree, None, shift, None, None) for shift in shifts] == expected
    assert [session_matches(data, {'conditions': tree}, shift) for shift in shifts] == expected
    # A late participant interval and overnight duty still use the original duty date.
    assert conditions_match(data, selected('Eid'), None, shifts[1],
                            datetime.fromisoformat(starts[-1]), datetime.fromisoformat('2026-10-07T05:00:00+00:00'))


@pytest.mark.parametrize('same_set', [False, True])
def test_gregorian_selection_does_not_resolve_unrelated_hijri_dates_outside_table(same_set):
    sets = date_sets()
    sets[SET]['entries'][1]['repeat'] = 'yearly_gregorian'
    hijri = {'id': uid('ramadan'), 'name': 'Ramadan', 'repeat': 'yearly_hijri',
             'first_day': '1447-09-01', 'last_day': '1447-09-29'}
    if same_set:
        sets[SET]['entries'].append(hijri)
    else:
        other = uid('other-set')
        sets[other] = {'id': other, 'name': 'Hijri holidays', 'entries': [hijri]}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': sets}
    holiday = {'start': '2080-10-07T08:00:00+03:00'}
    ordinary = {'start': '2080-10-06T08:00:00+03:00'}
    assert conditions_match(data, selected('National Day'), None, holiday, None, None)
    assert not conditions_match(data, selected('National Day'), None, ordinary, None, None)
    if not same_set:
        assert conditions_match(data, selected(), None, holiday, None, None)


def test_date_sets_are_session_fields_in_projections_and_counting_caches():
    tree = selected('Eid', count='each')
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': date_sets(), '_bucket_keys_cache': {}}
    assert has_session_lines(tree) and has_session_lines(tree, {'date_set_ids'})
    assert not has_person_lines(tree) and not has_dated_person_lines(tree)
    assert evaluate(tree, lambda field: None)  # Person-only projections leave the session unknown.
    unknown = rule_holds(data, 'p', None, None, None)
    assert evaluate(tree, unknown)
    holiday = {'start': '2026-10-05T08:00:00+03:00'}
    other_entry = {'start': '2026-10-07T08:00:00+03:00'}
    assert bucket_keys(tree, rule_holds(data, 'p', holiday, None, None))
    assert bucket_keys(tree, rule_holds(data, 'q', holiday, None, None))
    assert not bucket_keys(tree, rule_holds(data, 'p', other_entry, None, None))
    assert conditions_match(data, selected(), None, other_entry, None, None)


def test_all_date_sets_requires_same_day_membership_in_every_selected_set():
    sets = date_sets()
    other = uid('overlapping-set')
    sets[other] = {'id': other, 'name': 'Local breaks', 'entries': [
        {'id': uid('break'), 'name': 'Eid', 'repeat': 'once',
         'first_day': '2026-10-06', 'last_day': '2026-10-06'}]}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': sets}
    atom = {**selected('Eid', op='all')['conditions'][0], 'values': [SET, other]}
    tree = {'conditions': [atom]}
    assert not conditions_match(data, tree, None, {'start': '2026-10-05T08:00:00+03:00'}, None, None)
    assert conditions_match(data, tree, None, {'start': '2026-10-06T08:00:00+03:00'}, None, None)


def test_count_buckets_keep_named_date_predicates_distinct_when_session_is_unknown():
    tree = {'match': 'all', 'conditions': [selected('Eid', count='each')['conditions'][0],
                                          selected('Ramadan', op='none')['conditions'][0]]}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'date_sets': date_sets()}
    holiday = {'start': '2026-10-05T08:00:00+03:00'}
    actual = bucket_keys(tree, rule_holds(data, None, holiday, None, None))
    assert actual and actual == bucket_keys(tree, lambda field: None)
    row = {'id': uid('counted'), 'definition': {'scope': {'conditions': tree}}}
    assert len(counting_views(data, [row])) == 1


def test_entry_selection_survives_catalogue_expansion_grouping_and_implication():
    eid = line(id=uid('line'), where=selected('Eid'), numbers={'max': {'value': 1, 'level': 'high'}},
               settings={'max_hours': {'window': 'day'}})
    first = catalogue('hours_per_period', eid)
    second = copy.deepcopy(first)
    second['definition']['rule']['lines'][0]['where'] = selected('National Day')
    assert expanded([first]) != expanded([second])
    scope = active_rules([first])[0]['definition']['scope']
    assert scope['conditions']['conditions'][0]['entry_names'] == ['Eid']
    assert not implies({'conditions': selected('Eid')}, {'conditions': selected('National Day')})
    ordered = selected('Eid', 'National Day')
    reversed_names = selected('National Day', 'Eid')
    assert implies({'conditions': ordered}, {'conditions': reversed_names})
    mine = line(who={'conditions': [{'field': 'person_ids', 'values': [uid('person')]}]},
                where=selected('National Day'), numbers={'max': {'value': 1}})
    assert outranking_by_number(catalogue('hours_per_period', eid, mine)) == {}


def test_advisory_catalogue_date_scope_matches_validator_score_solver_and_rule_map(h):
    person = h.seed['people'][0]
    workspace = h.workspace(start='2026-10-04', end='2026-10-09')
    for day in range(4, 8):
        workspace = h.shift(workspace, f'2026-10-{day:02}T08:00:00+03:00',
                            f'2026-10-{day:02}T12:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    row = catalogue('hours_per_period', line(id=uid('advisory-line'),
        who={'conditions': [{'field': 'person_ids', 'values': [person]}]}, where=selected('Eid'),
        numbers={'max': {'value': 1, 'level': 'high'}}, settings={'max_hours': {'window': 'day'}}))
    row['department_id'] = h.seed['department_id']
    row['definition'] = PolicyDefinition.model_validate(row['definition']).model_dump(mode='json')
    data['date_sets'], data['policies'] = date_sets(), [row]
    validation = validate(copy.deepcopy(data))
    breaches = [f for f in validation['findings'] if f['rule_id'] == row['id']]
    assert len(breaches) == 2 and all(f['severity'] == 'warning' for f in breaches)
    from zoneinfo import ZoneInfo
    assert {aware(f['start']).astimezone(ZoneInfo('Asia/Riyadh')).date().isoformat() for f in breaches} == {
        '2026-10-05', '2026-10-06'}
    options = {'scoring': {'weights': {'coverage': 0, 'rules': 1, 'fairness': 0, 'preferences': 0, 'stability': 0}}}
    score = schedule_score(data, validation, options)
    assert score['components']['rules']['value'] == 0
    assert {c['rule_id'] for c in score['contributions']} == {row['id']}
    mapped = rule_map(copy.deepcopy(data))
    reached = next(r for r in mapped['rules'] if r['id'] == row['id'])
    assert {mapped['shifts'][n] for n in reached['sessions']} == {
        shift['id'] for shift in data['shifts'] if shift['start'][:10] in {'2026-10-05', '2026-10-06'}}
    assert reached['session_side'] and reached['person_side']
    # Existing assignments are fixed: CP-SAT must count exactly two breached daily bounds.
    solved = generate(copy.deepcopy(data), 3, workers=1, deterministic=True)
    assert solved['status'] == 'candidate_feasible', solved
    assert solved['objective_components']['rules']['value'] == score['components']['rules']['value'] == 0
    assert solved['compiled_component_values']['rules'] == pytest.approx(0, abs=0.0001)
    assert len([f for f in solved['validation']['findings'] if f['rule_id'] == row['id']]) == 2
    unscoped = copy.deepcopy(data)
    unscoped['policies'][0]['definition']['rule']['lines'][0]['where'] = None
    all_days = generate(unscoped, 3, workers=1, deterministic=True)
    assert all_days['status'] == 'candidate_feasible', all_days
    unscoped_validation = validate(copy.deepcopy(unscoped))
    assert len([f for f in unscoped_validation['findings'] if f['rule_id'] == row['id']]) == 4
    assert len([f for f in all_days['validation']['findings'] if f['rule_id'] == row['id']]) == 4
    # Ordinary rule instances are binary: four daily findings do not multiply this person's one breached instance.
    assert all_days['objective_components']['rules']['value'] == schedule_score(
        unscoped, unscoped_validation, options)['components']['rules']['value'] == 0
    assert all_days['compiled_component_values']['rules'] == pytest.approx(0, abs=0.0001)
    # A date-set mutation changes the resolver input, including score and map results in a later request.
    data['date_sets'][SET]['entries'][0].update(first_day='2026-10-08', last_day='2026-10-08')
    clear = validate(copy.deepcopy(data))
    assert not [f for f in clear['findings'] if f['rule_id'] == row['id']]
    assert schedule_score(data, clear, options)['score'] == 100
    assert next(r for r in rule_map(data)['rules'] if r['id'] == row['id'])['sessions'] == []
