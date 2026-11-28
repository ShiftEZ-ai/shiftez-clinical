"""Bounded assumption diagnosis on a copy of the already-built solver model."""
from .time import now as clock_now
import time

from ortools.sat.python import cp_model
from .solver_provenance import requirement, source_key
from .solver_capacity import capacity_core, arithmetic


def fair_share_search_notice(objective, seconds, status):
    """The result warning is rendered visibly by the existing auto-schedule result panel, including workload."""
    search = (objective or {}).get('search', {})
    receipt = {**search, 'plateau_seconds': seconds, 'status': status}
    if status == 'OPTIMAL':
        return {**receipt, 'stop_reason': 'optimal', 'fairness_improving': False}, None
    if objective is None:
        return {**receipt, 'stop_reason': 'not_reached'}, 'Fair share optimization was not reached within the search allowance.'
    if (objective or {}).get('stopped_without_improvement') and search.get('stop_reason', 'plateau') == 'plateau':
        return {**receipt, 'stop_reason': 'plateau', 'fairness_improving': False}, (
            'Search stopped on the ordinary plateau; '
            + ('A better distribution may still exist.' if search.get('fairness_penalty') is None else
               'fairness was fully satisfied.' if search['fairness_penalty'] == 0 else
               f'fairness made no further progress within its {seconds}-second window. A better distribution may still exist.'))
    if search.get('fairness_improving'):
        reason = 'an operator stopped the run' if search.get('stop_reason') == 'operator_stop' else 'the search time allowance ended'
        return receipt, f'Search stopped while fairness was still improving; {reason}.'
    if search.get('stop_reason') == 'operator_stop':
        return receipt, 'An operator stopped Fair share search; a better distribution may still exist.'
    if search.get('stop_reason') in {'time_limit', 'slice_limit'}:
        return receipt, 'Fair share search exhausted its time allowance; a better distribution may still exist.'
    return receipt, 'Fair share search ended before proving optimality; a better distribution may still exist.'


def explain_unmet_requirements(data, validation, objectives):
    """Describe canonical shortfalls without treating incomplete optimization as infeasibility."""
    from .validation import finding_observations
    policies = {p['id']: p['name'] for p in data['policies']}
    rows, goals = [], {}
    for finding in finding_observations(validation):
        if finding['code'] not in {'QUOTA_BELOW_MINIMUM', 'SCOPED_COVERAGE_GAP', 'BALANCE_GOAL', 'BALANCE_PAIR'}:
            continue
        detail = finding.get('detail') or {}
        if detail.get('applies_to_nobody'):
            continue  # Empty configuration scope is not a search shortfall.
        actual = detail.get('actual', detail.get('available'))
        required = detail.get('amount', detail.get('required'))
        if finding['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'}:
            tier = 'weighted'
        else:
            tier = ('diagnostic_minima' if finding['code'] == 'QUOTA_BELOW_MINIMUM' else 'coverage') if finding['severity'] == 'error' else 'weighted'
        index = next((i for i, row in enumerate(objectives) if row['name'] == tier), None)
        pending = objectives[:index + 1] if index is not None else objectives
        incomplete = index is None or any(not row['proven_optimal'] for row in pending)
        stalled = incomplete and any(row.get('stopped_without_improvement') for row in pending)
        code = ('SEARCH_STOPPED_WITHOUT_IMPROVEMENT' if stalled else 'SEARCH_INCOMPLETE') if incomplete else 'OBJECTIVE_TRADEOFF'
        reason = ('Search stopped without further improvement; this shortfall was not proved unavoidable.' if stalled else
                  'Search ended before proving this shortfall unavoidable.' if incomplete else
                  'The optimized priorities retain this shortfall; this does not prove the individual requirement impossible.')
        if finding['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'} and detail.get('goal_id'):
            # Goal identities already distinguish comparison groups, count buckets and calendar windows.
            key = detail['goal_id']
            if key not in goals:
                goals[key] = {'rule_id': finding.get('rule_id'), 'goal_id': key, 'person_id': None,
                    'shift_id': None, 'start': finding.get('start'), 'end': finding.get('end'),
                    'severity': 'warning', 'code': code, 'finding_code': finding['code'],
                    'tier': tier, 'proven': False, 'person_ids': set(), 'detail': {'goal_id': key}}
                rows.append(goals[key])
            row = goals[key]
            if finding.get('person_id'):
                row['person_ids'].add(finding['person_id'])
            row['affected_people'] = len(row['person_ids'])
            row['message'] = (f"{policies.get(finding.get('rule_id'), 'Fair share')}: "
                              f"{row['affected_people']} {'person' if row['affected_people'] == 1 else 'people'} outside their band. {reason}")
            continue
        rows.append({**{k: finding.get(k) for k in ('rule_id', 'shift_id', 'person_id', 'start', 'end', 'severity')},
                     'code': code, 'finding_code': finding['code'], 'actual': actual, 'required': required,
                     'detail': detail, 'tier': tier, 'proven': False,
                     'message': (f"{finding.get('message', policies.get(finding.get('rule_id'), 'Fair share'))}: {reason}"
                                 if finding['code'] in {'BALANCE_GOAL', 'BALANCE_PAIR'}
                                 else f"{policies.get(finding['rule_id'], finding['message'])}: {actual} of {required}. {reason}")})
    for row in goals.values():
        row['person_ids'] = sorted(row['person_ids'])
    return rows


def explain_conflict(model, data, control, seconds):
    started = time.monotonic()
    previous = control.diagnostic_deadline
    control.diagnostic_deadline = started + seconds
    try:
        return _explain_conflict(model, data, control, seconds, started)
    finally:
        control.diagnostic_deadline = previous


def _explain_conflict(model, data, control, seconds, started):
    deadline = started + seconds
    control.check()
    control.stage('explaining')
    groups, references = {}, {}
    for index, constraint in enumerate(model.proto.constraints):
        control.check()
        if time.monotonic() >= deadline:
            break
        if constraint.name.startswith(('policy:', 'duty:')) and (constraint.has_linear() or constraint.has_bool_or()):
            ref = requirement(constraint.name, data)
            if ref is None:
                continue
            key = source_key(ref)
            references[index] = {**ref, 'key': key}
            groups.setdefault(key, []).append(index)
    constant_conflicts = [references[i]['key'] for i in references if model.proto.constraints[i].has_bool_or()
        and not model.proto.constraints[i].bool_or.literals and not model.proto.constraints[i].enforcement_literal]
    capacity = None if constant_conflicts else capacity_core(model, groups, references, control, deadline)
    status, keys, basis = cp_model.INFEASIBLE, list(dict.fromkeys(constant_conflicts)), 'full_model'
    selected_groups = groups
    if capacity:
        keys, selected_groups = capacity
        basis = 'capacity_relaxation'
    elif not constant_conflicts and time.monotonic() < deadline:
        diagnostic = model.clone()
        diagnostic.clear_objective()
        diagnostic.clear_hints()
        diagnostic.clear_assumptions()
        guards = {key: diagnostic.new_bool_var('diagnostic:' + key) for key in groups}
        for index, ref in references.items():
            diagnostic.proto.constraints[index].enforcement_literal.append(guards[ref['key']].index)
        diagnostic.add_assumptions(list(guards.values()))
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = max(0, deadline - time.monotonic())
        status = control.solve(solver, diagnostic, 'diagnostics')
        by_index = {flag.index: key for key, flag in guards.items()}
        keys = [by_index[i] for i in solver.sufficient_assumptions_for_infeasibility()] if status == cp_model.INFEASIBLE else []
    elif not constant_conflicts:
        status = cp_model.UNKNOWN
    implementation_error = status in (cp_model.MODEL_INVALID, cp_model.FEASIBLE, cp_model.OPTIMAL)
    requirements = list({model.proto.constraints[i].name: {k: v for k, v in references[i].items() if k != 'key'}
                         for key in keys for i in selected_groups[key]}.values())
    evidence = arithmetic(model, {key: selected_groups[key] for key in keys}, references, data, control, deadline) if keys else None
    message = ('The conflict check disagreed with the scheduling model or could not validate it. No proposal can be applied.' if implementation_error else
               ('These requirements cannot all be met together with eligibility, availability and the other mandatory rules: ' +
                ', '.join(dict.fromkeys(r['name'] + (f" (line {r['line'] + 1}, {r['slot']})" if r.get('line') is not None and r['line'] >= 0 else '')
                                      for r in requirements))) if keys else
               'The strict requirements cannot all be met, but the proof did not identify a named rule line.' if status == cp_model.INFEASIBLE else
               'The strict requirements cannot all be met. The cause is unknown: the check ran out of time before it could name the rules involved.')
    policies = {p['id']: p for p in data.get('policies', [])}
    sessions_by_id = {s['id']: s for s in data.get('shifts', [])}

    def session_context(ids):
        session_ids = sorted(set(ids))
        service_ids = sorted({service_id for sid in session_ids
                              for service_id in sessions_by_id.get(sid, {}).get('service_ids', [])})
        return session_ids, service_ids

    decisions = []
    for ref in requirements:
        if ref['kind'] != 'policy':
            continue
        session_ids, service_ids = session_context(ref.get('session_ids', []))
        target = {k: ref.get(k) for k in ('rule_id', 'line_id', 'line', 'slot', 'starts_on', 'ends_on')}
        decisions.append({'kind': 'open_rule', **target, 'service_ids': service_ids,
                          'person_ids': [], 'session_ids': session_ids,
                          'requires_approval': False, 'effect_status': 'not_checked'})
        if (not ref['safety'] and ref.get('comparison') == 'maximum' and ref.get('rule_kind') == 'block_quota'
                and len(ref['person_ids']) == 1 and ref.get('starts_on') and ref.get('ends_on')):
            decisions.append({'kind': 'propose_cap_change', **target, 'service_ids': service_ids,
                              'person_ids': ref['person_ids'], 'session_ids': session_ids,
                              'requires_approval': True, 'effect_status': 'not_checked'})
        policy = policies.get(ref.get('rule_id')) or {}
        definition = policy.get('definition') or {}
        if not ref['safety'] and definition.get('exception_allowed'):
            for person_id in ref.get('person_ids', []):
                decisions.append({'kind': 'person_exception', **target, 'person_id': person_id,
                                  'service_ids': service_ids, 'person_ids': [person_id],
                                  'session_ids': session_ids, 'requires_approval': True,
                                  'effect_status': 'not_checked'})
    if keys:
        session_ids, service_ids = session_context(evidence['session_ids'] or
                                                   [sid for r in requirements for sid in r['session_ids']])
        decisions.append({'kind': 'review_eligibility', 'service_ids': service_ids,
                          'person_ids': sorted({pid for r in requirements for pid in r['person_ids']}),
                          'session_ids': session_ids,
                          **{k: evidence[k] for k in ('starts_on', 'ends_on') if k in evidence},
                          'requires_approval': False, 'effect_status': 'not_checked'})
        # This is only a diagnostic choice. Safety requirements remain hard and
        # never receive a leave-open decision.
        if (evidence and evidence.get('complete')
                and (evidence.get('required_places') or 0) > (evidence.get('maximum_possible') or 0)
                and not any(ref.get('safety') for ref in requirements)):
            places = (evidence.get('required_places') or 0) - (evidence.get('maximum_possible') or 0)
            decisions.append({'kind': 'leave_open', 'service_ids': service_ids,
                              'person_ids': [], 'session_ids': session_ids, 'places': places,
                              'requires_approval': False, 'effect_status': 'not_checked'})
    import hashlib
    import json
    merged = {}
    for row in decisions:
        key = json.dumps({k: v for k, v in row.items() if k not in ('person_ids', 'session_ids')}, sort_keys=True)
        target = merged.setdefault(key, {**row, 'person_ids': set(), 'session_ids': set()})
        for field in ('person_ids', 'session_ids'):
            target[field].update(row[field])
    decisions = [{**row, 'person_ids': sorted(row['person_ids']), 'session_ids': sorted(row['session_ids'])}
                 for row in merged.values()]
    people_ids = sorted({pid for r in requirements for pid in r['person_ids']})
    session_ids = {sid for r in requirements for sid in r['session_ids']}
    cause_session_ids, cause_service_ids = session_context(
        (evidence or {}).get('session_ids') or session_ids)
    cause_starts = ((evidence or {}).get('starts_on') or
                    next((r.get('starts_on') for r in requirements if r.get('starts_on')), None))
    cause_ends = ((evidence or {}).get('ends_on') or
                  next((r.get('ends_on') for r in reversed(requirements) if r.get('ends_on')), None))
    elapsed = time.monotonic() - started
    result = {'code': 'DIAGNOSTIC_MODEL_ERROR' if implementation_error else 'CONFLICTING_REQUIREMENTS' if keys else 'CONFLICT_EXPLANATION_UNRESOLVED',
        'strict_status': 'INFEASIBLE', 'explanation_status': 'INFEASIBLE' if keys else 'MODEL_INVALID' if implementation_error else 'UNKNOWN',
        'implementation_error': implementation_error,
        'proof_kind': 'sufficient_set_with_background' if keys else 'unresolved', 'basis': basis,
        'proven': bool(keys), 'minimal': False, 'exhaustive': False,
        'requirements': requirements, 'input_fingerprint': data['fingerprint'],
        'people': [{'id': pid, 'name': data.get('people', {}).get(pid, {}).get('display_name', pid)}
                   for pid in people_ids],
        'sessions': [{'id': s['id'], 'name': s['type_snapshot']['name'], 'start': s['start'], 'end': s['end']}
                     for s in data.get('shifts', []) if s['id'] in session_ids],
        'checked_at': clock_now().isoformat(),
        'elapsed_seconds': elapsed, 'solver_status': cp_model.CpSolver().status_name(status), 'message': message,
        'causes': [{'key': hashlib.sha256('|'.join(sorted(keys)).encode()).hexdigest(),
                    'message': evidence['message'] if evidence['complete'] else message, 'requirements': requirements,
                    'evidence': evidence, 'decisions': decisions,
                    'session_ids': cause_session_ids, 'starts_on': cause_starts,
                    'ends_on': cause_ends, 'service_ids': cause_service_ids}] if keys else [],
        'timing': {'budget_seconds': seconds, 'elapsed_seconds': elapsed,
                   'stop_reason': 'model_error' if implementation_error else 'completed' if keys else
                                  'no_named_set' if status == cp_model.INFEASIBLE else 'allowance_exhausted'},
        'boundary': 'The listed requirements are enough to conflict, given clinical permissions, availability and protected work. They may not be the only cause: fixing them may still leave another conflict.'}
    result['elapsed_seconds'] = result['timing']['elapsed_seconds'] = time.monotonic() - started
    return result
