import copy
from importlib import import_module

from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.serialization import generation_defaults_record
from apps.worker.main import claim_job, prepare_deferred_generation
from packages.contracts.schemas import DepartmentSettingsInput, GenerationOptions
from packages.domain.solver_rules import generation_manifest
from scripts.manage import alembic_config, migrate
from .migration_support import current_seed_columns, historical_seed


def settings(h, defaults=None):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'reason': 'Synthetic default assignment preservation check'}
    if defaults is not None:
        payload['generation_defaults'] = defaults
    return h.command('admin', 'department.settings', payload, dept['id'], org['revision'])


def test_new_and_inherited_department_defaults_keep_assignments(h):
    assert GenerationOptions().mode == 'fill_gaps'
    assert h.get('coordinator', '/api/policies/capabilities')['generation_defaults']['mode'] == 'fill_gaps'
    assert generation_manifest()['default_options']['mode'] == 'fill_gaps'
    first = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[first])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    assert claim and claim['id'] == job['id']
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot['generation_defaults']['mode'] == snapshot['generation_options']['mode'] == 'fill_gaps'
        assert snapshot['shifts'][0]['participants'][0]['person_id'] == first
        assert db.get(m.Department, h.seed['department_id']).generation_defaults == {}


@pytest.mark.parametrize('mode', [None, 'rebalance', 'fill_gaps'])
def test_saving_unrelated_settings_preserves_mode_provenance_and_audits_explicit_choices(h, mode):
    if mode is not None:
        saved = settings(h, {'mode': mode})
        assert saved['department']['generation_defaults']['mode'] == mode
        with transaction(h.app.state.engine) as db:
            history = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings'))
            assert history.snapshot['generation_defaults']['mode'] == mode
            assert history.reason == 'Synthetic default assignment preservation check'
            audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'department.settings'))
            assert 'generation_defaults' in audit.detail['payload_fields']
    saved = settings(h, {'search_seconds': 600})
    defaults = saved['department']['generation_defaults']
    assert defaults['search_minutes'] == 10
    if mode is None:
        assert 'mode' not in defaults
    else:
        assert defaults['mode'] == mode
    unchanged = settings(h)
    assert unchanged['department'] == saved['department']
    assert GenerationOptions.model_validate(defaults).mode == (mode or 'fill_gaps')
    w = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).input_snapshot['generation_options']['mode'] == (mode or 'fill_gaps')


def test_default_adoption_migration_adds_audit_without_rewriting_settings(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'old-default.db')
    command.upgrade(alembic_config(url), '0060_quota_line_contract')
    engine = make_engine(url)
    with current_seed_columns(engine), transaction(engine, write=True) as db:
        seed = historical_seed(db)
        department = db.get(m.Department, seed['department_id'])
        department.generation_defaults = {'scoring': {'wish_budget_points': 6}}
        departments = [department]
        for mode in ('rebalance', 'fill_gaps'):
            row = m.Department(organisation_id=seed['organisation_id'], name='Saved ' + mode,
                               generation_defaults={'mode': mode}, revision=4)
            db.add(row)
            departments.append(row)
        db.flush()
        before = {row.id: (row.revision, copy.deepcopy(row.generation_defaults)) for row in departments}
    migrate(url)
    with transaction(engine) as db:
        assert {row.id: (row.revision, row.generation_defaults) for row in db.scalars(select(m.Department))} == before
        events = list(db.scalars(select(m.AuditEvent)))
        assert len(events) == 1 and events[0].department_id == seed['department_id']
        assert events[0].action == 'department.generation_default'
        assert events[0].detail == {'migration_revision': '0061_keep_assignments_default',
            'field': 'generation_defaults.mode', 'before': 'rebalance', 'after': 'fill_gaps', 'source': 'inherited_default'}
        actor = db.get(m.Identity, events[0].actor_id)
        assert actor.disabled and actor.issuer == 'system-migration'
        assert actor.password_hash is None and actor.person_id is None
        assert not db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == actor.id))
    migrate(url)
    with transaction(engine) as db:
        assert len(list(db.scalars(select(m.AuditEvent)))) == 1
    engine.dispose()


@pytest.mark.postgres
def test_default_adoption_migration_inserts_audit_on_postgres(pg):
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        department = db.get(m.Department, pg.seed['department_id'])
        department.generation_defaults = {'scoring': {'wish_budget_points': 6}}
        saved = m.Department(organisation_id=pg.seed['organisation_id'], name='Saved Rebalance',
                             generation_defaults={'mode': 'rebalance'}, revision=4)
        db.add(saved)
        db.flush()
        before = {row.id: (row.revision, copy.deepcopy(row.generation_defaults)) for row in (department, saved)}
    migration = import_module('migrations.versions.0061_keep_assignments_default')
    with engine.begin() as bind, Operations.context(MigrationContext.configure(bind)):
        migration.upgrade()
    with transaction(engine) as db:
        assert {row.id: (row.revision, row.generation_defaults) for row in db.scalars(select(m.Department))} == before
        events = list(db.scalars(select(m.AuditEvent)))
        assert len(events) == 1 and events[0].department_id == department.id
        assert events[0].action == 'department.generation_default'
        assert events[0].detail['before'] == 'rebalance' and events[0].detail['after'] == 'fill_gaps'
        actor = db.get(m.Identity, events[0].actor_id)
        assert actor.disabled and actor.issuer == 'system-migration' and actor.password_hash is None
        assert not db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == actor.id))


@pytest.mark.parametrize('defaults', [None, {'scoring': {'wish_budget_points': 6}}])
def test_settings_undo_does_not_pin_an_inherited_assignment_mode(h, defaults):
    before = h.get('admin', '/api/me')['organisation']
    saved = settings(h, defaults)
    assert 'mode' not in saved['previous'].get('generation_defaults', {})
    restored = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo settings'},
                         h.seed['department_id'], saved['organisation']['revision'])
    with transaction(h.app.state.engine) as db:
        assert 'mode' not in db.get(m.Department, h.seed['department_id']).generation_defaults
    if defaults is None:
        assert restored['organisation'] == before


def test_explicit_default_mode_choice_still_invalidates_generation_inputs(h):
    before = h.get('admin', '/api/me')['organisation']['input_revision']
    saved = settings(h, {'mode': 'fill_gaps'})
    assert saved['organisation']['input_revision'] == before + 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Department, h.seed['department_id']).generation_defaults['mode'] == 'fill_gaps'


@pytest.mark.parametrize('defaults', [{}, {'mode': 'rebalance'}, {'mode': 'fill_gaps'}, {
    'weight_preset': 'fairness_first', 'objective_order': ['balance', 'changes', 'preferences', 'targets'],
    'scoring': {'wish_budget_points': 6}}], ids=['inherited', 'rebalance', 'fill-gaps', 'legacy'])
def test_settings_undo_restores_assignment_mode_provenance_and_next_run(h, defaults):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).generation_defaults = copy.deepcopy(defaults)
    inherited = 'mode' not in defaults
    expected = GenerationOptions.model_validate(generation_defaults_record(defaults)).model_dump(mode='json')
    if inherited:
        expected.pop('mode')
    saved = settings(h, {'mode': 'fill_gaps' if defaults.get('mode') == 'rebalance' else 'rebalance',
                         'search_seconds': 600})
    assert saved['previous'].get('inherit_generation_mode', False) is inherited
    payload = {**saved['previous'], 'reason': 'Undo assignment mode and other defaults'}
    restored = h.command('admin', 'department.settings', payload, h.seed['department_id'],
                         saved['organisation']['revision'], key='settings-mode-undo-replay')
    replay = h.command('admin', 'department.settings', payload, h.seed['department_id'],
                       saved['organisation']['revision'], key='settings-mode-undo-replay')
    assert replay == restored
    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, h.seed['department_id'])
        assert department.generation_defaults == expected
        assert department.revision == restored['department']['revision']
        assert ('mode' not in department.generation_defaults) is inherited
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).input_snapshot['generation_options']['mode'] == defaults.get('mode', 'fill_gaps')
        assert db.get(m.Department, h.seed['department_id']).generation_defaults == expected


@pytest.mark.parametrize('options', [
    {'inherit_generation_mode': True, 'generation_defaults': {'mode': 'rebalance'}},
    {'inherit_generation_mode': True, 'generation_defaults': {'mode': 'fill_gaps'}},
    {'inherit_generation_mode': None}, {'inherit_generation_mode': 1}, {'inherit_generation_mode': 'true'},
])
def test_settings_inherited_mode_marker_rejects_ambiguous_or_nonboolean_values(options):
    payload = {'department_name': 'Synthetic department', 'department_revision': 1,
               'organisation_name': 'Synthetic organisation', 'timezone': 'Asia/Riyadh', 'week_start': 6,
               'reason': 'Synthetic invalid mode restore check', **options}
    with pytest.raises(ValidationError):
        DepartmentSettingsInput.model_validate(payload)
