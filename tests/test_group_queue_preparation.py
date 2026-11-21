"""Work and freshness contracts for scheduling-group enqueue."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from apps.api import commands, models as m
from apps.api.db import transaction
from packages.domain import conditions, generation_scope
from packages.domain.planning_groups import GroupResolver
from tests.clock_support import freeze_clock
from tests.test_backend_reads_scale import read_department
from tests.test_planning_groups_drafts import REASON, group, integrated, tree


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_group_queue_evaluates_population_once_per_person_outside_writer(scale_harness, database, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    h = scale_harness(database)
    workspace = read_department(h)
    with transaction(h.app.state.engine) as db:
        draft = db.get(m.Workspace, workspace['id'])
        people = sorted(set(db.scalars(select(m.Membership.person_id).where(
            m.Membership.department_id == draft.department_id))))
        sessions = len(list(db.scalars(select(m.Shift.id).where(m.Shift.workspace_id == draft.id))))
    assert len(people) >= 150 and sessions >= 500
    version = group(h, people=people[:55])
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON},
                          workspace['id'], workspace['revision'])['workspace']
    evaluations, authority_checks = [], []
    writing = False
    retry_write, primary = commands.retry_write, GroupResolver.primary

    def writer(*args, **kwargs):
        nonlocal writing
        writing = True
        try:
            return retry_write(*args, **kwargs)
        finally:
            writing = False

    def count(evaluate):
        def counted(*args, **kwargs):
            evaluations.append(writing)
            return evaluate(*args, **kwargs)
        return counted

    def check_authority(*args):
        authority_checks.append(writing)
        return primary(*args)

    monkeypatch.setattr(commands, 'retry_write', writer)
    monkeypatch.setattr(GroupResolver, 'primary', check_authority)
    monkeypatch.setattr(generation_scope, 'population_allows', count(generation_scope.population_allows))
    monkeypatch.setattr(generation_scope, 'scope_segments', count(conditions.scope_segments), raising=False)
    job = h.command('admin', 'job.create', {'kind': 'generation', 'workspace_id': workspace['id'],
        'planning_group_version_id': version['id'], 'lookahead': 'none'}, revision=workspace['revision'])['job']
    assert job['status'] == 'queued'
    assert 0 < len(evaluations) <= len(people)
    assert not any(evaluations)
    assert authority_checks and all(authority_checks)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_group_queue_rechecks_population_when_membership_changes(h, monkeypatch):
    workspace, shift, version = integrated(h)
    person = h.seed['people'][0]
    with transaction(h.app.state.engine) as db:
        membership = db.scalar(select(m.Membership).where(m.Membership.person_id == person))
        category_id = membership.category_id
        membership_id = membership.id
    retry_write = commands.retry_write
    writer_reached = []

    def concurrent_edit(*args, **kwargs):
        if kwargs['context']['command_kind'] == 'job.create':
            writer_reached.append(True)
            with transaction(h.app.state.engine, write=True) as db:
                db.info['_track_input_changes'] = True
                db.get(m.Membership, membership_id).ends_on = date.fromisoformat(shift['start'][:10])
        return retry_write(*args, **kwargs)

    monkeypatch.setattr(commands, 'retry_write', concurrent_edit)
    refused = h.command('admin', 'job.create', {'kind': 'generation', 'workspace_id': workspace['id'],
        'planning_group_version_id': version['id'], 'lookahead': 'none',
        'generation_scope': {'conditions': {'match': 'all', 'conditions': [
            *tree(person)['conditions'], {'field': 'category_ids', 'values': [category_id]}]}}},
        revision=workspace['revision'], status=422)
    assert writer_reached == [True]
    assert refused['error']['code'] == 'INVALID_SCOPE'


def test_batched_population_matches_canonical_scope_across_change_points():
    from packages.domain.generation_scope import population_candidates

    duties = [{'id': sid, 'start': start, 'end': end} for sid, start, end in (
        ('before', '2026-10-01T09:00:00Z', '2026-10-01T12:00:00Z'),
        ('until-change', '2026-10-01T22:00:00Z', '2026-10-02T00:00:00Z'),
        ('straddling', '2026-10-01T22:00:00Z', '2026-10-02T03:00:00Z'),
        ('after', '2026-10-02T10:00:00Z', '2026-10-02T12:00:00Z'))]
    scope = {'match': 'all', 'conditions': [{'field': 'category_ids', 'values': ['category']}]}
    data = {'organisation': {'timezone': 'UTC'}, 'shifts': duties, 'people': {'one': {}, 'two': {}},
            'categories': {'category': {}}, 'memberships': [
                {'person_id': 'one', 'category_id': 'category', 'ends_on': '2026-10-02'},
                {'person_id': 'two', 'category_id': 'category', 'starts_on': '2026-10-02'}],
            'generation_scope': {'conditions': scope}}
    expected = {duty['id']: [pid for pid in data['people'] if generation_scope.population_allows(data, duty, pid)]
                for duty in duties}
    assert expected == {'before': ['one'], 'until-change': ['one'], 'straddling': [], 'after': ['two']}
    assert population_candidates(data, set(expected)) == expected


def test_batched_population_keeps_capability_grants_scoped_to_clinics():
    from packages.domain.generation_scope import population_candidates

    duty = {'start': '2026-10-01T09:00:00Z', 'end': '2026-10-01T12:00:00Z', 'assignment_type_id': 'duty'}
    duties = [{**duty, 'id': clinic, 'service_ids': [clinic]} for clinic in ('first', 'second')]
    scope = {'conditions': [{'field': 'capability_ids', 'values': ['capability']}]}
    data = {'organisation': {'timezone': 'UTC'}, 'shifts': duties, 'people': {'person': {}},
            'types': {'duty': {'department_id': 'department'}}, 'capabilities': {'capability': {}},
            'grants': [{'person_id': 'person', 'capability_id': 'capability', 'department_id': 'department',
                        'service_id': 'first'}],
            'generation_scope': {'conditions': scope}}
    assert population_candidates(data, {'first', 'second'}) == {'first': ['person'], 'second': []}
