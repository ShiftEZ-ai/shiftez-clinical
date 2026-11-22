"""The one resolver: a catalogue rule as the old-shaped rule copies every check reads.

`expand(policy)` turns each line into one copy per slot it writes: numbers of a line that write the same old kind and
fixed settings at the same level are one copy (a run's shortest and longest run are one check); a quota's minimum,
maximum and target are three, as a range always was. A copy keeps the rule's id, carries the old kind in `kind`, the
line in `template_line = {id, line, slot}`, and is built through the old schemas, so every safety validation applies.

Most specific wins only between lines with the same where (clinics, places, duty types, days, hours) and settings
whose dates overlap: a person's line (tier 3) › a rank, programme, tag, skill, rotation or cohort line (2) › everyone
(1). The losing line's copies carry the internal condition `outranked_by` (policy-namespaced source line ids), which
`conditions.rule_holds` evaluates at the moment; a quota line a person's own line outranks leaves that person out for
the whole block, as quota lines always did. Safety entries never outrank: every matching line applies.
"""
import json
import uuid
from copy import deepcopy
from functools import lru_cache

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from packages.contracts.rule_catalogue import _MODELS, CATALOGUE_BY_ID, NUMBER_ORDER, catalogue_entry, slot_for_number as _slot
from packages.contracts.schemas import NEVER_OVERRIDDEN, SAFETY_STRICTNESS, PolicyDefinition, RuleLine, _canonical_tree, safety_rule
from .conditions import INHERITED_LEVEL, OUTRANKED, absolute_values

LEVELS = ('off', 'low', 'medium', 'high', 'critical', 'mandatory')
# These values change how a line is applied, not the quantity it counts.  Keeping them
# out of the measure lets the normal precedence resolver select one line's dials.
FAIR_SHARE_DIALS = frozenset({'share_adjustment', 'tolerance_percent', 'tolerance_units',
                              'weight', 'optimize_greedy'})
SCORING_DEFAULT_SOURCE = 'C2 built-in defaults (owner approved 2026-10-15)'


def read_quality_for(value):
    """Use a snapshot's sidecar read store, or a private store for this read."""
    quality = getattr(value, 'read_quality', None)
    return quality if quality is not None else {}


def department_catalogue_version(policies, *, default_actor_id=None, lineage=()):
    """Legacy catalogues require conversion; verified default replacement lineages stay exempt."""
    policies = list(policies)
    by_id = {p['id']: p for p in [*lineage, *policies]}

    def seeded_default(policy):
        seen = set()
        while policy['id'] not in seen:
            seen.add(policy['id'])
            if (policy.get('source') == SCORING_DEFAULT_SOURCE and default_actor_id
                    and policy.get('created_by') == default_actor_id == policy.get('approved_by')):
                return True
            definition = policy['definition']
            previous = by_id.get(definition.get('replaces_id'))
            if (policy['kind'] not in {'balance_goal', 'block_quota'} or not policy.get('approved_by')
                    or definition.get('severity') != 'advisory' or definition.get('merged_ids')
                    or not previous or previous['kind'] != policy['kind']
                    or previous.get('department_id') != policy.get('department_id')
                    or previous['definition'].get('severity') != 'advisory'):
                return False
            policy = previous
        return False

    return 1 if any(
        p.get('status') == 'active'
        and (p['kind'] != 'catalogue' or p['definition']['rule'].get('catalogue_version', 1) == 1)
        and not (p['kind'] != 'catalogue' and seeded_default(p))
        for p in policies) else 2


def scoring_default_id(department_id, key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'fmuc:{department_id}:scoring-default:{key}'))


def _entry(rule):
    return catalogue_entry(rule['id'], rule.get('catalogue_version', 1))


def fact_policy(row):
    """The same catalogue resolver reads independently approved person/calendar data."""
    data = row.get('data') or row
    line = deepcopy(data['line'])
    subjects = [('person_ids', row.get('person_id')), ('programme_ids', data.get('programme_id'))]
    extra = [{'field': field, 'op': 'any', 'values': [value]} for field, value in subjects if value]
    if extra:
        line['who'] = {'match': 'all', 'conditions': ([line['who']] if line.get('who') else []) + extra}
    catalogue_id = data['catalogue_id']
    version = data.get('catalogue_version', 2 if catalogue_entry(catalogue_id, 2) else 1)
    return {'id': row['id'], 'revision': row.get('revision', 1), 'department_id': row.get('department_id'),
            **({'_protected_interval': data['interval'], 'event_occurrence_id': row['event_occurrence_id']}
               if row.get('kind') == 'protected_time' and data.get('interval') else {}),
            '_v2_source': True,
            **({'_overtime_fact': True, '_overtime_request': bool(row.get('request_id')),
                '_overtime_source': data.get('offer_source'), '_overtime_dates': data.get('offer_dates')}
               if row.get('kind') == 'overtime_offer' else {}),
            'name': row.get('name') or row.get('note') or _entry({'id': catalogue_id, 'catalogue_version': version})['name'],
            'status': row.get('status', 'active'), 'active': row.get('on', True),
            'kind': 'catalogue', 'starts_on': row.get('starts_on') or line.get('starts_on'),
            'ends_on': row.get('ends_on') or line.get('ends_on'),
            'definition': {'rule': {'kind': 'catalogue', 'id': catalogue_id, 'catalogue_version': version,
                                    'on': row.get('on', True), 'settings': {}, 'lines': [line]},
                           'exception_allowed': bool(data.get('exception_allowed', False))}}


def _line_identity(line, *, person=None):
    """Canonical identity for one obligation line; values and scope, never provenance, decide equality."""
    who = line.get('who')
    if person and not who:
        who = {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    return json.dumps([
        _canonical_tree(who),
        _canonical_tree(line.get('where')),
        sorted(line.get('weekdays') or range(7)),
        line.get('start_time') or '00:00', line.get('end_time') or '24:00',
        line.get('starts_on'), line.get('ends_on'),
        json.dumps(line.get('settings') or {}, sort_keys=True, default=str),
        sorted((key, (float(number.get('value')) if number.get('value') is not None else None),
                number.get('level') or 'mandatory')
               for key, number in (line.get('numbers') or {}).items()),
    ], sort_keys=True, default=str)


def _carrier_lines(policy):
    rule = (policy.get('definition') or {}).get('rule') or {}
    if rule.get('kind') == 'catalogue':
        return rule.get('lines') or []
    from .rule_catalogue_migration import Kept, lines_of
    try:
        return lines_of(policy)[1]
    except (Kept, KeyError, TypeError, ValueError):
        return []


def _has_identical_person_line(policy, fact):
    person = fact.get('person_id')
    if not person:
        return False
    fact_line = deepcopy((fact.get('data') or {}).get('line') or {})
    if person not in named_people(fact_line):
        fact_line = fact_policy(fact)['definition']['rule']['lines'][0]
    return any((line.get('who') is None or person in named_people(line))
               and _line_identity(line, person=person) == _line_identity(fact_line, person=person)
               for line in _carrier_lines(policy))


def _safety_number(data):
    line = data.get('line') or {}
    entry = catalogue_entry(data.get('catalogue_id'), data.get('catalogue_version', 1))
    if not entry:
        return False
    rule = {'id': data['catalogue_id'], 'catalogue_version': data.get('catalogue_version', 1)}
    return any(_slot(entry, key, rule, line, list(line.get('numbers') or {}))['kind'] in NEVER_OVERRIDDEN
               for key in line.get('numbers') or {})


def _safety_fact(data):
    return _safety_number(data) or any(binding.get('primitive_kind') in NEVER_OVERRIDDEN
                                       for binding in ((data.get('line') or {}).get('source_bindings') or {}).values())


def _native_facts(policies, facts):
    """Personal Fair-share amounts amend their exact approved goal, never create another goal."""
    policies = list(policies)
    positions = {p['id']: n for n, p in enumerate(policies)}
    source_all = {}
    source_history = set()

    for fact in facts:
        data = fact.get('data') or {}
        pins = data.get('line', {}).get('source_bindings') or {}
        source = next(iter(pins.values()), None)
        if (source and source.get('policy_id') in positions
                and not (fact.get('kind') == 'target' and data.get('catalogue_id') == 'fair_share')):
            if fact.get('status', 'active') != 'active' or not fact.get('on', True):
                continue
            source_id = source['policy_id']
            if safety_rule(policies[positions[source_id]]['definition']):
                if not _has_identical_person_line(policies[positions[source_id]], fact):
                    policies.append(fact_policy(fact))
                continue
            source_history.add(source_id)
            source_all.setdefault(source_id, []).append(fact)
            continue
        if fact.get('status', 'active') != 'active' or not fact.get('on', True):
            continue
        goal = data.get('goal_ref')
        if fact['kind'] != 'target' or data['catalogue_id'] != 'fair_share' or not goal:
            policies.append(fact_policy(fact))
            continue
        position = positions.get(goal['policy_id'])
        if position is None:
            raise ValueError('Personal Fair-share target has no active source goal')
        policy = deepcopy(policies[position])
        if policy.get('revision', 1) != goal['revision']:
            raise ValueError('Personal Fair-share target source revision is stale')
        d, rule = policy['definition'], policy['definition']['rule']
        number = (data['line'].get('numbers') or {}).get('target')
        if not number:
            continue
        if rule['kind'] == 'catalogue':
            matches = [line for line in rule.get('lines') or []
                       if line.get('_source_identity', line['id']) == goal.get('line_id')]
            if len(matches) != 1:
                raise ValueError('Personal Fair-share target needs one exact source line')
            [line] = matches
            body = line.setdefault('settings', {}).setdefault('balance_goal', {})
            level = (line['numbers'].get('target') or line['numbers'].get('apply') or {}).get('level', 'medium')
        elif rule['kind'] == 'balance_goal' and not goal.get('line_id'):
            body = rule
            level = d.get('strength') or 'medium'
        else:
            raise ValueError('Personal target reference is not its original Fair-share goal')
        if number.get('level') not in (None, level):
            raise ValueError('A personal Fair-share amount inherits its source goal strength')
        amounts = dict(body.get('person_amounts') or {})
        if number.get('value') is None:
            amounts.pop(fact['person_id'], None)
        else:
            amounts[fact['person_id']] = number['value']
        body['person_amounts'] = amounts
        policy['_v2_source'] = True
        policies[position] = policy
    if not source_history:
        return policies
    # A projection can arrive before its conversion binding rows. Reuse the immutable
    # fact lines under the source policy id so independent bounds are not replaced by
    # the stale legacy carrier. The binding-backed path remains authoritative when present.
    # Rewrites: base scope/projection-only marker (184-197), identical lines merged
    # into one person_ids=any line (199-234), default facts narrowed with
    # person_ids=none (242-248), and replacements spliced under source id (255-257).
    for source_id in source_history:
        position = positions[source_id]
        base = policies[position]
        all_facts = source_all[source_id]
        has_default = any(next(iter((fact.get('data') or {}).get('line', {}).get('source_bindings', {}).values()), {}).get('line_index') is None
                          for fact in all_facts)
        listed = bool((base.get('definition') or {}).get('listed_only'))
        if has_default or listed:
            policies[position] = {**base, '_v2_source_projection_only': True}
        else:
            row_people = sorted({fact['person_id'] for fact in all_facts if fact.get('person_id')})
            definition = deepcopy(base['definition'])
            definition['per_person'] = []
            scope = definition['scope']
            if row_people:
                exclusion = {'field': 'person_ids', 'op': 'none', 'values': row_people}
                existing = scope.get('conditions')
                if existing and existing.get('match', 'all') == 'all':
                    scope['conditions'] = {**existing, 'conditions': [*(existing.get('conditions') or []), exclusion]}
                else:
                    scope['conditions'] = {'match': 'all', 'conditions': ([existing] if existing else []) + [exclusion]}
            policies[position] = {**base, 'definition': {**definition, 'scope': scope}}
        replacements = []
        grouped = {}
        for fact in source_all.get(source_id, ()):
            line = (fact.get('data') or {}).get('line') or {}
            key = json.dumps({k: v for k, v in line.items() if k not in ('id', 'who', 'source_bindings')},
                             sort_keys=True, default=str)
            grouped.setdefault(key, []).append(fact)
        facts_for_source = []
        for group in grouped.values():
            if len(group) == 1:
                facts_for_source.extend(group)
                continue
            merged = deepcopy(group[0])
            line = merged.setdefault('data', {}).setdefault('line', {})
            people = sorted({fact['person_id'] for fact in group if fact.get('person_id')})
            who = deepcopy(line.get('who') or {'match': 'all', 'conditions': []})
            found = False

            def merge_people(tree):
                nonlocal found
                conditions = []
                for condition in tree.get('conditions') or []:
                    if 'conditions' in condition:
                        conditions.append(merge_people(condition))
                    elif condition.get('field') == 'person_ids' and condition.get('op', 'any') in {'any', 'is'}:
                        found = True
                        conditions.append({'field': 'person_ids', 'op': 'any', 'values': people})
                    else:
                        conditions.append(condition)
                return {**tree, 'conditions': conditions}

            who = merge_people(who)
            if not found:
                who.setdefault('conditions', []).append({'field': 'person_ids', 'op': 'any', 'values': people})
            line['who'] = who
            merged['person_id'] = None
            facts_for_source.append(merged)
        row_people = sorted({fact['person_id'] for fact in all_facts
                             if next(iter((fact.get('data') or {}).get('line', {}).get('source_bindings', {}).values()), {}).get('line_index') is not None
                             and fact.get('person_id') and not _safety_fact(fact.get('data') or {})})
        for fact in facts_for_source:
            pin = next(iter((fact.get('data') or {}).get('line', {}).get('source_bindings', {}).values()), None)
            index = pin.get('line_index') if pin else None
            fact_for_policy = fact
            if index is None and row_people and not _safety_fact(fact.get('data') or {}):
                fact_for_policy = deepcopy(fact)
                line = fact_for_policy.setdefault('data', {}).setdefault('line', {})
                who = line.get('who')
                held = list((who or {}).get('conditions') or [])
                held.append({'field': 'person_ids', 'op': 'none', 'values': row_people})
                line['who'] = {**(who or {'match': 'all'}), 'conditions': held}
            replacement = fact_policy(fact_for_policy)
            rows = (base.get('definition') or {}).get('per_person') or []
            if index is not None and 0 <= index < len(rows):
                replacement['_v2_template_line'] = {**deepcopy(rows[index]), 'line': index}
            else:
                replacement['_v2_template_line'] = None
            replacements.append({**replacement, 'id': source_id, 'revision': base.get('revision', 1),
                                 'name': base.get('name') or replacement.get('name')})
        policies[position + 1:position + 1] = replacements
        shift = len(replacements)
        if shift:
            positions = {key: value + shift if value > position else value for key, value in positions.items()}
    return policies


def seeded_policies(policies, rows):
    """Append immutable migration defaults to their exact policy version without rewriting it."""
    by_policy = {}
    for row in rows:
        by_policy.setdefault(row['policy_id'], []).append(row)
    result = []
    for policy in policies:
        extra = [row['line'] for row in by_policy.get(policy['id'], [])
                 if row['policy_revision'] == policy.get('revision', 1)]
        rule = policy['definition']['rule']
        if extra and rule['kind'] == 'catalogue':
            existing = {line['id'] for line in rule.get('lines', [])}
            lines = rule.get('lines', []) + [line for line in extra if line['id'] not in existing]
            policy = {**policy, 'definition': {**policy['definition'], 'rule': {**rule, 'lines': lines}}}
        result.append(policy)
    return result


def _fair_share_read_quality(read_quality, findings, policies, completed, context=None):
    """Record read-only Fair-share data quality without changing stored rows."""
    if read_quality is None:
        return
    if context is not None and context.get('_catalogue_v2_materialized'):
        # Omitted goals cannot reproduce their captured warnings from the materialized policy list.
        current = {finding['rule_id'] for finding in findings}
        materialized = {policy['id'] for policy in policies}
        previous_output = read_quality.get('_fair_share_goal_completed_ids')
        if previous_output is not None and materialized == set(previous_output):
            findings = [finding for finding in read_quality.get('_fair_share_goal_findings', ())
                        if finding['rule_id'] not in current] + findings
    policy_ids = sorted({finding['rule_id'] for finding in findings})
    read_quality['_fair_share_goal_findings'] = findings
    read_quality['_fair_share_goal_completed_ids'] = sorted({policy['id'] for policy in completed})
    read_quality['_fair_share_goal_read_quality'] = {
        'count': len(policy_ids), 'policy_ids': policy_ids,
        'message': f"{len(policy_ids)} rules stored incomplete",
    }


def _complete_fair_share_goals(policies, read_quality, context=None):
    """Complete stored Fair-share goals from their own source data, or omit them read-only."""
    # shortcut: read-time completion of balance_goal.metric for rows from maintenance's conversion; remove with the fill-only backfill
    findings = []
    completed = []

    def goal_settings(policy, line=None):
        try:
            # Malformed stored goals are reported and omitted; raw precision stays for the solver's hard guard.
            goal = fair_share_goal_settings(policy['definition']['rule'], line, stored_goal=True)
        except ValidationError as error:
            issue = error.errors(include_input=False, include_url=False)[0]
            problem = ': '.join(filter(None, ('.'.join(map(str, issue['loc'])), issue['msg'])))
            findings.append({'code': 'FAIR_SHARE_GOAL_INVALID', 'severity': 'warning', 'rule_id': policy['id'],
                'message': f"Fair-share target is invalid: {problem}; open the rule ({policy['id']})",
                'detail': {'stored_invalid': True, 'policy_id': policy['id'], 'problem': problem}})
            return None
        if goal is None:
            findings.append({'code': 'FAIR_SHARE_GOAL_INCOMPLETE', 'severity': 'warning',
                             'rule_id': policy['id'],
                             'message': f"Fair-share target has no measure; open the rule ({policy['id']})",
                             'detail': {'stored_incomplete': True, 'policy_id': policy['id']}})
        return goal

    for policy in policies:
        rule = policy.get('definition', {}).get('rule', {})
        if rule.get('kind') == 'balance_goal':
            if goal_settings(policy) is not None:
                completed.append(policy)
            continue
        if rule.get('kind') != 'catalogue' or rule.get('id') != 'fair_share':
            completed.append(policy)
            continue
        lines = []
        changed = False
        for line in rule.get('lines') or []:
            goal = goal_settings(policy, line)
            if goal is None:
                continue
            existing = (line.get('settings') or {}).get('balance_goal') or {}
            if existing.get('metric') is None:
                changed = True
                lines.append({**line, 'settings': {**(line.get('settings') or {}), 'balance_goal': goal}})
            else:
                lines.append(line)
        if lines:
            if not changed and len(lines) == len(rule.get('lines') or []):
                completed.append(policy)
            else:
                completed.append({**policy, 'definition': {**policy['definition'],
                    'rule': {**rule, 'lines': lines}}})
    _fair_share_read_quality(read_quality, findings, policies, completed, context)
    return completed


def bound_policies(policies, context=None, *, read_quality=None):
    read_quality = read_quality if read_quality is not None else read_quality_for(context)
    result = _bound_policies(policies, context)
    from .fairness import legacy_semantics
    if context is None:
        return _complete_fair_share_goals(result, read_quality)
    if not (legacy_semantics(context) or context.get('fairness_default_window')):
        return _complete_fair_share_goals(result, read_quality, context)
    # A frozen publication may have omitted defaults. Materialize its captured or legacy defaults
    # in the read projection only; issued rows and evidence remain byte-for-byte unchanged.
    frozen = []
    for policy in result:
        rule = policy['definition']['rule']
        if not legacy_semantics(context):
            from packages.contracts.schemas import BalanceGoal
            window = context['fairness_default_window']
            if rule['kind'] == 'balance_goal':
                rule = BalanceGoal.measure_defaults(rule, window=window)
            elif rule['kind'] == 'catalogue' and rule.get('id') == 'fair_share':
                parent = (rule.get('settings') or {}).get('balance_goal', {})
                rule = {**rule, 'lines': [{**line, 'settings': {**(line.get('settings') or {}),
                    'balance_goal': BalanceGoal.measure_defaults(
                        {**parent, **(line.get('settings') or {}).get('balance_goal', {})}, window=window)}}
                    for line in rule.get('lines', [])]}
            frozen.append({**policy, 'definition': {**policy['definition'], 'rule': rule}})
            continue
        if rule['kind'] == 'catalogue' and rule.get('id') == 'fair_share':
            settings = rule.get('settings') or {}
            goal = {'period': 'rota', 'share_by': 'available_days', **settings.get('balance_goal', {})}
            rule = {**rule, 'settings': {**settings, 'balance_goal': goal}}
            policy = {**policy, 'definition': {**policy['definition'], 'rule': rule}}
        frozen.append(policy)
    return _complete_fair_share_goals(frozen, read_quality, context)


def safety_display_key(policy):
    """Show the contract's strictest numeric safety obligation first; retain every copy."""
    rule = policy['definition']['rule']
    limits = SAFETY_STRICTNESS.get(rule['kind'], {})
    return (not bool(limits), rule['kind'] if limits else '', tuple(
        (rule.get(field) is None, -(rule.get(field) or 0) if direction == 'max' else rule.get(field) or 0)
        for field, direction in limits.items()))


def catalogue_display_policies(policies, context):
    """Join reconstructed safety lines to their edited catalogue row for rule reads."""
    from packages.contracts.rule_catalogue import CATALOGUE_MAPPING
    policies = list(policies)
    by_id = {policy['id']: policy for policy in policies}
    catalogue = {policy['definition']['rule'].get('id'): policy for policy in policies
                 if policy['definition']['rule']['kind'] == 'catalogue'
                 and policy.get('status') == 'active' and policy.get('active', True)}
    merged, hidden, source_ids = {}, set(), {}
    for row in context.get('source_bindings') or []:
        binding = row.get('data', row)
        source = by_id.get(binding['source_policy_id'])
        target = catalogue.get(CATALOGUE_MAPPING.get(binding.get('source_catalogue_id')))
        if not source or not target or source['definition']['rule']['kind'] not in NEVER_OVERRIDDEN:
            continue
        lines = source.get('_v2_source_lines') or {}
        for destination in binding['destinations']:
            if destination['kind'] != 'policy':
                continue
            index = destination['source_line_index']
            line = lines.get(index, lines.get(str(index)))
            if line is not None:
                merged.setdefault(target['id'], {})[destination['line_id']] = {
                    **line, 'id': destination['line_id']}
                source_ids.setdefault(target['id'], set()).add(source['id'])
                hidden.add(source['id'])
    result = []
    for policy in policies:
        if policy['id'] in hidden:
            continue
        if policy['id'] in merged:
            rule = policy['definition']['rule']
            lines = merged[policy['id']]
            lines.update({line['id']: line for line in rule.get('lines') or []})
            policy = {**policy, '_display_source_ids': sorted(source_ids[policy['id']]),
                      'definition': {**policy['definition'], 'rule': {**rule, 'lines': list(lines.values())}}}
        result.append(policy)
    return result


def _bound_policies(policies, context=None):
    """Reassemble approved source partitions from their current destinations, never retired policy definitions."""
    policies = seeded_policies(policies, (context or {}).get('policy_seed_lines', []))
    if not context or context.get('_catalogue_v2_materialized') or any(p.get('_v2_source') for p in policies):
        return list(policies)
    from .rule_catalogue_migration import source_policy
    bindings = [b.get('data', b) for b in context.get('source_bindings') or []
                if b.get('status', 'active') == 'active' and b.get('active', True)]
    facts = context.get('scheduling_facts') or []
    if not bindings:
        return _native_facts(policies, facts)

    def versions(rows, fact=False):
        by_id, successors = {r['id']: r for r in rows}, {}
        for row in rows:
            for previous in ([row.get('replaces_id')] if fact else
                             [row.get('replaces_id'), row.get('definition', {}).get('replaces_id'),
                              *(row.get('definition', {}).get('merged_ids') or [])]):
                if previous:
                    successors.setdefault(previous, []).append(row['id'])

        def current(key):
            seen, found, queue = set(), [], [key]
            while queue:
                item = queue.pop()
                if item in seen:
                    continue
                seen.add(item)
                row = by_id.get(item)
                enabled = row and row.get('status', 'active') == 'active' and (row.get('on', True) if fact else True)
                if enabled:
                    found.append(row)
                else:
                    queue.extend(successors.get(item, ()))
            if len(found) > 1:
                raise ValueError('Ambiguous catalogue source replacement')
            return found[0] if found else None
        deltas = {}
        def delta(key):
            if key in deltas:
                return deltas[key]
            visited, path, total, queue = set(), set(), 0, [(key, False)]
            while queue:
                item, leaving = queue.pop()
                if leaving:
                    path.remove(item)
                    visited.add(item)
                    continue
                if item in path:
                    raise ValueError('Cyclic catalogue source replacement')
                if item in visited:
                    continue
                path.add(item)
                queue.append((item, True))
                row = by_id.get(item)
                if row and (row.get('status', 'active') == 'active' or
                            row.get('status') == 'retired' and (item == key or row.get('approved_by'))):
                    total += max(0, row.get('revision', 1) - 1)
                queue.extend((successor, False) for successor in successors.get(item, ()))
            deltas[key] = total
            return total
        current.delta = delta
        return current

    policy_version, fact_version = versions(policies), versions(facts, True)
    used_lines, used_facts, sources, line_indexes = {}, set(), [], {}
    for binding in sorted(bindings, key=lambda b: b['source_order']):
        destinations = []
        for destination in binding['destinations']:
            row = (fact_version if destination['kind'] == 'fact' else policy_version)(destination['id'])
            if row is None:
                continue
            if destination['kind'] == 'fact':
                if (_safety_fact(row.get('data') or {})
                        and (row.get('kind') != 'protected_time'
                             or any(item['kind'] == 'policy' for item in binding['destinations']))):
                    # A policy destination or non-calendar safety fact remains independently visible.
                    continue
                used_facts.add(row['id'])
                line = row['data']['line']
            else:
                if row['id'] not in line_indexes:
                    line_indexes[row['id']] = {line['id']: line for line in row['definition']['rule'].get('lines') or []}
                line = line_indexes[row['id']].get(destination['line_id'])
                if line is None:
                    continue
                rule = row['definition']['rule']
                line = {**line, 'settings': _settings(rule, line)}
                if line.get('source_default'):
                    default = line['source_default']
                    line['source_default'] = {**default, 'settings': _settings(rule, default)}
                if not row.get('active', True) or not row['definition']['rule'].get('on', True):
                    line = {**line, '_source_default_only': True}
                used_lines.setdefault(row['id'], set()).add(line['id'])
            destinations.append((destination, line))
        if destinations or not binding['destinations']:
            source = source_policy(binding, destinations)
            if source is not None:
                roots = {(d['kind'], d['id']) for d in binding['destinations']}
                source['revision'] = binding['source_revision'] + sum(
                    (fact_version if kind == 'fact' else policy_version).delta(root) for kind, root in roots)
                sources.append(source)
    remaining = []
    old_ids = {b['source_policy_id'] for b in bindings}
    for policy in policies:
        if policy['id'] in old_ids:
            continue
        if policy['id'] in used_lines:
            rule = policy['definition']['rule']
            lines = [line for line in rule.get('lines') or [] if line['id'] not in used_lines[policy['id']]]
            if lines:
                remaining.append({**policy, 'definition': {**policy['definition'], 'rule': {**rule, 'lines': lines}}})
        else:
            remaining.append(policy)
    native = [f for f in facts if f['id'] not in used_facts and (
        not any((f.get('data', {}).get('line', {}).get('source_bindings') or {}).values())
        or _safety_fact(f.get('data') or {}))]
    # With binding rows, source-bound non-safety facts are consumed above; safety facts stay native.
    return _native_facts([*sources, *remaining], native)


def source_edits(copies):
    """Apply approved destination scopes and per-number strengths to legacy-shaped source copies."""
    from .conditions import INTERNAL, _parts, effective_severity
    partitioned = []
    for copy in copies:
        variants = copy.get('_v2_base_variants') if not copy.get('template_line') else None
        if not variants:
            partitioned.append(copy)
            continue
        for variant in variants:
            line = variant['line']
            if not any(number.get('value') is not None or key == 'apply'
                       for key, number in (line.get('numbers') or {}).items()):
                continue
            # Keep the default copy's exclusions; only its approved subjects and values fan out.
            partitioned.append({**copy,
                'definition': {**copy['definition'], 'rule': deepcopy(variant['rule'])},
                '_v2_source_lines': {**copy['_v2_source_lines'], -1: line}, '_v2_source_default': line,
                '_v2_changed_scope': [*copy.get('_v2_changed_scope', ()), -1],
                '_v2_changed_dates': [*copy.get('_v2_changed_dates', ()), -1],
                '_v2_setting_edits': {n: edit for n, edit in (copy.get('_v2_setting_edits') or {}).items()
                                      if n not in (-1, '-1')}})
    out = []
    for copy in _parts(partitioned):
        lines = copy.get('_v2_source_lines')
        if lines is None:
            out.append(copy)
            continue
        n = (copy.get('template_line') or {}).get('line', -1)
        original_n = (copy.get('_v2_row_indices') or [])[n] if n >= 0 and copy.get('_v2_row_indices') else n
        line = lines.get(n, lines.get(str(n)))
        if line is None:
            continue
        if line.get('_source_default_only'):
            continue
        kind = copy['definition']['rule']['kind']
        pins = line.get('source_bindings') or (copy.get('_v2_source_default') or {}).get('source_bindings') or {}
        keys = [key for key, pin in pins.items() if pin['primitive_kind'] == kind]
        if kind == 'block_quota':
            comparison = copy['definition']['rule']['comparison']
            keys = [{'minimum': 'min', 'maximum': 'max', 'target': 'target'}[comparison]]
        numbers = {**(copy.get('_v2_source_default') or {}).get('numbers', {}), **(line.get('numbers') or {})}
        if kind == 'block_quota':
            number = numbers.get(keys[0])
            if n >= 0 and copy.get('_v2_source_default') and (not number or number.get('value') is None):
                number = copy['_v2_source_default'].get('numbers', {}).get(keys[0])
            if not number or number.get('value') is None:
                continue
        levels = {numbers[key].get('level') for key in keys if key in numbers and numbers[key].get('level') is not None}
        original = 'mandatory' if copy['definition']['severity'] == 'mandatory' else copy['definition'].get('strength') or 'medium'
        levels = levels or {original}
        # A converted cover target retains its explicit advisory-only slot through legacy-shaped projection.
        advisory_slot = (keys == ['target'] and kind == 'minimum_cover')
        levels = {effective_severity(kind, level, advisory_slot=advisory_slot) for level in levels}
        for level in LEVELS:
            if level not in levels:
                continue
            item = {k: v for k, v in copy.items() if k not in ('_v2_source_lines', '_v2_source_default', '_v2_changed_scope', '_v2_changed_dates', '_v2_row_indices', '_v2_row_templates', '_v2_setting_edits', '_v2_validation_definition', '_v2_base_variants', '_v2_base_exclusions')}
            if copy.get('template_line'):
                item['template_line'] = {**copy['template_line'], 'line': original_n}
            d = item['definition']
            edits = copy.get('_v2_setting_edits') or {}
            changed = edits.get(n, edits.get(str(n)))
            if changed:
                d = _edited_copy(d, {**d['rule'], **changed})
            if level != original:
                d = {**d, 'severity': 'mandatory' if level == 'mandatory' else 'advisory',
                     'strength': 'medium' if level == 'mandatory' else level}
            if len(levels) > 1:
                body = dict(d['rule'])
                entry = _entry({'id': next(iter(pins.values()))['catalogue_id']})
                for key in keys:
                    if key in numbers and numbers[key].get('level') != level:
                        slot = _slot(entry, key, {}, line, keys)
                        if slot['field']:
                            body[slot['field']] = None
                d = {**d, 'rule': body}
            if n in copy.get('_v2_changed_scope', ()):
                def internals(tree):
                    return [leaf for child in tree.get('conditions') or []
                            for leaf in (internals(child) if 'conditions' in child else [child] if child.get('field') in INTERNAL else [])]
                held = internals(d['scope'].get('conditions') or {})
                held += [leaf for leaf in copy.get('_v2_base_exclusions', ()) if leaf.get('field') not in INTERNAL]
                scope = scope_of(line)
                if held:
                    scope['conditions'] = {'match': 'all', 'conditions': ([scope['conditions']] if scope.get('conditions') else []) + held}
                d = {**d, 'scope': scope}
                item.update(starts_on=line.get('starts_on'), ends_on=line.get('ends_on'))
            if kind == 'block_quota' and n in copy.get('_v2_changed_dates', ()):
                body = dict(d['rule'])
                body['starts_on'] = _later(body['starts_on'], line.get('starts_on'))
                body['ends_on'] = _earlier(body['ends_on'], line.get('ends_on'))
                if body.get('ends_on') and str(body['starts_on']) >= str(body['ends_on']):
                    continue
                d = _edited_copy(d, body)
            if kind == 'block_quota':
                # These copies are already split into bounds/targets; schema normalization can re-add empty fields.
                d = {**d, 'rule': {k: v for k, v in d['rule'].items() if k not in ('target_amount', 'target_strength')}}
            item['definition'] = d
            if advisory_slot:
                item['_advisory_catalogue_slot'] = True
            out.append(item)
    return out


def _edited_copy(definition, rule):
    # Resolved listed-only copies have no authoring rows; their narrowed scope already selects the row.
    scope = definition['scope']
    # Quota scopes are checked before resolution; a generated person intersection can exceed authoring group depth.
    checked_scope = {**scope, 'conditions': None} if rule['kind'] == 'block_quota' else scope
    valid = _valid(json.dumps({**definition, 'listed_only': False, 'scope': checked_scope, 'rule': rule}, sort_keys=True, default=str))
    return {**valid, 'scope': scope, 'listed_only': definition.get('listed_only', False)}

# A top-level "any of" line an old flat scope field can hold is written there (as the old rules and their lines did):
# lists, and single values. Applicability and qualification share dated evaluation in conditions.py.
FLAT_MANY = {'person_ids': 500, 'category_ids': 100, 'service_ids': 32}
FLAT_ONE = {'programme_ids': 'programme_id', 'rotation_type_ids': 'rotation_type_id', 'location_ids': 'location_id',
            'assignment_type_ids': 'assignment_type_id'}


def tier(line):
    """How specific a line's who is: a person 3, any other who 2, everyone 1."""
    if '_source_tier' in line:
        return line['_source_tier']  # Rota projections retain the binding line's precedence.
    from .validation import named_people as scope_people
    who = line.get('who')
    if not who or not who.get('conditions'):
        return 1
    if line.get('who_group'):  # people named by a tag (conditions.with_tags)
        return 2
    return 3 if scope_people({'conditions': who}) else 2


def named_people(line):
    """The people required by a person line, using the shared scope shortcut."""
    from .validation import named_people as scope_people
    return sorted(scope_people({'conditions': line.get('who')}) or ())


def unconditional_people(line):
    """Only an unconditional person list replaces that person's entire quota block."""
    who = line.get('who') or {}
    while len(who.get('conditions') or []) == 1 and 'conditions' in who['conditions'][0]:
        who = who['conditions'][0]
    conditions = who.get('conditions') or []
    return (not line.get('who_group') and len(conditions) == 1 and conditions[0].get('field') == 'person_ids'
            and conditions[0].get('op', 'any') == 'any')


def scope_of(line):
    """A line's who, where, days and hours as an old rule scope."""
    scope = {'weekdays': list(line.get('weekdays') or range(7)), 'start_time': line.get('start_time') or '00:00',
             'end_time': line.get('end_time') or '24:00'}
    rest = []
    for tree in (line.get('who'), line.get('where')):
        if not tree or not tree.get('conditions'):
            continue
        if tree.get('count') == 'each' or tree.get('match', 'all') != 'all' and len(tree['conditions']) > 1:
            rest.append(tree)
            continue
        for c in tree['conditions']:
            field = c.get('field')
            if 'conditions' not in c and c.get('value') is None and c.get('count') != 'each' and c.get('op', 'any') == 'any':
                values = absolute_values(c)
                if values is not None and len(set(values)) == len(values):
                    if field in FLAT_MANY and not scope.get(field) and len(values) <= FLAT_MANY[field]:
                        scope[field] = list(values)
                        continue
                    if field in FLAT_ONE and not scope.get(FLAT_ONE[field]) and len(values) == 1:
                        scope[FLAT_ONE[field]] = values[0]
                        continue
            rest.append(c)
    if len(rest) == 1 and 'conditions' in rest[0]:
        scope['conditions'] = rest[0]
    elif rest:
        scope['conditions'] = {'match': 'all', 'conditions': rest}
    return scope


def _settings(rule, line):
    kinds = set(rule.get('settings') or {}) | set(line.get('settings') or {})
    return {k: {**(rule.get('settings') or {}).get(k, {}), **(line.get('settings') or {}).get(k, {})} for k in kinds}


def fair_share_goal_settings(rule, line=None, *, stored_goal=False):
    """Resolve a Fair-share goal from its parent and line, never from catalogue defaults."""
    line = line or {}
    if rule.get('kind') == 'balance_goal':
        settings = {key: value for key, value in rule.items() if key != 'kind'}
    elif rule.get('id') == 'fair_share':
        settings = dict((rule.get('settings') or {}).get('balance_goal') or {})
    else:
        return None
    settings.update((line.get('settings') or {}).get('balance_goal') or {})
    metric = settings.get('metric')
    if not metric:
        return None
    target = (line.get('numbers') or {}).get('target') or {}
    if settings.get('amount') is None and target.get('value') is not None:
        settings['amount'] = target['value']
    from packages.contracts.schemas import BalanceGoal
    resolved = BalanceGoal.model_validate(BalanceGoal.measure_defaults(
        {'kind': 'balance_goal', **settings}), context={'stored_goal': stored_goal}).model_dump(mode='json', exclude={'kind'})
    if target.get('value') is not None:
        resolved.pop('amount', None)  # the target number is the canonical amount on a line
    return resolved


def _later(a, b):
    return b if a is None or b is not None and str(b) > str(a) else a


def _earlier(a, b):
    return b if a is None or b is not None and str(b) < str(a) else a


def _overlap(x, y):
    start, end = _later(x[0], y[0]), _earlier(x[1], y[1])
    return start is None or end is None or str(start) < str(end)


def _spans(rule, lines, policy=None):
    """Comparable effective dates; a policy envelope cannot grant precedence to an inactive line."""
    out = []
    for line in lines:
        a, b = when(rule, line)
        if policy is not None:
            a, b = _later(policy.get('starts_on'), a), _earlier(policy.get('ends_on'), b)
        out.append(('' if a is None else str(a), '￿' if b is None else str(b)))
    return out


def _meets(x, y):
    return max(x[0], y[0]) < min(x[1], y[1])


def _pairs(members, spans):
    """(k, n), k before n in `members`, whose dates overlap: a sweep by start date, so members on dates that never meet
    are never compared (shortcut: members that all overlap still make every pair — that many pairs is the answer)."""
    at, active, out = {m: i for i, m in enumerate(members)}, [], []
    for m in sorted(members, key=lambda m: spans[m][0]):
        active = [a for a in active if spans[a][1] > spans[m][0]]  # ended before m starts: meets nothing later either
        out += [(a, m) if at[a] < at[m] else (m, a) for a in active if _meets(spans[a], spans[m])]
        active.append(m)
    return out


@lru_cache(maxsize=8192)
def _valid(text, stored_goal=True, *, resolved_catalogue_line=True):
    """A stored definition through the old schemas, including historical zero prohibitions."""
    definition = json.loads(text)
    rule = definition.get('rule') if isinstance(definition, dict) else None
    rule = rule if isinstance(rule, dict) else {}
    consecutive = ('max_consecutive_days', 'max_consecutive_weekends')
    legacy_zero = rule.get('kind') in consecutive and rule.get('maximum') == 0
    zero_lines = []
    if legacy_zero:
        rule['maximum'] = 1  # Stored zero prohibitions remain readable; authored consecutive limits stay positive.
    if rule.get('kind') == 'catalogue' and isinstance(rule.get('id'), str) and isinstance(rule.get('catalogue_version', 1), int):
        entry = _entry(rule)
        lines = rule.get('lines')
        if entry and 'max' in entry['numbers'] and isinstance(lines, list):
            for n, line in enumerate(lines):
                numbers = line.get('numbers') if isinstance(line, dict) else None
                maximum = numbers.get('max') if isinstance(numbers, dict) else None
                if not isinstance(maximum, dict) or maximum.get('value') != 0:
                    continue
                try:
                    slot = _slot(entry, 'max', rule, line, list(numbers))
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue  # The schema still reports malformed definitions below.
                if slot['kind'] in consecutive:
                    maximum['value'] = 1
                    zero_lines.append(n)
    valid = PolicyDefinition.model_validate(definition, context={
        'historical': True, 'resolved_catalogue_line': resolved_catalogue_line, 'stored_goal': stored_goal}).model_dump(mode='json')
    if legacy_zero:
        valid['rule']['maximum'] = 0
    for n in zero_lines:
        valid['rule']['lines'][n]['numbers']['max']['value'] = 0
    return valid


@lru_cache(maxsize=len(_MODELS))
def _defaults(kind):
    """The settings an old kind's schema writes when they are left out: a setting left out and its default are equal.
    Shared: never changed."""
    defaults = to_jsonable_python({f: i.get_default(call_default_factory=True) for f, i in _MODELS[kind].model_fields.items()
                               if not i.is_required()})
    if kind == 'allocation_overtime':
        defaults['volunteer_required'] = False
    return defaults


def resolved_settings(kind, configured):
    if kind == 'balance_goal':
        configured = _MODELS[kind].measure_defaults(configured)
    return {**_defaults(kind), **configured}


def _kinds(rule, line):
    entry = _entry(rule)
    keys = [k for k in (line.get('numbers') or {}) if k in entry['numbers']]
    return sorted({_slot(entry, k, rule, line, keys)['kind'] for k in keys})


def measure(rule, line, block=False):
    """What a line counts: the settings (the defaults, the rule's, then the line's) of the old kinds its numbers write —
    a window, a metric, overtime, participation... Two lines measure the same thing only when these are equal. A quota's
    block is when it counts, like the line's dates, so it is left out unless `block`."""
    entry, settings = _entry(rule), _settings(rule, line)
    out = {}
    for k in _kinds(rule, line):
        given = resolved_settings(k, settings.get(k, {}))
        out[k] = {f: given.get(f) for f in entry['line_settings'][k]
                  if not (k == 'balance_goal' and f in FAIR_SHARE_DIALS)
                  and (block or k != 'block_quota' or f not in ('starts_on', 'ends_on'))}
    return out


def when(rule, line):
    """(starts_on, ends_on) a line counts in: its dates, within its block when it is a quota."""
    start, end = line.get('starts_on'), line.get('ends_on')
    if 'block_quota' in _kinds(rule, line):
        block = _settings(rule, line).get('block_quota', {})
        start, end = _later(start, block.get('starts_on')), _earlier(end, block.get('ends_on'))
    return start, end


def line_validity(policy, line, day):
    """Counting validity within its source row; reach and lifecycle authority are separate facts."""
    start, end = when(policy['definition']['rule'], line)
    start, end = _later(start, policy.get('starts_on')), _earlier(end, policy.get('ends_on'))
    effective = 'ended' if end and str(end) <= str(day) else 'future' if start and str(start) > str(day) else 'active'
    return {'effective': effective, 'starts_on': start, 'ends_on': end}


def _where(rule, line):
    return [_canonical_tree(line.get('where')), sorted(line.get('weekdays') or range(7)), line.get('start_time') or '00:00',
            line.get('end_time') or '24:00', measure(rule, line)]


def duplicates(rule):
    """[(k, n)]: line indexes that are the same line — the same measure and numbers (which ones), who, where, days and
    hours, on overlapping dates. Lines measuring different things (a day, a week, a block; other days or hours, which
    change what a count counts) are never the same: they all apply."""
    lines, seen = rule.get('lines') or [], {}
    for n, line in enumerate(lines):
        partitions = sorted({b['precedence_partition'] for b in (line.get('source_bindings') or {}).values()})
        key = json.dumps([partitions, _canonical_tree(line.get('who')), sorted(line.get('numbers') or {}), *_where(rule, line), measure(rule, line, block=True)],
                         sort_keys=True, default=str)
        seen.setdefault(key, []).append(n)
    spans = _spans(rule, lines)
    return sorted((p for same in seen.values() if len(same) > 1 for p in _pairs(same, spans)), key=lambda p: (p[1], p[0]))


def has_value(line, key):
    """Whether a line sets this number: a value (or `apply`); a number with no value is an explicit None."""
    number = (line.get('numbers') or {}).get(key)
    return number is not None and (key == 'apply' or number.get('value') is not None)


def _group(rule, line, k, keys, *, default_partition=None):
    """A number's group key: the line's where, days, hours and what number `k` counts there (the old kind it writes
    beside `keys` and that kind's settings, a quota's block aside). Lines share a number only within one group."""
    entry = _entry(rule)
    kind = _slot(entry, k, rule, line, keys)['kind']
    given = resolved_settings(kind, _settings(rule, line).get(kind, {}))
    counts = {f: given.get(f) for f in entry['line_settings'][kind]
              if not (kind == 'balance_goal' and f in FAIR_SHARE_DIALS)
              and (kind != 'block_quota' or f not in ('starts_on', 'ends_on'))}
    if kind == 'allocation_overtime':
        counts.pop('volunteer_required', None)  # Volunteering changes permission, not the duty a line applies to.
    partition = ((line.get('source_bindings') or {}).get(k) or {}).get('precedence_partition')
    if partition is None:
        partition = default_partition
    return json.dumps([partition, _canonical_tree(line.get('where')), sorted(line.get('weekdays') or range(7)), line.get('start_time') or '00:00',
                       line.get('end_time') or '24:00', kind, counts], sort_keys=True, default=str)


def _by_number(rule):
    """{(number, group key): [line indexes]}: the lines that set a number (a value or an explicit None) with the same
    where, days, hours and what it counts (`_group`)."""
    entry, out = _entry(rule), {}
    for n, line in enumerate(rule.get('lines') or []):
        keys = [k for k in (line.get('numbers') or {}) if k in entry['numbers']]
        for k in keys:
            out.setdefault((k, _group(rule, line, k, keys)), []).append(n)
    return out


def outranking_by_number(policy):
    """{(line index, number): [winning line indexes]} — blank inherits reviewed: a more specific line
    replaces a broader line's number only when it sets that number itself (a value, or None to remove it), with the same
    where, days, hours and measure on overlapping dates; a number it leaves blank stays the broader line's, from the
    nearest tier that sets it. Safety entries: none (every line applies, the strictest binds)."""
    rule = policy['definition']['rule']
    entry = _entry(rule)
    lines = rule.get('lines') or []
    if len(lines) < 2:
        return {}
    tiers = [tier(line) for line in lines]
    if len(set(tiers)) < 2:
        return {}
    spans, out = _spans(rule, lines, policy), {}
    for (k, _), group in _by_number(rule).items():
        if _slot(entry, k, rule, lines[group[0]], list(lines[group[0]].get('numbers') or {}))['kind'] in NEVER_OVERRIDDEN:
            continue
        levels = {n: lines[n]['numbers'][k].get('level', 'mandatory') for n in group}
        for n, w in _pairs(group, spans):
            if tiers[n] == tiers[w]:
                continue
            if tiers[n] > tiers[w]:
                n, w = w, n
            level, higher = levels[n], levels[w]
            if level is None or higher is None or (level == 'mandatory') == (higher == 'mandatory'):
                out.setdefault((n, k), []).append(w)
    return {key: sorted(winners) for key, winners in sorted(out.items())}


def outranking(policy, *, by_number=None):
    """{line index: [winning line indexes]} — the lines a more specific line takes at least one number from."""
    out = {}
    for (n, _), wins in (outranking_by_number(policy) if by_number is None else by_number).items():
        out.setdefault(n, set()).update(wins)
    return {n: sorted(w) for n, w in sorted(out.items())}


def line_relations(policy, would=None):
    """Editor provenance from the same groups, date spans and expansion used by active_rules."""
    rule = policy['definition']['rule']
    lines = rule.get('lines') or []
    tiers = [tier(line) for line in lines]
    if len(set(tiers)) < 2:
        return {}
    broader = [[] for _ in lines]
    for left, right in _pairs(range(len(lines)), _spans(rule, lines, policy)):
        if tiers[left] < tiers[right]:
            broader[right].append(left)
        elif tiers[right] < tiers[left]:
            broader[left].append(right)
    out, groups = {}, {}
    def group(n, key):
        if (n, key) not in groups:
            line = lines[n]
            groups[n, key] = _group(rule, line, key, list(line['numbers']))
        return groups[n, key]
    for n, line in enumerate(lines):
        candidates = []
        strictest = {}
        for k in broader[n]:
            other = lines[k]
            if would is not None and not (would.get(line['id'], set()) & would.get(other['id'], set())):
                continue
            shared = set(line['numbers']) & set(other['numbers'])
            if not shared:
                continue
            same_where = (_canonical_tree(line.get('where')) == _canonical_tree(other.get('where'))
                and sorted(line.get('weekdays') or range(7)) == sorted(other.get('weekdays') or range(7))
                and line.get('start_time', '00:00') == other.get('start_time', '00:00')
                and line.get('end_time', '24:00') == other.get('end_time', '24:00'))
            same_settings = all(group(n, key) == group(k, key) for key in shared)
            strength = {key: other['numbers'][key].get('level', 'mandatory') for key in shared}
            same_strength = all(line['numbers'][key].get('level') is None or
                (line['numbers'][key].get('level', 'mandatory') == 'mandatory') ==
                (strength[key] == 'mandatory') for key in shared)
            safety = any(_slot(_entry(rule), key, rule, line, list(line['numbers']))['kind'] in NEVER_OVERRIDDEN for key in shared)
            reason = ('safety' if safety else 'where_differs' if not same_where else
                      'settings_differ' if not same_settings else 'strength_differs' if not same_strength else None)
            effective = {}
            for key in shared:
                values = [source['numbers'][key].get('value') for source in (line, other)
                          if source['numbers'][key].get('value') is not None]
                if key == 'max' and values and reason is not None:
                    effective[key] = min(values)
                elif key == 'min' and values and reason is not None:
                    effective[key] = max(values)
                else:
                    effective[key] = line['numbers'][key].get('value')
                    if effective[key] is None and values:
                        effective[key] = values[0]
                if reason is not None and key in {'min', 'max'}:
                    strictest.setdefault(key, []).extend(values)
            candidates.append((reason is None, same_where, same_settings, tiers[k], -k,
                {'line_id': other['id'], 'line_no': k + 1, 'overrides': reason is None, 'reason': reason,
                 'fix': 'match_strength' if reason == 'strength_differs' else 'match_settings' if reason == 'settings_differ' and same_where else None,
                 'default_settings': deepcopy(_settings(rule, other)), 'default_severity': strength,
                 'effective_numbers': effective, 'both_apply': reason is not None}))
        if candidates:
            relation = max(candidates, key=lambda x: x[:-1])[-1]
            if relation['reason'] is not None:
                for key, values in strictest.items():
                    if key == 'max' and values:
                        relation['effective_numbers'][key] = min(values)
                    elif key == 'min' and values:
                        relation['effective_numbers'][key] = max(values)
            out[line['id']] = relation
    return out


def default_line_strengths(rule, given, previous_ids=(), would=None):
    """New lines inherit omitted settings and levels; explicit measures and historical lines keep their meaning."""
    lines = deepcopy(given)
    normalized = [RuleLine.model_validate(line).model_dump(mode='json') for line in lines]
    policy = {'id': 'preview', 'definition': {'rule': {**rule, 'lines': normalized}},
              'starts_on': None, 'ends_on': None}
    relations = line_relations(policy, would)
    changed = False
    for n, line in enumerate(lines):
        relation = relations.get(line['id'])
        if (line['id'] not in previous_ids and not line.get('settings')
                and line.get('measure') is None and line.get('period') is None and relation
                and relation['reason'] in (None, 'settings_differ', 'strength_differs')):
            line['settings'] = deepcopy(relation['default_settings'])
            if line['settings'] != normalized[n]['settings']:
                normalized[n] = RuleLine.model_validate(line).model_dump(mode='json')
                changed = True
    if changed:
        relations = line_relations(policy, would)
    for line in lines:
        relation = relations.get(line['id'])
        if line['id'] in previous_ids or not relation or relation['reason'] not in (None, 'strength_differs'):
            continue
        for key, number in line['numbers'].items():
            if number.get('level') is None and key in relation['default_severity']:
                number['level'] = relation['default_severity'][key]
    return lines


def both_apply(policy, *, same=None, include_safety=False):
    """[(k, n)]: lines of equal tier that set the same number (with a value) with the same where and measure on
    overlapping dates — neither is more specific, so both apply to anyone both name (a rank and a #tag). The same line
    twice aside (`duplicates`). Include safety to report matching numbers that bind across tiers too."""
    rule = policy['definition']['rule']
    lines = rule.get('lines') or []
    same = set(duplicates(rule) if same is None else same)
    tiers, spans, out = [tier(line) for line in lines], _spans(rule, lines, policy), set()
    entry = _entry(rule)
    for (number, _), group in _by_number(rule).items():
        first = lines[group[0]]
        if include_safety and _slot(entry, number, rule, first, list(first['numbers']))['kind'] in NEVER_OVERRIDDEN:
            out.update(p for p in _pairs([n for n in group if has_value(lines[n], number)], spans) if p not in same)
            continue
        together = {}  # person lines only meet lines naming one of their people; other lines meet their own tier
        for n in group:
            if has_value(lines[n], number):
                for key in named_people(lines[n]) if tiers[n] == 3 else [tiers[n]]:
                    together.setdefault(key, []).append(n)
        out.update(p for members in together.values() for p in _pairs(members, spans) if p not in same)
    return sorted(out)


def inherited(policy, would=None):
    """{line id: {number: {value, level, from}}} — the numbers a line leaves blank and takes from a broader line that
    counts the same thing there (`_group`: where, days, hours and the measure the number would write on this line; the
    nearest tier that sets it, covering its people today when `would` — line id → people — is given), as the resolver
    applies them. A broader explicit None leaves nothing to take; a broader line measuring something else (a daily
    limit beside a block quota) is not inherited — it applies on its own."""
    from .conditions import effective_severity

    rule = policy['definition']['rule']
    entry = _entry(rule)
    lines = rule.get('lines') or []
    tiers = [tier(line) for line in lines]
    if len(set(tiers)) < 2:
        return {}
    numbers = [set(line.get('numbers') or {}) for line in lines]
    differing = set.union(*numbers) - set.intersection(*numbers)
    possible = [key for key in entry['numbers'] if key != 'apply' and key in differing]
    if not possible:
        return {}
    spans, out, sets, held = _spans(rule, lines, policy), {}, {}, {}
    for (k, group), members in _by_number(rule).items():  # (group, number, tier): the lines that set it, in order
        for w in members:
            sets.setdefault((group, k, tiers[w]), []).append(w)

    def holding(bucket, mine):
        """The lines of a bucket holding for at least one of `mine` today, in order (by person, built once)."""
        if bucket not in held:
            held[bucket] = {}
            for w in sets[bucket]:
                for p in would.get(lines[w]['id'], ()):
                    held[bucket].setdefault(p, []).append(w)
        return sorted({w for p in mine for w in held[bucket].get(p, ())})
    for n, line in enumerate(lines):
        mine = (would or {}).get(line['id'])
        keys = [k for k in (line.get('numbers') or {}) if k in entry['numbers']]
        for k in possible:
            if k in (line.get('numbers') or {}):
                continue
            group = _group(rule, line, k, keys + [k])  # what this number would count on this line
            for t in range(tiers[n] - 1, 0, -1):  # the nearest tier that sets it; within it, the first line
                bucket = (group, k, t)
                if bucket not in sets:
                    continue
                src = next((w for w in (holding(bucket, mine) if mine else sets[bucket]) if _meets(spans[n], spans[w])), None)
                if src is not None:
                    if has_value(lines[src], k):
                        source = lines[src]
                        number = source['numbers'][k]
                        slot = _slot(entry, k, rule, source, source['numbers'])
                        level = effective_severity(slot['kind'], number.get('level'), advisory_slot=slot['advisory'])
                        out.setdefault(line['id'], {})[k] = {**number, 'level': level, 'from': source['id']}
                    break
    return out


def contradictions(policy, would=None):
    """[(line index, the inherited numbers at fault, inherited(…) of that line)]: a blank number taken from a broader
    line that contradicts the line's own, min ≤ target ≤ max read on effective numbers (a "max 0" taking a minimum).
    The one reading, for Needs attention and the check as you edit."""
    from packages.contracts.rule_catalogue import number_range_errors
    lines, out = policy['definition']['rule'].get('lines') or [], []
    got_all = inherited(policy, would)
    for n, line in enumerate(lines):
        got = got_all.get(line['id'], {})
        numbers = {**{k: v['value'] for k, v in got.items()}, **{k: v['value'] for k, v in line['numbers'].items() if has_value(line, k)}}
        bad = sorted(k for k in got if k in ('min', 'target', 'max'))
        if bad and number_range_errors(numbers):
            out.append((n, bad, got))
    return out


def _broader_levels(lines, by_number):
    """Shared explicit-level candidates per blank number/group/tier; the evaluator resolves dated membership."""
    tiers, out = [tier(line) for line in lines], {}
    for (k, _), group in by_number.items():
        first = {}
        for w in group:
            if has_value(lines[w], k) and lines[w]['numbers'][k].get('level', 'mandatory') is not None:
                first.setdefault(tiers[w], []).append(w)
        candidates = {t: tuple(w for lower in sorted(first, reverse=True) if lower < t for w in first[lower]) for t in (2, 3)}
        for n in group:
            if lines[n]['numbers'][k].get('level', 'mandatory') is not None:
                continue
            below = [t for t in first if t < tiers[n]]
            if below:
                out[(n, k)] = candidates[tiers[n]]
    return out


def _levels(lines, n, k, broader, entry, slot, sources, keys, groups):
    """Fixed-level copies, selected at evaluation time from matching, dated source lines."""
    from .conditions import effective_severity

    if effective_severity(slot['kind'], None, advisory_slot=slot['advisory']) == 'mandatory':
        return [('mandatory', None)]
    number = lines[n]['numbers'][k]
    if number.get('level', 'mandatory') is not None:
        return [(number.get('level', 'mandatory'), None)]
    fallback = 'medium' if slot['advisory'] or 'mandatory' not in entry['levels'] else 'mandatory'
    mine = sources[keys[n]]
    group_key = (id(broader.get((n, k))), mine['starts_on'], mine['ends_on'])
    if group_key not in groups:
        candidates = tuple(w for w in broader.get((n, k), ()) if _overlap((mine['starts_on'], mine['ends_on']),
                                                                       (sources[keys[w]]['starts_on'], sources[keys[w]]['ends_on'])))
        groups[group_key] = (candidates, tuple(keys[w] for w in candidates))
    candidates, references = groups[group_key]
    levels = {lines[w]['numbers'][k].get('level', 'mandatory') for w in candidates}
    # An everyone line covering this line's complete dates guarantees that fallback cannot be reached.
    covers = any(not (lines[w].get('who') or {}).get('conditions')
                 and (not sources[keys[w]]['starts_on'] or mine['starts_on'] and sources[keys[w]]['starts_on'] <= mine['starts_on'])
                 and (not sources[keys[w]]['ends_on'] or mine['ends_on'] and mine['ends_on'] <= sources[keys[w]]['ends_on']) for w in candidates)
    if not covers:
        levels.add(fallback)
    if len(levels) == 1:
        return [(next(iter(levels)), None)]
    mine.setdefault('inherit', {})[k] = {'sources': references, 'fallback': fallback}
    return [(level, f'{keys[n]}#{k}#{level}') for level in LEVELS if level in levels]


def source_key(policy, line):
    return f"{policy['id']}:{line['id']}"


def source_lines(policy):
    """Original line applicability, including None-only lines; shared once across all numeric copies."""
    if '_catalogue_sources' in policy:
        return policy['_catalogue_sources']
    text = json.dumps(policy, sort_keys=True, default=str)
    copies = _expanded(text)
    return copies[0]['_catalogue_sources'] if copies else _sources(text)


@lru_cache(maxsize=64)
def _sources(text):
    policy = json.loads(text)
    return _source_lines(policy)


def _source_lines(policy):
    rule = policy['definition']['rule']
    out = {}
    for n, line in enumerate(rule.get('lines') or []):
        a, b = when(rule, line)
        out[source_key(policy, line)] = {'id': line.get('_source_identity', line['id']), 'policy_id': policy['id'],
            'line': line.get('_source_index', n), 'tier': tier(line),
            'starts_on': _later(policy.get('starts_on'), a), 'ends_on': _earlier(policy.get('ends_on'), b),
            'definition': {'scope': scope_of(line)}, 'numbers': line.get('numbers') or {},
            'whole_person': unconditional_people(line)}
    return out


def expand(policy, outrank=True, *, stored_goal=True):
    """The copies a catalogue rule's lines make, in the order checks read them (minimums, maximums, targets, …).
    Each call gets its own copies (definition, scope and rule dicts); equal rules are built once. `outrank=False`:
    each line as it would read alone (what a more specific line takes from it)."""
    out = []
    for c in _expanded(json.dumps(policy, sort_keys=True, default=str), outrank, stored_goal):
        d = c['definition']
        has_template = '_v2_template_line' in c
        template = c.get('_v2_template_line')
        item = {k: v for k, v in c.items() if k != '_v2_template_line'}
        out.append({**item, 'template_line': (deepcopy(template) if template is not None else {}) if has_template else dict(c['template_line']),
                    'definition': {**d, 'scope': dict(d['scope']), 'rule': dict(d['rule'])}})
    return out


def prepare_catalogue_rule(policy):
    # Cross-tier overlap can be quadratic. Prime its primitive definitions without computing precedence.
    lines = policy['definition']['rule'].get('lines') or []
    outrank = len({tier(line) for line in lines}) <= 1
    _expanded(json.dumps(policy, sort_keys=True, default=str), outrank, True)


@lru_cache(maxsize=64)
def _expanded(text, outrank=True, stored_goal=True):
    return tuple(_expand(json.loads(text), outrank, stored_goal))


def _expand(policy, outrank=True, stored_goal=True):
    d = policy['definition']
    rule = d['rule']
    if not rule.get('on', True):
        return []
    entry = _entry(rule)
    lines = rule.get('lines') or []
    sources = _source_lines(policy)
    source_keys = [source_key(policy, line) for line in lines]
    level_groups = {}
    beaten = outranking_by_number(policy) if outrank else {}
    broader = _broader_levels(lines, _by_number(rule)) if any(v.get('level', 'mandatory') is None for line in lines
                                                               for v in (line.get('numbers') or {}).values()) else {}
    # An inherited advisory line cannot replace a mandatory zero.
    inherited_winners = {w for (n, k), winners in beaten.items() if k == 'max'
                         and lines[n]['numbers'][k].get('value') == 0
                         for w in winners if lines[w]['numbers'][k].get('level', 'mandatory') is None}
    winner_levels = {w: dict(_levels(lines, w, 'max', broader, entry,
        _slot(entry, 'max', rule, lines[w], list(lines[w]['numbers'])), sources, source_keys, level_groups))
        for w in sorted(inherited_winners)}
    live_zero_lines = set(policy.get('_live_zero_line_ids', ()))
    plans = []  # per line: {(kind, fixed, level, winners): [first key, [(slot, value, key)]]}
    for n, line in enumerate(lines):
        settings, groups, keys = _settings(rule, line), {}, list(line.get('numbers') or {})
        for key in NUMBER_ORDER:
            if not has_value(line, key):  # blank inherits; an explicit None writes nothing (and outranks the number)
                continue
            number = line['numbers'][key]
            slot = _slot(entry, key, rule, line, keys)
            for level, selector in _levels(lines, n, key, broader, entry, slot, sources, source_keys, level_groups):
                if policy.get('_zero_max_projection') and level != 'mandatory':
                    continue
                if key == 'max' and level == 'mandatory' and line['id'] in live_zero_lines:
                    continue
                if slot['advisory'] and level == 'mandatory':
                    raise ValueError(f"{entry['name']}: a {key.replace('_', ' ')} here is a preference, not mandatory")
                winners = tuple((w, None) for w in beaten.get((n, key), ()))
                if key == 'max' and level == 'mandatory' and number.get('value') == 0:
                    winners = tuple((w, winner_levels.get(w, {}).get('mandatory')) for w, _ in winners
                                    if 'mandatory' in winner_levels.get(w, (lines[w]['numbers'][key].get('level', 'mandatory'),)))
                group = groups.setdefault((slot['kind'], json.dumps(slot['fixed'], sort_keys=True), level, winners, selector), [key, []])
                group[1].append((slot, number.get('value'), key))
        plans.append((settings, groups))
    out = []
    for key in NUMBER_ORDER:
        for n, (line, (settings, groups)) in enumerate(zip(lines, plans)):
            for (kind, _, level, winners, selector), (first, numbers) in groups.items():
                if first == key:
                    copy = _copy(policy, line, n, kind, level, numbers, settings, winners, lines, sources, selector, stored_goal)
                    if copy is not None:
                        out.append(copy)
    return out


def _copy(policy, line, n, kind, level, numbers, settings, winners, lines, sources, selector, stored_goal=True):
    from .conditions import effective_severity

    if line.get('_source_default_only') and (kind != 'block_quota' or not selector):
        return None
    d = policy['definition']
    advisory = any(slot['advisory'] for slot, _, _ in numbers)
    defaults = _entry(d['rule'])['default_settings'].get(kind, {}) if d['rule'].get('catalogue_version', 1) == 2 else {}
    body = {**defaults, **settings.get(kind, {}), 'kind': kind}
    if kind == 'allocation_overtime':
        body.setdefault('volunteer_required', False)  # Missing persisted settings retain pre-volunteer behaviour.
    for slot, value, _ in numbers:
        body.update(slot['fixed'])
        if slot['field']:
            body[slot['field']] = int(value) if float(value).is_integer() else value
    starts_on, ends_on = _later(policy.get('starts_on'), line.get('starts_on')), _earlier(policy.get('ends_on'), line.get('ends_on'))
    if kind == 'block_quota':
        # Validate the configured block before intersection: an inactive line cannot hide a malformed quota.
        body['starts_on'] = body.get('starts_on') or line.get('starts_on') or policy.get('starts_on')
        body['ends_on'] = body.get('ends_on') or line.get('ends_on') or policy.get('ends_on')
    definition = {'scope': scope_of(line), 'severity': 'mandatory' if level == 'mandatory' else 'advisory',
                  'strength': 'medium' if level == 'mandatory' else level,
                  'balance_between_people': d.get('balance_between_people', True),
                  'exception_allowed': bool(d.get('exception_allowed')) and not advisory, 'rule': body,
                  'merged_ids': list(d.get('merged_ids') or []), 'calendar_entry_id': line.get('calendar_entry_id')}
    valid = _valid(json.dumps(definition, sort_keys=True, default=str), stored_goal)
    quota_window = None
    if kind == 'block_quota':
        block = valid['rule']
        quota_window = (block['starts_on'], block['ends_on'])
        a, b = _later(block['starts_on'], starts_on), _earlier(block['ends_on'], ends_on)
        if b is not None and str(a) >= str(b):
            return None  # Valid ranges with no shared day impose no quota; their source records remain available.
        # A nonempty clipped period must still contain its configured complete rolling window.
        valid = _valid(json.dumps({**valid, 'rule': {**block, 'starts_on': a, 'ends_on': b}}, sort_keys=True, default=str))
        if selector and b is not None:
            sources[source_key(policy, line)]['inherit'][numbers[0][2]]['block'] = (a, b)
    if line.get('_source_default_only'):
        return None
    scope = valid['scope']
    if winners:
        direct = {w for w, guard in winners if kind == 'block_quota' and guard is None and unconditional_people(lines[w])}
        people = sorted({p for w in direct for p in named_people(lines[w])})
        dynamic = [guard or source_key(policy, lines[w]) for w, guard in winners if w not in direct]
        extra = ([{'field': 'person_ids', 'op': 'none', 'values': people}] if people else []) + (
            [{'field': OUTRANKED, 'op': 'none', 'values': dynamic}] if dynamic else [])
        scope = {**scope, 'conditions': {'match': 'all', 'conditions': ([scope['conditions']] if scope.get('conditions') else []) + extra}}
    if selector:
        scope = {**scope, 'conditions': {'match': 'all', 'conditions': ([scope['conditions']] if scope.get('conditions') else [])
                                      + [{'field': INHERITED_LEVEL, 'op': 'any', 'values': [selector]}]}}
    return {**policy, 'kind': kind, 'starts_on': starts_on, 'ends_on': ends_on,
            **({'_quota_full_window': quota_window} if quota_window else {}),
            '_catalogue_sources': sources,
            **({'_advisory_catalogue_slot': True} if advisory and effective_severity(kind, 'advisory') == 'mandatory' else {}),
            # Conversion makes the original policy partition explicit; keep grouping metadata identical.
            **({'_fair_share_group': {'where': _group(d['rule'], line, numbers[0][2], list(line['numbers']),
                                                     default_partition=policy['id']),
                'who': _canonical_tree(line.get('who')), 'tier': tier(line),
                'source': source_key(policy, line)}} if kind == 'balance_goal' else {}),
            # A quota copy is one bound or one target, as `_parts` reads a range: no target fields of its own.
            'definition': {**valid, 'scope': scope, 'rule': {k: v for k, v in valid['rule'].items()
                                                             if kind != 'block_quota' or k not in ('target_amount', 'target_strength')}},
            'template_line': {'id': line.get('_source_identity', line['id']), 'line': line.get('_source_index', n),
                              'slot': '+'.join(k for _, _, k in numbers)}}


def resolved_overtime_offers(data, person_id, start, end, shift=None):
    """Eligibility reads the same dated, ranked lines as scheduling, before the volunteer gate.

    Without a duty, discover overlapping line windows; with a duty, require complete coverage. Request-generated
    offers cannot become a new eligibility source. Ordinary approved offers and converted source lines remain valid.
    """
    from datetime import date
    from .conditions import active_rules, with_tags
    from .generation import allocation_policy_matches, overtime_cap_ids
    from .time import aware, local_midnight
    from .validation import person_in_scope, scope_intervals, within_dates
    start, end = aware(start), aware(end)
    prepared = data.get('_overtime_resolved')
    if prepared is None:
        prepared = dict(data)
        prepared['policies'] = bound_policies(prepared.get('policies') or [], context=prepared)
        prepared['_catalogue_v2_materialized'] = True
        prepared = with_tags(prepared)
        prepared['_overtime_originals'] = {p['id']: p for p in prepared.get('policies') or []}
        prepared['policies'] = active_rules(prepared.get('policies') or [], context=prepared)
        prepared['_overtime_cap_ids'] = overtime_cap_ids(prepared)
        data['_overtime_resolved'] = prepared
    zone, out = prepared['organisation']['timezone'], []
    for policy in prepared['policies']:
        if policy['definition']['rule']['kind'] != 'allocation_overtime' or policy.get('_overtime_request'):
            continue
        if not set(policy['definition']['rule'].get('required_policy_ids', [])) <= prepared['_overtime_cap_ids']:
            continue
        if shift is not None:
            duty = {**shift, 'start': start.isoformat(), 'end': end.isoformat()}
            if not allocation_policy_matches(prepared, policy, duty, person_id):
                continue
        else:
            if not within_dates(policy, start, end, zone):
                continue
            lo = max(start, local_midnight(date.fromisoformat(str(policy['starts_on'])), zone)) if policy.get('starts_on') else start
            hi = min(end, local_midnight(date.fromisoformat(str(policy['ends_on'])), zone)) if policy.get('ends_on') else end
            scope = policy['definition']['scope']
            if hi <= lo or not person_in_scope(prepared, scope, person_id, lo, hi) or not scope_intervals(prepared, scope, person_id, lo, hi):
                continue
        source = prepared['_overtime_originals'][policy['id']]
        template, rule = policy.get('template_line') or {}, source['definition']['rule']
        if rule['kind'] == 'catalogue':
            line = next((line for line in rule['lines'] if line.get('_source_identity', line['id']) == template.get('id')), None)
            if line is None:
                continue
        elif source.get('_v2_source_lines'):
            n = template.get('line', -1)
            indices = source.get('_v2_row_indices') or []
            index = indices.index(n) if n >= 0 and indices else n
            line = source['_v2_source_lines'].get(index, source['_v2_source_lines'].get(str(index)))
        else:
            from .rule_catalogue_migration import Kept, NAMESPACE, lines_of
            try:
                _, lines = lines_of(source)
                n = template.get('line')
                index = n + (0 if source['definition'].get('listed_only') else 1) if n is not None else 0
                line = lines[index]
            except Kept:
                import uuid
                # A mixed legacy OR cannot split into who/where; the frozen request retains its exact source gate.
                scope, body = policy['definition']['scope'], policy['definition']['rule']
                line = {'id': str(uuid.uuid5(NAMESPACE, f"{policy['id']}:{template.get('line', -1)}")),
                        'numbers': {'apply': {'level': policy['definition'].get('strength', 'medium')}},
                        'settings': {'allocation_overtime': {k: v for k, v in body.items() if k != 'kind'}},
                        'weekdays': scope.get('weekdays', list(range(7))), 'start_time': scope.get('start_time', '00:00'),
                        'end_time': scope.get('end_time', '24:00'), 'starts_on': policy.get('starts_on'), 'ends_on': policy.get('ends_on')}
        out.append({**policy, '_overtime_line': {k: deepcopy(v) for k, v in line.items() if k in RuleLine.model_fields}})
    return out


# Needs attention: what the checks above find in the rules in force, with fixes. ------

ATTENTION = ('inherits', 'shadowed', 'both', 'duplicate', 'ending', 'counting_ends', 'nobody', 'blocks')


def counting_attention(policy):
    """Finite counting ends that leave later effective validity without an obligation."""
    rule = policy['definition']['rule']
    out = []
    for n, line in enumerate(rule.get('lines') or []):
        if 'block_quota' not in _kinds(rule, line):
            continue
        block = _settings(rule, line).get('block_quota', {})
        end = block.get('ends_on')
        validity_end = _earlier(policy.get('ends_on'), line.get('ends_on'))
        if end and (not validity_end or str(end) < str(validity_end)):
            out.append({'kind': 'counting_ends', 'line': n, 'line_id': line['id'],
                        'counting_end': str(end), 'validity_end': str(validity_end) if validity_end else None})
    return out


def _fix(kind, recommended=False, ops=(), **more):
    """One way to settle an issue: `ops` change lines ({line_id, remove} or {line_id, numbers / ends_on}); none = keep."""
    return {'kind': kind, 'recommended': recommended, 'ops': list(ops), **more}


def _ranked(fixes):
    """The recommended fix first (exactly one is)."""
    return sorted(fixes, key=lambda f: not f['recommended'])


def attention(rows, would, today, findings=(), soon=30):
    """Issues in the catalogue rules in force, each with a recommended fix and alternatives, in a stable order (kind,
    then the rule's catalogue order, then its lines). Read through the same checks as propose: `both_apply`,
    `duplicates`; `would` is the people each line holds for today alone (line id → set). Kinds:
    shadowed (every number replaced for everyone today), both (equal tiers both apply), duplicate,
    ending (its last day within `soon` days), nobody (valid today, holds for no one: keep, as dated memberships may change),
    blocks (mandatory lines have errors in rotas being planned, one issue per set of lines with
    the drafts inside: `findings` carry `workspace_id`, `workspace` and the validator's `rule_id`, `detail.line`).
    Words are the client's: an issue carries facts and line ids only."""
    from datetime import date, timedelta
    from .validation import finding_observations
    order = {e: n for n, e in enumerate(CATALOGUE_BY_ID)}
    today = date.fromisoformat(str(today))
    out = []
    for row in sorted(rows, key=lambda r: order.get(r['definition']['rule']['id'], 0)):
        rule = row['definition']['rule']
        if not rule.get('on', True):
            continue
        entry, lines = _entry(rule), rule.get('lines') or []
        counting_ends = {warning['line']: warning for warning in counting_attention(row)}
        ids = [line['id'] for line in lines]
        index = {i: n for n, i in enumerate(ids)}
        people = [would.get(i, set()) for i in ids]
        active = [line_validity(row, line, today)['effective'] == 'active' for line in lines]

        def issue(kind, at, facts=None, fixes=(), count=None, key=''):
            return {'id': f"{kind}:{row['id']}:{key or '+'.join(ids[n] for n in at)}", 'kind': kind, 'rule_id': row['id'],
                    'catalogue_id': rule['id'], 'lines': list(at), 'line_ids': [ids[n] for n in at], 'people': count,
                    'facts': facts or {}, 'fixes': _ranked(list(fixes))}
        for n, bad, got in contradictions(row, would):
            line = lines[n]
            none = {**line['numbers'], **{k: {'value': None, 'level': got[k].get('level', 'mandatory')} for k in bad}}
            out.append(issue('inherits', [n, *sorted({index[got[k]['from']] for k in bad})], {'line': ids[n], 'inherited': {k: got[k] for k in bad}},
                             count=len(people[n]), fixes=[_fix('none', True, [{'line_id': ids[n], 'numbers': none}], numbers=bad), _fix('acknowledge')]))
        outranked = outranking_by_number(row)
        for n, line in enumerate(lines):
            keys = sorted(line.get('numbers') or {})
            if active[n] and people[n] and keys and all(
                    people[n] <= set().union(*(people[w] for w in outranked.get((n, key), ()) if active[w]))
                    for key in keys):
                winners = sorted({w for key in keys for w in outranked[(n, key)] if active[w] and people[w] & people[n]})
                out.append(issue('shadowed', [n], {'winners': [ids[w] for w in winners], 'numbers': keys}, count=len(people[n]),
                                 fixes=[_fix('acknowledge', True), _fix('remove', False,
                                     [{'line_id': ids[n], 'remove': True}], line_id=ids[n])]))
        for k, n in both_apply(row):
            met = people[k] & people[n]
            if not met:
                continue
            same = lines[k]['numbers'] == lines[n]['numbers']
            # Today's people subset proves no coverage of future dates, memberships or duty conditions.
            fixes = [_fix('remove', False, [{'line_id': ids[x], 'remove': True}], line_id=ids[x]) for x in (k, n)]
            out.append(issue('both', [k, n], {'same_numbers': same, 'covered': False}, count=len(met),
                             fixes=fixes + [_fix('acknowledge', True)]))
        for k, n in duplicates(rule):
            out.append(issue('duplicate', [k, n], fixes=[_fix('remove', True, [{'line_id': ids[n], 'remove': True}], line_id=ids[n]),
                                                       _fix('remove', False, [{'line_id': ids[k], 'remove': True}], line_id=ids[k])]))
        for n, line in enumerate(lines):
            start, end = line.get('starts_on'), line.get('ends_on')
            if n in counting_ends:
                out.append(issue('counting_ends', [n], {key: counting_ends[n][key] for key in ('counting_end', 'validity_end')},
                                 fixes=[_fix('acknowledge', True)]))
            end = end and date.fromisoformat(str(end))
            if end and today < end and (end - today).days - 1 <= soon:
                out.append(issue('ending', [n], {'last_day': str(end - timedelta(days=1)), 'days': (end - today).days - 1}, fixes=[
                    _fix('acknowledge', True), _fix('extend', False, [{'line_id': ids[n], 'ends_on': str(end + timedelta(days=365))}])]))
            valid = (not start or str(start) <= str(today)) and (not end or today < end)
            if valid and within(row, today) and not people[n]:
                # Named people may join or return later, just as a group can gain members after today.
                out.append(issue('nobody', [n], {'group': tier(line) != 3}, fixes=[
                    _fix('remove', False, [{'line_id': ids[n], 'remove': True}], line_id=ids[n]), _fix('acknowledge', True)]))
        mine = {}
        for f in findings:
            if f.get('rule_id') == row['id'] and f.get('severity') == 'error':
                mine.setdefault((f.get('workspace') or '', f['workspace_id']), []).append(f)
        mandatory = [any(v.get('level', 'mandatory') == 'mandatory' for v in line['numbers'].values()) for line in lines]
        blocked = {}  # the lines that block → the drafts they block: one issue per set of lines, its drafts inside
        for (name, workspace), found in sorted(mine.items()):
            observations = list(finding_observations({'findings': found}))
            at = sorted({x for f in observations if f.get('rule_id') == row['id'] and f.get('severity') == 'error'
                         and isinstance(x := (f.get('detail') or {}).get('line'), int) and 0 <= x < len(lines)})
            if not at:  # shortcut: most findings name the rule, not the line; the mandatory lines holding for their people today
                named = {f.get('person_id') for f in observations if f.get('rule_id') == row['id'] and f.get('severity') == 'error'} - {None}
                at = [n for n in range(len(lines)) if mandatory[n] and people[n] & named]
            blocked.setdefault(tuple(at), []).append({'workspace_id': workspace, 'workspace': name, 'count': len(found),
                                                      'message': found[0].get('message'), 'person_id': found[0].get('person_id')})
        for at, drafts in sorted(blocked.items()):
            hard = [n for n in at if mandatory[n]]
            soften = [{'line_id': ids[n], 'numbers': {k: {**v, 'level': 'medium'} if v.get('level', 'mandatory') == 'mandatory' else v
                                                      for k, v in lines[n]['numbers'].items()}} for n in hard]
            advisory = soften and 'medium' in entry['levels'] and not entry['safety']  # safety stays mandatory
            out.append(issue('blocks', list(at), {'drafts': drafts, 'workspace_ids': sorted(d['workspace_id'] for d in drafts),
                                                  'count': sum(d['count'] for d in drafts)},
                             fixes=[_fix('resolve', n == 0, workspace_id=d['workspace_id'], workspace=d['workspace']) for n, d in enumerate(drafts)]
                             + ([_fix('advisory', False, soften)] if advisory else [])))
    return sorted(out, key=lambda x: ATTENTION.index(x['kind']))  # stable: within a kind, catalogue order and lines


# Facts that move with the day or a rota, not with the rule: they never expire an acknowledgement.
_VOLATILE = frozenset({'days', 'count', 'drafts', 'covered'})


def fingerprint(issue, lines):
    """What an acknowledgement of an issue holds for: its kind, rule, the involved lines as stored (`lines`: line id →
    line, before tags are read as their members, so a tag gaining a member keeps it) and its effective numbers (what
    gives way, what is inherited). Any change to them is a new fingerprint: the acknowledgement expires. The version
    id, who it reaches today and the days left are not part of it."""
    import hashlib
    body = [issue['kind'], issue['catalogue_id'], [lines.get(i) for i in issue['line_ids']],
            {k: v for k, v in issue['facts'].items() if k not in _VOLATILE}]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def within(row, day):
    """Whether a rule row holds on a day (its own dates)."""
    return (not row.get('starts_on') or str(row['starts_on']) <= str(day)) and (not row.get('ends_on') or str(day) < str(row['ends_on']))


def undated_quota(rule, line):
    """Whether a line counts as a quota (a minimum, target, or a maximum the settings make a quota) with no block: no
    dates of its own and no block dates in its settings."""
    entry = _entry(rule)
    keys = [k for k in (line.get('numbers') or {}) if k in entry['numbers']]
    block = _settings(rule, line).get('block_quota', {})
    if block.get('repeat'):
        return False
    return (any(_slot(entry, k, rule, line, keys)['kind'] == 'block_quota' for k in keys)
            and not (line.get('starts_on') and line.get('ends_on')) and not (block.get('starts_on') and block.get('ends_on')))


def issues(definition):
    """Why a catalogue rule's lines cannot be checked (each line through the old schemas), as solver issues."""
    from pydantic import ValidationError
    found, lines = [], definition['rule'].get('lines') or []
    rule = definition['rule']
    for n, line in enumerate(lines):
        numbers = line.get('numbers') or {}
        groups = {}
        for key in ('min', 'target', 'max'):
            if has_value(line, key):
                groups.setdefault(_group(rule, line, key, list(numbers)), []).append(numbers[key]['value'])
        if any(values != sorted(values) for values in groups.values()):
            found.append({'field': f'definition.rule.lines.{n}.numbers', 'code': 'INVALID_RULE_DEFINITION',
                          'message': f'Line {n + 1}: minimum must not exceed target or maximum of the same measure'})
    if found:
        return found
    try:  # usually every line is fine: one expansion; otherwise line by line, to name each
        expand({'id': '', 'definition': {**definition, 'rule': {**definition['rule'], 'on': True}}}, outrank=False, stored_goal=False)
        return []
    except (ValidationError, ValueError):
        pass
    for n, line in enumerate(lines):
        try:
            expand({'id': '', 'definition': {**definition, 'rule': {**definition['rule'], 'on': True, 'lines': [line]}}}, stored_goal=False)
        except (ValidationError, ValueError) as error:
            message = error.errors()[0]['msg'] if isinstance(error, ValidationError) else str(error)
            field = f'definition.rule.lines.{n}'
            if isinstance(error, ValidationError):
                detail = error.errors()[0]
                if detail['type'] == 'missing' and detail['loc']:
                    label = _entry(rule).get('setting_labels', {}).get(detail['loc'][-1])
                    if label:
                        message = f'choose {label.lower()}'
                        field += '.settings.' + '.'.join(map(str, detail['loc'][1:]))
            found.append({'field': field, 'code': 'INVALID_RULE_DEFINITION',
                          'message': f"Line {n + 1}: {message.removeprefix('Value error, ')}"})
    return found
