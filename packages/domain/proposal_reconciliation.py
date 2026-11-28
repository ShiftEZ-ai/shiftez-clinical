"""Select only frozen proposal bundles; current saved work is never rescheduled."""
import copy
import time
from collections import defaultdict

from ortools.sat.python import cp_model
from packages.contracts.schemas import GenerationOptions, ParticipantInput
from .input_manifest import shift_input, manifest
from .recurrence import inactive, series_candidate_allowed
from .validation import evaluate
from .time import aware
from .feasibility import compare_findings


def comparison(before, after, *, aggregate_coverage=True, severities=('error',)):
    def check(value):
        return {**value, 'status': 'candidate_feasible' if value['valid'] else 'requirements_conflict',
                'diagnostics': [], 'candidate_evidence': []}
    result = compare_findings(check(before), check(after), aggregate_coverage=aggregate_coverage,
                              severities=severities, protect_intervals=True)
    if not all((value.get('quota_report') or {}).get('complete', True) for value in (before, after)):
        result.update(comparison_complete=False, no_new_problems=False)
    return result


def participant(row):
    value = ParticipantInput.model_validate({key: row[key] for key in ParticipantInput.model_fields if key in row}).model_dump(mode='json')
    for key in ('start', 'end'):
        value[key] = aware(value[key]).isoformat()
    value['segments'] = [{key: aware(part[key]).isoformat() for key in ('start', 'end')} for part in value['segments']]
    return value


def overlay(data, result, additions, removals):
    value = copy.deepcopy(data)
    removed = {result['removals'][i]['assignment_id'] for i in removals}
    changes = defaultdict(list)
    for i in additions:
        addition = result['additions'][i]
        changes[addition['shift_id']].append({**participant(addition['participant']),
            'id': addition['participant'].get('id') or 'proposal-' + str(i), 'revision': 1})
    touched = set(changes) | {result['removals'][i]['shift_id'] for i in removals}
    for row in value['shifts']:
        if row['id'] not in touched:
            continue
        row['participants'] = [p for p in row['participants'] if p['id'] not in removed] + changes[row['id']]
        row['revision'] += 1
        if row.get('series_id'):
            row['series_modified'] = True
    value['workspace']['content_revision'] += 1
    value['input_manifest'] = manifest(value)
    value['fingerprint'] = value['input_manifest']['token']
    return value


def reconcile(current, frozen, result, addition_indexes, removal_indexes, *, baseline=None, evaluator=None):
    evaluator = evaluator or evaluate
    original = {s['id']: s for s in frozen['shifts']}
    now = {s['id']: s for s in current['shifts']}
    bundles = {}
    replacement_sessions = {result['removals'][i]['shift_id'] for i in removal_indexes}
    owners = {}
    for field, indexes in (('addition_indexes', addition_indexes), ('removal_indexes', removal_indexes)):
        for i in indexes:
            row = result['additions' if field == 'addition_indexes' else 'removals'][i]
            sid = row['shift_id']
            key = sid if sid in replacement_sessions else 'addition:' + str(i)
            bundle = bundles.setdefault(key, {'shift_id': sid, 'addition_indexes': [], 'removal_indexes': []})
            bundle[field].append(i)
            if field == 'addition_indexes':
                owners[sid, row['participant']['person_id']] = key
    # A proposed named-supervisor relationship is one inseparable proposal bundle.
    for i in addition_indexes:
        row = result['additions'][i]
        own = owners[row['shift_id'], row['participant']['person_id']]
        supervisor = owners.get((row['shift_id'], row['participant'].get('supervisor_person_id')))
        if supervisor and own != supervisor and own in bundles and supervisor in bundles:
            for field in ('addition_indexes', 'removal_indexes'):
                bundles[supervisor][field].extend(bundles[own][field])
            del bundles[own]
            owners = {pair: supervisor if key == own else key for pair, key in owners.items()}
    conflicts, eligible = [], {}
    for key, bundle in bundles.items():
        sid = bundle['shift_id']
        shift, old = now.get(sid), original.get(sid)
        code = None
        if not shift or not old or shift_input(shift) != shift_input(old):
            code = 'CONTENT_CONFLICT'
        elif shift['locked'] or shift.get('cancelled') or inactive(shift):
            code = 'SESSION_NOT_EDITABLE'
        elif any(not series_candidate_allowed(current, shift, result['additions'][i]['participant']['person_id'])
                 for i in bundle['addition_indexes']):
            code = 'RECURRING_POOL_CHANGED'
        if code:
            conflicts.append({'bundle_id': key, **bundle, 'code': code, 'findings': [],
                'launch': shift_input(old) if old else None, 'current': shift_input(shift) if shift else None})
        else:
            eligible[key] = bundle
    for sid, bundle in bundles.items():
        bundle['person_ids'] = sorted({result['additions'][i]['participant']['person_id'] for i in bundle['addition_indexes']} |
                                     {result['removals'][i]['person_id'] for i in bundle['removal_indexes']})
    def indexes(selected):
        return ([i for sid in selected for i in eligible[sid]['addition_indexes']],
                [i for sid in selected for i in eligible[sid]['removal_indexes']])
    accepted = list(eligible)
    ai, ri = indexes(accepted)
    candidate = overlay(current, result, ai, ri)
    checked = evaluator(candidate)
    # A complete valid candidate cannot worsen a mandatory finding.  Only a
    # candidate with gaps needs the baseline comparison and subset search.
    if checked['valid'] and (checked.get('quota_report') or {}).get('complete', True):
        changes = {'new_or_worse_findings': [], 'comparison_complete': True, 'no_new_problems': True}
    else:
        baseline = evaluator(current) if baseline is None else baseline
        changes = comparison(baseline, checked)
    optimal, solver_status, allowance_exhausted = True, None, False
    full_conflicts = changes['new_or_worse_findings']
    if full_conflicts or not changes['comparison_complete']:
        accepted, optimal, solver_status = retain_bundles(current, result, eligible)
        allowance_exhausted = solver_status == 'UNKNOWN'
        ai, ri = indexes(accepted)
        candidate = overlay(current, result, ai, ri)
        checked = evaluator(candidate)
        changes = comparison(baseline, checked)
        if not changes['no_new_problems']:
            accepted, ai, ri = [], [], []
            candidate, checked = current, baseline
            solver_status, optimal = 'UNRESOLVED', False
        for sid in sorted(eligible.keys() - set(accepted)):
            bundle = eligible[sid]
            findings = [f for f in full_conflicts if f.get('proven') is not False
                        and not f.get('implementation_error') and (
                            f.get('shift_id') == bundle['shift_id'] or
                            f.get('person_id') in bundle['person_ids'])]
            conflicts.append({'bundle_id': sid, **bundle,
                              'code': 'MANDATORY_CONFLICT' if findings else 'RECONCILIATION_UNRESOLVED',
                              'findings': findings})
    for conflict in conflicts:
        conflict['person_ids'] = bundles[conflict['bundle_id']]['person_ids']
    status = ('partial' if conflicts else 'ready') if ai or ri else (
        'content_conflict' if any(c['code'] == 'CONTENT_CONFLICT' for c in conflicts) else 'unresolved')
    return {'status': status, 'addition_indexes': sorted(ai), 'removal_indexes': sorted(ri),
            'conflicting_placements': conflicts, 'retention_optimal': optimal,
            'solver_status': solver_status, 'reconciliation_allowance_exhausted': allowance_exhausted,
            'validation': checked, 'data': candidate}


def retain_bundles(current, result, bundles):
    """Reuse CP-SAT constraints, cap each existing deficit, then maximize atomic edit bundles.

    The final canonical comparison remains authoritative. A baseline that cannot be
    represented safely or is not proven optimal retains the proposal and commits nothing.
    """
    from .cp_sat import _prepare
    from .solver_control import SolverControl, TimeLimit
    options = GenerationOptions.model_validate(current.get('generation_options') or {})
    started = time.monotonic()
    seconds = options.diagnostic_seconds
    data = copy.deepcopy(current)
    data['generation_options'] = {**options.model_dump(mode='json'), 'mode': 'rebalance'}
    options = GenerationOptions.model_validate(data['generation_options'])
    data['generation_session_ids'] = sorted({b['shift_id'] for b in bundles.values()})
    removed = {result['removals'][i]['assignment_id'] for b in bundles.values() for i in b['removal_indexes']}
    for row in data['shifts']:
        row['participants'] = [{**p, 'fixed': True} if p['id'] not in removed else p for p in row['participants']]
    control = SolverControl()
    control.deadline = started + seconds
    try:
        prepared = _prepare(data, seconds, 1, control, options, False, True)
    except TimeLimit:
        return [], False, 'UNKNOWN'
    if 'status' in prepared:
        return [], False, prepared['status']
    model, choices = prepared['model'], prepared['choices']
    base = {(s['id'], p['person_id']): p for s in current['shifts'] for p in s['participants']}
    desired = {}
    flags = {sid: model.new_bool_var('retain:' + sid) for sid in bundles}
    for key, bundle in bundles.items():
        sid = bundle['shift_id']
        for i in bundle['removal_indexes']:
            row = result['removals'][i]
            desired[sid, row['person_id']] = (key, None)
        for i in bundle['addition_indexes']:
            row = result['additions'][i]
            desired[sid, row['participant']['person_id']] = (key, row['participant'])
    pairing_subjects = {(sid, pid) for sid, pid, _supervisor in prepared['pairings']}
    exact = True
    for pair in base.keys() | desired.keys():
        represented = prepared['participants'].get(pair)
        frozen_rows = [base.get(pair), desired.get(pair, (None, None))[1]]
        for row in frozen_rows:
            if row is None:
                continue
            if represented is None:
                exact = False
                continue
            proposed, compiled = participant(row), participant(represented)
            exact = exact and all(proposed[key] == compiled[key] for key in ('start', 'end', 'segments', 'participation'))
            for key, variables in (('overtime', prepared['overtime_choices']), ('team_lead', prepared['lead_vars'])):
                if pair not in variables and proposed[key] != compiled[key]:
                    exact = False
            if pair not in pairing_subjects:
                exact = exact and proposed['supervisor_person_id'] == compiled['supervisor_person_id']
    for pair in set(choices) | set(desired):
        old = base.get(pair)
        sid, new = desired.get(pair, (None, old))
        flag = flags[sid] if sid is not None else 0
        x = choices.get(pair, 0)
        model.add(x == int(old is not None) + (int(new is not None) - int(old is not None)) * flag)
        # A changed role, overtime flag or named supervisor is part of the same indivisible edit.
        for field, variables in (('overtime', prepared['overtime_choices']), ('team_lead', prepared['lead_vars'])):
            if pair in variables:
                before, after = int(bool((old or {}).get(field))), int(bool((new or {}).get(field)))
                model.add(variables[pair] == before + (after - before) * flag)
    for (sid, pid, supervisor), variable in prepared['pairings'].items():
        old = base.get((sid, pid)) or {}
        owner, new = desired.get((sid, pid), (None, old))
        flag = flags[owner] if owner is not None else 0
        before, after = int(old.get('supervisor_person_id') == supervisor), int((new or {}).get('supervisor_person_id') == supervisor)
        model.add(variable == before + (after - before) * flag)
    baseline = model.clone()
    for flag in flags.values():
        baseline.add(baseline.get_bool_var_from_proto_index(flag.index) == 0)
    domains = prepared['diagnostic_domains']
    baseline.minimize(sum(baseline.get_int_var_from_proto_index(i) for i in domains))
    def solve(value):
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.max_time_in_seconds = max(.001, started + seconds - time.monotonic())
        try:
            return solver, control.solve(solver, value, 'diagnostics')
        except TimeLimit:
            return solver, cp_model.UNKNOWN
    baseline_solver, status = solve(baseline)
    if status != cp_model.OPTIMAL:
        return [], False, baseline_solver.status_name(status)
    for index in domains:
        # Each source/interval keeps its own deficit ceiling; no cross-person compensation.
        model.add(model.get_int_var_from_proto_index(index) <= baseline_solver.value(baseline.get_int_var_from_proto_index(index)))
    model.maximize(sum(flag * (len(bundles[key]['addition_indexes']) + len(bundles[key]['removal_indexes']))
                       for key, flag in flags.items()))
    solver, status = solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], False, solver.status_name(status)
    return [sid for sid, flag in flags.items() if solver.value(flag)], status == cp_model.OPTIMAL and exact, solver.status_name(status)
