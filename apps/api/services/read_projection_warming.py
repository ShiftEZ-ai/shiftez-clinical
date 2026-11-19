"""Warm one stale workspace per pass; unchanged successes and cooling failures do no work."""
import logging
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from apps.api import models as m
from apps.api.db import DatabaseWriteDeferred, DatabaseWriteTimeout, transaction
from apps.api.settings import Settings, quota_policy_limit
from packages.domain.input_manifest import digest, toolchain
from .read_projections import input_stamp, source_identity, CANONICAL_AUDIENCE

log = logging.getLogger('rota.worker')


def warm_one(engine, *, cursor_key='workspaces', blocking=False, settings=None):
    settings = settings or engine.get_execution_options().get('rota_write_settings') or Settings()
    reader = engine.execution_options(isolation_level='REPEATABLE READ') if engine.dialect.name == 'postgresql' else engine
    failed, report_failure = None, False
    with transaction(reader) as db:
        W, D, O, C = m.Workspace, m.Department, m.Organisation, m.WorkspaceReadProjection
        source = source_identity(db)
        compiler = digest(toolchain(quota_policy_limit()))
        fields = {'input': O.input_revision, 'availability': O.availability_revision,
                  'workspace': W.revision, 'content': W.content_revision, 'department': D.revision}
        # A source digest changes on every deployment, including API-only
        # changes that cannot alter evaluator output.  The release-versioned
        # toolchain is the durable projection contract; keep warm rows across
        # deploys and rebuild only when that contract or data revisions move.
        # A missing source is retryable state for a row that failed before its source was captured;
        # a valid stored source remains warm across deployments.
        failure = C.value['failure']
        retry_at = failure['retry_at'].as_float()
        retry_due = retry_at <= m.now().timestamp()
        source_missing = C.value['source'].as_string().is_(None)
        stale = [C.stamp.is_(None), C.value.is_(None),
                 source_missing if blocking else and_(source_missing, or_(retry_at.is_(None), retry_due)),
                 C.value['toolchain'].as_string().is_distinct_from(compiler)]
        stale += [C.value[key].as_integer().is_distinct_from(column) for key, column in fields.items()]
        # Failure receipts are disposable maintenance state, never successful read projections.
        stale.append(failure['error_code'].as_string().is_not(None) if blocking else
                     retry_due)
        query = select(W).options(defer(W.validation), defer(W.review_snapshot)).join(D).join(O).outerjoin(C,
            (C.workspace_id == W.id) & (C.audience == CANONICAL_AUDIENCE) & (C.kind == 'warm')).where(
            W.archived_at.is_(None), or_(*stale)).order_by(W.id).limit(1)
        workspace = db.scalar(query)
        if workspace is None:
            return False
        if engine.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        # Only background warmers share this row; no interactive writer waits on it.
        Cursor = m.ReadProjectionWarmCursor
        if db.get(Cursor, cursor_key) is None:
            db.execute(insert(Cursor).values(key=cursor_key).on_conflict_do_nothing(index_elements=['key']))
        if db.scalar(select(Cursor).where(Cursor.key == cursor_key).with_for_update(skip_locked=not blocking)) is None:
            return False
        stamp = input_stamp(db, workspace)
        department = db.get(D, workspace.department_id)
        organisation = db.get(O, department.organisation_id)
        revisions = {'input': organisation.input_revision, 'availability': organisation.availability_revision,
            'workspace': workspace.revision, 'content': workspace.content_revision, 'department': department.revision,
            'source': source, 'toolchain': compiler}
        previous = db.scalar(select(C.value).where(C.workspace_id == workspace.id,
            C.audience == CANONICAL_AUDIENCE, C.kind == 'warm')) or {}
        previous_failure = (previous.get('failure') or {}) if all(
            previous.get(key) == value for key, value in revisions.items()) else {}
        if blocking and previous_failure.get('retry_at', 0) > m.now().timestamp():
            raise RuntimeError(f"Read projection warming failed for {workspace.id} "
                               f"({previous_failure['projection_kind']}: {previous_failure['error_code']}); retry pending")
        from .workspace_reads import stored_checks
        from .rota_summary import summary_check
        from apps.api.snapshots import fingerprints
        stage = 'source_identity'
        try:
            # Roll back partial local cache writes, but commit a failed-attempt receipt outside the savepoint.
            with db.begin_nested():
                if source is None:
                    raise RuntimeError('Source changed since startup; restart before warming projections')
                stage = 'stored_check'
                check = stored_checks(db, [workspace])
                stage = 'fingerprint'
                fingerprints(db, [workspace], checks=check)
                stage = 'summary_check'
                summary_check(db, workspace)
        except (SQLAlchemyError, DatabaseWriteDeferred, DatabaseWriteTimeout):
            raise  # Connection/transaction failures belong to the worker's database retry policy.
        except Exception as exc:
            failed = (type(exc), exc, exc.__traceback__)
            origin = exc.__traceback__
            while origin.tb_next is not None:
                origin = origin.tb_next
            location = {'module': origin.tb_frame.f_globals.get('__name__'),
                        'function': origin.tb_frame.f_code.co_name, 'line': origin.tb_lineno}
            identity = digest([stage, type(exc).__name__, location])
            retry_seconds = min(settings.worker_projection_retry_max_seconds,
                max(settings.worker_projection_retry_seconds, previous_failure.get('retry_seconds', 0) * 2))
            report_failure = identity != previous_failure.get('identity')
            revisions['failure'] = {'identity': identity, 'error_code': type(exc).__name__,
                'projection_kind': stage, 'error_location': location, 'retry_seconds': retry_seconds,
                'retry_at': m.now().timestamp() + retry_seconds}
        if db.info.get('read_projection_store_skipped'):
            # A reader that lost the SQLite write race cannot publish a successful warm receipt.
            if blocking:
                raise RuntimeError('Read projection warming could not store a projection')
            return False
        value = dict(workspace_id=workspace.id, audience=CANONICAL_AUDIENCE, kind='warm', stamp=stamp, value=revisions)
        db.execute(insert(C).values(**value).on_conflict_do_update(
            index_elements=['workspace_id', 'audience', 'kind'], set_={'stamp': stamp, 'value': revisions}))
    if report_failure:
        log.warning('read_projection_warm_failed', exc_info=failed, extra={
            'workspace_id': workspace.id, 'projection_kind': stage, 'input_stamp': stamp,
            **{key: revisions['failure'][key] for key in ('error_code', 'error_location', 'retry_seconds')}})
    if failed and blocking:
        raise failed[1].with_traceback(failed[2])
    return failed is None


def warm_all(engine, *, settings=None):
    """Drain current stale revisions; subsequent calls are read-only until inputs move."""
    count = 0
    while warm_one(engine, cursor_key='deployment', blocking=True, settings=settings):
        count += 1
    return count
