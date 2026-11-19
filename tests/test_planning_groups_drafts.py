"""Responsibility is dated configuration; private drafts stay private through the whole read graph."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from threading import Barrier
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
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


def test_generation_rejects_broader_populations_and_accepts_owned_conditions(h):
    workspace, shift, _ = integrated(h)
    refused = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'])) is None
    accepted = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'generation_scope': {'conditions': tree(h.seed['people'][0])}}, revision=workspace['revision'])
    assert accepted['job']['status'] == 'queued'


def test_generation_scope_fields_refuse_outside_and_accept_owned_population(h):
    from packages.contracts.schemas import GENERATION_SCOPE_FIELDS

    own, outside = h.seed['people'][:2]
    identity(h, 'generation_scope_outside', ['operator'], person=outside)
    workspace, _, _ = integrated(h, people=[], ends_on='2026-10-07')
    settings(h, group_lookahead='none')
    starts_on, ends_on = date(2020, 1, 1), date(2026, 10, 7)
    with transaction(h.app.state.engine, write=True) as db:
        department_id = h.seed['department_id']
        own_category = m.Category(department_id=department_id, name='Owned scope category', starts_on=starts_on,
                                  ends_on=ends_on, active=True)
        outside_category = m.Category(department_id=department_id, name='Outside scope category', starts_on=starts_on,
                                      ends_on=ends_on, active=True)
        own_capability = m.Capability(department_id=department_id, name='Owned scope capability', starts_on=starts_on,
                                      ends_on=ends_on, active=True)
        outside_capability = m.Capability(department_id=department_id, name='Outside scope capability',
                                          starts_on=starts_on, ends_on=ends_on, active=True)
        own_programme = m.Programme(department_id=department_id, name='Owned scope programme', starts_on=starts_on,
                                    ends_on=ends_on, active=True)
        outside_programme = m.Programme(department_id=department_id, name='Outside scope programme', starts_on=starts_on,
                                        ends_on=ends_on, active=True)
        db.add_all([own_category, outside_category, own_capability, outside_capability, own_programme, outside_programme])
        db.flush()
        own_cohort = m.Cohort(department_id=department_id, programme_id=own_programme.id, name='Owned scope cohort',
                              starts_on=starts_on, ends_on=ends_on)
        outside_cohort = m.Cohort(department_id=department_id, programme_id=outside_programme.id,
                                 name='Outside scope cohort', starts_on=starts_on, ends_on=ends_on)
        own_rotation_type = m.RotationType(department_id=department_id, name='Owned scope rotation', starts_on=starts_on,
                                           ends_on=ends_on, active=True)
        outside_rotation_type = m.RotationType(department_id=department_id, name='Outside scope rotation',
                                               starts_on=starts_on, ends_on=ends_on, active=True)
        own_service = m.Service(department_id=department_id, name='Owned scope service', starts_on=starts_on,
                                ends_on=ends_on, active=True)
        outside_service = m.Service(department_id=department_id, name='Outside scope service', starts_on=starts_on,
                                    ends_on=ends_on, active=True)
        own_tag = m.Tag(department_id=department_id, name='Owned scope tag', labels=['people'])
        outside_tag = m.Tag(department_id=department_id, name='Outside scope tag', labels=['people'])
        db.add_all([own_cohort, outside_cohort, own_rotation_type, outside_rotation_type,
                    own_service, outside_service, own_tag, outside_tag])
        db.flush()
        db.add_all([
            m.Membership(department_id=department_id, person_id=own, category_id=own_category.id,
                         starts_on=starts_on, ends_on=ends_on),
            m.Membership(department_id=department_id, person_id=outside, category_id=outside_category.id,
                         starts_on=starts_on, ends_on=ends_on),
            m.CapabilityGrant(department_id=department_id, person_id=own, capability_id=own_capability.id,
                              granted_by=h.seed['identities']['credentialer'], evidence='Synthetic scope evidence',
                              starts_on=starts_on, ends_on=ends_on),
            m.CapabilityGrant(department_id=department_id, person_id=outside, capability_id=outside_capability.id,
                              granted_by=h.seed['identities']['credentialer'], evidence='Synthetic scope evidence',
                              starts_on=starts_on, ends_on=ends_on),
            m.ProgrammeMembership(department_id=department_id, person_id=own, programme_id=own_programme.id,
                                  starts_on=starts_on, ends_on=ends_on),
            m.ProgrammeMembership(department_id=department_id, person_id=outside, programme_id=outside_programme.id,
                                  starts_on=starts_on, ends_on=ends_on),
            m.Rotation(department_id=department_id, person_id=own, cohort_id=own_cohort.id,
                       rotation_type_id=own_rotation_type.id, service_id=own_service.id,
                       starts_on=starts_on, ends_on=ends_on),
            m.Rotation(department_id=department_id, person_id=outside, cohort_id=outside_cohort.id,
                       rotation_type_id=outside_rotation_type.id, service_id=outside_service.id,
                       starts_on=starts_on, ends_on=ends_on),
            m.Eligibility(department_id=department_id, person_id=own, service_id=own_service.id,
                          starts_on=starts_on, ends_on=ends_on),
            m.Eligibility(department_id=department_id, person_id=outside, service_id=outside_service.id,
                          starts_on=starts_on, ends_on=ends_on),
            m.TagMember(department_id=department_id, tag_id=own_tag.id, kind='people', member_id=own,
                        starts_on=starts_on, ends_on=ends_on),
            m.TagMember(department_id=department_id, tag_id=outside_tag.id, kind='people', member_id=outside,
                        starts_on=starts_on, ends_on=ends_on),
        ])

    values = {
        'category_ids': (own_category.id, outside_category.id),
        'capability_ids': (own_capability.id, outside_capability.id),
        'programme_ids': (own_programme.id, outside_programme.id),
        'rotation_type_ids': (own_rotation_type.id, outside_rotation_type.id),
        'cohort_ids': (own_cohort.id, outside_cohort.id),
        'tags': (own_tag.name, outside_tag.name),
        'person_ids': (own, outside),
        'eligible_service_ids': (own_service.id, outside_service.id),
        'roles': ('staff', 'operator'),
    }
    assert set(values) == set(GENERATION_SCOPE_FIELDS)
    for field in GENERATION_SCOPE_FIELDS:
        for value, status in zip((values[field][1], values[field][0]), (403, 200)):
            result = h.command('coordinator', 'job.create', {
                'workspace_id': workspace['id'], 'kind': 'generation',
                'generation_scope': {'conditions': {'match': 'all', 'conditions': [
                    {'field': field, 'values': [value]}]}},
            }, revision=workspace['revision'], status=status)
            if status == 200:
                assert result['job']['status'] == 'queued'
                h.command('coordinator', 'job.cancel', {'reason': 'Synthetic scope parity case'},
                          result['job']['id'], result['job']['revision'])


@pytest.mark.parametrize('indexed', [False, True])
def test_generation_role_condition_does_not_mutate_caller_data(indexed):
    from packages.domain.conditions import rule_holds
    from packages.domain.time import aware

    data = {'organisation': {'timezone': 'UTC'}, 'role_bindings': [
        {'person_id': 'subject', 'role': 'staff', 'starts_on': '2020-01-01'},
        {'person_id': 'subject', 'role': 'auditor', 'ends_on': '2026-10-04'},
        {'person_id': 'subject', 'role': 'operator', 'revoked': True},
        {'person_id': 'other', 'role': 'coordinator'},
    ]}
    if indexed:
        data['_role_bindings_by_person'] = {
            person: [row for row in data['role_bindings'] if row['person_id'] == person]
            for person in ('subject', 'other')}
    before = deepcopy(data)
    a, b = aware('2026-10-04T08:00:00+00:00'), aware('2026-10-04T12:00:00+00:00')
    holds = rule_holds(data, 'subject', None, a, b)
    assert data == before
    assert holds('roles') == {'staff'}
    assert rule_holds(data, 'other', None, a, b)('roles') == {'coordinator'}
    assert data == before


@pytest.mark.parametrize('control', ['continue', 'lease_retry'])
def test_default_scope_generation_with_overlapping_memberships_can_continue_or_retry(h, control):
    from dataclasses import replace
    from apps.api.main import create_app
    from apps.worker.main import JOB_LEASE_SECONDS, claim_job, prepare_deferred_generation, process_one

    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    own, overlapping = h.seed['people'][:2]
    group(h, people=[overlapping], owner='admin', primary_priority=5)
    workspace, shift, selected = integrated(h, membership=tree(own, overlapping))
    settings(h, group_lookahead='none')
    payload = {'workspace_id': workspace['id'], 'kind': 'generation',
               'planning_group_version_id': selected['id']}
    refused = h.command('coordinator', 'job.create', {**payload,
        'generation_scope': {'conditions': tree(own, overlapping)}},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'])) is None
    queued = h.command('coordinator', 'job.create', payload, revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    request = deepcopy(claim['input']['generation_request'])
    assert 'generation_scope' not in request
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        assert not row.input_snapshot.get('_deferred_generation')
        assert row.input_snapshot['planning_population'][shift['id']] == [own]
        if control == 'continue':
            row.status, row.completed_at, row.result = 'succeeded', m.now(), {'status': 'needs_more_time'}
        else:
            row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)
    target = (h.command('coordinator', 'job.continue', {'reason': REASON}, queued['id'])['job']
              if control == 'continue' else queued)
    assert process_one(h.app.state.engine, settings=h.settings)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, target['id'])
        assert row.status == 'succeeded' and row.error_code is None
        assert row.attempts == (2 if control == 'lease_retry' else 1)
        assert row.input_snapshot['generation_request'] == request
        assert row.input_snapshot['planning_population'][shift['id']] == [own]


def _prepared_owned_group_job(h):
    from dataclasses import replace
    from apps.api.main import create_app
    from apps.worker.main import claim_job, prepare_deferred_generation

    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, _, selected = integrated(h, people=[])
    settings(h, group_lookahead='none')
    scope = {'conditions': tree(h.seed['people'][0])}
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': scope},
        revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    return workspace, selected, queued


def test_prepared_group_generation_can_continue_with_owned_population(h):
    from apps.worker.main import process_one

    workspace, _, queued = _prepared_owned_group_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        row.status, row.completed_at, row.result = 'succeeded', m.now(), {'status': 'needs_more_time'}
    continued = h.command('coordinator', 'job.continue', {'reason': REASON}, queued['id'])['job']
    assert process_one(h.app.state.engine, settings=h.settings)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, continued['id'])
        assert row.status == 'succeeded' and row.error_code is None
        assert db.get(m.Job, queued['id']).workspace_id == workspace['id']


def test_prepared_group_generation_lease_retry_rechecks_owned_population(h):
    from apps.worker.main import JOB_LEASE_SECONDS, claim_job, prepare_deferred_generation

    _, _, queued = _prepared_owned_group_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)
        retry = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, retry, h.settings)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, queued['id'])
        assert row.status == 'running' and row.error_code == 'WORKER_LEASE_EXPIRED'


def test_prepared_group_generation_broad_retry_refuses_before_supersession(h):
    from apps.api.errors import Problem
    from apps.worker.main import JOB_LEASE_SECONDS, claim_job, prepare_deferred_generation

    workspace, selected, queued = _prepared_owned_group_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        older = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='succeeded',
            input_fingerprint='older-prepared-run', input_snapshot={}, supersedes=[],
            created_at=m.now() - timedelta(minutes=1), result={'status': 'candidate_feasible'})
        db.add(older)
        row = db.get(m.Job, queued['id'])
        row.input_snapshot = {**row.input_snapshot, 'planning_group_version_id': selected['id']}
        row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)
    changed = h.command('admin', 'planning_group.update', {'definition': {
        **selected['definition'], 'membership': tree(h.seed['people'][1])}, 'reason': REASON},
        selected['group_id'], selected['revision'])['group']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        row.input_snapshot = {**row.input_snapshot, 'planning_group_version_id': changed['id'],
            'generation_request': {**row.input_snapshot['generation_request'], 'planning_group_version_id': changed['id']}}
        retry = claim_job(db)
    with pytest.raises(Problem) as refused:
        prepare_deferred_generation(h.app.state.engine, retry, h.settings)
    assert refused.value.code == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, older.id).supersedes == []
        assert db.get(m.Job, queued['id']).supersedes == []


def test_role_scoped_group_generation_rejects_broader_roles_and_accepts_owned_roles(h):
    identity(h, 'role_scope_staff', ['staff', 'coordinator'], person=h.seed['people'][0])
    identity(h, 'role_scope_coordinator', ['coordinator'], person=h.seed['people'][1])
    role_membership = {'match': 'all', 'conditions': [{'field': 'roles', 'values': ['staff']}]}
    workspace, _, selected = integrated(h, people=[], membership=role_membership)
    settings(h, group_lookahead='none')
    broad = {'conditions': {'match': 'all', 'conditions': [{'field': 'roles', 'values': ['coordinator']}]}}
    refused = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': broad},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'])) is None
    owned = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': {'conditions': role_membership}},
        revision=workspace['revision'])
    assert owned['job']['status'] == 'queued'
    h.command('coordinator', 'job.cancel', {'reason': REASON}, owned['job']['id'], owned['job']['revision'])
    person_scope = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': {'conditions': tree(h.seed['people'][0])}},
        revision=workspace['revision'])
    assert person_scope['job']['status'] == 'queued'


def test_generation_authority_distinguishes_same_time_duties_with_different_services(h):
    workspace, _, selected = integrated(h, people=[])
    settings(h, group_lookahead='none')
    workspace = h.shift(workspace, people=[], service=1, role='admin')['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        services = list(db.scalars(select(m.ShiftService.service_id).join(
            m.Shift, m.Shift.id == m.ShiftService.shift_id).where(m.Shift.workspace_id == workspace['id'])
            .order_by(m.Shift.start, m.Shift.id)))
        assert len(services) == 2 and services[0] != services[1]
        capability = m.Capability(department_id=h.seed['department_id'], name='Synthetic scope capability',
                                  starts_on=date(2020, 1, 1), active=True)
        db.add(capability)
        db.flush()
        db.add_all([m.CapabilityGrant(department_id=h.seed['department_id'], person_id=person,
            capability_id=capability.id, service_id=service, granted_by=h.seed['identities']['credentialer'],
            evidence=REASON, starts_on=date(2020, 1, 1))
            for person, service in ((h.seed['people'][0], None), (h.seed['people'][1], services[1]))])
    refused = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': {'conditions': {'match': 'all',
            'conditions': [{'field': 'capability_ids', 'values': [capability.id]}]}}},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'])) is None


@pytest.mark.parametrize('scoped', [False, True])
def test_compact_population_matches_full_snapshot_for_every_group_scope_field(h, scoped):
    from apps.api.services.planning_groups import _compact_generation_snapshot
    from apps.api.snapshots import domain_snapshot
    from packages.domain.generation_scope import population_allows

    role_identity = identity(h, 'role_scope_equivalence', ['auditor'], person=h.seed['people'][0])
    workspace, shift, _ = integrated(h, people=[])
    later = h.shift(workspace, start='2026-10-08T08:00:00+03:00', end='2026-10-08T12:00:00+03:00',
                    people=[], role='admin')
    later_shift_id = later['shifts'][0]['id']
    scope_ends = date(2026, 10, 7)
    with transaction(h.app.state.engine) as db:
        workspace_row = db.get(m.Workspace, workspace['id'])
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == role_identity)):
            binding.ends_on = scope_ends
        programme = m.Programme(department_id=h.seed['department_id'], name='Equivalence programme',
                                starts_on=date(2020, 1, 1), ends_on=date(2030, 1, 1), active=True)
        db.add(programme)
        db.flush()
        cohort = m.Cohort(department_id=h.seed['department_id'], programme_id=programme.id,
                          name='Equivalence cohort', starts_on=date(2020, 1, 1), ends_on=date(2030, 1, 1))
        rotation_type = m.RotationType(department_id=h.seed['department_id'], name='Equivalence rotation',
                                       starts_on=date(2020, 1, 1), ends_on=date(2030, 1, 1), active=True)
        db.add_all([cohort, rotation_type])
        db.flush()
        db.add_all([
            m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                                  programme_id=programme.id, starts_on=date(2020, 1, 1), ends_on=scope_ends),
            m.Rotation(department_id=h.seed['department_id'], person_id=h.seed['people'][0], cohort_id=cohort.id,
                       rotation_type_id=rotation_type.id, service_id=h.seed['services'][0],
                       starts_on=date(2020, 1, 1), ends_on=scope_ends),
        ])
        expired_category = m.Category(department_id=h.seed['department_id'], name='Expired scope category',
                                      starts_on=date(2020, 1, 1), ends_on=scope_ends, active=True)
        db.add(expired_category)
        db.flush()
        db.add(m.Membership(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                            category_id=expired_category.id, starts_on=date(2020, 1, 1), ends_on=scope_ends))
        tag = m.Tag(department_id=h.seed['department_id'], name='Equivalence tag', labels=['people'])
        db.add(tag)
        db.flush()
        db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag.id, kind='people',
                           member_id=h.seed['people'][0], starts_on=date(2020, 1, 1), ends_on=scope_ends))
        full = domain_snapshot(db, workspace_row, include_fairness=False, include_objectives=False,
                               include_fairness_history=False)
        compact = _compact_generation_snapshot(db, workspace_row, s.JobInput(
            workspace_id=workspace['id'], kind='generation'))
    person_id = h.seed['people'][0]
    person_rows = {name: next((row for row in compact.get(name, [])
                               if row.get('person_id') == person_id), {})
                   for name in ('memberships', 'grants', 'programme_memberships', 'rotations')}
    assert all(person_rows[name] for name in person_rows)
    assert compact['role_bindings']
    conditions = [
        ('person_ids', [person_id]),
        ('category_ids', [expired_category.id]),
        ('capability_ids', [person_rows['grants'].get('capability_id', 'missing-capability')]),
        ('programme_ids', [person_rows['programme_memberships'].get('programme_id', 'missing-programme')]),
        ('rotation_type_ids', [person_rows['rotations'].get('rotation_type_id', 'missing-rotation-type')]),
        ('cohort_ids', [person_rows['rotations'].get('cohort_id', 'missing-cohort')]),
        ('eligible_service_ids', [compact['eligibilities'][0]['service_id'] if compact['eligibilities'] else 'missing-service']),
        ('tags', ['Equivalence tag']),
        ('roles', ['auditor']),
    ]
    assert {field for field, _ in conditions} == set(s.GENERATION_SCOPE_FIELDS)
    compact_shift = next(row for row in compact['shifts'] if row['id'] == shift['id'])
    full_shift = next(row for row in full['shifts'] if row['id'] == shift['id'])
    for field, values in conditions:
        scope = {'conditions': {'match': 'all', 'conditions': [{'field': field, 'values': values}]}}
        if scoped:
            with transaction(h.app.state.engine) as db:
                compact = _compact_generation_snapshot(db, db.get(m.Workspace, workspace['id']),
                    s.JobInput(workspace_id=workspace['id'], kind='generation', generation_scope=scope), scope=scope)
            compact_shift = next(row for row in compact['shifts'] if row['id'] == shift['id'])
        compact_requested = {**compact, 'generation_scope': scope}
        full_requested = {**full, 'generation_scope': scope}
        assert population_allows(compact_requested, compact_shift, person_id) == population_allows(
            full_requested, full_shift, person_id), field
        if field in {'category_ids', 'programme_ids', 'rotation_type_ids', 'cohort_ids', 'roles'}:
            compact_later = next(row for row in compact['shifts'] if row['id'] == later_shift_id)
            full_later = next(row for row in full['shifts'] if row['id'] == later_shift_id)
            assert not population_allows(compact_requested, compact_later, person_id)
            assert not population_allows(full_requested, full_later, person_id)


def test_broader_selected_group_generation_refuses_before_insert(h):
    workspace, _, first = integrated(h)
    settings(h, group_lookahead='none')
    def reject_insert(*args):
        pytest.fail('unauthorized generation reached Job INSERT')

    event.listen(m.Job, 'before_insert', reject_insert)
    try:
        refused = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
            'planning_group_version_id': first['id'],
            'generation_scope': {'conditions': tree(h.seed['people'][0], h.seed['people'][1])}},
            revision=workspace['revision'], status=403)
    finally:
        event.remove(m.Job, 'before_insert', reject_insert)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.workspace_id == workspace['id'])) is None


def test_refused_group_generation_does_not_supersede_other_group_runs(h):
    workspace, _, first = integrated(h)
    other = group(h, people=[h.seed['people'][1]], owner='admin')
    settings(h, group_lookahead='none')
    broad = {'conditions': tree(h.seed['people'][0], h.seed['people'][1])}
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['admin'], kind='generation', status='succeeded',
            input_fingerprint='other-succeeded', input_snapshot={
                '_deferred_generation': True, 'planning_group_version_id': other['id'],
                'generation_scope': broad}, supersedes=[], created_at=m.now() - timedelta(minutes=1),
            completed_at=m.now(), result={'status': 'candidate_feasible'}))
    refused = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': first['id'],
        'generation_scope': broad},
        revision=workspace['revision'], status=403)
    assert refused['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine, write=True) as db:
        rows = list(db.scalars(select(m.Job).where(m.Job.workspace_id == workspace['id'])))
        assert len(rows) == 1 and rows[0].supersedes == []


def test_authorized_group_generation_supersedes_only_after_worker_guard(h):
    from apps.api.services.jobs import require_latest_run
    workspace, _, selected = integrated(h)
    settings(h, group_lookahead='none')
    scope = {'conditions': tree(h.seed['people'][0])}
    first = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': scope},
        revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        older = db.get(m.Job, first['id'])
        older.status, older.completed_at = 'succeeded', m.now()
        older.result = {'status': 'candidate_feasible'}
    second = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': scope},
        revision=workspace['revision'])['job']
    from apps.worker.main import claim_job, prepare_deferred_generation
    with transaction(h.app.state.engine) as db:
        queued = db.get(m.Job, second['id'])
        assert queued.supersedes == []
        assert 'score_baseline' not in queued.input_snapshot
        assert db.get(m.Job, first['id']).supersedes == []
        require_latest_run(db, db.get(m.Job, first['id']))
    with transaction(h.app.state.engine, write=True) as db:
        claim = claim_job(db)
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine) as db:
        prepared = db.get(m.Job, second['id'])
        assert prepared.supersedes == [first['id']]
        assert 'score_baseline' not in prepared.input_snapshot


def test_continue_rechecks_current_group_population_before_creating_job(h):
    from dataclasses import replace
    from apps.api.main import create_app
    from apps.worker.main import process_one

    h.settings = replace(h.settings, generation_engine='cp_sat')
    h.app = create_app(h.settings)
    workspace, _, selected = integrated(h)
    settings(h, group_lookahead='none')
    scope = {'conditions': tree(h.seed['people'][0])}
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id'], 'generation_scope': scope},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    with transaction(h.app.state.engine, write=True) as db:
        previous = db.get(m.Job, queued['id'])
        previous.result = {**(previous.result or {}), 'status': 'needs_more_time'}
        previous.input_snapshot = {**previous.input_snapshot,
            'planning_group_version_id': selected['id'],
            'generation_request': {'workspace_id': workspace['id'], 'kind': 'generation',
                                   'planning_group_version_id': selected['id'],
                                   'generation_scope': scope}}
    changed = h.command('admin', 'planning_group.update', {'definition': {
        **selected['definition'], 'membership': tree(h.seed['people'][1])}, 'reason': REASON},
        selected['group_id'], selected['revision'])['group']
    with transaction(h.app.state.engine) as db:
        previous = db.get(m.Job, queued['id'])
        previous.input_snapshot = {**previous.input_snapshot,
            'planning_group_version_id': changed['id'],
            'generation_request': {'workspace_id': workspace['id'], 'kind': 'generation',
                                   'planning_group_version_id': changed['id'],
                                   'generation_scope': scope}}
    denied = h.command('coordinator', 'job.continue', {'minutes': 1, 'reason': REASON},
                       queued['id'], status=403)
    assert denied['error']['code'] == 'GROUP_GENERATION_SCOPE'
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Job.id).where(m.Job.id != queued['id'],
                                                m.Job.workspace_id == workspace['id'],
                                                m.Job.kind == 'generation')) is None
        assert db.get(m.Job, queued['id']).supersedes == []


def test_worker_scope_refusal_is_audited_private_and_sanitized(h):
    from apps.worker.main import process_one

    identity(h, 'unrelated_scheduler', ['coordinator'])
    workspace, shift, selected = integrated(h, co_editor_identity_ids=[h.seed['identities']['admin']])
    settings(h, group_lookahead='none')
    queued = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'planning_group_version_id': selected['id']}, revision=workspace['revision'])['job']
    outside = h.seed['people'][1]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, queued['id'])
        row.input_snapshot = {**row.input_snapshot, 'generation_request': {**row.input_snapshot['generation_request'],
            'generation_scope': {'conditions': tree(h.seed['people'][0], outside)}}}
        row.progress = {'stage': 'checking', 'solution': {'pairs': [{'shift_id': shift['id'], 'person_id': outside}]}}

    def unexpected_scheduler(*args, **kwargs):
        pytest.fail('scope refusal must not call the scheduler')

    assert process_one(h.app.state.engine, scheduler=unexpected_scheduler)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, queued['id'])
        assert row.status == 'failed' and row.error_code == 'GROUP_GENERATION_SCOPE'
        assert row.supersedes == []
        audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.correlation_id == row.id,
            m.AuditEvent.action == 'command.refused')).one()
        assert audit.detail['error_code'] == 'GROUP_GENERATION_SCOPE' and audit.detail['status'] == 403
    for role in ('coordinator', 'admin'):
        for suffix in ('', '?full=true', '/status'):
            response = h.get(role, '/api/jobs/' + queued['id'] + suffix)['job']
            assert response['error_code'] == 'GROUP_GENERATION_SCOPE'
            assert response['input_fingerprint'] is None
            assert 'generation_scope' not in response and 'planning_context' not in response
            assert outside not in str(response)
        assert queued['id'] in {row['id'] for row in h.get(role, '/api/jobs')['jobs']}
    for suffix in ('', '/status'):
        h.get('unrelated_scheduler', '/api/jobs/' + queued['id'] + suffix, status=404)
    assert queued['id'] not in {row['id'] for row in h.get('unrelated_scheduler', '/api/jobs')['jobs']}


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
