"""The remembered catalogue never hides a change: any edit shows on the next read."""
from collections import OrderedDict
import pytest
from datetime import date
from types import SimpleNamespace

from packages.contracts.schemas import ROLE_NAMES
from packages.contracts import schemas as s
from apps.api.errors import Problem
from apps.api.services import rule_catalogue
from apps.api.settings import Settings
from tests.governance_support import identity


def _clear_catalogue_list_cache(app):
    endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', None) == '/api/catalogues')
    for cell in endpoint.__closure__ or ():
        if isinstance(cell.cell_contents, OrderedDict):
            cell.cell_contents.clear()


def _conditional_catalogue_queries(h, tag):
    from sqlalchemy import event
    statements = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', listener)
    try:
        response = h.client('coordinator').get('/api/catalogues', headers={'If-None-Match': tag})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', listener)
    return response, statements


def test_conditional_catalogue_etag_reads_creator_stamp_without_building_lists(h):
    client = h.client('coordinator')
    first = client.get('/api/catalogues')
    tag = first.headers['etag']
    warm, warm_sql = _conditional_catalogue_queries(h, tag)
    _clear_catalogue_list_cache(h.app)
    cold, cold_sql = _conditional_catalogue_queries(h, tag)
    assert warm.status_code == cold.status_code == 304
    assert len(warm_sql) == len(cold_sql) == 9


def test_non_rotation_catalogues_keep_the_2000_row_bound(h):
    from datetime import date
    from apps.api import models as m
    from apps.api.db import transaction
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Service(department_id=h.seed['department_id'], name=f'Synthetic service {i}',
                            starts_on=date(2020, 1, 1)) for i in range(2001))
    assert h.get('coordinator', '/api/catalogues', status=409)['error']['code'] == 'CATALOGUE_LIMIT'


def test_edits_show_immediately_and_unchanged_reads_are_not_resent(h):
    client = h.client('coordinator')
    first = client.get('/api/catalogues')
    tag = first.headers['etag']
    assert client.get('/api/catalogues', headers={'If-None-Match': tag}).status_code == 304
    person = h.configure('people', {'display_name': 'Cache Newcomer'})['record']
    after = client.get('/api/catalogues', headers={'If-None-Match': tag})
    assert after.status_code == 200 and any(p['id'] == person['id'] for p in after.json()['people'])
    h.command('coordinator', 'catalogue.bulk_update', {'catalogue': 'people', 'items': [{'id': person['id'], 'revision': person['revision']}], 'set': {'display_name': 'Cache Renamed'}, 'reason': 'Rename'})
    names = {p['id']: p['display_name'] for p in client.get('/api/catalogues').json()['people']}
    assert names[person['id']] == 'Cache Renamed'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('counts_only', [False, True], ids=['lists', 'counts'])
@pytest.mark.parametrize('owner_first', [True, False], ids=['owner-warms', 'other-warms'])
def test_private_workspace_policies_never_cross_catalogue_readers(h, counts_only, owner_first):
    from apps.api import models as m
    from apps.api.db import transaction

    owner, other = 'g9_owner (test)', 'g9_other (test)'
    for reader in (owner, other):
        identity(h, reader, ['coordinator'])
    private = h.command(owner, 'workspace.create', {
        'name': 'Private policy draft (test)', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'visibility': 'private', 'purpose': 'sandbox'})['workspace']
    policy = h.command(owner, 'policy.create', {
        'name': 'Private policy (test)', 'owner': 'Synthetic owner (test)', 'source': 'Privacy regression',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 1}}})['record']
    # Existing workspace-linked policies remain readable; new sandbox rules use adjustments instead.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        row.definition = {**row.definition, 'workspace_id': private['id']}
    assert policy['id'] not in {row['id'] for row in h.get(other, '/api/policies?status=proposed')['policies']}

    endpoint = next(route.endpoint for route in h.app.routes if getattr(route, 'path', None) == '/api/catalogues')
    cache = next(cell.cell_contents for cell in endpoint.__closure__ or ()
                 if isinstance(cell.cell_contents, OrderedDict))
    path = '/api/catalogues?include=policies' + ('&counts_only=true' if counts_only else '')
    readers = (owner, other) if owner_first else (other, owner)
    responses = {readers[0]: h.client(readers[0]).get(path)}
    shared = dict(cache)
    responses[readers[1]] = h.client(readers[1]).get(path)
    assert all(response.status_code == 200 for response in responses.values())
    own, peer = responses[owner], responses[other]
    if counts_only:
        assert own.json()['counts']['policies'] == peer.json()['counts']['policies'] + 1
        assert own.json()['rules_in_force'] == peer.json()['rules_in_force']
    else:
        assert policy['id'] not in {row['id'] for row in peer.json()['policies']}
        assert policy['id'] in {row['id'] for row in own.json()['policies']}
        assert own.json()['_versions']['policies'] != peer.json()['_versions']['policies']
        delta = h.get(other, path + '&known=' + own.json()['_versions']['policies'])
        assert delta['policies'] == peer.json()['policies']
    assert own.headers['etag'] != peer.headers['etag']
    for reader, response in responses.items():
        assert h.client(reader).get(path).json() == response.json()
        assert h.client(reader).get(path, headers={'If-None-Match': response.headers['etag']}).status_code == 304
        other_tag = responses[other if reader == owner else owner].headers['etag']
        assert h.client(reader).get(path, headers={'If-None-Match': other_tag}).status_code == 200
    assert cache == shared
    assert len([key for key in cache if key[-1] == 'policies']) == 1
    assert policy['id'] not in str(cache)


def test_boot_catalogues_hydrate_identical_full_records_and_keep_separate_etags(h):
    client = h.client('coordinator')
    full = client.get('/api/catalogues')
    boot = client.get('/api/catalogues?boot=true')
    assert boot.status_code == 200
    compact = boot.json()
    assert {'people', 'categories', 'services', 'assignment_types', 'tags'} <= compact.keys()
    assert not {'memberships', 'eligibilities', 'grants', 'policies', 'exceptions', 'tag_members',
                'programme_memberships', 'rotations', 'mentorships', 'programme_calendar_entries',
                'recurring_series', 'operating_calendars', 'resource_pools', 'department_content', 'closures'} & compact.keys()
    assert set(compact['_versions']) == compact.keys() - {'_versions'}
    assert all(compact[key] == full.json()[key] for key in compact if key != '_versions')
    known = ','.join(compact['_versions'].values())
    delta = client.get('/api/catalogues?known=' + known).json()
    assert not (compact.keys() - {'_versions'}) & delta.keys()
    assert {**compact, **delta} == full.json()
    assert client.get('/api/catalogues?boot=true', headers={'If-None-Match': boot.headers['etag']}).status_code == 304
    assert client.get('/api/catalogues', headers={'If-None-Match': boot.headers['etag']}).status_code == 200
    assert client.get('/api/catalogues?boot=true', headers={'If-None-Match': full.headers['etag']}).status_code == 200
    assert client.get('/api/catalogues?boot=true&known=' + known).json() == {'_versions': compact['_versions']}


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_boot_and_hydrated_catalogues_keep_permissions(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    permitted = role not in {'staff', 'operator', 'installation_admin'}
    for path in ('/api/catalogues?boot=true', '/api/catalogues?known='):
        result = h.get(role, path, status=200 if permitted else 403)
        if permitted:
            assert result['people']
            if role == 'programme_lead':
                assert result['programmes'] == []  # An unlinked director leads no programme.


def test_boot_catalogues_keep_live_edits(h):
    initial = h.get('admin', '/api/catalogues?boot=true')
    person = h.configure('people', {'display_name': 'Boot newcomer'})['record']
    changed = h.get('admin', '/api/catalogues?boot=true&known=' + ','.join(initial['_versions'].values()))
    assert any(p['id'] == person['id'] for p in changed['people'])
    assert changed['_versions']['people'] != initial['_versions']['people']


def test_rota_detail_answers_304_until_an_edit_or_a_check(h):
    client = h.client('coordinator')
    w = h.workspace()
    path = '/api/workspaces/' + w['id']
    tag = client.get(path).headers['etag']
    assert client.get(path, headers={'If-None-Match': tag}).status_code == 304
    w = h.shift(w)['workspace']
    after = client.get(path, headers={'If-None-Match': tag})
    assert after.status_code == 200 and len(after.json()['shifts']) == 1
    tag = after.headers['etag']
    # A rota check keeps the revision but replaces the validation: the detail is sent again.
    h.lifecycle(w, 'validate')
    checked = client.get(path, headers={'If-None-Match': tag})
    assert checked.status_code == 200 and checked.json()['workspace']['validation']


def test_rule_catalogue_revalidation_stops_before_projection(h, monkeypatch):
    """Revision-only conditional reads must not rebuild the department catalogue."""
    from apps.api.services import rule_catalogue

    client = h.client('auditor')
    first = client.get('/api/rules/catalogue')
    assert first.status_code == 200 and first.headers.get('etag')
    called = []
    original = rule_catalogue._catalogue_uncached

    def fail_if_rebuilt(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached', fail_if_rebuilt)
    repeat = client.get('/api/rules/catalogue', headers={'if-none-match': first.headers['etag']})
    assert repeat.status_code == 304 and repeat.content == b'' and not called


def test_rule_catalogue_projection_survives_api_process_cache_reset(h, monkeypatch):
    """A second API worker can serve the durable catalogue without rebuilding proof/reach."""
    h.workspace('Catalogue projection anchor')
    client = h.client('auditor')
    first = client.get('/api/rules/catalogue')
    assert first.status_code == 200
    rule_catalogue._catalogue_cache.clear()
    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached',
                        lambda *_args, **_kwargs: pytest.fail('durable catalogue miss rebuilt the proof'))
    second = client.get('/api/rules/catalogue')
    assert second.status_code == 200 and second.json() == first.json()


def test_rule_catalogue_projection_stamp_is_bounded(h, monkeypatch):
    """The structured ETag stamp is digested before it enters varchar(64) projection storage."""
    from apps.api.services import rule_catalogue

    h.workspace('Catalogue projection anchor')
    seen = []

    def capture(_db, _actor, _kind, stamp, build, **_kwargs):
        seen.append(stamp)
        return build()

    monkeypatch.setattr(rule_catalogue, 'cached_department', capture, raising=False)
    # The API path imports the helper from the module at call time.
    monkeypatch.setattr('apps.api.services.read_projections.cached_department', capture)
    first = h.client('auditor').get('/api/rules/catalogue')
    assert first.status_code == 200
    assert seen and len(seen[0]) == 64


def test_rule_catalogue_single_flight_wait_is_bounded_without_stale_safety_data(monkeypatch):
    """A cold proof build cannot pin a request worker or serve an old rules proof."""
    waits = []
    assert Settings().catalogue_build_wait_seconds == 60

    class Pending:
        def wait(self, timeout):
            waits.append(timeout)
            return False

    actor = SimpleNamespace(department_id='department', identity_id='identity', person_id=None,
                            roles={'coordinator'}, permits=lambda *roles: 'coordinator' in roles)
    stamp = ('revision',)
    from apps.api.services.read_projections import department_audience
    audience_values = ('catalogue-shared-v1', 1, False, '2026-11-07', None, False)
    audience = department_audience(None, actor, audience_values, shared_body=True)
    key = ('department', 1, False, '2026-11-07', None, False, stamp, audience)
    rule_catalogue._catalogue_cache.pop(key, None)
    monkeypatch.setattr(rule_catalogue, 'require_catalogue_read', lambda *_args: None)
    monkeypatch.setattr(rule_catalogue, '_catalogue_uncached',
                        lambda *_args, **_kwargs: pytest.fail('bounded waiter must not rebuild'))
    rule_catalogue._catalogue_building[key] = Pending()
    try:
        with pytest.raises(Problem) as error:
            rule_catalogue.catalogue(None, actor, day=date(2026, 11, 7), _stamp=stamp)
    finally:
        rule_catalogue._catalogue_building.pop(key, None)
    assert error.value.status == 503 and error.value.code == 'RULE_CATALOGUE_BUSY'
    assert waits == [Settings().catalogue_build_wait_seconds]
    assert error.value.details == {'retry_after': Settings().catalogue_build_wait_seconds}


def test_rule_catalogue_busy_exposes_retry_after(h, monkeypatch):
    """Busy proof reads tell the browser when to retry instead of looking like a dead server."""
    from apps.api.services import rule_catalogue

    def busy(*_args, **_kwargs):
        raise Problem(503, 'RULE_CATALOGUE_BUSY', 'The rule catalogue is still updating; try again',
                      {'retry_after': 7})

    monkeypatch.setattr(rule_catalogue, 'catalogue', busy)
    response = h.client('auditor').get('/api/rules/catalogue')
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '7'


def test_rule_catalogue_updating_envelope_returns_202_and_preserves_null_job(h, monkeypatch):
    from apps.api.services import rule_catalogue

    updating = s.CatalogueUpdating(state='updating', projection='rule_catalogue', stamp='catalogue-stamp',
                                   job_id=None, retry_after_seconds=7)
    monkeypatch.setattr(rule_catalogue, 'catalogue', lambda *_args, **_kwargs: updating)
    response = h.client('auditor').get('/api/rules/catalogue')
    assert response.status_code == 202
    assert response.headers['Retry-After'] == '7'
    assert response.json() == updating.model_dump(mode='json')


def test_draft_detail_revalidation_stops_before_session_projection(h):
    from sqlalchemy import event

    client = h.client('coordinator')
    w = h.workspace()
    path = '/api/workspaces/' + w['id']
    first = client.get(path)
    statements = []
    listener = lambda _connection, _cursor, statement, _parameters, _context, _executemany: statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', listener)
    try:
        repeat = client.get(path, headers={'if-none-match': first.headers['etag']})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', listener)
    assert repeat.status_code == 304 and repeat.content == b''
    assert not any('from shifts' in sql.lower() for sql in statements)


@pytest.mark.parametrize('path_factory', [
    lambda wid: '/api/workspaces',
    lambda wid: f'/api/workspaces/{wid}/fix-list?include_evidence=false',
    lambda wid: f'/api/workspaces/{wid}/parts?part=shifts&start=2026-10-04&end=2026-10-11',
    lambda wid: f'/api/jobs?workspace_id={wid}',
])
def test_department_read_revalidation_returns_before_projection(h, path_factory):
    """The other planner reads use the same cheap validator contract as detail."""
    client = h.client('coordinator')
    workspace = h.workspace()
    path = path_factory(workspace['id'])
    first = client.get(path)
    assert first.status_code == 200 and first.headers.get('etag')
    repeat = client.get(path, headers={'If-None-Match': first.headers['etag']})
    assert repeat.status_code == 304 and repeat.content == b''
