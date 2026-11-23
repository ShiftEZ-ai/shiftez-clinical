"""Selective timeline reads preserve the stored-check and permission boundaries."""
from datetime import date

import pytest

from apps.api import models as m
from apps.api.db import bump_snapshot_input_revisions, transaction
from apps.api.settings import quota_policy_limit
from packages.contracts import schemas as s
from packages.domain.input_manifest import digest, toolchain
from packages.domain.quotas import METRIC_VERSION
from packages.domain.time import aware
from tests.test_timeline_data import END, START, actor_for, read, timeline_records


def linked_absence(h):
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).leave_reason_display = 'type'
        report = m.RequestRecord(department_id=h.seed['department_id'], person_id=person, kind='absence_report',
            start=aware('2026-10-04T08:00:00+03:00'), end=aware('2026-10-04T12:00:00+03:00'),
            private_reason='Legacy report reason', status='approved', created_by=h.seed['identities']['staff'],
            details={'reporter_category': 'sick', 'note': 'Private report note'})
        db.add(report); db.flush()
        db.add(m.Availability(department_id=h.seed['department_id'], person_id=person, kind='approved_leave',
            start=report.start, end=report.end, leave_type='', private_reason='',
            recorded_by=h.seed['identities']['coordinator'], request_id=report.id))
    return person


def assert_linked_absence_label(body, person):
    row = next(p for p in body['people'] if p['id'] == person)
    leave = next(item for item in row['items'] if item['kind'] == 'leave')
    from packages.contracts.schemas import ABSENCE_CATEGORY_LABELS
    assert leave['label'] == ABSENCE_CATEGORY_LABELS['sick']  # the contract's display label, never the raw key


def test_timeline_linked_absence_has_a_label_fallback(h):
    person = linked_absence(h)
    body = h.get('coordinator', f'/api/timeline?start=2026-10-01&end=2026-10-10&layers=leave&person_ids={person}')
    assert_linked_absence_label(body, person)


def test_work_linked_absence_has_a_label_fallback(h):
    person = linked_absence(h)
    body = h.get('coordinator', f'/api/people/{person}/timeline?start=2026-10-01&end=2026-10-10&layers=leave')
    assert_linked_absence_label(body, person)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_timeline_layer_contract_and_empty_selection(h, monkeypatch):
    from apps.api.services import timeline
    ids = timeline_records(h)
    def forbidden(*args, **kwargs):
        pytest.fail('A timeline read must never acquire the full rule snapshot or run validation')
    monkeypatch.setattr(timeline, '_check_data', forbidden)
    monkeypatch.setattr(timeline, 'evaluate', forbidden)
    path = f'/api/people/{ids["person"]}/timeline?start=2026-10-01&end=2026-11-01'
    default = h.get('coordinator', path)
    assert not any(i['kind'] == 'eligibility' for i in default['people'][0]['items'])
    assert not next(l for l in default['layers'] if l['kind'] == 'eligibility')['requested']
    for selected in ('rotation', 'eligibility', 'leave', 'protected_time', 'academic', 'pending', '', 'none'):
        body = h.get('coordinator', path + '&layers=' + selected)
        s.TimelineResponse.model_validate(body)
        assert {l['kind'] for l in body['layers'] if l['requested']} == ({selected} if selected and selected != 'none' else set())
        assert all(l['count'] == 0 for l in body['layers'] if not l['requested'])
        items = body['people'][0]['items']
        assert {i['kind'] for i in items} == ({selected} if selected and selected != 'none' else set())
        assert body['people'][0]['conflicts'] == body['people'][0]['duty_density'] == []
    bulk = h.get('coordinator', '/api/timeline?start=2026-10-01&end=2026-11-01&layers=rotation&person_ids=' + ids['person'])
    single = h.get('coordinator', path + '&layers=rotation')
    assert bulk['people'] == single['people'] and bulk['layers'] == single['layers']
    error = h.get('coordinator', path + '&layers=not_a_layer', status=422)
    assert error['error']['code'] == 'TIMELINE_LAYERS'


def stored_publication(h, *, stale=None, findings=None):
    with transaction(h.app.state.engine, write=True) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        workspace = m.Workspace(department_id=h.seed['department_id'], name='Synthetic stored timeline check',
            starts_on=START, ends_on=END, status='published', created_by=h.seed['identities']['coordinator'],
            last_edited_by=h.seed['identities']['coordinator'])
        db.add(workspace); db.flush()
        # Reserve the publication insert's stamp before sealing its immutable evidence.
        bump_snapshot_input_revisions(db, [org.id])
        dirty = {'input_revision': org.input_revision, 'availability_revision': org.availability_revision,
                 'content_revision': workspace.content_revision}
        manifest = {'dirty_hint': dirty, 'components': {'toolchain': digest(toolchain(quota_policy_limit()))}}
        if stale in dirty:
            dirty[stale] -= 1
        if stale == 'toolchain':
            manifest['components']['toolchain'] = 'old-engine'
        if stale == 'legacy':
            manifest = {}
        if findings is None:
            findings = [{'code': 'BALANCE_GOAL', 'message': 'Synthetic stored advisory finding', 'severity': 'warning',
                'person_id': pid, 'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00', 'detail': {}}
                for pid in h.seed['people'][:2]]
        db.add(m.Publication(department_id=h.seed['department_id'], workspace_id=workspace.id, sequence=1,
            starts_on=START, ends_on=END, published_by=h.seed['identities']['coordinator'],
            snapshot={}, validation={'contracts': {'block_quota': METRIC_VERSION, 'programmes': 'programmes-v1'},
                                     'input_manifest': manifest, 'findings': findings}, fingerprint='0' * 64))
    return findings


@pytest.mark.parametrize('stale', [None, 'input_revision', 'availability_revision', 'content_revision', 'toolchain', 'legacy'])
def test_timeline_findings_are_only_current_stored_evidence(h, monkeypatch, stale):
    from apps.api.services import timeline
    findings = stored_publication(h, stale=stale)
    monkeypatch.setattr(timeline, 'evaluate', lambda *a, **kw: pytest.fail('timeline ran validation'))
    full = read(h, layers='conflict')
    selected = read(h, layers='conflict', person_ids=[h.seed['people'][0]])
    expected = findings[:1] if stale is None else []
    assert selected['people'][0]['conflicts'] == expected
    assert next(p for p in full['people'] if p['id'] == h.seed['people'][0])['conflicts'] == expected
    assert all(not p['conflicts'] for p in read(h, layers='rotation')['people'])
    with transaction(h.app.state.engine) as db:
        outside = timeline.list_data(db, actor_for(h, db),
            date(2026, 10, 6), END, layers='conflict')
    assert all(not p['conflicts'] for p in outside['people'])


def test_timeline_hidden_layers_do_not_change_people_filter_semantics(h):
    ids = timeline_records(h)
    filt = s.CollectionFilter.model_validate({'conditions': {'conditions': [
        {'field': 'eligible_service_ids', 'values': [h.seed['services'][0]]}]}}, context={'table_key': 'people'})
    full = read(h, collection_filter=filt, layers='eligibility')
    empty = read(h, collection_filter=filt, layers='')
    assert ids['person'] in {p['id'] for p in empty['people']}
    assert {p['id'] for p in full['people']} == {p['id'] for p in empty['people']}
