"""Synthetic contact data: per-type privacy, typed edits, immutable history and bounded reads."""
from datetime import date
import json
import re
from types import SimpleNamespace
from time import perf_counter

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.auth import Actor, effective_roles
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import contacts
from apps.api.services.people import visible_people
from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_data_safety import session_limits_database

pytestmark = pytest.mark.postgres


def contact_type(h, **changes):
    return h.configure('contact_types', {'name': 'Synthetic phone', 'kind': 'phone',
        'starts_on': '2020-01-01', **changes})['record']


def set_value(h, typ, value='5550100', person=None, role='admin', **kwargs):
    return h.command(role, 'person.contact.set', {'person_id': person or h.seed['people'][1],
        'contact_type_id': typ['id'], 'value': value, 'reason': 'Synthetic contact edit',
        **kwargs.pop('payload', {})}, **kwargs)


def undo(h, result):
    action = result['undo']
    return h.command('admin', action['kind'], action['payload'], action.get('target_id'), action.get('expected_revision'))


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_each_role_sees_only_configured_types_and_no_history_values(pg, role):
    person = pg.seed['people'][1]
    identity(pg, 'contact_reader', [role])
    public = contact_type(pg, visibility={'mode': 'everyone'})
    restricted = contact_type(pg, name='Synthetic restricted', visibility={'mode': 'roles', 'roles': ['coordinator']})
    own = contact_type(pg, name='Synthetic own', visibility={'mode': 'self_and_admins'})
    for typ, value in ((public, '5550101'), (restricted, '5550102'), (own, '5550103')):
        set_value(pg, typ, value)
    rows = pg.get('contact_reader', '/api/directory')['people']
    values = {c['value'] for p in rows if p['id'] == person for c in p.get('contacts', [])}
    expected = {'5550101'} | ({'5550102'} if role in {'admin', 'coordinator'} else set()) | ({'5550103'} if role == 'admin' else set())
    assert values == expected
    card = pg.get('contact_reader', f'/api/people/{person}/card')
    assert {c['value'] for c in card['contacts']} == expected
    assert ('workload' in card) == (role in s.PERMISSIONS['fairness.read_team'])
    history = pg.get('admin', f'/api/people/{person}/contacts/history')
    assert history['versions'] and not any(v in json.dumps(history) for v in ('5550101', '5550102', '5550103'))


def test_type_versions_undo_stale_and_effective_dates(pg):
    typ = contact_type(pg, visibility={'mode': 'everyone'}, one_tap=True)
    changed = pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'name': 'Renamed synthetic phone', 'visibility': {'mode': 'roles', 'roles': ['coordinator']}}, typ['id'], typ['revision'])
    restored = undo(pg, changed)['record']
    assert restored['name'] == typ['name'] and restored['revision'] == 3
    pg.command('admin', 'catalogue.update', {'catalogue': 'contact_types',
        'value': {k: typ[k] for k in s.ContactTypeInput.model_fields}, 'reason': 'Stale synthetic write'},
        typ['id'], typ['revision'], status=409)
    versions = pg.get('admin', f"/api/catalogues/contact_types/{typ['id']}/history")['versions']
    assert [v['object_revision'] for v in versions] == [1, 2, 3]
    assert all(v['actor_id'] == pg.seed['identities']['admin'] for v in versions)
    set_value(pg, typ)
    pg.configure('contact_types', {**{k: restored[k] for k in s.ContactTypeInput.model_fields},
        'starts_on': '2027-01-01'}, typ['id'], restored['revision'])
    assert not pg.get('staff', f"/api/people/{pg.seed['people'][1]}/card")['contacts']


def test_value_validation_undo_override_and_mirror(pg):
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'pager')
    person = pg.seed['people'][0]
    person_row = next(p for p in pg.get('admin', '/api/catalogues?include=people')['people'] if p['id'] == person)
    pg.configure('people', {'display_name': 'Synthetic contact owner', 'pager': '4417'}, person, person_row['revision'])
    typ = pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'format': {'pattern': '^[0-9]{4}$', 'max_length': 4}, 'one_tap': True,
        'dial': {'line': '+15550100', 'join': 'wait'}, 'person_may_change': True,
        'edited_by': 'person_and_admins'}, typ['id'], typ['revision'])['record']
    current = pg.get('staff', f'/api/people/{person}/card')['contacts'][0]
    set_value(pg, typ, 'bad', person, revision=current['revision'], status=422)
    edited = set_value(pg, typ, '4418', person, role='staff', revision=current['revision'],
                       payload={'visibility_override': {'mode': 'self_and_admins'}})
    assert edited['record']['revision'] == current['revision'] + 1
    card = pg.get('staff', f'/api/people/{person}/card')
    assert card['contacts'][0]['dial_target'] == 'tel:+15550100;4418'
    assert not next(p for p in pg.get('auditor', '/api/directory')['people'] if p['id'] == person).get('contacts')
    restored = undo(pg, edited)['record']
    assert restored['value'] == '4417'
    cleared = set_value(pg, typ, '', person, revision=restored['revision'])
    assert cleared['record']['value'] == ''
    assert undo(pg, cleared)['record']['value'] == '4417'
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.Person, person).pager == restored['value']
        audit = json.dumps([record(r) for r in db.scalars(select(m.AuditEvent))])
        # Whole values only: timestamps (.441793) and ids may contain the same digits.
        assert not re.search(r'(?<![0-9A-Za-z.])441[78](?![0-9A-Za-z])', audit)
    set_value(pg, typ, '4420', pg.seed['people'][1], role='staff', status=403)


def test_seed_defaults_and_legacy_fallback_are_role_configurable(pg):
    with transaction(pg.app.state.engine, write=True) as db:
        person = db.get(m.Person, pg.seed['people'][1])
        person.work_phone, person.pager, person.work_email = '+15550101', '4422', 'hidden@example.test'
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'work_phone')
    before = pg.get('staff', f"/api/people/{pg.seed['people'][1]}/card")
    assert {c['value'] for c in before['contacts']} == {'+15550101', '4422'}
    changed = set_value(pg, typ, '+15550102')
    assert changed['undo']['payload']['value'] == '+15550101'
    undo(pg, changed)
    pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'visibility': {'mode': 'roles', 'roles': ['coordinator']}}, typ['id'], typ['revision'])
    after = pg.get('staff', f"/api/people/{pg.seed['people'][1]}/card")
    assert {c['value'] for c in after['contacts']} == {'4422'}
    client = pg.client('staff')
    first = client.get(f"/api/people/{pg.seed['people'][1]}/card")
    assert first.headers['cache-control'] == 'private, no-cache'
    assert first.headers['etag']
    second = client.get(f"/api/people/{pg.seed['people'][1]}/card",
                        headers={'if-none-match': first.headers['etag']})
    assert second.status_code == 304 and not second.content


def test_open_contact_defaults_require_a_chosen_role_when_restricted(pg):
    role = s.INSTALLATION_ADMIN_ROLE
    identity(pg, 'installation_contact_reader', [role])
    person = pg.seed['people'][1]
    card = f'/api/people/{person}/card'
    defaults = pg.get('admin', '/api/contact-types')['contact_types']
    public = [row for row in defaults if row['visibility']['mode'] == 'everyone']
    assert {row['legacy_field'] for row in public} == {'work_phone', 'pager'}
    assert all(row['visibility']['roles'] == [] for row in public)
    typ = next(row for row in public if row['legacy_field'] == 'work_phone')
    set_value(pg, typ)
    assert [c['value'] for c in pg.get('installation_contact_reader', card)['contacts']] == ['5550100']

    value = {k: typ[k] for k in s.ContactTypeInput.model_fields}
    restricted = pg.configure('contact_types', {**value, 'visibility': {'mode': 'roles'}},
                              typ['id'], typ['revision'])['record']
    assert restricted['visibility'] == {'mode': 'roles', 'roles': []}
    assert not pg.get('installation_contact_reader', card)['contacts']

    pg.configure('contact_types', {**value, 'visibility': {'mode': 'roles', 'roles': [role]}},
                 typ['id'], restricted['revision'])
    assert [c['value'] for c in pg.get('installation_contact_reader', card)['contacts']] == ['5550100']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_seeded_mcd_pager_dials_directly_without_one_tap(pg, role):
    identity(pg, f'pager_reader_{role}', [role])
    person = pg.seed['people'][1]
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Person, person).pager = '4417'
    card = pg.get(f'pager_reader_{role}', f'/api/people/{person}/card')
    pager = next(contact for contact in card['contacts'] if contact['kind'] == 'pager')
    assert pager['dial_target'] == 'tel:4417'


def test_action_off_overrides_the_kind_action_for_pager_phone_and_email(pg):
    person = pg.seed['people'][1]
    with transaction(pg.app.state.engine, write=True) as db:
        row = db.get(m.Person, person)
        row.pager, row.work_phone, row.work_email = '4417', '+15550100', 'synthetic@example.test'
    for legacy in ('pager', 'work_phone', 'work_email'):
        typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == legacy)
        pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
            'action_off': True}, typ['id'], typ['revision'])
    card = pg.get('admin', f'/api/people/{person}/card')
    assert all(contact.get('dial_target') is None for contact in card['contacts']
               if contact['legacy_field'] in {'pager', 'work_phone', 'work_email'})


def test_contact_override_cache_is_person_specific(pg):
    typ = contact_type(pg, person_may_change=True, visibility={'mode': 'everyone'})
    person = pg.seed['people'][1]
    set_value(pg, typ, '5550199', payload={'visibility_override': {'mode': 'self_and_admins'}})
    identity(pg, 'owner_credentialer', ['credentialer'], person)
    identity(pg, 'other_credentialer', ['credentialer'], pg.seed['people'][2])
    own = pg.get('owner_credentialer', '/api/catalogues?include=people')
    other = pg.get('other_credentialer', '/api/catalogues?include=people')
    assert '5550199' in json.dumps(own) and '5550199' not in json.dumps(other)


@pytest.mark.parametrize('kind,value,line,target', [
    ('phone', '+1 (555) 0100', '', 'tel:+15550100'),
    ('pager', '4417', '', 'tel:4417'),
    ('extension', '4417', '', 'tel:4417'),
    ('pager', '4417', '+15550100', 'tel:+15550100;4417'),
    ('extension', '4321 / 8765', '', None),
    ('pager', '*4417#', '+15550100', None),
    ('pager', '1' * 16, '', None),
    ('phone', '555;911', '', None),
    ('email', 'synthetic@example.test', '', 'mailto:synthetic@example.test'),
])
def test_dial_targets(kind, value, line, target):
    assert contacts.dial_target(SimpleNamespace(one_tap=False, action_off=False, kind=kind,
                                                dial={'line': line, 'join': 'wait'}), value) == target


@pytest.mark.parametrize('kind', ['phone', 'pager', 'extension', 'email'])
def test_action_off_removes_the_kind_action_but_keeps_copy(kind):
    assert contacts.dial_target(SimpleNamespace(one_tap=True, action_off=True, kind=kind,
                                                dial={'line': '+15550100', 'join': 'wait'}),
                                '4417' if kind != 'email' else 'synthetic@example.test') is None


def test_pager_pause_joins_the_configured_line_with_a_comma():
    assert contacts.dial_target(SimpleNamespace(one_tap=False, action_off=False, kind='pager',
                                                dial={'line': '+15550100', 'join': 'pause'}),
                                '4417') == 'tel:+15550100,4417'


def test_directory_150_people_uses_batched_contact_queries(pg):
    with transaction(pg.app.state.engine, write=True) as db:
        db.add_all(m.Person(organisation_id=pg.seed['organisation_id'], display_name=f'Synthetic contact {i} (test)',
                           work_phone=f'555{i:04d}', pager=str(i)) for i in range(150))
    actor = Actor(pg.seed['identities']['staff'], pg.seed['department_id'], pg.seed['organisation_id'],
                  'Synthetic reader', effective_roles({'staff'}), None, m.uid())
    counts = []
    for count in (1, 150):
        statements = []
        def listened(_conn, _cursor, statement, *_args):
            statements.append(statement)
        with transaction(pg.app.state.engine) as db:
            people = [record(p) for p in db.scalars(select(m.Person).limit(count))]
            event.listen(pg.app.state.engine, 'before_cursor_execute', listened)
            begin = perf_counter()
            try:
                result = visible_people(db, actor, people)
            finally:
                event.remove(pg.app.state.engine, 'before_cursor_execute', listened)
            print(f'contacts people={count} queries={len(statements)} elapsed={perf_counter()-begin:.4f}s')
        assert len(result) == count
        counts.append(len(statements))
    assert counts[0] == counts[1]


def test_migration_retains_all_existing_data(session_limits_database):
    from alembic import command
    from sqlalchemy import MetaData, Table
    from scripts.manage import alembic_config
    from apps.api.db import make_engine
    from tests.migration_support import historical_seed, without_installation_admin_backfill
    from migrations import safety
    from pathlib import Path
    cfg = alembic_config(session_limits_database)
    command.upgrade(cfg, '0135_applied_population_index')
    engine = make_engine(session_limits_database)
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        table = Table('people', MetaData(), autoload_with=db.connection())
        db.execute(table.update().where(table.c.id == seed['people'][0]).values(
            active=False, pager='4417', work_phone='+15550101', work_email='retained@example.test'))
    old = MetaData()
    old.reflect(bind=engine)
    def snapshot():
        with engine.connect() as db:
            return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                    for name, table in old.tables.items() if name != 'alembic_version'}
    before = snapshot()
    command.upgrade(cfg, 'head')
    assert without_installation_admin_backfill(snapshot()) == before
    with transaction(engine) as db:
        types = list(db.scalars(select(m.ContactType)))
        assert len(types) == len(s.DEFAULT_CONTACT_TYPES)
        assert not list(db.scalars(select(m.PersonContact)))
        for row in types:
            default = next(t for t in s.DEFAULT_CONTACT_TYPES if t['legacy_field'] == row.legacy_field)
            expected = {k: value for k, value in default.items() if k not in {'id', 'visibility'}}
            # This migration retains the audience it seeded, independent of current defaults.
            seeded = ['staff', 'coordinator', 'approver', 'credentialer', 'admin', 'auditor', 'programme_lead', 'operator']
            expected['visibility'] = ({'mode': 'everyone', 'roles': seeded}
                                      if row.legacy_field not in {'work_extension', 'work_email'}
                                      else {'mode': 'roles', 'roles': ['admin', 'coordinator']})
            assert all(getattr(row, k) == value for k, value in expected.items())
    for revision, expected_class in (('0136_person_contacts', 'fill_only'),
                                     ('0137_display_settings', 'fill_only'),
                                     ('0138_contact_action_off', 'additive'),
                                     ('0139_duty_short_code', 'fill_only')):
        path = Path(f'migrations/versions/{revision}.py')
        assert safety.classification(path) == expected_class and not safety.declaration_errors(path)
    engine.dispose()


def test_person_card_published_team_and_draft_boundary(pg):
    target, supervisor, other = pg.seed['people']
    typ = pg.configure('assignment_types', {'name': 'Synthetic paired duty', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [pg.seed['trainee_category']],
            'capability_id': pg.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    workspace = pg.workspace(start='2026-10-03', end='2026-10-11')
    start, end = '2026-10-03T08:00:00+03:00', '2026-10-03T12:00:00+03:00'
    workspace = pg.shift(workspace, start, end, typ=typ['id'], people=[
        {'person_id': target, 'start': start, 'end': end, 'supervisor_person_id': supervisor},
        {'person_id': supervisor, 'start': start, 'end': end},
        {'person_id': other, 'start': start, 'end': end}])['workspace']
    pg.publish(workspace)
    draft = pg.shift(pg.workspace('Synthetic future draft'), people=[target])['workspace']
    staff = pg.get('staff', f'/api/people/{target}/card')
    assert {p['person_id'] for p in staff['today'][0]['team']} == {target, supervisor}
    assert next(p for p in staff['today'][0]['team'] if p['person_id'] == target)['supervisor_person_id'] == supervisor
    assert all(p['rank'] for p in staff['today'][0]['team'])
    assert not staff['coming_up']
    coordinator = pg.get('coordinator', f'/api/people/{target}/card')
    assert coordinator['coming_up'][0]['workspace_id'] == draft['id']
    assert {'rules', 'can_work', 'workload', 'fairness'} <= coordinator.keys()


def test_default_ids_are_department_scoped_and_cross_tenant_writes_refused(pg):
    from dataclasses import replace
    from apps.api.services.common import scoped
    from apps.api.errors import Problem
    actor = Actor(pg.seed['identities']['admin'], pg.seed['department_id'], pg.seed['organisation_id'],
                  'Synthetic admin', effective_roles({'admin'}), None, m.uid())
    with transaction(pg.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=actor.organisation_id, name='Synthetic other department')
        db.add(dept); db.flush()
        contacts.seed(db, dept.id); db.flush()
        seeded = {row.legacy_field: row.visibility for row in db.scalars(
            select(m.ContactType).where(m.ContactType.department_id == dept.id))}
        assert seeded['work_extension'] == {'mode': 'roles', 'roles': ['admin', 'coordinator', 'approver']}
        assert seeded['work_email'] == {'mode': 'roles', 'roles': ['admin', 'coordinator', 'approver']}
        ident = s.DEFAULT_CONTACT_TYPES[0]['id']
        first, second = scoped(db, m.ContactType, ident, actor), scoped(db, m.ContactType, ident, replace(actor, department_id=dept.id))
        assert first is not second and first.department_id != second.department_id
        first.name = 'Synthetic local label'
        assert second.name != first.name
    custom = contact_type(pg)
    with transaction(pg.app.state.engine) as db:
        with pytest.raises(Problem):
            scoped(db, m.ContactType, custom['id'], replace(actor, department_id=dept.id))


def test_import_preview_honors_personal_contact_override(pg):
    from tests.test_staff_import import csv_of, row, preview
    person = pg.seed['people'][0]
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'pager')
    typ = pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'person_may_change': True}, typ['id'], typ['revision'])['record']
    set_value(pg, typ, '551122', person, payload={'visibility_override': {'mode': 'self_and_admins'}})
    with transaction(pg.app.state.engine) as db:
        name = db.get(m.Person, person).display_name
    result = preview(pg, csv_of(row(display_name=name, pager='551123')), role='credentialer')
    assert '551122' not in json.dumps(result) and 'pager' not in result['rows'][0]['person']['changes']


def test_all_role_visibility_modes_and_person_override_ceiling():
    for role in s.ROLE_NAMES:
        actor = Actor('reader', 'department', 'organisation', 'Synthetic reader', effective_roles({role}), 'self', 'session')
        for person in ('self', 'other'):
            expected = {mode: True if person == 'self' else allowed for mode, allowed in {
                'everyone': True, 'roles': role in {'admin', 'coordinator'},
                'self_and_admins': role == 'admin'}.items()}
            for mode, allowed in expected.items():
                assert contacts.permits(actor, {'mode': mode, 'roles': ['coordinator']}, person) == allowed
                for override, narrowed in expected.items():
                    assert (contacts.permits(actor, {'mode': mode, 'roles': ['coordinator']}, person)
                            and contacts.permits(actor, {'mode': override, 'roles': ['coordinator']}, person)) == (allowed and narrowed)


def test_hidden_values_do_not_leak_through_edits_or_retained_receipts(pg):
    import uuid
    person = pg.seed['people'][1]
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'work_phone')
    key = str(uuid.uuid4())
    set_value(pg, typ, '555018811', key=key)
    coordinator_key = str(uuid.uuid4())
    with transaction(pg.app.state.engine) as db:
        row = db.get(m.Person, person)
        rev = row.revision
    reply = pg.command('coordinator', 'catalogue.update', {'catalogue': 'people',
        'value': {'display_name': 'Synthetic changed name'}, 'reason': 'Synthetic name edit'}, person, rev, key=coordinator_key)
    assert '555018811' in json.dumps(reply)
    pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'visibility': {'mode': 'self_and_admins'}}, typ['id'], typ['revision'])
    replay = pg.committed('coordinator', coordinator_key)
    assert '555018811' not in json.dumps(replay)
    changed = pg.configure('people', {'display_name': 'Synthetic second name'}, person, reply['record']['revision'], role='coordinator')
    assert '555018811' not in json.dumps(changed)
    set_value(pg, typ, '555018812', role='coordinator', status=403)


def test_people_edit_persists_legacy_extension_after_catalogue_reload(h):
    person = h.seed['people'][0]
    initial = h.get('coordinator', '/api/catalogues?include=people')
    before = next(row for row in initial['people'] if row['id'] == person)
    value = {key: before[key] for key in ('display_name', 'active', 'title', 'directory_section',
                                          'work_phone', 'work_extension', 'pager', 'staff_number',
                                          'directory_order', 'short_code', 'tags') if key in before}
    value['work_extension'] = '4321'
    command = {'kind': 'catalogue.update', 'target_id': person, 'expected_revision': before['revision'],
               'payload': {'catalogue': 'people', 'value': value, 'reason': 'Set office extension'}}
    preview = h.client('coordinator').post('/api/catalogue/preview', json=command)
    assert preview.status_code == 200, preview.text
    reply = h.command('coordinator', 'catalogue.update', command['payload'], person, before['revision'],
                      expected_fingerprint=preview.json()['source_fingerprint'])
    saved = reply['record']
    assert saved['work_extension'] == '4321'
    assert reply['previous_value']['work_extension'] == before.get('work_extension', '')
    conditional = h.client('coordinator').get('/api/catalogues?include=people&known=' + initial['_versions']['people'])
    assert conditional.status_code == 200, conditional.text
    reloaded = next(row for row in conditional.json()['people'] if row['id'] == person)
    assert reloaded['work_extension'] == '4321'


def test_pager_expiry_invalidates_schedule_etag(pg, monkeypatch):
    from datetime import timedelta
    from tests.clock_support import freeze_clock
    from apps.api.services.schedule_view import sessions
    person = pg.seed['people'][0]
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'pager')
    pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'ends_on': '2026-10-04'}, typ['id'], typ['revision'])
    set_value(pg, typ, '551144', person)
    pg.publish(pg.shift(pg.workspace())['workspace'])
    actor = Actor(pg.seed['identities']['coordinator'], pg.seed['department_id'], pg.seed['organisation_id'],
                  'Synthetic reader', effective_roles({'coordinator'}), None, m.uid())
    with transaction(pg.app.state.engine) as db:
        tag, before = sessions(db, actor, date(2026, 10, 1), date(2026, 11, 1))
        assert before['people'][person]['pager'] == '551144'
    freeze_clock(monkeypatch, m.now() + timedelta(days=1))
    with transaction(pg.app.state.engine) as db:
        changed, after = sessions(db, actor, date(2026, 10, 1), date(2026, 11, 1), if_none_match=tag)
        assert changed != tag and after is not None
        assert 'pager' not in after['people'][person]


def test_preview_and_bulk_undo_respect_current_contact_visibility(pg):
    import uuid
    from tests.test_catalogue_preview import preview
    person = pg.seed['people'][1]
    typ = next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == 'work_phone')
    set_value(pg, typ, '555018833')
    before = next(p for p in pg.get('admin', '/api/catalogues?include=people')['people'] if p['id'] == person)
    key = str(uuid.uuid4())
    result = pg.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people',
        'items': [{'id': person, 'revision': before['revision']}], 'set': {'title': 'Synthetic title'},
        'reason': 'Synthetic bulk edit'}, key=key)
    assert result['previous_values'][0]['work_phone'] == '555018833'
    pg.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields},
        'visibility': {'mode': 'self_and_admins'}}, typ['id'], typ['revision'])
    command = {'kind': 'catalogue.update', 'target_id': person, 'expected_revision': result['records'][0]['revision'],
        'payload': {'catalogue': 'people', 'value': {'display_name': 'Synthetic preview'}, 'reason': 'Synthetic preview'}}
    assert '555018833' not in json.dumps(preview(pg, command, role='coordinator'))
    assert '555018833' not in json.dumps(pg.committed('coordinator', key))
    from tests.test_bulk import preview as bulk_preview
    assert '555018833' not in json.dumps(bulk_preview(pg, 'coordinator', {
        'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'},
        'items': [{'target_id': person, 'expected_revision': result['records'][0]['revision'],
                   'changes': [{'field': 'title', 'value': 'Synthetic preview title'}]}],
        'reason': 'Synthetic bulk preview'}))
    response = pg.client('coordinator').post('/api/bulk/rows', json={'selection': {'collection': 'people'}})
    assert response.status_code == 200, response.text
    assert '555018833' not in response.text


def test_contact_type_bulk_create_and_undo_use_the_existing_writer(pg):
    body = {'item_kind': 'catalogue.create', 'items': [{'payload': {'catalogue': 'contact_types',
        'value': {'name': 'Synthetic bulk type', 'kind': 'phone', 'starts_on': '2020-01-01'}}}],
        'reason': 'Synthetic bulk configuration'}
    result = pg.command('admin', 'bulk.apply', body)
    typ = result['items'][0]['record']
    assert typ['format'] == s.ContactFormat().model_dump()
    pg.command('admin', **result['undo'])
    assert not next(t for t in pg.get('admin', '/api/contact-types')['contact_types'] if t['id'] == typ['id'])['active']


def test_explicit_removal_hides_typed_contacts_without_rewriting_them(pg):
    person = pg.configure('people', {'display_name': 'Synthetic inactive contact', 'active': False})['record']
    typ = contact_type(pg, visibility={'mode': 'everyone'})
    saved = set_value(pg, typ, '555018844', person['id'])['record']
    pg.command('admin', 'person.anonymise', {'reason': 'Synthetic explicit removal'}, person['id'], person['revision'])
    assert not pg.get('admin', f"/api/people/{person['id']}/card")['contacts']
    directory = pg.get('admin', '/api/directory')
    assert '555018844' not in json.dumps(directory)
    with transaction(pg.app.state.engine) as db:
        assert record(db.get(m.PersonContact, saved['id'])) == saved
