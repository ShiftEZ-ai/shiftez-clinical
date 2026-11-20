"""Saved scores, failed-run explanations and persisted advisory acceptance exposed to the web review."""
from dataclasses import replace

import pytest

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import retry_write, transaction
from apps.api.services.jobs import create_job
from apps.api.services.issues import issue_key
from apps.worker.main import claim_job, heartbeat_job, process_one
from packages.contracts.schemas import Command
from packages.domain.cp_sat import generate
from packages.domain.score import schedule_score
from packages.domain.solver_control import SolverControl
from packages.domain.validation import evaluate


def run_job(h, workspace, **options):
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'],
            payload={'workspace_id': workspace['id'], 'kind': 'generation', **options}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    process_one(h.app.state.engine, settings=replace(h.settings, generation_engine='cp_sat'), solver_workers=1)
    return h.get('coordinator', '/api/jobs/' + job['id'])['job']


@pytest.mark.parametrize('cancelled', [False, True], ids=['active', 'cancelled-at-lease'])
def test_deferred_generation_claim_reports_preparation_before_snapshot(h, cancelled):
    """Active leases report preparation before snapshot; cancelled leases fail the shared scope guard."""
    workspace = h.shift(h.workspace('Deferred progress'), people=[])['workspace']
    session_id = h.detail(workspace)['shifts'][0]['id']
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'session_ids': [session_id]}, revision=workspace['revision'])['job']
    if cancelled:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Shift, session_id).cancelled = True
        process_one(h.app.state.engine)
        failed = h.get('coordinator', '/api/jobs/' + queued['id'])['job']
        assert failed['status'] == 'failed' and failed['error_code'] == 'INVALID_SCOPE'
        return
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    assert claim['id'] == queued['id']
    assert claim['progress']['stage'] == 'preparing'
    assert claim['progress']['elapsed_seconds'] == 0


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_deferred_generation_progress_stage_never_steps_back(h):
    workspace = h.shift(h.workspace('Deferred progress order'), people=[])['workspace']
    session_id = h.detail(workspace)['shifts'][0]['id']
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'session_ids': [session_id]}, revision=workspace['revision'])['job']
    engine = h.app.state.engine
    claim = retry_write(engine, claim_job)
    assert claim['id'] == queued['id'] and claim['progress']['stage'] == 'preparing'
    stages = [claim['progress']['stage']]
    for stage in ('checking', 'preparing', 'finding', 'scoring', 'ready'):
        assert retry_write(engine, lambda db, stage=stage: heartbeat_job(db, claim, {'stage': stage}))
        with transaction(engine) as db:
            stages.append(db.get(m.Job, queued['id']).progress['stage'])
    assert stages == ['preparing', 'preparing', 'preparing', 'finding', 'scoring', 'ready']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_succeeded_generation_progress_uses_the_saved_score(h, monkeypatch):
    monkeypatch.setattr(SolverControl, 'SCORE_INTERVAL_SECONDS', 3600)
    workspace = h.shift(h.workspace('Final progress score'), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job = run_job(h, workspace, generation_options={'mode': 'fill_gaps', 'search_minutes': 1})
    assert job['status'] == 'succeeded', job
    saved = job['result']['summary']['score_saved']
    assert job['progress']['best_score']['total'] == saved['uncapped_score']


def test_inherited_keep_is_used_by_the_direct_solver_and_explicit_rebalance_still_works(h):
    flexible, first_only = h.seed['people'][:2]
    h.policy({'kind': 'min_rest', 'hours': 24})
    h.policy({'kind': 'restriction', 'weekdays': [0]}, {'person_id': first_only})
    workspace = h.shift(h.workspace(), people=[flexible])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    source['people'] = {pid: source['people'][pid] for pid in (flexible, first_only)}
    for options in ({}, {'shortlist_width': 0}):
        source['generation_options'] = options
        result = generate(source, 5)
        assert result['effective_options']['mode'] == 'fill_gaps'
        assert result['status'] == 'requirements_conflict' and not result['removals']
    source['generation_options'] = {'mode': 'rebalance'}
    result = generate(source, 5)
    assert result['status'] == 'candidate_feasible' and len(result['removals']) == 1
    assert result['summary']['existing_assignments'] == 1 and result['summary']['changes_kept'] == 0


def drain(h):
    """The worker takes every queued job (the complete check maintenance queues after an edit with a worker running)."""
    while process_one(h.app.state.engine):
        pass


@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'])
def test_score_saved_matches_apply_with_changes_kept_as_a_separate_fact(h, mode):
    workspace = h.shift(h.workspace(), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job = run_job(h, workspace, generation_options={'mode': mode})
    summary = job['result']['summary']
    assert summary['existing_assignments'] == 1
    assert summary['changes_kept'] == 1 - len(job['result']['removals'])
    assert summary['score_saved'] == summary['score']
    assert summary['score_saved']['components']['stability']['value'] is None
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply the complete reviewed proposal'}, revision=workspace['revision'])
    drain(h)  # BE-18: the reply's check may be provisional; the worker's complete check carries the score
    assert h.detail(workspace)['workspace']['validation']['score'] == summary['score_saved']
    source = h.snapshot(applied['workspace'])
    assert summary['score_saved'] == schedule_score(source, evaluate(source))
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == summary


def test_empty_assignments_count_applicable_rules_without_earned_preference_or_fairness_score(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    h.command('staff', 'wish.create', {'person_id': person, 'kind': 'prefer_off', 'first_day': '2026-10-04',
        'last_day': '2026-10-04', 'points': 1})
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    score = schedule_score(source, evaluate(source))
    assert score['version'] == 'score-v12'
    assert score['score'] == 0 and score['raw'] == 0
    assert score['components']['coverage']['value'] == 0
    assert score['components']['rules']['value'] == 1
    assert all(score['components'][part]['value'] is None for part in ('preferences', 'fairness'))
    assert score['people'] == {} and sum(row['points'] for row in score['why']) == 100
    source['shifts'][0]['participants'] = [{'person_id': person, 'start': source['shifts'][0]['start'],
        'end': source['shifts'][0]['end'], 'participation': 'staff', 'overtime': False}]
    assigned = schedule_score(source, evaluate(source))
    assert assigned['score'] == assigned['raw'] == 84.2
    assert {part: row['value'] for part, row in assigned['components'].items()} == {
        'coverage': 1, 'rules': 1, 'fairness': 1, 'preferences': 0, 'stability': None}


@pytest.mark.parametrize('outside_work', [False, True])
def test_empty_focus_counts_fixed_rule_opportunities_and_solver_fills_unmet_optional_target(h, outside_work):
    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Optional session', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    if outside_work:
        workspace = h.shift(workspace, typ=typ, start='2026-10-05T08:00:00+03:00',
                            end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'target', 'amount': 1}, {'person_id': person}, severity='advisory')
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 2},
             {'person_id': person}, severity='advisory')
    workspace = h.refresh_draft_baseline(workspace)
    h.command('staff', 'wish.create', {'person_id': person, 'kind': 'prefer_on',
                                     'first_day': '2026-10-04', 'last_day': '2026-10-04', 'points': 1})
    source = h.snapshot(workspace)
    source['focus_ids'] = [s['id'] for s in source['shifts'] if not s['participants']]
    scored = schedule_score(source, evaluate(source))
    assert scored['score'] == scored['raw'] == 0
    assert scored['components']['rules']['value'] == (1 if outside_work else 0)
    assert all(scored['components'][part]['value'] is None for part in ('coverage', 'fairness', 'preferences', 'stability'))
    result = generate(source, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['removals'] == []
    if outside_work:
        assert result['additions'] == []  # The assignment outside the focus already meets the block target.
    else:
        addition, = result['additions']
        assert addition['shift_id'] == source['focus_ids'][0]
        assert addition['participant']['person_id'] == person
        focus = next(s for s in source['shifts'] if s['id'] == addition['shift_id'])
        assert addition['participant']['start'] == focus['start']
        assert addition['participant']['end'] == focus['end']
    saved = result['summary']['score_saved']
    assert saved['score'] == saved['raw'] == (0 if outside_work else 100)
    expected = {'coverage': None, 'rules': 1, 'fairness': None if outside_work else 1,
                'preferences': None if outside_work else 1, 'stability': None}
    assert {part: row['value'] for part, row in saved['components'].items()} == expected
    expected['stability'] = 1
    assert {part: row['value'] for part, row in result['objective_components'].items()} == expected
    assert result['compiled_component_values'] == expected


@pytest.mark.parametrize('fixture', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_two_unpublishable_scores_match_footer_after_apply_with_signed_delta(request, fixture):
    h = request.getfixturevalue(fixture)
    typ = h.configure('assignment_types', {'name': 'Four required places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 4, 'maximum': 4}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    before = h.get('coordinator', '/api/workspaces/' + workspace['id'] + '/summary')['score']
    job = run_job(h, workspace, diagnostic_proposal=True)
    summary = job['result']['summary']
    assert job['result']['status'] == 'candidate_with_gaps' and job['result']['additions']
    assert not summary['score_before']['publishable'] and not summary['score_saved']['publishable']
    assert summary['score_before'] == before
    assert summary['score_saved']['score'] == 59 < summary['score_saved']['uncapped_score']
    # score_delta is the uncapped difference (maintenance), even with both sides unpublishable.
    assert summary['score_delta'] == round(summary['score_saved']['uncapped_score'] - before['uncapped_score'], 1) > 0
    assert summary['filled_places'] > 0 and summary['open_places_count'] > 0
    assert summary['required_places'] == 4 and summary['sessions_affected'] == 1
    assert summary['proven_open_places'] == summary['open_places_count'] == 1
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Apply the incomplete proposal'}, revision=workspace['revision'])
    drain(h)  # BE-18: the reply's check may be provisional; the worker's complete check carries the score
    after = h.get('coordinator', '/api/workspaces/' + workspace['id'] + '/summary')['score']
    assert applied['workspace']['id'] == workspace['id']
    assert summary['score_saved'] == summary['score'] == after
    assert round(after['uncapped_score'] - before['uncapped_score'], 1) == summary['score_delta']


def test_infeasible_summary_counts_places_and_sessions_without_claiming_staleness(h):
    person = h.seed['people'][0]
    restriction = h.policy({'kind': 'restriction'}, {'person_id': person})
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    source['people'] = {person: source['people'][person]}
    result = generate(source, 5)
    assert result['status'] == 'requirements_conflict'
    assert result['summary']['open_places_count'] == result['summary']['sessions_affected'] == 1
    assert result['summary']['proven_open_places'] == 1
    assert any(r['kind'] == 'rule' and r['rule_id'] == restriction['id'] for r in result['summary']['top_reasons'])
    # The engine facts job.create freezes with every run: without them currency is honestly unknown (None).
    from packages.domain.solver_rules import COMPILER_VERSION
    source = {**source, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Job(department_id=workspace['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint=source['fingerprint'], input_snapshot=source, result=result)
        db.add(row)
        db.flush()
        job_id = row.id
    fresh = h.get('coordinator', '/api/jobs/' + job_id)['job']
    assert fresh['result_stale'] is False
    assert fresh['result']['summary']['open_places_count'] == 1
    assert fresh['result']['summary']['proven_open_places'] == 1
    h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    assert h.get('coordinator', '/api/jobs/' + job_id)['job']['result_stale'] is True


def test_december_requirements_conflict_cannot_be_applied_or_change_the_draft(h):
    workspace = h.shift(h.workspace('December 2026 proposal', '2026-12-01', '2027-01-01'),
                        start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00', people=[])["workspace"]
    source = h.snapshot(workspace)
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Job(department_id=workspace['department_id'], workspace_id=workspace['id'],
                    requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
                    input_fingerprint=source['fingerprint'], input_snapshot={},
                    result={'status': 'requirements_conflict', 'additions': [], 'removals': []})
        db.add(row)
        db.flush()
        job_id = row.id

    rejected = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job_id, 'reason': 'Apply December proposal'}, revision=workspace['revision'], status=422)
    assert rejected['error']['code'] == 'NO_CANDIDATE'
    assert h.snapshot(workspace) == source
    assert h.get('coordinator', '/api/jobs/' + job_id)['job']['status'] == 'succeeded'


def test_conflict_explanation_names_rule_without_solver_jargon(h):
    person = h.seed['people'][0]
    policy = h.policy({'kind': 'min_rest', 'hours': 24})
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    source['people'] = {person: source['people'][person]}
    result = generate(source, 5)
    assert result['status'] == 'requirements_conflict'
    assert any(r['rule_id'] == policy['id'] for r in result['summary']['top_reasons'])
    explanation = result['diagnostics'][-1]['message']
    assert 'cannot all be met together' in explanation and 'remaining scheduling safeguards' not in explanation
    assert result['summary']['open_places_count'] == result['summary']['sessions_affected'] == 2
    assert result['summary']['proven_open_places'] == 0


def test_top_reasons_do_not_attribute_a_conflict_to_unrelated_open_sessions(h):
    from packages.domain.solver_summary import summarise
    workspace = h.workspace()
    for day in (4, 5, 6):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    diagnostics = [{'code': 'CONFLICTING_REQUIREMENTS', 'requirements': [
        {'kind': 'policy', 'rule_id': 'rest', 'name': 'Rest'},
        *({'kind': 'duty', 'shift_id': sid} for sid in source['focus_ids'][:2])]}]
    summary = summarise(source, evaluate(source), [], set(source['focus_ids']), diagnostics=diagnostics)
    assert summary['sessions_affected'] == 3
    assert summary['top_reasons'] == [{'kind': 'rule', 'code': 'CONFLICTING_REQUIREMENTS',
        'message': 'Rest', 'rule_id': 'rest', 'person_id': None, 'session_count': 2, 'places_count': 2}]
    diagnostics[0]['requirements'] = diagnostics[0]['requirements'][:1]
    summary = summarise(source, evaluate(source), [], set(source['focus_ids']), diagnostics=diagnostics)
    assert summary['top_reasons'][0]['session_count'] == 0


@pytest.mark.parametrize('capability_gap', [False, True], ids=['people-shortage', 'skill-shortage'])
def test_direct_shortages_are_top_reasons_instead_of_needed_elsewhere(h, capability_gap):
    requirements = [{'minimum': 2, 'capabilities': [{'capability_id': h.seed['capability'], 'minimum': 1}]
                     if capability_gap else []}]
    typ = h.configure('assignment_types', {'name': 'Direct shortage', 'starts_on': '2020-01-01',
        'definition': {'requirements': requirements}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    source = h.snapshot(workspace)
    if capability_gap:
        source['grants'] = []
    else:
        person = h.seed['people'][0]
        source['people'] = {person: source['people'][person]}
    result = generate(source, 5)
    assert result['status'] == 'requirements_conflict'
    code = 'INSUFFICIENT_CAPABILITY_CANDIDATES' if capability_gap else 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES'
    diagnostic = next(d for d in result['diagnostics'] if d['code'] == code)
    reason = next(r for r in result['summary']['top_reasons'] if r['code'] == code)
    assert reason['kind'] == 'eligibility' and reason['session_count'] == 1
    assert reason['message'] == diagnostic['message']
    assert diagnostic['message'] in result['summary']['open_places'][0]['message']
    assert 'needed elsewhere' not in result['summary']['open_places'][0]['message']
    assert result['summary']['proven_open_places'] == (0 if capability_gap else 1)


@pytest.mark.parametrize('diagnostic_proposal', [False, True])
def test_proven_open_places_distinguish_impossible_from_fillable_unfilled_sessions(h, diagnostic_proposal):
    h.policy({'kind': 'restriction', 'weekdays': [6]})
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    result = generate(h.snapshot(workspace), 5, diagnostic_proposal=diagnostic_proposal)
    assert result['status'] == ('candidate_with_gaps' if diagnostic_proposal else 'requirements_conflict')
    assert result['summary']['proven_open_places'] == 1
    assert result['summary']['open_places_count'] == (1 if diagnostic_proposal else 2)
    assert len(result['additions']) == int(diagnostic_proposal)


def test_proven_open_places_deduplicate_overlapping_requirements_without_display_limits(h):
    from packages.domain.solver_summary import summarise
    typ = h.configure('assignment_types', {'name': 'Overlapping staffing needs', 'starts_on': '2020-01-01',
        'definition': {'requirements': [
            {'minimum': 5, 'from_minute': 0, 'to_minute': 180},
            {'minimum': 4, 'from_minute': 60, 'to_minute': 240}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    source = h.snapshot(workspace)
    result = generate(source, 5)
    assert result['status'] == 'requirements_conflict'
    # One shortage proof per requirement window, plus the named clash of the two (maintenance); the summary dedupes them.
    assert [d['code'] for d in result['diagnostics']].count('INSUFFICIENT_WHOLE_DUTY_CANDIDATES') == 2
    assert any(d['code'] == 'CONFLICTING_REQUIREMENTS' for d in result['diagnostics'])
    summary = summarise(source, evaluate(source), result['candidate_evidence'], set(source['focus_ids']),
                        diagnostics=result['diagnostics'], explain_limit=0, trial_limit=0)
    assert summary['open_places'] == [] and summary['open_places_count'] == 5
    assert summary['proven_open_places'] == 2
    for proof in result['diagnostics']:
        proof['proven'] = False
    unproven = summarise(source, evaluate(source), result['candidate_evidence'], set(source['focus_ids']),
                         diagnostics=result['diagnostics'])
    assert unproven['proven_open_places'] == 0


@pytest.mark.parametrize('locked', [False, True], ids=['outside-scope', 'locked'])
def test_proven_open_places_exclude_sessions_the_run_cannot_edit(h, locked):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    from packages.domain.solver_summary import summarise
    workspace = h.shift(h.workspace(), people=[])['workspace']
    source = h.snapshot(workspace)
    source['shifts'][0]['locked'] = locked
    diagnostics = direct_coverage_shortages(source, [], set())
    assert diagnostics and diagnostics[0]['proven']
    summary = summarise(source, evaluate(source), [], set(), diagnostics=diagnostics)
    assert summary['open_places_count'] == 1 and summary['proven_open_places'] == 0


def test_top_person_reason_names_the_person_already_working_then(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, people=[])['workspace']
    source = h.snapshot(workspace)
    person = h.seed['people'][0]
    source['people'] = {person: source['people'][person]}
    result = generate(source, 5, diagnostic_proposal=True)
    assert result['status'] == 'candidate_with_gaps'
    assert any(r['kind'] == 'person' and r['code'] == 'TIME_OVERLAP' and r['person_id'] == person
               for r in result['summary']['top_reasons'])


def test_panel_acceptance_exposes_the_resolve_key_and_persists_with_undo(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'restriction'}, {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    job = run_job(h, workspace)
    acceptance = job['advisory_acceptance']
    issue, = acceptance['issues']
    finding, = [f for f in job['result']['validation']['findings'] if f['severity'] == 'warning']
    assert issue == {'key': issue_key(finding), 'accepted': False, 'can_accept': True}
    accepted = h.command('coordinator', 'issues.resolve', {'workspace_id': workspace['id'], 'action': 'accept',
        'keys': [issue['key']], 'reason': 'Accept the advisory shown in auto-schedule'},
        revision=acceptance['workspace_revision'])['workspace']
    assert accepted['accepted_issues'][0]['key'] == issue['key']
    reread = h.get('coordinator', '/api/jobs/' + job['id'])['job']['advisory_acceptance']['issues'][0]
    assert reread['accepted'] is True and reread['can_accept'] is False
    assert h.detail(workspace)['workspace']['accepted_issues'] == accepted['accepted_issues']
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo advisory acceptance'},
                       accepted['id'], accepted['revision'])['workspace']
    assert not undone['accepted_issues']


def test_proposal_only_advisory_can_be_accepted_only_after_apply(h):
    h.policy({'kind': 'restriction'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    job = run_job(h, workspace)
    assert job['advisory_acceptance']['issues']
    assert not any(i['can_accept'] for i in job['advisory_acceptance']['issues'])
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'],
        'job_id': job['id'], 'reason': 'Save the proposal before accepting its issues'}, revision=workspace['revision'])
    drain(h)  # maintenance: issues of the saved proposal are accepted against its complete check
    applied['workspace'] = h.detail(workspace)['workspace']
    current = h.get('coordinator', '/api/jobs/' + job['id'])['job']['advisory_acceptance']
    keys = [i['key'] for i in current['issues'] if i['can_accept']]
    assert keys
    accepted = h.command('coordinator', 'issues.resolve', {'workspace_id': workspace['id'], 'action': 'accept',
        'keys': keys, 'reason': 'Persist the reviewed proposal advisory'}, revision=applied['workspace']['revision'])
    assert {a['key'] for a in accepted['workspace']['accepted_issues']} == set(keys)
