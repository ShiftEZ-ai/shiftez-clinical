"""Keep existing work includes usual assignees materialised from recurring sessions."""
import copy
from functools import partial

import pytest
from sqlalchemy import func, select

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.domain import cp_sat, generation, solver_rules, validation
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.generation import generate as greedy_generate
from .test_h1_template_conflicts import approved_leave, assigned_template
from .test_template_scheduler_audit import use_cp_sat


def usual_workspace(h):
    series = assigned_template(h)
    workspace = h.command('coordinator', 'workspace.create', {
        'name': 'Synthetic usual assignee preservation', 'starts_on': '2026-10-04',
        'ends_on': '2026-10-11', 'series_ids': [series['id']]})['workspace']
    usual = h.detail(workspace)['shifts'][0]
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    return workspace, usual


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
@pytest.mark.parametrize('mode', [None, 'fill_gaps'], ids=['default', 'keep-existing'])
@pytest.mark.parametrize('conflict,code', [('leave', 'UNAVAILABLE'), ('absence', 'UNAVAILABLE'),
                                         ('rest', 'MIN_REST')])
def test_usual_assignees_are_kept_and_conflicts_reported(h, engine, mode, conflict, code):
    workspace, usual = usual_workspace(h)
    pid = usual['participants'][0]['person_id']
    if conflict == 'leave':
        approved_leave(h, pid)
    elif conflict == 'absence':
        h.command('coordinator', 'availability.record', {'person_id': pid, 'kind': 'recorded_leave',
            'start': usual['start'], 'end': usual['end'], 'private_reason': 'Private synthetic absence'})
    else:
        h.policy({'kind': 'min_rest', 'hours': 12})
        workspace = h.shift(workspace, start='2026-10-04T13:00:00+03:00',
                            end='2026-10-04T17:00:00+03:00', people=[pid])['workspace']
    source = h.snapshot(workspace)
    if mode is not None:
        source['generation_options'] = {'mode': mode}
    # Legacy templates stored fixed=True without an explicit operator edit.
    for shift in source['shifts']:
        if shift['id'] == usual['id']:
            shift['participants'][0]['fixed'] = True
    original = copy.deepcopy(source)
    result = engine(source, time_budget_seconds=5)
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['removals']
    assert any(f['code'] == code and f['person_id'] == pid for f in result['issues'])
    assert any(f['code'] == code and f['person_id'] == pid for f in result['validation']['findings'])
    assert any(a['shift_id'] != usual['id'] for a in result['additions'])
    assert source == original
    assert 'Private synthetic' not in str(result)


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate])
def test_default_job_apply_reload_and_undo_keep_usual_assignee_on_leave(h, engine):
    if engine is cp_generate:
        use_cp_sat(h)
    workspace, usual = usual_workspace(h)
    approved_leave(h, usual['participants'][0]['person_id'])
    before = h.detail(workspace)['shifts']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    process_one(h.app.state.engine, scheduler=partial(cp_generate, time_budget_seconds=5)
                if engine is cp_generate else None)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_with_gaps' and result['additions'] and not result['removals']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Keep usual assignee and review their leave conflict'},
        revision=workspace['revision'])
    saved = next(s for s in h.detail(workspace)['shifts'] if s['id'] == usual['id'])
    assert all(p in saved['participants'] for p in usual['participants'])
    assert any(f['code'] == 'UNAVAILABLE' for f in applied['validation']['findings'])
    h.lifecycle(applied['workspace'], 'submit', status=422)
    h.command('coordinator', 'workspace.undo', {'reason': 'Undo only the reviewed gap filling'},
              workspace['id'], applied['workspace']['revision'])
    restored = h.detail(workspace)
    # Undo restores content; only explicitly revisioned cancel commands need the new rota revision.
    for row in before:
        for conflict in row.get('double_booking_conflicts', []):
            for action in conflict['cancel_actions']:
                assert action['payload']['workspace_id'] == workspace['id']
                if 'expected_revision' in action:
                    action['expected_revision'] = restored['workspace']['revision']
    assert restored['shifts'] == before


@pytest.mark.parametrize('fixture_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_release_version_bump_rejects_pre_keep_candidate(request, monkeypatch, fixture_name, engine):
    h = request.getfixturevalue(fixture_name)
    if engine == 'cp_sat':
        use_cp_sat(h)
    workspace, usual = usual_workspace(h)
    approved_leave(h, usual['participants'][0]['person_id'])

    def previous_keep(data, shift, participant):
        return generation.assignment_fixed(shift, participant) or (
            data.get('generation_options', {}).get('mode') == 'fill_gaps'
            and not (shift.get('series_id') and not shift.get('series_modified')))

    # A feature PR retains DEV's identifiers; only a release bump invalidates its earlier candidates.
    old_algorithm = cp_sat.COMPILER_VERSION if engine == 'cp_sat' else generation.ALGORITHM_VERSION
    with monkeypatch.context() as old:
        old.setattr(generation, 'keep_assignment', previous_keep)
        old.setattr(cp_sat, 'keep_assignment', previous_keep)
        job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
            'generation_options': {'mode': 'fill_gaps'}}, revision=workspace['revision'])['job']
        scheduler = partial(cp_generate, time_budget_seconds=5) if engine == 'cp_sat' else None
        process_one(h.app.state.engine, scheduler=scheduler)
        completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert completed['status'] == 'succeeded' and not completed['result_stale']
        assert completed['result']['algorithm_version'] == old_algorithm
        assert any(r['assignment_id'] == usual['participants'][0]['id'] for r in completed['result']['removals'])

    assert not h.get('coordinator', '/api/jobs/' + job['id'])['job']['result_stale']
    released_validator = validation.ENGINE_VERSION + '-release-test'
    monkeypatch.setattr(snapshots, 'ENGINE_VERSION', released_validator)
    monkeypatch.setattr(validation, 'ENGINE_VERSION', released_validator)
    monkeypatch.setattr(generation, 'ALGORITHM_VERSION', generation.ALGORITHM_VERSION + '-release-test')
    monkeypatch.setattr(cp_sat, 'COMPILER_VERSION', solver_rules.COMPILER_VERSION + '-release-test')
    monkeypatch.setattr(solver_rules, 'COMPILER_VERSION', cp_sat.COMPILER_VERSION)

    before = h.detail(workspace)
    with transaction(h.app.state.engine) as db:
        changes_before = db.scalar(select(func.count()).select_from(m.ChangeSet))
        applies_before = db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == 'generation.apply'))
    rejected = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Attempt to apply a pre-Keep candidate after the release version bump'}, revision=workspace['revision'], status=409)
    assert rejected['error']['code'] == 'STALE_SCHEDULER'
    assert h.detail(workspace) == before
    current = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert current['result_stale'] and current['status'] == 'succeeded'
    assert {k: current[k] for k in ('revision', 'result')} == {k: completed[k] for k in ('revision', 'result')}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Assignment, usual['participants'][0]['id']) is not None
        assert not db.get(m.Shift, usual['id']).series_modified
        assert db.scalar(select(func.count()).select_from(m.ChangeSet)) == changes_before
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == 'generation.apply')) == applies_before

    fresh = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                      revision=workspace['revision'])['job']
    process_one(h.app.state.engine, scheduler=scheduler)
    candidate = h.get('coordinator', '/api/jobs/' + fresh['id'])['job']
    assert candidate['status'] == 'succeeded' and not candidate['result_stale']
    assert candidate['result']['algorithm_version'] != completed['result']['algorithm_version']
    assert candidate['result']['additions'] and not candidate['result']['removals']
    assert any(f['code'] == 'UNAVAILABLE' for f in candidate['result']['issues'])
    h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': fresh['id'],
        'reason': 'Apply the fresh gap filling while preserving the usual assignee'}, revision=workspace['revision'])
    saved = next(s for s in h.detail(workspace)['shifts'] if s['id'] == usual['id'])
    assert all(p in saved['participants'] for p in usual['participants'])
