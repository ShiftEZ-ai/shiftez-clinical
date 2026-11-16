"""Operation-local external rows preserve fresh snapshots, checks and transactional rollback."""
import copy
from datetime import datetime

import pytest
from sqlalchemy import func, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.services import planning
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from tests.test_block_quotas import quota
from tests.test_event_calendar import event_row
from tests.test_series_reconciliation import template


DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def context(h, bounded):
    prior = h.shift(h.workspace('Published external context'))['workspace']
    publication_id = h.publish(prior)['publication_id']
    if bounded:
        quota(h, scope={}, starts_on='2026-01-01', ends_on='2027-01-01', amount=100, severity='advisory')
    workspace = h.workspace('Creation input phases', start='2026-11-01', end='2026-11-08')
    return workspace, prior, publication_id


def add_own(db, h, workspace, day):
    typ = db.get(m.AssignmentType, h.seed['type'])
    start, end = (datetime.fromisoformat(f'2026-11-{day:02}T{clock}:00+00:00') for clock in ('05:00', '09:00'))
    row = m.Shift(id=m.uid(), workspace_id=workspace.id, assignment_type_id=typ.id, type_revision=typ.revision,
                  type_snapshot=record(typ), location_id=h.seed['locations'][0], start=start, end=end)
    db.add(row)
    db.flush()
    db.add_all([m.ShiftService(shift_id=row.id, service_id=h.seed['services'][0]),
                m.Assignment(shift_id=row.id, person_id=h.seed['people'][0], start=start, end=end)])
    db.flush()


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('edited', [False, True])
def test_reused_external_context_equals_fresh_inputs_check_and_score(database, bounded, shared, edited, request, monkeypatch):
    h = request.getfixturevalue(database)
    w, prior, _ = context(h, bounded)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        add_own(db, h, workspace, 1)
        source = snapshots.domain_snapshot(db, workspace)
        original = copy.deepcopy(source)
        add_own(db, h, workspace, 2)
        if edited:
            workspace.content_revision += 1
            db.flush()
        fresh = snapshots.domain_snapshot(db, workspace)
        inputs = snapshots.snapshot_inputs(db, db.get(m.Organisation,
            db.get(m.Department, workspace.department_id).organisation_id)) if shared else None
        original_inputs = copy.deepcopy(inputs)
        own = snapshots.workspace_shifts(db, workspace.id) if shared else None
        reads, real = [], snapshots.shift_records
        def read_rows(db, rows):
            rows = list(rows)
            reads.append({r.workspace_id for r in rows})
            return real(db, rows)
        monkeypatch.setattr(snapshots, 'shift_records', read_rows)
        reused = snapshots.domain_snapshot(db, workspace, external_snapshot=source, shared_inputs=inputs, own_shifts=own)
        assert reused == fresh and len(reused['focus_ids']) == 2
        assert reused['fingerprint'] != source['fingerprint'], 'Semantic session content changes even if a caller omitted its revision bump'
        assert not any(prior['id'] in ids for ids in reads), 'Own edits reuse unchanged external rows without reading them again'
        assert evaluate(reused) == evaluate(fresh)
        defaults = db.get(m.Department, workspace.department_id).generation_defaults
        assert schedule_score(reused, evaluate(reused), defaults) == schedule_score(fresh, evaluate(fresh), defaults)
        assert source == original, 'The earlier input snapshot stays unchanged'
        assert inputs == original_inputs, 'The shared batch inputs stay unchanged'


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('revision', ['input_revision', 'availability_revision'])
def test_changed_organisation_revision_reloads_external_rows(database, revision, request, monkeypatch):
    h = request.getfixturevalue(database)
    w, prior, _ = context(h, False)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        source = snapshots.domain_snapshot(db, workspace)
        source['shifts'] = []  # Reusing invalidated context would lose the published rows.
        original = copy.deepcopy(source)
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        setattr(organisation, revision, getattr(organisation, revision) + 1)
        workspace.content_revision += 1
        db.flush()
        fresh = snapshots.domain_snapshot(db, workspace)
        reads, real = [], snapshots.shift_records
        def read_rows(db, rows):
            rows = list(rows)
            reads.append({r.workspace_id for r in rows})
            return real(db, rows)
        monkeypatch.setattr(snapshots, 'shift_records', read_rows)
        assert snapshots.domain_snapshot(db, workspace, external_snapshot=source) == fresh
        assert any(prior['id'] in ids for ids in reads), 'Changed configuration or availability invalidates external context'
        assert source == original


@pytest.mark.parametrize('database', DATABASES)
def test_reused_inputs_keep_event_protection_fresh_after_change_and_savepoint_rollback(database, request, monkeypatch):
    h = request.getfixturevalue(database)
    occurrence = event_row(h, settings={'blocks_duties': True}, audience={'person_ids': [h.seed['people'][0]]},
                           start='2026-11-01T08:00:00+03:00', end='2026-11-01T09:00:00+03:00')
    w, prior, _ = context(h, False)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        add_own(db, h, workspace, 1)
        inputs = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        source = snapshots.domain_snapshot(db, workspace, shared_inputs=inputs)
        original, original_inputs = copy.deepcopy(source), copy.deepcopy(inputs)
        workspace.content_revision += 1
        db.flush()
        reads, real = [], snapshots.shift_records
        def read_rows(db, rows):
            rows = list(rows)
            reads.append({r.workspace_id for r in rows})
            return real(db, rows)
        monkeypatch.setattr(snapshots, 'shift_records', read_rows)

        def check(blocked):
            fresh = snapshots.domain_snapshot(db, workspace)
            reads.clear()
            reused = snapshots.domain_snapshot(db, workspace, external_snapshot=source, shared_inputs=inputs)
            assert reused == fresh
            assert any(r.get('event_occurrence_id') == occurrence['id'] for r in reused['scheduling_facts']) == blocked
            assert any(f['code'] == 'RESTRICTION' and f.get('person_id') == h.seed['people'][0]
                       for f in evaluate(reused)['findings']) == blocked
            assert not any(prior['id'] in ids for ids in reads)
            assert source == original and inputs == original_inputs

        check(True)
        savepoint = db.begin_nested()
        db.get(m.EventOccurrence, occurrence['id']).cancelled = True
        db.flush()
        check(False)
        savepoint.rollback()
        check(True)


@pytest.mark.parametrize('database', DATABASES)
def test_invalid_contexts_fall_back_to_fresh_external_rows(database, request, monkeypatch):
    h = request.getfixturevalue(database)
    w, prior, _ = context(h, True)
    changes = [('fingerprint', None, 'stale'),
               *[('workspace', key, value) for key, value in (
                   ('id', 'different'), ('department_id', 'different'), ('status', 'published'), ('content_revision', -1),
                   ('starts_on', '2026-10-31'), ('ends_on', '2026-11-09'),
                   ('base_publication_id', 'different'), ('replaces_publication_id', 'different'))],
               ('organisation', 'timezone', 'UTC'), ('quota_policy_limit', None, -1),
               ('quota_context', 'start', '2026-01-02T00:00:00+00:00'),
               ('quota_context', 'end', '2026-12-31T00:00:00+00:00'),
               ('quota_context', 'complete', False), ('quota_context', 'limit_exceeded', True),
               ('quota_context', None, None)]
    with transaction(h.app.state.engine) as db:
        workspace = db.get(m.Workspace, w['id'])
        fresh = snapshots.domain_snapshot(db, workspace)
        assert any(r['workspace_id'] == prior['id'] for r in fresh['shifts'])
        reads, real = [], snapshots.shift_records
        def read_rows(db, rows):
            rows = list(rows)
            reads.append({r.workspace_id for r in rows})
            return real(db, rows)
        monkeypatch.setattr(snapshots, 'shift_records', read_rows)
        for section, key, value in changes:
            source = copy.deepcopy(fresh)
            source['shifts'] = []  # A missed invalidation would lose the published context.
            if key is None:
                source[section] = value
            else:
                source[section][key] = value
            reads.clear()
            assert snapshots.domain_snapshot(db, workspace, external_snapshot=source) == fresh, (section, key)
            assert any(prior['id'] in ids for ids in reads), (section, key)


@pytest.mark.parametrize('database', DATABASES)
def test_failure_after_reused_creation_check_rolls_back_every_new_row(database, request, monkeypatch):
    h = request.getfixturevalue(database)
    _, _, publication_id = context(h, False)
    series = template(h, days=(6,), ends_on='2026-11-08')
    classes = (m.Workspace, m.Shift, m.ShiftService, m.Assignment, m.ChangeSet, m.CommandReceipt,
               m.AuditEvent, m.FairnessLedger, m.PublicationPerson)
    def state():
        with transaction(h.app.state.engine) as db:
            return ([db.scalar(select(func.count()).select_from(cls)) for cls in classes],
                    copy.deepcopy(db.get(m.Publication, publication_id).snapshot))
    before = state()
    reused, real_check, real_external = [], planning.revalidate, snapshots._unchanged_external_shifts
    def external(*args):
        result = real_external(*args)
        reused.append(result is not None)
        return result
    def fail_after_check(*args, **kwargs):
        result = real_check(*args, **kwargs)
        assert result['coverage'] and reused[-1]
        raise Problem(409, 'SYNTHETIC_ROLLBACK', 'Abort after the final creation check')
    monkeypatch.setattr(snapshots, '_unchanged_external_shifts', external)
    monkeypatch.setattr(planning, 'revalidate', fail_after_check)
    result = h.command('coordinator', 'workspace.create', {'name': 'Rolled-back recurring creation',
        'starts_on': '2026-11-01', 'ends_on': '2026-11-08', 'series_ids': [series['id']]}, status=409)
    assert result['error']['code'] == 'SYNTHETIC_ROLLBACK' and reused[-1]
    assert state() == before


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('method', ['create', 'expand'])
def test_template_booking_window_keeps_crossing_boundaries_and_batch_collisions(database, method, request, monkeypatch):
    h = request.getfixturevalue(database)
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Boundary booking duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    prior = h.workspace('Published neighbouring bookings', start='2026-10-31', end='2026-11-09')
    bookings = {}
    for name, pid, start, end in (
            ('touch_start', first, '2026-10-31T20:00:00+03:00', '2026-11-01T00:00:00+03:00'),
            ('cross_start', second, '2026-10-31T22:00:00+03:00', '2026-11-01T02:00:00+03:00'),
            ('cross_end', second, '2026-11-07T22:00:00+03:00', '2026-11-08T02:00:00+03:00'),
            ('touch_end', first, '2026-11-08T00:00:00+03:00', '2026-11-08T04:00:00+03:00')):
        changed = h.shift(prior, typ=typ, start=start, end=end, people=[pid])
        prior = changed['workspace']
        bookings[name] = changed['shifts'][0]['id']
    h.publish(prior)

    def series(pid, weekday, start, end):
        return template(h, acknowledge_overlaps=True, assignment_type_id=typ, ends_on='2026-11-09', definition={
            'anchor': '2026-10-04', 'days': [{'weekday': weekday, 'start_time': start, 'end_time': end}],
            'service_ids': [h.seed['services'][0]], 'default_person_ids': [pid], 'usual_assignees_fixed': False})
    later = series(first, 6, '08:00', '12:00')
    earlier = series(second, 6, '00:00', '04:00')
    duplicate = series(first, 6, '08:00', '12:00')
    last = series(second, 5, '20:00', '24:00')
    series_ids = [later['id'], earlier['id'], duplicate['id'], last['id']]
    indexed, real = [], planning.materialise_occurrence
    def materialise(*args, **kwargs):
        row = real(*args, **kwargs)
        indexed.append({sid for values in args[6]['_template_bookings'].values() for sid, _, _ in values})
        return row
    monkeypatch.setattr(planning, 'materialise_occurrence', materialise)
    if method == 'create':
        result = h.command('coordinator', 'workspace.create', {'name': 'Out-of-order template setup',
            'starts_on': '2026-11-01', 'ends_on': '2026-11-08', 'series_ids': series_ids})
    else:
        w = h.workspace('Out-of-order template expansion', start='2026-11-01', end='2026-11-08')
        before = h.detail(w)['shifts']
        result = h.command('coordinator', 'series.expand', {'workspace_id': w['id'], 'series_ids': series_ids},
                           revision=w['revision'])
    workspace = result['workspace']
    rows = {row['series_id']: row for row in h.detail(workspace)['shifts']}
    assert len(rows) == 4 and [p['person_id'] for p in rows[later['id']]['participants']] == [first]
    for series_id, pid in ((earlier['id'], second), (duplicate['id'], first), (last['id'], second)):
        row = rows[series_id]
        assert row['participants'] == []
        assert [(r['person_id'], r['code']) for r in row['template_skipped']] == [(pid, 'TIME_OVERLAP')]
    assert all({bookings['cross_start'], bookings['cross_end']} <= ids for ids in indexed)
    assert all(not {bookings['touch_start'], bookings['touch_end']} & ids for ids in indexed)
    fresh = h.snapshot(workspace)
    assert set(bookings.values()) <= {row['id'] for row in fresh['shifts']}, 'The final validator retains broad history'
    stored = h.detail(workspace)['workspace']['validation']
    from packages.domain.rota_summary import fix_list_context
    assert stored['fix_list_context'] == fix_list_context(fresh)
    assert {k: v for k, v in stored.items() if k not in ('score', 'evaluated_at', 'diagnostic_version',
        'score_dependencies', 'input_manifest', 'fix_list_context', 'fix_list_state', 'fix_list_delta')} == evaluate(fresh)
    if method == 'expand':
        h.command('coordinator', 'workspace.undo', {'reason': 'Restore the whole template batch'},
                  workspace['id'], workspace['revision'])
        assert h.detail(workspace)['shifts'] == before
