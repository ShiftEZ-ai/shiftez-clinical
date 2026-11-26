"""Configurable leave types use the existing catalogue, command, audit and undo paths."""
import uuid
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import make_url

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.contracts.schemas import PERMISSIONS, ROLE_NAMES
from packages.domain.leave_types import matching_type
from packages.domain.validation import evaluate
from scripts.manage import alembic_config, migrate
from .governance_support import identity
from .test_leave_disclosure import setting, leave_of


def choices(h):
    return h.get('staff', '/api/leave-types')['leave_types']


def selected(h, code='ANNUAL'):
    return next(t for t in choices(h) if t['short_code'] == code)


def value(row, **changes):
    from packages.contracts.schemas import LeaveTypeInput
    return {**{k: row[k] for k in LeaveTypeInput.model_fields}, **changes}


def absence(h, typ=None, **changes):
    return h.command('coordinator', 'availability.record', {
        'person_id': h.seed['people'][0], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'kind': 'recorded_leave',
        'private_reason': 'Synthetic private leave note', 'leave_type_id': (typ or selected(h))['id'], **changes})['record']


def request_leave(h, typ=None, role='staff', status=200, **changes):
    return h.command(role, 'request.create', {
        'person_id': h.seed['people'][0], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'private_reason': 'Synthetic private request note',
        'leave_type_id': (typ or selected(h))['id'], **changes}, status=status)


@pytest.mark.parametrize('label,code', [('Annual leave', 'ANNUAL'), (' sick ', 'SICK'), ('Conference', 'STUDY'),
                                      ('EMERG', 'EMERG'), ('Annual / Sick', None), ('', None), ('unrecognized', None)])
def test_domain_mapping_is_exact_and_conservative(label, code):
    from packages.domain.leave_types import DEFAULT_LEAVE_TYPES
    types = [dict(name=n, short_code=c) for n, c, _ in DEFAULT_LEAVE_TYPES]
    matched = matching_type(label, types)
    assert (matched['short_code'] if matched else None) == code
    assert matching_type('Sick', types + [dict(name='Sick', short_code='SICK2')]) is None


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_catalogue_permissions_for_every_role(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    assert [t['name'] for t in h.get(role, '/api/leave-types')['leave_types']] == [
        'Annual', 'Sick', 'Study/Conference', 'Emergency', 'Other']
    payload = {'name': 'Synthetic custom type', 'short_code': 'CUSTOM', 'colour': 'violet', 'sort_order': 20}
    saved = h.configure('leave_types', payload, role=role, status=200 if role == 'admin' else 403)
    if role == 'admin':
        row = saved['record']
        h.configure('leave_types', value(row, active=False), row['id'], row['revision'])
    else:
        row = selected(h)
        h.configure('leave_types', value(row, active=False), row['id'], row['revision'], role=role, status=403)
    h.get(role, '/api/catalogues', status=200 if role in PERMISSIONS['catalogue.read'] else 403)


def test_crud_archive_order_history_and_checked_undo(h):
    row = h.configure('leave_types', {'name': 'Custom', 'short_code': 'CUST', 'colour': 'pink', 'sort_order': 0})['record']
    edited = h.configure('leave_types', value(row, name='Custom renamed', active=False), row['id'], row['revision'])
    assert row['id'] not in {t['id'] for t in choices(h)}
    archived = next(t for t in h.get('admin', '/api/catalogues')['leave_types'] if t['id'] == row['id'])
    assert archived['active'] is False
    h.configure('leave_types', edited['previous_value'], row['id'], row['revision'], status=409)
    restored = h.configure('leave_types', edited['previous_value'], row['id'], edited['record']['revision'])['record']
    assert restored['name'] == 'Custom' and restored['active'] is True
    listed = choices(h)
    assert listed == sorted(listed, key=lambda t: (t['sort_order'], t['name'], t['id']))
    history = h.get('admin', f"/api/catalogues/leave_types/{row['id']}/history")['versions']
    assert [r['object_revision'] for r in history] == [1, 2, 3]
    assert [r['snapshot']['active'] for r in history] == [True, False, True]
    with transaction(h.app.state.engine) as db:
        audits = list(db.scalars(sa.select(m.AuditEvent).where(m.AuditEvent.target_id == row['id'])))
        assert sorted(r.action for r in audits) == ['catalogue.create', 'catalogue.update', 'catalogue.update']
        assert all(r.detail['catalogue'] == 'leave_types' for r in audits)


@pytest.mark.parametrize('changes,status', [({'colour': 'red'}, 422), ({'sort_order': -1}, 422),
    ({'short_code': 'longcode'}, 422), ({'name': ' annual '}, 409), ({'short_code': 'SICK'}, 409)])
def test_catalogue_validation(h, changes, status):
    h.configure('leave_types', {'name': 'Custom', 'short_code': 'CUST', **changes}, status=status)


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_request_create_permissions_unchanged(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    request_leave(h, role=role, status=200 if role in ('staff', 'coordinator', 'admin') else 403)


def test_department_isolation_and_active_selection(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
        db.add(department); db.flush()
        foreign = m.LeaveType(department_id=department.id, name='Foreign private catalogue', short_code='FORGN')
        db.add(foreign); db.flush()
        foreign_id = foreign.id
    assert foreign_id not in {t['id'] for t in choices(h)}
    h.configure('leave_types', {'name': 'Edited', 'short_code': 'FORGN'}, foreign_id, 1, status=404)
    request_leave(h, typ={'id': foreign_id}, status=404)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'actual_absence',
        'leave_type_id': foreign_id, 'private_reason': 'Synthetic note', 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00'}, status=404)
    row = selected(h)
    archived = h.configure('leave_types', value(row, active=False), row['id'], row['revision'])['record']
    request_leave(h, typ=archived, status=422)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'actual_absence',
        'leave_type': 'Annual leave', 'private_reason': 'Synthetic note', 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00'}, status=422)


def test_archived_request_approval_undo_and_all_read_projections(h):
    typ = selected(h)
    row = request_leave(h, typ)['record']
    h.configure('leave_types', value(typ, name='Annual renamed', active=False), typ['id'], typ['revision'])
    approved = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic approval'}, row['id'], row['revision'])['record']
    assert approved['leave_type_id'] == typ['id'] and approved['leave_type_name'] == 'Annual renamed'
    all_item = h.get('staff', '/api/requests/all?id=' + row['id'])['item']
    legacy = h.get('staff', '/api/requests')['requests'][0]
    recorded = h.get('staff', '/api/availability')['availability'][0]
    home = next(i for i in h.get('staff', '/api/home')['my_requests'] if i['id'] == row['id'])
    assert all(i['leave_type_name'] == 'Annual renamed' and i['leave_type_id'] == typ['id'] for i in (all_item, legacy, recorded, home))
    assert home['counts_as_unavailable'] is True and 'private_reason' not in home
    assert h.get('staff', '/api/requests/all?q=Annual%20renamed')['total'] == 1
    undone = h.command('approver', 'request.undo', {'reason': 'Undo synthetic decision'}, approved['id'], approved['revision'])['record']
    assert undone['leave_type_id'] == typ['id']
    assert h.get('staff', '/api/availability')['availability'][0]['retracted'] is True
    again = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approve again'}, undone['id'], undone['revision'])['record']
    assert again['leave_type_id'] == typ['id']


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_planning_and_calendar_display_share_catalogue_name(h, mode):
    typ = selected(h)
    record = absence(h, typ, leave_type='Original text is retained')
    h.configure('leave_types', value(typ, name='Vacation', active=False), typ['id'], typ['revision'])
    setting(h, mode)
    w = h.shift(h.workspace())['workspace']
    shown = leave_of(h, w)[0]
    events = h.get('coordinator', '/api/calendar?start=2026-10-04&end=2026-10-05')['events']
    event = next(i for i in events if i['id'] == record['id'])
    if mode == 'none':
        assert shown is None and 'leave' not in event and event['leave_type_name'] is None
    else:
        assert shown == event['leave']
        assert shown['type'] == 'Vacation' and event['leave_type_name'] == 'Vacation'
        assert ('note' in shown) == (mode == 'details')
    item = h.get('coordinator', '/api/requests/all?id=' + record['id'])['item']
    assert item['leave_type_name'] == 'Vacation' and item['leave_type'] == 'Original text is retained'
    assert ('private_reason' in item) == (mode == 'details')
    if mode == 'details':
        assert item['private_reason'] == 'Synthetic private leave note'


def test_availability_defaults_are_captured_not_retroactive(h):
    typ = h.configure('leave_types', {'name': 'Assignable study', 'short_code': 'ASSTDY', 'counts_as_unavailable': False})['record']
    recorded = absence(h, typ)
    pending = request_leave(h, typ)['record']
    assert all(not i['counts_as_unavailable'] for i in h.get('staff', '/api/home')['my_requests'])
    h.configure('leave_types', value(typ, counts_as_unavailable=True), typ['id'], typ['revision'])
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic decision'}, pending['id'], pending['revision'])
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    assert not data['availability'] and evaluate(data)['valid']
    assert not recorded['counts_as_unavailable']
    from .test_staff_wishes import wish
    assert wish(h, '2026-10-04', kind='prefer_on')['wish']['kind'] == 'prefer_on'
    current = selected(h)
    blocked = absence(h, current)
    h.configure('leave_types', value(current, counts_as_unavailable=False), current['id'], current['revision'])
    data = h.snapshot(w)
    assert [i['id'] for i in data['availability']] == [blocked['id']]
    assert 'UNAVAILABLE' in {f['code'] for f in evaluate(data)['findings']}


@pytest.mark.parametrize('mode', ['none', 'type', 'details'])
def test_person_work_uses_catalogue_names_and_captured_defaults_with_disclosure(h, mode):
    typ = h.configure('leave_types', {'name': 'Custom study', 'short_code': 'CSTDY',
                                     'counts_as_unavailable': False})['record']
    recorded = absence(h, typ, leave_type='Original leave text')
    requested = request_leave(h, typ)['record']
    h.configure('leave_types', value(typ, name='Renamed study', active=False, counts_as_unavailable=True),
                typ['id'], typ['revision'])
    setting(h, mode)
    path = f"/api/people/{h.seed['people'][0]}/availability?start=2026-10-04&end=2026-10-05"
    for role in ('coordinator', 'staff'):
        data = h.get(role, path)
        rows = [next(r for r in data['availability'] if r['id'] == recorded['id']),
                next(r for r in data['requests'] if r['id'] == requested['id'])]
        visible = role == 'staff' or mode != 'none'
        assert all(r['label'] == ('Renamed study' if visible else 'On leave or away') for r in rows)
        assert all(r['leave_type_name'] == ('Renamed study' if visible else None) for r in rows)
        assert all(r['leave_type_id'] == (typ['id'] if visible else None) for r in rows)
        assert all(r['counts_as_unavailable'] is False and 'private_reason' not in r for r in rows)
        assert all(('note' in r) == (role == 'coordinator' and mode == 'details') for r in rows)
        if role == 'staff':
            calendar = h.get(role, '/api/calendar?start=2026-10-04&end=2026-10-05')
            event = next(e for e in calendar['events'] if e['id'] == recorded['id'])
            assert event['label'] == event['leave_type_name'] == event['leave']['type'] == 'Renamed study'
            assert event['counts_as_unavailable'] is False and 'note' not in event['leave']


def test_captured_defaults_reach_both_schedulers_and_fairness(h):
    from packages.domain.cp_sat import generate as solve
    from packages.domain.generation import generate as greedy
    from packages.domain.fairness import member_days
    typ = h.configure('leave_types', {'name': 'Assignable leave', 'short_code': 'ASGN', 'counts_as_unavailable': False})['record']
    absence(h, typ)
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    person = h.seed['people'][0]
    data['people'] = {person: data['people'][person]}
    h.policy({'kind': 'minimum_cover', 'minimum': 1})
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    for generate in (lambda d: solve(d, 3), greedy):
        result = generate(data)
        assert result['status'] == 'candidate_feasible', result
        assert any(a['participant']['person_id'] == person for a in result['additions'])
    assert member_days(data, person, date(2026, 10, 4), date(2026, 10, 5)) == [date(2026, 10, 4)]
    absence(h, end='2026-10-04T13:00:00+03:00')
    assert member_days(h.snapshot(w), person, date(2026, 10, 4), date(2026, 10, 5)) == []


def test_nonleave_records_keep_their_unavailability_and_staff_calendar_notes_stay_private(h):
    typ = h.configure('leave_types', {'name': 'Assignable leave', 'short_code': 'ASGN', 'counts_as_unavailable': False})['record']
    for kind in ('external_commitment', 'protected_time', 'off_duty'):
        h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': kind,
            'leave_type_id': typ['id'], 'private_reason': 'Private absence note', 'start': '2026-10-04T08:00:00+03:00',
            'end': '2026-10-04T12:00:00+03:00'}, status=422)
    record = absence(h)
    setting(h, 'details')
    event = next(e for e in h.get('staff', '/api/calendar?start=2026-10-04&end=2026-10-05')['events'] if e['id'] == record['id'])
    assert event['leave']['type'] == 'Annual' and 'note' not in event['leave']


@pytest.mark.postgres
def test_postgres_catalogue_reference_archive_and_undo(pg):
    test_crud_archive_order_history_and_checked_undo(pg)
    test_archived_request_approval_undo_and_all_read_projections(pg)


def legacy_mapping(url, monkeypatch):
    from .migration_support import add_historical_admin

    command.upgrade(alembic_config(url), '0060_quota_line_contract')
    engine = make_engine(url)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    base = dict(revision=3, created_at=now)
    ids = [str(uuid.uuid4()) for _ in range(5)]
    org_id, department, second, person, actor = ids
    with engine.begin() as db:
        metadata = sa.MetaData()
        tables = {name: sa.Table(name, metadata, autoload_with=db) for name in ('organisations', 'departments', 'people', 'identities', 'availability', 'requests')}
        db.execute(tables['organisations'].insert(), dict(base, id=org_id, name='Legacy Org', timezone='Asia/Riyadh', week_start=6, input_revision=9, availability_revision=5))
        for dept in (department, second):
            db.execute(tables['departments'].insert(), dict(base, id=dept, organisation_id=org_id, name=dept, generation_defaults={}))
        db.execute(tables['people'].insert(), dict(base, id=person, organisation_id=org_id, display_name='Legacy synthetic person', active=True, title='', work_phone='', work_extension='', staff_number='', directory_section='', directory_order=1000))
        db.execute(tables['identities'].insert(), dict(base, id=actor, issuer='test', subject='legacy', display_name='Legacy recorder', disabled=True))
        request_id = str(uuid.uuid4())
        interval = dict(start=now, end=datetime(2026, 1, 2, tzinfo=timezone.utc), private_reason='Details stay private and unchanged')
        db.execute(tables['requests'].insert(), dict(base, **interval, id=request_id, department_id=department, person_id=person, kind='leave', status='approved', created_by=actor))
        for kind in ('leave', 'meeting'):
            db.execute(tables['requests'].insert(), dict(base, **interval, id=str(uuid.uuid4()), department_id=department,
                person_id=person, kind=kind, status='submitted', created_by=actor))
        expected = {
            'Annual leave': 'ANNUAL', ' annual ': 'ANNUAL', 'ANNUAL': 'ANNUAL',
            'Sick leave': 'SICK', 'SICK': 'SICK', 'Study': 'STUDY', 'Study leave': 'STUDY',
            'conference': 'STUDY', 'Conference leave': 'STUDY', 'Study/Conference': 'STUDY',
            'Study/Conference leave': 'STUDY', 'Emergency': 'EMERG', 'Emergency leave': 'EMERG',
            'EMERG': 'EMERG', 'Other': 'OTHER', 'Other leave': 'OTHER',
            'Mixed annual/sick': 'OTHER', 'unrecognized': 'OTHER', '': 'OTHER',
        }
        labels = list(expected)
        for label in labels:
            db.execute(tables['availability'].insert(), dict(base, **interval, id=str(uuid.uuid4()), department_id=department,
                person_id=person, kind='approved_leave' if label == 'Annual leave' else 'actual_absence', leave_type=label,
                recorded_by=actor, request_id=request_id if label == 'Annual leave' else None, retracted=label == 'SICK'))
        add_historical_admin(db, department)
        before = {name: [dict(r) for r in db.execute(sa.select(table).order_by(table.c.id)).mappings()] for name, table in tables.items()}
    engine.dispose()
    from packages.domain import leave_types
    monkeypatch.setattr(leave_types, 'DEFAULT_LEAVE_TYPES', (('Changed runtime type', 'NEW', 'pink'),))
    monkeypatch.setattr(leave_types, 'LEGACY_CODES', {'annual leave': 'SICK'})

    def runtime_matching_must_not_run(*args):
        raise AssertionError('Historical migrations must not call the runtime leave matcher')

    monkeypatch.setattr(leave_types, 'matching_type', runtime_matching_must_not_run)
    migrate(url)
    engine = make_engine(url)
    try:
        with engine.begin() as db:
            metadata = sa.MetaData()
            for name, original in before.items():
                table = sa.Table(name, metadata, autoload_with=db)
                # Earlier additive migrations may append audit identities; every original row and field must survive.
                after = list(db.execute(sa.select(table).where(table.c.id.in_([r['id'] for r in original]))
                                        .order_by(table.c.id)).mappings())
                assert [{k: r[k] for k in original[0]} for r in after] == original
            types = sa.Table('leave_types', metadata, autoload_with=db)
            rows = list(db.execute(sa.select(types)).mappings())
            assert len(rows) == 10
            for dept in (department, second):
                seeded = sorted((r for r in rows if r['department_id'] == dept), key=lambda r: r['sort_order'])
                assert [(r['name'], r['short_code'], r['colour']) for r in seeded] == [
                    ('Annual', 'ANNUAL', 'teal'), ('Sick', 'SICK', 'coral'), ('Study/Conference', 'STUDY', 'blue'),
                    ('Emergency', 'EMERG', 'amber'), ('Other', 'OTHER', 'slate')]
                assert all(r['active'] and r['counts_as_unavailable'] for r in seeded)
            types_by_id = {r['id']: r for r in rows}
            for row in db.execute(sa.select(metadata.tables['availability'])).mappings():
                assert types_by_id[row['leave_type_id']]['short_code'] == expected[row['leave_type']]
                assert types_by_id[row['leave_type_id']]['department_id'] == row['department_id']
                assert row['counts_as_unavailable'] is True
            for request_row in db.execute(sa.select(metadata.tables['requests'])).mappings():
                if request_row['kind'] != 'leave':
                    assert request_row['leave_type_id'] is None
                else:
                    expected_code = 'ANNUAL' if request_row['status'] == 'approved' else 'OTHER'
                    assert types_by_id[request_row['leave_type_id']]['short_code'] == expected_code
    finally:
        engine.dispose()


def test_migration_maps_existing_data_without_rewriting_original_fields(tmp_path, monkeypatch):
    legacy_mapping('sqlite:///' + str(tmp_path / 'legacy.db'), monkeypatch)


@pytest.mark.postgres
def test_postgres_legacy_mapping_in_owned_disposable_schema(pg, monkeypatch):
    schema = 'rota_test_leave_' + uuid.uuid4().hex
    engine = pg.app.state.engine
    with engine.begin() as db:
        db.execute(sa.text('CREATE SCHEMA ' + schema))
    url = make_url(pg.settings.database_url).update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    try:
        legacy_mapping(url, monkeypatch)
    finally:
        with engine.begin() as db:
            db.execute(sa.text('DROP SCHEMA ' + schema + ' CASCADE'))
