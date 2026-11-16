"""Source-linked candidate exclusions; never an overall feasibility proof."""

from .recurrence import inactive, series_definition
from .time import now as clock_now
from .time import aware, overlaps, local_midnight
from .validation import (_qualification_key, qualification_failures, qualified, within_dates, blocking_restrictions,
                          capabilities_for, categories_for, person_rows, is_open_extra_cover,
                          effective_type_definition)
from datetime import date, timedelta
from collections import Counter


def focused_candidate_people(data, shift):
    """Rule checks cover every person in each active focused session, including fixed work."""
    return data.get('people', {}) if not inactive(shift) and shift['id'] in data.get('focus_ids', ()) else ()


def direct_coverage_shortages(data, evidence, editable):
    """Prove distinct headcount/capability upper bounds, never full feasibility."""
    from .generation_scope import population_allows
    check = data.get('_check_budget')
    indexed = {(row['shift_id'], row['person_id']): row for row in evidence}
    findings = []
    checked_at = clock_now().isoformat()
    rule_names = {p['id']: p['name'] for p in data['policies']}
    for shift in data['shifts']:
        if check is not None:
            check()
        if inactive(shift) or shift['id'] not in data['focus_ids']:
            continue
        sid = shift['id']
        start, end = aware(shift['start']), aware(shift['end'])
        existing = {p['person_id'] for p in shift['participants']}
        candidates = []
        for pid in data['people']:
            if check is not None:
                check()
            row = indexed.get((sid, pid))
            if row is None:
                row = candidate_evidence(data, pid, shift)
            if pid in existing:
                # Retained manual placements supply cover even when automation may not choose them.
                row = {**row, 'exclusions': [r for r in row['exclusions'] if r['code'] != 'OVERTIME_NOT_ALLOWED']}
            reasons = list(row['exclusions'])
            if pid not in existing and not population_allows(data, shift, pid):
                reasons.append({'code': 'OUTSIDE_GENERATION_POPULATION', 'shift_id': sid})
            if sid not in editable and pid not in existing:
                reasons.append({'code': 'DUTY_NOT_EDITABLE', 'shift_id': sid})
            candidates.append({'person_id': pid, 'exclusions': reasons})
        definition = effective_type_definition(data, shift)
        supervision = definition.get('supervision')
        available = [c for c in candidates if not c['exclusions']]
        # A pool containing only whole-duty trainees cannot supervise itself.
        # Keep this necessary condition conservative when ranks change mid-duty.
        if supervision and available and all(
                set(supervision['trainee_category_ids']) & categories_for(data, c['person_id'], start, end)
                for c in available):
            for candidate in available:
                candidate['exclusions'].append({'code': 'SUPERVISION_UNAVAILABLE',
                    'capability_id': supervision['capability_id'],
                    'assignment_type_id': shift['assignment_type_id'], 'type_revision': shift['type_revision']})
        # Optional type floors cannot prove infeasibility: the model may leave this session empty.
        if is_open_extra_cover(definition):
            continue
        for position, requirement in enumerate(definition['requirements']):
            a = start + timedelta(minutes=requirement['from_minute'])
            b = start + timedelta(minutes=requirement['to_minute']) if requirement.get('to_minute') is not None else end
            if not start <= a < b <= end:
                continue
            checks = [(a, b, requirement['minimum'], None, f'definition.requirements.{position}.minimum')]
            # Grants and capabilities can change at local midnight during overnight
            # duties. Check each authority interval instead of intersecting people
            # across the whole duty and falsely declaring a shortage.
            boundaries = {a, b}
            for row in [*data['grants'], *data['capabilities'].values()]:
                for field in ('starts_on', 'ends_on'):
                    if row.get(field):
                        point = local_midnight(date.fromisoformat(row[field]), data['organisation']['timezone'])
                        if a < point < b:
                            boundaries.add(point)
            points = sorted(boundaries)
            for clause_index, clause in enumerate(requirement['capabilities']):
                checks.extend((lo, hi, clause['minimum'], clause['capability_id'],
                    f'definition.requirements.{position}.capabilities.{clause_index}.minimum')
                    for lo, hi in zip(points, points[1:]))
            for lo, hi, minimum, capability, path in checks:
                considered = []
                for candidate in candidates:
                    if check is not None:
                        check()
                    reasons = list(candidate['exclusions'])
                    pid = candidate['person_id']
                    if capability and capability not in capabilities_for(data, pid, shift, lo, hi):
                        reasons.append({'code': 'CAPABILITY_NOT_GRANTED', 'capability_id': capability,
                            'existing_record_ids': [g['id'] for g in data['grants']
                                if g['person_id'] == pid and g['capability_id'] == capability]})
                    considered.append({'person_id': pid, 'exclusions': reasons})
                eligible = [p['person_id'] for p in considered if not p['exclusions']]
                if len(eligible) >= minimum:
                    continue
                label = data['capabilities'][capability]['name'] if capability else None
                missing = minimum - len(eligible)
                message = f"Needs {'at least ' if eligible else ''}{missing} more {'person' if missing == 1 else 'people'}"
                message += (f" with {label}" if label else '') + ' for this interval — '
                message += (f"at most {len(eligible)} eligible {'person' if len(eligible) == 1 else 'people'} could work it"
                            if eligible else 'nobody eligible is free')
                blocker_labels = {'PERSON_INACTIVE': 'inactive staff', 'MEMBERSHIP_MISSING': 'department membership',
                    'RANK_NOT_ALLOWED': 'rank eligibility', 'ELIGIBILITY_MISSING': 'clinic eligibility',
                    'UNAVAILABLE': 'unavailability', 'EXTERNAL_ROTATION': 'rotation away from the department',
                    'DUTY_NOT_EDITABLE': 'session outside this run', 'OUTSIDE_GENERATION_POPULATION': 'outside the selected group', 'SUPERVISION_UNAVAILABLE': 'no eligible supervisor',
                    'CAPABILITY_NOT_GRANTED': f'missing {label}' if label else 'missing required skills',
                    'OVERTIME_NOT_ALLOWED': 'not eligible for overtime on this duty'}
                blockers = Counter(blocker for candidate in considered for blocker in {
                    rule_names.get(reason.get('rule_id'), 'a scheduling restriction') if reason['code'] == 'RESTRICTION'
                    else blocker_labels.get(reason['code']) for reason in candidate['exclusions']} if blocker)
                if blockers:
                    main = min(blockers, key=lambda blocker: (-blockers[blocker], blocker))
                    message += f" (main blocker: {main})"
                findings.append({'code': 'INSUFFICIENT_CAPABILITY_CANDIDATES' if capability else 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES',
                    'proof_kind': 'necessary_condition', 'proven': True,
                    'workspace_id': data['workspace']['id'], 'shift_id': sid,
                    'start': lo.isoformat(), 'end': hi.isoformat(), 'service_ids': list(shift['service_ids']),
                    'assignment_type_id': shift['assignment_type_id'], 'type_revision': shift['type_revision'],
                    'requirement_name': shift['type_snapshot']['name'], 'capability_id': capability,
                    'capability_name': label, 'requirement_path': path,
                    'required': minimum, 'available_upper_bound': len(eligible),
                    'unit': 'distinct_people', 'available_person_ids': eligible, 'candidates': considered,
                    'input_fingerprint': data['fingerprint'], 'checked_at': checked_at,
                    'message': (f"This session is locked with {len(eligible)} of {minimum} {'person' if minimum == 1 else 'people'}"
                                + (f" with {label}" if label else '') + '. Unlock it or add someone by hand.')
                               if shift['locked'] else message,
                    'boundary': 'Rest, competing duties, supervision and workload can reduce this upper bound further. Other conflicts have not been ruled out.'})
    return findings


def potential_candidate_people(data, shifts):
    """A necessary audience bound; ordinary or supervised sessions still need every person checked."""
    people = set()
    for shift in shifts:
        definition = series_definition(shift)
        if not definition.get('usual_assignees_fixed') or effective_type_definition(data, shift).get('supervision'):
            return None
        people.update(definition.get('default_person_ids', []))
        people.update(p['person_id'] for p in shift.get('participants', []))
    return people


def _usual_candidate_allowed(data, person_id, shift):
    from .recurrence import series_candidate_allowed
    return series_candidate_allowed(data, shift, person_id) or any(
        p['person_id'] == person_id for p in shift.get('participants', []))


def candidate_eligible(data, person_id, shift):
    """Boolean audience checks need no detailed evidence for a disallowed fixed outsider."""
    if not _usual_candidate_allowed(data, person_id, shift):
        return False
    definition = effective_type_definition(data, shift)
    return (qualified(data, person_id, shift, definition,
                      aware(shift['start']), aware(shift['end']))
            and candidate_evidence(data, person_id, shift)['eligible_and_available'])


def _absence_exclusions(data, person_id, a, b):
    """Complete absence reasons, shared only inside one immutable evaluation."""
    cache = data.get('_candidate_absence_cache')
    key = person_id, a, b
    if cache is not None and key in cache:
        return [dict(row) for row in cache[key]]
    reasons = []
    zone = data['organisation']['timezone']
    for row in person_rows(data, 'rotations', person_id):
        if (row['person_id'] == person_id and not row.get('department_available', True)
                and within_dates(row, a, b, zone)):
            reasons.append({'code': 'EXTERNAL_ROTATION', 'record_id': row['id'],
                            'starts_on': row['starts_on'], 'ends_on': row.get('ends_on')})
    for row in person_rows(data, 'availability', person_id):
        if row['person_id'] == person_id and overlaps(a, b, aware(row['start']), aware(row['end'])):
            reasons.append({'code': 'UNAVAILABLE', 'record_id': row['id'],
                            'start': row['start'], 'end': row['end']})
    if cache is not None:
        cache[key] = tuple(tuple(row.items()) for row in reasons)
    return reasons


def candidate_evidence(data, person_id, shift):
    a, b = aware(shift['start']), aware(shift['end'])
    definition = effective_type_definition(data, shift)
    evidence_cache = (data.get('_candidate_evidence_cache')
                      if '_candidate_evidence_policy_source' in data else None)
    cache_key = (id(data.get('_candidate_evidence_policy_source', data.get('policies'))), shift['id'], person_id, a, b)
    if evidence_cache is not None:
        known = evidence_cache.get(cache_key)
        if known is not None:
            return {**known, 'exclusions': list(known['exclusions'])}
    qualification_cache = data.get('_qualification_evidence_cache')
    known = False
    if qualification_cache:
        try:
            known = qualification_cache.get(_qualification_key(data, person_id, shift,
                                                                definition, a, b)) is True
        except (KeyError, TypeError):
            pass  # Canonical evidence below retains malformed-input validation and errors.
    qualification_key = _qualification_key(data, person_id, shift, definition, a, b)
    failures_cache = data.get('_qualification_failures_cache')
    if known:
        reasons = []
    elif failures_cache is not None:
        reasons = list(failures_cache.get(qualification_key) or ())
        if not reasons and qualification_key not in failures_cache:
            reasons = qualification_failures(data, person_id, shift, definition, a, b)
            failures_cache[qualification_key] = tuple(reasons)
    else:
        reasons = qualification_failures(data, person_id, shift, definition, a, b)
    if not _usual_candidate_allowed(data, person_id, shift):
        reasons.append({'code': 'USUAL_ASSIGNEES_FIXED', 'series_id': shift.get('series_id')})
    if definition.get('extra_cover', False):
        from .generation import allocation_overtime_allowed
        if not allocation_overtime_allowed(data, shift, person_id):
            reasons.append({'code': 'OVERTIME_NOT_ALLOWED', 'assignment_type_id': shift['assignment_type_id']})
    reasons.extend(_absence_exclusions(data, person_id, a, b))
    for policy in blocking_restrictions(data, person_id, shift, a, b):
        reasons.append({'code': 'RESTRICTION', 'rule_id': policy['id'],
                        'rule_revision': policy['revision']})
    result = {'person_id': person_id, 'shift_id': shift['id'],
              'start': shift['start'], 'end': shift['end'],
              'eligible_and_available': not reasons, 'exclusions': reasons,
              'boundary': 'Other assignments, rest, workload and supervision are not checked here.'}
    if evidence_cache is not None:
        evidence_cache[cache_key] = {**result, 'exclusions': tuple(reasons)}
    return result
