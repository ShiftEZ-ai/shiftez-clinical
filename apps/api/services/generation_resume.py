"""Keep only the current generation's private, disposable preparation and hint."""
from datetime import timedelta

from sqlalchemy import and_, exists, func, not_, null, or_, select, update
from sqlalchemy.orm import aliased, load_only

from apps.api import models as m
from apps.api.auth import current_roles
from packages.domain.time import aware


RESUMABLE_GENERATION_RESULTS = frozenset({'needs_more_time', 'unknown_time_limit'})
RESUMABLE_GENERATION_STOP_REASONS = frozenset({'time_limit', 'plateau', 'target_gap', 'target_score', 'stop_by', 'slice_limit', 'operator_stop'})
RECOVERABLE_RUN_FAILURES = frozenset({'WORKER_LEASE_EXPIRED', 'STALE_INPUTS', 'ACCESS_REVOKED'})
GENERATION_RUN_ERROR_RESULTS = frozenset({'invalid_inputs', 'model_invalid', 'translation_error', 'implementation_error'})


def generation_resume_days(db):
    from apps.api.settings import Settings
    return (db.get_bind().get_execution_options().get('rota_write_settings') or Settings.from_env()).generation_resume_days


def has_generation_checkpoint(job):
    """A compact hint plus its prepared model is enough to continue any terminal run."""
    result = job.result or {}
    checkpoint = job.resume_checkpoint or {}
    return bool(job.kind == 'generation' and job.resume_state and checkpoint
                and (result.get('checkpointed') or checkpoint.get('checkpoint_assignments')
                     or checkpoint.get('has_solution') or checkpoint.get('hint')
                     or checkpoint.get('strict_conflict')))


def may_run(db, identity, job):
    """Current requester access: planners' checks/solves, or staff's open-offer audience work."""
    roles = current_roles(db, identity.id, job.department_id)
    if job.kind == "swap_audience":
        from apps.api.services.swaps import staff_identity
        return bool(roles & {"coordinator", "approver"} or
                    staff_identity(db, identity.id, identity.person_id, job.department_id))
    return "coordinator" in roles or (job.kind in {"feasibility", "catalogue_proof"} and "approver" in roles)


def generation_resume_available(db, job, *, days, error=None, expired=None, completed_before_cancel=False):
    """Whether this job still owns private preparation that its requester may continue."""
    if job.kind != 'generation' or not (job.resume_state and job.resume_checkpoint):
        return False
    identity = db.get(m.Identity, job.requested_by)
    if not identity or identity.disabled or not may_run(db, identity, job):
        return False
    if expired is None:
        expired = resume_expired(job, days)
    if expired or not resume_current(db, job):
        return False
    result = job.result or {}
    if error is not None and error not in RECOVERABLE_RUN_FAILURES:
        return False
    if result.get('status') in GENERATION_RUN_ERROR_RESULTS:
        return False
    completed_proposal = (not result.get('checkpointed')
                          and result.get('status') in {'candidate_feasible', 'candidate_with_gaps'}
                          and result.get('input_fingerprint') == job.input_fingerprint)
    return not completed_proposal and not completed_before_cancel


def clear_generation_resume(db, *, workspace_id=None, organisation_id=None, job_id=None, keep_job_id=None):
    conditions = [m.Job.kind == 'generation', or_(m.Job.resume_state.is_not(None), m.Job.resume_checkpoint.is_not(None))]
    if workspace_id is not None:
        conditions.append(m.Job.workspace_id == workspace_id)
    if organisation_id is not None:
        conditions.append(m.Job.department_id.in_(select(m.Department.id).where(m.Department.organisation_id == organisation_id)))
    if job_id is not None:
        conditions.append(m.Job.id == job_id)
    if keep_job_id is not None:
        conditions.append(m.Job.id != keep_job_id)
    return db.execute(update(m.Job).where(*conditions).values(resume_state=null(), resume_checkpoint=null())
                      .execution_options(synchronize_session='fetch')).rowcount


def generation_created_at(db, workspace_id):
    # A fixed or coarse clock must not make the previous attempt look newer than its successor.
    previous = db.scalar(select(func.max(m.Job.created_at)).where(m.Job.workspace_id == workspace_id,
                                                                 m.Job.kind == 'generation'))
    now = m.now()
    return max(now, aware(previous) + timedelta(microseconds=1)) if previous else now


def resume_expired(job, days):
    return aware(job.completed_at or job.created_at) < m.now() - timedelta(days=days)


def generation_admitted(job):
    return job.input_snapshot['_generation_authority_pending'].as_boolean().is_not(True)


def resume_current(db, job, *, days=None):
    if days is not None and resume_expired(job, days):
        return False
    latest = db.scalar(select(m.Job.id).where(m.Job.workspace_id == job.workspace_id, m.Job.kind == 'generation',
        or_(m.Job.id == job.id, generation_admitted(m.Job)))
        .order_by(m.Job.created_at.desc(), m.Job.seq.desc()).limit(1))
    if latest != job.id:
        return False
    workspace = db.scalar(select(m.Workspace).where(m.Workspace.id == job.workspace_id).options(load_only(
        m.Workspace.status, m.Workspace.archived_at, m.Workspace.department_id,
        m.Workspace.content_revision, m.Workspace.base_publication_id)))
    # Frozen preparation remains useful after input/content edits; only run authority expires here.
    return bool(workspace and workspace.status == 'draft' and workspace.archived_at is None)


def prune_generation_resume(db, *, days, limit=None):
    newer = aliased(m.Job)
    superseded = exists(select(newer.id).where(newer.workspace_id == m.Job.workspace_id, newer.kind == 'generation',
        generation_admitted(newer), or_(newer.created_at > m.Job.created_at,
            and_(newer.created_at == m.Job.created_at, newer.seq > m.Job.seq))))
    invalid_workspace = m.Job.workspace_id.in_(select(m.Workspace.id).where(or_(
        m.Workspace.status != 'draft', m.Workspace.archived_at.is_not(None))))
    checkpoint_candidate = and_(m.Job.resume_state.is_not(None), m.Job.resume_checkpoint.is_not(None), or_(
        m.Job.result['checkpointed'].as_boolean().is_(True),
        func.coalesce(m.Job.result['status'].as_string(), '').in_(('candidate_feasible', 'candidate_with_gaps'))))
    checkpoint_candidate = or_(checkpoint_candidate,
        and_(m.Job.resume_state.is_not(None), m.Job.resume_checkpoint.is_not(None),
             m.Job.resume_checkpoint['strict_conflict']['strict_status'].as_string() == 'INFEASIBLE'))
    terminal = and_(m.Job.status.not_in(['queued', 'running']), not_(checkpoint_candidate),
        func.coalesce(m.Job.result['status'].as_string(), '').not_in(RESUMABLE_GENERATION_RESULTS))
    expired = func.coalesce(m.Job.completed_at, m.Job.created_at) < m.now() - timedelta(days=days)
    eligible = m.Job.kind == 'generation'
    eligible = and_(eligible, or_(m.Job.resume_state.is_not(None), m.Job.resume_checkpoint.is_not(None)),
                    or_(expired, terminal, superseded, invalid_workspace))
    if limit is not None:
        selected = select(m.Job.id).where(eligible).order_by(m.Job.created_at, m.Job.id).limit(limit)
        eligible = and_(m.Job.id.in_(selected), eligible)
    return db.execute(update(m.Job).where(eligible).values(resume_state=null(), resume_checkpoint=null())
        .execution_options(synchronize_session='fetch')).rowcount
