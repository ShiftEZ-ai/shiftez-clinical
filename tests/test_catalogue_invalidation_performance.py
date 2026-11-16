"""Catalogue/import scale guard: one solver-resume invalidation per atomic command."""
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from tests.test_backend_scale import scale_department
from tests.test_staff_import import csv_of, row


@pytest.mark.parametrize('kind', ['catalogue.bulk_update', 'staff_import.apply'])
def test_department_catalogue_writes_invalidate_solver_resume_once(h, kind):
    workspace, _, _, _ = scale_department(h)
    people = h.get('coordinator', '/api/catalogues')['people']
    assert len(people) == 163
    title = 'Synthetic department import title'
    if kind == 'staff_import.apply':
        batch = h.command('coordinator', 'staff_import.preview', {
            'source_name': 'Department import budget',
            'csv_text': csv_of(*[row(display_name=p['display_name'], title=title) for p in people])})['batch']
        payload, target, revision = {'reason': 'Department import budget'}, batch['id'], batch['revision']
    else:
        payload = {'catalogue': 'people', 'items': [{'id': p['id'], 'revision': p['revision']} for p in people],
                   'set': {'title': title}, 'reason': 'Department catalogue budget'}
        target = revision = None
    job_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        org_revision = db.get(m.Organisation, h.seed['organisation_id']).input_revision
        db.add(m.Job(id=job_id, department_id=h.seed['department_id'], workspace_id=workspace['id'],
            kind='generation', status='succeeded', requested_by=h.seed['identities']['coordinator'],
            input_fingerprint='synthetic', input_snapshot={}, resume_state={'private': True},
            resume_checkpoint={'private': True}))
    invalidations = []
    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.upper().startswith('UPDATE JOBS SET RESUME_STATE='):
            invalidations.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        fingerprint = h.get('coordinator', '/api/staff-imports/' + target)['source_fingerprint'] if kind == 'staff_import.apply' else None
        result = h.command('coordinator', kind, payload, target, revision, expected_fingerprint=fingerprint)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    # Input writes retain frozen preparation; the compatibility decorator no
    # longer clears a resumable job during the batch.
    assert len(invalidations) == 0
    if kind == 'staff_import.apply':
        assert result['applied']['updated_people'] == len(people)
    else:
        assert len(result['records']) == len(people)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == org_revision + 1
        updated = list(db.scalars(select(m.Person).where(m.Person.id.in_([p['id'] for p in people]))))
        revisions = {p['id']: p['revision'] for p in people}
        assert all(p.title == title and p.revision == revisions[p.id] + 1 for p in updated)
        job = db.get(m.Job, job_id)
        assert job.resume_state == {'private': True} and job.resume_checkpoint == {'private': True}


def test_catalogue_batch_still_checks_each_revision_and_rolls_back(h):
    people = h.get('coordinator', '/api/catalogues')['people'][:2]
    with transaction(h.app.state.engine) as db:
        before = db.get(m.Organisation, h.seed['organisation_id']).input_revision
    result = h.command('coordinator', 'catalogue.bulk_update', {
        'catalogue': 'people', 'items': [{'id': people[0]['id'], 'revision': people[0]['revision']},
                                      {'id': people[1]['id'], 'revision': people[1]['revision'] + 1}],
        'set': {'title': 'Must roll back'}, 'reason': 'Refused stale batch'}, status=409)
    assert result['error']['code'] == 'STALE_REVISION'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == before
        assert all(db.get(m.Person, p['id']).title == p['title'] for p in people)


@pytest.mark.parametrize('kind', ['catalogue.bulk_update', 'staff_import.apply'])
def test_department_catalogue_writes_retain_solver_resume(h, kind):
    workspace, _, _, _ = scale_department(h)
    people = h.get('coordinator', '/api/catalogues')['people']
    assert len(people) == 163
    title = 'Synthetic department import title'
    if kind == 'staff_import.apply':
        batch = h.command('coordinator', 'staff_import.preview', {
            'source_name': 'Department import budget',
            'csv_text': csv_of(*[row(display_name=p['display_name'], title=title) for p in people])})['batch']
        payload, target, revision = {'reason': 'Department import budget'}, batch['id'], batch['revision']
    else:
        payload = {'catalogue': 'people', 'items': [{'id': p['id'], 'revision': p['revision']} for p in people],
                   'set': {'title': title}, 'reason': 'Department catalogue budget'}
        target = revision = None
    job_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        org_revision = db.get(m.Organisation, h.seed['organisation_id']).input_revision
        db.add(m.Job(id=job_id, department_id=h.seed['department_id'], workspace_id=workspace['id'],
            kind='generation', status='succeeded', requested_by=h.seed['identities']['coordinator'],
            input_fingerprint='synthetic', input_snapshot={}, resume_state={'private': True},
            resume_checkpoint={'private': True}))
    invalidations = []
    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.upper().startswith('UPDATE JOBS SET RESUME_STATE='):
            invalidations.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        fingerprint = h.get('coordinator', '/api/staff-imports/' + target)['source_fingerprint'] if kind == 'staff_import.apply' else None
        result = h.command('coordinator', kind, payload, target, revision, expected_fingerprint=fingerprint)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    # A query-count budget is invariant under coverage, CI load and CPU speed.
    assert invalidations == []
    if kind == 'staff_import.apply':
        assert result['applied']['updated_people'] == len(people)
    else:
        assert len(result['records']) == len(people)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == org_revision + 1
        updated = list(db.scalars(select(m.Person).where(m.Person.id.in_([p['id'] for p in people]))))
        revisions = {p['id']: p['revision'] for p in people}
        assert all(p.title == title and p.revision == revisions[p.id] + 1 for p in updated)
        job = db.get(m.Job, job_id)
        assert job.input_fingerprint == 'synthetic' and job.input_snapshot == {}
        assert job.resume_state == job.resume_checkpoint == {'private': True}


def test_input_revision_updates_are_nested_and_roll_back_after_failure(h, monkeypatch):
    from apps.api.services import common, generation_resume
    actor = SimpleNamespace(organisation_id=h.seed['organisation_id'])
    calls = []
    monkeypatch.setattr(generation_resume, 'clear_generation_resume', lambda db, **values: calls.append(values))

    @common.batch_input_invalidations
    def inner(db, actor):
        common.bump_inputs(db, actor)

    @common.batch_input_invalidations
    def outer(db, actor, fail=False):
        inner(db, actor)
        common.bump_inputs(db, actor)
        if fail:
            raise ValueError('Refuse the entire batch')

    with transaction(h.app.state.engine, write=True) as db:
        before = db.get(m.Organisation, actor.organisation_id).input_revision
        outer(db, actor)
        assert db.get(m.Organisation, actor.organisation_id).input_revision == before
        with pytest.raises(ValueError, match='Refuse the entire batch'), db.begin_nested():
            outer(db, actor, fail=True)
        assert db.get(m.Organisation, actor.organisation_id).input_revision == before
        common.bump_inputs(db, actor)
        assert db.get(m.Organisation, actor.organisation_id).input_revision == before
        assert calls == []


def test_input_invalidation_batch_is_nested_and_cleans_up_after_failure(h, monkeypatch):
    from apps.api.services import common, generation_resume
    actor = SimpleNamespace(organisation_id=h.seed['organisation_id'])
    calls = []
    monkeypatch.setattr(generation_resume, 'clear_generation_resume', lambda db, **values: calls.append(values))

    @common.batch_input_invalidations
    def inner(db, actor):
        common.bump_inputs(db, actor)

    @common.batch_input_invalidations
    def outer(db, actor, fail=False):
        inner(db, actor)
        common.bump_inputs(db, actor)
        if fail:
            raise ValueError('Refuse the entire batch')

    with transaction(h.app.state.engine, write=True) as db:
        outer(db, actor)
        assert calls == []
        with pytest.raises(ValueError, match='Refuse the entire batch'):
            outer(db, actor, fail=True)
        assert len(calls) == 0
        common.bump_inputs(db, actor)
        assert len(calls) == 0
