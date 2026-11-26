"""Date set configuration shares checked catalogue writes, audit and immutable publications."""
import copy
import uuid

import pytest
from alembic import command
from sqlalchemy import select, text

from apps.api import models as m
from apps.api.db import make_engine, transaction
from apps.api.snapshots import domain_snapshot
from packages.contracts import schemas as s
from scripts.manage import alembic_config, migrate
from .governance_support import identity
from .test_scheduling_facts_api import decide


def entry(**changes):
    return {'id': str(uuid.uuid4()), 'name': 'Public holiday', 'first_day': '2026-10-04',
            'last_day': '2026-10-05', 'repeat': 'once', **changes}


def date_set(h, **changes):
    return h.configure('date_sets', {'name': 'Public holidays', 'entries': [entry()], **changes})['record']


def change(h, row, kind, status=200, **payload):
    return h.command('admin', 'date_set.' + kind, {'reason': 'Synthetic date configuration', **payload},
                     row['id'], row['revision'], status=status)


def value(row, **changes):
    return {**{key: copy.deepcopy(row[key]) for key in s.DateSetInput.model_fields}, **changes}


def protection(h, condition):
    return h.command('coordinator', 'calendar.protection.propose', {
        'kind': 'protected_time', 'person_id': h.seed['people'][0], 'catalogue_id': 'protected_time',
        'line': {'id': str(uuid.uuid4()), 'where': {'conditions': [{'match': 'any', 'conditions': [condition]}]},
                 'numbers': {'apply': {'value': None, 'level': 'mandatory'}}},
        'reason': 'Synthetic holiday protection'})['fact']


def reference_change(h, row, action, status=200):
    if action in ('rename', 'all_rename'):
        return change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'}, status=status)
    if action == 'entry_delete':
        return change(h, row, 'entry.delete', entry_id=row['entries'][0]['id'], status=status)
    if action == 'set_delete':
        return change(h, row, 'delete', status=status)
    if action == 'bulk_update':
        return h.command('admin', 'catalogue.bulk_update', {'catalogue': 'date_sets',
            'items': [{'id': row['id'], 'revision': row['revision']}], 'active': False,
            'reason': 'Synthetic bulk archive'}, status=status)
    return h.configure('date_sets', value(row, **({'entries': []} if action == 'catalogue_update' else {'active': False})),
                       row['id'], row['revision'], status=status)


def test_set_entry_crud_revisions_audit_and_undo(h):
    initial = date_set(h)
    item = entry(name='National Day', first_day='2026-09-23', last_day='2026-09-23', repeat='yearly_gregorian')
    added = change(h, initial, 'entry.create', entry=item)
    row = added['record']
    assert len(row['entries']) == 2
    change(h, initial, 'entry.create', entry=item, status=409)
    renamed = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'})
    row = renamed['record']
    assert row['entries'][0]['name'] == 'Renamed holiday'
    assert not h.get('staff', '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05&entry_name=Public%20holiday')
    restored = h.configure('date_sets', renamed['previous_value'], row['id'], row['revision'])['record']
    assert restored['entries'][0]['name'] == 'Public holiday'
    assert h.get('staff', '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05&entry_name=Public%20holiday')
    deleted = change(h, restored, 'entry.delete', entry_id=item['id'])
    assert [item['name'] for item in deleted['record']['entries']] == ['Public holiday']
    restored = h.configure('date_sets', deleted['previous_value'], row['id'], deleted['record']['revision'])['record']
    archived = change(h, restored, 'delete')
    assert h.get('staff', '/api/date-sets')['date_sets'] == []
    assert h.get('admin', '/api/date-sets?include_inactive=true')['date_sets'][0]['active'] is False
    row = h.configure('date_sets', archived['previous_value'], restored['id'], archived['record']['revision'])['record']
    assert s.DateSetRecord.model_validate(h.get('staff', '/api/date-sets')['date_sets'][0]) == s.DateSetRecord.model_validate(row)
    history = h.get('admin', f"/api/catalogues/date_sets/{row['id']}/history")['versions']
    assert [item['object_revision'] for item in history] == list(range(1, row['revision'] + 1))
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == row['id'])))
        assert len(events) == row['revision']
        assert {'date_set.entry.create', 'date_set.entry.update', 'date_set.entry.delete', 'date_set.delete'} <= {e.action for e in events}
        assert db.get(m.DateSet, row['id']).entries == row['entries']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_date_set_role_permissions(h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    row = date_set(h)
    assert h.get(role, '/api/date-sets')['date_sets'][0]['id'] == row['id']
    assert h.get(role, '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05')[0]['name'] == 'Public holiday'
    h.get(role, '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05&include_inactive=true',
          status=200 if role in s.PERMISSIONS['date_sets.read_all'] else 403)
    h.get(role, '/api/date-sets?include_inactive=true',
          status=200 if role in s.PERMISSIONS['date_sets.read_all'] else 403)
    h.command(role, 'date_set.delete', {'reason': 'Role permission check'}, row['id'], row['revision'],
              status=200 if role == 'admin' else 403)
    h.configure('date_sets', {'name': 'Role-owned set'}, role=role, status=200 if role == 'admin' else 403)


def test_admin_can_preview_inactive_sets_without_activating_them(h):
    active = date_set(h)
    inactive = date_set(h, name='Switched-off dates', active=False)
    path = '/api/date-sets/resolved?start=2026-10-04&end=2026-10-05'
    assert {row['set_id'] for row in h.get('admin', path)} == {active['id']}
    assert not h.get('admin', path + f"&set_id={inactive['id']}")
    assert {row['set_id'] for row in h.get('admin', path + '&include_inactive=true')} == {active['id'], inactive['id']}
    preview = h.get('admin', path + f"&include_inactive=true&set_id={inactive['id']}&entry_name=Public%20holiday")
    assert [row['set_id'] for row in preview] == [inactive['id']]
    assert not h.get('admin', path + f"&include_inactive=true&set_id={inactive['id']}&entry_name=Other")
    assert not h.get('staff', path + f"&include_inactive=false&set_id={inactive['id']}")
    assert {row['id'] for row in h.get('staff', '/api/date-sets')['date_sets']} == {active['id']}


def test_invalid_hijri_start_is_rejected_on_write_and_old_stored_entries_are_isolated(h):
    bad = entry(name='Old Shaban end', repeat='yearly_hijri', first_day='1448-08-30', last_day='1448-09-01')
    rejected = h.configure('date_sets', {'name': 'Invalid dates', 'entries': [bad]}, status=422)
    assert 'Not every Hijri month has a 30th' in str(rejected)
    row = date_set(h, entries=[entry(first_day='2025-02-28', last_day='2025-02-28')])
    other = date_set(h, name='Other dates', entries=[entry(first_day='2025-02-28', last_day='2025-02-28')])
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.DateSet, row['id']).entries = [bad, *row['entries']]
    resolved = h.get('staff', '/api/date-sets/resolved?start=2025-02-28&end=2025-02-28')
    assert {item['set_id'] for item in resolved} == {row['id'], other['id']}
    assert {item['entry_id'] for item in resolved} == {row['entries'][0]['id'], other['entries'][0]['id']}
    for path in ('/api/schedule/days', '/api/schedule/sessions', '/api/calendar'):
        markers = h.get('staff', path + '?start=2025-02-28&end=2025-03-01')['date_set_markers']
        assert {marker['set_id'] for marker in markers['2025-02-28']} == {row['id'], other['id']}
    workspace = h.workspace(start='2025-02-28', end='2025-03-01')
    checked = h.shift(workspace, start='2025-02-28T08:00:00+03:00', end='2025-02-28T12:00:00+03:00')
    assert checked['validation']['valid']


def test_hijri_adjustment_only_changes_its_native_year_and_can_be_cleared(h):
    row = date_set(h, entries=[entry(name='Eid', repeat='yearly_hijri', first_day='1447-10-01', last_day='1447-10-04')])
    before = h.get('admin', '/api/date-sets/resolved?start=2025-01-01&end=2027-12-31')
    assert len(before) == 3
    original = next(item for item in before if item['year'] == 1447)
    row = change(h, row, 'adjust', entry_id=row['entries'][0]['id'], year=1447,
                 override={'year': 1447, 'first_day': '2026-03-21', 'last_day': '2026-03-24'})['record']
    after = h.get('staff', '/api/date-sets/resolved?start=2025-01-01&end=2027-12-31')
    changed = next(item for item in after if item['year'] == 1447)
    assert changed['adjusted'] is True and changed['first_day'] == '2026-03-21'
    assert [item for item in before if item['year'] != 1447] == [item for item in after if item['year'] != 1447]
    row = change(h, row, 'adjust', entry_id=row['entries'][0]['id'], year=1447, override=None)['record']
    assert next(item for item in h.get('staff', '/api/date-sets/resolved?start=2026-01-01&end=2026-12-31')
                if item['year'] == 1447) == original


def test_imports_are_atomic_checked_and_undoable_and_copy_keeps_source(h):
    initial = date_set(h, entries=[])
    csv = 'name,first_day,last_day,repeat\nNational Day,2026-09-23,2026-09-23,yearly_gregorian\n'
    imported = change(h, initial, 'import', format='csv', content=csv)
    row = imported['record']
    assert row['entries'][0]['repeat'] == 'yearly_gregorian'
    ical = '\r\n'.join(['BEGIN:VCALENDAR', 'VERSION:2.0', 'BEGIN:VEVENT', 'SUMMARY:Year end',
                        'DTSTART;VALUE=DATE:20261230', 'DTEND;VALUE=DATE:20270102', 'END:VEVENT', 'END:VCALENDAR'])
    row = change(h, row, 'import', format='ical', content=ical)['record']
    assert row['entries'][1]['last_day'] == '2027-01-01'
    change(h, row, 'import', format='csv', content=csv + 'Broken,not-a-date,2026-01-01,once\n', status=422)
    assert s.DateSetRecord.model_validate(h.get('admin', '/api/date-sets')['date_sets'][0]) == s.DateSetRecord.model_validate(row)
    copied = change(h, row, 'copy_next_year', year=2026, name='Next year holidays')['record']
    assert copied['id'] != row['id']
    assert {item['name']: (item['first_day'], item['last_day']) for item in copied['entries']} == {
        'Year end': ('2027-12-30', '2028-01-01')}
    assert all(item['repeat'] == 'once' for item in copied['entries'])
    with transaction(h.app.state.engine) as db:
        audit = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == copied['id']))
        assert audit.action == 'date_set.copy_next_year' and audit.detail['source_date_set_id'] == row['id']
    assert s.DateSetRecord.model_validate(next(item for item in h.get('admin', '/api/date-sets')['date_sets']
                                             if item['id'] == row['id'])) == s.DateSetRecord.model_validate(row)
    row = h.configure('date_sets', imported['previous_value'], row['id'], row['revision'])['record']
    assert row['entries'] == []


def test_yearly_ical_metadata_survives_storage_legacy_edits_and_undo(h):
    row = date_set(h, entries=[])
    content = '\r\n'.join(['BEGIN:VCALENDAR', 'BEGIN:VEVENT', 'SUMMARY:Two days',
                           'DTSTART;VALUE=DATE:20250228', 'DTEND;VALUE=DATE:20250302',
                           'RRULE:FREQ=YEARLY', 'END:VEVENT', 'END:VCALENDAR'])
    row = change(h, row, 'import', format='ical', content=content)['record']
    original = row['entries'][0]
    assert (original['first_day'], original['last_day'], original['start_year'], original['fixed_duration']) == (
        '2025-02-28', '2025-03-01', 2025, True)

    legacy = {key: val for key, val in original.items() if key not in {'start_year', 'fixed_duration'}}
    row = h.configure('date_sets', value(row, name='Renamed set', entries=[legacy]), row['id'], row['revision'])['record']
    row = change(h, row, 'entry.update', entry={**legacy, 'name': 'Renamed entry'})['record']
    assert row['entries'][0]['start_year'] == 2025 and row['entries'][0]['fixed_duration'] is True
    assert not h.get('staff', '/api/date-sets/resolved?start=2024-01-01&end=2024-12-31')
    assert [(item['first_day'], item['last_day']) for item in
            h.get('staff', '/api/date-sets/resolved?start=2028-01-01&end=2028-12-31')] == [('2028-02-28', '2028-02-29')]
    reloaded = h.get('admin', '/api/date-sets')['date_sets'][0]
    assert reloaded['entries'] == row['entries']

    cleared = change(h, row, 'entry.update', entry={**row['entries'][0], 'start_year': None, 'fixed_duration': False})
    assert cleared['record']['entries'][0]['start_year'] is None
    assert cleared['record']['entries'][0]['fixed_duration'] is False
    row = h.configure('date_sets', cleared['previous_value'], row['id'], cleared['record']['revision'])['record']
    assert row['entries'][0]['start_year'] == 2025 and row['entries'][0]['fixed_duration'] is True

    bad = content.replace('RRULE:FREQ=YEARLY', 'RRULE:FREQ=YEARLY;COUNT=2')
    rejected = change(h, row, 'import', format='ical', content=bad, status=422)
    assert 'iCal event 1:' in str(rejected) and 'RRULE' in str(rejected)
    assert s.DateSetRecord.model_validate(h.get('admin', '/api/date-sets')['date_sets'][0]) == s.DateSetRecord.model_validate(row)


def test_undo_of_metadata_addition_clears_it_on_older_stored_entries(h):
    row = date_set(h, entries=[entry(repeat='yearly_gregorian', first_day='2025-02-28', last_day='2025-03-01')])
    legacy = {key: val for key, val in row['entries'][0].items() if key not in {'start_year', 'fixed_duration'}}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.DateSet, row['id']).entries = [legacy]
    changed = change(h, row, 'entry.update', entry={**legacy, 'start_year': 2025, 'fixed_duration': True})
    restored = h.configure('date_sets', changed['previous_value'], row['id'], changed['record']['revision'])['record']
    assert restored['entries'][0]['start_year'] is None and restored['entries'][0]['fixed_duration'] is False
    assert h.get('staff', '/api/date-sets/resolved?start=2024-01-01&end=2024-12-31')


def test_department_isolation_and_reference_validation(h):
    row = date_set(h)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
        db.add(other); db.flush()
        foreign = m.DateSet(department_id=other.id, name='Private date set', entries=[])
        db.add(foreign); db.flush()
        foreign_id = foreign.id
    assert foreign_id not in {item['id'] for item in h.get('admin', '/api/date-sets')['date_sets']}
    h.get('admin', f'/api/date-sets/resolved?start=2026-01-01&end=2026-12-31&set_id={foreign_id}', status=404)
    h.get('admin', f'/api/date-sets/resolved?start=2026-01-01&end=2026-12-31&include_inactive=true&set_id={foreign_id}', status=404)
    h.command('admin', 'date_set.delete', {'reason': 'Cross-department refusal'}, foreign_id, 1, status=404)
    for set_id, names, status in [(foreign_id, [], 404), (str(uuid.uuid4()), [], 404),
                                 (row['id'], ['Typo'], 422), (row['id'], ['Public holiday'], 200)]:
        calendar = h.configure('operating_calendars', {'name': 'Scoped calendar', 'starts_on': '2020-01-01',
            'definition': {'timezone': 'Asia/Riyadh', 'date_set_rules': [
                {'date_set_id': set_id, 'entry_names': names, 'windows': []}]}}, status=status)
        policy = h.command('coordinator', 'policy.create', {'name': 'Scoped date rule', 'owner': 'Synthetic owner',
            'source': 'Synthetic reference validation', 'starts_on': '2020-01-01',
            'definition': {'severity': 'mandatory', 'rule': {'kind': 'restriction'},
                'scope': {'conditions': {'conditions': [{'field': 'date_set_ids', 'values': [set_id], 'entry_names': names}]}}}}, status=status)
        if status == 404:
            for reply in (calendar, policy):
                assert reply['error']['code'] == 'NOT_FOUND'
                assert reply['error']['message'] == 'Record not found'


@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'catalogue_update', 'bulk_update'])
def test_date_set_changes_preserve_rule_and_calendar_references(h, action):
    row = date_set(h)
    condition = {'field': 'date_set_ids', 'values': [row['id']], 'entry_names': ['Public holiday']}
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [
        {'match': 'any', 'conditions': [condition]}]}})
    proposed = h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week',
        'lines': [{'id': str(uuid.uuid4()), 'where': {'conditions': [condition]}, 'numbers': {'max': {'value': 2}}}],
        'reason': 'Synthetic date-scoped quota'})['record']
    calendar = h.configure('operating_calendars', {'name': 'Holiday calendar', 'starts_on': '2020-01-01',
        'definition': {'timezone': 'Asia/Riyadh', 'date_set_rules': [
            {'date_set_id': row['id'], 'entry_names': ['Public holiday'], 'windows': []}]}})['record']
    before = h.get('admin', '/api/date-sets')
    if action == 'rename':
        reply = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'}, status=409)
    elif action == 'entry_delete':
        reply = change(h, row, 'entry.delete', entry_id=row['entries'][0]['id'], status=409)
    elif action == 'set_delete':
        reply = change(h, row, 'delete', status=409)
    elif action == 'catalogue_update':
        reply = h.configure('date_sets', value(row, entries=[]), row['id'], row['revision'], status=409)
    else:
        reply = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'date_sets',
            'items': [{'id': row['id'], 'revision': row['revision']}], 'active': False,
            'reason': 'Synthetic bulk archive'}, status=409)
    assert reply['error']['code'] == 'DATE_SET_IN_USE'
    assert all(item['name'] in reply['error']['message'] for item in [rule, proposed, calendar])
    assert h.get('admin', '/api/date-sets') == before
    assert len(h.get('admin', f"/api/catalogues/date_sets/{row['id']}/history")['versions']) == 1


def test_date_set_reference_names_can_remain_in_another_selected_set_or_entry(h):
    row = date_set(h, entries=[entry(), entry()])
    other = date_set(h, name='Other holidays')
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [{'field': 'date_set_ids',
        'values': [row['id'], other['id']], 'entry_names': ['Public holiday']}]}})
    calendar = h.configure('operating_calendars', {'name': 'Any entry closes', 'starts_on': '2020-01-01',
        'definition': {'timezone': 'Asia/Riyadh', 'date_set_rules': [
            {'date_set_id': row['id'], 'windows': []}]}})['record']
    row = change(h, row, 'entry.delete', entry_id=row['entries'][0]['id'])['record']
    row = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'})['record']
    # A broad reference still names the set even after its last named entry changes.
    reply = change(h, row, 'delete', status=409)
    assert rule['name'] in reply['error']['message'] and calendar['name'] in reply['error']['message']


def test_retired_rules_and_inactive_calendars_do_not_block_date_set_changes(h):
    row = date_set(h)
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [{'field': 'date_set_ids',
        'values': [row['id']], 'entry_names': ['Public holiday']}]}})
    h.command('approver', 'policy.retire', {'reason': 'Synthetic rule no longer used'}, rule['id'], rule['revision'])
    h.configure('operating_calendars', {'name': 'Inactive calendar', 'starts_on': '2020-01-01', 'active': False,
        'definition': {'timezone': 'Asia/Riyadh', 'date_set_rules': [
            {'date_set_id': row['id'], 'entry_names': ['Public holiday'], 'windows': []}]}})
    row = change(h, row, 'entry.delete', entry_id=row['entries'][0]['id'])['record']
    assert change(h, row, 'delete')['record']['active'] is False


@pytest.mark.parametrize('grouped', [False, True], ids=['explicit', 'grouped'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'all_rename'])
def test_policy_restoration_rechecks_date_set_references(h, grouped, action):
    from .test_rule_templates import activate, propose

    row = date_set(h)
    selected = [row['id']]
    if action == 'all_rename':
        selected.append(date_set(h, name='Other holidays')['id'])
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [
        {'match': 'any', 'conditions': [{'field': 'date_set_ids', 'values': selected,
                                       'op': 'all' if action == 'all_rename' else 'any',
                                       'entry_names': ['Public holiday']}]}]}})
    if grouped:
        other = h.policy({'kind': 'restriction'}, {'person_ids': [h.seed['people'][0]]})
        target = activate(h, propose(h, {'kind': 'restriction'}, name='Grouped restrictions',
                                    merged_ids=[other['id'], rule['id']]))
    else:
        target = h.command('approver', 'policy.retire', {'reason': 'Synthetic retirement'},
                           rule['id'], rule['revision'])['record']
    if action in ('rename', 'all_rename'):
        row = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'})['record']
    elif action == 'entry_delete':
        row = change(h, row, 'entry.delete', entry_id=row['entries'][0]['id'])['record']
    else:
        row = change(h, row, 'delete')['record']
    before = [p for state in ('active', 'proposed', 'retired')
        for p in h.get('approver', '/api/policies?status=' + state)['policies']]
    with transaction(h.app.state.engine) as db:
        history_before = [(item.id, item.object_revision) for item in db.scalars(
            select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'policy').order_by(m.DefinitionHistory.id))]
    command = 'policy.retire' if grouped else 'policy.restore'
    reply = h.command('approver', command, {'reason': 'Restore synthetic restrictions'},
                      target['id'], target['revision'], status=422)
    assert reply['error']['code'] == ('DATE_SET_INACTIVE' if action == 'set_delete' else 'DATE_SET_ENTRY_NAMES')
    assert (row['name'] if action == 'set_delete' else 'Public holiday') in reply['error']['message']
    if action == 'all_rename':
        assert row['name'] in reply['error']['message']
    assert [p for state in ('active', 'proposed', 'retired')
        for p in h.get('approver', '/api/policies?status=' + state)['policies']] == before
    with transaction(h.app.state.engine) as db:
        assert [(item.id, item.object_revision) for item in db.scalars(
            select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'policy').order_by(m.DefinitionHistory.id))] == history_before
    h.configure('date_sets', value(row, active=True, entries=[entry()]), row['id'], row['revision'])
    restored = h.command('approver', command, {'reason': 'References repaired'}, target['id'], target['revision'])
    assert rule['id'] in restored['restored_ids']
    policies = {item['id']: item for item in [p for state in ('active', 'proposed', 'retired')
        for p in h.get('approver', '/api/policies?status=' + state)['policies']]}
    assert policies[rule['id']]['status'] == 'active' and policies[rule['id']]['active'] is True
    if grouped:
        assert other['id'] in restored['restored_ids'] and policies[target['id']]['status'] == 'retired'


def test_policy_restore_rejects_a_missing_date_set_without_disclosing_its_id(h):
    row = date_set(h)
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [
        {'field': 'date_set_ids', 'values': [row['id']]}]}})
    retired = h.command('approver', 'policy.retire', {'reason': 'Synthetic retirement'},
                        rule['id'], rule['revision'])['record']
    with transaction(h.app.state.engine, write=True) as db:
        db.delete(db.get(m.DateSet, row['id']))
    before = [p for state in ('active', 'proposed', 'retired')
        for p in h.get('approver', '/api/policies?status=' + state)['policies']]
    reply = h.command('approver', 'policy.restore', {'reason': 'Synthetic restore'},
                      rule['id'], retired['revision'], status=404)
    assert reply['error']['code'] == 'NOT_FOUND' and reply['error']['message'] == 'Record not found'
    assert [p for state in ('active', 'proposed', 'retired')
        for p in h.get('approver', '/api/policies?status=' + state)['policies']] == before


@pytest.mark.parametrize('fact_status', ['proposed', 'active'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'catalogue_update', 'deactivate', 'bulk_update'])
def test_date_set_changes_preserve_fact_references(h, fact_status, action):
    row = date_set(h)
    fact = protection(h, {'field': 'date_set_ids', 'values': [row['id']], 'entry_names': ['Public holiday']})
    if fact_status == 'active':
        fact = decide(h, fact)['fact']
    path = f"/api/people/{fact['person_id']}/scheduling-facts"
    before = h.get('approver', path)
    sets_before = h.get('admin', '/api/date-sets')
    reply = reference_change(h, row, action, status=409)
    assert reply['error']['code'] == 'DATE_SET_IN_USE'
    assert 'Fact: Calendar protection' in reply['error']['message']
    assert h.get('admin', '/api/date-sets') == sets_before
    assert h.get('approver', path) == before
    assert len(h.get('admin', f"/api/catalogues/date_sets/{row['id']}/history")['versions']) == 1


@pytest.mark.parametrize('fact_status', ['retired', 'declined', 'foreign'])
def test_unused_facts_do_not_block_date_set_changes(h, fact_status):
    row = date_set(h)
    fact = protection(h, {'field': 'date_set_ids', 'values': [row['id']], 'entry_names': ['Public holiday']})
    if fact_status == 'declined':
        decide(h, fact, 'decline')
    else:
        fact = decide(h, fact)['fact']
        if fact_status == 'retired':
            decide(h, fact, 'retire')
        else:
            with transaction(h.app.state.engine, write=True) as db:
                other = m.Department(organisation_id=h.seed['organisation_id'], name='Other department')
                db.add(other); db.flush()
                db.get(m.SchedulingFact, fact['id']).department_id = other.id
    row = reference_change(h, row, 'entry_delete')['record']
    assert reference_change(h, row, 'set_delete')['record']['active'] is False


@pytest.mark.parametrize('predecessor', [False, True], ids=['retired', 'predecessor'])
@pytest.mark.parametrize('action', ['rename', 'entry_delete', 'set_delete', 'deactivate', 'all_rename', 'missing_set'])
def test_fact_restoration_rechecks_date_set_references(h, predecessor, action):
    row = date_set(h)
    selected = [row['id']]
    if action == 'all_rename':
        selected.append(date_set(h, name='Other holidays')['id'])
    fact = decide(h, protection(h, {'field': 'date_set_ids', 'values': selected,
        'op': 'all' if action == 'all_rename' else 'any', 'entry_names': ['Public holiday']}))['fact']
    if predecessor:
        replacement = {**fact['data'], 'kind': fact['kind'], 'person_id': fact['person_id'],
            'line': {**fact['data']['line'], 'where': None}, 'replaces_id': fact['id'],
            'expected_revision': fact['revision'], 'reason': 'Synthetic replacement without a holiday condition'}
        target = decide(h, h.command('coordinator', 'calendar.protection.propose', replacement)['fact'])['fact']
    else:
        target = decide(h, fact, 'retire')['fact']
    if action == 'missing_set':
        with transaction(h.app.state.engine, write=True) as db:
            db.delete(db.get(m.DateSet, row['id']))
    else:
        row = reference_change(h, row, action)['record']
    path = f"/api/people/{fact['person_id']}/scheduling-facts"
    before = h.get('approver', path)
    with transaction(h.app.state.engine) as db:
        history_before = [(item.id, item.object_revision) for item in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'scheduling_fact').order_by(m.DefinitionHistory.id))]
    reply = decide(h, target, 'undo', status=404 if action == 'missing_set' else 422)
    assert reply['error']['code'] == ('NOT_FOUND' if action == 'missing_set' else
        'DATE_SET_INACTIVE' if action in ('set_delete', 'deactivate') else 'DATE_SET_ENTRY_NAMES')
    if action == 'missing_set':
        assert reply['error']['message'] == 'Record not found'
    else:
        assert (row['name'] if action in ('set_delete', 'deactivate', 'all_rename')
                else 'Public holiday') in reply['error']['message']
    assert h.get('approver', path) == before
    with transaction(h.app.state.engine) as db:
        assert [(item.id, item.object_revision) for item in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == 'scheduling_fact').order_by(m.DefinitionHistory.id))] == history_before
    if action == 'missing_set':
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.DateSet(id=row['id'], department_id=row['department_id'], **value(row)))
    else:
        h.configure('date_sets', value(row, active=True, entries=[entry()]), row['id'], row['revision'])
    decide(h, target, 'undo')
    assert [item['id'] for item in h.get('approver', path)['active']] == [fact['id']]


@pytest.mark.parametrize('action', ['activate', 'propose'])
def test_fact_admission_rejects_inactive_date_sets(h, action):
    row = date_set(h)
    fact = protection(h, {'field': 'date_set_ids', 'values': [row['id']], 'entry_names': ['Public holiday']})
    # Simulate a date set deactivated before the reference guard existed.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.DateSet, row['id']).active = False
    if action == 'activate':
        reply = decide(h, fact, status=422)
    else:
        reply = h.command('coordinator', 'calendar.protection.propose', {
            **fact['data'], 'kind': fact['kind'], 'person_id': fact['person_id'],
            'line': {**fact['data']['line'], 'id': str(uuid.uuid4())}, 'on': False,
            'reason': 'Synthetic inactive reference'}, status=422)
    assert reply['error']['code'] == 'DATE_SET_INACTIVE' and row['name'] in reply['error']['message']
    own = h.get('approver', f"/api/people/{fact['person_id']}/scheduling-facts")
    assert own['active'] == [] and [item['id'] for item in own['proposed']] == [fact['id']]


def test_an_inactive_date_set_cannot_preserve_a_rules_entry_name(h):
    row = date_set(h)
    other = date_set(h, name='Inactive holidays', active=False)
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [{'field': 'date_set_ids',
        'values': [row['id'], other['id']], 'entry_names': ['Public holiday']}]}})
    reply = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'}, status=409)
    assert reply['error']['code'] == 'DATE_SET_IN_USE' and rule['name'] in reply['error']['message']


def test_all_date_set_references_need_named_entries_in_every_selected_set(h):
    row = date_set(h)
    other = date_set(h, name='Other holidays')
    rule = h.policy({'kind': 'restriction'}, {'conditions': {'conditions': [{'field': 'date_set_ids', 'op': 'all',
        'values': [row['id'], other['id']], 'entry_names': ['Public holiday']}]}})
    reply = change(h, row, 'entry.update', entry={**row['entries'][0], 'name': 'Renamed holiday'}, status=409)
    assert reply['error']['code'] == 'DATE_SET_IN_USE' and rule['name'] in reply['error']['message']


def test_resolved_range_validation_and_contract_metadata(h):
    row = date_set(h)
    h.get('admin', '/api/date-sets/resolved?start=2026-10-05&end=2026-10-04', status=422)
    h.get('admin', '/api/date-sets/resolved?start=2026-01-01&end=2040-01-01', status=422)
    h.get('admin', '/api/date-sets/resolved?start=not-a-date&end=2026-10-04', status=422)
    assert h.get('admin', f"/api/date-sets/resolved?start=2026-10-04&end=2026-10-04&set_id={row['id']}&entry_name=Public%20holiday")
    assert not h.get('admin', '/api/date-sets/resolved?start=2026-10-06&end=2026-10-06')
    metadata = h.get('staff', '/api/contracts')
    assert 'DateSetInput' in metadata['schemas']
    assert 'date_set_ids' in metadata['condition_fields']
    assert metadata['condition_field_definitions']


def test_publication_date_sets_are_frozen_while_operational_snapshot_is_current(h):
    row = date_set(h)
    workspace = h.shift(h.workspace())['workspace']
    published = h.publish(workspace)
    changed = h.configure('date_sets', value(row, name='Changed after publication', entries=[]), row['id'], row['revision'])['record']
    with transaction(h.app.state.engine) as db:
        pub = db.get(m.Publication, published['publication_id'])
        assert pub.snapshot['date_sets'][row['id']] == row
        workspace = db.get(m.Workspace, workspace['id'])
        assert domain_snapshot(db, workspace)['date_sets'][row['id']] == row
        assert domain_snapshot(db, workspace, current_scheduling=True)['date_sets'][row['id']] == changed


@pytest.mark.parametrize('prior_revision', ['0070_open_ended_attachments', '0076_monitor_events'])
def test_migration_is_additive_and_preserves_existing_calendar(tmp_path, prior_revision):
    from .migration_support import add_historical_admin

    url = 'sqlite:///' + str(tmp_path / 'date-set-upgrade.db')
    command.upgrade(alembic_config(url), prior_revision)
    engine = make_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO organisations (id,revision,created_at,name,timezone,week_start,input_revision,availability_revision) "
                        "VALUES ('org',1,CURRENT_TIMESTAMP,'Existing','Asia/Riyadh',6,1,1)"))
        db.execute(text("INSERT INTO departments (id,revision,created_at,organisation_id,name,generation_defaults) "
                        "VALUES ('dept',1,CURRENT_TIMESTAMP,'org','Existing','{}')"))
        db.execute(text("INSERT INTO operating_calendars (id,revision,created_at,department_id,name,description,definition,starts_on,active) "
                        "VALUES ('cal',1,CURRENT_TIMESTAMP,'dept','Existing','Preserved','{\"timezone\":\"Asia/Riyadh\",\"week\":[]}', '2020-01-01',1)"))
        add_historical_admin(db, 'dept')
        before = tuple(db.execute(text('SELECT * FROM operating_calendars')).one())
    migrate(url)
    with engine.connect() as db:
        assert tuple(db.execute(text('SELECT * FROM operating_calendars')).one()) == before
        assert db.execute(text('SELECT count(*) FROM date_sets')).scalar_one() == 0
    engine.dispose()
