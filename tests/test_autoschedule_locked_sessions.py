import copy
from dataclasses import replace

import pytest

from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.jobs import create_job
from apps.worker.main import process_one
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate
from packages.domain.generation import citation_finding_key
from packages.domain.validation import evaluate


def locked_and_editable(h, *, seed_restricted=False):
    typ = h.configure('assignment_types', {'name': 'Two-person clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}]}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[h.seed['people'][0]], seed_restricted=seed_restricted)['workspace']
    locked = h.snapshot(w)['shifts'][0]['id']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                typ=typ['id'], people=[])['workspace']
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [locked],
        'action': 'lock', 'reason': 'Preserve the manual assignment'}, revision=w['revision'])['workspace']
    editable = next(s['id'] for s in h.snapshot(w)['shifts'] if s['id'] != locked)
    return w, locked, editable


@pytest.mark.parametrize('cover_rule', [False, True], ids=['session-cover', 'mandatory-cover-rule'])
def test_locked_understaffed_session_stays_open_while_other_sessions_are_filled(h, cover_rule):
    if cover_rule:
        h.policy({'kind': 'minimum_cover', 'minimum': 2})
    w, locked, editable = locked_and_editable(h)
    data = h.snapshot(w)
    original = copy.deepcopy(data)
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['optimality_proven'] and not result['validation']['valid']
    assert data == original
    assert result['removals'] == []
    assert {a['shift_id'] for a in result['additions']} == {editable}
    assert len(result['additions']) == 2
    assert all(f['shift_id'] == locked for f in result['validation']['findings'] if f['severity'] == 'error')
    open_place, = result['summary']['open_places']
    assert (open_place['shift_id'], open_place['required'], open_place['open']) == (locked, 2, 1)
    assert open_place['message'] == 'This session is locked with 1 of 2 people. Unlock it or add someone by hand.'
    shortage = next(d for d in result['diagnostics'] if d['shift_id'] == locked)
    assert shortage['message'] == open_place['message']


def test_locked_gap_candidate_applies_only_to_editable_sessions(h):
    w, locked, editable = locked_and_editable(h)
    before = h.snapshot(w)
    locked_participants = next(s['participants'] for s in before['shifts'] if s['id'] == locked)
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'],
            payload={'workspace_id': w['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    process_one(h.app.state.engine, settings=replace(h.settings, generation_engine='cp_sat'), solver_workers=1)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_with_gaps', result
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Fill editable sessions and preserve the locked gap'}, revision=w['revision'])['workspace']
    after = h.snapshot(applied)
    assert next(s['participants'] for s in after['shifts'] if s['id'] == locked) == locked_participants
    assert len(next(s['participants'] for s in after['shifts'] if s['id'] == editable)) == 2
    assert not applied['validation']['valid']


def test_locked_staffing_gap_is_reported_without_blocking_editable_feasibility(h):
    w, locked, editable = locked_and_editable(h)
    result = generate(h.snapshot(w), 5, feasibility_only=True)
    assert result['status'] == 'candidate_with_gaps', result
    assert any(d.get('shift_id') == locked for d in result.get('diagnostics', []))
    assert result['removals'] == []
    assert result['additions'] and all(a['shift_id'] == editable for a in result['additions'])
    assert not result.get('optimality_proven')


def test_locked_staffing_gap_never_bypasses_a_restriction_on_kept_work(h):
    h.policy({'kind': 'restriction', 'weekdays': [6]}, {'person_id': h.seed['people'][0]})
    w, locked, editable = locked_and_editable(h, seed_restricted=True)
    data = h.snapshot(w)
    original = {citation_finding_key(f) for f in evaluate(data)['findings']
                if f['severity'] == 'error' and f['shift_id'] == locked}
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['validation']['valid'] and not result['optimality_proven']
    assert result['removals'] == []
    assert len(result['additions']) == 2 and {a['shift_id'] for a in result['additions']} == {editable}
    assert {citation_finding_key(f) for f in result['validation']['findings'] if f['severity'] == 'error'} == original
    assert any(f['code'] == 'RESTRICTION' and f['shift_id'] == locked for f in result['issues'])


def test_locked_staffing_gap_never_bypasses_pooled_minimum_cover(h):
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:1]})
    w = h.shift(h.workspace(), people=[])['workspace']
    locked = h.snapshot(w)['shifts'][0]['id']
    w = h.shift(w, people=[])['workspace']
    data = h.snapshot(w)
    next(s for s in data['shifts'] if s['id'] == locked)['locked'] = True
    person = h.seed['people'][0]
    data['people'] = {person: data['people'][person]}
    result = generate(data, 5)
    assert result['status'] == 'requirements_conflict', result
    assert result['additions'] == result['removals'] == []


def test_locked_service_group_peer_supplies_fixed_cover_for_editable_session(h):
    """A fixed overlapping peer counts toward a service-group minimum."""
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:1]})
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    w = h.shift(w, people=[])['workspace']
    data = h.snapshot(w)
    locked = next(row for row in data['shifts'] if row['participants'])
    editable = next(row for row in data['shifts'] if not row['participants'])
    locked['locked'] = True
    data['generation_session_ids'] = [editable['id']]

    result = generate(data, 5, feasibility_only=True)

    assert result['status'] == 'candidate_feasible', result
