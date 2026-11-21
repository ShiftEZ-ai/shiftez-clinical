"""Small first read and windowed detail, using the existing review/check owners."""
from sqlalchemy import or_, select
from sqlalchemy.orm import load_only

from apps.api import models as m
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import fingerprints, project_series_shifts, shift_records
from packages.domain.time import aware
from .common import local_date_span, visible_rota_name
from . import reviews
from .planning import allowed_actions, redo_available
from .planning_groups import unrestricted
from .workspace_access import access_record
from .workspace_reads import stored_checks


def full_shifts(db, workspace_id):
    shift = m.Shift
    # Mapped columns retain workspace visibility criteria without creating Shift instances.
    columns = [getattr(shift, key) for key in shift.__mapper__.column_attrs.keys()]
    rows = db.execute(select(*columns).where(shift.workspace_id == workspace_id)
                      .order_by(shift.start, shift.id)).mappings()
    return shift_records(db, rows)


def compact(db, actor, workspace):
    from apps.api.workspace_baselines import attach as attach_baseline
    check = stored_checks(db, [workspace])[workspace.id]
    current = fingerprints(db, [workspace], checks={workspace.id: check}, reload=False)[workspace.id]
    org = db.get(m.Organisation, actor.organisation_id)
    metadata = attach_baseline(db, workspace, record(workspace, exclude=('validation', 'review_snapshot')))
    from .people import with_adjustment_names
    metadata = with_adjustment_names(db, actor, metadata)
    publication_id = db.scalar(select(m.Publication.id).where(m.Publication.workspace_id == workspace.id))
    historical = db.scalar(select(m.AuditEvent.id).where(
        m.AuditEvent.department_id == actor.department_id, m.AuditEvent.target_id == workspace.id,
        m.AuditEvent.action.in_({'workspace.submit', 'workspace.publish'})).limit(1)) is not None
    metadata.update(name=visible_rota_name(db, actor, workspace.id, workspace.name),
        publication_id=publication_id, validation=check['validation'],
        allowed_actions=allowed_actions(db, actor, workspace, historical=historical,
                                        published=publication_id is not None))
    fork = db.scalar(select(m.WorkspaceForkBase).options(load_only(m.WorkspaceForkBase.id,
        m.WorkspaceForkBase.source_workspace_id, m.WorkspaceForkBase.source_revision,
        m.WorkspaceForkBase.source_content_revision)).where(m.WorkspaceForkBase.workspace_id == workspace.id))
    if fork:
        parent_revision = db.scalar(select(m.Workspace.revision).where(m.Workspace.id == fork.source_workspace_id))
        metadata['fork_base'] = {'id': fork.id, 'source_workspace_id': fork.source_workspace_id,
            'source_revision': fork.source_revision, 'source_content_revision': fork.source_content_revision,
            'parent_changed_since': max(0, (parent_revision or fork.source_revision) - fork.source_revision)}
    health = {**check['health'], 'fresh': bool(check['checked'] and check['fingerprint'] == current)}
    return {'view': 'compact', 'workspace': metadata,
        'current_input_revision': org.input_revision, 'current_availability_revision': org.availability_revision,
        'current_fingerprint': current, 'health': health, 'summary': health.get('summary'),
        'review_progress': reviews.viewer_progress(db, actor, workspace, current_fingerprint=current),
        'review_items': check['review_items'], 'reviewer_involved': reviews.contributed(db, workspace, actor.identity_id),
        'can_change_fixed': actor.permits('coordinator') and unrestricted(db, actor, access_record(db, workspace)),
        'can_redo': actor.permits('coordinator') and redo_available(db, actor, workspace),
        'swap_id': db.scalar(select(m.SwapRequest.id).where(m.SwapRequest.workspace_id == workspace.id)),
        'approvals': [record(row) for row in db.scalars(select(m.Approval).where(m.Approval.workspace_id == workspace.id))]}


def part(db, actor, workspace, query, settings):
    require(0 < (query.end - query.start).days <= settings.workspace_read_max_days,
            'WORKSPACE_READ_WINDOW', f'Choose 1 to {settings.workspace_read_max_days} days', 422)
    limit = query.limit or settings.workspace_read_page_size
    require(limit <= settings.workspace_read_page_size, 'WORKSPACE_READ_LIMIT',
            f'Read at most {settings.workspace_read_page_size} items at a time', 422)
    org = db.get(m.Organisation, actor.organisation_id)
    lo, hi = local_date_span(query.start, query.end, org.timezone)
    shifts = select(m.Shift).where(m.Shift.workspace_id == workspace.id, m.Shift.start < hi, m.Shift.end > lo)
    coverage = []
    if query.part == 'shifts':
        from .series_conflicts import session_actions
        selected = list(db.scalars(shifts.order_by(m.Shift.start, m.Shift.id)))
        focus = {row.id for row in selected}
        peers = []
        if selected:
            first, last = min(aware(row.start) for row in selected), max(aware(row.end) for row in selected)
            if first < lo or last > hi:
                # Overnight tails can meet an own session outside the displayed day, just as external ones can.
                peers = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace.id,
                    m.Shift.start < last, m.Shift.end > first, or_(m.Shift.start >= hi, m.Shift.end <= lo))))
        # Project all overlapping sessions before paging so a page edge cannot hide a conflict.
        projected = project_series_shifts(db, workspace, shift_records(db, [*selected, *peers]), window=(lo, hi))
        rows = [row for row in projected if row['id'] in focus]
        total = len(rows)
        from .people import duty_ranks
        items = duty_ranks(db, actor.department_id, rows[query.offset:query.offset + limit], org.timezone)
        items = session_actions(db, actor, items)
    else:
        from .rota_summary import workspace_validation
        from .series_conflicts import finding_actions
        from .leave_disclosure import disclose_leave
        from packages.domain.validation import finding_observations
        ids = set(db.scalars(shifts.with_only_columns(m.Shift.id)))

        def in_window(finding):
            for row in finding_observations({'findings': [finding]}):
                related = set((row.get('detail') or {}).get('affected_shift_ids') or [])
                if row.get('shift_id'):
                    related.add(row['shift_id'])
                if related:
                    if related & ids:
                        return True
                elif not row.get('start') or not row.get('end') or (aware(row['start']) < hi and aware(row['end']) > lo):
                    return True  # Unlocated, rota-wide findings must never disappear.
            return False

        validation = workspace_validation(db, workspace) or {}
        rows = [row for row in validation.get('findings', []) if in_window(row)]
        coverage = [row for row in validation.get('coverage', []) if row['shift_id'] in ids
            and (not row.get('start') or not row.get('end') or (aware(row['start']) < hi and aware(row['end']) > lo))]
        total = len(rows)
        projected = finding_actions(db, actor, {'findings': rows[query.offset:query.offset + limit]})
        disclose_leave(db, actor, projected)
        items = projected['findings']
    return {'workspace_id': workspace.id, 'revision': workspace.revision, 'part': query.part,
        'start': query.start, 'end': query.end, 'items': items, 'total': total, 'offset': query.offset,
        'limit': limit, 'has_more': query.offset + len(items) < total,
        'coverage': coverage[query.offset:query.offset + limit], 'coverage_total': len(coverage),
        'coverage_has_more': query.offset + limit < len(coverage)}
