import copy
from uuid import NAMESPACE_URL, uuid5
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import and_, delete, exists, func, insert, select, tuple_, update
from sqlalchemy.orm import defer
from packages.contracts import schemas as s
from packages.domain.recurrence import expand, inactive, series_state, stored_series_definition, with_series_state
from packages.domain.generation import MANIFEST as GENERATION_LIMITS, assignment_fixed
from packages.domain.time import aware, local_midnight, local_stamp, configured_duration_matches, overlaps
from packages.domain.participation import shifted_segments
from packages.domain.validation import (INTERNAL_CHECK_KEYS, evaluate, ancestor_ids, attached_to, qualified_supervisor, trainee_intervals,
                                        rule_coverage_people, supervision_commitments, policy_applies)
from packages.domain.resources import date_set_closes, resource_intervals
from packages.domain.date_sets import DateSetRangeError, prepare_date_sets
from packages.domain.rule_map import NUMBERS, adjustable, rule_number, weakens_safety
from packages.domain.rule_serialization import stored_definition
from packages.domain.assignment_override import assignment_reason_allowed, ZERO_LIMIT_FINDING_CODES

from apps.api import models as m, snapshots
from apps.api.auth import require_role, person_access, current_roles, require_catalogue_read
from apps.api.errors import Problem, require
from apps.api.serialization import record, job_progress_record
from apps.api.serialization import job_reconciliation_columns, job_result_with_reconciliation, retain_job_reconciliation
from apps.api.snapshots import (domain_snapshot, fingerprint, publication_snapshot,
                                shift_record, workspace_content, workspace_shifts)
from .common import scoped, expected, bump_inputs, outbox
from . import reviews, swaps, schedule
from .planning_preparation import command_compute, command_evaluate, command_id, command_time
from .planning_groups import require_change_authority
from .wishes import SETTLED, require_no_override_overlap

MANUAL_PLACEMENT_BLOCKERS = frozenset({'UNAVAILABLE', 'EXTERNAL_ROTATION', 'RESTRICTION'})
MAX_SHIFTS = GENERATION_LIMITS['max_sessions']


def draft(db, actor, workspace_id, revision):
    require_role(actor, "coordinator")
    workspace = scoped(db, m.Workspace, workspace_id, actor)
    from .workspace_access import require_edit
    require_edit(db, actor, workspace)
    expected(workspace, revision)
    require(workspace.status == "draft", "WORKFLOW_STATE", "Only a draft can be edited; reopen review or create an amendment", 409)
    require(workspace.archived_at is None, "WORKFLOW_STATE", "This draft was discarded; restore it before editing", 409)
    return workspace


def _archive_state(workspace, access):
    return {
        'status': workspace.status,
        'content_revision': workspace.content_revision,
        'submitted_fingerprint': workspace.submitted_fingerprint,
        'submitted_at': workspace.submitted_at.isoformat() if workspace.submitted_at else None,
        'review_snapshot': workspace.review_snapshot,
        'validation': workspace.validation,
        'return_note': workspace.return_note,
        'visibility': access['visibility'],
        'purpose': access['purpose'],
    }


def _running_workspace_job(db, workspace_id):
    return db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace_id,
        m.Job.status.in_({'queued', 'running'})).limit(1))


def delete_draft_refused_clause(workspace_id):
    return and_(
        ~exists(select(1).where(m.WorkspaceBaseline.workspace_id == workspace_id)),
        exists(select(1).where(m.PublicationChangeList.workspace_id == workspace_id)))


def _delete_draft_refused(db, workspace):
    return bool(db.scalar(select(delete_draft_refused_clause(workspace.id))))


def allowed_actions(db, actor, workspace, *, historical=False, published=False, delete_refused=None):
    """Server-owned lifecycle affordances for a rota row/detail."""
    if not actor.permits('coordinator', 'admin'):
        return []
    from .workspace_access import access_record
    access = access_record(db, workspace)
    if access['visibility'] == 'private' and workspace.created_by != actor.identity_id:
        return []
    if access['purpose'] == 'sandbox' and workspace.created_by != actor.identity_id:
        return []
    if access['purpose'] == 'integrated' and not actor.permits('admin'):
        from .planning_groups import coordinator
        if not coordinator(db, actor):
            return []
    if workspace.archived_at is not None:
        actions = ['workspace.restore']
        if workspace.status == 'draft' and not historical and not published and (workspace.archive_state or {}).get('status') == 'draft':
            refused = _delete_draft_refused(db, workspace) if delete_refused is None else delete_refused
            if not refused:
                actions.append('workspace.delete_draft')
        return actions
    actions = ['workspace.archive']
    if workspace.status == 'draft' and not historical and not published:
        refused = _delete_draft_refused(db, workspace) if delete_refused is None else delete_refused
        if not refused:
            actions.append('workspace.delete_draft')
    return actions


def _restore_conflict(db, workspace, state):
    if state.get('purpose') != 'integrated':
        return
    from .planning_groups import default_rota_id
    default = default_rota_id(db, workspace)
    require(default in (None, workspace.id), 'ROTA_RESTORE_CONFLICT',
            'Another active integrated rota occupies this period or scope; resolve it before restoring', 409)
    access = m.WorkspaceAccessVersion
    conflict = db.scalar(select(m.Workspace.id).join(access, access.id == m.Workspace.access_version_id).where(
        m.Workspace.department_id == workspace.department_id, m.Workspace.id != workspace.id,
        m.Workspace.archived_at.is_(None), access.purpose == 'integrated',
        m.Workspace.starts_on < workspace.ends_on, m.Workspace.ends_on > workspace.starts_on).limit(1))
    require(conflict is None, 'ROTA_RESTORE_CONFLICT',
            'Another active integrated rota occupies this period or scope; resolve it before restoring', 409)


def _delete_draft_data(db, workspace):
    """Remove only the never-submitted draft graph; immutable audit/evidence stays."""
    workspace.access_version_id = None
    workspace.access_version = None
    db.flush()
    shift_ids = select(m.Shift.id).where(m.Shift.workspace_id == workspace.id)
    db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id.in_(shift_ids)))
    db.execute(delete(m.Assignment).where(m.Assignment.shift_id.in_(shift_ids)))
    db.execute(delete(m.Shift).where(m.Shift.workspace_id == workspace.id))
    for cls in (m.Job, m.ChangeSet, m.WorkspaceReadProjection,
                m.WorkspaceAccessVersion, m.WorkspaceMergeProposal, m.WorkspaceForkBase):
        if cls is m.WorkspaceMergeProposal:
            db.execute(delete(cls).where((cls.source_workspace_id == workspace.id) | (cls.target_workspace_id == workspace.id)))
        elif cls is m.WorkspaceForkBase:
            db.execute(delete(cls).where((cls.workspace_id == workspace.id) | (cls.source_workspace_id == workspace.id)))
        else:
            db.execute(delete(cls).where(cls.workspace_id == workspace.id))
    # These rows are staff or swap records that merely point at a draft; keep
    # them and remove only the optional workspace link.
    db.execute(update(m.RequestRecord).where(m.RequestRecord.workspace_id == workspace.id).values(workspace_id=None))
    db.execute(update(m.WishOverride).where(m.WishOverride.workspace_id == workspace.id).values(workspace_id=None))
    db.execute(update(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id).values(workspace_id=None))
    db.execute(delete(m.PublicationChangeList).where(m.PublicationChangeList.workspace_id == workspace.id))
    db.execute(m.Workspace.__table__.delete().where(m.Workspace.id == workspace.id))


def archive(db, actor, command):
    """Archive/restore every rota state, or permanently delete an untouched draft."""
    s.Decision.model_validate(command.payload)
    workspace = scoped(db, m.Workspace, command.target_id, actor)
    from .workspace_access import require_edit
    from .planning_groups import admit_private
    access = require_edit(db, actor, workspace)
    if not actor.permits('admin'):
        from .planning_groups import unrestricted
        require(unrestricted(db, actor, access), 'GROUP_EDIT_AUTHORITY',
                'Only the Rota Coordinator can change the integrated rota lifecycle', 403)
    expected(workspace, command.expected_revision)

    if command.kind == 'workspace.delete_draft':
        require_role(actor, 'coordinator', 'admin')
        require(workspace.status == 'draft', 'WORKFLOW_STATE', 'Only a draft can be permanently deleted', 409)
        require(_running_workspace_job(db, workspace.id) is None, 'ROTA_JOB_RUNNING',
                'A check or auto-schedule is running on this rota. Try again when it finishes.', 409)
        historical = db.scalar(select(m.AuditEvent.id).where(m.AuditEvent.department_id == actor.department_id,
            m.AuditEvent.target_id == workspace.id,
            m.AuditEvent.action.in_({'workspace.submit', 'workspace.publish'})).limit(1))
        published = db.scalar(select(m.Publication.id).where(m.Publication.workspace_id == workspace.id).limit(1))
        archived_state = (workspace.archive_state or {}).get('status')
        review_evidence = db.scalar(select(m.ReviewDecision.id).where(m.ReviewDecision.workspace_id == workspace.id).limit(1))
        review_evidence = review_evidence or db.scalar(select(m.Approval.id).where(m.Approval.workspace_id == workspace.id).limit(1))
        require(not historical and published is None and not review_evidence
                and archived_state not in {'submitted', 'approved', 'published', 'superseded'},
                'DRAFT_DELETE_FORBIDDEN', 'This rota was submitted or published before; archive it instead', 409)
        # Draft baselines are immutable evidence. Keep a baseline-backed draft recoverable
        # instead of attempting the destructive path, which the baseline FK/guard rejects.
        has_baseline = db.scalar(select(m.WorkspaceBaseline.id).where(
            m.WorkspaceBaseline.workspace_id == workspace.id).limit(1)) is not None
        if has_baseline:
            require(workspace.archived_at is None, 'WORKFLOW_STATE',
                    'This draft is already archived; restore it before editing', 409)
            command = command.model_copy(update={'kind': 'workspace.archive'})
        else:
            require(not _delete_draft_refused(db, workspace), 'DRAFT_DELETE_FORBIDDEN',
                    'This draft holds published changes; archive it instead.', 409)
            referenced = db.scalar(select(m.WorkspaceForkBase.id).where(
                m.WorkspaceForkBase.source_workspace_id == workspace.id,
                m.WorkspaceForkBase.workspace_id != workspace.id).limit(1))
            require(referenced is None, 'WORKSPACE_REFERENCED',
                    'Another draft is based on this rota; archive it instead', 409)
            workspace_id = workspace.id
            _delete_draft_data(db, workspace)
            return {'workspace': {'id': workspace_id, 'deleted': True, 'allowed_actions': []}, 'deleted': True}

    restoring = command.kind == 'workspace.restore'
    if restoring:
        require(workspace.archived_at is not None, 'WORKFLOW_STATE', 'This rota is already active', 409)
        state = workspace.archive_state or {'status': 'draft', 'content_revision': workspace.content_revision,
                                            'purpose': access['purpose']}
        _restore_conflict(db, workspace, state)
        if state.get('visibility') == 'private':
            admit_private(db, actor, restoring=workspace.id)
        require_no_override_overlap(db, workspace, workspace.starts_on, workspace.ends_on)
        workspace.status = state.get('status', 'draft')
        workspace.content_revision = state.get('content_revision', workspace.content_revision)
        workspace.submitted_fingerprint = state.get('submitted_fingerprint')
        workspace.submitted_at = datetime.fromisoformat(state['submitted_at']) if state.get('submitted_at') else None
        workspace.review_snapshot = state.get('review_snapshot')
        workspace.validation = state.get('validation')
        workspace.return_note = state.get('return_note')
        if workspace.base_publication_id and workspace.status in {'submitted', 'approved'}:
            from .published_changes import on_reviewed
            on_reviewed(db, actor, workspace, command.payload['reason'])
        workspace.archived_at = workspace.archived_by = workspace.archive_reason = workspace.archive_state = None
        workspace.revision += 1
        db.flush()
        if workspace.status == 'draft':
            from apps.api.workspace_baselines import capture_draft_baseline, latest as latest_baseline
            if latest_baseline(db, workspace.id) is None:
                capture_draft_baseline(db, workspace, actor.identity_id,
                                       reason="Captured draft baseline when restoring draft")
        return {'workspace': {**record(workspace, exclude=('validation',)),
                              'allowed_actions': allowed_actions(db, actor, workspace)}}

    require(workspace.archived_at is None, 'WORKFLOW_STATE', 'This rota is already archived', 409)
    require(_running_workspace_job(db, workspace.id) is None, 'ROTA_JOB_RUNNING',
            'A check or auto-schedule is running on this rota. Try again when it finishes.', 409)
    state = _archive_state(workspace, access)
    if workspace.status in {'submitted', 'approved'}:
        # Review withdrawal and archive are one transaction; review evidence is retained.
        from . import reviews
        reviews.withdraw(db, actor, workspace, command.payload['reason'])
    workspace.archive_state = state
    workspace.archived_at, workspace.archived_by = m.now(), actor.identity_id
    workspace.archive_reason = command.payload['reason']
    from .generation_resume import clear_generation_resume
    clear_generation_resume(db, workspace_id=workspace.id)
    workspace.revision += 1
    db.flush()
    return {'workspace': {**record(workspace, exclude=('validation',)),
                          'allowed_actions': allowed_actions(db, actor, workspace)}}


def revalidate(db, workspace, own=None, *, data_out=None, external_snapshot=None, shared_inputs=None, store=True, include_internal=False,
               _overlap_witness=None, _incremental=None, reuse=False, acquired_data=None, _overlap_before=None,
               _overlap_output=None, final_compute=False, creation_before=None, mandatory_rules=True,
               current_mandatory=False):
    """Check the rota, or its published changes (read-only when store=False). `own` receives all owned sessions."""
    db.flush()
    # Keep the same department context alive through snapshot and scoring; the ORM identity map is weak.
    department = db.get(m.Department, workspace.department_id)
    data = acquired_data if acquired_data is not None else domain_snapshot(
        db, workspace, external_snapshot=external_snapshot, shared_inputs=shared_inputs, mandatory_rules=mandatory_rules,
        current_mandatory=current_mandatory)
    from .rota_summary import current_check
    if reuse and (current_check(db, workspace, data['fingerprint']) if workspace.base_publication_id else
                  complete_check(workspace.validation, data, department.generation_defaults)):
        result = copy.deepcopy(workspace.validation)
        if own is not None:
            focus = set(data['focus_ids'])
            own.extend(row for row in data['shifts'] if row['id'] in focus)
        if data_out is not None:
            data_out.append(data)
        if store:
            workspace.validation = result
        return result if include_internal else {k: v for k, v in result.items() if k not in INTERNAL_CHECK_KEYS}
    reviewed_candidate = workspace.status in ('submitted', 'approved') and workspace.submitted_fingerprint == data['fingerprint']
    receipt = (workspace.validation or {}).get('wish_receipt') if reviewed_candidate else None
    full_data = data
    data = dict(data)
    original_focus = set(data["focus_ids"])
    from .published_changes import validation_scope as published_scope
    amendment_scope = published_scope(db, workspace, data)
    checked = {**data, '_distribution_cache': {}, '_workload_normalization_cache': {},
               '_scope_interval_cache': {}, '_policy_applies_cache': {}, '_allocation_match_cache': {},
               '_policy_applies_people': True}
    if _overlap_witness is not None:
        checked['_overlap_witness'] = _overlap_witness
    if check := db.info.get('_check_budget'):
        checked['_check_budget'] = check
    if _incremental is not None and not amendment_scope:
        before, previous = _incremental
        partial = assignment_validation_scope(data, before, previous)
        if partial is not None:
            checked['_partial'] = partial
    # Retained issued evidence is not a current whole-rota check, so amendments cannot issue a schedule score.
    from packages.domain.score import schedule_score
    defaults = department.generation_defaults
    preparation = db.info.get('_planning_preparation')
    previous_check = copy.deepcopy(preparation['validation_baselines'].get(
        workspace.id, workspace.validation)) if preparation else None
    accepted_issues = copy.deepcopy(workspace.accepted_issues or []) if preparation else []
    evaluated_at = command_time(db).isoformat()
    def compute():
        checked['_supervision_commitments'] = supervision_commitments(data)
        result = evaluate(checked, session_parts=True)
        if not amendment_scope:
            result['score'] = schedule_score(checked, result, defaults)
            if preparation is not None:
                from packages.domain.finding_causes import DIAGNOSTIC_VERSION
                from packages.domain.score import score_dependencies
                from packages.domain.rota_summary import fix_list_context, remember_fix_list
                result.update(diagnostic_version=DIAGNOSTIC_VERSION, score_dependencies=score_dependencies(defaults),
                    input_manifest={k: data['input_manifest'][k] for k in ('format', 'token', 'components', 'dirty_hint')},
                    evaluated_at=evaluated_at, fix_list_context=fix_list_context(data))
                remember_fix_list(previous_check, result, accepted_issues=accepted_issues,
                                  summary_cache=preparation['summary_cache'])
        if _overlap_before is None:
            return result
        from packages.domain.overlap import per_session_overlaps
        overlap_focus = checked.get('_partial', {}).get('shifts')
        overlap_data = {**data, 'focus_ids': overlap_focus} if overlap_focus is not None else data
        focus_ids = set(data['focus_ids'])
        previous_ids = {row['id'] for row in _overlap_before['shifts']}
        old_data = with_series_state({**data,
            'shifts': _overlap_before['shifts'] + [row for row in data['shifts'] if row['id'] not in focus_ids],
            'focus_ids': previous_ids})
        old_overlap_data = {**old_data, 'focus_ids': overlap_focus} if overlap_focus is not None else old_data
        return result, (per_session_overlaps(overlap_data, conflicts=_overlap_witness.get('conflicts') if _overlap_witness else None,
                                              focus_ids=focus_ids),
                        per_session_overlaps(old_overlap_data, focus_ids=previous_ids) if previous_ids else {})
    # One pure calculation after rollback; replay still verifies its complete canonical manifest under the writer.
    from packages.domain.input_manifest import shift_input
    # Rollback recreates metadata timestamps; only canonical scheduling fields bind the overlap baseline.
    overlap_before = ([shift_input(row) for row in _overlap_before['shifts']]
                      if _overlap_before is not None else None)
    computed = command_compute(db, 'rota_check', checked, compute,
                               options={'defaults': defaults, 'overlap_before': overlap_before}, final=final_compute,
                               input_token=data['fingerprint'] if preparation else None)
    if _overlap_before is not None:
        result, overlap_flags = computed
        if _overlap_output is not None:
            _overlap_output['flags'] = overlap_flags
    else:
        result = computed
    if creation_before is not None:
        # Admission shares the saved staffing check and runs after the workspace write in preparation.
        admit_assignment_change(db, None, workspace, creation_before, 'CREATE_INVALID', staffing_gaps_only=True,
                                evaluated=(None if amendment_scope else result, full_data))
    if amendment_scope:
        from .published_changes import retain_issued_checks
        result = retain_issued_checks(db, workspace, data, result)
        result["scope"] = "published_changes"
        result["base_publication_id"] = workspace.base_publication_id
        base_until = db.get(m.Publication, workspace.base_publication_id).effective_until
        result['base_effective_until'] = base_until.isoformat() if base_until else None
        if report := result.get("quota_report"):
            report.update(scope="published_changes", context_complete=report["complete"], complete=False,
                          basis="Changed duties and affected people, with retained issued evidence; not a complete department recheck. " + report["basis"])
    if workspace.status in SETTLED:
        result["wishes"] = data["wishes"]
    if receipt is not None:
        result['wish_receipt'] = receipt
    if not amendment_scope:
        from packages.domain.finding_causes import DIAGNOSTIC_VERSION
        from packages.domain.score import score_dependencies
        result['diagnostic_version'] = DIAGNOSTIC_VERSION
        result['score_dependencies'] = score_dependencies(department.generation_defaults)
    result['input_manifest'] = {k: data['input_manifest'][k] for k in ('format', 'token', 'components', 'dirty_hint')}
    result["evaluated_at"] = m.now().isoformat()
    from packages.domain.rota_summary import fix_list_context, remember_fix_list
    result['fix_list_context'] = fix_list_context(full_data if amendment_scope else data)
    remember_fix_list(workspace.validation, result, accepted_issues=workspace.accepted_issues or [],
                      summary_cache=(db.info.get('_planning_preparation') or db.info.get('_generation_plan') or {}).get('summary_cache'))
    if reviewed_candidate and (workspace.validation or {}).get('review_item_decisions'):
        result['review_item_decisions'] = workspace.validation['review_item_decisions']
    if store:
        workspace.validation = result
    if own is not None:
        own.extend(s for s in data["shifts"] if s["id"] in original_focus)
    if data_out is not None:
        data_out.append({**(full_data if amendment_scope else data),
                         **({'_overlap_witness': _overlap_witness} if _overlap_witness is not None else {}),
                         **({'_changed_overlap_focus': checked['_partial']['shifts']} if '_partial' in checked else {})})
    return result if include_internal else {key: value for key, value in result.items() if key not in INTERNAL_CHECK_KEYS}


def complete_check(check, data, options):
    from .rota_summary import reusable_check
    return (reusable_check(check, data['fingerprint'], options)
            and {row['shift_id'] for row in check['coverage']} == set(data['focus_ids']))


def background_checks_available(db):
    from .operations import worker_state
    return worker_state(db, command_time(db))['worker_liveness'] == 'running'


def assignment_reply_available(db, command):
    """Choose execution strategy only; the writer repeats every admission and authority check."""
    if command.expected_fingerprint:
        return False
    if command.kind in {'workspace.undo', 'workspace.redo'}:
        workspace = db.get(m.Workspace, command.target_id)
        if (workspace is None or workspace.status != 'draft' or workspace.base_publication_id
                or not background_checks_available(db)):
            return False
        change = db.scalar(latest_change(workspace, m.ChangeSet))
        if change is None or command.kind == 'workspace.undo' and change.undone or (
                command.kind == 'workspace.redo' and not change.before_snapshot.get('_undo_of')):
            return False
        current = workspace_content(db, workspace)
        target = change.before_snapshot
        return _participant_only_snapshot(target, current)
    if command.kind != 'shift.update':
        return False
    value = s.ShiftEdit.model_validate(command.payload)
    row = db.get(m.Shift, value.shift_id)
    workspace = db.get(m.Workspace, value.workspace_id)
    typ = db.get(m.AssignmentType, value.assignment_type_id)
    return bool(row and workspace and typ and row.workspace_id == workspace.id and not workspace.base_publication_id
        and typ.revision == row.type_revision and typ.id == row.assignment_type_id
        and row.location_id == value.location_id and aware(row.start) == value.start and aware(row.end) == value.end
        and row.attributes == value.attributes
        and set(db.scalars(select(m.ShiftService.service_id).where(m.ShiftService.shift_id == row.id))) == set(value.service_ids)
        and background_checks_available(db))


def _participant_only_snapshot(target, current):
    """Whether an Undo/Redo changes assignment rows only, with no session/rule drift."""
    before_workspace, after_workspace = target.get('workspace') or {}, current.get('workspace') or {}
    ignored_workspace = {'revision', 'content_revision', 'last_edited_by', 'submitted_fingerprint', 'validation'}
    if {key: value for key, value in before_workspace.items() if key not in ignored_workspace} != {
            key: value for key, value in after_workspace.items() if key not in ignored_workspace}:
        return False
    before_rows = {row['id']: row for row in target.get('shifts') or []}
    after_rows = {row['id']: row for row in current.get('shifts') or []}
    if set(before_rows) != set(after_rows):
        return False
    ignored_row = {'participants', 'revision', 'series_modified'}
    changed = False
    for shift_id, before in before_rows.items():
        after = after_rows[shift_id]
        if {key: value for key, value in before.items() if key not in ignored_row} != {
                key: value for key, value in after.items() if key not in ignored_row}:
            return False
        changed |= before.get('participants') != after.get('participants')
    return changed


def pending_assignment_check(db, workspace, before, *, admission=None):
    """Non-blocking findings belong to the worker; preserve current overlap flags in the edit reply."""
    from packages.domain.overlap import per_session_overlaps
    from packages.domain.validation_scope import assignment_scope, bounded_assignment_scope
    data = admission[1] if admission is not None else snapshots.overlap_projection_inputs(db, workspace, workspace_shifts(db, workspace.id))
    focus = set(data['focus_ids'])
    current = [row for row in data['shifts'] if row['id'] in focus]
    old = {row['id']: row for row in before['shifts']}
    changed = [row for row in current if row['participants'] != old[row['id']]['participants']]
    scope = (bounded_assignment_scope(data, [old[row['id']] for row in changed], changed)
             if admission is not None else assignment_scope(data, [old[row['id']] for row in changed], changed))
    if scope is None:
        return None
    # Names decorate conflicts only; no rule, scoring or diagnostic graph is needed.
    if admission is None:
        data['people'] = {pid: {'id': pid, 'display_name': name} for pid, name in db.execute(
            select(m.Person.id, m.Person.display_name).where(m.Person.organisation_id ==
                db.get(m.Department, workspace.department_id).organisation_id))}
        data['services'] = {sid: {'id': sid, 'name': name} for sid, name in db.execute(
            select(m.Service.id, m.Service.name).where(m.Service.department_id == workspace.department_id))}
        from .read_projections import input_stamp
        data['fingerprint'] = input_stamp(db, workspace)
    data['workspace'] = record(workspace, exclude=('validation',))
    old_data = with_series_state({**data, 'shifts': before['shifts'] +
        [row for row in data['shifts'] if row['id'] not in focus]})
    flags = per_session_overlaps({**data, 'focus_ids': scope['shifts']}, focus_ids=focus)
    old_flags = per_session_overlaps({**old_data, 'focus_ids': scope['shifts']}, focus_ids=focus)
    data['_command_overlap_flags'] = flags, old_flags
    return (admission[0] if admission is not None else
            {'scope': 'command_changes', '_command_scope': scope, 'findings': [], 'coverage': []}), data


def assignment_check(db, workspace, before, *, shared_inputs=None):
    """Only assignment edits may defer the complete check; structural/configuration changes stay synchronous."""
    if (workspace.base_publication_id or db.info.get('bulk') is not None or db.info.get('_planner_preview')
            or not background_checks_available(db)):
        return None
    data = domain_snapshot(db, workspace, shared_inputs=shared_inputs)
    if assignment_check_scope(data, before) is None:
        return None
    from packages.domain.validation_scope import bounded_assignment_scope, evaluate_assignments
    focus = set(data['focus_ids'])
    current = [row for row in data['shifts'] if row['id'] in focus]
    old = {row['id']: row for row in before['shifts']}
    changed = [row for row in current if row['participants'] != old[row['id']]['participants']]
    scope = bounded_assignment_scope(data, [old[row['id']] for row in changed], changed)
    if scope is None:
        return None
    def compute():
        from packages.domain.overlap import per_session_overlaps
        old_data = with_series_state({**data, 'shifts': before['shifts'] +
            [row for row in data['shifts'] if row['id'] not in focus]})
        return (evaluate_assignments(data, scope),
                per_session_overlaps({**data, 'focus_ids': scope['shifts']}, focus_ids=focus),
                per_session_overlaps({**old_data, 'focus_ids': scope['shifts']}, focus_ids=focus))
    result, flags, old_flags = command_compute(db, 'assignment_validation', data, compute,
        options={k: sorted(v) if isinstance(v, set) else v for k, v in scope.items()})
    data = {**data, '_command_overlap_flags': (flags, old_flags)}
    return result, data


def defer_complete_check(db, actor, workspace, previous, result, data):
    """Keep historical whole-rota evidence visibly stale until the ordinary worker replaces it."""
    from .jobs import queue_validation, queue_current_validation
    from packages.domain.rota_summary import fix_list_context, refresh_fix_list_context, remember_fix_list, fix_list_delta, rota_summary
    scope = result.pop('_command_scope')
    checked_scope = 'engine_version' in result
    preparation = db.info.get('_planning_preparation')
    if preparation is None or preparation['replay']:
        if db.info.get('_assignment_reply'):
            queue_current_validation(db, actor, workspace)
        else:
            queue_validation(db, actor, workspace, {k: v for k, v in data.items() if k != '_command_overlap_flags'})
    from .issues import issue_key
    fresh_keys = {issue_key(f) for f in result['findings']}
    findings = [f for f in previous.get('findings', []) if not checked_scope or (
        f.get('shift_id') not in scope['shifts'] and f.get('person_id') not in scope['people'] and issue_key(f) not in fresh_keys)]
    pending_reply = db.info.get('_assignment_reply')
    refreshed = {row['shift_id'] for row in result['coverage']}
    coverage = [row for row in previous.get('coverage', [])
                if row['shift_id'] not in (refreshed if pending_reply else scope['shifts'])]
    # The complete result belongs to its old fingerprint; never represent a partial check as fresh evidence.
    context = (refresh_fix_list_context(previous.get('fix_list_context', {}), data, scope)
               if pending_reply else fix_list_context(data))
    result = {**previous, 'scope': 'command_changes', 'fingerprint': previous.get('fingerprint', ''),
              'fix_list_context': context,
              'findings': findings + result['findings'], 'coverage': coverage + result['coverage']}
    result.pop('score', None)
    result.pop('checking', None)  # Job state owns liveness; failures/cancellation must return to Needs a check.
    for key in INTERNAL_CHECK_KEYS:
        result.pop(key, None)
    result['error_count'] = sum(f['severity'] == 'error' for f in result['findings'])
    result['warning_count'] = sum(f['severity'] == 'warning' for f in result['findings'])
    result['valid'] = not result['error_count']
    if not db.info.get('_assignment_reply'):
        result['evaluated_at'] = m.now().isoformat()
        remember_fix_list(previous, result, accepted_issues=workspace.accepted_issues or [],
                              summary_cache=(db.info.get('_planning_preparation') or db.info.get('_generation_plan') or {}).get('summary_cache'))
    else:
        accepted_issues = workspace.accepted_issues or []
        before_fix = (rota_summary(previous, accepted_issues=accepted_issues)['fix_list']
                      if previous.get('evaluated_at') else None)
        after_fix = rota_summary(result, accepted_issues=accepted_issues)['fix_list']
        after_fix['summary']['checking'] = True  # Partial findings update now; the queued worker owns complete evidence.
        result['fix_list_delta'] = fix_list_delta(before_fix, after_fix)
        if before_fix is None:
            # Without a baseline, do not invent a claim about what this edit introduced or cleared.
            result['fix_list_delta'].update(cleared=[], new=[], changed=[])
    workspace.validation = result
    return {k: v for k, v in result.items() if k not in INTERNAL_CHECK_KEYS}


def assignment_check_scope(data, before):
    """Assignment-only edits invalidate their sessions and simultaneous pooled-cover/supervision peers."""
    focus = set(data['focus_ids'])
    now = {row['id']: row for row in data['shifts'] if row['id'] in focus}
    old = with_series_state({**data, 'shifts': before['shifts'], 'focus_ids': list(now)})['shifts']
    if {row['id'] for row in old} != set(now):
        return None
    changed = []
    for row in old:
        current = now[row['id']]
        ignored = {'participants', 'revision', 'series_modified'}
        if ({key: value for key, value in row.items() if key not in ignored}
                != {key: value for key, value in current.items() if key not in ignored}):
            return None
        if row['participants'] != current['participants']:
            changed.append(current)
    spans = [(aware(row['start']), aware(row['end'])) for row in changed]
    return {sid for sid, row in now.items()
            if any(aware(row['start']) < b and a < aware(row['end']) for a, b in spans)}


def assignment_validation_scope(data, before, previous):
    """Share complete receipts only across participant edits with identical canonical inputs."""
    if '_session_parts' not in previous:
        return None
    before_workspace = before.get('workspace')
    if before_workspace is None:
        return None
    for key in ('starts_on', 'ends_on', 'rule_adjustments', 'target_overrides'):
        if before_workspace.get(key, []) != data['workspace'].get(key, []):
            return None
    old_components = (previous.get('input_manifest') or {}).get('components') or {}
    if not all(old_components.get(k) == data['input_manifest']['components'][k]
               for k in ('mandatory_inputs', 'objective_inputs', 'toolchain')):
        return None
    if assignment_check_scope(data, before) is None:
        return None
    from packages.domain.validation_scope import bounded_assignment_scope
    old = {row['id']: row for row in before['shifts']}
    changed = [row for row in data['shifts'] if row['id'] in old
               and row['participants'] != old[row['id']]['participants']]
    scope = bounded_assignment_scope(data, [old[row['id']] for row in changed], changed)
    if scope is None:
        return None
    if '_person_parts' not in previous:
        scope['people'] = set(data['people'])
    return {**scope, 'complete': True, 'previous': previous}


def changed(db, actor, workspace, before, reason, *, content_changed=True, undoable=True,
            external_snapshot=None, shared_inputs=None, prepared=None, defer_assignments=False, review_rejection=False,
            staffed_creation=False):
    """Every draft edit: one ChangeSet (one Undo) and a synchronous check. The reply says what changed, the same for a forward
    edit, an undo and a Resolve issues batch: the rota and its health summary, the sessions changed or removed, and the
    issues that came and went, so the open page patches it in place instead of reading the rota again."""
    from .planning_groups import guard_changes
    guard_changes(db, actor, workspace, before, review_rejection=review_rejection)
    from .issues import issue_key, issue_changes
    previous_validation = workspace.validation or {}
    previous = workspace.validation or {}
    if preparation := db.info.get('_planning_preparation'):
        # Check metadata is derived, not an edit. Keep the incremental baseline stable
        # across preparation and replay; canonical inputs and revision still gate the write.
        previous = preparation['validation_baselines'].setdefault(workspace.id, previous)
    incremental = (before, previous) if '_session_parts' in previous else None
    issues_before = {issue_key(f) for f in (workspace.validation or {}).get('findings', [])}
    if any(before['workspace'][key] != getattr(workspace, key).isoformat() for key in ('starts_on', 'ends_on')):
        from .workspace_access import access_record
        if access_record(db, workspace)['purpose'] != 'sandbox':
            require_no_override_overlap(db, workspace, workspace.starts_on, workspace.ends_on)
    workspace.revision += 1
    if content_changed:
        workspace.content_revision += 1
        workspace.submitted_fingerprint = None
    if not review_rejection:
        workspace.last_edited_by = actor.identity_id
    else:
        before = {**before, '_review_action': 'return'}
    bulk = db.info.get("bulk")
    changes = bulk.setdefault("workspace_changes", {}) if bulk is not None else {}
    change = changes.get(workspace.id) if undoable else None
    # A failed preview savepoint expunges a newly created change set; never reuse that entry.
    if change is not None and change in db and not change.undone:
        change.resulting_content_revision = workspace.content_revision
    else:
        change = m.ChangeSet(workspace_id=workspace.id, before_snapshot=before,
                             resulting_content_revision=workspace.content_revision,
                             actor_id=actor.identity_id, reason=reason, undone=not undoable)
        db.add(change)
        if bulk is not None and undoable:
            changes[workspace.id] = change
    count = db.scalar(select(func.count()).select_from(m.Shift).where(m.Shift.workspace_id == workspace.id))
    require(count <= MAX_SHIFTS, "WORKSPACE_LIMIT", f"A workspace is bounded to {MAX_SHIFTS} sessions")
    now, captured = [], []  # reuse the check's batched inputs for flags and both changed peers
    overlap_witness = {}
    delta_scope = None
    pending_reply = bool(db.info.get('_assignment_reply') and defer_assignments)
    assignment_prepared = defer_assignments and (prepared is None or pending_reply)
    if pending_reply:
        prepared = pending_assignment_check(db, workspace, before, admission=prepared)
    elif assignment_prepared:
        prepared = assignment_check(db, workspace, before, shared_inputs=shared_inputs)
    if prepared is None:
        overlap_result = {}
        result = revalidate(db, workspace, now, data_out=captured, external_snapshot=external_snapshot,
                            shared_inputs=shared_inputs, _overlap_witness=overlap_witness, _incremental=incremental,
                            # Preview replies use content and findings, without per-session flags.
                            _overlap_before=None if db.info.get('_planner_preview') else before,
                            _overlap_output=overlap_result,
                            creation_before=before if staffed_creation else None,
                            final_compute=(db.info.get('_planning_preparation') or {}).get('kind')
                                in {'shift.action', 'shift.copy', 'series.expand', 'shift.update', 'workspace.undo', 'workspace.redo'})
        if overlap_result:
            db.info['_command_overlap_flags'] = overlap_result['flags']
    else:
        result, data = prepared
        proposed = data
        if not assignment_prepared:
            data = with_series_state({**data, 'shifts': workspace_shifts(db, workspace.id) +
                    [s for s in data['shifts'] if s['id'] not in set(data['focus_ids'])]})
        from packages.domain.input_manifest import FIELDS, manifest, project, shift_input
        if not pending_reply:
            if db.info.get('_generation_plan'):
                # Every other input is the stamped detached graph; compare the saved semantic content once.
                fields = (*FIELDS['workspace'], 'content_revision')
                matched = (project(record(workspace, exclude=('validation',)), fields) == project(proposed['workspace'], fields)
                    and {row['id']: shift_input(row) for row in data['shifts']} ==
                        {row['id']: shift_input(row) for row in proposed['shifts']})
                require(matched, 'RECONCILIATION_MISMATCH',
                        'The saved batch differs from the reviewed plan; the proposal is retained', 409)
                current_manifest = proposed['input_manifest']
            else:
                current_manifest = manifest(data)
            require(current_manifest['token'] == data['fingerprint'], 'RECONCILIATION_MISMATCH',
                    'The saved batch differs from the reviewed plan; the proposal is retained', 409)
            result = {**result, 'fingerprint': current_manifest['token'], 'evaluated_at': m.now().isoformat(), 'input_manifest': {k: current_manifest[k] for k in ('format', 'token', 'components', 'dirty_hint')}}
            from packages.domain.rota_summary import fix_list_context, remember_fix_list
            result['fix_list_context'] = fix_list_context(data)
            remember_fix_list(previous, result, accepted_issues=workspace.accepted_issues or [],
                              summary_cache=(db.info.get('_planning_preparation') or db.info.get('_generation_plan') or {}).get('summary_cache'))
        now.extend(s for s in data['shifts'] if s['id'] in set(data['focus_ids']))
        captured.append(data)
        if result.get('scope') == 'command_changes':
            delta_scope = result.get('_command_scope') or {}
            overlap_focus = result['_command_scope']['shifts']
            result = defer_complete_check(db, actor, workspace, previous, result, data)
            captured[-1] = {**data, '_changed_overlap_focus': overlap_focus}
        else:
            workspace.validation = result
    data = captured[0]
    current_fingerprint = data.get('fingerprint') or ''
    current_organisation = data.get('organisation') or {}
    from .planning_groups import record_group_changes
    if pending_reply:
        from .workspace_access import access_record
        if access_record(db, workspace)['purpose'] == 'integrated':
            # Group provenance still uses the canonical dated Conditions inputs, without fairness or scoring.
            org = db.get(m.Organisation, db.get(m.Department, workspace.department_id).organisation_id)
            group_data = {**data, **snapshots.snapshot_inputs(db, org)}
            record_group_changes(db, workspace, change, before, group_data)
    else:
        record_group_changes(db, workspace, change, before, data)
    focus_ids = set(data['focus_ids'])
    previous_ids = {row['id'] for row in before['shifts']}
    # Compare both sides with the same derived recurrence state, without changing the stored Undo snapshot.
    old_data = with_series_state({**data,
        'shifts': before['shifts'] + [row for row in data['shifts'] if row['id'] not in focus_ids],
        'focus_ids': previous_ids})
    was = {row['id']: row for row in old_data['shifts'][:len(before['shifts'])]}
    if db.info.get('_planner_preview'):
        # Impact comparisons consume content only; the complete check still supplies actionable findings.
        from .series_conflicts import finding_actions
        return {'workspace': record(workspace, exclude=('validation',)),
                'validation': finding_actions(db, actor, result),
                'shifts': [row for row in now if was.get(row['id']) != row],
                'removed_shift_ids': sorted(set(was) - {row['id'] for row in now})}
    from packages.domain.overlap import per_session_overlaps
    from .series_conflicts import session_actions, finding_actions
    overlap_focus = data.get('_changed_overlap_focus')
    overlap_data = {**data, 'focus_ids': overlap_focus} if overlap_focus is not None else data
    old_overlap_data = {**old_data, 'focus_ids': overlap_focus} if overlap_focus is not None else old_data
    if '_command_overlap_flags' in db.info:
        flags, old_flags = db.info.pop('_command_overlap_flags')
    elif '_command_overlap_flags' in data:
        flags, old_flags = data['_command_overlap_flags']
    else:
        def compute_overlap_flags():
            current = per_session_overlaps(overlap_data, conflicts=overlap_witness.get('conflicts'), focus_ids=focus_ids)
            previous = per_session_overlaps(old_overlap_data, focus_ids=previous_ids) if previous_ids else {}
            return current, previous
        flags, old_flags = command_compute(db, 'overlap_flags', overlap_data, compute_overlap_flags,
            options={'focus_ids': sorted(focus_ids), 'previous_focus_ids': sorted(previous_ids)})
    before_rows = [{**row, 'double_booking_conflicts': old_flags[row['id']]} if old_flags.get(row['id']) else row
                   for row in was.values()]
    now = [{**row, 'double_booking_conflicts': flags.get(row['id'], [])}
           if flags.get(row['id']) or old_flags.get(row['id']) else row for row in now]
    was = {r['id']: r for r in before_rows}
    changed_rows = session_actions(db, actor, [row for row in now if was.get(row['id']) != row], disclose=False)
    result = finding_actions(db, actor, {**result, **({'checking': True} if pending_reply else {})})
    delta = edit_delta(before, workspace, changed_rows, sorted(set(was) - {r['id'] for r in now}),
                       previous_validation, result, scope=locals().get('delta_scope'),
                       current_fingerprint=current_fingerprint,
                       current_input_revision=current_organisation.get('input_revision'),
                       current_availability_revision=current_organisation.get('availability_revision'))
    workspace_record = record(workspace, exclude=("validation",))
    from .people import with_adjustment_names
    workspace_record = with_adjustment_names(db, actor, workspace_record)
    from packages.domain.rota_summary import rota_summary
    summary = rota_summary(result, accepted_issues=workspace.accepted_issues or [],
                           cache=(db.info.get('_planning_preparation') or db.info.get('_generation_plan') or {}).get('summary_cache'))
    return {"workspace": workspace_record, "validation": result, "health": health(workspace, validation=result, summary=summary),
            "shifts": changed_rows, "removed_shift_ids": sorted(set(was) - {r['id'] for r in now}),
            'fix_list_delta': result['fix_list_delta'],
            'fix_list': rota_fix_list(workspace, validation=result, summary=summary),
            "issues": issue_changes(issues_before, {issue_key(f): f for f in result['findings']}),
            "delta": delta}


def rota_fix_list(workspace, *, validation=None, summary=None):
    from .acknowledged_gaps import projection
    return projection(workspace, validation, summary=summary)


def health(workspace, *, validation=None, summary=None):
    """A short summary of the stored rota check, so lists can say what needs doing without loading the rota."""
    v = (workspace.validation if validation is None else validation) or None
    if not v:
        return {"checked": False}
    from packages.domain.rota_summary import rota_summary
    if summary is None:
        summary = rota_summary(v, accepted_issues=getattr(workspace, 'accepted_issues', None) or [], compact=True)
    return {"checked": True, "valid": summary['valid'], "errors": summary['issues']['blocking'],
            "warnings": summary['issues']['advisory'], "open_places": summary['places_open'],
            "staffed": len({r['shift_id'] for r in v.get('coverage', []) if r['status'] in ('covered', 'gap')}),
            "score": (summary['score'] or {}).get('score'),
            "summary": {**{k: v for k, v in summary.items() if k not in ('sessions', 'findings', 'score', 'fix_list', 'observations', 'repair_tasks', 'cause_groups')},
                        'fix_list': {'summary': {**summary['fix_list']['summary'],
                                                 'check_status': None, 'check_queued_at': None}}},
            "uncapped_score": (summary['score'] or {}).get('uncapped_score'),
            "capped": (summary['score'] or {}).get('capped', False)}


def edit_delta(before, workspace, changed_rows, removed_shift_ids, previous_validation, validation, *, scope=None,
               current_fingerprint='', current_input_revision=None, current_availability_revision=None):
    """Build the bounded patch consumed by the planner after a draft edit.

    The command already has both sides of D65's scoped check.  Project only
    changed sessions, affected findings and their coverage; a broad operation
    deliberately returns ``None`` so the caller can use the authoritative full
    read.  This keeps the reply proportional to the edit rather than the rota.
    """
    from packages.domain.coverage import coverage_rows
    from .issues import issue_key

    revision_from = int(before['workspace']['revision'])
    revision_to = int(workspace.revision)
    shifts = list(changed_rows or [])
    removed = sorted(set(removed_shift_ids or []))
    touched_shifts = {row.get('id') for row in shifts if row.get('id')} | set(removed)
    touched_shifts |= {value for value in (scope or {}).get('shifts', ()) if value}
    before_shifts = {row['id']: row for row in before.get('shifts', [])}
    touched_people = {p.get('person_id') for row in shifts for p in row.get('participants', []) if p.get('person_id')}
    touched_people |= {p.get('person_id') for sid in touched_shifts for p in before_shifts.get(sid, {}).get('participants', [])
                       if p.get('person_id')}
    touched_people |= {value for value in (scope or {}).get('people', ()) if value}

    # A delta without the current input token would make the held check look
    # fresh when it is not.  The caller must use the normal full read instead.
    if not current_fingerprint or not touched_shifts and not touched_people:
        return None

    old_findings = {issue_key(row): row for row in (previous_validation or {}).get('findings', [])}
    new_findings = {issue_key(row): row for row in (validation or {}).get('findings', [])}

    # A structural edit can change a rest/weekly/pooled finding on a neighbouring
    # session without changing that session row.  Include every finding whose
    # stable issue key or evidence changed, so the client replaces the complete
    # finding scope and can remove resolved rows as well as add new ones.
    for key in set(old_findings) | set(new_findings):
        if old_findings.get(key) == new_findings.get(key):
            continue
        for finding in (old_findings.get(key), new_findings.get(key)):
            if not finding:
                continue
            if finding.get('shift_id'):
                touched_shifts.add(finding['shift_id'])
            if finding.get('person_id'):
                touched_people.add(finding['person_id'])
            touched_shifts.update((finding.get('detail') or {}).get('affected_shift_ids') or [])

    old_coverage = {repr((row.get('shift_id'), row.get('start'), row.get('end'))): row
                    for row in coverage_rows(previous_validation or {})}
    new_coverage = {repr((row.get('shift_id'), row.get('start'), row.get('end'))): row
                    for row in coverage_rows(validation or {})}
    for key in set(old_coverage) | set(new_coverage):
        if old_coverage.get(key) == new_coverage.get(key):
            continue
        for row in (old_coverage.get(key), new_coverage.get(key)):
            if row and row.get('shift_id'):
                touched_shifts.add(row['shift_id'])

    def finding_touched(row):
        if row.get('shift_id') in touched_shifts or row.get('person_id') in touched_people:
            return True
        detail = row.get('detail') or {}
        return bool(touched_shifts.intersection(detail.get('affected_shift_ids') or []))

    findings = [row for key, row in new_findings.items()
                if finding_touched(row)]
    coverage = [row for row in coverage_rows(validation or {})
                if row.get('shift_id') in touched_shifts]
    limits = s.EDIT_DELTA_LIMITS
    if (len(shifts) > limits['shifts'] or len(removed) > limits['shifts']
            or len(touched_shifts) > limits['coverage'] or len(touched_people) > limits['coverage']
            or len(findings) > limits['findings'] or len(coverage) > limits['coverage']):
        return None

    # Coverage rows are intervals.  Summary counts use the same strongest
    # bounds as coverage_places, taking each session's peak once rather than
    # summing overlapping intervals.
    all_coverage = coverage_rows(validation or {})
    by_shift = {}
    for row in all_coverage:
        if row.get('shift_id') not in by_shift:
            by_shift[row['shift_id']] = {'places_needed': 0, 'places_open': 0}
        peak = by_shift[row['shift_id']]
        peak['places_needed'] = max(peak['places_needed'], int(row.get('places_needed') or 0))
        peak['places_open'] = max(peak['places_open'], int(row.get('places_open') or 0))
    places_needed = sum(row['places_needed'] for row in by_shift.values())
    places_open = sum(row['places_open'] for row in by_shift.values())
    summary_counts = {
        'sessions': len(by_shift),
        'places_needed': places_needed,
        'places_filled': places_needed - places_open,
        'places_open': places_open,
        'open_sessions': sum(bool(row['places_open']) for row in by_shift.values()),
    }
    return {'base_revision': revision_from, 'revision': revision_to, 'shifts': shifts,
            'removed_shift_ids': removed, 'assignments': [], 'removed_assignment_ids': [],
            'findings': {'shift_ids': sorted(touched_shifts), 'person_ids': sorted(touched_people), 'items': findings},
            'coverage': {'shift_ids': sorted(touched_shifts), 'person_ids': sorted(touched_people), 'items': coverage},
            'summary_counts': summary_counts, 'current_fingerprint': current_fingerprint,
            'current_input_revision': current_input_revision,
            'current_availability_revision': current_availability_revision, 'stale': False}


def assignment_source(actor, source: s.AssignmentSourceKind, run_id=None):
    return dict(source=source, source_run_id=run_id, source_actor_id=actor.identity_id, source_at=m.now(),
                override_reason=None)


def assignment_override_people(db, workspace, value, previous, existing, typ, known):
    """New placements use the same absence, restriction and zero-limit checks as scheduling."""
    context_changed = existing is not None and (
        existing.assignment_type_id != value.assignment_type_id or existing.location_id != value.location_id
        or (known['shift_services'].get(existing.id, set()) if 'shift_services' in known else
            set(db.scalars(select(m.ShiftService.service_id).where(m.ShiftService.shift_id == existing.id))))
        != set(value.service_ids))
    moved = {p.person_id for p in value.participants if context_changed or p.person_id not in previous
             or (aware(previous[p.person_id].start), aware(previous[p.person_id].end)) != (p.start, p.end)}
    participant_fields_changed = {p.person_id for p in value.participants if p.person_id in previous and any(
        getattr(previous[p.person_id], key) != p.model_dump(mode='json')[key]
        for key in ('participation', 'overtime', 'team_lead', 'supervisor_person_id', 'segments'))}
    counted = moved | participant_fields_changed
    if not counted:
        return set()
    if 'assignment_inputs' not in known:
        from packages.domain.conditions import active_rules
        # Use the same draft snapshot as every other write path so live safety
        # overlay inputs cannot be bypassed by the bounded placement check.
        data = domain_snapshot(db, workspace, own_shifts=[], include_fairness=False)
        known['assignment_inputs'] = {**data, 'policies': active_rules(data['policies'], context=data)}
    data = known['assignment_inputs']
    shift = {**(record(existing) if existing else {}),
             **value.model_dump(mode='json', exclude={'workspace_id', 'shift_id', 'override_reason'}),
             'id': existing.id if existing else command_id(db), 'cancelled': False, 'locked': False,
             'type_revision': typ.revision, 'type_snapshot': known['snapshot', typ.id]}
    from packages.domain.candidate_evidence import candidate_evidence
    evidence = {pid: candidate_evidence(data, pid, shift)['exclusions'] for pid in moved}
    known['override_findings'] = [
        {'code': reason['code'], 'person_id': pid,
         'severity': 'error',
         'start': reason.get('start') or shift['start'], 'end': reason.get('end') or shift['end'],
         **({'record_id': reason['record_id']} if reason.get('record_id') else {}),
         **({'availability_id': reason['record_id']} if reason.get('record_id') and reason['code'] == 'UNAVAILABLE' else {}),
         **({'rotation_id': reason['record_id']} if reason.get('record_id') and reason['code'] == 'EXTERNAL_ROTATION' else {}),
         **({'rule_id': reason['rule_id']} if reason.get('rule_id') else {})}
        for pid, exclusions in evidence.items() for reason in exclusions
        # Dev's interactive gate: only availability, away rotations and restrictions refuse a manual
        # placement; other exclusions (eligibility, membership, rank, overlap, rest) save as findings.
        if reason['code'] in MANUAL_PLACEMENT_BLOCKERS]
    blocked = {finding['person_id'] for finding in known['override_findings']}
    known['override_allowed_people'] = {pid for pid in blocked if all(
        assignment_reason_allowed(data, finding)
        for finding in known['override_findings'] if finding['person_id'] == pid)}
    known['existing_allowed_people'] = {pid for pid in blocked
                                        if pid in previous
                                        and (aware(previous[pid].start), aware(previous[pid].end)) ==
                                        next((p.start, p.end) for p in value.participants if p.person_id == pid)
                                        and (not previous[pid].override_reason or pid not in participant_fields_changed)
                                        and pid in known['override_allowed_people']}
    from apps.api.workspace_baselines import _zero_limit_policy
    zero_limits = [zero for policy in data['policies'] if (zero := _zero_limit_policy(policy)) is not None]
    if zero_limits and counted - blocked:
        shift['participants'] = [{**p, 'id': command_id(db), 'shift_id': shift['id']} for p in shift['participants']]
        trial = {**data, 'policies': zero_limits, 'shifts': [shift], 'focus_ids': [shift['id']],
                 'quota_subjects': counted, '_partial': {'shifts': {shift['id']}, 'people': counted}}
        if (trials := known.get('zero_limit_trials')) is not None:
            # A batch caller checks every trial in one prepared calculation (zero_limit_blocked).
            trials.append(trial)
            return blocked
        checked = command_evaluate(db, trial)
        blocked |= zero_limit_blocked(checked)
        known['override_findings'].extend(
            {'code': f['code'], 'message': f['message'], 'person_id': f['person_id'], 'start': f.get('start'), 'end': f.get('end'),
             'severity': f.get('severity'), 'rule_id': f.get('rule_id'), 'detail': f.get('detail')}
            for f in checked['person_findings']
            if f['severity'] == 'error' and f['code'] in ZERO_LIMIT_FINDING_CODES)
        known['override_allowed_people'] = {pid for pid in blocked if all(
            assignment_reason_allowed(data, finding)
            for finding in known['override_findings'] if finding['person_id'] == pid)}
    return blocked


def zero_limit_blocked(checked):
    return {f['person_id'] for f in checked['person_findings'] if f['severity'] == 'error'
            and f['code'] in ZERO_LIMIT_FINDING_CODES}


def zero_limit_trial_results(db, trials):
    """The same per-session zero-limit evaluations as assignment_override_people, as one prepared calculation:
    preparation runs once for the whole batch instead of once per session, and the writer verifies one manifest."""
    if not trials:
        return []
    if check := db.info.get('_check_budget'):
        trials = [{**trial, '_check_budget': check} for trial in trials]
    shifts = [trial['shifts'][0] for trial in trials]
    combined = {**trials[0], 'shifts': shifts, 'focus_ids': [shift['id'] for shift in shifts],
                'quota_subjects': set().union(*(trial['quota_subjects'] for trial in trials)),
                '_partial': {'shifts': {shift['id'] for shift in shifts},
                             'people': set().union(*(trial['_partial']['people'] for trial in trials))}}
    return command_compute(db, 'zero_limit_trials', combined, lambda: [evaluate(trial) for trial in trials],
                           options=[[trial['focus_ids'][0], sorted(trial['quota_subjects'])] for trial in trials])


def write_shift(db, actor, workspace, value, existing=None, series_id=None, batch=None, *,
                source: s.AssignmentSourceKind = 'manual', preserve_source=False, inputs=None, override_refusal=None):
    if existing:
        require(existing.workspace_id == workspace.id, "NOT_FOUND", "Session not found", 404)
        require(not existing.locked, "ASSIGNMENT_LOCKED", "Explicitly unlock the session before editing", 409)
    previous = {p.person_id: p for p in db.scalars(select(m.Assignment).where(
        m.Assignment.shift_id == existing.id))} if existing else {}
    known = batch.setdefault('inputs', {}) if batch is not None else inputs if inputs is not None else {}
    def checked(cls, object_id):
        key = (cls, object_id)
        if key not in known:
            known[key] = person_access(db, actor, object_id) if cls is m.Person else scoped(db, cls, object_id, actor)
        return known[key]
    # Keep checked rows alive for this batch; reads must not flush one pending session at a time.
    with db.no_autoflush:
        typ = checked(m.AssignmentType, value.assignment_type_id)
        checked(m.Location, value.location_id)
        for service_id in value.service_ids:
            checked(m.Service, service_id)
        for participant in value.participants:
            checked(m.Person, participant.person_id)
            if participant.supervisor_person_id:
                checked(m.Person, participant.supervisor_person_id)
        if 'organisation' not in known:
            known['organisation'] = db.get(m.Organisation, actor.organisation_id)
    zone = known['organisation'].timezone
    require(local_midnight(workspace.starts_on, zone) <= value.start and value.end <= local_midnight(workspace.ends_on, zone),
            "OUTSIDE_WORKSPACE", "The full session, including its overnight end, must lie inside the workspace")
    if ('definition', typ.id) not in known:
        known['definition', typ.id] = s.TypeDefinition.model_validate(typ.definition)
        known['snapshot', typ.id] = record(typ)
    definition = known['definition', typ.id]
    require(all(p.start == value.start and p.end == value.end for p in value.participants),
            'FULL_ASSIGNMENT_REQUIRED', 'Every person must book the complete assignment; configure a separate shorter duty when needed')
    require(configured_duration_matches(typ.definition, value.start, value.end, zone),
            'CONFIGURED_DURATION_REQUIRED', 'Book the complete configured duration; configure a separate duty type for shorter duties')
    require(definition.session_unit_basis == 'fixed', 'FIXED_SESSION_UNITS_REQUIRED',
            'Update this duty type to count session units per complete assignment')
    s.validate_attributes(definition, value.attributes)
    require((value.end - value.start).total_seconds() / 60 <= definition.maximum_duration_minutes,
            "DURATION_LIMIT", "Duration exceeds the assignment type's maximum")
    require(len(value.service_ids) <= 1 or definition.allow_shared_services, "UNSUPPORTED_SHARED_DUTY", "Type does not support a reviewed common duty")
    require(definition.purpose != "staffed" or bool(value.service_ids), "SERVICE_REQUIRED", "A staffed duty needs at least one service")
    reason = getattr(value, 'override_reason', None)
    reason = reason.strip() if reason is not None else None
    overridden = (assignment_override_people(db, workspace, value, previous, existing, typ, known)
                  if override_refusal else set())
    allowed = known.get('override_allowed_people', set())
    if overridden - allowed:
        from .staffing import exclusion_text
        # Name what blocks first (rank, membership, fixed usual assignees, a commitment...), then why no reason helps.
        first = next((f for f in known.get('override_findings', [])
                      if f['person_id'] in overridden - allowed and not assignment_reason_allowed(known['assignment_inputs'], f)),
                     None)
        require(False, 'EDIT_INVALID',
                (exclusion_text(first['code']) + ' ' if first else '')
                + 'This edit introduces a conflict that a manual reason cannot override', 422,
                {'findings': known.get('override_findings', [])})
    needs_reason = overridden - known.get('existing_allowed_people', set())
    if needs_reason:
        from .staffing import blocker_text
        # Always name who needs a reason (one or several); one reason never admits several people.
        details = {'people': sorted(needs_reason),
                   'findings': [f for f in known.get('override_findings', []) if f.get('person_id') in needs_reason]}
        zero_findings = [f for f in details['findings'] if f['code'] in ZERO_LIMIT_FINDING_CODES]
        current_rule = next((f['rule_id'] for f in zero_findings if (f.get('detail') or {}).get('live_zero')), None)
        if current_rule:
            override_refusal += f' (current department rule {current_rule})'
        reasoned = source == 'manual' and reason is not None and len(reason) >= 3
        if len(needs_reason) > 1 and reasoned:
            require(False, 'PERSON_UNAVAILABLE',
                    'Enter a reason for one person at a time; each admission covers one person and their findings'
                    + (f' (current department rule {current_rule})' if current_rule else ''), 422, details)
        first = details['findings'][0] if details['findings'] else None
        require(reasoned and len(needs_reason) == 1, 'PERSON_UNAVAILABLE',
                (blocker_text(first['code']) + ' ' if first else '') + override_refusal, 422, details)
        blockers = []
        for finding in known.get('override_findings', []):
            if finding.get('person_id') not in needs_reason:
                continue
            blocker = {'code': finding['code'], 'person_id': finding['person_id']}
            if finding.get('shift_id'):
                blocker['shift_id'] = finding['shift_id']
            if finding.get('availability_id'):
                blocker['availability_id'] = finding['availability_id']
            elif finding.get('rotation_id'):
                blocker['rotation_id'] = finding['rotation_id']
            elif finding.get('rule_id'):
                blocker['rule_id'] = finding['rule_id']
            blockers.append(blocker)
        db.info.setdefault('_assignment_override_blockers', []).extend(blockers)
    if existing:
        row = existing
        row.revision += 1
        if row.series_id:
            row.series_modified = True
        db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id == row.id))
    else:
        row = m.Shift(id=command_id(db), workspace_id=workspace.id, series_id=series_id)
        if batch is None:
            db.add(row)
        else:
            batch.setdefault('shifts', []).append(row)
    row.assignment_type_id, row.type_revision, row.type_snapshot = typ.id, typ.revision, known['snapshot', typ.id]
    row.location_id, row.start, row.end, row.attributes = value.location_id, value.start, value.end, value.attributes
    if batch is None:
        db.flush()
    links, retained_ids = [], set()
    for service_id in value.service_ids:
        links.append(m.ShiftService(shift_id=row.id, service_id=service_id))
    for participant in value.participants:
        fields = participant.model_dump(exclude={"segments"})
        prior = previous.get(participant.person_id)
        if 'fixed' not in participant.model_fields_set:
            fields['fixed'] = prior.fixed if prior else False
        fields['segments'] = [{"start": part.start.isoformat(), "end": part.end.isoformat()} for part in participant.segments]
        # Reconciliation explicitly retains people; ordinary manual assignment edits record a new decision.
        unchanged = prior and (preserve_source or all(
            field == (aware(getattr(prior, key)) if key in {'start', 'end'} else getattr(prior, key))
            for key, field in fields.items() if key != 'fixed'))
        provenance = ({key: getattr(prior, key) for key in s.AssignmentSource.model_fields} if unchanged
                      else assignment_source(actor, source))
        retained = prior and (preserve_source or (aware(prior.start), aware(prior.end)) == (participant.start, participant.end))
        provenance['override_reason'] = reason if participant.person_id in needs_reason else prior.override_reason if retained else None
        # Keep the actual row only when both normalized content and the resulting provenance are unchanged.
        if prior and all(
            (aware(field) if key in {'start', 'end', 'source_at'} and field is not None else field)
            == (aware(getattr(prior, key)) if key in {'start', 'end', 'source_at'} and getattr(prior, key) is not None
                else getattr(prior, key))
            for key, field in {**fields, **provenance}.items()):
            retained_ids.add(prior.id)
            continue
        links.append(m.Assignment(id=command_id(db), shift_id=row.id, **fields, **provenance))
    replaced_ids = [prior.id for prior in previous.values() if prior.id not in retained_ids]
    if replaced_ids:
        db.execute(delete(m.Assignment).where(m.Assignment.id.in_(replaced_ids)))
    if batch is None:
        db.add_all(links)
        db.flush()
    else:
        batch.setdefault('links', []).extend(links)
    return row


def flush_shift_batch(db, batch):
    # IDs are already assigned: bulk inserts need neither ORM bookkeeping nor returned defaults per row.
    db.flush()
    shifts = batch.pop('shifts', [])
    if shifts:
        db.execute(insert(m.Shift), [{k: v for k, v in vars(row).items() if k != '_sa_instance_state'} for row in shifts])
    links = batch.pop('links', [])
    for cls in (m.ShiftService, m.Assignment):
        rows = [{k: v for k, v in vars(row).items() if k != '_sa_instance_state'} for row in links if isinstance(row, cls)]
        if rows:
            db.execute(insert(cls), rows)
    db.flush()


def chunks(ids, size=1000):
    """`ids` in lists of at most `size`, for IN (...) clauses."""
    return [ids[k:k + size] for k in range(0, len(ids), size)]


def selected_shifts(db, workspace, ids):
    ids = set(ids)
    rows = {row.id: row for chunk in chunks(sorted(ids)) for row in db.scalars(select(m.Shift).where(
        m.Shift.workspace_id == workspace.id, m.Shift.id.in_(chunk)))}
    require(len(rows) == len(ids), 'NOT_FOUND', 'Session not found', 404)
    return rows


def remove_participants(db, pairs):
    for chunk in chunks(sorted(set(pairs))):
        db.execute(delete(m.Assignment).where(tuple_(m.Assignment.shift_id, m.Assignment.person_id).in_(chunk)))


def clear_shift_participants(db, ids):
    for chunk in chunks(sorted(set(ids))):
        db.execute(delete(m.Assignment).where(m.Assignment.shift_id.in_(chunk)))


def remove_shifts(db, ids):
    clear_shift_participants(db, ids)
    for chunk in chunks(sorted(set(ids))):
        db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id.in_(chunk)))
        db.execute(delete(m.Shift).where(m.Shift.id.in_(chunk)))


def prime_shift_batch(db, actor, batch, references):
    """Read each referenced catalogue/person group once; retain the existing access checks."""
    known = batch.setdefault('inputs', {})
    for cls, ids in references:
        for chunk in chunks(sorted(ids)):
            for row in db.scalars(select(cls).where(cls.id.in_(chunk))):
                known[cls, row.id] = row
            for object_id in chunk:
                known[cls, object_id] = person_access(db, actor, object_id) if cls is m.Person else scoped(db, cls, object_id, actor)


def prime_series_batch(db, actor, batch, series_rows):
    definitions = [stored_series_definition(row.definition) for row in series_rows]
    prime_shift_batch(db, actor, batch, (
        (m.AssignmentType, {row.assignment_type_id for row in series_rows}),
        (m.Location, {row.location_id for row in series_rows}),
        (m.Service, {sid for definition in definitions for sid in definition.service_ids}),
        (m.Person, {pid for definition in definitions for pid in definition.default_person_ids})))


def selected_series(db, actor, ids):
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    known = list(db.scalars(select(m.RecurringSeries).where(m.RecurringSeries.id.in_(ids))))
    return [scoped(db, m.RecurringSeries, sid, actor) for sid in ids]


def copy_snapshot_into(db, actor, workspace, source, preserve_ids=False, offset=timedelta(0), detach_series=False, session_map=None):
    assignment_map, batch = {}, {'shifts': [], 'links': []}
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    lo, hi = local_midnight(workspace.starts_on, zone), local_midnight(workspace.ends_on, zone)
    prime_shift_batch(db, actor, batch, (
        (m.Person, {p['person_id'] for old in source['shifts'] for p in old['participants']}),
        (m.Service, {sid for old in source['shifts'] for sid in old['service_ids']})))
    for old in source["shifts"]:
        lineage = old.get('lineage_id')
        if not preserve_ids:
            lineage = (lineage or old['id']) if not detach_series and not offset else None
        row = m.Shift(id=old["id"] if preserve_ids else command_id(db), workspace_id=workspace.id,
                      lineage_id=lineage,
                      assignment_type_id=old["assignment_type_id"], type_revision=old["type_revision"],
                      type_snapshot=old["type_snapshot"], location_id=old["location_id"],
                      start=aware(old["start"]) + offset, end=aware(old["end"]) + offset,
                      cancelled=old["cancelled"], locked=old["locked"], attributes=old["attributes"],
                      cancellation_reason=old.get("cancellation_reason"),
                      series_id=None if detach_series else old.get("series_id"),
                      series_revision=None if detach_series else old.get("series_revision"),
                      series_snapshot=None if detach_series else old.get('series_snapshot'),
                      occurrence_on=None if detach_series or not old.get("occurrence_on") else date.fromisoformat(old["occurrence_on"]),
                      series_modified=False if detach_series else old.get("series_modified", False),
                      template_skipped=old.get('template_skipped', []))
        if preserve_ids:
            row.created_at, row.revision = aware(old["created_at"]), old["revision"]
        require(lo <= row.start and row.end <= hi, "OUTSIDE_WORKSPACE", "Copied session falls outside the workspace")
        batch['shifts'].append(row)
        if session_map is not None:
            session_map[row.id] = old['id']
        for service_id in old["service_ids"]:
            batch['links'].append(m.ShiftService(shift_id=row.id, service_id=service_id))
        for p in old["participants"]:
            new_id = p["id"] if preserve_ids else command_id(db)
            assignment_map[p["id"]] = new_id
            restored = {"created_at": aware(p["created_at"]), "revision": p["revision"]} if preserve_ids else {}
            provenance = (s.AssignmentSource.model_validate({key: p[key] for key in s.AssignmentSource.model_fields if key in p}).model_dump()
                          if preserve_ids or not detach_series and not offset
                          else assignment_source(actor, 'manual'))
            batch['links'].append(m.Assignment(id=new_id, shift_id=row.id, **restored,
                                  **provenance,
                                  person_id=p["person_id"], start=aware(p["start"]) + offset,
                                  end=aware(p["end"]) + offset, participation=p["participation"], overtime=p["overtime"],
                                  fixed=p.get('fixed', bool(old.get('series_id'))),
                                  supervisor_person_id=p.get('supervisor_person_id'), team_lead=p.get('team_lead', False),
                                  segments=shifted_segments(p, offset)))
    flush_shift_batch(db, batch)
    return assignment_map


def create_workspace(db, actor, command, settings):
    require_role(actor, "coordinator")
    payload = dict(command.payload)
    baseline_scope = payload.pop("baseline", None)
    if baseline_scope is None:
        baseline_scope = payload.pop("snapshot_scope", None)
    if baseline_scope is None and ("baseline_mode" in payload or "baseline_sources" in payload):
        baseline_scope = {"mode": payload.pop("baseline_mode", "department"),
                          "sources": payload.pop("baseline_sources", [])}
    if baseline_scope is not None:
        baseline_scope = s.WorkspaceBaselineScope.model_validate(baseline_scope).model_dump(mode="json")
    value = s.WorkspaceInput.model_validate(payload)
    from .planning_groups import admit_private, designate
    from .workspace_access import append_access
    if value.visibility == 'private':
        admit_private(db, actor)
    organisation = db.get(m.Organisation, actor.organisation_id)
    fork_source, fork_base_content = None, None
    if value.source_workspace_id:
        fork_source = scoped(db, m.Workspace, value.source_workspace_id, actor)
        expected(fork_source, value.source_revision)
        fork_base_content = workspace_content(db, fork_source)
    base = None
    if value.base_publication_id:
        base = scoped(db, m.Publication, value.base_publication_id, actor)
        require(base.current, "SUPERSEDED_BASE", "Start an amendment from the current publication", 409)
        require(value.starts_on == base.starts_on and value.ends_on == base.ends_on,
                "AMENDMENT_SCOPE", "A change to a published rota keeps that rota's dates")
    workspace = m.Workspace(created_at=command_time(db), department_id=actor.department_id, created_by=actor.identity_id,
                            last_edited_by=actor.identity_id, **value.model_dump(exclude={
                                'source_workspace_id', 'source_revision', 'copy_people', 'copy_alignment', 'series_ids', 'generate_assignments',
                                'fork_from', 'visibility', 'purpose', 'baseline'}))
    db.add(workspace)
    db.flush()
    if value.purpose == 'integrated':
        designate(db, actor, workspace, 'Created the department rota')
    elif value.visibility != 'shared' or value.purpose != 'alternative' or 'purpose' in value.model_fields_set:
        append_access(db, actor, workspace, value.visibility, value.purpose, 'Created a planning draft')
    if workspace.replaces_publication_id:
        schedule.check_replacement(db, workspace)
    if base:
        from .rota_adjustments import inherited_adjustments
        workspace.target_overrides = inherited_adjustments(base, 'target_overrides')
        workspace.rule_adjustments = inherited_adjustments(base, 'rule_adjustments')
        # Only the days it is still in force: a later rota may have replaced it from a date.
        copy_snapshot_into(db, actor, workspace, schedule.in_force_snapshot(db, base))
    setup, batch, fork_session_map = {'created': 0, 'skipped': 0}, {'inputs': {'organisation': organisation}}, {}
    if value.source_workspace_id:
        source = fork_source
        require(source.ends_on - source.starts_on == value.ends_on - value.starts_on,
                'PERIOD_LENGTH_MISMATCH', 'The source and new rota must cover the same number of days')
        same_period_fork = (value.purpose == 'sandbox' and source.starts_on == value.starts_on
                            and source.ends_on == value.ends_on)
        if same_period_fork:
            copied = copy.deepcopy(fork_base_content)
            if not value.copy_people:
                for row in copied['shifts']:
                    row['participants'] = []
            copy_snapshot_into(db, actor, workspace, copied, session_map=fork_session_map)
            workspace.rule_adjustments = copy.deepcopy(source.rule_adjustments or [])
            workspace.target_overrides = copy.deepcopy(source.target_overrides or [])
            setup['created'] = len(copied['shifts'])
        zone = db.get(m.Organisation, actor.organisation_id).timezone
        local_zone = ZoneInfo(zone)
        source_shifts = [] if same_period_fork else fork_base_content['shifts']
        prime_shift_batch(db, actor, batch, (
            (m.AssignmentType, {r['assignment_type_id'] for r in source_shifts}),
            (m.Location, {r['location_id'] for r in source_shifts}),
            (m.Service, {sid for r in source_shifts for sid in r['service_ids']}),
            (m.Person, {pid for r in source_shifts for p in r['participants']
                        for pid in (p['person_id'], p.get('supervisor_person_id')) if pid} if value.copy_people else set())))
        for old in source_shifts:
            if old['cancelled']:
                continue
            a, b = aware(old['start']).astimezone(local_zone), aware(old['end']).astimezone(local_zone)
            target = value.starts_on + (a.date() - source.starts_on)
            finish = target + (b.date()-a.date())
            start = local_stamp(target, a.time().isoformat(), zone)
            end = local_stamp(finish, b.time().isoformat(), zone)
            if target >= value.ends_on or end > local_midnight(value.ends_on, zone):
                setup['skipped'] += 1
                continue
            def aligned(stamp):
                local = aware(stamp).astimezone(local_zone)
                return local_stamp(target + (local.date() - a.date()), local.time().isoformat(), zone)
            participants = [s.ParticipantInput(person_id=p['person_id'], start=aligned(p['start']), end=aligned(p['end']),
                fixed=p.get('fixed', bool(old.get('series_id'))),
                participation=p['participation'], overtime=p['overtime'],
                supervisor_person_id=p.get('supervisor_person_id'), team_lead=p.get('team_lead', False),
                segments=[{key: aligned(part[key]).isoformat()
                    for key in ('start', 'end')} for part in p.get('segments', [])])
                for p in old['participants']] if value.copy_people else []
            write_shift(db, actor, workspace, s.ShiftInput(assignment_type_id=old['assignment_type_id'],
                location_id=old['location_id'], service_ids=old['service_ids'], attributes=old['attributes'],
                start=start, end=end, participants=participants), batch=batch)
            setup['created'] += 1
    flush_shift_batch(db, batch)
    from apps.api.workspace_baselines import capture_draft_baseline
    baseline = capture_draft_baseline(db, workspace, actor.identity_id, baseline_scope,
                                      reason="Captured draft baseline on creation")
    shared_inputs = baseline.canonical_payload
    if fork_source is not None and value.purpose == 'sandbox' and fork_session_map:
        db.add(m.WorkspaceForkBase(department_id=actor.department_id, workspace_id=workspace.id,
            source_workspace_id=fork_source.id, source_revision=fork_source.revision,
            source_content_revision=fork_source.content_revision, base_content=fork_base_content,
            fork_content=workspace_content(db, workspace), session_map=fork_session_map, actor_id=actor.identity_id,
            reason='Created private draft from current rota'))
        db.flush()
    template_data = domain_snapshot(db, workspace, shared_inputs=shared_inputs,
        **({'own_shifts': []} if not (base or value.source_workspace_id) else {})) if value.series_ids else None
    skipped_people = []
    series_rows = selected_series(db, actor, value.series_ids)
    prime_series_batch(db, actor, batch, series_rows)
    for series in series_rows:
        require(series.active, 'SERIES_INACTIVE', 'Choose active recurring templates')
        definition = stored_series_definition(series.definition)
        start, end = max(workspace.starts_on, series.starts_on), min(workspace.ends_on, series.ends_on or workspace.ends_on)
        for occurrence in series_occurrences(template_data, series, definition, start, end):
            row = materialise_occurrence(db, actor, workspace, series, definition, occurrence, template_data, batch=batch)
            row.series_revision = series.revision
            row.occurrence_on = occurrence['start'].astimezone(ZoneInfo(definition.timezone)).date()
            skipped_people.extend({'shift_id': row.id, **r} for r in row.template_skipped)
            setup['created'] += 1
    flush_shift_batch(db, batch)
    require(setup['created'] <= MAX_SHIFTS, 'WORKSPACE_LIMIT', f'A rota supports at most {MAX_SHIFTS} sessions')
    if workspace.urgency == "urgent":
        reviews.resolve_route(db, workspace)
    # Only this draft's sessions changed; catalogues and external context remain under the same write lock.
    if value.visibility == 'private' and fork_base_content is not None:
        from .jobs import queue_validation
        data = domain_snapshot(db, workspace, shared_inputs=shared_inputs)
        preparation = db.info.get('_planning_preparation')
        if preparation is None or preparation['replay']:
            queue_validation(db, actor, workspace, data)
        result = None
    else:
        result = revalidate(db, workspace, external_snapshot=template_data, shared_inputs=shared_inputs,
                            final_compute=not value.generate_assignments)
    if value.generate_assignments:
        from .jobs import create_job
        create_job(db, actor, s.Command(kind='job.create', expected_revision=workspace.revision,
            payload={'workspace_id': workspace.id, 'kind': 'generation',
                     'repair_unavailable': True, 'repair_conflicts': True}), settings)
    # Published rotas and other drafts on these days: the client warns and offers the ways out (schedule.overlaps).
    from apps.api.workspace_baselines import attach as attach_baseline, projection as baseline_projection
    workspace_value = attach_baseline(db, workspace, record(workspace, exclude=("validation",)), baseline=baseline)
    return {"workspace": workspace_value, "baseline": baseline_projection(baseline),
            "snapshot_hash": baseline.payload_hash, "validation": result, 'setup': setup, 'template_skipped': skipped_people,
            'overlaps': schedule.overlaps(db, workspace.department_id, workspace)}


def resolve_overlap(db, actor, command):
    """Place a draft beside the published rota it overlaps: start after it, or replace it from a date."""
    value = s.OverlapChoice.model_validate(command.payload)
    workspace = draft(db, actor, command.target_id, command.expected_revision)
    require(not workspace.base_publication_id, "AMENDMENT_SCOPE", "A change to a published rota keeps that rota's dates", 409)
    option = next((o for o in schedule.overlaps(db, workspace.department_id, workspace)["options"] if o["choice"] == value.choice), None)
    require(option is not None, "NO_OVERLAP", "This rota does not overlap a published one", 409)
    require(option["available"], "OVERLAP_CHOICE_UNAVAILABLE", option["why"] or "That choice is not available for these dates", 409)
    start = date.fromisoformat(option["starts_on"])
    if value.choice == "replace_from" and value.starts_on:
        require(option["earliest"] <= value.starts_on.isoformat() <= option["latest"], "REPLACEMENT_SCOPE",
                f"Replace from a day between {option['earliest']} and {option['latest']}", 409)
        start = value.starts_on
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    early = db.scalar(select(func.count()).select_from(m.Shift).where(m.Shift.workspace_id == workspace.id,
                      m.Shift.start < local_midnight(start, zone)))
    require(not early, "OUTSIDE_WORKSPACE", f"{early} session{'s are' if early != 1 else ' is'} before {start.isoformat()}; "
            "move or remove them first", 409)
    before = workspace_content(db, workspace)
    workspace.starts_on = start
    workspace.replaces_publication_id = option["publication_id"] if value.choice == "replace_from" else None
    if workspace.replaces_publication_id:
        schedule.check_replacement(db, workspace)
    return changed(db, actor, workspace, before, value.reason)


# The one number each adjustable rule kind has (what a rota may change).
ADJUSTABLE = {kind: field for kind, (field, _, _) in NUMBERS.items()}


def adjusted_rule(policy, entry):
    """The rule as this rota uses it (validated like any rule), or None when switched off."""
    if weakens_safety(policy, entry):
        return policy  # Keep the stored adjustment for audit/clear, without weakening the safety rule.
    if entry.get('line_id'):
        return scoped_adjusted_rule(policy, entry)
    if entry["mode"] == "off":
        return None
    definition = copy.deepcopy(policy["definition"])
    if s.safety_rule(definition):
        definition['exception_allowed'] = False  # Historical flags grant no waiver; stored evidence stays intact.
    rule = definition['rule']
    amount = entry["amount"]
    if rule['kind'] == 'catalogue':
        number = rule_number(rule)
        if number is None:
            raise ValueError('This catalogue rule has no single number to change')
        rule['lines'][0]['numbers'][number['field']]['value'] = amount
    else:
        field = ADJUSTABLE[rule['kind']]
        rule[field] = int(amount) if isinstance(rule[field], int) and float(amount).is_integer() else amount
    raw = stored_definition(definition) if rule['kind'] == 'catalogue' else definition
    validated = s.PolicyDefinition.model_validate(raw).model_dump(mode="json")
    result = {**policy, "definition": validated, "rota_adjusted": True}
    if rule['kind'] == 'catalogue':
        for line, source in zip(validated['rule']['lines'], rule['lines'], strict=True):
            line.update((key, value) for key, value in source.items() if key.startswith('_'))
        from packages.domain.rule_lines import expand as expand_rule
        expand_rule(result)  # Catalogue v1 numbers are validated by their resolved primitive schemas.
    return result


def scoped_adjusted_rule(policy, entry):
    """Project a saved line/number adjustment; department rows and other people/dates retain their meaning."""
    from packages.domain.rule_lines import expand, tier, when
    definition = copy.deepcopy(policy['definition'])
    rule = definition['rule']
    if rule['kind'] != 'catalogue':
        return policy
    projected, metadata = [], {}
    for n, line in enumerate(rule['lines']):
        identity = line.get('_source_identity', line['id'])
        key = entry['number']
        a, b = when(rule, line)
        first = max(str(d) for d in (policy.get('starts_on'), a, entry['starts_on']) if d)
        last = min(str(d) for d in (policy.get('ends_on'), b, entry['ends_on']) if d)
        original = {k: v for k, v in line.items() if not k.startswith('_')}
        source = {**{k: copy.deepcopy(v) for k, v in line.items() if k.startswith('_')},
                  '_source_identity': identity, '_source_index': line.get('_source_index', n), '_source_tier': tier(line)}
        def add(row, label):
            row = copy.deepcopy(row)
            row['id'] = str(uuid5(NAMESPACE_URL, f"{line['id']}:{entry['id']}:{label}"))
            projected.append(row)
            metadata[row['id']] = copy.deepcopy(source)
            return row['id']
        person = entry.get('person_id')
        if (identity != entry['line_id'] or key not in line['numbers'] or first >= last or person and (
                source.get('_adjustment_person_id') not in (None, person) or
                person in source.get('_adjustment_excluded_people', []))):
            projected.append(original)
            metadata[original['id']] = source
            continue
        other_numbers = {k: v for k, v in original['numbers'].items() if k != key}
        if other_numbers:
            add({**original, 'numbers': other_numbers,
                 'source_bindings': {k: v for k, v in original.get('source_bindings', {}).items() if k != key}}, 'other')
        single = {**original, 'numbers': {key: original['numbers'][key]},
                  'source_bindings': {k: v for k, v in original.get('source_bindings', {}).items() if k == key}}
        if not a or str(a) < first:
            add({**single, 'ends_on': first}, 'before')
        if not b or last < str(b):
            add({**single, 'starts_on': last}, 'after')
        during = {**single, 'starts_on': first, 'ends_on': last}
        if person:
            # Disjoint person branches retain the authored predicate; exclusions never nest per adjustment.
            base_who = source.get('_adjustment_base_who', original.get('who'))
            def who(op, people):
                return {'match': 'all', 'conditions': ([base_who] if base_who else []) + [
                    {'field': 'person_ids', 'op': op, 'values': people}]}
            if not source.get('_adjustment_person_id'):
                excluded = sorted(set(source.get('_adjustment_excluded_people', [])) | {person})
                ident = add({**during, 'who': who('none', excluded)}, 'others')
                metadata[ident].update(_adjustment_base_who=copy.deepcopy(base_who),
                                       _adjustment_excluded_people=excluded)
            source.update(_adjustment_base_who=copy.deepcopy(base_who), _adjustment_person_id=person,
                          _adjustment_excluded_people=[])
            during = {**during, 'who': who('any', [person])}
        if entry['mode'] != 'off':
            add({**during, 'numbers': {key: {**single['numbers'][key], 'value': entry['amount']}}}, 'adjusted')
    rule['lines'] = projected
    definition = s.PolicyDefinition.model_validate(definition).model_dump(mode='json')
    for line in definition['rule']['lines']:
        line.update(metadata[line['id']])
    result = {**policy, 'definition': definition, 'rota_adjusted': True}
    expand(result)
    return result


def adjusted_rules(policy, entries):
    """All consumers project every scope, rather than dropping all but one row per policy."""
    for entry in sorted(entries, key=lambda r: bool(r.get('line_id'))):
        if policy is None:
            break
        source_id = (policy.get('_source_policy_id', policy['id'])
                     if policy.get('_live_zero') or policy.get('_live_mandatory_recheck') else policy['id'])
        if entry['policy_id'] == source_id:
            policy = adjusted_rule(policy, entry)
    return policy


def rule_adjustment_rows(workspace, policy, value, identity_id, *, db=None):
    """One admission and scope replacement for the command and read-only candidate previews."""
    require(policy['status'] == "active", "RULE_NOT_ACTIVE", "Only active department rules can be adjusted for a rota", 409)
    scope = value.model_dump(mode='json', include={'line_id', 'number', 'person_id', 'starts_on', 'ends_on'})
    if value.line_id:
        rule = policy['definition']['rule']
        line = next((line for line in rule.get('lines', []) if line['id'] == value.line_id), None)
        require(line is not None and value.number in line['numbers'], 'NOT_ADJUSTABLE', 'Choose a number on an active catalogue line')
        from packages.domain.rule_lines import when
        a, b = when(rule, line)
        first = max([workspace.starts_on, policy['starts_on'] if isinstance(policy['starts_on'], date) else date.fromisoformat(policy['starts_on']),
                     *([date.fromisoformat(str(a))] if a else [])])
        last = min([workspace.ends_on, *([date.fromisoformat(str(b))] if b else []),
                    *([date.fromisoformat(str(policy['ends_on']))] if policy.get('ends_on') else [])])
        scope.update(starts_on=str(value.starts_on or first), ends_on=str(value.ends_on or last))
        require(first <= date.fromisoformat(scope['starts_on']) < date.fromisoformat(scope['ends_on']) <= last,
                'RELAX_PERIOD', 'Choose adjustment dates within the rota and binding line')
    def same(row):
        return row['policy_id'] == policy['id'] and all(row.get(key) == amount for key, amount in scope.items())
    prior = next((row for row in workspace.rule_adjustments if same(row)), None)
    others = [row for row in workspace.rule_adjustments if not same(row)]
    if value.mode == "clear":
        require(len(others) < len(workspace.rule_adjustments), "ADJUSTMENT_MISSING", "This rota has no adjustment for that rule")
    else:
        if value.line_id:
            require(not any(row['policy_id'] == policy['id'] and (
                not row.get('line_id') and row['mode'] == 'off' or
                row.get('line_id') == value.line_id and row.get('number') == value.number and
                (value.person_id is None or row.get('person_id') in (None, value.person_id)) and
                row['starts_on'] < scope['ends_on'] and scope['starts_on'] < row['ends_on']) for row in others),
                'ADJUSTMENT_OVERLAP', 'Edit the existing adjustment for this person and period', 409)
        elif value.mode == 'off':
            require(not any(row['policy_id'] == policy['id'] and row.get('line_id') for row in others),
                    'ADJUSTMENT_OVERLAP', 'Clear this rule’s scoped adjustments before pausing the whole rule', 409)
        entry = {'id': prior['id'] if prior and prior.get('id') else command_id(db),
                 "policy_id": policy['id'], 'policy_name': policy['name'], "policy_revision": policy['revision'], "mode": value.mode, "amount": value.amount,
                 **scope, 'reason': value.reason, "by": identity_id, "at": command_time(db).isoformat()}
        require(not weakens_safety(policy, entry), 'SAFETY_RULE',
                'A rota adjustment cannot switch off or weaken a safety rule', 409)
        if value.mode == "set" or value.line_id:
            rule = policy['definition']['rule']
            require(value.line_id is not None or rule['kind'] in ADJUSTABLE or rule['kind'] == 'catalogue' and adjustable(rule),
                    "NOT_ADJUSTABLE", "This rule has no single number to change; edit the department rule instead")
            try:
                adjusted_rules(policy, [*others, entry])
            except ValueError as error:  # pydantic's first message says what is wrong with the number
                detail = error.errors()[0]["msg"] if hasattr(error, "errors") else str(error)
                require(False, "INVALID_ADJUSTMENT", f"That number is not allowed for this rule: {detail}")
        others.append(entry)
    return others


def rule_adjust(db, actor, command):
    from .policies import effective_source
    value = s.RuleAdjustmentInput.model_validate(command.payload)
    workspace = draft(db, actor, command.target_id, command.expected_revision)
    policy = effective_source(db, actor, scoped(db, m.Policy, value.policy_id, actor))
    if value.person_id:
        person_access(db, actor, value.person_id)
    rows = rule_adjustment_rows(workspace, policy, value, actor.identity_id, db=db)
    before = workspace_content(db, workspace)
    workspace.rule_adjustments = rows
    return changed(db, actor, workspace, before, value.reason)


def target_override(db, actor, command):
    value = s.TargetOverrideInput.model_validate(command.payload)
    workspace = draft(db, actor, command.target_id, command.expected_revision)
    policy = scoped(db, m.Policy, value.policy_id, actor)
    require_catalogue_read(actor, 'policies')
    person_access(db, actor, value.person_id)
    existing = workspace.target_overrides
    remaining = [r for r in existing if (r['policy_id'], r['person_id']) != (policy.id, value.person_id)]
    if value.amount is None:
        require(len(remaining) < len(existing), 'TARGET_OVERRIDE_MISSING', 'There is no saved adjustment to remove')
        before = workspace_content(db, workspace)
        workspace.target_overrides = remaining
        return changed(db, actor, workspace, before, value.reason)
    report = command_evaluate(db, domain_snapshot(db, workspace)).get('quota_report') or {}
    source_targets = [row for row in report.get('rows', []) if row['policy_id'] == policy.id
                      and row['comparison'] == 'target' and row.get('target_calculation') is not None]
    require(source_targets,
            'TARGET_ONLY', 'Only configurable rota targets may be adjusted; mandatory limits remain unchanged')
    targets = [row for row in source_targets if row['person_id'] == value.person_id]
    require(targets,
            'TARGET_NOT_APPLICABLE', 'This target does not apply to this employee and rota')
    for row in targets:
        value.amount = s.quota_target_amount(row['metric'], value.amount)
    before = workspace_content(db, workspace)
    previous = next((r for r in existing if (r['policy_id'], r['person_id']) == (policy.id, value.person_id)), {})
    remaining.append({**value.model_dump(mode='json'), 'id': previous.get('id') or command_id(db),
                      'policy_name': targets[0]['policy_name'], 'policy_revision': targets[0]['policy_revision'],
                      'rule_fingerprints': sorted({row['target_calculation']['rule_fingerprint'] for row in targets}),
                      'by': actor.identity_id, 'at': command_time(db).isoformat()})
    require(len(remaining) <= 2000, 'TARGET_LIMIT', 'A rota supports at most 2000 target adjustments')
    workspace.target_overrides = remaining
    return changed(db, actor, workspace, before, value.reason)


def name_attached_supervisors(db, workspace, row, *, shared_inputs=None, explicit_clears=()):
    """Fill missing manual pairings through the same qualification and rule checks as the rota."""
    from packages.domain.conditions import active_rules
    from packages.domain.supervision_levelling import levelled_order, session_spread_policies
    from packages.domain.feasibility import compare_findings
    if not row.type_snapshot['definition'].get('supervision'):
        return
    if shared_inputs is not None:
        # Dated trainee classification reads canonical memberships/categories, not other duties.
        # Only actual missing pairs need the full rota context and mandatory comparison below.
        current = shift_record(db, row)
        if not any(person['person_id'] not in explicit_clears and not person.get('supervisor_person_id')
                   and trainee_intervals(shared_inputs, current, person)
                   for person in current['participants']):
            return
    data = domain_snapshot(db, workspace, shared_inputs=shared_inputs, include_fairness=False,
                           include_objectives=False)
    shift = next(s for s in data['shifts'] if s['id'] == row.id)
    missing = [(p, intervals) for p in shift['participants']
               if p['person_id'] not in explicit_clears and not p.get('supervisor_person_id')
               and (intervals := trainee_intervals(data, shift, p))]
    if not missing:
        return
    budget = {'_check_budget': check} if (check := db.info.get('_check_budget')) else {}
    context = {**data, **budget}
    context['policies'] = active_rules(data['policies'], context=context)
    # Naming a pair changes this session and the trainees' supervisor-grouped limits, never quota amounts.
    trial = {**data, **budget, '_partial': {'shifts': {row.id}, 'people': {p['person_id'] for p, _ in missing}},
             'quota_subjects': set()}
    def pairing_findings():
        result = command_evaluate(db, trial, explain_findings=False)
        return {'findings': [*result['session_findings'], *result['person_findings']]}

    baseline = None
    def pairing_check(participant, pid):
        nonlocal baseline
        if baseline is None:
            baseline = pairing_findings()
        participant['supervisor_person_id'] = pid
        proposed = pairing_findings()
        participant['supervisor_person_id'] = None
        if (not compare_findings(baseline, proposed, protect_intervals=True)['new_or_worse_findings'] and not any(
                f['code'] in {'SUPERVISION_PAIRING', 'ATTACHMENT_PAIRING'} and f.get('shift_id') == row.id
                and f.get('person_id') == participant['person_id'] for f in proposed['findings'])):
            return proposed

    pending = []
    for participant, intervals in missing:
        supervisors = set.intersection(*(
            {p['person_id'] for p in rule_coverage_people(context, shift, lo, hi)
             if qualified_supervisor(context, shift, p, lo, hi)} for lo, hi in intervals))
        attachments = [attached_to(data, participant['person_id'], shift, lo, hi) for lo, hi in intervals]
        attached = {pid: 'required' if any(links[pid] == 'required' for links in attachments) else 'preferred'
                    for pid in supervisors.intersection(*(links.keys() for links in attachments))}
        choices = sorted(attached, key=lambda pid: (attached[pid] != 'required', pid))
        if any(session_spread_policies(
                context['policies'],
                lambda policy, member, duty, lo, hi: policy_applies(context, policy, member, duty, lo, hi),
                participant['person_id'], shift, lo, hi)
               for lo, hi in intervals):
            choices = levelled_order(choices, [p.get('supervisor_person_id') for p in shift['participants']])
        choices = [pid for pid in choices if pairing_check(participant, pid) is not None]
        if choices:
            pending.append((participant, choices, any(attached[pid] == 'required' for pid in choices)))
    # Reserve required and constrained places, then relocate only tentative automatic pairs when needed.
    pending.sort(key=lambda item: (not item[2], len(item[1]), item[0]['person_id']))
    automatic = {p['person_id']: (p, choices) for p, choices, _ in pending}
    # Temporary displacement uses the original check; a complete path must preserve the last accepted check.
    accepted = baseline
    def place(participant, seen_people, seen_supervisors):
        seen_people.add(participant['person_id'])
        choices = [pid for pid in automatic[participant['person_id']][1] if pid not in seen_supervisors]
        for pid in choices:
            if ((proposed := pairing_check(participant, pid)) is not None
                    and not compare_findings(accepted, proposed, protect_intervals=True)['new_or_worse_findings']):
                participant['supervisor_person_id'] = pid
                return proposed
        for pid in choices:
            if pid in seen_supervisors:
                continue
            seen_supervisors.add(pid)
            for other, _ in automatic.values():
                if other['person_id'] in seen_people or other.get('supervisor_person_id') != pid:
                    continue
                other['supervisor_person_id'] = None
                if pairing_check(participant, pid) is not None:
                    participant['supervisor_person_id'] = pid
                    if (proposed := place(other, seen_people, seen_supervisors)) is not None:
                        return proposed
                    participant['supervisor_person_id'] = None
                other['supervisor_person_id'] = pid

    for participant, _, _ in pending:
        if (proposed := place(participant, set(), set())) is not None:
            accepted = proposed
    named = [p for p, _, _ in pending if p.get('supervisor_person_id')]
    for participant in named:
        db.get(m.Assignment, participant['id']).supervisor_person_id = participant['supervisor_person_id']
    if named and (preparation := db.info.get('_planning_preparation')):
        # A preparation snapshot taken to choose pairs predates the saved assignment fields.
        preparation['snapshots'].clear()


def admit_assignment_change(db, actor, workspace, before, code=None, *, shared_inputs=None,
                            override_reason=None, message=None, staffing_gaps_only=False, evaluated=None):
    if code is None and actor is None:
        # Keep the pure comparison helper's historical four-argument form for domain tests.
        db, actor, workspace, before, code = None, None, actor, workspace, before
    contexts = None
    if actor is not None:
        # Dependency and authority checks must reject before this D65 comparison can expose findings.
        from .planning_groups import guard_changes
        authority = guard_changes(db, actor, workspace, before)
        contexts = db.info['_planning_group_authority_contexts']
        contexts.pop(workspace.id, None)
    from packages.domain.validation_scope import assignment_scope, evaluate_assignments
    from packages.domain.proposal_reconciliation import comparison as compare_checks
    from packages.domain.input_manifest import shift_input
    from packages.domain.finding_causes import DRAFT_STAFFING_GAP_CODES, EDIT_NON_REFUSING_CODES
    # Keep all reservation/rule windows as context, but never load scoring or fairness evidence for admission.
    checked, data = evaluated if evaluated is not None else (None, domain_snapshot(
        db, workspace, shared_inputs=shared_inputs, include_fairness=False, include_objectives=False))
    focus = set(data['focus_ids'])
    old = {row['id']: row for row in before['shifts']}
    current = [row for row in data['shifts'] if row['id'] in focus]
    touched = [row for row in current if row['id'] not in old or shift_input(row) != shift_input(old[row['id']])]
    prior = [old[row['id']] for row in touched if row['id'] in old]
    open_places = []
    if staffing_gaps_only:
        if not prior and not any(row['participants'] for row in touched):
            return None
        # Compare staffing a new session with the same session left empty.
        open_places = [{**row, 'participants': []} for row in touched if row['id'] not in old]
    scope = assignment_scope(data, prior, touched)
    if staffing_gaps_only:
        if checked is not None:
            from packages.domain.validation import finding_observations
            gaps = [f for f in finding_observations(checked) if f['code'] in DRAFT_STAFFING_GAP_CODES
                    and f.get('shift_id') in scope['shifts'] and f['severity'] == 'error']
            if not gaps:
                return None
            checked = {**checked, 'findings': gaps}
        else:
            from packages.domain import leadership
            from packages.domain.validation import effective_type_definition
            candidates = [row for row in data['shifts'] if row['id'] in scope['shifts']] + prior
            lead_data = dict(data)
            leadership.prepare(lead_data)
            if not any(effective_type_definition(data, row).get('supervision')
                       or any(p['participation'] == 'trainee' for p in row['participants'])
                       or leadership.requirements(lead_data, row, row['start'], row['end']) for row in candidates):
                return None
    baseline = with_series_state({**data, 'shifts': before['shifts'] + open_places +
        [row for row in data['shifts'] if row['id'] not in focus]})
    def compare():
        before_check = evaluate_assignments(baseline, scope, explain_findings=False)
        after_check = checked if checked is not None else evaluate_assignments(data, scope, explain_findings=False)
        return compare_checks(before_check, after_check), after_check
    preparation = db.info.get('_planning_preparation') if db is not None else None
    computed = compare() if evaluated is not None else command_compute(db, 'edit_admission', data, compare,
        options={'before': [shift_input(row) for row in prior],
                 'scope': {k: sorted(v) if isinstance(v, set) else v for k, v in scope.items()}},
        input_token=data['fingerprint'] if preparation else None,
        defer=bool(preparation and preparation['kind'] == 'shift.update'))
    if computed is None:
        # Only rollback preparation speculates; the writer enforces the captured admission before saving.
        if contexts is not None:
            contexts[workspace.id] = (before, actor.identity_id, workspace.content_revision, snapshots.snapshot_hash(before), authority)
        return None
    comparison, checked = computed
    findings = comparison['new_or_worse_findings']
    if code == 'SWAP_INVALID':
        touched_ids = {row['id'] for row in touched}
        current_calendar = [f for f in checked['findings']
                            if f.get('code') == 'CALENDAR_CLOSED' and f.get('shift_id') in touched_ids]
        known_findings = {(f.get('code'), f.get('shift_id'), f.get('start'), f.get('end')) for f in findings}
        findings.extend(f for f in current_calendar
                        if (f.get('code'), f.get('shift_id'), f.get('start'), f.get('end')) not in known_findings)
    # An open place (a session short of people) stays must-fix work in the Fix list; it never refuses the edit.
    findings = [f for f in findings if f.get('code') not in EDIT_NON_REFUSING_CODES]
    if staffing_gaps_only:
        findings = [f for f in findings if f.get('code') in DRAFT_STAFFING_GAP_CODES]
    if override_reason is not None and len(override_reason.strip()) >= 3:
        findings = [f for f in findings if not assignment_reason_allowed(data, f)]
    require(comparison['comparison_complete'], 'CHECK_INCOMPLETE', 'The edit could not be checked completely', 422)
    refusal_code = ('PERSON_UNAVAILABLE' if code == 'EDIT_INVALID' and findings
                    and all(assignment_reason_allowed(data, finding) for finding in findings) else code)
    # Move and swap have no reason field: keep their own message there.
    refusal_message = ('This edit introduces a conflict that a manual reason cannot override'
                       if refusal_code == 'EDIT_INVALID' else message)
    current_rule = next((f['rule_id'] for f in findings if (f.get('detail') or {}).get('live_zero')), None)
    if current_rule:
        refusal_message = (message or refusal_message) + f' (current department rule {current_rule})'
    # A preview refuses exactly as its save will (F14; Master ruling 10 Nov).
    require(not findings, refusal_code, refusal_message or (findings[0]['message'] if findings else 'The edit introduces a blocking issue'),
            422, refusal_details(findings))
    if contexts is not None:
        contexts[workspace.id] = (before, actor.identity_id, workspace.content_revision, snapshots.snapshot_hash(before), authority)
    return checked, data


def save_shift(db, actor, command, update=False):
    value = (s.ShiftEdit if update else s.ShiftCreate).model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    if db.info.get('_assignment_reply'):
        require(assignment_reply_available(db, command), 'STALE_INPUTS',
                'The session inputs or worker changed; refresh before saving again', 409, {'recoverable': True})
    if not update and 'session_creation' in db.info.get('bulk', {}):
        from .bulk import create_session
        return create_session(db, actor, workspace, value)
    before = workspace_content(db, workspace)
    existing = db.get(m.Shift, value.shift_id) if update else None
    if update:
        require(existing is not None and existing.workspace_id == workspace.id, "NOT_FOUND", "Session not found", 404)
    prior = next((row for row in before['shifts'] if existing and row['id'] == existing.id), {})
    proposed = {**prior, 'id': existing.id if existing else None,
                **value.model_dump(mode='json', include=set(s.ShiftInput.model_fields))}
    previous = {p['person_id']: p for p in prior.get('participants', [])}
    for participant, parsed in zip(proposed['participants'], value.participants):
        if 'fixed' not in parsed.model_fields_set:
            participant['fixed'] = previous.get(parsed.person_id, {}).get('fixed', False)
    require_change_authority(db, actor, workspace, before, after={**before, 'shifts': [
        row for row in before['shifts'] if row['id'] != proposed['id']] + [proposed]})
    inputs = {}
    # The session editor (a new session, or its impact preview) has no reason field; the fill panel does.
    # Both send expected_fingerprint, so it cannot tell them apart.
    refusal = ('Save the session without this person, then use the planner fill panel to assign them with a reason; the conflict still blocks submission'
               if not update or db.info.get('_planner_preview')
               else 'Enter a reason to assign them anyway; the conflict still blocks submission')
    # Run the same synchronous gate for every interactive write; unchanged person/interval
    # assignments are exempted inside assignment_override_people, while changed flags or times
    # must pass the current reason decision before the scoped admission comparison runs.
    override_check = refusal if 'bulk' not in db.info else None
    row = write_shift(db, actor, workspace, value, existing, inputs=inputs,
                      override_refusal=override_check)
    if value.participants:
        # Keep an explicit clear visible to admission even when an attachment could fill it again.
        explicit_clears = {p.person_id for p in value.participants
                           if 'supervisor_person_id' in p.model_fields_set and p.supervisor_person_id is None
                           and previous.get(p.person_id, {}).get('supervisor_person_id')}
        name_attached_supervisors(db, workspace, row, shared_inputs=inputs.get('shared_inputs'),
                                  explicit_clears=explicit_clears)
    admission = None
    if 'bulk' not in db.info and update:
        admission = admit_assignment_change(db, actor, workspace, before, 'EDIT_INVALID',
            shared_inputs=inputs.get('shared_inputs'), override_reason=value.override_reason, message=refusal)
    reason = value.override_reason.strip() if value.override_reason is not None else None
    result = changed(db, actor, workspace, before, reason or ("Edit session" if update else "Create session"),
                     shared_inputs=inputs.get('shared_inputs'), defer_assignments=update,
                     prepared=admission if db.info.get('_assignment_reply') else None,
                     staffed_creation=not update and bool(value.participants) and 'bulk' not in db.info)
    result["shift_id"] = row.id
    return result


def shift_action(db, actor, command):
    value = s.ShiftAction.model_validate(command.payload)
    require(len(value.reason.strip()) >= 3, 'REASON_REQUIRED', 'Enter a meaningful change reason')
    revision = command.expected_revision
    if value.action == 'cancel' and revision is None:
        workspace = scoped(db, m.Workspace, value.workspace_id, actor)
        revision = workspace.revision
    workspace = draft(db, actor, value.workspace_id, revision)
    before = workspace_content(db, workspace)
    selected_ids = set(value.shift_ids)
    if value.action == 'unassign':
        projected = snapshots.project_series_shifts(db, workspace, before['shifts'])
        selected = [row for row in projected if row['id'] in selected_ids]
        require(not any(inactive(row) for row in selected),
                'SESSION_CLOSED', 'Reopen selected sessions before clearing their people', 409)
        require(not any(assignment_fixed(row, participant) for row in selected for participant in row['participants']),
                'ASSIGNMENT_FIXED', 'Unfix selected assignments before clearing their people', 409)
    unavailable = set()
    if value.action == "release_unavailable":
        # The exact absent-person delta is checked again by changed() after the current findings are known.
        require_change_authority(db, actor, workspace, before)
        unavailable = {(f['shift_id'], f['person_id']) for f in command_evaluate(db, domain_snapshot(db, workspace))['findings']
                       if f['code'] in {'UNAVAILABLE', 'EXTERNAL_ROTATION'}}
        require(any(sid in value.shift_ids for sid, _ in unavailable), 'NO_UNAVAILABLE_ASSIGNMENTS',
                'No recorded absence or external rotation affects the selected sessions', 409)
    rows = selected_shifts(db, workspace, value.shift_ids)
    require(value.action == 'unlock' or not any(row.locked for row in rows.values()),
            'ASSIGNMENT_LOCKED', 'Unlock before changing a locked session', 409)
    if value.action == 'remove':
        remove_shifts(db, rows)
        return changed(db, actor, workspace, before, value.reason)
    released = {sid for sid, _ in unavailable if sid in rows}
    if value.action == 'release_unavailable':
        require(not any(row.cancelled for row in rows.values()), 'SESSION_CLOSED', 'Reopen the session before finding cover', 409)
        # An explicit reviewed action releases absent people, never healthy fixed work.
        remove_participants(db, [(sid, pid) for sid, pid in unavailable if sid in rows])
    elif value.action == 'unassign':
        require(not any(row.cancelled for row in rows.values()), 'SESSION_CLOSED', 'Reopen the session before clearing its people', 409)
        clear_shift_participants(db, rows)
    types = {}
    if value.action == 'refresh_types':
        prime_shift_batch(db, actor, types, ((m.AssignmentType, {row.assignment_type_id for row in rows.values()}),))
    for sid, row in rows.items():
        if value.action in {"cancel", "reopen"}:
            row.cancelled = value.action == "cancel"
            row.cancellation_reason = value.reason if row.cancelled else None
        elif value.action == "release_unavailable":
            if sid not in released:
                continue
        elif value.action in {"lock", "unlock"}:
            row.locked = value.action == "lock"
        elif value.action == "refresh_types":
            typ = types['inputs'][m.AssignmentType, row.assignment_type_id]
            s.validate_attributes(s.TypeDefinition.model_validate(typ.definition), row.attributes)
            row.type_revision, row.type_snapshot = typ.revision, record(typ)
        # Refreshing a duty-type snapshot is metadata reconciliation, not an
        # occurrence edit. Keep recurring sessions attached to their series.
        if row.series_id and value.action not in {"lock", "unlock", "refresh_types"}:
            row.series_modified = True
        row.revision += 1
    if value.action in {'unassign', 'release_unavailable'}:
        code = 'UNASSIGN_INVALID' if value.action == 'unassign' else 'RELEASE_UNAVAILABLE_INVALID'
        admit_assignment_change(db, actor, workspace, before, code, staffing_gaps_only=True)
    return changed(db, actor, workspace, before, value.reason)


def copy_shifts(db, actor, command):
    value = s.ShiftCopy.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    before = workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, before, session_definitions=True)
    source = {"shifts": [r for r in before["shifts"] if r["id"] in value.shift_ids]}
    require(len(source["shifts"]) == len(set(value.shift_ids)), "NOT_FOUND", "Some selected sessions were not found", 404)
    # Deliberate elapsed-day copy; recurrence is the local-wall-time alternative.
    copy_snapshot_into(db, actor, workspace, source, offset=timedelta(days=value.offset_days), detach_series=True)
    return changed(db, actor, workspace, before, value.reason)


def replace_participants(db, shift_ids, replacements):
    from packages.domain.participation import replaced_participants
    rows = list(db.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_(shift_ids))))
    for shift_id in shift_ids:
        assignments = {row.id: row for row in rows if row.shift_id == shift_id}
        for value in replaced_participants([record(row) for row in assignments.values()], replacements):
            row = assignments[value['id']]
            if row.supervisor_person_id != value['supervisor_person_id']:
                row.supervisor_person_id = value['supervisor_person_id']
                row.revision += 1
            row.person_id = value['person_id']


def refusal_details(findings):
    """The planner needs the clash, never the whole score, coverage or diagnostic graph."""
    return {'findings': [{key: row[key] for key in (
        'code', 'message', 'severity', 'person_id', 'shift_id', 'start', 'end',
        'record_id', 'availability_id', 'rotation_id', 'rule_id') if key in row}
        for row in findings if row.get('severity') == 'error']}


def swap(db, actor, command):
    value = s.DraftSwap.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    before = workspace_content(db, workspace)
    one, two = db.get(m.Assignment, value.first_assignment_id), db.get(m.Assignment, value.second_assignment_id)
    require(one is not None and two is not None, "NOT_FOUND", "Assignments not found", 404)
    a, b = db.get(m.Shift, one.shift_id), db.get(m.Shift, two.shift_id)
    require(a.workspace_id == workspace.id and b.workspace_id == workspace.id and a.id != b.id,
            "INVALID_SWAP", "Select two different sessions in the same draft")
    require(not a.locked and not b.locked, "ASSIGNMENT_LOCKED", "Both sessions must be unlocked", 409)
    require(one.person_id != two.person_id, "INVALID_SWAP", "Select two different people")
    other_a = set(db.scalars(select(m.Assignment.person_id).where(m.Assignment.shift_id == a.id)))
    other_b = set(db.scalars(select(m.Assignment.person_id).where(m.Assignment.shift_id == b.id)))
    require(two.person_id not in other_a and one.person_id not in other_b, "DUPLICATE_PARTICIPATION", "A swap cannot duplicate a participant")
    replace_participants(db, [a.id, b.id], {one.id: two.person_id, two.id: one.person_id})
    for participant in (one, two):
        for key, field in assignment_source(actor, 'swap').items():
            setattr(participant, key, field)
    one.revision += 1
    two.revision += 1
    for row in (a, b):
        if row.series_id:
            row.series_modified = True
    admit_assignment_change(db, actor, workspace, before, 'SWAP_INVALID', message='The complete swap failed validation; neither side was saved')
    result = changed(db, actor, workspace, before, value.reason)
    return result


def target_deficit(check, shift_id):
    """An open place on this session, including scoped/group cover observed by the shared validator."""
    from packages.domain.coverage import session_open_places
    return session_open_places(check, shift_id)


def move(db, actor, command):
    """Move one unlocked draft placement, preserving existing problems and exposing its vacated place."""
    from packages.domain.feasibility import compare_findings
    from packages.domain.finding_causes import COVERAGE_CODES, EDIT_NON_REFUSING_CODES
    from packages.domain.recurrence import series_candidate_allowed

    value = s.DraftMove.model_validate(command.payload)
    require(len(value.reason.strip()) >= 3, 'REASON_REQUIRED', 'Enter a meaningful change reason')
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    assignment = db.get(m.Assignment, value.assignment_id)
    require(assignment is not None, 'NOT_FOUND', 'Assignment not found', 404)
    source, target = db.get(m.Shift, assignment.shift_id), db.get(m.Shift, value.target_shift_id)
    require(source is not None and target is not None and source.workspace_id == workspace.id
            and target.workspace_id == workspace.id and source.id != target.id,
            'INVALID_MOVE', 'Choose two different sessions in the same draft')
    require(not source.locked and not target.locked, 'ASSIGNMENT_LOCKED', 'Both sessions must be unlocked', 409)
    before = workspace_content(db, workspace)
    proposed = copy.deepcopy(before)
    shifts = {row['id']: row for row in proposed['shifts']}
    participant = next(p for p in shifts[source.id]['participants'] if p['id'] == assignment.id)
    shifts[source.id]['participants'].remove(participant)
    participant.update(start=shifts[target.id]['start'], end=shifts[target.id]['end'], segments=[],
                       supervisor_person_id=None)
    shifts[target.id]['participants'].append(participant)
    require_change_authority(db, actor, workspace, before, after=proposed)
    person_access(db, actor, assignment.person_id)
    data = domain_snapshot(db, workspace)
    shifts = {row['id']: row for row in data['shifts'] if row['id'] in data['focus_ids']}
    origin, destination = shifts[source.id], shifts[target.id]
    require(not inactive(origin) and not inactive(destination), 'SESSION_CLOSED',
            'Reopen both sessions before moving a person', 409)
    seat = next(row for row in origin['participants'] if row['id'] == assignment.id)
    require(not assignment_fixed(origin, seat), 'ASSIGNMENT_FIXED', 'Unfix this assignment before moving it', 409)
    require(not any(row['person_id'] == assignment.person_id for row in destination['participants']),
            'DUPLICATE_PARTICIPATION', 'This person is already on the target session')
    require(series_candidate_allowed(data, destination, assignment.person_id), 'USUAL_ASSIGNEES_FIXED',
            'This recurring session keeps its configured usual people', 409)
    baseline = command_evaluate(db, data)
    open_places = target_deficit(baseline, target.id)
    require(open_places > 0, 'NO_OPEN_PLACE', 'The target session has no open place', 409)
    participant = {key: seat[key] for key in s.ParticipantInput.model_fields if key in seat}
    participant.update(start=destination['start'], end=destination['end'], segments=[], fixed=False, supervisor_person_id=None)
    parsed = s.ShiftInput.model_validate({**{key: destination[key] for key in (
        'assignment_type_id', 'location_id', 'service_ids', 'attributes', 'start', 'end')},
        'participants': [{key: row[key] for key in s.ParticipantInput.model_fields if key in row}
                         for row in destination['participants']] + [participant]}).participants[-1]
    assignment.shift_id = target.id
    for key, field in parsed.model_dump().items():
        setattr(assignment, key, field)
    for key, field in assignment_source(actor, 'manual').items():
        setattr(assignment, key, field)
    assignment.revision += 1
    for row in (source, target):
        row.revision += 1
        if row.series_id:
            row.series_modified = True
    name_attached_supervisors(db, workspace, target)
    admit_assignment_change(db, actor, workspace, before, 'MOVE_INVALID', message='The move introduces a mandatory problem; no assignment was moved')
    result = changed(db, actor, workspace, before, value.reason)
    # Compare validator evidence; the reply also contains actor-specific resolution actions.
    introduced = compare_findings(baseline, workspace.validation, protect_intervals=True)['new_or_worse_findings']
    # Moving leaves its source open, while new skill, lead, supervision and other mandatory problems still block it.
    require(not any(row['code'] not in EDIT_NON_REFUSING_CODES
                    or row['code'] in COVERAGE_CODES and row.get('shift_id') != source.id
                    for row in introduced),
            'MOVE_INVALID', 'The move introduces a mandatory problem; no assignment was moved', 422, refusal_details(introduced))
    remaining = target_deficit(workspace.validation, target.id)
    require(remaining < open_places, 'MOVE_DOES_NOT_FILL',
            'This person does not fill the target open place; no assignment was moved', 422, refusal_details(result['validation']['findings']))
    return result


def latest_change(workspace, *columns):
    """The edit Undo puts back (or, after an Undo, the step Redo re-applies): the newest at the rota's content revision."""
    return (select(*columns).where(m.ChangeSet.workspace_id == workspace.id,
                                   m.ChangeSet.resulting_content_revision == workspace.content_revision)
            .order_by(m.ChangeSet.before_snapshot['workspace']['revision'].as_integer().desc()).limit(1))


def put_back(db, actor, workspace, snapshot, current):
    """Make the draft what `snapshot` recorded (Undo and Redo). Only the sessions that differ are rewritten (proportional
    to the edit); returns whether the content changed."""
    before_shifts = {r['id']: r for r in snapshot['shifts']}
    same = {r['id'] for r in current['shifts'] if before_shifts.get(r['id']) == r}
    changed_ids = [r['id'] for r in current['shifts'] if r['id'] not in same]
    for chunk in chunks(changed_ids):
        db.execute(delete(m.Assignment).where(m.Assignment.shift_id.in_(chunk)))
        db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id.in_(chunk)))
        db.execute(delete(m.Shift).where(m.Shift.id.in_(chunk)))
    db.flush()
    prior = snapshot['workspace']
    workspace.target_overrides = prior.get('target_overrides', [])
    workspace.rule_adjustments = prior.get('rule_adjustments', [])
    workspace.accepted_issues = prior.get('accepted_issues', [])
    workspace.starts_on = date.fromisoformat(str(prior['starts_on']))  # a placement beside a published rota is undone too
    workspace.replaces_publication_id = prior.get('replaces_publication_id')
    restored = record(workspace)
    copy_snapshot_into(db, actor, workspace, {'shifts': [r for i, r in before_shifts.items() if i not in same]}, preserve_ids=True)
    return (len(same) != len(before_shifts) or len(same) != len(current['shifts']) or any(
        current['workspace'][key] != restored[key]
        for key in ('target_overrides', 'rule_adjustments', 'starts_on', 'replaces_publication_id')))


def undo(db, actor, command):
    value = s.Decision.model_validate(command.payload)
    workspace = draft(db, actor, command.target_id, command.expected_revision)
    change = db.scalar(latest_change(workspace, m.ChangeSet))
    require(change is not None and not change.undone, "NO_UNDO", "No immediately preceding edit is available to undo", 409)
    current = workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, current, after=change.before_snapshot)
    content_changed = put_back(db, actor, workspace, change.before_snapshot, current)
    change.undone = True
    unapplied = []
    # Undoing an applied auto-schedule proposal: it is no longer applied (the panel then offers a fresh run).
    if content_changed:
        receipt_column, raw_revision, current_revision = job_reconciliation_columns()
        for job, receipt, revision in db.execute(select(m.Job, receipt_column, raw_revision).options(
                defer(m.Job.input_snapshot), defer(m.Job.result)).where(
                m.Job.workspace_id == workspace.id, m.Job.status == 'applied',
                current_revision == change.resulting_content_revision)):
            result = job_result_with_reconciliation(job, {'reconciliation': receipt,
                                                          'applied_content_revision': revision})
            if result['applied_content_revision'] == change.resulting_content_revision:
                job.status = 'succeeded'
                if result['reconciliation']:
                    retain_job_reconciliation(job, {**result['reconciliation'], 'status': 'undone'})
                job.progress = job_progress_record(job.progress)
                job.revision += 1
                unapplied.append(job.id)
    # Undo is one step: the undo itself is not undoable (Undo again is refused). Its change set keeps the arrangement it
    # replaced, marked as an Undo, so the same person can Redo it until the rota changes again (redo below, SW3-A-06).
    current = {**current, '_undo_of': change.id, '_unapplied_job_ids': unapplied}
    return changed(db, actor, workspace, current, value.reason, content_changed=content_changed, undoable=False,
                   defer_assignments=bool(db.info.get('_assignment_reply')))


def redo_available(db, actor, workspace):
    """Redo applies right after this person's own Undo, while the rota's content is still what that Undo left."""
    if workspace.status != 'draft' or workspace.archived_at is not None:
        return False
    row = db.execute(latest_change(workspace, m.ChangeSet.actor_id,
                                   m.ChangeSet.before_snapshot['_undo_of'].as_string())).first()
    return row is not None and row[1] is not None and row[0] == actor.identity_id


def require_redo_placements(db, actor, workspace, snapshot, current):
    """Redo places people again: the same absence, rotation, restriction and zero-limit check as a forward edit
    (write_shift). A placement it now blocks must carry the reason recorded with it, else nothing is redone."""
    now = {r['id']: r for r in current['shifts']}
    rows = [r for r in snapshot['shifts'] if now.get(r['id']) != r and not r.get('cancelled') and r['participants']]
    previous, kept, services = {}, {}, {}
    # One bounded query per kind for the whole redo, never one per session (sessions, their clinics, their people).
    for chunk in chunks([r['id'] for r in rows if r['id'] in now]):
        for a in db.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_(chunk))):
            previous.setdefault(a.shift_id, {})[a.person_id] = a
        kept.update((row.id, row) for row in db.scalars(select(m.Shift).where(m.Shift.id.in_(chunk))))
        for shift_id, service_id in db.execute(select(m.ShiftService.shift_id, m.ShiftService.service_id)
                                               .where(m.ShiftService.shift_id.in_(chunk))):
            services.setdefault(shift_id, set()).add(service_id)
    known = {'organisation': db.get(m.Organisation, actor.organisation_id), 'shift_services': services,
             'zero_limit_trials': []}
    reasons, blocked, owners = {}, set(), []
    for row in rows:
        typ = db.get(m.AssignmentType, row['assignment_type_id'])
        known.setdefault(('snapshot', typ.id), record(typ))
        value = s.ShiftInput.model_validate({**{key: row[key] for key in (
            'assignment_type_id', 'location_id', 'service_ids', 'attributes', 'start', 'end')},
            'participants': [{key: p[key] for key in s.ParticipantInput.model_fields if key in p} for p in row['participants']]})
        reasons[row['id']] = {p['person_id']: p.get('override_reason') for p in row['participants']}
        row_blocked = assignment_override_people(db, workspace, value, previous.get(row['id'], {}),
                                                 kept.get(row['id']), typ, known)
        allowed = known.get('override_allowed_people', set())
        blocked |= {pid for pid in row_blocked
                    if not reasons[row['id']].get(pid) or pid not in allowed}
        owners += [row['id']] * (len(known['zero_limit_trials']) - len(owners))
    for owner, checked in zip(owners, zero_limit_trial_results(db, known['zero_limit_trials'])):
        blocked |= {f['person_id'] for f in checked['person_findings']
                    if f['severity'] == 'error' and f['code'] in ZERO_LIMIT_FINDING_CODES
                    and (not reasons[owner].get(f['person_id'])
                         or not assignment_reason_allowed(known['assignment_inputs'], f))}
    if blocked:
        names = sorted(db.scalars(select(m.Person.display_name).where(m.Person.id.in_(blocked))))
        require(False, 'REDO_BLOCKED', f"Nothing was redone: {', '.join(names)} can no longer be placed there (leave, rotation "
                "or a restriction changed since). Make the edit again, with a reason if it must stand.", 409,
                {'person_ids': sorted(blocked)})


def redo(db, actor, command):
    """Redo is the inverse of the latest Undo (SW3-A-06): the same person, only while nothing changed the rota since (any
    edit moves its content revision, so a new edit ends Redo). It re-runs the forward edit's placement check and the
    whole rota check, and is itself an edit: audited, and undoable again."""
    value = s.Decision.model_validate(command.payload)
    workspace = draft(db, actor, command.target_id, command.expected_revision)
    step = db.scalar(latest_change(workspace, m.ChangeSet))
    require(step is not None and step.before_snapshot.get('_undo_of') and step.actor_id == actor.identity_id, 'NO_REDO',
            'Nothing to redo: Redo follows your own Undo, until the rota changes again', 409)
    current = workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, current, after=step.before_snapshot)
    require_redo_placements(db, actor, workspace, step.before_snapshot, current)
    content_changed = put_back(db, actor, workspace, step.before_snapshot, current)
    # An auto-schedule proposal its Undo took off the draft is applied again, at the content Redo just made.
    receipt_column, _, _ = job_reconciliation_columns()
    revision = workspace.content_revision + int(content_changed)
    for job, receipt in db.execute(select(m.Job, receipt_column).options(defer(m.Job.input_snapshot),
            defer(m.Job.result)).where(m.Job.id.in_(step.before_snapshot.get('_unapplied_job_ids') or []),
                                      m.Job.workspace_id == workspace.id, m.Job.status == 'succeeded')):
        receipt = job_result_with_reconciliation(job, {'reconciliation': receipt})['reconciliation']
        job.status = 'applied'
        retain_job_reconciliation(job, {**receipt, 'status': 'applied', 'applied_content_revision': revision}
                                  if receipt else None, applied_revision=revision)
        job.progress = job_progress_record(job.progress)
        job.revision += 1
    return changed(db, actor, workspace, current, value.reason, content_changed=content_changed,
                   defer_assignments=bool(db.info.get('_assignment_reply')))


def series_participants(definition, occurrence):
    return [s.ParticipantInput(person_id=pid, start=occurrence["start"], end=occurrence["end"],
                               fixed=definition.usual_assignees_fixed)
            for pid in definition.default_person_ids]


def fixed_series_participants(definition, occurrence, snapshot, existing_person_ids=()):
    """Keep existing placements; only force available usual people into an active occurrence."""
    from packages.domain.validation import index_person_rows
    if '_person_rows' not in snapshot:
        snapshot['_person_rows'] = index_person_rows(snapshot)
    state = series_state(snapshot, {**occurrence,
        'series_snapshot': {'definition': definition.model_dump()}})
    # Closed occurrences retain their assignments as evidence and never enter allocation.
    absent = {} if state['closed'] else {row['person_id']: row for row in state['usual_assignee_unavailability']}
    participants, skipped = [], []
    for participant in series_participants(definition, occurrence):
        pid = participant.person_id
        if pid in existing_person_ids:
            continue
        if pid not in absent:
            participants.append(participant)
            continue
        code = 'EXTERNAL_ROTATION' if 'EXTERNAL_ROTATION' in absent[pid]['codes'] else 'UNAVAILABLE'
        message = 'Away on rotation' if code == 'EXTERNAL_ROTATION' else 'Approved or recorded unavailability'
        skipped.append({'person_id': pid, 'code': code, 'message': message,
                        'start': occurrence['start'].isoformat(), 'end': occurrence['end'].isoformat()})
    return participants, skipped


def series_occurrences(snapshot, series, definition, start, end):
    """Date-set openings filter templates through the same calendar evaluator as validation."""
    if start >= end:
        return []
    owners = [snapshot['services'][sid] for sid in definition.service_ids]
    owners += [snapshot['locations'][lid] for lid in ancestor_ids(snapshot, series.location_id)]
    calendar_ids = {owner['calendar_id'] for owner in owners if owner.get('calendar_id')}
    opening_calendars = snapshot.get('operating_calendars', {})
    calendars = [opening_calendars[cid] for cid in calendar_ids
                 if cid in opening_calendars and opening_calendars[cid]['definition'].get('date_set_rules')]
    type_definition = snapshot['types'][series.assignment_type_id]['definition']

    def closed(occurrence):
        intervals = [(calendar, occurrence['start'], occurrence['end']) for calendar in calendars]
        for demand, a, _, b in resource_intervals(occurrence, type_definition):
            pool = snapshot['resource_pools'][demand['resource_pool_id']]
            if a < b and pool.get('calendar_id') in opening_calendars:
                intervals.append((opening_calendars[pool['calendar_id']], a, b))
        return any(date_set_closes(calendar, a, b, snapshot.get('date_sets'), data=snapshot)
                   for calendar, a, b in intervals)

    try:
        snapshot = {**snapshot, '_date_set_cache': prepare_date_sets(snapshot)}
        return [occurrence for occurrence in expand(definition, start, end) if not closed(occurrence)]
    except DateSetRangeError as error:
        require(False, 'DATE_SET_RANGE', f'Cannot expand recurring template: {error}')
    except ValueError as error:
        require(False, 'SERIES_TIME_UNRESOLVED', f'Cannot expand recurring template: {error}')


def materialise_occurrence(db, actor, workspace, series, definition, occurrence, snapshot, batch=None):
    from packages.domain.validation import rotation_blocks, index_person_rows, person_rows
    if '_template_bookings' not in snapshot:
        snapshot['_person_rows'] = index_person_rows(snapshot)
        snapshot['_template_bookings'] = {}
        zone = snapshot['organisation']['timezone']
        lo, hi = local_midnight(workspace.starts_on, zone), local_midnight(workspace.ends_on, zone)
        for shift in snapshot['shifts']:
            if not inactive(shift):
                for p in shift['participants']:
                    start, end = aware(p['start']), aware(p['end'])
                    # Every saved occurrence lies within this draft; neighbouring history cannot overlap it.
                    # Keep overnight bookings crossing either boundary. The full validator still reads all history.
                    if start < hi and lo < end:
                        snapshot['_template_bookings'].setdefault(p['person_id'], []).append((shift['id'], start, end))
    a, b = occurrence['start'], occurrence['end']
    participants = []
    usual, skipped = (fixed_series_participants(definition, occurrence, snapshot)
                      if definition.usual_assignees_fixed else (series_participants(definition, occurrence), []))
    busy = set()
    if usual and not definition.usual_assignees_fixed:
        from packages.domain.overlap import evaluate_overlaps
        candidate = {'id': 'pending-series-occurrence', **occurrence}
        bookings = {p.person_id: [(candidate, None, a, b),
            *(({'id': sid}, None, lo, hi) for sid, lo, hi in snapshot['_template_bookings'].get(p.person_id, []))]
            for p in usual}
        busy = {row['person_id'] for row in evaluate_overlaps(snapshot, focus_ids={candidate['id']},
                include_unavailability=False, commitments=bookings)}
    for participant in usual:
        pid = participant.person_id
        if definition.usual_assignees_fixed:
            participants.append(participant)
            continue
        if rotation_blocks(snapshot, pid, a, b):
            code, message = 'EXTERNAL_ROTATION', 'Away on rotation'
        elif any(r['person_id'] == pid and overlaps(a, b, aware(r['start']), aware(r['end']))
                 for r in person_rows(snapshot, 'availability', pid)):
            code, message = 'UNAVAILABLE', 'Approved or recorded unavailability'
        elif pid in busy:
            code, message = 'TIME_OVERLAP', 'Already booked at this time'
        else:
            participants.append(participant)
            continue
        skipped.append({'person_id': pid, 'code': code, 'message': message,
                        'start': a.isoformat(), 'end': b.isoformat()})
    row = write_shift(db, actor, workspace, s.ShiftInput(assignment_type_id=series.assignment_type_id,
        location_id=series.location_id, service_ids=definition.service_ids, attributes=definition.attributes,
        participants=participants, **occurrence), series_id=series.id, batch=batch, source='recurring')
    row.template_skipped = skipped
    series_snapshots = batch.setdefault('series_snapshots', {}) if batch is not None else {}
    key = series.id, series.revision
    if key not in series_snapshots:
        series_snapshots[key] = record(series)
    row.series_snapshot = series_snapshots[key]
    if batch is None:
        snapshot['shifts'].append(shift_record(db, row))
    for p in participants:
        snapshot['_template_bookings'].setdefault(p.person_id, []).append((row.id, p.start, p.end))
    return row


def expand_series(db, actor, command):
    value = s.SeriesExpand.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    held = snapshots.take_precondition(db, workspace)
    before = held.get('content') or workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, before, session_definitions=True)
    count = 0
    organisation = db.get(m.Organisation, actor.organisation_id)
    shared_inputs = held.get('inputs')
    if shared_inputs is None:
        shared_inputs = snapshots.prepared_snapshot_inputs(db, organisation)
    template_data = held.get('data')
    if template_data is None:
        template_data = domain_snapshot(db, workspace, shared_inputs=shared_inputs)
    skipped_people, batch = [], {'inputs': {'organisation': organisation}}
    series_rows = selected_series(db, actor, value.series_ids or [value.series_id])
    prime_series_batch(db, actor, batch, series_rows)
    previous_rows = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace.id,
        m.Shift.series_id.in_([r.id for r in series_rows]))))
    for series in series_rows:
        require(series.active, "SERIES_INACTIVE", "This recurring series is inactive")
        start, end = max(workspace.starts_on, series.starts_on), min(workspace.ends_on, series.ends_on or workspace.ends_on)
        require(start < end, "NO_OCCURRENCES", "The series and workspace do not intersect")
        definition = stored_series_definition(series.definition)
        previous = [r for r in previous_rows if r.series_id == series.id]
        require(all(r.occurrence_on is not None and r.series_revision is not None for r in previous),
                "SERIES_LINEAGE_UNKNOWN", "Legacy instances have unknown template provenance. Edit them individually; do not infer occurrence identities.", 409)
        require(all(r.series_revision == series.revision for r in previous),
                "SERIES_CHANGED", "The template changed. Use the reviewed following-occurrence update instead of duplicating old instances.", 409)
        existing = {r.occurrence_on for r in previous}
        occurrences = series_occurrences(template_data, series, definition, start, end)
        for occurrence in occurrences:
            day = occurrence["start"].astimezone(ZoneInfo(definition.timezone)).date()
            if day in existing:
                continue
            row = materialise_occurrence(db, actor, workspace, series, definition, occurrence, template_data, batch=batch)
            row.series_revision, row.occurrence_on = series.revision, day
            skipped_people.extend({'shift_id': row.id, **r} for r in row.template_skipped)
            count += 1
    flush_shift_batch(db, batch)
    require(count > 0, "NO_NEW_OCCURRENCES", "No new occurrences; existing series instances were not duplicated", 409)
    # Only this draft's sessions changed; reuse its catalogues and published rows within the same write lock.
    result = changed(db, actor, workspace, before, "Materialise reviewed recurring series",
                     external_snapshot=template_data, shared_inputs=shared_inputs)
    result["created_count"] = count
    result['template_skipped'] = skipped_people
    return result


def reconcile_series(db, actor, command):
    """Replace a bounded future logical-date slice, atomically, on explicit review.

    Occurrence identity is the stored series-local date, not an inferred start
    instant. Earlier dates and historical publications are never rewritten.
    """
    value = s.SeriesReconcile.model_validate(command.payload)
    workspace = draft(db, actor, value.workspace_id, command.expected_revision)
    series = scoped(db, m.RecurringSeries, value.series_id, actor)
    expected(series, value.expected_series_revision)
    require(workspace.starts_on <= value.from_on < workspace.ends_on, "OUTSIDE_WORKSPACE",
            "The following-occurrence date must lie within this workspace")
    before = workspace_content(db, workspace)
    require_change_authority(db, actor, workspace, before, session_definitions=True)
    previous = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace.id,
        m.Shift.series_id == series.id).order_by(m.Shift.start, m.Shift.id)))
    require(all(r.occurrence_on is not None and r.series_revision is not None for r in previous),
            "SERIES_LINEAGE_UNKNOWN", "Legacy instances have unknown template provenance. Edit them individually; do not infer occurrence identities.", 409)
    selected = {r.occurrence_on: r for r in previous if r.occurrence_on >= value.from_on}
    require(not any(r.locked for r in selected.values()), "ASSIGNMENT_LOCKED",
            "Following-occurrence updates never change locked sessions. Unlock them explicitly or choose a later boundary.", 409)
    old_records = {r["id"]: r for r in before["shifts"]}
    touched = [r for r in selected.values() if r.series_modified or r.cancelled or old_records[r.id]["participants"]]
    require(not touched or value.replace_modified, "SERIES_MODIFIED",
            "This slice contains manual changes, cancellations or assigned people. Explicitly acknowledge replacement before applying.", 409)
    definition = stored_series_definition(series.definition)
    start, end = max(value.from_on, series.starts_on), min(workspace.ends_on, series.ends_on or workspace.ends_on)
    template_data = domain_snapshot(db, workspace)
    occurrences = series_occurrences(template_data, series, definition, start, end) if series.active else []
    planned = {r["start"].astimezone(ZoneInfo(definition.timezone)).date(): r for r in occurrences}
    # Refuse ambiguity even if a future recurrence definition permits >1 per day.
    require(len(planned) == len(occurrences), "SERIES_AMBIGUOUS", "An occurrence date must have one unambiguous session", 409)
    created = updated = removed = 0
    for day, row in selected.items():
        if day not in planned:
            db.execute(delete(m.Assignment).where(m.Assignment.shift_id == row.id))
            db.execute(delete(m.ShiftService).where(m.ShiftService.shift_id == row.id))
            db.delete(row)
            removed += 1
    db.flush()
    template_data, skipped_people = domain_snapshot(db, workspace), []
    for day, occurrence in sorted(planned.items()):
        row = selected.get(day)
        if row is None:
            row = materialise_occurrence(db, actor, workspace, series, definition, occurrence, template_data)
            row.series_revision, row.occurrence_on = series.revision, day
            skipped_people.extend({'shift_id': row.id, **r} for r in row.template_skipped)
            created += 1
            continue
        participants = []
        if row and value.participant_policy == "preserve_offsets":
            delta = occurrence["start"] - aware(row.start)
            for person in old_records[row.id]["participants"]:
                a, b = aware(person["start"]) + delta, aware(person["end"]) + delta
                require(occurrence["start"] <= a < b <= occurrence["end"], "SERIES_PARTICIPANT_OUTSIDE",
                        "A retained participation interval will not fit the new session. Edit it individually or explicitly clear people; no interval was truncated.", 409)
                participants.append(s.ParticipantInput(person_id=person["person_id"], start=a, end=b,
                    participation=person["participation"], overtime=person["overtime"],
                    fixed=person.get('fixed', False),
                    supervisor_person_id=person.get('supervisor_person_id'), team_lead=person.get('team_lead', False),
                    segments=shifted_segments(person, delta)))
        if definition.usual_assignees_fixed:
            usual = set(definition.default_person_ids)
            participants = [p.model_copy(update={'fixed': True}) if p.person_id in usual else p for p in participants]
            present = {p.person_id for p in participants}
            forced, skipped = fixed_series_participants(definition, occurrence, template_data, present)
            participants.extend(forced)
        data = s.ShiftInput(assignment_type_id=series.assignment_type_id, location_id=series.location_id,
            service_ids=definition.service_ids, attributes=definition.attributes, participants=participants, **occurrence)
        row = write_shift(db, actor, workspace, data, existing=row, series_id=series.id, preserve_source=True)
        row.series_revision, row.occurrence_on = series.revision, day
        row.series_snapshot = record(series)
        if definition.usual_assignees_fixed:
            row.template_skipped = skipped
            skipped_people.extend({'shift_id': row.id, **r} for r in skipped)
        # New template timing does not erase the provenance of retained manual assignments.
        row.series_modified = bool(old_records[row.id].get('series_modified') and participants
                                   and value.participant_policy == 'preserve_offsets')
        row.cancelled = False
        row.cancellation_reason = None
        template_data['shifts'] = [r for r in template_data['shifts'] if r['id'] != row.id]
        current = shift_record(db, row)
        template_data['shifts'].append(current)
        if '_template_bookings' in template_data:
            bookings = template_data['_template_bookings']
            for pid in {p['person_id'] for p in [*old_records[row.id]['participants'], *current['participants']]}:
                bookings[pid] = [r for r in bookings.get(pid, []) if r[0] != row.id]
            for p in current['participants']:
                bookings.setdefault(p['person_id'], []).append((row.id, aware(p['start']), aware(p['end'])))
        updated += 1
    require(created or updated or removed, "NO_OCCURRENCES", "There are no existing or proposed occurrences in this slice", 409)
    result = changed(db, actor, workspace, before, value.reason)
    result.update({"created_count": created, "updated_count": updated, "removed_count": removed,
                   "preserved_count": len(previous) - len(selected), "series_revision": series.revision,
                   'template_skipped': skipped_people})
    return result


def validation_workspace(db, actor, command, *, check_revision=True):
    require_role(actor, 'coordinator')
    workspace = scoped(db, m.Workspace, command.target_id, actor)
    if check_revision:
        expected(workspace, command.expected_revision)
    s.WorkspaceValidation.model_validate(command.payload)
    require(workspace.archived_at is None, 'WORKFLOW_STATE', 'This draft was discarded; restore it first', 409)
    require(workspace.status not in {'published', 'superseded'}, 'WORKFLOW_STATE', 'Use operational coverage for a published rota', 409)
    return workspace


def validation_stamp(db, workspace):
    """Bind this request's repeatable read to its short writer, never to a cached check.

    Scheduling/configuration and published-context writers bump the organisation's input
    revisions; local edits bump the workspace revision. A changed stamp is resolved from
    canonical inputs outside the lock before any result can be stored.
    """
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    return (organisation.revision, organisation.input_revision, organisation.availability_revision,
            department.revision, department.catalogue_revision, workspace.revision,
            workspace.content_revision, workspace.status, workspace.archived_at)


def _publication_approval(db, workspace, fp):
    """Check the recorded approval after the final publication validation."""
    if workspace.review_snapshot:
        reviews.require_complete(db, workspace, current_fingerprint=fp)
        return
    approval = db.scalar(select(m.Approval).where(
        m.Approval.workspace_id == workspace.id,
        m.Approval.content_revision == workspace.content_revision,
        m.Approval.fingerprint == fp,
    ).order_by(m.Approval.created_at.desc(), m.Approval.seq.desc()))
    require(approval is not None, 'STALE_APPROVAL', reviews.STALE_APPROVAL_MESSAGE, 409)
    approver_identity = db.get(m.Identity, approval.approved_by)
    require(not approver_identity.disabled and 'approver' in current_roles(
        db, approval.approved_by, workspace.department_id),
        'APPROVER_REVOKED', 'The approving authority is no longer active; obtain a new review', 409)


def review_gate_check(db, workspace, *, reuse=False):
    """Check current mandatory rules and retain the frozen basis for review and publication evidence."""
    current = []
    result = revalidate(db, workspace, data_out=current, include_internal=True, reuse=reuse, store=False,
                        mandatory_rules=False, current_mandatory=True)
    if not current[0].get('_mandatory_overlay_changed'):
        return result, current[0], None
    frozen = []
    checked = revalidate(db, workspace, data_out=frozen, include_internal=True, reuse=reuse, store=False,
                         mandatory_rules=False)
    return checked, frozen[0], (result, current[0])


def lifecycle(db, actor, command, *, validation=None, shared_inputs=None):
    if command.kind == 'workspace.validate':
        workspace = validation_workspace(db, actor, command, check_revision=validation is None)
        if validation is None:
            revalidate(db, workspace)
        else:
            workspace.validation = validation
        # A check stores derived findings without invalidating edits open elsewhere.
        db.flush()
        return {'workspace': record(workspace, exclude=('validation',)), 'validation': workspace.validation,
                'fix_list': rota_fix_list(workspace), 'fix_list_delta': workspace.validation.get('fix_list_delta', {'cleared': [], 'new': []})}
    workspace = scoped(db, m.Workspace, command.target_id, actor)
    expected(workspace, command.expected_revision)
    value = (s.ReviewDecisionInput if command.kind == "workspace.approve" else
             s.WorkspaceSubmitInput if command.kind == 'workspace.submit' else s.Decision).model_validate(command.payload)
    from . import acknowledged_gaps
    action = command.kind.removeprefix("workspace.")
    if action == 'approve' and 'rejected_review_item_ids' in value.model_fields_set:
        current_items = acknowledged_gaps.review_items(workspace)
        rejected = set(value.rejected_review_item_ids)
        require(rejected <= {item['id'] for item in current_items}, 'REVIEW_ITEM_CHANGED',
                'Select current review items once each', 409)
        value = value.model_copy(update={'decisions': [s.RotaItemDecision(
            item_id=item['id'], kind='gap' if item['kind'] == 'acknowledged_gap' else 'adjustment',
            accept=item['id'] not in rejected, reason=value.reason if item['id'] in rejected else None)
            for item in current_items]})
    from .workspace_access import access_record
    require(access_record(db, workspace)['purpose'] != 'sandbox', 'SANDBOX_INTEGRATION_REQUIRED',
            'Merge or designate this draft as the rota before submitting it', 409)
    require(workspace.archived_at is None, "WORKFLOW_STATE", "This draft was discarded; restore it first", 409)
    if action == "approve":
        require_role(actor, "approver", "programme_lead")
    elif action == "publish":
        require_role(actor, "coordinator", "approver")
    elif action == "reopen" and "coordinator" not in actor.roles:
        # An approver returns a submitted rota to the coordinator: back to draft, with the reason in the audit log.
        require_role(actor, "approver", "programme_lead")
        require(workspace.status == "submitted", "WORKFLOW_STATE", "Only a rota in review can be returned to the coordinator", 409)
        outbox(db, actor, f"returned:{workspace.id}:{workspace.content_revision}", "returned", {"workspace_id": workspace.id})
        identity = db.get(m.Identity, actor.identity_id)
        workspace.return_note = {"by": actor.identity_id, "by_name": identity.display_name if identity else None,
                                 "at": m.now().isoformat(), "reason": value.reason}
    else:
        require_role(actor, "coordinator")
    if action == "reopen":
        require(workspace.status in {"submitted", "approved"}, "WORKFLOW_STATE", "Only a reviewed candidate can be reopened", 409)
        return_note = workspace.return_note
        if "coordinator" not in actor.roles:
            identity = db.get(m.Identity, actor.identity_id)
            return_note = {"by": actor.identity_id, "by_name": identity.display_name if identity else None,
                           "at": m.now().isoformat(), "reason": value.reason}
        # Reopening and archive withdrawal share the same review reset path.
        reviews.withdraw(db, actor, workspace, value.reason, return_note=return_note)
    elif action == "submit":
        from .planning_groups import require_submission_target
        require_submission_target(db, actor, workspace)
        from .planning_groups import require_submitter
        require_submitter(db, actor, workspace)
        require(workspace.status == "draft", "WORKFLOW_STATE", "Only a draft can be submitted", 409)
        from packages.domain.rota_summary import submission_prerequisite
        has_session = db.scalar(select(m.Shift.id).where(m.Shift.workspace_id == workspace.id).limit(1)) is not None
        if prerequisite := submission_prerequisite(int(has_session)):
            require(False, prerequisite['code'], prerequisite['message'], 422, prerequisite)
        result, data, current = review_gate_check(db, workspace, reuse=True) if validation is None else validation
        acknowledged_gaps.submit(db, actor, workspace, result, value.acknowledgements, value.reason,
                                 current=current[0] if current is not None else None)
        swaps.validate_publication(db, workspace)
        workspace.validation = {**result, "wishes": data["wishes"]}
        from packages.domain.rota_summary import remember_fix_list
        remember_fix_list(result, workspace.validation, submitted=True)
        from .wishes import _applied_reasons
        receipt = _applied_reasons(db, workspace, data, data['wishes'])
        if receipt is not None:
            workspace.validation = {**workspace.validation, 'wish_receipt': receipt}
        workspace.review_snapshot = reviews.resolve_route(db, workspace)
        workspace.submitted_at = m.now()
        workspace.status = "submitted"
        workspace.return_note = None
        # Submission changes preference reservations, but no other scheduling facts.
        # With no wishes, the writer-verified canonical inputs are already the submitted inputs.
        workspace.submitted_fingerprint = (data['fingerprint'] if not data['wishes']
                                           else fingerprint(db, workspace))
        workspace.validation = {**workspace.validation, 'fingerprint': workspace.submitted_fingerprint}
        from .published_changes import on_reviewed
        on_reviewed(db, actor, workspace, value.reason)
    elif action == "approve":
        require(workspace.status == "submitted", "WORKFLOW_STATE", "Only a submitted candidate can be approved", 409)
        fp = validation[1]['fingerprint'] if validation is not None else fingerprint(db, workspace)
        require(workspace.submitted_fingerprint == fp, "STALE_INPUTS",
                "The inputs used for submission have changed. Reopen this rota, check it and submit it again before review.", 409)
        result = revalidate(db, workspace, include_internal=True, reuse=True) if validation is None else validation[0]
        workspace.validation = result
        acknowledged_gaps.validate_item_decisions(db, actor, workspace, value)
        if any(not item.accept for item in value.decisions):
            return acknowledged_gaps.reject_items(db, actor, workspace, value)
        acknowledged_gaps.require_gap_decisions(workspace, result, value)
        swaps.validate_publication(db, workspace)
        decision = reviews.approve_step(db, actor, workspace, value, current_fingerprint=fp)
        from .published_changes import on_reviewed
        on_reviewed(db, actor, workspace, value.reason)
        acknowledged_gaps.record_decision(actor, workspace, value, decision)
    elif action == "publish":
        require(workspace.status == "approved", "WORKFLOW_STATE", "Only an approved candidate can be published", 409)
        from .notification_audience import is_test_identity, non_test_assignee_count
        settings = db.get_bind().get_execution_options().get("rota_write_settings")
        publisher = db.get(m.Identity, actor.identity_id)
        if settings and settings.publish_test_people_only and is_test_identity(publisher):
            real_people = non_test_assignee_count(db, workspace.id)
            require(real_people == 0, "PUBLISH_TEST_PEOPLE_ONLY",
                    f"DEV test accounts publish only test people: {real_people} real-named people assigned", 422)
        swap_request = swaps.validate_publication(db, workspace)
        # Publication mutates duties, availability and ledgers, never these raw configuration rows.
        # Each transaction acquires them independently; post-publication snapshots still read current history.
        # The writer flushes pending writes before revalidating current mandatory rules.
        # shortcut: a late mandatory rule can require two checks under the writer; upgrade: reuse complete frozen evidence when its inputs still match.
        checked, acquired, current = review_gate_check(db, workspace)
        fp = acquired['fingerprint']
        result = {key: value for key, value in checked.items() if key not in INTERNAL_CHECK_KEYS}
        # Revalidate before reading the review receipt.  A stale approval must
        # still run the current evaluator so publication can never bypass a
        # newly introduced mandatory finding.
        approval_error = None
        try:
            _publication_approval(db, workspace, fp)
        except Problem as error:
            if error.code != 'STALE_APPROVAL':
                raise
            approval_error = error
        try:
            if current is not None:
                live_result = {key: value for key, value in current[0].items() if key not in INTERNAL_CHECK_KEYS}
                acknowledged_gaps.require_accepted(workspace, live_result, reviewed=True)
            acknowledged_gaps.require_accepted(workspace, result, reviewed=True)
        except Problem as error:
            if error.code == 'VALIDATION_FAILED' and approval_error is not None:
                error.details = {**(error.details or {}), 'stale_approval': {
                    'code': approval_error.code, 'message': approval_error.message,
                    'details': approval_error.details}}
            raise
        if approval_error is not None:
            raise approval_error
        focus = set(acquired['focus_ids'])
        own = [row for row in acquired['shifts'] if row['id'] in focus]
        preparation = db.info.get('_planning_preparation')
        if preparation is not None and not preparation['replay']:
            from .published_changes import change_list, draft_changes
            row = change_list(db, workspace.base_publication_id) if workspace.base_publication_id else None
            if not draft_changes(row):
                # Unsent changes need successor checks later in this handler; ordinary publication has no more pure work.
                from .planning_preparation import RollbackPreparation
                raise RollbackPreparation()
        workspace.validation = checked
        result['gap_acknowledgements'] = acknowledged_gaps.audit_rows(workspace)
        result['review_items'] = acknowledged_gaps.review_items(workspace)
        result['review_item_decisions'] = (workspace.validation or {}).get('review_item_decisions', [])
        superseded, trimmed, effective_until = schedule.make_room(db, workspace)
        sequence = (db.scalar(select(func.max(m.Publication.sequence)).where(m.Publication.department_id == actor.department_id)) or 0) + 1
        workspace.status = "published"
        workspace.revision += 1
        publication = m.Publication(id=command_id(db), department_id=actor.department_id, workspace_id=workspace.id,
                                    sequence=sequence, starts_on=workspace.starts_on, ends_on=workspace.ends_on,
                                    supersedes_id=workspace.base_publication_id, published_by=actor.identity_id,
                                    effective_until=effective_until,
                                    snapshot=publication_snapshot(db, workspace, result, own_shifts=own, data=acquired),
                                    validation=result, fingerprint=fp)
        db.add(publication)
        db.flush()
        for person_id in sorted({a["person_id"] for s in publication.snapshot["shifts"] for a in s["participants"] if a.get("person_id")}):
            db.add(m.PublicationPerson(publication_id=publication.id, person_id=person_id))
        from .published_changes import materialize_absences
        materialized_absences = materialize_absences(db, actor, workspace, value.reason)
        zone = db.get(m.Organisation, actor.organisation_id).timezone
        from . import wishes
        if trimmed:
            # The replaced rota's ledgers now cover only the days it is still in force.
            wishes.published(db, actor, db.get(m.Workspace, trimmed.workspace_id), trimmed, zone)
        from .fairness import on_publish
        # With no new availability or changed issued window, final writer-checked inputs remain
        # exact. Ledger rebuilding still refreshes canonical history after removing the predecessor.
        same_window = superseded is None or (superseded.starts_on, superseded.ends_on) == (workspace.starts_on, workspace.ends_on)
        ledger_data = acquired if same_window and not (materialized_absences or workspace.replaces_publication_id
                                                             or trimmed or effective_until) else None
        on_publish(db, publication, trimmed, own_shifts=own, data=ledger_data, shared_inputs=shared_inputs)
        swaps.published(db, actor, swap_request, publication.id, value.reason)
        from .published_changes import on_published
        person_changes = on_published(db, actor, workspace, publication, value.reason)
        wishes.published(db, actor, workspace, publication, zone)
        bump_inputs(db, actor)
        from .common import neutral_rota_name
        outbox(db, actor, "publication:" + publication.id, "publication",
               {"publication_id": publication.id, "sequence": sequence, "workspace_name": neutral_rota_name(workspace.name),
                "supersedes_id": workspace.base_publication_id, 'person_changes': person_changes})
        # Other rotas planned for these days are now out of date: they overlap a published rota without a decision.
        outdated = schedule.overlaps(db, workspace.department_id, workspace)["drafts"]
        return {"workspace": record(workspace), "publication_id": publication.id, "sequence": sequence,
                # SW3-B-07: everything the web needs to switch to the published view at once, without a re-read.
                "published_view": {"publication_id": publication.id, "workspace_id": workspace.id, "sequence": sequence,
                                   "starts_on": publication.starts_on.isoformat(), "ends_on": publication.ends_on.isoformat()},
                "replaced": {"publication_id": trimmed.id, "effective_until": trimmed.effective_until.isoformat()} if trimmed else None,
                "outdated_drafts": outdated, 'fix_list': acknowledged_gaps.projection(workspace, result),
                'gap_acknowledgements': acknowledged_gaps.audit_rows(workspace),
                'review_items': acknowledged_gaps.review_items(workspace),
                'review_item_decisions': result.get('review_item_decisions', []),
                "message": "Publication committed. Notifications are queued, not acknowledged."}
    workspace.revision += 1
    if workspace.status != 'draft':
        from .generation_resume import clear_generation_resume
        clear_generation_resume(db, workspace_id=workspace.id)
    db.flush()
    if action == "approve" and workspace.status == "approved" and db.get(m.Department, workspace.department_id).swap_auto_publish:
        swap = db.scalar(select(m.SwapRequest).where(m.SwapRequest.workspace_id == workspace.id))
        if swap:
            result = swaps.automatically_publish(db, swap)
            if result:
                return result
    from .published_changes import review_payload
    return {"workspace": record(workspace, exclude=("validation",)), "validation": workspace.validation,
            **review_payload(db, workspace, actor),
            'fix_list': acknowledged_gaps.projection(workspace),
            'review_items': acknowledged_gaps.review_items(workspace),
            'review_item_decisions': (workspace.validation or {}).get('review_item_decisions', []),
            'fix_list_delta': (workspace.validation or {}).get('fix_list_delta', {'cleared': [], 'new': []}),
            'gap_acknowledgements': acknowledged_gaps.audit_rows(workspace)}
