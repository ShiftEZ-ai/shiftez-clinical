"""Read-route authorization and payload boundaries on the registered application."""
from contextlib import closing
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from starlette.routing import Match, Mount, Route, WebSocketRoute

from apps.api import main, models as m
from apps.api.db import transaction
from apps.api.services.leave_types import seed as seed_leave_types
from packages.contracts import schemas as s
from .governance_support import identity
from .test_organisation_isolation import create_sibling_department
from .test_postgres_roles import runtime  # noqa: F401 (the production-shaped role harness)
from .test_staff_wishes import wish


START, END = '2026-10-04', '2026-10-11'
PERIOD = {'start': START, 'end': END}
MISSING_ID = '00000000-0000-4000-8000-000000000001'
MISSING_TOKEN = 'unused-synthetic-link-with-a-valid-length'
SHELL = b'<!doctype html><title>Synthetic shell</title>'
SCRIPT = b'/* synthetic script */'

QUERY_SAMPLES = {
    '/auth/oidc/departments': {'continuation': MISSING_TOKEN},
    '/api/time/resolve': {'local': START + 'T12:00'},
    **dict.fromkeys((
        '/api/calendar', '/api/schedule/days', '/api/schedule/sessions', '/api/home/rotas',
        '/api/date-sets/resolved', '/api/timeline', '/api/rotation-board', '/api/people/{person_id}/timeline',
        '/api/people/{person_id}/rotations', '/api/people/{person_id}/availability',
    ), PERIOD),
    **dict.fromkeys(('/api/events/occurrences', '/api/requests/overtime-eligibility', '/api/schedule/overlaps'),
                    {'starts_on': START, 'ends_on': END}),
    '/api/events': {'once': 'true'},
    '/api/email/microsoft/callback': {'state': MISSING_TOKEN},
    '/api/workspaces/{workspace_id}/parts': {**PERIOD, 'part': 'shifts'},
    '/api/table-views': {'table': 'people'},
    '/api/search/references': {'collection': 'people', 'kind': 'people', 'q': 'synthetic'},
    '/api/bulk/rows': {'query': json.dumps({'selection': {'collection': 'people'}, 'limit': 1})},
}

PUBLIC_SHELLS = dict.fromkeys(('/', '/n/{token}', '/invite/{token}', '/reset/{token}'), SHELL)
PUBLIC_MANIFESTS = {'/assets/manifest.webmanifest', '/api/pwa/manifest.webmanifest'}
PUBLIC_PNGS = {'/api/pwa/icons/{artifact_id}.png', '/api/pwa/apple-touch-icon.png'}


def refused(response, status, code, message=None, *, case):
    assert response.status_code == status, (case, response.status_code)
    value = response.json()
    assert set(value) == {'error', 'correlation_id'}, case
    assert set(value['error']) == {'code', 'message', 'details'}, case
    assert value['error']['code'] == code and value['error']['details'] is None, case
    if message is not None:
        assert value['error']['message'] == message, case


@pytest.mark.timeout(60)
def test_every_registered_get_has_a_public_contract_or_requires_sign_in(h, tmp_path, monkeypatch):
    # Keep the production mount and handlers in the sweep even when the test shard has no frontend build.
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'sample.js').write_bytes(SCRIPT)
    (tmp_path / 'index.html').write_bytes(SHELL)
    (tmp_path / 'sw.js').write_bytes(SCRIPT)
    monkeypatch.setattr(main, 'WEB', tmp_path)
    app = main.create_app(h.settings, engine=h.app.state.engine)

    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]['id']
    other = h.workspace('Synthetic comparison draft')['id']
    person = h.seed['people'][0]
    samples = {
        **dict.fromkeys(('batch_id', 'content_id', 'conversion_id', 'entry_id', 'event_id', 'exception_id',
                         'image_id', 'import_id', 'job_id', 'notification_id', 'occurrence_id', 'policy_id',
                         'publication_id', 'report_id', 'request_id', 'result_id', 'series_id', 'swap_id'), MISSING_ID),
        'catalogue_id': 'people_per_session', 'kind': 'services', 'object_id': h.seed['services'][0],
        'operation_key': MISSING_ID, 'person_id': person, 'shift_id': shift,
        'token': MISSING_TOKEN, 'workspace_id': workspace['id'],
    }
    queries = {**QUERY_SAMPLES, '/api/workspaces/{workspace_id}/compare': {'other_id': other},
               '/api/fairness/sessions': {'person_id': person}, '/api/policies/applies': {'person_id': person},
               '/api/benchmark': {'workspace': workspace['id']}}
    public_errors = {
        '/auth/start': ('OIDC_UNCONFIGURED', 'Institutional sign-in is not available'),
        '/auth/oidc/departments': ('OIDC_UNCONFIGURED', 'Institutional sign-in is not available'),
        '/api/account-links/{token}': ('LINK_INVALID', None),
        '/api/branding/images/{image_id}': ('NOT_FOUND', 'Branding image not found'),
        '/calendar/{token}.ics': ('NOT_FOUND', 'Calendar not found'),
    }
    contracts = {'/auth/config': s.AuthConfig, '/api/branding': s.BrandingPublic}
    mounts = {'/assets': '/assets/sample.js'}
    assert all(isinstance(route, (Mount, Route, WebSocketRoute)) for route in app.routes), 'Unclassified router'
    assert {route.path for route in app.routes if isinstance(route, Mount)} == mounts.keys()
    routes = [route for route in app.routes if isinstance(route, Mount)
              or (isinstance(route, Route) and (route.methods is None or 'GET' in route.methods))]
    registered = {route.path for route in routes}
    assert len(routes) == len(registered), 'Duplicate GET registrations can shadow a route'
    assert (queries.keys() | public_errors.keys() | contracts.keys() | mounts.keys() | PUBLIC_SHELLS.keys()
            | PUBLIC_PNGS | PUBLIC_MANIFESTS | {'/sw.js', '/health/live', '/health/ready', '/health/service',
                                              '/auth/callback'}) <= registered

    with TestClient(app, base_url=h.settings.public_origin, follow_redirects=False) as client:
        # The health contract can be checked with a warm application and without a running worker.
        for route in routes:
            template = route.path
            path = mounts[template] if isinstance(route, Mount) else template.format_map(samples)
            scope = {'type': 'http', 'method': 'GET', 'path': path, 'root_path': ''}
            assert next(r for r in routes if r.matches(scope)[0] is Match.FULL) is route, template
            response = client.get(path, params=queries.get(template))
            if template in public_errors:
                code, message = public_errors[template]
                refused(response, 404, code, message, case=template)
            elif template in contracts:
                assert response.status_code == 200 and set(response.json()) == set(contracts[template].model_fields), template
                contracts[template].model_validate(response.json())
            elif template in PUBLIC_MANIFESTS:
                assert response.status_code == 200 and response.headers['content-type'] == 'application/manifest+json'
                value = response.json()
                manifest = s.PwaManifest.model_validate(value)
                assert set(value) == set(s.PwaManifest.model_fields)
                assert manifest.id == manifest.start_url == manifest.scope == '/'
                for icon in value['icons']:
                    assert set(icon) == set(s.PwaIcon.model_fields)
                    assert icon['src'].startswith('/api/pwa/icons/') and icon['src'].endswith('.png')
                samples['artifact_id'] = manifest.icons[0].src.rsplit('/', 1)[1].removesuffix('.png')
            elif template in PUBLIC_PNGS:
                assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
                assert response.content.startswith(b'\x89PNG\r\n\x1a\n')
            elif template in PUBLIC_SHELLS:
                assert response.status_code == 200 and response.content == PUBLIC_SHELLS[template], template
            elif template in {'/assets', '/sw.js'}:
                assert response.status_code == 200 and response.content == SCRIPT, template
            elif template == '/health/live':
                assert response.status_code == 200 and response.json() == {'status': 'alive', 'version': main.__version__}
            elif template == '/health/ready':
                assert response.status_code == 200 and response.json() == {'status': 'ready'}
            elif template == '/health/service':
                assert response.status_code == 503 and response.json() == {'status': 'unavailable'}
            elif template == '/auth/callback':
                assert response.status_code == 303 and response.headers['location'] == '/?signin_error=expired'
                assert not response.content and 'rota_session' not in response.cookies
            else:
                refused(response, 401, 'AUTH_REQUIRED', 'Sign in to continue', case=template)


PLANNERS = {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead'}
TEAM_REQUESTS = {'admin', 'coordinator', 'approver'}
LEAVE_NOTE = 'Synthetic local leave note'
OTHER_NOTE = 'Synthetic sibling leave note'
WISH_REASON = 'Synthetic competing preference evidence'


def recorded_leave(h, role, person, date, note):
    return h.command(role, 'availability.record', {
        'person_id': person, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': date + 'T00:00:00+03:00', 'end': date + 'T18:00:00+03:00', 'private_reason': note,
    })['record']


def notice(h, role, label):
    return h.configure('department_content', {'name': label, 'description': label, 'starts_on': '2020-01-01',
        'audience': {'everyone': True}, 'notify': False}, role=role)['record']


@pytest.fixture(params=['h', pytest.param('runtime', marks=pytest.mark.postgres)])
def scope_data(request):
    h = request.getfixturevalue(request.param)
    person, peer = h.seed['people'][:2]
    identity(h, 'programme_lead', ['programme_lead'], person=person)
    member_id = identity(h, 'sweep_nonmember', ['staff'], person=peer)
    nonmember = h.client('sweep_nonmember')
    assert nonmember.get('/api/me').json()['identity_id'] == member_id

    sibling = create_sibling_department(h)
    identity(h, 'sweep_sibling_coordinator', ['coordinator'], department=sibling['id'])
    identity(h, 'sweep_sibling_staff', ['staff'], person=person, department=sibling['id'])
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == member_id)).revoked = True
        db.get(m.Department, h.seed['department_id']).leave_reason_display = 'details'
        db.get(m.Department, sibling['id']).leave_reason_display = 'details'
        seed_leave_types(db, sibling['id'])

    workspace = h.shift(h.workspace('Synthetic local rota'), people=[person])['workspace']
    publication_id = h.publish(workspace)['publication_id']
    other_workspace = h.command('sweep_sibling_coordinator', 'workspace.create', {
        'name': 'Synthetic sibling rota', 'starts_on': START, 'ends_on': END,
    })['workspace']
    draft = h.workspace('Synthetic job draft', start='2026-10-18', end='2026-10-25')
    draft = h.shift(draft, start='2026-10-18T08:00:00+03:00', end='2026-10-18T12:00:00+03:00')['workspace']
    job = h.command('coordinator', 'job.create', {'kind': 'generation', 'workspace_id': draft['id']},
                    revision=draft['revision'])['job']

    own_wish = wish(h, '2026-10-12')['wish']
    peer_wish = wish(h, '2026-10-13', role='coordinator', person=peer)['wish']
    other_wish = wish(h, '2026-10-12', role='sweep_sibling_staff')['wish']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.StaffWish, own_wish['id']).outcome_reasons = [{
            'day': '2026-10-12', 'kind': 'competing_wishes', 'message': WISH_REASON,
            'competing_wishes': [{'wish_id': peer_wish['id'], 'person_id': peer}],
        }]

    local_leave = recorded_leave(h, 'coordinator', person, '2026-10-06', LEAVE_NOTE)
    peer_leave = recorded_leave(h, 'coordinator', peer, '2026-10-07', 'Synthetic peer leave note')
    other_leave = recorded_leave(h, 'sweep_sibling_coordinator', person, '2026-10-06', OTHER_NOTE)
    local_notice = notice(h, 'coordinator', 'Synthetic local announcement')
    other_notice = notice(h, 'sweep_sibling_coordinator', 'Synthetic sibling announcement')
    return h, nonmember, sibling, workspace, other_workspace, job, publication_id, (
        own_wish, peer_wish, other_wish), (local_leave, peer_leave, other_leave), (local_notice, other_notice)


@pytest.mark.timeout(90)
def test_read_families_respect_role_and_department(scope_data):
    h, nonmember, sibling, workspace, other_workspace, job, publication_id, wishes, leaves, notices = scope_data
    own_wish, peer_wish, other_wish = (row['id'] for row in wishes)
    own_leave, peer_leave, other_leave = (row['id'] for row in leaves)
    local_notice, other_notice = notices
    person, peer = h.seed['people'][:2]
    wish_path = '/api/wishes'
    request_path = '/api/requests/all'
    leave_path = f'/api/people/{person}/availability'
    workspace_path = f'/api/workspaces/{workspace["id"]}'
    other_workspace_path = f'/api/workspaces/{other_workspace["id"]}'
    job_path = f'/api/jobs/{job["id"]}'
    publication_path = f'/api/publications/{publication_id}'

    # A live session with its only binding revoked must reach the no-membership boundary.
    protected = {wish_path: {}, request_path: {'id': own_leave}, leave_path: PERIOD, workspace_path: {},
                 job_path: {}, publication_path: {}, '/api/availability': {}, '/api/content': {},
                 '/api/events': {'once': 'true'}}
    for path, params in protected.items():
        refused(nonmember.get(path, params=params), 403, 'FORBIDDEN', 'No active access binding for this department',
                case=path)

    # Same person in both departments. The second identity proves the sibling records exist and are readable there.
    sibling_staff = 'sweep_sibling_staff'
    sibling_planner = 'sweep_sibling_coordinator'
    me = h.get(sibling_staff, '/api/me')
    assert me['department']['id'] == sibling['id'] and me['organisation']['id'] == h.seed['organisation_id']
    assert [item['id'] for item in h.get(sibling_staff, wish_path)['wishes']] == [other_wish]
    sibling_leave = h.get(sibling_planner, f'{request_path}?id={other_leave}')
    assert sibling_leave['item']['id'] == other_leave and sibling_leave['item']['private_reason'] == OTHER_NOTE
    assert h.get(sibling_planner, other_workspace_path)['workspace']['id'] == other_workspace['id']
    assert [item['id'] for item in h.get(sibling_staff, '/api/content')['items']] == [other_notice['id']]
    for user, path in ((sibling_staff, publication_path), (sibling_planner, job_path), (sibling_planner, workspace_path)):
        refused(h.client(user).get(path), 404, 'NOT_FOUND', case=(user, path))

    feed_path = h.command('staff', 'calendar_feed.create', {})['path']
    with closing(TestClient(h.app, base_url=h.settings.public_origin)) as anonymous:
        feed = anonymous.get(feed_path)
    assert feed.status_code == 200 and feed.headers['content-type'].startswith('text/calendar')
    assert feed.headers['cache-control'] == 'private, no-cache'
    assert 'BEGIN:VEVENT' in feed.text and OTHER_NOTE not in feed.text and LEAVE_NOTE not in feed.text

    for role in s.ROLE_NAMES:
        client = h.client(role)
        me = h.get(role, '/api/me')
        assert role in me['roles'] and me['department']['id'] == h.seed['department_id']
        case = (role, wish_path)
        response = client.get(wish_path)
        if role not in PLANNERS | {'staff'}:
            refused(response, 403, 'FORBIDDEN', case=case)
        else:
            assert response.status_code == 200, case
            rows = {row['id']: row for row in response.json()['wishes']}
            assert set(rows) == ({own_wish} if role == 'staff' else {own_wish, peer_wish}), case
            [reason] = rows[own_wish]['outcome_reasons']
            if role == 'staff':
                assert reason == {'day': '2026-10-12', 'kind': 'competing_wishes',
                                  'message': "A colleague's higher-priority preference was honoured."}
                assert peer_wish not in response.text and peer not in response.text and WISH_REASON not in response.text
                refused(client.get(wish_path, params={'person_id': peer}), 403, 'FORBIDDEN', case=case)
            else:
                assert reason['competing_wishes'] == [{'wish_id': peer_wish, 'person_id': peer}]
                assert reason['message'] == WISH_REASON
            assert other_wish not in response.text, case

        response = client.get(request_path, params={'id': own_leave})
        case = (role, request_path)
        if role in TEAM_REQUESTS | {'staff'}:
            assert response.status_code == 200, case
            item = response.json()['item']
            assert item['id'] == own_leave and item['person_id'] == person, case
            if role == 'staff':
                assert 'private_reason' not in item and LEAVE_NOTE not in response.text
                refused(client.get(request_path, params={'id': peer_leave}), 404, 'NOT_FOUND', case=case)
            else:
                assert item['private_reason'] == LEAVE_NOTE
                assert h.get(role, f'{request_path}?id={peer_leave}')['item']['id'] == peer_leave
            refused(client.get(request_path, params={'id': other_leave}), 404, 'NOT_FOUND', case=case)
            wish_item = h.get(role, f'{request_path}?id={own_wish}')['item']
            assert wish_item['id'] == own_wish and wish_item['outcome_reasons'] == [reason], case
            if role == 'staff':
                refused(client.get(request_path, params={'id': peer_wish}), 404, 'NOT_FOUND', case=case)
        else:
            status, code = (404, 'NOT_FOUND') if role in {'auditor', 'programme_lead'} else (403, 'FORBIDDEN')
            refused(response, status, code, case=case)
            refused(client.get(request_path, params={'id': other_leave}), status, code, case=case)

        response = client.get(leave_path, params=PERIOD)
        case = (role, leave_path)
        if role in TEAM_REQUESTS | {'staff'}:
            assert response.status_code == 200, case
            [item] = response.json()['availability']
            assert item['id'] == own_leave and item['person_id'] == person and item['leave_type_name'] == 'Annual', case
            assert 'private_reason' not in item and other_leave not in response.text and OTHER_NOTE not in response.text
            if role == 'staff':
                assert 'note' not in item and LEAVE_NOTE not in response.text
            else:
                assert item['note'] == LEAVE_NOTE
        else:
            refused(response, 403, 'FORBIDDEN', case=case)

        response = client.get('/api/availability')
        case = (role, '/api/availability')
        if role in TEAM_REQUESTS | {'staff'}:
            assert response.status_code == 200, case
            rows = response.json()['availability']
            assert {row['id'] for row in rows} == ({own_leave} if role == 'staff' else {own_leave, peer_leave}), case
            assert all('private_reason' not in row for row in rows), case
            assert LEAVE_NOTE not in response.text and OTHER_NOTE not in response.text, case
        else:
            refused(response, 403, 'FORBIDDEN', case=case)

        for path, allowed, section, record_id in (
            (workspace_path, PLANNERS, 'workspace', workspace['id']),
            (job_path, {'admin', 'coordinator'}, 'job', job['id']),
            (publication_path, {'admin', 'coordinator', 'approver', 'auditor', 'staff'}, 'publication', publication_id),
        ):
            response = client.get(path)
            if role in allowed:
                assert response.status_code == 200 and response.json()[section]['id'] == record_id, (role, path)
            else:
                refused(response, 403, 'FORBIDDEN', case=(role, path))
        status, code = (404, 'NOT_FOUND') if role in PLANNERS else (403, 'FORBIDDEN')
        refused(client.get(other_workspace_path), status, code, case=(role, other_workspace_path))

        content = client.get('/api/content')
        assert content.status_code == 200 and [row['id'] for row in content.json()['items']] == [local_notice['id']], role
        assert other_notice['description'] not in content.text

        response = client.get('/api/events', params={'once': 'true'})
        assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream'), role
        [payload] = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert payload['department_id'] == h.seed['department_id'] and role in payload['roles']
        assert sibling['id'] not in response.text and OTHER_NOTE not in response.text
