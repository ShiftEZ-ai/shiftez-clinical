"""Private credentials, durable attempt reservations and current consent are enforced together."""
import hashlib
import json
from datetime import date, timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor, current_roles
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import score_check_ai as ai, score_check_provider as provider
from apps.worker import score_check as worker
from packages.contracts import schemas as s


SECRET = 'fictional-provider-key-never-return-this'


def actor(h, db, role='operator'):
    identity = db.get(m.Identity, h.seed['identities'][role])
    return Actor(identity.id, h.seed['department_id'], h.seed['organisation_id'], identity.display_name,
                 current_roles(db, identity.id, h.seed['department_id']), identity.person_id, '')


def command(h, kind, payload=None, target=None, revision=None, role='operator'):
    with transaction(h.app.state.engine, write=True) as db:
        return ai.command(db, actor(h, db, role), s.Command(kind=kind, payload=payload or {},
            target_id=target, expected_revision=revision), h.settings)


def profile(h, **values):
    object.__setattr__(h.settings, 'fmuc_data_key', '17' * 32)
    values = {'name': 'Synthetic provider', 'kind': 'openai_chat', 'model': 'synthetic-model',
              'base_url': 'https://example.com/v1', **values}
    row = command(h, 'ai_provider.save', values)['profile']
    return command(h, 'ai_provider.key', {'profile_id': row['id'], 'api_key': SECRET},
                   revision=row['revision'])['profile']


def enable(h, enabled=True):
    with transaction(h.app.state.engine) as db:
        revision = db.get(m.Department, h.seed['department_id']).revision
    return command(h, 'ai_provider.enabled', {'enabled': enabled}, revision=revision)


def queued_file(h, *, file_name='private-staff.csv', content=None):
    configured = profile(h, enabled=True)
    enable(h)
    content = content or b'person,date,duty\nFictional Private Staff,2026-10-04,AM\n'
    with transaction(h.app.state.engine, write=True) as db:
        who = actor(h, db, 'coordinator')
        batch = m.ScoreCheckImportBatch(department_id=who.department_id, created_by=who.identity_id,
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11),
            period_inferred=False,
            use_ai=True, profile_id=configured['id'], profile_revision=configured['revision'],
            privacy_acknowledged=True, privacy_acknowledged_by=who.identity_id, privacy_acknowledged_at=m.now())
        db.add(batch)
        db.flush()
        source = m.ImportSource(department_id=who.department_id, batch_id=batch.id,
            file_name=file_name, file_ref=m.uid(), format=file_name.rsplit('.', 1)[-1], group='Synthetic group',
            sha256=hashlib.sha256(content).hexdigest(), content=content)
        db.add(source)
        db.flush()
        job = ai.queue_file(db, who, batch, source, db.get(m.AiProviderProfile, configured['id']), h.settings)
        return configured, batch.id, source.id, job['id']


def test_provider_defaults_permissions_encryption_and_safe_projections(h):
    saved = profile(h)
    assert saved['enabled'] is False and saved['is_default'] is False and saved['key_set'] is True
    assert 'key_ciphertext' not in saved and SECRET not in json.dumps(saved)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, saved['id'])
        assert row.key_ciphertext != SECRET and provider.decrypt_key(row, h.settings) == SECRET
        assert ai.profiles(db, actor(h, db), h.settings) == {
            'enabled': False, 'revision': db.get(m.Department, h.seed['department_id']).revision, 'profiles': [saved]}
        for role in ('admin', 'coordinator', 'auditor', 'staff'):
            with pytest.raises(Problem) as failure:
                ai.profiles(db, actor(h, db, role), h.settings)
            assert failure.value.status == 403


def test_default_switch_revision_guard_and_delete_preserve_call_evidence(h):
    first = profile(h, is_default=True)
    second = profile(h, name='Second synthetic provider', is_default=True)
    with transaction(h.app.state.engine, write=True) as db:
        assert db.get(m.AiProviderProfile, first['id']).is_default is False
        stored = db.get(m.AiProviderProfile, second['id'])
        credential = stored.key_ciphertext, stored.key_saved_at
        call = m.AiCall(department_id=h.seed['department_id'], profile_id=second['id'],
            actor_id=h.seed['identities']['operator'], kind='openai_chat', model='synthetic-model', result='error')
        db.add(call)
    with pytest.raises(Problem) as stale:
        command(h, 'ai_provider.delete', {'reason': 'Remove synthetic profile'}, second['id'], second['revision'] - 1)
    assert stale.value.code == 'STALE_REVISION'
    command(h, 'ai_provider.delete', {'reason': 'Remove synthetic profile'}, second['id'], second['revision'])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, second['id'])
        assert row.archived_at is not None and not row.enabled
        assert (row.key_ciphertext, row.key_saved_at) == credential
        assert db.scalar(select(m.AiCall).where(m.AiCall.profile_id == row.id)) is not None
        assert second['id'] not in {p['id'] for p in ai.profiles(db, actor(h, db))['profiles']}


@pytest.mark.parametrize('global_enabled', [False, True])
def test_provider_delete_http_undo_restores_exact_credential_without_enabling_ai(h, global_enabled, caplog):
    saved = profile(h, enabled=True, is_default=True)
    enable(h, global_enabled)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, saved['id'])
        credential = row.key_ciphertext, row.key_saved_at
    delete_key, restore_key = str(uuid4()), str(uuid4())
    deleted = h.command('operator', 'ai_provider.delete', {'reason': 'Remove synthetic provider'},
                        saved['id'], saved['revision'], key=delete_key)
    assert deleted['deleted'] is True and deleted['id'] == saved['id']
    undo = deleted['undo']
    assert undo == {'kind': 'ai_provider.restore', 'target_id': saved['id'],
                    'expected_revision': saved['revision'] + 1, 'payload': {'reason': 'Undo provider deletion'}}
    assert saved['id'] not in {row['id'] for row in h.get('operator', '/api/ai-providers')['profiles']}
    current_default = profile(h, name='Another default', enabled=True, is_default=True)
    restored = h.command('operator', undo['kind'], undo['payload'], undo['target_id'],
                         undo['expected_revision'], key=restore_key)
    read = restored['profile']
    assert read['id'] == saved['id'] and read['revision'] == saved['revision'] + 2
    assert read['key_set'] is True and read['key_saved_at'] == saved['key_saved_at']
    assert read['enabled'] is False and read['is_default'] is False
    assert h.command('operator', undo['kind'], undo['payload'], undo['target_id'],
                     undo['expected_revision'], key=restore_key) == restored
    assert h.command('operator', 'ai_provider.delete', {'reason': 'Remove synthetic provider'},
                     saved['id'], saved['revision'], key=delete_key) == deleted
    assert saved['id'] not in {row['id'] for row in h.get('coordinator', '/api/score-imports/providers')['profiles']}
    disabled = h.command('operator', 'ai_provider.test', {'reason': 'Restored profile is still disabled'},
                         saved['id'], read['revision'], status=409)
    assert disabled['error']['code'] == 'SCORE_PROVIDER_DISABLED'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, saved['id'])
        assert row.archived_at is None and row.revision == read['revision']
        assert (row.key_ciphertext, row.key_saved_at) == credential
        assert provider.decrypt_key(row, h.settings) == SECRET
        assert db.get(m.AiProviderProfile, current_default['id']).is_default is True
        assert db.get(m.Department, h.seed['department_id']).ai_enabled is global_enabled
        receipts = list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind.in_(
            ['ai_provider.delete', 'ai_provider.restore']))))
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(
            ['ai_provider.delete', 'ai_provider.restore']))))
        assert len(receipts) == len(events) == 2
        assert {event.action for event in events} == {'ai_provider.delete', 'ai_provider.restore'}
        assert all(event.target_id == saved['id'] and event.actor_id == h.seed['identities']['operator']
                   for event in events)
        delete_receipt = next(receipt for receipt in receipts if receipt.command_kind == 'ai_provider.delete')
        assert delete_receipt.result['undo'] == undo
        evidence = json.dumps([deleted, restored, [receipt.result for receipt in receipts],
                               [event.detail for event in events]]) + caplog.text
    assert SECRET not in evidence and credential[0] not in evidence
    assert 'key_ciphertext' not in evidence and 'api_key' not in evidence


@pytest.mark.parametrize('role', [role for role in s.ROLE_NAMES if role not in s.PERMISSIONS['ai.providers']])
def test_provider_restore_http_is_developer_only(h, role):
    from .governance_support import identity
    if role not in h.seed['identities']:
        identity(h, role, [role])
    saved = profile(h)
    deleted = h.command('operator', 'ai_provider.delete', {'reason': 'Synthetic deletion'},
                        saved['id'], saved['revision'])
    undo = deleted['undo']
    h.command(role, undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'], status=403)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, saved['id'])
        assert row.archived_at is not None and row.revision == undo['expected_revision']
        assert row.key_ciphertext is not None


def test_provider_restore_http_requires_current_revision_and_deleted_state(h):
    saved = profile(h)
    deleted = h.command('operator', 'ai_provider.delete', {'reason': 'Synthetic deletion'},
                        saved['id'], saved['revision'])
    revision = deleted['undo']['expected_revision']
    payload = {'reason': 'Undo provider deletion'}
    assert h.command('operator', 'ai_provider.restore', payload, saved['id'], status=428)['error']['code'] == 'REVISION_REQUIRED'
    stale = h.command('operator', 'ai_provider.restore', payload, saved['id'], saved['revision'], status=409)
    assert stale['error']['code'] == 'STALE_REVISION' and stale['error']['details']['current_revision'] == revision
    restored = h.command('operator', 'ai_provider.restore', payload, saved['id'], revision)['profile']
    repeated = h.command('operator', 'ai_provider.restore', payload, saved['id'], revision, status=409)
    assert repeated['error']['code'] == 'STALE_REVISION'
    active = h.command('operator', 'ai_provider.restore', payload, saved['id'], restored['revision'], status=409)
    assert active['error']['code'] == 'WORKFLOW_STATE'


def test_provider_restore_http_hides_foreign_department_and_missing_records(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(department)
        db.flush()
        foreign = m.AiProviderProfile(department_id=department.id, created_by=h.seed['identities']['operator'],
            name='Foreign archived profile', kind='openai_chat', model='synthetic-model',
            base_url='https://example.com/v1', archived_at=m.now(), key_ciphertext='retained-opaque-envelope')
        db.add(foreign)
        db.flush()
        foreign_id, revision = foreign.id, foreign.revision
    for target in (foreign_id, str(uuid4())):
        rejected = h.command('operator', 'ai_provider.restore', {'reason': 'Undo provider deletion'}, target, revision, status=404)
        assert rejected['error']['code'] == 'NOT_FOUND'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AiProviderProfile, foreign_id)
        assert row.archived_at is not None and row.revision == revision
        assert row.key_ciphertext == 'retained-opaque-envelope'


def test_archived_provider_retains_key_but_refuses_edits_and_queued_calls(h, monkeypatch):
    saved = profile(h, enabled=True)
    enable(h)
    queued = h.command('operator', 'ai_provider.test', {'reason': 'Queue before deletion'},
                       saved['id'], saved['revision'])['job']
    deleted = h.command('operator', 'ai_provider.delete', {'reason': 'Synthetic deletion'},
                        saved['id'], saved['revision'])
    revision = deleted['undo']['expected_revision']
    for kind, payload in (
            ('ai_provider.test', {'reason': 'Deleted profiles cannot call'}),
            ('ai_provider.key', {'profile_id': saved['id'], 'api_key': SECRET}),
            ('ai_provider.save', {'name': saved['name'], 'kind': saved['kind'], 'model': saved['model'],
                                  'base_url': saved['base_url']})):
        assert h.command('operator', kind, payload, saved['id'], revision, status=404)['error']['code'] == 'NOT_FOUND'
    def forbidden(*args, **kwargs):
        pytest.fail('An archived provider must not make a call with its retained key')
    monkeypatch.setattr(provider, 'test_connection', forbidden)
    assert worker.process_one(h.app.state.engine, h.settings) is True
    with transaction(h.app.state.engine) as db:
        job = db.get(m.ScoreCheckExtractionJob, queued['id'])
        assert job.state == 'failed' and job.error_code == 'SCORE_PROVIDER_DISABLED'
        assert db.scalar(select(m.AiCall.id)) is None
        assert provider.decrypt_key(db.get(m.AiProviderProfile, saved['id']), h.settings) == SECRET


def test_merged_provider_save_accepts_write_only_key_and_keeps_it_when_omitted(h):
    object.__setattr__(h.settings, 'fmuc_data_key', '17' * 32)
    values = {'name': 'Merged synthetic provider', 'kind': 'openai_chat', 'model': 'synthetic-model',
              'base_url': 'https://example.com/v1', 'api_key': SECRET}
    assert SECRET not in repr(s.AiProviderProfileInput(**values))
    saved = command(h, 'ai_provider.save', values)['profile']
    assert saved['key_set'] is True and saved['revision'] == 1 and SECRET not in json.dumps(saved)
    with transaction(h.app.state.engine) as db:
        ciphertext = db.get(m.AiProviderProfile, saved['id']).key_ciphertext
        assert provider.decrypt_key(db.get(m.AiProviderProfile, saved['id']), h.settings) == SECRET
    values.pop('api_key')
    updated = command(h, 'ai_provider.save', values, saved['id'], saved['revision'])['profile']
    assert updated['key_set'] is True and updated['key_saved_at'] == saved['key_saved_at']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AiProviderProfile, saved['id']).key_ciphertext == ciphertext


def test_global_ai_switch_defaults_off_and_preserves_profile_configuration(h, monkeypatch):
    saved = profile(h, enabled=True)
    with pytest.raises(Problem) as disabled:
        command(h, 'ai_provider.test', {'reason': 'Test synthetic provider'}, saved['id'], saved['revision'])
    assert disabled.value.code == 'SCORE_AI_DISABLED'
    enabled = enable(h)
    assert enabled['enabled'] is True and enabled['profiles'] == [saved]
    job = command(h, 'ai_provider.test', {'reason': 'Test synthetic provider'}, saved['id'], saved['revision'])['job']
    disabled_settings = enable(h, False)
    assert disabled_settings == {'enabled': False, 'revision': enabled['revision'] + 1, 'profiles': [saved]}
    with pytest.raises(Problem) as stale:
        command(h, 'ai_provider.enabled', {'enabled': True}, revision=enabled['revision'])
    assert stale.value.code == 'STALE_REVISION'
    def forbidden(*args, **kwargs):
        pytest.fail('The department AI switch must block queued calls')
    monkeypatch.setattr(provider, 'test_connection', forbidden)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.ScoreCheckExtractionJob, job['id'])
        assert stored.state == 'failed' and stored.error_code == 'SCORE_AI_DISABLED'
        assert stored.result['ok'] is False and 'message' in stored.result
        assert db.get(m.AiProviderProfile, saved['id']).enabled is True
        assert db.scalar(select(m.AiCall.id)) is None


@pytest.mark.parametrize('kind,style', [('openai_compatible', 'responses'), ('gemini', 'chat'), ('azure', 'responses')])
def test_public_provider_kind_and_style_round_trip_through_legacy_storage(h, kind, style):
    values = {'name': 'Canonical synthetic provider', 'kind': kind, 'api_style': style,
              'model': 'synthetic-model', 'base_url': 'https://example.com/v1'}
    saved = command(h, 'ai_provider.save', values)['profile']
    assert saved['kind'] == kind and saved['api_style'] == style
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.AiProviderProfile, saved['id'])
        assert stored.kind == ('azure' if kind == 'azure' else 'openai_' + style)
        assert ai.profiles(db, actor(h, db))['profiles'] == [saved]


def test_unsupported_thinking_budget_is_rejected_before_profile_is_saved(h):
    with pytest.raises(Problem) as rejected:
        command(h, 'ai_provider.save', {'name': 'Unsupported budget', 'kind': 'anthropic',
            'model': 'synthetic-model', 'base_url': 'https://example.com/v1', 'thinking_budget': 500})
    assert rejected.value.code == 'SCORE_PROVIDER_REASONING'
    assert 'choose a reasoning level' in rejected.value.message
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.AiProviderProfile.id)) is None


def test_historical_integer_budget_remains_readable_without_changing_it(h):
    saved = profile(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.AiProviderProfile, saved['id']).reasoning = 2048
    with transaction(h.app.state.engine) as db:
        read = ai.profiles(db, actor(h, db))['profiles'][0]
        assert read['reasoning'] == 'budget' and read['thinking_budget'] == 2048
        assert db.get(m.AiProviderProfile, saved['id']).reasoning == 2048


def test_connection_test_is_queued_and_every_attempt_is_committed_before_remote_io(h, monkeypatch):
    saved = profile(h, enabled=True)
    enable(h)
    calls = []
    def remote(profile, *, settings, audit):
        event = {'attempt': 1, 'profile_id': profile.id, 'kind': profile.kind,
                 'model': profile.model, 'file_sha256': 'a' * 64}
        audit({**event, 'result': 'started'})
        with transaction(h.app.state.engine) as db:
            durable = db.scalar(select(m.AiCall))
            assert durable.result == 'pending' and durable.file_sha256 is None
        calls.append(True)
        audit({**event, 'result': 'valid', 'input_tokens': 2, 'output_tokens': 1, 'duration_ms': 3})
        return {'ok': True, 'usage': {'input_tokens': 2, 'output_tokens': 1}}
    monkeypatch.setattr(provider, 'test_connection', remote)
    job = command(h, 'ai_provider.test', {'reason': 'Test synthetic provider'}, saved['id'], saved['revision'])['job']
    assert job['state'] == 'queued' and not calls
    assert worker.process_one(h.app.state.engine, h.settings) is True
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.ScoreCheckExtractionJob, job['id'])
        assert stored.state == 'completed' and stored.result == {'ok': True, 'message': 'Connection successful.'}
        call = db.scalar(select(m.AiCall))
        assert call.result == 'valid' and call.input_tokens == 2 and call.completed_at is not None
        assert SECRET not in json.dumps(ai.recent_calls(db, actor(h, db)))
    public = h.get('operator', '/api/ai-providers/calls')['calls']
    assert len(public) == 1 and public[0]['job_id'] == job['id']
    assert public[0]['result'] == 'valid' and public[0]['input_tokens'] == 2 and public[0]['output_tokens'] == 1
    h.get('admin', '/api/ai-providers/calls', status=403)
    assert len(calls) == 1


@pytest.mark.parametrize('separator,file_name', [(',', 'private-staff.csv'), ('\t', 'private-staff.tsv')])
def test_extraction_keeps_content_private_and_validates_before_persisting(h, monkeypatch, separator, file_name):
    content = f'person{separator}date{separator}duty\nFictional Private Staff{separator}2026-10-04{separator}AM\n'.encode()
    _profile, _batch, source_id, job_id = queued_file(h, file_name=file_name, content=content)
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        event = {'attempt': 1, 'profile_id': profile.id, 'kind': profile.kind,
                 'model': profile.model, 'file_sha256': files[0].sha256}
        audit({**event, 'result': 'started'})
        result = parse_score_check_file(files[0], group='Synthetic group')
        result = validate(result)
        audit({**event, 'result': 'valid', 'duration_ms': 1})
        return result, {}
    monkeypatch.setattr(provider, 'extract', extract)
    assert worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        source, job = db.get(m.ImportSource, source_id), db.get(m.ScoreCheckExtractionJob, job_id)
        assert source.extraction['assignments'][0]['person'] == 'Fictional Private Staff'
        assert job.state == 'completed' and job.result == {'assignments': 1, 'unparsed': 0}
        public = json.dumps({'job': ai.job_record(job), 'calls': ai.recent_calls(db, actor(h, db))})
        assert 'Fictional Private Staff' not in public and file_name not in public and SECRET not in public


@pytest.mark.parametrize('revoke', ['access', 'profile', 'revision', 'consent'])
def test_current_access_profile_and_consent_are_rechecked_before_remote_call(h, monkeypatch, revoke):
    configured, batch_id, source_id, job_id = queued_file(h)
    with transaction(h.app.state.engine, write=True) as db:
        if revoke == 'access':
            db.get(m.Identity, h.seed['identities']['coordinator']).disabled = True
        elif revoke == 'profile':
            db.get(m.AiProviderProfile, configured['id']).enabled = False
        elif revoke == 'revision':
            db.get(m.AiProviderProfile, configured['id']).revision += 1
        else:
            db.get(m.ScoreCheckExtractionJob, job_id).privacy_acknowledged_at = None
    def forbidden(*args, **kwargs):
        pytest.fail('Revoked reading must not call a provider')
    monkeypatch.setattr(provider, 'extract', forbidden)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).state == 'failed'
        assert db.get(m.ImportSource, source_id).status == 'failed'
        assert db.scalar(select(m.AiCall.id)) is None


def test_profile_selection_changes_only_future_files(h, monkeypatch):
    first, batch_id, source_id, job_id = queued_file(h)
    second = profile(h, name='Next file provider', enabled=True)
    with transaction(h.app.state.engine, write=True) as db:
        batch = db.get(m.ScoreCheckImportBatch, batch_id)
        batch.profile_id, batch.profile_revision = second['id'], second['revision']
        batch.use_ai, batch.privacy_acknowledged, batch.privacy_acknowledged_by = False, False, None
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        assert profile.id == first['id']
        return validate(parse_score_check_file(files[0], group='Synthetic group')), {}
    monkeypatch.setattr(provider, 'extract', extract)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        assert job.state == 'completed' and job.profile_revision == first['revision']
        assert job.privacy_acknowledged_at is not None
        assert db.get(m.ImportSource, source_id).extraction is not None


@pytest.mark.parametrize('invalid', ['file', 'cell', 'person_id', 'date'])
def test_untrusted_extraction_references_are_rejected_before_persistence(h, monkeypatch, invalid):
    _profile, _batch, source_id, job_id = queued_file(h)
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        value = parse_score_check_file(files[0], group='Synthetic group')
        if invalid == 'file':
            value['files'][0]['file_ref'] = 'not-the-uploaded-file'
        elif invalid == 'cell':
            value['assignments'][0]['cell_ref'] = 'Invented!X999'
        elif invalid == 'person_id':
            value['people'][0]['suggested_person_id'] = m.uid()
        else:
            value['assignments'][0]['date'] = '2027-01-01'
        return validate(value), {}
    monkeypatch.setattr(provider, 'extract', extract)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).state == 'failed'
        source = db.get(m.ImportSource, source_id)
        assert source.status == 'failed' and source.extraction is None


def test_revoked_consent_after_reply_prevents_saving_extraction(h, monkeypatch):
    _profile, batch_id, source_id, job_id = queued_file(h)
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        value = validate(parse_score_check_file(files[0], group='Synthetic group'))
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.ScoreCheckExtractionJob, job_id).privacy_acknowledged_at = None
        return value, {}
    monkeypatch.setattr(provider, 'extract', extract)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).error_code == 'SCORE_AI_CONSENT'
        assert db.get(m.ImportSource, source_id).extraction is None


def test_candidate_changes_after_reply_are_revalidated_at_save(h, monkeypatch):
    _profile, _batch, source_id, job_id = queued_file(h)
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        value = parse_score_check_file(files[0], group='Synthetic group')
        value['people'][0]['suggested_person_id'] = h.seed['people'][0]
        value = validate(value)
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Person, h.seed['people'][0]).active = False
        return value, {}
    monkeypatch.setattr(provider, 'extract', extract)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).error_code == 'EXTRACTION_ID'
        assert db.get(m.ImportSource, source_id).extraction is None


@pytest.mark.parametrize('closed', ['removed', 'discarded', 'saved'])
def test_closed_sources_do_not_call_provider_or_reopen_batch(h, monkeypatch, closed):
    _profile, batch_id, source_id, job_id = queued_file(h)
    with transaction(h.app.state.engine, write=True) as db:
        if closed == 'removed':
            db.get(m.ImportSource, source_id).removed_at = m.now()
        else:
            db.get(m.ScoreCheckImportBatch, batch_id).status = closed
    def forbidden(*args, **kwargs):
        pytest.fail('A closed import must not call a provider')
    monkeypatch.setattr(provider, 'extract', forbidden)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).error_code == 'SCORE_SOURCE_CHANGED'
        source, batch = db.get(m.ImportSource, source_id), db.get(m.ScoreCheckImportBatch, batch_id)
        assert source.extraction is None
        assert source.removed_at is not None if closed == 'removed' else batch.status == closed
        assert db.scalar(select(m.AiCall.id)) is None


def test_discard_during_reading_does_not_reopen_import_or_persist_reply(h, monkeypatch):
    _profile, batch_id, source_id, job_id = queued_file(h)
    def extract(profile, prompt, files, schema, *, settings, audit, validate):
        from apps.api.services.score_check_parser import parse_score_check_file
        value = validate(parse_score_check_file(files[0], group='Synthetic group'))
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.ScoreCheckImportBatch, batch_id).status = 'discarded'
        return value, {}
    monkeypatch.setattr(provider, 'extract', extract)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, job_id).error_code == 'SCORE_SOURCE_CHANGED'
        assert db.get(m.ScoreCheckImportBatch, batch_id).status == 'discarded'
        assert db.get(m.ImportSource, source_id).extraction is None


def test_expired_lease_after_reserved_call_never_repeats_paid_request(h, monkeypatch):
    configured, _batch, source_id, job_id = queued_file(h)
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        job.state, job.claim_token = 'running', m.uid()
        job.lease_expires_at = m.now() - timedelta(seconds=1)
        db.add(m.AiCall(department_id=job.department_id, profile_id=configured['id'],
            actor_id=job.requested_by, job_id=job.id, attempt=1, kind='openai_chat',
            model='synthetic-model', file_sha256='a' * 64, result='pending'))
    def forbidden(*args, **kwargs):
        pytest.fail('An interrupted paid request must not be replayed')
    monkeypatch.setattr(provider, 'extract', forbidden)
    worker.process_one(h.app.state.engine, h.settings)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        assert job.state == 'failed' and job.error_code == 'SCORE_PROVIDER_INTERRUPTED'
        assert db.get(m.ImportSource, source_id).status == 'failed'
        calls = list(db.scalars(select(m.AiCall)))
        assert len(calls) == 1 and calls[0].result == 'error'


def test_coordinator_provider_choices_are_minimal_usable_and_department_scoped(h):
    from .governance_support import identity

    saved = profile(h, enabled=True, base_url='https://example.com:8443/private/provider/v1')
    enable(h)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other test department', ai_enabled=True)
        db.add(other)
        db.flush()
        for name, overrides in (
                ('Disabled', {'enabled': False}), ('No key', {'key_ciphertext': None}),
                ('Archived', {'archived_at': m.now()}), ('Other department', {'department_id': other.id})):
            db.add(m.AiProviderProfile(**{'department_id': h.seed['department_id'],
                'created_by': h.seed['identities']['operator'], 'name': name, 'kind': 'openai_chat',
                'base_url': 'https://hidden.example.com/config', 'model': 'private-model',
                'enabled': True, 'key_ciphertext': 'synthetic-opaque-ciphertext', **overrides}))
    expected = {'profiles': [{'id': saved['id'], 'revision': saved['revision'], 'name': saved['name'],
        'kind': 'openai_compatible', 'base_url': 'example.com:8443', 'model': saved['model'], 'is_default': False}]}
    for role in ('coordinator', 'admin'):
        assert h.get(role, '/api/score-imports/providers') == expected
    for role in ('staff', 'approver', 'programme_lead', 'auditor', 'operator'):
        if role not in h.seed['identities']:
            identity(h, role, [role])
        h.get(role, '/api/score-imports/providers', status=403)
    enable(h, False)
    assert h.get('coordinator', '/api/score-imports/providers') == {'profiles': []}


def test_failed_provider_messages_never_enter_job_or_audit(h, monkeypatch, caplog):
    _profile, _batch, _source, job_id = queued_file(h)
    def remote(*args, **kwargs):
        raise RuntimeError('Private failure ' + SECRET + ' Fictional Private Staff')
    monkeypatch.setattr(provider, 'extract', remote)
    worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        job = db.get(m.ScoreCheckExtractionJob, job_id)
        assert job.error_code == 'SCORE_EXTRACTION_FAILED' and job.result == {}
    assert SECRET not in caplog.text and 'Fictional Private Staff' not in caplog.text


def test_provider_queue_is_independent_of_normal_job_backlog(h, monkeypatch):
    from apps.worker import main
    saved = profile(h, enabled=True)
    enable(h)
    ai_job = command(h, 'ai_provider.test', {'reason': 'Test synthetic provider'}, saved['id'], saved['revision'])['job']
    workspace = h.workspace()
    ordinary = h.command('coordinator', 'job.create', {'kind': 'validation', 'workspace_id': workspace['id']},
                         revision=workspace['revision'])['job']
    monkeypatch.setattr(provider, 'test_connection', lambda *args, **kwargs: {'ok': True, 'usage': {}})
    assert main.process_one(h.app.state.engine, settings=h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, ordinary['id']).status == 'succeeded'
        assert db.get(m.ScoreCheckExtractionJob, ai_job['id']).state == 'queued'
    assert worker.process_one(h.app.state.engine, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ScoreCheckExtractionJob, ai_job['id']).state == 'completed'


def test_provider_http_contract_receipts_and_logs_keep_keys_private(h, monkeypatch, caplog):
    object.__setattr__(h.settings, 'fmuc_data_key', '17' * 32)
    payload = {'name': 'HTTP synthetic provider', 'kind': 'openai_compatible', 'api_style': 'chat',
               'model': 'synthetic-model', 'base_url': 'https://example.com/v1', 'enabled': True, 'api_key': SECRET}
    key = str(uuid4())
    reply = h.command('operator', 'ai_provider.save', payload, key=key)
    saved = reply['profile']
    assert saved['key_set'] is True and saved['kind'] == 'openai_compatible'
    assert h.command('operator', 'ai_provider.save', payload, key=key)['profile'] == saved
    settings = h.get('operator', '/api/ai-providers')
    assert settings['enabled'] is False and settings['profiles'] == [saved]
    assert h.get('coordinator', '/api/score-imports/providers') == {'profiles': []}
    h.command('operator', 'ai_provider.enabled', {'enabled': True}, revision=settings['revision'])
    h.get('coordinator', '/api/ai-providers', status=403)
    h.get('coordinator', '/api/ai-providers/calls', status=403)
    notice = h.get('coordinator', '/api/score-imports/providers')
    assert notice == {'profiles': [{'id': saved['id'], 'revision': saved['revision'],
        'name': saved['name'], 'kind': saved['kind'], 'base_url': 'example.com',
        'model': saved['model'], 'is_default': False}]}
    assert s.ScoreProviderChoices.model_validate(notice).profiles[0].id == saved['id']
    calls = []
    def remote(profile, *, settings, audit):
        event = {'attempt': 1, 'profile_id': profile.id, 'kind': profile.kind,
                 'model': profile.model, 'file_sha256': None}
        audit({**event, 'result': 'started'})
        calls.append(True)
        audit({**event, 'result': 'valid', 'input_tokens': 2, 'output_tokens': 1, 'duration_ms': 1})
        return {'ok': True, 'usage': {}}
    monkeypatch.setattr(provider, 'test_connection', remote)
    queued = h.command('operator', 'ai_provider.test', {'reason': 'Explicit synthetic connection check'},
                       saved['id'], saved['revision'])['job']
    assert queued['state'] == 'queued' and not calls
    path = '/api/score-extraction-jobs/' + queued['id']
    h.get('coordinator', path, status=403)
    assert h.get('operator', path)['state'] == 'queued'
    worker.process_one(h.app.state.engine, h.settings)
    terminal = h.get('operator', path)
    audit = h.get('operator', '/api/ai-providers/calls')
    assert terminal['result'] == {'ok': True, 'message': 'Connection successful.'}
    assert len(calls) == 1 and audit['calls'][0]['result'] == 'valid'
    public = json.dumps([reply, settings, notice, terminal, audit])
    assert SECRET not in public and 'key_ciphertext' not in public and 'api_key' not in public
    with transaction(h.app.state.engine) as db:
        receipts = list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.command_kind.like('ai_provider.%'))))
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like('ai_provider.%'))))
        assert SECRET not in json.dumps([r.result for r in receipts])
        assert SECRET not in json.dumps([e.detail for e in events])
    assert SECRET not in caplog.text
