"""Applying a run after its inputs changed rechecks it against the current rota: a changed session retains the
proposal without a write; retracted leave lets the still-valid suggestion apply against current inputs."""
from dataclasses import replace

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.jobs import create_job
from apps.worker.main import process_one
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate


def without_leave(value):
    """A job read without its read-time leave disclosure (`leave` on UNAVAILABLE items, from current availability)."""
    if isinstance(value, dict):
        return {k: without_leave(v) for k, v in value.items() if k != 'leave'}
    return [without_leave(v) for v in value] if isinstance(value, list) else value


def disclosed(value):
    """Whether any item at any depth carries a leave disclosure."""
    if isinstance(value, dict):
        return 'leave' in value or any(disclosed(v) for v in value.values())
    return isinstance(value, list) and any(disclosed(v) for v in value)


def leave_run(h, note):
    person = h.seed['people'][0]
    leave = h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': note})['record']
    workspace = h.shift(h.workspace('Synthetic changed-input apply'), people=[person])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation', 'generation_options': {'mode': 'fill_gaps'}}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
    return leave, workspace, job


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_job_read_discloses_leave_only_while_it_is_current_at_every_depth(request, backend):
    h = request.getfixturevalue(backend)
    note = 'Synthetic private leave note'
    leave, workspace, job = leave_run(h, note)
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job['id']).result
    read = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    # Disclosure is a read-time projection: the stored evidence carries none, and the read adds it in nested
    # Fix-list and summary items as well as the top-level issues.
    assert not disclosed(stored) and disclosed(read)
    assert {k for k, v in read.items() if disclosed(v)} >= {'issues', 'fix_list_before'}
    h.command('coordinator', 'availability.retract', {'reason': 'Leave cancelled'}, leave['id'], leave['revision'])
    fresh = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert not disclosed(fresh) and without_leave(fresh) == without_leave(read)
    assert note not in str(read) + str(fresh)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).result == stored


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('change', ['replacement', 'leave_retracted'])
def test_apply_after_input_change_retains_or_applies_against_current_inputs(request, backend, change):
    h = request.getfixturevalue(backend)
    person = h.seed['people'][0]
    leave = h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic leave for changed-input apply'})['record']
    workspace = h.shift(h.workspace('Synthetic changed-input apply'), people=[person])['workspace']
    original = h.detail(workspace)['shifts'][0]
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation', 'generation_options': {'mode': 'fill_gaps'}}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs(scheduler=lambda snapshot, **options: generate(snapshot, time_budget_seconds=5, workers=1, **options))
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['result']['status'] == 'candidate_with_gaps'
    [addition] = completed['result']['additions']
    assert not completed['result']['removals'] and addition['shift_id'] == original['id']
    if change == 'replacement':
        start, end = '2026-10-08T08:00:00+03:00', '2026-10-08T12:00:00+03:00'
        workspace = h.command('coordinator', 'shift.update', {'workspace_id': workspace['id'],
            'shift_id': original['id'], 'assignment_type_id': original['assignment_type_id'],
            'location_id': original['location_id'], 'service_ids': original['service_ids'],
            'attributes': original['attributes'], 'start': start, 'end': end,
            'participants': [{'person_id': person, 'start': start, 'end': end}]}, revision=workspace['revision'])['workspace']
    else:
        h.command('coordinator', 'availability.retract', {'reason': 'Leave cancelled'}, leave['id'], leave['revision'])
    before = h.snapshot(workspace)
    assert before['fingerprint'] != completed['input_fingerprint']
    reply = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Apply after the inputs changed'}, revision=workspace['revision'])
    receipt = reply['reconciliation']
    assert receipt['validation_current'] and receipt['current_fingerprint'] == before['fingerprint']
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job['id'])
        assert stored.result['reconciliation'] == receipt
        status, applied = stored.status, stored.result.get('applied')
    if change == 'replacement':
        assert reply['proposal_retained'] is True
        assert receipt['status'] == 'content_conflict'
        assert receipt['addition_indexes'] == receipt['removal_indexes'] == []
        assert h.snapshot(workspace) == before
        assert status == 'succeeded' and applied is None
    else:
        assert 'proposal_retained' not in reply
        assert receipt['status'] == 'applied' and receipt['addition_indexes'] == [0]
        assert status == 'applied' and applied['added_count'] == 1
        after = h.detail(workspace)
        [shift] = [row for row in after['shifts'] if row['id'] == original['id']]
        assert {p['person_id'] for p in shift['participants']} == (
            {p['person_id'] for p in original['participants']} | {addition['participant']['person_id']})
        assert after['workspace']['validation']['valid']


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_apply_after_proposed_person_becomes_unavailable_retains_without_writing(request, backend):
    h = request.getfixturevalue(backend)
    w = h.shift(h.workspace('Synthetic proposed-person leave'), people=h.seed['people'][:1])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    previous = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine)
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}}, revision=h.detail(w)['workspace']['revision'])['job']
    assert process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    [addition] = job['result']['additions']
    person = addition['participant']['person_id']
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00',
        'private_reason': 'Synthetic proposed-person leave'})
    revision = h.detail(w)['workspace']['revision']
    refused = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': previous['id'],
        'reason': 'Reject superseded synthetic proposal'}, revision=revision, status=409)
    assert refused['error']['code'] == 'RUN_SUPERSEDED'
    before = h.snapshot(w)
    reply = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Retain synthetic unavailable-person proposal'}, revision=revision)
    assert reply['proposal_retained'] is True
    assert reply['reconciliation']['status'] == 'unresolved'
    assert reply['reconciliation']['validation_current'] is True
    assert reply['reconciliation']['addition_indexes'] == reply['reconciliation']['removal_indexes'] == []
    assert h.snapshot(w) == before
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job['id'])
        assert stored.status == 'succeeded' and stored.result.get('applied') is None
        assert stored.result['reconciliation'] == reply['reconciliation']
