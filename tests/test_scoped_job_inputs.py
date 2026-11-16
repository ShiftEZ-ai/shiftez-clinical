"""Scoped semantic currency and atomic proposal salvage; concurrency uses explicit barriers."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import event
from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning, generation_reconciliation
from apps.worker.main import claim_job, heartbeat_job, finish_job, prepare_deferred_generation
from packages.domain.input_manifest import manifest
from packages.domain.validation import evaluate
from packages.domain.proposal_reconciliation import comparison
from packages.domain.generation_scope import selected_session_ids
from .test_autoschedule_budget_api import budget_h  # noqa: F401  (the CP-SAT harness)


def wish(h, day='2026-12-17'):
    return h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'kind': 'prefer_off',
        'first_day': day, 'last_day': day, 'points': 1})['wish']


def proposal(h, w, people=None):
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'},
                    revision=w['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    source = h.snapshot(w)
    shifts = [s for s in source['shifts'] if s['id'] in source['focus_ids']]
    result = {'status': 'candidate_feasible', 'additions': [
        {'shift_id': row['id'], 'participant': {'id': str(uuid4()), 'person_id': (people or h.seed['people'])[i],
            'start': row['start'], 'end': row['end']}} for i, row in enumerate(shifts)], 'removals': [],
        'input_fingerprint': source['fingerprint'], 'optimality_proven': True}
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Job, job['id']); stored.status='succeeded'; stored.result=result
    return job, source, result


def apply(h, w, job, **kw):
    return h.command('coordinator', 'generation.apply', {'workspace_id': w['id'], 'job_id': job['id'],
        'reason': 'Review retained proposal'}, revision=w['revision'], **kw)


@pytest.mark.parametrize('selection', ['missing', 'empty', 'selected'])
def test_request_session_selection_is_normalized_before_freezing(h, selection):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    focus = h.snapshot(w)['focus_ids']
    requested = focus[:1] if selection == 'selected' else []
    payload = {'workspace_id': w['id'], 'kind': 'generation'}
    if selection != 'missing':
        payload['session_ids'] = requested
    job = h.command('coordinator', 'job.create', payload, revision=w['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert snapshot.get('generation_session_ids') == (requested or None)
        assert ('generation_session_ids' in snapshot) == bool(requested)
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Job, job['id']).input_snapshot
        assert set(selected_session_ids(snapshot)) == set(requested or focus)
        assert ('generation_session_ids' in snapshot) == bool(requested)


@pytest.mark.parametrize('selection', ['missing', 'empty', 'selected'])
def test_apply_uses_frozen_session_selection_without_empty_fallback(h, selection):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job, source, result = proposal(h, w)
    selected = [result['additions'][0]['shift_id']] if selection == 'selected' else []
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        if selection != 'missing':
            row.input_snapshot = {**row.input_snapshot, 'generation_session_ids': selected}
    payload = {'workspace_id': w['id'], 'job_id': job['id'], 'reason': 'Apply frozen session selection'}
    if selection == 'selected':
        payload['addition_indexes'] = [0]
    before = h.snapshot(w)
    reply = h.command('coordinator', 'generation.apply', payload, revision=w['revision'],
                      status=409 if selection == 'empty' else 200)
    if selection == 'empty':
        assert reply['error']['code'] == 'INVALID_SCOPE'
        assert h.snapshot(w) == before
        assert h.get('coordinator', '/api/jobs/' + job['id'])['job']['status'] == 'succeeded'
    else:
        assigned = {row['id'] for row in h.detail(w)['shifts'] if row['participants']}
        assert assigned == set(selected if selection == 'selected' else source['focus_ids'])


def test_manifest_ignores_metadata_and_counters_but_detects_phantoms(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    before = h.snapshot(w)
    wish(h)
    with transaction(h.app.state.engine, write=True) as db:
        person=db.get(m.Person,h.seed['people'][0]); person.display_name='Different display'; person.revision+=1
        workspace=db.get(m.Workspace,w['id']); workspace.name='New name'; workspace.revision+=1
    after=h.snapshot(w)
    assert after['fingerprint']==before['fingerprint']
    wish(h,'2026-10-04')
    assert h.snapshot(w)['fingerprint']!=before['fingerprint']
    drift=copy.deepcopy(before)
    drift['availability'].append({'id':'inserted','person_id':h.seed['people'][0],
        'start':'2026-10-04T06:00:00+00:00','end':'2026-10-04T08:00:00+00:00','counts_as_unavailable':True})
    assert manifest(drift)['components']['mandatory_inputs']!=before['input_manifest']['components']['mandatory_inputs']


@pytest.mark.postgres
@pytest.mark.parametrize('day',['2026-12-17','2026-10-04'])
def test_running_job_survives_input_edits(pg,day):
    w=pg.shift(pg.workspace(),people=[])['workspace']
    job=pg.command('coordinator','job.create',{'workspace_id':w['id'],'kind':'generation'},revision=w['revision'])['job']
    with transaction(pg.app.state.engine,write=True) as db:
        claim=claim_job(db)
    wish(pg,day)
    with transaction(pg.app.state.engine,write=True) as db:
        assert heartbeat_job(db,claim,{'stage':'searching'})
        assert finish_job(db,claim,{'status':'unknown_time_limit','additions':[],'removals':[]},None)
    assert pg.get('coordinator','/api/jobs/'+job['id'])['job']['status']=='succeeded'


@pytest.mark.postgres
@pytest.mark.parametrize('change',['unrelated_wish','metadata','related_wish','content'])
def test_check_compare_boundary(pg,monkeypatch,change):
    w=pg.shift(pg.workspace(),people=[])['workspace']
    pg.client('staff')
    entered,release=Event(),Event()
    real=planning.evaluate
    def paused(data,**kwargs):
        entered.set(); assert release.wait(600)
        return real(data,**kwargs)
    monkeypatch.setattr(planning,'evaluate',paused)
    expected=409 if change in {'related_wish','content'} else 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(pg.command,'coordinator','workspace.validate',{'reason':'Check'},w['id'],w['revision'],status=expected)
        try:
            assert entered.wait(600)
            if change.endswith('wish'):
                wish(pg,'2026-10-04' if change=='related_wish' else '2026-12-17')
            else:
                with transaction(pg.app.state.engine,write=True) as db:
                    row=db.get(m.Workspace,w['id']); row.name='Concurrent edit'; row.revision+=1
                    if change=='content': row.content_revision+=1
        finally: release.set()
        result=future.result(timeout=600)
    if expected==409:
        assert result['error']['code']=='STALE_INPUTS'
        assert result['error']['details']['recoverable']
        assert result['error']['details']['validation']
    else:
        assert result['validation']['fingerprint']==pg.snapshot(w)['fingerprint']


def test_soft_wish_reconciles_without_losing_original_result_and_is_idempotent(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    job,source,original=proposal(h,w)
    added=wish(h,'2026-10-04')
    key=str(uuid4())
    applied=apply(h,w,job,key=key)
    receipt=applied['reconciliation']
    assert receipt['status']=='applied' and receipt['addition_indexes']==[0]
    assert applied['fix_list']['summary']['must'] == 0
    assert applied['fix_list_delta']['cleared'] and not applied['fix_list_delta']['new']
    assert applied['validation']['fix_list_context']['workspace']['id'] == w['id']
    assert receipt['conflicting_placement_count']==0
    fact=next(r for r in receipt['changed_facts'] if r['kind']=='wishes' and r['id']==added['id'])
    assert fact['actor']['id']==h.seed['identities']['staff']
    assert fact['person_name'] and fact['first_day']=='2026-10-04'
    assert apply(h,w,job,key=key)['reconciliation']==receipt
    with transaction(h.app.state.engine) as db:
        stored=db.get(m.Job,job['id'])
        assert stored.input_snapshot['fingerprint']==source['fingerprint']
        assert all(stored.result[k]==v for k,v in original.items())
    current=h.snapshot(w)
    assert current['fingerprint']==h.detail(w)['workspace']['validation']['fingerprint']
    assert applied['validation']['score'] == receipt['validation']['score']


def test_absence_rejects_only_conflicting_bundle(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[])['workspace']
    job,source,_=proposal(h,w)
    with transaction(h.app.state.engine,write=True) as db:
        db.add(m.Availability(person_id=h.seed['people'][0],kind='recorded_leave',department_id=h.seed['department_id'],recorded_by=h.seed['identities']['coordinator'],start=__import__('datetime').datetime.fromisoformat('2026-10-04T08:00:00+03:00'),
            end=__import__('datetime').datetime.fromisoformat('2026-10-04T12:00:00+03:00'),private_reason='Private medical reason'))
    before=h.snapshot(w)
    applied=apply(h,w,job)
    assert applied['reconciliation']['addition_indexes']==[1],applied['reconciliation']
    assert applied['reconciliation']['conflicting_placement_count']==1
    assert 'Private medical reason' not in str(applied)
    assert comparison(evaluate(before),evaluate(h.snapshot(w)))['no_new_problems']


def test_coupled_quota_selects_joint_subset(h):
    w=h.shift(h.workspace(),people=[])['workspace']
    w=h.shift(w,start='2026-10-05T08:00:00+03:00',end='2026-10-05T12:00:00+03:00',people=[])['workspace']
    pid=h.seed['people'][0]
    job,_,_=proposal(h,w,[pid,pid])
    h.policy({'kind':'max_duties','maximum':1,'window':'week'},{'person_id':pid})
    w=h.refresh_draft_baseline(w)
    before=h.snapshot(w)
    applied=apply(h,w,job)
    assert len(applied['reconciliation']['addition_indexes'])==1,applied['reconciliation']
    assert comparison(evaluate(before),evaluate(h.snapshot(w)))['no_new_problems']


@pytest.mark.postgres
@pytest.mark.parametrize('change',['absence','planner_content'])
def test_apply_compare_boundary_retains_proposal_without_writes(pg,monkeypatch,change):
    w=pg.shift(pg.workspace(),people=[])['workspace']
    job,_,original=proposal(pg,w)
    entered,release=Event(),Event()
    real=generation_reconciliation.prepare
    def paused(*args,**kwargs):
        plan=real(*args,**kwargs); entered.set(); assert release.wait(600); return plan
    monkeypatch.setattr(generation_reconciliation,'prepare',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(apply,pg,w,job)
        try:
            assert entered.wait(600)
            with transaction(pg.app.state.engine,write=True) as db:
                if change=='absence':
                    from packages.domain.time import aware
                    db.add(m.Availability(person_id=pg.seed['people'][0],kind='recorded_leave',department_id=pg.seed['department_id'],recorded_by=pg.seed['identities']['coordinator'],private_reason='Private',start=aware('2026-10-04T08:00:00+03:00'),end=aware('2026-10-04T12:00:00+03:00')))
                else:
                    row=db.get(m.Workspace,w['id']); row.content_revision+=1; row.revision+=1
        finally: release.set()
        result=future.result(timeout=600)
    receipt = result['reconciliation']
    assert receipt['status']=='revalidation_required'
    assert result['proposal_action'] == 'reconcile_and_apply' and result['proposal_reviewable'] is True
    assert receipt['prepared_fingerprint'] != receipt['current_fingerprint']
    assert not receipt['validation_current']
    assert receipt['changed_facts']
    if change == 'absence':
        assert any(row['kind'] == 'availability' for row in receipt['changed_facts'])
    assert not h_assignments(pg,w)
    with transaction(pg.app.state.engine) as db:
        stored=db.get(m.Job,job['id']); assert stored.status=='succeeded'
        assert stored.result['additions']==original['additions']


def h_assignments(h,w):
    return [p for row in h.detail(w)['shifts'] for p in row['participants']]


@pytest.mark.parametrize('harness_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_named_input_changes_and_receipt_pages_are_scoped(request, harness_name):
    h = request.getfixturevalue(harness_name)
    w = h.shift(h.workspace(), people=[])['workspace']
    job, _, _ = proposal(h, w)
    added = wish(h, '2026-10-04')
    path = '/api/jobs/' + job['id']
    changes = h.get('coordinator', path + '/input-changes?limit=1')
    assert changes['total'] >= 1
    all_changes = h.get('coordinator', path + '/input-changes')['changes']
    fact = next(row for row in all_changes if row['id'] == added['id'])
    assert fact['actor']['id'] == h.seed['identities']['staff']
    assert fact['after']['kind'] == 'prefer_off' and fact['after']['points'] == 1
    receipt = apply(h, w, job)['reconciliation']
    page = h.get('coordinator', path + '/reconciliation?section=changed_facts&limit=1')
    assert page['receipt_id'] == receipt['id'] and page['total'] == receipt['changed_fact_count']
    assert page['changed_facts'] == receipt['changed_facts'][:1]
    empty = h.get('coordinator', path + '/reconciliation?offset=10000')
    assert not empty['has_more'] and empty['changed_facts'] == []
    h.get('staff', path + '/input-changes', status=403)
    h.get('staff', path + '/reconciliation', status=403)


def test_same_session_independent_additions_salvage_and_undo(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    job, source, original = proposal(h, w)
    shift = source['shifts'][0]
    original['additions'].append({'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}})
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job['id']).result = original
        db.add(m.Availability(person_id=h.seed['people'][0], department_id=h.seed['department_id'],
            kind='recorded_leave', recorded_by=h.seed['identities']['coordinator'],
            start=__import__('datetime').datetime.fromisoformat(shift['start']),
            end=__import__('datetime').datetime.fromisoformat(shift['end']), private_reason='Private'))
    reply = apply(h, w, job)
    receipt = reply['reconciliation']
    assert receipt['addition_indexes'] == [1] and receipt['conflicting_placement_count'] == 1
    assert receipt['conflicting_placements'][0]['person_ids'] == [h.seed['people'][0]]
    assert {p['person_id'] for p in h_assignments(h, w)} == {h.seed['people'][1]}
    undone = h.lifecycle(reply['workspace'], 'undo')['workspace']
    assert not h_assignments(h, undone)
    stored = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert stored['result']['additions'] == original['additions']
    assert stored['reconciliation']['status'] == 'undone'


def test_disjoint_session_content_is_preserved_and_overlapping_proposal_is_recoverable(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    job, source, _ = proposal(h, w)
    first = source['shifts'][0]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Shift, first['id']); row.locked = True; row.revision += 1
        workspace = db.get(m.Workspace, w['id']); workspace.revision += 1; workspace.content_revision += 1
        revision = workspace.revision
    w = {**w, 'revision': revision}
    reply = apply(h, w, job)
    receipt = reply['reconciliation']
    assert receipt['addition_indexes'] == [1]
    conflict = receipt['conflicting_placements'][0]
    assert conflict['code'] == 'CONTENT_CONFLICT' and not conflict['launch']['locked'] and conflict['current']['locked']
    current = h.detail(w)['shifts']
    assert next(s for s in current if s['id'] == first['id'])['locked']
    assert len(h_assignments(h, w)) == 1


def test_stale_planner_write_returns_the_edit_for_recovery(h):
    w = h.workspace()
    current = h.shift(w, people=[])['workspace']
    rejected = h.shift(w, people=[], status=409)
    details = rejected['error']['details']
    assert details['recoverable'] and details['current_revision'] == current['revision']
    assert details['submitted_edit']['kind'] == 'shift.create'
    assert details['submitted_edit']['payload']['workspace_id'] == w['id']


def test_rule_metadata_and_unrelated_period_do_not_invalidate_but_active_numbers_do(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    rule = h.policy({'kind': 'max_duties', 'maximum': 3, 'window': 'week'})
    w = h.refresh_draft_baseline(w)
    before = h.snapshot(w)['fingerprint']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, rule['id']); row.name = 'Display label'; row.revision += 1
    assert h.snapshot(w)['fingerprint'] == before
    data = h.snapshot(w)
    source = next(p for p in data['policies'] if p['id'] == rule['id'])
    source['definition']['rule']['maximum'] = 2
    assert manifest(data)['token'] != before
    source['starts_on'] = '2026-12-17'
    without = manifest(data)['token']
    source['definition']['rule']['maximum'] = 1
    assert manifest(data)['token'] == without


def test_submitted_rota_survives_unrelated_wish_but_relevant_change_requires_resubmission(h):
    w = h.shift(h.workspace())['workspace']
    submitted = h.lifecycle(w, 'submit')['workspace']
    wish(h)
    approved = h.lifecycle(submitted, 'approve', 'approver')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).tags = ['New scheduling tag']
    rejected = h.lifecycle(approved, 'publish', status=409)
    assert rejected['error']['code'] in {'STALE_INPUTS', 'STALE_APPROVAL'}


@pytest.mark.postgres
def test_writer_wait_observes_absence_committed_before_lock_acquisition(pg):
    w = pg.shift(pg.workspace(), people=[])['workspace']
    job, source, _ = proposal(pg, w)
    waiting = Event()
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith('SELECT pg_advisory_xact_lock('):
            waiting.set()
    event.listen(pg.app.state.engine, 'before_cursor_execute', observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction(pg.app.state.engine, write=True) as db:
                future = pool.submit(apply, pg, w, job)
                assert waiting.wait(600)
                shift = source['shifts'][0]
                from packages.domain.time import aware
                db.add(m.Availability(person_id=pg.seed['people'][0], department_id=pg.seed['department_id'],
                    kind='recorded_leave', recorded_by=pg.seed['identities']['coordinator'],
                    start=aware(shift['start']), end=aware(shift['end']), private_reason='Private'))
            reply = future.result(timeout=600)
    finally:
        event.remove(pg.app.state.engine, 'before_cursor_execute', observe)
    assert reply['reconciliation']['status'] == 'revalidation_required'
    assert not h_assignments(pg, w)


def test_manifest_column_contract_requires_classifying_new_inputs():
    from sqlalchemy import inspect
    from packages.domain.input_manifest import FIELDS, SHIFT_FIELDS, PARTICIPANT_FIELDS
    models = {'people': m.Person, 'memberships': m.Membership, 'availability': m.Availability,
              'shifts': m.Shift, 'participants': m.Assignment}
    metadata = {
        'people': {'colour', 'created_at', 'directory_order', 'directory_section', 'display_name', 'organisation_id',
                   'family_name', 'given_name', 'pager', 'revision', 'rota_aliases', 'rota_name', 'short_code', 'staff_number', 'title',
                   'work_email', 'work_extension', 'work_phone'},
        # withdrawn rows are excluded by the live-read filter (db.py), so the flag itself is never a job input
        'memberships': {'created_at', 'revision', 'withdrawn'},
        'availability': {'created_at', 'department_id', 'leave_type', 'leave_type_id', 'manual_type_override',
                         'private_reason', 'recorded_by', 'request_id', 'revision'},
        'shifts': {'cancellation_reason', 'created_at', 'type_snapshot'},
        'participants': {'created_at', 'override_reason', 'revision', 'shift_id', 'source', 'source_actor_id', 'source_at', 'source_run_id'}}
    for kind, model in models.items():
        fields = SHIFT_FIELDS if kind == 'shifts' else PARTICIPANT_FIELDS if kind == 'participants' else FIELDS[kind]
        assert set(inspect(model).columns.keys()) - set(fields) == metadata[kind]


def test_manifest_covers_external_work_classification_used_by_rest(h):
    from packages.domain.validation import index_person_rows
    w = h.shift(h.workspace())['workspace']
    h.policy({'kind': 'min_rest', 'hours': 11})
    data = h.snapshot(w)
    row = {'id': 'external', 'person_id': h.seed['people'][0], 'kind': 'off_duty',
           'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-04T07:00:00+03:00',
           'counts_as_unavailable': True}
    data['availability'].append(row)
    data['_person_rows'] = index_person_rows(data)
    before = manifest(data)['token']
    checked = evaluate(data)
    row['kind'] = 'external_commitment'
    data['_person_rows'] = index_person_rows(data)
    assert manifest(data)['token'] != before
    assert evaluate(data)['error_count'] > checked['error_count']



def test_runtime_options_and_validator_version_follow_canonical_owners(h, monkeypatch):
    from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS, GenerationOptions
    from packages.domain import validation
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    before = manifest(data)['token']
    runtime = {key: GenerationOptions().model_dump()[key] for key in GENERATION_RUNTIME_OPTIONS}
    runtime.update({'search_minutes': 2, 'max_search_minutes': 120, 'stop_without_improvement_seconds': 30,
                    'fairness_plateau_seconds': 45})
    assert runtime.keys() == GENERATION_RUNTIME_OPTIONS
    data['generation_options'] = {**data['generation_options'], **runtime}
    assert manifest(data)['token'] == before
    monkeypatch.setattr(validation, 'ENGINE_VERSION', validation.ENGINE_VERSION + '-next')
    assert manifest(data)['token'] != before


def test_feasibility_receipt_survives_objective_wish_and_stales_on_mandatory_absence(h):
    from apps.worker.main import process_one
    from tests.test_contextual_feasibility import activate
    w = h.shift(h.workspace())['workspace']
    rule = h.command('coordinator', 'policy.create', {
        'name': 'Reviewed rest', 'owner': 'Department', 'source': 'Synthetic contextual review',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'feasibility',
        'check_seconds': 60, 'rule_changes': [{'policy_id': rule['id'], 'expected_revision': rule['revision']}]},
        revision=w['revision'])['job']
    process_one(h.app.state.engine)
    assert not h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale']
    wish(h, '2026-10-04')
    assert not h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale']
    # A rolled-back independent activation proves the exact receipt is consumable without changing the fixture.
    client = h.client('approver')
    from apps.api.auth import authenticate
    from apps.api.services.policies import activate_policies
    from packages.contracts.schemas import Command
    class Rollback(Exception):
        pass
    with pytest.raises(Rollback):
        with transaction(h.app.state.engine, write=True) as db:
            actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
            result = activate_policies(db, actor, [Command(kind='policy.activate', target_id=rule['id'],
                expected_revision=rule['revision'], payload={'reason': 'Independent review', 'check_job_ids': [job['id']]})],
                require_checks=True)
            assert result['records'][0]['status'] == 'active'
            raise Rollback()
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'recorded_leave', 'private_reason': 'Private',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'})
    assert h.get('approver', '/api/jobs/' + job['id'])['job']['check_stale']
    from apps.api.errors import Problem
    with pytest.raises(Problem) as rejected:
        activate(h, rule, job['id'])
    assert rejected.value.code == 'RULE_CHECK_REQUIRED'


def test_partial_interval_subset_never_inherits_native_retention_optimality(h):
    from packages.domain.proposal_reconciliation import reconcile
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    pid = h.seed['people'][0]
    _job, frozen, result = proposal(h, w, [pid, pid])
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, {'person_id': pid})
    result['additions'][0]['participant']['end'] = '2026-10-04T10:00:00+03:00'
    plan = reconcile(h.snapshot(w), frozen, result, [0, 1], [])
    assert not plan['retention_optimal']
    assert comparison(evaluate(h.snapshot(w)), plan['validation'])['no_new_problems']



def test_display_only_history_cannot_claim_an_earlier_semantic_edit(h):
    from packages.contracts.schemas import PersonInput
    w = h.shift(h.workspace(), people=[])['workspace']
    job, _source, _result = proposal(h, w)
    person = next(row for row in h.get('admin', '/api/catalogues')['people'] if row['id'] == h.seed['people'][0])
    person = h.configure('people', {**{key: person[key] for key in PersonInput.model_fields if key in person}, 'tags': ['Changed fact']},
        person['id'], person['revision'])['record']
    h.configure('people', {**{key: person[key] for key in PersonInput.model_fields if key in person}, 'pager': '4000'},
        person['id'], person['revision'], role='coordinator')
    facts = h.get('coordinator', '/api/jobs/' + job['id'] + '/input-changes')['changes']
    change = next(row for row in facts if row['kind'] == 'people' and row['id'] == person['id'])
    assert change['actor']['id'] == h.seed['identities']['admin']
    assert change['actor']['id'] != h.seed['identities']['coordinator']


def test_scheduling_defaults_name_their_audited_editor_without_claiming_runtime_edits(h):
    from .test_staff_wishes import department_settings
    w = h.shift(h.workspace(), people=[])['workspace']
    job, _, _ = proposal(h, w)
    before_snapshot = h.snapshot(w)
    before = before_snapshot['fingerprint']
    options = {'scoring': {'weights': {'fairness': 35}}}
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': options},
        h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    frozen = h.snapshot(w)
    assert frozen['fingerprint'] == before
    assert frozen['generation_options'] == before_snapshot['generation_options']
    w = h.refresh_draft_baseline(w)
    changed_snapshot = h.snapshot(w)
    assert changed_snapshot['fingerprint'] != before
    assert changed_snapshot['input_manifest']['components']['objective_inputs'] != before_snapshot['input_manifest']['components']['objective_inputs']
    assert changed_snapshot['input_manifest']['components']['mandatory_inputs'] == before_snapshot['input_manifest']['components']['mandatory_inputs']
    assert changed_snapshot['generation_options']['scoring']['weights']['fairness'] == options['scoring']['weights']['fairness']
    # Execution-time controls are audited but do not alter the scheduling-input projection.
    h.command('admin', 'department.settings', {**department_settings(h),
        'generation_defaults': {**options, 'search_minutes': 2}}, h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'])
    w = h.refresh_draft_baseline(w)
    runtime_snapshot = h.snapshot(w)
    assert runtime_snapshot['generation_options']['search_minutes'] == 2
    # Refresh advances the draft content revision; compare the semantic input components instead of
    # treating that audit revision as a scheduling-input change.
    for component in ('mandatory_inputs', 'objective_inputs'):
        assert runtime_snapshot['input_manifest']['components'][component] == changed_snapshot['input_manifest']['components'][component]
    with transaction(h.app.state.engine) as db:
        latest = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'department.settings')
                           .order_by(m.AuditEvent.seq.desc()).limit(1))
        assert not any(ref['kind'] == 'generation_options' for ref in latest.detail['input_changes'])
    facts = h.get('coordinator', '/api/jobs/' + job['id'] + '/input-changes')['changes']
    change = next(row for row in facts if row['kind'] == 'generation_options')
    assert change['source_id'] == h.seed['department_id']
    assert change['actor']['id'] == h.seed['identities']['admin']
    assert change['after']['scoring']['weights']['fairness'] == 35


@pytest.mark.parametrize('change', ['inputs', 'compiler', 'inputs_and_compiler'])
def test_only_changed_inputs_are_offered_a_recheck_a_changed_scheduler_is_refused(budget_h, change):
    """maintenance: Apply re-checks a proposal whose inputs changed, but refuses one whose scheduler changed (STALE_SCHEDULER),
    so only the first reads revalidation_required (the page's "Check again and apply"). CP-SAT jobs carry the compiler."""
    h = budget_h
    w = h.shift(h.workspace(), people=[])['workspace']
    job, _, _ = proposal(h, w)
    if change in {'inputs', 'inputs_and_compiler'}:
        wish(h, '2026-10-04')
    if change in {'compiler', 'inputs_and_compiler'}:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Job, job['id'])
            row.input_snapshot = {**row.input_snapshot, 'compiler_version': 'old-compiler'}
    read = h.get('coordinator', f"/api/jobs/{job['id']}")['job']
    assert read['result_stale'] is True
    listed = next(j for j in h.get('coordinator', f"/api/jobs?workspace_id={w['id']}")['jobs'] if j['id'] == job['id'])
    if change == 'inputs':
        assert read['currency'] == 'revalidation_required' and listed['currency'] != 'current'
    else:
        assert read['currency'] == 'unknown' and listed['currency'] != 'revalidation_required'
        assert apply(h, w, job, status=409)['error']['code'] == 'STALE_SCHEDULER'
