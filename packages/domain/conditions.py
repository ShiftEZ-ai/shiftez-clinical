"""The one condition evaluator: rule scopes, the solver, quotas, the rule preview and announcement audiences.

A tree is `{match: all|any, conditions: [line | group]}`; a line has a field, operator and legacy `values` or `value` operand.
Person fields are what a person holds at that moment (dated ranks, skills, programmes, rotations, cohorts) plus
tags; session fields (clinic, place with everything inside it, duty type, frozen usual people) come from the session.
"""
from bisect import bisect_left, bisect_right
from calendar import month_abbr, month_name, monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import re
from functools import lru_cache
from itertools import combinations, product
from packages.contracts.condition_limits import check_count_bucket_budget
from packages.contracts.rule_catalogue import PRIMITIVE_CONDITION_FIELDS
from packages.contracts.schemas import SESSION_SCOPE_FIELDS, GENERATION_SCOPE_FIELDS
from .time import aware, local_midnight


@dataclass
class EvaluationContext:
    """One request's calendar and already-authorized viewer facts; no database reads in the evaluator."""
    timezone: str
    week_start: int
    today: date | None = None
    as_of: date | None = None
    duty_day: date | None = None
    values: dict = field(default_factory=dict)
    allowed_tokens: frozenset | None = None
    context: str = 'filter'
    table_key: str | None = None
    metrics: dict = field(default_factory=dict)
    _resolved: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        from zoneinfo import ZoneInfo
        from . import time
        if self.week_start not in range(7):
            raise ValueError('The department week start must be between Monday (0) and Sunday (6)')
        zone = ZoneInfo(self.timezone)
        if self.today is None:
            self.today = time.now().astimezone(zone).date()
        if self.as_of is None:
            self.as_of = self.today


def _month_shift(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def _date_range(token, n, unit, context):
    duty = token in {'duty_day', 'duty_week', 'duty_month', 'days_before_duty', 'days_after_duty'}
    day = context.duty_day if duty else context.today
    if day is None:
        raise ValueError('A duty-relative date needs the duty day')
    if token in {'today', 'duty_day', 'yesterday', 'tomorrow'}:
        day += timedelta(days={'yesterday': -1, 'tomorrow': 1}.get(token, 0))
        return day, day
    if token in {'days_before_duty', 'days_after_duty'}:
        return (day - timedelta(days=n), day) if token == 'days_before_duty' else (day, day + timedelta(days=n))
    if token in {'last_n', 'next_n'}:
        if unit == 'months':
            return (_month_shift(day, -n) + timedelta(days=1), day) if token == 'last_n' else (day, _month_shift(day, n) - timedelta(days=1))
        width = n * (7 if unit == 'weeks' else 1) - 1
        return (day - timedelta(days=width), day) if token == 'last_n' else (day, day + timedelta(days=width))
    which, period = token.split('_', 1)
    offset = {'last': -1, 'next': 1}.get(which, 0)
    if period == 'week':
        start = day - timedelta(days=(day.weekday() - context.week_start) % 7) + timedelta(weeks=offset)
        return start, start + timedelta(days=6)
    months = {'month': 1, 'quarter': 3, 'year': 12}[period]
    start = date(day.year, ((day.month - 1) // months) * months + 1, 1)
    start = _month_shift(start, offset * months)
    return start, _month_shift(start, months) - timedelta(days=1)


def _date_value(value, context):
    from zoneinfo import ZoneInfo
    if isinstance(value, datetime):
        return aware(value).astimezone(ZoneInfo(context.timezone)).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        if 'T' in value or ' ' in value:
            return aware(value).astimezone(ZoneInfo(context.timezone)).date()
        return date.fromisoformat(value)
    raise ValueError('Choose an ISO date or a relative date')



def shown_date(value, context, metadata):
    """Stored exclusive ends select their included last day; chosen operands stay unchanged."""
    if metadata and metadata.get('exclusive_last_day'):
        if isinstance(value, datetime) or isinstance(value, str) and ('T' in value or ' ' in value):
            return _date_value(aware(value) - timedelta(microseconds=1), context)
        return _date_value(value, context) - timedelta(days=1)
    return _date_value(value, context)

def _resolution(value, label, context, *, is_date=False):
    if is_date:
        bounds = value if isinstance(value, (list, tuple)) else (value, value)
        start, end = sorted(_date_value(v, context) for v in bounds)
        display = f'{start.day} {start:%b %Y}'
        if start != end:
            display = f'{start.day} {start:%b} – {end.day} {end:%b %Y}'
        return {'label': label or display, 'from': start.isoformat(), 'to': end.isoformat(),
                'display': display, 'value': [start.isoformat(), end.isoformat()]}
    display = ', '.join(str(v) for v in value) if isinstance(value, (list, tuple, set)) else str(value) if value is not None else ''
    return {'label': label or display, 'from': None, 'to': None, 'display': display, 'value': value}


def _relative_definition(operand, context, field):
    from packages.contracts.schemas import condition_field_definition, RELATIVE_TOKEN_CATALOGUE, RelativeOperand
    metadata = condition_field_definition(field, context.table_key)
    operand_field = (metadata.get('operand_field') or field) if metadata else field
    validated = RelativeOperand.model_validate(operand)
    token = validated.token
    definition = next(item for item in RELATIVE_TOKEN_CATALOGUE if item['token'] == token)
    if context.context not in definition['contexts']:
        raise ValueError('Rule and audience conditions accept only duty-relative dates and absolute values')
    if context.allowed_tokens is not None and token not in context.allowed_tokens:
        raise ValueError(f"{definition['label']} is not meaningful for this viewer or field")
    if metadata and (metadata['value_kind'] not in definition['field_kinds'] or
                     definition['fields'] and operand_field not in definition['fields']):
        raise ValueError(f"{definition['label']} does not apply to {metadata['label']}")
    return validated, definition


def resolve_operand(operand, context, field=None, *, row=None):
    """Resolve without mutating the saved operand. All date endpoints are inclusive local calendar days."""
    from packages.contracts.schemas import condition_field_definition
    if hasattr(operand, 'model_dump'):
        operand = operand.model_dump(mode='json', exclude_none=True)
    metadata = condition_field_definition(field, context.table_key if context else None)
    operand_field = (metadata.get('operand_field') or field) if metadata else field
    if not isinstance(operand, dict) or operand.get('kind') == 'absolute':
        value = operand.get('value') if isinstance(operand, dict) else operand
        return _resolution(value, '', context, is_date=metadata and metadata['value_kind'] == 'date')
    if operand.get('kind') != 'relative':
        raise ValueError('Choose an absolute or relative operand')
    validated, definition = _relative_definition(operand, context, field)
    token = validated.token
    # Calendar and viewer values are immutable for the lifetime of this request, not per row.
    key = (token, validated.n, validated.unit, field)
    if token != 'its_target' and key in context._resolved:
        return context._resolved[key]
    if 'date' in definition['field_kinds']:
        try:
            value = _date_range(token, validated.n, validated.unit, context)
        except (OverflowError, KeyError) as exc:
            raise ValueError('The relative date range is outside supported calendar dates') from exc
        result = _resolution(value, definition['label'], context, is_date=True)
    else:
        value = context.values.get(token)
        if isinstance(value, dict):
            value = value.get(operand_field)
        if value is None and token == 'its_target':
            value = (row or {}).get('target', context.metrics.get(field, context.metrics.get(operand_field, {})).get('target'))
        if value is None and token == 'department_average':
            value = context.metrics.get(field, context.metrics.get(operand_field, {})).get('average')
        if value is None:
            raise ValueError(f"{definition['label']} is not meaningful for this viewer or field")
        if isinstance(value, set):
            value = sorted(value)
        result = _resolution(value, definition['label'], context)
        if isinstance(value, (list, tuple)):
            if token == 'my_clinics':
                clinics = context.values[token]
                clinics = clinics.get('service_ids', value) if isinstance(clinics, dict) else clinics
                result['display'] = f'{len(set(clinics))} + sub-clinics'
            else:
                noun = {'person_ids': 'person', 'service_ids': 'clinic', 'eligible_service_ids': 'clinic',
                        'location_ids': 'location', 'category_ids': 'rank', 'programme_ids': 'programme',
                        'cohort_ids': 'cohort', 'department_ids': 'department', 'rota_ids': 'rota',
                        'workspace_id': 'rota'}.get(operand_field, 'person' if 'people' in definition['field_kinds'] else 'value')
                count = len(set(value))
                result['display'] = f'{count} ' + ('people' if noun == 'person' and count != 1 else noun + ('s' if count != 1 else ''))
    if token != 'its_target':
        context._resolved[key] = result
    return result


def validate_relative_conditions(tree, context, *, row=None):
    """Reject unavailable values even when an empty result or an OR branch would hide the line."""
    for node in (tree or {}).get('conditions', []):
        if 'conditions' in node:
            validate_relative_conditions(node, context, row=row)
        elif node.get('value') is not None:
            operands = node['value'] if isinstance(node['value'], (list, tuple)) else (node['value'],)
            for operand in operands:
                if isinstance(operand, dict) and operand.get('kind') == 'relative':
                    if operand.get('token') == 'its_target' and row is None and context.metrics.get(node['field'], {}).get('target_available'):
                        _relative_definition(operand, context, node['field'])
                        continue
                    resolve_operand(operand, context, node['field'], row=row)


def parse_date_short_form(text, context):
    """Deterministic short forms, then catalogue label matches; no browser-specific date parsing."""
    from packages.contracts.schemas import RELATIVE_TOKEN_CATALOGUE
    query = ' '.join(text.casefold().split())
    if not query:
        return []
    candidates = []
    for definition in RELATIVE_TOKEN_CATALOGUE:
        if 'date' not in definition['field_kinds'] or context.context not in definition['contexts']:
            continue
        if not definition['needs_n'] and query in {definition['label'].casefold(), definition['token'].replace('_', ' ')}:
            candidates.append((0, {'kind': 'relative', 'token': definition['token']}))
    rolling = re.fullmatch(r'(last|next) ([1-9]\d{0,3}) (day|week|month)s?', query)
    if rolling:
        which, n, unit = rolling.groups()
        candidates.append((0, {'kind': 'relative', 'token': which + '_n', 'n': int(n), 'unit': unit + 's'}))
    offset = re.fullmatch(r'([+-]?\d{1,5})d', query) or re.fullmatch(r'in (-?\d{1,5}) days?', query)
    try:
        if offset:
            day = context.today + timedelta(days=int(offset[1]))
            candidates.append((0, {'kind': 'absolute', 'value': day.isoformat()}))
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', query):
            candidates.append((0, {'kind': 'absolute', 'value': date.fromisoformat(query).isoformat()}))
        else:
            for month in range(1, 13):
                if query in {month_abbr[month].casefold(), month_name[month].casefold()}:
                    first = date(context.today.year, month, 1)
                    last = date(first.year, month, monthrange(first.year, month)[1])
                    candidates.append((0, {'kind': 'absolute', 'value': [first.isoformat(), last.isoformat()]}))
    except (ValueError, OverflowError):
        return []
    for definition in RELATIVE_TOKEN_CATALOGUE:
        if ('date' in definition['field_kinds'] and context.context in definition['contexts'] and
            not definition['needs_n'] and query in definition['label'].casefold()):
            candidates.append((1, {'kind': 'relative', 'token': definition['token']}))
    matches, seen = [], set()
    for rank, operand in sorted(candidates, key=lambda item: item[0]):
        key = json.dumps(operand, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        try:
            resolved = resolve_operand(operand, context, 'date')
        except ValueError:
            continue
        matches.append({'operand': operand, **resolved, 'rank': rank})
    return matches

PERSON_FIELDS = GENERATION_SCOPE_FIELDS
SESSION_FIELDS = SESSION_SCOPE_FIELDS
# Legacy flat scope fields that name people; everything else in a scope describes sessions or times.
PERSON_SCOPE_KEYS = frozenset({'person_id', 'person_ids', 'category_id', 'category_ids', 'programme_id',
                               'rotation_type_id', 'tags', 'capability_ids'})
# Internal person line (never saved): the active rules, by id, that replace this rule for the person at that moment.
OVERRIDDEN = 'overridden_by'
# Internal person line (never saved): the rule templates, by id, where the person has their own line at that moment.
OWN_LINE = 'own_line_of'
# Internal person line (never saved): the catalogue lines, by line id, that are more specific for the person at that
# moment (packages/domain/rule_lines.py).
OUTRANKED = 'outranked_by'
INHERITED_LEVEL = 'inherited_level'
COUNT_BUCKET = 'count_bucket'
INTERNAL = frozenset({OVERRIDDEN, OWN_LINE, OUTRANKED, INHERITED_LEVEL, COUNT_BUCKET})
DATED_PERSON_FIELDS = (PERSON_FIELDS - {'roles', 'person_ids', 'tags'}) | INTERNAL
SCOPE_CONTEXT_BUDGET = 65536


# A tag may stand in a line for the records it labels: its members of that kind are what the line names.
TAG_FIELDS = {'person_ids': 'people', 'usual_assignee_ids': 'people', 'service_ids': 'services', 'eligible_service_ids': 'services',
              'location_ids': 'locations', 'assignment_type_ids': 'assignment_types', 'programme_ids': 'programmes'}


def with_tags(data):
    """Tags read as their members at this moment (data['tags']: {id: {name, members: {kind: [ids]}}}), in place: each
    person holds the names of their tags as `tags`, and every tag a rule names (in a line, a flat list or one of the
    rule's lines) also names its members, so every reader of a scope (this evaluator, the reach map, the solver's quick
    checks) sees plain records. A place's members match everything inside them, as places do. Idempotent."""
    from .rule_lines import bound_policies, read_quality_for
    if 'policies' in data and not data.get('_catalogue_v2_materialized') and (data.get('source_bindings') or data.get('scheduling_facts')):
        data['policies'] = bound_policies(data.get('policies') or [], data, read_quality=read_quality_for(data))
        data['_catalogue_v2_materialized'] = True
    tags = data.get('tags') or {}
    if not tags:
        return data
    for t in tags.values():
        for pid in t['members'].get('people', []):
            person = data.get('people', {}).get(pid)
            if person is not None and t['name'].lower() not in {h.lower() for h in person.get('tags') or []}:
                person['tags'] = (person.get('tags') or []) + [t['name']]

    def policy(p):
        d = p['definition']
        if (d.get('rule') or {}).get('kind') == 'catalogue':  # a catalogue rule names tags on its lines' who and where
            return {**p, 'definition': {**d, 'rule': {**d['rule'],
                'settings': {kind: _tagged_rule(data, values, kind) for kind, values in (d['rule'].get('settings') or {}).items()},
                'lines': [_tagged_line(data, line) for line in d['rule'].get('lines') or []]}}}
        rows = [{k: _named(tags, k, v) if k in TAG_FIELDS and isinstance(v, list) else v for k, v in r.items()}
                for r in d.get('per_person') or []]
        return {**p, **({'_v2_source_lines': {n: _tagged_line(data, line) for n, line in p['_v2_source_lines'].items()}}
                      if '_v2_source_lines' in p else {}),
                **({'_v2_base_variants': [{**variant, 'line': _tagged_line(data, variant['line'])}
                                         for variant in p['_v2_base_variants']]}
                   if '_v2_base_variants' in p else {}),
                'definition': {**d, **({'rule': _tagged_rule(data, d['rule'])} if 'rule' in d else {}), 'scope': tag_scope(data, d['scope']),
                               **({'per_person': rows} if 'per_person' in d else {})}}
    data['policies'] = [policy(p) for p in data.get('policies') or []]
    return data


def _tagged_line(data, line):
    """A catalogue line with the tags its who and where name read as their members. A who naming people by a tag is a
    group (`who_group`), not a person: a person's own line is more specific than it."""
    tags = data['tags']
    out = dict(line)
    for side in ('who', 'where'):
        if line.get(side):
            out[side] = tag_scope(data, {'conditions': line[side]})['conditions']
    if line.get('settings'):
        out['settings'] = {kind: _tagged_rule(data, values, kind) for kind, values in line['settings'].items()}
    if line.get('source_default'):
        out['source_default'] = _tagged_line(data, line['source_default'])
    if any(v in tags for values in necessary_person_values(line.get('who')) for v in values):
        out['who_group'] = True
    return out


def _tagged_rule(data, rule, kind=None):
    """Embedded selectors use the same tag expansion as the rule's scope."""
    out = dict(rule)
    for key, many in PRIMITIVE_CONDITION_FIELDS.get(kind or rule.get('kind'), {}).items():
        if rule.get(key):
            trees = rule[key] if many else [rule[key]]
            resolved = [tag_scope(data, {'conditions': tree})['conditions'] for tree in trees]
            out[key] = resolved if many else resolved[0]
    return out


def tag_scope(data, scope):
    """A scope with the tags it names read as their members (see with_tags)."""
    tags = data.get('tags') or {}
    if not tags:
        return scope

    def tree(t):
        def line(c):
            operand = c.get('value')
            values = absolute_values(c)
            if operand is None:
                return {**c, 'values': _named(tags, c['field'], values or [], replace=has_each(c) or c.get('op') == 'all')}
            if values is not None and c['field'] in TAG_FIELDS | {'tags': ''}:
                return {**c, 'value': {'kind': 'absolute', 'value': _named(tags, c['field'], values,
                                                                        replace=c.get('op') == 'all')}}
            return c
        return {**t, 'conditions': [tree(c) if 'conditions' in c else line(c) for c in t['conditions']]}
    out = {k: _named(tags, k, v) if (k in TAG_FIELDS or k == 'tags') and isinstance(v, list) else v for k, v in scope.items()}
    if scope.get('conditions'):
        out['conditions'] = tree(scope['conditions'])
    return out


def _named(tags, field, values, replace=False):
    """Values plus, for each tag among them, its name (a tags line) or its members of the line's kind."""
    if replace:
        return list(dict.fromkeys(m for v in values for m in (
            [tags[v]['name']] if field == 'tags' and v in tags else
            tags[v]['members'].get(TAG_FIELDS[field], []) if field in TAG_FIELDS and v in tags else [v])))
    extra = [m for v in values if v in tags
             for m in ([tags[v]['name']] if field == 'tags' else tags[v]['members'].get(TAG_FIELDS.get(field), []))]
    return list(dict.fromkeys(list(values) + extra)) if extra else values


def _operand_line_values(line, holds, context, row):
    from packages.contracts.schemas import condition_line_definition
    field, op = line['field'], line.get('op', 'any')
    metadata = condition_line_definition(field, context.table_key if context else None, op, line.get('value'))
    kind = metadata['value_kind'] if metadata else None
    # Collection readers bind every field to one candidate person; generic readers use scalar row values.
    held = (row[field] if kind in {'date', 'text', 'number'} and row is not None and field in row
            and getattr(holds, 'row', None) is not row else holds(field))
    if field == 'date_set_ids' and line.get('value') is not None and hasattr(holds, 'date_sets'):
        held = holds.date_sets(absolute_values(line), line.get('entry_names'))
    # None from a rule projection means unknown, whereas a row's null means empty.
    if held is None and row is None:
        values = absolute_values(line) or []
        return True, {value for value in values if isinstance(value, (str, int, float, bool))}, None
    empty = held is None or held == '' or held == [] or held == set() or held == ()
    actual = set(held) if isinstance(held, (list, tuple, set, frozenset)) else set() if empty else {held}
    if op in {'empty', 'not_empty'}:
        return (empty if op == 'empty' else not empty), actual, actual
    if empty:
        return op in {'none', 'is_not', 'not_contains'}, set(), actual
    context = context or getattr(holds, 'condition_context', None)
    if callable(context):
        context = context()
    if context is None and _has_relative(line.get('value')) and hasattr(holds, 'relative_context'):
        context = _rule_context(*holds.relative_context)
    if context is None and _has_relative(line.get('value')):
        raise ValueError('Relative conditions need an evaluation context')
    operands = line.get('value') if isinstance(line.get('value'), (list, tuple)) else (line.get('value'),)
    if (any(isinstance(value, dict) and value.get('token') == 'its_target' for value in operands) and
        row is not None and 'target' in row and row['target'] is None):
        return False, set(), actual
    operand = line.get('value')
    if kind == 'date' and operand is None:
        wanted = {_date_value(value, context) for value in absolute_values(line) or []}
        actual = {shown_date(value, context, metadata) for value in actual}
        selected = wanted & actual
        matched = bool(wanted) and wanted <= actual if op == 'all' else not selected if op == 'none' else bool(selected)
        return matched, selected, actual
    if op == 'between':
        first, last = (resolve_operand(value, context, field, row=row) for value in operand)
        if kind == 'date':
            bounds = [min(first['from'], last['from']), max(first['to'], last['to'])]
            bounds = [date.fromisoformat(value) for value in bounds]
            actual = {shown_date(value, context, metadata) for value in actual}
        else:
            bounds = sorted([first['value'], last['value']])
        matched = any(bounds[0] <= value <= bounds[1] for value in actual)
        return matched, actual if matched else set(), actual
    resolved = resolve_operand(operand, context, field, row=row)
    wanted = resolved['value']
    if kind == 'date':
        start, end = date.fromisoformat(resolved['from']), date.fromisoformat(resolved['to'])
        actual = {shown_date(value, context, metadata) for value in actual}
        matched = any((value < start if op == 'before' else value > end if op == 'after' else
                       value <= end if op == 'on_or_before' else value >= start if op == 'on_or_after' else
                       not start <= value <= end if op in {'none', 'is_not'} else start <= value <= end)
                      for value in actual)
    elif kind == 'number':
        matched = any((value > wanted if op == 'gt' else value >= wanted if op == 'gte' else
                       value < wanted if op == 'lt' else value <= wanted if op == 'lte' else
                       value != wanted if op == 'is_not' else value == wanted) for value in actual)
    elif kind == 'text':
        wanted = str(wanted).lower()
        matched = any((wanted in str(value).lower() if op == 'contains' else
                       wanted not in str(value).lower() if op == 'not_contains' else
                       str(value).lower().startswith(wanted) if op == 'starts_with' else
                       str(value).lower().endswith(wanted) if op == 'ends_with' else
                       str(value).lower() != wanted if op == 'is_not' else str(value).lower() == wanted)
                      for value in actual)
    else:
        wanted = set(wanted) if isinstance(wanted, (list, tuple, set, frozenset)) else {wanted}
        if field == 'tags':
            wanted, actual = {str(v).lower() for v in wanted}, {str(v).lower() for v in actual}
        selected = wanted & actual
        matched = bool(wanted) and wanted <= actual if op == 'all' else not selected if op in {'none', 'is_not'} else bool(selected)
        return matched, selected, actual
    return matched, actual if matched else set(), actual


def _has_relative(operand):
    return any(_has_relative(value) for value in operand) if isinstance(operand, (list, tuple)) else (
        isinstance(operand, dict) and operand.get('kind') == 'relative')


@lru_cache(maxsize=8192)
def _is_date_field(field, table_key=None):
    from packages.contracts.schemas import condition_field_definition
    metadata = condition_field_definition(field, table_key)
    return bool(metadata and metadata['value_kind'] == 'date')


def _line_values(line, holds, context=None, row=None):
    if line['field'] not in INTERNAL and (line.get('value') is not None or line.get('op') in {'empty', 'not_empty'}):
        return _operand_line_values(line, holds, context, row)
    if _is_date_field(line['field'], context.table_key if context else None):
        return _operand_line_values(line, holds, context, row)
    listed = absolute_values(line) or []
    held = (holds.date_sets(listed, line.get('entry_names')) if line['field'] == 'date_set_ids' and hasattr(holds, 'date_sets') else
            holds(line['field'], listed) if line['field'] in INTERNAL else holds(line['field']))
    wanted = {v.lower() for v in listed} if line['field'] == 'tags' else set(listed)
    values = wanted if held is None else wanted & held
    op = line.get('op', 'any')
    if op == 'is' or op == 'is_not':
        op = 'any' if op == 'is' else 'none'
    matched = held is None or (bool(wanted) and wanted <= held if op == 'all' else not values if op == 'none' else bool(values))
    return matched, values, held


def evaluate(tree, holds, context=None, *, row=None, match_line=None, all_of=all, any_of=any):
    """Match a line or tree. `holds(field)` is the set held, or None when unknown here: an unknown line counts as met, so a
    session-only check selects every session some person could match; the per-person check then decides.
    SQL readers supply leaf/combine operations, retaining this same group traversal before paging."""
    if 'field' in tree and 'conditions' not in tree:
        return _line_values(tree, holds, context, row)[0] if match_line is None else match_line(tree, holds)
    results = []
    for c in tree.get('conditions') or []:
        if 'conditions' in c:
            results.append(evaluate(c, holds, context, row=row, match_line=match_line,
                                    all_of=all_of, any_of=any_of))
            continue
        results.append(_line_values(c, holds, context, row)[0] if match_line is None else match_line(c, holds))
    return bool(results) and (all_of(results) if tree.get('match', 'all') == 'all' else any_of(results))


def evaluate_candidates(tree, holds, candidates, context=None, *, person_fields=PERSON_FIELDS, row=None, match_line=None):
    """Participant sets use all visible people; other positives share one person and negatives exclude everyone."""
    def match(line, witness):
        personal = line['field'] in person_fields
        typed_negative = personal and line.get('op') in {'is_not', 'not_contains'}
        participant_set = line['field'] == 'person_ids' and line.get('op') == 'all'
        source = holds if participant_set or personal and (typed_negative or line.get('op') == 'none') else witness
        if typed_negative:
            line = {**line, 'op': 'is' if line['op'] == 'is_not' else 'contains'}
        matched = (evaluate(line, source, context, row=row) if match_line is None else match_line(line, source))
        return not matched if typed_negative else matched

    return any(evaluate(tree, witness, context, row=row, match_line=match) for witness in candidates or [holds])


def _possible(requirements, holds):
    """A branch's unknown fields must admit one witness; only its listed values are considered."""
    fields = {}
    for field, op, values in requirements:
        clauses = fields.setdefault(field, {'any': [], 'all': set(), 'none': set()})
        if op == 'any':
            clauses['any'].append(values)
        else:
            clauses[op].update(values)
    for field, clauses in fields.items():
        forbidden, required = clauses['none'], clauses['all']
        choices = [values - forbidden for values in clauses['any']]
        if required & forbidden or any(not values for values in choices):
            return False
        if field == 'location_ids' and (choices or required):
            from .validation import ancestor_ids
            locations = {'locations': getattr(holds, 'locations', {})}
            candidates = required | set().union(*choices)
            if not any(not (held := set(ancestor_ids(locations, value))) & forbidden
                       and required <= held and all(values & held for values in choices) for value in candidates):
                return False
        elif field in {'person_ids', 'assignment_type_ids'}:
            choices += [required] if required else []
            if len(required) > 1 or choices and not choices[0].intersection(*choices[1:]):
                return False
    return True


def _buckets(node, holds, path, constrained=False):
    if 'conditions' not in node:
        matched, values, held = _line_values(node, holds)
        if not matched:
            return set()
        field = (node['field'], tuple(sorted(set(node.get('entry_names') or ())))) if node['field'] == 'date_set_ids' else node['field']
        requirements = ()
        if constrained and held is None:
            op = node.get('op', 'any')
            indexed = op in {'any', 'all', 'none', 'is', 'is_not'} and absolute_values(node) is not None
            indexed = indexed and node['field'] not in {'date', 'starts_on', 'ends_on', 'start', 'end', 'first_day', 'last_day'}
            if indexed:
                requirements = ((field, {'is': 'any', 'is_not': 'none'}.get(op, op), frozenset(values)),)
        if node.get('count') == 'each' and node.get('op') != 'none':
            return {(((path + ':' + node['field'], str(value)),), requirements + (
                ((field, 'any', frozenset({value})),) if requirements else ())) for value in values}
        return {((), requirements)} if not requirements or _possible(requirements, holds) else set()
    children = [_buckets(c, holds, f'{path}.{n}' if path else str(n), constrained) for n, c in enumerate(node.get('conditions') or [])]
    if not children or node.get('match', 'all') == 'all' and not all(children):
        return set()
    if node.get('match', 'all') == 'any':
        return {(((path + ':group', str(n)),) + key, requirements) for n, paths in enumerate(children)
                for key, requirements in paths} if node.get('count') == 'each' else set().union(*children)
    out = set()
    for paths in product(*children):
        requirements = tuple(clause for _, clauses in paths for clause in clauses)
        if requirements and not _possible(requirements, holds):
            continue
        if node.get('count') == 'each':
            out.update((((path + ':group', str(n)),) + key, requirements) for n, (key, _) in enumerate(paths))
        else:
            out.add((tuple(atom for key, _ in paths for atom in key), requirements))
    return out


def bucket_keys(tree, holds, line='Condition line'):
    """Sorted, unique keys on matching paths. Unknown fields enumerate listed values/branches, including zero buckets.
    AND combines keys; OR joins paths; an each group selects a child after its all/any gate has matched."""
    cache = getattr(holds, 'bucket_cache', None)
    if cache is not None:
        person, shift, a, b = holds.bucket_context
        entry = cache.get(id(tree))
        if entry is None or entry[0] is not tree:
            # Trees stay immutable within a check/run; validate before caching, not on every bucket lookup.
            check_count_bucket_budget(tree, line)
            entry = cache[id(tree)] = (tree, has_person_lines(tree), {})
        cache_key = (person if entry[1] else None, id(shift), a, b)
        cached = entry[2].get(cache_key)
        if cached is not None and cached[0] is shift:
            return cached[1]
    else:
        check_count_bucket_budget(tree, line)
    keys = tuple(sorted({key for key, _ in _buckets(tree, holds, '', has_each(tree))})) if tree else ((),)
    if cache is not None:
        entry[2][cache_key] = (shift, keys)
    return keys


def bucket_atoms(key, field):
    """The selected values of one condition field; bucket path syntax stays here."""
    return tuple(value for path, value in key if path.rpartition(':')[2] == field)


def bucket_count(key, field, paths):
    """Label reached field paths as Each only when their own atom or containing branch is selected."""
    for atom, value in key:
        path, _, kind = atom.rpartition(':')
        if kind == field and path in paths:
            return 'each'
        if kind == 'group':
            child = (path + '.' if path else '') + value
            if any(p == child or p.startswith(child + '.') for p in paths):
                return 'each'
    return 'together'


def matched_paths(tree, holds, key=None):
    """Positive branches that match; exclusions constrain the tree but never appear as reached records."""
    check_count_bucket_budget(tree)
    out = []
    constrained = has_each(tree)
    if key is not None and key not in bucket_keys(tree, holds):
        return out
    def visit(node, path, required):
        if 'conditions' in node:
            if not evaluate(node, holds) or constrained and not _buckets(node, holds, path, True):
                return
            for n, child in enumerate(node['conditions']):
                child_path = f'{path}.{n}' if path else str(n)
                child_key = None
                if required is not None:
                    if node.get('count') == 'each':
                        if not required or required[0] != (path + ':group', str(n)):
                            continue
                        child_key = required[1:]
                    elif node.get('match', 'all') == 'any':
                        child_key = required
                    else:
                        child_key = tuple(atom for atom in required if atom[0].split(':')[0] == child_path
                                          or atom[0].startswith(child_path + '.'))
                    if not any(child_key == possible for possible, _ in _buckets(child, holds, child_path, constrained)):
                        continue
                visit(child, child_path, child_key)
        elif node.get('op', 'any') not in {'none', 'is_not', 'not_contains', 'empty'} and _line_values(node, holds)[0]:
            out.append(path)
    if tree:
        visit(tree, '', key)
    return out


def counting_key(value):
    return tuple(tuple(atom) for atom in (value or {}).get('count_bucket', ()))


def has_each(tree):
    return bool(tree) and (tree.get('count') == 'each' and ('conditions' in tree or tree.get('op') != 'none')
                          or any(has_each(c) for c in tree.get('conditions') or []))


@lru_cache(maxsize=2048)
def _bucket_descriptor(text):
    tree, key = json.loads(text)
    return _bucket_tree(json.dumps(tree, sort_keys=True)), tuple(tuple(atom) for atom in key)


@lru_cache(maxsize=2048)
def _bucket_tree(text):
    # Buckets of the same immutable descriptor tree share its identity for check-scoped evaluation caching.
    return json.loads(text)


def counting_views(data, policies):
    """Bind each counting bucket to the same condition evaluator; old rules retain their exact object and definition."""
    out = []
    def unknown(field, values=()):
        return None
    unknown.locations = data.get('locations', {})
    for policy in policies:
        data.get('_check_budget', lambda: None)()
        scope = policy['definition']['scope']
        tree = scope_tree(scope)
        if 'count_bucket' in policy or not has_each(tree):
            out.append(policy)
            continue
        # Flat programme/rotation scopes select overlaps, rather than membership throughout a duty.
        flat = {k: v for k, v in scope.items() if k not in {'conditions', 'tags', 'capability_ids', 'programme_id', 'rotation_type_id'}}
        tree = {**tree, 'conditions': tree['conditions'] + _requirements(flat)}
        tree = tag_scope(data, {'conditions': tree})['conditions']
        line = policy.get('template_line', {}).get('id') or policy.get('name') or policy['id']
        for key in bucket_keys(tree, unknown, f'Line {line}'):
            selector = {'field': COUNT_BUCKET, 'op': 'any', 'values': [json.dumps([tree, key], sort_keys=True)]}
            bound = {**scope, 'conditions': {'match': 'all', 'conditions': [selector]}}
            out.append({**policy, 'count_bucket': key, '_count_scope': scope,
                        'definition': {**policy['definition'], 'scope': bound}})
    return out


def check_definition_count_budget(data, definition):
    """Check original catalogue lines too: explicit None numbers do not emit engine copies."""
    catalogue = definition['rule']['kind'] == 'catalogue'
    lines = definition['rule']['lines'] if catalogue else [{'who': definition['scope'].get('conditions')}]
    for line in lines:
        tree = {'match': 'all', 'conditions': [line[side] for side in ('who', 'where') if line.get(side)]}
        label = f"Line {line['id']}" if catalogue else 'Scope line'
        check_count_bucket_budget(tag_scope(data, {'conditions': tree})['conditions'], label)


def scope_tree(scope):
    """A rule's conditions, with the older group fields (tags, skills) read as the same lines. None: no conditions."""
    lines = [{'field': f, 'op': 'any', 'values': scope[f]} for f in ('tags', 'capability_ids') if scope.get(f)]
    if scope.get('conditions'):
        lines.append(scope['conditions'])
    return {'match': 'all', 'conditions': lines} if lines else None


def has_session_lines(tree, fields=SESSION_FIELDS):
    return bool(tree) and any(has_session_lines(c, fields) if 'conditions' in c else
                              any(has_session_lines(_bucket_descriptor(v)[0], fields) for v in absolute_values(c) or []) if c['field'] == COUNT_BUCKET else
                              c['field'] in fields
                              for c in tree['conditions'])


def has_person_lines(tree):
    """Whether a tree needs a person, including the original filters inside an internal bucket selector."""
    return bool(tree) and any(has_person_lines(c) if 'conditions' in c else
                              any(has_person_lines(_bucket_descriptor(v)[0]) for v in absolute_values(c) or []) if c['field'] == COUNT_BUCKET else
                              c['field'] not in SESSION_FIELDS
                              for c in tree['conditions'])


def necessary_person_values(tree):
    """Positive Person lists required through ALL or logically equivalent one-condition ANY groups."""
    trees = [tree]
    while trees:
        group = trees.pop()
        if not group or (group.get('match', 'all') != 'all' and len(group.get('conditions') or []) != 1):
            continue
        for line in group.get('conditions') or []:
            if 'conditions' in line:
                trees.append(line)
            elif line['field'] == 'person_ids' and line.get('op', 'any') in {'any', 'is'}:
                values = absolute_values(line)
                if values is not None:
                    yield values


def absolute_values(line):
    """Literal values from a dict/model condition; None means resolution needs a context."""
    operand = line.get('value') if isinstance(line, dict) else getattr(line, 'value', None)
    if operand is None:
        # Internal selectors cache by the stored list's identity; readers never mutate it.
        values = line.get('values') if isinstance(line, dict) else getattr(line, 'values', None)
        return values if values is not None else []

    def literals(value):
        if hasattr(value, 'model_dump'):
            value = value.model_dump(mode='json')
        if isinstance(value, dict):
            if value.get('kind') == 'relative':
                return None
            if value.get('kind') == 'absolute':
                return literals(value['value'])
        if isinstance(value, (list, tuple)):
            result = []
            for item in value:
                values = literals(item)
                if values is None:
                    return None
                result.extend(values)
            return result
        return [] if value is None else [value]

    return literals(operand)


def has_dated_person_lines(tree):
    return bool(tree) and any(has_dated_person_lines(c) if 'conditions' in c else
                              any(has_dated_person_lines(_bucket_descriptor(v)[0]) for v in absolute_values(c) or []) if c['field'] == COUNT_BUCKET else
                              c['field'] in DATED_PERSON_FIELDS
                              for c in tree['conditions'])


def rule_holds(data, person_id, shift, a, b, *, rotation_at=None):
    """Values throughout [a, b); quota attribution may bind placement to the duty's start instant."""
    from datetime import timedelta
    from .validation import ancestor_ids, capabilities_for, categories_for, person_rows, valid_dates
    zone = data['organisation']['timezone']
    rotation_a, rotation_b = (rotation_at, rotation_at + timedelta(microseconds=1)) if rotation_at is not None else (a, b)

    def date_sets(set_ids=None, entry_names=None):
        if shift is None:
            return None
        from zoneinfo import ZoneInfo
        from .date_sets import matching_date_sets
        day = aware(shift.get('start') or a).astimezone(ZoneInfo(zone)).date()
        return matching_date_sets(data, day, set_ids, entry_names)

    def dated(name, key, definitions):
        return {r[key] for r in person_rows(data, name, person_id) if r.get(key) and valid_dates(r, a, b, zone)
                and valid_dates(data.get(definitions, {}).get(r[key], {'active': False}), a, b, zone)}

    def current_rotation(row):
        cohort = data.get('cohorts', {}).get(row['cohort_id'], {'active': False})
        programme = data.get('programmes', {}).get(cohort.get('programme_id'), {'active': False})
        return (valid_dates(row, rotation_a, rotation_b, zone) and valid_dates(cohort, rotation_a, rotation_b, zone)
                and (not cohort.get('programme_id') or valid_dates(programme, rotation_a, rotation_b, zone)))

    def holds(field, values=()):
        if field == COUNT_BUCKET:
            return {value for value in values if (lambda pair: pair[1] in bucket_keys(pair[0], holds))(_bucket_descriptor(value))}
        if field in SESSION_FIELDS:
            if shift is None:
                return None
            if field == 'date':
                from zoneinfo import ZoneInfo
                return aware(shift.get('start') or a).astimezone(ZoneInfo(zone)).date()
            if field == 'date_set_ids':
                return date_sets(values or None)
            if shift.get('_date_projection'):
                return None
            if field == 'usual_assignee_ids':
                from .recurrence import usual_assignee_ids
                return None if shift.get('_quota_subject_context') else usual_assignee_ids(shift)
            if field == 'service_ids':
                return set(shift['service_ids'])
            if field == 'location_ids':
                return set(ancestor_ids(data, shift['location_id'])) if shift.get('location_id') else set()
            return {shift['assignment_type_id']}
        if person_id is None:
            return None
        if (shift is None and person_id in data.get('_project_clinic_skills', ())
                and field in INTERNAL | {'capability_ids'}):
            return None
        if field == 'person_ids':
            return {person_id}
        if field == OWN_LINE:
            return {o['id'] for o in selector_policies(data, values, person_id=person_id) if valid_dates(o, a, b, zone)}
        if field == OUTRANKED:
            by_person, others = _winners(data, values)
            return {c['key'] for c in (*by_person.get(person_id, ()), *others) if valid_dates(c, a, b, zone)
                    and scope_matches_throughout(data, c['definition']['scope'], person_id, shift, a, b, rotation_at=rotation_at)}
        if field == INHERITED_LEVEL:
            return {value for value in values if _inherited_level(data, value, person_id, a, b, shift=shift)}
        if field == OVERRIDDEN:
            return {o['id'] for o in selector_policies(data, values) if valid_dates(o, a, b, zone)
                    and scope_matches_throughout(data, o['definition']['scope'], person_id, shift, a, b, rotation_at=rotation_at)}
        if field == 'tags':
            return {t.lower() for t in data['people'].get(person_id, {}).get('tags') or []}
        if field == 'category_ids':
            return categories_for(data, person_id, a, b)
        if field == 'capability_ids':
            if shift is not None and not shift.get('_date_projection'):
                return capabilities_for(data, person_id, shift, a, b)
            return {g['capability_id'] for g in person_rows(data, 'grants', person_id) if not g.get('revoked')
                    and valid_dates(g, a, b, zone) and valid_dates(data['capabilities'].get(g['capability_id'], {'active': False}), a, b, zone)}
        if field == 'programme_ids':
            return dated('programme_memberships', 'programme_id', 'programmes')
        if field == 'eligible_service_ids':  # clinics the person may work throughout [a, b)
            return {r['service_id'] for r in person_rows(data, 'eligibilities', person_id) if valid_dates(r, a, b, zone)}
        if field == 'rotation_type_ids':
            return {r['rotation_type_id'] for r in person_rows(data, 'rotations', person_id)
                    if r.get('rotation_type_id') and current_rotation(r)
                    and valid_dates(data.get('rotation_types', {}).get(r['rotation_type_id'], {'active': False}), rotation_a, rotation_b, zone)}
        if field == 'cohort_ids':
            # Cohort membership follows a current rotation, never one that ended or has not started.
            return {r['cohort_id'] for r in person_rows(data, 'rotations', person_id)
                    if current_rotation(r)}
        return set()
    holds.locations = data.get('locations', {})
    holds.date_sets = date_sets
    holds.bucket_cache = data.get('_bucket_keys_cache')
    holds.bucket_context = (person_id if rotation_at is None else (person_id, rotation_at), shift, a, b)
    holds.relative_context = (data, shift, a)
    return holds


def _rule_context(data, shift, start):
    from zoneinfo import ZoneInfo
    organisation = data['organisation']
    zone, week_start = organisation['timezone'], organisation.get('week_start')
    if week_start is None:
        raise ValueError('Duty-relative dates require the configured department week start')
    day = aware(shift.get('start') or start).astimezone(ZoneInfo(zone)).date() if shift is not None else None
    cache = data.setdefault('_condition_contexts', {})
    key = (zone, week_start, day)
    if key not in cache:
        cache[key] = EvaluationContext(timezone=zone, week_start=week_start, duty_day=day, context='rule')
    return cache[key]


def source_index(policies, cache=None):
    """Original catalogue applicability indexed once, namespaced by policy and line, including None-only lines."""
    entry = cache.get(id(policies)) if cache is not None else None
    if entry is None or entry[0] is not policies or entry[2] != len(policies):
        from .rule_lines import source_lines
        lines, seen, by_id, by_owner = {}, set(), {}, {}
        for p in policies:  # a reader holding stored catalogue rules (not yet read as copies) still finds their lines
            by_id.setdefault(p['id'], []).append(p)
            by_owner.setdefault((p['id'], (p.get('template_line') or {}).get('person_id')), []).append(p)
            if '_catalogue_sources' not in p and p['definition']['rule']['kind'] != 'catalogue':
                continue
            sources = source_lines(p)
            if id(sources) not in seen:
                lines.update({key: {**source, 'key': key} for key, source in sources.items()})
                seen.add(id(sources))
        entry = (policies, lines, len(policies), {}, {}, {}, {}, by_id, by_owner)
        if cache is not None:
            cache[id(policies)] = entry
    return entry


def selector_policies(data, values, *, person_id=None):
    """Internal selectors inspect their referenced copies, not every expanded rule for every duty."""
    policies, cache = data.get('policies', []), data.get('_line_index_cache')
    if cache is None:
        return (p for p in policies if p['id'] in values and (person_id is None or
                (p.get('template_line') or {}).get('person_id') == person_id))
    entry = source_index(policies, cache)
    return (p for value in values for p in (entry[7].get(value, ()) if person_id is None
            else entry[8].get((value, person_id), ())))


def _winners(data, values):
    """Original source lines referenced by a selector, indexed by explicitly named people when possible."""
    from .validation import named_people
    entry = source_index(data.get('policies', []), data.get('_line_index_cache'))
    found = entry[3].get(id(values))
    if found is None or found[0] is not values:
        by_person, others = {}, []
        for i in values:
            copy = entry[1].get(i)
            named = None if copy is None else named_people(copy['definition']['scope'])
            if copy is not None and named is None:
                others.append(copy)
            for p in named or ():
                by_person.setdefault(p, []).append(copy)
        found = entry[3][id(values)] = (values, by_person, others)
    return found[1], found[2]


def level_selector(value):
    """A source, number, fixed level and optional materialized quota-window dates (internal only)."""
    selector, *window = value.split('@')
    source, number, level = selector.rsplit('#', 2)
    return source, number, level, tuple(window)


def bind_level_window(scope, start, end):
    """Quota windows resolve inherited strength over their own block without changing shared source descriptors."""
    def tree(t):
        return {**t, 'conditions': [tree(c) if 'conditions' in c else
            {**{key: value for key, value in c.items() if key != 'value'},
             'values': [json.dumps([tree(_bucket_descriptor(v)[0]), _bucket_descriptor(v)[1]], sort_keys=True)
                        for v in absolute_values(c) or []]} if c['field'] == COUNT_BUCKET else
            {**{key: value for key, value in c.items() if key != 'value'},
             'values': [f"{v.split('@')[0]}@{start}@{end}" for v in absolute_values(c) or []]} if c['field'] == INHERITED_LEVEL else c
            for c in t['conditions']]}
    return {**scope, 'conditions': tree(scope['conditions'])} if scope.get('conditions') else scope


def _source_points(sources, a, b, zone, cache=None):
    from datetime import date
    if cache is not None and sources:
        key = ('source_points', id(sources), zone)
        known = cache.get(key)
        if known is None or known[0] is not sources:
            points = tuple(sorted({local_midnight(date.fromisoformat(str(source[f])), zone)
                                   for source in sources for f in ('starts_on', 'ends_on') if source.get(f)}))
            cache[key] = (sources, points)
        else:
            points = known[1]
        return set(points[bisect_right(points, a):bisect_left(points, b)])
    return {t for source in sources for f in ('starts_on', 'ends_on') if source.get(f)
            if a < (t := local_midnight(date.fromisoformat(str(source[f])), zone)) < b}


def selector_sources(policies, tree, cache=None):
    """Referenced catalogue descriptors and legacy policy rows, including sources of an inherited level."""
    if not tree:
        return []
    entry = cache.get(id(policies)) if cache is not None else None
    found = entry[5].get(id(tree)) if entry is not None and entry[0] is policies and entry[2] == len(policies) else None
    if found is not None and found[0] is tree:
        return found[1]
    refs, inherited, policy_refs = set(), [], set()
    def visit(t):
        for c in (t or {}).get('conditions') or []:
            if 'conditions' in c:
                visit(c)
            elif c['field'] == COUNT_BUCKET:
                for value in absolute_values(c) or []:
                    visit(_bucket_descriptor(value)[0])
            elif c['field'] == OUTRANKED:
                refs.update(absolute_values(c) or [])
            elif c['field'] in {OVERRIDDEN, OWN_LINE}:
                policy_refs.update(absolute_values(c) or [])
            elif c['field'] == INHERITED_LEVEL:
                for value in absolute_values(c) or []:
                    key, number, _, _ = level_selector(value)
                    refs.add(key)
                    inherited.append((key, number))
    visit(tree)
    if not refs and not policy_refs:
        return []
    entry = source_index(policies, cache)
    index = entry[1]
    for key, number in inherited:
        refs.update(index.get(key, {}).get('inherit', {}).get(number, {}).get('sources', ()))
    sources = [index[key] for key in sorted(refs) if key in index]
    if policy_refs:
        sources.extend(p for p in policies if p['id'] in policy_refs)
    entry[5][id(tree)] = (tree, sources)
    return sources


def _inherited_level(data, value, person, a, b, *, shift=None):
    from datetime import date
    from zoneinfo import ZoneInfo
    from .recurrence import usual_assignee_ids
    from .rule_lines import LEVELS
    from .validation import valid_dates
    key, number, wanted, window = level_selector(value)
    entry, zone = source_index(data.get('policies', []), data.get('_line_index_cache')), data['organisation']['timezone']
    index = entry[1]
    mine = index.get(key)
    setting = (mine or {}).get('inherit', {}).get(number)
    if setting is None:
        return False  # an incomplete source pool never grants an arbitrary inherited level
    block = window or setting.get('block')
    # Nonquota strengths follow this duty's clinic/department-specific skills. A quota still resolves one strongest
    # person/window level, rather than multiplying its budget for each clinic or duty type.
    context_shift = None if block else shift
    context = (context_shift['assignment_type_id'], tuple(sorted(context_shift.get('service_ids') or [])),
               context_shift.get('location_id'), usual_assignee_ids(context_shift)) if context_shift is not None else None
    duty_day = aware(context_shift.get('start') or a).astimezone(ZoneInfo(zone)).date() if context_shift is not None else None
    cache, cache_key = entry[4], (person, context, duty_day, key, number, tuple(block) if block else (a, b))
    if cache_key in cache:
        return wanted == cache[cache_key]
    sources = [index[k] for k in setting['sources'] if k in index]
    timeline_key = (person, context, tuple(setting['sources']), number, setting['fallback'])
    timelines = data['_line_index_cache'].setdefault('timelines', {}) if '_line_index_cache' in data else entry[6]
    timeline = timelines.get(timeline_key)
    if timeline is None:
        dates = data.get('_person_dates_cache')
        if dates is not None:
            if ('points', person) not in dates:
                dates[('points', person)] = _change_points(data, person)
            points = dates[('points', person)]
        else:
            points = _change_points(data, person)
        boundaries = {local_midnight(date.fromisoformat(str(s[f])), zone) for s in sources
                      for f in ('starts_on', 'ends_on') if s.get(f)}
        timeline = timelines[timeline_key] = (tuple(sorted(set(points) | boundaries)), {})
    def at(lo, hi, duty=context_shift):
        duty_context = (duty['assignment_type_id'], tuple(sorted(duty.get('service_ids') or [])),
                        duty.get('location_id'), usual_assignee_ids(duty)) if duty is not None else None
        day = aware(duty.get('start') or lo).astimezone(ZoneInfo(zone)).date() if duty is not None else None
        interval = (duty_context, day, bisect_right(timeline[0], lo), bisect_left(timeline[0], hi))
        if interval in timeline[1]:
            return timeline[1][interval]
        matched = [s for s in sources if valid_dates(s, lo, hi, zone)
                    and scope_matches_throughout(data, s['definition']['scope'], person, duty, lo, hi)]
        nearest = max((s['tier'] for s in matched), default=0)
        resolved = max((s['numbers'][number].get('level', 'mandatory') for s in matched if s['tier'] == nearest),
                       key=LEVELS.index, default=setting['fallback'])
        timeline[1][interval] = resolved
        return resolved
    # A changing level does not create extra quota budgets: one obligation per person/window, at the strongest
    # resolved level encountered while its original subject scope applies. Its contribution scope is unchanged.
    if block:
        data = {**data, '_bucket_keys_cache': None}
        a, b = (local_midnight(date.fromisoformat(str(day)), zone) for day in block)
        points = sorted(set(change_points(data, person, a, b)) | _source_points([mine, *sources], a, b, zone))
        department = next(p['department_id'] for p in data['policies'] if p['id'] == mine['policy_id'])
        scopes = [mine['definition']['scope'], *(s['definition']['scope'] for s in sources)]
        budget = data.get('_quota_subject_context_budget')
        if budget is None:
            budget = timelines.setdefault('scope_context_budget', [SCOPE_CONTEXT_BUDGET])
        levels = []
        for lo, hi in zip(points, points[1:] + [b]):
            for start, end, duty in _scope_duty_contexts(data, scopes, person, lo, hi, department, budget=budget):
                if (valid_dates(mine, start, end, zone)
                    and scope_matches_throughout(data, mine['definition']['scope'], person, duty, start, end)):
                    level = at(start, end, duty)
                    levels.append(level)
                    if level == LEVELS[-1]:  # One exact mandatory witness already establishes the strongest level.
                        cache[cache_key] = level
                        return wanted == level
    else:
        levels = [at(a, b)]
    resolved = max(levels, key=LEVELS.index) if levels else None
    cache[cache_key] = resolved
    return wanted == resolved


def conditions_match(data, tree, person_id, shift, a, b):
    return tree is None or evaluate(tree, rule_holds(data, person_id, shift, a, b))


def scope_applies(data, scope, person_id, shift, a, b):
    """An obligation applies if its complete scope holds at any moment (None shift: person-side reach)."""
    return any(matched for _, _, matched in scope_segments(data, scope, person_id, shift, a, b))


def _bound_bucket_tree(tree, key, path=''):
    """Bind one bucket to ordinary predicates before a projection removes field paths."""
    if 'conditions' not in tree:
        if tree.get('count') == 'each' and tree.get('op') != 'none' and key is not None:
            values = [value for atom, value in key if atom == path + ':' + tree['field']]
            return ({'match': 'all', 'conditions': [
                tree, {**{k: v for k, v in tree.items() if k not in {'value', 'count'}},
                       'op': 'any', 'values': values}]} if values else {'conditions': []})
        return tree
    children = tree.get('conditions') or []
    if key is not None and tree.get('count') == 'each':
        selected = next((int(value) for atom, value in key if atom == path + ':group'), None)
        if selected is None or not 0 <= selected < len(children):
            return {'conditions': []}
        rest = tuple(atom for atom in key if atom[0] != path + ':group')
        return {**tree, 'conditions': [
            _bound_bucket_tree(child, rest if n == selected else None, f'{path}.{n}' if path else str(n))
            for n, child in enumerate(children) if tree.get('match', 'all') == 'all' or n == selected]}
    bound = []
    for n, child in enumerate(children):
        child_path = f'{path}.{n}' if path else str(n)
        child_key = None if key is None else tuple(atom for atom in key
            if atom[0].split(':')[0] == child_path or atom[0].startswith(child_path + '.'))
        if key is not None and tree.get('match', 'all') == 'any' and child_key != key:
            continue
        bound.append(_bound_bucket_tree(child, child_key, child_path))
    return {**tree, 'conditions': bound}


def quota_day_scope(scope):
    """Duty dates, clocks and skills select counted work, not departmental presence."""
    duty_fields = {'date', 'date_set_ids', 'weekdays', 'start_time', 'end_time', 'capability_ids'}
    def project(node):
        if 'conditions' not in node:
            if node.get('field') == COUNT_BUCKET:
                return project({'match': 'any', 'conditions': [
                    _bound_bucket_tree(*_bucket_descriptor(value)) for value in absolute_values(node) or []]})
            return None if node.get('field') in duty_fields else node
        children = [project(child) for child in node.get('conditions', [])]
        if node.get('match', 'all') == 'any' and None in children:
            return None
        kept = [child for child in children if child is not None]
        return {**node, 'conditions': kept} if kept or not children else None
    result = {k: v for k, v in scope.items() if k not in duty_fields}
    if result.get('conditions'):
        result['conditions'] = project(result['conditions'])
    return result


def scope_matches_throughout(data, scope, person_id, shift, a, b, *, rotation_at=None):
    """Counting, qualification and relaxation require the scope throughout the window.

    Quota contributions alone bind rotation/cohort placement to the duty start via rotation_at.
    """
    cache = data.get('_scope_matches_cache')
    key = id(scope), person_id, id(shift), a, b, rotation_at
    if cache is not None and key in cache:
        return cache[key][2]
    result = all(matched for _, _, matched in scope_segments(data, scope, person_id, shift, a, b, rotation_at=rotation_at))
    if cache is not None and len(cache) < 65_536:
        # Keep identity-keyed inputs alive; this cache belongs to one immutable evaluation only.
        cache[key] = scope, shift, result
    return result


def scope_segments(data, scope, person_id, shift, a, b, *, rotation_at=None, extra_points=()):
    """Yield (start, end, matched) using the one complete scope evaluator on each dated segment.

    Reach readers may add transitive rule boundaries without preparing the same scope again per segment.
    """
    from .validation import (categories_for, category_scope_ids, every_day_all_day, person_scope_ids,
                             scope_intervals, session_matches)
    tree = {'match': 'all', 'conditions': _requirements(scope)} if rotation_at is not None else scope_tree(scope)
    if a is not None and b is not None and a >= b:
        yield a, b, False
        return
    if rotation_at is not None and shift is not None:
        # Quota clock filters select the assignment once; dated qualification is checked on every segment below.
        clocks = {k: scope[k] for k in ('weekdays', 'start_time', 'end_time') if k in scope}
        if not every_day_all_day(clocks) and not scope_intervals(data, clocks, person_id, a, b):
            yield a, b, False
            return

    def matches(lo, hi):
        if rotation_at is not None:
            # One full tree for quota credit, including flat fields and placement-aware precedence/buckets.
            return not tree['conditions'] or evaluate(tree, rule_holds(data, person_id, shift, lo, hi, rotation_at=rotation_at))
        if (person_scope_ids(scope) and person_id not in person_scope_ids(scope)
                or category_scope_ids(scope) and not set(category_scope_ids(scope)) & categories_for(data, person_id, lo, hi)
                or not conditions_match(data, tree, person_id, shift, lo, hi)):
            return False
        if shift is None or shift.get('_date_projection'):
            return all(scope_intervals(data, {k: scope[k]}, person_id, lo, hi)
                       for k in ('programme_id', 'rotation_type_id') if scope.get(k))
        return session_matches(data, scope, shift) and (
            lo < hi if every_day_all_day(scope) else bool(scope_intervals(data, scope, person_id, lo, hi)))

    dated = person_id is not None and (has_dated_person_lines(tree) or category_scope_ids(scope)
                                       or scope.get('programme_id') or scope.get('rotation_type_id'))
    if not dated and not extra_points:
        yield a, b, matches(a, b)
        return
    # Source descriptors cache by identity; use the stored tree, not the temporary scope_tree wrapper.
    points = _condition_points(data, scope.get('conditions'), person_id, a, b) if dated else [a]
    if extra_points:
        points = sorted(set(points) | {point for point in extra_points if a < point < b})
    if len(points) == 1:  # No strictly internal boundary: the complete scope is constant over this window.
        yield a, b, matches(a, b)
        return
    yield from ((lo, hi, matches(lo, hi)) for lo, hi in zip(points, points[1:] + [b]))


def change_points(data, person_id, a, b):
    """Local midnights inside [a, b) where what this person holds can change.

    `data['_person_dates_cache']` (opt-in): complete boundaries once per person in an immutable check/run,
    including staffing checks carrying each person's complete dated records. Interval results are fresh lists."""
    cache = data.get('_person_dates_cache')
    if cache is not None:
        key = ('points', person_id)
        if key not in cache:
            cache[key] = _change_points(data, person_id)
        points = cache[key]
    else:
        points = _change_points(data, person_id)
    return [a, *points[bisect_right(points, a):bisect_left(points, b)]]


def _change_points(data, person_id):
    from datetime import date
    from .validation import person_rows
    zone = data['organisation']['timezone']
    def dates(rows):
        return {local_midnight(date.fromisoformat(str(r[f])), zone) for r in rows
                for f in ('starts_on', 'ends_on') if r.get(f)}
    rows, definitions = [], []
    for name, fields in (('memberships', (('category_id', 'categories'),)),
                         ('grants', (('capability_id', 'capabilities'),)),
                         ('programme_memberships', (('programme_id', 'programmes'),)),
                         ('rotations', (('rotation_type_id', 'rotation_types'), ('cohort_id', 'cohorts'))),
                         ('eligibilities', ())):
        mine = tuple(person_rows(data, name, person_id))
        rows.extend(mine)
        definitions.extend(data.get(kind, {}).get(r.get(field), {}) for r in mine for field, kind in fields)
        if name == 'rotations':
            definitions.extend(data.get('programmes', {}).get(data.get('cohorts', {}).get(r.get('cohort_id'), {}).get('programme_id'), {})
                               for r in mine)
    return tuple(sorted(dates(rows) | dates(definitions)))


def _condition_points(data, tree, person_id, a, b):
    points = change_points(data, person_id, a, b)
    if not tree:
        return points
    cache = data.get('_line_index_cache')
    sources = _source_points(selector_sources(data.get('policies', []), tree, cache),
                             a, b, data['organisation']['timezone'], cache)
    return sorted(set(points) | sources) if sources else points


def ever_matches(data, tree, person_id, a, b):
    """Whether the person-side of the tree holds at some moment in [a, b) (quota subjects, the rule preview)."""
    if tree is None:
        return True
    points = _condition_points(data, tree, person_id, a, b)
    return any(conditions_match(data, tree, person_id, None, lo, hi) for lo, hi in zip(points, points[1:] + [b]))


class ScopeContextLimit(ValueError):
    """A bounded subject projection could not establish complete scope reach."""


def _date_predicate_points(lines, a, b, zone):
    from types import SimpleNamespace
    context, points = SimpleNamespace(timezone=zone), set()
    for line in lines:
        operand = line.get('value')
        operands = operand if isinstance(operand, (list, tuple)) else [operand] if operand is not None else []
        literals = ([value for item in operands for value in absolute_values({'value': item}) or []]
                    if operand is not None else absolute_values(line) or [])
        # Allowed duty-relative ranges contain their own day; only absolute bounds can change this predicate.
        for value in literals:
            day = _date_value(value, context)
            days = [day] + ([day + timedelta(days=1)] if day < date.max else [])
            points.update(point for bound in days if a < (point := local_midnight(bound, zone)) < b)
    return points


def scope_has_duty_witness(data, scope, person_id, a, b, department_id, *, limit=4096, budget=None):
    """Quota subjects exist before assignments. Clinic-scoped skills need one coherent possible duty, rather than
    treating skills from every clinic as simultaneously held. Unaffected scopes retain their person-only projection.
    Retained catalogue records describe scope contexts even after retirement. Whether a new duty is currently
    eligible/available is a separate check; it must not erase an empty rota's minimum obligation.
    """
    from .validation import session_matches, session_scope
    requirements = _requirements(scope)
    # Unknown duty/dynamic fields count as met. False therefore proves that this static person selection
    # cannot match any duty, including the negative selections generated when another line replaces it.
    if requirements and not evaluate({'conditions': requirements},
                                    lambda field, values=(): {person_id} if field == 'person_ids' else None):
        return False
    view = {**data, '_bucket_keys_cache': None}  # virtual duties must not retain an unbounded identity cache
    setup = session_scope(scope)
    calendar_possible = not has_session_lines(scope_tree(setup), {'date', 'date_set_ids'})
    opportunity = False
    for lo, hi, shift in _scope_duty_contexts(view, [scope], person_id, a, b, department_id, limit=limit, budget=budget):
        if shift is None:
            return scope_applies(view, scope, person_id, None, lo, hi)
        calendar_possible = calendar_possible or scope_applies(
            view, setup, None, {**shift, '_date_projection': True}, lo, hi)
        opportunity = opportunity or bool(shift.get('_date_projection')) or session_matches(view, setup, shift)
        if scope_applies(view, scope, person_id, shift, lo, hi):
            return True
    if not opportunity and calendar_possible:
        raise ScopeContextLimit('Duty setup does not resolve these clinic-scoped quotas; reconcile their clinics, types and places')
    return False


def _scope_duty_contexts(data, scopes, person_id, a, b, department_id, *, limit=4096, budget=None):
    from .validation import ancestor_ids, person_rows, within_dates
    scopes = list(scopes)
    seen = {id(scope) for scope in scopes}
    for scope in scopes:
        for source in selector_sources(data.get('policies', []), scope.get('conditions'), data.get('_line_index_cache')):
            referred = source['definition']['scope']
            if id(referred) not in seen:
                seen.add(id(referred))
                scopes.append(referred)
    named = {field: set() for field in ('capability_ids', 'service_ids', 'assignment_type_ids', 'location_ids')}
    date_selections, date_conditions, skill_presence = set(), [], False

    def visit(node):
        nonlocal skill_presence
        if 'conditions' in node:
            for child in node.get('conditions') or []:
                visit(child)
        elif node.get('field') == COUNT_BUCKET:
            for value in absolute_values(node) or []:
                visit(_bucket_descriptor(value)[0])
        elif node.get('field') == 'date_set_ids':
            date_selections.add((tuple(sorted(set(absolute_values(node) or []))), tuple(sorted(set(node.get('entry_names') or [])))))
        elif node.get('field') == 'date':
            date_conditions.append(node)
        elif node.get('field') in named:
            named[node['field']].update(absolute_values(node) or [])
            skill_presence |= node['field'] == 'capability_ids' and node.get('op') in {'empty', 'not_empty'}
    for item in scopes:
        visit({'conditions': _requirements(item)})
    zone = data['organisation']['timezone']
    relevant = [g for g in person_rows(data, 'grants', person_id) if not g.get('revoked')
                and (skill_presence or g['capability_id'] in named['capability_ids']) and within_dates(g, a, b, zone)]
    clinic_context = any(g.get('service_id') or g['department_id'] != department_id for g in relevant)
    if not clinic_context and not date_conditions and not date_selections:
        yield a, b, None
        return
    points = {point for item in scopes for point in _condition_points(data, item.get('conditions'), person_id, a, b)}
    points |= _date_predicate_points(date_conditions, a, b, zone)
    if date_selections:
        from zoneinfo import ZoneInfo
        from .date_sets import resolve_date_sets
        last = (b - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
        first = a.astimezone(ZoneInfo(zone)).date()
        for set_ids, names in sorted(date_selections):
            for occurrence in resolve_date_sets(data, first, last, set_ids or None, names):
                days = [date.fromisoformat(occurrence['first_day'])]
                end = date.fromisoformat(occurrence['last_day'])
                if end < last:
                    days.append(end + timedelta(days=1))
                points.update(point for day in days if a < (point := local_midnight(day, zone)) < b)
    points = sorted(points)
    if not clinic_context:
        for n, (lo, hi) in enumerate(zip(points, points[1:] + [b])):
            if n >= limit or budget is not None and budget[0] <= 0:
                raise ScopeContextLimit('These date-scoped quota conditions need a narrower scope or separate reconciliation')
            if budget is not None:
                budget[0] -= 1
            yield lo, hi, {'_date_projection': True, '_quota_subject_context': True, 'start': lo, 'end': hi,
                           'assignment_type_id': None, 'service_ids': (), 'location_id': None}
        return
    grants = [g for g in relevant if g['department_id'] == department_id]
    from packages.contracts.schemas import ShiftInput
    max_services = next(bound.max_length for bound in ShiftInput.model_fields['service_ids'].metadata
                        if getattr(bound, 'max_length', None) is not None)

    # Equal unnamed clinics have identical scope membership and dated skill grants. Selecting more than one from
    # such a class changes neither membership nor the shared-duty skill intersection, so one representative suffices.
    services = {}
    for sid, service in sorted(data.get('services', {}).items()):
        if service.get('department_id') != department_id:
            continue
        key = (sid if sid in named['service_ids'] else None,
               tuple(sorted({(g['capability_id'], str(g.get('starts_on') or ''), str(g.get('ends_on') or ''))
                             for g in grants if g.get('service_id') == sid})))
        services.setdefault(key, sid)
    types = {}
    for tid, typ in sorted(data.get('types', {}).items()):
        if typ.get('department_id') != department_id:
            continue
        definition = typ.get('definition') or {}
        key = (tid if tid in named['assignment_type_ids'] else None, definition.get('purpose', 'staffed'),
               bool(definition.get('allow_shared_services')), tuple(sorted(definition.get('allowed_location_type_ids') or [])))
        types.setdefault(key, tid)
    allowed = sorted({key[3] for key in types})
    locations = {}
    for lid, location in sorted(data.get('locations', {}).items()):
        if location.get('department_id') != department_id:
            continue
        key = (tuple(sorted(named['location_ids'] & set(ancestor_ids(data, lid)))),
               tuple(not kinds or location.get('location_type_id') in kinds for kinds in allowed))
        locations.setdefault(key, lid)
    if not types or not locations:
        raise ScopeContextLimit('Duty types and locations are required to resolve clinic-scoped quota subjects')
    examined = 0
    for lo, hi in zip(points, points[1:] + [b]):
        available_services = list(services.values())
        for key, tid in types.items():
            _, purpose, shared, permitted = key
            for lid in locations.values():
                if permitted and data['locations'][lid].get('location_type_id') not in permitted:
                    continue
                for size in range(0 if purpose != 'staffed' else 1, min(max_services if shared else 1, len(available_services)) + 1):
                    for clinics in combinations(available_services, size):
                        examined += 1
                        if examined > limit or budget is not None and budget[0] <= 0:
                            raise ScopeContextLimit('These clinic-scoped quota conditions need a narrower scope or separate reconciliation')
                        if budget is not None:
                            budget[0] -= 1
                        # A future quota duty has no frozen usual providers yet.
                        shift = {'id': 'quota-subject-context', '_quota_subject_context': True, 'assignment_type_id': tid,
                                 'location_id': lid, 'service_ids': clinics, 'start': lo, 'end': hi}
                        yield lo, hi, shift
    if not examined:
        raise ScopeContextLimit('No supported duty context is available to resolve clinic-scoped quota subjects')


def _narrowed(policy, lines, **dates):
    """A copy of a rule (same id, so findings name it) that also needs `lines`, with its dates cut to `dates`."""
    definition = policy['definition']
    scope = dict(definition['scope'])
    if lines:
        scope['conditions'] = {'match': 'all', 'conditions': ([scope['conditions']] if scope.get('conditions') else []) + lines}
    item = {**policy, 'definition': {**definition, 'scope': scope, 'rule': dict(definition['rule']), 'per_person': [], 'overrides': []}}
    if dates.get('starts_on') and (not item.get('starts_on') or dates['starts_on'] > item['starts_on']):
        item['starts_on'] = dates['starts_on']
    if dates.get('ends_on') and (not item.get('ends_on') or dates['ends_on'] < item['ends_on']):
        item['ends_on'] = dates['ends_on']
    return item


def resolved_rule_context(data):
    """The complete runtime rule context, including catalogue lines, facts and mandatory caps."""
    context = with_tags(dict(data))
    context.setdefault('_overtime_originals', {p['id']: p for p in context.get('policies') or []})
    context['policies'] = active_rules(context.get('policies', []), context=context)
    return context


def active_rules(policies, overrides=True, context=None, *, read_quality=None):
    """Active rules as every check reads them (the validator, the solver, quotas, the "Why?" panel).

    A rule with lines becomes one copy per line (that person or programme, its clinics, duty types, days, hours and
    dates, its number) plus the rule's own number for everyone else it matches — unless the rule is for its lines only;
    a nonsafety rule another active rule overrides stops applying to that rule's people while it is in force.
    Safety rules keep every matching default and person-line obligation, and cannot be overridden. Every copy keeps
    the rule's id. Idempotent: the copies carry no lines or overrides of their own."""
    from packages.contracts.schemas import line_field, line_rule, safety_rule
    from .rule_lines import bound_policies, expand, read_quality_for, source_edits
    if isinstance(policies, dict):
        context, policies = policies, policies.get('policies') or []
    read_quality = read_quality if read_quality is not None else read_quality_for(context)
    policies = bound_policies(policies, context, read_quality=read_quality)
    live = [p for p in policies if p['status'] == 'active' and p.get('active', True)
            and not p.get('_v2_source_projection_only')]
    replaced_by = {}
    if overrides:
        targets = {target for p in live for target in p['definition'].get('overrides') or []}
        protected = {p['id'] for p in live if p['id'] in targets and safety_rule(p['definition'])}
        for p in live:
            for target in p['definition'].get('overrides') or []:
                if target in protected:
                    continue  # Historical override records never waive a protected safety obligation.
                # once per bound a range is checked as (as when ranges were split before their lines)
                replaced_by.setdefault(target, []).extend([p['id']] * (2 if _is_range(p['definition']['rule']) else 1))
    out = []
    for p in live:
        if context and (check := context.get('_check_budget')):
            check()
        # A catalogue rule's lines are its copies (packages/domain/rule_lines.py); nothing overrides it.
        out += expand(p) if p['definition']['rule']['kind'] == 'catalogue' else source_edits(_copies(p, replaced_by, line_field, line_rule))
    return out


def _is_range(rule):
    return rule['kind'] == 'block_quota' and rule.get('comparison') == 'range'


def _parts(copies):
    """A quota is checked by its bounds and its target, each a copy with the rule's id: a range as its minimum and its
    maximum, a target as an advisory target. All minimums first, then maximums, then targets (the order checks read).
    The copies carry no target fields, so a quota saved with an empty target reads exactly as one saved before targets."""
    if not any(c['definition']['rule']['kind'] == 'block_quota' and (_is_range(c['definition']['rule']) or 'target_amount' in c['definition']['rule']
                                                                     or 'target_strength' in c['definition']['rule']) for c in copies):
        return copies

    def part(c, role):
        d, rule = c['definition'], c['definition']['rule']
        bare = {k: v for k, v in rule.items() if k not in ('target_amount', 'target_strength')}
        if role == 'target':
            if rule.get('target_amount') is None:
                return None
            return {**c, 'definition': {**d, 'severity': 'advisory', 'strength': rule.get('target_strength') or d.get('strength') or 'medium',
                    'exception_allowed': False, 'rule': {**bare, 'comparison': 'target', 'amount': rule['target_amount'], 'maximum_amount': None}}}
        if not _is_range(rule):
            return None if role == 'maximum' else {**c, 'definition': {**d, 'rule': bare}}
        return {**c, 'definition': {**d, 'rule': {**bare, 'comparison': role, 'amount': rule['amount'] if role == 'minimum' else rule['maximum_amount'], 'maximum_amount': None}}}
    return [x for role in ('minimum', 'maximum', 'target') for c in copies if (x := part(c, role)) is not None]


def _copies(p, replaced_by, line_field, line_rule):
    """A rule as the copies its lines and overrides make (each keeps the rule's id)."""
    from packages.contracts.schemas import safety_rule
    out = []
    d = p['definition']
    rows = d.get('per_person') or []
    template_people = p.get('_quota_template_people')
    quota_indices = p.get('_quota_line_indices') if p.get('_v2_source_lines') is None else None
    if not rows and not template_people and not d.get('overrides') and p['id'] not in replaced_by:
        return [p]
    if template_people is not None or quota_indices is not None:
        p = {key: value for key, value in p.items() if key not in ('_quota_template_people', '_quota_line_indices')}
    replaced = [{'field': OVERRIDDEN, 'op': 'none', 'values': replaced_by[p['id']]}] if p['id'] in replaced_by else []
    # A quota line is that person's block, so listed people follow only their lines; a nonsafety limit's line replaces
    # the rule's number on the line's dates, and the rule's own number still holds for them on other dates.
    # A rule for its listed people only has no copy for anyone else.
    listed = template_people if template_people is not None else sorted({r['person_id'] for r in rows if r.get('person_id')})
    others = ([] if not listed or safety_rule(d) else [{'field': 'person_ids', 'op': 'none', 'values': listed}] if d['rule']['kind'] == 'block_quota'
              else [{'field': OWN_LINE, 'op': 'none', 'values': [p['id']]}])
    if not d.get('listed_only'):
        default = _narrowed(p, replaced + others)
        if p.get('_v2_base_variants'):
            default['_v2_base_exclusions'] = replaced + others
        out.append(default)
    field = line_field(d)
    for n, r in enumerate(rows):
        # A line is a narrower copy of the rule: who (a person, or a programme's members, as the scope names them),
        # and optionally its own clinics, duty types, days and hours.
        who = [{'field': 'person_ids', 'op': 'any', 'values': [r['person_id']]}] if r.get('person_id') else []
        types = [{'field': 'assignment_type_ids', 'op': 'any', 'values': r['assignment_type_ids']}] if r.get('assignment_type_ids') else []
        item = _narrowed(p, replaced + who + types, starts_on=r.get('starts_on'), ends_on=r.get('ends_on'))
        scope = item['definition']['scope']
        if r.get('programme_id'):
            scope['programme_id'] = r['programme_id']
        if r.get('category_ids'):
            scope.update(category_id=None, category_ids=r['category_ids'])
        if r.get('service_ids'):
            scope.update(service_id=None, service_ids=r['service_ids'])
        if r.get('weekdays') is not None:
            scope['weekdays'] = r['weekdays']
        if r.get('start_time') is not None:
            scope.update(start_time=r['start_time'], end_time=r['end_time'])
        # Its number, its own settings (a trigger, supervisors, trainee ranks) and a quota line's block.
        item['definition']['rule'] = line_rule(d['rule'], r, field)
        item['template_line'] = {**(p.get('_v2_row_templates') or rows)[n], 'line': quota_indices[n] if quota_indices else n}
        out.append(item)
    return out


def _requirements(scope):
    """A scope as lines that must all hold: the older flat fields as lines, then its own conditions."""
    def one(many, single):
        return scope.get(many) or ([scope[single]] if scope.get(single) else [])
    lines = [{'field': f, 'op': 'any', 'values': v} for f, v in (
        ('person_ids', one('person_ids', 'person_id')), ('category_ids', one('category_ids', 'category_id')),
        ('service_ids', one('service_ids', 'service_id')), ('programme_ids', one('', 'programme_id')),
        ('rotation_type_ids', one('', 'rotation_type_id')), ('assignment_type_ids', one('', 'assignment_type_id')),
        ('location_ids', one('', 'location_id')), ('tags', scope.get('tags') or []),
        ('capability_ids', scope.get('capability_ids') or [])) if v]
    tree = scope.get('conditions')
    if tree:
        lines += tree['conditions'] if tree.get('count') != 'each' and (tree.get('match', 'all') == 'all' or len(tree['conditions']) == 1) else [tree]
    return lines


def _covers(q, r):
    """Line (or group) q guarantees line r."""
    if q == r:
        return True
    def op(line):
        return {'is': 'any', 'is_not': 'none'}.get(line.get('op'), line.get('op', 'any'))
    if 'conditions' in q or 'conditions' in r or q['field'] != r['field'] or op(q) != op(r):
        return False
    if set(q.get('entry_names') or ()) != set(r.get('entry_names') or ()):
        return False
    q_values, r_values = absolute_values(q), absolute_values(r)
    if q_values is None or r_values is None or op(q) not in {'any', 'all', 'none'}:
        return False  # Scalar/range and unresolved relative implications need an explicit proof.
    norm = (lambda v: {x.lower() for x in v}) if q['field'] == 'tags' else set
    return norm(q_values) <= norm(r_values) if op(q) == 'any' else norm(q_values) >= norm(r_values)


def implies(scope, other):
    """Whether everyone and every duty `scope` matches is also matched by `other`. A sure, conservative check:
    each requirement of `other` is guaranteed by one of `scope`, and its days and hours lie inside `other`'s."""
    days = set(scope.get('weekdays', range(7))) <= set(other.get('weekdays', range(7)))
    s0, s1 = scope.get('start_time', '00:00'), scope.get('end_time', '24:00')
    o0, o1 = other.get('start_time', '00:00'), other.get('end_time', '24:00')
    hours = (o0, o1) == ('00:00', '24:00') or (s0, s1) == (o0, o1) or (s0 < s1 and o0 < o1 and o0 <= s0 and s1 <= o1)
    mine = _requirements(scope)
    return days and hours and all(any(_covers(q, r) for q in mine) for r in _requirements(other))
