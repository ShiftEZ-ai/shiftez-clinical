"""Table aliases and hints use the same resolver as canonical condition fields."""
from copy import deepcopy
from datetime import date

import pytest

from packages.domain.conditions import EvaluationContext, evaluate, evaluate_candidates, resolve_operand


def context(**kwargs):
    return EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=date(2026, 10, 18), **kwargs)


@pytest.mark.parametrize('token,field,values,display', [
    ('me', 'person_ids', ['p'], '1 person'),
    ('people_i_approve', 'person_ids', ['p', 'q', 'p'], '2 people'),
    ('my_team', 'person_ids', [], '0 people'),
    ('people_i_supervise', 'person_ids', ['p'], '1 person'),
    ('viewed_rota', 'rota_ids', ['rota'], '1 rota'),
    ('my_clinics', 'service_ids', {'service_ids': ['a', 'b', 'c']}, '3 + sub-clinics'),
    ('my_clinics', 'location_ids', {'service_ids': ['a', 'b'], 'location_ids': ['wing', 'room', 'desk']}, '2 + sub-clinics'),
])
def test_choice_hints_count_records_without_exposing_ids_in_display(token, field, values, display):
    facts = {token: values}
    stored = deepcopy(facts)
    result = resolve_operand({'kind': 'relative', 'token': token}, context(values=facts), field)
    assert result['display'] == display
    assert result['value'] == (values[field] if isinstance(values, dict) else values)
    assert facts == stored


def test_requests_person_alias_uses_ids_and_date_column_uses_department_calendar():
    ctx = context(table_key='requests', values={'me': ['p']})
    assert resolve_operand({'kind': 'relative', 'token': 'me'}, ctx, 'person')['value'] == ['p']
    bounds = resolve_operand({'kind': 'relative', 'token': 'this_week'}, ctx, 'start')
    assert (bounds['from'], bounds['to']) == ('2026-10-18', '2026-10-24')
    predicate = {'conditions': [{'field': 'start', 'op': 'within',
                                 'value': {'kind': 'relative', 'token': 'this_week'}}]}
    assert evaluate(predicate, lambda _: set(), ctx, row={'start': '2026-10-20'})
    assert not evaluate(predicate, lambda _: set(), ctx, row={'start': '2026-10-25'})


def test_collection_location_alias_keeps_clinic_descendants_in_resolved_ids():
    ctx = context(table_key='schedule-additions', values={'my_clinics': {
        'service_ids': ['clinic'], 'location_ids': ['wing', 'room', 'desk']}})
    result = resolve_operand({'kind': 'relative', 'token': 'my_clinics'}, ctx, 'location')
    assert result['value'] == ['wing', 'room', 'desk']
    assert result['display'] == '1 + sub-clinics'


def test_table_metadata_cannot_make_a_viewer_token_valid_for_rules():
    with pytest.raises(ValueError, match='duty-relative'):
        resolve_operand({'kind': 'relative', 'token': 'me'},
                        context(table_key='requests', context='rule', values={'me': ['p']}), 'person')


@pytest.mark.parametrize('op,operand,held,expected', [
    ('gt', 3, 4, True), ('gt', 3, 2, False),
    ('between', [{'kind': 'absolute', 'value': 3}, 5], 4, True),
    ('contains', 'staff', 'Senior staff', True), ('contains', 'staff', 'Consultant', False),
    ('none', None, {'Consultant'}, False), ('empty', None, [], True),
])
def test_numbered_rule_line_columns_use_shared_contract_type_inference(op, operand, held, expected):
    line = {'field': 'c0', 'op': op}
    if operand is not None:
        line['value'] = operand
    elif op == 'none':
        line['values'] = ['Consultant']
    assert evaluate({'conditions': [line]}, lambda _: held, context(table_key='rule-line-rows')) == expected


def test_shared_tree_traversal_retains_context_with_sql_leaf_and_combine_operations():
    ctx = context(table_key='requests')
    tree = {'match': 'any', 'conditions': [{'match': 'all', 'conditions': [
        {'field': 'start', 'op': 'within', 'value': {'kind': 'relative', 'token': 'this_week'}},
        {'field': 'end', 'op': 'not_empty'}]}, {'field': 'person', 'op': 'empty'}]}
    seen = []

    def sql_leaf(line, columns):
        seen.append(line['field'])
        if line.get('value'):
            bounds = resolve_operand(line['value'], ctx, line['field'])
            return (columns(line['field']), bounds['from'], bounds['to'])
        return (columns(line['field']), line['op'])

    result = evaluate(tree, lambda key: key, ctx, match_line=sql_leaf,
                      all_of=lambda parts: ('and', *parts), any_of=lambda parts: ('or', *parts))
    assert result == ('or', ('and', ('start', '2026-10-18', '2026-10-24'), ('end', 'not_empty')),
                      ('person', 'empty'))
    assert seen == ['start', 'end', 'person']


@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('nested', [False, True])
def test_participant_sets_use_all_people_without_combining_their_attributes(typed, nested):
    people = [{'person_ids': {'p'}, 'category_ids': {'senior'}, 'tags': {'night'}},
              {'person_ids': {'q'}, 'category_ids': {'junior'}, 'tags': {'day'}}]
    holds = lambda field: set().union(*(person[field] for person in people))
    witnesses = lambda: [person.__getitem__ for person in people]
    chosen = {'field': 'person_ids', 'op': 'all', **(
        {'value': {'kind': 'absolute', 'value': ['p', 'q']}} if typed else {'values': ['p', 'q']})}
    tree = {'conditions': [chosen]}
    if nested:
        tree = {'match': 'any', 'conditions': [
            {'field': 'person_ids', 'values': ['missing']}, {'conditions': [tree]}]}
    assert evaluate(tree, holds)
    assert evaluate_candidates(tree, holds, witnesses())
    for extra, expected in [
        ({'field': 'person_ids', 'op': 'all', 'values': ['p', 'missing']}, False),
        ({'field': 'category_ids', 'op': 'all', 'values': ['senior', 'junior']}, False),
        ({'conditions': [{'field': 'person_ids', 'values': ['p']},
                         {'field': 'tags', 'values': ['day']}]}, False),
        ({'conditions': [{'field': 'category_ids', 'values': ['junior']},
                         {'field': 'tags', 'values': ['day']}]}, True),
        ({'field': 'tags', 'op': 'none', 'values': ['day']}, False),
        ({'field': 'tags', 'op': 'is_not', 'value': 'day'}, False),
    ]:
        predicate = {'conditions': [tree, extra]}
        for match_line in (None, lambda line, source: evaluate(line, source)):
            assert evaluate_candidates(predicate, holds, witnesses(), match_line=match_line) is expected
