"""PERF-2: reuse frozen work without reusing authority or final writer inputs."""
from copy import deepcopy
import time

import pytest
from sqlalchemy import event, insert, select

from apps.api import models as m, snapshots
from apps.api.serialization import json_value
from apps.api.db import transaction
from apps.api.services import planning, planning_preparation, reviews
from tests.clock_support import freeze_clock
from tests.test_be18_command_latency import edit
from tests.test_fix_list_performance import backend


def test_preparation_inputs_are_isolated_between_rounds_and_reacquired_on_replay(backend):
    h = backend
    plan = {'seed': 'perf2', 'at': m.now(), 'checks': []}
    with transaction(h.app.state.engine) as db:
        org = db.get(m.Organisation, h.seed['organisation_id'])
        planning_preparation.bind(db, plan)
        first = snapshots.snapshot_inputs(db, org)
        expected = deepcopy(first)
        first['people'].clear()
        planning_preparation.bind(db, plan)
        second = snapshots.snapshot_inputs(db, org)
        assert second == expected
        second['locations'].clear()
        assert snapshots.snapshot_inputs(db, org) == expected
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Location, h.seed['locations'][0]).capacity += 1
    with transaction(h.app.state.engine) as db:
        planning_preparation.bind(db, plan, replay=True)
        current = snapshots.snapshot_inputs(db, db.get(m.Organisation, h.seed['organisation_id']))
        assert current['locations'][h.seed['locations'][0]]['capacity'] == expected['locations'][h.seed['locations'][0]]['capacity'] + 1


def test_complete_save_computes_supervision_validation_and_score_once_after_rollback(h, monkeypatch):
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace())['workspace']
    shift = h.detail(w)['shifts'][0]
    calls = []
    original = planning.command_compute
    def counted(db, kind, data, compute, **kwargs):
        def pure():
            assert not db.in_transaction()
            calls.append(kind)
            return compute()
        return original(db, kind, data, pure, **kwargs)
    monkeypatch.setattr(planning, 'command_compute', counted)
    reply = edit(h, w, shift, [])
    assert calls.count('rota_check') == 1
    assert calls.count('edit_admission') == 1
    assert set(calls) == {'edit_admission', 'rota_check'}
    with monkeypatch.context() as patch:
        patch.setattr(planning, 'command_compute', original)
        with transaction(h.app.state.engine) as db:
            expected = planning.revalidate(db, db.get(m.Workspace, w['id']), store=False, include_internal=True)
    # A repeated check advances fix-list history; its canonical findings, coverage and score are identical.
    ignored = {'fix_list_state', 'fix_list_delta', '_session_parts', '_person_parts'}
    assert {k: v for k, v in reply['validation'].items() if k not in ignored} == json_value({
        k: v for k, v in expected.items() if k not in ignored})


def test_approval_reuses_writer_fingerprint_across_review_only_writes(h, monkeypatch):
    w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    def forbidden(*a, **kw):
        pytest.fail('Review-only writes must reuse the fingerprint already acquired under the writer lock')
    monkeypatch.setattr(reviews, 'fingerprint', forbidden)
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    assert h.lifecycle(w, 'publish')['publication_id']


def test_publication_ledgers_are_written_once_in_final_writer(backend, monkeypatch):
    from apps.api.services import fairness
    h = backend
    w = h.lifecycle(h.shift(h.workspace())['workspace'], 'submit')['workspace']
    w = h.lifecycle(w, 'approve', 'approver')['workspace']
    original, calls = fairness.on_publish, []
    def counted(db, *a, **kw):
        assert db.connection().info['rota_write']
        calls.append(True)
        return original(db, *a, **kw)
    monkeypatch.setattr(fairness, 'on_publish', counted)
    assert h.lifecycle(w, 'publish')['publication_id']
    assert calls == [True]


def test_mixed_draft_and_publication_keeps_issued_closure(backend):
    h = backend
    draft = h.shift(h.workspace('Draft'), people=[])['workspace']
    issued = h.shift(h.workspace('Issued'), people=[])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id.in_([draft['id'], issued['id']]))))
        for row in rows:
            row.series_snapshot = {'definition': {'close_when_all_usual_unavailable': True,
                'default_person_ids': h.seed['people']}}
        frozen = next(row for row in rows if row.workspace_id == issued['id'])
        workspace = db.get(m.Workspace, issued['id'])
        workspace.status = 'published'
        db.add(m.Publication(workspace_id=workspace.id, department_id=workspace.department_id, sequence=1,
            starts_on=workspace.starts_on, ends_on=workspace.ends_on, current=True,
            published_by=h.seed['identities']['coordinator'], fingerprint='0' * 64,
            snapshot={'shifts': [{'id': frozen.id, 'series_state_frozen': True, 'closed': True,
                'closure_reason': 'usual_assignees_unavailable', 'usual_assignee_unavailability': []}]},
            validation=workspace.validation))
        selected, closed = [row.id for row in rows], frozen.id
    with transaction(h.app.state.engine) as db:
        assert snapshots.inactive_series_shift_ids(db, selected) == {closed}


@pytest.fixture(scope='module')
def department_draft_closure(scale_harness):
    h = scale_harness('h')
    saved = h.shift(h.workspace(), people=[])
    w, source = saved['workspace'], saved['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.Shift, source['id'])
        rows = [{'id': m.uid(), 'workspace_id': w['id'], 'assignment_type_id': source['assignment_type_id'],
            'type_revision': source['type_revision'], 'type_snapshot': source['type_snapshot'],
            'location_id': source['location_id'], 'start': duty.start,
            'end': duty.end, 'cancelled': i == 0,
            'series_snapshot': {'definition': {'close_when_all_usual_unavailable': True,
                'default_person_ids': h.seed['people']}}} for i in range(540)]
        db.execute(insert(m.Shift), rows)
    statements = []
    def counted(_c, _cu, statement, _p, _ctx, _many):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', counted)
    try:
        with transaction(h.app.state.engine) as db:
            started = time.process_time()
            inactive = snapshots.inactive_series_shift_ids(db, [r['id'] for r in rows])
            elapsed = time.process_time() - started
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', counted)
    print('PERF-2 draft closure', {'duties': len(rows), 'cpu_seconds': elapsed, 'statements': len(statements)})
    return rows, inactive, statements, elapsed


def test_department_draft_closure_uses_two_scalar_queries_without_absence_graph(department_draft_closure):
    rows, inactive, statements, _ = department_draft_closure
    assert inactive == {rows[0]['id']}
    assert len(statements) == 2
    assert all(s.startswith('SELECT shifts.id') for s in statements)
    assert all('availability' not in s and 'rotations' not in s and 'shifts.type_snapshot' not in s for s in statements)


@pytest.mark.cpu_budget
def test_department_draft_closure_cpu_budget(department_draft_closure):
    from tests.test_scheduler_performance import _budget
    # Native measurement: 4.4 ms for 540 duties; allow ample runner headroom and coverage tracing.
    assert department_draft_closure[3] < _budget(0.1)
