import copy
import pytest
from dataclasses import replace
from typing import get_args

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction, retry_write
from apps.api.main import create_app
from apps.api.services.jobs import create_job
from apps.worker.main import claim_job, process_one
from packages.contracts.schemas import Command, GENERATION_WEIGHT_PRESETS, GenerationOptions, GenerationWeightPreset, ScoreWeights
from packages.domain.cp_sat import canonical_order, generate
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate


def process_cp_sat(h, **kwargs):
    """Run a CP-SAT request with matching worker settings."""
    return process_one(h.app.state.engine,
                       settings=replace(h.settings, generation_engine='cp_sat'), **kwargs)


def test_weight_presets_are_valid_and_coverage_is_unchanged():
    assert set(get_args(GenerationWeightPreset)) == set(GENERATION_WEIGHT_PRESETS)
    assert GenerationOptions().weight_preset == 'department_defaults'
    assert GENERATION_WEIGHT_PRESETS['preferences_first']['label'] == 'Preferences first'
    assert GENERATION_WEIGHT_PRESETS['department_defaults']['weights'] is None
    for preset in GENERATION_WEIGHT_PRESETS.values():
        if preset['weights'] is not None:
            weights = ScoreWeights.model_validate(preset['weights'])
            assert weights.coverage == ScoreWeights().coverage
    assert GENERATION_WEIGHT_PRESETS['fairness_first']['weights']['fairness'] == 40
    assert GENERATION_WEIGHT_PRESETS['fairness_first']['weights']['preferences'] == 10


def test_legacy_priority_options_parse_but_are_not_serialized():
    options = GenerationOptions.model_validate({'objective_order': ['balance', 'changes', 'preferences', 'targets'],
        'scoring': {'tolerance': {'targets': 50, 'preferences': 10}}})
    assert options.scoring.weights == ScoreWeights()
    schema = GenerationOptions.model_json_schema()
    assert schema['properties']['objective_order']['deprecated']
    assert schema['$defs']['ScoringSettings']['properties']['tolerance']['deprecated']
    assert 'objective_order' not in options.model_dump()
    assert 'tolerance' not in options.model_dump()['scoring']
    assert 'objective_order' not in options.model_dump_json()
    assert 'tolerance' not in options.model_dump_json()
    output = GenerationOptions.model_json_schema(mode='serialization')
    assert 'objective_order' not in output['properties']
    assert 'tolerance' not in output['$defs']['ScoringSettings']['properties']


@pytest.mark.parametrize('send_options', [False, True])
def test_legacy_options_are_hidden_on_read_and_stripped_on_save(h, send_options):
    from .test_staff_wishes import department_settings
    legacy = {'objective_order': ['balance', 'changes', 'preferences', 'targets'],
              'scoring': {'tolerance': {'targets': 50}}}
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).generation_defaults = copy.deepcopy(legacy)
    assert h.get('admin', '/api/me')['department']['generation_defaults'] == {'scoring': {}}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Department, h.seed['department_id']).generation_defaults == legacy
    payload = {**department_settings(h), **({'generation_defaults': legacy} if send_options else {})}
    saved = h.command('admin', 'department.settings', payload, h.seed['department_id'],
                      h.get('admin', '/api/me')['organisation']['revision'])
    with transaction(h.app.state.engine) as db:
        settings = db.get(m.Department, h.seed['department_id']).generation_defaults
    assert 'objective_order' not in settings
    assert 'tolerance' not in settings['scoring']
    assert saved['department']['generation_defaults'] == settings
    assert 'objective_order' not in saved['previous']['generation_defaults']
    assert 'tolerance' not in saved['previous']['generation_defaults']['scoring']
    w = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': legacy}, revision=w['revision'])['job']
    score = {'score': 77, 'settings': {'weights': {'fairness': 20}, 'tolerance': {'targets': 50}}}
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        assert 'objective_order' not in row.input_snapshot['generation_options']
        assert 'tolerance' not in row.input_snapshot['generation_options']['scoring']
        # Older evidence stays stored as written; API option views omit the retired fields.
        row.input_snapshot = {**row.input_snapshot, 'generation_engine': 'cp_sat', 'generation_options': legacy}
        row.result = {'effective_options': {**legacy, 'search_seconds': 5},
                      'summary': {key: copy.deepcopy(score) for key in ('score_before', 'score_saved', 'score')},
                      'validation': {'score': copy.deepcopy(score)}}
        db.get(m.Workspace, w['id']).validation = {'score': copy.deepcopy(score)}
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['effective_options'] == {'scoring': {}}
    assert detail['result']['effective_options'] == {'scoring': {}, 'search_seconds': 5}
    for key in ('score_before', 'score_saved', 'score'):
        assert detail['result']['summary'][key] == {**score, 'settings': {'weights': {'fairness': 20}}}
    assert 'tolerance' not in detail['result']['validation']['score']['settings']
    assert 'tolerance' not in h.detail(w)['workspace']['validation']['score']['settings']
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.input_snapshot['generation_options'] == legacy
        assert row.result['effective_options'] == {**legacy, 'search_seconds': 5}
        assert row.result['summary']['score'] == row.result['validation']['score'] == score
        assert db.get(m.Workspace, w['id']).validation['score'] == score


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('legacy_preset', [None, 'fairness_first', 'preferences_first', 'fewest_changes'])
def test_department_weights_apply_to_ordinary_runs_even_with_a_legacy_preset(request, database, legacy_preset):
    from .test_staff_wishes import department_settings
    h = request.getfixturevalue(database)
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    weights = ScoreWeights(fairness=7, preferences=23).model_dump()
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'weights': weights}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        stored = {**department.generation_defaults,
                  **({'weight_preset': legacy_preset} if legacy_preset else {})}
        department.generation_defaults = copy.deepcopy(stored)
    defaults = h.get('admin', '/api/me')['department']['generation_defaults']
    assert defaults['weight_preset'] == 'department_defaults'
    assert defaults['scoring']['weights'] == weights
    w = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    assert job['effective_options']['weight_preset'] == 'department_defaults'
    assert job['effective_options']['scoring']['weights'] == weights
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot['generation_defaults']['weight_preset'] == 'department_defaults'
        assert snapshot['generation_options']['scoring']['weights'] == weights
        assert not snapshot['generation_options_overridden']
        assert db.get(m.Department, h.seed['department_id']).generation_defaults == stored
    process_cp_sat(h)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible', result
    assert {key: component['weight'] for key, component in result['objective_components'].items()} == weights
    # Metadata saves and their Undo payloads must not reintroduce a stored run preset.
    saved = h.command('admin', 'department.settings', department_settings(h), h.seed['department_id'],
                      h.get('admin', '/api/me')['organisation']['revision'])
    if legacy_preset is None:
        assert 'generation_defaults' not in saved['previous']
    else:
        assert saved['previous']['generation_defaults']['weight_preset'] == 'department_defaults'
    assert saved['department']['generation_defaults']['weight_preset'] == 'department_defaults'
    assert saved['department']['generation_defaults']['scoring']['weights'] == weights
    undo = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo'},
                     h.seed['department_id'], saved['organisation']['revision'])
    assert undo['department']['generation_defaults']['scoring']['weights'] == weights


@pytest.mark.parametrize('preset', [key for key in GENERATION_WEIGHT_PRESETS if key != 'department_defaults'])
def test_department_settings_reject_run_weight_presets(h, preset):
    from .test_staff_wishes import department_settings
    before = h.get('admin', '/api/me')
    rejected = h.command('admin', 'department.settings', {**department_settings(h),
        'department_name': 'Must not be saved', 'generation_defaults': {'weight_preset': preset,
            'scoring': {'weights': {'fairness': 7, 'preferences': 23}}}},
        h.seed['department_id'], before['organisation']['revision'], status=422)
    assert rejected['error']['code'] == 'INVALID_OPTIONS'
    assert 'only to individual runs' in rejected['error']['message']
    assert 'department_defaults' in rejected['error']['message']
    assert h.get('admin', '/api/me')['department'] == before['department']


@pytest.mark.parametrize('preset', GENERATION_WEIGHT_PRESETS)
def test_run_weight_preset_steers_search_but_saved_and_review_scores_use_department_weights(h, preset):
    from .test_staff_wishes import department_settings
    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    weights = ScoreWeights(fairness=7, preferences=23).model_dump()
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'weights': weights}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    w = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'weight_preset': preset}}, revision=w['revision'])['job']
    resolved = GENERATION_WEIGHT_PRESETS[preset]['weights'] or weights
    assert job['effective_options']['weight_preset'] == preset
    assert job['effective_options']['scoring']['weights'] == resolved
    with transaction(h.app.state.engine) as db:
        snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
    assert snapshot['generation_options']['scoring']['weights'] == resolved
    assert snapshot['generation_defaults']['scoring']['weights'] == weights
    process_cp_sat(h)
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['status'] == 'succeeded'
    result = detail['result']
    assert result['status'] == 'candidate_feasible', result
    assert detail['effective_options'] == result['effective_options']
    assert {key: component['weight'] for key, component in result['objective_components'].items()} == resolved
    for key in ('score_before', 'score_saved', 'score'):
        assert result['summary'][key]['settings']['weights'] == weights
        assert 'tolerance' not in result['summary'][key]['settings']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Review the preset proposal using department weights'}, revision=w['revision'])
    # Commands carry the compact check summary; the authoritative score stays
    # on the reloaded workspace detail.
    assert 'score' not in applied['validation']
    saved_check = h.detail(applied['workspace'])['workspace']['validation']
    assert saved_check['score']['settings']['weights'] == weights
    assert saved_check['score'] == result['summary']['score_saved']
    assert h.get('admin', '/api/me')['department']['generation_defaults']['scoring']['weights'] == weights
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, job['id']).input_snapshot
        for key in ('generation_options', 'generation_defaults',
                    'generation_options_overridden', 'diagnostic_proposal'):
            assert stored[key] == snapshot[key]
        assert stored['baseline']['hash'] == snapshot['baseline']['hash']


def test_department_defaults_and_run_override_are_snapshotted(h):
    me = h.get('admin', '/api/me')
    dept, org = me['department'], me['organisation']
    defaults = GenerationOptions(mode='fill_gaps', search_seconds=600, fill_tier_time_share=0.25).model_dump(mode='json')
    h.command('admin', 'department.settings', {
        'department_name': dept['name'], 'department_revision': dept['revision'],
        'organisation_name': org['name'], 'timezone': org['timezone'],
        'week_start': org['week_start'], 'generation_defaults': defaults,
        'reason': 'Configure departmental generation defaults'}, dept['id'], org['revision'])
    w = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'],
            payload={'workspace_id': w['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine='cp_sat'))['job']
        row = db.get(m.Job, job['id'])
        assert row.input_snapshot['generation_options'] == defaults
        assert not row.input_snapshot['generation_options_overridden']
    process_cp_sat(h, solver_workers=2)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible'
    assert result['solver_workers'] == 2
    assert result['effective_options'] == defaults
    detail = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert detail['effective_options'] == defaults
    assert detail['compiler_version'] == result['algorithm_version']
    assert 'input_snapshot' not in detail
    override = {'search_seconds': 300}
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': override}, revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot['generation_options'] == {**defaults, 'search_minutes': 5}
        assert snapshot['generation_defaults'] == defaults
        assert snapshot['generation_options_overridden']
        assert db.get(m.Department, dept['id']).generation_defaults == defaults


@pytest.mark.parametrize('lead_balance', [None, 'off'], ids=['department-leads', 'legacy-run-leads-off'])
def test_a_partial_scoring_override_keeps_the_departments_other_numbers(h, lead_balance):
    """Run overrides steer the search; displayed scores keep the saved draft's department context."""
    me = h.get('admin', '/api/me')
    dept, org = me['department'], me['organisation']
    scoring = {'weights': {'coverage': 40, 'rules': 30, 'fairness': 10, 'preferences': 10, 'stability': 10},
               'strengths': {'low': 2, 'medium': 4, 'high': 12, 'critical': 40}, 'wish_budget_points': 6}
    h.command('admin', 'department.settings', {
        'department_name': dept['name'], 'department_revision': dept['revision'], 'organisation_name': org['name'],
        'timezone': org['timezone'], 'week_start': org['week_start'], 'generation_defaults': {'scoring': scoring},
        'reason': 'Tune scoring'}, dept['id'], org['revision'])
    from .test_team_lead import lead_type
    typ = lead_type(h, requirements=[{'minimum': 1, 'maximum': 1}]) if lead_balance else None
    w = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    override = {'wish_debt_cap': 1, **({'lead_balance': lead_balance} if lead_balance else {})}
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'scoring': override}}, revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        request = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
        run = request['generation_options']['scoring']
    snapshot = h.snapshot(w)
    snapshot['generation_defaults'] = {'scoring': scoring}
    snapshot['generation_options'] = {'scoring': {**scoring, **override}}
    assert snapshot['fairness_strengths'] == scoring['strengths']
    assert run['weights'] == scoring['weights'] and run['wish_debt_cap'] == 1
    assert run['strengths'] == scoring['strengths'] and run['wish_budget_points'] == 6
    assert run['lead_balance'] == (lead_balance or 'medium')
    # The same assignment sets have the same public score before and after saving the proposal.
    data = snapshot
    result = generate(data, 5)
    assert result['status'] == 'candidate_feasible' and result['additions'] and not result['removals']
    before = canonical_order(copy.deepcopy(data))
    if lead_balance:
        from .test_team_lead import lead_goal
        from packages.domain.fairness import goal_identity
        lead = lead_goal(data, typ)
        assert lead['definition']['strength'] == 'medium' != lead_balance
    before_validation = evaluate(before)
    assert result['summary']['score_before'] == schedule_score(before, before_validation, {'scoring': scoring})
    assert result['summary']['score_before']['score'] == h.detail(w)['workspace']['validation']['score']['score']
    after = copy.deepcopy(before)
    for change in result['additions']:
        next(s for s in after['shifts'] if s['id'] == change['shift_id'])['participants'].append(change['participant'])
    after_validation = evaluate(after)
    assert result['summary']['score'] == schedule_score(after, after_validation, {'scoring': scoring})
    if lead_balance:
        # A retired run flag cannot switch off the department's approved Fair share line.
        for validation in (before_validation, after_validation, result['validation']):
            [report] = [g for g in validation['goals'] if g['goal_id'] == goal_identity(lead)]
            assert report['strength'] == 'medium'
        for candidate, validation, key in ((before, before_validation, 'score_before'), (after, after_validation, 'score')):
            assert any(g['goal_id'] == goal_identity(lead) for g in validation['goals'])
            value = result['summary'][key]['components']['fairness']['value']
            assert (value is not None) == any(s['participants'] for s in candidate['shifts'])
    assert result['summary']['score']['score'] > result['summary']['score_before']['score']
    for key in ('score_before', 'score'):
        assert result['summary'][key]['settings']['weights'] == scoring['weights']
        assert result['summary'][key]['components']['stability']['value'] is None


def test_fill_gaps_does_not_rebalance_existing_people(h):
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'min_rest', 'hours': 24})
    h.policy({'kind': 'restriction', 'weekdays': [0]}, {'person_id': second})
    w = h.shift(h.workspace(), people=[first])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = generate(data, 5)
    assert result['status'] == 'requirements_conflict'
    assert result['additions'] == result['removals'] == []
    data['generation_options'] = {'mode': 'rebalance'}
    assert generate(data, 5)['status'] == 'candidate_feasible'


def test_generation_claims_are_serialized_across_workspaces(h):
    for name in ('First rota', 'Second rota'):
        w = h.workspace(name)
        h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
    assert retry_write(h.app.state.engine, claim_job)
    assert retry_write(h.app.state.engine, claim_job) is None


def test_explicit_diagnostic_job_preserves_inputs_and_reports_mandatory_gaps(h):
    import pytest
    from pydantic import ValidationError
    from packages.contracts.schemas import JobInput
    h.policy({'kind': 'minimum_cover', 'minimum': 10})
    w = h.shift(h.workspace(), people=[])['workspace']
    before = h.snapshot(w)
    with pytest.raises(ValidationError, match='require a generation job'):
        JobInput(workspace_id=w['id'], kind='validation', diagnostic_proposal=True)
    rejected = h.command('coordinator', 'job.create', {'workspace_id': w['id'],
        'kind': 'generation', 'diagnostic_proposal': True}, revision=w['revision'], status=422)
    assert rejected['error']['code'] == 'DIAGNOSTIC_ENGINE_REQUIRED'
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'],
            payload={'workspace_id': w['id'], 'kind': 'generation', 'diagnostic_proposal': True}),
            replace(h.settings, generation_engine='cp_sat'))['job']
        assert job['diagnostic_proposal'] is True
        assert db.get(m.Job, job['id']).input_snapshot['diagnostic_proposal'] is True
    process_cp_sat(h)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'succeeded'
    assert completed['result']['status'] == 'candidate_with_gaps'
    assert not completed['result']['validation']['valid']
    assert not completed['result']['optimality_proven']
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' and f['severity'] == 'error'
               for f in completed['result']['validation']['findings'])
    assert h.snapshot(w) == before
    assert not h.get('coordinator', '/api/publications')['publications']


def test_scheduler_upgrade_rejects_old_proposals_before_mutation(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'],
            payload={'workspace_id': w['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    process_cp_sat(h)
    before = h.snapshot(w)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        original_input, original_result = dict(row.input_snapshot), dict(row.result)
    for side in ('input', 'result'):
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job['id'])
            row.input_snapshot = {**original_input, **({'compiler_version': 'older-version'} if side == 'input' else {})}
            row.result = {**original_result, **({'algorithm_version': 'older-version'} if side == 'result' else {})}
        rejected = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'],
            'job_id': job['id'], 'reason': 'Review stale scheduler proposal'}, revision=w['revision'], status=409)
        assert rejected['error']['code'] == 'STALE_SCHEDULER'
        assert h.snapshot(w) == before
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot, row.result = original_input, original_result
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'],
        'job_id': job['id'], 'reason': 'Review current scheduler proposal'}, revision=w['revision'])
    assert applied['validation']['valid']
    pair = {key: original_result['summary'][key] for key in ('score_before', 'score')}
    assert pair['score']['score'] > pair['score_before']['score']
    assert pair['score_before']['score'] == schedule_score(before, evaluate(before), original_input['generation_defaults'])['score']
    applied_snapshot = h.snapshot(applied['workspace'])
    assert 'score' not in applied['validation']
    saved_check = h.detail(applied['workspace'])['workspace']['validation']
    assert saved_check['score'] == schedule_score(applied_snapshot, evaluate(applied_snapshot),
                                                  original_input['generation_defaults'])
    assert saved_check['score']['components']['stability']['value'] is None
    assert saved_check['score']['score'] == pair['score']['score']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == original_result['summary']
    # A later edit cannot rewrite the run's comparison or make its old suggestions applicable again.
    changed = h.shift(applied['workspace'], start='2026-10-05T08:00:00+03:00',
                      end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    assert changed['validation']['score']['score'] != pair['score_before']['score']
    reread = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary']
    assert {key: reread[key] for key in pair} == pair
    after_edit = h.snapshot(changed)
    rejected = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Old comparison cannot authorize a changed rota'}, revision=changed['revision'], status=409)
    # The job is still applied, so its status is checked before the input fingerprint.
    assert rejected['error']['code'] == 'JOB_STATE'
    assert h.snapshot(changed) == after_edit


def test_worker_does_not_reinterpret_a_queued_old_scheduler_job(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    before = h.snapshot(w)
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'],
            payload={'workspace_id': w['id'], 'kind': 'generation'}),
            replace(h.settings, generation_engine='cp_sat'))['job']
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'compiler_version': 'older-version'}
    def must_not_solve(*args, **kwargs):
        raise AssertionError('Old queued job must be rejected before solving')
    process_cp_sat(h, scheduler=must_not_solve)
    completed = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert completed['status'] == 'failed'
    assert completed['error_code'] == 'ValueError'
    assert not completed['result']
    assert h.snapshot(w) == before


@pytest.mark.parametrize('field,value', [
    ('wish_budget_points', 0), ('wish_period', 'quarter'),
    *[(f'weights.{key}', 0) for key in ('coverage', 'rules', 'fairness', 'preferences', 'stability')],
])
@pytest.mark.parametrize('preset', ['department_defaults', 'fairness_first'])
def test_run_rejects_changes_to_department_governed_options(h, field, value, preset):
    w = h.shift(h.workspace(), people=[])['workspace']
    key, _, nested = field.partition('.')
    override = {key: {nested: value} if nested else value}
    before = h.get('admin', '/api/me')['department']['generation_defaults']
    rejected = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'weight_preset': preset, 'scoring': override}}, revision=w['revision'], status=422)
    assert rejected['error']['code'] == 'DEPARTMENT_OPTIONS_ONLY'
    assert rejected['error']['message'] == 'Change department limits, score weights, preference points and renewal in Configuration, not a single run'
    assert rejected['error']['details'] == [{'field': 'generation_options.scoring.' + field,
        'message': 'Use department Auto-schedule & scoring settings'}]
    assert h.get('admin', '/api/me')['department']['generation_defaults'] == before
    assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] == []


def test_legacy_full_options_match_current_policy_without_rewriting_old_jobs(h):
    from .test_staff_wishes import department_settings
    defaults = GenerationOptions.model_validate(h.get('admin', '/api/me')['department']['generation_defaults']).model_dump(mode='json')
    w = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': defaults}, revision=w['revision'])['job']
    process_cp_sat(h)
    stored = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    with transaction(h.app.state.engine) as db:
        snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
    assert snapshot['generation_options'] == defaults
    changed = copy.deepcopy(defaults)
    changed['scoring']['weights']['coverage'] += 1
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': changed},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    w = h.refresh_draft_baseline(w)
    rejected = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': defaults}, revision=w['revision'], status=422)
    assert rejected['error']['code'] == 'DEPARTMENT_OPTIONS_ONLY'
    assert rejected['error']['details'][0]['field'] == 'generation_options.scoring.weights.coverage'
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result'] == stored['result']
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Job, job['id']).input_snapshot
        for key in ('generation_options', 'generation_defaults',
                    'generation_options_overridden', 'diagnostic_proposal'):
            assert current[key] == snapshot[key]
        assert current['baseline']['hash'] == snapshot['baseline']['hash']
    fresh = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, fresh['id']).input_snapshot['generation_options'] == changed


@pytest.mark.parametrize('rule_strength,run_leads', [
    ('off', 'medium'), ('medium', 'off'), ('off', None), ('medium', None),
])
def test_approved_lead_rule_loads_history_and_legacy_run_flags_cannot_override_it(h, rule_strength, run_leads):
    from .test_staff_wishes import department_settings
    from .test_team_lead import anyone_leads, lead_counts, lead_goal, week
    from packages.domain.fairness import goal_identity
    first, second = h.seed['people'][:2]
    typ = anyone_leads(h)
    if rule_strength == 'off':
        fair = next(p for p in h.get('admin', '/api/policies?status=active')['policies']
            if p['definition']['rule'].get('id') == 'fair_share')
        rule = fair['definition']['rule']
        for line in rule['lines']:
            if line.get('settings', {}).get('balance_goal', {}).get('assignment_type_ids') == [typ]:
                line['numbers']['apply']['level'] = 'off'
        proposal = h.command('admin', 'rule.propose', {'catalogue_id': 'fair_share',
            'catalogue_version': rule['catalogue_version'], 'expected_policy_id': fair['id'],
            'expected_revision': fair['revision'], 'lines': rule['lines'],
            'reason': 'Explicitly disable the approved lead-sharing goal'})['record']
        h.command('approver', 'policy.activate', {'reason': 'Independent lead-sharing review'},
            proposal['id'], proposal['revision'])
    department_leads = 'off' if rule_strength == 'medium' else 'medium'
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {'scoring': {'lead_balance': department_leads}}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    h.publish(week(h, typ, 'Published lead history', 4, lambda s, e: [
        {'person_id': first, 'start': s, 'end': e, 'team_lead': True}, second], days=2))
    w = week(h, typ, 'Next lead turns', 11, lambda s, e: [first, second], days=2)
    department = h.get('admin', '/api/me')['department']['generation_defaults']
    publications = h.get('coordinator', '/api/publications')
    override = {'mode': 'fill_gaps', 'shortlist_width': 0}
    if run_leads is not None:
        override['scoring'] = {'lead_balance': run_leads}
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'], payload={
            'workspace_id': w['id'], 'kind': 'generation', 'generation_options': override}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    process_cp_sat(h)
    with transaction(h.app.state.engine) as db:
        snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
    lead = lead_goal(snapshot, typ)
    assert lead['definition']['strength'] == rule_strength
    assert 'lead_balance' not in snapshot
    goal_id = goal_identity(lead)
    history = snapshot.get('fairness_history', {}).get(goal_id)
    if rule_strength == 'off':
        assert not history or not history['people']
    else:
        assert history['people'][first]['value'] == 2 and history['people'][second]['value'] == 0
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    goals = [g for g in result['validation'].get('goals', []) if g['goal_id'] == goal_id]
    if rule_strength == 'off':
        assert all(not goal['people'] and not goal['pairs'] for goal in goals)
    else:
        assert len(goals) == 1 and goals[0]['history']
        rows = {r['person_id']: r for r in goals[0]['people']}
        assert rows[first]['prior'] == 2 and rows[second]['prior'] == 0
        assert lead_counts(result) == {second: 2}
    assert h.get('admin', '/api/me')['department']['generation_defaults'] == department
    assert h.get('coordinator', '/api/publications') == publications
    manual = h.snapshot(w)
    assert lead_goal(manual, typ)['definition']['strength'] == rule_strength
    manual_history = manual.get('fairness_history', {}).get(goal_id)
    if rule_strength == 'off':
        assert not manual_history or not manual_history['people']
    else:
        assert manual_history['people'] == history['people']
    assert result['summary']['score_before']['score'] == h.detail(w)['workspace']['validation']['score']['score']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).input_snapshot == snapshot
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Review the proposed lead assignments'}, revision=w['revision'])
    assert 'score' not in applied['validation']
    assert h.detail(applied['workspace'])['workspace']['validation']['score']['score'] == result['summary']['score']['score']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == result['summary']


def test_run_wish_catchup_keeps_the_saved_draft_score_before_and_after_apply(h):
    from .test_staff_wishes import wish
    person = h.seed['people'][0]
    wish(h, '2026-10-04', points=4)
    wish(h, '2026-10-06')
    h.publish(h.shift(h.workspace('Declined wishes'), people=[person])['workspace'])
    h.policy({'kind': 'allocation_preference', 'priority': 1}, {'person_id': person}, severity='advisory')
    wish(h, '2026-10-12', points=2)
    w = h.workspace('Catch-up score consistency', start='2026-10-11', end='2026-10-18')
    w = h.shift(w, start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00', people=[person])['workspace']
    w = h.shift(w, start='2026-10-13T08:00:00+03:00', end='2026-10-13T12:00:00+03:00', people=[])['workspace']
    before = h.detail(w)['workspace']['validation']['score']['score']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=w['revision'], payload={
            'workspace_id': w['id'], 'kind': 'generation', 'generation_options': {
                'mode': 'fill_gaps', 'scoring': {'wish_debt_cap': 1}}}),
            replace(h.settings, generation_engine='cp_sat'))['job']
    process_cp_sat(h, solver_workers=2)
    with transaction(h.app.state.engine) as db:
        snapshot = copy.deepcopy(db.get(m.Job, job['id']).input_snapshot)
    assert snapshot['wishes'][0]['multiplier'] == 1
    assert snapshot['score_context']['wishes'][0]['multiplier'] == 1.35
    assert set(snapshot['score_context']) == {'wishes'}
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert result['status'] == 'candidate_feasible' and result['additions'] and not result['removals']
    assert result['summary']['score_before']['score'] == before
    run_score = schedule_score(snapshot, evaluate(snapshot), snapshot['generation_defaults'])
    # Mandatory gaps cap both shown values; the preference component still retains the department wish weights.
    assert run_score['components']['preferences'] != result['summary']['score_before']['components']['preferences']
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Review the catch-up proposal'}, revision=w['revision'])
    assert 'score' not in applied['validation']
    assert h.detail(applied['workspace'])['workspace']['validation']['score']['score'] == result['summary']['score']['score']
    assert h.detail(applied['workspace'])['workspace']['validation']['score']['score'] == result['summary']['score']['score']
    assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']['summary'] == result['summary']
