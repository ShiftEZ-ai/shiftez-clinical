"""Plain-language rule explanations shared by the API and private reports.

The resolver is deliberately a thin presentation layer over ``active_rules``.  It never
decides whether a line wins: the copies returned by the same resolver used by the
checker are filtered for the requested person/rank and rendered here.
"""
import logging
import re
from collections import defaultdict
from datetime import date, timedelta
from pydantic import ValidationError
from packages.contracts.schemas import CONDITION_FIELDS, CONDITION_OPERATORS, safety_rule
from packages.contracts.rule_catalogue import (QUESTION_GROUPS, line_comparison,
                                                question_group_entry, question_group_for_effective_kind)

from .conditions import (INHERITED_LEVEL, OUTRANKED, OVERRIDDEN, OWN_LINE, absolute_values,
                         active_rules, has_session_lines, scope_applies, scope_tree,
                         scope_has_duty_witness)
from .rule_map import rule_number
from .time import local_midnight
from .validation import index_person_rows, person_active_end, within_dates

logger = logging.getLogger(__name__)


class RuleExplanationInputError(ValueError):
    """A stored rule cannot be rendered because its explanation input is malformed."""

WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
WHO_FIELDS = {'person_ids', 'person_id', 'category_ids', 'category_id', 'programme_ids', 'programme_id',
              'rotation_type_ids', 'rotation_type_id', 'cohort_ids', 'cohort_id', 'tags', 'capability_ids'}
WHERE_FIELDS = {'service_ids', 'service_id', 'location_ids', 'location_id', 'assignment_type_ids', 'assignment_type_id'}
_KIND_LABELS = {
    'max_duties': ('duties', 'maximum'), 'max_hours': ('hours', 'maximum'),
    'max_consecutive_days': ('days in a row', 'maximum'),
    'max_consecutive_weekends': ('weekends in a row', 'maximum'),
    'min_rest': ('hours of rest between duties', 'minimum'),
    'travel_buffer': ('minutes between duties', 'minimum'),
    'minimum_cover': ('people', 'minimum'), 'maximum_participants': ('people', 'maximum'),
    'supervision_limit': ('trainees', 'maximum'), 'team_lead_requirement': ('team leads', 'minimum'),
    'run_length': ('working days in a row', 'maximum'), 'days_off_run': ('days off together', 'minimum'),
}


def _tree_values(tree, fields, *, positive_only=False):
    values = defaultdict(list)

    def visit(node):
        if not node:
            return
        if 'conditions' in node:
            for child in node.get('conditions') or ():
                visit(child)
            return
        field = node.get('field')
        if field not in fields or positive_only and node.get('op', 'any') in {'none', 'is_not'}:
            return
        for value in absolute_values(node) or ():
            values[field].append(value)

    visit(tree)
    return values


def _scope_values(scope, fields, *, include_conditions=True, positive_only=False):
    values = defaultdict(list)
    aliases = {
        'person_id': 'person_ids', 'category_id': 'category_ids', 'programme_id': 'programme_ids',
        'rotation_type_id': 'rotation_type_ids', 'cohort_id': 'cohort_ids', 'service_id': 'service_ids',
        'location_id': 'location_ids', 'assignment_type_id': 'assignment_type_ids',
    }
    for field in fields:
        key = aliases.get(field, field)
        raw = scope.get(key)
        if raw is None:
            raw = scope.get(field)
        if raw is not None:
            values[key].extend(raw if isinstance(raw, (list, tuple, set)) else [raw])
    if include_conditions:
        for key, found in _tree_values(scope.get('conditions'), fields, positive_only=positive_only).items():
            values[key].extend(found)
    return {key: list(dict.fromkeys(found)) for key, found in values.items()}


def _label(data, collection, ident):
    row = data.get(collection, {}).get(ident)
    return (row or {}).get('name') or (row or {}).get('display_name') or (row or {}).get('short_code') or str(ident)


def _join(values):
    values = list(dict.fromkeys(str(value) for value in values if value))
    if not values:
        return ''
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f'{values[0]} and {values[1]}'
    return ', '.join(values[:-1]) + f', and {values[-1]}'


def _rank_text(row):
    """Use the short rank name in prose while retaining the catalogue's id in the contract."""
    name = row.get('name') or row.get('short_code') or ''
    if ' · ' in name:
        family, code = name.rsplit(' · ', 1)
        return f'{family} {code}'
    return name


def _rank_group(labels):
    """Collapse adjacent resident ranks for report/UI prose without changing scope semantics."""
    labels = list(dict.fromkeys(labels))
    residents = []
    for label in labels:
        if label.casefold().startswith('resident '):
            code = label.rsplit(' ', 1)[-1]
            if code.startswith('R') and code[1:].isdigit():
                residents.append((int(code[1:]), code))
    if len(residents) == len(labels) and len(residents) > 1:
        residents.sort()
        if [code for _, code in residents] == [f'R{i}' for i in range(residents[0][0], residents[-1][0] + 1)]:
            return f'Residents ({residents[0][1]}–{residents[-1][1]})'
    if len(labels) > 1 and any(label.casefold().startswith('fellow') for label in labels) \
            and any('consultant' in label.casefold() for label in labels):
        return 'Fellow and above'
    return _join(labels)


def _value_labels(data, field, values):
    collections = {
        'person_ids': 'people', 'person_id': 'people', 'category_ids': 'categories', 'category_id': 'categories',
        'programme_ids': 'programmes', 'programme_id': 'programmes', 'rotation_type_ids': 'rotation_types',
        'rotation_type_id': 'rotation_types', 'cohort_ids': 'cohorts', 'cohort_id': 'cohorts',
        'service_ids': 'services', 'service_id': 'services', 'location_ids': 'locations', 'location_id': 'locations',
        'eligible_service_ids': 'services', 'date_set_ids': 'date_sets',
        'assignment_type_ids': 'types', 'assignment_type_id': 'types',
    }
    if field in {'tags', 'capability_ids'}:
        collection = 'capabilities' if field == 'capability_ids' else None
        return [_label(data, collection, value) if collection else str(value) for value in values]
    collection = collections.get(field)
    if not collection:
        return [str(value) for value in values]
    labels = [_label(data, collection, value) for value in values]
    if field in {'category_ids', 'category_id'}:
        return [_rank_text(data.get('categories', {}).get(value, {})) for value in values]
    return labels


def _compact_labels(data, field, values):
    labels = _value_labels(data, field, values)
    if len(labels) <= 8:
        return _join(labels)
    # A tenant may expose named clinic areas.  Use them when present; otherwise keep the
    # count and the noun so the sentence stays readable without inventing a grouping.
    groups = data.get('service_areas') or data.get('service_groups') or data.get('areas') or {}
    selected = set(values)
    if isinstance(groups, dict):
        for ident, group in groups.items():
            members = set(group.get('service_ids') or group.get('clinic_ids') or group.get('members') or ()) if isinstance(group, dict) else set()
            if members and members == selected:
                return f"all {group.get('name') or _label(data, 'areas', ident)} ({len(labels)})"
    noun = 'clinics' if field in {'service_ids', 'service_id'} else 'duty types' if 'assignment' in field else 'places'
    return f'all selected {noun} ({len(labels)})'


def _who_text(data, scope, selected, allowed_categories=()):
    values = _scope_values(scope, WHO_FIELDS, positive_only=True)
    people = [_label(data, 'people', value) for value in values.get('person_ids', ())
              if value in data.get('people', {}) and (not selected or value in selected)]
    ranks = [_rank_text(data.get('categories', {}).get(value, {})) for value in values.get('category_ids', ())
             if value in data.get('categories', {}) and (not allowed_categories or value in allowed_categories)]
    programmes = [f'{_label(data, "programmes", value)} programme'
                  for value in values.get('programme_ids', ()) if value in data.get('programmes', {})]
    rotations = [f'{_label(data, "rotation_types", value)} rotation'
                 for value in values.get('rotation_type_ids', ()) if value in data.get('rotation_types', {})]
    tags = list(values.get('tags', ()))
    rank = _rank_group(ranks) if ranks else ''
    memberships = [*programmes, *rotations]
    if rank and memberships and not people and not tags:
        return f'{rank} on {_join(memberships)}'
    parts = [part for part in [*people, rank, *memberships, *tags] if part]
    if parts:
        return _join(parts)
    if allowed_categories:
        requested = [_rank_text(data.get('categories', {}).get(value, {}))
                     for value in allowed_categories if value in data.get('categories', {})]
        if requested:
            return _rank_group(requested)
    if len(selected) == 1:
        return _label(data, 'people', next(iter(selected)))
    return 'Everyone'


def _restriction_allowed(data, scope, kind):
    """For a Keep-off restriction, invert an OR of exclusions into its allowed duty pattern."""
    if kind not in {'restriction', 'workload_limit'}:
        return ''
    tree = scope.get('conditions') or {}

    def leaves(node):
        if not node:
            return []
        if 'conditions' not in node:
            return [node]
        found = []
        for child in node.get('conditions') or ():
            found.extend(leaves(child))
        return found

    # Catalogue lines often have a top-level ``all`` containing the authored who
    # tree, where tree and resolver-added precedence predicates.  Find the authored
    # OR of exclusions anywhere in that tree, while requiring every leaf to be a
    # session selector so an unrelated OR can never be inverted into permission.
    candidates = []

    def visit(node):
        if not node or 'conditions' not in node:
            return
        if node.get('match', 'all') == 'any':
            candidate = leaves(node)
            if candidate and all(leaf.get('field') in WHERE_FIELDS for leaf in candidate):
                candidates.append(candidate)
        for child in node.get('conditions') or ():
            visit(child)

    visit(tree)
    for leaves_for_candidate in candidates:
        allowed = []
        for field in ('service_ids', 'location_ids', 'assignment_type_ids'):
            values = []
            for leaf in leaves_for_candidate:
                if leaf.get('field') == field and leaf.get('op') in {'none', 'is_not'}:
                    values.extend(absolute_values(leaf) or ())
            if values:
                allowed.append((field, list(dict.fromkeys(values))))
        if allowed:
            break
    else:
        return ''
    by_field = {}
    for field, values in allowed:
        by_field[field] = _compact_labels(data, field, values)
    clinic = by_field.get('service_ids') or by_field.get('location_ids')
    duty = by_field.get('assignment_type_ids')
    if clinic and duty:
        return f'{clinic} on {duty}'
    return _join(by_field.values())


def _where_text(data, scope, *, kind=None):
    allowed = _restriction_allowed(data, scope, kind)
    if allowed:
        return allowed
    values = _scope_values(scope, WHERE_FIELDS, positive_only=True)
    parts = []
    for field, collection in (('service_ids', 'services'), ('location_ids', 'locations'), ('assignment_type_ids', 'types')):
        found = [value for value in values.get(field, ()) if value in data.get(collection, {})]
        if found:
            parts.append(_compact_labels(data, field, found))
    return _join(parts) or 'All clinics and duties'


_FIELD_NOUNS = {
    'person_ids': 'people', 'category_ids': 'ranks', 'programme_ids': 'programmes',
    'rotation_type_ids': 'rotations', 'cohort_ids': 'cohorts', 'service_ids': 'clinics',
    'location_ids': 'places', 'assignment_type_ids': 'duty types', 'tags': 'tags',
    'capability_ids': 'capabilities', 'eligible_service_ids': 'eligible clinics',
    'usual_assignee_ids': 'usual assignees', 'date_set_ids': 'date sets', 'roles': 'roles',
    'date': 'duty dates', 'starts_on': 'start dates', 'ends_on': 'end dates',
    'start': 'start values', 'end': 'end values', 'name': 'names', 'display_name': 'display names',
    'short_code': 'short codes', 'note': 'notes', 'number': 'numbers', 'target': 'targets',
    'capacity': 'capacities', 'points': 'points', 'min': 'minimums', 'max': 'maximums',
    'rota_ids': 'rotas', 'workspace_id': 'rotas', 'department_ids': 'departments',
}
_CONDITION_OPERATORS = {operator for operators in CONDITION_OPERATORS.values() for operator in operators}


def _condition_leaf_text(data, node):
    field = node.get('field')
    op = node.get('op', 'any')
    values = absolute_values(node)
    if values is None:
        operand = node.get('value')
        operands = operand if isinstance(operand, (list, tuple)) else (operand,)
        values = []
        for item in operands:
            if isinstance(item, dict) and item.get('token'):
                values.append(str(item['token']).replace('_', ' '))
            else:
                return None
    if field == OUTRANKED:
        if op in {'none', 'is_not'}:
            return 'unless a more-specific line applies'
        return 'when a more-specific line applies'
    if field == OVERRIDDEN:
        if op in {'none', 'is_not'}:
            return 'unless this rule is overridden'
        return 'when this rule is overridden'
    if field == OWN_LINE:
        if op in {'none', 'is_not'}:
            return 'unless this rule has its own line'
        return 'when this rule has its own line'
    if field == INHERITED_LEVEL:
        return 'with the inherited level' if op not in {'none', 'is_not'} else 'unless a more-specific level applies'
    if field not in set(CONDITION_FIELDS) | {OUTRANKED, OVERRIDDEN, OWN_LINE, INHERITED_LEVEL}:
        return None
    if field not in {OUTRANKED, OVERRIDDEN, OWN_LINE, INHERITED_LEVEL}:
        if op not in _CONDITION_OPERATORS:
            return None
    if field == 'usual_assignee_ids':
        labels = _value_labels(data, 'person_ids', values)
    elif field == 'roles':
        labels = [str(value) for value in values]
    elif field == 'date_set_ids' and node.get('entry_names'):
        labels = [str(value) for value in node['entry_names']]
    else:
        labels = _value_labels(data, field, values)
    noun = _FIELD_NOUNS.get(field, field.replace('_', ' '))
    if op == 'empty':
        return f'{noun} are empty'
    if op == 'not_empty':
        return f'{noun} are present'
    if not labels:
        return None
    value_text = (_compact_labels(data, field, values)
                  if field in WHERE_FIELDS | {'eligible_service_ids'}
                  else _rank_group(labels) if field in {'category_ids', 'category_id'}
                  else _join(labels))
    if op in {'any', 'is'}:
        return f'{noun} are {value_text}'
    if op == 'all':
        return f'{noun} include all of {value_text}'
    if op in {'none', 'is_not'}:
        return f'{noun} are not {value_text}'
    if op in {'contains', 'starts_with', 'ends_with', 'not_contains'}:
        verb = 'contain' if op == 'contains' else 'do not contain' if op == 'not_contains' else op.replace('_', ' ')
        return f'{noun} {verb} {value_text}'
    if op in {'before', 'after', 'on_or_before', 'on_or_after', 'gt', 'gte', 'lt', 'lte', 'between', 'within'}:
        comparator = {'on_or_before': 'on or before', 'on_or_after': 'on or after',
                      'gte': 'at least', 'lte': 'at most', 'gt': 'greater than', 'lt': 'less than',
                      'within': 'within'}.get(op, op)
        return f'{noun} are {comparator} {value_text}'
    return None


def _condition_text(data, tree, *, restriction_allowed=''):
    """Render non-trivial scope conditions; return a marker when a new operator is unknown."""
    if not tree:
        return ''
    if restriction_allowed:
        return ''
    unsupported = False

    def visit(node, parent_match='all'):
        nonlocal unsupported
        if not node:
            return '', True
        if 'conditions' in node:
            match = node.get('match', 'all')
            pieces = []
            seen = set()
            for child in node.get('conditions') or ():
                piece, supported = visit(child, match)
                unsupported |= not supported
                if piece and piece not in seen:
                    seen.add(piece)
                    pieces.append(piece)
            if not pieces:
                return '', True
            joiner = ' and ' if match == 'all' else ' or '
            return joiner.join(pieces), True
        field, op = node.get('field'), node.get('op', 'any')
        if parent_match == 'all' and field in WHO_FIELDS | WHERE_FIELDS and op in {'any', 'is', 'all'}:
            return '', True
        rendered = _condition_leaf_text(data, node)
        return (rendered, True) if rendered else ('', False)

    expression, _ = visit(tree)
    if unsupported:
        marker = '(conditions apply — see lines)'
        return f'{expression}; {marker}' if expression else marker
    # Positive all-of conditions are already represented by the who/where fields.
    leaves = []
    def collect(node):
        if 'conditions' in node:
            for child in node.get('conditions') or ():
                collect(child)
        else:
            leaves.append(node)
    collect(tree)
    if tree.get('match', 'all') == 'all' and all(c.get('op', 'any') in {'any', 'is', 'all'}
                                                  and c.get('field') in WHO_FIELDS | WHERE_FIELDS
                                                  and c.get('field') not in {OUTRANKED, OVERRIDDEN, OWN_LINE, INHERITED_LEVEL}
                                                  for c in leaves):
        return ''
    return expression


def _when_text(copy, workspace, zone, source_line=None, source_rule=None, *, rota=True):
    def day_text(day):
        return f'{day.day} {day.strftime("%B %Y")}'

    if rota:
        first = date.fromisoformat(str(workspace['starts_on']))
        last = date.fromisoformat(str(workspace['ends_on'])) - timedelta(days=1)
        if copy.get('starts_on'):
            first = max(first, date.fromisoformat(str(copy['starts_on'])))
        if copy.get('ends_on'):
            last = min(last, date.fromisoformat(str(copy['ends_on'])) - timedelta(days=1))
        dates = f'{day_text(first)}–{day_text(last)}' if first <= last else 'outside this rota'
    else:
        rule = source_rule or copy.get('definition', {}).get('rule') or {}
        if source_line is not None:
            # Catalogue policy activation is lifecycle metadata; the authored line owns the rule's dates.
            from packages.domain.rule_lines import when
            starts_on, ends_on = when(rule, source_line)
            block = (source_line.get('settings') or {}).get('block_quota') or {}
            if (starts_on and not ends_on and block.get('repeat')
                    and str(starts_on) == str(copy.get('starts_on'))):
                # This start is also the block anchor; rule.propose auto-filled it, so it will show in the counting-period field once that field exists.
                starts_on = None
        else:
            starts_on, ends_on = copy.get('starts_on'), copy.get('ends_on')
        first = date.fromisoformat(str(starts_on)) if starts_on else None
        last = date.fromisoformat(str(ends_on)) - timedelta(days=1) if ends_on else None
        if first and last:
            dates = day_text(first) if first == last else f'{day_text(first)}–{day_text(last)}'
        elif first:
            dates = f'from {day_text(first)}'
        elif last:
            dates = f'until {day_text(last)}'
        else:
            dates = ''
    scope = copy['definition']['scope']
    days = scope.get('weekdays')
    day_text = 'every day' if not days or set(days) == set(range(7)) else _join(WEEKDAYS[d] for d in sorted(days))
    clocks = ''
    start, end = scope.get('start_time', '00:00'), scope.get('end_time', '24:00')
    if (start, end) != ('00:00', '24:00'):
        clocks = f', {start}–{end}'
    return f'{dates}, {day_text}{clocks}' if dates else f'{day_text}{clocks}'


def _period_words(rule, line):
    """Render the configured count window for catalogue and legacy rules."""
    from packages.domain.rule_lines import measure
    from packages.contracts.rule_catalogue import catalogue_entry, slot_for_number

    catalogue_rule = rule.get('kind') == 'catalogue'
    if catalogue_rule and not rule.get('id'):
        raise RuleExplanationInputError('A catalogue rule explanation needs a catalogue id')
    measured = measure(rule, line, block=True) if catalogue_rule else {rule.get('kind'): rule}
    kind = ('block_quota' if measured.get('block_quota') else
            'max_duties' if measured.get('max_duties') else 'max_hours' if measured.get('max_hours') else None)
    if not kind:
        return ''
    entry = catalogue_entry(rule.get('id'), rule.get('catalogue_version', 1)) if catalogue_rule else None
    settings = dict(measured[kind])
    if entry:
        settings.update(entry.get('default_settings', {}).get(kind, {}))
        settings.update((rule.get('settings') or {}).get(kind, {}))
        settings.update((line.get('settings') or {}).get(kind, {}))
        keys = list(line.get('numbers') or {})
        for key in keys:
            slot = slot_for_number(entry, key, rule, line, keys)
            if slot['kind'] == kind:
                settings.update(slot.get('fixed') or {})
                break
    alignment = settings.get('window_alignment')
    window = settings.get('window')
    days = settings.get('window_days')
    if alignment == 'calendar_month' or window == 'month':
        return 'a month'
    if alignment == 'calendar_week' or window == 'week':
        return 'a week'
    if alignment == 'day' or window == 'day' or days == 1:
        return 'a day'
    if alignment == 'fixed' or window == 'fixed':
        return f'per {days}-day block' if days else 'a period'
    if (alignment == 'rolling' or window == 'rolling' or days) and days:
        return f'in any {days} days'
    if alignment == 'block':
        return 'a period'
    return ''


def _number_with_period(number, period):
    if period and number not in {'no duties', 'a different duty pattern', 'preferred allocation'}:
        return f'{number} {period}'
    return number


def _number_text(rule, data=None, *, exact=False, line_numbers=None):
    kind = rule.get('kind')
    if kind == 'forbidden_transition':
        from_ids, to_ids = rule.get('from_type_ids') or (), rule.get('to_type_ids') or ()
        if data and from_ids and to_ids:
            from_text = _compact_labels(data, 'assignment_type_ids', from_ids)
            to_text = _compact_labels(data, 'assignment_type_ids', to_ids)
            overtime = ' when either duty is overtime' if rule.get('overtime_only') else ''
            return f'must not schedule {to_text} after {from_text}{overtime}'
        return 'must follow a different duty pattern (conditions apply — see lines)'
    if kind == 'supervision_limit':
        maximum = rule.get('maximum')
        if maximum is not None:
            return f'at most {maximum:g} {_singular("trainees", maximum)}'
        ranks = rule.get('trainee_category_ids') or ()
        if ranks and data:
            labels = [_rank_text(data.get('categories', {}).get(value, {})) for value in ranks]
            labels = [label for label in labels if label]
            if labels:
                return f'supervision applies to {_rank_group(labels)}'
        return 'supervision applies'
    if kind == 'team_lead_requirement':
        limits = []
        for key, direction in (('minimum', 'at least'), ('maximum', 'at most')):
            value = rule.get(key)
            if value is not None:
                limits.append(f'{direction} {value:g} {_singular("team leads", value)}')
        return ' and '.join(limits) or 'the team-lead requirement applies'
    number = rule_number(rule) or {}
    value = number.get('value')
    if kind == 'block_quota' and value is None:
        value = rule.get('amount')
    if value is None:
        if kind == 'restriction':
            return 'no duties'
        if kind == 'shift_succession':
            return 'a different duty pattern'
        if kind == 'allocation_overtime':
            return 'overtime may be offered'
        if kind == 'allocation_preference':
            return 'preferred allocation'
        if kind == 'balance_goal':
            return 'a fair share'
        return 'the configured limit'
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    unit, direction = _KIND_LABELS.get(kind, (number.get('unit') or rule.get('metric') or 'units', None))
    unit = {'session_units': 'session units', 'duties': 'duties', 'hours': 'hours'}.get(unit, unit)
    if exact:
        return f'exactly {value:g} {_singular(unit, value)}'
    if line_numbers and line_comparison(line_numbers) == 'range':
        minimum = line_numbers['min']['value']
        maximum = line_numbers['max']['value']
        return f'between {minimum:g} and {maximum:g} {_singular(unit, maximum)}'
    if kind == 'block_quota':
        direction = {'minimum': 'at least', 'maximum': 'at most', 'target': 'aim for'}.get(rule.get('comparison'), 'aim for')
    else:
        direction = {'minimum': 'at least', 'maximum': 'at most'}.get(direction, direction or 'configured as')
    if kind == 'allocation_preference':
        return f'priority {value}'
    if kind == 'balance_goal':
        return f'aim for {value:g} {unit}'
    if kind == 'block_quota' and rule.get('comparison') == 'target':
        return f'{direction} {value:g} {_singular(unit, value)}'
    return f'{direction} {value:g} {_singular(unit, value)}'


def _short_number(numbers):
    """Compact comparison token for narrow cells; the full wording stays in ``cell``."""
    comparison = line_comparison(numbers)
    if comparison == 'exact':
        return f"= {numbers['min']['value']:g}"
    if comparison == 'minimum':
        return f"≥ {numbers['min']['value']:g}"
    if comparison == 'maximum':
        return f"≤ {numbers['max']['value']:g}"
    if comparison == 'range':
        return f"{numbers['min']['value']:g}–{numbers['max']['value']:g}"
    if comparison == 'target':
        return f"aim {numbers['target']['value']:g}"
    return None


def _cell_text(copy, data, number, where, source_line=None, source_rule=None):
    """Short cell wording built from the same number/scope/date inputs as the explainer sentence."""
    rule = source_rule or copy['definition']['rule']
    line = source_line or copy.get('template_line') or {
        'where': copy['definition'].get('scope', {}),
        'weekdays': copy['definition'].get('scope', {}).get('weekdays'),
        'start_time': copy['definition'].get('scope', {}).get('start_time'),
        'end_time': copy['definition'].get('scope', {}).get('end_time'),
    }
    from packages.domain.rule_lines import measure, when

    catalogue_rule = rule.get('kind') == 'catalogue'
    measured = measure(rule, line, block=True) if catalogue_rule else {rule.get('kind'): rule}
    quota = measured.get('block_quota') or {}
    limit = measured.get('max_duties') or measured.get('max_hours') or {}
    settings = quota or limit
    overtime = settings.get('overtime')
    period = _period_words(rule, line)
    parts = []
    if where != 'All clinics and duties':
        parts.append(where)
    if overtime == 'only':
        parts.append('overtime')
    elif overtime == 'exclude':
        parts.append('non-overtime')
    rendered_number = _number_with_period(number, period)
    rendered = ' '.join([*parts, rendered_number]) if parts else rendered_number
    _, ends_on = when(rule, line) if catalogue_rule else (line.get('starts_on'), line.get('ends_on') or copy.get('ends_on'))
    if ends_on:
        end = date.fromisoformat(str(ends_on)) - timedelta(days=1)
        rendered += f', until {end.day} {end:%b}'
    qualified = bool(parts or period or ends_on)
    return rendered, qualified


def _singular(unit, value):
    try:
        one = float(value) == 1
    except (TypeError, ValueError):
        one = False
    if not one:
        return unit
    return {'duties': 'duty', 'people': 'person', 'hours': 'hour', 'trainees': 'trainee',
            'team leads': 'team lead', 'days in a row': 'day in a row',
            'weekends in a row': 'weekend in a row', 'session units': 'session unit'}.get(unit, unit)


def _person_noun(who, *, singular=False):
    text = who.strip()
    if text.casefold().startswith('resident '):
        rank = text.split(' ', 1)[1]
        return f'resident {rank}' if singular else f'residents {rank}'
    if text.casefold().startswith('residents ('):
        ranks = text[text.find('(') + 1:text.rfind(')')]
        return f'resident from {ranks}' if singular else f'residents from {ranks}'
    if text.casefold().startswith('fellow'):
        return 'fellow or above' if text.casefold() == 'fellow and above' else text[0].lower() + text[1:]
    if text.casefold().startswith('everyone'):
        return 'person'
    return f"{'person' if singular else 'people'} from {text}"


def _text(copy, who, where, when, number, *, condition='', level='mandatory', weight=None,
          restriction_allowed='', period=''):
    kind = copy['definition']['rule'].get('kind')
    subject = who if where == 'All clinics and duties' else f'{who} at {where}'
    if kind in {'restriction', 'workload_limit'} and number == 'no duties':
        if restriction_allowed:
            subject, phrase = who, f'may work only {restriction_allowed}; otherwise must have no duties'
        elif where != 'All clinics and duties':
            subject, phrase = who, f'never work {where}'
        else:
            phrase = 'must have no duties'
    elif kind in {'forbidden_transition', 'shift_succession'}:
        phrase = number if kind == 'forbidden_transition' else f'must follow {number}'
    elif kind == 'minimum_cover':
        cover_number = number.removesuffix(' person').removesuffix(' people')
        singular = cover_number.startswith(('at least 1', 'at most 1'))
        phrase = f'{cover_number} {_person_noun(who, singular=singular)} per session'
        subject = f'{who} across all clinics and duties' if where == 'All clinics and duties' else where
    elif kind in {'supervision_limit', 'team_lead_requirement'}:
        phrase = number
    elif kind in {'allocation_overtime', 'allocation_preference', 'balance_goal'}:
        phrase = number
    else:
        phrase = _number_with_period(number, period)
    if level == 'advisory':
        phrase = f'{phrase} (preference, {weight or "configured weight"})'
    qualifier = f'; {condition}' if condition else ''
    return f'{subject}: {phrase} ({when}{qualifier}).'


def _summary_short(sentence):
    """Keep the rule's core wording while dropping dates and preference qualifiers."""
    core = sentence['text'].removesuffix('.').rsplit(' (', 1)[0]
    core = re.sub(r'\s+\(preference, [^)]+\)', '', core).replace('(', '').replace(')', '')
    number, cell = sentence.get('number'), sentence.get('cell')
    if number and cell and number in cell:
        period = cell.rsplit(number, 1)[1].split(', until ', 1)[0].strip()
        if period and period not in core:
            core = f'{core} {period}'
    return ' '.join(core.split())


def _question_group(data, original, scope, effective_kind=None, effective_severity='mandatory'):
    """Resolve the catalogue's clinician question for an effective copy.

    V2 owns the labels. Historical catalogue ids use their mapped V2 entry, with
    protected/no-duty aliases and person/group scope routing retained in metadata.
    """
    rule = original.get('definition', {}).get('rule', {})
    catalogue_id = rule.get('id')
    version = rule.get('catalogue_version', 1)
    entry = question_group_entry(catalogue_id, version)
    values = _scope_values(scope, WHO_FIELDS, positive_only=True)
    scope_kind = 'person' if values.get('person_ids') or values.get('person_id') else 'group'
    if not entry:
        return (question_group_for_effective_kind(effective_kind, severity=effective_severity, scope=scope_kind)
                or QUESTION_GROUPS['who_where'])
    aliases = entry.get('question_group_aliases') or {}
    if rule.get('id') in aliases:
        return aliases[rule['id']]
    slot_groups = entry.get('question_group_by_slot') or {}
    if effective_kind in slot_groups:
        return slot_groups[effective_kind]
    group = entry.get('question_group')
    by_scope = entry.get('question_group_by_scope') or {}
    if by_scope:
        if values.get('person_ids') or values.get('person_id'):
            return by_scope.get('person', group)
        if any(values.get(field) for field in ('category_ids', 'category_id', 'programme_ids', 'programme_id',
                                                'rotation_type_ids', 'rotation_type_id', 'cohort_ids', 'cohort_id',
                                                'tags', 'capability_ids')):
            return by_scope.get('group', group)
        return by_scope.get('group', group)
    return group or QUESTION_GROUPS['who_where']


def _merge_effective_sentence(row, sentence):
    """Coalesce one line's mandatory number and equal advisory target.

    Catalogue expansion emits separate copies for a minimum and a target even when
    they are the same effective number.  The explanation is one line: the mandatory
    obligation wins, so its text never carries advisory wording.
    """
    source_ids = tuple(sentence.get('source_line_ids') or ())
    if not source_ids:
        row['sentences'].append(sentence)
        return
    facts = tuple(sentence[key] for key in ('who', 'where', 'when', 'safety'))
    existing = next((item for item in row['sentences']
                     if tuple(item[key] for key in ('who', 'where', 'when', 'safety')) == facts
                     and tuple(item.get('source_line_ids') or ()) == source_ids), None)
    if existing is None:
        row['sentences'].append(sentence)
        return
    if existing['number'] != sentence['number']:
        row['sentences'].append(sentence)
        return
    if existing['level'] == sentence['level'] == 'advisory':
        # Preserve distinct preference weights even when their rendered number agrees.
        if existing.get('weight') != sentence.get('weight'):
            row['sentences'].append(sentence)
            return
    elif existing['level'] != sentence['level']:
        mandatory = existing if existing['level'] == 'mandatory' else sentence
        index = row['sentences'].index(existing)
        row['sentences'][index] = mandatory
        existing = mandatory
    existing['source_line_ids'] = list(dict.fromkeys(existing['source_line_ids'] + sentence['source_line_ids']))


def _selector_tokens(value):
    if not value or value == 'everyone':
        return []
    return [part.strip() for part in value.split(',') if part.strip()]


def _people_for_who(data, tokens, first, last):
    zone = data['organisation']['timezone']
    active_ends = {pid: end for pid, person in data.get('people', {}).items()
                   if (end := person_active_end(person, last, zone)) is not None and first < end}
    if not tokens:
        return set(active_ends)
    people = set()
    rows = data.get('_person_rows') or index_person_rows(data)
    for token in tokens:
        kind, _, ident = token.partition(':')
        if kind == 'person' or token in data.get('people', {}):
            if ident in active_ends:
                people.add(ident)
            elif token in active_ends:
                people.add(token)
            continue
        category = ident if kind == 'rank' else token
        if category not in data.get('categories', {}):
            continue
        for pid, end in active_ends.items():
            if any(row.get('category_id') == category and within_dates(row, first, end, zone)
                   for row in rows['memberships'].get(pid, ())):
                people.add(pid)
    return people


def _where_tokens(value):
    return _selector_tokens(value)


def _where_matches(data, scope, tokens):
    if not tokens:
        return True
    values = _scope_values(scope, WHERE_FIELDS, positive_only=True)
    selected = set()
    for token in tokens:
        kind, _, ident = token.partition(':')
        if kind in {'clinic', 'service'}:
            selected.add(ident)
        elif kind in {'place', 'location'}:
            selected.add(ident)
        elif kind in {'type', 'duty'}:
            selected.add(ident)
        else:
            selected.add(token)
    present = set().union(*(set(found) for found in values.values()))
    return not present or bool(selected & present)


def _scope_reaches_person(data, scope, person_id, first, last):
    """Ask the canonical scope evaluator whether a line can bind this person.

    A line's ``where`` is a session selector, so a person-only reach check must not
    reject it merely because ``scope_applies(..., shift=None)`` has no session to
    inspect.  The rule-map's duty-witness projection handles that case with the
    same evaluator and virtual duty contexts.
    """
    last = person_active_end(data['people'].get(person_id), last, data['organisation']['timezone'])
    if last is None or first >= last:
        return False
    if not has_session_lines(scope_tree(scope)):
        return scope_applies(data, scope, person_id, None, first, last)
    try:
        return scope_has_duty_witness(data, scope, person_id, first, last, data['workspace']['department_id'])
    except ValueError:
        # A malformed or unsupported session projection cannot make a configured
        # person line disappear from an explanation; the checker remains the
        # authority for validation of that scope.
        return scope_applies(data, scope, person_id, None, first, last)


def _effective_scope(copy, authored_lines=None):
    """Keep authored line predicates alongside resolver-added internal predicates.

    ``rule_lines.scope_of`` intentionally flattens simple positive predicates into
    legacy scope fields.  The effective copy therefore needs the original catalogue
    line's trees to explain negative, grouped, or otherwise non-flat conditions.
    """
    scope = copy['definition']['scope']
    source_id = (copy.get('template_line') or {}).get('id')
    if not source_id or not authored_lines:
        return scope
    source_line = authored_lines.get((copy.get('id'), source_id)) or authored_lines.get(source_id)
    if not source_line:
        return scope
    trees = [source_line.get(key) for key in ('who', 'where') if source_line.get(key)]
    trees = [tree for tree in trees if tree.get('conditions')]
    if not trees:
        return scope
    existing = scope.get('conditions')
    def contains(node, candidate):
        if node == candidate:
            return True
        return any(contains(child, candidate) for child in node.get('conditions') or ()) if isinstance(node, dict) else False

    conditions = ([existing] if existing else []) + [tree for tree in trees if not contains(existing, tree)]
    if not conditions:
        return scope
    return {**scope, 'conditions': {'match': 'all', 'conditions': conditions}}


def _authored_line_index(policies):
    """Index catalogue source lines once so explanation rendering stays bounded."""
    index = {}
    for policy in policies:
        if policy.get('definition', {}).get('rule', {}).get('kind') != 'catalogue':
            continue
        for line in policy.get('definition', {}).get('rule', {}).get('lines') or ():
            line_id = line.get('id')
            if line_id:
                index[(policy.get('id'), line_id)] = line
                index.setdefault(line_id, line)
            source_id = line.get('_source_identity')
            if source_id:
                index[(policy.get('id'), source_id)] = line
                index.setdefault(source_id, line)
    return index


def explain_rules(data, *, who=None, where=None, rota=True):
    """Return the API's explained-rule contract for a snapshot.

    ``who`` is ``person:<id>`` or ``rank:<category id>`` (comma-separated values are
    accepted for private/report tooling).  ``where`` accepts bare ids or
    ``service:``, ``location:`` and ``type:`` prefixes.  Rota reads clip dates to
    the workspace; department rule reads use each effective copy's own dates.
    """
    from .rule_lines import catalogue_display_policies, safety_display_key
    data = {**data, 'policies': catalogue_display_policies(data.get('policies') or [], data)}
    workspace = data['workspace']
    zone = data['organisation']['timezone']
    first = local_midnight(date.fromisoformat(str(workspace['starts_on'])), zone)
    last = local_midnight(date.fromisoformat(str(workspace['ends_on'])), zone)
    tokens = _selector_tokens(who)
    selected = _people_for_who(data, tokens, first, last)
    where_tokens = _where_tokens(where)
    runtime = {**data, 'policies': active_rules(data.get('policies') or [], context=data),
               '_person_rows': data.get('_person_rows') or index_person_rows(data),
               '_person_dates_cache': {}, '_line_index_cache': {}, '_scope_matches_cache': {},
               '_scope_interval_cache': {}, '_scope_membership_index': data.get('_scope_membership_index')}
    originals = {p['id']: p for p in data.get('policies') or []}
    policy_order = {policy_id: index for index, policy_id in enumerate(originals)}
    authored_lines = _authored_line_index(data.get('policies') or ())
    grouped = {}
    order = []
    for copy in sorted(runtime['policies'], key=lambda copy: (
            policy_order.get(copy['id'], len(originals)), safety_display_key(copy))):
        if copy.get('id') not in originals or not within_dates(copy, first, last, zone):
            continue
        scope = _effective_scope(copy, authored_lines)
        if not _where_matches(data, scope, where_tokens):
            continue
        if tokens:
            start = max(first, local_midnight(date.fromisoformat(str(copy['starts_on'])), zone)) if copy.get('starts_on') else first
            end = min(last, local_midnight(date.fromisoformat(str(copy['ends_on'])), zone)) if copy.get('ends_on') else last
            matches = {pid for pid in selected if _scope_reaches_person(runtime, scope, pid, start, end)}
            if not matches:
                continue
        else:
            matches = set()
        original = originals[copy['id']]
        source_id = (copy.get('template_line') or {}).get('id')
        kind = copy['definition']['rule'].get('kind')
        original_rule = (original.get('definition') or {}).get('rule') or {}
        definition = copy.get('definition') or {}
        sentence_level = definition.get('severity', 'mandatory')
        level = 'mandatory' if sentence_level == 'mandatory' else 'advisory'
        question_group = QUESTION_GROUPS['who_where']
        safety = True
        safety_error = None
        try:
            safety = safety_rule(definition)
        except Exception as error:
            safety_error = error
        try:
            if safety_error is not None:
                raise safety_error
            sentence_who = _who_text(data, scope, matches,
                                     {token.partition(':')[2] for token in tokens if token.startswith('rank:')})
            restriction_allowed = _restriction_allowed(data, scope, kind)
            source_line = authored_lines.get((copy.get('id'), source_id)) or authored_lines.get(source_id)
            sentence_when = _when_text(copy, workspace, zone, source_line, original.get('definition', {}).get('rule'), rota=rota)
            sentence_where = _where_text(data, scope, kind=kind)
            line_numbers = source_line.get('numbers') if source_line else None
            exact = bool(line_numbers and line_comparison(line_numbers) == 'exact')
            number = _number_text(copy['definition']['rule'], data, exact=exact, line_numbers=line_numbers)
            weight = None if sentence_level == 'mandatory' else copy['definition'].get('strength')
            period = _period_words(original_rule or copy['definition']['rule'], source_line or copy.get('template_line') or {
                'where': copy['definition'].get('scope', {}),
                'weekdays': copy['definition'].get('scope', {}).get('weekdays'),
                'start_time': copy['definition'].get('scope', {}).get('start_time'),
                'end_time': copy['definition'].get('scope', {}).get('end_time'),
            })
            condition = _condition_text(data, scope.get('conditions'), restriction_allowed=restriction_allowed)
            question_group = _question_group(data, original, scope, kind, sentence_level)
            cell, qualified = _cell_text(copy, data, number, sentence_where, source_line,
                                         original_rule)
            sentence = {'text': _text(copy, sentence_who, sentence_where, sentence_when, number,
                                      condition=condition, level=level, weight=weight,
                                      restriction_allowed=restriction_allowed, period=period),
                        'question_group': question_group, 'who': sentence_who, 'where': sentence_where, 'when': sentence_when,
                        'number': number, 'level': level, 'weight': weight,
                        'cell': cell, 'short': _short_number(line_numbers), 'qualified': qualified,
                        'safety': safety,
                        'source_line_ids': [source_id] if source_id else []}
            rid = original['id']
            if rid not in grouped:
                grouped[rid] = {'rule_id': rid, 'title': original.get('name') or 'Rule',
                                'catalogue_id': original_rule.get('id'),
                                'question_group': question_group,
                                'level': sentence['level'], 'safety': sentence['safety'], 'sentences': [],
                                'line_ids': [], 'summary_sentence': None, 'summary_short': None,
                                'unexplained': False}
                order.append(rid)
            row = grouped[rid]
            row['level'] = 'mandatory' if row['level'] == 'mandatory' or sentence['level'] == 'mandatory' else 'advisory'
            row['safety'] = bool(row['safety'] or sentence['safety'])
            _merge_effective_sentence(row, sentence)
        except Exception as error:
            if isinstance(error, (ValidationError, RuleExplanationInputError)) and not safety:
                logger.warning(
                    "Skipping malformed rule line in explanation",
                    extra={'policy_id': copy.get('id'), 'line_id': source_id, 'error_type': type(error).__name__},
                    exc_info=True,
                )
                continue
            rid = original['id']
            row = grouped.get(rid)
            if row is None:
                row = {'rule_id': rid, 'title': original.get('name') or 'Rule',
                       'catalogue_id': original_rule.get('id'),
                       'question_group': question_group, 'level': level, 'safety': bool(safety),
                       'sentences': [], 'line_ids': [], 'summary_sentence': None,
                       'summary_short': None, 'unexplained': True}
                grouped[rid] = row
                order.append(rid)
            else:
                row['unexplained'] = True
                row['safety'] = bool(row['safety'] or safety)
                row['level'] = 'mandatory' if row['level'] == 'mandatory' or level == 'mandatory' else 'advisory'
            logger.error(
                "Rule safety classification failed" if safety_error is not None else "Rule line explanation failed",
                extra={'policy_id': copy.get('id'), 'line_id': source_id, 'error_type': type(error).__name__},
                exc_info=True,
            )
            continue
        for line_id in sentence.get('source_line_ids') or ():
            if line_id not in row['line_ids']:
                row['line_ids'].append(line_id)
    # Approved person exceptions are explanations in their own right and must use the same rule title.
    for exception in data.get('exceptions') or ():
        if exception.get('status') != 'approved' or exception.get('policy_id') not in grouped:
            continue
        if tokens and exception.get('person_id') not in selected:
            continue
        policy = originals.get(exception['policy_id'])
        if not policy or exception.get('policy_revision') != policy.get('revision'):
            continue
        end = exception.get('ends_on') or 'open-ended'
        who_text = _label(data, 'people', exception.get('person_id'))
        sentence = {'text': f'{who_text} has an approved exception to this rule from {exception.get("starts_on")} to {end}.',
                    'question_group': grouped[exception['policy_id']]['question_group'],
                    'who': who_text, 'where': 'Approved exception', 'when': f'{exception.get("starts_on")}–{end}',
                    'number': 'exception', 'cell': 'Approved exception', 'qualified': True,
                    'level': 'advisory', 'safety': False, 'source_line_ids': []}
        grouped[exception['policy_id']]['sentences'].append(sentence)
    rows = [grouped[rid] for rid in order if grouped[rid]['sentences'] or grouped[rid].get('unexplained')]
    for row in rows:
        row['summary_sentence'] = row['sentences'][0]['text'] if row['sentences'] else None
        row['summary_short'] = _summary_short(row['sentences'][0]) if row['sentences'] else None
        original = originals.get(row['rule_id']) or {}
        order_by_line = {line.get('id'): index for index, line in enumerate(
            original.get('definition', {}).get('rule', {}).get('lines') or ())}
        row['line_ids'] = sorted(row['line_ids'], key=lambda line_id: (order_by_line.get(line_id, 10**9), line_id))
    # Safety obligations are easiest to audit first, followed by other Must rules and
    # finally Prefer rules.  This is presentation ordering only; active_rules remains
    # the authority for precedence and applicability.
    rows.sort(key=lambda row: (0 if row['safety'] else 1, 0 if row['level'] == 'mandatory' else 1, order.index(row['rule_id'])))
    return {'workspace_id': workspace['id'], 'who': who if who and who != 'everyone' else None,
            'rules': rows}
