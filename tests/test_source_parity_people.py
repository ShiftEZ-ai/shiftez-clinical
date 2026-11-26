"""Configurable rota labels survive storage, import matching, Undo and privacy controls."""
import copy
import csv
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.services import feedback
from migrations import safety
from packages.contracts.schemas import PersonInput
from packages.domain.people import person_aliases
from tests.test_staff_import import apply, catalogues, import_business_state, preview
from .migration_support import add_historical_admin


LABEL = 'Preferred / label (test)'
ALIAS = 'Label, ; | "quoted" <tag> & العربية'


def sheet(**values):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(values))
    writer.writeheader()
    writer.writerow(values)
    return out.getvalue()


def test_person_alias_projection_is_lossless_neutral_and_shared():
    person = {'display_name': 'Unchanged name', 'rota_name': LABEL, 'rota_aliases': [ALIAS, LABEL]}
    before = copy.deepcopy(person)
    assert person_aliases(person, ['legacy label', LABEL]) == sorted([LABEL, ALIAS, 'legacy label'])
    assert person_aliases({'display_name': 'Legacy person'}) == []
    assert person == before


@pytest.mark.parametrize('change', [{'rota_name': 'x' * 161}, {'rota_aliases': ['x'] * 31},
                                   {'rota_aliases': ['']}, {'rota_aliases': 'text'}, {'short_code': 'with space'}])
def test_rota_label_bounds_keep_short_codes_unchanged(change):
    with pytest.raises(ValidationError):
        PersonInput.model_validate({'display_name': 'Synthetic person', **change})


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_rota_labels_catalogue_history_undo_and_older_client_edits(database, request):
    h = request.getfixturevalue(database)
    saved = h.configure('people', {'display_name': 'Synthetic person (test)', 'short_code': 'SYN',
                                  'rota_name': LABEL, 'rota_aliases': [ALIAS]})['record']
    assert saved['rota_name'] == LABEL and saved['rota_aliases'] == [ALIAS] and saved['short_code'] == 'SYN'
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name='Synthetic import',
                               source_key='alias:legacy label', target_kind='people', target_id=saved['id']))
    found = next(p for p in catalogues(h)['people'] if p['id'] == saved['id'])
    assert found['aliases'] == sorted([LABEL, ALIAS, 'legacy label'])
    changed = h.configure('people', {'display_name': saved['display_name'], 'short_code': 'SYN', 'title': 'Updated'},
                          saved['id'], saved['revision'])['record']
    assert changed['rota_name'] == LABEL and changed['rota_aliases'] == [ALIAS]
    cleared = h.configure('people', {'display_name': saved['display_name'], 'short_code': 'SYN',
                                    'rota_name': '', 'rota_aliases': []}, saved['id'], changed['revision'])
    assert cleared['record']['rota_name'] == '' and cleared['record']['rota_aliases'] == []
    restored = h.configure('people', cleared['previous_value'], saved['id'], cleared['record']['revision'])['record']
    assert restored['rota_name'] == LABEL and restored['rota_aliases'] == [ALIAS]
    history = h.get('auditor', '/api/catalogues/people/' + saved['id'] + '/history')['versions']
    assert history[-1]['snapshot']['rota_name'] == LABEL and history[-1]['snapshot']['rota_aliases'] == [ALIAS]
    h.configure('people', {'display_name': 'Forbidden', 'rota_name': LABEL}, role='staff', status=403)


def test_staff_import_matches_configured_and_retained_aliases_and_refuses_ambiguity(h):
    one = h.configure('people', {'display_name': 'Synthetic person one', 'staff_number': 'SYN-ONE',
                                'short_code': 'ONE', 'rota_name': LABEL, 'rota_aliases': [ALIAS, 'Shared']})['record']
    h.configure('people', {'display_name': 'Synthetic person two', 'rota_aliases': ['Shared']})
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name='Synthetic import',
                               source_key='alias:legacy label', target_kind='people', target_id=one['id']))
    for name in ('  PREFERRED /   LABEL (TEST)  ', ALIAS, 'Legacy Label'):
        row = preview(h, sheet(display_name=name))['rows'][0]
        assert row['person']['id'] == one['id'] and row['person']['by'] == 'aliases' and not row['errors']
    ambiguous = preview(h, sheet(display_name='shared'))['rows'][0]
    assert 'More than one person' in ambiguous['errors']['display_name']
    identified = preview(h, sheet(display_name='shared', staff_number='SYN-ONE'))['rows'][0]
    assert identified['person']['id'] == one['id'] and identified['person']['by'] == 'staff_number'


def test_staff_import_adds_arbitrary_aliases_losslessly_idempotently_and_undoes(h):
    person = h.configure('people', {'display_name': 'Synthetic import person', 'short_code': 'SYN',
                                   'rota_name': LABEL, 'rota_aliases': ['Before']})['record']
    text = sheet(display_name=person['display_name'], short_code='syn', rota_name=LABEL,
                 rota_aliases=json.dumps([ALIAS, '  Added alias  '], ensure_ascii=False))
    planned = preview(h, text)
    assert not planned['rows'][0]['errors']
    result = apply(h, 'coordinator', planned['batch'])
    saved = next(p for p in catalogues(h)['people'] if p['id'] == person['id'])
    assert saved['rota_aliases'] == ['Before', ALIAS, 'Added alias'] and saved['short_code'] == 'SYN'
    reviewed = h.get('coordinator', '/api/staff-imports/' + result['batch']['id'])
    before = import_business_state(h)
    audit = h.get('auditor', '/api/audit')['events']
    refused = h.command('coordinator', 'staff_import.apply', {'reason': 'Re-apply old import'},
                        result['batch']['id'], result['batch']['revision'],
                        expected_fingerprint=reviewed['source_fingerprint'], status=409)
    assert refused['error']['code'] == 'IMPORT_ALREADY_APPLIED'
    assert import_business_state(h) == before
    assert h.get('auditor', '/api/audit')['events'] == audit
    rerun = preview(h, text)
    assert rerun['batch']['id'] != result['batch']['id']
    assert rerun['rows'][0]['person']['action'] == 'same' and not rerun['rows'][0]['person']['changes']
    assert rerun['summary']['people_pending'] == rerun['summary']['credentials_pending'] == 0
    before = catalogues(h)
    no_op = apply(h, 'coordinator', rerun['batch'])
    assert not any(no_op['applied'].values()) and catalogues(h) == before
    latest = h.get('coordinator', '/api/staff-imports/' + result['batch']['id'])['batch']
    h.command('coordinator', 'staff_import.undo', {'reason': 'Undo synthetic aliases'}, latest['id'], latest['revision'])
    saved = next(p for p in catalogues(h)['people'] if p['id'] == person['id'])
    assert saved['rota_aliases'] == ['Before'] and saved['rota_name'] == LABEL
    actions = [event['action'] for event in h.get('auditor', '/api/audit')['events']]
    assert 'staff_import.apply' in actions and 'staff_import.undo' in actions
    for raw in ('not json', '"single string"', '[false]', '[""]'):
        assert 'rota_aliases' in preview(h, sheet(display_name=person['display_name'], rota_aliases=raw))['rows'][0]['errors']
    new = preview(h, sheet(display_name='Synthetic new import person', short_code='NEW', rota_name='New rota label',
                           rota_aliases=json.dumps(['New / alias, ; | "quoted"'])))
    assert new['rows'][0]['person']['action'] == 'new'
    apply(h, 'coordinator', new['batch'])
    created = next(p for p in catalogues(h)['people'] if p['short_code'] == 'NEW')
    assert created['rota_name'] == 'New rota label' and created['rota_aliases'] == ['New / alias, ; | "quoted"']


def test_rota_names_are_anonymised_and_redacted_from_feedback(h):
    saved = h.configure('people', {'display_name': 'Synthetic private person', 'active': False,
                                  'rota_name': LABEL, 'rota_aliases': [ALIAS]})['record']
    with transaction(h.app.state.engine) as db:
        actor = SimpleNamespace(organisation_id=h.seed['organisation_id'], department_id=h.seed['department_id'])
        cleaned = feedback._sanitizer(db, actor, '')(f'{LABEL}; {ALIAS}')
        assert LABEL not in cleaned and ALIAS not in cleaned and 'redacted' in cleaned
    anonymised = h.command('admin', 'person.anonymise', {'reason': 'Synthetic leaver privacy'}, saved['id'], saved['revision'])['record']
    assert anonymised['rota_name'] == '' and anonymised['rota_aliases'] == []


def test_rota_configuration_migration_keeps_existing_person_values(tmp_path):
    path = tmp_path / 'labels-upgrade.db'
    cfg = Config('alembic.ini')
    cfg.set_main_option('sqlalchemy.url', 'sqlite:///' + str(path))
    command.upgrade(cfg, '0083_swap_approval_settings')
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO organisations (id, revision, created_at, name, timezone, week_start, input_revision, availability_revision) "
                   "VALUES ('org', 1, '2026-10-03', 'Synthetic organisation', 'UTC', 6, 1, 1)")
        db.execute("INSERT INTO people (id, revision, created_at, organisation_id, display_name, active, short_code) "
                   "VALUES ('person', 7, '2026-10-03', 'org', 'Retained display name', 1, 'OLD')")
        before = db.execute('SELECT id, revision, display_name, short_code FROM people').fetchall()
    engine = make_engine('sqlite:///' + str(path))
    try:
        with engine.begin() as db:
            add_historical_admin(db, 'dept', 'admin', organisation_id='org')
    finally:
        engine.dispose()
    command.upgrade(cfg, 'head')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id, revision, display_name, short_code FROM people').fetchall() == before
        assert db.execute('SELECT rota_name, rota_aliases FROM people').fetchall() == [('', '[]')]
    migration = safety.VERSIONS / '0074_source_rota_configuration.py'
    assert not safety.findings(migration) and not safety.needs_confirmation(migration)
