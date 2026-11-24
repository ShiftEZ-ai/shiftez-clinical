import pytest
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from sqlalchemy import event, select

from apps.api import commands, models as m, snapshots
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.services.jobs import require_latest_run
from apps.worker.main import JOB_LEASE_SECONDS, claim_job, prepare_deferred_generation, process_one
from tests.test_events import event as calendar_event, event_type, freeze_clock, repeat
from tests.test_generation_population import scope
from tests.test_planning_groups_drafts import REASON, group, integrated, settings, tree


DATABASES = ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)]


@pytest.fixture
def snapshot_builds(monkeypatch):
    builds = []
    build = snapshots._domain_snapshot

    def counted(db, *args, **kwargs):
        builds.append(bool(db.connection().info.get('rota_write')))
        return build(db, *args, **kwargs)

    monkeypatch.setattr(snapshots, '_domain_snapshot', counted)
    return builds


@pytest.mark.parametrize('h', DATABASES, indirect=True)
@pytest.mark.parametrize('empty', [False, True])
def test_population_queue_checks_scope_off_writer(h, snapshot_builds, empty, monkeypatch):
    from packages.domain.time import aware

    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    calendar_event(h, event_type(h, blocks_duties=True), repeat=repeat(), repeat_until='2026-10-19')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    snapshot_builds.clear()
    selection = scope('tags', 'No matching intake') if empty else scope('person_ids', h.seed['people'][0])
    reply = h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'generation', 'generation_scope': selection,
    }, revision=workspace['revision'], status=422 if empty else 200)
    if empty:
        assert reply['error']['code'] == 'INVALID_SCOPE'
    else:
        assert reply['job']['status'] == 'queued'
    assert snapshot_builds == [False]


@pytest.mark.parametrize('h', DATABASES, indirect=True)
def test_group_queue_rechecks_moved_inputs_off_writer(h, snapshot_builds, monkeypatch):
    workspace, _, group = integrated(h)
    snapshot_builds.clear()
    write = commands.retry_write
    moved = False

    def advance_inputs(engine, operation, **kwargs):
        nonlocal moved
        if kwargs.get('context', {}).get('command_kind') == 'job.create' and not moved:
            with transaction(engine, write=True) as db:
                db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
            moved = True
        return write(engine, operation, **kwargs)

    monkeypatch.setattr(commands, 'retry_write', advance_inputs)
    job = h.command('coordinator', 'job.create', {
        'workspace_id': workspace['id'], 'kind': 'generation', 'planning_group_version_id': group['id'], 'lookahead': 'none',
    }, revision=workspace['revision'])['job']
    assert moved
    assert job['status'] == 'queued'
    assert snapshot_builds == [False, False]



@pytest.mark.parametrize('h', DATABASES, indirect=True)
def test_unscoped_queue_does_not_load_shifts(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    statements = []
    engine = h.app.state.engine

    def counted(conn, _cursor, sql, *_args):
        if conn.info.get('rota_write') and 'shifts.type_snapshot' in sql:
            statements.append(sql)

    event.listen(engine, 'before_cursor_execute', counted)
    try:
        reply = h.command('coordinator', 'job.create', {
            'workspace_id': workspace['id'], 'kind': 'generation',
        }, revision=workspace['revision'])
    finally:
        event.remove(engine, 'before_cursor_execute', counted)
    assert reply['job']['status'] == 'queued'
    assert statements == []


@pytest.mark.postgres
@pytest.mark.parametrize('h', ['postgresql'], indirect=True)
def test_generation_role_lookup_is_dated_and_read_only(h):
    from packages.domain.conditions import rule_holds, scope_segments
    from packages.domain.time import aware

    workspace = h.workspace()
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(
            m.RoleBinding.identity_id == h.seed['identities']['staff'], m.RoleBinding.role == 'staff'))
        binding.ends_on = date(2026, 10, 5)
    with transaction(h.app.state.engine) as db:
        bindings = snapshots.generation_role_bindings(db, db.get(m.Workspace, workspace['id']))
    data = {'organisation': {'timezone': 'UTC'}, 'generation_role_bindings': bindings}
    original = deepcopy(data)
    morning = aware('2026-10-04T08:00:00+00:00'), aware('2026-10-04T12:00:00+00:00')
    tomorrow = tuple(at + timedelta(days=1) for at in morning)
    assert rule_holds(data, person_id, None, *morning)('roles') == {'staff'}
    assert rule_holds(data, person_id, None, *tomorrow)('roles') == set()
    midnight = aware('2026-10-05T00:00:00+00:00')
    role_scope = scope('roles', 'staff')
    assert list(scope_segments(data, role_scope, person_id, None, morning[0], tomorrow[1])) == [
        (morning[0], midnight, True), (midnight, tomorrow[1], False)]
    assert data == original


@pytest.mark.parametrize('h', DATABASES, indirect=True)
@pytest.mark.parametrize('control', ['continue', 'lease_retry'])
@pytest.mark.parametrize('population_moved', [False, True])
def test_implicit_group_selection_stays_implicit_after_preparation(h, control, population_moved):
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    owned, overlapping, stays = h.seed['people'][:3]
    group(h, people=[overlapping], owner='admin', primary_priority=5)
    workspace, shift, selected = integrated(h, membership=tree(owned, overlapping, stays))
    settings(h, group_lookahead='none')
    payload = {'workspace_id': workspace['id'], 'kind': 'generation',
               'planning_group_version_id': selected['id']}
    refused = h.command('coordinator', 'job.create', {**payload,
        'generation_scope': {'conditions': tree(owned, overlapping)}},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    engine = h.app.state.engine
    with transaction(engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'],
                                                m.Job.kind == 'generation')) is None
    queued = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    with transaction(engine, write=True) as db:
        claim = claim_job(db)
    original = deepcopy(claim['input']['generation_request'])
    assert 'generation_scope' not in original
    prepare_deferred_generation(engine, claim, h.settings)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        assert set(row.input_snapshot['planning_population'][shift['id']]) == {owned, stays}
        if control == 'continue':
            row.status, row.completed_at, row.result = 'succeeded', m.now(), {'status': 'needs_more_time'}
        else:
            row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)
    if population_moved:
        group(h, people=[owned], owner='admin', primary_priority=5)
    if control == 'continue':
        response = h.command('coordinator', 'job.continue', {'reason': REASON}, queued['id'],
                             status=403 if population_moved else 200)
        if population_moved:
            assert response['error']['code'] == 'GROUP_GENERATION_SCOPE'
            with transaction(engine) as db:
                assert list(db.scalars(select(m.Job.id).where(m.Job.workspace_id == workspace['id'],
                                                              m.Job.kind == 'generation'))) == [queued['id']]
            return
        target = response['job']
    else:
        target = queued

    def scheduler(*_args, **_kwargs):
        assert not population_moved
        return {'status': 'needs_more_time'}

    assert process_one(engine, settings=h.settings, scheduler=scheduler)
    with transaction(engine) as db:
        row = db.get(m.Job, target['id'])
        expected = ('failed', 'GROUP_GENERATION_SCOPE') if population_moved else ('succeeded', None)
        assert (row.status, row.error_code) == expected
        assert row.attempts == (2 if control == 'lease_retry' else 1)
        assert row.input_snapshot['_generation_request'] == original


def test_scope_run_supersedes_only_after_worker_authority(h):
    workspace, _, selected = integrated(h)
    settings(h, group_lookahead='none')
    engine = h.app.state.engine
    payload = {'workspace_id': workspace['id'], 'kind': 'generation',
               'planning_group_version_id': selected['id'],
               'generation_scope': scope('person_ids', h.seed['people'][0])}
    older = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    with transaction(engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(engine, claim, h.settings)
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, older['id'])
        row.status, row.completed_at, row.result = 'succeeded', m.now(), {'status': 'candidate_feasible'}
    newer = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    with transaction(engine) as db:
        assert db.get(m.Job, newer['id']).supersedes == []
        require_latest_run(db, db.get(m.Job, older['id']))
    with transaction(engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(engine, claim, h.settings)
    with transaction(engine) as db:
        assert db.get(m.Job, newer['id']).supersedes == [older['id']]

def test_generation_scope_fields_refuse_outside_and_accept_owned_population(h):
    from datetime import date
    from packages.contracts.schemas import GENERATION_SCOPE_FIELDS
    from tests.governance_support import identity

    own, outside = h.seed['people'][:2]
    identity(h, 'generation_scope_outside', ['operator'], person=outside)
    workspace, _, _ = integrated(h, people=[])
    settings(h, group_lookahead='none')
    starts_on, ends_on = date(2020, 1, 1), date(2026, 10, 7)
    with transaction(h.app.state.engine, write=True) as db:
        department_id = h.seed['department_id']
        own_category = m.Category(department_id=department_id, name='Owned scope category', starts_on=starts_on,
                                  ends_on=ends_on, active=True)
        outside_category = m.Category(department_id=department_id, name='Outside scope category', starts_on=starts_on,
                                      ends_on=ends_on, active=True)
        own_capability = m.Capability(department_id=department_id, name='Owned scope capability', starts_on=starts_on,
                                      ends_on=ends_on, active=True)
        outside_capability = m.Capability(department_id=department_id, name='Outside scope capability',
                                          starts_on=starts_on, ends_on=ends_on, active=True)
        own_programme = m.Programme(department_id=department_id, name='Owned scope programme', starts_on=starts_on,
                                    ends_on=ends_on, active=True)
        outside_programme = m.Programme(department_id=department_id, name='Outside scope programme', starts_on=starts_on,
                                        ends_on=ends_on, active=True)
        db.add_all([own_category, outside_category, own_capability, outside_capability, own_programme, outside_programme])
        db.flush()
        own_cohort = m.Cohort(department_id=department_id, programme_id=own_programme.id, name='Owned scope cohort',
                              starts_on=starts_on, ends_on=ends_on)
        outside_cohort = m.Cohort(department_id=department_id, programme_id=outside_programme.id,
                                 name='Outside scope cohort', starts_on=starts_on, ends_on=ends_on)
        own_rotation_type = m.RotationType(department_id=department_id, name='Owned scope rotation', starts_on=starts_on,
                                           ends_on=ends_on, active=True)
        outside_rotation_type = m.RotationType(department_id=department_id, name='Outside scope rotation',
                                               starts_on=starts_on, ends_on=ends_on, active=True)
        own_service = m.Service(department_id=department_id, name='Owned scope service', starts_on=starts_on,
                                ends_on=ends_on, active=True)
        outside_service = m.Service(department_id=department_id, name='Outside scope service', starts_on=starts_on,
                                    ends_on=ends_on, active=True)
        own_tag = m.Tag(department_id=department_id, name='Owned scope tag', labels=['people'])
        outside_tag = m.Tag(department_id=department_id, name='Outside scope tag', labels=['people'])
        db.add_all([own_cohort, outside_cohort, own_rotation_type, outside_rotation_type,
                    own_service, outside_service, own_tag, outside_tag])
        db.flush()
        db.add_all([
            m.Membership(department_id=department_id, person_id=own, category_id=own_category.id,
                         starts_on=starts_on, ends_on=ends_on),
            m.Membership(department_id=department_id, person_id=outside, category_id=outside_category.id,
                         starts_on=starts_on, ends_on=ends_on),
            m.CapabilityGrant(department_id=department_id, person_id=own, capability_id=own_capability.id,
                              granted_by=h.seed['identities']['credentialer'], evidence='Synthetic scope evidence',
                              starts_on=starts_on, ends_on=ends_on),
            m.CapabilityGrant(department_id=department_id, person_id=outside, capability_id=outside_capability.id,
                              granted_by=h.seed['identities']['credentialer'], evidence='Synthetic scope evidence',
                              starts_on=starts_on, ends_on=ends_on),
            m.ProgrammeMembership(department_id=department_id, person_id=own, programme_id=own_programme.id,
                                  starts_on=starts_on, ends_on=ends_on),
            m.ProgrammeMembership(department_id=department_id, person_id=outside, programme_id=outside_programme.id,
                                  starts_on=starts_on, ends_on=ends_on),
            m.Rotation(department_id=department_id, person_id=own, cohort_id=own_cohort.id,
                       rotation_type_id=own_rotation_type.id, service_id=own_service.id,
                       starts_on=starts_on, ends_on=ends_on),
            m.Rotation(department_id=department_id, person_id=outside, cohort_id=outside_cohort.id,
                       rotation_type_id=outside_rotation_type.id, service_id=outside_service.id,
                       starts_on=starts_on, ends_on=ends_on),
            m.Eligibility(department_id=department_id, person_id=own, service_id=own_service.id,
                          starts_on=starts_on, ends_on=ends_on),
            m.Eligibility(department_id=department_id, person_id=outside, service_id=outside_service.id,
                          starts_on=starts_on, ends_on=ends_on),
            m.TagMember(department_id=department_id, tag_id=own_tag.id, kind='people', member_id=own,
                        starts_on=starts_on, ends_on=ends_on),
            m.TagMember(department_id=department_id, tag_id=outside_tag.id, kind='people', member_id=outside,
                        starts_on=starts_on, ends_on=ends_on),
        ])

    values = {
        'category_ids': (own_category.id, outside_category.id),
        'capability_ids': (own_capability.id, outside_capability.id),
        'programme_ids': (own_programme.id, outside_programme.id),
        'rotation_type_ids': (own_rotation_type.id, outside_rotation_type.id),
        'cohort_ids': (own_cohort.id, outside_cohort.id),
        'tags': (own_tag.name, outside_tag.name),
        'person_ids': (own, outside),
        'eligible_service_ids': (own_service.id, outside_service.id),
        'roles': ('staff', 'operator'),
    }
    assert set(values) == set(GENERATION_SCOPE_FIELDS)
    for field in GENERATION_SCOPE_FIELDS:
        for value, status in zip((values[field][1], values[field][0]), (403, 200)):
            result = h.command('coordinator', 'job.create', {
                'workspace_id': workspace['id'], 'kind': 'generation',
                'generation_scope': {'conditions': {'match': 'all', 'conditions': [
                    {'field': field, 'values': [value]}]}},
            }, revision=workspace['revision'], status=status)
            if status == 200:
                assert result['job']['status'] == 'queued'
                h.command('coordinator', 'job.cancel', {'reason': 'Synthetic scope parity case'},
                          result['job']['id'], result['job']['revision'])
