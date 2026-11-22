"""Rota counts in explicit units, shared by every projection of a stored check."""

from .recurrence import inactive
from datetime import date, timedelta
import hashlib
import json
from .time import aware


def contract_cause_sentence(codes, family, fallback):
    """Use the plain contract message unless a family deliberately owns the wording."""
    if family in {'coverage', 'fairness', 'leave_conflict', 'supervision'}:
        return fallback
    from packages.contracts.schemas import FINDING_MESSAGES
    messages = [FINDING_MESSAGES[code] for code in sorted(codes) if code in FINDING_MESSAGES]
    return ' '.join(dict.fromkeys(messages)) if len(messages) == len(codes) and messages else fallback


def issue_key(finding):
    values = [str(finding.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start')]
    if finding.get('code') == 'CATALOGUE_VALIDITY_CHANGED':
        values.extend(str((finding.get('detail') or {}).get(k) or '') for k in ('catalogue', 'record_id'))
    return '|'.join(values)


def submission_prerequisite(session_count):
    """Recorded sessions are required for submission; unknown historical counts are not inferred from cover."""
    if session_count == 0:
        return {'code': 'EMPTY_WORKSPACE', 'message': 'Add a session before submitting', 'action': 'add_session'}
    return None


def fix_list_context(data):
    """Small presentation/source receipt; no second rule resolver or private absence text."""
    from .conditions import active_rules, _requirements, PERSON_FIELDS
    from packages.contracts.rule_catalogue import catalogue_entry, RULE_CATALOGUE_V2, entry_kinds
    from .fix_list_facts import who_words, session_context
    policies = {p['id']: p for p in data['policies']}
    sources = {}
    for policy in active_rules(data['policies'], context=data):
        original = policies.get(policy['id'], policy)
        rule = original['definition']['rule']
        entry = catalogue_entry(rule['id'], rule.get('catalogue_version', 1)) if rule['kind'] == 'catalogue' else None
        line = policy.get('template_line') or {}
        if entry is None:
            entry = next((e for e in RULE_CATALOGUE_V2 if rule['kind'] in entry_kinds(e)), None)
        who = line.get('who') or {'match': 'all', 'conditions': [node for node in
            _requirements(policy['definition']['scope']) if 'conditions' in node or node['field'] in PERSON_FIELDS]}
        words = who_words(data, who) or (None, None)
        sources[json.dumps([policy['id'], line.get('line'), line.get('slot')])] = {
            'kind': policy['definition']['rule']['kind'], 'name': original['name'],
            'line_id': line.get('id'), 'who': who, 'who_label': words[0], 'who_many': words[1],
            'catalogue_id': entry['id'] if entry else None,
            'template': entry['fix_template'] if entry else None}
    focus = set(data['focus_ids'])
    person_shifts = {}
    sessions = {}
    for shift in data['shifts']:
        if shift['id'] in focus and not inactive(shift):
            sessions[shift['id']] = session_context(data, shift)
            for participant in shift['participants']:
                person_shifts.setdefault(participant['person_id'], set()).add(shift['id'])
    return {'sources': sources, 'people': {pid: p['display_name'] for pid, p in data['people'].items()},
            'focus_ids': sorted(focus), 'person_shift_ids': {pid: sorted(ids) for pid, ids in person_shifts.items()},
            'sessions': sessions, 'timezone': data['organisation']['timezone'],
            'weekend_days': (data.get('department') or {}).get('weekend_days'),
            'workspace': {k: data.get('workspace', {}).get(k) for k in ('id', 'starts_on', 'ends_on')}}


def with_fix_list_context(validation, data):
    """Default a missing context for a validation computed from this same `data`; never use it to refresh a stored check."""
    return validation if 'fix_list_context' in validation else {
        **validation, 'fix_list_context': fix_list_context(data)}


def refresh_fix_list_context(context, data, scope):
    """Refresh touched presentation receipts only; never resolve rules, check eligibility or load history."""
    from .fix_list_facts import session_context
    touched, people = set(scope['shifts']), set(scope['people'])
    focus = set(data['focus_ids'])
    sessions = {sid: row for sid, row in context.get('sessions', {}).items() if sid not in touched}
    person_shifts = {pid: set(ids) - touched for pid, ids in context.get('person_shift_ids', {}).items()}
    for shift in data['shifts']:
        if shift['id'] not in touched or shift['id'] not in focus or inactive(shift):
            continue
        sessions[shift['id']] = session_context(data, shift)
        for participant in shift['participants']:
            person_shifts.setdefault(participant['person_id'], set()).add(shift['id'])
    return {**context, 'sessions': sessions, 'focus_ids': sorted(focus),
        'person_shift_ids': {pid: sorted(ids) for pid, ids in person_shifts.items()},
        'people': {**context.get('people', {}), **{pid: data['people'][pid]['display_name'] for pid in people if pid in data['people']}},
        'timezone': data['organisation']['timezone'],
        'workspace': {k: data.get('workspace', {}).get(k) for k in ('id', 'starts_on', 'ends_on')}}


def fix_list(check, places, accepted_issues=(), *, context=None):
    """The single cause-counting projection. Findings and strict validation remain immutable evidence.

    Quotas share a repair per person, then compatible decisions join across their source rules.
    Coverage shares a session and diagnosis at its strongest level. Fairness is not a breach.
    """
    from .validation import finding_observations
    from .finding_causes import COVERAGE_CODES, DUTIES_DURING_APPROVED_LEAVE
    from packages.contracts.schemas import NEVER_OVERRIDDEN
    from packages.contracts.rule_catalogue import V2_CATALOGUE_BY_ID, catalogue_entry
    from .score import NOT_BREACHES
    from urllib.parse import urlencode
    from .fix_list_facts import coverage_signature, presentation, ordered_fixes, improvement_group, session_words
    context = context or check.get('fix_list_context') or {}
    sources, names = context.get('sources', {}), context.get('people', {})
    session_locks = {sid: row.get('locked', False) for sid, row in context.get('sessions', {}).items()}
    session_locks.update({row['shift_id']: row['locked'] for row in places.get('sessions', []) if 'locked' in row})
    findings = [f for f in check.get('change_findings', check.get('findings', [])) if f['severity'] in {'error', 'warning'}]
    # Union coverage parents by their actual sessions, including pooled requirements.
    parents = list(range(len(findings)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def source_for(row):
        detail = row.get('detail') or {}
        return sources.get(json.dumps([row.get('rule_id'), detail.get('line'), detail.get('slot')]), {})
    def catalogue_for(row):
        return source_for(row).get('catalogue_id')
    def leave_classification(row):
        return (row.get('classification') or
                (row.get('cause') or {}).get('classification') or
                (row.get('detail') or {}).get('classification'))
    def is_safety(row):
        kind = source_for(row).get('kind')
        # Advisory targets may use a safety primitive; only a mandatory breach is a safety finding.
        return row['severity'] == 'error' and (kind in NEVER_OVERRIDDEN or row['code'] == 'COVERAGE_GAP' or not kind
                                               or leave_classification(row) == DUTIES_DURING_APPROVED_LEAVE)
    reason_order = {'empty_population': 0, 'person_unavailable': 1, 'no_eligible_people': 2, 'capacity_short': 3}
    def diagnosis(rows):
        reasons = [(row.get('cause') or {}).get('code', 'unfilled') for row in rows if row['code'] in COVERAGE_CODES]
        return min(reasons, key=lambda code: (reason_order.get(code, 4), code)) if reasons else 'unfilled'
    observations, safety, coverage, seen = [], [], set(), {}
    for i, finding in enumerate(findings):
        rows = list(finding_observations({'findings': [finding]}))
        observations.append(rows)
        safety.append(any(is_safety(row) for row in rows))
        for row in rows:
            if row['code'] not in COVERAGE_CODES:
                continue
            coverage.add(i)
            for sid in (row.get('detail') or {}).get('affected_shift_ids') or [row.get('shift_id')]:
                if sid:
                    key = (sid, diagnosis(rows), row['code'] == 'OPEN_EXTRA_COVER', catalogue_for(row) == 'companion_cover',
                           coverage_signature(rows, source_for, COVERAGE_CODES))
                    if key in seen:
                        parents[root(i)] = root(seen[key])
                    seen[key] = i
    components = {}
    for i in coverage:
        components.setdefault(root(i), []).append(i)
    identities = {}
    for indices in components.values():
        level = 'must' if any(findings[i]['severity'] == 'error' or safety[i] for i in indices) else 'could'
        rows = [row for i in indices for row in observations[i]]
        reason = diagnosis(rows)
        catalogues = {catalogue_for(row) for row in rows if row['code'] in COVERAGE_CODES}
        # Keep companion-cover and ordinary eligibility causes separate when they
        # affect different sessions; a mixed component still gets one honest,
        # generic cause instead of a misleading catalogue-specific sentence.
        catalogue_id = next(iter(catalogues)) if len(catalogues) == 1 else None
        identity = ('coverage', level, reason, any(safety[i] for i in indices), catalogue_id,
                    coverage_signature(rows, source_for, COVERAGE_CODES))
        identities.update({i: identity for i in indices})
    groups = {}
    for i, finding in enumerate(findings):
        level = 'must' if finding['severity'] == 'error' or safety[i] else 'could'
        if finding['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'}:
            identity = ('fairness', 'could')
        elif finding['severity'] == 'warning' and finding['code'] in NOT_BREACHES:
            continue
        elif leave_classification(finding) == DUTIES_DURING_APPROVED_LEAVE and finding.get('person_id'):
            identity = ('leave_conflict', level, finding['person_id'], safety[i])
        elif i in identities:
            identity = identities[i]
        elif finding['code'].startswith('QUOTA_') and finding.get('person_id'):
            identity = ('quota', level, finding['person_id'], safety[i])
        else:
            identity = ('issue', level, finding.get('person_id'), finding.get('rule_id'), finding['code'], safety[i])
        groups.setdefault(identity, []).append(i)
    # One decision can repair colleagues sharing a connected rule set; unrelated rules stay separate.
    candidates = list(groups.items())
    decision_parents = list(range(len(candidates)))
    def decision_root(i):
        while decision_parents[i] != i:
            decision_parents[i] = decision_parents[decision_parents[i]]
            i = decision_parents[i]
        return i
    signatures, linked = [], {}
    for n, (identity, indices) in enumerate(candidates):
        family, level = identity[:2]
        safe = any(safety[i] for i in indices) if family != 'fairness' else False
        repairs = {hint.get('action') for i in indices for row in observations[i] for hint in row.get('fix_hints') or []}
        action = (({'no_eligible_people': 'open_eligibility', 'empty_population': 'open_eligibility',
                    'person_unavailable': 'choose_cover'}.get(identity[2]) or
                   ('add_sessions' if 'add_sessions' in repairs else 'open_eligibility'
                    if identity[2] == 'capacity_short' else 'autoschedule_scoped')) if family == 'coverage' else
                  'rebalance' if family == 'fairness' else 'choose_cover' if family == 'leave_conflict' else 'add_sessions' if 'add_sessions' in repairs
                  else 'open_eligibility' if 'open_eligibility' in repairs
                  else 'adjust_rota' if level == 'must' and not safe and family != 'coverage'
                  else 'autoschedule_scoped' if family == 'quota' else 'open_line')
        if any(row['code'] == 'TYPE_VERSION_CHANGED' for i in indices for row in observations[i]):
            action = 'refresh_sessions'
        if family == 'coverage' and action == 'autoschedule_scoped' and any(
                session_locks.get(sid)
                for i in indices for row in observations[i]
                for sid in (row.get('detail') or {}).get('affected_shift_ids') or [row.get('shift_id')]):
            action = 'choose_cover'
        if family == 'coverage' and identity[2] == 'capacity_short' and any(
                row.get('rule_id') for i in indices for row in observations[i]):
            action = 'open_line'
        signature = (family if family in {'coverage', 'fairness', 'leave_conflict'} else 'rules', level,
                     identity[2] if family in {'coverage', 'leave_conflict'} else None, action, safe,
                     identity[4] if family == 'coverage' else None,
                     identity[5] if family == 'coverage' else None)
        rules = {row['rule_id'] for i in indices for row in observations[i] if row.get('rule_id')}
        tokens = rules or {findings[i]['code'] for i in indices}
        if family == 'leave_conflict':
            tokens = {DUTIES_DURING_APPROVED_LEAVE}
        elif family == 'fairness':
            tokens = {'fairness'}
        for token in tokens:
            key = signature, token
            if key in linked:
                decision_parents[decision_root(n)] = decision_root(linked[key])
            linked[key] = n
        signatures.append(signature)
    decisions = {}
    for n, (_, indices) in enumerate(candidates):
        decisions.setdefault(decision_root(n), []).extend(indices)
    groups = {((signatures[n][0] if signatures[n][0] != 'rules' else
                'leave_conflict' if any(leave_classification(row) == DUTIES_DURING_APPROVED_LEAVE
                                        for i in indices for row in observations[i]) else
                'quota' if any(findings[i]['code'].startswith('QUOTA_') for i in indices) else 'issue'),
               signatures[n][1], signatures[n][2], signatures[n][3], signatures[n][4],
               signatures[n][5],
               # A rule-less decision is keyed by its codes (its linking tokens), so two never overwrite each other.
               tuple(sorted({row['rule_id'] for i in indices for row in observations[i] if row.get('rule_id')}
                            or {findings[i]['code'] for i in indices})), signatures[n][6]): indices
              for n, indices in decisions.items()}
    accepted = {a['key']: a for a in accepted_issues if isinstance(a, dict)}
    causes = []
    for identity, indices in groups.items():
        family, level = identity[:2]
        keys = sorted({issue_key(findings[i]) for i in indices})
        evidence, lines, who, pids, sids, starts, ends, rule_names, templates = [], {}, {}, set(), set(), [], [], set(), []
        prefills = {}
        leave_duties = set()
        acknowledgeable = level == 'must'
        for i in indices:
            for observation in observations[i]:
                detail = {k: v for k, v in (observation.get('detail') or {}).items() if k != 'derived_findings'}
                rid, line, slot = observation.get('rule_id'), detail.get('line'), detail.get('slot')
                source = sources.get(json.dumps([rid, line, slot]), {})
                if source.get('who'):
                    who[json.dumps(source['who'], sort_keys=True)] = source['who']
                if source.get('name'):
                    rule_names.add(source['name'])
                # Saved check context retains evidence; current catalogue templates own its presentation.
                entry = V2_CATALOGUE_BY_ID.get(source.get('catalogue_id')) or catalogue_entry(source.get('catalogue_id'))
                if entry:
                    templates.append(entry['fix_template'])
                if observation['severity'] == 'error':
                    # Unknown sources and built-in clinical checks fail closed, including derived safety evidence.
                    acknowledgeable &= bool(source.get('kind')) and source['kind'] not in NEVER_OVERRIDDEN
                    acknowledgeable &= observation['code'] not in COVERAGE_CODES
                if rid:
                    query = {'source': rid, **({'line': line} if line is not None else {})}
                    lines[rid, str(line), slot] = {'rule_id': rid, 'line': line, 'line_id': source.get('line_id'), 'slot': slot,
                                                  'href': '#rules?' + urlencode(query)}
                if observation.get('person_id'):
                    pids.add(observation['person_id'])
                sids.update(detail.get('affected_shift_ids') or ([observation['shift_id']] if observation.get('shift_id') else []))
                observation_cause = observation.get('cause') or {}
                if leave_classification(observation) == DUTIES_DURING_APPROVED_LEAVE:
                    sids.update(observation.get('leave_shift_ids') or observation_cause.get('leave_shift_ids')
                                or detail.get('leave_shift_ids') or [])
                    if (family == 'leave_conflict' and observation['code'] == 'UNAVAILABLE'
                            and observation.get('person_id') and observation.get('shift_id')):
                        leave_duties.add((observation['person_id'], observation['shift_id']))
                hints = observation.get('fix_hints') or []
                evidence_ids = set(detail.get('affected_shift_ids') or ([observation['shift_id']] if observation.get('shift_id') else []))
                for hint in hints:
                    prefill = hint.get('prefill') or {}
                    if hint.get('action') == identity[3]:
                        window = context.get('workspace', {})
                        scoped_prefill = {**prefill}
                        for key, bound in (('starts_on', max), ('ends_on', min)):
                            if prefill.get(key) and window.get(key):
                                scoped_prefill[key] = bound(prefill[key], str(window[key]))
                        if 'focus_ids' in context and 'shift_ids' in prefill:
                            scoped_prefill['shift_ids'] = sorted(set(prefill['shift_ids']).intersection(context['focus_ids']))
                        prefills[json.dumps(scoped_prefill, sort_keys=True)] = scoped_prefill
                    if prefill.get('starts_on'):
                        starts.append(prefill['starts_on'])
                    if prefill.get('ends_on'):
                        ends.append(prefill['ends_on'])
                    evidence_ids.update(prefill.get('shift_ids') or [])
                if family == 'fairness':
                    evidence_ids.update(context.get('person_shift_ids', {}).get(observation.get('person_id'), []))
                sids.update(evidence_ids)
                proration = detail.get('proration') or (observation.get('cause') or {}).get('proration')
                explanation = None
                if proration:
                    explanation = (f"{proration['configured_amount']:g} × {proration['working_days']} / "
                        f"{proration['full_working_days']} working days, rounded up to {proration['amount']:g}. "
                        f"The check covers {human_date_range(proration['starts_on'], proration['ends_on'])}, "
                        'including published duties outside this draft; only this draft’s days can be replanned here.')
                session_evidence = [context['sessions'][sid] for sid in sorted(evidence_ids) if sid in context.get('sessions', {})]
                cause = observation_cause
                reason = observation.get('message')
                if observation['code'] in COVERAGE_CODES and cause:
                    services = ', '.join(sorted({name for session in session_evidence for name in session['service_names']})) or 'these sessions'
                    period = human_date_range(*[str(context['workspace'][key]) for key in ('starts_on', 'ends_on')]) if all(
                        context.get('workspace', {}).get(key) for key in ('starts_on', 'ends_on')) else 'the checked dates'
                    reason = {'empty_population': f'Nobody matches this rule’s programme or group during {period}.',
                        'no_eligible_people': (f'No eligible companion meets {source.get("name") or "the companion requirement"} for {services} during {period}.'
                            if source.get('catalogue_id') == 'companion_cover' else
                            f'Nobody in the required group meets {source["name"]} for {services} during {period}.'
                            if observation['code'] == 'SCOPED_COVERAGE_GAP' and source.get('name') else
                            f'Nobody meets the eligibility requirements for {services} during {period}.'),
                        'person_unavailable': f'Eligible people are unavailable for {services} during {period}.',
                        'capacity_short': f'Eligible people or session capacity cannot meet this rule’s required cover.',
                        'unfilled': f'Eligible people can cover {services}; plan their duties.'}.get(cause.get('code'))
                    if reason:
                        reason = f"{cause.get('eligible_people', 0)} eligible: {reason[0].lower()}{reason[1:]}"
                evidence.append({**{k: observation.get(k) for k in ('code', 'severity', 'person_id', 'shift_id', 'rule_id', 'start', 'end', 'outside_rota')},
                    'shift_ids': sorted(evidence_ids),
                    'sessions': session_evidence, 'rule_name': source.get('name') or observation.get('message'),
                    'message': observation.get('message'), 'reason': reason,
                    'finding_key': issue_key(findings[i]), 'person_name': names.get(observation.get('person_id')),
                    'rule_kind': source.get('kind'), 'catalogue_id': source.get('catalogue_id'),
                    'detail': detail, 'cause': observation.get('cause'),
                    'explanation': explanation})
        if not starts:
            starts = [str(context['workspace']['starts_on'])] if context.get('workspace', {}).get('starts_on') else []
            ends = [str(context['workspace']['ends_on'])] if context.get('workspace', {}).get('ends_on') else []
        diagnostic = identity[2] if family == 'coverage' else None
        action = identity[3]
        template = templates[0] if templates else V2_CATALOGUE_BY_ID['workload_limit']['fix_template']
        if family == 'leave_conflict':
            sentence = 'Duties during approved leave'
        elif family == 'fairness':
            template = V2_CATALOGUE_BY_ID['fair_share']['fix_template']
        elif family == 'coverage':
            template = V2_CATALOGUE_BY_ID['people_per_session']['fix_template']
        elif any(row['code'] == 'SUPERVISION_PAIRING' for row in evidence):
            template = V2_CATALOGUE_BY_ID['supervision']['fix_template']
            family = 'supervision'
        people = ', '.join(names.get(pid, pid) for pid in sorted(pids))
        dates = human_date_range(max(min(starts), context.get('workspace', {}).get('starts_on') or min(starts)),
                                min(max(ends), context.get('workspace', {}).get('ends_on') or max(ends))) if starts and ends else ''
        rules = ', '.join(sorted(rule_names))
        need, have = ('needs', 'has') if len(pids) == 1 else ('need', 'have')
        people_subject = people if len(pids) == 1 else f'{len(pids)} people' if pids else 'These duties'
        metrics = {row['detail'].get('metric') for row in evidence}
        quantity = ('clinic time' if metrics <= {'session_units', 'scheduled_hours', 'reserved_hours', None}
                    else 'duties and clinic time' if metrics & {'session_units', 'scheduled_hours', 'reserved_hours'} else 'duties')
        codes = {findings[i]['code'] for i in indices}
        # SW3-A-15: a double booking has no rule template; it is two duties at once, not a mix of duties.
        quota_phrase = (f'{have} two duties at the same time' if codes == {'TIME_OVERLAP'} else
                        f'{need} more {quantity}' if codes <= {'QUOTA_BELOW_MINIMUM', 'QUOTA_BELOW_TARGET'} else
                        f'{have} too ' + ('much' if quantity == 'clinic time' else 'many') + f' {quantity}' if codes <= {'QUOTA_ABOVE_MAXIMUM', 'QUOTA_ABOVE_TARGET'} else
                        f'{need} a different mix of duties and clinic time')
        sessions_subject = f'{len(sids)} session' + ('s' if len(sids) != 1 else '')
        extra_cover = all(row.get('code') == 'OPEN_EXTRA_COVER' for row in evidence)
        companion = any(row.get('catalogue_id') == 'companion_cover' for row in evidence)
        impossible = next((row for row in evidence if row.get('rule_id') and
                           (row.get('cause') or {}).get('code') == 'capacity_short'), None)
        if impossible:
            from packages.contracts.rule_catalogue import staffing_pool_message
            cause = impossible['cause']
            coverage_sentence = (staffing_pool_message(impossible['rule_name'], cause['required'], cause['eligible_people'])
                if cause['required'] > cause['eligible_people'] else
                f'Rule cannot be met: {rules} needs more people than these sessions can hold.')
        elif extra_cover:
            coverage_sentence = f'Optional extra cover remains open for {sessions_subject}.'
        elif companion:
            coverage_sentence = (f'No eligible companion is available for {sessions_subject}.' if diagnostic == 'no_eligible_people'
                else f'{sessions_subject} ' + ('does' if len(sids) == 1 else 'do') + ' not meet the companion requirement.')
        elif family == 'coverage' and 'COVERAGE_GAP' not in codes and rules:
            coverage_sentence = (f'Nobody in the required group meets {rules} for {sessions_subject}.'
                if diagnostic in {'no_eligible_people', 'empty_population'} else
                f'{sessions_subject} ' + ('does' if len(sids) == 1 else 'do') + f' not meet {rules}.')
        else:
            coverage_sentence = {
                'no_eligible_people': (f'No eligible companion is available for {sessions_subject}.' if companion
                                       else f'Nobody eligible can cover {sessions_subject}.'),
                'person_unavailable': f'Eligible people are away for {sessions_subject}.',
                'empty_population': f'Nobody eligible matches the programme or group for {sessions_subject}.',
                'capacity_short': f'Too few eligible people or places can cover {sessions_subject}.'}.get(
                    diagnostic, f'{sessions_subject} ' + ('needs' if len(sids) == 1 else 'need') + ' cover.')
        if not pids and family != 'coverage':
            need, have = 'need', 'have'
        cid = hashlib.sha256(json.dumps([context.get('workspace', {}).get('id'), identity], sort_keys=True).encode()).hexdigest()[:24]
        evidence_hash = hashlib.sha256(json.dumps(sorted(evidence, key=lambda row: json.dumps(row, sort_keys=True)), sort_keys=True).encode()).hexdigest()
        matches = [accepted.get(key, {}) for key in keys]
        # Quota and overlapping absence evidence can describe the same leave duty.
        checks = len(leave_duties) or len(indices)
        meta = template['meta'].format(people=people, dates=dates, rules=rules, checks=checks)
        if action == 'open_eligibility' and any(row['code'] == 'CAPABILITY_GAP' for row in evidence):
            meta += ' · A credentialer grants the required skill'
        acknowledged = (acknowledgeable and all(a.get('cause_id') == cid and a.get('evidence_hash') == evidence_hash
                                               and a.get('reason') for a in matches))
        scope = {'person_ids': [] if family == 'coverage' else sorted(pids),
                'shift_ids': sorted(sids.intersection(context['focus_ids']) if 'focus_ids' in context else sids),
                'starts_on': max(min(starts), context.get('workspace', {}).get('starts_on') or min(starts)) if starts else None,
                'ends_on': min(max(ends), context.get('workspace', {}).get('ends_on') or max(ends)) if ends else None,
                'rule_lines': sorted(lines.values(), key=lambda row: (row['rule_id'], str(row['line']), str(row['slot']))),
                'prefills': list(prefills.values()), 'keep_existing': True}
        if family == 'leave_conflict':
            scope['classification'] = DUTIES_DURING_APPROVED_LEAVE
        scope['who'] = list(who.values())
        facts, fact_sentence = presentation(context, check, evidence, sids, pids, template, level=level, family=family)
        # An impossible rule needs configuration repair; it is not simply an unfilled place.
        if impossible or extra_cover or companion:
            fact_sentence = None
        fixes, eligible = ordered_fixes(action, scope, evidence, safety=identity[4], session_locks=session_locks)
        fallback_sentence = sentence if family == 'leave_conflict' else template['sentence'].format(
            people_subject=people_subject, need=need, have=have, quota_phrase=quota_phrase,
            sessions_subject=sessions_subject, coverage_sentence=coverage_sentence)
        cause_sentence = contract_cause_sentence(codes, family, fallback_sentence)
        session_findings = {}
        for row in evidence:
            for sid in row['shift_ids']:
                if sid in sids:
                    session_findings.setdefault(sid, row['finding_key'])
        causes.append({'id': cid, 'level': level, 'checks': checks,
            'who': list(who.values()), 'person_ids': sorted(pids), 'shift_ids': sorted(sids),
            'safety': identity[4], 'new': cid not in check['fix_list_state']['previous_ids'] if check.get('fix_list_state') else False,
            'sentence': session_words(fact_sentence or cause_sentence),
            'meta': session_words(' · '.join(part for part in meta.split(' · ') if part)),
            **facts, 'session_findings': session_findings,
            'action': fixes[0], 'fixes': fixes, 'eligible_people': eligible,
            'group': improvement_group(fixes[0]['kind']) if level == 'could' else None,
            'evidence': evidence, 'score_gain': None, 'score_gain_estimated': False,
            'rule_lines': sorted(lines.values(), key=lambda row: (row['rule_id'], str(row['line']), str(row['slot']))),
            'finding_keys': keys, 'evidence_hash': evidence_hash, 'acknowledgeable': bool(acknowledgeable and not identity[4]),
            'acknowledgement': matches[0] if acknowledged and not identity[4] else None})
    causes.sort(key=lambda cause: (cause['level'] != 'must', not cause['safety'], -cause['checks'], cause['id']))
    must = [c for c in causes if c['level'] == 'must']
    # Attribute existing scorer losses once, without inventing an alternate score or solver promise.
    score = (check.get('score') or {}) if not check.get('scope') else {}
    losses = {}
    by_id = {cause['id']: cause for cause in causes}
    for cause in causes:
        if cause['level'] == 'could':
            keys = [('fairness',)] if cause['action']['kind'] == 'rebalance' else [
                ('rules', row.get('rule_id'), row.get('person_id'), row.get('shift_id'),
                 row['detail'].get('line'), row['detail'].get('slot')) for row in cause['evidence']]
            for key in keys:
                losses.setdefault(key, set()).add(cause['id'])
    for contribution in score.get('contributions', []):
        key = ('fairness',) if contribution['component'] == 'fairness' else tuple(contribution.get(k)
            for k in ('component', 'rule_id', 'person_id', 'shift_id', 'line', 'slot'))
        candidates = losses.get(key, set())
        if len(candidates) == 1:
            cause = by_id[next(iter(candidates))]
            cause['score_gain'] = round((cause['score_gain'] or 0) + contribution['points'], 1)
            cause['score_gain_estimated'] = True
    fairness = {issue_key(f) for f in findings if f['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'}}
    since = (check.get('fix_list_state') or {}).get('since')
    ids = {c['id'] for c in causes}
    prerequisite = submission_prerequisite(len(context['focus_ids']) if 'focus_ids' in context else None)
    return {'workspace_id': context.get('workspace', {}).get('id'), 'revision': None, 'fingerprint': check.get('fingerprint'),
        'summary': {'must': len(must), 'could': len(causes) - len(must),
            'checking': bool(check.get('checking')),
            'population_note': population_note(check),
            'since': {'at': since['at'], 'cleared': len(set(since['ids']) - ids), 'new': len(ids - set(since['ids']))} if since else None,
            'checked_at': aware(check['evaluated_at']).isoformat().replace('+00:00', 'Z') if check.get('evaluated_at') else None,
            'places_filled': places['places_filled'], 'places_total': places['places_needed'], 'places_open': places['places_open'],
            'checks_must': sum(c['checks'] for c in must),
            'checks_could': sum(c['checks'] for c in causes if c['level'] == 'could') - len(fairness),
            'fairness_checks': len(fairness), 'acknowledged': sum(bool(c['acknowledgement']) for c in must),
            'score': None if must or prerequisite else score.get('score'), 'uncapped_score': score.get('uncapped_score'),
            'could_score_gain': (round(sum(c['score_gain'] for c in causes if c['level'] == 'could'), 1)
                if score and all(c['score_gain'] is not None for c in causes if c['level'] == 'could') else None),
            'submission_issue': prerequisite,
            'submit_blocked': bool(prerequisite) or any(not c['acknowledgement'] for c in must)}, 'causes': causes}


def population_note(check):
    population = check.get('planning_population')
    if population is None:
        return None
    count = population['inside_count']
    return (f"Checked for: {count} {'person' if count == 1 else 'people'} in the applied planning population"
            + (f"; {population['outside_count']} people outside this rota's population"
               if population['outside_count'] else ''))


def fix_list_delta(before, after):
    """Compact upserts from the existing scoped evidence, with stable decision IDs."""
    def compact(cause):
        return {**cause, 'evidence': []}
    old = {c['id']: c for c in (before or {}).get('causes', [])}
    new = {c['id']: c for c in (after or {}).get('causes', [])}
    common = old.keys() & new.keys()
    changed = sorted(cid for cid in common if
        {k: v for k, v in compact(old[cid]).items() if k != 'new'} !=
        {k: v for k, v in compact(new[cid]).items() if k != 'new'})
    added = sorted(new.keys() - old.keys())
    return {'cleared': sorted(old.keys() - new.keys()), 'new': added, 'changed': changed,
            'causes': [compact(new[cid]) for cid in sorted(set(added + changed))],
            'summary': (after or {}).get('summary')}


def fix_list_effect_projection(before_data, before_validation, after_data, after_validation):
    """Owned presentation hook for the shared D6/rota-adjustment effect service."""
    phases = []
    for data, validation in ((before_data, before_validation), (after_data, after_validation)):
        focus = set(data['focus_ids'])
        # A stored before-check may carry a context built from older inputs: always rebuild it from this data.
        check = {**validation, 'fix_list_context': fix_list_context(data)}
        summary = rota_summary(check, [row for row in data['shifts'] if row['id'] in focus],
                               (data.get('workspace') or {}).get('accepted_issues') or [])
        phases.append({'validation': validation, 'summary': summary, 'fix_list': summary['fix_list']})
    return {'before': phases[0], 'after': phases[1],
            'fix_list_delta': fix_list_delta(phases[0]['fix_list'], phases[1]['fix_list'])}


def remember_fix_list(previous, current, *, submitted=False, accepted_issues=(), summary_cache=None):
    """Persist only derived ID receipts in the existing check JSON; edits retain their normal Undo."""
    from zoneinfo import ZoneInfo
    before = rota_summary(previous, accepted_issues=accepted_issues, compact=True, cache=summary_cache)['fix_list'] if previous else None
    at = current['evaluated_at']
    day = aware(at).astimezone(ZoneInfo((current.get('fix_list_context') or {}).get('timezone', 'UTC'))).date().isoformat()
    state = (previous or {}).get('fix_list_state') or {}
    since = state.get('since')
    reset_since = submitted or not since or since.get('day') != day
    if reset_since:
        ids_after = rota_summary(current, accepted_issues=accepted_issues, compact=True, cache=summary_cache)['fix_list']['causes']
        since = {'at': at, 'day': day, 'ids': [c['id'] for c in ids_after]}
    current['fix_list_state'] = {'previous_ids': [c['id'] for c in (before or {'causes': []})['causes']], 'since': since}
    # Reset needs a second pass so the stored delta carries the state-aware `since` receipt.
    after = rota_summary(current, accepted_issues=accepted_issues, compact=True, cache=summary_cache)['fix_list']
    current['fix_list_delta'] = fix_list_delta(before, after)
    return current


def generation_fix_lists_fallback(source, result):
    """Return a conservative projection when display-only Fix list wording cannot be built."""
    from copy import deepcopy
    from packages.contracts.rule_catalogue import FIX_LIST_WORDS

    sentence = FIX_LIST_WORDS['generation_fallback']
    saved = source.get('fix_list_before')
    if isinstance(saved, dict):
        before = deepcopy(saved)
    else:
        validation = result.get('validation') if isinstance(result.get('validation'), dict) else {}
        summary = result.get('summary') if isinstance(result.get('summary'), dict) else {}
        required = max(int(summary.get('required_places') or 0), 0)
        filled = max(int(summary.get('filled_places') or 0), 0)
        must = max(int(validation.get('error_count') or 0), 0)
        before = {'workspace_id': (source.get('workspace') or {}).get('id'), 'revision': None,
                  'fingerprint': source.get('fingerprint'),
                  'summary': {'submission_issue': None, 'population_note': None, 'must': must, 'could': 0,
                      'checking': False, 'since': None, 'checked_at': None,
                      'places_filled': filled, 'places_total': required, 'places_open': max(required - filled, 0),
                      'checks_must': must, 'checks_could': 0, 'fairness_checks': 0, 'acknowledged': 0,
                      'submit_blocked': bool(must), 'score': None, 'uncapped_score': None,
                      'could_score_gain': None}, 'causes': [], 'review_items': [], 'planning_context': None}
    after = deepcopy(before)
    after['fingerprint'] = result.get('input_fingerprint') or after.get('fingerprint')
    changes = len(result.get('additions') or []) + len(result.get('removals') or [])
    return {'fix_list_before': before, 'fix_list_after': after,
            'open_places_before': None, 'open_places_after': (result.get('summary') or {}).get('open_places_count'),
            'score_delta': None,
            'changes_by_cause': [{'cause_id': None, 'sentence': sentence, 'level': 'could', 'changes': changes}],
            'fix_list_fallback': sentence}


def _generation_fix_lists(source, result):
    """Project a saved proposal in its complete rota context, outside any writer transaction."""
    from copy import deepcopy
    from .validation import evaluate
    from .score import schedule_score
    from .time import now
    before = deepcopy({**source, **source.get('score_context', {})})
    after = deepcopy(before)
    removed = {(row['shift_id'], row['assignment_id']) for row in result.get('removals', [])}
    added = {}
    for row in result.get('additions', []):
        added.setdefault(row['shift_id'], []).append(row['participant'])
    for shift in after['shifts']:
        shift['participants'] = [p for p in shift['participants'] if (shift['id'], p.get('id')) not in removed] + added.get(shift['id'], [])
    lists = []
    for data, saved in ((before, source.get('fix_list_before')), (after, None)):
        if saved is not None:
            lists.append(saved)
            continue
        check = dict(result['validation']) if data is after and result.get('validation') is not None else evaluate(data)
        score = (result.get('summary') or {}).get('score_saved' if data is after else 'score_before')
        check['score'] = score if score is not None else schedule_score(data, check,
            source.get('generation_defaults', source.get('generation_options')))
        check.update(fix_list_context=fix_list_context(data), evaluated_at=now().isoformat())
        lists.append(rota_summary(check, accepted_issues=(source.get('workspace') or {}).get('accepted_issues') or [])['fix_list'])
    causes_by_shift = {}
    for cause in lists[0]['causes']:
        for sid in cause['action']['scope']['shift_ids']:
            causes_by_shift.setdefault(sid, cause['id'])
    changes = {}
    for row in [*result.get('additions', []), *result.get('removals', [])]:
        cid = causes_by_shift.get(row['shift_id'])
        changes[cid] = changes.get(cid, 0) + 1
    causes = {cause['id']: cause for cause in lists[0].get('causes', [])}
    return {'fix_list_before': lists[0], 'fix_list_after': lists[1],
        'open_places_before': lists[0]['summary']['places_open'], 'open_places_after': lists[1]['summary']['places_open'],
        'score_delta': round(lists[1]['summary']['uncapped_score'] - lists[0]['summary']['uncapped_score'], 1)
            if all(value['summary']['uncapped_score'] is not None for value in lists) else None,
        'changes_by_cause': [{'cause_id': cid, 'sentence': causes.get(cid, {}).get('sentence'),
                              'level': causes.get(cid, {}).get('level'), 'changes': count}
                             for cid, count in sorted(changes.items(), key=lambda row: row[0] or '')]}


def generation_fix_lists(source, result, *, stopped=None):
    """Never let display-only wording turn a valid solver result into a failed job."""
    from .solver_control import Cancelled, TimeLimit
    # Stop has already spent its shared finalization allowance; do not start more validation or scoring here.
    if result.get('stop_reason') == 'operator_stop':
        return generation_fix_lists_fallback(source, result)
    if stopped is not None:
        previous_check = source.get('_check_budget', lambda: None)
        def check():
            if stopped():
                raise Cancelled()
            previous_check()
        source = {**source, '_check_budget': check}
    try:
        source.get('_check_budget', lambda: None)()
        return _generation_fix_lists(source, result)
    except (Cancelled, TimeLimit):
        return generation_fix_lists_fallback(source, result)
    except Exception as exc:
        origin = exc.__traceback__
        while origin is not None and origin.tb_next is not None:
            origin = origin.tb_next
        fallback = generation_fix_lists_fallback(source, result)
        fallback['_fix_list_presentation_error'] = {
            'error_code': type(exc).__name__,
            'error_location': {'module': origin.tb_frame.f_globals.get('__name__'),
                               'function': origin.tb_frame.f_code.co_name,
                               'line': origin.tb_lineno} if origin is not None else None}
        return fallback


def grouped_findings(findings, coverage):
    """Keep a cover shortfall caused solely by assigned people's blockers on those issues."""
    blockers = {}
    for finding in findings:
        if finding['severity'] == 'error' and finding['code'] in {
                'UNAVAILABLE', 'NOT_ELIGIBLE', 'EXTERNAL_ROTATION', 'RESTRICTION'} and finding.get('person_id'):
            blockers.setdefault(finding.get('shift_id'), []).append(finding)
    segments = {(row['shift_id'], row.get('start'), row.get('end')): row for row in coverage}
    derived, kept = {}, []
    for finding in findings:
        if finding['code'] != 'COVERAGE_GAP' or finding['severity'] != 'error':
            kept.append(finding)
            continue
        row = segments.get((finding.get('shift_id'), finding.get('start'), finding.get('end')), {})
        causes = [f for f in blockers.get(finding.get('shift_id'), [])
                  if f.get('start') and f.get('end') and finding.get('start') and finding.get('end')
                  and aware(f['start']) <= aware(finding['start']) and aware(finding['end']) <= aware(f['end'])]
        excluded = row.get('scheduled', 0) - row.get('available_eligible', 0)
        if (row.get('scheduled', 0) >= (finding.get('detail') or {}).get('required', 1)
                and 0 < excluded <= len({f['person_id'] for f in causes})):
            shortfalls = derived.setdefault(id(causes[0]), [])
            shortfalls.append({**finding, 'detail': {k: v for k, v in (finding.get('detail') or {}).items()
                                                   if k != 'derived_findings'}})
            shortfalls.extend({**finding, **f} for f in (finding.get('detail') or {}).get('derived_findings', []))
        else:
            kept.append(finding)
    return [{**f, 'detail': {**(f.get('detail') or {}),
                            'derived_findings': [*(f.get('detail') or {}).get('derived_findings', []), *derived[id(f)]]}}
            if id(f) in derived else f for f in kept]


def normalized_score(score):
    if not score or not isinstance(score.get('settings'), dict):
        return score
    return {**score, 'settings': {k: v for k, v in score['settings'].items() if k != 'tolerance'}}


def normalized_check(check):
    """Project historical findings without changing stored validation or its safety verdict."""
    if not check:
        return check
    from .validation import INTERNAL_CHECK_KEYS
    check = {key: value for key, value in check.items() if key not in INTERNAL_CHECK_KEYS}
    result = check
    score = normalized_score(check.get('score')) if not check.get('scope') else None
    if 'findings' in check:
        from .validation import grouped_rule_findings
        findings = grouped_rule_findings(grouped_findings(check['findings'], check.get('coverage', [])), ())
        errors = sum(f['severity'] == 'error' for f in findings)
        result = {**check, 'findings': findings, 'error_count': errors,
                  'warning_count': sum(f['severity'] == 'warning' for f in findings)}
        if score:
            score = {**score, 'blocking_count': errors}
    if (score and score.get('score') is not None and 'coverage' in check
            and not any(row['status'] not in {'cancelled', 'closed'} for row in check['coverage'])):
        score = None
    if 'coverage' in result:
        from .coverage import coverage_rows
        result = {**result, 'coverage': coverage_rows(result)}
    return {**result, 'score': score} if 'score' in check else result


def cause_groups(findings):
    """Group the same missing opportunity across people/rules; keep their stable issue references."""
    groups = {}
    for finding in findings:
        cause = finding.get('cause')
        if not cause:
            continue
        hints = finding.get('fix_hints', [])
        selector = {k: v for k, v in (hints[0]['prefill'] if hints else {}).items()
                    if k not in {'person_id', 'rule_id', 'shift_ids', 'starts_on', 'ends_on'}}
        identity = json.dumps([cause['code'], finding['severity'], selector], sort_keys=True)
        group = groups.setdefault(identity, {'key': hashlib.sha256(identity.encode()).hexdigest()[:20],
            'code': cause['code'], 'severity': finding['severity'], 'count': 0,
            'finding_keys': set(), 'person_ids': set(), 'rule_ids': set(), 'fix_hints': {}})
        if finding['key'] not in group['finding_keys']:
            group['count'] += 1
            group['finding_keys'].add(finding['key'])
        for field in ('person_id', 'rule_id'):
            if finding.get(field):
                group[field + 's'].add(finding[field])
        for hint in hints:
            # A group can span independently checked windows. Each form keeps its own exact range.
            global_action = hint['action'] in {'add_sessions', 'run_auto_schedule', 'open_rota'}
            value = {**hint, 'prefill': {**hint['prefill'], **({'person_id': None, 'rule_id': None} if global_action else {})}}
            group['fix_hints'][json.dumps(value, sort_keys=True)] = value
    return [{**g, 'finding_keys': sorted(g['finding_keys']), 'person_ids': sorted(g['person_ids']), 'rule_ids': sorted(g['rule_ids']),
             'fix_hints': list(g['fix_hints'].values())} for g in groups.values()]


def repair_task_identity(finding_key, observation):
    from .finding_causes import COVERAGE_CODES
    detail = observation.get('detail') or {}
    shifts = sorted(set(detail.get('affected_shift_ids') or ([observation['shift_id']] if observation.get('shift_id') else [])))
    interval = observation['code'] in COVERAGE_CODES and shifts and observation.get('start') and observation.get('end')
    identity = json.dumps(['coverage', shifts, observation['start'], observation['end']] if interval else ['issue', finding_key], sort_keys=True)
    return shifts, identity


def repair_counts(findings):
    """Compact counts use the full links' task identities, without constructing references, causes or hints."""
    from .validation import finding_observations
    blocking, advisory, observations = set(), set(), 0
    for finding in findings:
        key = '|'.join(str(finding.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))
        for observation in finding_observations({'findings': [finding]}):
            observations += 1
            if observation['severity'] in {'error', 'warning'}:
                _, identity = repair_task_identity(key, observation)
                (blocking if observation['severity'] == 'error' else advisory).add(identity)
    return {'total_tasks': len(blocking | advisory), 'blocking_tasks': len(blocking),
            'advisory_tasks': len(advisory - blocking), 'observations': observations}


def requirement_references(findings, accepted_issues):
    """Exact source/interval observations behind compatible issue keys and overlapping repair tasks."""
    from .validation import finding_observations
    accepted_issues = {a['key'] if isinstance(a, dict) else a for a in accepted_issues}
    issues, observations, tasks = [], [], {}
    for finding in findings:
        key = '|'.join(str(finding.get(k) or '') for k in ('code', 'rule_id', 'shift_id', 'person_id', 'start'))
        issues.append({**{k: finding.get(k) for k in ('code', 'severity', 'shift_id', 'person_id', 'rule_id')},
                       **{k: finding[k] for k in ('cause', 'fix_hints') if k in finding},
                       'key': key, 'accepted': key in accepted_issues})
        for observation in finding_observations({'findings': [finding]}):
            detail = observation.get('detail') or {}
            ref = {**{k: observation.get(k) for k in ('code', 'severity', 'shift_id', 'person_id', 'rule_id', 'start', 'end')},
                   'finding_key': key, 'line': detail.get('line'), 'slot': detail.get('slot')}
            # Include every source dimension, so independent same-window requirements never share an ID.
            identity = json.dumps([ref, detail], sort_keys=True)
            ref['id'] = hashlib.sha256(identity.encode()).hexdigest()
            observations.append(ref)
            if observation['severity'] not in {'error', 'warning'}:
                continue
            shifts, task_identity = repair_task_identity(key, observation)
            task = tasks.setdefault(task_identity, {'key': hashlib.sha256(task_identity.encode()).hexdigest(),
                'shift_ids': shifts, 'start': ref['start'], 'end': ref['end'], 'severity': observation['severity'],
                'finding_keys': set(), 'observation_ids': set()})
            if observation['severity'] == 'error':
                task['severity'] = 'error'
            task['finding_keys'].add(key)
            task['observation_ids'].add(ref['id'])
    return issues, observations, [{**task, 'finding_keys': sorted(task['finding_keys']),
        'observation_ids': sorted(task['observation_ids'])} for task in tasks.values()]


def cause_summary(validation, accepted_issues=()):
    """The bounded cause-panel projection, without sessions or full Fix-list evidence."""
    from .validation import finding_observations
    check = normalized_check(validation) or {}
    findings = check.get('findings', [])
    issues, observations, tasks = requirement_references(findings, accepted_issues)
    cause_refs = []
    for finding, ref in zip(findings, issues):
        for observation in finding_observations({'findings': [finding]}):
            if observation.get('cause'):
                cause_refs.append({**ref, 'severity': observation['severity'],
                                   'cause': observation['cause'],
                                   'fix_hints': observation.get('fix_hints', [])})
    return {'observations': observations, 'repair_tasks': tasks,
            'cause_groups': cause_groups(cause_refs)}


def blocking_without_open_places(findings, deficits, legacy_open):
    """A parent belongs to open-seat repair only if every mandatory observation fits that exact deficit."""
    from .validation import finding_observations

    def seat_repair(observation):
        detail = observation.get('detail') or {}
        # Old unsegmented base gaps already denote an open place; never infer a scoped requirement from them.
        if (observation['code'] == 'COVERAGE_GAP' and not observation.get('start') and not observation.get('end')
                and legacy_open.get(observation.get('shift_id'), 0) > 0):
            return True
        if (observation['code'] not in {'COVERAGE_GAP', 'SCOPED_COVERAGE_GAP'}
                or detail.get('affected_shift_ids') or not observation.get('start') or not observation.get('end')):
            return False
        deficit = deficits.get((observation.get('shift_id'), aware(observation['start']), aware(observation['end'])), 0)
        return 0 < detail.get('required', 0) - detail.get('available', 0) <= deficit

    return sum(f['severity'] == 'error' and not all(seat_repair(o) for o in
        finding_observations({'findings': [f]}) if o['severity'] == 'error') for f in findings)


def rota_summary(validation, shifts=None, accepted_issues=(), *, compact=False, cache=None):
    """A place is one required person on a session, filled throughout its required intervals.

    Count a session once: peak required cover and greatest coverage deficit across
    its intervals, including scoped/skill requirements. Non-staffing safety issues remain separate.
    Issues count findings, rather than affected sessions or people.
    """
    if cache is not None:
        from copy import deepcopy
        from .input_manifest import digest
        from .validation import INTERNAL_CHECK_KEYS
        value = {key: item for key, item in (validation or {}).items()
                 if key not in INTERNAL_CHECK_KEYS | {'fix_list_delta', '_command_scope'}}
        accepted = sorted(accepted_issues) if isinstance(accepted_issues, (set, frozenset)) else accepted_issues
        key = digest([bool(validation), value, shifts, accepted, compact])
        if key not in cache:
            cache[key] = rota_summary(validation, shifts, accepted_issues, compact=compact)
        return deepcopy(cache[key])
    check = normalized_check(validation) or {}
    from .validation import finding_observations
    sessions, deficits, legacy_open = {}, {}, {}
    for row in check.get('coverage', []):
        if row['status'] in {'cancelled', 'closed'}:
            continue
        first_interval = row['shift_id'] not in sessions
        session = sessions.setdefault(row['shift_id'], {'shift_id': row['shift_id'],
            'places_needed': 0, 'places_open': 0, 'extra_cover_places_open': 0, 'has_gap': False})
        # Historical checks may only retain a gap status: it still represents at least one required place.
        required = row['places_needed']
        session['places_needed'] = max(session['places_needed'], required)
        deficit = row['places_open']
        session['places_open'] = max(session['places_open'], deficit)
        if row.get('start') and row.get('end') and row['status'] == 'gap':
            deficits[row['shift_id'], aware(row['start']), aware(row['end'])] = deficit
        if first_interval and not row.get('start') and not row.get('end'):
            legacy_open[row['shift_id']] = deficit
        else:
            legacy_open.pop(row['shift_id'], None)
        session['has_gap'] |= row['status'] == 'gap'
    for finding in finding_observations(check):
        session = sessions.get(finding.get('shift_id'))
        if finding['code'] == 'OPEN_EXTRA_COVER' and session is not None:
            detail = finding.get('detail') or {}
            session['extra_cover_places_open'] = max(session['extra_cover_places_open'],
                max(0, detail.get('required', 0) - detail.get('available', 0)))
    for session in sessions.values():
        session['places_filled'] = session['places_needed'] - session['places_open']
        session['other_gaps'] = session['has_gap'] and not session['places_open']
    findings = check.get('findings', [])
    issue_refs, observations, tasks, cause_refs = [], [], [], []
    if not compact:
        issue_refs, observations, tasks = requirement_references(findings, accepted_issues)
        for finding, ref in zip(findings, issue_refs):
            for observation in finding_observations({'findings': [finding]}):
                if observation.get('cause'):
                    cause_refs.append({**ref, 'severity': observation['severity'], 'cause': observation['cause'],
                                       'fix_hints': observation.get('fix_hints', [])})
    for shift in shifts or []:
        if inactive(shift):
            continue
        session = sessions.setdefault(shift['id'], {'shift_id': shift['id'], 'places_needed': 0,
            'places_filled': 0, 'places_open': 0, 'extra_cover_places_open': 0, 'has_gap': False, 'other_gaps': False})
        session.update(start=shift['start'], end=shift['end'], locked=shift.get('locked', False),
                       person_ids=sorted({p['person_id'] for p in shift['participants']}))
    issues = {'blocking': check.get('error_count', 0), 'advisory': check.get('warning_count', 0)}
    issues['blocking_without_open_places'] = blocking_without_open_places(findings, deficits, legacy_open)
    score = check.get('score') if sessions else None
    if score is not None:
        uncapped = score.get('uncapped_score', score.get('raw', score['score']))
        score = {**score, 'uncapped_score': uncapped, 'blocking_count': issues['blocking'],
                 'capped': score['capped'] if isinstance(score.get('capped'), bool) else score['score'] < uncapped}
    result = {'checked': bool(validation), 'scope': check.get('scope'),
            'valid': bool(sessions) and check.get('valid') if validation else None,
            **{field: sum(s[field] for s in sessions.values())
               for field in ('places_needed', 'places_filled', 'places_open', 'extra_cover_places_open')},
            'sessions_with_gaps': sum(s['has_gap'] for s in sessions.values()),
            'other_gaps': sum(s['other_gaps'] for s in sessions.values()),
            'sessions_with_open_extra_cover': sum(s['extra_cover_places_open'] > 0 for s in sessions.values()),
            'issues': issues, 'score': score, 'session_count': len(sessions), 'repair_counts': repair_counts(findings),
            **({} if compact else {'sessions': list(sessions.values()), 'findings': issue_refs,
                'observations': observations, 'repair_tasks': tasks, 'cause_groups': cause_groups(cause_refs)})}
    if note := population_note(check):
        result['population_note'] = note
    # Issued-layer pills count stored findings; draft Fix lists count repair causes.
    extra = [f for f in findings if f['code'] == 'OPEN_EXTRA_COVER' and f['severity'] == 'warning']
    result['fix_list'] = fix_list(check, result, accepted_issues)
    # SX-13: the issued layer's footer says causes as the rota's Fix list does ("5 could improve"), not stored checks.
    result['issued_counts'] = {'sessions_with_gaps': result['sessions_with_gaps'],
        'blocking': issues['blocking'], 'advisory': issues['advisory'] - len(extra),
        'extra_cover_sessions': len({f.get('shift_id') for f in extra if f.get('shift_id')}),
        'must': result['fix_list']['summary']['must'], 'could': result['fix_list']['summary']['could']}
    return result


def human_date_range(starts_on, ends_on):
    start, last = date.fromisoformat(str(starts_on)), date.fromisoformat(str(ends_on)) - timedelta(days=1)
    if start == last:
        dates = f'{start.day} {start:%b %Y}'
    elif (start.year, start.month) == (last.year, last.month):
        dates = f'{start.day}–{last.day} {last:%b %Y}'
    else:
        dates = f'{start.day} {start:%b %Y} – {last.day} {last:%b %Y}'
    return dates


def suggested_rota_name(starts_on, ends_on):
    return 'Rota ' + human_date_range(starts_on, ends_on)


def rota_coverage_gaps(rotas):
    """Uncovered date intervals between the union of non-archived rota periods."""
    rows = sorted(rotas, key=lambda r: (r['starts_on'], r['ends_on'], r['id']))
    gaps, previous = [], None
    for row in rows:
        if previous is not None and row['starts_on'] > previous['ends_on']:
            start, end = previous['ends_on'], row['starts_on']
            gaps.append({'starts_on': str(start), 'ends_on': str(end),
                         'days': (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days,
                         'before_workspace_id': previous['id'], 'after_workspace_id': row['id'],
                         'suggested_name': suggested_rota_name(start, end)})
        if previous is None or row['ends_on'] > previous['ends_on']:
            previous = row
    return gaps


def planning_projection(data, groups, value, *, identity_id, selected_version_id=None, validation=None):
    """Group presentation of the canonical causes; global totals and evidence never change."""
    from .planning_groups import GroupResolver
    from .conditions import scope_matches_throughout, tag_scope
    from .validation import prepare_assignment_trials
    data = prepare_assignment_trials(data) if groups else data
    resolver = GroupResolver(data, groups)
    heads = {gid: max(rows, key=lambda g: g['revision']) for gid, rows in resolver.groups.items()}
    editable = sorted(gid for gid, rows in resolver.groups.items() if any(identity_id in
        set(g['owner_identity_ids']) | set(g.get('co_editor_identity_ids', [])) for g in rows if g.get('active', True)))
    selected = next((g for rows in resolver.groups.values() for g in rows if g['version_id'] == selected_version_id), None)
    if selected is None and selected_version_id is None and len(editable) == 1:
        selected = heads[editable[0]]
    selected_id = selected['group_id'] if selected else None
    shifts = {s['id']: s for s in data['shifts']}
    focus = set(data['focus_ids'])
    population_cache = {}
    causes, partition = [], dict.fromkeys(('mine', 'coupling', 'other_pending', 'unassigned'), 0)
    totals = {gid: {'must': 0, 'could': 0, 'acknowledged': 0} for gid in heads}
    held = set()
    for cause in value['causes']:
        responsible, versions, unassigned = set(), set(), False
        sessions = [shifts[sid] for sid in cause['shift_ids'] if sid in shifts]
        if not sessions:
            sessions = [s for s in data['shifts'] if s['id'] in focus]
        complete = bool(sessions)
        for shift in sessions:
            pids = set(cause['person_ids'])
            if not pids:
                who = cause.get('who') or [{'match': 'all', 'conditions': []}]
                for condition in who:
                    key = json.dumps(condition, sort_keys=True), shift['id']
                    if key not in population_cache:
                        scope = tag_scope(data, {'conditions': condition})
                        population_cache[key] = {pid for pid in data['people'] if scope_matches_throughout(
                            data, scope, pid, shift, aware(shift['start']), aware(shift['end']))}
                    pids.update(population_cache[key])
            participants = {p['person_id']: p for p in shift.get('participants', [])}
            if any('SUPERVIS' in e.get('code', '') or e.get('rule_kind') == 'supervision' for e in cause['evidence']):
                pids.update(p.get('supervisor_person_id') for pid, p in participants.items()
                            if pid in pids and p.get('supervisor_person_id'))
            if not pids:
                unassigned, complete = True, False
            for pid in pids:
                group = resolver.primary(pid, shift, participants.get(pid))
                if group is None:
                    unassigned = True
                else:
                    responsible.add(group['group_id'])
                    versions.update(group['responsibility_version_ids'])
        classification = ('coupling' if len(responsible) > 1 or responsible and unassigned else
            'unassigned' if unassigned or not responsible else 'mine' if selected_id in responsible else 'other_pending')
        partition[classification] += 1
        blocking = cause['level'] == 'must' and not cause.get('acknowledgement')
        for gid in responsible:
            totals[gid][cause['level']] += 1
            totals[gid]['acknowledged'] += int(bool(cause.get('acknowledgement')))
        attribution = {'classification': classification, 'group_ids': sorted(responsible),
            'responsible_group_ids': sorted(responsible), 'required_group_ids': sorted(responsible) if classification == 'coupling' else [],
            'responsibility_version_ids': sorted(versions), 'complete': complete and not unassigned}
        causes.append({**cause, 'attribution': attribution, 'workflow_effect': {
            'blocks_group_completion': blocking and classification in {'mine', 'coupling', 'unassigned'},
            'blocks_submission': blocking, 'acknowledgeable': cause['acknowledgeable']}})
    # Holds describe actual cross-group pairs, not the number of broken checks.
    for shift in data['shifts']:
        if shift['id'] not in focus:
            continue
        participants = {p['person_id']: p for p in shift.get('participants', [])}
        for pid, p in participants.items():
            sup = participants.get(p.get('supervisor_person_id'))
            if sup:
                a, b = resolver.primary(pid, shift, p), resolver.primary(sup['person_id'], shift, sup)
                if a and b and a['group_id'] != b['group_id'] and (selected_id is None or selected_id in {a['group_id'], b['group_id']}):
                    held.add(shift['id'])
    # Shared cover pools can be filled by several groups: project their canonical coverage, never add group totals.
    from .validation import qualified, rule_coverage_people, effective_type_definition, index_person_rows
    cover_data = {**data, '_person_rows': index_person_rows(data), '_qualification_cache': {}}
    group_cover = {gid: [] for gid in heads}
    for row in (validation or {}).get('coverage', []):
        shift = shifts.get(row['shift_id'])
        if shift is None or shift['id'] not in focus or row['status'] in {'cancelled', 'closed'}:
            continue
        a, b = aware(row.get('start') or shift['start']), aware(row.get('end') or shift['end'])
        interval = {'start': a, 'end': b}
        definition = effective_type_definition(cover_data, shift)
        eligible = set()
        for pid in data['people']:
            if qualified(cover_data, pid, shift, definition, a, b):
                group = resolver.primary(pid, shift, interval)
                if group:
                    eligible.add(group['group_id'])
        available = dict.fromkeys(eligible, 0)
        for person in rule_coverage_people(cover_data, shift, a, b, definition=definition):
            group = resolver.primary(person['person_id'], shift, interval)
            if group and group['group_id'] in available:
                available[group['group_id']] += 1
        for gid, count in available.items():
            group_cover[gid].append({**row, 'available_eligible': count})
    fill = {gid: rota_summary({'coverage': rows, 'findings': []}, compact=True) for gid, rows in group_cover.items()}
    return {**value, 'causes': causes, 'planning_context': {
        'group_version_id': selected['version_id'] if selected else None, 'group_id': selected_id,
        'capabilities': {'editable_group_ids': editable}, 'counts': partition,
        'held': {'count': len(held), 'session_ids': sorted(held)},
        'groups': [{'group_id': gid, 'group_version_id': g['version_id'], 'name': g['name'],
            'filled': fill[gid]['places_filled'], 'total': fill[gid]['places_needed'],
            'is_mine': gid in editable, **totals[gid], 'ready': not any(c['level'] == 'must' and
                not c.get('acknowledgement') and gid in c['attribution']['group_ids'] for c in causes)} for gid, g in sorted(heads.items())]}}
