"""Strict import boundaries, private provider reads and additive score-check storage."""
import importlib.util
from io import StringIO
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
import pytest
import sqlalchemy as sa

from apps.api import models as m
from migrations.safety import declaration_errors
from packages.contracts import schemas as s
from scripts.generate_contracts import render


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'migrations/versions/0120_score_check.py'
PERIOD_MIGRATION = ROOT / 'migrations/versions/0121_score_check_period.py'
GATE_MIGRATION = ROOT / 'migrations/versions/0122_score_check_gate.py'
NEW_MODELS = (m.AiProviderProfile, m.ScoreCheckImportBatch, m.ImportSource, m.ScoreCheckGapDecision,
              m.BenchmarkResult, m.ScoreCheckExtractionJob, m.AiCall)


def extraction():
    return {
        'files': [{'file_ref': 'rota.csv', 'layout': 'sessions_list', 'rows_read': 2, 'confidence': 1}],
        'people': [{'as_written': 'Synthetic person (test)', 'file_ref': 'rota.csv', 'cell_refs': ['Sheet1!A2'],
                    'confidence': .8, 'reason': 'Exact staff number'}],
        'duty_codes': [{'code': 'AM', 'file_ref': 'rota.csv', 'cell_refs': ['Sheet1!B2'],
                        'kind': 'duty', 'confidence': 1, 'reason': 'Labelled morning duty'}],
        'assignments': [{'person': 'Synthetic person (test)', 'date': '2026-11-01', 'file_ref': 'rota.csv',
                         'cell_ref': 'Sheet1!B2', 'duty_code': 'AM', 'start': '08:00', 'end': '16:00'}],
    }


def migration(path=MIGRATION):
    spec = importlib.util.spec_from_file_location('score_check_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extraction_keeps_evidence_and_forbids_extra_fields_at_every_depth():
    parsed = s.RotaExtraction.model_validate(extraction())
    assert parsed.assignments[0].cell_ref == 'Sheet1!B2'
    assert parsed.people[0].suggested_person_id is None
    assert parsed.people[0].reason == 'Exact staff number'
    schema = s.RotaExtraction.model_json_schema()
    assert schema['additionalProperties'] is False
    assert all(definition.get('additionalProperties') is False for definition in schema['$defs'].values()
               if definition.get('type') == 'object')
    for where in (None, 'files', 'people', 'duty_codes', 'assignments'):
        data = extraction()
        (data if where is None else data[where][0])['execute'] = 'hostile extra instruction'
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            s.RotaExtraction.model_validate(data)


@pytest.mark.parametrize('collection,field,value', [
    ('files', 'layout', 'invented'), ('files', 'confidence', 1.01), ('files', 'confidence', float('nan')),
    ('people', 'suggested_person_id', 'invented'), ('people', 'cell_refs', []),
    ('people', 'reason', ''), ('duty_codes', 'kind', 'holiday'), ('assignments', 'start', '25:00'),
    ('assignments', 'end', None),
])
def test_extraction_rejects_unsupported_or_untraceable_output(collection, field, value):
    data = extraction()
    data[collection][0][field] = value
    with pytest.raises(ValidationError):
        s.RotaExtraction.model_validate(data)


def test_upload_requires_only_files_and_explicit_cloud_privacy_acknowledgement():
    upload = {'files': [{'file_name': 'rota.csv', 'content_base64': 'YQ=='}]}
    parsed = s.ScoreCheckImportInput.model_validate(upload)
    assert parsed.name is None and parsed.starts_on is None and parsed.ends_on is None
    assert parsed.use_ai is False and parsed.profile_id is None
    assert parsed.confidence_threshold == .9 and parsed.manual_time_hours is None
    with pytest.raises(ValidationError):
        s.ScoreCheckImportInput(files=[])
    with pytest.raises(ValidationError, match='Acknowledge'):
        s.ScoreCheckImportInput.model_validate({**upload, 'use_ai': True})
    assert s.ScoreCheckImportInput.model_validate({**upload, 'use_ai': True, 'privacy_acknowledged': True}).use_ai
    with pytest.raises(ValidationError, match='both period dates'):
        s.ScoreCheckImportInput.model_validate({**upload, 'starts_on': '2026-11-01'})
    with pytest.raises(ValidationError, match='1–93 days'):
        s.ScoreCheckImportInput.model_validate({**upload, 'starts_on': '2026-11-01', 'ends_on': '2026-11-01'})


def test_gap_decisions_keep_add_match_exclude_resolve_and_undo_explicit():
    base = {'batch_id': str(uuid4()), 'gap_id': 'person:rota.csv:2'}
    for action in ('acknowledge', 'confirm', 'exclude'):
        assert s.ScoreCheckGapDecisionInput(**base, action=action).action == action
    for action in ('add', 'resolve', 'match'):
        with pytest.raises(ValidationError):
            s.ScoreCheckGapDecisionInput(**base, action=action)
    location = str(uuid4())
    assert s.ScoreCheckGapDecisionInput(**base, action='match', target_id=str(uuid4()), location_id=location).location_id == location
    assert s.ScoreCheckGapDecisionInput(**base, action='resolve', replacement={
        'person': 'Synthetic person (test)', 'date': '2026-11-01', 'file_ref': 'rota.csv',
        'cell_ref': 'Sheet1!B2', 'duty_code': 'AM'}).replacement
    with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
        s.ScoreCheckGapDecisionInput(**base, action='add', replacement={'display_name': 'Synthetic person', 'active': False})
    decision = str(uuid4())
    assert s.ScoreCheckUndoInput(batch_id=base['batch_id'], decision_id=decision).decision_id == decision


@pytest.mark.parametrize('field', ['group', 'made_by'])
def test_source_updates_reject_explicit_null_nonnullable_metadata(field):
    source = str(uuid4())
    assert s.ScoreCheckSourceUpdate(source_id=source).model_fields_set == {'source_id'}
    with pytest.raises(ValidationError, match='empty group or empty people list'):
        s.ScoreCheckSourceUpdate.model_validate({'source_id': source, field: None})
    assert s.ScoreCheckSourceUpdate(source_id=source, group='', made_by=[]).made_by == []


@pytest.mark.parametrize('dates', [
    {'starts_on': '2026-11-01'}, {'ends_on': '2026-12-01'}, {'starts_on': None},
    {'starts_on': '2026-11-01', 'ends_on': None},
    {'starts_on': '2026-11-01', 'ends_on': '2026-11-01'},
    {'starts_on': '2026-11-02', 'ends_on': '2026-11-01'},
    {'starts_on': '2026-11-01', 'ends_on': '2027-03-01'},
])
def test_source_updates_require_a_valid_paired_period(dates):
    with pytest.raises(ValidationError, match='file-period dates|1–93 days'):
        s.ScoreCheckSourceUpdate(source_id=str(uuid4()), **dates)


def test_source_period_can_be_edited_or_cleared_and_batch_exposes_inference():
    source = str(uuid4())
    assert s.ScoreCheckSourceUpdate(source_id=source, starts_on=None, ends_on=None).starts_on is None
    assert s.ScoreCheckSourceUpdate(source_id=source, starts_on='2026-11-01', ends_on='2026-12-01').ends_on.isoformat() == '2026-12-01'
    assert s.ScoreCheckBatch.model_fields['period_inferred'].default is True
    assert s.ScoreCheckUpdateInput(batch_id=str(uuid4())).period_inferred is None
    for inferred in (True, False):
        assert s.ScoreCheckUpdateInput(batch_id=str(uuid4()), period_inferred=inferred).period_inferred is inferred


def test_provider_profiles_have_no_default_vendor_and_secrets_are_write_only():
    config = {'name': 'Synthetic local profile', 'kind': 'openai_chat', 'base_url': 'http://localhost:8000/v1',
              'model': 'configured-model'}
    profile = s.AiProviderProfileInput.model_validate(config)
    assert not profile.enabled and not profile.is_default and not profile.allow_private_endpoint
    assert profile.structured_output == 'json_schema'
    assert s.PERMISSIONS['ai.providers'] == ('operator',)
    assert 'benchmark' in s.APP_ROUTE_PARTS
    with pytest.raises(ValidationError):
        s.AiProviderProfileInput(name='Unconfigured')
    with pytest.raises(ValidationError):
        s.AiProviderProfileInput.model_validate({**config, 'key_ciphertext': 'synthetic-not-a-key'})
    merged = s.AiProviderProfileInput.model_validate({**config, 'api_key': 'synthetic-not-a-key'})
    assert merged.api_key == 'synthetic-not-a-key' and 'api_key' not in merged.model_dump()
    assert 'synthetic-not-a-key' not in repr(merged)
    assert not {'api_key', 'key_ciphertext'} & s.AiProviderProfileRecord.model_fields.keys()
    key = s.AiProviderKeyInput(profile_id=str(uuid4()), api_key=' synthetic-not-a-key ')
    assert key.api_key == ' synthetic-not-a-key ' and 'synthetic-not-a-key' not in repr(key)
    assert key.model_json_schema()['properties']['api_key']['writeOnly'] is True
    assert not {'prompt', 'response', 'content', 'names'} & s.AiCallRecord.model_fields.keys()


def test_canonical_import_time_check_and_provider_settings_contracts():
    identifier = str(uuid4())
    parsed = s.ScoreImport(id=identifier, revision=1, saved_workspace_id=None, sources=[], time=None,
                           profile_id=None, groups=[])
    assert parsed.time is None
    assert s.ScoreImportSetTime(import_id=identifier, time={'work_hours': 40, 'people': 4}).time.work_hours == 40
    assert s.ScoreImportSetTime(import_id=identifier, time=None).time is None
    assert s.ScoreImportDecide(import_id=identifier, item_id='cell:1', decision=None).decision is None
    with pytest.raises(ValidationError, match='new record value'):
        s.ImportDecisionInput(kind='add')
    with pytest.raises(ValidationError, match='existing match'):
        s.ImportDecisionInput(kind='match')
    assert s.AiSettings(enabled=False, revision=1, profiles=[]).enabled is False
    assert s.AiEnabledInput(enabled=True).reason == ''
    assert s.AI_PROVIDER_KINDS == ('openai_compatible', 'gemini', 'anthropic', 'azure')
    assert 'budget' not in s.AI_REASONING_VALUES
    assert s.ScoreImportUpdateSource(import_id=identifier, source_id=identifier, period_start='2026-11-01').period_start
    assert s.ScoreImportUpdateSource(import_id=identifier, source_id=identifier, period_end='2026-12-01').period_end
    context = s.ReadingContext(file_ref='rota.csv', cell_ref='Sheet1!B2')
    assert context.cell_ref == 'Sheet1!B2'


def test_canonical_benchmark_preserves_unknown_score_and_numeric_units():
    side = s.BenchmarkSide(workspace_id=str(uuid4()), name='Synthetic draft', origin='import', detail='',
        score=None, publishable=False, before_cap=None, blocking=0, warnings=0, not_covered=0,
        fairness_gap=None, within_fair=None, people=0, wishes_met=None, time_hours=None, time_basis=None,
        time_detail='', parts=[{'label': 'Coverage', 'value': -3.6}, {'label': 'Stability', 'value': None}])
    assert side.score is None and side.fairness_gap is None
    assert side.parts[0].value == -3.6
    assert s.BenchmarkImportSummary(added=2, left_out=36, acknowledged=4).left_out == 36


def test_benchmark_reuses_whole_score_and_does_not_invent_missing_time_or_baseline():
    assert s.ScoreCheckBenchmarkRota.model_fields['score'].annotation == s.ScheduleScore | None
    assert s.ScoreCheckTime(source='not_entered', missing_files=1).work_hours is None
    assert s.ScoreCheckTime(source='measured', solver_seconds=3).review_hours is None
    assert s.ScoreCheckBenchmarkRota.model_fields['stability'].default is None
    assert s.ScoreCheckGapCounts().model_dump() == {
        'missing': 0, 'unknown': 0, 'uncertain': 0, 'unreadable': 0,
        'added': 0, 'excluded': 0, 'acknowledged': 0, 'unresolved': 0}
    key = str(uuid4())
    with pytest.raises(ValidationError, match='each draft once'):
        s.ScoreCheckBenchmarkInput(workspace_ids=[key, key])


def test_generated_public_contracts_match_their_pydantic_source():
    content, schemas = render()
    for name in ('RotaExtraction', 'ScoreCheckBatch', 'ScoreCheckGapDecisionInput', 'ScoreCheckUndoInput',
                 'ScoreCheckBenchmarkResult', 'AiProviderProfileInput', 'AiProviderKeyInput', 'AiCallRecord',
                 'ScoreImport', 'ImportCheck', 'Benchmark', 'ScoreImportDecide', 'AiSettings', 'AiEnabledInput',
                 'ScoreProviderChoices', 'ScoreProviderChoice', 'ImportMatch', 'ImportMatches'):
        assert f'export type {name} = ' in content
        assert schemas[name] == getattr(s, name).model_json_schema()
    assert 'export const SCORE_CHECK_LIMITS = ' in content
    for name in ('ScoreImportUpload', 'ScoreImportAddFiles'):
        assert schemas[name]['properties']['files']['items']['format'] == 'binary'
        assert f'export type {name} = ' in content
    assert '"files": Array<File>;' in content
    assert schemas['ScoreImportAddFiles']['required'] == ['files', 'revision']
    assert 'content_base64' not in content
    assert 'ScoreCheckImportInput' not in schemas
    assert not any(name.startswith('InternalScoreImport') for name in schemas)
    assert (ROOT / 'apps/web/src/generated.ts').read_text(encoding='utf-8') == content


def test_migration_is_additive_and_compiles_for_postgresql():
    assert declaration_errors(MIGRATION) == []
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        migration().upgrade()
    sql = output.getvalue()
    assert 'ADD COLUMN origin' in sql
    assert 'DROP ' not in sql and 'DELETE ' not in sql and 'UPDATE ' not in sql
    assert all(f'CREATE TABLE {model.__tablename__}' in sql for model in NEW_MODELS)
    assert declaration_errors(PERIOD_MIGRATION) == []
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        migration(PERIOD_MIGRATION).upgrade()
    assert 'ADD COLUMN period_inferred BOOLEAN DEFAULT true NOT NULL' in output.getvalue()
    assert declaration_errors(GATE_MIGRATION) == []
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        migration(GATE_MIGRATION).upgrade()
    assert 'ADD COLUMN score_check_enabled BOOLEAN DEFAULT false NOT NULL' in output.getvalue()


def test_migration_preserves_workspaces_and_installs_model_columns_and_safe_defaults():
    engine = sa.create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('departments', 'identities', 'publications', 'locations'):
            db.exec_driver_sql(f'CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)')
        db.exec_driver_sql('CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY, name TEXT, status TEXT)')
        db.exec_driver_sql("INSERT INTO workspaces VALUES ('workspace','Existing draft','draft')")
        db.exec_driver_sql("INSERT INTO departments VALUES ('department')")
        db.exec_driver_sql("INSERT INTO identities VALUES ('actor')")
        before = list(db.exec_driver_sql('SELECT * FROM workspaces'))
        with Operations.context(MigrationContext.configure(db)):
            migration().upgrade()
        db.exec_driver_sql("INSERT INTO score_check_import_batches "
            "(id,revision,created_at,department_id,created_by,name,starts_on,ends_on) "
            "VALUES ('batch',1,'2026-11-01','department','actor','Retained import','2026-11-01','2026-12-01')")
        retained_batch = list(db.exec_driver_sql('SELECT * FROM score_check_import_batches'))
        with Operations.context(MigrationContext.configure(db)):
            migration(PERIOD_MIGRATION).upgrade()
        assert list(db.exec_driver_sql('SELECT * FROM score_check_import_batches'))[0][:-1] == retained_batch[0]
        assert db.exec_driver_sql('SELECT period_inferred FROM score_check_import_batches').scalar_one() == 1
        db.exec_driver_sql("UPDATE score_check_import_batches SET period_inferred=false WHERE id='batch'")
        assert db.exec_driver_sql('SELECT period_inferred FROM score_check_import_batches').scalar_one() == 0
        assert list(db.exec_driver_sql('SELECT id,name,status FROM workspaces')) == before
        assert db.exec_driver_sql('SELECT origin FROM workspaces').scalar_one() == 'planner'
        inspector = sa.inspect(db)
        for model in NEW_MODELS:
            actual = {column['name']: column['nullable'] for column in inspector.get_columns(model.__tablename__)}
            assert actual == {column.name: column.nullable for column in model.__table__.columns}
        profile = sa.Table('ai_provider_profiles', sa.MetaData(), autoload_with=db)
        values = {'id': str(uuid4()), 'revision': 1, 'created_at': '2026-11-01', 'department_id': 'department',
                  'created_by': 'actor', 'name': 'Synthetic profile', 'kind': 'openai_chat',
                  'base_url': 'https://example.invalid/v1', 'model': 'configured-model'}
        # Driver SQL keeps this a database-default check rather than applying ORM defaults.
        columns = ','.join(values)
        db.exec_driver_sql(f"INSERT INTO ai_provider_profiles ({columns}) VALUES ({','.join('?' for _ in values)})",
                           tuple(values.values()))
        stored = db.execute(sa.select(profile)).mappings().one()
        assert stored['enabled'] is False and stored['is_default'] is False
        assert stored['key_ciphertext'] is None and stored['allow_private_endpoint'] is False
        assert stored['reasoning'] == 'none'
        assert stored['provider_kind'] is None and stored['api_style'] == 'chat'
        assert db.exec_driver_sql('SELECT ai_enabled FROM departments').scalar_one() == 0
        assert db.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    engine.dispose()
