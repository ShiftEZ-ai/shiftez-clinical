"""Small job-local population runs preserve existing evidence and keep complete safety context."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.main import create_app
from apps.api.services import jobs
from apps.api.snapshots import currency_hint
from apps.api.services.jobs import catalogue_proof_identity
from apps.worker.main import claim_job, prepare_deferred_generation, process_one
from packages.contracts import schemas as s
from packages.domain import cp_sat, generation
from packages.domain.generation_scope import (population_allows, protects_existing, selected_session_ids,
                                              has_population, run_population_people, union_population)
from packages.domain.solver_resume import resume_key


def scope(field, *values, protection='keep_existing'):
    return {'conditions': {'match': 'all', 'conditions': [{'field': field, 'op': 'any', 'values': list(values)}]},
            'protection': protection}


def create(h, w, population=None, **extra):
    return h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        **({'generation_scope': population} if population is not None else {}), **extra}, revision=w['revision'])['job']


def result(h, job):
    return h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']


def apply(h, w, job, status=200):
    return h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Apply reviewed population pass'}, revision=w['revision'], status=status)


@pytest.mark.parametrize('selection, expected', [
    ({}, ['a', 'b']), ({'generation_session_ids': None}, ['a', 'b']),
    ({'generation_session_ids': []}, []), ({'generation_session_ids': ['b']}, ['b'])])
def test_frozen_session_selection_and_population_share_empty_semantics(selection, expected):
    data = {'focus_ids': ['a', 'b'], 'shifts': [{'id': 'a'}, {'id': 'b'}],
            'people': {'p': {}, 'q': {}}, 'planning_population': {'a': ['p'], 'b': ['q']}, **selection}
    population = {pid for sid in expected for pid in data['planning_population'][sid]}
    assert selected_session_ids(data) == expected
    assert has_population(data) == bool(expected)
    assert run_population_people(data) == population
    assert union_population(data, [data]) == sorted(population)


@pytest.mark.parametrize('engine', ['legacy', 'cp_sat'])
def test_two_pass_current_data_preserves_all_prior_fields_and_undo(h, engine):
    h.settings = replace(h.settings, generation_engine=engine)
    h.app = create_app(h.settings)
    clinician = h.seed['people'][1]
    typ = h.configure('assignment_types', {'name': 'Incremental supervision (test)', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 3}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[])['workspace']
    first = create(h, w, scope('person_ids', clinician), diagnostic_proposal=engine == 'cp_sat')
    process_one(h.app.state.engine)
    assert {a['participant']['person_id'] for a in result(h, first)['additions']} == {clinician}
    w = apply(h, w, first)['workspace']
    assert not w['validation']['valid']
    h.lifecycle(w, 'submit', status=422)
    old_shift = deepcopy(h.detail(w)['shifts'][0])
    original = deepcopy(old_shift['participants'])
    # A new person's data arrives only after the first pass was applied.
    trainee = h.configure('people', {'display_name': 'Later trainee (test)', 'tags': ['Later intake']})['record']['id']
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-04'})
    h.command('credentialer', 'eligibility.create', {'person_id': trainee,
        'service_id': h.seed['services'][0], 'starts_on': '2026-10-04'})
    population = scope('tags', 'Later intake')
    population['conditions']['conditions'].append({'field': 'category_ids', 'values': [h.seed['trainee_category']]})
    second = create(h, w, population)
    # Assignment Apply may queue a complete validation before this generation job.
    while h.get('coordinator', '/api/jobs/' + second['id'])['job']['status'] == 'queued':
        assert process_one(h.app.state.engine)
    proposal = result(h, second)
    assert proposal['status'] == 'candidate_feasible', proposal
    assert not proposal['removals']
    assert len(proposal['additions']) == 1
    participant = proposal['additions'][0]['participant']
    assert participant['person_id'] == trainee and participant['supervisor_person_id'] == clinician
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, second['id'])
        assert trainee in stored.input_snapshot['people']
        assert len(stored.input_snapshot['people']) > 2  # Complete context, not the selected population.
        assert stored.input_snapshot['generation_scope'] == s.GenerationScope.model_validate(population).model_dump(mode='json')
        assert 'people' not in stored.read_metadata['input_snapshot']
    for view in (h.get('coordinator', '/api/jobs/' + second['id'])['job'],
                 next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] if j['id'] == second['id'])):
        assert view['generation_scope'] == s.GenerationScope.model_validate(population).model_dump(mode='json')
        assert 'input_snapshot' not in view
    w = apply(h, w, second)['workspace']
    after = h.detail(w)['shifts'][0]
    assert [p for p in after['participants'] if p['person_id'] == clinician] == original
    assert {k: v for k, v in after.items() if k not in {'participants', 'revision'}} == {
        k: v for k, v in old_shift.items() if k not in {'participants', 'revision'}}
    assert w['validation']['valid']
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id']).order_by(m.ChangeSet.created_at.desc())).reason
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'generation.apply'))
    w = h.command('coordinator', 'workspace.undo', {'reason': 'Undo only the later pass'}, w['id'], w['revision'])['workspace']
    assert h.detail(w)['shifts'][0]['participants'] == original


@pytest.mark.parametrize('engine', [generation.generate, cp_sat.generate])
@pytest.mark.parametrize('protection', ['keep_existing', 'rebalance_selected'])
def test_outsider_context_metadata_and_candidate_reduction(h, engine, protection):
    clinician, trainee = h.seed['people'][1], h.seed['people'][0]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Scoped paired clinic (test)', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
            'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[clinician])['workspace']
    data = h.snapshot(w)
    data['generation_scope'] = scope('person_ids', trainee, protection=protection)
    data['generation_options'] = {'mode': 'rebalance'}
    original = deepcopy(data)
    planned = engine(data, 5)
    assert planned['status'] == 'candidate_feasible', planned
    assert planned['removals'] == []
    assert [a['participant']['person_id'] for a in planned['additions']] == [trainee]
    assert planned['additions'][0]['participant']['supervisor_person_id'] == clinician
    assert planned['validation']['valid']
    assert data == original
    if engine is cp_sat.generate:
        assert {e['person_id'] for e in planned['candidate_evidence']} == {trainee}


def test_dated_population_requires_whole_actual_interval_and_keys_all_scope_inputs(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    pid = h.seed['people'][0]
    category = h.seed['trainee_category']
    data['memberships'] = [r for r in data['memberships'] if r['person_id'] != pid] + [
        {'id': str(uuid4()), 'person_id': pid, 'category_id': category, 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]
    data.pop('_person_rows', None)
    data['generation_scope'] = scope('category_ids', category)
    shift = data['shifts'][0]
    assert population_allows(data, shift, pid)
    crossing = {**shift, 'start': '2026-10-04T23:00:00+03:00', 'end': '2026-10-05T01:00:00+03:00'}
    assert not population_allows(data, crossing, pid)
    # OR is re-evaluated at each boundary; two ranks may jointly cover the complete duty.
    later = str(uuid4())
    data['categories'][later] = {**data['categories'][category], 'id': later}
    data['memberships'].append({'id': str(uuid4()), 'person_id': pid, 'category_id': later,
        'starts_on': '2026-10-05', 'ends_on': None})
    data['generation_scope']['conditions']['conditions'][0]['values'].append(later)
    assert population_allows(data, crossing, pid)
    participant = {'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'fixed': False}
    assert protects_existing(data, shift, participant)  # Includes selected people in Keep existing.
    options = s.GenerationOptions()
    key = resume_key(data, options, False, False)
    other = deepcopy(data)
    other['generation_scope']['protection'] = 'rebalance_selected'
    assert resume_key(other, options, False, False) != key
    other = deepcopy(data)
    other['generation_scope']['conditions']['conditions'][0]['values'].pop()
    assert resume_key(other, options, False, False) != key
    other = deepcopy(data)
    other['memberships'][0]['ends_on'] = '2026-10-06'
    assert resume_key(other, options, False, False) != key


@pytest.mark.parametrize('protection', ['keep_existing', 'rebalance_selected'])
@pytest.mark.parametrize('attack', ['old_worker', 'out_of_scope', 'remove', 'overwrite', 'outside_duty', 'source', 'empty_duties'])
def test_apply_rejects_untrusted_scoped_proposal_before_any_assignment_writes(h, attack, protection):
    outsider, selected = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[outsider])['workspace']
    baseline = deepcopy(h.detail(w)['shifts'])
    job = create(h, w, scope('person_ids', selected, protection=protection))
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        request = row.input_snapshot
    source = {**request, **h.snapshot(w),
              'generation_scope_source': catalogue_proof_identity()['source_digest']}
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        shift = source['shifts'][0]
        participant = deepcopy(shift['participants'][0])
        participant['person_id'] = outsider if attack in {'out_of_scope', 'overwrite'} else selected
        proposal = {'status': 'candidate_with_gaps', 'additions': [{'shift_id': shift['id'], 'participant': participant}],
            'removals': [], 'generation_scope': source['generation_scope'],
            'generation_scope_source': source['generation_scope_source']}
        if attack == 'old_worker':
            proposal.pop('generation_scope')
        if attack == 'source':
            proposal['generation_scope_source'] = 'old-source'
        if attack == 'outside_duty':
            proposal['additions'][0]['shift_id'] = str(uuid4())
        if attack == 'empty_duties':
            source['generation_session_ids'] = []
        if attack == 'remove':
            proposal['removals'] = [{'assignment_id': shift['participants'][0]['id'],
                'shift_id': shift['id'], 'person_id': outsider}]
        if attack == 'overwrite':
            source = {**source, 'generation_scope': scope('person_ids', outsider)}
            proposal['generation_scope'] = source['generation_scope']
        row.input_snapshot = source
        row.status, row.result = 'succeeded', proposal
    writes = []
    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith(('insert into assignments', 'delete from assignments', 'update assignments')):
            writes.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', record_write)
    try:
        reply = apply(h, w, job, status=409)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', record_write)
    assert writes == []
    assert reply['error']['code'] in {'STALE_SCHEDULER', 'INVALID_SCOPE', 'PROTECTED_ASSIGNMENT'}
    assert h.detail(w)['shifts'] == baseline
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).status == 'succeeded'
        assert not db.scalar(select(m.ChangeSet).where(m.ChangeSet.workspace_id == w['id'], m.ChangeSet.reason == 'Apply reviewed population pass'))


def test_contract_scope_references_empty_selection_and_legacy_omission(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    legacy = s.JobInput(workspace_id=w['id'], kind='generation')
    assert 'generation_scope' not in legacy.model_dump(mode='json')
    for population, kind in ((scope('service_ids', h.seed['services'][0]), 'generation'),
                             (scope('person_ids', h.seed['people'][0]), 'validation'),
                             ({'conditions': {'conditions': []}}, 'generation')):
        with pytest.raises(ValueError):
            s.JobInput(workspace_id=w['id'], kind=kind, generation_scope=population)
    h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_scope': scope('person_ids', str(uuid4()))}, revision=w['revision'], status=404)
    empty = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_scope': scope('tags', 'No matching intake')}, revision=w['revision'], status=422)
    assert empty['error']['code'] == 'INVALID_SCOPE'
    job = create(h, w)
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert 'generation_scope' not in snapshot and 'generation_scope_source' not in snapshot
    assert 'generation_scope' not in result(h, job)
    assert 'generation_scope' not in h.get('coordinator', '/api/jobs/' + job['id'])['job']


@pytest.mark.parametrize('action', ['apply', 'continue'])
def test_fresh_population_data_reconciles_apply_and_preserves_frozen_continue(h, action):
    if action == 'continue':
        h.settings = replace(h.settings, generation_engine='cp_sat')
        h.app = create_app(h.settings)
    w = h.shift(h.workspace(), people=[])['workspace']
    job = create(h, w, scope('person_ids', h.seed['people'][0]))
    process_one(h.app.state.engine)
    h.command('coordinator', 'membership.create', {'person_id': h.seed['people'][0],
        'category_id': h.seed['trainee_category'], 'starts_on': '2026-10-05'})
    if action == 'apply':
        reply = apply(h, w, job)
        assert reply['reconciliation']['status'] == 'applied'
        assert reply['reconciliation']['changed_facts']
        return
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        prepared_fingerprint = row.input_fingerprint
        row.result = {**row.result, 'status': 'needs_more_time'}
    reply = h.command('coordinator', 'job.continue', {'minutes': 1, 'reason': 'Continue frozen selection'}, job['id'])
    assert reply['job']['input_fingerprint'] == prepared_fingerprint


@pytest.mark.parametrize('engine', [generation.generate, cp_sat.generate])
def test_same_group_rebalance_is_explicit_and_keep_preserves_selected_rows(h, engine):
    first, second = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[first])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    data['availability'].append({'id': str(uuid4()), 'person_id': first, 'kind': 'recorded_leave',
        'start': shift['start'], 'end': shift['end']})
    data['generation_options'] = {'mode': 'rebalance'}
    data['generation_scope'] = scope('person_ids', first, second)
    kept = engine(data, 5)
    assert not kept.get('removals')
    assert not any(a['participant']['person_id'] == first for a in kept.get('additions', []))
    data['generation_scope']['protection'] = 'rebalance_selected'
    planned = engine(data, 5)
    assert planned['status'] == 'candidate_feasible', planned
    assert len(planned['removals']) == 1 and planned['removals'][0]['person_id'] == first
    assert {a['participant']['person_id'] for a in planned['additions']} == {second}


@pytest.mark.parametrize('engine', [generation.generate, cp_sat.generate])
def test_kept_unavailable_people_remain_visible_blockers(h, engine):
    outsider, selected = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[outsider])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    data['availability'].append({'id': str(uuid4()), 'person_id': outsider, 'kind': 'recorded_leave',
        'start': shift['start'], 'end': shift['end']})
    data['generation_scope'] = scope('person_ids', selected)
    planned = engine(data, 5)
    assert not planned.get('removals')
    assert planned['status'] != 'candidate_feasible'
    issues = planned.get('issues', []) + (planned.get('validation') or {}).get('findings', [])
    assert any(f.get('person_id') == outsider and f.get('severity', 'error') == 'error' for f in issues), planned


@pytest.mark.parametrize('engine', [generation.generate, cp_sat.generate])
def test_scoped_run_cannot_promote_a_protected_existing_lead(h, engine):
    outsider, selected = h.seed['people'][:2]
    from .test_team_lead import lead_type
    typ = lead_type(h, requirements=[{'minimum': 1, 'maximum': 1}])
    w = h.shift(h.workspace(), typ=typ, people=[outsider])['workspace']
    data = h.snapshot(w)
    data['generation_scope'] = scope('person_ids', selected)
    planned = engine(data, 5)
    assert not planned.get('removals') and not planned.get('additions')
    assert planned['status'] != 'candidate_feasible'


def test_matching_worker_source_is_required_before_scheduler_execution(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    job = create(h, w, scope('person_ids', h.seed['people'][0]))
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'generation_scope_source': 'obsolete-source'}
    called = []
    def unaware_scheduler(snapshot, **kwargs):
        called.append(snapshot)
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}
    process_one(h.app.state.engine, scheduler=unaware_scheduler)
    assert called == []
    stored = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert stored['status'] == 'failed'
    assert not h.detail(w)['shifts'][0]['participants']


def test_queued_population_binds_source_and_rejects_changed_build_at_lease(h, monkeypatch):
    w = h.shift(h.workspace(), people=[])['workspace']
    expected_source = catalogue_proof_identity()['source_digest']
    job = create(h, w, scope('person_ids', h.seed['people'][0]))
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        assert row.input_snapshot['generation_scope_source'] == expected_source
        claim = claim_job(db)
    monkeypatch.setattr(jobs, 'catalogue_proof_identity', lambda: {'source_digest': 'different-build'})
    with pytest.raises(Problem) as error:
        prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    assert error.value.code == 'STALE_INPUTS'


@pytest.mark.parametrize('protection', ['keep_existing', 'rebalance_selected'])
def test_apply_checks_population_against_actual_assignment_not_only_duty_start(h, protection):
    pid, other = h.seed['people'][:2]
    w = h.shift(h.workspace(), people=[other])['workspace']
    job = create(h, w, scope('person_ids', pid, protection=protection))
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        request = deepcopy(row.input_snapshot)
    source = {**request, **h.snapshot(w),
              'generation_scope_source': catalogue_proof_identity()['source_digest']}
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        shift = source['shifts'][0]
        source['generation_scope'] = scope('category_ids', h.seed['trainee_category'], protection=protection)
        source['memberships'] = [r for r in source['memberships'] if r['person_id'] != pid] + [
            {'id': str(uuid4()), 'person_id': pid, 'category_id': h.seed['trainee_category'],
             'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]
        row.input_snapshot = source
        # Matching at the start is insufficient when the person's rank ends during the proposal.
        row.result = {'status': 'candidate_with_gaps', 'generation_scope': source['generation_scope'],
            'generation_scope_source': source['generation_scope_source'], 'removals': [],
            'additions': [{'shift_id': shift['id'], 'participant': {'person_id': pid,
                'start': '2026-10-04T23:00:00+03:00', 'end': '2026-10-05T01:00:00+03:00'}}]}
        row.status = 'succeeded'
    assert apply(h, w, job, status=409)['error']['code'] == 'INVALID_SCOPE'
    assert [p['person_id'] for p in h.detail(w)['shifts'][0]['participants']] == [other]


def test_generation_apply_population_change_moves_currency_hint(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    job = create(h, w, scope('person_ids', h.seed['people'][0]))
    process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        before = currency_hint(db, db.get(m.Workspace, w['id']))
    reply = apply(h, w, job)
    with transaction(h.app.state.engine) as db:
        after = currency_hint(db, db.get(m.Workspace, w['id']))
    assert reply['reconciliation']['population_recorded']
    assert after['content_revision'] == before['content_revision'] + 1
