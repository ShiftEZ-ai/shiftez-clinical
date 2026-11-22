"""Deterministic work/SQL guards survive shared-runner CPU contention."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from threading import Event
import time

import pytest
from sqlalchemy import event, insert, select

from apps.api import models as m, snapshots
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import fairness, planning, schedule_view
from packages.domain import fairness as domain_fairness
from packages.domain.finding_causes import FindingCauses
from packages.domain.time import aware
from tests.test_series_overlap_schedule import publish_synthetic_rows


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def backend(request):
    return request.getfixturevalue(request.param)


def actor(h):
    return Actor(h.seed['identities']['coordinator'], h.seed['department_id'], h.seed['organisation_id'],
                 'Synthetic', frozenset({'coordinator'}), None, 'isolated')


def test_twenty_fairness_goals_use_one_trend_read_and_keep_values(backend, monkeypatch):
    h = backend
    w = h.shift(h.workspace())['workspace']
    publish_synthetic_rows(h, w)
    with transaction(h.app.state.engine, write=True) as db:
        pub = db.scalar(select(m.Publication).where(m.Publication.workspace_id == w['id']))
        db.add_all(m.FairnessLedger(department_id=h.seed['department_id'], publication_id=pub.id,
            metric_key='goal:g'+str(i), bucket=date(2026, 10, 4), value=i, person_id=None) for i in range(20))
    reports = [{'goal_id': 'g'+str(i), 'people': [], 'pairs': []} for i in range(20)]
    monkeypatch.setattr(domain_fairness, 'goal_reports', lambda data, groups=None: reports)
    monkeypatch.setattr(domain_fairness, 'quota_goal_reports', lambda data: [])
    selects = []
    def counted(conn, cursor, statement, parameters, context, many):
        if 'FROM fairness_ledger' in statement and 'metric_key' in statement:
            selects.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        with transaction(h.app.state.engine) as db:
            result = fairness.goals(db, actor(h), workspace_id=w['id'], starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    assert len(selects) == 1
    assert [r['trend'][0]['score'] for r in result['goals']] == list(range(20))


def test_home_projects_absence_matches_instead_of_five_hundred_future_sessions(backend, monkeypatch):
    h = backend
    saved = h.shift(h.workspace('Issued month', '2026-10-01', '2026-11-01'))
    w, row = saved['workspace'], saved['shifts'][0]
    start = aware('2026-10-05T00:00:00Z')
    with transaction(h.app.state.engine, write=True) as db:
        shifts = [{'id': m.uid(), 'workspace_id': w['id'], 'assignment_type_id': row['assignment_type_id'],
            'type_revision': row['type_revision'], 'type_snapshot': row['type_snapshot'], 'location_id': row['location_id'],
            'start': start+timedelta(hours=i), 'end': start+timedelta(hours=i+1)} for i in range(499)]
        db.execute(insert(m.Shift), shifts)
        db.execute(insert(m.Assignment), [{'shift_id': r['id'], 'person_id': h.seed['people'][0],
            'start': r['start'], 'end': r['end']} for r in shifts])
    publish_synthetic_rows(h, w)
    original, loaded = schedule_view.published_shifts, []
    def counted(*args, **kwargs):
        rows = original(*args, **kwargs)
        loaded.extend(r['id'] for r in rows)
        return rows
    monkeypatch.setattr(schedule_view, 'published_shifts', counted)
    assert not any(i['key'] == 'published_absence_conflicts' for i in h.get('coordinator', '/api/home')['attention'])
    assert loaded == []
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'start': row['start'], 'end': row['end'], 'kind': 'actual_absence', 'private_reason': 'Synthetic absence'})
    attention = h.get('coordinator', '/api/home')['attention']
    assert any(i['key'] == 'published_absence_conflicts' and i['count'] == 1 for i in attention)
    assert loaded == [row['id']]


def test_requests_batch_closure_without_reading_scheduling_policies(backend):
    h = backend
    for n in range(3):
        w = h.shift(h.workspace('Dynamic '+str(n)))['workspace']
        with transaction(h.app.state.engine, write=True) as db:
            row = db.scalar(select(m.Shift).where(m.Shift.workspace_id == w['id']))
            row.series_snapshot = {'definition': {'close_when_all_usual_unavailable': True,
                'default_person_ids': [h.seed['people'][0]]}}
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00',
        'kind': 'actual_absence', 'private_reason': 'Synthetic window'})
    policies = []
    def counted(conn, cursor, statement, parameters, context, many):
        if 'FROM policies' in statement:
            policies.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        rows = h.get('coordinator', '/api/requests/all')['items']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    assert policies == []
    impact = next(r['schedule_impact'] for r in rows if r['source'] == 'availability')
    assert impact['count'] == impact['draft_count'] == 3


@pytest.mark.parametrize('change, expected', [('missing', 'no_eligible_people'), ('leave', 'person_unavailable'), ('empty', 'empty_population')])
def test_zero_candidate_diagnoses_distinguish_eligibility_leave_and_empty_population(h, change, expected):
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    pid = h.seed['people'][0]
    policy = {'id': m.uid(), 'department_id': h.seed['department_id'], 'definition': {
        'rule': {'kind': 'minimum_cover', 'minimum': 1},
        'scope': {'person_id': m.uid() if change == 'empty' else pid}}}
    if change == 'missing':
        data['eligibilities'] = []
    if change == 'leave':
        data['availability'] = [{'id': m.uid(), 'person_id': pid, 'start': shift['start'], 'end': shift['end']}]
    original = deepcopy(data)
    result = FindingCauses(data).coverage('SCOPED_COVERAGE_GAP', shift['id'], aware(shift['start']), aware(shift['end']),
                                          {'required': 1, 'available': 0}, policy)
    assert result['cause']['code'] == expected
    assert data == original or {k: v for k, v in data.items() if not k.startswith('_')} == {k: v for k, v in original.items() if not k.startswith('_')}


def test_home_session_staffing_supplies_headcount_and_only_actual_open_places(h):
    w = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    pub = h.publish(w)
    value = h.get('coordinator', '/api/schedule/sessions?start=2026-10-04&end=2026-10-05')
    assert value['shifts'][0]['staffing'] == {'on_duty': 2, 'places_open': 0}


@pytest.mark.parametrize('action', ['submit', 'approve'])
@pytest.mark.parametrize('relevant', [False, True], ids=['unrelated-activity', 'changed-inputs'])
def test_slow_lifecycle_check_does_not_hold_writer_lock_and_rechecks_inputs(backend, monkeypatch, action, relevant):
    h = backend
    w = h.shift(h.workspace())['workspace']
    if action == 'approve':
        w = h.lifecycle(w, 'submit')['workspace']
    # A current stored check is reused without evaluating; drop it so the lifecycle runs a real (held) check.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, w['id']).validation = None
    role = 'coordinator' if action == 'submit' else 'approver'
    h.client(role)
    h.client('staff')
    entered, release = Event(), Event()
    original = planning.evaluate
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(120), 'Synthetic check was not released'
        return original(*args, **kwargs)
    monkeypatch.setattr(planning, 'evaluate', held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        checking = pool.submit(h.lifecycle, w, action, role, status=409 if relevant else 200)
        try:
            assert entered.wait(120)
            writing = pool.submit(h.command, 'staff', 'table_view.create', {'table_key': 'people',
                'name': 'Writer continues during lifecycle check', 'definition': {}})
            assert writing.result(timeout=120)['view']
            # A real scheduling input must invalidate the prepared candidate, regardless of
            # maintenance's narrower treatment of unrelated table-view activity.
            if relevant:
                with transaction(h.app.state.engine, write=True) as db:
                    department = db.get(m.Department, h.seed['department_id'])
                    department.revision += 1
                    department.generation_defaults = {**(department.generation_defaults or {}), 'mode': 'rebalance'}
                    db.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
            assert not checking.done()
        finally:
            release.set()
        result = checking.result(timeout=120)
        if relevant:
            assert result['error']['code'] == 'STALE_INPUTS'
        else:
            assert result['workspace']['status'] == ('submitted' if action == 'submit' else 'approved')


@pytest.mark.parametrize('action', ['submit', 'approve'])
def test_lifecycle_check_does_not_stamp_inputs_committed_after_its_reads(pg, monkeypatch, action):
    h = pg
    workspace = h.shift(h.workspace())['workspace']
    if action == 'approve':
        workspace = h.lifecycle(workspace, 'submit')['workspace']
    role = 'coordinator' if action == 'submit' else 'approver'
    engine = h.app.state.engine
    original = planning.swaps.validate_publication
    person_id = h.seed['people'][-1]

    def save_input(db, candidate):
        result = original(db, candidate)
        # Commit from a separate PostgreSQL writer after the check and before the currency mark.
        with transaction(engine, write=True, serialize=False) as writer:
            writer.get(m.Person, person_id).active = False
        return result

    with monkeypatch.context() as patch:
        patch.setattr(planning.swaps, 'validate_publication', save_input)
        reply = h.lifecycle(workspace, action, role, status=(200, 409))

    with transaction(engine) as db:
        assert not db.get(m.Person, person_id).active
        row = db.get(m.Workspace, workspace['id'])
        assert row.id not in snapshots.cheap_fingerprints(db, [row], checks={row.id: row.validation})
        if 'error' in reply:
            assert reply['error']['code'] == 'STALE_INPUTS'
            assert row.status == workspace['status']
    if action == 'approve' or 'workspace' in reply:
        submitted = workspace if action == 'approve' else reply['workspace']
        assert h.lifecycle(submitted, 'approve', 'approver', status=409)['error']['code'] == 'STALE_INPUTS'
