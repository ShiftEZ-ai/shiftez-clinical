"""Compact stored-check metadata; one owner for write-time and legacy streaming projection."""
from types import SimpleNamespace

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import object_session
from apps.api import models as m
from packages.domain.input_manifest import digest
from .planning import health
from .conditional_reads import revision_etag
from .read_projections import cached, source_identity, CANONICAL_AUDIENCE


def fast_etag(db, actor, workspace, *, include_check_state=False, include_job_state=False):
    """Build a draft-detail validator from bounded revision metadata.

    Draft detail is a large projection. Its mutable inputs all advance one of these
    row revisions or the organisation input counters, so a conditional read can stop
    before fingerprinting, wishes, review projections and session serialisation.
    """
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    validation_stamp = db.scalar(select(m.WorkspaceReadProjection.stamp).where(
        m.WorkspaceReadProjection.workspace_id == workspace.id,
        m.WorkspaceReadProjection.audience == CANONICAL_AUDIENCE,
        m.WorkspaceReadProjection.kind == 'stored_check'))
    if validation_stamp is None:
        # Legacy rows may lack the projection; use the writer's full validation stamp.
        validation_stamp = check_stamp(db, workspace)
    stamp = [workspace.revision, workspace.content_revision, workspace.status,
             validation_stamp, department.revision, department.catalogue_revision,
             department.leave_reason_display, organisation.revision,
             organisation.input_revision, organisation.availability_revision]
    if include_check_state:
        from .rota_summary import validation_job_stamp
        stamp.append(validation_job_stamp(db, workspace_id=workspace.id))
    if include_job_state:
        from .job_reads import job_stamp
        stamp.append(job_stamp(db, workspace_id=workspace.id))
    for cls, column in ((m.Approval, m.Approval.workspace_id),
                        (m.ReviewDecision, m.ReviewDecision.workspace_id),
                        (m.ChangeSet, m.ChangeSet.workspace_id),
                        (m.WorkspaceForkBase, m.WorkspaceForkBase.workspace_id),
                        (m.WorkspaceAccessVersion, m.WorkspaceAccessVersion.workspace_id),
                        (m.SwapRequest, m.SwapRequest.workspace_id),
                        (m.PublicationChangeList, m.PublicationChangeList.workspace_id)):
        stamp.append(tuple(db.execute(select(func.max(cls.revision), func.max(cls.created_at)).where(
            column == workspace.id)).one()))
    for cls in (m.AuditEvent, m.ReviewRoute, m.ReviewDelegation, m.Publication):
        query = select(func.max(cls.revision), func.max(cls.created_at)).where(
            cls.department_id == actor.department_id)
        if cls is m.AuditEvent:
            query = query.where(cls.target_id == workspace.id)
        stamp.append(tuple(db.execute(query).one()))
    parent = db.execute(select(m.Workspace.revision, m.Workspace.content_revision).join(
        m.WorkspaceForkBase, m.Workspace.id == m.WorkspaceForkBase.source_workspace_id).where(
            m.WorkspaceForkBase.workspace_id == workspace.id)).first()
    stamp.append(tuple(parent) if parent is not None else None)
    return revision_etag("workspace-detail", workspace.id, actor.identity_id, actor.person_id,
                        sorted(actor.roles), stamp)


def workspace_list_etag(db, actor, representation):
    """Revision validator for the paged workspace menu before health projections run."""
    department = db.get(m.Department, actor.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    from .job_reads import job_stamp
    workspace_stamp = db.execute(select(
        func.count(m.Workspace.id), func.coalesce(func.sum(m.Workspace.revision), 0),
        func.coalesce(func.sum(m.Workspace.content_revision), 0),
        func.max(m.Workspace.revision), func.max(m.Workspace.content_revision),
        func.max(m.Workspace.created_at),
        func.max(m.Workspace.starts_on), func.max(m.Workspace.ends_on)
    ).where(m.Workspace.department_id == actor.department_id)).one()
    publication_stamp = db.execute(select(func.count(m.Publication.id),
        func.coalesce(func.sum(m.Publication.revision), 0), func.max(m.Publication.revision),
        func.max(m.Publication.created_at)).where(m.Publication.department_id == actor.department_id)).one()
    change_stamp = db.execute(select(func.count(m.PublicationChangeList.id),
        func.coalesce(func.sum(m.PublicationChangeList.revision), 0),
        func.max(m.PublicationChangeList.revision), func.max(m.PublicationChangeList.created_at)).where(
            m.PublicationChangeList.department_id == actor.department_id)).one()
    fork_stamp = db.execute(select(func.count(m.WorkspaceForkBase.id),
        func.coalesce(func.sum(m.WorkspaceForkBase.revision), 0),
        func.max(m.WorkspaceForkBase.revision), func.max(m.WorkspaceForkBase.created_at)).where(
            m.WorkspaceForkBase.department_id == actor.department_id)).one()
    # Access versions are immutable history. Every access transition also
    # increments the owning workspace revision, so workspace_stamp already
    # invalidates this ETag; scanning the full access history here was a
    # repeated 3M-row read on large departments.
    from zoneinfo import ZoneInfo
    today = m.now().astimezone(ZoneInfo(organisation.timezone)).date()
    return revision_etag("workspace-list", actor.department_id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), representation,
                         (department.revision, department.catalogue_revision, organisation.revision,
                          organisation.input_revision, organisation.availability_revision,
                          today, tuple(workspace_stamp), job_stamp(db, department_id=actor.department_id), tuple(publication_stamp),
                          tuple(change_stamp), tuple(fork_stamp)))


def summary_etag(db, actor, workspace, view, group_version_id):
    """One-row validator for summary projections, before validation hydration."""
    row = db.execute(
        select(m.Workspace.revision, m.Workspace.content_revision, m.Workspace.status,
               m.Workspace.validation['evaluated_at'].as_string(),
               m.Workspace.validation['fingerprint'].as_string(),
               m.Department.revision, m.Department.catalogue_revision,
               m.Organisation.revision, m.Organisation.input_revision,
               m.Organisation.availability_revision)
        .join(m.Department, m.Department.id == m.Workspace.department_id)
        .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
        .where(m.Workspace.id == workspace.id)
    ).one()
    periods = db.execute(select(
        func.count(m.Workspace.id), func.coalesce(func.sum(m.Workspace.revision), 0),
        func.coalesce(func.sum(m.Workspace.content_revision), 0), func.max(m.Workspace.revision),
        func.max(m.Workspace.content_revision), func.max(m.Workspace.created_at)
    ).where(m.Workspace.department_id == workspace.department_id, m.Workspace.archived_at.is_(None))).one()
    from .operations import worker_state
    from .rota_summary import validation_job_stamp
    return revision_etag("workspace-summary", workspace.id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), view, group_version_id, tuple(row), tuple(periods),
                         validation_job_stamp(db, workspace_id=workspace.id),
                         worker_state(db, m.now())['worker_liveness'],
                         workspace.rule_adjustments or [], workspace.target_overrides or [],
                         workspace.accepted_issues or [])


def rule_map_etag(db, actor, workspace):
    """Revision validator for rule-map rechecks; no domain snapshot is built."""
    row = db.execute(
        select(m.Workspace.revision, m.Workspace.content_revision, m.Workspace.status,
               m.Workspace.validation['evaluated_at'].as_string(),
               m.Workspace.validation['fingerprint'].as_string(),
               m.Department.revision, m.Department.catalogue_revision,
               m.Organisation.revision, m.Organisation.input_revision,
               m.Organisation.availability_revision)
        .join(m.Department, m.Department.id == m.Workspace.department_id)
        .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
        .where(m.Workspace.id == workspace.id)
    ).one()
    proposals = db.execute(select(
        func.count(m.Policy.id), func.coalesce(func.sum(m.Policy.revision), 0),
        func.max(m.Policy.created_at), func.max(m.Policy.id)
    ).where(m.Policy.department_id == workspace.department_id, m.Policy.status == 'proposed')).one()
    return revision_etag("workspace-rule-map", workspace.id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), tuple(row), workspace.rule_adjustments or [],
                         workspace.target_overrides or [], tuple(proposals))


def publication_coverage_etag(db, actor, publication, workspace, organisation, department):
    """Revision validator for publication coverage before loading its snapshot."""
    return revision_etag("publication-coverage", publication.id, actor.identity_id, actor.person_id,
                         sorted(actor.roles), publication.revision, publication.effective_until,
                         workspace.revision, workspace.content_revision, department.revision,
                         organisation.revision, organisation.input_revision,
                         organisation.availability_revision)


def check_projection(workspace, check):
    from .acknowledged_gaps import review_items
    from apps.api.serialization import CHECK_SUMMARY
    manifest = (check or {}).get('input_manifest') or {}
    return {'checked': bool(check),
        'validation': {key: check[key] for key in CHECK_SUMMARY if key in (check or {})},
        'review_items': review_items(SimpleNamespace(id=workspace.id, validation=check,
            accepted_issues=workspace.accepted_issues, rule_adjustments=workspace.rule_adjustments,
            target_overrides=workspace.target_overrides), current=None if any(
                row.get('cause_id') for row in workspace.accepted_issues or []) else {'causes': []}),
        **{key: (check or {}).get(key) for key in ('fingerprint', 'evaluated_at')},
        'input_manifest': {key: manifest[key] for key in ('token', 'dirty_hint', 'components') if key in manifest},
        'health': health(SimpleNamespace(validation=check, accepted_issues=workspace.accepted_issues))}


def check_stamp(db, workspace):
    # Deferred collection reads must reuse the compact projection without hydrating
    # the full validation JSON; write listeners still include the changed value.
    if 'validation' not in workspace.__dict__:
        stamp = db.scalar(select(m.WorkspaceReadProjection.stamp).where(
            m.WorkspaceReadProjection.workspace_id == workspace.id,
            m.WorkspaceReadProjection.audience == CANONICAL_AUDIENCE,
            m.WorkspaceReadProjection.kind == 'stored_check'))
        if stamp is not None:
            return stamp
    return digest([source_identity(db), workspace.revision, workspace.accepted_issues,
                   workspace.validation])


def stored_checks(db, workspaces):
    workspaces = {w.id: w for w in workspaces}
    def acquire(ids):
        result = {wid: check_projection(workspaces[wid], workspaces[wid].validation) for wid in ids
                  if 'validation' in workspaces[wid].__dict__}
        missing = set(ids) - result.keys()
        if missing:
            rows = db.execute(select(m.Workspace.id, m.Workspace.validation).where(
                m.Workspace.id.in_(missing)).execution_options(yield_per=1))
            for wid, check in rows:
                result[wid] = check_projection(workspaces[wid], check)
        return result
    return cached(db, workspaces, 'stored_check',
                  {wid: check_stamp(db, w) for wid, w in workspaces.items()}, build_many=acquire)


def store_check(_mapper, connection, workspace):
    # Only already-loaded checks participate; metadata edits must not hydrate a large deferred check.
    state = inspect(workspace)
    if 'validation' not in workspace.__dict__:
        return
    db = object_session(workspace)
    if connection.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    C = m.WorkspaceReadProjection.__table__
    value = dict(workspace_id=workspace.id, audience=CANONICAL_AUDIENCE, kind='stored_check',
                 stamp=check_stamp(db, workspace), value=check_projection(workspace, workspace.validation))
    connection.execute(insert(C).values(**value).on_conflict_do_update(
        index_elements=['workspace_id', 'audience', 'kind'], set_={key: value[key] for key in ('stamp', 'value')}))
    if state.attrs.validation.history.has_changes() or state.attrs.accepted_issues.history.has_changes():
        connection.execute(C.delete().where(C.c.workspace_id == workspace.id, C.c.kind == 'warm'))
