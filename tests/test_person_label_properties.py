"""Synthetic label configuration, append-only history and permission-safe batched properties."""
import ast
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, event, select, text

from apps.api import models as m
from apps.api.auth import Actor, effective_roles
from apps.api.db import transaction
from apps.api.services import person_properties
from packages.contracts import schemas as s
from tests.governance_support import identity
from tests.test_data_safety import session_limits_database

pytestmark = pytest.mark.postgres


def display(h, **values):
    department = h.get('admin', '/api/me')['department']
    return h.command('admin', 'department.display', {'reason': 'Synthetic display change', **values},
                     department['id'], department['revision'])


def undo(h, reply):
    action = reply['undo']
    return h.command('admin', action['kind'], action['payload'], action['target_id'], action['expected_revision'])


def properties(h, role, people, keys, **kwargs):
    response = h.client(role).post('/api/people/properties', json={'person_ids': people, 'keys': keys, **kwargs})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_registry_contract_sources_and_legacy_views():
    assert set(s.LABEL_PARTS) >= {'short', 'level', 'pager', 'full', 'code', 'number'}
    assert tuple(s.LABEL_PART_DEFINITIONS) == s.LABEL_PARTS
    defaults = s.DisplaySettingsInput(reason='Synthetic defaults')
    assert not defaults.person_labels_enabled and defaults.person_label is None and defaults.properties == []
    assert defaults.honorifics == list(s.HONORIFIC_DEFAULTS)
    assert defaults.name_particles == list(s.NAME_PARTICLE_DEFAULTS)
    fields = {f['key'] for f in s.CONDITION_FIELD_DEFINITIONS}
    for key, definition in s.PERSON_PROPERTY_DEFINITIONS.items():
        assert (definition['source'] == 'condition' and key in fields or
                definition['source'] == 'person' and s.PersonInput.model_fields[key].json_schema_extra['property'] or
                definition['source'] in {'contact_types', 'fairness', 'availability'})
    assert s.PERSON_PROPERTY_DEFINITIONS['workload']['members'].keys() <= s.FAIRNESS_METRIC_LABELS.keys()
    saved = {'parts': ['full', 'short'], 'separator': 'space', 'honorific': 'none'}
    with pytest.warns(UserWarning, match='Legacy label'):
        assert s.PersonLabel.model_validate(saved).model_dump() == saved
    with pytest.raises(ValidationError, match='at most one'):
        s.PersonLabel.model_validate(saved, context={'label_write': True})
    with pytest.warns(UserWarning):
        view = s.TableViewDefinition(planner={'label': saved}, timeline={'bar_label': 'short'},
                                    hidden=['rank'], order=['rank', 'name'])
    assert view.planner.label.parts == saved['parts'] and view.timeline.bar_label == 'short'
    unknown = 'person.contact:Retired_1'
    assert s.TableViewDefinition(order=[unknown], timeline={'properties': [unknown]}).order == [unknown]
    assert s.ViewProperty(key=unknown).on is False
    assert s.PersonLabel(parts=['programme', 'first', 'level']).parts == ['programme', 'first', 'level']
    with pytest.raises(ValidationError):
        s.TableViewDefinition(order=['person.contact:bad/path'])


def test_display_versions_restore_undo_stale_and_projection(pg):
    first = display(pg, person_label={'parts': ['programme', 'short', 'level']},
                    honorifics=['Dr', 'Synthetic'], name_particles=['Al', 'Synthetic'],
                    properties=[{'key': 'person.away', 'on': True}], person_labels_enabled=True)
    second = display(pg, person_label={'parts': ['first']})
    assert second['department']['display_settings']['honorifics'] == ['Dr', 'Synthetic']
    restored = undo(pg, second)
    assert restored['department']['display_settings'] == first['department']['display_settings']
    with transaction(pg.app.state.engine) as db:
        versions = list(db.scalars(select(m.DisplaySettingsVersion).where(
            m.DisplaySettingsVersion.department_id == pg.seed['department_id']).order_by(m.DisplaySettingsVersion.revision)))
        assert [v.revision for v in versions] == [1, 2, 3, 4]
        assert versions[-1].definition == db.get(m.Department, pg.seed['department_id']).display_settings
        assert versions[-1].previous_id == versions[-2].id
        assert versions[-2].definition['person_label']['parts'] == ['first']
        assert list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_display')))
    pg.command('admin', 'department.display.restore', {'version_id': first['version_id'], 'reason': 'Synthetic stale'},
               pg.seed['department_id'], first['department']['revision'], status=409)
    page = pg.get('admin', '/api/department/display/versions?limit=2')
    assert [v['revision'] for v in page['versions']] == [4, 3]
    older = pg.get('admin', '/api/department/display/versions?limit=2&cursor=' + str(page['next_cursor']))
    assert [v['revision'] for v in older['versions']] == [2, 1] and older['next_cursor'] is None
    assert 'person_label' in page['versions'][0]['changed_fields']
    assert page['versions'][0]['actor_id'] == pg.seed['identities']['admin']
    assert undo(pg, restored)['department']['display_settings']['person_label']['parts'] == ['first']
    pg.command('admin', 'department.display', {'person_label': {'parts': ['first', 'full']},
        'reason': 'Synthetic invalid name'}, pg.seed['department_id'],
        pg.get('admin', '/api/me')['department']['revision'], status=422)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_admin_only_display_writes_and_history(pg, role):
    identity(pg, 'label_reader', [role])
    department = pg.get('admin', '/api/me')['department']
    permitted = 'admin' in effective_roles({role})
    pg.command('label_reader', 'department.display', {'names_default': 'short', 'reason': 'Synthetic permissions'},
               department['id'], department['revision'], status=200 if permitted else 403)
    response = pg.client('label_reader').get('/api/department/display/versions')
    assert response.status_code == (200 if permitted else 403)
    if not permitted:
        pg.command('label_reader', 'department.display.restore', {'version_id': m.uid(), 'reason': 'Synthetic permissions'},
                   department['id'], department['revision'], status=403)


def test_optional_exact_names_survive_legacy_edits_and_are_bulk_fields(pg):
    pid = pg.seed['people'][0]
    row = next(p for p in pg.get('admin', '/api/catalogues?include=people')['people'] if p['id'] == pid)
    first = pg.configure('people', {'display_name': 'Synthetic Full Name', 'given_name': 'Synthetic Given',
        'family_name': 'Al Example'}, pid, row['revision'])['record']
    second = pg.configure('people', {'display_name': 'Synthetic Full Name Updated'}, pid, first['revision'])['record']
    assert second['given_name'] == 'Synthetic Given' and second['family_name'] == 'Al Example'
    for field in ('given_name', 'family_name'):
        assert field in s.BULK_COLLECTIONS['people']['fields']
        assert pg.get('staff', f'/api/people/{pid}/card')['person'][field] == second[field]
        assert next(p for p in pg.get('staff', '/api/directory')['people'] if p['id'] == pid)[field] == second[field]


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_property_permissions_follow_contact_and_fairness_sources(pg, role):
    pid = pg.seed['people'][1]
    identity(pg, 'property_reader', [role])
    typ = pg.configure('contact_types', {'name': 'Synthetic private phone', 'kind': 'phone',
        'starts_on': '2020-01-01', 'visibility': {'mode': 'roles', 'roles': ['coordinator']}})['record']
    pg.command('admin', 'person.contact.set', {'person_id': pid, 'contact_type_id': typ['id'],
        'value': '5550199', 'reason': 'Synthetic contact'})
    key = 'person.contact:' + typ['id']
    result = properties(pg, 'property_reader', [pid], [key, 'person.workload:duties', 'person.away', 'person.future'])
    roles = effective_roles({role})
    assert (key in result[pid]) == ('coordinator' in roles)
    assert ('person.workload:duties' in result[pid]) == bool(roles.intersection(s.PERMISSIONS['fairness.read_team']))
    assert ('person.away' in result[pid]) == bool(roles.intersection({'coordinator', 'approver'}))
    assert 'person.future' not in result[pid]
    if key in result[pid]:
        card = pg.get('property_reader', f'/api/people/{pid}/card')
        assert result[pid][key] == next(c for c in card['contacts'] if c['contact_type_id'] == typ['id'])


def test_self_workload_and_request_bounds(pg):
    own, other = pg.seed['people'][:2]
    values = properties(pg, 'staff', [own, other], ['person.workload:duties', 'person.away'])
    assert values[own] == {'person.workload:duties': 0.0, 'person.away': False}
    assert values[other] == {}
    for body in ({'person_ids': [own] * 501, 'keys': []},
                 {'person_ids': [own], 'keys': ['person.away'] * 31},
                 {'person_ids': [own], 'keys': ['invalid']},
                 {'person_ids': [own], 'keys': [], 'period': {'starts_on': '2026-10-04', 'ends_on': '2026-10-01'}}):
        assert pg.client('staff').post('/api/people/properties', json=body).status_code == 422
    assert properties(pg, 'staff', [m.uid()], ['person.away']) == {}


def test_150_people_properties_have_constant_query_count(pg):
    with transaction(pg.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=pg.seed['organisation_id'], display_name=f'Synthetic label {i} (test)',
                           work_phone=f'555{i:04d}') for i in range(150)]
        db.add_all(people)
        db.flush()
        ids = [p.id for p in people]
    actor = Actor(pg.seed['identities']['admin'], pg.seed['department_id'], pg.seed['organisation_id'],
                  'Synthetic reader', effective_roles({'admin'}), None, m.uid())
    key = 'person.contact:' + next(t['id'] for t in s.DEFAULT_CONTACT_TYPES if t['legacy_field'] == 'work_phone')
    counts = []
    for size in (1, 150):
        statements = []
        def listened(_conn, _cursor, statement, *_args):
            statements.append(statement)
        with transaction(pg.app.state.engine) as db:
            event.listen(pg.app.state.engine, 'before_cursor_execute', listened)
            start = perf_counter()
            try:
                result = person_properties.read(db, actor, s.PersonPropertiesInput(person_ids=ids[:size],
                    keys=[key, 'person.away', *('person.workload:' + k for k in s.PERSON_PROPERTY_DEFINITIONS['workload']['members'])]))
            finally:
                event.remove(pg.app.state.engine, 'before_cursor_execute', listened)
        print(f'person properties people={size} queries={len(statements)} elapsed={perf_counter()-start:.4f}s')
        assert len(result) == size and all(v[key]['value'].startswith('555') for v in result.values())
        counts.append(len(statements))
    assert counts[0] == counts[1]


def test_additive_migration_retains_source_rows_and_seed_is_idempotent(session_limits_database):
    from alembic import command
    from apps.api.db import make_engine
    from scripts.manage import alembic_config
    from tests.migration_support import historical_seed
    from migrations import safety
    path = Path('migrations/versions/0137_display_settings.py')
    assert safety.classification(path) == 'fill_only' and not safety.declaration_errors(path)
    cfg = alembic_config(session_limits_database)
    command.upgrade(cfg, '0136_person_contacts')
    engine = make_engine(session_limits_database)
    try:
        with transaction(engine, write=True) as db:
            historical_seed(db)
        old = MetaData()
        old.reflect(bind=engine)
        def snapshot():
            with engine.connect() as db:
                return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                        for name, table in old.tables.items() if name != 'alembic_version'}
        before = snapshot()
        command.upgrade(cfg, 'head')
        assert snapshot() == before
        with transaction(engine, write=True) as db:
            versions = list(db.scalars(select(m.DisplaySettingsVersion)))
            assert versions
            for row in versions:
                assert row.definition == db.get(m.Department, row.department_id).display_settings
            insert = next(node.args[0].value for node in ast.walk(ast.parse(path.read_text()))
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr == 'execute')
            db.execute(text(insert))
            assert len(list(db.scalars(select(m.DisplaySettingsVersion)))) == len(versions)
            assert all(p.given_name == p.family_name == '' for p in db.scalars(select(m.Person)))
    finally:
        engine.dispose()
