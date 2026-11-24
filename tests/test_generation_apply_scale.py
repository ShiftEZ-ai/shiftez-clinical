"""Batch proposal writes preserve assignment contracts, audit and atomic undo."""
from .clock_support import freeze_clock
from datetime import timedelta
import re

import pytest
from sqlalchemy import event, func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import json_value, record
from apps.worker.main import claim_job, prepare_deferred_generation
from packages.contracts.schemas import ParticipantInput
from packages.domain.time import aware


def completed_proposal(h, workspace, additions, removals=()):
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'],
        'kind': 'generation'}, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status = 'succeeded'
        row.result = {'status': 'candidate_with_gaps', 'additions': additions, 'removals': list(removals)}
    return job


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('missing', ['unprepared', 'unprepared_scoped', 'shifts', 'people', 'focus_ids',
                                   'scoped_shifts', 'scoped_focus_ids'])
def test_apply_refuses_missing_frozen_inputs_without_writes(request, backend, missing):
    h = request.getfixturevalue(backend)
    workspace = h.shift(h.workspace(), people=h.seed['people'][:1])['workspace']
    shift = h.snapshot(workspace)['shifts'][0]
    old = shift['participants'][0]
    payload = {'workspace_id': workspace['id'], 'kind': 'generation'}
    if missing == 'unprepared_scoped' or missing.startswith('scoped_'):
        payload['generation_scope'] = {'conditions': {'conditions': [
            {'field': 'person_ids', 'values': [h.seed['people'][1]]}]}}
    job = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    if not missing.startswith('unprepared'):
        with transaction(h.app.state.engine, write=True) as db:
            claim = claim_job(db)
        prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        if missing.startswith('unprepared'):
            assert row.input_snapshot['_deferred_generation'] and 'shifts' not in row.input_snapshot
        else:
            row.input_snapshot = {key: value for key, value in row.input_snapshot.items()
                                  if key != missing.removeprefix('scoped_')}
        row.status = 'succeeded'
        row.result = {'status': 'candidate_with_gaps', 'additions': [{'shift_id': shift['id'],
            'participant': {'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}}],
            'removals': [{'shift_id': shift['id'], 'assignment_id': old['id'], 'person_id': old['person_id']}]}

    def retained():
        with transaction(h.app.state.engine) as db:
            return {model.__tablename__: [record(row) for row in db.scalars(select(model).order_by(model.id))]
                    for model in (m.Workspace, m.Shift, m.Assignment, m.Job, m.AuditEvent, m.ChangeSet, m.CommandReceipt)}

    before = retained()
    writes = []
    def record_write(connection, cursor, statement, parameters, context, executemany):
        if any(statement.lstrip().lower().startswith(f'{verb} {table}')
               for verb in ('insert into', 'update', 'delete from') for table in before):
            writes.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', record_write)
    try:
        reply = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
            'job_id': job['id'], 'reason': 'Reject a proposal without frozen inputs'},
            revision=workspace['revision'], status=409)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', record_write)
    assert reply['error']['code'] == 'STALE_INPUTS'
    assert reply['error']['message'] == 'This proposal has no complete frozen inputs; run auto-schedule again'
    assert writes == []
    assert retained() == before


@pytest.mark.parametrize('legacy_projection', [False, True])
def test_completed_proposal_result_write_budget(h, legacy_projection):
    workspace = h.workspace()
    for day in range(4, 7):
        for hour in range(8, 16, 2):
            workspace = h.shift(workspace, people=[],
                start=f'2026-10-{day:02}T{hour:02}:00:00+03:00',
                end=f'2026-10-{day:02}T{hour + 1:02}:00:00+03:00')['workspace']
    before = h.snapshot(workspace)['shifts']
    people = h.seed['people']
    additions = [{'shift_id': row['id'], 'participant': {
        'person_id': people[i % len(people)], 'start': row['start'], 'end': row['end']}}
        for i, row in enumerate(before)]
    job = completed_proposal(h, workspace, additions)
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.result = {**row.result, 'candidate_evidence': [
            {'shift_id': shift['id'], 'person_id': person,
             'exclusions': [{'code': 'UNAVAILABLE', 'detail': 'Synthetic evidence. ' * 64}]}
            for shift in before for person in people for _ in range(8)]}

    result_writes = []
    def count_result_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith('update jobs set '):
            result_writes.extend('result' in values and job['id'] in values.values()
                                 for values in (context.compiled_parameters or ()))

    event.listen(engine, 'before_cursor_execute', count_result_write)
    try:
        applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
            'job_id': job['id'], 'reason': 'Apply synthetic proposal'}, revision=workspace['revision'])
        assert applied['reconciliation']['status'] == 'applied'
        apply_writes = sum(result_writes)
        result_writes.clear()
        with transaction(engine, write=legacy_projection) as db:
            saved_result = db.get(m.Job, job['id']).result
            if legacy_projection:
                from sqlalchemy import null, update
                db.execute(update(m.Job).where(m.Job.id == job['id']).values(
                    read_metadata=null(), result_summary=null()))
        undone = h.lifecycle(applied['workspace'], 'undo')['workspace']
    finally:
        event.remove(engine, 'before_cursor_execute', count_result_write)

    assert (apply_writes, sum(result_writes)) == (1, 0)
    assert h.snapshot(undone)['shifts'] == before
    with transaction(engine) as db:
        assert db.get(m.Job, job['id']).result == saved_result
    for suffix in ('', '?full=true'):
        detail = h.get('coordinator', '/api/jobs/' + job['id'] + suffix)['job']
        assert detail['status'] == 'succeeded'
        assert detail['reconciliation']['status'] == detail['result']['reconciliation']['status'] == 'undone'
        assert detail['result']['additions'] == additions
    listed = next(row for row in h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
                  if row['id'] == job['id'])
    assert listed['result']['reconciliation']['status'] == 'undone'

    result_writes.clear()
    event.listen(engine, 'before_cursor_execute', count_result_write)
    try:
        redone = h.command('coordinator', 'workspace.redo', {'reason': 'Restore synthetic proposal'},
                           undone['id'], undone['revision'])['workspace']
    finally:
        event.remove(engine, 'before_cursor_execute', count_result_write)

    for suffix in ('', '?full=true'):
        detail = h.get('coordinator', '/api/jobs/' + job['id'] + suffix)['job']
        assert detail['status'] == 'applied'
        assert detail['reconciliation']['status'] == detail['result']['reconciliation']['status'] == 'applied'
        assert detail['result']['applied_content_revision'] == redone['content_revision']
        assert detail['reconciliation']['applied_content_revision'] == redone['content_revision']
        assert detail['result']['additions'] == additions
    listed = next(row for row in h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
                  if row['id'] == job['id'])
    assert listed['status'] == listed['result']['reconciliation']['status'] == 'applied'
    assert listed['result']['applied_content_revision'] == redone['content_revision']
    assert sum(result_writes) == 0
    with transaction(engine) as db:
        assert db.get(m.Job, job['id']).result == saved_result

    undone_again = h.lifecycle(redone, 'undo')['workspace']
    assert h.snapshot(undone_again)['shifts'] == before
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['reconciliation']['status'] == 'undone'
    reapplied = h.command('coordinator', 'generation.apply', {'workspace_id': undone_again['id'],
        'job_id': job['id'], 'reason': 'Reapply synthetic proposal'}, revision=undone_again['revision'])
    assert reapplied['reconciliation']['status'] == 'applied'
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['status'] == detail['reconciliation']['status'] == 'applied'
    assert detail['result']['applied_content_revision'] == reapplied['workspace']['content_revision']


@pytest.mark.postgres
def test_apply_reads_unchanged_proposal_payload_once(pg):
    workspace = pg.shift(pg.workspace(), people=[])['workspace']
    shift = pg.snapshot(workspace)['shifts'][0]
    job = completed_proposal(pg, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': pg.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}])
    payload_reads = []

    def count_payload_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT'):
            fields = set(re.findall(r'\bjobs\.(input_snapshot|result)\b(?=\s*(?:,|AS\b|FROM\b))', statement))
            if fields:
                payload_reads.append(fields)

    engine = pg.app.state.engine
    event.listen(engine, 'before_cursor_execute', count_payload_reads)
    try:
        reply = pg.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
            'job_id': job['id'], 'reason': 'Apply the reviewed proposal'}, revision=workspace['revision'])
    finally:
        event.remove(engine, 'before_cursor_execute', count_payload_reads)

    assert reply['reconciliation']['status'] == 'applied'
    assert payload_reads == [{'input_snapshot', 'result'}]


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_apply_refuses_addition_absent_from_frozen_shifts_without_writes(h, monkeypatch):
    from apps.api.auth import authenticate
    from apps.api.services import generation_reconciliation
    from packages.contracts.schemas import Command

    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.snapshot(workspace)['shifts'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}])
    payload = {'workspace_id': workspace['id'], 'job_id': job['id'], 'reason': 'Reject missing frozen session'}
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        plan = generation_reconciliation.prepare(db, actor, Command(kind='generation.apply',
            payload=payload, expected_revision=workspace['revision']))
    assert plan['receipt']['addition_indexes'] == [0]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'shifts': []}
    # The writer must refuse incomplete frozen inputs even with a previously prepared plan.
    monkeypatch.setattr(generation_reconciliation, 'prepare', lambda *args: plan)

    def retained():
        with transaction(h.app.state.engine) as db:
            return {model.__tablename__: [record(row) for row in db.scalars(select(model).order_by(model.id))]
                    for model in (m.Workspace, m.Shift, m.Assignment, m.Job, m.AuditEvent, m.ChangeSet, m.CommandReceipt)}

    before = retained()
    reply = h.command('coordinator', 'generation.apply', payload, revision=workspace['revision'], status=409)
    assert reply['error']['code'] == 'STALE_INPUTS'
    assert reply['error']['message'] == 'This proposal has no complete frozen inputs; run auto-schedule again'
    assert retained() == before


def test_batch_apply_retains_participant_fields_and_one_audited_undo(h, monkeypatch):
    workspace = h.shift(h.workspace())['workspace']
    before = h.snapshot(workspace)
    shift = before['shifts'][0]
    old = shift['participants'][0]
    assert old['source'] == 'manual'
    start, end = aware(shift['start']), aware(shift['end'])
    participant = {'person_id': old['person_id'], 'start': shift['start'], 'end': shift['end'],
        'participation': 'staff', 'overtime': True, 'fixed': True, 'team_lead': False,
        'supervisor_person_id': None, 'segments': [
            {'start': start.isoformat(), 'end': (start + timedelta(hours=1)).isoformat()},
            {'start': (end - timedelta(hours=1)).isoformat(), 'end': end.isoformat()}]}
    additions = [{'shift_id': shift['id'], 'participant': participant},
                 {'shift_id': shift['id'], 'participant': {'person_id': h.seed['people'][1],
                    'start': shift['start'], 'end': shift['end']}}]
    removals = [{'shift_id': shift['id'], 'assignment_id': old['id'], 'person_id': old['person_id']}]
    job = completed_proposal(h, workspace, additions, removals)
    applied_at = m.now()
    freeze_clock(monkeypatch, lambda: applied_at)
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply reviewed synthetic replacement'}, revision=workspace['revision'])
    after = h.snapshot(applied['workspace'])['shifts'][0]
    expected = json_value(ParticipantInput.model_validate(participant).model_dump())
    assigned = next(p for p in after['participants'] if p['person_id'] == old['person_id'])
    assert {key: assigned[key] for key in expected} == expected
    assert assigned['id'] != old['id'] and assigned['revision'] == 1
    for participant in after['participants']:
        assert participant['source'] == 'auto_schedule' and participant['source_run_id'] == job['id']
        assert participant['source_actor_id'] == h.seed['identities']['coordinator']
        assert aware(participant['source_at']) == m.now()
    assert after['revision'] == shift['revision'] + 1
    with transaction(h.app.state.engine) as db:
        change = db.scalar(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'],
            m.ChangeSet.resulting_content_revision == applied['workspace']['content_revision']))
        assert change.before_snapshot['shifts'] == before['shifts']
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == 'generation.apply')) == 1
        assert db.get(m.Job, job['id']).status == 'applied'
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo reviewed synthetic replacement'},
        applied['workspace']['id'], applied['workspace']['revision'])['workspace']
    assert h.snapshot(undone)['shifts'] == before['shifts']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'


def test_batch_apply_foreign_session_rolls_back_removals_and_additions(h):
    workspace = h.shift(h.workspace())['workspace']
    other = h.shift(h.workspace('Other synthetic rota'), people=[])['workspace']
    before = h.snapshot(workspace)
    shift, foreign = before['shifts'][0], h.snapshot(other)['shifts'][0]
    old = shift['participants'][0]
    participant = {'person_id': old['person_id'], 'start': shift['start'], 'end': shift['end']}
    job = completed_proposal(h, workspace, [
        {'shift_id': shift['id'], 'participant': participant},
        {'shift_id': foreign['id'], 'participant': participant}],
        [{'shift_id': shift['id'], 'assignment_id': old['id'], 'person_id': old['person_id']}])
    rejected = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Reject synthetic foreign session'}, revision=workspace['revision'], status=409)
    assert rejected['error']['code'] == 'INVALID_SCOPE'
    assert h.snapshot(workspace) == before
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(
            m.AuditEvent.action == 'generation.apply')) == 0


@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('change', ['additions', 'removals', 'replacement'])
def test_batch_apply_revises_each_changed_session_once_and_preserves_undo(h, recurring, change):
    if recurring:
        from .test_series_reconciliation import materialise, template
        series = template(h, days=(6,))
        workspace = materialise(h, series)
    else:
        workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    # Keep ordinary, movable bookings on the template as they are initially materialised.
    if change != 'additions':
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all(m.Assignment(shift_id=shift['id'], person_id=pid,
                start=aware(shift['start']), end=aware(shift['end'])) for pid in h.seed['people'][:2])
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    before = h.snapshot(workspace)
    shift = next(row for row in before['shifts'] if row['id'] == shift['id'])
    additions = [] if change == 'removals' else [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][2], 'start': shift['start'], 'end': shift['end']}}]
    removals = [] if change == 'additions' else [{'shift_id': shift['id'],
        'assignment_id': person['id'], 'person_id': person['person_id']} for person in shift['participants']]
    job = completed_proposal(h, workspace, additions, removals)
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply reviewed changes as one session revision'}, revision=workspace['revision'])
    after = h.snapshot(applied['workspace'])['shifts']
    changed = next(row for row in after if row['id'] == shift['id'])
    if change == 'removals':
        assert applied['reconciliation']['status'] == 'unresolved'
        assert h.snapshot(workspace)['shifts'] == before['shifts']
        return
    assert changed['revision'] == shift['revision'] + 1
    assert changed['series_modified'] == recurring
    assert {row['id']: row for row in after if row['id'] != shift['id']} == {
        row['id']: row for row in before['shifts'] if row['id'] != shift['id']}
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo reviewed session changes'},
        workspace['id'], applied['workspace']['revision'])['workspace']
    assert h.snapshot(undone)['shifts'] == before['shifts']


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize(
    "visibility,purpose",
    [("shared", "alternative"), ("private", "sandbox"), ("shared", "sandbox")],
    ids=["shared", "private", "sandbox"],
)
def test_apply_unchanged_draft(h, visibility, purpose):
    start, end = ("2026-11-15", "2026-11-22") if visibility == "private" else ("2026-10-04", "2026-10-11")
    if (visibility, purpose) == ("shared", "alternative"):
        workspace = h.workspace("Unchanged draft", start=start, end=end)
    else:
        workspace = h.command("coordinator", "workspace.create", {
            "name": "Unchanged draft",
            "starts_on": start,
            "ends_on": end,
            "visibility": visibility,
            "purpose": purpose,
        })["workspace"]
    workspace = h.shift(workspace, start=f"{start}T08:00:00+03:00", end=f"{start}T12:00:00+03:00",
                        people=[])["workspace"]
    shift = h.snapshot(workspace)["shifts"][0]
    job = completed_proposal(h, workspace, [{
        "shift_id": shift["id"],
        "participant": {"person_id": h.seed["people"][0], "start": shift["start"], "end": shift["end"]},
    }])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job["id"])
        row.result = {**row.result, "status": "unknown_time_limit"}
        assert db.get(m.Workspace, workspace["id"]).revision == workspace["revision"]

    applied = h.command("coordinator", "generation.apply", {
        "workspace_id": workspace["id"],
        "job_id": job["id"],
        "reason": "Apply unchanged proposal",
    }, revision=workspace["revision"])

    assert applied["reconciliation"]["status"] == "applied"
    assert applied["workspace"]["revision"] == workspace["revision"] + 1
    assert h.snapshot(applied["workspace"])["shifts"][0]["participants"][0]["person_id"] == h.seed["people"][0]


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
def test_apply_retains_real_session_change(h):
    workspace = h.shift(h.workspace("Changed draft"), people=[])["workspace"]
    shift = h.snapshot(workspace)["shifts"][0]
    job = completed_proposal(h, workspace, [{
        "shift_id": shift["id"],
        "participant": {"person_id": h.seed["people"][0], "start": shift["start"], "end": shift["end"]},
    }])
    changed = h.command("coordinator", "shift.update", {
        "workspace_id": workspace["id"],
        "shift_id": shift["id"],
        "assignment_type_id": shift["assignment_type_id"],
        "location_id": shift["location_id"],
        "service_ids": shift["service_ids"],
        "attributes": shift["attributes"],
        "start": "2026-10-04T09:00:00+03:00",
        "end": "2026-10-04T13:00:00+03:00",
        "participants": [],
    }, revision=workspace["revision"])["workspace"]
    before = h.snapshot(changed)

    reply = h.command("coordinator", "generation.apply", {
        "workspace_id": changed["id"],
        "job_id": job["id"],
        "reason": "Retain changed proposal for review",
    }, revision=changed["revision"])

    assert reply["proposal_retained"]
    assert reply["reconciliation"]["status"] == "content_conflict"
    assert any(fact["kind"] == "shifts" and fact["id"] == shift["id"]
               for fact in reply["reconciliation"]["changed_facts"])
    assert h.snapshot(changed) == before
