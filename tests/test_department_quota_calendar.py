"""Department quota calendars override default weekends and freeze on publication."""
import copy
from datetime import date
import uuid

from alembic import command
import pytest
from sqlalchemy import MetaData, Table, select, text

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.snapshots import domain_snapshot
from scripts.manage import alembic_config, migrate
from .migration_support import historical_seed
from .test_resources_calendars import calendar, update


def settings(h, *, role='admin', status=200, **changes):
    me = h.get('admin', '/api/me')
    organisation, department = me['organisation'], me['department']
    return h.command(role, 'department.settings', {
        'department_name': department['name'], 'department_revision': department['revision'],
        'organisation_name': organisation['name'], 'timezone': organisation['timezone'],
        'week_start': organisation['week_start'], 'reason': 'Synthetic department quota calendar selection',
        **changes}, department['id'], organisation['revision'], status=status)


def test_demo_explicitly_selects_its_configured_working_days_without_inference(h):
    department = h.get('admin', '/api/me')['department']
    selected = department['operating_calendar_id']
    calendars = h.get('admin', '/api/catalogues')['operating_calendars']
    chosen = next(row for row in calendars if row['id'] == selected)
    assert chosen['active'] and chosen['department_id'] == department['id']
    assert {row['weekday'] for row in chosen['definition']['week']} == set(range(7)) - set(department['weekend_days'])
    assert all(row['windows'] == [{'start_time': '00:00', 'end_time': '24:00'}]
               for row in chosen['definition']['week'])
    calendar(h)
    workspace = h.workspace()
    assert h.snapshot(workspace)['department']['operating_calendar_id'] == selected
    settings(h, operating_calendar_id=None)
    snapshot = h.snapshot(workspace)
    assert snapshot['operating_calendars'] and snapshot['department']['operating_calendar_id'] is None


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_calendar_selection_is_audited_invalidates_inputs_preserves_omissions_and_undoes(request, backend):
    h = request.getfixturevalue(backend)
    chosen = calendar(h)
    workspace = h.workspace()
    before = h.get('admin', '/api/me')
    original = before['department']['operating_calendar_id']
    old_fingerprint = h.detail(workspace)['current_fingerprint']
    saved = settings(h, operating_calendar_id=chosen['id'])
    assert saved['previous']['operating_calendar_id'] == original
    assert saved['department']['operating_calendar_id'] == chosen['id']
    assert saved['department']['revision'] == before['department']['revision'] + 1
    assert saved['organisation']['input_revision'] == before['organisation']['input_revision'] + 1
    assert h.detail(workspace)['current_fingerprint'] != old_fingerprint
    assert h.snapshot(workspace)['department']['operating_calendar_id'] == chosen['id']
    omitted = settings(h)
    assert omitted['department'] == saved['department']
    assert omitted['organisation']['input_revision'] == saved['organisation']['input_revision']
    restored = settings(h, **saved['previous'])
    assert restored['department']['operating_calendar_id'] == original
    assert restored['organisation']['input_revision'] == saved['organisation']['input_revision'] + 1
    cleared = settings(h, operating_calendar_id=None)
    assert cleared['department']['operating_calendar_id'] is None
    assert cleared['previous']['operating_calendar_id'] == original
    assert cleared['organisation']['input_revision'] == restored['organisation']['input_revision'] + 1
    with transaction(h.app.state.engine) as db:
        histories = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'department_settings',
            m.DefinitionHistory.object_id == before['department']['id']).order_by(m.DefinitionHistory.object_revision)))
        assert [row.snapshot['operating_calendar_id'] for row in histories] == [chosen['id'], original, None]
        audits = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'department.settings')))
        assert len(audits) >= 3


@pytest.mark.parametrize('invalid', ['missing', 'foreign', 'inactive'])
def test_changed_calendar_selection_requires_an_active_same_department_calendar(h, invalid):
    if invalid == 'inactive':
        identity = calendar(h, active=False)['id']
    elif invalid == 'foreign':
        with transaction(h.app.state.engine, write=True) as db:
            foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Separate synthetic department')
            db.add(foreign); db.flush()
            row = m.OperatingCalendar(department_id=foreign.id, name='Hidden calendar', starts_on=date(2020, 1, 1),
                definition={'timezone': 'UTC', 'week': []})
            db.add(row); db.flush()
            identity = row.id
    else:
        identity = str(uuid.uuid4())
    before = h.get('admin', '/api/me')
    rejected = settings(h, operating_calendar_id=identity, status=409 if invalid == 'inactive' else 404)
    assert rejected['error']['code'] == ('CALENDAR_INACTIVE' if invalid == 'inactive' else 'NOT_FOUND')
    after = h.get('admin', '/api/me')
    assert after['department'] == before['department']
    assert after['organisation']['input_revision'] == before['organisation']['input_revision']


def test_calendar_selection_keeps_administrator_permission(h):
    chosen = calendar(h)
    before = h.get('admin', '/api/me')['department']
    settings(h, role='coordinator', operating_calendar_id=chosen['id'], status=403)
    assert h.get('admin', '/api/me')['department'] == before


def test_publication_freezes_selected_calendar_and_definition(h):
    chosen, replacement = calendar(h), calendar(h)
    settings(h, operating_calendar_id=chosen['id'])
    workspace = h.shift(h.workspace())['workspace']
    published = h.publish(workspace)
    with transaction(h.app.state.engine) as db:
        issued = copy.deepcopy(db.get(m.Publication, published['publication_id']).snapshot)
    assert issued['department'] == {'id': h.seed['department_id'], 'operating_calendar_id': chosen['id'],
                                    'weekend_days': h.get('admin', '/api/me')['department']['weekend_days']}
    assert issued['operating_calendars'][chosen['id']]['definition'] == chosen['definition']
    update(h, 'operating_calendars', chosen, definition={'timezone': 'Asia/Riyadh', 'week': []})
    settings(h, operating_calendar_id=replacement['id'])
    frozen = h.snapshot(workspace)
    assert frozen['department']['operating_calendar_id'] == chosen['id']
    assert frozen['operating_calendars'][chosen['id']] == issued['operating_calendars'][chosen['id']]
    with transaction(h.app.state.engine) as db:
        current = domain_snapshot(db, db.get(m.Workspace, workspace['id']), current_scheduling=True)
        assert current['department']['operating_calendar_id'] == replacement['id']
        assert current['operating_calendars'][chosen['id']]['definition']['week'] == []
        assert db.get(m.Publication, published['publication_id']).snapshot == issued


def test_publication_freezes_default_weekends_without_a_selected_calendar(h):
    settings(h, operating_calendar_id=None, weekend_days=[4, 5])
    workspace = h.shift(h.workspace())['workspace']
    published = h.publish(workspace)
    with transaction(h.app.state.engine) as db:
        issued = copy.deepcopy(db.get(m.Publication, published['publication_id']).snapshot)
    assert issued['department'] == {'id': h.seed['department_id'], 'operating_calendar_id': None,
                                    'weekend_days': [4, 5]}
    settings(h, weekend_days=[5, 6])
    frozen = h.snapshot(workspace)
    assert frozen['department']['operating_calendar_id'] is None
    assert frozen['department']['weekend_days'] == [4, 5]
    with transaction(h.app.state.engine) as db:
        current = domain_snapshot(db, db.get(m.Workspace, workspace['id']), current_scheduling=True)
        assert current['department']['weekend_days'] == [5, 6]
        assert db.get(m.Publication, published['publication_id']).snapshot == issued


@pytest.mark.parametrize('selected_calendar', [True, False])
def test_weekend_changes_invalidate_inputs_only_when_the_default_is_used_and_undo(h, selected_calendar):
    if not selected_calendar:
        settings(h, operating_calendar_id=None)
    workspace = h.workspace()
    before = h.get('admin', '/api/me')
    old_fingerprint = h.detail(workspace)['current_fingerprint']
    saved = settings(h, weekend_days=[5, 6])
    assert saved['previous']['weekend_days'] == before['department']['weekend_days']
    assert saved['department']['weekend_days'] == [5, 6]
    assert saved['organisation']['input_revision'] == before['organisation']['input_revision'] + (not selected_calendar)
    assert (h.detail(workspace)['current_fingerprint'] != old_fingerprint) == (not selected_calendar)
    restored = settings(h, **saved['previous'])
    assert restored['department']['weekend_days'] == before['department']['weekend_days']
    assert restored['organisation']['input_revision'] == saved['organisation']['input_revision'] + (not selected_calendar)
    assert h.detail(workspace)['current_fingerprint'] == old_fingerprint


@pytest.mark.parametrize('selected_calendar', [True, False])
def test_weekend_changes_preserve_only_approvals_with_an_explicit_working_calendar(h, selected_calendar):
    if not selected_calendar:
        settings(h, operating_calendar_id=None)
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.lifecycle(h.lifecycle(workspace, 'submit')['workspace'], 'approve', 'approver')['workspace']
    settings(h, weekend_days=[5, 6])
    if selected_calendar:
        assert h.lifecycle(workspace, 'publish')['workspace']['status'] == 'published'
    else:
        assert h.lifecycle(workspace, 'publish', status=409)['error']['code'] == 'STALE_APPROVAL'


def test_migration_preserves_existing_departments_without_choosing_a_calendar(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'before-calendar.db')
    command.upgrade(alembic_config(url), '0098_bulk_privacy')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            connection = db.connection()
            calendars = Table('operating_calendars', MetaData(), autoload_with=connection)
            connection.execute(calendars.insert(), {'id': str(uuid.uuid4()), 'revision': 1,
                'created_at': m.now(), 'department_id': seed['department_id'], 'name': 'Existing unselected calendar',
                'description': '', 'starts_on': date(2020, 1, 1), 'active': True,
                'definition': {'timezone': 'UTC', 'week': []}})
            before = dict(connection.execute(text('SELECT * FROM departments WHERE id=:id'),
                                             {'id': seed['department_id']}).mappings().one())
        migrate(url)
        with engine.connect() as db:
            after = dict(db.execute(text('SELECT * FROM departments WHERE id=:id'),
                                    {'id': seed['department_id']}).mappings().one())
            assert after.pop('operating_calendar_id') is None
            assert after.pop('catalogue_revision') == 1
            assert after.pop('policy_retire_approvals_required') == 1
            assert after.pop('ai_enabled') == 0
            assert after.pop('score_check_enabled') == 0
            assert after == {**before, 'new_members_take_duties': 0}
            assert db.execute(text('SELECT COUNT(*) FROM operating_calendars')).scalar_one() == 1
    finally:
        engine.dispose()
