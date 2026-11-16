"""Joint CP-SAT completion evidence, projected to one authorized group's actual changes."""
from copy import deepcopy
from .proposal_reconciliation import overlay, comparison
from .validation import evaluate
from .generation_scope import population_allows, scope_receipt, selected_session_ids
from .rota_summary import generation_fix_lists
from .input_manifest import digest
from .score import schedule_score
from .solver_summary import summarise


def witness_progress(report):
    if report is None:
        return None
    def update(value):
        # A joint incumbent is not the actual-only proposal; publish stages without fictional roster/score changes.
        projected = {key: child for key, child in value.items() if key not in
            {'events', 'solution', 'best_score', 'shared_validation_passed', 'selected_scope_validation_passed'}}
        if projected.get('stage') == 'ready':
            projected['stage'] = 'validating'
        report({**projected, 'proof_scope': 'coupled_completion'})
    return update


def generate(snapshot, solve, *, control=None):
    joint = deepcopy(snapshot)
    joint.pop('lookahead', None)
    joint['_group_witness_population'] = joint.pop('planning_population')
    joint['generation_session_ids'] = list(snapshot['focus_ids'])
    # All existing outsider work is fixed. Other populations may receive witness additions only.
    scope = joint.pop('generation_scope')
    joint['_group_keep_existing'] = scope['protection'] == 'keep_existing'
    if joint['_group_keep_existing']:
        for shift in joint['shifts']:
            for person in shift['participants']:
                person['_witness_fixed'] = True
    result = solve(joint)
    check = control.check if control is not None else lambda: None
    check()
    full = result.get('validation') or {}
    diagnostic = bool(result.get('diagnostic_proposal') or snapshot.get('diagnostic_proposal'))
    feasible = not diagnostic and result.get('status') == 'candidate_feasible' and full.get('valid') is True
    status = ('OPTIMAL' if result.get('optimality_proven') else 'FEASIBLE') if feasible else (
        'INFEASIBLE' if result.get('status') == 'requirements_conflict' else 'UNKNOWN')
    completion = {'status': status, 'required_group_ids': sorted({g['group_id'] for g in snapshot.get('planning_group_versions', [])
        if g['group_id'] != snapshot['planning_group_id']}), 'input_fingerprint': snapshot['fingerprint'],
        'witness_hash': digest({'additions': result.get('additions'), 'removals': result.get('removals')}) if feasible else None}
    meta = {**scope_receipt(snapshot), 'lookahead': 'coupled', 'proof_scope': 'coupled_completion',
            'completion': completion, 'optimality_proven': False}
    shifts = {s['id']: s for s in snapshot['shifts']}
    additions = [a for a in result.get('additions', []) if feasible and a['shift_id'] in shifts and
        population_allows(snapshot, shifts[a['shift_id']], a['participant']['person_id'], a['participant'])]
    removals = [r for r in result.get('removals', []) if feasible and r['shift_id'] in shifts and
        population_allows(snapshot, shifts[r['shift_id']], r['person_id'])]
    # A trainee cannot be saved with a supervisor that exists only in the witness.
    removed = {(r['shift_id'], r['person_id']) for r in removals}
    real = {(s['id'], p['person_id']) for s in snapshot['shifts'] for p in s['participants']} - removed
    real.update((a['shift_id'], a['participant']['person_id']) for a in additions)
    own_decisions = len(additions)
    additions = [a for a in additions if not a['participant'].get('supervisor_person_id') or
                 (a['shift_id'], a['participant']['supervisor_person_id']) in real]
    changed_decisions = len(additions) != own_decisions
    proposed = {**result, 'additions': additions, 'removals': removals}
    actual = overlay(snapshot, proposed, list(range(len(additions))), list(range(len(removals))))
    actual['_check_budget'] = check
    checked = evaluate(actual)
    before = evaluate({**snapshot, '_check_budget': check})
    if not comparison(before, checked)['no_new_problems']:
        additions, removals, checked = [], [], before
        actual, changed_decisions = {**snapshot, '_check_budget': check}, True
    if feasible and changed_decisions:
        completion.update(status='UNKNOWN', witness_hash=None)
    proposed.update(additions=additions, removals=removals, validation=checked,
                    status=('candidate_with_gaps' if diagnostic else
                            ('candidate_feasible' if checked['valid'] else 'candidate_with_gaps') if feasible else result['status']))
    check()
    if control is not None:
        control.validated_result = {**proposed, **meta, 'summary': {}}
    # Display only actual placements/counts, including when no joint witness was established.
    summary = summarise(actual, checked, [], set(selected_session_ids(snapshot)))
    scoring = snapshot.get('generation_defaults', snapshot.get('generation_options'))
    score_before = schedule_score({**snapshot, **snapshot.get('score_context', {}), '_check_budget': check}, before, scoring)
    score_saved = schedule_score({**actual, **snapshot.get('score_context', {}), 'shifts': actual['shifts']}, checked, scoring)
    check()
    summary.update(score_before=score_before, score_saved=score_saved, score=score_saved)
    proposed['summary'] = summary
    proposed.update(generation_fix_lists({**snapshot, '_check_budget': check}, proposed,
        stopped=lambda: bool(control and (control.search_control() or {}).get('stop'))))
    from .generation_outcome import generation_outcome, score_value, solver_status
    final = {**proposed, **meta}
    final['solver_status'] = solver_status(final)
    final['score'] = score_value(final)
    return {**final, 'generation_outcome': generation_outcome(final)}
