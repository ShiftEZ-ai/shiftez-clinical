"""Preparation reuses detached evidence; the writer still checks currency and authority."""
from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from apps.api import snapshots
from apps.api.services import planning
from packages.contracts.schemas import ParticipantInput
from tests.test_backend_scale import post, scale_department
from tests.test_generation_apply_scale import completed_proposal
from tests.test_writer_preparation import writer_evaluations as writer_evaluations


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def harness(request):
    return request.getfixturevalue(request.param)


def edit_payload(workspace, shift, minutes=5):
    def moved(value):
        return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat()
    return {'workspace_id': workspace['id'], 'shift_id': shift['id'],
            **{key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'attributes')},
            'end': moved(shift['end']), 'participants': [
                {key: moved(value) if key == 'end' else value for key, value in participant.items()
                 if key in ParticipantInput.model_fields} for participant in shift['participants']],
            'override_reason': 'Synthetic reviewed edit'}


@pytest.mark.parametrize('action', ['edit', 'undo', 'apply'])
def test_scale_preparation_acquires_inputs_outside_writer_once(harness, monkeypatch, writer_evaluations, action, request):
    h = harness
    workspace, data, additions, _ = scale_department(h, assigned=action != 'apply')
    shift = next(row for row in data['shifts'] if row['id'] in data['focus_ids'])
    if action == 'undo':
        workspace = post(h, 'shift.update', edit_payload(workspace, shift), shift['id'], workspace['revision'])['workspace']
    if action == 'apply':
        job = completed_proposal(h, workspace, additions)
    h.client('coordinator')
    writer_evaluations.clear()
    acquired, evaluations, sql, writer_dml = [], [], Counter(), Counter()
    original_snapshot, original_evaluate = snapshots._domain_snapshot, planning.evaluate

    def snapshot(db, *args, **kwargs):
        acquired.append(bool(db.connection().info.get('rota_write')))
        return original_snapshot(db, *args, **kwargs)

    def evaluate(data, **options):
        evaluations.append(1)
        return original_evaluate(data, **options)

    def statement(connection, _cursor, statement, *_):
        verb = statement.split()[0].upper()
        sql[verb] += 1
        if connection.info.get('rota_write') and verb in {'INSERT', 'UPDATE', 'DELETE'}:
            writer_dml[verb, statement.split()[2 if verb == 'INSERT' else 1]] += 1

    monkeypatch.setattr(snapshots, '_domain_snapshot', snapshot)
    monkeypatch.setattr(planning, 'evaluate', evaluate)
    event.listen(h.app.state.engine, 'before_cursor_execute', statement)
    try:
        if action == 'edit':
            result = post(h, 'shift.update', edit_payload(workspace, shift), shift['id'], workspace['revision'])
        elif action == 'undo':
            result = post(h, 'workspace.undo', {'reason': 'Restore the prior assignments'}, workspace['id'], workspace['revision'])
        else:
            result = post(h, 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                          'reason': 'Apply the reviewed proposal'}, revision=workspace['revision'])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', statement)
    request.node.user_properties.append(('work', str({'snapshots': acquired, 'sql': dict(sql),
        'writer_dml': dict(writer_dml), 'writer_evaluations': writer_evaluations})))
    assert result['validation']['valid']
    assert not any(acquired), 'Snapshot acquisition must finish before the global writer'
    assert len(acquired) <= (3 if action == 'edit' else 1), acquired
    assert writer_dml['INSERT', 'assignments'] <= (3 if action == 'apply' else 1), writer_dml
    assert evaluations
    assert writer_evaluations and not any(writer_evaluations), 'Evaluation must finish before the global writer'
    if action == 'undo':
        restored = h.snapshot(result['workspace'])
        assert restored['shifts'] == data['shifts']
    # shortcut: target <=60 SELECTs (three preparation stages remain in edit); tighten when the follow-up lands
    assert sql['SELECT'] <= {'edit': 135, 'undo': 93, 'apply': 82}[action], dict(sql)


def test_replayed_snapshots_are_detached_and_equal_to_canonical_inputs(harness):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services.planning_preparation import bind
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    plan = {'seed': m.uid(), 'at': m.now(), 'kind': 'shift.update', 'checks': []}
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        bind(db, plan)
        prepared = snapshots.domain_snapshot(db, row)
        assert prepared == snapshots._domain_snapshot(db, row)
    for _ in range(2):
        with transaction(h.app.state.engine) as db:
            row = db.get(m.Workspace, workspace['id'])
            bind(db, plan, replay=True)
            replayed = snapshots.domain_snapshot(db, row)
            assert replayed == snapshots._domain_snapshot(db, row) == prepared
            replayed['people'].clear()
            replayed['shifts'].clear()


def test_preconditioned_edit_replays_the_same_snapshot_sequence(harness, monkeypatch):
    from uuid import uuid4
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    command = {'kind': 'shift.update', 'target_id': shift['id'], 'expected_revision': workspace['revision'],
               'payload': edit_payload(workspace, shift)}
    client = h.client('coordinator')
    preview = client.post('/api/planner/preview', json=command)
    assert preview.status_code == 200, preview.text
    acquired, original_snapshot = [], snapshots._domain_snapshot
    def snapshot(db, *args, **kwargs):
        acquired.append(bool(db.connection().info.get('rota_write')))
        return original_snapshot(db, *args, **kwargs)
    monkeypatch.setattr(snapshots, '_domain_snapshot', snapshot)
    applied = client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
        **command, 'expected_fingerprint': preview.json()['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    assert acquired and not any(acquired)


@pytest.mark.parametrize('source', ['organisation', 'availability', 'department', 'workspace', 'baseline'])
def test_prepared_snapshot_rejects_changed_source_currency(harness, source):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.errors import Problem
    from apps.api.services.planning_preparation import bind
    h = harness
    workspace = h.shift(h.workspace())['workspace']
    plan = {'seed': m.uid(), 'at': m.now(), 'kind': 'shift.update', 'checks': []}
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        bind(db, plan)
        snapshots.domain_snapshot(db, row)
    if source == 'baseline':
        workspace = h.refresh_draft_baseline(workspace)
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if source in {'organisation', 'availability'}:
                row = db.get(m.Organisation, h.seed['organisation_id'])
                field = 'input_revision' if source == 'organisation' else 'availability_revision'
            elif source == 'department':
                row, field = db.get(m.Department, h.seed['department_id']), 'catalogue_revision'
            else:
                row, field = db.get(m.Workspace, workspace['id']), 'content_revision'
            setattr(row, field, getattr(row, field) + 1)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        bind(db, plan, replay=True)
        with pytest.raises(Problem) as error:
            snapshots.domain_snapshot(db, row)
        assert error.value.code == 'STALE_INPUTS'


def test_summary_cache_checks_actual_findings_and_acceptances_and_returns_detached_values():
    from copy import deepcopy
    from packages.domain.rota_summary import rota_summary
    check = {'fingerprint': 'same', 'valid': True, 'findings': [], 'coverage': [
        {'shift_id': 'session', 'required': 2, 'available_eligible': 2, 'status': 'covered'}]}
    cache = {}
    first = rota_summary(check, cache=cache)
    first['fix_list']['causes'].clear()
    assert rota_summary(check, cache=cache) == rota_summary(check)
    updated = {**check, 'findings': [{'code': 'REST', 'severity': 'warning', 'rule_id': 'rule',
                                    'shift_id': 'session', 'person_id': 'person', 'start': '2026-10-04T00:00:00Z'}]}
    accepted = {'REST|rule|session|person|2026-10-04T00:00:00Z'}
    assert rota_summary(updated, cache=cache) == rota_summary(updated)
    assert rota_summary(updated, accepted_issues=accepted, cache=cache) == rota_summary(updated, accepted_issues=accepted)
    assert rota_summary(updated, compact=True, cache=cache) == rota_summary(updated, compact=True)
    scoped = {**updated, 'scope': 'command_changes', '_command_scope': {'shifts': {'session'}, 'people': {'person'}}}
    assert rota_summary(scoped, cache=cache) == rota_summary(scoped)
    frozen = deepcopy(rota_summary(updated, cache=cache))
    rota_summary(updated, cache=cache)['findings'].clear()
    assert rota_summary(updated, cache=cache) == frozen


def test_apply_reacquires_stale_inputs_outside_writer_and_retains_proposal(harness, monkeypatch):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services import generation_reconciliation
    h = harness
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}])
    prepare = generation_reconciliation.prepare
    acquired, original_snapshot = [], snapshots._domain_snapshot

    def snapshot(db, *args, **kwargs):
        acquired.append(bool(db.connection().info.get('rota_write')))
        return original_snapshot(db, *args, **kwargs)

    def prepared(db, actor, command):
        result = prepare(db, actor, command)
        with transaction(h.app.state.engine, write=True) as current:
            current.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
        return result

    monkeypatch.setattr(snapshots, '_domain_snapshot', snapshot)
    monkeypatch.setattr(generation_reconciliation, 'prepare', prepared)
    result = post(h, 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                  'reason': 'Retain stale proposal'}, revision=workspace['revision'])
    assert acquired and not any(acquired)
    assert result['proposal_retained'] and result['reconciliation']['status'] == 'revalidation_required'
    assert not h.detail(workspace)['shifts'][0]['participants']


def test_repeated_apply_input_changes_keep_the_existing_retained_reply(harness, monkeypatch):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services import generation_reconciliation
    h = harness
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}])
    calls = []
    def changed_after(function):
        def wrapped(db, actor, command, *args):
            result = function(db, actor, command, *args)
            calls.append(function.__name__)
            with transaction(h.app.state.engine, write=True) as current:
                current.get(m.Organisation, h.seed['organisation_id']).input_revision += 1
            return result
        return wrapped
    monkeypatch.setattr(generation_reconciliation, 'prepare', changed_after(generation_reconciliation.prepare))
    monkeypatch.setattr(generation_reconciliation, 'refresh_inputs', changed_after(generation_reconciliation.refresh_inputs))
    result = post(h, 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                  'reason': 'Retain a changing proposal'}, revision=workspace['revision'])
    assert calls == ['prepare', 'refresh_inputs', 'refresh_inputs']
    assert result['proposal_retained'] and result['reconciliation']['status'] == 'revalidation_required'
    assert result['proposal_action'] == 'reconcile_and_apply' and result['proposal_reviewable']
    assert not h.detail(workspace)['shifts'][0]['participants']


@pytest.mark.parametrize('action', ['edit', 'apply'])
def test_retained_check_time_uses_the_final_writer_clock(harness, monkeypatch, action):
    from apps.api.services import jobs
    from tests.clock_support import freeze_clock
    h = harness
    at = freeze_clock(monkeypatch)
    clock = [at]
    freeze_clock(monkeypatch, lambda: clock[0])
    workspace = h.shift(h.workspace(), people=[] if action == 'apply' else None)['workspace']
    shift = h.detail(workspace)['shifts'][0]
    if action == 'apply':
        job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
            'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}])
    changed = planning.changed
    def final_clock(db, *args, **kwargs):
        if db.connection().info.get('rota_write'):
            clock[0] = at + timedelta(minutes=1)
        return changed(db, *args, **kwargs)
    monkeypatch.setattr(planning, 'changed', final_clock)
    monkeypatch.setattr(jobs, 'changed', final_clock)
    if action == 'edit':
        result = post(h, 'shift.update', edit_payload(workspace, shift), shift['id'], workspace['revision'])
    else:
        result = post(h, 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
                      'reason': 'Keep the existing check time'}, revision=workspace['revision'])
    assert result['validation']['evaluated_at'] == clock[0].isoformat()
    assert clock[0] == at + timedelta(minutes=1)
# Include this file in hosted supervision and team-lead qualification.
