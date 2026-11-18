"""Presentation of recorded check facts; never evaluates a rule or certifies a candidate."""
from calendar import day_name
from collections import Counter
import json
import re
from zoneinfo import ZoneInfo

from packages.contracts.rule_catalogue import FIX_LIST_WORDS
from packages.contracts.schemas import FIX_ACTION_GROUPS, FIX_GROUP_LABELS
from .finding_causes import DUTIES_DURING_APPROVED_LEAVE
from .time import aware


_SHIFT_WORD = re.compile(r'\bshifts?\b', re.IGNORECASE)


def session_words(text):
    """Use the Planner's user-facing unit for generated Fix list wording."""
    if not isinstance(text, str):
        return text
    def replace(match):
        word = match.group(0)
        value = 'sessions' if word.lower() == 'shifts' else 'session'
        return value.upper() if word.isupper() else value.capitalize() if word[:1].isupper() else value
    return _SHIFT_WORD.sub(replace, text)


def ordinal(number):
    suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    return f'{number}{suffix}'


def plural(label, count):
    # Catalogue names remain authoritative; already plural names are not inflected twice.
    return label if count == 1 or label.lower().endswith('s') else label + 's'


def who_label(data, tree):
    """Name a single positive audience honestly; compound/negated audiences use the existing fallback."""
    from packages.contracts.schemas import CONDITION_FIELD_DEFINITIONS
    if not tree:
        return FIX_LIST_WORDS['person']
    if 'conditions' in tree:
        nodes = tree['conditions']
        return who_label(data, nodes[0]) if len(nodes) == 1 else FIX_LIST_WORDS['person'] if not nodes else None
    values = tree.get('values') or []
    if tree.get('op', 'any') not in {'any', 'all', 'is'} or len(values) != 1:
        return None
    field = tree.get('field')
    collection = next((f['kind'] for f in CONDITION_FIELD_DEFINITIONS if f['key'] == field and not f['session']), None)
    if field == 'tags':
        # Tag conditions store names, unlike the ID-valued catalogue operands.
        return values[0]
    if not collection:
        return None
    rows = data.get(collection) or {}
    row = rows.get(values[0], {}) if isinstance(rows, dict) else next(
        (row for row in rows if row.get('id') == values[0]), {})
    return row.get('name') or row.get('display_name') or None


def session_context(data, shift):
    return {'shift_id': shift['id'], 'service_ids': shift.get('service_ids', []),
        'service_names': [data.get('services', {}).get(sid, {}).get('name') for sid in shift.get('service_ids', [])],
        'location_name': data.get('locations', {}).get(shift.get('location_id'), {}).get('name'),
        'assignment_type_name': (shift.get('type_snapshot') or {}).get('name')
            or data.get('types', {}).get(shift.get('assignment_type_id'), {}).get('name'),
        'weekday': aware(shift['start']).astimezone(ZoneInfo(data['organisation']['timezone'])).weekday(),
        'locked': shift.get('locked', False)}


def coverage_signature(rows, source_for, coverage_codes):
    """Different audiences or measured headcounts cannot share one inline repair."""
    facts = set()
    for row in rows:
        if row['code'] not in coverage_codes:
            continue
        source, detail = source_for(row), row.get('detail') or {}
        required = detail.get('required', detail.get('target'))
        actual = detail.get('available')
        # Historical/aggregate supervision evidence is not a headcount of the missing audience.
        if required is not None and actual is not None:
            facts.add((json.dumps(source.get('who'), sort_keys=True), required, actual))
    return tuple(sorted(facts))


def where_label(context, shift_ids):
    sessions = context.get('sessions', {})
    rows = [sessions[sid] for sid in sorted(shift_ids) if sid in sessions]
    if not rows or len(rows) != len(shift_ids):
        return None
    types = {row.get('assignment_type_name') for row in rows}
    services = {tuple(row.get('service_names') or []) for row in rows}
    locations = {row.get('location_name') for row in rows}
    if len(types) != 1 or not next(iter(types)):
        return None
    service_names = next(iter(services)) if len(services) == 1 else ()
    if any(name is None for name in service_names):
        return None
    place = ' + '.join(service_names)
    if not place:
        place = next(iter(locations)) if len(locations) == 1 else None
    if not place:
        return None
    weekdays = {row.get('weekday') for row in rows}
    weekend = set(context.get('weekend_days') or [])
    if None in weekdays:
        day = ''
    elif weekend and weekdays == weekend:
        day = FIX_LIST_WORDS['weekend']
    elif weekend and weekdays == set(range(len(day_name))) - weekend:
        day = FIX_LIST_WORDS['weekday']
    else:
        day = ' / '.join(plural(day_name[d], len(rows)) for d in sorted(weekdays))
    duty = session_words(plural(next(iter(types)), len(rows)))
    return {'label': ' '.join(filter(None, (day, place, duty)))}


def presentation(context, check, evidence, shift_ids, person_ids, template, *, level, family):
    where = where_label(context, shift_ids)
    missing, facts = [], {}
    from .finding_causes import COVERAGE_CODES
    for row in evidence:
        if row['code'] not in COVERAGE_CODES:
            continue
        detail = row.get('detail') or {}
        source = (context.get('sources') or {}).get(json.dumps(
            [row.get('rule_id'), detail.get('line'), detail.get('slot')]), {})
        label, have = source.get('who_label'), detail.get('available')
        need = detail.get('required', detail.get('target'))
        if not label or not isinstance(have, int) or not isinstance(need, int) or not 0 <= have < need:
            continue
        facts.setdefault(label, set()).add((have, need))
    if facts and all(len(values) == 1 for values in facts.values()):
        missing = [{'who_label': label, 'have': next(iter(values))[0], 'need': next(iter(values))[1]}
                   for label, values in sorted(facts.items())]
    sessions = f"{len(shift_ids)} {FIX_LIST_WORDS['session' if len(shift_ids) == 1 else 'sessions']}"
    people = f"{len(person_ids)} {FIX_LIST_WORDS['person' if len(person_ids) == 1 else 'people']}"
    detail_line = FIX_LIST_WORDS['detail_sessions' if shift_ids else 'detail_people'].format(
        sessions=sessions, people=people)
    sentence = None
    if missing and len(missing) == 1:
        fact = missing[0]
        detail_line = FIX_LIST_WORDS['detail_missing'].format(sessions=sessions, have=fact['have'], need=fact['need'],
                                                            who=plural(fact['who_label'], fact['need']))
        if where and template.get('missing'):
            amount = fact['need'] - fact['have']
            text = (FIX_LIST_WORDS['one_missing'].format(ordinal=ordinal(fact['have'] + 1), who=fact['who_label'])
                    if amount == 1 else FIX_LIST_WORDS['many_missing'].format(count=amount, who=plural(fact['who_label'], amount)))
            sentence = template['missing'].format(where=where['label'], missing=text)
        diagnoses = {(row.get('cause') or {}).get('code') for row in evidence}
        if where and diagnoses <= {'no_eligible_people', 'empty_population'} and template.get('eligibility'):
            sentence = template['eligibility'].format(who_label=fact['who_label'], where=where['label'])
    if where and len(person_ids) == 1 and template.get('supervision') and any(
            row['code'] == 'SUPERVISION_PAIRING' and not (row.get('detail') or {}).get('supervisor_person_id') for row in evidence):
        person = context.get('people', {}).get(next(iter(person_ids)))
        if person:
            sentence = template['supervision'].format(person=person, where=where['label'])
    if level == 'could' and not check.get('scope') and len(person_ids) == 1 and template.get('workload'):
        pid = next(iter(person_ids))
        rules = {row.get('rule_id') for row in evidence}
        for goal in check.get('goals', []):
            if (goal.get('policy_id') not in rules or goal.get('metric') != 'duties'
                    or not goal.get('audience_label') or goal.get('history')):
                continue
            rows = [row for row in goal.get('people', [])
                    if isinstance(row, dict) and row.get('person_id') is not None and row.get('actual') is not None]
            own = next((row for row in rows if row.get('person_id') == pid), None)
            values = Counter(row.get('actual') for row in rows if row.get('person_id') != pid)
            if not own or not values:
                continue
            typical, count = values.most_common(1)[0]
            if count <= sum(values.values()) / 2:
                continue  # A median is not evidence that "most" colleagues have that workload.
            sentence = template['workload'].format(person=context['people'].get(pid, pid), actual=own['actual'],
                                                   group=goal['audience_label'], typical=typical)
            break
    if where:
        where = {**where, 'label': session_words(where['label'])}
    return {'where': where, 'missing': missing, 'detail_line': session_words(detail_line)}, session_words(sentence)


def ordered_fixes(action, scope, evidence, *, safety, session_locks):
    """Order navigation/repair choices using existing potential-cover counts, without candidate evaluation."""
    eligible = [(row.get('cause') or {}).get('eligible_people') for row in evidence]
    eligible = [count for count in eligible if count is not None]
    count = min(eligible) if eligible else None
    shifts = scope['shift_ids']
    unlocked = [sid for sid in shifts if not session_locks.get(sid)]
    fixes = []
    def add(kind, *, chosen_scope=scope, available=True, reason=None):
        if (kind == 'adjust_rota' and safety) or any(f['kind'] == kind for f in fixes):
            return
        fixes.append({'kind': kind, 'scope': chosen_scope, 'available': available, 'unavailable_reason': reason})
    codes = {row['code'] for row in evidence}
    supervision = bool(codes & {'SUPERVISION_GAP', 'SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING', 'SUPERVISION_LEVEL'})
    cover = any((row.get('cause') or {}).get('eligible_people_basis') == 'potential_cover_known_records' for row in evidence)
    if scope.get('classification') == DUTIES_DURING_APPROVED_LEAVE:
        add('choose_cover')
    if action == 'open_line':
        add('open_line')
    # A lead gap is a role decision on an existing seat.  It must open the
    # lead picker even when the session has no seat vacancy for ``assign``.
    if codes == {'LEAD_GAP'}:
        add('choose_cover')
    elif shifts and unlocked and (cover or supervision) and count is not None and count > 0:
        sid = unlocked[0]
        key = next((row['finding_key'] for row in evidence if sid in row['shift_ids']), None)
        add('assign_supervisor' if supervision else 'assign', chosen_scope={**scope, 'shift_id': sid, 'finding_key': key})
    if count == 0 and (cover or supervision):
        add('open_eligibility')
    if count != 0 or not (cover or supervision):
        add('clash' if codes == {'TIME_OVERLAP'} else action)
    if scope['rule_lines']:
        add('open_line')
    if shifts and (cover or supervision) and (count == 0 or not unlocked):
        sid = shifts[0]
        key = next((row['finding_key'] for row in evidence if sid in row['shift_ids']), None)
        add('assign_supervisor' if supervision else 'assign',
            chosen_scope={**scope, 'shift_id': sid, 'finding_key': key}, available=False,
            reason=FIX_LIST_WORDS['locked' if shifts and not unlocked else 'no_candidates'])
    if not fixes:
        add('open_line' if scope['rule_lines'] else 'clash', available=False,
            reason='No safe inline action is available for this finding')
    return fixes[:4], count


def improvement_group(kind):
    key = FIX_ACTION_GROUPS[kind]
    return {'key': key, 'label': FIX_GROUP_LABELS[key]}


def candidate_reason_short(candidate, quota_rows=(), *, timezone='UTC', types=None):
    """Use the already computed candidate verdict/workload. No extra checks, history reads or SQL."""
    findings = sorted(candidate.get('findings') or [], key=lambda row: row.get('severity') != 'error')
    for finding in findings:
        detail = finding.get('detail') or {}
        if finding['code'] == 'CONSECUTIVE_WEEKENDS' and detail.get('weekends'):
            return FIX_LIST_WORDS['weekend_run'].format(ordinal=ordinal(detail['weekends']))
        if detail.get('actual') is not None and detail.get('amount') is not None:
            return workload_reason(detail, finding.get('start'), finding.get('end'), timezone, types)
        if finding.get('message'):
            return finding['message'][:160]
    for row in quota_rows:
        if row.get('person_id') == candidate['person_id'] and row.get('actual') is not None and row.get('amount') is not None:
            return workload_reason(row, row.get('starts_on'), row.get('ends_on'), timezone, types)
    return candidate.get('reason_short') or FIX_LIST_WORDS['week_hours'].format(actual=candidate.get('week_hours') or 0)


def workload_reason(detail, start, end, timezone, types):
    from packages.contracts.schemas import FAIRNESS_METRIC_LABELS
    from datetime import date
    period = FIX_LIST_WORDS['in_window']
    if start and end:
        a = aware(start).astimezone(ZoneInfo(timezone)).date() if 'T' in str(start) else date.fromisoformat(str(start))
        b = aware(end).astimezone(ZoneInfo(timezone)).date() if 'T' in str(end) else date.fromisoformat(str(end))
        if a.day == b.day == 1 and (b.year * 12 + b.month) - (a.year * 12 + a.month) == 1:
            period = FIX_LIST_WORDS['this_month']
        elif (b - a).days == 7:
            period = FIX_LIST_WORDS['this_week']
    metric = detail.get('metric')
    unit = FAIRNESS_METRIC_LABELS.get(metric, metric.replace('_', ' ') if metric else 'duties').lower()
    ids = detail.get('assignment_type_ids') or []
    if metric == 'duties' and len(ids) == 1 and (types or {}).get(ids[0], {}).get('name'):
        unit = plural(types[ids[0]]['name'], detail['amount'])
    return FIX_LIST_WORDS['workload'].format(actual=detail['actual'], amount=detail['amount'], unit=unit, period=period)
