"""Assignment provenance follows actions, while retained work and Undo keep its evidence."""
import json
from datetime import date
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, select

from apps.api import models as m
from apps.api.db import make_engine, transaction
from packages.contracts import schemas as s
from scripts.manage import alembic_config, migrate
from alembic import command
from .test_series_reconciliation import template, materialise, amend_template, reconcile, edit
from .test_generation_apply_scale import completed_proposal
from .migration_support import historical_seed


SOURCE_FIELDS = tuple(s.AssignmentSource.model_fields)


def provenance(participant):
    return {key: participant[key] for key in SOURCE_FIELDS}


def assert_source(h, participant, kind):
    assert participant['source'] == kind
    assert participant['source_run_id'] is None
    assert participant['source_actor_id'] == h.seed['identities']['coordinator']
    assert participant['source_at'] is not None
    # wish_effects is the planner's wish overlay on the detail view, not part of the stored record
    s.ParticipantRecord.model_validate({k: v for k, v in participant.items() if k != 'wish_effects'})


def undo(h, workspace):
    return h.command('coordinator', 'workspace.undo', {'reason': 'Restore original provenance'},
                     workspace['id'], workspace['revision'])['workspace']


@pytest.mark.parametrize('fixture', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('remain_fixed', [False, True])
def test_manual_keep_lock_edit_and_undo_payload(request, fixture, remain_fixed):
    h = request.getfixturevalue(fixture)
    w = h.shift(h.workspace())['workspace']
    original = h.detail(w)['shifts'][0]
    person = original['participants'][0]
    assert_source(h, person, 'manual')
    # A row reconstruction caused by an unrelated session edit preserves the source.
    w = edit(h, w, original, location_id=h.seed['locations'][1])
    assert provenance(h.detail(w)['shifts'][0]['participants'][0]) == provenance(person)
    row = h.detail(w)['shifts'][0]
    participant = {key: row['participants'][0][key] for key in s.ParticipantInput.model_fields}
    w = edit(h, w, row, participants=[{**participant, 'fixed': True}])
    kept = h.detail(w)['shifts'][0]['participants'][0]
    assert kept['fixed']
    assert provenance(kept) == provenance(person)
    result = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [row['id']],
                       'action': 'lock', 'reason': 'Keep this reviewed duty'}, revision=w['revision'])
    assert result['shifts'][0]['locked']
    assert provenance(result['shifts'][0]['participants'][0]) == provenance(person)
    w = undo(h, result['workspace'])
    assert provenance(h.detail(w)['shifts'][0]['participants'][0]) == provenance(kept)
    w = edit(h, w, h.detail(w)['shifts'][0], participants=[{**participant, 'fixed': remain_fixed, 'overtime': True}])
    assert_source(h, h.detail(w)['shifts'][0]['participants'][0], 'manual')
    w = undo(h, w)
    assert provenance(h.detail(w)['shifts'][0]['participants'][0]) == provenance(kept)


@pytest.mark.parametrize('fixed', [False, True])
def test_unlocked_source_and_copies(h, fixed):
    w = h.shift(h.workspace(), people=[])['workspace']
    row = h.detail(w)['shifts'][0]
    job = completed_proposal(h, w, [{'shift_id': row['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': row['start'], 'end': row['end']}}])
    w = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
                   'reason': 'Assign the original duty'}, revision=w['revision'])['workspace']
    row = h.detail(w)['shifts'][0]
    w = edit(h, w, row, participants=[{key: row['participants'][0][key] for key in s.ParticipantInput.model_fields}
                                    | {'fixed': fixed}])
    row = h.detail(w)['shifts'][0]
    original = provenance(row['participants'][0])
    assert original['source'] == 'auto_schedule' and original['source_run_id'] == job['id']
    for action in ('lock', 'unlock'):
        result = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [row['id']],
                           'action': action, 'reason': 'Reviewed lock decision'}, revision=w['revision'])
        assert result['shifts'][0]['locked'] == (action == 'lock')
        assert provenance(result['shifts'][0]['participants'][0]) == original
        w = result['workspace']
    result = h.command('coordinator', 'shift.copy', {'workspace_id': w['id'], 'shift_ids': [row['id']],
                       'offset_days': 1, 'reason': 'Copy to a new duty'}, revision=w['revision'])
    copied = next(r for r in result['shifts'] if r['id'] != row['id'])
    assert_source(h, copied['participants'][0], 'manual')
    assert copied['participants'][0]['fixed'] == fixed
    w = undo(h, result['workspace'])
    assert provenance(h.detail(w)['shifts'][0]['participants'][0]) == original
    copied_workspace = h.command('coordinator', 'workspace.create', {'name': 'Next week copied duties',
        'starts_on': '2026-10-11', 'ends_on': '2026-10-18', 'source_workspace_id': w['id'],
        'source_revision': w['revision'], 'copy_people': True})['workspace']
    assert_source(h, h.detail(copied_workspace)['shifts'][0]['participants'][0], 'manual')
    assert h.detail(copied_workspace)['shifts'][0]['participants'][0]['fixed'] == fixed


@pytest.mark.parametrize('fixture', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_generation_retains_existing_sources_and_manual_retime_clears_run(request, fixture):
    h = request.getfixturevalue(fixture)
    w = h.shift(h.workspace())['workspace']
    row = h.detail(w)['shifts'][0]
    original = provenance(row['participants'][0])
    job = completed_proposal(h, w, [{'shift_id': row['id'], 'participant': {
        'person_id': h.seed['people'][1], 'start': row['start'], 'end': row['end']}}])
    w = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
                   'reason': 'Add one person while retaining existing work'}, revision=w['revision'])['workspace']
    row = h.detail(w)['shifts'][0]
    people = {p['person_id']: p for p in row['participants']}
    assert provenance(people[h.seed['people'][0]]) == original
    generated = people[h.seed['people'][1]]
    assert generated['source'] == 'auto_schedule' and generated['source_run_id'] == job['id']
    sources = {p['person_id']: provenance(p) for p in row['participants']}
    for fixed in (True, False):
        w = edit(h, w, row, participants=[{key: p[key] for key in s.ParticipantInput.model_fields}
                                        | {'fixed': fixed} for p in row['participants']])
        row = h.detail(w)['shifts'][0]
        assert all(p['fixed'] == fixed for p in row['participants'])
        assert {p['person_id']: provenance(p) for p in row['participants']} == sources
    w = edit(h, w, row, location_id=h.seed['locations'][1])
    row = h.detail(w)['shifts'][0]
    assert next(provenance(p) for p in row['participants'] if p['person_id'] == generated['person_id']) == provenance(generated)
    w = edit(h, w, row, start='2026-10-04T09:00:00+03:00', end='2026-10-04T13:00:00+03:00',
        participants=[{**{key: p[key] for key in s.ParticipantInput.model_fields},
                      'start': '2026-10-04T09:00:00+03:00', 'end': '2026-10-04T13:00:00+03:00'}
                      for p in row['participants']])
    assert all(p['source'] == 'manual' and p['source_run_id'] is None for p in h.detail(w)['shifts'][0]['participants'])
    w = undo(h, w)
    assert h.detail(w)['shifts'] == [row]


def test_draft_swap_undo_and_amendment_source_inheritance(h):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                people=[h.seed['people'][1]])['workspace']
    before = h.detail(w)['shifts']
    result = h.command('coordinator', 'draft.swap', {'workspace_id': w['id'],
        'first_assignment_id': before[0]['participants'][0]['id'],
        'second_assignment_id': before[1]['participants'][0]['id'], 'reason': 'Exchange both reviewed duties'},
        revision=w['revision'])
    for row in result['shifts']:
        assert_source(h, row['participants'][0], 'swap')
    w = undo(h, result['workspace'])
    assert h.detail(w)['shifts'] == before
    publication = h.publish(w)
    amendment = h.workspace(base=publication['publication_id'])
    assert [provenance(r['participants'][0]) for r in h.detail(amendment)['shifts']] == [
        provenance(r['participants'][0]) for r in before]


@pytest.mark.parametrize('fixture', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_bulk_keep_and_one_undo_preserve_each_assignments_source(request, fixture):
    h = request.getfixturevalue(fixture)
    w = h.shift(h.workspace(), people=[])['workspace']
    row = h.detail(w)['shifts'][0]
    job = completed_proposal(h, w, [{'shift_id': row['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': row['start'], 'end': row['end']}}])
    w = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Keep generated and manually assigned work together'}, revision=w['revision'])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    before = h.detail(w)['shifts']
    assert {p['source'] for shift in before for p in shift['participants']} == {'manual', 'auto_schedule'}
    items = []
    for shift in before:
        fields = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
        fields['participants'] = [{**{key: p[key] for key in s.ParticipantInput.model_fields}, 'fixed': True}
                                  for p in shift['participants']]
        items.append({'expected_revision': w['revision'], 'payload': {
            'workspace_id': w['id'], 'shift_id': shift['id'], **fields}})
    result = h.command('coordinator', 'bulk.apply', {'item_kind': 'shift.update', 'items': items,
        'reason': 'Keep both reviewed assignments'})
    after = h.detail(w)['shifts']
    assert all(p['fixed'] for shift in after for p in shift['participants'])
    assert [provenance(p) for shift in after for p in shift['participants']] == [
        provenance(p) for shift in before for p in shift['participants']]
    assert len(result['undo']['payload']['items']) == 1
    h.command('coordinator', **result['undo'])
    assert h.detail(w)['shifts'] == before


def test_recurring_materialisation_reconciliation_and_undo(h):
    series = template(h, days=(6, 0), definition={'anchor': '2026-10-04',
        'days': [{'weekday': day, 'start_time': '08:00', 'end_time': '12:00'} for day in (6, 0)],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': [h.seed['people'][0]]})
    w = materialise(h, series)
    before = h.detail(w)['shifts']
    for row in before:
        assert_source(h, row['participants'][0], 'recurring')
        assert row['participants'][0]['fixed']
    series = amend_template(h, series)
    result = reconcile(h, w, series, replace_modified=True)
    after = h.detail(result['workspace'])['shifts']
    assert after[1]['start'] != before[1]['start']
    assert [provenance(r['participants'][0]) for r in after] == [provenance(r['participants'][0]) for r in before]
    w = undo(h, result['workspace'])
    assert h.detail(w)['shifts'] == before
    created = h.command('coordinator', 'workspace.create', {'name': 'Template at creation',
        'starts_on': '2026-10-11', 'ends_on': '2026-10-18', 'series_ids': [series['id']]})['workspace']
    assert all(p['source'] == 'recurring' for row in h.detail(created)['shifts'] for p in row['participants'])


@pytest.mark.parametrize('missing', [False, True])
def test_undo_restores_unknown_source_from_legacy_rows_and_snapshots(h, missing):
    w = h.shift(h.workspace())['workspace']
    row = h.detail(w)['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        participant = db.get(m.Assignment, row['participants'][0]['id'])
        for key in SOURCE_FIELDS:
            setattr(participant, key, None)
    row = h.detail(w)['shifts'][0]
    assert all(value is None for value in provenance(row['participants'][0]).values())
    w = edit(h, w, row, participants=[{key: row['participants'][0][key] for key in s.ParticipantInput.model_fields}
                                    | {'fixed': True}])
    assert provenance(h.detail(w)['shifts'][0]['participants'][0]) == provenance(row['participants'][0])
    if missing:
        with transaction(h.app.state.engine, write=True) as db:
            change = db.scalar(select(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'],
                               m.ChangeSet.resulting_content_revision == w['content_revision']))
            snapshot = json.loads(json.dumps(change.before_snapshot))
            for participant in snapshot['shifts'][0]['participants']:
                for key in SOURCE_FIELDS:
                    participant.pop(key)
            change.before_snapshot = snapshot
    w = undo(h, w)
    assert h.detail(w)['shifts'] == [row]


def test_sources_are_output_only_and_generated(h):
    w = h.shift(h.workspace())['workspace']
    row = h.detail(w)['shifts'][0]['participants'][0]
    schema = json.loads(Path('packages/contracts/public-schemas.json').read_text())
    assert set(SOURCE_FIELDS) <= set(schema['ParticipantRecord']['properties'])
    source_kinds = {'auto_schedule', 'manual', 'swap', 'import', 'recurring'}
    assert set(schema['ParticipantRecord']['properties']['source']['anyOf'][0]['enum']) == source_kinds
    for kind in ('kept', 'locked'):
        with pytest.raises(ValidationError):
            s.ParticipantRecord.model_validate({**row, 'source': kind})
    assert not set(SOURCE_FIELDS) & set(s.ParticipantInput.model_fields)
    data = {key: row[key] for key in s.ParticipantInput.model_fields}
    for key in SOURCE_FIELDS:
        with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
            s.ParticipantInput.model_validate({**data, key: row[key]})


def test_migration_adds_null_columns_without_rewriting_existing_rows(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'old-assignments.db')
    command.upgrade(alembic_config(url), '0086_swap_open')
    engine = make_engine(url)
    with transaction(engine, write=True) as db:
        seed = historical_seed(db)
        metadata = MetaData()
        location_types, locations = [Table(name, metadata, autoload_with=db.connection())
                                     for name in ('location_types', 'locations')]
        effective = {'revision': 1, 'created_at': m.now(), 'department_id': seed['department_id'],
                     'starts_on': date(2020, 1, 1), 'active': True}
        db.execute(location_types.insert().values(id='old-location-type', name='Historical room type',
                                                 description='', **effective))
        db.execute(locations.insert().values(id='old-location', name='Historical room',
                                            location_type_id='old-location-type', capacity=1, **effective))
    engine.dispose()
    with sqlite3.connect(tmp_path / 'old-assignments.db') as db:
        db.execute("INSERT INTO workspaces (id, revision, created_at, department_id, name, starts_on, ends_on, status, "
                   "content_revision, created_by, last_edited_by, target_overrides, rule_adjustments, accepted_issues) "
                   "VALUES ('old-workspace', 1, '2026-10-01', ?, 'Old rota', '2026-10-04', '2026-10-11', 'draft', 1, ?, ?, '[]', '[]', '[]')",
                   (seed['department_id'], seed['identities']['coordinator'], seed['identities']['coordinator']))
        db.execute("INSERT INTO shifts (id, revision, created_at, workspace_id, assignment_type_id, type_revision, type_snapshot, "
                   "location_id, start, end, cancelled, locked, attributes, series_modified, template_skipped) "
                   "VALUES ('old-shift', 1, '2026-10-01', 'old-workspace', ?, 1, '{}', ?, '2026-10-04 05:00', '2026-10-04 09:00', 0, 0, '{}', 0, '[]')",
                   (seed['type'], 'old-location'))
        db.execute("INSERT INTO assignments (id, revision, created_at, shift_id, person_id, start, end, participation, overtime, fixed, team_lead, segments) "
                   "VALUES ('old-assignment', 3, '2026-10-01', 'old-shift', ?, '2026-10-04 05:00', '2026-10-04 09:00', 'staff', 0, 0, 0, '[]')", (seed['people'][0],))
        before = db.execute('SELECT * FROM assignments').fetchall()
    migrate(url)
    with sqlite3.connect(tmp_path / 'old-assignments.db') as db:
        after = db.execute('SELECT * FROM assignments').fetchall()
    assert after == [before[0] + (None,) * len(SOURCE_FIELDS)]
