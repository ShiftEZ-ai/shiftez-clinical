"""Published context constrains the solver without becoming a live draft preview."""
import json

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import claim_job, prepare_deferred_generation
from packages.domain.cp_sat import generate
from packages.domain.solver_rules import COMPILER_VERSION
from packages.domain.validation import evaluate


pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)


def prepare_job(h, job):
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    assert claim is not None and claim['id'] == job['id']
    assert claim['input']['_deferred_generation']
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)


def published_context(h):
    foreign = h.shift(h.workspace('Other department publication'))['workspace']
    shifts = h.detail(foreign)['shifts']
    validation = evaluate(h.snapshot(foreign))
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Other clinical department')
        db.add(department)
        db.flush()
        workspace = db.get(m.Workspace, foreign['id'])
        workspace.department_id, workspace.status = department.id, 'published'
        db.add(m.Publication(department_id=department.id, workspace_id=workspace.id, sequence=1,
            starts_on=workspace.starts_on, ends_on=workspace.ends_on,
            published_by=h.seed['identities']['coordinator'], snapshot={'shifts': shifts},
            validation=validation, fingerprint='0' * 64))
    return shifts[0]


@pytest.mark.parametrize('feasibility_only', [True, False])
def test_foreign_published_context_never_enters_live_or_final_job_payloads(h, feasibility_only):
    foreign = published_context(h)
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    own = h.detail(workspace)['shifts']
    selected, unselected = own
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'session_ids': [selected['id']]}, revision=workspace['revision'])['job']
    prepare_job(h, job)
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
    assert foreign['id'] not in snapshot['focus_ids']
    assert foreign['id'] in {shift['id'] for shift in snapshot['shifts']}
    progress = []
    result = generate(snapshot, 10, workers=1, feasibility_only=feasibility_only, progress=progress.append)
    assert result['status'] == 'candidate_feasible', result
    updates = [update for update in progress if update.get('events')]
    assert updates
    assert foreign['id'] not in json.dumps(progress)
    focus = set(snapshot['focus_ids'])
    assert {row['shift_id'] for row in result.get('candidate_evidence', [])} <= focus
    kept = {'shift_id': unselected['id'], 'person_id': unselected['participants'][0]['person_id']}
    for update in updates:
        assert kept in update['solution']['pairs']
        assert {pair['shift_id'] for pair in update['solution']['pairs']} <= focus
        for event in update['events']:
            assert {pair['shift_id'] for pair in event['adds'] + event['removes']} <= focus
    # Foreign published work still blocks an overlapping assignment to that person.
    assert all(row['participant']['person_id'] != foreign['participants'][0]['person_id']
               for row in result['additions'] if row['shift_id'] == selected['id'])
    assert {row['shift_id'] for row in result['additions']} == {selected['id']}
    assert not result['removals']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
        row.status, row.progress = 'running', updates[-1]
    live = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert kept in live['progress']['solution']['pairs']
    assert foreign['id'] not in json.dumps(live)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.status, row.result = 'succeeded', result
    final = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert final['result']['additions']
    assert foreign['id'] not in json.dumps(final)
    evidence = h.get('coordinator', '/api/jobs/' + job['id'] + '/evidence')
    assert {row['shift_id'] for row in evidence['candidates']} <= focus
    assert foreign['id'] not in json.dumps(h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id']))


@pytest.mark.parametrize('scope', ['draft', 'empty', 'missing', 'unprepared'])
def test_stored_live_progress_is_projected_to_the_entire_draft_without_mutation(h, scope):
    from tests.test_autoschedule_live_worker import improvement

    foreign = published_context(h)
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    own = h.detail(workspace)['shifts']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'session_ids': [own[0]['id']]}, revision=workspace['revision'])['job']
    if scope != 'unprepared':
        prepare_job(h, job)
    pairs = [{'shift_id': shift['id'], 'person_id': h.seed['people'][0]} for shift in [*own, foreign]]
    samples = [{'shift_id': pair['shift_id']} for pair in pairs]
    progress = {'stage': 'improving', 'events': [{**improvement(7), 'adds': pairs, 'removes': pairs}],
                'solution': {'seq': 7, 'pairs': pairs},
                'precheck_issues': [{'classification': 'outside_scope', 'code': 'TIME_OVERLAP',
                                     'count': len(samples), 'samples': samples}]}
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        snapshot = dict(row.input_snapshot)
        if scope == 'empty':
            snapshot['focus_ids'] = []
        elif scope == 'missing':
            snapshot.pop('focus_ids')
        elif scope == 'unprepared':
            assert snapshot['_deferred_generation'] and 'focus_ids' not in snapshot
        row.input_snapshot, row.status, row.progress = snapshot, 'running', progress
    response = h.get('coordinator', '/api/jobs/' + job['id'])
    detail = response['job']['progress']
    expected = pairs[:2] if scope == 'draft' else []
    assert detail['solution'] == {'seq': 7, 'pairs': expected}
    assert detail['events'] == [{**progress['events'][0], 'adds': expected, 'removes': expected}]
    assert detail['precheck_issues'] == [{**progress['precheck_issues'][0],
                                        'samples': samples[:2] if scope == 'draft' else []}]
    assert foreign['id'] not in json.dumps(response)
    listing = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])
    assert foreign['id'] not in json.dumps(listing)
    listed = next(row for row in listing['jobs'] if row['id'] == job['id'])
    assert not {'events', 'solution', 'precheck_issues'} & listed['progress'].keys()
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).progress == progress
