"""Application presentation remains independent of identity and clinical generation inputs."""
import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
import pytest
from sqlalchemy import select
from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.services import branding
from packages.contracts import schemas as s
from packages.web_assets import asset_url


def test_application_permission_streams_and_stops_at_the_first_unowned_department(monkeypatch):
    """Department coverage no longer decides access. Only live installation grants are streamed."""
    monkeypatch.setattr(auth, 'now', lambda: datetime(2026, 10, 5, 23, tzinfo=timezone.utc))

    class Rows:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def __iter__(self):
            yield SimpleNamespace(timezone='UTC', starts_on=date(2026, 10, 6), ends_on=None, revoked=False)
            yield SimpleNamespace(timezone='Etc/GMT-2', starts_on=date(2026, 10, 6), ends_on=None, revoked=False)
            pytest.fail('A live installation grant should stop the bounded read')

    rows = Rows()

    class Database:
        def execute(self, statement):
            assert statement.get_execution_options().get('stream_results')
            assert s.INSTALLATION_ADMIN_ROLE in statement.compile().params.values()
            return rows

    assert auth.has_installation_admin(Database(), str(uuid.uuid4()))
    assert rows.closed


def names(**changes):
    return {'app_name': 'Department rota', 'institution_name': 'Test institution',
            'reason': 'Choose application branding', **changes}


def raster(fmt='PNG', metadata=False):
    output = io.BytesIO()
    with Image.new('RGBA' if fmt != 'JPEG' else 'RGB', (4, 4), (20, 50, 70, 120) if fmt != 'JPEG' else (20, 50, 70)) as image:
        info = PngImagePlugin.PngInfo()
        info.add_text('private', 'Not part of the public logo')
        image.save(output, format=fmt, **({'pnginfo': info} if metadata else {}))
    return output.getvalue()


def upload(h, content=None, media_type='image/png', role='admin', status=200):
    response = h.client(role).post('/api/admin/branding/images', content=raster() if content is None else content,
                                   headers={'content-type': media_type})
    assert response.status_code == status, response.text
    return response.json()


@pytest.mark.usefixtures('branding_editor')
def test_public_default_and_saved_projection_are_minimal_and_independent_of_departments(h):
    with TestClient(h.app, base_url=h.settings.public_origin) as public:
        first = public.get('/api/branding')
        assert first.status_code == 200
        assert first.json() == {**public_defaults(), 'revision': 1}
        assert public.get('/api/admin/branding').status_code == 401
        assert public.get('/api/admin/branding/history').status_code == 401
        saved = h.command('admin', 'branding.update', names(), revision=1)
        assert saved['branding']['revision'] == 2 and saved['can_undo']
        assert public.get('/api/branding').json() == saved['branding']
        assert set(saved['branding']) == set(s.BRANDING_DEFAULTS) | {'revision'}
    me = h.get('admin', '/api/me')
    assert me['department']['name'] != 'Department rota'
    assert me['organisation']['name'] != 'Test institution'
    denied_roles = set(h.seed['identities']) - {'admin', *s.SYSTEM_ROLES}
    for role in (role for role in s.ROLE_NAMES if role in denied_roles):
        h.get(role, '/api/admin/branding', status=403)
        h.command(role, 'branding.update', names(), revision=2, status=403)
        upload(h, role=role, status=403)


@pytest.mark.usefixtures('branding_editor')
def test_branding_is_revision_guarded_audited_and_undoable_without_rewriting_history(h):
    h.command('admin', 'branding.update', names(), status=428)
    first = h.command('admin', 'branding.update', names(), revision=1)
    h.command('admin', 'branding.update', names(app_name='Stale change'), revision=1, status=409)
    h.command('admin', 'branding.update', names(), target=str(uuid.uuid4()), revision=2, status=422)
    before = h.get('admin', '/api/admin/branding/history')['versions']
    assert [v['object_revision'] for v in before] == [1, 2]
    assert before[-1]['reason'] == names()['reason']
    assert h.get('admin', '/api/admin/branding/history?limit=1&offset=1')['versions'] == before[1:]
    undone = h.command('admin', 'branding.undo', {'reason': 'Undo application branding'}, revision=2)
    assert undone['branding'] == {**public_defaults(), 'revision': 3}
    assert h.get('admin', '/api/admin/branding/history')['versions'][:2] == before
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.like('branding.%'))))
        assert [e.action for e in events] == ['branding.update', 'branding.undo']
        assert all(e.reason and e.actor_id for e in events)
        assert not db.scalar(select(m.AuthConfiguration))
    assert first['settings'] == names_without_reason()


def names_without_reason():
    return {**s.BrandingSettings().model_dump(), **{k: v for k, v in names().items() if k != 'reason'}}


@pytest.mark.usefixtures('branding_editor')
def test_two_simultaneous_updates_have_one_winner(h):
    client = h.client('admin')

    def save(name):
        return client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
            json={'kind': 'branding.update', 'expected_revision': 1, 'payload': names(app_name=name)}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ['First edit', 'Other edit'])) == [200, 409]
    assert len(h.get('admin', '/api/admin/branding/history')['versions']) == 2


@pytest.mark.parametrize('grant', ['missing', 'revoked', 'expired', 'future', 'current'])
@pytest.mark.usefixtures('branding_editor')
def test_all_application_departments_require_current_admin_even_across_organisations(h, grant):
    client = h.client('admin')
    actor_id = h.get('admin', '/api/me')['identity_id']
    original = h.command('admin', 'branding.update', names(), revision=1, key='branding-scope-replay')
    with transaction(h.app.state.engine, write=True) as db:
        initial = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == actor_id,
                            m.RoleBinding.role == s.INSTALLATION_ADMIN_ROLE))
        initial.revoked = True
        org = m.Organisation(name='Another organisation', timezone='UTC')
        db.add(org)
        db.flush()
        dept = m.Department(organisation_id=org.id, name='Another department')
        db.add(dept)
        db.flush()
        if grant != 'missing':
            today = m.now().date()
            db.add(m.RoleBinding(department_id=dept.id, identity_id=actor_id, role=s.INSTALLATION_ADMIN_ROLE,
                starts_on=today + timedelta(days=1) if grant == 'future' else today - timedelta(days=2),
                ends_on=today if grant == 'expired' else None, revoked=grant == 'revoked'))
    allowed = grant == 'current'
    assert h.get('admin', '/api/admin/branding')['can_edit'] is allowed
    h.get('admin', '/api/admin/branding/history', status=200 if allowed else 403)
    h.command('admin', 'branding.update', names(), revision=1, key='branding-scope-replay', status=200 if allowed else 403)
    h.command('admin', 'branding.update', names(app_name='Second name'), revision=2, status=200 if allowed else 403)
    upload(h, status=200 if allowed else 403)
    if not allowed:
        assert h.get('staff', '/api/branding') == original['branding']
        assert client.post('/api/commands', json={'kind': 'branding.undo', 'expected_revision': 2,
            'payload': {'reason': 'Undo from a scoped administrator'}},
            headers={'idempotency-key': str(uuid.uuid4())}).status_code == 403


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.usefixtures('branding_editor')
def test_operator_edits_branding_after_department_creation(h):
    from .test_department_create import name_test_administrators, payload, token

    name_test_administrators(h)
    original = h.get('staff', '/api/branding')
    spec = payload(h, first_admin_identity_id=h.seed['identities']['coordinator'])
    assert h.get('admin', '/api/admin/branding')['can_edit']
    h.command('operator', 'department.create', spec, expected_fingerprint=token(h, spec))
    assert h.get('admin', '/api/admin/branding')['can_edit']

    status = h.get('operator', '/api/admin/branding')
    assert status['can_edit'] and not status['can_undo']
    image = upload(h, role='operator')
    change = names(emblem_image_id=image['image_id'])
    revision = status['branding']['revision']
    saved = h.command('operator', 'branding.update', change, revision=revision, key='operator-branding')
    assert saved['can_edit'] and saved['can_undo']
    assert saved['branding']['emblem_url'] == image['url']
    replayed = h.command('operator', 'branding.update', change, revision=revision, key='operator-branding')
    assert replayed == saved
    assert h.get('admin', '/api/admin/branding')['branding'] == saved['branding']
    assert h.command('operator', 'branding.update', change, revision=revision, status=409)['error']['code'] == 'STALE_REVISION'
    assert h.get('operator', '/api/admin/branding/history')['versions'][-1]['actor_id'] == h.seed['identities']['operator']
    undone = h.command('operator', 'branding.undo', {'reason': 'Restore shared branding'}, revision=revision + 1)
    assert undone['branding'] == {**original, 'revision': revision + 2}

    image = upload(h)
    saved = h.command('admin', 'branding.update', names(emblem_image_id=image['image_id']), revision=revision + 2)
    assert saved['branding']['emblem_url'] == image['url']
    original_name = h.command('admin', 'branding.undo', {'reason': 'Restore shared branding'}, revision=revision + 3)
    assert original_name['branding'] == {**original, 'revision': revision + 4}


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.usefixtures('branding_editor')
def test_installation_admin_grants_are_explicit_audited_and_keep_a_live_holder(h):
    role = s.INSTALLATION_ADMIN_ROLE
    reason = {'reason': 'Transfer installation administration'}
    admin_id = h.seed['identities']['admin']
    coordinator_id = h.seed['identities']['coordinator']
    with transaction(h.app.state.engine) as db:
        first = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == admin_id, m.RoleBinding.role == role))
        first_id, first_revision = first.id, first.revision
    day = '2020-01-01'
    h.command('admin', 'role.grant', {'identity_id': coordinator_id, 'role': 'admin', 'starts_on': day, **reason})
    first_revoke = h.command('operator', 'role.revoke', reason, first_id, first_revision, status=409)
    assert first_revoke['error']['code'] == 'LAST_INSTALLATION_ADMIN'
    grant = {'identity_id': coordinator_id, 'role': role, 'starts_on': day, **reason}
    h.command('coordinator', 'role.grant', grant, status=403)
    h.command('coordinator', 'role.revoke', reason, first_id, first_revision, status=403)

    h.command('operator', 'role.grant', {**grant, 'identity_id': h.seed['identities']['staff'],
        'ends_on': (date.today() + timedelta(days=30)).isoformat()})
    assert h.get('staff', '/api/admin/branding')['can_edit']
    assert h.command('operator', 'role.revoke', reason, first_id, first_revision, status=409)[
        'error']['code'] == 'LAST_INSTALLATION_ADMIN'

    second = h.command('operator', 'role.grant', grant)['record']
    assert h.get('coordinator', '/api/admin/branding')['can_edit']
    h.command('coordinator', 'role.revoke', reason, first_id, first_revision)
    assert not h.get('admin', '/api/admin/branding')['can_edit']
    denied = h.command('coordinator', 'role.revoke', reason, second['id'], second['revision'], status=409)
    assert denied['error']['code'] == 'LAST_INSTALLATION_ADMIN'

    third = h.command('coordinator', 'role.grant', {**grant, 'identity_id': admin_id})['record']
    h.command('operator', 'role.revoke', reason, second['id'], second['revision'])
    assert h.get('admin', '/api/admin/branding')['can_edit']
    assert not h.get('coordinator', '/api/admin/branding')['can_edit']
    with transaction(h.app.state.engine) as db:
        events = db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.action.in_(('role.grant', 'role.revoke')),
            m.AuditEvent.target_id.in_([first_id, second['id'], third['id']]))).all()
        assert {(e.action, e.target_id, e.actor_id) for e in events} == {
            ('role.grant', second['id'], h.seed['identities']['operator']),
            ('role.revoke', first_id, coordinator_id),
            ('role.grant', third['id'], coordinator_id),
            ('role.revoke', second['id'], h.seed['identities']['operator']),
        }
        assert {event.reason for event in events} == {reason['reason']}


def test_disabling_the_last_installation_admin_is_refused(h):
    identity_id = h.seed['identities']['staff']
    reason = {'reason': 'Transfer installation administration'}
    h.command('operator', 'role.grant', {'identity_id': identity_id, 'role': s.INSTALLATION_ADMIN_ROLE,
        'starts_on': '2020-01-01', **reason})

    refused = h.command('admin', 'identity.disable', reason, identity_id, 1, status=409)

    assert refused['error']['code'] == 'LAST_INSTALLATION_ADMIN'
    assert h.get('staff', '/api/admin/branding')['can_edit']


@pytest.mark.usefixtures('branding_editor')
def test_pending_managed_account_has_no_installation_authority_in_its_department(h):
    admin_id = h.seed['identities']['admin']
    assert h.get('admin', '/api/admin/branding')['can_edit']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Account(identity_id=admin_id, username='pending.setup', email='pending.setup@example.test'))
    assert not h.get('admin', '/api/admin/branding')['can_edit']


def test_concurrent_revoke_and_disable_preserve_installation_admin(pg):
    h = pg
    role = s.INSTALLATION_ADMIN_ROLE
    reason = {'reason': 'Transfer installation administration'}
    staff, coordinator = (h.seed['identities'][name] for name in ('staff', 'coordinator'))
    grant = h.command('operator', 'role.grant', {'identity_id': staff, 'role': role,
        'starts_on': '2020-01-01', **reason})['record']
    h.command('operator', 'role.grant', {'identity_id': coordinator, 'role': role,
        'starts_on': '2020-01-01', **reason})
    h.client('operator')
    h.client('admin')
    ready = Barrier(2)

    def change(actor, kind, target, revision):
        ready.wait(timeout=10)
        return h.command(actor, kind, reason, target, revision, status=(200, 409))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(change, 'operator', 'role.revoke', grant['id'], grant['revision']),
            pool.submit(change, 'admin', 'identity.disable', coordinator, 1),
        ]
        failures = [error['code'] for result in results if (error := result.result().get('error'))]
    assert failures == ['LAST_INSTALLATION_ADMIN']
    with transaction(h.app.state.engine) as db:
        assert auth.has_installation_admin(db)


@pytest.mark.parametrize('fmt,media_type', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')])
@pytest.mark.usefixtures('branding_editor')
def test_raster_uploads_strip_metadata_keep_local_refs_and_preserve_history_assets(h, fmt, media_type):
    image = upload(h, raster(fmt, metadata=fmt == 'PNG'), media_type)
    assert upload(h, raster(fmt, metadata=fmt == 'PNG'), media_type) == image
    assert h.client('staff').get(image['url']).status_code == 404  # staging does not publish
    saved = h.command('admin', 'branding.update', names(emblem_image_id=image['image_id'],
        wordmark_image_id=image['image_id'], emblem_alt='Department emblem'), revision=1)
    assert saved['branding']['emblem_url'] == saved['branding']['wordmark_url'] == image['url']
    response = h.client('staff').get(image['url'])
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    with Image.open(io.BytesIO(response.content)) as clean:
        assert clean.size == (4, 4) and not clean.info and not clean.getexif()
        if fmt == 'PNG':
            assert clean.getpixel((0, 0))[3] == 120
    kept = h.command('admin', 'branding.update', names(app_name='Renamed'), revision=2)
    assert kept['settings']['emblem_image_id'] == image['image_id']
    assert kept['settings']['emblem_alt'] == 'Department emblem'
    reset = h.command('admin', 'branding.update', names(emblem_image_id=None), revision=3)
    assert reset['branding']['emblem_url'] == public_defaults()['emblem_url']
    restored = h.command('admin', 'branding.undo', {'reason': 'Restore previous emblem'}, revision=4)
    assert restored['branding']['emblem_url'] == image['url']
    assert h.client('staff').get(image['url']).status_code == 200
    histories = h.get('admin', '/api/admin/branding/history')['versions']
    assert all('content' not in v['snapshot'] for v in histories)
    assert 'image_id' not in h.get('staff', '/api/branding')


@pytest.mark.usefixtures('branding_editor')
def test_upload_and_update_boundaries_reject_unsafe_or_missing_assets(h, monkeypatch):
    upload(h, b'<svg onload="alert(1)"/>', 'image/svg+xml', status=415)
    upload(h, b'<svg onload="alert(1)"/>', status=422)
    upload(h, b'not an image', status=422)
    upload(h, raster(), 'image/jpeg', status=422)
    upload(h, raster()[:40], status=422)
    output = io.BytesIO()
    with Image.new('RGBA', (4, 4), 'red') as first, Image.new('RGBA', (4, 4), 'blue') as second:
        first.save(output, format='PNG', save_all=True, append_images=[second])
    upload(h, output.getvalue(), status=422)
    h.command('admin', 'branding.update', names(emblem_image_id=str(uuid.uuid4())), revision=1, status=404)
    h.command('admin', 'branding.update', names(wordmark_image_id='https://example.test/logo.png'), revision=1, status=422)
    h.command('admin', 'branding.update', names(app_name=' '), revision=1, status=422)
    client = h.client('admin')
    assert client.post('/api/admin/branding/images', content=raster(),
        headers={'content-type': 'image/png', 'x-csrf-token': 'invalid'}).status_code == 403
    assert client.post('/api/admin/branding/images', content=b'x' * (h.settings.max_body_bytes + 1),
        headers={'content-type': 'image/png'}).status_code == 413
    monkeypatch.setattr(branding, 'MAX_IMAGE_PIXELS', 15)
    upload(h, status=413)
    monkeypatch.setattr(branding, 'MAX_IMAGE_PIXELS', 16_000_000)
    monkeypatch.setattr(branding, 'MAX_FILE_BYTES', 2)
    upload(h, status=413)


def public_defaults():
    return {**s.BRANDING_DEFAULTS, **{key + '_url': asset_url(s.BRANDING_DEFAULTS[key + '_url'])
            for key in ('emblem', 'wordmark')}}
