"""Events retain occurrence history and use shared audiences, Tags and M1 receipt evidence."""
from .clock_support import freeze_clock
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from packages.contracts import schemas as s
from packages.domain.time import aware, local_midnight
from .governance_support import identity


def event_type(h, **changes):
    return h.configure('event_types', {'name': 'Synthetic event type', 'starts_on': '2020-01-01',
        'default_audience': {'roles': ['staff']}, **changes})['record']


def value(typ, **changes):
    return {'event_type_id': typ['id'], 'heading': 'Synthetic meeting', 'description': 'Synthetic agenda',
        'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T10:00:00+03:00',
        'format': 'in_person', 'place': 'Synthetic meeting room', **changes}


def event(h, typ=None, **changes):
    return h.configure('events', value(typ or event_type(h), **changes), role='coordinator')['record']


def occurrences(h, role='coordinator', start='2026-10-01', end='2026-11-01'):
    return h.get(role, f'/api/events/occurrences?starts_on={start}&ends_on={end}')['occurrences']


def definition(h, row):
    reply = h.get('coordinator', f"/api/events/definitions/{row['id']}")
    return reply['record']


def input_of(row, **changes):
    return {**{key: row[key] for key in s.EventInput.model_fields if key in row}, **changes}


def repeat(**changes):
    return {'timezone': 'Asia/Riyadh', 'anchor': '2026-10-04',
        'days': [{'weekday': 6, 'start_time': '09:00', 'end_time': '10:00'}], **changes}


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_type_picker_and_type_and_event_write_permissions(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    typ = event_type(h)
    choices = h.get(role, '/api/event-types')['event_types']
    chosen = next(row for row in choices if row['id'] == typ['id'])
    assert chosen['name'] == typ['name'] and chosen['attendance'] == 'expected'
    if role not in {'admin', 'coordinator'}:
        assert 'default_audience' not in chosen and 'default_tag_ids' not in chosen
    h.configure('event_types', {'name': 'Another synthetic type', 'starts_on': '2020-01-01'},
        role=role, status=200 if role == 'admin' else 403)
    h.configure('events', value(typ), role=role, status=200 if role in {'admin', 'coordinator'} else 403)


def test_new_department_starter_types_are_editable_and_names_do_not_control_behavior(h):
    rows = h.get('admin', '/api/event-types')['event_types']
    defaults = {row['name']: row for row in rows}
    assert set(defaults) == {'Morning meeting', 'M&M', 'Conference', 'Activity'}
    assert defaults['Morning meeting']['blocks_duties'] is False
    assert defaults['Conference']['blocks_duties'] is True
    activity = defaults['Activity']
    assert (activity['attendance'], activity['calendar'], activity['notify_inbox'], activity['rsvp']) == (
        'optional', 'everyone', False, True)
    assert activity['default_audience']['everyone'] is True
    edited = h.configure('event_types', {key: activity[key] for key in s.EventTypeInput.model_fields} |
        {'name': 'A renamed open event', 'blocks_duties': True}, activity['id'], activity['revision'])
    assert edited['record']['blocks_duties'] is True and edited['record']['name'] == 'A renamed open event'
    assert edited['previous_value']['name'] == 'Activity'
    assert len(h.get('admin', '/api/event-types')['event_types']) == 4


def test_raw_department_insertion_does_not_implicitly_seed_event_types(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Raw synthetic department')
        db.add(department)
        db.flush()
        assert db.scalar(select(func.count()).select_from(m.EventType).where(
            m.EventType.department_id == department.id)) == 0


def test_explicit_default_import_seeds_an_empty_legacy_department_once(h):
    department_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        # Raw insertion never seeds defaults; department creation explicitly owns that step.
        db.execute(insert(m.Department).values(id=department_id, organisation_id=h.seed['organisation_id'],
            name='Synthetic legacy department'))
    identity(h, 'legacy_admin', ['admin'], department=department_id)
    assert h.get('legacy_admin', '/api/event-types')['event_types'] == []
    imported = h.command('legacy_admin', 'event_types.import_defaults', {'reason': 'Import initial event configuration'})
    assert imported['imported'] == 4
    assert {row['name'] for row in imported['event_types']} == {'Morning meeting', 'M&M', 'Conference', 'Activity'}
    assert all(row['starts_on'] == '2026-10-03' for row in imported['event_types'])
    again = h.command('legacy_admin', 'event_types.import_defaults', {'reason': 'Repeat safe initial import'})
    assert again['imported'] == 0
    assert {row['id'] for row in h.get('legacy_admin', '/api/event-types')['event_types']} == {
        row['id'] for row in imported['event_types']}
    assert len(h.get('admin', '/api/event-types')['event_types']) == 4


def test_default_import_does_not_fill_in_a_custom_legacy_catalogue(h):
    department_id = m.uid()
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Department).values(id=department_id, organisation_id=h.seed['organisation_id'],
            name='Previously configured legacy department'))
    identity(h, 'custom_legacy_admin', ['admin'], department=department_id)
    custom = h.configure('event_types', {'name': 'Custom teaching', 'starts_on': '2020-01-01'},
        role='custom_legacy_admin')['record']
    reply = h.command('custom_legacy_admin', 'event_types.import_defaults', {'reason': 'Keep existing custom catalogue'})
    assert reply['imported'] == 0
    assert [row['id'] for row in h.get('custom_legacy_admin', '/api/event-types')['event_types']] == [custom['id']]


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_default_import_is_admin_only_and_keeps_configured_departments_unchanged(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    before = h.get('admin', '/api/event-types')['event_types']
    reply = h.command(role, 'event_types.import_defaults', {'reason': 'Synthetic import permission check'},
        status=200 if role == 'admin' else 403)
    if role == 'admin':
        assert reply['imported'] == 0
    assert h.get('admin', '/api/event-types')['event_types'] == before


@pytest.mark.parametrize('changes', [
    {'colour': 'red'}, {'default_start_time': '24:00'}, {'default_start_time': '25:15'},
    {'default_length_minutes': 0}, {'attendance': 'required'}, {'calendar': 'all'},
    {'default_audience': {}}, {'starts_on': '2026-10-10', 'ends_on': '2026-10-09'},
])
def test_type_contract_rejects_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        s.EventTypeInput.model_validate({'name': 'Synthetic type', 'starts_on': '2020-01-01', **changes})


@pytest.mark.parametrize('attendance', ['optional', 'expected', 'must_acknowledge'])
@pytest.mark.parametrize('blocks_duties', [False, True])
@pytest.mark.parametrize('rsvp', [False, True])
def test_optional_duty_protection_requires_going_response(attendance, blocks_duties, rsvp):
    value = {'name': 'Synthetic type', 'starts_on': '2020-01-01', 'attendance': attendance,
             'blocks_duties': blocks_duties, 'rsvp': rsvp}
    if attendance == 'optional' and blocks_duties and not rsvp:
        with pytest.raises(ValidationError, match='An optional event can take people off duties only when they answer Going'):
            s.EventTypeInput.model_validate(value)
    else:
        assert s.EventTypeInput.model_validate(value).model_dump(mode='json').items() >= value.items()


def test_optional_duty_protection_is_rejected_on_type_create_and_update(h):
    typ = event_type(h, attendance='optional', blocks_duties=True, rsvp=True)
    invalid = {key: typ[key] for key in s.EventTypeInput.model_fields} | {'rsvp': False}
    for target, revision in ((None, None), (typ['id'], typ['revision'])):
        reply = h.configure('event_types', invalid, target, revision, status=422)
        assert 'An optional event can take people off duties only when they answer Going' in str(reply)
    saved = next(row for row in h.get('admin', '/api/event-types')['event_types'] if row['id'] == typ['id'])
    assert saved['rsvp'] is True and saved['revision'] == typ['revision']


@pytest.mark.parametrize('changes', [
    {'start': '2026-10-04T09:00:00'}, {'end': '2026-10-04T09:00:00+03:00'},
    {'format': 'in_person', 'place': '', 'location_id': None},
    {'format': 'online', ('url'): ''}, {'format': 'hybrid', ('url'): ''},
    {'format': 'hybrid', 'place': '', ('url'): 'https://example.org/join'},
    {'format': 'online', ('url'): 'javascript:alert(1)'},
    {('url'): ('http' + 's://' + 'user' + ':sec' + 'ret@' + 'exam' + 'ple.' + 'org/' + 'join')}, {('url'): ('file' + ':///' + 'meet' + 'ing')},
    {'organiser_ids': ['00000000-0000-0000-0000-000000000001'] * 2},
    {'presenter_ids': ['00000000-0000-0000-0000-000000000001'] * 2},
    {'max_attendees': 0}, {'repeat_until': '2026-10-20'},
    {'skip_date_set_id': '00000000-0000-0000-0000-000000000001'},
])
def test_event_contract_rejects_unsafe_or_incomplete_inputs(changes):
    with pytest.raises(ValidationError):
        s.EventInput.model_validate(value({'id': '00000000-0000-0000-0000-000000000001'}, **changes))


@pytest.mark.parametrize('format,place,url', [
    ('in_person', 'Room A', ''), ('online', '', 'https://example.org/join'),
    ('hybrid', 'Room A', 'https://example.org/join'),
])
def test_event_formats_and_timezone_normalization(h, format, place, url):
    row = event(h, format=format, place=place, url=url, organiser_ids=[h.seed['people'][0]],
        presenter_ids=[h.seed['people'][1]])
    shown = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    assert shown['data']['format'] == format and shown['data']['place'] == place and shown['data'][('url')] == url
    assert shown['start'].startswith('2026-10-04T06:00:00')
    assert shown['end'].startswith('2026-10-04T07:00:00')
    assert shown['data']['organiser_ids'] == [h.seed['people'][0]]
    assert shown['data']['presenter_ids'] == [h.seed['people'][1]]


def test_create_defaults_tags_definition_history_checked_update_and_undo(h):
    tag = h.configure('tags', {'name': 'Synthetic CME', 'labels': ['events', 'department_content']})['record']
    typ = event_type(h, default_tag_ids=[tag['id']], colour='violet')
    row = event(h, typ)
    original = definition(h, row)
    assert original['tag_ids'] == [tag['id']] and original['audience']['roles'] == ['staff']
    assert h.get('coordinator', '/api/catalogues')['events'] == [
        {'id': row['id'], 'name': row['heading'], 'active': True}]
    with transaction(h.app.state.engine) as db:
        membership = db.scalar(select(m.TagMember).where(m.TagMember.kind == 'events', m.TagMember.member_id == row['id']))
        assert membership.tag_id == tag['id']
    updated = h.configure('events', input_of(original, heading='Changed meeting', tag_ids=[]),
        row['id'], row['revision'], role='coordinator')
    assert updated['previous_value']['tag_ids'] == [tag['id']]
    assert definition(h, row)['tag_ids'] == []
    h.configure('events', updated['previous_value'], row['id'], row['revision'], role='coordinator', status=409)
    restored = h.configure('events', updated['previous_value'], row['id'], updated['record']['revision'], role='coordinator')['record']
    assert restored['heading'] == row['heading'] and definition(h, row)['tag_ids'] == [tag['id']]
    history = h.get('admin', f"/api/catalogues/events/{row['id']}/history")['versions']
    assert [version['object_revision'] for version in history] == [1, 2, 3]
    for role in ('staff', 'approver'):
        h.get(role, f"/api/events/definitions/{row['id']}", status=403)


def test_event_type_update_is_audited_undoable_and_does_not_rewrite_occurrences(h):
    typ = event_type(h, calendar='everyone')
    row = event(h, typ)
    before = occurrences(h)[0]
    edited = h.configure('event_types', {key: typ[key] for key in s.EventTypeInput.model_fields} |
        {'name': 'Renamed type', 'calendar': 'none'}, typ['id'], typ['revision'])
    after = next(item for item in occurrences(h) if item['event_id'] == row['id'])
    assert after['data'] == before['data'] and after['revision'] == before['revision']
    restored = h.configure('event_types', edited['previous_value'], typ['id'], edited['record']['revision'])['record']
    assert restored['name'] == typ['name'] and restored['calendar'] == 'everyone'
    history = h.get('admin', f"/api/catalogues/event_types/{typ['id']}/history")['versions']
    assert [item['object_revision'] for item in history] == [1, 2, 3]


def test_existing_tag_members_command_labels_both_events_and_department_content(h):
    row = event(h)
    notice = h.configure('department_content', {'name': 'Tagged synthetic notice', 'starts_on': '2020-01-01'},
        role='coordinator')['record']
    tag = h.configure('tags', {'name': 'Shared CME', 'labels': ['events', 'department_content']})['record']
    h.command('coordinator', 'tag.members', {'add': [{'kind': 'events', 'id': row['id']},
        {'kind': 'department_content', 'id': notice['id']}], 'reason': 'Synthetic shared labels'}, tag['id'], tag['revision'])
    assert definition(h, row)['tag_ids'] == [tag['id']]
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    assert occurrence['data']['tag_ids'] == [tag['id']]
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(occurrence['data'], tag_ids=[]),
        'reason': 'Synthetic occurrence-only label edit'}, occurrence['id'], occurrence['revision'], status=422)
    master = definition(h, row)
    updated_value = input_of(master, heading='Keep current shared labels')
    updated_value.pop('tag_ids')
    h.configure('events', updated_value, row['id'], master['revision'], role='coordinator')
    assert definition(h, row)['tag_ids'] == [tag['id']]
    assert next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])['data']['tag_ids'] == [tag['id']]
    with transaction(h.app.state.engine) as db:
        assert {(item.kind, item.member_id) for item in db.scalars(select(m.TagMember).where(
            m.TagMember.tag_id == tag['id']))} == {('events', row['id']), ('department_content', notice['id'])}


def test_event_type_effective_dates_and_active_state_bind_every_occurrence(h):
    typ = event_type(h, starts_on='2026-10-05')
    h.configure('events', value(typ), role='coordinator', status=422)
    typ = event_type(h, starts_on='2026-10-04', ends_on='2026-10-11')
    event(h, typ)
    h.configure('events', value(typ, repeat=repeat(), repeat_until='2026-10-19'), role='coordinator', status=422)
    inactive = event_type(h, active=False)
    h.configure('events', value(inactive), role='coordinator', status=422)


def test_references_are_scoped_and_tags_must_label_events(h):
    typ = event_type(h)
    wrong_tag = h.configure('tags', {'name': 'People only', 'labels': ['people']})['record']
    h.configure('events', value(typ, tag_ids=[wrong_tag['id']]), role='coordinator', status=422)
    missing = m.uid()
    h.configure('events', value(typ, presenter_ids=[missing]), role='coordinator', status=422)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other synthetic department')
        db.add(other); db.flush()
        foreign = m.EventType(department_id=other.id, name='Private foreign type', starts_on=date(2020, 1, 1))
        db.add(foreign); db.flush()
        foreign_id = foreign.id
    h.configure('events', value({'id': foreign_id}), role='coordinator', status=404)
    assert foreign_id not in {row['id'] for row in h.get('staff', '/api/event-types')['event_types']}


def test_shared_condition_tree_filters_staff_and_keeps_editor_counts_private(h):
    staff_person = h.get('staff', '/api/me')['person_id']
    audience = {'where': {'match': 'all', 'conditions': [
        {'field': 'roles', 'values': ['staff']}, {'field': 'person_ids', 'values': [staff_person]}]}}
    visible = event(h, audience=audience)
    hidden = event(h, audience={'roles': ['coordinator']}, heading='For coordinators')
    shown = occurrences(h, 'staff')
    assert [row['event_id'] for row in shown] == [visible['id']]
    assert 'counts' not in shown[0] and shown[0]['receipt'] is None
    editor = occurrences(h)
    assert {row['event_id'] for row in editor} == {visible['id'], hidden['id']}
    assert next(row for row in editor if row['event_id'] == visible['id'])['counts']['audience'] == 1
    occurrence_id = shown[0]['id']
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
        h.get(role, f'/api/events/{occurrence_id}/receipts', status=200 if role in {'admin', 'coordinator'} else 403)


@pytest.mark.parametrize('role', [role for role in s.ROLE_NAMES if role not in {'admin', 'coordinator'}])
def test_non_editor_occurrences_allowlist_data_and_hide_type_audience_defaults(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    typ = event_type(h, default_audience={'person_ids': [h.seed['people'][1]]})
    row = event(h, typ, audience={'everyone': True})
    with transaction(h.app.state.engine, write=True) as db:
        occurrence = db.scalar(select(m.EventOccurrence).where(m.EventOccurrence.event_id == row['id']))
        occurrence.data = {**occurrence.data, 'private_config': 'Future occurrence configuration',
            'type': {**occurrence.data['type'], 'private_config': 'Future type configuration'}}
    shown = next(item for item in occurrences(h, role) if item['event_id'] == row['id'])
    assert shown['data']['heading'] == row['heading']
    assert shown['data']['type'] == shown['event_type']
    for data in (shown['data'], shown['data']['type'], shown['event_type']):
        assert not {'audience', 'default_audience', 'default_tag_ids', 'private_config'} & data.keys()
    assert 'counts' not in shown
    editor = next(item for item in occurrences(h) if item['event_id'] == row['id'])
    assert editor['data']['audience']['everyone'] is True
    assert editor['data']['type']['default_audience']['person_ids'] == [h.seed['people'][1]]
    assert editor['event_type']['default_audience'] == editor['data']['type']['default_audience']


def test_personal_occurrences_load_only_the_viewer_profile_once_per_date(h, monkeypatch):
    from apps.api.services import audiences
    row = event(h, repeat=repeat(), repeat_until='2026-10-19')
    event(h, audience={'roles': ['coordinator']}, heading='Hidden coordinator event')
    event(h, heading='Another event on the same date')
    person_id = h.get('staff', '/api/me')['person_id']
    original = audiences.profiles
    loaded = []

    def profiles(db, department_id, organisation_id, person_ids, day, *, context=None):
        assert person_ids == {person_id}
        loaded.append(day)
        return original(db, department_id, organisation_id, person_ids, day, context=context)

    def department_context(*args, **kwargs):
        pytest.fail('Personal occurrence reads must not load department-wide recipient facts')

    monkeypatch.setattr(audiences, 'profiles', profiles)
    monkeypatch.setattr(audiences, 'recipient_context', department_context)
    shown = occurrences(h, 'staff')
    assert len(shown) == 4
    assert sum(item['event_id'] == row['id'] for item in shown) == 3
    assert loaded == [date(2026, 10, day) for day in (4, 11, 18)]


def test_occurrence_range_is_local_half_open_and_bounded(h):
    row = event(h, start='2026-10-04T23:00:00+03:00', end='2026-10-05T01:00:00+03:00')
    assert [item['event_id'] for item in occurrences(h, start='2026-10-05', end='2026-10-06')] == [row['id']]
    assert occurrences(h, start='2026-10-06', end='2026-10-07') == []
    h.get('staff', '/api/events/occurrences?starts_on=2026-10-04&ends_on=2026-10-04', status=422)
    start = date(2026, 10, 1)
    end = start + timedelta(days=s.EVENT_OCCURRENCE_RANGE_MAX_DAYS)
    assert occurrences(h, 'staff', str(start), str(end))
    h.get('staff', f'/api/events/occurrences?starts_on={start}&ends_on={end + timedelta(days=1)}', status=422)


def test_recurring_edit_and_cancel_preserve_occurrence_identity_receipts_and_override(h):
    row = event(h, event_type(h, rsvp=True), repeat=repeat(), repeat_until='2026-10-19')
    original = occurrences(h)
    assert [item['start'][:10] for item in original] == ['2026-10-04', '2026-10-11', '2026-10-18']
    target = original[1]
    receipt_id = h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Synthetic RSVP'},
        target['id'], target['revision'])['receipt']['id']
    changed = h.command('coordinator', 'event.occurrence.update', {'value': input_of(target['data'],
        heading='One occurrence cancelled', cancelled=True), 'reason': 'Synthetic cancellation'},
        target['id'], target['revision'])['occurrence']
    assert changed['id'] == target['id']
    assert datetime.fromisoformat(changed['original_start']) == datetime.fromisoformat(target['original_start'])
    assert changed['cancelled'] and changed['overridden']
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Series renamed'), row['id'], master['revision'], role='coordinator')
    after = {item['id']: item for item in occurrences(h)}
    assert set(after) == {item['id'] for item in original}
    assert after[target['id']]['cancelled'] and after[target['id']]['data']['heading'] == 'One occurrence cancelled'
    assert after[original[0]['id']]['data']['heading'] == 'Series renamed'
    staff = {item['id']: item for item in occurrences(h, 'staff')}
    assert staff[target['id']]['receipt']['id'] == receipt_id
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(target['data']),
        'reason': 'Stale occurrence edit'}, target['id'], target['revision'], status=409)


@pytest.mark.parametrize('edit', ['definition', 'occurrence'])
def test_cancellation_after_audience_change_keeps_updated_details_private(h, edit):
    row = event(h)
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    process_one(h.app.state.engine)
    original = h.get('staff', '/api/notifications')['notifications']
    assert any(item['source_id'] == occurrence['id'] and item['title'] == row['heading'] for item in original)
    heading, agenda = 'Coordinators-only revised heading', 'Coordinators-only revised agenda'
    changes = {'audience': {'roles': ['coordinator']}, 'heading': heading, 'description': agenda, 'cancelled': True}
    if edit == 'definition':
        h.configure('events', input_of(definition(h, row), **changes), row['id'], row['revision'], role='coordinator')
    else:
        h.command('coordinator', 'event.occurrence.update', {'value': input_of(occurrence['data'], **changes),
            'reason': 'Restrict updated cancellation details'}, occurrence['id'], occurrence['revision'])
    process_one(h.app.state.engine)
    former = h.get('staff', '/api/notifications')
    assert heading not in str(former) and agenda not in str(former)
    cancellations = [item for item in former['notifications'] if item['source_id'] == occurrence['id']
                     and item['title'].startswith('Cancelled:')]
    assert len(cancellations) == 1
    current = h.get('coordinator', '/api/notifications')
    assert any(item['source_id'] == occurrence['id'] and item['title'] == f'Cancelled: {heading}'
               and item['message'] == agenda for item in current['notifications'])
    process_one(h.app.state.engine)
    assert h.get('staff', '/api/notifications') == former
    assert h.get('coordinator', '/api/notifications') == current


def test_recurring_rules_exclusions_and_first_interval_are_not_silently_ignored(h):
    typ = event_type(h)
    h.configure('events', value(typ, repeat=repeat(timezone='UTC'), repeat_until='2026-10-19'), role='coordinator', status=422)
    mismatched = repeat(days=[{'weekday': 0, 'start_time': '09:00', 'end_time': '10:00'}])
    h.configure('events', value(typ, repeat=mismatched, repeat_until='2026-10-19'), role='coordinator', status=422)
    row = event(h, typ, repeat=repeat(excluded_dates=['2026-10-11']), repeat_until='2026-10-18')
    assert [item['start'][:10] for item in occurrences(h) if item['event_id'] == row['id']] == ['2026-10-04']


def test_series_cancellation_and_undo_restore_active_overrides_and_keep_individual_cancellations(h):
    row = event(h, repeat=repeat(), repeat_until='2026-10-19')
    first, moved, cancelled = occurrences(h)
    edited = h.command('coordinator', 'event.occurrence.update', {'value': input_of(moved['data'],
        heading='Individually moved', start='2026-10-11T11:00:00+03:00', end='2026-10-11T12:00:00+03:00'),
        'reason': 'Synthetic individual edit'}, moved['id'], moved['revision'])['occurrence']
    individual_cancel = h.command('coordinator', 'event.occurrence.update', {'value': input_of(cancelled['data'],
        cancelled=True), 'reason': 'Synthetic individual cancellation'}, cancelled['id'], cancelled['revision'])['occurrence']
    master = definition(h, row)
    cancelled_series = h.configure('events', input_of(master, cancelled=True), row['id'], master['revision'],
        role='coordinator')
    assert all(item['cancelled'] for item in occurrences(h))
    h.configure('events', cancelled_series['previous_value'], row['id'], cancelled_series['record']['revision'],
        role='coordinator')
    restored = {item['id']: item for item in occurrences(h)}
    assert set(restored) == {first['id'], moved['id'], cancelled['id']}
    assert restored[first['id']]['cancelled'] is False
    assert restored[moved['id']]['cancelled'] is False and restored[moved['id']]['overridden'] is True
    assert datetime.fromisoformat(restored[moved['id']]['start']) == datetime.fromisoformat(edited['start'])
    assert restored[moved['id']]['data']['heading'] == 'Individually moved'
    assert restored[cancelled['id']]['cancelled'] is True
    assert restored[cancelled['id']]['revision'] == individual_cancel['revision']


def test_future_occurrence_cannot_be_moved_into_the_past(h):
    event(h)
    before = occurrences(h)[0]
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(before['data'],
        start='2026-10-01T09:00:00+03:00', end='2026-10-01T10:00:00+03:00'),
        'reason': 'Synthetic attempt to invent history'}, before['id'], before['revision'], status=409)
    after = occurrences(h)[0]
    assert after['start'] == before['start'] and after['revision'] == before['revision']


def test_past_occurrences_cannot_be_rewritten_by_occurrence_or_series_edit(h):
    row = event(h, start='2026-10-01T09:00:00+03:00', end='2026-10-01T10:00:00+03:00')
    before = occurrences(h)[0]
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(before['data'], heading='Rewritten'),
        'reason': 'Synthetic attempted history edit'}, before['id'], before['revision'], status=409)
    h.configure('events', input_of(definition(h, row), heading='New master heading'), row['id'], row['revision'], role='coordinator')
    after = occurrences(h)[0]
    assert after['data'] == before['data'] and after['revision'] == before['revision']


def test_rsvp_capacity_response_changes_and_own_receipt_only(h):
    other_person = next(pid for pid in h.seed['people'] if pid != h.get('staff', '/api/me')['person_id'])
    identity(h, 'other_staff', ['staff'], other_person)
    typ = event_type(h, rsvp=True, notify_inbox=False)
    row = event(h, typ, max_attendees=1)
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])

    def answer(role, response, status=200):
        return h.command(role, 'event.rsvp', {'response': response, 'reason': 'Synthetic RSVP'},
            occurrence['id'], occurrence['revision'], status=status)

    receipt = answer('staff', 'going')['receipt']
    assert receipt['identity_id'] == h.seed['identities']['staff'] and receipt['response'] == 'going'
    assert receipt['responded_at'] and receipt['read_at'] is None and receipt['acknowledged_at'] is None
    assert h.get('coordinator', f"/api/events/{occurrence['id']}/receipts")['total'] == 1
    assert answer('staff', 'going')['receipt']['revision'] == receipt['revision']
    answer('other_staff', 'going', status=409)
    answer('coordinator', 'going', status=403)
    assert answer('staff', 'not_going')['receipt']['response'] == 'not_going'
    assert answer('other_staff', 'going')['receipt']['response'] == 'going'
    cleared = answer('staff', None)['receipt']
    assert cleared['response'] is None and cleared['responded_at'] is None
    counts = next(item for item in occurrences(h) if item['id'] == occurrence['id'])['counts']
    assert counts['audience'] == 2 and counts['going'] == 1 and counts['not_going'] == 0 and counts['responded'] == 1
    page = h.get('coordinator', f"/api/events/{occurrence['id']}/receipts?page=1&page_size=1")
    assert page['total'] == 2 and len(page['receipts']) == 1
    h.get('coordinator', f"/api/events/{occurrence['id']}/receipts?page_size=201", status=422)


def test_rsvp_is_closed_when_disabled_cancelled_or_ended(h):
    for typ, changes in [(event_type(h, rsvp=False), {}), (event_type(h, rsvp=True), {'cancelled': True}),
        (event_type(h, rsvp=True), {'start': '2026-10-01T09:00:00+03:00', 'end': '2026-10-01T10:00:00+03:00'})]:
        row = event(h, typ, **changes)
        occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
        h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Synthetic RSVP'},
            occurrence['id'], occurrence['revision'], status=409)


def test_event_settings_validate_preserve_omitted_values_and_support_undo(h):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    assert dept['event_horizon_days'] == 365 and dept['event_receipt_window_days'] == 14
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
        'reason': 'Configure bounded event materialisation'}
    for bad in ({'event_horizon_days': 6}, {'event_horizon_days': s.EVENT_HORIZON_MAX_DAYS + 1},
        {'event_receipt_window_days': 0}, {'event_receipt_window_days': s.EVENT_HORIZON_MAX_DAYS + 1},
        {'event_horizon_days': None}, {'event_receipt_window_days': None}):
        h.command('admin', 'department.settings', {**payload, **bad}, dept['id'], org['revision'], status=422)
    saved = h.command('admin', 'department.settings', {**payload, 'event_horizon_days': 7,
        'event_receipt_window_days': 1}, dept['id'], org['revision'])
    assert saved['department']['event_horizon_days'] == 7 and saved['department']['event_receipt_window_days'] == 1
    assert saved['previous']['event_horizon_days'] == 365 and saved['previous']['event_receipt_window_days'] == 14
    kept = h.command('admin', 'department.settings', {**payload,
        'department_revision': saved['department']['revision']}, dept['id'], saved['organisation']['revision'])
    assert kept['department']['event_horizon_days'] == 7 and kept['department']['event_receipt_window_days'] == 1
    restored = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo event settings'},
        dept['id'], kept['organisation']['revision'])
    assert restored['department']['event_horizon_days'] == 365
    assert restored['department']['event_receipt_window_days'] == 14
    ceiling = h.command('admin', 'department.settings', {**payload,
        'department_revision': restored['department']['revision'], 'event_horizon_days': s.EVENT_HORIZON_MAX_DAYS,
        'event_receipt_window_days': s.EVENT_HORIZON_MAX_DAYS}, dept['id'], restored['organisation']['revision'])
    assert ceiling['department']['event_horizon_days'] == s.EVENT_HORIZON_MAX_DAYS
    assert ceiling['department']['event_receipt_window_days'] == s.EVENT_HORIZON_MAX_DAYS


@pytest.mark.parametrize('horizon', [7, 365])
def test_far_future_one_off_is_listed_and_delivered_beyond_series_horizon(h, horizon):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = horizon
    row = event(h, start='2028-04-03T09:00:00+03:00', end='2028-04-03T10:00:00+03:00')
    for role in ('coordinator', 'staff'):
        shown = occurrences(h, role, start='2028-04-01', end='2028-05-01')
        assert len(shown) == 1 and shown[0]['event_id'] == row['id']
        assert aware(shown[0]['start']) == aware(row['start'])
        assert shown[0]['receipt'] is None
    occurrence_id = shown[0]['id']
    process_one(h.app.state.engine)
    delivered = occurrences(h, 'staff', start='2028-04-01', end='2028-05-01')[0]
    receipt = delivered['receipt']
    assert delivered['id'] == occurrence_id
    assert receipt['identity_id'] == h.seed['identities']['staff'] and receipt['delivered_at']
    assert receipt['read_at'] is None and receipt['acknowledged_at'] is None
    notes = [item for item in h.get('staff', '/api/notifications')['notifications'] if item['source_kind'] == 'event']
    assert len(notes) == 1 and notes[0]['source_id'] == occurrence_id
    evidence = h.get('coordinator', f'/api/events/{occurrence_id}/receipts')
    assert evidence['total'] == 1 and evidence['receipts'][0]['id'] == receipt['id']


@pytest.mark.parametrize('repeat_until', [None, '2050-01-01'])
def test_daily_worker_extends_bounded_series_and_edit_rematerialises(h, monkeypatch, repeat_until):
    engine = h.app.state.engine
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    with transaction(engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.event_horizon_days, department.event_receipt_window_days = 7, 1
    daily = repeat(days=[{'weekday': weekday, 'start_time': '09:00', 'end_time': '10:00'} for weekday in range(7)])
    row = event(h, event_type(h, notify_inbox=False), repeat=daily, repeat_until=repeat_until)
    assert definition(h, row)['repeat_until'] == repeat_until
    first = occurrences(h)
    assert [item['start'][:10] for item in first] == [f'2026-10-{day:02}' for day in range(4, 10)]
    process_one(engine)
    with transaction(engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AnnouncementReceipt).where(
            m.AnnouncementReceipt.event_occurrence_id.in_([item['id'] for item in first]))) == 0
    freeze_clock(monkeypatch, lambda: aware('2026-10-04T05:00:00+00:00'))
    h.clients.pop('coordinator').close()
    process_one(engine)
    extended = occurrences(h)
    assert [item['start'][:10] for item in extended] == [f'2026-10-{day:02}' for day in range(4, 11)]
    assert {item['id'] for item in first} <= {item['id'] for item in extended}
    with transaction(engine) as db:
        receipted = set(db.scalars(select(m.AnnouncementReceipt.event_occurrence_id).where(
            m.AnnouncementReceipt.event_occurrence_id.in_([item['id'] for item in extended]))))
    assert receipted == {extended[0]['id']}
    process_one(engine)
    assert occurrences(h) == extended
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = 14
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Rebuilt current series'), row['id'], master['revision'], role='coordinator')
    edited = occurrences(h)
    assert [item['start'][:10] for item in edited] == [f'2026-10-{day:02}' for day in range(4, 18)]
    assert all(item['data']['heading'] == 'Rebuilt current series' for item in edited)


@pytest.mark.parametrize('legacy_snapshot', [False, True])
def test_daily_extension_keeps_overnight_occurrences_inside_event_type_dates(h, monkeypatch, legacy_snapshot):
    engine = h.app.state.engine
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = 7
    overnight = repeat(days=[{'weekday': day, 'start_time': '23:00', 'end_time': '01:00'} for day in range(7)])
    row = event(h, event_type(h, ends_on='2026-10-14', notify_inbox=False),
        start='2026-10-04T23:00:00+03:00', end='2026-10-05T01:00:00+03:00', repeat=overnight)
    if legacy_snapshot:
        with transaction(engine, write=True) as db:
            stored = db.get(m.Event, row['id'])
            stored.occurrence_data = {**stored.occurrence_data, 'type': {**stored.occurrence_data['type'],
                'attendance': 'optional', 'blocks_duties': True, 'rsvp': False}}
    first = occurrences(h)
    assert [item['start'][:10] for item in first] == [f'2026-10-{day:02}' for day in range(4, 10)]
    process_one(engine)
    freeze_clock(monkeypatch, lambda: aware('2026-10-08T06:00:00+00:00'))
    h.clients.pop('coordinator').close()
    process_one(engine)
    extended = occurrences(h)
    assert [item['start'][:10] for item in extended] == [f'2026-10-{day:02}' for day in range(4, 13)]
    assert {item['id'] for item in first} <= {item['id'] for item in extended}
    assert all(aware(item['end']) <= local_midnight(date(2026, 10, 14), 'Asia/Riyadh') for item in extended)
    assert definition(h, row)['repeat_until'] is None


def test_repeated_same_day_horizon_changes_extend_each_window(h, monkeypatch):
    engine = h.app.state.engine
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    daily = repeat(days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'} for day in range(7)])
    typ, row, existing_ids = event_type(h), None, set()
    for horizon in (7, 14, 21):
        me = h.get('admin', '/api/me')
        org, dept = me['organisation'], me['department']
        h.command('admin', 'department.settings', {'department_name': dept['name'],
            'department_revision': dept['revision'], 'organisation_name': org['name'],
            'timezone': org['timezone'], 'week_start': org['week_start'], 'event_horizon_days': horizon,
            'reason': 'Extend the current event window'}, dept['id'], org['revision'])
        if row is None:
            row = event(h, typ, repeat=daily)
        process_one(engine)
        shown = occurrences(h)
        assert [item['start'][:10] for item in shown] == [f'2026-10-{day:02}' for day in range(4, 3 + horizon)]
        assert all(not item['cancelled'] for item in shown)
        assert existing_ids <= {item['id'] for item in shown}
        existing_ids = {item['id'] for item in shown}
        with transaction(engine) as db:
            assert db.get(m.Department, dept['id']).events_checked_on == date(2026, 10, 3)
    process_one(engine)  # Deliver the notice created by the final reconciliation after its Undo hold.
    with transaction(engine) as db:
        sends = list(db.scalars(select(m.Outbox).where(m.Outbox.kind == 'event',
            m.Outbox.payload['event_id'].as_string() == row['id'])))
        assert len(sends) == 3 and all(item.state == 'available_in_app' for item in sends)


def test_shortened_window_keeps_matching_occurrences_overrides_and_receipts(h, monkeypatch):
    engine = h.app.state.engine
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = 21
    typ = event_type(h, rsvp=True, notify_inbox=False, blocks_duties=False)
    daily = repeat(days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'} for day in range(7)])
    row = event(h, typ, repeat=daily)
    original = {item['start'][:10]: item for item in occurrences(h)}
    target, override = original['2026-10-18'], original['2026-10-17']
    receipt = h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Keep attendance evidence'},
        target['id'], target['revision'])['receipt']
    h.command('coordinator', 'event.occurrence.update', {'value': input_of(override['data'], cancelled=True),
        'reason': 'Explicit individual cancellation'}, override['id'], override['revision'])
    with transaction(engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = 7
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Current bounded series'), row['id'], master['revision'], role='coordinator')
    shortened = {item['id']: item for item in occurrences(h, 'staff')}
    assert not shortened[target['id']]['cancelled']
    assert shortened[target['id']]['data']['heading'] == 'Current bounded series'
    assert shortened[target['id']]['receipt']['id'] == receipt['id']
    assert shortened[override['id']]['cancelled']
    h.configure('event_types', {key: typ[key] for key in s.EventTypeInput.model_fields} |
        {'name': 'Changed live type', 'blocks_duties': True}, typ['id'], typ['revision'])
    freeze_clock(monkeypatch, lambda: aware('2026-10-12T05:00:00+00:00'))
    process_one(engine)
    h.clients.pop('staff').close()
    restored = {item['id']: item for item in occurrences(h, 'staff')}
    current = restored[target['id']]
    assert current['cancelled'] is False and current['data']['heading'] == 'Current bounded series'
    assert current['receipt']['id'] == receipt['id'] and current['receipt']['response'] == 'going'
    assert current['data']['type']['name'] == typ['name'] and current['data']['type']['blocks_duties'] is False
    assert restored[override['id']]['cancelled'] and restored[override['id']]['overridden']


@pytest.mark.parametrize('horizon', [400, s.EVENT_HORIZON_MAX_DAYS])
def test_open_series_materialises_horizons_beyond_one_recurrence_preview(h, monkeypatch, horizon):
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).event_horizon_days = horizon
    daily = repeat(days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'} for day in range(7)])
    row = event(h, event_type(h, notify_inbox=False), repeat=daily)
    end = date(2026, 10, 3) + timedelta(days=horizon)
    with transaction(h.app.state.engine) as db:
        count, last = db.execute(select(func.count(m.EventOccurrence.id), func.max(m.EventOccurrence.start))
            .where(m.EventOccurrence.event_id == row['id'])).one()
        assert count == horizon - 1
        assert aware(last) < local_midnight(end, 'Asia/Riyadh')
        assert aware(last) == local_midnight(end - timedelta(days=1), 'Asia/Riyadh') + timedelta(hours=9)


def test_event_gets_do_not_materialise_receipts_and_read_creates_only_own(h):
    identity(h, 'another_event_reader', ['staff'])
    row = event(h, event_type(h, attendance='must_acknowledge', notify_inbox=False))
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    assert occurrence['receipt'] is None
    assert h.get('coordinator', f"/api/events/{occurrence['id']}/receipts")['total'] == 0
    opened = h.command('staff', 'notification.ack', {'reason': 'Read the event'},
        occurrence['id'], occurrence['revision'])['receipt']
    assert opened['identity_id'] == h.seed['identities']['staff'] and opened['read_at']
    assert opened['acknowledged_at'] is None and opened['delivered_at'] is None
    page = h.get('coordinator', f"/api/events/{occurrence['id']}/receipts")
    assert page['total'] == 1 and page['counts']['audience'] == 2 and page['counts']['read'] == 1
    assert next(item for item in occurrences(h, 'another_event_reader') if item['id'] == occurrence['id'])['receipt'] is None
    acknowledged = h.command('another_event_reader', 'notification.acknowledge', {'reason': 'Explicit acknowledgement'},
        occurrence['id'], occurrence['revision'])['receipt']
    assert acknowledged['read_at'] and acknowledged['acknowledged_at'] and acknowledged['delivered_at'] is None
    h.command('coordinator', 'notification.ack', {'reason': 'Outside the event audience'},
        occurrence['id'], occurrence['revision'], status=404)


def test_event_inbox_uses_canonical_acknowledgement_despite_legacy_timestamp(h):
    engine = h.app.state.engine
    row = event(h, event_type(h, attendance='must_acknowledge'))
    process_one(engine)
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    note = next(item for item in h.get('staff', '/api/notifications')['notifications']
        if item['source_id'] == occurrence['id'])
    legacy_at = m.now() - timedelta(days=1)
    with transaction(engine, write=True) as db:
        stored = db.get(m.Notification, note['id'])
        stored.read_at = stored.acknowledged_at = legacy_at
    pending = next(item for item in h.get('staff', '/api/notifications')['notifications']
        if item['id'] == note['id'])
    assert pending['kind'] == 'event' and pending['link'] == {'event_id': row['id']}
    assert pending['acknowledgement_required'] is True and pending['acknowledged_at'] is None
    assert occurrence['receipt']['acknowledged_at'] is None
    reply = h.command('staff', 'notification.acknowledge', {'reason': 'Explicit event acknowledgement'},
        pending['id'], pending['revision'])
    acknowledged = next(item for item in h.get('staff', '/api/notifications')['notifications']
        if item['id'] == note['id'])
    canonical = next(item for item in occurrences(h, 'staff') if item['id'] == occurrence['id'])['receipt']
    assert aware(acknowledged['acknowledged_at']) == aware(canonical['acknowledged_at']) == aware(reply['receipt']['acknowledged_at'])
    assert acknowledged['read_at'] == pending['read_at']
    with transaction(engine) as db:
        stored = db.get(m.Notification, note['id'])
        assert aware(stored.read_at) == aware(stored.acknowledged_at) == aware(legacy_at)


def input_revisions(h):
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        return org.input_revision, org.availability_revision


def semantic_fingerprints(h, workspace):
    from apps.api import snapshots
    from apps.api.services import planning, reviews
    from packages.domain.input_manifest import manifest
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        data = snapshots.domain_snapshot(db, row)
        check = planning.revalidate(db, row, store=False)
        return (check['fingerprint'], reviews.fingerprint(db, row),
                snapshots.fingerprints(db, [row])[row.id], manifest(data)['token'])


def test_nonblocking_event_create_edit_and_rsvp_leave_rota_inputs_current(h):
    typ = event_type(h, blocks_duties=False, rsvp=True)
    workspace = h.shift(h.workspace())['workspace']
    semantic = semantic_fingerprints(h, workspace)
    before = input_revisions(h)
    row = event(h, typ)
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Updated nonblocking event'), row['id'], master['revision'], role='coordinator')
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    edited = h.command('coordinator', 'event.occurrence.update', {'value': input_of(occurrence['data'],
        heading='Updated nonblocking occurrence'), 'reason': 'Synthetic occurrence edit'}, occurrence['id'], occurrence['revision'])['occurrence']
    h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Optional response'}, edited['id'], edited['revision'])
    # maintenance's registry contract conservatively stamps each writer transaction;
    # semantic checks remain current and occurrence reads remain read-only.
    assert input_revisions(h) == (before[0] + 4, before[1] + 4)
    assert semantic_fingerprints(h, workspace) == semantic


@pytest.mark.parametrize('occurrence_only', [False, True])
@pytest.mark.parametrize('before_blocks', [False, True])
def test_changing_between_blocking_and_nonblocking_types_invalidates_availability(h, occurrence_only, before_blocks):
    original = event_type(h, blocks_duties=before_blocks)
    replacement = event_type(h, blocks_duties=not before_blocks)
    row = event(h, original)
    before = input_revisions(h)
    if occurrence_only:
        occurrence = next(item for item in occurrences(h) if item['event_id'] == row['id'])
        h.command('coordinator', 'event.occurrence.update', {'value': input_of(occurrence['data'],
            event_type_id=replacement['id']), 'reason': 'Change duty blocking'}, occurrence['id'], occurrence['revision'])
    else:
        h.configure('events', input_of(definition(h, row), event_type_id=replacement['id']), row['id'], row['revision'], role='coordinator')
    assert input_revisions(h) == (before[0] + 1, before[1] + 1)


def test_past_blocking_occurrences_do_not_invalidate_later_nonblocking_series_edits(h, monkeypatch):
    freeze_clock(monkeypatch, lambda: aware('2026-10-03T06:00:00+00:00'))
    blocking, nonblocking = event_type(h, blocks_duties=True), event_type(h, blocks_duties=False)
    daily = repeat(days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'} for day in range(7)])
    row = event(h, blocking, repeat=daily, repeat_until='2026-10-07')
    freeze_clock(monkeypatch, lambda: aware('2026-10-05T05:00:00+00:00'))
    h.clients.pop('coordinator').close()
    h.configure('events', input_of(definition(h, row), event_type_id=nonblocking['id']),
        row['id'], row['revision'], role='coordinator')
    recorded = occurrences(h)
    assert recorded[0]['data']['type']['blocks_duties'] is True
    assert all(item['data']['type']['blocks_duties'] is False for item in recorded[1:])
    workspace = h.shift(h.workspace(start='2026-10-06'),
        start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00')['workspace']
    semantic = semantic_fingerprints(h, workspace)
    before = input_revisions(h)
    master = definition(h, row)
    h.configure('events', input_of(master, heading='Nonblocking future description'),
        row['id'], master['revision'], role='coordinator')
    assert input_revisions(h) == (before[0] + 1, before[1] + 1)
    assert semantic_fingerprints(h, workspace) == semantic


def test_blocking_event_rsvp_invalidates_availability_only_when_response_changes(h):
    row = event(h, event_type(h, blocks_duties=True, rsvp=True, attendance='optional'))
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    before = input_revisions(h)
    for _ in range(2):
        h.command('staff', 'event.rsvp', {'response': 'going', 'reason': 'Reserve event attendance'},
            occurrence['id'], occurrence['revision'])
        assert input_revisions(h) == (before[0] + 1, before[1] + 1)


@pytest.mark.postgres
def test_weekday_series_for_150_people_creates_no_receipts_on_create_or_edit(pg):
    h, engine = pg, pg.app.state.engine
    with transaction(engine, write=True) as db:
        people = [{'id': m.uid(), 'organisation_id': h.seed['organisation_id'], 'display_name': f'Synthetic meeting person {i}'}
            for i in range(150)]
        identities = [{'id': m.uid(), 'issuer': 'local-development', 'subject': f'event-write-scale-{i}',
            'display_name': person['display_name'], 'person_id': person['id']} for i, person in enumerate(people)]
        db.execute(insert(m.Person), people)
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.RoleBinding), [{'identity_id': item['id'], 'department_id': h.seed['department_id'],
            'role': 'staff', 'starts_on': date(2020, 1, 1)} for item in identities])
    weekday_repeat = repeat(anchor='2026-10-05', days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'}
        for day in range(5)])
    row = event(h, event_type(h, notify_inbox=False), start='2026-10-05T09:00:00+03:00',
        end='2026-10-05T10:00:00+03:00', repeat=weekday_repeat,
        audience={'person_ids': [person['id'] for person in people]})
    assert occurrences(h)[0]['counts']['audience'] == 150

    def receipt_count():
        with transaction(engine) as db:
            ids = select(m.EventOccurrence.id).where(m.EventOccurrence.event_id == row['id'])
            assert db.scalar(select(func.count()).select_from(m.EventOccurrence).where(
                m.EventOccurrence.event_id == row['id'])) >= 250
            return db.scalar(select(func.count()).select_from(m.AnnouncementReceipt).where(
                m.AnnouncementReceipt.event_occurrence_id.in_(ids)))

    assert receipt_count() == 0
    h.configure('events', input_of(definition(h, row), heading='Edited department weekday series'),
        row['id'], row['revision'], role='coordinator')
    assert receipt_count() == 0
