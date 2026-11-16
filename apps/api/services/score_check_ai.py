"""Developer-managed AI profiles and private, explicitly consented extraction jobs."""
from urllib.parse import urlsplit

from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import require_permission, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import json_value
from packages.contracts import schemas as s
from .common import expected, scoped
from . import score_check_provider as provider


def _profile_kind(row):
    return row.provider_kind or ('openai_compatible' if row.kind.startswith('openai_') else row.kind)


def profile_record(row):
    values = {name: getattr(row, name) for name in s.AiProviderProfileRecord.model_fields
              if name not in {'key_set', 'kind', 'api_style', 'thinking_budget'}}
    values['kind'] = _profile_kind(row)
    values['api_style'] = 'responses' if row.kind == 'openai_responses' else row.api_style
    values['thinking_budget'] = row.reasoning if type(row.reasoning) is int else None
    if type(row.reasoning) is int:
        values['reasoning'] = 'budget'
    return s.AiProviderProfileRecord(**json_value(values), key_set=bool(row.key_ciphertext)).model_dump(mode='json')


def profiles(db, actor, settings=None):
    require_permission(actor, 'ai.providers')
    rows = db.scalars(select(m.AiProviderProfile).where(
        m.AiProviderProfile.department_id == actor.department_id,
        m.AiProviderProfile.archived_at.is_(None)).order_by(m.AiProviderProfile.name, m.AiProviderProfile.id))
    department = db.get(m.Department, actor.department_id)
    return s.AiSettings(enabled=department.ai_enabled, revision=department.revision,
                        profiles=[profile_record(row) for row in rows]).model_dump(mode='json')


def choices(db, actor):
    require_role(actor, 'coordinator')
    department = db.get(m.Department, actor.department_id)
    rows = db.scalars(select(m.AiProviderProfile).where(
        m.AiProviderProfile.department_id == actor.department_id,
        m.AiProviderProfile.enabled.is_(True), m.AiProviderProfile.archived_at.is_(None),
        m.AiProviderProfile.key_ciphertext.is_not(None)).order_by(
            m.AiProviderProfile.name, m.AiProviderProfile.id)) if department.ai_enabled else []
    return s.ScoreProviderChoices(profiles=[s.ScoreProviderChoice(
        id=row.id, revision=row.revision, name=row.name, kind=_profile_kind(row),
        base_url=urlsplit(row.base_url).netloc, model=row.model, is_default=row.is_default)
        for row in rows if row.key_ciphertext]).model_dump(mode='json')


def recent_calls(db, actor, limit=100):
    require_permission(actor, 'ai.providers')
    rows = db.scalars(select(m.AiCall).where(m.AiCall.department_id == actor.department_id)
                      .order_by(m.AiCall.created_at.desc(), m.AiCall.id.desc()).limit(min(max(limit, 1), 100)))
    return {'calls': [s.AiCallRecord(**json_value({name: getattr(row, name)
                      for name in s.AiCallRecord.model_fields})).model_dump(mode='json') for row in rows]}


def _profile(db, actor, profile_id):
    row = scoped(db, m.AiProviderProfile, profile_id, actor)
    require(row.archived_at is None, 'NOT_FOUND', 'Record not found', 404)
    return row


def job_record(job):
    return json_value({name: getattr(job, name) for name in (
        'id', 'revision', 'kind', 'state', 'created_at', 'started_at', 'completed_at', 'error_code', 'result')})


def command(db, actor, command, settings):
    from .score_check import require_enabled
    require_enabled(db, actor.department_id)
    require_permission(actor, 'ai.providers')
    if command.kind == 'ai_provider.enabled':
        value = s.AiEnabledInput.model_validate(command.payload)
        department = db.get(m.Department, actor.department_id)
        require(command.target_id in (None, department.id), 'PROVIDER_TARGET', 'Use the current department')
        expected(department, command.expected_revision)
        department.ai_enabled = value.enabled
        department.revision += 1
        return profiles(db, actor, settings)
    if command.kind == 'ai_provider.save':
        value = s.AiProviderProfileInput.model_validate(command.payload)
        try:
            provider.validate_config(value.model_dump(exclude={'api_key', 'reason'}))
        except provider.ProviderError as error:
            raise Problem(422, error.code, str(error)) from None
        row = _profile(db, actor, command.target_id) if command.target_id else None
        if row:
            expected(row, command.expected_revision)
        else:
            require(command.expected_revision is None, 'STALE_REVISION', 'Reload provider settings before saving', 409)
        if value.is_default:
            for other in db.scalars(select(m.AiProviderProfile).where(
                    m.AiProviderProfile.department_id == actor.department_id,
                    m.AiProviderProfile.id != (row.id if row else ''), m.AiProviderProfile.is_default.is_(True))):
                other.is_default = False
                other.revision += 1
            db.flush()
        if row:
            row.revision += 1
        else:
            row = m.AiProviderProfile(id=m.uid(), department_id=actor.department_id, created_by=actor.identity_id)
            db.add(row)
        for name, item in value.model_dump(exclude={'reason', 'api_key', 'kind', 'api_style', 'thinking_budget'}).items():
            setattr(row, name, item)
        row.provider_kind = 'openai_compatible' if value.kind in {'openai_chat', 'openai_responses'} else value.kind
        row.api_style = ('responses' if value.kind == 'openai_responses' else 'chat'
                         if value.kind == 'openai_chat' else value.api_style)
        row.kind = ('openai_' + row.api_style if row.provider_kind in {'openai_compatible', 'gemini'} else row.provider_kind)
        if value.api_key is not None:
            try:
                row.key_ciphertext = provider.encrypt_key(row, value.api_key, settings)
            except provider.ProviderError as error:
                raise Problem(409, error.code, str(error)) from None
            row.key_saved_at = m.now()
        db.flush()
        return {'profile': profile_record(row)}
    if command.kind == 'ai_provider.key':
        value = s.AiProviderKeyInput.model_validate(command.payload)
        require(command.target_id in (None, value.profile_id), 'PROVIDER_TARGET', 'Choose the same provider')
        row = _profile(db, actor, value.profile_id)
        expected(row, command.expected_revision)
        try:
            row.key_ciphertext = provider.encrypt_key(row, value.api_key, settings)
        except provider.ProviderError as error:
            raise Problem(409, error.code, str(error)) from None
        row.key_saved_at, row.revision = m.now(), row.revision + 1
        db.flush()
        return {'profile': profile_record(row)}
    require(command.kind in {'ai_provider.delete', 'ai_provider.restore', 'ai_provider.test'},
            'COMMAND_UNKNOWN', 'Unknown AI provider command')
    s.Decision.model_validate(command.payload)
    row = (scoped(db, m.AiProviderProfile, command.target_id, actor) if command.kind == 'ai_provider.restore'
           else _profile(db, actor, command.target_id))
    expected(row, command.expected_revision)
    if command.kind == 'ai_provider.restore':
        require(row.archived_at is not None, 'WORKFLOW_STATE', 'Only a deleted provider can be restored', 409)
        row.archived_at, row.enabled, row.is_default = None, False, False
        row.revision += 1
        return {'profile': profile_record(row)}
    if command.kind == 'ai_provider.delete':
        # Archiving blocks use while retaining the encrypted key for Undo.
        row.archived_at, row.enabled, row.is_default = m.now(), False, False
        row.revision += 1
        return {'deleted': True, 'id': row.id, 'undo': {'kind': 'ai_provider.restore', 'target_id': row.id,
                'expected_revision': row.revision, 'payload': {'reason': 'Undo provider deletion'}}}
    require(row.enabled and row.key_ciphertext, 'SCORE_PROVIDER_DISABLED', 'Enable this provider and set its key first', 409)
    require(db.get(m.Department, actor.department_id).ai_enabled,
            'SCORE_AI_DISABLED', 'AI reading is off for this department', 409)
    require(not db.scalar(select(m.ScoreCheckExtractionJob.id).where(
        m.ScoreCheckExtractionJob.department_id == actor.department_id,
        m.ScoreCheckExtractionJob.profile_id == row.id,
        m.ScoreCheckExtractionJob.kind == 'test',
        m.ScoreCheckExtractionJob.state.in_(['queued', 'running'])).limit(1)),
        'JOB_ALREADY_RUNNING', 'A connection test is already queued or running', 409)
    job = m.ScoreCheckExtractionJob(department_id=actor.department_id, profile_id=row.id,
        requested_by=actor.identity_id, profile_revision=row.revision, kind='test')
    db.add(job)
    db.flush()
    return {'job': job_record(job)}


def queue_file(db, actor, batch, source, profile, settings):
    from .score_check import require_enabled
    require_enabled(db, actor.department_id)
    require_role(actor, 'coordinator')
    require(db.get(m.Department, actor.department_id).ai_enabled,
            'SCORE_AI_DISABLED', 'AI reading is off for this department', 409)
    require(source.department_id == batch.department_id == actor.department_id
            and source.batch_id == batch.id and source.removed_at is None,
            'NOT_FOUND', 'Record not found', 404)
    require(batch.status not in {'saved', 'discarded'}, 'IMPORT_STATE', 'This import is no longer being read', 409)
    require(batch.use_ai and batch.privacy_acknowledged and batch.privacy_acknowledged_at is not None
            and batch.privacy_acknowledged_by == actor.identity_id
            and profile is not None and batch.profile_id == profile.id
            and profile.department_id == actor.department_id,
            'SCORE_AI_CONSENT', 'Choose a provider and acknowledge where the file will be sent', 409)
    require(profile.enabled and profile.archived_at is None and profile.key_ciphertext,
            'SCORE_PROVIDER_DISABLED', 'Enable this provider and set its key first', 409)
    require(batch.profile_revision == profile.revision, 'STALE_REVISION', 'The provider changed; acknowledge it again', 409)
    require(source.extraction_job_id is None, 'JOB_ALREADY_RUNNING', 'This file already has a reading job', 409)
    job = m.ScoreCheckExtractionJob(department_id=actor.department_id, source_id=source.id,
        profile_id=profile.id, profile_revision=profile.revision, requested_by=actor.identity_id,
        privacy_acknowledged_at=batch.privacy_acknowledged_at, kind='extraction')
    db.add(job)
    db.flush()
    source.extraction_job_id, source.status, source.error_code = job.id, 'reading', None
    return job_record(job)
