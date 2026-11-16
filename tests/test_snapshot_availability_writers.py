"""Every real availability writer stamps once and invalidates a bound bulk preview."""
from datetime import datetime
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import availability_patterns, event_delivery, events
from tests.test_bulk_single_commands import SOURCE, recorded_leave
from tests.clock_support import freeze_clock
from tests.test_availability_patterns import pattern
from tests.test_bulk import people_change, preview
from tests.test_bulk_single_commands import availability
from tests.test_events import event, event_type, occurrences, input_of, definition, repeat
from tests.test_published_changes import issued, change


REASON = 'Synthetic stamp acceptance'
WRITERS = [
    'availability.record', 'availability.update', 'availability.undo_update', 'availability.retract',
    'availability.restore', 'availability.undo_create',
    'request.leave.create', 'request.leave.approve', 'request.leave.withdraw', 'request.leave.undo',
    'request.absence_report.create', 'request.absence_report.approve', 'request.absence_report.undo',
    'request.decide_many', 'request.undo_batch', 'request.link_rota',
    'pattern.propose_active', 'pattern.propose_pending', 'pattern.approve', 'pattern.decline',
    'pattern.undo', 'pattern.end', 'pattern.restore',
    'publication.stage_absence', 'publication.undo_absence', 'publication.publish_absence',
    'import.recorded_leave',
    'event.create', 'event.update', 'event.occurrence_update', 'event.rsvp', 'event.reconcile', 'event.receipts',
    'event.tag_members',
    'request.overtime_create', 'request.rotation_change', 'request.rotation_swap', 'person.anonymise',
    'availability.route_request', 'request.restore_absence_inverse',
]


def command(h, kind, payload, row, role='coordinator'):
    return h.command(role, kind, payload, row['id'], row['revision'])


def operation(h, writer, monkeypatch):
    """Set up prerequisites before binding the preview; return exactly one writer transaction."""
    if writer == 'availability.route_request':
        from tests.test_rotation_leave_policy import resident, record_away
        resident(h, policy='needs_approval')
        return lambda: record_away(h)
    if writer == 'request.restore_absence_inverse':
        from tests.test_bulk_availability_inverse_chain import approved_request
        _row, _absence, item = approved_request(h)
        undone = h.command('request_inverse_decider', 'bulk.apply', {'items': [item], 'reason': REASON})
        inverse = undone['undo']
        return lambda: h.command('request_inverse_decider', inverse['kind'], inverse['payload'])
    if writer == 'person.anonymise':
        availability(h)
        with transaction(h.app.state.engine, write=True) as db:
            person = db.get(m.Person, h.seed['people'][0])
            person.active = False
            person.revision += 1
            revision = person.revision
        return lambda: h.command('admin', 'person.anonymise', {'reason': REASON}, h.seed['people'][0], revision)
    if writer == 'request.overtime_create':
        from tests.test_overtime_volunteer import offer, offer_rule
        offer_rule(h)
        return lambda: offer(h)
    if writer in {'request.rotation_change', 'request.rotation_swap'}:
        programme = h.configure('programmes', {'name': 'Stamp programme', 'starts_on': '2026-01-01'})['record']
        cohort = h.configure('cohorts', {'name': 'Stamp cohort', 'programme_id': programme['id'],
            'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})['record']
        h.configure('programme_memberships', {'person_id': h.seed['people'][0], 'programme_id': programme['id'],
            'starts_on': '2026-01-01', 'ends_on': '2026-12-01'})
        first = h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
            'service_id': h.seed['services'][0], 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')['record']
        if writer == 'request.rotation_change':
            return lambda: h.command('staff', 'rotation.request_change', {'rotation_id': first['id'],
                'service_id': h.seed['services'][0], 'starts_on': '2026-10-01', 'ends_on': '2026-11-02', 'private_reason': REASON})
        second = h.configure('rotations', {'person_id': h.seed['people'][0], 'cohort_id': cohort['id'],
            'service_id': h.seed['services'][1], 'starts_on': '2026-11-01', 'ends_on': '2026-11-15'}, role='coordinator')['record']
        return lambda: h.command('coordinator', 'rotation.swap', {'rotation_id': first['id'], 'expected_revision': first['revision'],
            'other_rotation_id': second['id'], 'other_expected_revision': second['revision'], 'reason': REASON})
    if writer.startswith('availability.'):
        if writer == 'availability.record':
            return lambda: availability(h, kind='actual_absence')
        saved = h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
            'kind': 'actual_absence', 'start': '2026-10-04T08:00:00+03:00',
            'end': '2026-10-04T12:00:00+03:00', 'private_reason': REASON})
        row = saved['record']
        if writer == 'availability.undo_create':
            undo = saved['undo']
            return lambda: h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
        if writer == 'availability.restore':
            row = command(h, 'availability.retract', {'reason': REASON}, row)['record']
        if writer == 'availability.undo_update':
            changed = command(h, 'availability.update', {'end': '2026-10-04T13:00:00+03:00', 'reason': REASON}, row)
            undo = changed['undo']
            return lambda: h.command('coordinator', undo['kind'], undo['payload'], undo['target_id'], undo['expected_revision'])
        payload = {'reason': REASON, **({'end': '2026-10-04T13:00:00+03:00'} if writer == 'availability.update' else {})}
        return lambda: command(h, writer, payload, row)
    if writer.startswith('request.'):
        if 'absence_report' in writer:
            pub, shifts = issued(h)
            payload = {'kind': 'absence_report', 'publication_id': pub,
                'assignment_id': shifts[0]['participants'][0]['id'], 'reason': REASON}
        else:
            payload = {'kind': 'leave', 'person_id': h.seed['people'][0],
                'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'private_reason': REASON}
        if writer.endswith('.create'):
            return lambda: h.command('staff', 'request.create', payload)
        row = h.command('staff', 'request.create', payload)['record']
        if writer == 'request.decide_many':
            return lambda: h.command('approver', 'request.decide_many', {'items': [
                {'source': 'request', 'id': row['id'], 'expected_revision': row['revision']}],
                'decision': 'approve', 'reason': REASON})
        if writer == 'request.undo_batch':
            batch = h.command('approver', 'request.decide_many', {'items': [
                {'source': 'request', 'id': row['id'], 'expected_revision': row['revision']}],
                'decision': 'approve', 'reason': REASON})
            return lambda: h.command('approver', 'request.undo', {'batch_id': batch['batch_id'], 'reason': REASON})
        if writer == 'request.link_rota':
            row = command(h, 'request.decide', {'decision': 'approve', 'reason': REASON}, row, 'approver')['record']
            workspace = h.workspace()
            return lambda: command(h, 'request.link_rota', {'workspace_id': workspace['id'], 'reason': REASON}, row)
        if writer.endswith('.undo'):
            row = command(h, 'request.decide', {'decision': 'approve', 'reason': REASON}, row, 'approver')['record']
            return lambda: command(h, 'request.undo', {'reason': REASON}, row, 'approver')
        decision = 'withdraw' if writer.endswith('.withdraw') else 'approve'
        return lambda: command(h, 'request.decide', {'decision': decision, 'reason': REASON}, row,
                               'staff' if decision == 'withdraw' else 'approver')
    if writer.startswith('pattern.'):
        payload = pattern(h.seed['people'][1])
        if writer in {'pattern.propose_active', 'pattern.propose_pending'}:
            role = 'coordinator' if writer.endswith('active') else 'staff'
            payload = pattern(h.seed['people'][1 if role == 'coordinator' else 0])
            return lambda: h.command(role, 'availability_pattern.propose', payload)
        row = h.command('staff', 'availability_pattern.propose', pattern(h.seed['people'][0]))['pattern']
        if writer in {'pattern.approve', 'pattern.decline'}:
            return lambda: command(h, 'availability_pattern.decide', {
                'decision': 'approve' if writer.endswith('approve') else 'decline', 'reason': REASON}, row)
        row = command(h, 'availability_pattern.decide', {'decision': 'approve', 'reason': REASON}, row)['pattern']
        if writer == 'pattern.restore':
            row = command(h, 'availability_pattern.end', {'reason': REASON}, row)['pattern']
        return lambda: command(h, 'availability_pattern.' + writer.split('.')[1], {'reason': REASON}, row)
    if writer.startswith('publication.'):
        pub, shifts = issued(h)
        if writer == 'publication.stage_absence':
            return lambda: change(h, pub, shifts[0], absence_reason=REASON)
        staged = change(h, pub, shifts[0], absence_reason=REASON)
        if writer == 'publication.undo_absence':
            return lambda: h.command('coordinator', 'publication.change.undo', {
                'change_id': staged['change']['id'], 'reason': REASON}, pub, staged['workspace']['revision'])
        sent = h.command('coordinator', 'publication.changes.submit', {'reason': REASON}, pub, staged['workspace']['revision'])
        approved = h.lifecycle(sent['workspace'], 'approve', 'approver')['workspace']
        return lambda: h.lifecycle(approved, 'publish')
    if writer == 'import.recorded_leave':
        manifest = {'archive_sha256': 'd' * 64, 'files': [{'path': 'content/leave.json', 'value': {'leave': [
            {'staffId': 'stamp-person', 'from': '2026-10-04', 'to': '2026-10-05', 'type': 'Other', 'note': ''}]}}]}
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.ReferenceImport(department_id=h.seed['department_id'], source_name=SOURCE,
                archive_sha256=manifest['archive_sha256'], manifest=manifest))
            db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE,
                source_key='person:stamp-person', target_kind='people', target_id=h.seed['people'][0]))
        def import_leave():
            with transaction(h.app.state.engine, write=True) as db:
                assert recorded_leave(db, manifest)['added'] == 1
        return import_leave
    typ = event_type(h, blocks_duties=True, rsvp=True)
    if writer == 'event.create':
        return lambda: event(h, typ)
    if writer == 'event.reconcile':
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Department, h.seed['department_id']).event_horizon_days = 7
        event(h, typ, repeat=repeat(days=[{'weekday': day, 'start_time': '09:00', 'end_time': '10:00'} for day in range(7)]))
        def reconcile():
            freeze_clock(monkeypatch, lambda: datetime.fromisoformat('2026-10-08T06:00:00+00:00'))
            if h.clients.get('coordinator'):
                h.clients.pop('coordinator').close()
            with transaction(h.app.state.engine, write=True) as db:
                events.reconcile(db)
        return reconcile
    row = event(h, typ)
    if writer == 'event.tag_members':
        tag = h.configure('tags', {'name': 'Stamp event label', 'labels': ['events']}, role='coordinator')['record']
        return lambda: h.command('coordinator', 'tag.members', {'add': [{'kind': 'events', 'id': row['id']}],
            'remove': [], 'reason': REASON}, tag['id'], tag['revision'])
    if writer == 'event.update':
        master = definition(h, row)
        return lambda: h.configure('events', input_of(master, heading='Changed event'),
                                   row['id'], master['revision'], role='coordinator')
    if writer == 'event.receipts':
        occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
        def receipts():
            with transaction(h.app.state.engine, write=True) as db:
                event_delivery.sync_receipts(db, db.get(m.EventOccurrence, occurrence['id']))
        return receipts
    occurrence = next(item for item in occurrences(h, 'staff') if item['event_id'] == row['id'])
    if writer == 'event.rsvp':
        return lambda: command(h, 'event.rsvp', {'response': 'going', 'reason': REASON}, occurrence, 'staff')
    assert writer == 'event.occurrence_update'
    return lambda: command(h, 'event.occurrence.update', {
        'value': input_of(occurrence['data'], heading='Changed occurrence'), 'reason': REASON}, occurrence)


def revisions(h):
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        return org.input_revision, org.availability_revision


@pytest.mark.postgres
@pytest.mark.parametrize('writer', WRITERS)
def test_availability_writer_stamps_once_and_apply_refuses_stale_preview(pg, monkeypatch, writer):
    h = pg
    write = operation(h, writer, monkeypatch)
    payload = people_change(h)
    check = preview(h, 'coordinator', payload)
    before = revisions(h)
    write()
    assert revisions(h) == (before[0] + 1, before[1] + 1), writer
    fresh = preview(h, 'coordinator', payload)
    assert fresh['fingerprint'] != check['fingerprint'], writer
    error = h.command('coordinator', 'bulk.apply', payload, expected_fingerprint=check['fingerprint'], status=409)['error']
    assert error['code'] == 'STALE_PREVIEW', writer


@pytest.mark.postgres
def test_pattern_expansion_is_read_only_and_matches_the_active_pattern(pg):
    from datetime import datetime
    h = pg
    row = h.command('coordinator', 'availability_pattern.propose', pattern(h.seed['people'][1]))['pattern']
    before = revisions(h)
    with transaction(h.app.state.engine) as db:
        expanded = availability_patterns.expand(db, [h.seed['department_id']], h.seed['people'],
            datetime.fromisoformat('2026-10-04T00:00:00+03:00'), datetime.fromisoformat('2026-10-05T00:00:00+03:00'),
            'Asia/Riyadh')
    assert [entry['pattern_id'] for entry in expanded] == [row['id']]
    assert revisions(h) == before
