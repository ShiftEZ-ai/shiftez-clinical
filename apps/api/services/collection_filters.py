"""SQL leaves composed by the shared evaluator before collection counts and paging."""
from datetime import date, timedelta

from sqlalchemy import DateTime, JSON, String, and_, case, cast, false, func, or_, true
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from apps.api.errors import require
from packages.contracts import schemas as s
from packages.domain.conditions import absolute_values, evaluate, resolve_operand, validate_relative_conditions
from packages.domain.time import local_midnight


class _unicode_lower(FunctionElement):
    type = String()
    inherit_cache = True


@compiles(_unicode_lower)
def _compile_lower(element, compiler, **kwargs):
    return compiler.process(func.lower(*element.clauses), **kwargs)


@compiles(_unicode_lower, 'postgresql')
def _compile_unicode_lower(element, compiler, **kwargs):
    # Unicode root casing agrees with Python/JS lower(), including dotted I and final sigma.
    return compiler.process(func.lower(next(iter(element.clauses)).collate('unicode')), **kwargs)


class change_kind(FunctionElement):
    """The action namespace is the canonical kind of audit change on both supported databases."""
    type = String()
    inherit_cache = True


@compiles(change_kind)
def _compile_change_kind(element, compiler, **kwargs):
    action = next(iter(element.clauses))
    dot = func.instr(action, '.')
    return compiler.process(case((dot > 0, func.substr(action, 1, dot - 1)), else_=action), **kwargs)


@compiles(change_kind, 'postgresql')
def _compile_postgres_change_kind(element, compiler, **kwargs):
    return compiler.process(func.split_part(next(iter(element.clauses)), '.', 1), **kwargs)


def sort_keys(collection_filter, fields):
    """Reuse the saved collection view's primary and subsequent sort-key wire format."""
    if collection_filter is None:
        return []
    keys = ([collection_filter.sort] if collection_filter.sort else []) + collection_filter.then_sort
    require(all(field in fields for field, _ in keys), 'INVALID_SORT', 'Choose readable collection sort fields')
    require(len({field for field, _ in keys}) == len(keys), 'INVALID_SORT', 'Choose each collection sort field once')
    return keys


def order_by(query, collection_filter, columns, *, default=()):
    keys = sort_keys(collection_filter, columns)
    if not keys:
        return query
    ordering = []
    for field, direction in keys:
        column = columns[field]
        if isinstance(column.type, String):
            column = _unicode_lower(column)
        ordering.append((column.desc() if direction == 'desc' else column.asc()).nulls_last())
    return query.order_by(None).order_by(*ordering, *default)


def parse(value, *, table_key=None):
    return s.CollectionFilter.model_validate_json(value, context={"table_key": table_key}) if value else s.CollectionFilter()


def validate(tree, fields, context=None, *, table_key=None):
    if tree is None:
        return None
    table_key = table_key or (context.table_key if context else None)
    tree = s.CollectionConditions.model_validate(tree, context={"table_key": table_key})
    readable = set(fields)
    readable |= {entry['key'] for entry in s.table_condition_definitions(table_key)
                 if entry.get('operand_field') in readable}
    require(tree.fields() <= readable, 'INVALID_FILTER', 'Choose readable collection fields')
    definition = tree.model_dump(mode='json')
    if context:
        validate_relative_conditions(definition, context)
    return definition


def predicate(tree, columns, context=None, *, table_key=None):
    if tree is None:
        return true()
    table_key = table_key or (context.table_key if context else None)
    definition = validate(tree, columns, context, table_key=table_key)

    def column(field):
        source = field if field in columns else s.condition_field_definition(field, table_key)['operand_field']
        return columns[source]

    def match(line, holds):
        held = holds(line['field'])
        held = held if isinstance(held, tuple) else (held,)
        field, op = line['field'], line.get('op', 'any')
        metadata = s.condition_line_definition(field, table_key, op, line.get('value')) or {}
        kind = metadata.get('value_kind')
        bound_field = field if field in columns else metadata.get('operand_field', field)

        def empty(column):
            if isinstance(column.type, String):
                return or_(column.is_(None), column == '')
            if isinstance(column.type, JSON):
                return or_(column.is_(None), cast(column, String).in_(['null', '[]', '\"\"']))
            return column.is_(None)

        missing = and_(*(empty(column) for column in held))
        if op in {'empty', 'not_empty'}:
            return missing if op == 'empty' else ~missing
        if line.get('value') is None:
            if kind == 'date':
                parts = [match({**line, 'op': 'is', 'value': value}, holds) for value in absolute_values(line)]
                return and_(*parts) if op == 'all' else ~or_(*parts) if op == 'none' else or_(*parts)
            values = set(absolute_values(line))
            if op == 'all':
                return and_(*(or_(*(func.coalesce(column == value, false()) for column in held)) for value in values))
            found = or_(*(func.coalesce(column.in_(values), false()) for column in held))
            return ~found if op in {'none', 'is_not'} else found
        operand = line['value']
        if op == 'between':
            first, last = (resolve_operand(value, context, field) for value in operand)
            lower, upper = ((min(first['from'], last['from']), max(first['to'], last['to'])) if kind == 'date'
                            else sorted((first['value'], last['value'])))
        else:
            resolved = resolve_operand(operand, context, field)
            lower, upper = resolved['from'], resolved['to']

        def compare(column):
            if kind == 'date':
                start, end = date.fromisoformat(lower), date.fromisoformat(upper)
                source = s.condition_field_definition(getattr(column, 'key', None) or bound_field) or metadata
                exclusive = source.get('exclusive_last_day', False)
                if isinstance(column.type, DateTime):
                    start = local_midnight(start, context.timezone)
                    end = local_midnight(end + timedelta(days=1), context.timezone)
                    # The included last day owns a midnight-exclusive end on the following day.
                    if exclusive:
                        return (column <= start if op == 'before' else column > end if op == 'after' else
                                column <= end if op == 'on_or_before' else column > start if op == 'on_or_after' else
                                or_(column <= start, column > end) if op in {'none', 'is_not'} else and_(column > start, column <= end))
                    return (column < start if op == 'before' else column >= end if op == 'after' else
                            column < end if op == 'on_or_before' else column >= start if op == 'on_or_after' else
                            or_(column < start, column >= end) if op in {'none', 'is_not'} else and_(column >= start, column < end))
                if exclusive:
                    start, end = start + timedelta(days=1), end + timedelta(days=1)
                return (column < start if op == 'before' else column > end if op == 'after' else
                        column <= end if op == 'on_or_before' else column >= start if op == 'on_or_after' else
                        or_(column < start, column > end) if op in {'none', 'is_not'} else column.between(start, end))
            if kind == 'number':
                if op == 'between':
                    return column.between(lower, upper)
                wanted = resolved['value']
                return (column > wanted if op == 'gt' else column >= wanted if op == 'gte' else
                        column < wanted if op == 'lt' else column <= wanted if op == 'lte' else
                        column != wanted if op == 'is_not' else column == wanted)
            wanted = resolved['value']
            if kind == 'text':
                value, wanted = _unicode_lower(column), str(wanted).lower()
                return (value.contains(wanted, autoescape=True) if op == 'contains' else
                        ~value.contains(wanted, autoescape=True) if op == 'not_contains' else
                        value.startswith(wanted, autoescape=True) if op == 'starts_with' else
                        value.endswith(wanted, autoescape=True) if op == 'ends_with' else
                        value != wanted if op == 'is_not' else value == wanted)
            values = wanted if isinstance(wanted, (list, tuple)) else [wanted]
            return column.in_(values)

        if kind not in {'date', 'number', 'text'}:
            values = resolved['value']
            values = values if isinstance(values, (list, tuple)) else [values]
            if op == 'all':
                return and_(*(or_(*(func.coalesce(column == value, false()) for column in held)) for value in values))
            found = or_(*(func.coalesce(compare(column), false()) for column in held))
            return ~found if op in {'none', 'is_not'} else found
        found = or_(*(func.coalesce(compare(column), false()) for column in held))
        return or_(missing, found) if op in {'none', 'is_not', 'not_contains'} else found

    return evaluate(definition, column, context, match_line=match,
                    all_of=lambda parts: and_(*parts), any_of=lambda parts: or_(*parts))
