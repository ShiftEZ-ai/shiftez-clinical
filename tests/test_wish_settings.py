"""Department wish-kind policy uses the existing settings, audit, Undo and wish budget paths."""
import importlib
import json
from typing import get_args
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from scripts.generate_contracts import render
from .test_staff_wishes import department_settings


def save_settings(h, types, **extra):
    return h.command('admin', 'department.settings', {**department_settings(h),
        'wish_settings': {'types': types}, **extra}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])


def payload(h, kind='prefer_off', day='2026-10-05', **extra):
    return {'person_id': h.seed['people'][0], 'first_day': day, 'last_day': day, 'kind': kind,
            **({'assignment_type_id': h.seed['type']} if kind.endswith('shift_type') else {}),
            **({'limit': 2} if kind in s.PATTERN_WISH_KINDS - {'whole_weekends'} else {}), **extra}


def test_defaults_and_generated_contracts_use_the_existing_kind_and_role_literals():
    expected = {kind: {'allowed': True, 'may_repeat': True, 'default_points': 1,
                       'approval': 'none', 'approver_roles': []} for kind in get_args(s.WishKind)}
    assert s.WISH_KINDS == get_args(s.WishKind)
    assert s.WishSettings().model_dump() == {'types': expected}
    # Partial configuration fills every omitted kind and field; instances never share mutable defaults.
    first = s.WishSettings(types={'prefer_off': {'default_points': 2}})
    assert first.types['prefer_on'].default_points == 1
    first.types['prefer_off'].approver_roles.append(s.ROLE_NAMES[0])
    assert s.WishSettings().model_dump() == {'types': expected}
    source, schemas = render()
    assert f'export const WISH_KINDS = {json.dumps(list(s.WISH_KINDS))} as const;' in source
    assert f'export const MAX_WISH_POINTS = {s.MAX_WISH_POINTS};' in source
    assert f'export const WISH_RANGE_MAX_DAYS = {s.WISH_RANGE_MAX_DAYS};' in source
    assert 'export type WishSettings' in source and 'export type WishTypeSettings' in source
    kind_union = ' | '.join(json.dumps(kind) for kind in s.WISH_KINDS)
    assert f'Partial<Record<{kind_union}, WishTypeSettings>>' in source
    definitions = schemas['DepartmentSettingsInput']['$defs']
    assert definitions['WishSettings']['properties']['types']['propertyNames']['enum'] == list(s.WISH_KINDS)
    assert definitions['WishTypeSettings']['properties']['approver_roles']['items']['enum'] == list(s.ROLE_NAMES)
    points = definitions['WishTypeSettings']['properties']['default_points']
    assert (points['minimum'], points['maximum']) == (1, s.MAX_WISH_POINTS)
    points = schemas['StaffWishInput']['properties']['points']
    assert (points['minimum'], points['maximum']) == (1, s.MAX_WISH_POINTS)
    budget = definitions['ScoringSettings']['properties']['wish_budget_points']
    assert (budget['minimum'], budget['maximum']) == (0, s.MAX_WISH_POINTS)


@pytest.mark.parametrize('field,value', [
    ('default_points', 0), ('default_points', -1), ('default_points', True), ('default_points', 1.5),
    ('default_points', '2'), ('default_points', s.MAX_WISH_POINTS + 1),
    ('approval', 'sometimes'), ('approver_roles', ['invented_role']),
    ('approver_roles', ['admin', 'admin']), ('unknown_setting', True),
])
def test_invalid_type_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        s.WishSettings(types={'prefer_off': {field: value}})


def test_unknown_kinds_are_rejected_and_all_contract_roles_are_valid():
    with pytest.raises(ValidationError):
        s.WishSettings(types={'new_kind': {}})
    value = s.WishTypeSettings(approver_roles=list(s.ROLE_NAMES))
    assert value.approver_roles == list(s.ROLE_NAMES)
    for amount in (1, 101, s.MAX_WISH_POINTS):
        assert s.WishTypeSettings(default_points=amount).default_points == amount
        assert s.ScoringSettings(wish_budget_points=amount).wish_budget_points == amount
    assert s.ScoringSettings(wish_budget_points=0).wish_budget_points == 0


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_defaults_preserve_creating_every_kind_without_points(request, database):
    h = request.getfixturevalue(database)
    settings = s.WishSettings().model_dump()
    assert h.get('staff', '/api/me')['department']['wish_settings'] == settings
    for index, kind in enumerate(s.WISH_KINDS):
        created = h.command('staff', 'wish.create', payload(h, kind, f'2026-10-{5 + index:02}'))['wish']
        assert created['kind'] == kind and created['points'] == 1 and created['status'] == 'open'
    listed = h.get('staff', '/api/wishes?start=2026-10-01&end=2026-11-01')
    assert listed['type_settings'] == settings['types']
    assert (listed['budget_points'], listed['period']) == (10, 'month')
    assert listed['usage'][0]['used'] == len(s.WISH_KINDS)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_defaults_positive_large_points_budget_and_disabled_kinds(request, database):
    h = request.getfixturevalue(database)
    save_settings(h, {'prefer_off': {'allowed': False}, 'prefer_on': {'default_points': 1},
                      'prefer_shift_type': {'default_points': 101}, 'avoid_shift_type': {'default_points': 4}},
                  generation_defaults={'scoring': {'wish_budget_points': 110}})
    refused = h.command('staff', 'wish.create', payload(h), status=422)['error']
    assert refused['code'] == 'WISH_KIND_DISABLED' and 'not allowed' in refused['message']
    minimum = h.command('staff', 'wish.create', payload(h, 'prefer_on'))['wish']
    large = h.command('staff', 'wish.create', payload(h, 'prefer_shift_type', '2026-10-06'))['wish']
    # Explicit points retain today's meaning, even if they differ from the configured default.
    explicit = h.command('staff', 'wish.create', payload(h, 'avoid_shift_type', '2026-10-07', points=8))['wish']
    assert (minimum['points'], large['points'], explicit['points']) == (1, 101, 8)
    refused = h.command('staff', 'wish.create', payload(h, 'avoid_shift_type', '2026-10-08'), status=409)
    assert refused['error']['code'] == 'WISH_BUDGET'  # configured default must enter the same budget check
    with transaction(h.app.state.engine) as db:
        assert db.get(m.StaffWish, large['id']).points == 101
    workspace = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(workspace)
    assert next(w for w in snapshot['wishes'] if w['id'] == minimum['id'])['weight'] == 1 / 110
    assert next(w for w in snapshot['wishes'] if w['id'] == large['id'])['weight'] == 101 / 110


def test_disabled_kind_preserves_existing_wish_and_update_points(h):
    wish = h.command('staff', 'wish.create', payload(h, points=3))['wish']
    save_settings(h, {'prefer_off': {'allowed': False, 'default_points': 8}, 'prefer_on': {'allowed': False}})
    # Previously submitted wishes remain editable and evaluable; disabling controls new wishes/kind changes.
    changed = h.command('staff', 'wish.update', payload(h, note='Changed note'), wish['id'], wish['revision'])
    assert changed['wish']['points'] == changed['previous']['points'] == 3
    refused = h.command('staff', 'wish.update', payload(h, 'prefer_on'), wish['id'], changed['wish']['revision'], status=422)
    assert refused['error']['code'] == 'WISH_KIND_DISABLED'
    assert h.snapshot(h.shift(h.workspace())['workspace'])['wishes'][0]['points'] == 3


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_settings_are_validated_audited_undoable_and_preserved_when_omitted(request, database):
    h = request.getfixturevalue(database)
    before = h.get('admin', '/api/me')
    initial = before['department']['wish_settings']
    types = {kind: {'allowed': False, 'may_repeat': False, 'default_points': 1,
                    'approval': 'none', 'approver_roles': []}
             for index, kind in enumerate(s.WISH_KINDS)}
    for roles in (['not_a_role'], ['admin', 'admin']):
        invalid = {**department_settings(h), 'wish_settings': {'types': {'prefer_off': {'approver_roles': roles}}}}
        h.command('admin', 'department.settings', invalid, h.seed['department_id'], before['organisation']['revision'], status=422)
    invalid = {**department_settings(h), 'wish_settings': {'types': {'prefer_off': {'default_points': 0}}}}
    h.command('admin', 'department.settings', invalid, h.seed['department_id'], before['organisation']['revision'], status=422)
    denied = {**department_settings(h), 'wish_settings': {'types': types}}
    h.command('coordinator', 'department.settings', denied, h.seed['department_id'], before['organisation']['revision'], status=403)
    saved = save_settings(h, types)
    assert saved['previous']['wish_settings'] == initial
    assert saved['department']['wish_settings'] == {'types': types}
    assert saved['department']['revision'] == before['department']['revision'] + 1
    assert saved['organisation']['input_revision'] == before['organisation']['input_revision'] + 1
    h.command('admin', 'department.settings', denied, h.seed['department_id'], before['organisation']['revision'], status=409)
    kept = h.command('admin', 'department.settings', department_settings(h), h.seed['department_id'], saved['organisation']['revision'])
    assert kept['department'] == saved['department']
    assert h.get('staff', '/api/wishes')['type_settings'] == types
    assert h.get('admin', '/api/me')['department']['wish_settings'] == {'types': types}
    undo = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo preference settings'},
                     h.seed['department_id'], saved['organisation']['revision'])
    assert undo['department']['wish_settings'] == initial
    with transaction(h.app.state.engine) as db:
        history = list(db.scalars(sa.select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings')
                                 .order_by(m.DefinitionHistory.object_revision)))
        assert len(history) == 2
        assert history[0].snapshot['wish_settings'] == {'types': types}
        assert history[1].snapshot['wish_settings'] == initial
        assert history[1].reason == 'Undo preference settings'


@pytest.mark.parametrize('settings', [
    {'approval': 'always'}, {'approval': 'when_soft_clash'}, {'approver_roles': ['approver']}])
def test_unsupported_approval_settings_are_refused_without_saving_a_noop(h, settings):
    before = h.get('admin', '/api/me')
    refused = h.command('admin', 'department.settings', {**department_settings(h),
        'wish_settings': {'types': {'prefer_off': settings}}}, h.seed['department_id'],
        before['organisation']['revision'], status=422)
    assert refused['error']['code'] == 'WISH_APPROVAL_UNSUPPORTED'
    assert h.get('admin', '/api/me')['department'] == before['department']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_repeat_policy_blocks_new_standing_wishes_and_one_off_conversion(request, database):
    h = request.getfixturevalue(database)
    save_settings(h, {'prefer_off': {'may_repeat': False, 'default_points': 3},
                      'prefer_on': {'allowed': False}})
    repeat = {'weekdays': [0], 'starts_on': '2026-10-01'}
    wish = h.command('staff', 'wish.create', payload(h, day='2026-10-06'))['wish']
    assert wish['points'] == 3 and wish['repeat'] is None
    refused = h.command('staff', 'wish.create', payload(h, repeat=repeat), status=422)
    assert refused['error']['code'] == 'WISH_REPEAT_DISABLED'
    refused = h.command('staff', 'wish.update', payload(h, repeat=repeat), wish['id'], wish['revision'], status=422)
    assert refused['error']['code'] == 'WISH_REPEAT_DISABLED'
    refused = h.command('staff', 'wish.create', payload(h, 'prefer_on', repeat=repeat), status=422)
    assert refused['error']['code'] == 'WISH_KIND_DISABLED'
    assert h.get('staff', '/api/wishes')['wishes'] == [{**wish, 'locked': False}]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_existing_repeat_keeps_its_points_and_edits_after_policy_is_disabled(request, database):
    h = request.getfixturevalue(database)
    save_settings(h, {'prefer_off': {'default_points': 3}})
    repeat = {'weekdays': [0], 'starts_on': '2026-10-01'}
    wish = h.command('staff', 'wish.create', payload(h, repeat=repeat))['wish']
    assert wish['points'] == 3
    save_settings(h, {'prefer_off': {'allowed': False, 'may_repeat': False, 'default_points': 8},
                      'prefer_on': {'may_repeat': False}, 'avoid_shift_type': {'allowed': False}})
    changed = h.command('staff', 'wish.update', payload(h, repeat=repeat, note='Keep the standing preference'),
                        wish['id'], wish['revision'])
    assert changed['wish']['repeat'] == changed['previous']['repeat'] == wish['repeat']
    assert changed['wish']['points'] == changed['previous']['points'] == 3
    for kind, code in [('prefer_on', 'WISH_REPEAT_DISABLED'), ('avoid_shift_type', 'WISH_KIND_DISABLED')]:
        refused = h.command('staff', 'wish.update', payload(h, kind, repeat=repeat),
                            wish['id'], changed['wish']['revision'], status=422)
        assert refused['error']['code'] == code
    listed = h.get('staff', '/api/wishes')['wishes']
    assert listed == [{**changed['wish'], 'locked': False}]
    concrete = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])['wishes']
    assert concrete and all(w['standing_wish_id'] == wish['id'] and w['points'] == 3 for w in concrete)


def test_migration_preserves_existing_rows_checks_indexes_and_triggers(tmp_path):
    """Exercise SQLite's table-copy path with legacy unnamed checks and existing data."""
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'old-wishes.db'))
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE departments (id TEXT PRIMARY KEY)')
        db.exec_driver_sql("CREATE TABLE staff_wishes (id TEXT PRIMARY KEY, department_id TEXT, first_day TEXT, last_day TEXT, "
                           "kind TEXT, points INTEGER, status TEXT, CHECK(last_day >= first_day), CHECK(points BETWEEN 1 AND 10), "
                           "CHECK(status IN ('open','withdrawn')), "
                           "CHECK(kind IN ('prefer_off','prefer_on','prefer_shift_type','avoid_shift_type')))")
        db.exec_driver_sql('CREATE INDEX ix_staff_wishes_department_days ON staff_wishes(department_id,first_day)')
        db.exec_driver_sql("CREATE TRIGGER wishes_guard BEFORE UPDATE ON staff_wishes WHEN NEW.points < 0 "
                           "BEGIN SELECT RAISE(ABORT, 'invalid points'); END")
        db.exec_driver_sql("INSERT INTO departments VALUES ('d')")
        db.exec_driver_sql("INSERT INTO staff_wishes VALUES ('w','d','2026-10-05','2026-10-05','prefer_off',3,'open')")
        before = db.exec_driver_sql('SELECT * FROM staff_wishes').all()
        migration = importlib.import_module('migrations.versions.0097_wish_type_settings')
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        assert db.exec_driver_sql('SELECT * FROM staff_wishes').all() == before
        assert json.loads(db.exec_driver_sql('SELECT wish_settings FROM departments').scalar_one()) == {}
        checks = sa.inspect(db).get_check_constraints('staff_wishes')
        assert len(checks) == 4 and any('points BETWEEN 1 AND 2147483647' in c['sqltext'] for c in checks)
        assert sa.inspect(db).get_indexes('staff_wishes')[0]['name'] == 'ix_staff_wishes_department_days'
        assert db.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='wishes_guard'").scalar_one() == 1
        with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
            db.exec_driver_sql("INSERT INTO staff_wishes VALUES ('zero','d','2026-10-06','2026-10-06','prefer_off',0,'open')")
        db.exec_driver_sql("INSERT INTO staff_wishes VALUES ('max','d','2026-10-07','2026-10-07','prefer_off',2147483647,'open')")
        for column, value in [('points', 0), ('points', -1), ('points', s.MAX_WISH_POINTS + 1), ('kind', 'unknown'),
                              ('status', 'unknown'), ('last_day', '2026-10-01')]:
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.execute(sa.text(f'UPDATE staff_wishes SET {column}=:value WHERE id=\'w\''), {'value': value})
    engine.dispose()


@pytest.mark.postgres
def test_postgres_migration_preserves_legacy_zero_points_and_rejects_new_zero_points(pg):
    schema = 'rota_test_wishes_' + uuid.uuid4().hex
    with pg.app.state.engine.begin() as db:
        db.exec_driver_sql('CREATE SCHEMA ' + schema)
        db.exec_driver_sql('SET LOCAL search_path TO ' + schema)
        db.exec_driver_sql('CREATE TABLE departments (id TEXT PRIMARY KEY)')
        db.exec_driver_sql('CREATE TABLE staff_wishes (id TEXT PRIMARY KEY, points INTEGER CHECK(points BETWEEN 0 AND 10))')
        db.exec_driver_sql("INSERT INTO departments VALUES ('d')")
        db.exec_driver_sql("INSERT INTO staff_wishes VALUES ('legacy',0)")
        before = db.exec_driver_sql('SELECT * FROM staff_wishes').all()
        migration = importlib.import_module('migrations.versions.0097_wish_type_settings')
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        assert db.exec_driver_sql('SELECT * FROM staff_wishes').all() == before
        assert db.exec_driver_sql('SELECT wish_settings FROM departments').scalar_one() == {}
        assert db.exec_driver_sql("SELECT convalidated FROM pg_constraint WHERE conrelid='staff_wishes'::regclass "
                                 "AND conname='staff_wishes_points'").scalar_one() is False
        db.exec_driver_sql("INSERT INTO staff_wishes VALUES ('minimum',1),('maximum',2147483647)")
        for statement in ["INSERT INTO staff_wishes VALUES ('zero',0)",
                          "UPDATE staff_wishes SET points=0 WHERE id='minimum'",
                          "UPDATE staff_wishes SET id='changed' WHERE id='legacy'"]:
            with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                db.exec_driver_sql(statement)
        db.exec_driver_sql('DROP SCHEMA ' + schema + ' CASCADE')
