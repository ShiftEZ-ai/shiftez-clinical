"""Prepare a current-input plan outside the writer lock; compare-and-commit owns writes."""
from copy import deepcopy
from uuid import uuid5, NAMESPACE_URL
from packages.contracts import schemas as s
from packages.domain.proposal_reconciliation import reconcile
from packages.domain.generation_scope import selected_session_ids
from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import currency_hint, domain_snapshot, preparation_stamp, snapshot_hash, workspace_content
from apps.api.input_changes import changed_facts, rules_changed
from .common import scoped
from .planning import draft


class GenerationInputsChanged(Exception):
    """Reacquire changed proposal inputs outside the global writer."""


def refresh_inputs(db, actor, command, plan):
    value = s.GenerationApply.model_validate(command.payload)
    workspace = scoped(db, m.Workspace, value.workspace_id, actor)
    current = domain_snapshot(db, workspace, own_shifts=workspace_content(db, workspace)['shifts'], mandatory_rules=True)
    return {**plan, 'current_data': current, 'current_stamp': preparation_stamp(db, workspace)}


def prepare(db, actor, command):
    value = s.GenerationApply.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    job = scoped(db, m.Job, value.job_id, actor)
    require(job.workspace_id == workspace.id and job.kind == 'generation' and job.status == 'succeeded',
            'JOB_STATE', 'Select a completed proposal for this rota', 409)
    result = job.result or {}
    from .jobs import require_application_toolchain, require_latest_run
    require_application_toolchain(job)
    require_latest_run(db, job)
    require(result.get('status') in {'candidate_feasible', 'candidate_with_gaps', 'unknown_time_limit'},
            'NO_CANDIDATE', 'This job did not produce an applicable proposal')
    def indexes(kind, requested):
        rows = result.get(kind, [])
        selected = list(range(len(rows))) if requested is None else sorted(set(requested))
        require(all(0 <= i < len(rows) for i in selected), 'SCHEMA_INVALID', 'Select suggestions from this proposal', 422)
        return selected
    ai, ri = indexes('additions', value.addition_indexes), indexes('removals', value.removal_indexes)
    require(ai or ri, 'NO_ADDITIONS', 'Choose at least one suggestion to apply')
    before = workspace_content(db, workspace)
    current = domain_snapshot(db, workspace, own_shifts=before['shifts'], mandatory_rules=True)
    captured = deepcopy(current)
    stamp = preparation_stamp(db, workspace)
    frozen = job.input_snapshot
    selected = set(selected_session_ids(frozen))
    require(all(result['additions'][i]['shift_id'] in selected for i in ai)
            and all(result['removals'][i]['shift_id'] in selected for i in ri),
            'INVALID_SCOPE', 'The proposal contains a session outside this run', 409)
    removed = {result['removals'][i]['assignment_id'] for i in ri}
    replacements = {(r['shift_id'], r['person_id']) for r in result.get('removals', []) if r['assignment_id'] not in removed}
    require(not any((result['additions'][i]['shift_id'], result['additions'][i]['participant']['person_id']) in replacements for i in ai),
            'INCOMPLETE_REPLACEMENT', 'Select the matching removal before applying a replacement assignment', 422)
    current_fingerprint = current['fingerprint']
    from packages.domain.generation_scope import union_population, run_population_people
    contribution = run_population_people(frozen)
    contribution = None if contribution is None else sorted(contribution)
    population = union_population(current, [{'population_recorded': True, 'population_people': contribution}])
    population_changed = current.get('applied_planning_population') != population
    current['applied_planning_population'] = population
    normalized = deepcopy(result)
    assignment_ids = {i: str(uuid5(NAMESPACE_URL, job.id + ':' + str(i))) for i in ai}
    for i, ident in assignment_ids.items():
        normalized['additions'][i]['participant']['id'] = ident
    from .planning import background_checks_available, complete_check
    defaults = frozen.get('generation_defaults') or frozen.get('department', {}).get('generation_defaults') or {}
    baseline = workspace.validation if (not workspace.base_publication_id
        and not population_changed and complete_check(workspace.validation, current, defaults)) else None
    if not population_changed and not workspace.base_publication_id and background_checks_available(db):
        from packages.domain.validation_scope import assignment_scope, evaluate_assignments
        from packages.domain.proposal_reconciliation import overlay
        candidate = overlay(current, normalized, ai, ri)
        touched = {normalized['additions'][i]['shift_id'] for i in ai} | {normalized['removals'][i]['shift_id'] for i in ri}
        scope = assignment_scope(current, [r for r in current['shifts'] if r['id'] in touched],
                                 [r for r in candidate['shifts'] if r['id'] in touched])
        # Both comparisons use the same closure, including everyone added or removed by the selected batch.
        plan = reconcile(current, frozen, normalized, ai, ri,
                         evaluator=lambda data: evaluate_assignments(data, scope))
    else:
        from . import planning
        plan = reconcile(current, frozen, normalized, ai, ri, baseline=baseline, evaluator=planning.evaluate)
    from packages.domain.score import schedule_score, score_dependencies
    from packages.domain.finding_causes import DIAGNOSTIC_VERSION
    if not plan['validation'].get('scope'):
        plan['validation'].update(score=schedule_score(plan['data'], plan['validation'], defaults),
            diagnostic_version=DIAGNOSTIC_VERSION, score_dependencies=score_dependencies(defaults))
    from packages.domain.overlap import per_session_overlaps
    flags = (per_session_overlaps(plan['data'], focus_ids=set(current['focus_ids'])),
             per_session_overlaps(current, focus_ids=set(current['focus_ids'])))
    facts = changed_facts(db, job.input_snapshot, current, actor.department_id,
                          job.input_snapshot.get('audit_watermark', 0))
    receipt = {key: plan[key] for key in ('status', 'addition_indexes', 'removal_indexes', 'conflicting_placements',
                                         'retention_optimal', 'solver_status', 'reconciliation_allowance_exhausted')}
    rule_hold = rules_changed(facts) and bool(receipt['addition_indexes'] or receipt['removal_indexes'])
    if rule_hold:
        receipt.update(status='revalidation_required', addition_indexes=[], removal_indexes=[])
    receipt.update(format=1, job_id=job.id, source_fingerprint=job.input_fingerprint,
                   current_fingerprint=current_fingerprint, prepared_fingerprint=current_fingerprint,
                   validation_current=not rule_hold, content_revision=workspace.content_revision,
                   changed_facts=facts, changed_fact_count=len(facts),
                   conflicting_placement_count=len(receipt['conflicting_placements']),
                   validation={key: plan['validation'].get(key) for key in ('valid', 'error_count', 'warning_count', 'score', 'fingerprint')})
    receipt['id'] = snapshot_hash(receipt)
    summary_cache = {}
    evaluated_at = m.now().isoformat()
    if not plan['validation'].get('scope'):
        from packages.domain.rota_summary import fix_list_context, remember_fix_list
        plan['validation'].update(evaluated_at=evaluated_at, fix_list_context=fix_list_context(plan['data']),
            input_manifest={key: plan['data']['input_manifest'][key] for key in ('format', 'token', 'components', 'dirty_hint')})
        remember_fix_list(workspace.validation, plan['validation'], accepted_issues=workspace.accepted_issues or [],
                          summary_cache=summary_cache)
    return {'receipt': receipt, 'assignment_ids': assignment_ids, 'result_hash': snapshot_hash({k: v for k, v in result.items() if k != 'reconciliation'}),
            'actor_id': actor.identity_id, 'department_id': actor.department_id, 'workspace_id': workspace.id,
            'validation': plan['validation'], 'data': plan['data'], 'population_contribution': contribution,
            'currency_hint': currency_hint(db, workspace, mandatory_rules=True), 'current_data': captured,
            'current_stamp': stamp, 'overlap_flags': flags, 'summary_cache': summary_cache}
