"""Plain-language summary of an automatic schedule, built from the canonical validation.

Coverage in places, why any place stays open (who could not be used and which rule stopped them, by person),
advisory rules the proposal bends, and how evenly the work is spread. Private absence reasons are
never included: an absence is only "unavailable" (reason kind LEAVE).
"""

from .recurrence import inactive
from collections import Counter, defaultdict

from .conditions import counting_key
from .generation import duty_labels
from .patterns import wish_outcomes
from .time import aware
from .validation import evaluate, finding_observations
from .rota_summary import rota_summary, with_fix_list_context

NOT_USABLE = {'UNAVAILABLE': 'unavailable', 'EXTERNAL_ROTATION': 'away on rotation',
              'RESTRICTION': 'restricted by a rule', 'PERSON_INACTIVE': 'marked inactive',
              'MEMBERSHIP_MISSING': 'without current department membership',
              'RANK_NOT_ALLOWED': 'without an allowed rank for this duty',
              'ELIGIBILITY_MISSING': 'missing clinic eligibility',
               'CAPABILITY_NOT_GRANTED': 'missing a required skill',
               'OVERTIME_NOT_ALLOWED': 'not allowed overtime on this duty'}
FINDING_NAMES = {'TIME_OVERLAP': 'already working then', 'MIN_REST': 'minimum rest', 'MAX_HOURS': 'hours limit',
                 'MAX_DUTIES': 'duty limit', 'CONSECUTIVE_DAYS': 'consecutive days limit',
                 'CONSECUTIVE_WEEKENDS': 'consecutive weekends limit', 'FORBIDDEN_TRANSITION': 'next-day combination',
                 'TRAVEL_BUFFER': 'travel time', 'QUOTA_ABOVE_MAXIMUM': 'quota maximum',
                 'COVERAGE_TARGET': 'desired cover not reached', 'SUPERVISION_PAIRING': 'supervision',
                 'SUPERVISION_GAP': 'supervision', 'SUPERVISION_LEVEL': 'supervision', 'LEAD_GAP': 'team lead', 'LEAD_NOT_QUALIFIED': 'team lead'}


# One vocabulary for why a person cannot take a place: the planner's fill panel and the auto-schedule summary both tag
# reasons with these kinds (labels live once in the web client's findings.ts). Anything unlisted is a named rule.
REASON_KIND = {'UNAVAILABLE': 'LEAVE', 'UNAVAILABLE_LEAVE': 'LEAVE', 'EXTERNAL_ROTATION': 'AWAY', 'TIME_OVERLAP': 'CLASH',
               'MIN_REST': 'REST', 'TRAVEL_BUFFER': 'REST', 'FORBIDDEN_TRANSITION': 'REST', 'MAX_HOURS': 'HOURS',
               'MAX_DUTIES': 'HOURS', 'CONSECUTIVE_DAYS': 'HOURS', 'CONSECUTIVE_WEEKENDS': 'HOURS',
               'QUOTA_ABOVE_MAXIMUM': 'HOURS', 'NOT_ELIGIBLE': 'NOT ELIGIBLE', 'PERSON_INACTIVE': 'NOT ELIGIBLE',
               'MEMBERSHIP_MISSING': 'NOT ELIGIBLE', 'RANK_NOT_ALLOWED': 'NOT ELIGIBLE', 'ELIGIBILITY_MISSING': 'NOT ELIGIBLE',
                'OVERTIME_NOT_ALLOWED': 'NOT ELIGIBLE',
               'RESTRICTION': 'PROTECTED', 'SUPERVISOR_REQUIRED': 'SUPERVISOR', 'SUPERVISION_PAIRING': 'SUPERVISOR', 'SUPERVISION_LEVEL': 'SUPERVISOR'}
# The planner's Why tab sorts every person into one group (assigned people are added by the caller). An absence is only
# "unavailable": its private reason is never part of a finding.
WHY_GROUPS = ('assigned', 'free', 'rule', 'unavailable', 'not_eligible')


def why_group(row):
    """A fill-options row's group: free (no mandatory rule stops them), not eligible, unavailable (leave, rotation or
    another session at the time) or blocked by a rule."""
    if row['status'] != 'blocked':
        return 'free'
    kinds = {f['kind'] for f in row['findings'] if f['severity'] == 'error'}
    return 'not_eligible' if 'NOT ELIGIBLE' in kinds else 'unavailable' if kinds & {'LEAVE', 'AWAY', 'CLASH'} else 'rule'


# Closest options first: free people, then people a rule stops, then absences; ineligible people are only counted.
_REASON_ORDER = {'FREE': 0, 'LEAVE': 3, 'AWAY': 3, 'PROTECTED': 2}


def _people(n, adjective=''):
    return f"{n} {adjective}{'person' if n == 1 else 'people'}"


def summarise(data, validation, evidence, editable, *, diagnostics=(), explain_limit=12, trial_limit=30, people_limit=20):
    """`data` holds the proposed assignments; `evidence` is the solver's per-candidate eligibility evidence
    for the sessions it could change (`editable`). Each open place lists up to `people_limit` people with the reason
    they were not used (`kind` from REASON_KIND, or FREE: no rule stops them, they are needed elsewhere)."""
    check = data.get('_check_budget', lambda: None)
    check()
    focus = set(data['focus_ids'])
    shifts = {s['id']: s for s in data['shifts'] if not inactive(s) and s['id'] in focus}
    policy_names = {p['id']: p['name'] for p in data['policies']}
    reasons = {}
    decisive = set()
    proven_places = defaultdict(dict)

    def reason(kind, code, message, sid, rule_id=None, person_id=None, *, proven=False, places=None):
        key = kind, code, rule_id, person_id, message
        row, sessions = reasons.setdefault(key, ({'kind': kind, 'code': code, 'message': message,
                                                  'rule_id': rule_id, 'person_id': person_id}, set()))
        sessions.add(sid)
        if proven:
            decisive.add(key)
        if places is not None:
            proven_places[key][sid] = max(places, proven_places[key].get(sid, 0))

    def rule_name(finding):
        return policy_names.get(finding.get('rule_id')) or FINDING_NAMES.get(finding['code']) or finding['message']

    def exclusion_message(exclusion):
        code = exclusion['code']
        label = NOT_USABLE.get(code) or FINDING_NAMES.get(code) or 'not eligible for this session'
        if code == 'ELIGIBILITY_MISSING':
            service = data.get('services', {}).get(exclusion.get('service_id'), {}).get('name')
            return label + (f' for {service}' if service else '')
        if code == 'RANK_NOT_ALLOWED':
            ranks = [data.get('categories', {}).get(cid, {}).get('name')
                     for cid in exclusion.get('allowed_category_ids', [])]
            return label + (' (allowed: ' + ', '.join(rank for rank in ranks if rank) + ')' if any(ranks) else '')
        if code == 'CAPABILITY_NOT_GRANTED':
            capability = data.get('capabilities', {}).get(exclusion.get('capability_id'), {}).get('name')
            return f'missing required skill: {capability}' if capability else label
        if code == 'RESTRICTION' and policy_names.get(exclusion.get('rule_id')):
            return 'restricted by ' + policy_names[exclusion['rule_id']]
        return label

    validation = with_fix_list_context(validation, data)
    summary = rota_summary(validation)
    required, filled = summary['places_needed'], summary['places_filled']
    short = [(shifts[s['shift_id']]['start'], s['shift_id'], s['places_needed'], s['places_open'])
             for s in summary['sessions'] if s['shift_id'] in shifts and s['places_open']]
    extra_cover_by_shift = {s['shift_id']: s['extra_cover_places_open'] for s in summary['sessions']
                           if s['shift_id'] in shifts and s['extra_cover_places_open']}
    blocking_short = [row for row in short if row[1] not in extra_cover_by_shift]

    short_ids = {sid for _, sid, _, _ in short}
    open_by_shift = {sid: missing for _, sid, _, missing in short}
    evidence_by_shift = defaultdict(list)
    people_exclusions = {}
    for row in evidence:
        evidence_by_shift[row['shift_id']].append(row)
        if row['shift_id'] in editable:
            codes = {e['code'] for e in row['exclusions']} & {'PERSON_INACTIVE', 'MEMBERSHIP_MISSING'}
            pid = row['person_id']
            people_exclusions[pid] = people_exclusions[pid] & codes if pid in people_exclusions else codes
    excluded = defaultdict(set)
    for pid, codes in people_exclusions.items():
        for code in codes:
            excluded[code].add(pid)
    for row in evidence:
        if row['shift_id'] not in short_ids or shifts[row['shift_id']]['locked']:
            continue
        for exclusion in row['exclusions']:
            code, rid = exclusion['code'], exclusion.get('rule_id')
            if row['person_id'] in excluded.get(code, ()):
                continue
            kind = 'rule' if rid else 'eligibility' if REASON_KIND.get(code, 'NOT ELIGIBLE') == 'NOT ELIGIBLE' else 'person'
            message = policy_names.get(rid) or exclusion_message(exclusion)
            reason(kind, code, message, row['shift_id'], rid, row['person_id'] if kind == 'person' else None)
    shortages_by_shift = defaultdict(list)
    proven_shortages = defaultdict(int)
    for diagnostic in diagnostics:
        sid = diagnostic.get('shift_id')
        if sid in short_ids and not shifts[sid]['locked']:
            shortages_by_shift[sid].append(diagnostic['message'])
            proven = diagnostic.get('proven') is True
            places = (min(open_by_shift[sid], diagnostic['required'] - diagnostic['available_upper_bound'])
                      if proven and diagnostic.get('proof_kind') == 'necessary_condition' else None)
            reason('eligibility', diagnostic['code'], diagnostic['message'], sid, proven=proven, places=places)
            if (sid in editable and diagnostic['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES'
                    and diagnostic.get('proven') is True and diagnostic.get('proof_kind') == 'necessary_condition'):
                # Intervals overlap: their headcount deficits are lower bounds, not separate places to add.
                proven_shortages[sid] = max(proven_shortages[sid],
                                           diagnostic['required'] - diagnostic['available_upper_bound'])
        cited = {r['shift_id'] for r in diagnostic.get('requirements', []) if r['kind'] == 'duty'} & short_ids
        for requirement in diagnostic.get('requirements', []):
            if requirement['kind'] == 'policy':
                for sid in cited or {None}:
                    reason('rule', diagnostic['code'], requirement['name'], sid, requirement['rule_id'],
                           proven=diagnostic.get('proven') is True)
    live = [s for s in data['shifts'] if not inactive(s)]
    duties = defaultdict(list)
    for shift in live:
        for p in shift['participants']:
            duties[p['person_id']].append(shift)

    def blocking(shift, pid):
        """Mandatory rules that adding this person would break (their own duties and neighbours only)."""
        start, end = aware(shift['start']), aware(shift['end'])
        mine = sorted(duties[pid], key=lambda s: aware(s['start']))
        own = {s['id'] for s in mine}
        before = [s['id'] for s in mine if aware(s['start']) < start][-1:]
        after = [s['id'] for s in mine if aware(s['start']) > start][:1]
        keep = [s for s in live if s['id'] == shift['id'] or s['id'] in own
                or aware(s['start']) < end and start < aware(s['end'])]
        reduced = {**data, 'focus_ids': [shift['id'], *before, *after], 'quota_subjects': {pid},
                   'person_rule_subjects': {pid}, 'skip_coverage': True}
        candidate = {'id': 'summary-' + pid, 'shift_id': shift['id'], 'person_id': pid, 'start': shift['start'],
                     'end': shift['end'], 'participation': 'staff',
                     'overtime': shift['type_snapshot']['definition'].get('extra_cover', False), 'revision': 1}

        def errors(extra):
            result = evaluate({**reduced, 'shifts': [dict(s, participants=s['participants'] + extra)
                                                     if s['id'] == shift['id'] else s for s in keep]},
                              explain_findings=False)
            return {(f['code'], f['rule_id'], f.get('start'), f.get('end'), f.get('shift_id'),
                     (f.get('detail') or {}).get('line'), (f.get('detail') or {}).get('slot'),
                     counting_key(f.get('detail'))): f for f in finding_observations(result)
                    if f['person_id'] == pid and f['severity'] == 'error'}
        existing = errors([])
        return [f for key, f in errors([candidate]).items() if key not in existing]

    open_places = []
    for start, sid, need, missing in sorted(short)[:explain_limit]:
        check()
        shift = shifts[sid]
        assigned = {p['person_id'] for p in shift['participants']}
        if shift['locked']:
            message = f"This session is locked with {need - missing} of {_people(need)}. Unlock it or add someone by hand."
            open_places.append({'shift_id': sid, 'start': shift['start'], 'end': shift['end'], **duty_labels(data, shift),
                                'required': need, 'open': missing, 'unusable': {}, 'exclusion_counts': [], 'people': [], 'blocked_by': [],
                                'message': message})
            continue
        unusable, blocked, unexplained, tried, people = Counter(), Counter(), 0, 0, []
        exclusions = {}
        for row in evidence_by_shift[sid]:
            check()
            pid = row['person_id']
            if pid in assigned:
                continue
            if row['exclusions']:
                for item in row['exclusions']:
                    key = (item['code'], item.get('service_id'), item.get('rule_id'))
                    exclusions.setdefault(key, set()).add(pid)
                exclusion = row['exclusions'][0]
                code = exclusion['code']
                unusable[exclusion_message(exclusion)] += 1
                if REASON_KIND.get(code, 'NOT ELIGIBLE') != 'NOT ELIGIBLE':
                    people.append({'person_id': pid, 'kind': REASON_KIND[code], 'code': code, 'rule_id': None, 'rule': None})
                continue
            if tried >= trial_limit:
                unexplained += 1
                continue
            tried += 1
            found = sorted(blocking(shift, pid), key=lambda f: (f['code'], f['rule_id'] or ''))
            names = sorted({rule_name(f) for f in found})
            if names:
                blocked.update(names)
                for finding in found:
                    kind = 'person' if not finding['rule_id'] and REASON_KIND.get(finding['code']) in {'LEAVE', 'AWAY', 'CLASH'} else 'rule'
                    reason(kind, finding['code'], rule_name(finding), sid, finding['rule_id'], pid if kind == 'person' else None)
                first = found[0]
                kind = REASON_KIND.get(first['code'], 'RULE')
                # The kind already says rest, limit, clash...: name the rule only when it is a configured one (or unnamed).
                people.append({'person_id': pid, 'kind': kind, 'code': first['code'], 'rule_id': first['rule_id'],
                               'rule': policy_names.get(first['rule_id']) or (rule_name(first) if kind == 'RULE' else None)})
            else:
                unexplained += 1
                people.append({'person_id': pid, 'kind': 'FREE', 'code': None, 'rule_id': None, 'rule': None})
        people.sort(key=lambda p: _REASON_ORDER.get(p['kind'], 1))
        parts = [] if sid in editable else ['this session is locked or outside the sessions being scheduled']
        parts += shortages_by_shift[sid]
        parts += [_people(n) + ' ' + label for label, n in unusable.most_common()]
        if blocked:
            parts.append('blocked by ' + ', '.join(f'{name} ({n})' for name, n in blocked.most_common()))
        if unexplained and not shortages_by_shift[sid]:
            parts.append(_people(unexplained, 'other eligible ') + ' needed elsewhere or for supervision')
        open_places.append({'shift_id': sid, 'start': shift['start'], 'end': shift['end'], **duty_labels(data, shift),
                            'required': need, 'open': missing, 'unusable': dict(unusable), 'people': people[:people_limit],
                            'exclusion_counts': [{'code': code, 'service_id': service, 'rule_id': rule, 'people': len(ids)}
                                for (code, service, rule), ids in exclusions.items()],
                            'blocked_by': [{'name': name, 'people': n} for name, n in blocked.most_common()],
                            'message': f"{missing} of {need} place{'s' if need != 1 else ''} open"
                                       + (': ' + '; '.join(parts) if parts else '') + '.'})

    advisory = Counter(rule_name(f) for f in validation['findings'] if f['severity'] == 'warning')
    open_extra_cover = [row for row in open_places if row['shift_id'] in extra_cover_by_shift]
    open_places = [row for row in open_places if row['shift_id'] not in extra_cover_by_shift]
    # Staff wishes with days in this rota, fully granted or not (one pass over the proposal).
    wishes = wish_outcomes(data)
    hours, counts = defaultdict(float), Counter()
    for shift in shifts.values():
        for p in shift['participants']:
            hours[p['person_id']] += (aware(p['end']) - aware(p['start'])).total_seconds() / 3600
            counts[p['person_id']] += 1
    spread = None
    if counts:
        values = list(hours.values())
        spread = {'people': len(counts), 'hours_min': round(min(values), 1), 'hours_max': round(max(values), 1),
                  'hours_mean': round(sum(values) / len(values), 1),
                  'duties_min': min(counts.values()), 'duties_max': max(counts.values())}
    for _, sid, need, missing in short:
        if shifts[sid]['locked']:
            reason('session', 'LOCKED_SESSION',
                   f"This session is locked with {need - missing} of {_people(need)}. Unlock it or add someone by hand.", sid)
    ranked = [(key in decisive, {**row, 'session_count': len(sessions - {None}),
               'places_count': sum(proven_places[key].get(sid, open_by_shift.get(sid, 0)) for sid in sessions)})
              for key, (row, sessions) in reasons.items()]
    ranked.sort(key=lambda item: (-item[0], -item[1]['places_count'], -item[1]['session_count'],
                                 item[1]['kind'], item[1]['message'], item[1]['rule_id'] or '', item[1]['person_id'] or ''))
    exclusion_labels = {'PERSON_INACTIVE': 'marked inactive', 'MEMBERSHIP_MISSING': 'no department membership'}
    return {'required_places': required, 'filled_places': filled, 'rota_summary': summary,
            'coverage_percent': round(100 * filled / required, 1) if required else 100.0,
            'open_places': open_places, 'open_place_count': len(blocking_short),
            'open_places_count': required - filled - sum(extra_cover_by_shift.values()),
            'open_extra_cover': open_extra_cover,
            'open_extra_cover_places_count': sum(extra_cover_by_shift.values()),
            'open_extra_cover_session_count': len(extra_cover_by_shift),
            'proven_open_places': sum(min(missing, proven_shortages[sid]) for _, sid, _, missing in blocking_short),
            'sessions_affected': len(blocking_short), 'top_reasons': [row for _, row in ranked[:5]],
            'excluded_people': [{'code': code, 'people_count': len(people),
                                 'message': f'{_people(len(people))} excluded: {exclusion_labels[code]}'}
                                for code, people in sorted(excluded.items())],
            'advisory': [{'name': name, 'count': n} for name, n in advisory.most_common()],
            'workload': spread, 'wishes': {'met': sum(share == 1 for share, _ in wishes.values()), 'total': len(wishes)}}
