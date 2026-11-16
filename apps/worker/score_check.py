"""One leased file per job; paid attempts are reserved durably and never replayed."""
import json
import logging
import re
import threading
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from apps.api import live, models as m
from apps.api.auth import Actor, current_roles
from apps.api.db import DatabaseWriteDeferred, retry_write, transaction
from apps.api.errors import require
from apps.api.settings import Settings
from apps.api.services import score_check_provider as provider
from packages.contracts import schemas as s
from packages.domain.time import aware
from packages.domain.worker_contract import JOB_LEASE_SECONDS

log = logging.getLogger('rota.worker')


def _actor(db, job):
    identity = db.get(m.Identity, job.requested_by)
    department = db.get(m.Department, job.department_id)
    require(identity is not None and not identity.disabled and department is not None,
            'ACCESS_REVOKED', 'Access has been revoked', 403)
    roles = current_roles(db, identity.id, department.id)
    required = s.PERMISSIONS['ai.providers'] if job.kind == 'test' else ('coordinator',)
    require(bool(roles.intersection(required)), 'ACCESS_REVOKED', 'Access has been revoked', 403)
    return Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                 roles, identity.person_id, '')


def _context(db, job):
    from apps.api.services.score_check import require_enabled
    require_enabled(db, job.department_id)
    actor = _actor(db, job)
    require(db.get(m.Department, job.department_id).ai_enabled,
            'SCORE_AI_DISABLED', 'AI reading is off for this department', 409)
    profile = db.get(m.AiProviderProfile, job.profile_id)
    require(profile is not None and profile.department_id == job.department_id
            and profile.enabled and profile.archived_at is None and profile.key_ciphertext,
            'SCORE_PROVIDER_DISABLED', 'AI reading is off for this provider', 409)
    require(job.profile_revision == profile.revision,
            'STALE_REVISION', 'The provider changed; acknowledge it before a new reading or test', 409)
    source, batch = None, None
    if job.kind == 'extraction':
        source = db.get(m.ImportSource, job.source_id)
        batch = db.get(m.ScoreCheckImportBatch, source.batch_id) if source else None
        require(source is not None and batch is not None and source.department_id == batch.department_id == job.department_id
                and source.extraction_job_id == job.id and source.removed_at is None
                and batch.status not in {'saved', 'discarded'},
                'SCORE_SOURCE_CHANGED', 'The reading job no longer belongs to this file', 409)
        require(job.privacy_acknowledged_at is not None,
                'SCORE_AI_CONSENT', 'Acknowledge the current provider before reading this file', 409)
    return actor, profile, source, batch


def _current(db, claim):
    job = db.get(m.ScoreCheckExtractionJob, claim['id'])
    require(job is not None and job.state == 'running' and job.claim_token == claim['token']
            and job.lease_expires_at is not None and aware(job.lease_expires_at) > m.now(),
            'SCORE_JOB_LEASE', 'The file reading lease ended', 409)
    return job


def _store(db, job, extraction, error):
    if error:
        for call in db.scalars(select(m.AiCall).where(m.AiCall.job_id == job.id, m.AiCall.result == 'pending')):
            call.result, call.error_code, call.completed_at = 'error', error, m.now()
    if job.kind == 'extraction':
        source = db.get(m.ImportSource, job.source_id)
        batch = db.get(m.ScoreCheckImportBatch, source.batch_id) if source else None
        if (source is not None and source.extraction_job_id == job.id and source.removed_at is None
                and batch is not None and batch.status not in {'saved', 'discarded'}):
            source.error_code = error
            if error:
                source.status = 'failed'
            else:
                source.extraction = extraction
                source.status = 'needs_review' if extraction.get('unparsed') or extraction.get('warnings') else 'ready'
            source.revision += 1
            from apps.api.services import score_check
            score_check.refresh(db, batch)
    job.state, job.error_code, job.completed_at = ('failed' if error else 'completed'), error, m.now()
    job.lease_expires_at = None
    if job.kind == 'test':
        job.result = {'ok': not error, 'message': ('Connection test failed. Check the provider settings.'
                      if error else 'Connection successful.')}
    else:
        job.result = {} if error else {'assignments': len(extraction.get('assignments', [])),
                                      'unparsed': len(extraction.get('unparsed', []))}
    job.revision += 1
    live.signal(db, live.job_topic(job.department_id, job.id), scope="jobs", id=job.id)


def claim_job(db):
    enabled = select(m.Department.id).where(m.Department.score_check_enabled.is_(True))
    expired = db.scalars(select(m.ScoreCheckExtractionJob).where(m.ScoreCheckExtractionJob.department_id.in_(enabled),
        m.ScoreCheckExtractionJob.state == 'running',
        m.ScoreCheckExtractionJob.lease_expires_at <= m.now()).order_by(m.ScoreCheckExtractionJob.created_at)
        .limit((db.get_bind().get_execution_options().get('rota_write_settings') or Settings()).worker_batch_size)
        .with_for_update(skip_locked=True))
    for job in expired:
        attempted = db.scalar(select(m.AiCall.id).where(m.AiCall.job_id == job.id).limit(1))
        if attempted:
            # A crash may have happened after the provider charged but before the reply was saved.
            _store(db, job, None, 'SCORE_PROVIDER_INTERRUPTED')
        else:
            job.state, job.claim_token, job.lease_expires_at = 'queued', None, None
            job.revision += 1
    job = db.scalar(select(m.ScoreCheckExtractionJob).where(m.ScoreCheckExtractionJob.department_id.in_(enabled),
        m.ScoreCheckExtractionJob.state == 'queued')
        .order_by(m.ScoreCheckExtractionJob.created_at, m.ScoreCheckExtractionJob.id)
        .limit(1).with_for_update(skip_locked=True))
    if job is None:
        return None
    try:
        _context(db, job)
    except Exception as error:
        _store(db, job, None, safe_error(error))
        return {'finished': True, 'department_id': job.department_id}
    job.state, job.claim_token = 'running', m.uid()
    job.started_at, job.lease_expires_at = m.now(), m.now() + timedelta(seconds=JOB_LEASE_SECONDS)
    job.revision += 1
    return {'id': job.id, 'token': job.claim_token, 'department_id': job.department_id}


def safe_error(error):
    code = getattr(error, 'code', '')
    return code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'SCORE_EXTRACTION_FAILED'


def _audit(engine, claim, event):
    def store(db):
        job = _current(db, claim)
        _context(db, job)
        attempt = event['attempt']
        require(type(attempt) is int and 1 <= attempt <= 2,
                'SCORE_PROVIDER_AUDIT', 'The provider attempt cannot be recorded', 409)
        row = db.scalar(select(m.AiCall).where(m.AiCall.job_id == job.id, m.AiCall.attempt == attempt))
        if event['result'] == 'started':
            require(row is None, 'SCORE_PROVIDER_INTERRUPTED', 'This provider attempt was already reserved', 409)
            row = m.AiCall(department_id=job.department_id, profile_id=job.profile_id, actor_id=job.requested_by,
                job_id=job.id, attempt=attempt, kind=event['kind'], model=event['model'],
                file_sha256=event.get('file_sha256') if job.kind == 'extraction' else None, result='pending')
            db.add(row)
        else:
            require(row is not None and row.result == 'pending' and event['result'] in {'valid', 'invalid', 'error'},
                    'SCORE_PROVIDER_AUDIT', 'The provider attempt cannot be recorded', 409)
            row.result, row.completed_at = event['result'], m.now()
            row.error_code = event.get('error_code')
            for name in ('input_tokens', 'output_tokens', 'duration_ms'):
                value = event.get(name)
                setattr(row, name, value if type(value) is int and value >= 0 else None)
        db.flush()
    retry_write(engine, store)


def _run(engine, claim, settings, stopped):
    from apps.api.services import score_check
    with transaction(engine) as db:
        job = _current(db, claim)
        actor, profile, source, _batch = _context(db, job)
        frozen = SimpleNamespace(**{column.key: getattr(profile, column.key)
                                    for column in m.AiProviderProfile.__table__.columns})
        kind = job.kind
        if source is not None:
            source_id, content, name, file_ref = source.id, source.content, source.file_name, source.file_ref
            context = score_check.extraction_context(db, actor, source)
    def audit(event):
        require(not stopped.is_set(), 'SCORE_JOB_LEASE', 'The file reading lease ended', 409)
        _audit(engine, claim, event)
    if kind == 'test':
        provider.test_connection(frozen, settings=settings, audit=audit)
        return {'ok': True}, None
    if name.lower().endswith(('.csv', '.tsv', '.xlsx')):
        from apps.api.services.score_check_parser import prepare_score_check_file
        prepared = prepare_score_check_file(content, name, file_ref=file_ref)
    else:
        prepared = provider.prepare_file(file_ref, name, content)
    def validate(value):
        try:
            with transaction(engine) as db:
                try:
                    require(not stopped.is_set(), 'SCORE_JOB_LEASE', 'The file reading lease ended', 409)
                    current = _current(db, claim)
                    current_actor, current_profile, current_source, _batch = _context(db, current)
                    require(current_profile.revision == frozen.revision and current_source.id == source_id
                            and current_source.sha256 == prepared.sha256,
                            'SCORE_SOURCE_CHANGED', 'The provider or source changed during reading', 409)
                except Exception:
                    raise provider.ProviderError('SCORE_PROVIDER_AUDIT') from None
                return score_check.validate_extraction(db, current_actor, current_source, value, inventory=prepared)
        except SQLAlchemyError:
            raise provider.ProviderError('SCORE_PROVIDER_AUDIT') from None
    prompt = ('Read this rota into the required extraction schema. Preserve exact references and as-written values. '
              'Report uncertainty; suggestions require later human confirmation. Context: '
              + json.dumps(context, ensure_ascii=False, default=str))
    extraction, _usage = provider.extract(frozen, prompt, [prepared], s.RotaExtraction,
        settings=settings, audit=audit, validate=validate)
    return extraction, prepared


def run_loop(engine, settings, stopped):
    """One extraction slot per worker, independent of solves and email dispatch."""
    delay = settings.worker_poll_seconds
    while not stopped.is_set():
        worked = False
        try:
            worked = process_one(engine, settings)
        except DatabaseWriteDeferred:
            log.debug('score_check_pass_deferred')
        except Exception as error:
            log.warning('score_check_loop_failed', extra={'error_code': safe_error(error)})
        if worked:
            delay = settings.worker_poll_seconds
        stopped.wait(delay)
        delay = settings.worker_poll_seconds if worked else min(delay * 2, settings.worker_idle_max_seconds)


def score_check_pending(engine):
    """Read-only queue probe; idle score-check workers never take the write lock."""
    reader = (engine.execution_options(isolation_level='REPEATABLE READ', postgresql_readonly=True)
              if engine.dialect.name == 'postgresql' else engine)
    with transaction(reader) as db:
        enabled = select(m.Department.id).where(m.Department.score_check_enabled.is_(True))
        now = m.now()
        return bool(db.scalar(select(m.ScoreCheckExtractionJob.id).where(
            m.ScoreCheckExtractionJob.department_id.in_(enabled),
            (m.ScoreCheckExtractionJob.state == 'queued') |
            ((m.ScoreCheckExtractionJob.state == 'running') &
             (m.ScoreCheckExtractionJob.lease_expires_at <= now))).limit(1)))


def process_one(engine, settings):
    if not score_check_pending(engine):
        return False
    claim = retry_write(engine, claim_job)
    if claim is None:
        return False
    if claim.get('finished'):
        return True
    engine = engine.execution_options(rota_defer_busy_writer=False)
    finished, stopped = threading.Event(), threading.Event()
    def renew():
        while not finished.wait(max(1, JOB_LEASE_SECONDS // 3)):
            try:
                def extend(db):
                    job = _current(db, claim)
                    _context(db, job)
                    job.lease_expires_at = m.now() + timedelta(seconds=JOB_LEASE_SECONDS)
                retry_write(engine, extend)
                from .main import heartbeat
                heartbeat(engine)
            except Exception:
                stopped.set()
                return
    monitor = threading.Thread(target=renew, daemon=True)
    monitor.start()
    extraction, prepared, error = None, None, None
    try:
        extraction, prepared = _run(engine, claim, settings, stopped)
    except Exception as failure:
        error = safe_error(failure)
        log.warning('score_check_failed', extra={'job_id': claim['id'], 'error_code': error})
    finally:
        finished.set()
        monitor.join()
    def finish(db):
        job = _current(db, claim)
        nonlocal error
        if error is None:
            try:
                actor, _profile, source, _batch = _context(db, job)
                if source is not None:
                    from apps.api.services import score_check
                    score_check.validate_extraction(db, actor, source, extraction, inventory=prepared)
            except Exception as failure:
                error = safe_error(failure)
        _store(db, job, extraction, error)
    try:
        retry_write(engine, finish)
    except Exception as failure:
        log.warning('score_check_finish_failed', extra={'job_id': claim['id'], 'error_code': safe_error(failure)})
    return True
