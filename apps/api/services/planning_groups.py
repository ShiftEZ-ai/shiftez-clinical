"""Versioned group configuration and shared-rota write admission."""
from copy import deepcopy
from datetime import date, timedelta

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import aliased

from apps.api import models as m
from apps.api.auth import department_roles, require_permission, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import record
from packages.contracts import schemas as s
from packages.domain.planning_groups import GroupResolver
from packages.domain.time import local_midnight
from .common import expected, scoped
from .workspace_access import access_record, append_access, require_edit, shared_clause


def load_groups(db, department_id, *, starts_on, ends_on):
    """One bounded-window acquisition; fully superseded historical versions stay in paginated history."""
    cls, newer = m.PlanningGroupVersion, aliased(m.PlanningGroupVersion)
    a, b = str(starts_on), str(ends_on)
    first, last = cls.definition['starts_on'].as_string(), cls.definition['ends_on'].as_string()
    lo = case((first < a, a), else_=first)
    hi = case((or_(last.is_(None), last > b), b), else_=last)
    dominates = exists(select(newer.id).where(newer.group_id == cls.group_id,
        newer.department_id == department_id, newer.revision > cls.revision,
        newer.definition['starts_on'].as_string() <= lo,
        or_(newer.definition['ends_on'].as_string().is_(None), newer.definition['ends_on'].as_string() >= hi)))
    return [record(row) for row in db.scalars(select(cls).where(cls.department_id == department_id,
        first < b, or_(last.is_(None), last > a), ~dominates).order_by(cls.group_id, cls.revision))]


def default_rota_id(db, workspace):
    key = workspace.department_id, workspace.starts_on, workspace.ends_on
    cache = db.info.setdefault('default_rotas', {})
    if key not in cache:
        cache[key] = _default_rota_id(db, workspace)
    return cache[key]


def _default_rota_id(db, workspace):
    """A read-only default; explicit access versions always own an operator's choice."""
    W, A = m.Workspace, m.WorkspaceAccessVersion
    rows = db.execute(select(W.id, W.starts_on, W.ends_on, W.access_version_id, A.purpose).outerjoin(
        A, A.id == W.access_version_id).where(W.department_id == workspace.department_id,
        W.archived_at.is_(None), W.base_publication_id.is_(None), W.status.in_(('draft', 'submitted', 'approved')),
        W.starts_on < workspace.ends_on, W.ends_on > workspace.starts_on,
        ~W.id.in_(select(m.PublicationChangeList.workspace_id)))
        .order_by(W.created_at, W.id)).all()
    explicit = next((r.id for r in rows if r.purpose == 'integrated'), None)
    if explicit:
        return explicit
    candidates = [r.id for r in rows if r.access_version_id is None and r.starts_on == workspace.starts_on
                  and r.ends_on == workspace.ends_on]
    if not candidates:
        return None
    versions = load_groups(db, workspace.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on)
    return candidates[0] if any(row['definition'].get('active', True) for row in versions) else None


def settings_row(db, department_id):
    return db.scalar(select(m.PlanningSettingsVersion).where(m.PlanningSettingsVersion.department_id == department_id)
                     .order_by(m.PlanningSettingsVersion.revision.desc()).limit(1))


def settings_definition(db, department_id):
    row = settings_row(db, department_id)
    return s.PlanningSettingsDefinition.model_validate(row.definition if row else {}).model_dump(mode='json')


def coordinator(db, actor):
    return actor.permits('coordinator') and actor.identity_id in settings_definition(db, actor.department_id)['coordinator_identity_ids']


def unrestricted(db, actor, access):
    """Whether guard_changes lets this person change everything in the rota, including session definitions and fixed
    assignments: any rota but the period's integrated one, or its Rota Coordinator (SW3-A-10: reads expose it)."""
    return access['purpose'] != 'integrated' or coordinator(db, actor)


def validate_editors(db, actor, identities):
    roles = department_roles(db, actor.department_id)
    require(set(identities) <= {identity for identity, held in roles.items() if 'coordinator' in held},
            'PLANNING_EDITOR_ACCESS', 'Every scheduling owner and co-editor needs active scheduling access', 422)


def configure(db, actor, command):
    require_permission(actor, 'planning.configure')
    require(command.kind in {'planning_group.create', 'planning_group.update', 'planning_group.rollback',
                            'planning.settings', 'planning.settings.rollback'}, 'UNKNOWN_COMMAND', 'Unsupported planning configuration command')
    group = command.kind.startswith('planning_group.')
    cls = m.PlanningGroupVersion if group else m.PlanningSettingsVersion
    query = select(cls).where(cls.department_id == actor.department_id)
    if group and command.kind != 'planning_group.create':
        query = query.where(cls.group_id == command.target_id)
    previous = None if command.kind == 'planning_group.create' else db.scalar(query.order_by(cls.revision.desc()).limit(1))
    if group and command.kind != 'planning_group.create':
        require(previous is not None, 'NOT_FOUND', 'Record not found', 404)
    if previous:
        expected(previous, command.expected_revision)
    else:
        require(command.expected_revision is None, 'STALE_REVISION', 'Configuration was not created yet', 409)
    rollback = command.kind.endswith('.rollback')
    if rollback:
        value = s.PlanningRollback.model_validate(command.payload)
        source = scoped(db, cls, value.version_id, actor)
        require(previous is not None and (not group or source.group_id == previous.group_id),
                'PLANNING_VERSION_SCOPE', 'Choose a version of this configuration', 422)
        definition = source.definition
    else:
        value = (s.PlanningGroupInput if group else s.PlanningSettingsInput).model_validate(command.payload)
        definition = value.definition.model_dump(mode='json')
    parsed = (s.PlanningGroupDefinition if group else s.PlanningSettingsDefinition).model_validate(definition)
    definition = parsed.model_dump(mode='json')
    identities = (parsed.owner_identity_ids + parsed.co_editor_identity_ids) if group else parsed.coordinator_identity_ids
    validate_editors(db, actor, identities)
    if group and parsed.active:
        require(settings_definition(db, actor.department_id)['coordinator_identity_ids'],
                'PLANNING_COORDINATOR_REQUIRED', 'Choose Rota Coordinators in Scheduling groups before activating a group', 422)
    if not group and not parsed.coordinator_identity_ids:
        latest = select(m.PlanningGroupVersion.group_id, func.max(m.PlanningGroupVersion.revision).label('revision')).where(
            m.PlanningGroupVersion.department_id == actor.department_id).group_by(m.PlanningGroupVersion.group_id).subquery()
        active = db.scalar(select(m.PlanningGroupVersion.id).join(latest,
            (latest.c.group_id == m.PlanningGroupVersion.group_id) & (latest.c.revision == m.PlanningGroupVersion.revision)).where(
            m.PlanningGroupVersion.definition['active'].as_boolean().is_(True)).limit(1))
        require(active is None, 'PLANNING_COORDINATOR_REQUIRED',
                'Keep at least one Rota Coordinator while scheduling groups are active', 422)
    if group:
        from .policies import condition_references
        condition_references(db, actor, parsed.membership)
    row = cls(department_id=actor.department_id, definition=definition,
        revision=previous.revision + 1 if previous else 1, previous_id=previous.id if previous else None,
        actor_id=actor.identity_id, reason=value.reason,
        **({'group_id': previous.group_id if previous else m.uid()} if group else {}))
    db.add(row)
    db.flush()
    db.info.pop('default_rotas', None)
    undo = {'kind': 'planning_group.rollback' if group else 'planning.settings.rollback',
            'target_id': row.group_id if group else None, 'expected_revision': row.revision,
            'payload': {'version_id': previous.id, 'reason': value.reason}} if previous else {
                'kind': 'planning_group.update' if group else 'planning.settings',
                'target_id': row.group_id if group else None, 'expected_revision': row.revision,
                'payload': {'definition': {**definition, 'active': False} if group else
                            s.PlanningSettingsDefinition().model_dump(mode='json'), 'reason': value.reason}}
    return {'group' if group else 'settings': record(row), 'undo': undo}


def list_groups(db, actor, *, offset=0, limit=100, group_id=None):
    require_role(actor, *s.PERMISSIONS['planning.read'])
    cls = m.PlanningGroupVersion
    query = select(cls).where(cls.department_id == actor.department_id)
    if group_id:
        query = query.where(cls.group_id == group_id).order_by(cls.revision.desc())
    else:
        latest = select(cls.group_id, func.max(cls.revision).label('revision')).where(
            cls.department_id == actor.department_id).group_by(cls.group_id).subquery()
        query = query.join(latest, (latest.c.group_id == cls.group_id) & (latest.c.revision == cls.revision)).order_by(cls.group_id)
    rows = list(db.scalars(query.offset(offset).limit(limit + 1)))
    result = [record(row) for row in rows[:limit]]
    names = owner_names(db, result)
    return {'groups': [{**row, **names[row['id']]} for row in result], 'has_more': len(rows) > limit}


def read_settings(db, actor, *, history=False, offset=0, limit=100):
    require_role(actor, *s.PERMISSIONS['planning.read'])
    if history:
        rows = list(db.scalars(select(m.PlanningSettingsVersion).where(m.PlanningSettingsVersion.department_id == actor.department_id)
            .order_by(m.PlanningSettingsVersion.revision.desc()).offset(offset).limit(limit + 1)))
        return {'versions': [record(row) for row in rows[:limit]], 'has_more': len(rows) > limit}
    row = settings_row(db, actor.department_id)
    return {'settings': record(row) if row else None, 'definition': settings_definition(db, actor.department_id)}


def admit_private(db, actor, *, restoring=None):
    limit = settings_definition(db, actor.department_id)['active_private_drafts_per_owner']
    if limit is None:
        return
    count = db.scalar(select(func.count()).select_from(m.Workspace).where(m.Workspace.department_id == actor.department_id,
        m.Workspace.created_by == actor.identity_id, m.Workspace.archived_at.is_(None),
        ~shared_clause(), *([m.Workspace.id != restoring] if restoring else [])))
    require(count < limit, 'PRIVATE_DRAFT_LIMIT', 'Archive a private draft before creating or restoring another', 409)


def archive_due(db, actor, settings):
    """Owner-triggered maintenance; only the caller's private drafts, retained in full and restorable."""
    value = settings_definition(db, actor.department_id)['archive_after_days']
    if value is None:
        return {'archived_workspace_ids': [], 'has_more': False}
    from .bulk_settings import limits
    batch_size = limits(db, actor, settings)['bulk_direct_max']
    rows = list(db.scalars(select(m.Workspace).where(m.Workspace.department_id == actor.department_id,
        m.Workspace.created_by == actor.identity_id, m.Workspace.archived_at.is_(None), m.Workspace.status == 'draft',
        ~shared_clause(), m.Workspace.created_at <= m.now() - timedelta(days=value))
        .order_by(m.Workspace.created_at, m.Workspace.id).limit(batch_size + 1)))
    from . import planning
    for workspace in rows[:batch_size]:
        workspace.archive_state = planning._archive_state(workspace, access_record(db, workspace))
        workspace.archived_at, workspace.archived_by = m.now(), actor.identity_id
        workspace.archive_reason = 'Automatic private draft retention'
        workspace.revision += 1
        db.info.setdefault('private_workspace_ids', set()).add(workspace.id)
    return {'archived_workspace_ids': [row.id for row in rows[:batch_size]], 'has_more': len(rows) > batch_size}


def workspace_command(db, actor, command, settings):
    require_role(actor, 'coordinator')
    value = s.Decision.model_validate(command.payload)
    if command.kind == 'workspace.archive_due':
        return archive_due(db, actor, settings)
    workspace = scoped(db, m.Workspace, command.target_id, actor)
    expected(workspace, command.expected_revision)
    access = access_record(db, workspace)
    require(workspace.status == 'draft' and workspace.archived_at is None,
            'WORKFLOW_STATE', 'Choose an active draft', 409)
    if command.kind == 'workspace.share':
        require(workspace.created_by == actor.identity_id, 'DRAFT_OWNER_ONLY', 'Only the creator can share this draft', 403)
        require(access['visibility'] == 'private', 'ALREADY_SHARED', 'This draft is already shared', 409)
        append_access(db, actor, workspace, 'shared', access['purpose'], value.reason)
    else:
        require(coordinator(db, actor), 'ROTA_COORDINATOR_ONLY', 'Only a configured Rota Coordinator can designate the rota', 403)
        require(access['visibility'] == 'shared', 'SHARE_REQUIRED', 'Share this draft before making it the rota', 409)
        designate(db, actor, workspace, value.reason)
    workspace.revision += 1
    return {'workspace': record(workspace), 'audience': list(s.PERMISSIONS['planning.read'])}


def designate(db, actor, workspace, reason):
    require(coordinator(db, actor), 'ROTA_COORDINATOR_ONLY', 'Only a configured Rota Coordinator can designate the rota', 403)
    access = m.WorkspaceAccessVersion
    competing = list(db.scalars(select(m.Workspace).join(access, access.id == m.Workspace.access_version_id).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.id != workspace.id,
        m.Workspace.archived_at.is_(None), access.purpose == 'integrated',
        m.Workspace.starts_on < workspace.ends_on, m.Workspace.ends_on > workspace.starts_on,
        m.Workspace.status.in_(('draft', 'submitted', 'approved')))))
    require(not any(row.status != 'draft' for row in competing), 'ROTA_IN_REVIEW', 'Finish the current review before replacing the rota', 409)
    require(not competing, 'ROTA_ALREADY_DESIGNATED', 'This period already has a rota; retain this draft as an alternative', 409)
    append_access(db, actor, workspace, 'shared', 'integrated', reason)


def require_submission_target(db, actor, workspace):
    if access_record(db, workspace)['purpose'] == 'integrated':
        return
    access = m.WorkspaceAccessVersion
    default = default_rota_id(db, workspace)
    require(default is None or default == workspace.id, 'ROTA_INTEGRATION_REQUIRED',
            'Submit the department rota for this period; retain this draft as an alternative', 409)
    active_rota = db.scalar(select(m.Workspace.id).join(access, access.id == m.Workspace.access_version_id).where(
        m.Workspace.department_id == actor.department_id, m.Workspace.id != workspace.id,
        m.Workspace.archived_at.is_(None), access.purpose == 'integrated', m.Workspace.status.in_(('draft', 'submitted', 'approved')),
        m.Workspace.starts_on < workspace.ends_on, m.Workspace.ends_on > workspace.starts_on).limit(1))
    require(active_rota is None, 'ROTA_INTEGRATION_REQUIRED', 'Submit the department rota for this period; retain this draft as an alternative', 409)


_AUTHORITY_COMPLETE = object()


def guard_change_authority(db, actor, workspace, before, *, review_rejection=False, after=None,
                           session_definitions=False):
    """Check edit authority before an assignment admission evaluates safety."""
    access = require_edit(db, actor, workspace)
    if not review_rejection and unrestricted(db, actor, access):
        return _AUTHORITY_COMPLETE
    return _guard_scoped_change_authority(db, actor, workspace, before, review_rejection=review_rejection,
                                          after=after, session_definitions=session_definitions)


def _guard_scoped_change_authority(db, actor, workspace, before, *, review_rejection=False, after=None,
                                   session_definitions=False):
    """Check scoped edit authority after checking access and unrestricted authority."""
    if review_rejection:
        # The review handler has checked independent route authority. This permits only its exact return delta.
        require_role(actor, 'approver', 'programme_lead')
        from apps.api.snapshots import workspace_content
        current = workspace_content(db, workspace)
        require(before['workspace']['status'] == 'submitted' and workspace.status == 'draft'
                and before['shifts'] == current['shifts'] and all(
                    row in before['workspace'].get(field, []) for field in
                    ('rule_adjustments', 'target_overrides', 'accepted_issues') for row in current['workspace'].get(field, [])),
                'REVIEW_RETURN_SCOPE', 'A review return may only remove rejected items', 403)
        return _AUTHORITY_COMPLETE
    versions = load_groups(db, actor.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on)
    require(any(row['definition'].get('active', True) and actor.identity_id in
                row['definition']['owner_identity_ids'] + row['definition'].get('co_editor_identity_ids', [])
                for row in versions),
            'GROUP_EDIT_AUTHORITY', 'This assignment belongs to another group or the Rota Coordinator', 403)
    require(not session_definitions, 'SESSION_DEFINITION_AUTHORITY',
            'Only the Rota Coordinator can change session definitions', 403)
    from apps.api.snapshots import domain_snapshot, workspace_content
    from .planner_tools import normalise_shift
    current = deepcopy(after) if after is not None else workspace_content(db, workspace)
    require(all(before['workspace'].get(key) == current['workspace'].get(key) for key in
        ('starts_on', 'ends_on', 'replaces_publication_id', 'accepted_issues')),
        'ROTA_COORDINATOR_ONLY', 'Department-wide changes require the Rota Coordinator', 403)
    old, new = ({row['id']: row for row in content['shifts']} for content in (before, current))
    require(old.keys() == new.keys(), 'SESSION_DEFINITION_AUTHORITY', 'Only the Rota Coordinator can add or remove session definitions', 403)
    deltas = []
    for sid in old:
        left, right = normalise_shift(old[sid]), normalise_shift(new[sid])
        a, b = left.pop('participants'), right.pop('participants')
        left.pop('series_modified', None)
        right.pop('series_modified', None)
        require(left == right, 'SESSION_DEFINITION_AUTHORITY', 'Only the Rota Coordinator can change session definitions', 403)
        previous, proposed = ({p['person_id']: p for p in people} for people in (a, b))
        for pid in previous.keys() | proposed.keys():
            if previous.get(pid) != proposed.get(pid):
                require(bool((previous.get(pid) or {}).get('fixed')) == bool((proposed.get(pid) or {}).get('fixed')),
                        'SESSION_DEFINITION_AUTHORITY', 'Only the Rota Coordinator can change fixed assignments', 403)
                deltas.extend((pid, shift, person) for shift, person in
                    ((old[sid], previous.get(pid)), (new[sid], proposed.get(pid))) if person is not None)
    data = domain_snapshot(db, workspace, include_fairness=False)
    resolver = GroupResolver(data, versions)
    for field in ('target_overrides', 'rule_adjustments'):
        was, now = before['workspace'].get(field, []), current['workspace'].get(field, [])
        for entry in [row for row in was if row not in now] + [row for row in now if row not in was]:
            person_id = entry.get('person_id')
            require(person_id, 'GROUP_ADJUSTMENT_SCOPE', 'Limit this adjustment to a person in your scheduling group', 403)
            interval = {'id': 'adjustment', '_date_projection': True,
                'start': local_midnight(date.fromisoformat(str(entry.get('starts_on') or workspace.starts_on)), data['organisation']['timezone']),
                'end': local_midnight(date.fromisoformat(str(entry.get('ends_on') or workspace.ends_on)), data['organisation']['timezone'])}
            primary = resolver.primary(person_id, interval)
            require(primary is not None and actor.identity_id in primary['editor_identity_ids'],
                    'GROUP_ADJUSTMENT_SCOPE', 'This adjustment affects another group or the Rota Coordinator', 403)
    for person_id, shift, participant in deltas:
        primary = resolver.primary(person_id, shift, participant)
        require(primary is not None and actor.identity_id in primary['editor_identity_ids'],
                'GROUP_EDIT_AUTHORITY', 'This assignment belongs to another group or the Rota Coordinator', 403)
    return data, new, deltas


def require_change_authority(db, actor, workspace, before, *, after=None, session_definitions=False,
                             review_rejection=False):
    """Authorize proposed content before evaluating people; reuse only the same exact edit."""
    contexts = db.info.setdefault('_planning_group_authority_contexts', {})
    cached = contexts.pop(workspace.id, None)
    from apps.api.snapshots import snapshot_hash
    before_digest = None
    authority = None
    if (cached and not review_rejection and not session_definitions and cached[0] is before
            and cached[1] == actor.identity_id and cached[2] == workspace.content_revision):
        # Only scoped authority is retained; access and unrestricted authority are still checked live.
        access = require_edit(db, actor, workspace)
        if unrestricted(db, actor, access):
            return _AUTHORITY_COMPLETE
        before_digest = snapshot_hash(before)
        if cached[3] == before_digest:
            from apps.api.snapshots import workspace_content
            from .planner_tools import normalise_shift
            current = after if after is not None else workspace_content(db, workspace)
            new = {row['id']: row for row in current['shifts']}
            previous = cached[4][1]
            # Automatic pairing and other later mutations must not inherit authority for a different proposal.
            if new.keys() == previous.keys() and all(normalise_shift(new[sid]) == normalise_shift(previous[sid])
                    for sid in new) and all(current['workspace'].get(key) == before['workspace'].get(key) for key in
                    ('starts_on', 'ends_on', 'replaces_publication_id', 'accepted_issues', 'rule_adjustments', 'target_overrides')):
                authority = cached[4]
    if authority is None:
        # A retained context already checked access live above; do not read it twice.
        check = _guard_scoped_change_authority if before_digest is not None else guard_change_authority
        authority = check(db, actor, workspace, before, after=after,
            session_definitions=session_definitions, review_rejection=review_rejection)
    if not review_rejection and authority is not _AUTHORITY_COMPLETE:
        # Retain the source content so equal revisions cannot reuse a different edit. Unrestricted
        # authority is re-checked live on every call, so it needs no digest of the whole rota.
        contexts[workspace.id] = (before, actor.identity_id, workspace.content_revision,
                                  before_digest or snapshot_hash(before), authority)
    return authority


def retain_assignment_authority(db, workspace, before, actor, authority):
    if authority is not _AUTHORITY_COMPLETE:
        from apps.api.snapshots import snapshot_hash
        db.info['_planning_group_authority_contexts'][workspace.id] = (
            before, actor.identity_id, workspace.content_revision, snapshot_hash(before), authority)


def guard_changes(db, actor, workspace, before, *, review_rejection=False, generation_apply=False):
    """All manual, import, bulk, Undo and generation writes converge on planning.changed."""
    if generation_apply and actor.permits('admin'):
        require_edit(db, actor, workspace)
        return _AUTHORITY_COMPLETE
    context = require_change_authority(db, actor, workspace, before, review_rejection=review_rejection)
    db.info['_planning_group_authority_contexts'].pop(workspace.id, None)
    if context is _AUTHORITY_COMPLETE:
        return context
    data, new, deltas = context
    # An unchanged dependent assignment cannot be left without its named supervisor.
    touched = {shift['id'] for _, shift, _ in deltas}
    for sid in touched:
        people = {p['person_id']: p for p in new[sid]['participants']}
        for participant in people.values():
            supervisor = participant.get('supervisor_person_id')
            require(not supervisor or supervisor in people, 'GROUP_DEPENDENCY_PROTECTED',
                    'Another assignment relies on this supervisor; keep or replace the supervision together', 409)
            if supervisor:
                from packages.domain.participation import presence_intervals
                spans = presence_intervals(people[supervisor])
                from packages.domain.validation import trainee_intervals
                from packages.domain.time import union_hours, duration_hours
                require(all(union_hours([(max(lo, a), min(hi, b)) for lo, hi in spans]) >= duration_hours(a, b)
                            for a, b in trainee_intervals(data, new[sid], participant)), 'GROUP_DEPENDENCY_PROTECTED',
                        'Keep the supervisor present throughout the dependent assignment', 409)
    return context


def _generation_group_context(db, actor, workspace, value):
    """Acquire the cheap group decision shared by enqueue and worker preparation."""
    access = require_edit(db, actor, workspace)
    if access['purpose'] != 'integrated' or (value.planning_group_version_id is None
            and (actor.permits('admin') or coordinator(db, actor))):
        require(value.planning_group_version_id is None, 'GROUP_VERSION_SCOPE', 'Group runs require the department rota', 422)
        return None, None, {}, False
    versions = load_groups(db, actor.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on)
    selected_group = next((row for row in versions if row['id'] == value.planning_group_version_id), None)
    require(value.planning_group_version_id is None or selected_group is not None,
            'GROUP_VERSION_SCOPE', 'Choose an effective scheduling group for this period', 422)
    if selected_group is None:
        return None, versions, {}, False
    definition = selected_group['definition']
    editors = set(definition['owner_identity_ids']) | set(definition.get('co_editor_identity_ids', []))
    require(definition.get('active', True) and (actor.permits('admin') or actor.identity_id in editors),
            'GROUP_GENERATION_SCOPE', 'Choose only your scheduling group population', 403)
    default_scope = value.generation_scope is None
    scope = value.generation_scope or s.GenerationScope(conditions=definition['membership'])
    lookahead = value.lookahead or settings_definition(db, actor.department_id)['group_lookahead']
    value.lookahead = lookahead
    return selected_group, versions, {
        'planning_group_version_id': selected_group['id'],
        'planning_group_id': selected_group['group_id'],
        'lookahead': lookahead,
        'planning_context': {'group_version_id': selected_group['id'], 'group_id': selected_group['group_id'],
            'protection': scope.protection},
    }, default_scope


class GenerationAdmissionChanged(Exception):
    """The command writer must release its lock before refreshing scope evidence."""


def guard_generation(db, actor, workspace, snapshot, value, *, frozen=False, require_population=False):
    from packages.domain.generation_scope import has_population, population_candidates, selected_session_ids

    selected_group, versions, metadata, default_scope = _generation_group_context(db, actor, workspace, value)
    enqueue = snapshot is None
    check_nonempty = (enqueue or require_population) and value.generation_scope is not None
    if versions is None and value.generation_scope is None:
        return metadata
    scope = (s.GenerationScope(conditions=selected_group['definition']['membership'])
             if default_scope else value.generation_scope)
    uses_roles = (scope is not None and 'roles' in scope.conditions.fields()) or any(
        'roles' in s.Conditions.model_validate(row['definition']['membership']).fields() for row in versions or ())
    roles = None
    if uses_roles:
        from apps.api.snapshots import generation_role_bindings
        roles = generation_role_bindings(db, workspace)
    if enqueue:
        metadata['_generation_authority_pending'] = True
    if uses_roles:
        metadata['generation_role_bindings'] = roles
    scope = scope.model_dump(mode='json') if scope else None

    capture = db.info.get('_generation_admission_capture') if enqueue else None
    replay = db.info.get('_generation_admission_replay') if enqueue else None
    event_currencies = []
    if capture is not None or replay is not None:
        from apps.api.snapshots import currency_match, preparation_stamp, prepared_event_currency_matches
        key = (actor, workspace.id, value.session_ids, scope, versions, metadata)
        if replay is not None and replay.get('key') != key:
            raise GenerationAdmissionChanged()
        stamp = preparation_stamp(db, workspace)
        if replay is not None:
            if (not currency_match(replay['stamp'], stamp)
                    or not all(prepared_event_currency_matches(db, events) for events in replay['events'])):
                raise GenerationAdmissionChanged()
            if refusal := replay.get('refusal'):
                raise Problem(*refusal)
            return replay['metadata']
        capture.update(key=key, stamp=stamp, events=event_currencies)

    try:
        if enqueue:
            from apps.api.snapshots import domain_snapshot
            snapshot = domain_snapshot(db, workspace, include_fairness=False,
                                       include_objectives=False, include_fairness_history=False, include_manifest=False,
                                       _event_currency=event_currencies if capture is not None else None)
        if uses_roles and not enqueue and not frozen:
            snapshot['generation_role_bindings'] = roles
        requested = {**snapshot, 'generation_scope': scope}
        if uses_roles:
            requested['generation_role_bindings'] = roles
        selected = set(selected_session_ids(snapshot) if frozen else value.session_ids or snapshot['focus_ids'])
        if versions is not None:
            candidates = population_candidates(requested, selected) if enqueue and selected_group else None
            population = _generation_population(requested, selected, versions, actor, selected_group, default_scope,
                                                candidates=candidates)
        if check_nonempty:
            requested['generation_session_ids'] = selected
            require(any(population.values()) if selected_group else has_population(requested), 'INVALID_SCOPE',
                    'No people match this group during the selected duties; change the population or duty selection', 422)
    except Problem as refusal:
        if capture is not None:
            capture['refusal'] = (refusal.status, refusal.code, refusal.message, refusal.details)
        raise

    if selected_group:
        metadata = {**metadata, 'planning_context': {**metadata['planning_context'], 'selected_session_ids': sorted(selected)}}
        if not enqueue and not frozen:
            snapshot.update({**metadata,
                'planning_population': {sid: sorted(pids) for sid, pids in population.items()},
                'planning_group_versions': versions})
            if default_scope:
                snapshot['generation_scope'] = requested['generation_scope']
    if capture is not None:
        capture['metadata'] = metadata
    return metadata


def _generation_population(snapshot, selected, versions, actor, selected_group, default_scope, *, candidates=None):
    from packages.domain.generation_scope import population_allows

    resolver = GroupResolver(snapshot, versions)
    admin = actor.permits('admin')
    population = {}
    for shift in snapshot['shifts']:
        if shift['id'] not in selected or shift.get('cancelled'):
            continue
        population[shift['id']] = []
        people = (candidates[shift['id']] if candidates is not None else
                  (pid for pid in snapshot['people'] if population_allows(snapshot, shift, pid)))
        for pid in people:
            primary = resolver.primary(pid, shift)
            if selected_group is not None and (default_scope or admin) and (
                    primary is None or primary['group_id'] != selected_group['group_id']):
                continue
            require(primary is not None and (admin or actor.identity_id in primary['editor_identity_ids'])
                    and (selected_group is None or primary['group_id'] == selected_group['group_id']),
                    'GROUP_GENERATION_SCOPE', 'Choose only your scheduling group population', 403)
            population[shift['id']].append(pid)
    return population


def guard_frozen_generation(db, actor, workspace, snapshot):
    value = s.JobInput(workspace_id=workspace.id, kind='generation',
                       generation_scope=snapshot.get('generation_scope'),
                       planning_group_version_id=snapshot.get('planning_group_version_id'),
                       lookahead=snapshot.get('lookahead'))
    guard_generation(db, actor, workspace, snapshot, value, frozen=True)


def guard_job_control(db, actor, job):
    workspace = scoped(db, m.Workspace, job.workspace_id, actor)
    access = require_edit(db, actor, workspace)
    require(job.requested_by == actor.identity_id or (job.kind == 'generation' and actor.permits('admin'))
            or unrestricted(db, actor, access),
            'GROUP_JOB_AUTHORITY', 'Control your own group run or ask the Rota Coordinator', 403)


def fix_list_context(db, actor, workspace, fix_list, *, group_version_id=None, validation=None):
    from apps.api.snapshots import domain_snapshot
    from packages.domain.rota_summary import planning_projection
    versions = load_groups(db, actor.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on)
    require(group_version_id is None or any(row['id'] == group_version_id for row in versions),
            'GROUP_VERSION_SCOPE', 'Choose an effective group for this period', 422)
    data = domain_snapshot(db, workspace) if versions else {'shifts': [], 'focus_ids': []}
    projected = planning_projection(data, versions, fix_list,
        identity_id=actor.identity_id, selected_version_id=group_version_id,
        validation=validation if validation is not None else workspace.validation)
    return review_projection(db, workspace, projected, data=data, versions=versions)


def require_submitter(db, actor, workspace):
    if access_record(db, workspace)['purpose'] != 'integrated' or coordinator(db, actor):
        return
    versions = load_groups(db, actor.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on)
    require(any(actor.identity_id in row['definition']['owner_identity_ids'] and row['definition'].get('active', True)
                for row in versions), 'GROUP_SUBMIT_AUTHORITY', 'A group owner or the Rota Coordinator must submit the rota', 403)


def review_projection(db, workspace, fix_list, *, data, versions):
    """One compact grouped review, including original adjustment authors, for every screen width."""
    context = fix_list['planning_context']
    resolver = GroupResolver(data, versions)
    causes = {c['id']: c for c in fix_list['causes']}
    items = []
    for item in fix_list['review_items']:
        source = item['source']
        ids = []
        if versions and source.get('person_id'):
            interval = {'id': 'review', '_date_projection': True,
                'start': local_midnight(date.fromisoformat(str(source.get('starts_on') or workspace.starts_on)), data['organisation']['timezone']),
                'end': local_midnight(date.fromisoformat(str(source.get('ends_on') or workspace.ends_on)), data['organisation']['timezone'])}
            primary = resolver.primary(source['person_id'], interval)
            ids = [primary['group_id']] if primary else []
        elif item['id'] in causes:
            ids = causes[item['id']]['attribution']['group_ids']
        items.append({**item, 'group_ids': ids, 'author_identity_id': source.get('by'),
            'at': source.get('at'), 'sentence': source.get('sentence') or item.get('name') or item['kind'],
            'rule_id': source.get('policy_id')})
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(
        m.Identity.id.in_({item['author_identity_id'] for item in items} - {None}))).all()) if items else {}
    for item in items:
        item['author_name'] = names.get(item['author_identity_id'])
    owners = owner_names(db, versions)
    last = last_group_changes(db, workspace, group_ids={g['group_id'] for g in context['groups']})
    active = set(db.scalars(select(m.Job.read_metadata['input_snapshot']['planning_group_id'].as_string()).where(
        m.Job.workspace_id == workspace.id, m.Job.kind == 'generation', m.Job.status.in_(['queued', 'running']))))
    return {**fix_list, 'review_items': items, 'planning_context': {**context,
        'group_lookahead': settings_definition(db, workspace.department_id)['group_lookahead'],
        'groups': [{**g, 'adjustments': sum(g['group_id'] in item['group_ids'] for item in items
            if item['kind'] != 'acknowledged_gap'), **owners[g['group_version_id']],
            'last_change': last.get(g['group_id']), 'active_run': g['group_id'] in active} for g in context['groups']]}}


def owner_names(db, versions):
    ids = {ident for row in versions for ident in row['definition']['owner_identity_ids']}
    names = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(m.Identity.id.in_(ids))).all()) if ids else {}
    return {row['id']: {'owner_names': [names[i] for i in row['definition']['owner_identity_ids'] if i in names],
        'owner_name': ', '.join(names[i] for i in row['definition']['owner_identity_ids'] if i in names) or None}
        for row in versions}


def record_group_changes(db, workspace, change, before, data):
    """Attach slice provenance to the existing new ChangeSet; never rewrite historical evidence."""
    if access_record(db, workspace)['purpose'] != 'integrated':
        return
    from packages.domain.workspace_merge import definition, assignment, adjustment_key
    resolver = GroupResolver(data, load_groups(db, workspace.department_id, starts_on=workspace.starts_on, ends_on=workspace.ends_on))
    old = {row['id']: row for row in before['shifts']}
    focus = set(data['focus_ids'])
    current = {row['id']: row for row in data['shifts'] if row['id'] in focus}
    touched = set(change.before_snapshot.get('_planning_group_ids', []))
    def add(pid, shift, participant):
        primary = resolver.primary(pid, shift, participant)
        if primary:
            touched.add(primary['group_id'])
    for sid in old.keys() | current.keys():
        left, right = old.get(sid), current.get(sid)
        if left == right:
            continue
        if left is None or right is None or definition(left) != definition(right):
            for shift in (left, right):
                if shift:
                    for pid in data['people']:
                        add(pid, shift, None)
        else:
            a, b = ({p['person_id']: p for p in row['participants']} for row in (left, right))
            for pid in a.keys() | b.keys():
                if assignment(a.get(pid)) != assignment(b.get(pid)):
                    for shift, person in ((left, a.get(pid)), (right, b.get(pid))):
                        if person:
                            add(pid, shift, person)
    for field in ('rule_adjustments', 'target_overrides'):
        was = {adjustment_key(row): row for row in before['workspace'].get(field, [])}
        now = {adjustment_key(row): row for row in data['workspace'].get(field, [])}
        for key in was.keys() | now.keys():
            if was.get(key) == now.get(key):
                continue
            row = now.get(key) or was[key]
            interval = {'id': 'adjustment', '_date_projection': True,
                'start': local_midnight(date.fromisoformat(str(row.get('starts_on') or workspace.starts_on)), data['organisation']['timezone']),
                'end': local_midnight(date.fromisoformat(str(row.get('ends_on') or workspace.ends_on)), data['organisation']['timezone'])}
            for pid in [row['person_id']] if row.get('person_id') else data['people']:
                add(pid, interval, interval)
    change.before_snapshot = {**change.before_snapshot, '_planning_group_ids': sorted(touched)}


def last_group_changes(db, workspace, *, group_ids=None):
    """One aggregate query returns at most one change per group, without loading Undo snapshots."""
    from sqlalchemy import JSON, cast, literal, true
    if group_ids is not None and not group_ids:
        return {}
    c = m.ChangeSet
    path = c.before_snapshot['_planning_group_ids']
    empty = literal('[]') if db.bind.dialect.name == 'sqlite' else cast(literal('[]'), JSON)
    events = select(c.seq, c.actor_id, c.created_at,
        case((path.as_string().is_(None), empty), else_=path).label('groups')).where(c.workspace_id == workspace.id).cte()
    if db.bind.dialect.name == 'sqlite':
        expanded = func.json_each(events.c.groups).table_valued('value', joins_implicitly=True)
    else:
        expanded = func.json_array_elements_text(events.c.groups).table_valued('value').render_derived()
    latest = select(expanded.c.value.label('group_id'), func.max(events.c.seq).label('seq')).select_from(events.join(expanded, true()))
    if group_ids is not None:
        latest = latest.where(expanded.c.value.in_(group_ids))
    latest = latest.group_by(expanded.c.value).subquery()
    rows = db.execute(select(latest.c.group_id, m.Identity.display_name, c.created_at).select_from(latest)
        .join(c, c.seq == latest.c.seq).outerjoin(m.Identity, m.Identity.id == c.actor_id)).all()
    from packages.domain.time import aware
    return {gid: {'by_name': name, 'at': aware(at).isoformat()} for gid, name, at in rows}
