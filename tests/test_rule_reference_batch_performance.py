"""Catalogue proposals resolve a department's references together without bypassing object access."""
from collections import Counter

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import policies
from packages.contracts import schemas as s
from .test_backend_scale import measure, scale_department
from .test_rule_catalogue_api import line, person, propose


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def proposal_measurement(request, scale_harness):
    h = scale_harness(request.param)
    _, data, _, _ = scale_department(h)
    lines = [line(who=person(pid), numbers={'max': {'value': 10}}) for pid in data['people']]
    h.client('coordinator')
    selects = Counter()
    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            for table in ('people', 'tags'):
                if ' FROM ' + table + ' ' in ' '.join(statement.split()):
                    selects[table] += 1
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        result, metrics = measure(h, '163-person catalogue proposal', lambda: propose(h, 'duties_per_week', lines))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    return lines, result, selects, metrics


def test_department_proposal_batches_reference_reads(proposal_measurement):
    lines, result, selects, metrics = proposal_measurement
    assert len(lines) == 163
    # One people preview and one union reference read, with a small allowance for request authentication.
    assert 0 < selects['people'] <= 3 and 0 < selects['tags'] <= 3, (selects, metrics)
    assert result['record']['status'] == 'proposed'
    stored = result['record']['definition']['rule']['lines']
    assert [row['id'] for row in stored] == [row['id'] for row in lines]
    assert [row['who'] for row in stored] == [s.Conditions.model_validate(row['who']).model_dump(mode='json') for row in lines]
    assert all(row['numbers']['max']['value'] == 10 for row in stored)
    assert not result['check']['issues'] and not result['check']['duplicates'] and not result['check']['overlaps']


@pytest.mark.parametrize('invalid', ['person_organisation', 'tag_department', 'tag_kind'])
def test_batched_reference_reads_keep_each_object_authorization(h, invalid):
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        if invalid == 'person_organisation':
            organisation = m.Organisation(name='Other synthetic organisation')
            db.add(organisation)
            db.flush()
            row = m.Person(organisation_id=organisation.id, display_name='Other organisation person')
        elif invalid == 'tag_department':
            department = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
            db.add(department)
            db.flush()
            row = m.Tag(department_id=department.id, name='Other department tag', labels=['people'])
        else:
            row = m.Tag(department_id=h.seed['department_id'], name='Service tag', labels=['services'])
        db.add(row)
        db.flush()
        invalid_id = row.id
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        tree = s.Conditions.model_validate({'conditions': [
            {'field': 'person_ids', 'values': [h.seed['people'][0], invalid_id]}]})
        with pytest.raises(Problem) as error:
            policies.condition_references(db, actor, tree, reference_rows={})
        assert error.value.code == ('TAG_KIND' if invalid == 'tag_kind' else 'NOT_FOUND')


def test_shared_reference_rows_do_not_skip_each_condition_tag_kind(h):
    tag = h.configure('tags', {'name': 'People only', 'labels': ['people']})['record']
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        references = {}
        for field in ('person_ids', 'service_ids'):
            tree = s.Conditions.model_validate({'conditions': [{'field': field, 'values': [tag['id']]}]})
            if field == 'person_ids':
                policies.condition_references(db, actor, tree, reference_rows=references)
            else:
                with pytest.raises(Problem) as error:
                    policies.condition_references(db, actor, tree, reference_rows=references)
                assert error.value.code == 'TAG_KIND'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_reference_union_chunks_bind_parameters_and_preserves_first_refusal(scale_harness, monkeypatch, database):
    h = scale_harness(database)
    client = h.client('coordinator')
    identifiers = [h.seed['people'][0], *[m.uid() for _ in range(1000)]]
    tree = s.Conditions.model_validate({'conditions': [
        {'field': 'person_ids', 'values': identifiers[start:start + 500]} for start in range(0, len(identifiers), 500)]})
    batches, authorized = {}, []
    def counted(_connection, _cursor, statement, parameters, _context, _many):
        normalized = ' '.join(statement.split())
        if ' IN (' in normalized:
            for table in ('people', 'tags'):
                if ' FROM ' + table + ' ' in normalized:
                    batches.setdefault(table, []).append(len(parameters))
    scoped = policies.scoped
    def checked(db, cls, ident, actor, **kwargs):
        authorized.append(ident)
        return scoped(db, cls, ident, actor, **kwargs)
    monkeypatch.setattr(policies, 'scoped', checked)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        event.listen(h.app.state.engine, 'before_cursor_execute', counted)
        try:
            with pytest.raises(Problem) as error:
                policies.condition_references(db, actor, tree)
        finally:
            event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    assert error.value.code == 'NOT_FOUND' and error.value.status == 404
    assert authorized == identifiers[:2], 'Prefetch must preserve the original per-object authorization order'
    assert batches == {'people': [1000, 1], 'tags': [1000, 1]}, batches
