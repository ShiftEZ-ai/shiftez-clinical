"""One frozen publication baseline for amendment search, comparison and governed review."""
import copy
import json

import pytest

pytestmark = pytest.mark.usefixtures('score_check_enabled', 'cp_sat_engine')
from ortools.sat.python import cp_model
from sqlalchemy import func, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services.generation_resume import resume_current
from apps.api.settings import quota_policy_limit
from apps.worker.main import claim_job
from packages.contracts import schemas as s
from packages.domain.cp_sat_score import SCORE_SCALE, stability_component
from packages.domain.score import publication_baseline, schedule_score
from packages.domain.validation import ENGINE_VERSION, evaluate
from tests.test_schedule_score import data, shift
from tests.test_score_check_benchmark import imported


def amendment(h):
    publication = h.publish(h.shift(h.workspace('Immutable score baseline'))['workspace'])['publication_id']
    with transaction(h.app.state.engine, write=True) as db:
        options = s.GenerationOptions.model_validate(db.get(m.Department, h.seed['department_id']).generation_defaults or {})
        options.scoring.weights = s.ScoreWeights(coverage=0, rules=0, fairness=0, preferences=0, stability=1)
        db.get(m.Department, h.seed['department_id']).generation_defaults = options.model_dump(mode='json')
    source = imported(h, publication)
    queued = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']}, revision=source['revision'])
    return publication, source, queued['workspace'], queued['job']


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_publication_baseline_frozen_in_empty_auto_and_unchanged_through_review(request, backend):
    h = request.getfixturevalue(backend)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = True
    publication_id, source, auto, job = amendment(h)
    with transaction(h.app.state.engine) as db:
        queued_snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
        evidence = queued_snapshot['score_baseline']
        # Generation requests are intentionally deferred; the worker materialises the full
        # snapshot later, while the queued request retains only immutable scoring evidence.
        frozen = snapshots.domain_snapshot(db, db.get(m.Workspace, auto['id']))
        rows = [row for row in frozen['shifts'] if row['id'] in frozen['focus_ids']]
        assert len(rows) == 1 and rows[0]['participants'] == []
        sid = rows[0]['id']
        original, replacement = h.seed['people'][:2]
        assert evidence['publication_id'] == publication_id and evidence['state'] == 'available'
        assert publication_baseline(json.loads(json.dumps(frozen))) == {(sid, original): (False, False)}
        loss, present, baseline = stability_component(cp_model.CpModel(), frozen,
            {(sid, original): 0, (sid, replacement): 1}, {}, {})
        assert (loss, present) == (SCORE_SCALE, 1)
        assert baseline == publication_baseline(frozen)
        kept = stability_component(cp_model.CpModel(), frozen, {(sid, original): 1}, {}, {})
        assert kept[:2] == (0, 1)
        source_before = snapshots.workspace_content(db, db.get(m.Workspace, source['id']))
    h.command('coordinator', 'job.cancel', {'reason': 'Review a deterministic changed assignment'}, target=job['id'])
    body = {key: rows[0][key] for key in s.ShiftInput.model_fields if key != 'participants'}
    changed = h.command('coordinator', 'shift.update', {'workspace_id': auto['id'], 'shift_id': sid, **body,
        'participants': [{'person_id': replacement, 'start': rows[0]['start'], 'end': rows[0]['end']}]},
        revision=auto['revision'])['workspace']
    current = h.snapshot(changed)
    scoring = h.get('admin', '/api/me')['department']['generation_defaults']['scoring']
    expected = schedule_score(current, evaluate(current), {'scoring': scoring})
    assert expected['components']['stability']['value'] == 0
    assert 'score' not in h.detail(changed)['workspace']['validation']
    rerun = h.command('coordinator', 'job.create', {'workspace_id': auto['id'], 'kind': 'generation'},
        revision=changed['revision'])['job']
    with transaction(h.app.state.engine) as db:
        refreshed = db.get(m.Job, rerun['id']).input_snapshot
        assert refreshed['score_baseline'] == evidence
        current = snapshots.domain_snapshot(db, db.get(m.Workspace, auto['id']))
        assert next(row for row in current['shifts'] if row['id'] == sid)['participants'][0]['person_id'] == replacement
        assert db.get(m.Job, job['id']).input_snapshot == queued_snapshot
    h.command('coordinator', 'job.cancel', {'reason': 'Finish deterministic review parity check'}, target=rerun['id'])
    report = h.command('coordinator', 'score_check.benchmark', {'workspace_ids': [source['id'], auto['id']]}, status=409)
    assert report['error']['code'] == 'BENCHMARK_SCOPE'
    assert expected['score'] == 0 and expected['components']['stability']['value'] == 0
    checked = h.command('coordinator', 'workspace.validate', {'reason': 'Check the same assignments'},
                        target=changed['id'], revision=changed['revision'])
    assert checked['validation']['scope'] == 'published_changes' and 'score' not in checked['validation']
    assert h.get('coordinator', f"/api/workspaces/{auto['id']}/summary")['score'] is None
    submitted = h.lifecycle(changed, 'submit')['workspace']
    assert 'score' not in h.detail(submitted)['workspace']['validation']
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    assert 'score' not in h.detail(approved)['workspace']['validation']
    published_id = h.lifecycle(approved, 'publish')['publication_id']
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published_id)
        assert publication.validation['scope'] == 'published_changes' and 'score' not in publication.validation
        assert publication.snapshot['score_baseline'] == evidence
        assert snapshots.domain_snapshot(db, db.get(m.Workspace, auto['id']))['score_baseline'] == evidence
        assert snapshots.workspace_content(db, db.get(m.Workspace, source['id'])) == source_before
        # The original retained publication predates this input; it is never rewritten or given a fabricated baseline.
        original_publication = db.get(m.Publication, publication_id)
        assert 'score_baseline' not in original_publication.snapshot
        assert 'score_baseline' not in snapshots.domain_snapshot(db, db.get(m.Workspace, original_publication.workspace_id))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).score_check_enabled = False
    with transaction(h.app.state.engine) as db:
        frozen_publication = copy.deepcopy(db.get(m.Publication, published_id).snapshot)
        view = snapshots.domain_snapshot(db, db.get(m.Workspace, auto['id']))
        assert view['score_baseline'] == evidence
        assert schedule_score(view, evaluate(view), {'scoring': scoring}) == expected
        assert db.get(m.Publication, published_id).snapshot == frozen_publication


@pytest.mark.parametrize('state,assignments,present,value', [
    ('unavailable', None, 0, None),
    ('available', [], 1, 1),
    ('partial', [{'shift_id': 's', 'person_id': 'a', 'team_lead': False, 'overtime': False}], 1, 0),
])
def test_unavailable_empty_and_partial_baselines_match_the_compiled_component(state, assignments, present, value):
    snapshot = data([shift('s', 4, ['b'])])
    snapshot['score_baseline'] = {'publication_id': 'retained', 'state': state, 'assignments': assignments}
    loss, actual_present, baseline = stability_component(cp_model.CpModel(), snapshot,
        {('s', 'a'): 0, ('s', 'b'): 1}, {}, {})
    assert actual_present == present and baseline == publication_baseline(snapshot)
    score = schedule_score(snapshot, {'findings': [], 'coverage': []})
    assert score['components']['stability']['value'] == value
    if present:
        assert loss == (1 - value) * SCORE_SCALE


@pytest.mark.parametrize('before,after,selected', [
    ((False, True), (False, False), True),
    ((False, False), (False, True), True),
    ((True, False), (False, False), True),
    ((False, False), (True, False), True),
    ((False, False), (True, True), True),
    ((True, True), (True, True), True),
    ((True, True), (True, True), False),
])
def test_constant_publication_flags_and_removals_match_compiled_stability(before, after, selected):
    snapshot = data([shift('s', 4, ['a'] if selected else [])])
    key = ('s', 'a')
    current = {'person_id': 'a', 'team_lead': after[0], 'overtime': after[1]}
    if selected:
        snapshot['shifts'][0]['participants'][0].update(current)
    snapshot['score_baseline'] = {'state': 'available', 'assignments': [
        {'shift_id': 's', 'person_id': 'a', 'team_lead': before[0], 'overtime': before[1]}]}
    loss, present, _ = stability_component(cp_model.CpModel(), snapshot, {key: int(selected)}, {}, {},
                                          participants={key: current})
    changed = int(not selected or before != after)
    assert (loss, present) == (changed * SCORE_SCALE, 1)
    checked = schedule_score(snapshot, {'findings': [], 'coverage': []})
    assert checked['components']['stability']['value'] == 1 - changed


def test_new_assignments_do_not_change_publication_stability():
    snapshot = data([shift('s', 4, ['a', 'b'])])
    snapshot['shifts'][0]['participants'][1].update(team_lead=True, overtime=True)
    snapshot['score_baseline'] = {'state': 'available', 'assignments': [
        {'shift_id': 's', 'person_id': 'a', 'team_lead': False, 'overtime': False}]}
    participants = {('s', row['person_id']): row for row in snapshot['shifts'][0]['participants']}
    compiled = stability_component(cp_model.CpModel(), snapshot, dict.fromkeys(participants, 1), {}, {}, participants=participants)
    assert compiled[:2] == (0, 1)
    assert schedule_score(snapshot, {'findings': [], 'coverage': []})['components']['stability']['value'] == 1


def test_pre_baseline_amendment_generation_preserves_frozen_execution_without_mutating_assignments(h):
    _publication, _source, auto, job = amendment(h)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, auto['id'])
        department = db.get(m.Department, workspace.department_id)
        organisation = db.get(m.Organisation, department.organisation_id)
        legacy = snapshots.snapshot_hash({'engine_version': ENGINE_VERSION, 'quota_policy_limit': quota_policy_limit(),
            'workspace_id': workspace.id, 'content_revision': workspace.content_revision,
            'input_revision': organisation.input_revision, 'availability_revision': organisation.availability_revision,
            'base_publication_id': workspace.base_publication_id})
        queued = db.get(m.Job, job['id'])
        old_input = dict(queued.input_snapshot)
        old_input.pop('score_baseline')
        old_input['fingerprint'] = legacy
        queued.input_snapshot, queued.input_fingerprint = old_input, legacy
        db.flush()
        assert resume_current(db, queued)
        claim = claim_job(db)
        assert claim['id'] == queued.id and claim['input']['fingerprint'] == legacy
        assert queued.status == 'running' and queued.input_snapshot == old_input
        assert snapshots.workspace_shifts(db, auto['id'])[0]['participants'] == []


def test_duplicate_auto_admission_returns_existing_draft_and_job_after_revision_guard(h):
    source = imported(h)
    request = {'workspace_id': source['id']}
    first = h.command('coordinator', 'score_check.auto', request, revision=source['revision'], key='same-auto-request')
    replay = h.command('coordinator', 'score_check.auto', request, revision=source['revision'], key='same-auto-request')
    assert replay['workspace']['id'] == first['workspace']['id'] and replay['job']['id'] == first['job']['id']
    stale = h.command('coordinator', 'score_check.auto', request, revision=source['revision'] - 1, status=409)
    assert stale['error']['code'] == 'STALE_REVISION'
    duplicate = h.command('coordinator', 'score_check.auto', request, revision=source['revision'], status=409)
    assert duplicate['error']['code'] == 'BENCHMARK_AUTO_EXISTS'
    assert duplicate['error']['details'] == {'workspace_id': first['workspace']['id'],
        'revision': first['workspace']['revision'], 'job_id': first['job']['id'], 'job_status': 'queued'}
    assert h.get('coordinator', '/api/benchmark?workspace=' + source['id'])['sides'][1]['workspace_id'] == first['workspace']['id']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Workspace)) == 2
        assert db.scalar(select(func.count()).select_from(m.Job)) == 1


@pytest.mark.parametrize('difference', ['demand', 'context'])
def test_auto_admission_does_not_reuse_or_modify_an_unrelated_draft(h, difference):
    source = imported(h)
    first = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']}, revision=source['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        unrelated = db.get(m.Workspace, first['workspace']['id'])
        if difference == 'demand':
            db.scalar(select(m.Shift).where(m.Shift.workspace_id == unrelated.id)).attributes = {'different_demand': True}
        else:
            unrelated.target_overrides = [{'policy_id': m.uid(), 'policy_revision': 1,
                'person_id': h.seed['people'][0], 'amount': 5}]
        untouched = snapshots.workspace_content(db, unrelated)
    second = h.command('coordinator', 'score_check.auto', {'workspace_id': source['id']}, revision=source['revision'])
    assert second['workspace']['id'] != first['workspace']['id']
    assert h.get('coordinator', '/api/benchmark?workspace=' + source['id'])['sides'][1]['workspace_id'] == second['workspace']['id']
    with transaction(h.app.state.engine) as db:
        assert snapshots.workspace_content(db, db.get(m.Workspace, first['workspace']['id'])) == untouched
        assert db.get(m.Job, first['job']['id']).status == 'queued'
