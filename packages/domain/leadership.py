"""One dated interpretation of the named leader role, including frozen legacy inputs."""
from datetime import date

from .conditions import active_rules, change_points, scope_matches_throughout, scope_segments
from .time import aware, local_midnight
from .participation import presence_intervals


def department_id(data, shift):
    return (shift.get('department_id') or data.get('types', {}).get(shift['assignment_type_id'], {}).get('department_id')
            or data.get('department', {}).get('id'))


def source(requirement):
    policy = requirement.get('policy')
    if policy:
        line = policy.get('template_line') or {}
        return {'policy_id': policy['id'], 'revision': policy.get('revision', 1),
                'line_id': line.get('id'), 'line_index': line.get('line'), 'slot': line.get('slot'),
                **requirement.get('origin', {})}
    return requirement['origin']


def _requirement_key(requirement):
    """Stable source identity for one catalogue line across matching duties."""
    policy = requirement.get('policy')
    if policy:
        line = policy.get('template_line') or {}
        return ('policy', policy.get('id'), policy.get('revision', 1), line.get('id'),
                line.get('line'), line.get('slot'))
    legacy = requirement.get('origin') or {}
    return ('legacy', legacy.get('legacy_type_id'), legacy.get('legacy_type_revision'))


def _concurrent(requirement):
    return requirement.get('count_scope', 'duty') == 'concurrent_duties'


def concurrent_groups(data, shift, a=None, b=None):
    """Return shared line obligations as ``{start, end, requirement, duties}`` rows.

    A group is formed only from the same policy line and matching duty scope. Its
    bounds are swept from all duty and requirement boundaries, so staggered and
    partial overlaps are evaluated independently. The current duty is always in
    its own group; callers deduplicate rows by ``key/start/end``.
    """
    a, b = aware(a or shift['start']), aware(b or shift['end'])
    base = [r for r in requirements(data, shift, a, b) if _concurrent(r)]
    if not base:
        return []
    all_shifts = [s for s in data.get('shifts', ()) if s.get('id')]
    from .validation import coverage_boundaries, coverage_boundary_inputs
    boundary_inputs = coverage_boundary_inputs(data)
    rows = []
    seen = set()
    for requirement in base:
        key = _requirement_key(requirement)
        matches = []
        for other in all_shifts:
            oa, ob = aware(other['start']), aware(other['end'])
            lo, hi = max(a, oa), min(b, ob)
            if lo >= hi:
                continue
            other_req = next((r for r in requirements(data, other, lo, hi)
                              if _concurrent(r) and _requirement_key(r) == key), None)
            if other_req is not None:
                matches.append((other, other_req, max(lo, other_req['start']), min(hi, other_req['end'])))
        if not matches:
            continue
        points = {a, b}
        for other, req, lo, hi in matches:
            points.update((lo, hi, aware(other['start']), aware(other['end'])))
            points.update(coverage_boundaries(data, other, a, b, inputs=boundary_inputs))
            for participant in other.get('participants', ()):
                points.update(t for t in (aware(participant['start']), aware(participant['end'])) if a < t < b)
        points = sorted(t for t in points if a <= t <= b)
        for lo, hi in zip(points, points[1:]):
            duties = [(other, req) for other, req, x, y in matches if x <= lo and hi <= y]
            if not duties:
                continue
            marker = (key, lo, hi)
            if marker in seen:
                continue
            seen.add(marker)
            rows.append({'start': lo, 'end': hi, 'key': key, 'requirement': requirement, 'duties': duties})
    return rows


def is_group_anchor(data, shift, requirement, a, b):
    """Only the first duty reports a shared finding for a group interval."""
    key = _requirement_key(requirement)
    peers = []
    for candidate in data.get('shifts', ()):
        lo, hi = max(aware(a), aware(candidate['start'])), min(aware(b), aware(candidate['end']))
        if lo < hi and any(_requirement_key(r) == key and _concurrent(r)
                           for r in requirements(data, candidate, lo, hi)):
            peers.append(candidate)
    return not peers or shift['id'] == min(s['id'] for s in peers)


def lead_turn_pairs(data, shifts=None):
    """Return flagged assignments counted as one lead turn per shared line/person."""
    shifts = list(data.get('shifts', ()) if shifts is None else shifts)
    counted = {(shift['id'], p['person_id']) for shift in shifts for p in shift.get('participants', ())
               if p.get('team_lead')}
    if not counted:
        return counted
    # Resolve the same immutable rule set once for the whole ledger, not once per duty.
    data = {**data, 'shifts': shifts}
    prepare(data)
    seen = set()
    for shift in shifts:
        for group in concurrent_groups(data, shift):
            marker = (group['key'], group['start'], group['end'])
            if marker in seen:
                continue
            seen.add(marker)
            leaders = {}
            for duty, _ in group['duties']:
                for p in duty.get('participants', ()):
                    if p.get('team_lead'):
                        leaders.setdefault(p['person_id'], []).append(duty['id'])
            for pid, duty_ids in leaders.items():
                for sid in duty_ids[1:]:
                    counted.discard((sid, pid))
    return counted


def _windows(data, policy, shift, a, b):
    from .validation import session_scope, scope_intervals, within_dates
    scope = session_scope(policy['definition']['scope'])
    if policy.get('department_id') and department_id(data, shift) != policy['department_id']:
        return []
    out = []
    for lo, hi in scope_intervals(data, scope, None, a, b):
        # Local dates and nested clock predicates can change within the window.
        points = {lo, hi}
        for field in ('starts_on', 'ends_on'):
            if policy.get(field):
                point = local_midnight(date.fromisoformat(str(policy[field])), data['organisation']['timezone'])
                if lo < point < hi:
                    points.add(point)
        for x, y in zip(sorted(points), sorted(points)[1:]):
            if within_dates(policy, x, y, data['organisation']['timezone']):
                out.extend((u, v) for u, v, matched in scope_segments(data, scope, None, shift, x, y) if matched)
    return out


def _policies(data):
    if '_leadership_policies' in data:
        return data['_leadership_policies']
    return [p for p in data.get('_active_policies', data.get('policies', []))
            if p['definition']['rule']['kind'] == 'team_lead_requirement'] if '_active_policies' in data else [
        p for p in active_rules(data) if p['definition']['rule']['kind'] == 'team_lead_requirement']


def prepare(data):
    """Opt in only on an immutable request/run snapshot; participant-only trials may share this preparation."""
    if '_person_rows' not in data:
        from .validation import index_person_rows
        data['_person_rows'] = index_person_rows(data)
    data.setdefault('_person_dates_cache', {})
    data.setdefault('_scope_interval_cache', {})
    data.pop('_leadership_policies', None)
    data['_leadership_policies'] = _policies(data)
    data['_leadership_cache'] = {}


def requirements(data, shift, a, b):
    a, b = aware(a), aware(b)
    cache = data.get('_leadership_cache')
    if cache is None:
        return _requirements(data, shift, a, b)
    start, end = aware(shift['start']), aware(shift['end'])
    key = shift['id'], start, end, id(shift['type_snapshot'])
    if key not in cache:
        cache[key] = _requirements(data, shift, start, end)
    return [{**r, 'start': max(a, r['start']), 'end': min(b, r['end'])}
            for r in cache[key] if a < r['end'] and r['start'] < b]


def _requirements(data, shift, a, b):
    """Duty-side obligations, never filtered by whether any person qualifies."""
    from .validation import effective_type_definition
    a, b = aware(a), aware(b)
    definition = effective_type_definition(data, shift)
    policies = _policies(data)
    if definition.get('purpose', 'staffed') != 'staffed' or not (definition.get('lead') or policies):
        return []
    receipt = next((row for row in data.get('type_lead_ownership', [])
                    if row['assignment_type_id'] == shift['assignment_type_id']
                    and row['department_id'] == department_id(data, shift)), None)
    cutover = local_midnight(date.fromisoformat(str(receipt['effective_on'])), data['organisation']['timezone']) if receipt else b
    result = []
    lead = definition.get('lead')
    if lead is not None and not {'minimum', 'maximum', 'capability_ids', 'category_ids'} <= lead.keys():
        from packages.contracts.schemas import TeamLead
        lead = TeamLead.model_validate(lead).model_dump(mode='json')
    if lead and a < min(b, cutover):
        result.append({'start': a, 'end': min(b, cutover), 'minimum': lead['minimum'], 'maximum': lead['maximum'],
                       'continuity': 'continuous', 'legacy': lead,
                       'origin': {'legacy_type_id': shift['assignment_type_id'],
                                  'legacy_type_revision': shift.get('type_revision', shift['type_snapshot'].get('revision', 1))}})
    for policy in policies:
        rule = policy['definition']['rule']
        for lo, hi in _windows(data, policy, shift, a, b):
            origin = {}
            if receipt:
                # Until the explicit transfer date, the legacy type remains the sole owner.
                lo = max(lo, cutover)
                origin = {'legacy_type_id': receipt['assignment_type_id'],
                          'legacy_type_revision': receipt['source_type_revision'], 'ownership_id': receipt['id']}
            elif lead:
                # Admission requires conversion of every legacy type before new Rules can own it.
                continue
            if lo < hi:
                result.append({'start': lo, 'end': hi, **rule, 'policy': policy, 'origin': origin})
    return result


def segments(data, shift, a, b, *, people=()):
    a, b = aware(a), aware(b)
    rules = requirements(data, shift, a, b)
    if not rules:
        return [(a, b, [])]
    points = {a, b, *(t for r in rules for t in (r['start'], r['end']))}
    for pid in people:
        points.update(change_points(data, pid, a, b))
    points = sorted(points)
    return [(lo, hi, [r for r in rules if r['start'] <= lo and hi <= r['end']])
            for lo, hi in zip(points, points[1:])]


def matches(data, pid, shift, requirement, a, b):
    from .validation import lead_qualified
    if 'legacy' in requirement:
        return lead_qualified(data, pid, shift, requirement['legacy'], a, b)
    return scope_matches_throughout(data, requirement['policy']['definition']['scope'], pid, shift, a, b)


def qualified(data, pid, shift, a, b):
    rows = segments(data, shift, a, b, people=[pid])
    return any(rules for _, _, rules in rows) and all(
        matches(data, pid, shift, r, lo, hi) for lo, hi, rules in rows for r in rules)


def coverage_qualified(data, pid, shift, a, b):
    """Whole-duty cover requires Who across the duty, even for one observed interval."""
    lead = summary(data, shift, a, b)
    return bool(lead and qualified(data, pid, shift, a, b)
                and (lead['continuity'] != 'whole_duty'
                     or qualified(data, pid, shift, aware(shift['start']), aware(shift['end']))))


def summary(data, shift, a=None, b=None):
    """Compatibility-shaped aggregate; numeric consumers request one constant segment."""
    a, b = aware(a or shift['start']), aware(b or shift['end'])
    rules = requirements(data, shift, a, b)
    if not rules:
        return None
    maximum = [r['maximum'] for r in rules if r['maximum'] is not None]
    return {'minimum': max(r['minimum'] for r in rules), 'maximum': min(maximum) if maximum else None,
            'continuity': 'whole_duty' if any(r['continuity'] == 'whole_duty' for r in rules) else 'continuous',
            'capability_ids': [], 'category_ids': [], '_requirements': rules,
            'sources': [source(r) for r in rules]}


def covers_required(data, participant, shift):
    """Whole-duty leaders are the same flagged people over every required interval."""
    intervals = presence_intervals(participant)
    return all(any(a <= r['start'] and r['end'] <= b for a, b in intervals)
               for r in requirements(data, shift, aware(shift['start']), aware(shift['end']))
               if r['continuity'] == 'whole_duty')


def counts(data, shift, a, b, *, available=None, scheduled=None):
    """The shared numeric population: qualified available minima, all factual flags for maxima."""
    from .validation import rule_coverage_people
    a, b = aware(a), aware(b)
    lead = summary(data, shift, a, b)
    if not lead:
        return None
    available = rule_coverage_people(data, shift, a, b) if available is None else available
    scheduled = [p for p in shift['participants'] if any(lo <= a and b <= hi for lo, hi in presence_intervals(p))] if scheduled is None else scheduled
    start, end = aware(shift['start']), aware(shift['end'])
    whole = lead['continuity'] == 'whole_duty'
    count = sum(coverage_qualified(data, p['person_id'], shift, a, b)
                and (not whole or covers_required(data, p, shift))
                for p in available if p.get('team_lead'))
    flagged = [p for p in shift['participants'] if p.get('team_lead') and any(
        lo < r['end'] and r['start'] < hi for lo, hi in presence_intervals(p)
        for r in requirements(data, shift, start, end))] if whole else [p for p in scheduled if p.get('team_lead')]
    lead['available'], lead['scheduled'] = count, len(flagged)
    concurrent = [r for r in lead['_requirements'] if _concurrent(r)]
    if concurrent:
        # Shared obligations count distinct people over the matching duty set.
        groups = concurrent_groups(data, shift, a, b)
        for group in groups:
            req = group['requirement']
            qualified_named, named_people = set(), set()
            for duty, duty_req in group['duties']:
                for person in duty.get('participants', ()):
                    pid = person['person_id']
                    present = any(x <= group['start'] and group['end'] <= y for x, y in presence_intervals(person))
                    if not present or not person.get('team_lead'):
                        continue
                    named_people.add(pid)
                    if matches(data, pid, duty, duty_req, group['start'], group['end']) \
                            and (req['continuity'] != 'whole_duty' or covers_required(data, person, duty)):
                        qualified_named.add(pid)
            group['available'] = len(qualified_named)
            group['scheduled'] = len(named_people)
        if groups:
            lead['_groups'] = groups
            # Preserve the old scalar shape for callers that only need one duty;
            # validation reads each group's values below.
            if all(_concurrent(r) for r in lead['_requirements']):
                lead['available'] = groups[0]['available']
                lead['scheduled'] = groups[0]['scheduled']
    return lead
