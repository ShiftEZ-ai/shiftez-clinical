"""Shared rota integration: atomic intent, current authority, canonical checks and witness scope."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.main import create_app
from apps.api.services import reviews
from packages.contracts import schemas as s
from packages.domain.rota_summary import planning_projection, rota_summary, fix_list_context
from packages.domain.validation import evaluate
from packages.domain.workspace_merge import plan
from packages.domain.time import aware
from .governance_support import identity
from .test_planning_groups_drafts import group, settings, integrated, edit, roles, REASON


def fork(h, workspace, role='coordinator'):
    return h.command(role, 'workspace.create', {'name': 'Private merge experiment',
        'starts_on': workspace['starts_on'], 'ends_on': workspace['ends_on'], 'purpose': 'sandbox',
        'visibility': 'private', 'fork_from': {'workspace_id': workspace['id'], 'revision': workspace['revision']}})['workspace']


def preview(h, source, target, role='coordinator', status=200):
    return h.command(role, 'workspace.merge.preview', {'source_workspace_id': source['id'], 'reason': REASON, 'background': False},
        target['id'], target['revision'], status=status)


def apply(h, target, proposal, status=200, role='coordinator', **options):
    return h.command(role, 'workspace.merge.apply', {'merge_id': proposal['merge']['id'],
        'reason': REASON, 'background': False, **options}, target['id'], target['revision'], status=status)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_merge_private_preview_is_owner_only_across_role_names(h, role):
    roles(h)
    workspace, _, _ = integrated(h)
    source = fork(h, workspace)
    own = h.detail(source)['shifts'][0]
    source = edit(h, source, own, [{'person_id': h.seed['people'][0], 'start': own['start'], 'end': own['end']}])['workspace']
    status = 200 if role == 'coordinator' else 404 if role == 'admin' else 403
    preview(h, source, workspace, role=role, status=status)
    proposal = preview(h, source, workspace)
    apply(h, workspace, proposal, role=role, status=status)


def test_configured_coordinator_merges_unowned_people_but_not_another_groups_changes(h):
    workspace, _, _ = integrated(h)
    source = fork(h, workspace, role='admin')
    local = h.get('admin', '/api/workspaces/' + source['id'])['shifts'][0]
    source = edit(h, source, local, [{'person_id': pid, 'start': local['start'], 'end': local['end']}
        for pid in h.seed['people'][:2]], role='admin')['workspace']
    proposal = preview(h, source, workspace, role='admin')
    assert {b['person_id']: b['kind'] for b in proposal['merge']['bundles']} == {
        h.seed['people'][0]: 'out_of_scope', h.seed['people'][1]: 'fits'}
    merged = apply(h, workspace, proposal, role='admin')['workspace']
    assert [p['person_id'] for p in h.detail(merged)['shifts'][0]['participants']] == [h.seed['people'][1]]


def test_merge_display_facts_copy_time_and_explicit_skipped_choices(h):
    workspace, _, _ = integrated(h)
    source = fork(h, workspace)
    local = h.detail(source)['shifts'][0]
    source = edit(h, source, local, [{'person_id':pid, 'start':local['start'], 'end':local['end']}
        for pid in h.seed['people'][:2]])['workspace']
    proposal = preview(h, source, workspace)
    assert proposal['merge']['copied_at']
    bundles = proposal['merge']['bundles']
    assert all(b['facts']['sentence'] and b['facts']['detail'] for b in bundles)
    skipped = next(b for b in bundles if b['kind'] == 'out_of_scope')
    merged = apply(h,workspace,proposal,choices={skipped['id']:'not_merged'})['workspace']
    assert {p['person_id'] for p in h.detail(merged)['shifts'][0]['participants']} == {h.seed['people'][0]}


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_group_names_latest_slice_change_active_run_and_canonical_fill(h):
    from sqlalchemy import event
    from apps.api.services.planning_groups import last_group_changes
    workspace, shift, first = integrated(h)
    second = group(h,people=[h.seed['people'][1]],owner='admin')
    setting = h.get('admin','/api/planning-settings')['settings']
    h.command('admin','planning.settings',{'definition':{**setting['definition'],'group_lookahead':'none'},'reason':REASON},
        revision=setting['revision'])
    workspace = edit(h,workspace,shift,[{'person_id':h.seed['people'][0], 'start':shift['start'], 'end':shift['end']}])['workspace']
    detail = h.detail(workspace)
    first_change = next(g for g in detail['planning_context']['groups'] if g['group_id']==first['group_id'])['last_change']
    shift = detail['shifts'][0]
    workspace = edit(h,workspace,shift,[*shift['participants'],
        {'person_id':h.seed['people'][1], 'start':shift['start'], 'end':shift['end']}],role='admin')['workspace']
    job = h.command('coordinator','job.create',{'workspace_id':workspace['id'],'kind':'generation',
        'planning_group_version_id':first['id']},revision=workspace['revision'])['job']
    context = h.get('coordinator','/api/workspaces/'+workspace['id']+'/summary')['fix_list']['planning_context']
    groups = {g['group_id']:g for g in context['groups']}
    assert context['group_lookahead']=='none'
    assert groups[first['group_id']]['last_change']==first_change and first_change['by_name']
    with transaction(h.app.state.engine) as db:
        assert groups[second['group_id']]['last_change']['by_name']==db.get(m.Identity,h.seed['identities']['admin']).display_name
        statements = []
        def capture(*args): statements.append(args[2])
        event.listen(h.app.state.engine,'before_cursor_execute',capture)
        try:
            assert set(last_group_changes(db,db.get(m.Workspace,workspace['id'])))=={first['group_id'],second['group_id']}
        finally:
            event.remove(h.app.state.engine,'before_cursor_execute',capture)
        assert len([q for q in statements if 'change_sets' in q])==1
    assert groups[first['group_id']]['active_run'] and not groups[second['group_id']]['active_run']
    assert all(g['owner_name'] and g['owner_names'] and g['filled']==g['total']==1 for g in groups.values())
    listed = h.get('coordinator','/api/planning-groups')['groups']
    assert all(g['owner_name'] and g['owner_names'] for g in listed)
    h.command('coordinator','job.cancel',{'reason':REASON},job['id'],job['revision'])
    assert not any(g['active_run'] for g in h.detail(workspace)['planning_context']['groups'])


def test_group_generation_non_editor_is_refused_before_queueing(h):
    workspace, _, first = integrated(h)
    other = identity(h, 'other_scheduler', ['coordinator'])
    with transaction(h.app.state.engine) as db:
        jobs_before = db.scalar(select(func.count(m.Job.id)).where(m.Job.workspace_id == workspace['id']))
        audits_before = db.scalar(select(func.count(m.AuditEvent.id)).where(m.AuditEvent.action == 'job.create'))
    refused = h.command('other_scheduler', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': first['id']}, revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count(m.Job.id)).where(m.Job.workspace_id == workspace['id'])) == jobs_before
        assert db.scalar(select(func.count(m.AuditEvent.id)).where(m.AuditEvent.action == 'job.create')) == audits_before
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'], m.Job.requested_by == other,
            m.Job.kind == 'generation'))


def test_deferred_group_worker_rechecks_changed_authority(h):
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, _, first = integrated(h)
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': first['id']}, revision=workspace['revision'])['job']
    h.command('admin', 'planning_group.update', {'definition': {**first['definition'],
        'owner_identity_ids': [h.seed['identities']['admin']]}, 'reason': REASON}, first['group_id'], first['revision'])
    from apps.worker.main import process_one
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'failed' and row.error_code == 'GROUP_VERSION_SCOPE'


def test_group_owner_names_follow_each_history_version(h):
    first = group(h)
    changed = h.command('admin','planning_group.update',{'definition':{**first['definition'],
        'owner_identity_ids':[h.seed['identities']['admin']]},'reason':REASON},first['group_id'],first['revision'])['group']
    rows = h.get('coordinator','/api/planning-groups?group_id='+first['group_id'])['groups']
    assert [row['id'] for row in rows]==[changed['id'],first['id']]
    assert rows[0]['owner_name'] and rows[1]['owner_name'] and rows[0]['owner_name']!=rows[1]['owner_name']


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_private_forks_never_enter_competing_drafts_even_for_their_owner(h,role):
    roles(h)
    workspace, _, _ = integrated(h)
    source = fork(h,workspace)
    readable = role in {'admin','coordinator','approver','programme_lead','auditor'}
    overlaps = h.get(role,'/api/schedule/overlaps?workspace_id='+workspace['id'],status=200 if readable else 403)
    if not readable:
        return
    assert not overlaps['drafts']
    rows = h.get(role,'/api/workspaces')['workspaces']
    assert not next(w for w in rows if w['id']==workspace['id'])['overlap']['drafts']
    h.command('coordinator','workspace.share',{'reason':REASON},source['id'],source['revision'])
    assert [w['id'] for w in h.get(role,'/api/schedule/overlaps?workspace_id='+workspace['id'])['drafts']]==[source['id']]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_fork_lineage_merge_preserves_other_population_and_one_undo(h):
    own, outsider = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name':'Two independent cover places','starts_on':'2020-01-01',
        'definition':{'requirements':[{'minimum':1,'maximum':2}]}})['record']['id']
    workspace, shift, _ = integrated(h, typ=typ)
    source = fork(h, workspace)
    local = h.detail(source)['shifts'][0]
    assert local['lineage_id'] == shift['id']
    source = edit(h, source, local, [{'person_id': own, 'start': local['start'], 'end': local['end']}])['workspace']
    # Parent changes after the fork: an unchanged stale outsider row may never overwrite it.
    workspace = edit(h, workspace, shift, [{'person_id': outsider, 'start': shift['start'], 'end': shift['end']}], role='admin')['workspace']
    outsider_before = h.detail(workspace)['shifts'][0]['participants'][0]
    proposal = preview(h, source, workspace)
    assert [b['kind'] for b in proposal['merge']['bundles']] == ['fits']
    reply = apply(h, workspace, proposal)
    merged = h.detail(reply['workspace'])
    assert {p['person_id'] for p in merged['shifts'][0]['participants']} == {own, outsider}
    assert next(p for p in merged['shifts'][0]['participants'] if p['person_id'] == outsider) == outsider_before
    shared = h.get('admin', '/api/workspaces/' + source['id'])['workspace']
    assert shared['visibility'] == 'shared' and shared['archived_at']
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'],
            m.ChangeSet.resulting_content_revision == reply['workspace']['content_revision'])))) == 1
    undone = h.command('coordinator', 'workspace.undo', {'reason': REASON}, workspace['id'], reply['workspace']['revision'])
    assert h.detail(undone['workspace'])['shifts'][0]['participants'] == [outsider_before]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_three_way_same_person_conflict_and_stale_preview_are_fail_closed(h):
    workspace, shift, _ = integrated(h)
    source = fork(h, workspace)
    local = h.detail(source)['shifts'][0]
    person = {'person_id': h.seed['people'][0], 'start': local['start'], 'end': local['end']}
    source = edit(h, source, local, [person])['workspace']
    workspace = edit(h, workspace, shift, [{**person, 'team_lead': True}], role='admin')['workspace']
    proposal = preview(h, source, workspace)
    bundle = proposal['merge']['bundles'][0]
    assert bundle['kind'] == 'both_changed'
    assert apply(h, workspace, proposal, bundle_ids=[bundle['id']], status=422)['error']['code'] == 'MERGE_CHOICE'
    # Scope changes invalidate the preview even without changing either rota revision.
    row = group(h, people=[h.seed['people'][1]], primary_priority=20)
    denied = apply(h, workspace, proposal, bundle_ids=[bundle['id']], choices={bundle['id']: 'yours'}, status=409)
    assert denied['error']['code'] == 'MERGE_STALE'
    assert h.detail(workspace)['shifts'][0]['participants'][0]['team_lead']


def test_merge_excludes_outgroup_changes_and_rechecks_new_absence(h):
    own, outsider = h.seed['people'][:2]
    workspace, shift, _ = integrated(h)
    source = fork(h, workspace)
    local = h.detail(source)['shifts'][0]
    source = edit(h, source, local, [{'person_id': p, 'start': local['start'], 'end': local['end']} for p in (own, outsider)])['workspace']
    proposal = preview(h, source, workspace)
    kinds = {b.get('person_id'): b['kind'] for b in proposal['merge']['bundles']}
    assert kinds == {own: 'fits', outsider: 'out_of_scope'}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Availability(department_id=h.seed['department_id'], person_id=own, start=datetime.fromisoformat(local['start']),
            end=datetime.fromisoformat(local['end']), kind='recorded_leave',
            counts_as_unavailable=True, private_reason='Synthetic absence', recorded_by=h.seed['identities']['coordinator']))
    assert apply(h, workspace, proposal, status=409)['error']['code'] == 'MERGE_STALE'
    again = preview(h, source, workspace)
    assert not again['merge']['can_apply']
    assert apply(h, workspace, again, status=409)['error']['code'] == 'MERGE_CONFLICT'
    assert not h.detail(workspace)['shifts'][0]['participants']


def test_private_experiment_does_not_make_creator_a_shared_contributor(h):
    workspace, _, _ = integrated(h)
    identity(h, 'sandbox_only', ['coordinator', 'approver'])
    h.command('sandbox_only', 'workspace.create', {'name': 'Never merged', 'starts_on': workspace['starts_on'],
        'ends_on': workspace['ends_on'], 'visibility': 'private', 'purpose': 'sandbox',
        'fork_from': {'workspace_id': workspace['id'], 'revision': workspace['revision']}})
    with transaction(h.app.state.engine) as db:
        assert not reviews.contributed(db, db.get(m.Workspace, workspace['id']), h.seed['identities']['sandbox_only'])
        with pytest.raises(DBAPIError):
            db.execute(text('UPDATE workspace_fork_bases SET reason = :reason'), {'reason': 'Forbidden rewrite'})


def test_group_projection_is_one_partition_and_coupling_blocks_both(h):
    a, b = h.seed['people'][:2]
    workspace, shift, first = integrated(h, people=[a, {'person_id': b, 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'supervisor_person_id': a}])
    second = group(h, people=[b], owner='admin', name='Other configurable population')
    data = h.snapshot(workspace)
    check = evaluate(data); check['fix_list_context'] = fix_list_context(data)
    canonical = rota_summary(check, data['shifts'])['fix_list']
    synthetic = {'id': 'coupled', 'level': 'must', 'person_ids': [a,b], 'shift_ids': [shift['id']],
        'evidence': [], 'acknowledgeable': False, 'acknowledgement': None}
    projection = planning_projection(data, [first, second], {**canonical, 'causes': [synthetic]},
        identity_id=h.seed['identities']['coordinator'])
    assert projection['summary'] == canonical['summary']
    assert projection['planning_context']['counts'] == {'mine': 0, 'coupling': 1, 'other_pending': 0, 'unassigned': 0}
    assert all(g['must'] == 1 and not g['ready'] for g in projection['planning_context']['groups'])
    assert projection['planning_context']['held']['session_ids'] == [shift['id']]
    payload = h.get('approver', '/api/workspaces/' + workspace['id'] + '/summary')['fix_list']
    assert sum(payload['planning_context']['counts'].values()) == len(payload['causes'])


def test_group_job_scope_default_lookahead_and_actual_witness_projection(h):
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    h.policy({'kind':'block_quota', 'metric':'duties', 'comparison':'minimum', 'amount':1,
        'starts_on':'2026-10-04', 'ends_on':'2026-10-11'}, {'person_id':h.seed['people'][0]})
    workspace, _, first = integrated(h)
    group(h, people=[h.seed['people'][1]], owner='admin')
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': first['id']}, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine) as db:
        data = deepcopy(db.get(m.Job, job['id']).input_snapshot)
    assert data['planning_group_id'] == first['group_id']
    assert data['planning_group_version_id'] == first['id']
    assert data['lookahead'] == 'coupled'
    from apps.worker.main import process_one
    started = perf_counter()
    assert process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    print({'group_solve_seconds': perf_counter() - started, 'completion': result['completion']['status']})
    assert result['completion']['status'] in {'OPTIMAL', 'FEASIBLE'}
    assert all(a['participant']['person_id'] == h.seed['people'][0] for a in result['additions'])
    assert all(r['person_id'] == h.seed['people'][0] for r in result['removals'])
    assert result['optimality_proven'] is False  # joint optimum is never an actual-only optimum
    with transaction(h.app.state.engine) as db:
        prepared = db.get(m.Job, job['id']).input_snapshot
    assert result['planning_population'] == prepared['planning_population']
    for view in (h.get('coordinator', '/api/jobs/' + job['id'] + '/status')['job'],
                 next(j for j in h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs'] if j['id'] == job['id'])):
        assert view['completion'] == result['completion'] and view['planning_group_version_id'] == first['id']
    with transaction(h.app.state.engine) as db:
        progress = db.get(m.Job, job['id']).progress
        assert not progress.get('events') and not progress.get('solution') and progress.get('best_score') is None
    applied = h.command('coordinator', 'generation.apply', {'workspace_id':workspace['id'], 'job_id':job['id'],
        'reason':REASON}, revision=workspace['revision'])['workspace']
    assert {p['person_id'] for row in h.detail(applied)['shifts'] for p in row['participants']} == {h.seed['people'][0]}


def test_group_owner_submission_and_unowned_coordinator_are_enforced(h):
    workspace, shift, _ = integrated(h, people=[h.seed['people'][0]])
    identity(h, 'other_scheduler', ['coordinator'])
    denied = h.command('other_scheduler', 'workspace.submit', {'reason': REASON}, workspace['id'], workspace['revision'], status=403)
    assert denied['error']['code'] == 'GROUP_SUBMIT_AUTHORITY'
    submitted = h.lifecycle(workspace, 'submit')['workspace']
    assert submitted['status'] == 'submitted'


def test_compact_empty_rejection_list_approves_every_review_item(h):
    from .test_be5_governance import binding_fixture
    from .test_be5_rota_adjustments import scoped_payload
    binding, policy, workspace = binding_fixture(h)
    group(h)
    settings(h)
    workspace = h.command('admin', 'workspace.designate', {'reason':REASON}, workspace['id'], workspace['revision'])['workspace']
    workspace = h.command('coordinator', 'workspace.rule_adjust', scoped_payload(policy,binding,h.seed['people'][0]),
        workspace['id'], workspace['revision'])['workspace']
    submitted = h.lifecycle(workspace,'submit')['workspace']
    approved = h.command('approver','workspace.approve',{'reason':REASON,'rejected_review_item_ids':[]},
        submitted['id'], submitted['revision'])['workspace']
    assert approved['status'] == 'approved'
    detail = h.get('approver', '/api/workspaces/' + approved['id'])
    assert all(row['accept'] for row in detail['workspace']['validation']['review_item_decisions'])
    with pytest.raises(ValueError, match='Choose complete decisions'):
        s.ReviewDecisionInput.model_validate({'reason':REASON,'rejected_review_item_ids':[],
            'decisions':[{'item_id':str(uuid4()),'kind':'adjustment','accept':True}]})


def test_coupled_progress_never_labels_witness_roster_or_score_as_actual():
    from packages.domain.group_generation import witness_progress
    updates = []
    witness_progress(updates.append)({'stage':'ready','elapsed_seconds':3,'events':[{'adds':[{'person_id':str(uuid4())}]}],
        'solution':{'pairs':[]}, 'best_score':{'total':100}, 'shared_validation_passed':True})
    assert updates == [{'stage':'validating','elapsed_seconds':3,'proof_scope':'coupled_completion'}]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_scoped_adjustment_merge_preserves_justification_and_rejection_returns_whole_rota(h):
    from .test_be5_governance import binding_fixture
    from .test_be5_rota_adjustments import scoped_payload
    binding, policy, workspace = binding_fixture(h)
    group(h)
    settings(h)
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, workspace['id'], workspace['revision'])['workspace']
    source = fork(h, workspace)
    source = h.command('coordinator', 'workspace.rule_adjust', scoped_payload(policy, binding, h.seed['people'][0]),
        source['id'], source['revision'])['workspace']
    original = source['rule_adjustments'][0]
    proposal = preview(h, source, workspace)
    assert proposal['merge']['can_apply']
    result = apply(h, workspace, proposal)
    merged = result['workspace']
    assert merged['rule_adjustments'] == [original]
    submitted = h.lifecycle(merged, 'submit')['workspace']
    detail = h.get('approver', '/api/workspaces/' + workspace['id'])
    item = next(i for i in detail['review_items'] if i['kind'] == 'rule_adjustment')
    assert item['reason'] == original['reason'] and item['author_identity_id'] == original['by']
    assert item['group_ids'] and item['author_name']
    returned = h.command('approver', 'workspace.approve', {'reason': 'Return the entire rota for a workload change',
        'rejected_review_item_ids': [item['id']]}, workspace['id'], submitted['revision'])['workspace']
    assert returned['status'] == 'draft' and returned['review_snapshot'] is None
    assert returned['submitted_fingerprint'] is None and not returned['rule_adjustments']
    detail = h.get('approver', '/api/workspaces/' + workspace['id'])
    assert not detail['reviewer_involved']
    repaired = h.command('coordinator', 'workspace.rule_adjust', scoped_payload(policy,binding,h.seed['people'][0]),
        returned['id'],returned['revision'])['workspace']
    submitted = h.lifecycle(repaired,'submit')['workspace']
    assert h.get('approver','/api/workspaces/'+workspace['id'])['review_progress']['can_decide']
    assert h.command('approver','workspace.approve',{'reason':REASON,'rejected_review_item_ids':[]},
        workspace['id'],submitted['revision'])['workspace']['status']=='approved'


def test_merge_person_move_is_one_bundle_and_safety_failure_cannot_offset_improvement():
    def row(sid, people):
        return {'id': sid, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
            'participants': people, 'type_snapshot': {}, 'service_ids': []}
    pid, one, two = str(uuid4()), str(uuid4()), str(uuid4())
    p = {'person_id': pid, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}
    base = {'workspace': {}, 'shifts': [row(one,[p]), row(two,[])]}
    source = {'workspace': {}, 'shifts': [row(one,[]), row(two,[p])]}
    bundles = plan(str(uuid4()), base, base, source, base, {one:one,two:two},
        assignment_allowed=lambda *args: True, adjustment_allowed=lambda *args: True)
    assert len(bundles) == 1 and len(bundles[0]['changes']) == 2


def test_group_joint_status_does_not_invent_feasibility_on_unknown_or_infeasible(h):
    from packages.domain.group_generation import generate as joint
    workspace, _, first = integrated(h)
    data = h.snapshot(workspace)
    data.update(planning_group_id=first['group_id'], planning_group_version_id=first['id'],
        planning_population={data['shifts'][0]['id']: [h.seed['people'][0]]},
        generation_scope={'conditions': first['definition']['membership'], 'protection': 'keep_existing'})
    for status, expected in (('requirements_conflict', 'INFEASIBLE'), ('needs_more_time','UNKNOWN')):
        result = joint(data, lambda _: {'status': status, 'additions': [], 'removals': [], 'optimality_proven': False,
            'validation': {'valid': True}, 'summary': {'filled_places': 999}})
        assert result['completion']['status'] == expected and not result['additions']
        assert result['validation']['valid'] is False and result['summary']['filled_places'] == 0


@pytest.mark.parametrize('protection', ['fixed', 'locked', 'supervisor'])
@pytest.mark.parametrize('protected_first', [True, False])
def test_atomic_merge_protection_survives_other_session_definition_conflicts(protection, protected_first):
    pid, dependent = str(uuid4()), str(uuid4())
    person = {'person_id':pid, 'start':'2026-10-04T08:00:00+03:00', 'end':'2026-10-04T12:00:00+03:00'}
    protected, other = ('a', 'b') if protected_first else ('b', 'a')
    base = {'workspace':{}, 'shifts':[
        {'id':sid, 'type_revision':1, 'participants':[dict(person)]} for sid in (protected, other)]}
    if protection == 'fixed':
        base['shifts'][0]['participants'][0]['fixed'] = True
    elif protection == 'locked':
        base['shifts'][0]['locked'] = True
    else:
        base['shifts'][0]['participants'].append({**person, 'person_id':dependent, 'supervisor_person_id':pid})
    source, target = deepcopy(base), deepcopy(base)
    for row in source['shifts']:
        row['participants'] = [p for p in row['participants'] if p['person_id'] != pid]
    target['shifts'][1]['type_revision'] += 1
    bundles = plan(str(uuid4()),base,base,source,target,{protected:protected,other:other},
        assignment_allowed=lambda *args:True,adjustment_allowed=lambda *args:True)
    assert len(bundles) == 1 and len(bundles[0]['changes']) == 2
    assert bundles[0]['kind'] == 'protected_coupling'
    assert bundles[0]['allowed_choices'] == ['rota', 'not_merged']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_fork_base_guards_preserve_evidence_on_delete_or_invalid_insert(h):
    workspace, _, _ = integrated(h)
    source = fork(h, workspace)
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.WorkspaceForkBase).where(m.WorkspaceForkBase.workspace_id == source['id']))
        values = {col.name: getattr(row, col.name) for col in m.WorkspaceForkBase.__table__.columns}
        before = deepcopy(values)
    with pytest.raises(DBAPIError), transaction(h.app.state.engine, write=True) as db:
        db.execute(text('DELETE FROM workspace_fork_bases WHERE id = :id'), {'id': values['id']})
    with pytest.raises(DBAPIError), transaction(h.app.state.engine, write=True) as db:
        db.execute(m.WorkspaceForkBase.__table__.insert().values(**{**values, 'id': str(uuid4()),
            'workspace_id': workspace['id'], 'revision': 0}))
    with transaction(h.app.state.engine) as db:
        retained = db.get(m.WorkspaceForkBase, values['id'])
        assert {col.name: getattr(retained, col.name) for col in m.WorkspaceForkBase.__table__.columns} == before


def test_imported_assignment_author_alias_is_excluded_from_review(h):
    workspace, _, _ = integrated(h)
    source = fork(h, workspace)
    local = h.detail(source)['shifts'][0]
    source = edit(h, source, local, [{'person_id': h.seed['people'][0], 'start': local['start'], 'end': local['end']}])['workspace']
    # Retained assignment provenance is server-owned; construct a historical author in the fixture.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == source['id']))
        row.source_actor_id = h.seed['identities']['approver']
    merged = apply(h, workspace, preview(h, source, workspace))['workspace']
    submitted = h.lifecycle(merged, 'submit')['workspace']
    assert h.command('approver', 'workspace.approve', {'reason': REASON}, submitted['id'], submitted['revision'],
        status=403)['error']['code'] == 'SELF_APPROVAL'


def test_coupled_generation_never_offers_a_witness_only_supervisor_as_actual(h):
    from .test_generation_population import scope
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    own, outsider = h.seed['people'][:2]
    sid = data['shifts'][0]['id']
    from packages.domain.group_generation import generate as joint
    data.update(planning_group_id=str(uuid4()), planning_group_version_id=str(uuid4()),
        planning_population={sid:[own]}, generation_scope=scope('person_ids',own))
    def witness(_):
        return {'status':'candidate_feasible', 'validation':{'valid':True}, 'optimality_proven':False,
            'generation_outcome': {'status': 'FEASIBLE', 'candidate': 'valid', 'strict_status': None, 'optimality_proven': False},
            'additions':[{'shift_id':sid,'participant':{'person_id':pid, 'start':data['shifts'][0]['start'],
                'end':data['shifts'][0]['end'], **({'supervisor_person_id':outsider} if pid == own else {})}}
                for pid in (own, outsider)], 'removals':[]}
    result = joint(data, witness)
    assert result['completion']['status'] == 'UNKNOWN' and not result['additions']
    assert result['completion']['witness_hash'] is None
    assert result['summary']['filled_places'] == 0
    assert result['generation_outcome'] == {'status': 'UNKNOWN', 'candidate': 'with_gaps',
                                            'strict_status': None, 'optimality_proven': False}


def test_supervisor_partial_move_and_fixed_flag_require_current_authority(h):
    own, trainee = h.seed['people'][1:]
    configured = h.configure('assignment_types', {'name':'Configured training session', 'starts_on':'2020-01-01',
        'definition':{'requirements':[{'minimum':1,'maximum':3}], 'supervision': {
            'trainee_category_ids':[h.seed['trainee_category']], 'capability_id':h.seed['supervisor_capability'],
            'trainees_per_supervisor':1, 'require_pairing':True}}})['record']
    group(h, people=[own])
    settings(h)
    created = h.shift(h.workspace(), typ=configured['id'], role='admin', people=[own, {'person_id':trainee,
        'participation':'trainee', 'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'supervisor_person_id':own}])
    workspace = h.command('admin','workspace.designate',{'reason':REASON},created['workspace']['id'],created['workspace']['revision'])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    shorter = [{**p,'end':'2026-10-04T10:00:00+03:00'} if p['person_id']==own else p for p in shift['participants']]
    assert edit(h,workspace,shift,shorter,status=422)['error']['code']=='FULL_ASSIGNMENT_REQUIRED'
    assert edit(h,workspace,shift,[p for p in shift['participants'] if p['person_id'] != own],
        status=409)['error']['code']=='GROUP_DEPENDENCY_PROTECTED'
    fixed = [{**p,'fixed':True} if p['person_id']==own else p for p in shift['participants']]
    assert edit(h,workspace,shift,fixed,status=403)['error']['code']=='SESSION_DEFINITION_AUTHORITY'


def test_three_way_diff_at_department_scale_is_batched_and_measured(h):
    pid = [str(uuid4()) for _ in range(150)]
    template = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])['shifts'][0]
    shifts = [{**deepcopy(template), 'id':str(uuid4()), 'participants':[]} for _ in range(500)]
    base = {'workspace':{}, 'shifts':shifts}
    source = deepcopy(base)
    for i, row in enumerate(source['shifts']):
        row['participants'] = [{'person_id':pid[i % len(pid)], 'start':row['start'], 'end':row['end']}]
    started = perf_counter()
    bundles = plan(str(uuid4()),base,base,source,base,{row['id']:row['id'] for row in shifts},
        assignment_allowed=lambda *args:True,adjustment_allowed=lambda *args:True)
    assert len(bundles)==150 and sum(len(b['changes']) for b in bundles)==500
    print({'merge_people':150,'merge_sessions':500,'merge_bundles':len(bundles),'seconds':perf_counter()-started})


def test_joint_witness_at_department_scale_is_measured_without_sql(h):
    from sqlalchemy import event
    from packages.domain.cp_sat import generate
    from packages.domain.input_manifest import manifest
    from .test_planning_groups_drafts import tree
    data = h.snapshot(h.shift(h.workspace(),people=[])['workspace'])
    original = h.seed['people'][0]
    people = [str(uuid4()) for _ in range(150)]
    services = [str(uuid4()) for _ in people]
    data['people'] = {pid:{**data['people'][original], 'id':pid} for pid in people}
    for field in ('memberships','grants'):
        prototype = [row for row in data[field] if row['person_id'] == original]
        data[field] = [{**row,'id':str(uuid4()),'person_id':pid} for row in prototype for pid in people]
    service = data['services'][h.seed['services'][0]]
    eligibility = next(row for row in data['eligibilities'] if row['person_id'] == original and row['service_id'] == service['id'])
    data['services'] = {sid:{**service,'id':sid} for sid in services}
    data['eligibilities'] = [{**eligibility,'id':str(uuid4()),'person_id':pid,'service_id':sid} for pid,sid in zip(people,services)]
    template = data['shifts'][0]
    data['locations'][template['location_id']]['capacity'] = len(people)
    data['shifts'] = [{**deepcopy(template),'id':str(uuid4()),'service_ids':[services[i % len(people)]],
        'start':(aware(template['start']) + timedelta(days=i//17)).isoformat(),
        'end':(aware(template['end']) + timedelta(days=i//17)).isoformat()} for i in range(500)]
    data['focus_ids'] = [row['id'] for row in data['shifts']]
    data['workspace']['ends_on'] = (date.fromisoformat(data['workspace']['starts_on']) + timedelta(days=31)).isoformat()
    versions = [{'id':str(uuid4()),'group_id':str(uuid4()),'revision':1,'definition':{
        'name':'Configured scale population','starts_on':'2020-01-01','ends_on':None,'membership':tree(*population),
        'owner_identity_ids':[h.seed['identities']['coordinator']], 'primary_priority':1}}
        for population in (people[:75],people[75:])]
    data.update(planning_group_id=versions[0]['group_id'],planning_group_version_id=versions[0]['id'],
        planning_group_versions=versions,planning_population={sid:people[:75] for sid in data['focus_ids']},
        generation_scope={'conditions':tree(*people[:75]),'protection':'keep_existing'},lookahead='coupled')
    data['input_manifest'] = manifest(data)
    data['fingerprint'] = data['input_manifest']['token']
    statements = []
    def capture(*args):
        statements.append(args[2])
    event.listen(h.app.state.engine,'before_cursor_execute',capture)
    started = perf_counter()
    try:
        result = generate(data,workers=1)
        elapsed = perf_counter()-started
        from packages.domain.proposal_reconciliation import overlay
        actual = overlay(data,result,list(range(len(result['additions']))),list(range(len(result['removals']))))
        canonical = rota_summary(result['validation'],actual['shifts'])['fix_list']
        projection_started = perf_counter()
        projected = planning_projection(actual,versions,canonical,identity_id=h.seed['identities']['coordinator'],
            validation=result['validation'])
        projection_seconds = perf_counter()-projection_started
    finally:
        event.remove(h.app.state.engine,'before_cursor_execute',capture)
    assert result['completion']['status'] in {'OPTIMAL','FEASIBLE'}
    assert len(result['additions']) == 275 and not result['removals'] and not statements
    assert result['open_places_after'] == 225 and result['validation']['valid'] is False
    pools = {g['group_id']:g for g in projected['planning_context']['groups']}
    assert (pools[versions[0]['group_id']]['filled'],pools[versions[0]['group_id']]['total'])==(275,275)
    assert (pools[versions[1]['group_id']]['filled'],pools[versions[1]['group_id']]['total'])==(0,225)
    print({'joint_people':150,'joint_sessions':500,'actual_additions':275,'sql':len(statements),
        'seconds':elapsed,'group_projection_seconds':projection_seconds,'native_phase_timings':result.get('phase_timings')})


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_merge_rechecks_source_changed_while_check_runs_without_writer_lock(h,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from packages.domain import validation
    workspace,_,_ = integrated(h)
    source = fork(h,workspace)
    local = h.detail(source)['shifts'][0]
    source = edit(h,source,local,[{'person_id':h.seed['people'][0], 'start':local['start'],'end':local['end']}])['workspace']
    proposal = preview(h,source,workspace)
    h.client('staff')
    entered,release = Event(),Event()
    evaluate = validation.evaluate
    def paused(data,**options):
        if not entered.is_set():
            entered.set()
            assert release.wait(30), 'Concurrent writer did not finish'
        return evaluate(data,**options)
    monkeypatch.setattr(validation,'evaluate',paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(apply,h,workspace,proposal,status=409)
        try:
            assert entered.wait(30)
            # These complete before validation resumes: no application writer lock is held by the check.
            h.command('staff','table_view.create',{'table_key':'people','name':'During merge check','definition':{}})
            changed = edit(h,source,local,[])['workspace']
            assert changed['revision'] == source['revision'] + 1
            assert not h.detail(changed)['shifts'][0]['participants']
        finally:
            release.set()
        assert pending.result(timeout=30)['error']['code'] in {'MERGE_STALE','STALE_INPUTS'}
    assert not h.detail(workspace)['shifts'][0]['participants']


def test_supervisor_partial_move_requires_group_authority_before_d65(h):
    own, trainee = h.seed['people'][1:]
    configured = h.configure('assignment_types', {'name':'Configured training session', 'starts_on':'2020-01-01',
        'definition':{'requirements':[{'minimum':1,'maximum':3}], 'supervision': {
            'trainee_category_ids':[h.seed['trainee_category']], 'capability_id':h.seed['supervisor_capability'],
            'trainees_per_supervisor':1, 'require_pairing':True}}})['record']
    group(h, people=[own], owner='admin')
    settings(h)
    created = h.shift(h.workspace(), typ=configured['id'], role='admin', people=[own, {'person_id':trainee,
        'participation':'trainee', 'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'supervisor_person_id':own}])
    workspace = h.command('admin','workspace.designate',{'reason':REASON},created['workspace']['id'],created['workspace']['revision'])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    denied = edit(h,workspace,shift,[p for p in shift['participants'] if p['person_id'] != own], status=403)
    assert denied['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert h.detail(workspace)['shifts'][0]['participants'] == shift['participants']


@pytest.mark.parametrize('preview', [False, True])
def test_supervisor_swap_requires_group_authority_before_d65(h, preview):
    own, trainee, outsider = h.seed['people']
    configured = h.configure('assignment_types', {'name':'Configured swap training session', 'starts_on':'2020-01-01',
        'definition':{'requirements':[{'minimum':1,'maximum':3}], 'supervision': {
            'trainee_category_ids':[h.seed['trainee_category']], 'capability_id':h.seed['supervisor_capability'],
            'trainees_per_supervisor':1, 'require_pairing':True}}})['record']
    group(h, people=[own], owner='admin')
    settings(h)
    draft = h.workspace()
    first = h.shift(draft, typ=configured['id'], role='admin', people=[own, {'person_id':trainee,
        'participation':'trainee', 'start':'2026-10-04T08:00:00+03:00','end':'2026-10-04T12:00:00+03:00',
        'supervisor_person_id':own}])
    second = h.shift(first['workspace'], typ=configured['id'], role='admin', people=[outsider])
    workspace = h.command('admin','workspace.designate',{'reason':REASON},second['workspace']['id'],
        second['workspace']['revision'])['workspace']
    detail = h.detail(workspace)
    source = next(row for row in detail['shifts'] if any(p['person_id'] == own for p in row['participants']))
    target = next(row for row in detail['shifts'] if any(p['person_id'] == outsider for p in row['participants']))
    payload = {'workspace_id': workspace['id'], 'first_assignment_id': next(
        p['id'] for p in source['participants'] if p['person_id'] == own),
        'second_assignment_id': next(p['id'] for p in target['participants'] if p['person_id'] == outsider),
        'reason': REASON}
    before = {row['id']: row['participants'] for row in detail['shifts']}
    if preview:
        response = h.client('coordinator').post('/api/planner/preview', json={
            'kind': 'draft.swap', 'payload': payload, 'expected_revision': workspace['revision']})
        assert response.status_code == 403, response.text
        denied = response.json()
    else:
        denied = h.command('coordinator', 'draft.swap', payload, revision=workspace['revision'], status=403)
    assert denied['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert {row['id']: row['participants'] for row in h.detail(workspace)['shifts']} == before


@pytest.mark.parametrize('update', [False, True], ids=['create', 'edit'])
@pytest.mark.parametrize('preview', [False, True], ids=['save', 'preview'])
@pytest.mark.parametrize('role', ['coordinator', 'admin'], ids=['unauthorized', 'owner'])
def test_save_shift_requires_group_authority_before_restricted_person_findings(h, update, preview, role):
    from .test_event_calendar import event_row

    restricted = h.seed['people'][1]
    workspace, shift, _ = integrated(h, people=[], owner='admin')
    event_row(h, settings={'blocks_duties': True}, audience={'person_ids': [restricted]})
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], participants=[{
        'person_id': restricted, 'start': shift['start'], 'end': shift['end']}])
    if update:
        payload['shift_id'] = shift['id']
    command = {'kind': 'shift.update' if update else 'shift.create',
               'expected_revision': workspace['revision'], 'payload': payload}
    original = h.detail(workspace)
    response = h.client(role).post('/api/planner/preview' if preview else '/api/commands',
        json=command, headers={} if preview else {'idempotency-key': str(uuid4())})
    if role == 'admin':
        assert response.status_code == 422, response.text
        assert response.json()['error']['code'] == 'EDIT_INVALID'
        assert 'RESTRICTION' in {f['code'] for f in response.json()['error']['details']['findings']}
    else:
        assert response.status_code == 403, response.text
        assert response.json()['error']['code'] == 'GROUP_EDIT_AUTHORITY'
        assert 'findings' not in response.text and 'people' not in (response.json()['error'].get('details') or {})
        with transaction(h.app.state.engine) as db:
            actor = db.get(m.Identity, h.seed['identities'][role])
            for person in db.scalars(select(m.Person).where(m.Person.id.in_(h.seed['people']))):
                if person.id != actor.person_id:
                    assert person.id not in response.text and person.display_name not in response.text
    assert h.detail(workspace) == original


def test_save_shift_reuses_early_exact_edit_authority_before_person_evaluation(h, monkeypatch):
    from apps.api.services import planning, planning_groups

    own = h.seed['people'][1]
    group(h, people=[own])
    created = h.shift(h.workspace(), people=[], role='admin')
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, created['workspace']['id'],
        created['workspace']['revision'])['workspace']
    calls = []
    guard, check = planning_groups.guard_change_authority, planning.assignment_override_people

    def authority(*args, **kwargs):
        calls.append((args[0], args[3]))
        return guard(*args, **kwargs)

    def people(*args, **kwargs):
        db, workspace = args[:2]
        context = db.info['_planning_group_authority_contexts'][workspace.id]
        assert sum(session is db and before is context[0] for session, before in calls) == 1, (
            'Authority must run before the per-person reason gate')
        return check(*args, **kwargs)

    monkeypatch.setattr(planning_groups, 'guard_change_authority', authority)
    monkeypatch.setattr(planning, 'assignment_override_people', people)
    shift = created['shifts'][0]
    edit(h, workspace, shift, [{'person_id': own, 'start': shift['start'], 'end': shift['end']}])
    assert calls, 'Authority must run before the per-person reason gate'


@pytest.mark.parametrize('preview', [False, True])
def test_move_requires_group_authority_before_baseline_findings(h, monkeypatch, preview):
    from apps.api.services import planning

    own = h.seed['people'][1]
    group(h, people=[own], owner='admin')
    first = h.shift(h.workspace(), people=[own], role='admin')
    second = h.shift(first['workspace'], people=[], role='admin',
        start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, second['workspace']['id'],
        second['workspace']['revision'])['workspace']
    original = h.detail(workspace)

    def forbidden(*args, **kwargs):
        pytest.fail('A move must check authority before evaluating its baseline findings')

    monkeypatch.setattr(planning, 'command_evaluate', forbidden)
    payload = {'workspace_id': workspace['id'], 'assignment_id': first['shifts'][0]['participants'][0]['id'],
               'target_shift_id': second['shift_id'], 'reason': REASON}
    response = h.client('coordinator').post('/api/planner/preview' if preview else '/api/commands',
        json={'kind': 'draft.move', 'expected_revision': workspace['revision'], 'payload': payload},
        headers={} if preview else {'idempotency-key': str(uuid4())})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert 'findings' not in response.text and own not in response.text
    assert h.detail(workspace) == original


def test_cached_proposed_authority_does_not_admit_a_later_different_assignment(h):
    from apps.api.services import planning, planning_groups

    own, outsider = h.seed['people'][1:]
    group(h, people=[own])
    created = h.shift(h.workspace(), people=[], role='admin')
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, created['workspace']['id'],
        created['workspace']['revision'])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, h.client('admin').cookies['rota_session'], h.client('admin').cookies['rota_csrf'], write=True)
        coordinator = authenticate(db, h.client('coordinator').cookies['rota_session'],
            h.client('coordinator').cookies['rota_csrf'], write=True)
        row = db.get(m.Workspace, workspace['id'])
        before = planning.workspace_content(db, row)
        proposed = deepcopy(before)
        shift = proposed['shifts'][0]
        shift['participants'] = [{'person_id': own, 'start': shift['start'], 'end': shift['end']}]
        planning_groups.require_change_authority(db, actor, row, before, after=proposed)
        fields = {key: shift[key] for key in s.ShiftInput.model_fields}
        fields['participants'][0]['person_id'] = outsider
        planning.write_shift(db, actor, row, s.ShiftInput.model_validate(fields), db.get(m.Shift, shift['id']))
        with pytest.raises(Problem) as refused:
            planning.changed(db, coordinator, row, before, REASON)
        assert (refused.value.status, refused.value.code) == (403, 'GROUP_EDIT_AUTHORITY')
        assert row.id not in db.info['_planning_group_authority_contexts']


def test_authority_cache_binds_actor_and_is_discarded_on_preview_rollback(h):
    from apps.api.services import bulk, planning, planning_groups

    own = h.seed['people'][1]
    group(h, people=[own])
    settings(h)
    workspace = h.shift(h.workspace(), role='admin', people=[own])['workspace']
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, workspace['id'], workspace['revision'])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        admin = authenticate(db, h.client('admin').cookies['rota_session'], h.client('admin').cookies['rota_csrf'], write=True)
        coordinator = authenticate(db, h.client('coordinator').cookies['rota_session'],
            h.client('coordinator').cookies['rota_csrf'], write=True)
        row = db.get(m.Workspace, workspace['id'])
        before = planning.workspace_content(db, row)
        planning_groups.require_change_authority(db, admin, row, before)
        assert db.info['_planning_group_authority_contexts'][row.id][1] == admin.identity_id
        row.content_revision += 1
        fresh = deepcopy(before)
        planning_groups.require_change_authority(db, admin, row, fresh)
        assert db.info['_planning_group_authority_contexts'][row.id][0] is fresh
        changed = deepcopy(fresh)
        changed['shifts'][0]['participants'] = []
        planning_groups.require_change_authority(db, admin, row, changed)
        assert db.info['_planning_group_authority_contexts'][row.id][0] is changed
        planning_groups.require_change_authority(db, coordinator, row, before)
        assert db.info['_planning_group_authority_contexts'][row.id][1] == coordinator.identity_id
        with pytest.raises(RuntimeError):
            with bulk._preview_savepoint(db):
                raise RuntimeError('synthetic savepoint rollback')
        assert row.id not in db.info.get('_planning_group_authority_contexts', {})


def test_group_owner_can_release_own_absent_person_from_a_mixed_session(h):
    own, outsider = h.seed['people'][1:]
    group(h, people=[own])
    group(h, people=[outsider], owner='admin')
    settings(h)
    created = h.shift(h.workspace(), role='admin', people=[own, outsider])
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, created['workspace']['id'],
        created['workspace']['revision'])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    h.command('coordinator', 'availability.record', {'person_id': own, 'kind': 'actual_absence',
        'start': shift['start'], 'end': shift['end'], 'private_reason': 'Synthetic absence'})
    result = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'],
        'shift_ids': [shift['id']], 'action': 'release_unavailable', 'reason': REASON},
        revision=workspace['revision'])
    assert [p['person_id'] for p in h.detail(result['workspace'])['shifts'][0]['participants']] == [outsider]


@pytest.mark.parametrize('preview', [False, True])
def test_refused_bulk_session_never_evaluates_or_discloses_rota_findings(h, monkeypatch, preview):
    from apps.api.services import catalogue_preview

    workspace, shift, _ = integrated(h, people=[], owner='admin')
    restricted = h.seed['people'][1]
    original = h.detail(workspace)
    payload = {key: shift[key] for key in s.ShiftInput.model_fields}
    payload.update(workspace_id=workspace['id'], participants=[{
        'person_id': restricted, 'start': shift['start'], 'end': shift['end']}])

    def forbidden(*args, **kwargs):
        pytest.fail('A refused bulk session must not evaluate rota impact findings')

    monkeypatch.setattr(catalogue_preview, 'evaluate_preview', forbidden)
    response = h.client('coordinator').post('/api/commands/preview' if preview else '/api/commands',
        headers={'idempotency-key': str(uuid4())}, json={
        'kind': 'bulk.apply', 'payload': {'reason': REASON, 'items': [{
            'item_kind': 'shift.create', 'target_id': workspace['id'],
            'expected_revision': workspace['revision'], 'payload': payload}]}})
    assert response.status_code == (200 if preview else 403), response.text
    if preview:
        assert response.json()['rows'][0]['issues'][0]['code'] == 'GROUP_EDIT_AUTHORITY'
        assert response.json()['impacts'] == []
    else:
        assert response.json()['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert restricted not in response.text
    assert h.detail(workspace) == original


@pytest.mark.parametrize('mismatch', ['before', 'content_revision'])
def test_authority_context_is_bound_to_the_exact_edit(h, mismatch):
    from apps.api.services import planning

    own = h.seed['people'][1]
    group(h, people=[own], owner='admin')
    settings(h)
    created = h.shift(h.workspace(), role='admin', people=[own])
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, created['workspace']['id'],
        created['workspace']['revision'])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        admin = authenticate(db, h.client('admin').cookies['rota_session'], h.client('admin').cookies['rota_csrf'], write=True)
        coordinator = authenticate(db, h.client('coordinator').cookies['rota_session'],
            h.client('coordinator').cookies['rota_csrf'], write=True)
        row = db.get(m.Workspace, workspace['id'])
        before_a = planning.workspace_content(db, row)
        planning.admit_assignment_change(db, admin, row, before_a, 'EDIT_INVALID')
        before_b = planning.workspace_content(db, row) if mismatch == 'before' else before_a
        if mismatch == 'content_revision':
            row.content_revision += 1
        assignment = db.scalar(select(m.Assignment).where(m.Assignment.shift_id == created['shifts'][0]['id']))
        db.delete(assignment)
        db.flush()
        with pytest.raises(Problem) as refused:
            planning.changed(db, coordinator, row, before_b, REASON)
        assert (refused.value.status, refused.value.code) == (403, 'GROUP_EDIT_AUTHORITY')
        assert row.id not in db.info['_planning_group_authority_contexts']


@pytest.mark.parametrize('failure_stage', ['authority', 'safety'])
def test_refused_admission_clears_previous_authority_context(h, monkeypatch, failure_stage):
    from apps.api.services import planning, planning_groups

    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, h.client('coordinator').cookies['rota_session'],
            h.client('coordinator').cookies['rota_csrf'], write=True)
        row = db.get(m.Workspace, workspace['id'])
        before = planning.workspace_content(db, row)
        planning.admit_assignment_change(db, actor, row, before, 'EDIT_INVALID')
        assert row.id in db.info['_planning_group_authority_contexts']

        def refuse(*args, **kwargs):
            status, code = (403, 'GROUP_EDIT_AUTHORITY') if failure_stage == 'authority' else (422, 'EDIT_INVALID')
            raise Problem(status, code, 'Refused admission')

        monkeypatch.setattr(planning_groups if failure_stage == 'authority' else planning,
            'guard_change_authority' if failure_stage == 'authority' else 'domain_snapshot', refuse)
        if failure_stage == 'authority':
            before = planning.workspace_content(db, row)
        with pytest.raises(Problem):
            planning.admit_assignment_change(db, actor, row, before, 'EDIT_INVALID')
        assert row.id not in db.info['_planning_group_authority_contexts']


@pytest.mark.parametrize('kind', ['import.preview', 'import.reference_preview', 'import.refresh', 'import.apply'])
def test_import_requires_group_authority_before_source_rows(h, kind):
    from .test_operations import mappings

    workspace, _, _ = integrated(h, people=[], owner='admin')
    original = h.detail(workspace)
    source_id = str(uuid4())
    payloads = {
        'import.preview': {'source_name': 'Synthetic authority check.csv', 'csv_text': 'invalid header'},
        'import.reference_preview': {'reference_import_id': source_id},
        'import.refresh': {'batch_id': source_id, 'reason': REASON},
        'import.apply': {'batch_id': source_id, 'mappings': mappings(h, [h.seed['people'][1]]), 'reason': REASON},
    }
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())},
        json={'kind': kind, 'expected_revision': workspace['revision'],
              'payload': {'workspace_id': workspace['id'], **payloads[kind]}})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert 'findings' not in response.text
    assert all(person_id not in response.text for person_id in h.seed['people'])
    assert h.detail(workspace) == original


@pytest.mark.parametrize('action', ['preview', 'replace', 'unassign', 'remove', 'accept'])
def test_resolve_issues_requires_group_authority_before_current_findings(h, monkeypatch, action):
    from apps.api.services import issues
    from .test_event_calendar import event_row

    restricted = h.seed['people'][1]
    workspace, _, _ = integrated(h, people=[restricted], owner='admin')
    event_row(h, settings={'blocks_duties': True}, audience={'person_ids': [restricted]})
    workspace = h.lifecycle(h.detail(workspace)['workspace'], 'validate', role='admin')['workspace']
    original = h.detail(workspace)
    keys = [issues.issue_key(f) for f in original['workspace']['validation']['findings']
            if f.get('person_id') == restricted]
    assert keys

    def forbidden(*args, **kwargs):
        pytest.fail('Issue resolution must check authority before evaluating current findings')

    monkeypatch.setattr(issues, 'chosen', forbidden)
    if action == 'preview':
        response = h.client('coordinator').post('/api/planner/issues/replace-preview',
            json={'workspace_id': workspace['id'], 'expected_revision': workspace['revision'], 'keys': keys})
    else:
        response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())},
            json={'kind': 'issues.resolve', 'expected_revision': workspace['revision'], 'payload': {
                'workspace_id': workspace['id'], 'action': action, 'keys': keys, 'reason': REASON,
                **({'plan_hash': '0' * 64} if action == 'replace' else {})}})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    assert 'findings' not in response.text and restricted not in response.text
    assert h.detail(workspace) == original
# Include this file in hosted supervision and team-lead qualification.
