"""One visibility boundary for the entire workspace graph, including aggregate reads."""
from contextlib import contextmanager

from sqlalchemy import event, exists, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from apps.api import models as m
from apps.api.errors import require


@contextmanager
def shared_scope(db, actor):
    """Private drafts and their policy/history graph never enter a shared cache."""
    if db is None:
        yield
        return
    previous = {key: db.info.get(key) for key in ('workspace_actor', 'workspace_shared_only')}
    db.info.update(workspace_actor=actor, workspace_shared_only=True)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                db.info.pop(key, None)
            else:
                db.info[key] = value


def _visibility_clause(workspace, identity_id=None, *, correlate_workspace=False):
    access = m.WorkspaceAccessVersion.__table__
    allowed = access.c.visibility == 'shared'
    if identity_id is not None:
        allowed = or_(allowed, workspace.created_by == identity_id)
    current = exists(select(1).where(access.c.id == workspace.access_version_id,
                                    access.c.workspace_id == workspace.id, allowed))
    if correlate_workspace:
        current = current.correlate_except(access)
    return or_(workspace.access_version_id.is_(None), current)


def shared_clause(workspace=m.Workspace):
    return _visibility_clause(workspace)


def visible_ids(identity_id, planner):
    workspace = m.Workspace.__table__.c
    return select(workspace.id).where(_visibility_clause(workspace, identity_id if planner else None))


def _workspace_graph_criteria(identity_id, planner, shift_workspace_id=None):
    ids = visible_ids(identity_id, planner)
    workspace_clause = (
        (lambda workspace: _visibility_clause(workspace, identity_id, correlate_workspace=True)) if planner
        else (lambda workspace: _visibility_clause(workspace, correlate_workspace=True)))
    criteria = [with_loader_criteria(m.Workspace, workspace_clause, include_aliases=True)]
    for mapper in m.Base.registry.mappers:
        cls = mapper.class_
        if 'workspace_id' in mapper.columns:
            column = getattr(cls, 'workspace_id')
            criteria.append(with_loader_criteria(cls, or_(column.is_(None), column.in_(ids)), include_aliases=True))
    criteria.append(with_loader_criteria(m.WorkspaceMergeProposal,
        m.WorkspaceMergeProposal.source_workspace_id.in_(ids), include_aliases=True))
    shift = m.Shift.__table__
    shifts = select(shift.c.id).where(shift.c.workspace_id.in_(ids))
    if shift_workspace_id is not None:
        shifts = shifts.where(shift.c.workspace_id == shift_workspace_id)
    for cls in (m.Assignment, m.ShiftService):
        criteria.append(with_loader_criteria(cls, cls.shift_id.in_(shifts), include_aliases=True))
    criteria.append(with_loader_criteria(m.CommandReceipt,
        or_(m.CommandReceipt.private_owner_id.is_(None), m.CommandReceipt.private_owner_id == identity_id), include_aliases=True))
    for cls, paths in ((m.Policy, (m.Policy.definition['workspace_id'].as_string(),)),
                       (m.DefinitionHistory, (m.DefinitionHistory.snapshot['workspace_id'].as_string(),
                                              m.DefinitionHistory.snapshot['definition']['workspace_id'].as_string()))):
        for column in paths:
            criteria.append(with_loader_criteria(cls, or_(column.is_(None), column.in_(ids)), include_aliases=True))
    return tuple(criteria)


@event.listens_for(Session, 'do_orm_execute')
def filter_workspace_graph(state):
    """Authenticated product sessions are filtered before joins/counts/pagination; workers retain system access."""
    info = state.session.info
    actor = info.get('workspace_actor')
    if not state.is_select or actor is None:
        return
    key = (actor.identity_id, actor.permits('coordinator') and not info.get('workspace_shared_only'),
           state.execution_options.get('shift_workspace_id'))
    memo = info.get('_request_workspace_criteria')
    criteria = memo.get(key) if memo is not None else None
    if criteria is None:
        criteria = _workspace_graph_criteria(*key)
        if memo is not None:
            # The predicates still query live access versions on every SELECT.
            memo[key] = criteria
    state.statement = state.statement.options(*criteria)


@event.listens_for(Session, 'before_flush')
def private_audit_envelope(db, _context, _instances):
    if not db.info.get('private_workspace_ids'):
        return
    actor = db.info.get('workspace_actor')
    for row in db.new:
        if isinstance(row, m.CommandReceipt) and actor:
            row.private_owner_id = actor.identity_id


def access_record(db, workspace):
    if workspace.access_version_id is None:
        from .planning_groups import default_rota_id
        purpose = 'integrated' if (workspace.base_publication_id is None and workspace.archived_at is None
            and workspace.status in ('draft', 'submitted', 'approved') and default_rota_id(db, workspace) == workspace.id) else 'alternative'
        return {'visibility': 'shared', 'purpose': purpose, 'owner_identity_id': workspace.created_by,
                'access_version_id': None, 'access_revision': 0}
    row = workspace.access_version
    require(row is not None and row.workspace_id == workspace.id, 'WORKSPACE_ACCESS_INVALID',
            'Workspace access must be repaired before continuing', 409)
    return {'visibility': row.visibility, 'purpose': row.purpose, 'owner_identity_id': workspace.created_by,
            'access_version_id': row.id, 'access_revision': row.revision}


def require_visible(db, actor, workspace):
    access = access_record(db, workspace)
    require(access['visibility'] == 'shared' or workspace.created_by == actor.identity_id and actor.permits('coordinator'),
            'NOT_FOUND', 'Record not found', 404)
    return access


def require_edit(db, actor, workspace):
    access = require_visible(db, actor, workspace)
    require(access['purpose'] != 'sandbox' or workspace.created_by == actor.identity_id,
            'DRAFT_OWNER_ONLY', 'Only the creator can edit this sandbox; make your own copy', 403)
    if access['visibility'] == 'private':
        db.info.setdefault('private_workspace_ids', set()).add(workspace.id)
    return access


def append_access(db, actor, workspace, visibility, purpose, reason):
    previous = access_record(db, workspace)
    require(previous['visibility'] != 'shared' or visibility == 'shared' or workspace.access_version_id is None,
            'SHARING_ONE_WAY', 'Shared drafts cannot become private', 409)
    row = m.WorkspaceAccessVersion(id=m.uid(), workspace_id=workspace.id,
        revision=previous['access_revision'] + 1, visibility=visibility, purpose=purpose,
        previous_id=workspace.access_version_id, actor_id=actor.identity_id, reason=reason)
    db.add(row)
    db.flush()
    db.info.pop('default_rotas', None)
    workspace.access_version_id = row.id
    workspace.access_version = row
    if visibility == 'private':
        db.info.setdefault('private_workspace_ids', set()).add(workspace.id)
    db.flush()
    return access_record(db, workspace)


def private_operation(db, command, result):
    workspace_id = ((result.get('workspace') or {}).get('id') or (result.get('job') or {}).get('workspace_id')
                    or (result.get('record') or {}).get('workspace_id') or command.payload.get('workspace_id'))
    if not workspace_id and command.kind.startswith('workspace.'):
        workspace_id = command.target_id
    if command.kind == 'workspace.merge.preview':
        workspace_id = command.payload.get('source_workspace_id')
    if command.kind == 'workspace.merge.apply' and result.get('job'):
        proposal = db.get(m.WorkspaceMergeProposal, command.payload.get('merge_id'))
        workspace_id = proposal.source_workspace_id if proposal else None
    workspace = db.get(m.Workspace, workspace_id) if workspace_id else None
    private = workspace is not None and access_record(db, workspace)['visibility'] == 'private'
    if private:
        db.info.setdefault('private_workspace_ids', set()).add(workspace.id)
    return private or command.kind in {'bulk.apply', 'workspace.archive_due'} and bool(db.info.get('private_workspace_ids'))


def audit_envelope(db, command, result, event):
    """Minimize this private operation only; mixed batches retain unrelated governance evidence."""
    if private_operation(db, command, result):
        event.reason = 'Private draft operation'
        event.detail = {'private': True,
                        **({'assignment_override_blockers': event.detail['assignment_override_blockers']}
                           if event.detail.get('assignment_override_blockers') else {}),
                        **({'self_approved': event.detail['self_approved']}
                           if event.detail.get('self_approved') else {})}
    return event
