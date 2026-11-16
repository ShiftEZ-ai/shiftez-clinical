"""Open-ended attachments retain overlap, programme and clinical pairing checks."""
import copy
from datetime import date
import importlib
import json

from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
import pytest
from pydantic import ValidationError
import sqlalchemy as sa

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.contracts.schemas import MentorshipInput
from packages.domain import generation
from packages.domain.cp_sat import generate
from packages.domain.rule_map import rule_map
from packages.domain.time import aware
from packages.domain.validation import attached_to, evaluate
from scripts.manage import alembic_config, migrate
from .migration_support import historical_seed
from .test_attachments import END, START, people, setup, supervisor_of


@pytest.mark.parametrize('ending', [{}, {'ends_on': None}])
def test_attachment_contract_accepts_an_omitted_or_null_last_day(h, ending):
    value = MentorshipInput.model_validate({'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1],
                                           'starts_on': '2026-10-01', **ending})
    assert value.ends_on is None


@pytest.mark.parametrize('change', [
    {'ends_on': '2026-10-01'}, {'ends_on': '2026-09-30'}, {'mentor_id': 'self'},
])
def test_open_ended_attachment_contract_keeps_date_order_and_different_consultant(h, change):
    value = {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1], 'starts_on': '2026-10-01'}
    value.update(change)
    if value['mentor_id'] == 'self':
        value['mentor_id'] = value['person_id']
    with pytest.raises(ValidationError):
        MentorshipInput.model_validate(value)


@pytest.mark.parametrize('ending', [{}, {'ends_on': None}])
def test_open_ended_attachment_save_reload_edit_and_undo(h, ending):
    value = {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1],
             'starts_on': '2026-10-01', **ending}
    saved = h.configure('mentorships', value, role='coordinator')['record']
    assert saved['ends_on'] is None
    catalogues = h.get('coordinator', '/api/catalogues')
    assert next(r for r in catalogues['mentorships'] if r['id'] == saved['id'])['ends_on'] is None
    profile = h.get('staff', '/api/me/profile')['catalogues']['mentorships']
    assert next(r for r in profile if r['id'] == saved['id'])['ends_on'] is None
    assert next(r for r in h.snapshot(h.workspace())['mentorships'] if r['id'] == saved['id'])['ends_on'] is None
    updated = h.configure('mentorships', {**value, 'ends_on': '2026-11-01'}, saved['id'], saved['revision'],
                          role='coordinator')
    assert updated['record']['ends_on'] == '2026-11-01'
    restored = h.configure('mentorships', updated['previous_value'], saved['id'], updated['record']['revision'],
                           role='coordinator')['record']
    assert restored['ends_on'] is None
    assert h.get('coordinator', '/api/catalogues')['grants'] == catalogues['grants']


@pytest.mark.parametrize('other_dates', [
    {'starts_on': '2036-10-01', 'ends_on': '2036-11-01'},
    {'starts_on': '2026-09-01', 'ends_on': '2026-10-02'},
    {'starts_on': '2026-09-01', 'ends_on': None},
])
def test_open_ended_attachment_rejects_overlapping_consultants(h, other_dates):
    value = {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1], 'starts_on': '2026-10-01'}
    h.configure('mentorships', value, role='coordinator')
    h.configure('mentorships', {**value, 'mentor_id': h.seed['people'][2], **other_dates}, role='coordinator', status=409)
    h.configure('mentorships', {**value, 'starts_on': '2026-09-01', 'ends_on': '2026-10-01'}, role='coordinator')


@pytest.mark.parametrize('programme_end,attachment_end,status', [
    (None, None, 200), (None, '2026-11-01', 200),
    ('2026-11-01', None, 422), ('2026-11-01', '2026-11-01', 200),
    ('2026-11-01', '2026-11-02', 422),
])
def test_open_ended_attachment_preserves_programme_containment(h, programme_end, attachment_end, status):
    programme = h.configure('programmes', {'name': 'Synthetic attachment programme', 'starts_on': '2026-01-01',
                                          'ends_on': programme_end}, role='coordinator')['record']
    h.configure('mentorships', {'person_id': h.seed['people'][0], 'mentor_id': h.seed['people'][1],
                              'programme_id': programme['id'], 'starts_on': '2026-10-01', 'ends_on': attachment_end},
                role='coordinator', status=status)


def test_open_ended_attachment_validation_and_rule_reach(h):
    trainee, b, c, typ = setup(h, ends_on=None)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c, paired=b))['workspace'])
    shift = data['shifts'][0]
    assert attached_to(data, trainee, shift, aware(START), aware(END)) == {c: 'required'}
    assert attached_to(data, trainee, shift, aware('2036-10-04T08:00:00+03:00'),
                       aware('2036-10-04T12:00:00+03:00')) == {c: 'required'}
    assert attached_to(data, trainee, shift, aware('2026-09-30T08:00:00+03:00'),
                       aware('2026-09-30T12:00:00+03:00')) == {}
    assert 'ATTACHMENT_PAIRING' in {f['code'] for f in evaluate(data)['findings']}
    bounded = copy.deepcopy(data)
    bounded['mentorships'][0]['ends_on'] = '2026-11-01'
    assert rule_map(data) == rule_map(bounded)


@pytest.mark.parametrize('strength', ['required', 'preferred'])
def test_solver_and_greedy_keep_open_ended_attachment_pairing(h, strength):
    trainee, b, c, typ = setup(h, strength=strength, ends_on=None)
    data = h.snapshot(h.shift(h.workspace(), typ=typ, people=people(trainee, b, c))['workspace'])
    # Exercise scheduler pairing from a missing pair, after the manual save supplied it.
    next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    solved = generate(copy.deepcopy(data), 20)
    assert solved['status'] == 'candidate_feasible'
    assert supervisor_of(solved, trainee) == c
    greedy = generation.generate(copy.deepcopy(data), 20)
    assert greedy['validation']['valid'], greedy['validation']['findings']
    assert supervisor_of(greedy, trainee) == c


def assert_migrated_attachment_guards(engine, saved):
    with transaction(engine) as db:
        assert db.execute(sa.text('SELECT * FROM mentorships WHERE id = :id'), {'id': saved['id']}).mappings().one() == saved
    assert next(c for c in sa.inspect(engine).get_columns('mentorships') if c['name'] == 'ends_on')['nullable']
    table = sa.Table('mentorships', sa.MetaData(), autoload_with=engine)
    value = {**saved, **{key: json.loads(saved[key]) if isinstance(saved[key], str) else saved[key]
                        for key in ('assignment_type_ids', 'service_ids')}}
    for key in ('starts_on', 'ends_on'):
        if isinstance(value[key], str):
            value[key] = date.fromisoformat(value[key])
    if isinstance(value['created_at'], str):
        value['created_at'] = aware(value['created_at'])
    with engine.begin() as bind:
        bind.execute(table.insert(), {**value, 'id': 'open-ended', 'ends_on': None})
    for changes in ({'ends_on': value['starts_on']}, {'mentor_id': value['person_id'], 'ends_on': None}):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as bind:
            bind.execute(table.insert(), {**value, 'id': 'invalid', **changes})


def test_sqlite_attachment_upgrade_preserves_rows_and_guards(tmp_path, monkeypatch):
    url = 'sqlite:///' + str(tmp_path / 'attachments.db')
    command.upgrade(alembic_config(url), '0069_schedule_view_scope')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            attachment = m.Mentorship(department_id=seed['department_id'], person_id=seed['people'][0],
                mentor_id=seed['people'][1], starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1),
                assignment_type_ids=[seed['type']], service_ids=[seed['services'][0]], strength='required')
            db.add(attachment)
            db.flush()
            saved = dict(db.execute(sa.text('SELECT * FROM mentorships WHERE id = :id'), {'id': attachment.id}).mappings().one())
        migrate(url)
        assert_migrated_attachment_guards(engine, saved)
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_postgres_attachment_upgrade_preserves_rows_and_guards(pg):
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        attachment = m.Mentorship(department_id=pg.seed['department_id'], person_id=pg.seed['people'][0],
            mentor_id=pg.seed['people'][1], starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1),
            assignment_type_ids=[pg.seed['type']], service_ids=[pg.seed['services'][0]], strength='required')
        db.add(attachment)
        db.flush()
        saved = dict(db.execute(sa.text('SELECT * FROM mentorships WHERE id = :id'), {'id': attachment.id}).mappings().one())
    with engine.begin() as bind, Operations.context(MigrationContext.configure(bind)):
        Operations(MigrationContext.configure(bind)).alter_column('mentorships', 'ends_on', existing_type=sa.Date(), nullable=False)
        importlib.import_module('migrations.versions.0070_open_ended_attachments').upgrade()
    assert_migrated_attachment_guards(engine, saved)
