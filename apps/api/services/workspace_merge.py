"""Authorized, fingerprint-bound three-way merge through the ordinary planning writer."""
from datetime import date

from sqlalchemy import insert, select

from apps.api import models as m
from apps.api.errors import require
from apps.api.snapshots import domain_snapshot, workspace_content, snapshot_hash
from packages.contracts import schemas as s
from packages.domain.planning_groups import GroupResolver
from packages.domain.workspace_merge import plan, overlay
from packages.domain.proposal_reconciliation import comparison
from packages.domain.time import aware, local_midnight
from . import planning_groups as groups
from .common import expected, scoped
from .planning_preparation import command_compute, command_id, command_time
from .workspace_access import require_edit, append_access


def admit(db, actor, source, target):
    require_edit(db, actor, source)
    require_edit(db, actor, target)
    require(source.created_by == actor.identity_id, 'DRAFT_OWNER_ONLY', 'Only the draft creator can merge it', 403)
    require(source.id != target.id and source.status == target.status == 'draft'
            and source.archived_at is target.archived_at is None,
            'WORKFLOW_STATE', 'Choose two active drafts', 409)
    require(groups.access_record(db, source)['purpose'] == 'sandbox' and
            groups.access_record(db, target)['purpose'] == 'integrated',
            'MERGE_TARGET_INVALID', 'Merge a sandbox into the department rota', 409)
    require(source.starts_on == target.starts_on and source.ends_on == target.ends_on,
            'MERGE_PERIOD', 'Draft and rota must cover the same period', 409)
    fork = db.scalar(select(m.WorkspaceForkBase).where(m.WorkspaceForkBase.workspace_id == source.id))
    require(fork is not None and fork.source_workspace_id == target.id, 'FORK_BASE_REQUIRED',
            'This draft has no mergeable base for this rota', 409)
    return fork


def prepare(db, actor, source, target):
    fork = admit(db, actor, source, target)
    content = workspace_content(db, target)
    source_content = workspace_content(db, source)
    data = domain_snapshot(db, target)
    versions = groups.load_groups(db, actor.department_id, starts_on=target.starts_on, ends_on=target.ends_on)
    resolver = GroupResolver(data, versions)
    is_coordinator = groups.coordinator(db, actor)
    def allowed(pid, shift, row):
        primary = resolver.primary(pid, shift, row)
        # D19 assigns unowned/ambiguous people to the coordinator; other groups remain excluded.
        return is_coordinator if primary is None else actor.identity_id in primary['editor_identity_ids']
    def adjustment_allowed(row):
        pid = row.get('person_id')
        if not pid:
            return False
        interval = {'id': 'adjustment', '_date_projection': True,
            'start': local_midnight(date.fromisoformat(str(row.get('starts_on') or target.starts_on)), data['organisation']['timezone']).isoformat(),
            'end': local_midnight(date.fromisoformat(str(row.get('ends_on') or target.ends_on)), data['organisation']['timezone']).isoformat()}
        return allowed(pid, interval, interval)
    bundles = plan(source.id, fork.base_content, fork.fork_content, source_content, content,
                   fork.session_map, assignment_allowed=allowed, adjustment_allowed=adjustment_allowed)
    for bundle in bundles:
        adjustment = bundle.get('yours') or bundle.get('before') or {}
        person = data['people'].get(bundle.get('person_id') or adjustment.get('person_id'), {})
        subject = person.get('display_name') or 'This session'
        sentence = (adjustment.get('sentence') or f"{subject}: {adjustment.get('policy_name') or 'rota adjustment'}"
            if bundle.get('field') else f"{subject}: {len(bundle['changes'])} assignment changes" if bundle.get('person_id')
            else f"{len(bundle['changes'])} assignment change" + ('' if len(bundle['changes']) == 1 else 's') if bundle['changes'] else 'Session definition changes')
        explanations = {
            'fits': 'These draft changes fit the current rota.',
            'both_changed': 'The draft and current rota both changed this work. Choose which to retain.',
            'protected_coupling': ' '.join(sorted(set(bundle['facts'].get('protection_reasons', [])))) or 'Keep this protected work in the rota.',
            'input_changed': 'The session definition changed after this draft was copied. Review the current inputs.',
            'out_of_scope': 'This change is outside your group or changes a session definition. It stays in the draft.'}
        if bundle.get('person_id') and len(bundle['changes']) == 1:
            sentence = f"{subject}: 1 assignment change"
        bundle['facts'].update(sentence=sentence, detail=explanations[bundle['kind']],
            shift_ids=sorted({c['shift_id'] for c in bundle['changes']}))
        for changed in bundle['changed_inputs']:
            changed['what'] = 'Session definition changed'
    binding = {'source_hash': snapshot_hash(source_content), 'source_revision': source.revision,
        'target_fingerprint': data['fingerprint'], 'target_revision': target.revision,
        'group_versions': versions, 'settings': groups.settings_definition(db, actor.department_id),
        'fork_base_id': fork.id, 'bundles': bundles}
    return content, data, bundles, snapshot_hash(binding), fork


def checked(db, data, content, bundles, choices, binding, *, for_apply=False):
    candidate = overlay(content, bundles, choices)
    touched = {c['shift_id'] for b in bundles if choices.get(b['id']) in {'yours', 'left_out'} for c in b['changes']}
    for shift in candidate['shifts']:
        if shift['id'] in touched:
            shift['revision'] += 1
            if shift.get('series_id'):
                shift['series_modified'] = True
    workspace = db.get(m.Workspace, data['workspace']['id'])
    original = workspace.rule_adjustments, workspace.target_overrides
    try:
        workspace.rule_adjustments = candidate['workspace'].get('rule_adjustments', [])
        workspace.target_overrides = candidate['workspace'].get('target_overrides', [])
        after_data = domain_snapshot(db, workspace, own_shifts=candidate['shifts'])
    finally:
        workspace.rule_adjustments, workspace.target_overrides = original
        db.flush()
    from .planning import complete_check
    defaults = db.get(m.Department, workspace.department_id).generation_defaults
    baseline = workspace.validation if complete_check(workspace.validation, data, defaults) else None
    from packages.domain.input_manifest import manifest
    after_data['workspace'] = {**after_data['workspace'], 'content_revision': workspace.content_revision + 1}
    after_data['input_manifest'] = manifest(after_data)
    after_data['fingerprint'] = after_data['input_manifest']['token']
    def compute():
        from packages.domain.validation import evaluate
        from packages.domain.rota_summary import rota_summary, fix_list_context
        value = after_data
        before = baseline if baseline is not None else evaluate(data, session_parts=True)
        if baseline is None:
            before['input_manifest'] = data['input_manifest']
        # A merge that changes only participants can reuse the target's complete receipt.
        # Session definitions, dates, adjustments and rule/input changes keep the full fallback.
        from .planning import assignment_validation_scope
        partial = assignment_validation_scope(value, {'workspace': data['workspace'], 'shifts': content['shifts']}, before)
        after = evaluate({**value, **({'_partial': partial} if partial is not None else {})}, session_parts=for_apply)
        # Preview consumes findings only; the saved canonical check also owns its full score.
        if for_apply:
            from packages.domain.score import schedule_score, score_dependencies
            from packages.domain.finding_causes import DIAGNOSTIC_VERSION
            after.update(score=schedule_score(value, after, defaults), diagnostic_version=DIAGNOSTIC_VERSION,
                         score_dependencies=score_dependencies(defaults))
        after['fix_list_context'] = fix_list_context(value)
        compared = comparison(before, after)
        from .issues import issue_key
        old_surplus = {issue_key(f): f['detail']['scheduled'] - f['detail']['places']
                       for f in before['findings'] if f['code'] == 'SURPLUS_COVER'}
        surplus = [f for f in after['findings'] if f['code'] == 'SURPLUS_COVER' and
            f['detail']['scheduled'] - f['detail']['places'] > old_surplus.get(issue_key(f), 0)]
        if surplus:
            compared = {**compared, 'no_new_problems': False,
                        'new_or_worse_findings': [*compared['new_or_worse_findings'], *surplus]}
        return {'validation': after, 'comparison': compared,
                'fix_list': rota_summary(after, candidate['shifts'], candidate['workspace'].get('accepted_issues') or [])['fix_list']}
    result = command_compute(db, 'workspace_merge', data, compute, options={'binding': binding, 'choices': choices,
        'after': after_data['fingerprint'], 'for_apply': for_apply})
    return {**result, 'data': after_data}


def preview(db, actor, command):
    value = s.WorkspaceMergePreviewInput.model_validate(command.payload)
    if value.background:
        return enqueue(db, actor, command, value)
    target = scoped(db, m.Workspace, command.target_id, actor)
    expected(target, command.expected_revision)
    source = scoped(db, m.Workspace, value.source_workspace_id, actor)
    content, data, bundles, binding, fork = prepare(db, actor, source, target)
    selected = {b['id']: 'yours' for b in bundles if b['kind'] == 'fits'}
    check = checked(db, data, content, bundles, selected, binding)
    payload = {'source_workspace_id': source.id, 'target_workspace_id': target.id,
        'source_revision': source.revision, 'source_content_revision': source.content_revision,
        'target_revision': target.revision, 'target_content_revision': target.content_revision,
        'fork_base_id': fork.id, 'copied_at': fork.created_at.isoformat(), 'binding': binding, 'bundles': bundles,
        'selected_bundle_ids': sorted(selected), 'after': check['fix_list']['summary'],
        'new_or_worse_findings': check['comparison']['new_or_worse_findings'],
        'can_apply': check['comparison']['no_new_problems']}
    proposal = m.WorkspaceMergeProposal(id=command_id(db), created_at=command_time(db),
        department_id=actor.department_id, source_workspace_id=source.id, target_workspace_id=target.id,
        source_revision=source.revision, source_content_revision=source.content_revision,
        target_revision=target.revision, target_content_revision=target.content_revision,
        payload=payload, actor_id=actor.identity_id, reason=value.reason)
    db.add(proposal)
    db.flush()
    return {'merge': {'id': proposal.id, **payload}}


def apply(db, actor, command):
    from .planning import changed, chunks, remove_participants, selected_shifts, assignment_source
    value = s.WorkspaceMergeApplyInput.model_validate(command.payload)
    if value.background:
        return enqueue(db, actor, command, value)
    target = scoped(db, m.Workspace, command.target_id, actor)
    expected(target, command.expected_revision)
    proposal = scoped(db, m.WorkspaceMergeProposal, value.merge_id, actor)
    require(proposal.actor_id == actor.identity_id, 'NOT_FOUND', 'Record not found', 404)
    require(proposal.status == 'preview' and proposal.target_workspace_id == target.id,
            'MERGE_STATE', 'Choose an unused preview for this rota', 409)
    source = scoped(db, m.Workspace, proposal.source_workspace_id, actor)
    before, data, bundles, binding, _ = prepare(db, actor, source, target)
    require(binding == proposal.payload['binding'], 'MERGE_STALE',
            'The draft, rota or scheduling inputs changed; preview the merge again', 409)
    by_id = {b['id']: b for b in bundles}
    selected = set(proposal.payload['selected_bundle_ids'] if value.bundle_ids is None else value.bundle_ids)
    require(selected <= by_id.keys() and value.choices.keys() <= by_id.keys() and all(
                key in selected or choice in {'rota', 'not_merged'} and choice in by_id[key]['allowed_choices']
                for key, choice in value.choices.items()),
            'MERGE_BUNDLE', 'Choose bundles from this preview', 422)
    choices = {}
    for key in sorted(selected):
        choice = value.choices.get(key, 'yours' if by_id[key]['kind'] == 'fits' else None)
        require(choice in by_id[key]['allowed_choices'], 'MERGE_CHOICE', 'Resolve every selected conflict', 422)
        choices[key] = choice
    active = [b for b in bundles if choices.get(b['id']) in {'yours', 'left_out'}]
    require(active, 'NO_MERGE_CHANGES', 'Choose at least one authorized draft change', 422)
    check = checked(db, data, before, bundles, choices, binding, for_apply=True)
    require(check['comparison']['no_new_problems'], 'MERGE_CONFLICT',
            'These changes introduce or worsen mandatory findings or exceed cover places; resolve them before merging', 409,
            check['comparison']['new_or_worse_findings'])
    proposed = overlay(before, bundles, choices)
    # Imported adjustments retain their author/reason, and are re-admitted against current rule definitions.
    from .policies import effective_sources
    from .planning import rule_adjustment_rows
    policy_ids = {b['yours']['policy_id'] for b in active if b.get('field') and b.get('yours')}
    policies = {p.id: p for p in db.scalars(select(m.Policy).where(m.Policy.id.in_(policy_ids)))} if policy_ids else {}
    effective = effective_sources(db, actor, list(policies.values())) if policies else {}
    for b in active:
        entry = b.get('yours')
        if b.get('field') and entry:
            policy = policies.get(entry['policy_id'])
            require(policy is not None and policy.department_id == actor.department_id,
                    'MERGE_RULE_CHANGED', 'An adjusted rule is no longer available', 409)
            policy = effective[policy.id]
            require(policy['revision'] == entry.get('policy_revision'), 'MERGE_RULE_CHANGED',
                    'An adjusted rule changed; update the draft adjustment first', 409)
            if b['field'] == 'rule_adjustments':
                args = {k: entry[k] for k in s.RuleAdjustmentInput.model_fields if k in entry}
                rule_adjustment_rows(target, policy, s.RuleAdjustmentInput.model_validate(args), entry['by'], db=db)
            else:
                rule = policy['definition']['rule']
                require(rule['kind'] == 'block_quota' and rule['comparison'] == 'target',
                        'TARGET_ONLY', 'Only configurable targets may be imported', 409)
    changes = [c for b in active for c in b['changes']]
    touched = {c['shift_id'] for c in changes}
    shifts = selected_shifts(db, target, touched)
    remove_participants(db, [(c['shift_id'], c['person_id']) for c in changes])
    planned_assignments = {(row['id'], p['person_id']): p for row in check['data']['shifts'] for p in row['participants']}
    rows, authors = [], {actor.identity_id}
    for b in active:
        if b.get('yours') and b.get('field'):
            authors.add(b['yours'].get('by'))
        for c in b['changes']:
            if c.get('source', {}).get('source_actor_id'):
                authors.add(c['source']['source_actor_id'])
            if c['yours'] is not None:
                p = s.ParticipantInput.model_validate(c['yours']).model_dump(mode='json')
                p['start'], p['end'] = aware(p['start']), aware(p['end'])
                planned = planned_assignments[c['shift_id'], c['person_id']]
                rows.append({'id': planned['id'], 'created_at': command_time(db), 'revision': planned['revision'],
                    'shift_id': c['shift_id'], **p, **assignment_source(actor, 'manual')})
    for batch in chunks(rows):
        db.execute(insert(m.Assignment), batch)
    for row in shifts.values():
        row.revision += 1
        if row.series_id:
            row.series_modified = True
    target.rule_adjustments = proposed['workspace'].get('rule_adjustments', [])
    target.target_overrides = proposed['workspace'].get('target_overrides', [])
    db.flush()
    result = changed(db, actor, target, before, value.reason, prepared=(check['validation'], check['data']))
    if groups.access_record(db, source)['visibility'] == 'private':
        append_access(db, actor, source, 'shared', 'sandbox', value.reason)
    source.archived_at = command_time(db)
    source.revision += 1
    proposal.status, proposal.revision = 'applied', proposal.revision + 1
    proposal.payload = {**proposal.payload, 'applied_bundle_ids': [b['id'] for b in active],
                        'contributor_identity_ids': sorted(authors - {None}), 'final_fix_list': check['fix_list']}
    result.update(merge_id=proposal.id, source_workspace_id=source.id, shared=True,
                  applied_bundle_ids=[b['id'] for b in active])
    return result


def enqueue(db, actor, command, value):
    from apps.api.serialization import job_record
    target = scoped(db, m.Workspace, command.target_id, actor)
    expected(target, command.expected_revision)
    if command.kind == 'workspace.merge.preview':
        source = scoped(db, m.Workspace, value.source_workspace_id, actor)
    else:
        proposal = scoped(db, m.WorkspaceMergeProposal, value.merge_id, actor)
        require(proposal.actor_id == actor.identity_id, 'NOT_FOUND', 'Record not found', 404)
        require(proposal.status == 'preview' and proposal.target_workspace_id == target.id,
                'MERGE_STATE', 'Choose an unused preview for this rota', 409)
        source = scoped(db, m.Workspace, proposal.source_workspace_id, actor)
    admit(db, actor, source, target)
    inputs = {'workspace': {'id': target.id}, 'source_id': source.id, 'source_revision': source.revision,
              'command': command.model_copy(update={'payload': {**command.payload, 'background': False}}).model_dump(mode='json')}
    job = m.Job(department_id=actor.department_id, workspace_id=target.id, requested_by=actor.identity_id,
                kind='merge_preview' if command.kind == 'workspace.merge.preview' else 'merge_apply',
                input_fingerprint=snapshot_hash(inputs), input_snapshot=inputs)
    db.add(job)
    db.flush()
    return {'job': job_record(job)}
