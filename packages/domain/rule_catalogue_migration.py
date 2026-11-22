"""Rules in force as catalogue rules. Pure: `plan` reads the department's rules and
returns one proposed catalogue rule per entry (its lines are the old rules and their lines) and a report of the rules
kept as they are, each with its reason. A rule joins only when its lines read exactly as it did (`expanded`, with its
id read as the catalogue rule's): anything else is reported, never silently changed."""
import json
import hashlib
from copy import deepcopy
from datetime import date, timedelta
from typing import get_args
import re
import uuid

from packages.contracts.rule_catalogue import CATALOGUE_BY_ID, CATALOGUE_MAPPING, V2_CATALOGUE_BY_ID, NEVER_OVERRIDDEN, PERSON_CALENDAR_FACT_MAPPING, _MODELS, catalogue_entry, slot_for_number
from packages.contracts.schemas import LEAD_FIELDS, SESSION_SCOPE_FIELDS, PolicyDefinition, line_field, line_rule
from .conditions import absolute_values, active_rules, effective_severity
from .rule_groups import PROTECTED_TIME, expanded, expanded_copies, normal
from .rule_lines import (SCORING_DEFAULT_SOURCE, _defaults, _settings, both_apply, department_catalogue_version,
                         duplicates, fair_share_goal_settings, line_validity, named_people, outranking, scope_of, tier)

NAMESPACE = uuid.UUID('6f1c0b8e-6c55-4a39-9a43-6a2f7c1d9e10')  # line ids are derived from the old rule: a plan is repeatable
REPORT = 'Not in the catalogue yet: '
SAFETY_REVIEW_REASON = 'Review this safety rule and approve a mandatory version before conversion'


class Kept(Exception):
    """This rule stays as it is (the message says why)."""


def _weekly(rule):
    return rule.get('window_alignment') == 'calendar_week' or rule.get('window_alignment') == 'fixed' and rule.get('window_days') == 7


def entry_of(p):
    """The catalogue entry an old rule belongs to."""
    d, rule = p['definition'], p['definition']['rule']
    kind = rule['kind']
    if kind == 'block_quota':
        metric = rule['metric']
        if metric == 'duties':
            return 'duties_per_week' if _weekly(rule) else 'duties_per_month' if rule.get('window_alignment') == 'calendar_month' else 'duties_per_period'
        if metric == 'scheduled_hours':
            return 'hours_per_week' if _weekly(rule) else 'hours_per_period'
        return 'session_units_per_week' if metric == 'session_units' and _weekly(rule) else 'other_counts_per_period'
    if kind in ('max_duties', 'max_hours'):
        return ('duties' if kind == 'max_duties' else 'hours') + ('_per_week' if rule.get('window', 'week') == 'week' else '_per_period')
    if kind in ('minimum_cover', 'maximum_participants'):
        if kind == 'minimum_cover' and (rule.get('when_person_ids') or any((r.get('settings') or {}).get('when_person_ids') for r in d.get('per_person') or [])):
            return 'companion_cover'
        scope = d['scope']
        people = (scope.get('person_id') or scope.get('person_ids') or scope.get('category_id') or scope.get('category_ids')
                  or scope.get('programme_id') or scope.get('rotation_type_id') or scope.get('tags') or scope.get('capability_ids')
                  or (scope.get('conditions') or {}).get('conditions') and _fields(scope['conditions']) & LEAD_FIELDS
                  or any(r.get('person_id') or r.get('programme_id') or r.get('category_ids') for r in d.get('per_person') or []))
        return 'kind_per_session' if people else 'people_per_session'
    if kind == 'restriction':
        return 'protected_time' if d.get('calendar_entry_id') or re.search(PROTECTED_TIME, p.get('name') or '', re.I) else 'keep_off'
    if kind == 'allocation_preference':
        return 'allocation_order' if d.get('line_number') == 'session_priority' or rule.get('session_priority') and not rule.get('priority') else 'preferences'
    if kind == 'shift_succession':
        return {'forward': 'shift_order', 'avoid': 'not_after', 'rest_after_series': 'rest_after_series'}[rule['mode']]
    return {'min_rest': 'rest_between_duties', 'forbidden_transition': 'not_after', 'max_consecutive_days': 'days_in_a_row',
            'max_consecutive_weekends': 'weekends_in_a_row', 'travel_buffer': 'travel_between_places', 'supervision_limit': 'supervision',
            'allocation_overtime': 'overtime_offers', 'balance_goal': 'fair_share', 'run_length': 'days_in_a_row',
            'days_off_run': 'days_off_together', 'weekend_shape': 'weekend_shape', 'together_apart': 'together_apart'}[kind]


def _fields(tree):
    return {f for c in tree['conditions'] for f in (_fields(c) if 'conditions' in c else {c['field']})}


def numbers_of(entry, rule, d, field):
    """A line's numbers and its own settings (by old kind) from the old rule it checks."""
    kind, level = rule['kind'], 'mandatory' if d['severity'] == 'mandatory' else d.get('strength') or 'medium'
    numbers, used = {}, {'kind'}
    if kind == 'block_quota':
        comparison = rule['comparison']
        keys = {'range': [('min', 'amount'), ('max', 'maximum_amount')], 'minimum': [('min', 'amount')], 'maximum': [('max', 'amount')],
                'target': [('target', 'amount')]}[comparison]
        for key, source in keys:
            numbers[key] = {'value': rule[source], 'level': level}
        if rule.get('target_amount') is not None:
            numbers['target'] = {'value': rule['target_amount'], 'level': rule.get('target_strength') or d.get('strength') or 'medium'}
        used |= {'comparison', 'amount', 'maximum_amount', 'target_amount', 'target_strength'}
    else:
        # One primitive field may represent a hard minimum or an advisory target. Prefer its declared advisory slot
        # for an advisory source before consuming the field; its fixed settings still have to match exactly.
        ordered = sorted(entry['numbers'].items(), key=lambda item: not (d['severity'] == 'advisory' and any(
            s['advisory'] and s['kind'] == kind and all(rule.get(k) == v for k, v in s['fixed'].items()) for s in item[1])))
        for key, slots in ordered:
            slot = next((s for s in slots if s['kind'] == kind and all(rule.get(k) == v for k, v in s['fixed'].items())), None)
            if slot is None or key in ('priority', 'session_priority') and slot['field'] != (field or 'priority'):
                continue
            if slot['field'] is None or slot['field'] in used or rule.get(slot['field']) is None:
                continue
            numbers[key] = {'value': rule[slot['field']], 'level': level}
            used |= {slot['field'], *slot['fixed']}
        if not numbers:
            slot = next((s for s in entry['numbers'].get('apply', []) if s['kind'] == kind and all(rule.get(k) == v for k, v in s['fixed'].items())), None)
            if slot is None:
                raise Kept(f"its {kind.replace('_', ' ')} settings have no line in “{entry['name']}”")
            numbers['apply'] = {'value': None, 'level': level}
            used |= set(slot['fixed'])
    return numbers, {k: v for k, v in rule.items() if k not in used}


def _later(a, b):
    return b if a is None or b is not None and str(b) > str(a) else a


def _earlier(a, b):
    return b if a is None or b is not None and str(b) < str(a) else a


def _route(c):
    fields = _fields(c) if 'conditions' in c else {c['field']}
    if fields <= LEAD_FIELDS:
        return 'who'
    if fields <= SESSION_SCOPE_FIELDS:
        return 'where'
    raise Kept('its conditions mix people and sessions under "any"')


def who_where(scope, extra_who=(), extra_where=()):
    """An old scope as a line's who and where: flat fields first (the resolver writes them back flat), then the
    condition lines. A single programme or rotation condition stays a condition (it reads membership throughout)."""
    who, where = [], []

    def flat(field, *keys):
        values = [v for k in keys for v in (scope.get(k) if isinstance(scope.get(k), list) else [scope[k]] if scope.get(k) else [])]
        return [{'field': field, 'op': 'any', 'values': values}] if values else []
    who += flat('person_ids', 'person_id', 'person_ids') + flat('category_ids', 'category_id', 'category_ids') + flat('programme_ids', 'programme_id')
    who += flat('rotation_type_ids', 'rotation_type_id') + flat('tags', 'tags') + flat('capability_ids', 'capability_ids')
    where += flat('service_ids', 'service_id', 'service_ids') + flat('location_ids', 'location_id') + flat('assignment_type_ids', 'assignment_type_id')
    tree = scope.get('conditions')
    if tree and tree.get('conditions'):
        lines = tree['conditions'] if tree.get('count') != 'each' and (tree.get('match', 'all') == 'all' or len(tree['conditions']) == 1) else [tree]
        taken = {c['field'] for c in who + where}
        for c in lines:
            if ('conditions' not in c and c['field'] in ('programme_ids', 'rotation_type_ids') and c.get('op', 'any') == 'any'
                    and c.get('value') is None and len(absolute_values(c) or ()) == 1 and c['field'] not in taken):
                c = {'match': 'all', 'conditions': [c]}
            (who if _route(c) == 'who' else where).append(c)
    who += list(extra_who)
    where += list(extra_where)
    if len(who) > 20 or len(where) > 20:
        raise Kept('more conditions than a line holds')
    return ({'match': 'all', 'conditions': who} if who else None), ({'match': 'all', 'conditions': where} if where else None)


def lines_of(p, *, validate=True):
    """An old rule as catalogue lines, exactly as `active_rules` reads it (its own line for everyone it matches unless
    it is for its lines only, then one line per row of its table)."""
    d = normal(p['definition']) if validate else p['definition']
    entry = CATALOGUE_BY_ID[entry_of({**p, 'definition': d})]
    rule, scope, rows, field = d['rule'], d['scope'], d['per_person'], line_field(d)
    items = [] if d['listed_only'] else [(scope, rule, p.get('starts_on'), p.get('ends_on'), (), ())]
    for r in rows:
        s = dict(scope)
        if r.get('programme_id'):
            s['programme_id'] = r['programme_id']
        if r.get('category_ids'):
            s.update(category_id=None, category_ids=r['category_ids'])
        if r.get('service_ids'):
            s.update(service_id=None, service_ids=r['service_ids'])
        if r.get('weekdays') is not None:
            s['weekdays'] = r['weekdays']
        if r.get('start_time') is not None:
            s.update(start_time=r['start_time'], end_time=r['end_time'])
        who = [{'field': 'person_ids', 'op': 'any', 'values': [r['person_id']]}] if r.get('person_id') else []
        types = [{'field': 'assignment_type_ids', 'op': 'any', 'values': r['assignment_type_ids']}] if r.get('assignment_type_ids') else []
        items.append((s, line_rule(rule, r, field), _later(p.get('starts_on'), r.get('starts_on')), _earlier(p.get('ends_on'), r.get('ends_on')), who, types))
    out = []
    for n, (s, r, starts, ends, who, types) in enumerate(items):
        numbers, settings = numbers_of(entry, r, d, field)
        line_who, line_where = who_where(s, who, types)
        out.append({'id': str(uuid.uuid5(NAMESPACE, f"{p['id']}:{n}")), 'who': line_who, 'where': line_where,
                    'weekdays': list(s.get('weekdays', range(7))), 'start_time': s.get('start_time', '00:00'), 'end_time': s.get('end_time', '24:00'),
                    'numbers': numbers, 'settings': {r['kind']: settings}, 'starts_on': str(starts) if starts else None,
                    'ends_on': str(ends) if ends else None, 'calendar_entry_id': d.get('calendar_entry_id'),
                    'note': f"From “{p['name']}”"[:500]})
    return entry['id'], out


def catalogue_row(entry_id, rules, lines, previous=None, department_id=None, row_id='00000000-0000-4000-9000-000000000000'):
    """The proposed catalogue rule (a policy input and, for the proof, the row it becomes)."""
    starts = min([str(p['starts_on']) for p in rules] + ([str(previous['starts_on'])] if previous else []))
    definition = {'rule': {'kind': 'catalogue', 'id': entry_id, 'on': True, 'settings': {}, 'lines': lines},
                  'exception_allowed': all(p['definition'].get('exception_allowed') for p in rules),
                  'merged_ids': ([previous['id']] if previous else []) + [p['id'] for p in rules]}
    payload = {'name': CATALOGUE_BY_ID[entry_id]['name'], 'owner': rules[0].get('owner') or 'Department', 'active': True,
               'source': f"Rule catalogue: {len(rules)} rule{'s' if len(rules) != 1 else ''} as lines", 'starts_on': starts, 'ends_on': None,
               'definition': normal(definition)}
    return payload, {**payload, 'id': row_id, 'kind': 'catalogue', 'status': 'active', 'revision': 1, 'department_id': department_id}


def held_reasons(policies, exceptions=(), adjusted=(), targets=()):
    """Rules that stay as they are whatever their shape: {rule id: reason}."""
    out = {}
    overridden = {t for p in policies if p['status'] == 'active' for t in p['definition'].get('overrides') or []}
    waiting = {i for p in policies if p['status'] == 'proposed' for i in p['definition'].get('merged_ids') or []}
    for p in policies:
        d = p['definition']
        reason = ('a rota-only rule' if d.get('workspace_id') else 'it replaces another rule for some people' if d.get('overrides')
                  else 'another rule replaces it for some people' if p['id'] in overridden
                  else 'it has person exceptions' if p['id'] in set(exceptions)
                  else 'a rota not archived changes it' if p['id'] in set(adjusted)
                  else 'a rota not archived changes its target' if p['id'] in set(targets)
                  else 'a proposal waiting for approval takes it over' if p['id'] in waiting else None)
        if reason:
            out[p['id']] = reason
    return out


def plan(policies, held=None, week_start=0, department_id=None):
    """(proposals, report). `policies`: the department's active and proposed rules (catalogue rules too); `held`:
    {rule id: reason} for rules that must stay (see `held_reasons`). Each proposal: {catalogue_id, rule_ids, payload,
    row, replaces_id}; each report item: {rule_id, name, kind, reason}.

    Overtime offers name the limits they need by id; a limit grouped before is known by the rule in force that holds
    it. An offer that joins names that rule (its lines carry the old ids); an offer kept as it is keeps the limits it
    needs out of the catalogue, so its old ids still read as in force."""
    held = dict(held or {})
    active = {p['id']: p for p in policies if p['status'] == 'active' and p.get('active', True)}
    holder = {x: p['id'] for p in active.values() for x in p['definition'].get('merged_ids') or []}

    def in_force(i):
        seen = set()
        while i not in active and i in holder and i not in seen:
            seen.add(i)
            i = holder[i]
        return i
    for _ in range(10):
        proposals, report = _plan(policies, held, week_start, department_id, in_force)
        moved = set(id_map(proposals))
        needs = {}
        for p in active.values():
            if p['kind'] == 'allocation_overtime' and p['id'] not in moved:
                for x in p['definition']['rule'].get('required_policy_ids') or []:
                    if in_force(x) != x and in_force(x) in moved:
                        needs.setdefault(in_force(x), f"an overtime offer kept as it is (“{p['name']}”) needs it under an earlier name")
        if not needs:
            return proposals, report
        held.update(needs)
    raise RuntimeError('Overtime limits kept moving between plans')


def _plan(policies, held, week_start, department_id, in_force):
    active = [p for p in policies if p['status'] == 'active' and p.get('active', True)]
    catalogue = {p['definition']['rule']['id']: p for p in active if p['kind'] == 'catalogue'}
    waiting = {p['definition']['rule']['id'] for p in policies if p['status'] == 'proposed' and p['kind'] == 'catalogue'}
    report, by_entry = [], {}
    for p in sorted((p for p in active if p['kind'] != 'catalogue'), key=lambda p: (p['name'], p['id'])):
        try:
            if p['id'] in held:
                raise Kept(held[p['id']])
            definition = p['definition']
            stored = definition.get('severity', 'mandatory')
            if effective_severity(definition['rule']['kind'], stored) != stored:
                raise Kept(SAFETY_REVIEW_REASON)
            entry_id, lines = lines_of(p)
            for line in lines:  # an overtime offer names the limits in force that hold the ones it needs
                offer = line['settings'].get('allocation_overtime')
                if offer and offer.get('required_policy_ids'):
                    offer['required_policy_ids'] = list(dict.fromkeys(in_force(x) for x in offer['required_policy_ids']))
            if entry_id in waiting:
                raise Kept(f"a proposal for “{CATALOGUE_BY_ID[entry_id]['name']}” is waiting for approval")
            by_entry.setdefault(entry_id, []).append((p, lines))
        except (Kept, ValueError, KeyError) as error:
            report.append(_reported(p, error))
    proposals = []
    for n, (entry_id, members) in enumerate(sorted(by_entry.items())):
        row_id = f'00000000-0000-4000-9000-{n:012d}'
        members, kept = _resolved(entry_id, members, catalogue.get(entry_id), department_id, row_id, week_start)
        report += kept
        if members:
            rules = [p for p, _ in members]
            lines = [line for previous in [catalogue.get(entry_id)] if previous for line in previous['definition']['rule']['lines']]
            lines += [line for _, ls in members for line in ls]
            payload, row = catalogue_row(entry_id, rules, lines, catalogue.get(entry_id), department_id, row_id)
            proposals.append({'catalogue_id': entry_id, 'rule_ids': [p['id'] for p in rules], 'payload': payload, 'row': row,
                              'replaces_id': catalogue[entry_id]['id'] if entry_id in catalogue else None})
    return proposals, sorted(report, key=lambda r: (r['name'], r['rule_id']))


def _reported(p, error):
    message = str(error) if isinstance(error, Kept) else f"its settings do not fit a catalogue line ({str(error).splitlines()[0][:160]})"
    return {'rule_id': p['id'], 'name': p['name'], 'kind': p['kind'], 'reason': REPORT + message}


def _resolved(entry_id, members, previous, department_id, row_id, week_start):
    """The rules of one entry that read exactly the same as lines of one catalogue rule (and the others, reported)."""
    report, alone = [], {}
    while members:
        rules = [p for p, _ in members]
        lines = ([line for line in previous['definition']['rule']['lines']] if previous else []) + [line for _, ls in members for line in ls]
        origin = {line['id']: p for p, ls in members for line in ls}
        try:
            payload, row = catalogue_row(entry_id, rules, lines, previous, department_id, row_id)
            copies = active_rules([row])
        except (ValueError, KeyError) as error:  # one rule's lines are not valid catalogue lines: find it alone
            # Each rule is checked alone once (not once per rule removed: that was quadratic at department scale).
            for p, ls in members:
                if p['id'] not in alone:
                    alone[p['id']] = _valid_alone(entry_id, p, ls, department_id, row_id)
            bad = next((p for p, _ in members if not alone[p['id']]), members[-1][0])
            report.append(_reported(bad, error))
            members = [(p, ls) for p, ls in members if p is not bad]
            continue
        removed = set()

        def present(n):
            owner = origin.get(lines[n]['id'])
            return owner is None or owner['id'] not in removed

        # Removing lines cannot create duplicates or precedence: reuse their indexed relations, in the same order.
        for k, n in duplicates(row['definition']['rule']):
            if not present(k) or not present(n):
                continue
            first, second = origin.get(lines[k]['id']), origin.get(lines[n]['id'])
            kept = second or first
            if kept is None:
                continue
            other = lines[k] if kept is second else lines[n]  # the line it duplicates: of another rule, or the catalogue rule's
            whose = ('two of its lines are the same line' if origin.get(other['id']) is kept else
                     f"a line of it is the same line as one of “{_source(other, origin, previous)}”")
            report.append({'rule_id': kept['id'], 'name': kept['name'], 'kind': kept['kind'],
                           'reason': REPORT + whose + " (what it counts, who, where, days and dates); both apply today"})
            removed.add(kept['id'])
        for n, wins in outranking(row).items():
            if not present(n):
                continue
            loser = origin.get(lines[n]['id'])
            for w in wins:
                if not present(w):
                    continue
                winner = origin.get(lines[w]['id'])
                if winner is not None and winner is not loser:
                    report.append({'rule_id': winner['id'], 'name': winner['name'], 'kind': winner['kind'],
                                   'reason': REPORT + f"a line of it would replace a line of “{loser['name'] if loser else 'the catalogue rule'}” for the same "
                                                     "people and sessions (both apply today); unresolved outranking"})
                    removed.add(winner['id'])
        if removed:
            members = [(p, ls) for p, ls in members if p['id'] not in removed]
            continue
        different = [p for p, ls in members if not _same(p, ls, copies, row_id, week_start)]
        if different:
            report += [_reported(p, Kept('its lines would not check exactly what it checks today')) for p in different]
            members = [(p, ls) for p, ls in members if p not in different]
            continue
        return members, report
    return [], report


def _source(line, origin, previous):
    """The rule a line comes from: an old rule joining now, or the old rule a catalogue line was made from (its note
    “From “…””), else the catalogue rule itself."""
    if origin.get(line['id']):
        return origin[line['id']]['name']
    found = re.fullmatch(r'From “(.+)”', line.get('note') or '')
    return found.group(1) if found else previous['name']


def _valid_alone(entry_id, p, lines, department_id, row_id):
    try:
        active_rules([catalogue_row(entry_id, [p], lines, None, department_id, row_id)[1]])
        return True
    except (ValueError, KeyError):
        return False


def _same(p, lines, copies, row_id, week_start):
    """Whether an old rule's lines, among the catalogue rule's, read exactly as the old rule does."""
    ids = {line['id'] for line in lines}
    before = expanded([p], {p['id']: row_id}, week_start)
    after = expanded_copies(copies, None, week_start, select=lambda c: c['template_line']['id'] in ids)
    return before == after


def id_map(proposals):
    """Old rule id -> the catalogue rule (row id) it became, for the proof."""
    return {i: x['row']['id'] for x in proposals for i in x['rule_ids'] + ([x['replaces_id']] if x['replaces_id'] else [])}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _census_rule(policy):
    """Return the stored or losslessly projected lines used by the conversion proof."""
    definition = policy['definition']
    source_rule = definition['rule']
    version = source_rule.get('catalogue_version', 1)
    if source_rule['kind'] == 'catalogue':
        entry_id, lines = source_rule.get('id'), deepcopy(source_rule.get('lines') or [])
        rule = deepcopy(source_rule)
    else:
        entry_id, lines = lines_of(policy)
        rule = {'kind': 'catalogue', 'id': entry_id, 'catalogue_version': 1, 'lines': lines}
    return entry_id, version, rule, lines


def _line_safety(entry_id, version, rule, line, *, mandatory=True):
    try:
        entry = catalogue_entry(entry_id, version)
        numbers = line.get('numbers') or {}
        for key, number in numbers.items():
            slot = slot_for_number(entry, key, rule, line, numbers)
            if effective_severity(slot['kind'], None, advisory_slot=slot['advisory']) != 'mandatory':
                continue
            level = number.get('level', 'mandatory')
            if level is None:
                level = 'medium' if slot['advisory'] or 'mandatory' not in entry['levels'] else 'mandatory'
            if (level == 'mandatory') == mandatory:
                return True
    except (KeyError, TypeError, ValueError):
        return False
    return False


def _line_classifications(policy, entry_id, version, rule, lines):
    """Classify a source line using the same duplicate/precedence helpers as authoring."""
    projected = {**policy, 'definition': {'rule': rule}}
    try:
        duplicate_indexes = {index for pair in duplicates(rule) for index in pair}
        overlap_indexes = {index for index, winners in outranking(projected).items() for index in (index, *winners)}
        equal_tier_indexes = {index for pair in both_apply(projected) for index in pair}
        authority_error = False
    except (KeyError, TypeError, ValueError):
        # A malformed legacy row stays an owner decision; the census never invents a second checker.
        duplicate_indexes = overlap_indexes = equal_tier_indexes = set()
        authority_error = True
    target_id = CATALOGUE_MAPPING.get(entry_id, entry_id)
    out = []
    for index, line in enumerate(lines):
        people = named_people(line) if tier(line) == 3 else []
        person_fact = _fact_kind(entry_id, version, rule, line, people)
        if authority_error:
            classification = 'owner_decision'
        elif index in duplicate_indexes or index in overlap_indexes or index in equal_tier_indexes:
            classification = 'duplicate_or_overlap'
        elif person_fact:
            classification = 'person_fact'
        elif target_id == 'workload_limit':
            classification = 'v2_workload_limit'
        else:
            classification = 'owner_decision'
        out.append({'classification': classification,
                    'duplicate': index in duplicate_indexes,
                    'overlap': index in overlap_indexes,
                    'equal_tier': index in equal_tier_indexes,
                    'mandatory_safety': _line_safety(entry_id, version, rule, line)})
    return out


def _fact_kind(entry_id, version, rule, line, people):
    fact_kind = PERSON_CALENDAR_FACT_MAPPING.get(entry_id)
    if fact_kind and (people or line.get('calendar_entry_id')):
        return fact_kind
    if (catalogue_entry(entry_id, version) or {}).get('safety'):
        return None
    if line.get('calendar_entry_id'):
        return 'protected_time'
    if people:
        if entry_id == 'overtime_offers':
            return 'overtime_offer'
        if CATALOGUE_MAPPING.get(entry_id, entry_id) == 'workload_limit':
            return 'workload'
        if entry_id == 'fair_share' and line.get('settings', {}).get('balance_goal', {}).get('aim') == 'target':
            return 'target'
    return None


def census(policies, *, audit_refs=None):
    """Deterministic, read-only source census for the rules-cleanup handoff.

    Active and proposed rows are projected for consolidation. Retired rows remain
    as hash-bound history references and are never treated as current rules.
    """
    refs = audit_refs or {}
    sources, lines, history = [], [], []
    for policy in sorted(policies, key=lambda p: (str(p.get('id')), int(p.get('revision', 1)), str(p.get('status')))):
        source_ref = refs.get(policy.get('id'), {'definition_history_ids': [], 'audit_event_ids': [],
                                                 'rule_source_binding_ids': [], 'migration_proposal_ids': []})
        if policy.get('status') not in {'active', 'proposed'}:
            history.append({'source_policy_id': policy.get('id'), 'source_revision': policy.get('revision', 1),
                            'status': policy.get('status'), 'kind': policy.get('kind'),
                            'classification': 'superseded' if source_ref.get('rule_source_binding_ids') else 'retired',
                            'source_hash': _digest(policy), 'audit_refs': deepcopy(source_ref)})
            continue
        try:
            entry_id, version, rule, source_lines = _census_rule(policy)
        except (KeyError, TypeError, ValueError) as error:
            sources.append({'source_policy_id': policy.get('id'), 'source_revision': policy.get('revision', 1),
                            'status': policy.get('status'), 'kind': policy.get('kind'), 'catalogue_id': None,
                            'classification': 'owner_decision', 'reason': str(error), 'line_ids': [],
                            'source_hash': _digest(policy), 'audit_refs': deepcopy(source_ref)})
            continue
        classifications = _line_classifications(policy, entry_id, version, rule, source_lines)
        source_lines_out = []
        for index, (line, finding) in enumerate(zip(source_lines, classifications)):
            dates = line_validity({**policy, 'definition': {'rule': rule}}, line, '')
            row = {'source_policy_id': policy['id'], 'source_revision': policy.get('revision', 1),
                   'source_line_id': line['id'], 'line_index': index, 'status': policy['status'],
                   'kind': policy['kind'], 'catalogue_id': entry_id,
                   'target_catalogue_id': CATALOGUE_MAPPING.get(entry_id, entry_id),
                   'scope': scope_of(line), 'who': deepcopy(line.get('who')), 'where': deepcopy(line.get('where')),
                   'weekdays': list(line.get('weekdays') or range(7)),
                   'start_time': line.get('start_time') or '00:00', 'end_time': line.get('end_time') or '24:00',
                   'starts_on': dates['starts_on'], 'ends_on': dates['ends_on'],
                   'calendar_entry_id': line.get('calendar_entry_id'), 'note': line.get('note', ''),
                   'strengths': {key: (number.get('level') or 'mandatory')
                                 for key, number in (line.get('numbers') or {}).items()},
                   'numbers': deepcopy(line.get('numbers') or {}),
                   'source_bindings': deepcopy(line.get('source_bindings') or {}),
                   'line_hash': _digest(line), 'audit_refs': deepcopy(source_ref), **finding}
            source_lines_out.append(row)
            lines.append(row)
        classifications_set = {row['classification'] for row in source_lines_out}
        source_classification = ('duplicate_or_overlap' if 'duplicate_or_overlap' in classifications_set else
                                 'person_fact' if classifications_set == {'person_fact'} else
                                 'v2_workload_limit' if classifications_set == {'v2_workload_limit'} else
                                 'owner_decision' if not classifications_set or 'owner_decision' in classifications_set else 'mixed')
        sources.append({'source_policy_id': policy['id'], 'source_revision': policy.get('revision', 1),
                        'status': policy['status'], 'kind': policy['kind'], 'catalogue_id': entry_id,
                        'target_catalogue_id': CATALOGUE_MAPPING.get(entry_id, entry_id),
                        'classification': source_classification,
                        'line_ids': [row['source_line_id'] for row in source_lines_out],
                        'source_hash': _digest(policy), 'audit_refs': deepcopy(source_ref)})
    body = {'version': 'rules-cleanup-census-1', 'sources': sources, 'lines': lines, 'history': history,
            'counts': {'sources': len(sources), 'lines': len(lines), 'history_sources': len(history),
                       'active_sources': sum(row['status'] == 'active' for row in sources),
                       'proposed_sources': sum(row['status'] == 'proposed' for row in sources),
                       'superseded_history': sum(row['classification'] == 'superseded' for row in history)}}
    body['hash'] = _digest(body)
    return body


def _derived(*parts):
    return str(uuid.uuid5(NAMESPACE, ':'.join(str(p) for p in parts)))


def _number_shapes(value):
    """JSON number spelling is source shape metadata, never another clinical value."""
    return {key: 'float' if isinstance(number, float) else 'int' for key, number in value.items()
            if isinstance(number, (int, float)) and not isinstance(number, bool)}


def _source_line(rule, line):
    """Freeze the resolved primitive settings, including fields fixed by the old catalogue entry."""
    entry = catalogue_entry(rule['id'], rule.get('catalogue_version', 1))
    line = deepcopy(line)
    settings = _settings(rule, line)
    if rule.get('id') == 'fair_share':
        goal = fair_share_goal_settings(rule, line)
        if goal is None:
            raise Kept('Fair-share target has no measure; open the rule')
        settings['balance_goal'] = goal
    for key in line.get('numbers') or {}:
        slot = slot_for_number(entry, key, rule, line, list(line['numbers']))
        settings.setdefault(slot['kind'], {}).update({k: v for k, v in slot['fixed'].items() if k != 'comparison'})
    line['settings'] = settings
    return line


def _primitive_line(entry_id, line, shape, fallback=None):
    """Read a legacy primitive from its canonical destination; the old evaluator still expands its rows."""
    entry = CATALOGUE_BY_ID[entry_id]
    kind = shape['kind']
    settings = (line.get('settings') or {}).get(kind, {})
    body = {**_defaults(kind), **entry['default_settings'].get(kind, {}), **(fallback or {}), **settings, 'kind': kind}
    keys = list(line.get('numbers') or {})
    for key, number in (line.get('numbers') or {}).items():
        slots = [s for s in entry['numbers'].get(key, ()) if s['kind'] == kind]
        if not slots:
            continue
        slot = slots[0]
        body.update({field: settings.get(field, value) for field, value in slot['fixed'].items()})
        if slot['field'] and number.get('value') is not None:
            value = number['value']
            annotation = _MODELS[kind].model_fields[slot['field']].annotation
            value = int(value) if float(value).is_integer() and (annotation is int or int in get_args(annotation)) else value
            if kind == 'block_quota':
                field = ('maximum_amount' if shape.get('comparison') == 'range' and key == 'max' else
                         'target_amount' if key == 'target' and shape.get('comparison') != 'target' else 'amount')
                body[field] = value
                if field == 'target_amount':
                    body['target_strength'] = number.get('level')
            else:
                body[slot['field']] = value
    if kind == 'block_quota':
        body['comparison'] = shape['comparison']
        # Cleared bounds emit no copy; placeholders only permit the legacy range/target expansion.
        if body.get('amount') is None:
            body['amount'] = body.get('target_amount') or 0 if body['comparison'] == 'maximum' else 0
        if body['comparison'] == 'range' and body.get('maximum_amount') is None:
            body['maximum_amount'] = max(body['amount'], body.get('target_amount') or 0)
    keys = list(shape['keys'])
    if kind == 'block_quota' and body.get('target_amount') is not None:
        keys.extend(key for key in ('target_amount', 'target_strength') if key not in keys)
    for key, value in settings.items():
        field = _MODELS[kind].model_fields.get(key)
        if key not in keys and field is not None and value != _defaults(kind).get(key, field.get_default(call_default_factory=True)):
            keys.append(key)
    out = {k: body.get(k) for k in keys}
    for key, number_shape in shape.get('number_shapes', {}).items():
        value = out.get(key)
        if isinstance(value, (int, float)):
            if number_shape == 'float':
                out[key] = float(value)
            elif float(value).is_integer():
                out[key] = int(value)
    return out


def _fact_line_people(line, people):
    """Narrow an explicit person predicate without wrapping or moving its Count paths."""
    line = deepcopy(line)
    # Unchanged subjects retain the authored filters and their strict-proof/report identities.
    if set(named_people(line)) == people:
        return line
    def narrow(condition):
        if condition.get('field') != 'person_ids' or condition.get('op', 'any') not in {'any', 'is'}:
            return condition
        selected = absolute_values(condition)
        if selected is None:
            return condition
        selected = [person for person in selected if person in people]
        if condition.get('value') is not None:
            return {**condition, 'value': {'kind': 'absolute', 'value': selected}}
        return {**condition, 'values': selected}
    def tree(value):
        return {**value, 'conditions': [tree(c) if 'conditions' in c else narrow(c)
                                      for c in value.get('conditions') or []]}
    if line.get('who'):
        line['who'] = tree(line['who'])
    return line


def source_policy(binding, destinations):
    """Compatibility metadata supplies identity/shape; only approved destinations supply clinical values."""
    source = deepcopy(binding['source'])
    source['_v2_source'] = True
    d = source['definition']
    by_line, personal = {}, {}
    for destination, line in destinations:
        if destination.get('role') == 'personal_amount':
            number = (line.get('numbers') or {}).get('target')
            if number and number.get('value') is not None:
                personal.setdefault(destination['source_line_index'], {})[destination['person_id']] = number['value']
            continue
        by_line.setdefault(destination['source_line_index'], []).append((destination, line))
    lines = []
    for n, items in sorted(by_line.items()):
        # A multi-person source remains one obligation; separate fact versions may narrow its subjects independently.
        groups = {}
        for destination, line in items:
            clean = deepcopy(line)
            groups.setdefault(_digest(clean), []).append((destination, clean))
        for group in groups.values():
            destination, line = group[0]
            people = {item['person_id'] for item, _ in group if item.get('person_id')}
            if people:
                line = _fact_line_people(line, people)
            line['id'] = destination['source_line_id']
            line['_source_index'] = n
            if len(groups) > 1:
                line['_source_identity'] = line['id']
                line['id'] = _derived('source-variant', line['id'], _digest(line))
            if n in personal:
                line.setdefault('settings', {}).setdefault('balance_goal', {})['person_amounts'] = personal[n]
            lines.append((n, line))
    if binding['source_kind'] == 'catalogue':
        rule = deepcopy(binding['rule_shape'])
        target_id = CATALOGUE_MAPPING.get(rule['id'], rule['id'])
        if target_id:
            rule.update(id=target_id, catalogue_version=2)
        rule['settings'] = {}
        rule['lines'] = [line for _, line in lines]
        d['rule'] = rule
        return source
    base_lines = [line for n, line in lines if n == -1]
    entry = CATALOGUE_BY_ID[binding['source_catalogue_id']]
    meaningful = [line for line in base_lines if any(number.get('value') is not None or
                  any(slot['kind'] == binding['source_kind'] and slot['field'] is None
                      for slot in entry['numbers'].get(key, ()))
                  for key, number in (line.get('numbers') or {}).items())]
    if base_lines and not meaningful and not any(n >= 0 for n, _ in lines):
        return None
    base = next(iter(meaningful or base_lines), None)
    if base is None:
        base = next((line['source_default'] for _, line in lines if line.get('source_default')), None)
    live_default = base is not None
    if not live_default and d.get('listed_only'):
        entry = CATALOGUE_BY_ID[binding['source_catalogue_id']]
        # A cleared number without a default contributes nothing; apply switches still do.
        lines = [(n, line) for n, line in lines if any(number.get('value') is not None or
                 any(slot['kind'] == binding['source_kind'] and slot['field'] is None for slot in entry['numbers'].get(key, ()))
                 for key, number in (line.get('numbers') or {}).items())]
    if base is None and d.get('listed_only') and lines:
        # Explicit listed rows own every number and resolved setting; no live default is needed.
        base = lines[0][1]
    if base is None:
        return None
    rule = _primitive_line(binding['source_catalogue_id'], base, binding['rule_shape'])
    if -1 in personal:
        rule['person_amounts'] = personal[-1]
    d['rule'] = rule
    if len(base_lines) > 1:
        source['_v2_base_variants'] = [{'line': line, 'rule': _primitive_line(
            binding['source_catalogue_id'], line, binding['rule_shape'])} for line in base_lines]
    rows, row_indices, row_templates, quota_blocks = [], [], [], []
    quota_rows = rule['kind'] == 'block_quota' and any(n >= 0 for n, _ in lines)
    explicit_quota = not live_default and quota_rows
    row_counts = {}
    for n, _ in lines:
        if n >= 0:
            row_counts[n] = row_counts.get(n, 0) + 1
    runtime_lines = [(n, line) for n, line in lines if n < 0]
    row_shapes = binding.get('row_shapes') or []
    for n, line in lines:
        if n < 0:
            continue
        shape = row_shapes[n]
        row = deepcopy(shape['row'])
        row_rule_shape = {**binding['rule_shape'], 'keys': list(dict.fromkeys([*binding['rule_shape']['keys'], *shape['settings_keys']])),
                          'number_shapes': {**binding['rule_shape'].get('number_shapes', {}), **shape.get('setting_number_shapes', {})}}
        own = _primitive_line(binding['source_catalogue_id'], line, row_rule_shape, rule if live_default else None)
        field = line_field(d)
        number_key = shape.get('amount_key')
        number = (line.get('numbers') or {}).get(number_key)
        if shape.get('has_amount') or number:
            value = number.get('value') if number else None
            row['amount'] = (None if value is None else float(value) if shape.get('amount_float') else
                             int(value) if value is not None and float(value).is_integer() else value)
        settings = {k: own.get(k) for k in shape['settings_keys']}
        settings.update({k: None for k in shape['settings_none']})
        if shape['has_settings']:
            row['settings'] = None if shape.get('settings_null') and not settings else settings
        row_templates.append(deepcopy(row))
        if row_counts[n] > 1 and not row.get('person_id'):
            # Divergent facts partition one generic authoring row; its public provenance stays generic.
            people = named_people(line)
            if people:
                row['person_id'] = people[0]
        if quota_rows:
            quota_blocks.append(own)
            # Every approved bound belongs to this row, even if its old authoring shape inherited it.
            if explicit_quota:
                row['amount'] = (row['amount'] if row.get('amount') is not None else
                                 own.get('target_amount') or 0 if rule['comparison'] == 'maximum' else 0)
            row['settings'] = {**(row.get('settings') or {}), 'target_amount': own.get('target_amount')}
            if rule['comparison'] == 'range':
                row['settings']['maximum_amount'] = (own['maximum_amount'] if own.get('maximum_amount') is not None
                                                     else max(row.get('amount') or 0, own.get('target_amount') or 0))
        runtime_lines.append((len(rows), line))
        rows.append(row)
        row_indices.append(n)
    if binding.get('has_rows'):
        d['per_person'] = rows
    if explicit_quota:
        rule.update(target_amount=None, target_strength=None)
        rule['amount'] = min(row['amount'] for row in rows)
        rule['starts_on'] = min([body['starts_on'] for body in quota_blocks] + [row['starts_on'] for row in rows if row.get('starts_on')])
        ends = [body.get('ends_on') for body in quota_blocks]
        rule['ends_on'] = (max([*ends, *[row['ends_on'] for row in rows if row.get('ends_on')]])
                           if all(ends) else None)
        if rule['comparison'] == 'range':
            rule['maximum_amount'] = max(row['settings']['maximum_amount'] for row in rows)
    if quota_rows:
        # Independent v2 rows need no common metric/window/block. Keep the schema carrier private.
        carrier = {**rule, 'metric': 'scheduled_hours', 'window_days': None, 'window_alignment': 'rolling',
                   'window_anchor_on': None, 'prorate_partial_windows': False, 'repeat': False,
                   'prorate_department_days': False, 'amount': 0, 'target_amount': None, 'target_strength': None}
        if rule['comparison'] == 'range':
            carrier['maximum_amount'] = max(row['settings']['maximum_amount'] for row in rows)
        starts = [date.fromisoformat(str(row['starts_on'])) for row in rows if row.get('starts_on')]
        ends = [date.fromisoformat(str(row['ends_on'])) for row in rows if row.get('ends_on')]
        if starts or ends:
            # Original row dates fit one valid block and retain distinct same-person row identities.
            carrier['starts_on'] = min(starts + [end - timedelta(days=1) for end in ends]).isoformat()
            carrier['ends_on'] = max(ends + [start + timedelta(days=1) for start in starts]).isoformat()
        else:
            carrier['starts_on'], carrier['ends_on'] = quota_blocks[0]['starts_on'], quota_blocks[0]['ends_on']
        if rule.get('repeat') or not carrier.get('ends_on'):
            carrier['ends_on'] = (date.fromisoformat(str(carrier['starts_on'])) + timedelta(days=1)).isoformat()
        source['_v2_validation_definition'] = {**d, 'rule': carrier}
        PolicyDefinition.model_validate(source['_v2_validation_definition'], context={'historical': True})
    if rows:
        source['_v2_row_templates'] = row_templates
    # This assembled wrapper is structurally checked above; the compiler checks every operative row separately.
    _, expected = lines_of(source, validate=not quota_rows)
    offset = 0 if d.get('listed_only') else 1
    expected = ({-1: expected[0]} if offset and expected else {}) | {
        n: line for n, line in enumerate(expected[offset:])}
    fields = ('who', 'where', 'weekdays', 'start_time', 'end_time', 'starts_on', 'ends_on')
    source['_v2_source_lines'] = {n: line for n, line in runtime_lines}
    source['_v2_source_default'] = base if live_default else {}
    source['_v2_row_indices'] = row_indices
    source['_v2_changed_scope'] = [n for n, line in runtime_lines
                                  if any(line.get(key) != expected.get(n, {}).get(key) for key in fields)]
    source['_v2_changed_dates'] = [n for n, line in runtime_lines
                                  if any(line.get(key) != expected.get(n, {}).get(key) for key in ('starts_on', 'ends_on'))]
    kind = binding['rule_shape']['kind']
    editable = (V2_CATALOGUE_BY_ID.get(CATALOGUE_MAPPING[binding['source_catalogue_id']])
                or CATALOGUE_BY_ID[binding['source_catalogue_id']])['line_settings'].get(kind, ())
    if kind == 'block_quota':
        editable = set(editable) - {'comparison', 'amount', 'maximum_amount', 'target_amount', 'target_strength'}
    edits = {}
    for n, line in runtime_lines:
        entry = CATALOGUE_BY_ID[binding['source_catalogue_id']]
        selected = {slot_for_number(entry, key, {}, line, list(line.get('numbers') or {}))['field']
                    for key in line.get('numbers') or {}}
        current = {**_defaults(kind), **rule, **line.get('settings', {}).get(kind, {})}
        previous = {**_defaults(kind), **expected.get(n, {}).get('settings', {}).get(kind, {})}
        changed = {key: current.get(key) for key in set(editable) - selected if current.get(key) != previous.get(key)}
        if changed:
            edits[n] = changed
    source['_v2_setting_edits'] = edits
    return source


def plan_v2(policies, held=None, week_start=0, department_id=None, *, tag_ids=(), default_actor_id=None,
            today=None, publication_start=None):
    """One deterministic department proposal. A blocker prevents every part of its activation."""
    held = dict(held or {})
    result = {'version': 2, 'department_id': department_id, 'targets': [], 'scheduling_facts': [],
              'bindings': [], 'source_manifest': [], 'blocked': [], 'report': []}
    cutoff = min(str(today), str(publication_start)) if today and publication_start else str(today) if today else None
    if cutoff:
        result['retirement_cutoff'] = cutoff
    tag_ids = set(tag_ids)
    active = [p for p in policies if p['status'] == 'active' and p.get('active', True)]
    if any(p['status'] == 'proposed' for p in policies):
        result['blocked'].append({'rule_id': '', 'name': '', 'kind': 'proposal',
                                  'reason': 'Resolve pending policy proposals before the department conversion',
                                  'proposal_ids': [p['id'] for p in policies if p['status'] == 'proposed']})
    targets = {}
    for p in policies:
        result['source_manifest'].append({'id': p['id'], 'revision': p.get('revision', 1), 'status': p['status'],
                                          'active': p.get('active', True), 'hash': _digest(p)})
    # Undo retains destination history. A later reviewed generation must create fresh
    # records, while repeated previews of unchanged sources remain deterministic.
    generation = _digest([result['source_manifest'], cutoff]) if cutoff else _digest(result['source_manifest'])
    for order, p in enumerate(active):
        if p['kind'] == 'catalogue' and p['definition']['rule'].get('id') == 'team_leads' and p['definition']['rule'].get('catalogue_version', 1) == 2:
            # Leadership can be authored before whole-department conversion; its receipts remain independent.
            continue
        if p['kind'] != 'catalogue' and department_catalogue_version(
                [p], default_actor_id=default_actor_id, lineage=policies) == 2:
            # Standalone maintenance defaults remain unchanged through conversion and Undo.
            continue
        blocker_targets = {}
        try:
            if p['id'] in held:
                raise Kept(held[p['id']])
            d, original = p['definition'], p['definition']['rule']
            version = original.get('catalogue_version', 1)
            if d.get('workspace_id') or d.get('overrides'):
                raise Kept('Rota-only rules and override chains require an exact reviewed selector mapping')
            if any(p['id'] in (q['definition'].get('overrides') or []) for q in active):
                raise Kept('Another rule overrides this source; resolve the override before conversion')
            source = deepcopy(p)
            source['definition'] = {k: deepcopy(v) for k, v in d.items() if k not in ('rule', 'per_person')}
            binding = {'source_policy_id': p['id'], 'source_revision': p.get('revision', 1), 'source_order': order,
                       'source': source, 'source_kind': original['kind'], 'destinations': []}
            if original['kind'] == 'catalogue':
                # Native defaults must join later legacy imports without colliding on their destination rule.
                native_default = (version == 2 and p.get('source') == SCORING_DEFAULT_SOURCE
                    and default_actor_id and p.get('created_by') == default_actor_id == p.get('approved_by')
                    and original['id'] in {'fair_share', 'workload_limit'})
                if version != 1 and not native_default:
                    raise Kept('This department already contains catalogue v2; convert only once')
                entry_id = original['id']
                binding['rule_shape'] = {k: deepcopy(v) for k, v in original.items() if k not in ('lines', 'settings')}
                material = [(n, _source_line(original, line)) for n, line in enumerate(original.get('lines') or [])]
            else:
                entry_id, legacy_lines = lines_of(p)
                shape = {'kind': original['kind'], 'keys': list(original), 'number_shapes': _number_shapes(original)}
                if original['kind'] == 'block_quota':
                    shape['comparison'] = original['comparison']
                binding.update(source_catalogue_id=entry_id, rule_shape=shape, has_rows='per_person' in d, row_shapes=[])
                entry = CATALOGUE_BY_ID[entry_id]
                general_p = {**p, 'definition': {**d, 'per_person': [], 'listed_only': False}}
                _, [base] = lines_of(general_p)
                base = _source_line({'id': entry_id}, base)
                material = [] if d.get('listed_only') else [(-1, base)]
                offset = 0 if d.get('listed_only') else 1
                field = line_field(d)
                for n, row in enumerate(d.get('per_person') or []):
                    line = _source_line({'id': entry_id}, legacy_lines[n + offset])
                    amount_key = next((key for key in line['numbers'] if any(
                        slot['kind'] == original['kind'] and slot['field'] == field for slot in entry['numbers'][key])), None)
                    if row.get('amount') is None and amount_key:
                        if len(line['numbers']) > 1:
                            line['numbers'].pop(amount_key, None)
                        elif amount_key in line['numbers']:
                            line['numbers'][amount_key]['value'] = None
                    settings = row.get('settings') or {}
                    binding['row_shapes'].append({'row': {k: deepcopy(v) for k, v in row.items() if k not in ('amount', 'settings')},
                                                  'has_amount': 'amount' in row, 'amount_key': amount_key,
                                                  'amount_float': isinstance(row.get('amount'), float),
                                                  'has_settings': 'settings' in row,
                                                  'settings_null': row.get('settings') is None,
                                                  'setting_number_shapes': _number_shapes(settings),
                                                  'settings_keys': [k for k, v in settings.items() if v is not None],
                                                  'settings_none': [k for k, v in settings.items() if v is None]})
                    material.append((n, line))
            old_rule = original if original['kind'] == 'catalogue' else {'id': entry_id}
            projected = {**p, 'definition': {'rule': old_rule}}
            if cutoff:
                material = [(n, line) for n, line in material
                            if line_validity(projected, line, cutoff)['effective'] != 'ended']
            if original['kind'] != 'catalogue' and d.get('listed_only') and field is not None and any(
                    d['per_person'][n].get('amount') is None for n, _ in material):
                inherited_indexes = [index for index, _ in material if d['per_person'][index].get('amount') is None]
                groupable = bool(inherited_indexes) and all(
                    (d.get('per_person') or [])[index].get('programme_id') and
                    not ((d.get('per_person') or [])[index].get('settings') or {})
                    for index in inherited_indexes)
                if groupable:
                    for index, item in material:
                        if index in inherited_indexes:
                            item['source_default'] = deepcopy(base)
                else:
                    material[0][1]['source_default'] = base
            stored = d.get('severity', 'mandatory')
            if material and (effective_severity(original['kind'], stored) != stored
                    or any(_line_safety(entry_id, version, old_rule, candidate, mandatory=False)
                           for _, line in material for candidate in (line, line.get('source_default')) if candidate)):
                raise Kept(SAFETY_REVIEW_REASON)
            target_id = CATALOGUE_MAPPING.get(entry_id, entry_id)
            for n, line in material:
                source_line_id = line['id']
                original_entry = catalogue_entry(entry_id, version)
                pins = {}
                selected_fields = {}
                for key in line['numbers']:
                    slot = slot_for_number(original_entry, key, old_rule, line, list(line['numbers']))
                    selected_fields.setdefault(slot['kind'], set()).add(slot['field'])
                    pins[key] = {'policy_id': p['id'], 'revision': p.get('revision', 1), 'line_id': source_line_id,
                                 'line_index': n if n >= 0 else None, 'number_key': key, 'slot': key,
                                 'primitive_kind': slot['kind'], 'precedence_partition': p['id'], 'source_order': order,
                                 'exception_allowed': bool(d.get('exception_allowed')), 'catalogue_id': entry_id, 'catalogue_version': version}
                line['source_bindings'] = pins
                line['id'] = _derived('v2-line', p['id'], source_line_id)
                destination_entry = V2_CATALOGUE_BY_ID.get(target_id) or original_entry
                for kind, settings in line['settings'].items():
                    fields = {slot['field'] for slots in original_entry['numbers'].values() for slot in slots if slot['kind'] == kind}
                    for key in set(settings) - set(destination_entry['line_settings'].get(kind, ())):
                        defaults = {**_defaults(kind), **destination_entry['default_settings'].get(kind, {})}
                        if key in fields and settings[key] != defaults.get(key):
                            raise Kept(f'The unselected {kind}.{key} fallback needs an explicit source-default representation')
                line['settings'] = {kind: {k: value for k, value in settings.items()
                                           if k in destination_entry['line_settings'].get(kind, ()) and k not in selected_fields.get(kind, ())}
                                    for kind, settings in line['settings'].items()}
                if line.get('source_default'):
                    default = line['source_default']
                    default['source_bindings'] = {}
                    default_fields = {}
                    for key in default['numbers']:
                        slot = slot_for_number(original_entry, key, old_rule, default, list(default['numbers']))
                        default_fields.setdefault(slot['kind'], set()).add(slot['field'])
                        default['source_bindings'][key] = {'policy_id': p['id'], 'revision': p.get('revision', 1),
                            'line_id': default['id'], 'number_key': key, 'slot': key, 'primitive_kind': slot['kind'],
                            'precedence_partition': p['id'], 'source_order': order, 'exception_allowed': bool(d.get('exception_allowed')),
                            'catalogue_id': entry_id, 'catalogue_version': version}
                    default['settings'] = {kind: {k: value for k, value in settings.items()
                                                  if k in destination_entry['line_settings'].get(kind, ()) and k not in default_fields.get(kind, ())}
                                           for kind, settings in default['settings'].items()}
                people = named_people(line) if tier(line) == 3 else []
                if any(person in tag_ids for person in people):
                    people = []
                offer = entry_id == 'overtime_offers'
                kind = _fact_kind(entry_id, version, old_rule, line, people)
                own_target = kind == 'target'
                inherited = []
                if line.get('source_default') and kind == 'workload' and d.get('listed_only'):
                    inherited = [(index, item) for index, item in material if index >= 0 and
                                 (d.get('per_person') or [])[index].get('amount') is None]
                    # A programme-scoped listed row can retain its source default as one bound catalogue line.
                    # Person-only rows still block: converting those would freeze an inaccessible default in facts.
                    groupable = bool(inherited) and all(
                        (d.get('per_person') or [])[index].get('programme_id') and
                        not ((d.get('per_person') or [])[index].get('settings') or {})
                        for index, _ in inherited)
                    if groupable:
                        default = line.pop('source_default')
                        line['numbers'] = deepcopy(default['numbers'])
                        line['settings'] = deepcopy(default.get('settings') or {})
                        line['source_bindings'] = {
                            key: {**pin, 'line_id': source_line_id, 'line_index': n}
                            for key, pin in (default.get('source_bindings') or {}).items()}
                        target = targets.setdefault(target_id, {'lines': [], 'sources': []})
                        target['lines'].append(line)
                        if p not in target['sources']:
                            target['sources'].append(p)
                        binding['destinations'].append({'kind': 'policy',
                            'id': _derived('v2-policy', generation, department_id, target_id),
                            'line_id': line['id'], 'source_line_id': source_line_id, 'source_line_index': n})
                        continue
                if offer and not people:
                    raise Kept('An overtime offer must name explicit people; dynamic groups cannot become frozen person offers')
                if kind:
                    if line.get('source_default'):
                        inherited = inherited or [(index, item) for index, item in material if index >= 0 and
                                                   (d.get('per_person') or [])[index].get('amount') is None]
                        blocker_targets = {'person_ids': sorted({d['per_person'][index]['person_id'] for index, _ in inherited
                                                                if d['per_person'][index].get('person_id')}),
                                           'line_ids': [item['id'] if item is not line else source_line_id for _, item in inherited]}
                        raise Kept(REPORT + 'its inherited default would be frozen inside a person/calendar fact; expose an editable catalogue default before conversion')
                    subjects = people or [None]
                    for person_id in subjects:
                        fact_id = _derived('v2-fact', generation, p['id'], source_line_id, person_id, kind)
                        fact_entry = entry_id if kind == 'protected_time' else target_id or entry_id
                        data = {'catalogue_id': fact_entry, 'catalogue_version': 1 if kind == 'protected_time' or not target_id else 2,
                                'line': deepcopy(line), 'exception_allowed': bool(d.get('exception_allowed'))}
                        if own_target:
                            data['goal_ref'] = {'policy_id': p['id'], 'revision': p.get('revision', 1),
                                               'line_id': source_line_id if original['kind'] == 'catalogue' else None, 'slot': 'target'}
                        fact = {'id': fact_id, 'department_id': department_id, 'person_id': person_id,
                                'calendar_entry_id': line.get('calendar_entry_id'), 'kind': kind, 'data': data,
                                'revision': 1, 'status': 'active', 'on': original.get('on', True),
                                'starts_on': line.get('starts_on'), 'ends_on': line.get('ends_on'), 'note': line.get('note', '')}
                        result['scheduling_facts'].append(fact)
                        binding['destinations'].append({'kind': 'fact', 'id': fact_id, 'line_id': line['id'],
                                                         'source_line_id': source_line_id, 'source_line_index': n,
                                                         'person_id': person_id})
                else:
                    target = targets.setdefault(target_id, {'lines': [], 'sources': []})
                    amounts = line.get('settings', {}).get('balance_goal', {}).pop('person_amounts', {})
                    for person_id, amount in amounts.items():
                        amount_line = deepcopy(line)
                        amount_line['numbers'] = {'target': {'value': amount, 'level': (line['numbers'].get('target') or line['numbers'].get('apply') or {}).get('level', 'medium')}}
                        amount_line['source_bindings'] = {'target': {**next(iter(pins.values())), 'number_key': 'target', 'slot': 'target'}}
                        amount_line['who'] = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person_id]}]}
                        amount_line['id'] = _derived('v2-person-target-line', p['id'], source_line_id, person_id)
                        fact_id = _derived('v2-person-target', generation, p['id'], source_line_id, person_id)
                        result['scheduling_facts'].append({'id': fact_id, 'department_id': department_id, 'person_id': person_id,
                            'calendar_entry_id': None, 'kind': 'target', 'revision': 1, 'status': 'active', 'on': original.get('on', True),
                            'data': {'catalogue_id': 'fair_share', 'catalogue_version': 2, 'line': amount_line,
                                     'goal_ref': {'policy_id': p['id'], 'revision': p.get('revision', 1),
                                                  'line_id': source_line_id if original['kind'] == 'catalogue' else None, 'slot': 'target'}}})
                        binding['destinations'].append({'kind': 'fact', 'id': fact_id, 'line_id': amount_line['id'],
                            'source_line_id': source_line_id, 'source_line_index': n, 'role': 'personal_amount', 'person_id': person_id})
                    target['lines'].append(line)
                    if p not in target['sources']:
                        target['sources'].append(p)
                    binding['destinations'].append({'kind': 'policy', 'id': _derived('v2-policy', generation, department_id, target_id),
                        'line_id': line['id'], 'source_line_id': source_line_id, 'source_line_index': n})
            result['bindings'].append(binding)
            result['report'].append({'rule_id': p['id'], 'name': p['name'], 'catalogue_id': entry_id,
                                      'destinations': deepcopy(binding['destinations'])})
        except (Kept, ValueError, KeyError) as error:
            result['blocked'].append({'rule_id': p['id'], 'name': p['name'], 'kind': p['kind'], 'reason': str(error), **blocker_targets})
    for target_id, target in targets.items():
        sources = target['sources']
        payload = {'name': V2_CATALOGUE_BY_ID[target_id]['name'], 'owner': sources[0].get('owner') or 'Department',
                   'active': True, 'source': 'Rule catalogue v2 department conversion',
                   'starts_on': min(str(p['starts_on']) for p in sources), 'ends_on': None,
                   'definition': {'rule': {'kind': 'catalogue', 'id': target_id, 'catalogue_version': 2,
                                           'on': True, 'settings': {}, 'lines': target['lines']},
                                  'exception_allowed': all(p['definition'].get('exception_allowed') for p in sources)}}
        row_id = _derived('v2-policy', generation, department_id, target_id)
        result['targets'].append({'catalogue_id': target_id, 'rule_ids': [p['id'] for p in sources], 'payload': payload,
                                  'row': {**deepcopy(payload), 'id': row_id, 'department_id': department_id,
                                          'kind': 'catalogue', 'status': 'active', 'revision': 1}})
    result['targets'].sort(key=lambda target: list(V2_CATALOGUE_BY_ID).index(target['catalogue_id']))
    for target in result['targets']:
        try:
            PolicyDefinition.model_validate(target['payload']['definition'], context={'historical': True})
        except ValueError as error:
            result['blocked'].append({'rule_id': target['row']['id'], 'name': target['payload']['name'], 'kind': 'contract',
                                      'reason': f'Converted catalogue contract: {error}'})
    from .rule_lines import fact_policy
    for fact in result['scheduling_facts']:
        try:
            PolicyDefinition.model_validate(fact_policy(fact)['definition'], context={'historical': True})
        except ValueError as error:
            result['blocked'].append({'rule_id': fact['id'], 'name': fact.get('note', ''), 'kind': 'contract',
                                      'reason': f'Converted person/calendar contract: {error}'})
    for blocked in result['blocked']:
        for field in ('proposal_ids', 'person_ids', 'line_ids'):
            blocked.setdefault(field, [])
    result['input_hash'] = _digest(result['source_manifest'])
    result['plan_hash'] = _digest(result)
    return result


def after_snapshot(data, proposal):
    """Isolated dry-run inputs: originals remain immutable evidence; a blocked plan changes nothing."""
    if proposal['blocked']:
        raise ValueError('Department conversion is blocked: ' + '; '.join(row['reason'] for row in proposal['blocked']))
    moved = {binding['source_policy_id'] for binding in proposal['bindings']}
    out = deepcopy({**data, 'policies': [p for p in data.get('policies') or [] if p['id'] not in moved]})
    out['policies'].extend(deepcopy(t['row']) for t in proposal['targets'])
    out['scheduling_facts'] = [*(out.get('scheduling_facts') or []), *deepcopy(proposal['scheduling_facts'])]
    out['source_bindings'] = [*(out.get('source_bindings') or []), *deepcopy(proposal['bindings'])]
    out.pop('_catalogue_v2_materialized', None)
    return out


def plan_type_leads(types, effective_on, department_id, histories=()):
    """Pure equivalent conversion of every still-legacy leader definition, never an activation."""
    from packages.contracts.schemas import TeamLead
    effective_on = date.fromisoformat(str(effective_on)).isoformat()
    history = {(row['object_id'], row['object_revision']): row['id'] for row in histories}
    lines, sources = [], []

    def strip_counts(tree):
        if not tree:
            return tree
        return {key: [strip_counts(child) for child in value] if key == 'conditions' else deepcopy(value)
                for key, value in tree.items() if key != 'count'}

    for row in sorted(types, key=lambda r: r['id']):
        if row['department_id'] != department_id or not row['definition'].get('lead'):
            continue
        lead = TeamLead.model_validate(row['definition']['lead']).model_dump(mode='json')
        digest = hashlib.sha256(json.dumps(lead, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        ident = str(uuid.uuid5(NAMESPACE, f"team-leads:{department_id}:{row['id']}"))
        who = [{'field': field, 'op': 'any', 'values': sorted(lead[field])}
               for field in ('category_ids', 'capability_ids') if lead[field]]
        if lead.get('conditions'):
            who.append(strip_counts(lead['conditions']))
        numbers = {'min': {'value': lead['minimum'], 'level': 'mandatory'}}
        if lead['maximum'] is not None:
            numbers['max'] = {'value': lead['maximum'], 'level': 'mandatory'}
        lines.append({'id': ident, 'who': {'match': 'all', 'conditions': who} if who else None,
                      'where': {'match': 'all', 'conditions': [
                          {'field': 'assignment_type_ids', 'op': 'any', 'values': [row['id']]}]},
                      'starts_on': effective_on, 'numbers': numbers,
                      'settings': {'team_lead_requirement': {'continuity': 'continuous'}}})
        sources.append({'type_id': row['id'], 'type_revision': row['revision'], 'lead_hash': digest,
                        'source_history_id': history.get((row['id'], row['revision'])), 'destination_line_id': ident})
    payload = {'effective_on': effective_on, 'sources': sources}
    manifest_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'lines': lines, 'legacy_lead_conversion': {**payload, 'source_manifest_hash': manifest_hash} if sources else None,
            'normalizations': ['Count annotations retain Boolean qualification semantics'],
            'differences': [], 'equivalent': True}
