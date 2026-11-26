"""Responsibility is dated configuration; private drafts stay private through the whole read graph."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services import planning_groups, policies
from packages.contracts import schemas as s
from packages.domain.planning_groups import GroupResolver, attribute_cause
from .governance_support import identity

REASON = 'Synthetic scheduling responsibility decision'


def roles(h):
    identity(h, 'programme_lead', ['programme_lead'])
    return h


def tree(*people):
    return {'match': 'all', 'conditions': [{'field': 'person_ids', 'values': list(people)}]}


def group(h, people=None, owner='coordinator', **changes):
    if not h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids']:
        settings(h)
    definition = {'name': 'Arbitrary planning population', 'starts_on': '2020-01-01',
        'membership': tree(*(people or [h.seed['people'][0]])),
        'owner_identity_ids': [h.seed['identities'][owner]], 'primary_priority': 10, **changes}
    return h.command('admin', 'planning_group.create', {'definition': definition, 'reason': REASON})['group']


def settings(h, **changes):
    current = h.get('admin', '/api/planning-settings')['settings']
    return h.command('admin', 'planning.settings', {'definition': {
        'coordinator_identity_ids': [h.seed['identities']['admin']], **changes}, 'reason': REASON},
        revision=current['revision'] if current else None)['settings']


def private(h, role='coordinator', **changes):
    return h.command(role, 'workspace.create', {'name': 'Secret sandbox name', 'starts_on': '2026-10-04',
        'ends_on': '2026-10-11', 'visibility': 'private', 'purpose': 'sandbox', **changes})['workspace']


def edit(h, workspace, shift, participants, role='coordinator', status=200, **changes):
    fields = {key: shift[key] for key in s.ShiftInput.model_fields}
    fields['participants'] = [{key: p[key] for key in s.ParticipantInput.model_fields if key in p} for p in participants]
    return h.command(role, 'shift.update', {**fields, 'workspace_id': workspace['id'], 'shift_id': shift['id'], **changes},
                     revision=workspace['revision'], status=status)


def integrated(h, *, people=None, typ=None, **group_changes):
    group_row = group(h, **group_changes)
    settings(h)
    created = h.shift(h.workspace(), people=people if people is not None else [], typ=typ, role='admin')
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON},
        created['workspace']['id'], created['workspace']['revision'])['workspace']
    return workspace, created['shifts'][0], group_row


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_private_names_content_jobs_history_and_copy_are_creator_only_for_every_role(h, role):
    roles(h)
    workspace = h.shift(private(h))['workspace']
    shift = h.detail(workspace)['shifts'][0]
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
        revision=workspace['revision'])['job']
    allowed = role == 'coordinator'
    planner = role in {'admin', 'coordinator', 'approver', 'auditor', 'programme_lead'}
    for suffix in ('', '/summary', '/rule-map', '/shifts/' + shift['id'] + '/history'):
        h.get(role, '/api/workspaces/' + workspace['id'] + suffix, status=200 if allowed else 404 if planner else 403)
    listing = h.get(role, '/api/workspaces?q=Secret', status=200 if planner else 403)
    if planner:
        assert (workspace['id'] in {w['id'] for w in listing['workspaces']}) == allowed
    h.get(role, '/api/jobs/' + job['id'], status=200 if allowed else 404)
    if role in {'admin', 'coordinator', 'approver', 'operator'}:
        jobs = h.get(role, '/api/jobs')['jobs']
        assert (job['id'] in {row['id'] for row in jobs}) == allowed
    if role in {'admin', 'coordinator'}:
        reply = h.command(role, 'workspace.create', {'name': 'Copy attempt', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
            'source_workspace_id': workspace['id'], 'source_revision': workspace['revision'], 'copy_people': True},
            status=200 if allowed else 404)
        if allowed:
            assert reply['setup']['created'] == 1


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_configuration_commands_and_reads_use_authoritative_roles(h, role):
    roles(h)
    payload = {'definition': {'coordinator_identity_ids': [h.seed['identities']['coordinator']]}, 'reason': REASON}
    h.command(role, 'planning.settings', payload, status=200 if role == 'admin' else 403)
    h.get(role, '/api/planning-groups', status=200 if role in {'admin', *s.PERMISSIONS['planning.read']} else 403)


def test_group_owner_without_configuration_permission_is_refused_but_admin_can_update(h):
    first = group(h, owner='coordinator')
    assert 'coordinator' not in s.PERMISSIONS['planning.configure']
    path = '/api/planning-groups?group_id=' + first['group_id']
    before = h.get('coordinator', path)['groups']
    payload = {'definition': {**first['definition'], 'name': 'Renamed population'}, 'reason': REASON}
    refused = h.command('coordinator', 'planning_group.update', payload,
        first['group_id'], first['revision'], status=403)
    assert refused['error']['code'] == 'FORBIDDEN'
    assert h.get('coordinator', path)['groups'] == before
    changed = h.command('admin', 'planning_group.update', payload, first['group_id'], first['revision'])['group']
    assert changed['definition'] == payload['definition']
    assert changed['revision'] == first['revision'] + 1 and changed['previous_id'] == first['id']
    history = h.get('coordinator', path)['groups']
    assert [row['id'] for row in history] == [changed['id'], first['id']]
    assert history[0]['definition'] == payload['definition']


def test_group_history_rollback_and_settings_are_append_only_with_stale_write_refusal(h):
    first = group(h)
    second = h.command('admin', 'planning_group.update', {'definition': {**first['definition'], 'name': 'Renamed population'},
        'reason': REASON}, first['group_id'], first['revision'])['group']
    h.command('admin', 'planning_group.update', {'definition': first['definition'], 'reason': REASON},
        first['group_id'], first['revision'], status=409)
    restored = h.command('admin', 'planning_group.rollback', {'version_id': first['id'], 'reason': REASON},
        first['group_id'], second['revision'])['group']
    assert restored['definition'] == first['definition'] and restored['previous_id'] == second['id']
    history = h.get('admin', '/api/planning-groups?group_id=' + first['group_id'] + '&limit=2')
    assert [r['revision'] for r in history['groups']] == [3, 2] and history['has_more']
    configured = settings(h, active_private_drafts_per_owner=1, archive_after_days=30)
    changed = h.command('admin', 'planning.settings', {'definition': {**configured['definition'], 'archive_after_days': 60},
        'reason': REASON}, revision=configured['revision'])['settings']
    restored_settings = h.command('admin', 'planning.settings.rollback', {'version_id': configured['id'], 'reason': REASON},
        revision=changed['revision'])['settings']
    assert restored_settings['definition'] == configured['definition']
    with pytest.raises(DBAPIError):
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(text('UPDATE planning_group_versions SET reason = :reason WHERE id = :id'), {'reason': 'Overwrite', 'id': first['id']})


def test_configuration_undo_appends_versions_and_audit_names_the_stable_target(h):
    configured = settings(h)
    forged_target = str(uuid4())
    created = h.command('admin', 'planning_group.create', {'definition': {
        'name': 'Undoable population', 'starts_on': '2020-01-01', 'membership': tree(h.seed['people'][0]),
        'owner_identity_ids': [h.seed['identities']['coordinator']], 'primary_priority': 10}, 'reason': REASON}, forged_target)
    def undo(reply):
        value = reply['undo']
        return h.command('admin', value['kind'], value['payload'], value['target_id'], value['expected_revision'])
    retired = undo(created)
    assert retired['group']['revision'] == 2 and not retired['group']['definition']['active']
    restored = undo(retired)
    assert restored['group']['revision'] == 3 and restored['group']['definition'] == created['group']['definition']
    initial_settings = h.command('admin', 'planning.settings', {'definition': {
        'coordinator_identity_ids': [h.seed['identities']['coordinator']], 'archive_after_days': 30}, 'reason': REASON}, forged_target, configured['revision'])
    reset = undo(initial_settings)
    assert reset['settings']['revision'] == configured['revision'] + 2 and reset['settings']['definition'] == configured['definition']
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(('planning_group.create', 'planning.settings')))))
        assert {e.target_id for e in events} == {created['group']['group_id'], h.seed['department_id']}


def test_workspace_list_access_is_batched_and_visibility_heads_are_current(h):
    workspaces = [private(h, name='Batched sandbox ' + str(n)) for n in range(5)]
    relationship_reads = []
    def capture(state):
        if state.is_relationship_load:
            relationship_reads.append(state.statement)
    event.listen(Session, 'do_orm_execute', capture)
    try:
        result = h.get('coordinator', '/api/workspaces?q=Batched')
    finally:
        event.remove(Session, 'do_orm_execute', capture)
    assert {row['id'] for row in result['workspaces']} == {w['id'] for w in workspaces}
    assert all(row['visibility'] == 'private' and row['access_revision'] == 1 for row in result['workspaces'])
    assert not relationship_reads
    first = workspaces[0]
    shared = h.command('coordinator', 'workspace.share', {'reason': REASON}, first['id'], first['revision'])['workspace']
    assert shared['access_revision'] == 2 and shared['visibility'] == 'shared'
    assert h.get('admin', '/api/workspaces/' + first['id'])['workspace']['access_version_id'] == shared['access_version_id']


def test_private_audit_envelope_keeps_unrelated_governance_and_self_approval_evidence(h):
    from apps.api.services.workspace_access import audit_envelope
    workspace = private(h)
    with transaction(h.app.state.engine) as db:
        db.info['private_workspace_ids'] = {workspace['id']}
        details = {'self_approved': {'reason': 'Recorded governance reason', 'permission_id': str(uuid4())}}
        governance = m.AuditEvent(action='policy.activate', reason=REASON, detail=deepcopy(details))
        public = s.Command(kind='policy.activate', target_id=str(uuid4()), payload={'reason': REASON})
        assert audit_envelope(db, public, {}, governance).detail == details
        assert governance.reason == REASON
        private_event = m.AuditEvent(action='shift.update', reason='Secret text', detail=deepcopy(details))
        command = s.Command(kind='shift.update', payload={'workspace_id': workspace['id']})
        sanitized = audit_envelope(db, command, {}, private_event)
        assert sanitized.reason == 'Private draft operation'
        assert sanitized.detail == {'private': True, **details}


def test_membership_is_dated_deterministic_and_attribution_is_disjoint(h):
    first = group(h)
    second = group(h, primary_priority=20)
    created = h.shift(h.workspace(), role='admin')
    data = h.snapshot(created['workspace'])
    shift = data['shifts'][0]
    with transaction(h.app.state.engine) as db:
        groups = planning_groups.load_groups(db, h.seed['department_id'], starts_on='2026-10-04', ends_on='2026-10-11')
    resolver = GroupResolver(data, groups)
    assert resolver.primary(h.seed['people'][0], shift)['group_id'] == first['group_id']
    assert resolver.primary(h.seed['people'][1], shift) is None
    mine = attribute_cause(data, groups, person_ids=[h.seed['people'][0]], shift_ids=[shift['id']], selected_group_id=first['group_id'])
    assert mine['classification'] == 'mine' and mine['responsibility_version_ids'] == [first['id']]
    assert attribute_cause(data, groups, person_ids=[h.seed['people'][0]], shift_ids=[shift['id']],
        selected_group_id=second['group_id'])['classification'] == 'other_pending'
    assert attribute_cause(data, groups, person_ids=[h.seed['people'][1]], shift_ids=[shift['id']])['classification'] == 'unassigned'
    assert attribute_cause(data, groups, person_ids=[h.seed['people'][0]], shift_ids=[shift['id']],
        required_group_ids=[second['group_id']])['classification'] == 'coupling'
    assert not attribute_cause(data, groups, person_ids=[], shift_ids=[shift['id']])['complete']
    # Equal priorities never choose by UUID/name. A future version takes effect only on its own dates.
    tied = {**second, 'definition': {**second['definition'], 'primary_priority': 10}}
    assert GroupResolver(data, [first, tied]).primary(h.seed['people'][0], shift) is None
    future = {**first, 'id': str(uuid4()), 'revision': 2,
        'definition': {**first['definition'], 'starts_on': '2026-10-05', 'active': False}}
    future_shift = {**shift, 'start': '2026-10-05T08:00:00+03:00', 'end': '2026-10-05T12:00:00+03:00'}
    assert GroupResolver(data, [first, future]).primary(h.seed['people'][0], shift)
    assert GroupResolver(data, [first, future]).primary(h.seed['people'][0], future_shift) is None
    overnight = {**shift, 'start': '2026-10-04T23:00:00+03:00', 'end': '2026-10-05T03:00:00+03:00'}
    assert GroupResolver(data, [first, future]).primary(h.seed['people'][0], overnight) is None


def test_shared_rota_slice_retains_outsider_rows_and_reauthorizes_undo(h):
    own, outsider = h.seed['people'][:2]
    workspace, shift, configured = integrated(h, people=[outsider])
    shift = h.detail(workspace)['shifts'][0]
    original = deepcopy(shift['participants'])
    own_participant = {'person_id': own, 'start': shift['start'], 'end': shift['end']}
    reply = edit(h, workspace, shift, [*original, own_participant])
    workspace = reply['workspace']
    after = h.detail(workspace)['shifts'][0]
    assert next(p for p in after['participants'] if p['person_id'] == outsider) == original[0]
    denied = edit(h, workspace, after, [own_participant], status=403)
    assert denied['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [shift['id']],
        'action': 'cancel', 'reason': REASON}, revision=workspace['revision'], status=403)
    h.shift(workspace, status=403)
    # Responsibility revocation cannot be bypassed by reversing an earlier authorized command.
    h.command('admin', 'planning_group.update', {'definition': {**configured['definition'],
        'owner_identity_ids': [h.seed['identities']['admin']]}, 'reason': REASON}, configured['group_id'], configured['revision'])
    h.command('coordinator', 'workspace.undo', {'reason': REASON}, workspace['id'], workspace['revision'], status=403)
    assert h.detail(workspace)['shifts'][0] == after


def test_shared_sandbox_is_view_and_copy_only_but_private_experiments_are_department_wide(h):
    workspace = h.shift(private(h), people=[h.seed['people'][1]])['workspace']
    assert h.command('coordinator', 'workspace.submit', {'reason': REASON}, workspace['id'], workspace['revision'], status=409)['error']['code'] == 'SANDBOX_INTEGRATION_REQUIRED'
    shared = h.command('coordinator', 'workspace.share', {'reason': REASON}, workspace['id'], workspace['revision'])['workspace']
    assert shared['visibility'] == 'shared' and shared['purpose'] == 'sandbox'
    h.get('admin', '/api/workspaces/' + workspace['id'])
    h.command('admin', 'workspace.archive', {'reason': REASON}, shared['id'], shared['revision'], status=403)
    h.command('admin', 'workspace.share', {'reason': REASON}, shared['id'], shared['revision'], status=403)
    h.command('coordinator', 'workspace.share', {'reason': REASON}, shared['id'], shared['revision'], status=409)


def test_private_drafts_do_not_block_rule_activation_or_emit_notices_and_audit_is_minimized(h):
    workspace = h.shift(private(h))['workspace']
    with transaction(h.app.state.engine) as db:
        receipts = list(db.scalars(select(m.CommandReceipt).where(m.CommandReceipt.private_owner_id.is_not(None))))
        assert receipts
        events = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action.in_(('workspace.create', 'shift.create')))))
        assert events and all(row.reason == 'Private draft operation' and row.detail == {'private': True} for row in events)
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.payload['workspace_id'].as_string() == workspace['id'])))
    candidate = h.command('coordinator', 'policy.create', {'name': 'Private conflict ignored', 'owner': 'Test', 'source': REASON,
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'max_duties', 'maximum': 0},
        'scope': {'conditions': tree(h.seed['people'][0])}, 'severity': 'mandatory'}})['record']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client('coordinator').cookies.get('rota_session'), settings=h.settings)
        assert policies.policy_impact(db, actor, db.get(m.Policy, candidate['id'])) == []
    h.command('approver', 'policy.activate', {'reason': REASON}, candidate['id'], candidate['revision'])


def test_private_home_and_permanent_conversion_manifest_ignore_own_sandbox(h):
    from apps.api.services.rule_catalogue_v2 import _inputs
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies.get('rota_session'), settings=h.settings)
        before = _inputs(db, actor)
    workspace = h.shift(private(h))['workspace']
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies.get('rota_session'), settings=h.settings)
        assert _inputs(db, actor) == before
    home = h.get('coordinator', '/api/home')
    assert workspace['id'] not in str(home) and 'Secret sandbox name' not in str(home)
    assert not h.get('coordinator', '/api/home/rotas?start=2026-10-04&end=2026-10-11')['workspaces']


def test_system_catalogue_proof_does_not_pick_up_private_drafts(h, monkeypatch):
    from apps.worker import catalogue_proof
    from apps.api.services import rule_catalogue_v2
    public = h.workspace()
    private(h)
    checked = []
    def proof(db, actor, candidate, rotas, **kwargs):
        checked.extend(row.id for row in rotas)
        return {'identical': True}
    monkeypatch.setattr(catalogue_proof, 'catalogue_proof_stale', lambda *args: False)
    monkeypatch.setattr(rule_catalogue_v2, '_proof', proof)
    inputs = {'candidate': {}, 'rota_ids': [public['id']], 'manifest': 'synthetic', 'plan_hash': 'synthetic', 'toolchain': {}}
    with transaction(h.app.state.engine) as db:
        result = catalogue_proof.run(db, {'department_id': h.seed['department_id'],
            'requested_by': h.seed['identities']['coordinator'], 'input': inputs},
            progress=lambda value: None, cancelled=lambda: False)
    assert checked == [public['id']] and result['status'] == 'identical'


def test_group_projection_at_department_scale_does_no_per_person_or_session_sql(h):
    created = h.shift(h.workspace(), role='admin')
    data = h.snapshot(created['workspace'])
    people = [str(uuid4()) for _ in range(150)]
    data['people'] = {pid: {'id': pid} for pid in people}
    definitions = [{'group_id': str(uuid4()), 'id': str(uuid4()), 'revision': 1,
        'definition': {'name': 'Configured population', 'starts_on': '2020-01-01', 'ends_on': None,
            'membership': tree(*population), 'owner_identity_ids': [h.seed['identities']['coordinator']],
            'primary_priority': priority}} for priority, population in enumerate((people[:75], people[75:]))]
    shifts = [{**data['shifts'][0], 'id': str(uuid4())} for _ in range(500)]
    statements = []
    def capture(*args):
        statements.append(args[2])
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    start = perf_counter()
    try:
        resolver = GroupResolver(data, definitions)
        assigned = [resolver.primary(pid, shift)['group_id'] for shift in shifts for pid in people]
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert len(assigned) == 75_000 and not statements
    assert assigned[:75] == [definitions[0]['group_id']] * 75
    print({'people': 150, 'sessions': 500, 'resolutions': len(assigned), 'sql': len(statements), 'seconds': perf_counter() - start})


def test_linked_supervisor_cannot_be_removed_from_another_groups_unchanged_assignment(h):
    own, outsider = h.seed['people'][:2]
    workspace, shift, _ = integrated(h, people=[own, {'person_id': outsider,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'supervisor_person_id': own}])
    outsider_row = next(p for p in shift['participants'] if p['person_id'] == outsider)
    denied = edit(h, workspace, shift, [outsider_row], status=409)
    assert denied['error']['code'] == 'GROUP_DEPENDENCY_PROTECTED'
    assert len(h.detail(workspace)['shifts'][0]['participants']) == 2


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_only_configured_coordinator_can_change_integrated_session_definitions(h, role):
    roles(h)
    workspace, shift, _ = integrated(h)
    result = h.command(role, 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [shift['id']],
        'action': 'cancel', 'reason': REASON}, revision=workspace['revision'], status=200 if role == 'admin' else 403)
    if role == 'coordinator':
        assert result['error']['code'] == 'SESSION_DEFINITION_AUTHORITY'


def test_admin_does_not_bypass_slice_authority_and_coeditors_need_live_scheduling_access(h):
    group_row = group(h, co_editor_identity_ids=[h.seed['identities']['admin']])
    bad = {**group_row['definition'], 'co_editor_identity_ids': [h.seed['identities']['staff']]}
    h.command('admin', 'planning_group.update', {'definition': bad, 'reason': REASON}, group_row['group_id'], group_row['revision'], status=422)
    configured = settings(h)
    legacy = h.shift(h.workspace(), people=[], role='admin')
    workspace = h.command('admin', 'workspace.designate', {'reason': REASON}, legacy['workspace']['id'], legacy['workspace']['revision'])['workspace']
    h.command('admin', 'planning.settings', {'definition': {**configured['definition'],
        'coordinator_identity_ids': [h.seed['identities']['coordinator']]}, 'reason': REASON}, revision=configured['revision'])
    own = {'person_id': h.seed['people'][0], 'start': legacy['shifts'][0]['start'], 'end': legacy['shifts'][0]['end']}
    workspace = edit(h, workspace, legacy['shifts'][0], [own], role='admin')['workspace']
    other = {**own, 'person_id': h.seed['people'][1]}
    edit(h, workspace, h.detail(workspace)['shifts'][0], [own, other], role='admin', status=403)


def test_private_limits_archive_restore_and_age_settings_preserve_evidence(h):
    settings(h, active_private_drafts_per_owner=1, archive_after_days=30)
    workspace = private(h)
    h.command('coordinator', 'workspace.create', {'name': 'Over limit', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'visibility': 'private', 'purpose': 'sandbox'}, status=409)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Workspace, workspace['id']).created_at = m.now() - timedelta(days=31)
    assert h.command('coordinator', 'workspace.archive_due', {'reason': REASON})['archived_workspace_ids'] == [workspace['id']]
    newer = private(h)
    archived = h.get('coordinator', '/api/workspaces/' + workspace['id'])['workspace']
    h.command('coordinator', 'workspace.restore', {'reason': REASON}, workspace['id'], archived['revision'], status=409)
    h.command('coordinator', 'workspace.archive', {'reason': REASON}, newer['id'], newer['revision'])
    h.command('coordinator', 'workspace.restore', {'reason': REASON}, workspace['id'], archived['revision'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_generation_rejects_broader_populations_and_accepts_owned_conditions(h):
    workspace, _, group_row = integrated(h)
    payload = {'workspace_id': workspace['id'], 'kind': 'generation'}
    owned, other = h.seed['people'][:2]
    broad = {'generation_scope': {'conditions': tree(owned, other)}}
    for extra in ({}, broad, {**broad, 'planning_group_version_id': group_row['id']}):
        refused = h.command('coordinator', 'job.create', {**payload, **extra},
            revision=workspace['revision'], status=403)
        assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
        assert refused['error']['message'] == 'Choose only your scheduling group population'
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'], m.Job.kind == 'generation'))

    accepted = h.command('coordinator', 'job.create', {**payload,
        'generation_scope': {'conditions': tree(owned)}}, revision=workspace['revision'])['job']
    assert accepted['status'] == 'queued'
    h.command('coordinator', 'job.cancel', {'reason': REASON}, accepted['id'])
    settings(h, coordinator_identity_ids=[h.seed['identities']['coordinator']])
    selected = h.command('admin', 'job.create', {**payload, 'planning_group_version_id': group_row['id'],
        'lookahead': 'none'}, revision=workspace['revision'])['job']
    assert selected['status'] == 'queued'
    assert 'outside_group_count' not in selected['planning_context']
    h.command('admin', 'job.cancel', {'reason': REASON}, selected['id'])
    partial = h.command('admin', 'job.create', {**payload, **broad, 'planning_group_version_id': group_row['id'],
        'lookahead': 'none'}, revision=workspace['revision'])['job']
    assert partial['planning_context']['outside_group_count'] == 1
    assert h.get('admin', '/api/jobs/' + partial['id'])['job']['planning_context']['outside_group_count'] == 1
    h.command('admin', 'job.cancel', {'reason': REASON}, partial['id'])
    unrestricted = h.command('admin', 'job.create', payload, revision=workspace['revision'])['job']
    assert unrestricted['status'] == 'queued'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_generation_continuation_rechecks_group_authority(h):
    from dataclasses import replace

    from apps.api.main import create_app
    from apps.worker.main import process_one

    h.app.state.engine.dispose()
    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, _, _ = integrated(h)
    configured = {'coordinator_identity_ids': [h.seed['identities']['coordinator']]}
    settings(h, **configured)
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine, scheduler=lambda *_args, **_kwargs: {'status': 'needs_more_time'})

    settings(h)
    refused = h.command('coordinator', 'job.continue', {'reason': REASON}, job['id'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'

    settings(h, **configured)
    continued = h.command('coordinator', 'job.continue', {'reason': REASON}, job['id'])['job']
    assert continued['status'] == 'queued'
    settings(h)
    assert process_one(h.app.state.engine, scheduler=lambda *_args, **_kwargs: pytest.fail('Ran without group authority'))
    failed = h.get('coordinator', '/api/jobs/' + continued['id'])['job']
    assert failed['status'] == 'failed' and failed['error_code'] == 'GROUP_GENERATION_SCOPE'


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('future_group', [False, True])
def test_unlinked_admin_reads_ungrouped_draft_summary_and_fix_list(h, future_group):
    identity(h, 'summary-admin', ['admin'])
    workspace = h.shift(h.workspace(), people=[])['workspace']
    if future_group:
        group(h, starts_on='2026-11-01')

    path = f'/api/workspaces/{workspace["id"]}'
    summary = h.get('summary-admin', path + '/summary')
    fixes = h.get('summary-admin', path + '/fix-list?view=lean')
    assert summary['workspace_id'] == fixes['workspace_id'] == workspace['id']
    assert summary['fix_list']['planning_context']['groups'] == fixes['planning_context']['groups'] == []
    assert [cause['id'] for cause in fixes['causes']] == [cause['id'] for cause in summary['fix_list']['causes']]


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('with_groups', [False, True], ids=['without-groups', 'with-groups'])
def test_sole_admin_queues_and_applies_whole_department_generation(h, with_groups):
    admin = h.seed['identities']['admin']
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Identity).where(m.Identity.id != admin).values(disabled=True))

    client = h.client('admin')

    def command(kind, payload, revision=None):
        response = client.post('/api/commands', headers={'idempotency-key': str(uuid4())},
                               json={'kind': kind, 'payload': payload, 'expected_revision': revision})
        assert response.status_code == 200, response.text
        return response.json()

    assert h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids'] == []
    workspace = command('workspace.create', {'name': 'Department schedule',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['workspace']
    workspace = command('shift.create', {'workspace_id': workspace['id'],
        'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
        'service_ids': [h.seed['services'][0]], 'start': '2026-10-04T08:00:00+03:00',
        'end': '2026-10-04T12:00:00+03:00', 'participants': []}, workspace['revision'])['workspace']
    if with_groups:
        group(h, owner='admin')
        assert h.get('admin', '/api/planning-settings')['definition']['coordinator_identity_ids'] == [admin]
        settings(h, coordinator_identity_ids=[h.seed['identities']['coordinator']])
    detail = h.get('admin', '/api/workspaces/' + workspace['id'])
    assert detail['workspace']['purpose'] == ('integrated' if with_groups else 'alternative')

    job = command('job.create', {'workspace_id': workspace['id'], 'kind': 'generation'}, workspace['revision'])['job']
    assert job['status'] == 'queued'
    assert h.process_jobs()
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'succeeded' and row.result['additions']
        assert row.input_snapshot.get('generation_scope') is None
        assert row.input_snapshot.get('planning_population') is None

    applied = command('generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Apply the department schedule'}, workspace['revision'])
    assert applied['reconciliation']['status'] == 'applied'
    detail = h.get('admin', '/api/workspaces/' + workspace['id'])
    assert detail['shifts'][0]['participants']


def test_generation_apply_and_bulk_cannot_bypass_current_slice_authority(h):
    from apps.api.snapshots import fingerprint
    workspace, shift, _ = integrated(h)
    participant = {'person_id': h.seed['people'][1], 'start': shift['start'], 'end': shift['end']}
    source = {**h.snapshot(workspace), 'generation_engine': 'legacy'}
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint=fingerprint(db, db.get(m.Workspace, workspace['id'])),
            input_snapshot=source,
            result={'status': 'candidate_feasible', 'additions': [{'shift_id': shift['id'], 'participant': participant}], 'removals': []})
        db.add(job); db.flush(); job_id = job.id
    denied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job_id, 'reason': REASON},
        revision=workspace['revision'], status=403)
    assert denied['error']['code'] == 'GROUP_EDIT_AUTHORITY'
    body = {'reason': REASON, 'items': [{'item_kind': 'shift.action', 'target_id': workspace['id'],
        'expected_revision': workspace['revision'], 'payload': {'workspace_id': workspace['id'],
        'shift_ids': [shift['id']], 'action': 'cancel'}}]}
    h.command('coordinator', 'bulk.apply', body, status=409)
    assert not h.detail(workspace)['shifts'][0]['participants']


def test_alternative_cannot_be_submitted_over_an_existing_department_rota(h):
    integrated(h)
    alternative = h.shift(h.workspace('An alternative'))['workspace']
    denied = h.command('coordinator', 'workspace.submit', {'reason': REASON}, alternative['id'], alternative['revision'], status=409)
    assert denied['error']['code'] == 'ROTA_INTEGRATION_REQUIRED'


@pytest.mark.postgres
def test_postgres_private_capacity_is_atomic_between_concurrent_commands(pg):
    h = pg
    settings(h, active_private_drafts_per_owner=1)
    client = h.client('coordinator')
    gate = Barrier(2)
    def create(index):
        gate.wait()
        return client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={'kind': 'workspace.create',
            'payload': {'name': 'Racing private draft ' + str(index), 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
                        'visibility': 'private', 'purpose': 'sandbox'}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert next(r for r in results if r.status_code == 409).json()['error']['code'] == 'PRIVATE_DRAFT_LIMIT'


@pytest.mark.postgres
def test_postgres_group_versions_are_immutable_and_concurrent_updates_do_not_overwrite(pg):
    h = pg
    first = group(h)
    client = h.client('admin')
    gate = Barrier(2)
    def update(index):
        gate.wait()
        return client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={'kind': 'planning_group.update',
            'target_id': first['group_id'], 'expected_revision': 1,
            'payload': {'definition': {**first['definition'], 'name': 'Concurrent version ' + str(index)}, 'reason': REASON}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.PlanningGroupVersion)) == 2
    with pytest.raises(DBAPIError):
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(text('DELETE FROM planning_group_versions WHERE id = :id'), {'id': first['id']})


@pytest.mark.postgres
def test_postgres_concurrent_designation_keeps_one_department_rota(pg):
    h = pg
    settings(h)
    workspaces = [h.workspace('Racing department rota ' + str(n)) for n in range(2)]
    client, gate = h.client('admin'), Barrier(2)
    def designate(workspace):
        gate.wait()
        return client.post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={'kind': 'workspace.designate',
            'target_id': workspace['id'], 'expected_revision': workspace['revision'], 'payload': {'reason': REASON}})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(designate, workspaces))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert next(r for r in results if r.status_code == 409).json()['error']['code'] == 'ROTA_ALREADY_DESIGNATED'


def test_compact_detail_of_an_integrated_rota_carries_its_grouped_review_and_planning_context(h):
    workspace, _, _ = integrated(h, people=[h.seed['people'][1]])
    path = f"/api/workspaces/{workspace['id']}"
    full, compact = h.get('coordinator', path), h.get('coordinator', path + '?view=compact')
    # Group scope and Submit authority show on the first paint of the window, from the whole read's one owner.
    assert full['planning_context'] and compact['planning_context'] == full['planning_context']
    assert compact['review_items'] == full['review_items']
