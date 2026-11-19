"""Template setup and reviewed solver changes preserve the dated scheduling contract."""
from dataclasses import replace
from functools import partial

import pytest

from apps.api.main import create_app
from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from packages.contracts.schemas import GenerationOptions
from packages.domain.cp_sat import generate
from packages.domain.solver_rules import COMPILER_VERSION
from packages.domain.time import aware
from .test_participation_segments import person, with_relief
from .test_series_reconciliation import template
from .test_rule_safety_audit import safety_policy
from .test_team_lead import START, END, lead_type


def use_cp_sat(h):
    # Exercise the HTTP dispatcher with its actual settings, not only the job helper.
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings, engine=h.app.state.engine.execution_options(rota_write_settings=h.settings))


@pytest.mark.parametrize('source', ['templates', 'previous'])
def test_new_draft_auto_schedule_uses_the_configured_engine(h, source):
    use_cp_sat(h)
    payload = {'name': 'November automatic setup', 'starts_on': '2026-11-01',
               'ends_on': '2026-12-01', 'generate_assignments': True}
    if source == 'templates':
        series = template(h, days=(6,), starts_on='2026-11-01', ends_on='2026-12-01')
        payload['series_ids'] = [series['id']]
    else:
        previous = h.shift(h.workspace(start='2026-10-02', end='2026-11-01'))['workspace']
        payload.update(source_workspace_id=previous['id'], source_revision=previous['revision'])
    created = h.command('coordinator', 'workspace.create', payload)
    jobs = h.get('coordinator', '/api/jobs?workspace_id=' + created['workspace']['id'])['jobs']
    assert created['setup']['created'] > 0 and len(jobs) == 1
    job = h.get('coordinator', '/api/jobs/' + jobs[0]['id'])['job']
    assert job['generation_engine'] == 'cp_sat'
    # Deferred generation binds the compiler when the worker freezes the queued request.
    assert job['compiler_version'] is None
    defaults = h.get('coordinator', '/api/me')['department']['generation_defaults']
    assert job['effective_options'] == GenerationOptions.model_validate(defaults).model_dump(mode='json')
    assert h.get('coordinator', '/api/publications')['publications'] == []
    assert process_one(h.app.state.engine, scheduler=partial(generate, time_budget_seconds=5), settings=h.settings)
    prepared = h.get('coordinator', '/api/jobs/' + jobs[0]['id'])['job']
    assert prepared['compiler_version'] == COMPILER_VERSION


def lead_proposal(h, segmented=False):
    use_cp_sat(h)
    first, second = h.seed['people'][:2]
    typ = lead_type(h, requirements=[{'minimum': 1, 'maximum': 2}])
    participant = person(first) if segmented else {'person_id': first, 'start': START, 'end': END}
    w = h.shift(h.workspace(), typ=typ, people=[participant,
        {'person_id': second, 'start': START, 'end': END, 'fixed': True}])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}, 'diagnostic_proposal': segmented}, revision=w['revision'])['job']
    process_one(h.app.state.engine, settings=h.settings, scheduler=partial(generate, time_budget_seconds=5))
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == ('candidate_with_gaps' if segmented else 'candidate_feasible'), result
    assert [(a['participant']['person_id'], a['participant']['team_lead']) for a in result['additions']] == [(first, True)]
    assert [r['person_id'] for r in result['removals']] == [first]
    return w, job, result


def test_applying_segmented_proposal_preserves_break_and_mandatory_gap(h):
    w, job, candidate = lead_proposal(h, segmented=True)
    original = h.detail(w)['shifts'][0]['participants']
    proposed = candidate['additions'][0]['participant']
    assert proposed['segments'] and not candidate['validation']['valid']
    assert 'LEAD_GAP' in {f['code'] for f in candidate['validation']['findings'] if f['severity'] == 'error'}
    result = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'],
        'job_id': job['id'], 'reason': 'Review incomplete lead proposal'}, revision=w['revision'])
    participants = h.detail(w)['shifts'][0]['participants']
    saved = next(p for p in participants if p['person_id'] == proposed['person_id'])
    assert saved['segments'] == proposed['segments']
    assert {p['person_id']: p['fixed'] for p in participants} == {p['person_id']: p['fixed'] for p in original}
    assert not result['validation']['valid']
    h.lifecycle(result['workspace'], 'submit', status=422)
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore the original segmented duty'},
              w['id'], result['workspace']['revision'])
    assert h.detail(w)['shifts'][0]['participants'] == original


def test_applying_a_new_assignment_defaults_missing_fixed_without_copying_solver_metadata(h):
    use_cp_sat(h)
    w = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    process_one(h.app.state.engine, settings=h.settings, scheduler=partial(generate, time_budget_seconds=5))
    candidate = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert candidate['status'] == 'candidate_feasible' and len(candidate['additions']) == 1
    proposed = candidate['additions'][0]['participant']
    assert 'fixed' not in proposed
    result = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'],
        'job_id': job['id'], 'reason': 'Apply a new ordinary assignment'}, revision=w['revision'])
    saved = h.detail(w)['shifts'][0]['participants'][0]
    assert not saved['fixed'] and saved['segments'] == []
    assert saved['person_id'] == proposed['person_id'] and saved['id'] != proposed['id']
    assert result['validation']['valid']


@pytest.mark.parametrize('alignment', ['date', 'weekday'])
def test_copy_previous_period_preserves_presence_offsets(h, alignment):
    w = h.shift(h.workspace(start='2026-10-02', end='2026-11-01'), people=with_relief(h))['workspace']
    source = h.detail(w)['shifts'][0]
    result = h.command('coordinator', 'workspace.create', {'name': 'Copied presence intervals',
        'starts_on': '2026-11-01', 'ends_on': '2026-12-01', 'source_workspace_id': w['id'],
        'source_revision': w['revision'], 'copy_people': True, 'copy_alignment': alignment})
    copied = h.detail(result['workspace'])['shifts'][0]
    original = next(p for p in source['participants'] if p['segments'])
    saved = next(p for p in copied['participants'] if p['person_id'] == original['person_id'])
    delta = aware(copied['start']) - aware(source['start'])
    assert [(aware(s['start']), aware(s['end'])) for s in saved['segments']] == [
        (aware(s['start']) + delta, aware(s['end']) + delta) for s in original['segments']]
    assert h.detail(w)['shifts'][0] == source
    assert result['validation']['valid'] and not copied['locked']


def test_partial_apply_refuses_a_split_replacement_before_database_conflict(h):
    w, job, _ = lead_proposal(h)
    before = h.detail(w)
    result = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'addition_indexes': [0], 'removal_indexes': [], 'reason': 'Review selected lead change'},
        revision=w['revision'], status=422)
    assert result['error']['code'] == 'INCOMPLETE_REPLACEMENT'
    assert h.detail(w) == before
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'addition_indexes': [0], 'removal_indexes': [0], 'reason': 'Apply the complete reviewed lead change'},
        revision=w['revision'])
    assert applied['validation']['valid']


@pytest.mark.parametrize('kind,field,amount', [
    ('min_rest', 'hours', 14), ('minimum_cover', 'minimum', 3), ('maximum_participants', 'maximum', 1)])
def test_stricter_safety_adjustments_preserve_historical_evidence_and_can_be_cleared(h, kind, field, amount):
    policy = safety_policy(h, kind, legacy_exception=True)
    w = h.workspace()
    changed = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'set',
        'amount': amount, 'reason': 'Use a stricter safety limit for this rota'}, w['id'], w['revision'])['workspace']
    projected = next(p for p in h.snapshot(changed)['policies'] if p['id'] == policy['id'])
    assert projected['definition']['rule'][field] == amount
    assert not projected['definition']['exception_allowed']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, policy['id']).definition == policy['definition']
    cleared = h.command('coordinator', 'workspace.rule_adjust', {'policy_id': policy['id'], 'mode': 'clear',
        'reason': 'Return to the unchanged department safety rule'}, w['id'], changed['revision'])['workspace']
    assert cleared['rule_adjustments'] == []
    restored = next(p for p in h.snapshot(cleared)['policies'] if p['id'] == policy['id'])
    assert restored['definition'] == policy['definition']
# Include this file in hosted supervision and team-lead qualification.
