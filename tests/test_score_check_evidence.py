"""Review decisions belong to one piece of evidence and its current clinical context."""
import copy
import hashlib

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled')
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import score_check, score_check_provider as provider
from .test_score_check import acknowledge_missing, change, csv_upload, decide, gap_for
from .test_score_check_ai import actor


def fresh_check(h, batch):
    return h.get('coordinator', '/api/score-imports/' + batch['id'] + '/check')


def current_batch(h, batch):
    return h.get('coordinator', '/api/score-checks/' + batch['id'])['batch']


def resolved_import(h, typ=None):
    batch = csv_upload(h, [['Alex Morgan', '2026-10-04', typ or 'Clinical review session',
        'Continuity clinic', '08:00', '12:00']], starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    return decide(h, batch, gap_for(batch, 'Continuity clinic'), 'confirm', location_id=h.seed['locations'][0])


def stored_evidence(h, monkeypatch, format='csv', *, unclear=2):
    """Synthetic OCR preserves real declarations, validation and ordinary draft writers."""
    batch = csv_upload(h, [
        ['Alex Morgan', '2026-10-04', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
        ['Alex Morgan', '2026-10-05', 'Clinical review session', 'Continuity clinic', '08:00', '12:00'],
    ], starts_on='2026-10-04', ends_on='2026-10-11')['batch']
    with transaction(h.app.state.engine, write=True) as db:
        source = db.get(m.ImportSource, batch['files'][0]['id'])
        extraction = copy.deepcopy(source.extraction)
        page_ref = 'PDF!P1' if format == 'pdf' else 'Image!1' if format == 'png' else extraction['assignments'][0]['cell_ref']
        if format != 'csv':
            source.format, source.file_name = format, 'synthetic-page.' + format
            source.content = b'synthetic-source-page-with-two-valid-and-two-unreadable-rows'
            source.sha256 = hashlib.sha256(source.content).hexdigest()
            for collection in ('people', 'duty_codes', 'clinics'):
                for item in extraction[collection]:
                    item['cell_refs'] = [page_ref]
            for assignment in extraction['assignments']:
                assignment['cell_ref'] = page_ref
            monkeypatch.setattr(provider, 'prepare_file', lambda file_ref, filename, content:
                provider.PreparedFile(file_ref, hashlib.sha256(content).hexdigest(), 'Synthetic page', frozenset({page_ref})))
        extraction['clinics'][0]['suggested_location_id'] = h.seed['locations'][0]
        extraction['unparsed'] = [{'file_ref': source.file_ref, 'cell_ref': page_ref,
            'value': f'Unclear source row {index + 1}', 'reason': 'One row needs a complete reading'}
            for index in range(unclear)]
        source.extraction = score_check.validate_extraction(db, actor(h, db, 'coordinator'), source, extraction)
        source.status = 'needs_review'
        source.revision += 1
    check = fresh_check(h, batch)
    return current_batch(h, batch), check


@pytest.mark.parametrize('format', ['pdf', 'png'])
def test_two_unreadable_items_on_one_page_have_independent_skip_decisions(h, monkeypatch, format):
    batch, check = stored_evidence(h, monkeypatch, format)
    cells = [item for item in check['items'] if item['section'] == 'cell']
    assert len(cells) == 2 and len({cell['id'] for cell in cells}) == 2
    assert cells[0]['reading_context']['cell_ref'] == cells[1]['reading_context']['cell_ref']
    original_ids = [cell['id'] for cell in cells]
    first = next(g for g in batch['gaps'] if g['id'] == original_ids[0])
    batch = decide(h, batch, first, 'exclude')
    assert next(g for g in batch['gaps'] if g['id'] == original_ids[0])['decision']['action'] == 'exclude'
    assert next(g for g in batch['gaps'] if g['id'] == original_ids[1]).get('decision') is None
    assert batch['counts']['unreadable'] == 1
    batch = decide(h, batch, next(g for g in batch['gaps'] if g['id'] == original_ids[1]), 'exclude')
    batch = acknowledge_missing(h, batch)
    saved = change(h, batch, 'save')
    shifts = h.detail(saved['workspace'])['shifts']
    assert len(shifts) == 2
    assert {shift['start'][:10] for shift in shifts} == {'2026-10-04', '2026-10-05'}
    assert all([p['person_id'] for p in shift['participants']] == [h.seed['people'][0]] for shift in shifts)


@pytest.mark.parametrize('format', ['pdf', 'png'])
def test_correcting_two_items_on_one_page_preserves_neighbours_and_undo_is_local(h, monkeypatch, format):
    batch, check = stored_evidence(h, monkeypatch, format)
    cells = [item for item in check['items'] if item['section'] == 'cell']
    decisions = []
    for index, cell in enumerate(cells):
        gap = next(g for g in batch['gaps'] if g['id'] == cell['id'])
        assignment = {**cell['reading_context'], 'person': 'Alex Morgan', 'date': f'2026-10-0{index + 6}',
            'duty_code': 'Clinical review session', 'clinic': 'Continuity clinic', 'start': '08:00', 'end': '12:00'}
        batch = decide(h, batch, gap, 'resolve', replacement=assignment)
        decisions.append(next(g for g in batch['gaps'] if g['id'] == cell['id'])['decision']['id'])
        assert batch['files'][0]['result']['assignment_count'] == index + 3
    batch = change(h, batch, 'undo', decision_id=decisions[0])['batch']
    assert batch['files'][0]['result']['assignment_count'] == 3
    assert next(g for g in batch['gaps'] if g['id'] == cells[0]['id']).get('decision') is None
    assert next(g for g in batch['gaps'] if g['id'] == cells[1]['id'])['decision']['id'] == decisions[1]
    batch = acknowledge_missing(h, batch)
    saved = change(h, batch, 'save')
    assert {shift['start'][:10] for shift in h.detail(saved['workspace'])['shifts']} == {
        '2026-10-04', '2026-10-05', '2026-10-07'}


@pytest.mark.parametrize('action', ['undecided', 'exclude', 'resolve'])
def test_ambiguous_tabular_cell_never_applies_its_original_assignment_without_a_reading(h, monkeypatch, action):
    batch, check = stored_evidence(h, monkeypatch, unclear=1)
    cell, = [item for item in check['items'] if item['section'] == 'cell']
    if action != 'undecided':
        replacement = ({**cell['reading_context'], 'person': 'Alex Morgan', 'date': '2026-10-04',
            'duty_code': 'Clinical review session', 'clinic': 'Continuity clinic', 'start': '13:00', 'end': '17:00'}
            if action == 'resolve' else None)
        batch = decide(h, batch, next(g for g in batch['gaps'] if g['id'] == cell['id']), action,
                       **({'replacement': replacement} if replacement else {}))
    batch = acknowledge_missing(h, batch)
    saved = change(h, batch, 'save')
    shifts = h.detail(saved['workspace'])['shifts']
    assert len(shifts) == (2 if action == 'resolve' else 1)
    assert any(shift['start'].startswith('2026-10-05T05:00:00') for shift in shifts)
    assert not any(shift['start'].startswith('2026-10-04T05:00:00') for shift in shifts)
    if action == 'resolve':
        assert any(shift['start'].startswith('2026-10-04T10:00:00') for shift in shifts)


def test_editing_period_requires_new_missing_staff_acknowledgements(h):
    batch = acknowledge_missing(h, resolved_import(h))
    previous = gap_for(batch, 'Taylor Noor', 'missing')
    assert previous['decision']['action'] == 'acknowledge' and batch['can_save']
    batch = change(h, batch, 'update', starts_on='2026-10-04', ends_on='2026-10-12')['batch']
    current = gap_for(batch, 'Taylor Noor', 'missing')
    assert current['id'] != previous['id'] and current.get('decision') is None
    assert batch['counts']['missing'] > 0 and batch['can_save'] is False
    assert change(h, batch, 'save', status=409)['error']['code'] in {'IMPORT_ALARMS', 'IMPORT_CHECK_CHANGED'}


@pytest.mark.parametrize('refresh_first', [False, True])
def test_check_and_save_detect_staff_who_joined_after_the_previous_check(h, refresh_first):
    batch = acknowledge_missing(h, resolved_import(h))
    assert batch['can_save']
    newcomer = h.configure('people', {'display_name': 'New synthetic colleague'})['record']
    h.command('coordinator', 'membership.create', {'person_id': newcomer['id'],
        'category_id': h.seed['category'], 'starts_on': '2026-10-04'})
    if refresh_first:
        check = fresh_check(h, batch)
        missing = [item for item in check['items'] if item['section'] == 'missing' and item['as_written'] == newcomer['display_name']]
        assert len(missing) == 1 and missing[0]['decision'] is None
        batch = current_batch(h, batch)
    assert change(h, batch, 'save', status=409)['error']['code'] in {'IMPORT_ALARMS', 'IMPORT_CHECK_CHANGED'}
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Workspace)) == 0
    check = fresh_check(h, batch)
    assert any(item['section'] == 'missing' and item['as_written'] == newcomer['display_name']
               and item['decision'] is None for item in check['items'])


def test_changed_type_cover_requirement_invalidates_previous_acknowledgement(h):
    definition = {'requirements': [{'minimum': 2, 'maximum': 3}]}
    typ = h.configure('assignment_types', {'name': 'Synthetic shared duty', 'starts_on': '2020-01-01',
                                          'definition': definition})['record']
    batch = acknowledge_missing(h, resolved_import(h, typ['name']))
    previous = {g['id'] for g in batch['gaps'] if g['entity_kind'] == 'coverage'}
    assert previous and batch['can_save']
    updated = {**typ['definition'], 'requirements': [{'minimum': 3, 'maximum': 3}]}
    h.configure('assignment_types', {'name': typ['name'], 'starts_on': '2020-01-01', 'definition': updated},
                target=typ['id'], revision=typ['revision'])
    assert change(h, batch, 'save', status=409)['error']['code'] in {'IMPORT_ALARMS', 'IMPORT_CHECK_CHANGED'}
    fresh_check(h, batch)
    current = current_batch(h, batch)
    alarms = [g for g in current['gaps'] if g['entity_kind'] == 'coverage']
    assert alarms and all(g['id'] not in previous and g.get('decision') is None for g in alarms)
    assert current['can_save'] is False


def test_scoped_minimum_cover_rule_and_its_changed_evidence_need_acknowledgement(h):
    policy = h.policy({'kind': 'minimum_cover', 'minimum': 2}, {'assignment_type_id': h.seed['type']})
    batch = resolved_import(h)
    cover = [g for g in batch['gaps'] if g['entity_kind'] == 'coverage']
    assert cover, 'The normal minimum-cover rule must appear in import alarms'
    batch = acknowledge_missing(h, batch)
    previous = {g['id'] for g in cover}
    # A second writer changes the same rule identity, exercising evidence freshness rather than a new-rule ID.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, policy['id'])
        definition = copy.deepcopy(row.definition)
        definition['rule']['minimum'] = 3
        row.definition, row.revision = definition, row.revision + 1
    check = fresh_check(h, batch)
    alarms = [item for item in check['items'] if item['section'] == 'missing' and item['kind'] == 'session']
    assert alarms and all(item['id'] not in previous and item['decision'] is None for item in alarms)
    assert current_batch(h, batch)['can_save'] is False


@pytest.mark.parametrize('label', ['Clinical review session', 'Continuity clinic'])
def test_leaving_out_the_only_duty_code_or_clinic_marks_its_staff_missing_and_undo_restores_them(h, monkeypatch, label):
    batch, _ = stored_evidence(h, monkeypatch, unclear=0)
    assert not any(g['kind'] == 'missing' and g['suggested_target_id'] == h.seed['people'][0] for g in batch['gaps'])
    batch = decide(h, batch, gap_for(batch, label), 'exclude')
    excluded = gap_for(batch, label)['decision']
    missing = gap_for(batch, 'Alex Morgan', 'missing')
    assert missing['suggested_target_id'] == h.seed['people'][0]
    assert missing.get('decision') is None and batch['can_save'] is False
    batch = change(h, batch, 'undo', decision_id=excluded['id'])['batch']
    assert not any(g['kind'] == 'missing' and g['suggested_target_id'] == h.seed['people'][0] for g in batch['gaps'])
    assert batch['files'][0]['result']['assignment_count'] == 2
