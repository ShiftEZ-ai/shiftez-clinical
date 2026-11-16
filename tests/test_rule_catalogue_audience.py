"""Shared proof/reach never carries a reader's private draft choices or authority."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event, Lock

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import rule_catalogue
from packages.contracts import schemas as s
from tests.governance_support import identity


PATH = '/api/rules/catalogue?version=2&include_hidden=true'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_rule_detail_uses_the_rules_read_audience(h):
    path = '/api/rules/catalogue/duties_per_week/detail'
    readers = tuple(dict.fromkeys(('admin', *s.PERMISSIONS['rules.read'])))
    for role in readers:
        if role not in h.seed['identities']:
            identity(h, role, [role])
        response = h.client(role).get(path)
        assert response.status_code == 200, (role, response.text)
        assert response.json()['entry']['id'] == 'duties_per_week'

    non_reader = next(role for role in s.ROLE_NAMES if role != 'admin' and role not in s.PERMISSIONS['rules.read'])
    if non_reader not in h.seed['identities']:
        identity(h, non_reader, [non_reader])
    refused = h.client(non_reader).get(path)
    assert refused.status_code == 403, (non_reader, refused.text)


def readers(h, count=3):
    for i in range(count):
        identity(h, f'catalogue_reader_{i}', ['coordinator', 'approver'], person=h.seed['people'][i % 3])
    return [h.client(f'catalogue_reader_{i}') for i in range(count)]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_coordinator_counts_reuse_the_shared_dependency_stamp(h, monkeypatch):
    monkeypatch.setattr(rule_catalogue, '_catalogue_stamp',
                        lambda *_args: pytest.fail('counts must reuse the shared dependency stamp'))
    first = h.get('coordinator', '/api/catalogues?counts_only=true')
    assert isinstance(first['rules_in_force'], int)
    assert h.get('coordinator', '/api/catalogues?counts_only=true') == first


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_coordinator_counts_refresh_after_policy_edits(h):
    path = '/api/catalogues?counts_only=true'
    before = h.get('coordinator', path)['rules_in_force']
    policy = h.policy({'kind': 'minimum_cover', 'minimum': 1})
    assert h.get('coordinator', path)['rules_in_force'] == before + 1
    h.command('approver', 'policy.retire', {'reason': 'Synthetic count invalidation'},
              policy['id'], policy['revision'])
    assert h.get('coordinator', path)['rules_in_force'] == before


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_coordinator_counts_exclude_private_draft_policies_for_both_readers(h):
    from tests.test_rule_catalogue_in_force import add_policy

    h.workspace('Shared count projection anchor')
    first, second = readers(h, 2)
    path = '/api/catalogues?counts_only=true'
    before = first.get(path).json()['rules_in_force']
    private = h.command('catalogue_reader_0', 'workspace.create', {
        'name': 'Private count regression draft', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    add_policy(h, {'kind': 'minimum_cover', 'minimum': 1}, definition={'workspace_id': private['id']})
    assert first.get('/api/workspaces/' + private['id']).status_code == 200
    assert second.get('/api/workspaces/' + private['id']).status_code == 404
    assert first.get(path).json()['rules_in_force'] == before
    rule_catalogue._catalogue_cache.clear()  # The second reader also exercises the durable shared count.
    assert second.get(path).json()['rules_in_force'] == before


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('anchor', [False, True])
def test_same_rights_readers_share_one_build_per_stamp(h, monkeypatch, anchor):
    if anchor:
        h.workspace('Shared catalogue anchor')
    clients = readers(h)
    builds = []
    original = rule_catalogue._catalogue_uncached

    def counted(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    for _ in range(2):
        bodies = []
        for i, client in enumerate(clients):
            if anchor and i == 1:
                rule_catalogue._catalogue_cache.clear()  # Another API process has only the durable shared body.
            response = client.get(PATH)
            assert response.status_code == 200, response.text
            bodies.append(response.json())
        assert bodies[0] == bodies[1] == bodies[2]
        assert '_live_definitions' not in bodies[0]
        assert len(builds) == 1
        # A policy revision invalidates reach for every reader, even without a new row.
        with transaction(h.app.state.engine, write=True) as db:
            db.scalar(select(m.Policy).where(m.Policy.department_id == h.seed['department_id'])).revision += 1
        builds.clear()


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_concurrent_same_rights_readers_wait_for_one_durable_build(h, monkeypatch):
    h.workspace('Single-flight durable anchor')
    clients = readers(h)
    ready = Barrier(3)
    waiters_ready = Event()
    mutex = Lock()
    waits = []
    builds = []
    original = rule_catalogue._catalogue_uncached

    class Flight:
        def __init__(self):
            self.event = Event()

        def wait(self, timeout):
            with mutex:
                waits.append(True)
                if len(waits) == 2:
                    waiters_ready.set()
            return self.event.wait(timeout)

        def set(self):
            self.event.set()

    def counted(*args, **kwargs):
        builds.append(True)
        assert waiters_ready.wait(15), 'both other readers must join this in-flight build'
        return original(*args, **kwargs)

    def read(client):
        ready.wait(timeout=15)
        response = client.get(PATH)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(rule_catalogue, 'Event', Flight)
    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    with ThreadPoolExecutor(max_workers=3) as executor:
        bodies = list(executor.map(read, clients))
    assert builds == [True] and waits == [True, True]
    assert bodies[0] == bodies[1] == bodies[2]


@pytest.mark.parametrize('first_role', ['coordinator', 'credentialer'])
def test_different_rights_have_separate_bodies_in_both_read_orders(h, monkeypatch, first_role):
    workspace = h.workspace('Planner-only source')
    builds = []
    original = rule_catalogue._catalogue_uncached

    def counted(db, actor, *args, **kwargs):
        builds.append(actor.roles)
        return original(db, actor, *args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    other = 'credentialer' if first_role == 'coordinator' else 'coordinator'
    bodies = {role: h.get(role, PATH) for role in (first_role, other)}
    assert len(builds) == 2
    assert workspace['id'] in {row['workspace_id'] for row in bodies['coordinator']['source_options']}
    assert bodies['credentialer']['source_options'] == [
        {'workspace_id': None, 'label': 'Live', 'captured_at': None, 'changed': 0}]
    for role in (first_role, other):
        assert h.get(role, PATH) == bodies[role]
    assert len(builds) == 2
    denied = h.client('staff').get(PATH)
    assert denied.status_code == 403 and 'approved_by_name' not in denied.text


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_same_rights_keep_private_sources_and_self_approval_per_reader(h, monkeypatch):
    h.workspace('Shared projection anchor')
    clients = readers(h, 2)
    private = h.command('catalogue_reader_0', 'workspace.create', {
        'name': 'Only first reader may see this source', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SelfApprovalPermission(department_id=h.seed['department_id'],
            identity_id=h.seed['identities']['catalogue_reader_0'], actions=['rule.activate'],
            starts_on=date(2020, 1, 1), ends_on=None,
            created_by=h.seed['identities']['admin'], reason='Synthetic reader-specific grant'))
    builds = []
    original = rule_catalogue._catalogue_uncached

    def counted(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    first, second = [client.get(PATH) for client in clients]
    assert first.status_code == second.status_code == 200
    assert len(builds) == 1
    assert private['id'] in {row['workspace_id'] for row in first.json()['source_options']}
    assert private['id'] not in {row['workspace_id'] for row in second.json()['source_options']}
    assert clients[0].get('/api/me').json()['self_approval'] == ['rule.activate']
    assert clients[1].get('/api/me').json()['self_approval'] == []
    # Grants and own-proposal eligibility are already resolved by /api/me and commands, never cached here.
    assert 'self_approval' not in first.json() and 'self_approval' not in second.json()
    with transaction(h.app.state.engine) as db:
        projections = list(db.scalars(select(m.WorkspaceReadProjection).where(
            m.WorkspaceReadProjection.kind == 'rule_catalogue')))
        assert len(projections) == 1
        assert 'source_options' not in projections[0].value
        assert private['id'] not in str(projections[0].value)
    first_body = first.json()
    first_body['entries'].clear()
    assert clients[0].get(PATH).json()['entries']
    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached',
                        lambda *_args, **_kwargs: pytest.fail('conditional reads must stop before building'))
    monkeypatch.setattr(rule_catalogue, '_catalogue_overlay',
                        lambda *_args, **_kwargs: pytest.fail('conditional reads must stop before the overlay'))
    for client, response in zip(clients, (first, second)):
        unchanged = client.get(PATH, headers={'If-None-Match': response.headers['etag']})
        assert unchanged.status_code == 304 and unchanged.content == b''
    assert len(builds) == 1


def test_write_transaction_reads_pending_changes_instead_of_the_shared_cache(h):
    h.get('coordinator', PATH)
    from apps.api.auth import Actor
    actor = Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                  'Synthetic coordinator', frozenset({'coordinator'}), None, 'test')
    with transaction(h.app.state.engine, write=True) as db:
        policy = db.scalar(select(m.Policy).where(m.Policy.department_id == actor.department_id))
        policy.name = 'Pending policy change in this writer'
        # Even an unchanged supplied stamp cannot authorize a cached read in a writer.
        body = rule_catalogue.catalogue(db, actor, 2, True, _stamp=('not-current',))
        assert any(entry['rule'] and entry['rule']['name'] == policy.name for entry in body['entries'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_private_draft_findings_from_first_builder_never_reach_second_reader(h, monkeypatch):
    from tests.test_rule_catalogue_api import approve, line, propose

    live = approve(h, propose(h, 'duties_per_week', [line(numbers={'max': {'value': 1}})])['record'])
    shared = h.workspace('Shared draft with findings')
    first, second = readers(h, 2)
    private = h.command('catalogue_reader_0', 'workspace.create', {
        'name': 'Private first-builder draft', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        for workspace in (shared, private):
            db.get(m.Workspace, workspace['id']).validation = {'findings': [{
                'rule_id': live['id'], 'severity': 'error', 'code': 'QUOTA',
                'message': workspace['name'], 'person_id': h.seed['people'][0], 'detail': {'line': 0}}]}
    assert private['name'] in first.get('/api/workspaces/' + private['id']).text
    assert second.get('/api/workspaces/' + private['id']).status_code == 404
    builds, findings = [], []
    original_build = rule_catalogue._catalogue_uncached
    original_findings = rule_catalogue.draft_findings

    def counted(db, actor, *args, **kwargs):
        builds.append(actor.identity_id)
        assert db.info['workspace_shared_only'] is True
        return original_build(db, actor, *args, **kwargs)

    def observed_findings(db, actor):
        found = original_findings(db, actor)
        findings.extend(found)
        return found

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', counted)
    monkeypatch.setattr(rule_catalogue, 'draft_findings', observed_findings)
    first_body = first.get(PATH).json()
    rule_catalogue._catalogue_cache.clear()  # The second reader consumes the durable first-builder body.
    second_response = second.get(PATH)
    assert second_response.status_code == 200
    second_body = second_response.json()
    assert builds == [h.seed['identities']['catalogue_reader_0']]
    assert {finding['workspace_id'] for finding in findings} == {shared['id']}
    for body in (first_body, second_body):
        assert shared['id'] in str(body['attention'])
        assert private['id'] not in str(body['attention'])
        assert private['name'] not in str(body['attention'])
    assert private['id'] in {row['workspace_id'] for row in first_body['source_options']}
    assert private['id'] not in {row['workspace_id'] for row in second_body['source_options']}
    assert second.get(PATH).json() == second_body
    assert builds == [h.seed['identities']['catalogue_reader_0']]
