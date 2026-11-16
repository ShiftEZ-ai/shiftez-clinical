"""Checked single-record changes shared by the generic bulk dispatcher."""
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import INTEGRITY_CONFLICT_MESSAGE, Problem
from apps.api.services.catalogue import _require_unreferenced, relationship_shortening_problem
from .governance_support import identity
from tests.outbox_support import process_one_after_undo
from datetime import timedelta
from packages.domain.time import local_midnight

# Synthetic import fixtures preserve provenance without loading a department archive.
SOURCE = 'rota-reference'

def recorded_leave(db, manifest):
    source = db.scalar(select(m.ReferenceImport).where(m.ReferenceImport.archive_sha256 == manifest['archive_sha256']))
    if not source:
        raise ValueError('Stage the directory first')
    leaves = next((f['value']['leave'] for f in manifest['files'] if f['path'] == 'content/leave.json'), [])
    if not leaves:
        return {'recorded_leave': 0}
    mappings = {r.source_key: r for r in db.scalars(select(m.SourceMapping).where(
        m.SourceMapping.department_id == source.department_id, m.SourceMapping.source_name == SOURCE))}
    provenance = db.scalar(select(m.Identity).where(m.Identity.issuer == 'urn:fmuc:reference-import', m.Identity.subject == source.id))
    if not provenance:
        provenance = m.Identity(issuer='urn:fmuc:reference-import', subject=source.id,
            display_name='Reference import provenance (not an approver)', disabled=True)
        db.add(provenance); db.flush()
    added = 0
    for index, leave in enumerate(leaves):
        key = 'leave:'+str(index)
        if key in mappings:
            continue
        begin, end = date.fromisoformat(leave['from']), date.fromisoformat(leave['to']) + timedelta(days=1)
        if end <= begin or (end - begin).days > 366:
            raise ValueError('Invalid source leave interval')
        from apps.api.services.leave_types import resolve
        from types import SimpleNamespace
        typ = resolve(db, SimpleNamespace(department_id=source.department_id), text=leave['type'])
        row = m.Availability(department_id=source.department_id,
            person_id=mappings['person:'+leave['staffId']].target_id,
            start=local_midnight(begin, 'Asia/Riyadh'), end=local_midnight(end, 'Asia/Riyadh'),
            kind='recorded_leave', leave_type=leave['type'], recorded_by=provenance.id,
            leave_type_id=typ.id, counts_as_unavailable=typ.counts_as_unavailable,
            private_reason=leave.get('note', ''))
        db.add(row); db.flush()
        db.add(m.SourceMapping(department_id=source.department_id, source_name=SOURCE,
            source_key=key, target_kind='availability', target_id=row.id))
        added += 1
    return {'recorded_leave': len(leaves), 'added': added, 'application_approvals_created': 0}

def source_aliases(db, manifest):
    from collections import defaultdict
    source = db.scalar(select(m.ReferenceImport).where(m.ReferenceImport.archive_sha256 == manifest['archive_sha256']))
    if not source:
        raise ValueError('Stage the directory first')
    mappings = {r.source_key: r for r in db.scalars(select(m.SourceMapping).where(
        m.SourceMapping.department_id == source.department_id, m.SourceMapping.source_name == SOURCE))}
    staff = next(f['value']['staff'] for f in manifest['files'] if f['path'] == 'content/staff.json')
    aliases = defaultdict(set)
    for person in staff:
        for name in [person.get('rotaName', ''), *person.get('rotaAliases', [])]:
            if name and name.strip():
                aliases[' '.join(name.split()).casefold()].add(mappings['person:' + person['id']].target_id)
    added = 0
    for alias, targets in aliases.items():
        if len(targets) != 1:
            continue
        key, target = 'alias:' + alias, next(iter(targets))
        if len(key) > 300:
            raise ValueError('Source alias exceeds the supported identifier length')
        if key in mappings:
            if mappings[key].target_id != target:
                raise ValueError('Source alias conflicts with an existing person mapping')
            continue
        db.add(m.SourceMapping(department_id=source.department_id, source_name=SOURCE,
            source_key=key, target_kind='people', target_id=target))
        added += 1
    if added:
        department = db.get(m.Department, source.department_id)
        db.get(m.Organisation, department.organisation_id).revision += 1
    return {'aliases': sum(len(v) == 1 for v in aliases.values()), 'ambiguous': sum(len(v) > 1 for v in aliases.values()), 'added': added}


def relationship(h, kind, *, starts_on='2026-10-01', ends_on='2026-11-01'):
    catalogue, key = {'membership': ('categories', 'category_id'), 'eligibility': ('services', 'service_id'),
                      'grant': ('capabilities', 'capability_id')}[kind]
    ref = h.configure(catalogue, {'name': 'Bulk dates fixture', 'starts_on': '2020-01-01'})['record']
    payload = {'person_id': h.seed['people'][0], key: ref['id'], 'starts_on': starts_on, 'ends_on': ends_on}
    if kind == 'grant':
        payload['evidence'] = 'Synthetic credential verification'
    role = 'coordinator' if kind == 'membership' else 'credentialer'
    return h.command(role, kind + '.create', payload)['record'], payload, role


def undo(h, role, value, **kwargs):
    return h.command(role, value['kind'], value['payload'], value.get('target_id'), value.get('expected_revision'), **kwargs)


@pytest.mark.parametrize('kind', ['membership', 'eligibility', 'grant'])
def test_relationship_dates_partial_edit_clear_noop_history_and_undo(h, kind):
    row, _, role = relationship(h, kind)
    changed = h.command(role, kind + '.update_dates', {'starts_on': '2026-09-01', 'ends_on': None,
                        'reason': 'Correct the relationship dates'}, row['id'], row['revision'])
    after = changed['record']
    assert after['starts_on'] == '2026-09-01' and after['ends_on'] is None and after['revision'] == 2
    same = h.command(role, kind + '.update_dates', {'ends_on': None, 'reason': 'Already correct'}, after['id'], after['revision'])
    assert same['record']['revision'] == after['revision']
    restored = undo(h, role, changed['undo'])['record']
    assert (restored['starts_on'], restored['ends_on']) == (row['starts_on'], row['ends_on'])
    undo(h, role, changed['undo'], status=409)
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id'])
                                   .order_by(m.DefinitionHistory.object_revision)))
    assert [v.object_revision for v in versions] == [1, 2, 3]
    assert [v.kind for v in versions] == [kind + '.create', kind + '.update_dates', kind + '.update_dates']


@pytest.mark.parametrize('kind', ['membership', 'eligibility', 'grant'])
def test_relationship_dates_reuse_overlap_validation_and_roles(h, kind):
    row, payload, role = relationship(h, kind)
    h.command(role, kind + '.create', {**payload, 'starts_on': '2026-12-01', 'ends_on': '2027-01-01'})
    refused = h.command(role, kind + '.update_dates', {'ends_on': '2026-12-02', 'reason': 'Would overlap'},
                        row['id'], row['revision'], status=409)
    assert refused['error']['code'] == 'DUPLICATE_RELATIONSHIP'
    assert refused['error']['message'] == 'An overlapping record already defines this relationship'
    h.command(role, kind + '.update_dates', {'ends_on': '2026-09-01', 'reason': 'Invalid interval'},
              row['id'], row['revision'], status=422)
    h.command(role, kind + '.update_dates', {'starts_on': None, 'reason': 'Start is required'},
              row['id'], row['revision'], status=422)
    other = 'credentialer' if kind == 'membership' else 'coordinator'
    h.command(other, kind + '.update_dates', {'ends_on': '2026-11-02', 'reason': 'Wrong role'},
              row['id'], row['revision'], status=403)
    if kind != 'membership':
        identity(h, 'own_credentials', ['credentialer'], person=row['person_id'])
        own = h.command('own_credentials', kind + '.update_dates', {'ends_on': '2026-11-02', 'reason': 'Own permission'},
                        row['id'], row['revision'], status=403)
        assert own['error']['code'] == 'SELF_APPROVAL'


def test_shortening_confirmation_reports_the_actual_before_and_after_dates():
    row = SimpleNamespace(starts_on=date(2026, 10, 1), ends_on=None)
    issue = relationship_shortening_problem(row, {'ends_on': '2026-12-01', 'reason': 'Dated change'})
    # Dates as the app shows them, the end as the last day included.
    assert issue.code == 'WOULD_SHORTEN' and issue.message == 'This shortens 1 Oct 2026 onwards to 1 Oct 2026 – 30 Nov 2026'
    assert issue.details['before']['ends_on'] is None
    assert issue.details['after']['ends_on'] == '2026-12-01'
    assert relationship_shortening_problem(row, {'starts_on': '2026-09-01', 'reason': 'Earlier start'}) is None
    with pytest.raises(ValueError, match='End date must follow start date'):
        relationship_shortening_problem(row, {'ends_on': '2026-09-01', 'reason': 'Invalid interval'})


def availability(h, **changes):
    return h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'recorded_leave', 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'private_reason': 'PRIVATE original explanation', **changes})['record']


def test_availability_retype_and_dates_keep_reason_and_return_checked_reverse(h):
    row = availability(h)
    typ = h.configure('leave_types', {'name': 'Assignable study', 'short_code': 'ASSTDY', 'counts_as_unavailable': False})['record']
    changed = h.command('coordinator', 'availability.update', {'kind': 'actual_absence', 'leave_type_id': typ['id'],
        'end': '2026-10-04T13:00:00+03:00', 'reason': 'Correct recorded leave type'}, row['id'], row['revision'])
    assert changed['record']['kind'] == 'actual_absence' and changed['record']['counts_as_unavailable'] is False
    assert changed['record']['manual_type_override'] is True
    assert 'PRIVATE' not in str(changed) and 'private_reason' not in changed['record']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Availability, row['id'])
        assert stored.private_reason == 'PRIVATE original explanation'
        version = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row['id']))
        assert version.snapshot['manual_type_override'] is True
    restored = undo(h, 'coordinator', changed['undo'])['record']
    assert all(restored[key] == row[key] for key in ('kind', 'start', 'end', 'leave_type_id', 'counts_as_unavailable', 'recorded_by'))
    undo(h, 'coordinator', changed['undo'], status=409)


def test_availability_date_edit_keeps_archived_type_captured_default_and_noop_revision(h):
    typ = h.configure('leave_types', {'name': 'Assignable study', 'short_code': 'ASSTDY', 'counts_as_unavailable': False})['record']
    row = availability(h, leave_type_id=typ['id'])
    from packages.contracts.schemas import LeaveTypeInput
    h.configure('leave_types', {**{k: typ[k] for k in LeaveTypeInput.model_fields}, 'active': False,
                               'counts_as_unavailable': True}, typ['id'], typ['revision'])
    same = h.command('coordinator', 'availability.update', {'end': row['end'], 'reason': 'Keep current date'}, row['id'], row['revision'])
    assert same['record']['revision'] == row['revision']
    edited = h.command('coordinator', 'availability.update', {'end': '2026-10-04T13:00:00+03:00', 'reason': 'Correct end'},
                       row['id'], row['revision'])['record']
    assert edited['counts_as_unavailable'] is False and edited['leave_type_id'] == typ['id']
    assert edited['manual_type_override'] is False


def test_availability_undo_restores_archived_type_and_original_captured_default(h):
    from packages.contracts.schemas import LeaveTypeInput
    typ = h.configure('leave_types', {'name': 'Originally assignable leave', 'short_code': 'ORIGNL', 'counts_as_unavailable': False})['record']
    row = availability(h, leave_type_id=typ['id'], leave_type='Original imported label')
    h.configure('leave_types', {**{k: typ[k] for k in LeaveTypeInput.model_fields}, 'active': False,
                               'counts_as_unavailable': True}, typ['id'], typ['revision'])
    other = next(t for t in h.get('staff', '/api/leave-types')['leave_types'] if t['short_code'] == 'STUDY')
    changed = h.command('coordinator', 'availability.update', {'leave_type_id': other['id'], 'reason': 'Retype historical leave'},
                        row['id'], row['revision'])
    assert changed['undo']['kind'] == 'availability.undo_update'
    assert changed['previous_snapshot']['counts_as_unavailable'] is False
    assert 'PRIVATE' not in str(changed)
    restored = undo(h, 'coordinator', changed['undo'])
    fields = ('kind', 'start', 'end', 'leave_type_id', 'counts_as_unavailable', 'manual_type_override', 'leave_type', 'recorded_by')
    assert all(restored['record'][key] == row[key] for key in fields)
    redone = undo(h, 'coordinator', restored['undo'])['record']
    assert all(redone[key] == changed['record'][key] for key in fields)


def test_receipt_backed_availability_undo_refuses_tampering_other_owners_and_newer_data(h):
    row = availability(h)
    changed = h.command('coordinator', 'availability.update', {'end': '2026-10-04T13:00:00+03:00', 'reason': 'Correct end'},
                        row['id'], row['revision'])
    inverse = changed['undo']
    h.command('coordinator', inverse['kind'], {**inverse['payload'], 'previous_snapshot': {'counts_as_unavailable': False}},
              row['id'], changed['record']['revision'], status=422)
    h.command('coordinator', inverse['kind'], {**inverse['payload'], 'operation_id': '00000000-0000-0000-0000-000000000000'},
              row['id'], changed['record']['revision'], status=404)
    identity(h, 'other_coordinator', ['coordinator'])
    undo(h, 'other_coordinator', inverse, status=404)
    later = h.command('coordinator', 'availability.update', {'end': '2026-10-04T14:00:00+03:00', 'reason': 'Later correction'},
                       row['id'], changed['record']['revision'])['record']
    undo(h, 'coordinator', inverse, status=409)
    h.command('coordinator', inverse['kind'], inverse['payload'], row['id'], later['revision'], status=409)


def test_bulk_availability_preview_and_receipt_backed_undo_roundtrip(h):
    row = availability(h)
    payload = {'item_kind': 'availability.update', 'items': [{'target_id': row['id'], 'expected_revision': row['revision'],
               'changes': [{'field': 'kind', 'value': 'actual_absence'},
                           {'field': 'end', 'value': '2026-10-04T13:00:00+03:00'}]}], 'reason': 'Bulk absence corrections'}
    preview = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['ready'] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, row['id']).revision == row['revision']
    result = h.command('coordinator', 'bulk.apply', payload)
    inverse = result['undo']
    assert inverse['payload']['items'][0]['item_kind'] == 'availability.undo_update'
    undo(h, 'coordinator', inverse)
    with transaction(h.app.state.engine) as db:
        restored = db.get(m.Availability, row['id'])
        assert restored.kind == row['kind'] and not restored.manual_type_override
        assert restored.private_reason == 'PRIVATE original explanation'


@pytest.mark.parametrize('payload', [{'end': '2026-10-04T08:00:00+03:00'}, {'end': '2028-10-04T12:00:00+03:00'},
                                    {'kind': 'off_duty', 'leave_type_id': 'unknown'}, {'private_reason': 'overwrite private note'}])
def test_availability_update_validates_merged_values(h, payload):
    row = availability(h)
    h.command('coordinator', 'availability.update', {**payload, 'reason': 'Invalid edit'}, row['id'], row['revision'], status=422)


def test_availability_update_requires_coordinator_and_independence(h):
    row = availability(h)
    payload = {'end': '2026-10-04T13:00:00+03:00', 'reason': 'Correct end'}
    h.command('credentialer', 'availability.update', payload, row['id'], row['revision'], status=403)
    identity(h, 'own_absence', ['coordinator'], person=row['person_id'])
    refusal = h.command('own_absence', 'availability.update', payload, row['id'], row['revision'], status=403)
    assert refusal['error']['code'] == 'SELF_APPROVAL'


def test_imported_availability_manual_type_survives_reimport_with_provenance(h):
    manifest = {'archive_sha256': 'a' * 64, 'files': [{'path': 'content/leave.json', 'value': {'leave': [
        {'staffId': 'fixture-person', 'from': '2026-10-04', 'to': '2026-10-05', 'type': 'Other', 'note': 'PRIVATE source note'}]}}]}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.ReferenceImport(department_id=h.seed['department_id'], archive_sha256=manifest['archive_sha256'],
                                 source_name=SOURCE, manifest=manifest))
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE, source_key='person:fixture-person',
                              target_kind='people', target_id=h.seed['people'][0]))
        db.flush()
        assert recorded_leave(db, manifest)['added'] == 1
        mapping = db.scalar(select(m.SourceMapping).where(m.SourceMapping.source_key == 'leave:0'))
        row = db.get(m.Availability, mapping.target_id)
        original = (row.id, row.recorded_by, row.private_reason, mapping.id)
    study = next(t for t in h.get('staff', '/api/leave-types')['leave_types'] if t['short_code'] == 'STUDY')
    edited = h.command('coordinator', 'availability.update', {'leave_type_id': study['id'], 'reason': 'Correct imported type'}, original[0], 1)['record']
    manifest['files'][0]['value']['leave'][0]['type'] = 'Annual'
    with transaction(h.app.state.engine, write=True) as db:
        assert recorded_leave(db, manifest)['added'] == 0
        row = db.get(m.Availability, original[0])
        mapping = db.scalar(select(m.SourceMapping).where(m.SourceMapping.source_key == 'leave:0'))
        assert (row.id, row.recorded_by, row.private_reason, mapping.id) == original
        assert row.leave_type_id == study['id'] and row.manual_type_override and row.revision == edited['revision']
        assert row.leave_type == 'Other'


def test_retract_and_revoke_have_revision_bound_single_record_reverses(h):
    absence = availability(h)
    process_one_after_undo(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Outbox.state).where(
            m.Outbox.event_key == 'availability:' + absence['id'])) == 'available_in_app'
    retracted = h.command('coordinator', 'availability.retract', {'reason': 'Correct mistaken record'}, absence['id'], absence['revision'])
    identity(h, 'own_restore', ['coordinator', 'credentialer'], person=absence['person_id'])
    assert undo(h, 'own_restore', retracted['undo'], status=403)['error']['code'] == 'SELF_APPROVAL'
    restored_absence = undo(h, 'coordinator', retracted['undo'])
    assert restored_absence['record']['retracted'] is False
    retracted_again = undo(h, 'coordinator', restored_absence['undo'])['record']
    assert retracted_again['retracted'] is True
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.Outbox).where(m.Outbox.event_key.like('availability-retracted:' + absence['id'] + '%'))))
        keys = [event.event_key for event in events]
        assert len(keys) == 2 and len(set(keys)) == 2
        assert {event.state for event in events} == {'superseded', 'queued'}
    grant, _, role = relationship(h, 'grant')
    revoked = h.command(role, 'grant.revoke', {'ends_on': '2026-10-15', 'reason': 'Mistaken revocation'}, grant['id'], grant['revision'])
    assert undo(h, 'own_restore', revoked['undo'], status=403)['error']['code'] == 'SELF_APPROVAL'
    restored = undo(h, role, revoked['undo'])['record']
    assert not restored['revoked'] and restored['ends_on'] == grant['ends_on']
    undo(h, role, revoked['undo'], status=409)


@pytest.mark.parametrize('changes,status', [
    ({'ends_on': '2026-10-15'}, 409),
    ({'ends_on': '2026-10-20'}, 409),
    ({'starts_on': '2026-09-01', 'ends_on': '2026-10-20'}, 409),
    ({'ends_on': None}, 409),
], ids=['same_dates', 'later_end', 'moved_start', 'cleared_end'])
def test_revoke_already_revoked_grant_preserves_dates_and_revision(h, changes, status):
    grant, _, role = relationship(h, 'grant')
    revoked = h.command(role, 'grant.revoke', {'ends_on': '2026-10-15', 'reason': 'Revoke invalid evidence'},
                        grant['id'], grant['revision'])['record']
    refused = h.command(role, 'grant.revoke', {**changes, 'reason': 'Repeat revocation'},
                        revoked['id'], revoked['revision'], status=status)
    if status == 409:
        assert refused['error']['code'] == 'WORKFLOW_STATE'
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CapabilityGrant, grant['id'])
        assert (row.starts_on.isoformat(), row.ends_on.isoformat(), row.revoked, row.revision) == (
            revoked['starts_on'], revoked['ends_on'], True, revoked['revision'])
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row.id)
                                 .order_by(m.DefinitionHistory.object_revision)))
        assert [version.kind for version in history] == ['grant.create', 'grant.revoke']


def test_catalogue_bulk_update_returns_one_atomic_bulk_undo(h):
    rows = h.get('admin', '/api/catalogues')['services'][:2]
    changed = h.command('admin', 'catalogue.bulk_update', {'catalogue': 'services',
        'items': [{'id': row['id'], 'revision': row['revision']} for row in rows],
        'set': {'description': 'Bulk edit fixture'}, 'reason': 'Shared description'})
    assert changed['undo']['kind'] == 'bulk.apply'
    assert len(changed['undo']['payload']['items']) == 2
    undo(h, 'admin', changed['undo'])
    after = {row['id']: row for row in h.get('admin', '/api/catalogues')['services']}
    assert all(after[row['id']]['description'] == row['description'] for row in rows)


def test_create_undo_requires_creator_and_unchanged_revision(h):
    row, _, role = relationship(h, 'membership')
    identity(h, 'other_creator', ['admin'])
    h.command('other_creator', 'membership.undo_create', {'items': [{'id': row['id'], 'revision': row['revision']}],
                                                        'reason': 'Wrong creator'}, status=403)
    h.command(role, 'membership.undo_create', {'items': [{'id': row['id'], 'revision': row['revision']}], 'reason': 'Undo creation'})
    created = h.configure('services', {'name': 'Unreferenced temporary clinic', 'starts_on': '2020-01-01'})['record']
    h.command('other_creator', 'catalogue.undo_create', {'catalogue': 'services', 'reason': 'Wrong creator'},
              created['id'], created['revision'], status=403)
    h.command('admin', 'catalogue.undo_create', {'catalogue': 'services', 'reason': 'Undo creation'}, created['id'], created['revision'])
    assert created['id'] not in {row['id'] for row in h.get('admin', '/api/catalogues')['services']}
    changed, _, role = relationship(h, 'membership')
    edited = h.command(role, 'membership.update_dates', {'ends_on': '2026-11-02', 'reason': 'Later edit'},
                       changed['id'], changed['revision'])['record']
    for revision in (changed['revision'], edited['revision']):
        h.command(role, 'membership.undo_create', {'items': [{'id': changed['id'], 'revision': revision}],
                                                 'reason': 'Changed creation'}, status=409)


def test_referenced_catalogue_creation_undo_is_a_row_issue_and_apply_is_atomic(h):
    unused = h.configure('services', {'name': 'Unused creation fixture', 'starts_on': '2020-01-01'})['record']
    used = h.configure('services', {'name': 'Referenced creation fixture', 'starts_on': '2020-01-01'})['record']
    permission = h.command('credentialer', 'eligibility.create', {'person_id': h.seed['people'][0], 'service_id': used['id'],
        'starts_on': '2026-10-01', 'ends_on': None})['record']
    payload = {'item_kind': 'catalogue.undo_create', 'common': {'catalogue': 'services'},
        'items': [{'target_id': row['id'], 'expected_revision': row['revision'], 'payload': {}} for row in (unused, used)],
        'reason': 'Undo both clinic creations'}
    response = h.client('admin').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert [(row['result'], [issue['code'] for issue in row['issues']]) for row in preview['items']] == [
        ('ready', []), ('problem', ['INTEGRITY_CONFLICT'])]
    assert preview['counts']['ready'] == 1 and preview['counts']['problem'] == 1
    issue = preview['items'][1]['issues'][0]
    assert issue['code'] == 'INTEGRITY_CONFLICT' and issue['message'] == INTEGRITY_CONFLICT_MESSAGE
    refused = h.command('admin', 'bulk.apply', payload, status=409)['error']
    assert refused['code'] == 'INTEGRITY_CONFLICT'
    assert refused['details']['failed_index'] == 1 and refused['details']['completed'] == 0
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Service, unused['id']).revision == unused['revision']
        assert db.get(m.Service, used['id']).revision == used['revision']
        assert db.get(m.Eligibility, permission['id']).service_id == used['id']
        histories = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id.in_([unused['id'], used['id']]))))
        assert len(histories) == 2


def test_creation_undo_reference_guard_checks_unflushed_children(h):
    service = h.configure('services', {'name': 'Pending reference fixture', 'starts_on': '2020-01-01'})['record']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Service, service['id'])
        child = m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0], service_id=row.id,
                              starts_on=date(2026, 10, 1))
        db.add(child)
        with db.no_autoflush, pytest.raises(Problem) as caught:
            _require_unreferenced(db, row)
        assert caught.value.code == 'INTEGRITY_CONFLICT' and child in db.new
        db.expunge(child)


@pytest.mark.parametrize('kind', ['membership', 'eligibility', 'grant'])
def test_bulk_relationship_peer_cache_includes_earlier_creations_and_preview_rollback(h, kind):
    original, payload, role = relationship(h, kind)
    payload = {**payload, 'starts_on': '2026-12-01', 'ends_on': '2027-01-01'}
    batch = {'item_kind': kind + '.create', 'items': [{'payload': payload}, {'payload': payload}],
             'reason': 'Duplicate relationship creation fixture'}
    response = h.client(role).post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': batch})
    assert response.status_code == 200, response.text
    assert response.json()['counts']['ready'] == 1 and response.json()['counts']['problem'] == 1
    assert response.json()['items'][1]['issues'][0]['code'] == 'DUPLICATE_RELATIONSHIP'
    refused = h.command(role, 'bulk.apply', batch, status=409)['error']
    assert refused['code'] == 'DUPLICATE_RELATIONSHIP' and refused['details']['failed_index'] == 1
    cls, key = {'membership': (m.Membership, 'category_id'), 'eligibility': (m.Eligibility, 'service_id'),
                'grant': (m.CapabilityGrant, 'capability_id')}[kind]
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(cls).where(cls.person_id == original['person_id'], getattr(cls, key) == payload[key])))
        assert len(rows) == 1 and rows[0].id == original['id']


def test_bulk_people_share_one_new_department_tag_and_keep_per_person_history(h):
    people = h.get('coordinator', '/api/catalogues')['people'][:2]
    payload = {'item_kind': 'catalogue.update', 'common': {'catalogue': 'people'},
        'items': [{'target_id': person['id'], 'expected_revision': person['revision'],
                   'changes': [{'field': 'tags', 'how': 'add', 'value': 'Shared bulk team'}]} for person in people],
        'reason': 'Label a team together'}
    response = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    assert response.json()['counts']['ready'] == 2
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.Tag).where(m.Tag.name == 'Shared bulk team'))) == []
    result = h.command('coordinator', 'bulk.apply', payload)
    with transaction(h.app.state.engine) as db:
        tags = list(db.scalars(select(m.Tag).where(m.Tag.department_id == h.seed['department_id'], m.Tag.name == 'Shared bulk team')))
        assert len(tags) == 1
        members = list(db.scalars(select(m.TagMember).where(m.TagMember.tag_id == tags[0].id)))
        assert {member.member_id for member in members} == {person['id'] for person in people}
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.batch_id == result['batch_id'])))
        assert len([version for version in versions if version.kind == 'tags' and version.object_id == tags[0].id]) == 1
        assert len([version for version in versions if version.kind == 'tag.member_add']) == 2
        assert len([version for version in versions if version.kind == 'people']) == 2


@pytest.mark.parametrize('operations,expected_members,expected_events', [
    (['add', 'add'], 1, ['tag.member_add']),
    (['add', 'remove'], 0, ['tag.member_add', 'tag.member_remove']),
])
def test_bulk_tag_members_sees_pending_adds_and_removals_with_history(h, operations, expected_members, expected_events):
    tag = h.configure('tags', {'name': 'Shared clinic group', 'labels': ['services']}, role='coordinator')['record']
    service_id = h.seed['services'][0]
    payload = {'item_kind': 'tag.members', 'items': [
        {'target_id': tag['id'], 'expected_revision': tag['revision'],
         'payload': {operation: [{'kind': 'services', 'id': service_id}]}} for operation in operations],
        'reason': 'Compose clinic tag changes'}
    response = h.client('coordinator').post('/api/commands/preview', json={'kind': 'bulk.apply', 'payload': payload})
    assert response.status_code == 200, response.text
    assert response.json()['counts']['problem'] == 0
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.TagMember).where(m.TagMember.tag_id == tag['id']))) == []
    result = h.command('coordinator', 'bulk.apply', payload)
    assert result['counts']['changed'] == len(expected_events)
    assert result['counts']['unchanged'] == len(operations) - len(expected_events)
    with transaction(h.app.state.engine) as db:
        members = list(db.scalars(select(m.TagMember).where(m.TagMember.tag_id == tag['id'])))
        assert len(members) == expected_members
        assert all(member.kind == 'services' and member.member_id == service_id for member in members)
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.batch_id == result['batch_id'])
                                  .order_by(m.DefinitionHistory.object_revision)))
        member_versions = [version for version in versions if version.kind.startswith('tag.member_')]
        assert [version.kind for version in member_versions] == expected_events
        assert all(version.snapshot['member_id'] == service_id for version in member_versions)
        assert len({version.object_id for version in member_versions}) == 1
        assert db.get(m.Tag, tag['id']).revision == tag['revision'] + len(expected_events)


@pytest.mark.parametrize('kind', ['membership', 'eligibility', 'grant', 'availability'])
def test_new_updates_never_cross_the_department_boundary(h, kind):
    if kind == 'availability':
        row, role = availability(h), 'coordinator'
        cls, action, payload = m.Availability, 'update', {'end': '2026-10-04T13:00:00+03:00'}
    else:
        row, _, role = relationship(h, kind)
        cls = {'membership': m.Membership, 'eligibility': m.Eligibility, 'grant': m.CapabilityGrant}[kind]
        action, payload = 'update_dates', {'ends_on': '2026-11-02'}
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign department')
        db.add(department)
        db.flush()
        db.get(cls, row['id']).department_id = department.id
    h.command(role, kind + '.' + action, {**payload, 'reason': 'Foreign target'}, row['id'], row['revision'], status=404)
