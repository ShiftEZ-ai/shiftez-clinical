"""Notifications and imported notes use operator copy without losing source evidence."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from tests.outbox_support import process_one_after_undo as process_one
from tests.test_bulk_single_commands import SOURCE, recorded_leave, source_aliases
from .test_event_delivery import notes, scheduled


def test_event_without_description_names_requests(h):
    _, [occurrence_id] = scheduled(h)
    with transaction(h.app.state.engine, write=True) as db:
        occurrence = db.get(m.EventOccurrence, occurrence_id)
        occurrence.data = {**occurrence.data, 'description': ''}
    process_one(h.app.state.engine)
    [notification] = notes(h)
    assert notification['message'] == 'Open Requests for the details.'
    assert notification['source_kind'] == 'event' and notification['source_id'] == occurrence_id


@pytest.mark.parametrize('note', ['', 'Synthetic leave detail'])
def test_new_leave_import_keeps_note_separate_from_provenance(h, note):
    manifest = {'archive_sha256': 'a' * 64, 'files': [{'path': 'content/leave.json', 'value': {'leave': [
        {'staffId': 'synthetic-person', 'from': '2026-10-04', 'to': '2026-10-04', 'type': 'Annual', 'note': note}]}}]}
    with transaction(h.app.state.engine, write=True) as db:
        source = m.ReferenceImport(department_id=h.seed['department_id'], archive_sha256=manifest['archive_sha256'],
                                   source_name=SOURCE, manifest=manifest)
        db.add(source)
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE,
            source_key='person:synthetic-person', target_kind='people', target_id=h.seed['people'][0]))
        db.flush()
        assert recorded_leave(db, manifest)['added'] == 1
        db.flush()
        absence = db.scalar(select(m.Availability).where(m.Availability.department_id == h.seed['department_id']))
        mapping = db.scalar(select(m.SourceMapping).where(m.SourceMapping.source_key == 'leave:0'))
        importer = db.get(m.Identity, absence.recorded_by)
        assert absence.private_reason == note
        assert mapping.target_kind == 'availability' and mapping.target_id == absence.id
        assert importer.disabled and importer.issuer == 'urn:fmuc:reference-import' and importer.subject == source.id
        assert recorded_leave(db, manifest)['added'] == 0
        assert db.scalar(select(m.Availability.private_reason).where(m.Availability.id == absence.id)) == note
        assert db.scalar(select(m.RequestRecord.id)) is None


def test_imported_alias_invalidates_only_its_organisation_and_refreshes_people(h):
    manifest = {'archive_sha256': 'b' * 64, 'files': [{'path': 'content/staff.json', 'value': {'staff': [
        {'id': 'synthetic-person', 'rotaName': 'Synthetic imported alias'}]}}]}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.ReferenceImport(department_id=h.seed['department_id'], archive_sha256=manifest['archive_sha256'],
                                 source_name=SOURCE, manifest=manifest))
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name=SOURCE,
            source_key='person:synthetic-person', target_kind='people', target_id=h.seed['people'][0]))
        other = m.Organisation(name='Other synthetic organisation')
        db.add(other)
        db.flush()
        other_id, other_revision = other.id, other.revision
        org = db.get(m.Organisation, h.seed['organisation_id'])
        revision, inputs = org.revision, org.input_revision
    before = h.get('coordinator', '/api/catalogues')
    with transaction(h.app.state.engine, write=True) as db:
        assert source_aliases(db, manifest)['added'] == 1
        org = db.get(m.Organisation, h.seed['organisation_id'])
        assert (org.revision, org.input_revision) == (revision + 1, inputs)
        assert db.get(m.Organisation, other_id).revision == other_revision
        assert source_aliases(db, manifest)['added'] == 0
        assert org.revision == revision + 1
    after = h.get('coordinator', '/api/catalogues?known=' + ','.join(before['_versions'].values()))
    assert after['_versions']['people'] != before['_versions']['people']
    person = next(row for row in after['people'] if row['id'] == h.seed['people'][0])
    assert 'synthetic imported alias' in person['aliases']
