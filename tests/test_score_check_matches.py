"""Confident matches expose their original evidence and reuse audited import decisions."""
import copy

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import func, select, update

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import score_check
from packages.contracts import schemas as s
from .governance_support import identity
from .test_score_check_frontend_api import change, check, current, decide, upload


def matches(h, value, query='', role='coordinator', status=200):
    return h.get(role, '/api/score-imports/' + value['id'] + '/matches' + query, status=status)


def test_confident_matches_are_paginated_with_original_evidence_and_no_changes(h):
    value = upload(h)
    before = check(h, value)
    first = matches(h, value, '?limit=1')
    second = matches(h, value, '?limit=1&offset=1')
    assert s.ImportMatches.model_validate(first).import_id == value['id']
    assert set(first) == {'import_id', 'revision', 'items', 'total', 'offset', 'limit', 'has_more'}
    assert first['total'] == second['total'] == before['matched'] == 2
    assert first['revision'] == value['revision'] and first['offset'] == 0 and first['limit'] == 1
    assert first['has_more'] and not second['has_more']
    assert first == matches(h, value, '?limit=1')
    items = first['items'] + second['items']
    assert {item['kind'] for item in items} == {'person', 'duty_code'}
    assert len({item['id'] for item in items}) == 2
    assert not {item['id'] for item in items} & {item['id'] for item in before['items']}
    with transaction(h.app.state.engine) as db:
        source = db.get(m.ImportSource, value['sources'][0]['id'])
        for item in items:
            assert item['as_written'] == item['matched']['label']
            assert item['confidence'] == 1
            assert item['source_id'] == source.id and item['file_name'] == source.file_name
            assert item['file_ref'] == source.file_ref
            assert item['cell_refs'] and all(ref.startswith('CSV!') for ref in item['cell_refs'])
            assert 'content' not in item and 'extraction' not in item
        assert db.scalar(select(func.count()).select_from(m.ScoreCheckGapDecision)) == 0
    assert matches(h, value, '?offset=100')['items'] == []
    assert matches(h, value, '?offset=100')['has_more'] is False
    assert current(h, value) == value


@pytest.mark.parametrize('kind', ['person', 'duty_code', 'clinic'])
def test_any_confident_match_can_be_overridden_and_undone_using_decide(h, kind):
    value = upload(h)
    if kind == 'clinic':
        with transaction(h.app.state.engine, write=True) as db:
            source = db.get(m.ImportSource, value['sources'][0]['id'])
            extraction = copy.deepcopy(source.extraction)
            extraction['clinics'][0]['suggested_location_id'] = h.seed['locations'][0]
            source.extraction = extraction
            score_check.refresh(db, db.get(m.ScoreCheckImportBatch, value['id']))
    before = matches(h, value)
    item = next(item for item in before['items'] if item['kind'] == kind)
    if kind == 'person':
        target = next(person['id'] for person in h.get('coordinator', '/api/catalogues')['people']
                      if person['display_name'] == 'Staff 02(test)')
    else:
        catalogue = 'assignment_types' if kind == 'duty_code' else 'services'
        extra = {'definition': {}} if kind == 'duty_code' else {}
        target = h.command('admin', 'catalogue.create', {'catalogue': catalogue,
            'value': {'name': 'Alternative reviewed match', 'starts_on': '2020-01-01', **extra},
            'reason': 'Synthetic match override coverage'})['record']['id']
    decision = {'kind': 'match', 'target_id': target}
    if kind == 'clinic':
        decision['location_id'] = h.seed['locations'][0]
    result = decide(h, value, item, decision)
    assert next(row for row in result['items'] if row['id'] == item['id'])['decision']['kind'] == 'match'
    assert matches(h, value)['total'] == before['total'] - 1
    value = current(h, value)
    assert change(h, value, 'decide', item_id=item['id'], decision=decision, status=409)['error']['code'] == 'GAP_DECIDED'
    result = decide(h, value, item, None)
    assert item['id'] not in {row['id'] for row in result['items']}
    assert matches(h, value)['items'] == before['items']
    with transaction(h.app.state.engine) as db:
        saved = db.scalar(select(m.ScoreCheckGapDecision).where(m.ScoreCheckGapDecision.gap_id == item['id']))
        assert saved.target_id == target and saved.undone_at is not None
        assert saved.created_by == saved.undone_by == h.seed['identities']['coordinator']
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == 'score_import.decide', m.AuditEvent.target_id == value['id'])) == 2


def test_confident_match_leave_out_and_undo_restore_the_automatic_count(h):
    value = upload(h)
    before = matches(h, value)
    item = next(row for row in before['items'] if row['kind'] == 'person')
    decided = decide(h, value, item, {'kind': 'leave_out'})
    assert next(row for row in decided['items'] if row['id'] == item['id'])['decision']['kind'] == 'leave_out'
    assert matches(h, value)['total'] == before['total'] - 1
    value = current(h, value)
    decide(h, value, item, None)
    assert matches(h, value)['items'] == before['items']


def test_confident_match_review_and_override_are_department_scoped_and_coordinator_only(h):
    value = upload(h)
    item = matches(h, value)['items'][0]
    assert matches(h, value, role='admin')['total'] == 2
    for role in s.ROLE_NAMES:
        if role in {'coordinator', 'admin'}:
            continue
        if role not in h.seed['identities']:
            identity(h, role, [role])
        matches(h, value, role=role, status=403)
        change(h, value, 'decide', item_id=item['id'], decision={'kind': 'leave_out'}, role=role, status=403)
    with transaction(h.app.state.engine, write=True) as db:
        other = m.Department(organisation_id=h.seed['organisation_id'], name='Other match department')
        db.add(other)
        db.flush()
        other_id = other.id
    identity(h, 'other-match-coordinator', ['coordinator'], department=other_id)
    matches(h, value, role='other-match-coordinator', status=404)
    change(h, value, 'decide', item_id=item['id'], decision={'kind': 'leave_out'},
           role='other-match-coordinator', status=404)
    another = upload(h)
    change(h, another, 'decide', item_id=item['id'], decision={'kind': 'leave_out'}, status=404)


def test_confident_match_page_limits_and_removed_sources(h):
    value = upload(h)
    for query in ('?limit=0', '?limit=101', '?offset=-1', '?offset=1000001'):
        matches(h, value, query, status=422)
    value = change(h, value, 'remove_source', source_id=value['sources'][0]['id'])
    assert matches(h, value) == {'import_id': value['id'], 'revision': value['revision'], 'items': [],
                                'total': 0, 'offset': 0, 'limit': 50, 'has_more': False}


def test_source_removed_between_match_snapshot_and_filename_read_keeps_page_consistent(h, monkeypatch):
    value = upload(h)
    source = value['sources'][0]
    snapshot = score_check._gap_state

    def removal_after_snapshot(db, batch):
        captured = snapshot(db, batch)
        db.execute(update(m.ImportSource).where(m.ImportSource.id == source['id']).values(
            removed_at=m.now()).execution_options(synchronize_session=False))
        db.execute(update(m.ScoreCheckImportBatch).where(m.ScoreCheckImportBatch.id == batch.id).values(
            gaps=[], revision=batch.revision + 1).execution_options(synchronize_session=False))
        return captured

    monkeypatch.setattr(score_check, '_gap_state', removal_after_snapshot)
    page = matches(h, value)
    assert page['revision'] == value['revision'] and page['total'] == 2
    assert {item['file_name'] for item in page['items']} == {source['file_name']}
    monkeypatch.setattr(score_check, '_gap_state', snapshot)
    fresh = matches(h, value)
    assert fresh['revision'] == value['revision'] + 1 and fresh['total'] == 0 and fresh['items'] == []


def test_person_matches_use_duty_dates_without_a_selected_period(h):
    from datetime import date

    future, undated, _ = h.seed['people']
    with transaction(h.app.state.engine, write=True) as db:
        person = db.get(m.Person, future)
        person.active, person.inactive_from = False, date(2026, 10, 6)
        legacy = db.get(m.Person, undated)
        legacy.active = False
        # Keep fuzzy name suggestions separate from this test of date eligibility.
        person.display_name = 'Synthetic cutover'
        legacy.display_name = 'Legacy example'
        labels = {future: person.display_name, undated: legacy.display_name}
    for day, expected in (('2026-10-05', future), ('2026-10-06', None)):
        value = upload(h, [[label, day, 'Clinical review session', 'Continuity clinic', '08:00', '12:00']
                           for label in labels.values()])
        with transaction(h.app.state.engine) as db:
            source = db.get(m.ImportSource, value['sources'][0]['id'])
            suggestions = {p['as_written']: p['suggested_person_id'] for p in source.extraction['people']}
        assert suggestions == {labels[future]: expected, labels[undated]: None}
        missing = upload(h, [[labels[undated], day, 'Clinical review session', 'Continuity clinic', '08:00', '12:00']])
        with transaction(h.app.state.engine) as db:
            batch = db.get(m.ScoreCheckImportBatch, missing['id'])
            missing_ids = {gap['suggested_target_id'] for gap in batch.gaps if gap['kind'] == 'missing'}
        assert (future in missing_ids) is (expected is not None)
        assert undated not in missing_ids
    value = upload(h, [[labels[future], day, 'Clinical review session', 'Continuity clinic', '08:00', '12:00']
                       for day in ('2026-10-05', '2026-10-06')])
    with transaction(h.app.state.engine) as db:
        source = db.get(m.ImportSource, value['sources'][0]['id'])
        assert source.extraction['people'][0]['suggested_person_id'] is None
