"""Last-day condition operands are inclusive; storage remains exclusive and immutable."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from packages.contracts import schemas as s
from packages.domain.conditions import EvaluationContext, evaluate, resolve_operand
from packages.domain.time import local_midnight


CASES = [
    ('is', '2026-12-31', True), ('is', '2027-01-01', False),
    ('is', {'kind': 'relative', 'token': 'today'}, True),
    ('before', '2026-12-31', False), ('before', '2027-01-01', True),
    ('after', '2026-12-30', True), ('after', '2026-12-31', False),
    ('on_or_before', '2026-12-31', True), ('on_or_before', '2026-12-30', False),
    ('on_or_after', '2026-12-31', True), ('on_or_after', '2027-01-01', False),
    ('between', ['2026-12-30', {'kind': 'relative', 'token': 'today'}], True),
    ('between', ['2027-01-01', '2027-01-02'], False),
    ('within', {'kind': 'relative', 'token': 'this_year'}, True),
    ('within', {'kind': 'relative', 'token': 'next_month'}, False),
    ('empty', None, False), ('not_empty', None, True),
]


@pytest.mark.parametrize('zone', ['Asia/Riyadh', 'America/New_York'])
@pytest.mark.parametrize('field,stored', [('ends_on', 'date'), ('end', 'midnight'), ('end', 'daytime'), ('last_day', 'inclusive')])
@pytest.mark.parametrize('op,operand,expected', CASES)
def test_exclusive_last_day_fields_use_the_shown_day_once(zone, field, stored, op, operand, expected):
    day = date(2026, 12, 31)
    actual = (day + timedelta(days=1) if stored == 'date' else
              local_midnight(day + timedelta(days=1), zone) if stored == 'midnight' else
              local_midnight(day, zone) + timedelta(hours=12) if stored == 'daytime' else day)
    context = EvaluationContext(timezone=zone, week_start=6, today=day)
    tree = {'conditions': [{'field': field, 'op': op, **({'value': operand} if operand is not None else {})}]}
    before = deepcopy(tree)
    assert evaluate(tree, lambda _: {actual}, context, row={field: actual}) is expected
    assert tree == before


@pytest.mark.parametrize('field,actual', [('ends_on', '2027-01-01'), ('end', '2026-12-31T21:00:00+00:00'), ('last_day', '2026-12-31')])
@pytest.mark.parametrize('op,values,expected', [('any', ['2026-12-31'], True), ('none', ['2026-12-31'], False),
    ('all', ['2026-12-31', '2027-01-01'], False), ('any', ['2026-12-30', '2026-12-31'], True)])
def test_legacy_date_literal_lists_have_the_same_inclusive_day(field, actual, op, values, expected):
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=0, today=date(2026, 12, 31))
    tree = {'conditions': [{'field': field, 'op': op, 'values': values}]}
    assert evaluate(tree, lambda _: {actual}, context, row={field: actual}) is expected


@pytest.mark.parametrize('field', ['ends_on', 'end', 'last_day'])
@pytest.mark.parametrize('actual', [None, '', []])
def test_open_last_day_is_empty_for_exclusive_and_inclusive_fields(field, actual):
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=0)
    assert evaluate({'conditions': [{'field': field, 'op': 'empty'}]}, lambda _: actual, context, row={field: actual})
    assert not evaluate({'conditions': [{'field': field, 'op': 'not_empty'}]}, lambda _: actual, context, row={field: actual})


def test_last_day_metadata_is_derived_from_one_server_catalogue_and_operands_stay_inclusive():
    assert s.EXCLUSIVE_LAST_DAY_FIELDS == {'ends_on', 'end'}
    for table, fields in s.table_condition_registry().items():
        for field in fields.values():
            expected = field['value_kind'] == 'date' and (field.get('operand_field') or field['key']) in s.EXCLUSIVE_LAST_DAY_FIELDS
            assert field['exclusive_last_day'] is expected, (table, field['key'])
    assert s.condition_field_definition('dates.end', 'requests')['exclusive_last_day']
    assert not s.condition_field_definition('last_day', 'wishes')['exclusive_last_day']
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=0, today=date(2026, 12, 31))
    for field in s.EXCLUSIVE_LAST_DAY_FIELDS:
        for operand in ('2026-12-31', {'kind': 'relative', 'token': 'today'}):
            result = resolve_operand(operand, context, field)
            assert result['from'] == result['to'] == '2026-12-31'



def test_last_day_fallback_metadata_uses_exclusive_source_without_changing_physical_inclusive_field():
    from apps.api.services.bulk_selection import _held
    day = date(2026, 12, 31)
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'people': {}, '_bulk_day': day, '_bulk_department': 'synthetic'}
    row = {'id': 'synthetic-eligibility', 'starts_on': date(2020, 1, 1), 'ends_on': date(2027, 1, 1)}
    start, end = local_midnight(day, 'Asia/Riyadh'), local_midnight(day + timedelta(days=1), 'Asia/Riyadh')
    held = _held(data, start, end, row, 'eligibilities', {})
    context = EvaluationContext(timezone='Asia/Riyadh', week_start=6, today=day, table_key='eligibilities')
    assert s.condition_field_definition('last_day', 'eligibilities')['operand_field'] == 'ends_on'
    assert s.condition_field_definition('last_day', 'requests')['operand_field'] == 'end'
    assert s.condition_field_definition('last_day', 'wishes')['operand_field'] == 'last_day'
    assert not s.condition_field_definition('last_day', 'wishes')['exclusive_last_day']
    for field in ('ends_on', 'last_day'):
        assert evaluate({'conditions': [{'field': field, 'op': 'is', 'value': '2026-12-31'}]}, held, context, row=row)
