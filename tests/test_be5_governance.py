"""maintenance: dated governance effects, explicit grant expiry and fail-closed preference policy."""
import copy
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.read_projection_warming import warm_all
from apps.api.services.rule_effects import affected_workspaces
from apps.api.services.wishes import multipliers
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules
from packages.domain.rule_effects import observed_effect
from packages.domain.rule_lines import default_line_strengths, line_relations
from packages.domain.time import local_midnight
from .clock_support import freeze_clock
from .governance_support import identity
from .test_rule_catalogue_api import line, person, propose, approve
from .test_wish_settings import payload, save_settings
from .test_staff_wishes import department_settings


def actor_for(h, db, role='coordinator'):
    client = h.client(role)
    return authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=False)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_explicit_no_end_and_legacy_grants_remain_valid_visible_and_undoable(request, database):
    h = request.getfixturevalue(database)
    value = {'role': 'approver', 'actions': ['rota.approve'], 'starts_on': '2026-01-01',
             'no_end_date': True, 'reason': 'Standing sole approver cover explicitly chosen'}
    grant = h.command('admin', 'self_approval.grant', value)['permission']
    assert grant['ends_on'] is None
    for explicit in (False, True):
        listing = next(p for p in h.get('admin', '/api/self-approval')['permissions'] if p['id'] == grant['id'])
        assert listing['state'] == 'active' and listing['no_end_date'] and listing['end_label'] == 'No end date'
        if not explicit:
            # Existing evidence contains only null ends, never the new input-only choice.
            with transaction(h.app.state.engine) as db:
                assert 'no_end_date' not in db.scalar(select(m.DefinitionHistory).where(
                    m.DefinitionHistory.kind == 'self_approval', m.DefinitionHistory.object_id == grant['id'])).snapshot
    revoked = h.command('admin', 'self_approval.revoke', {'reason': 'Independent reviewer returned'}, grant['id'], grant['revision'])['permission']
    restored = h.command('admin', 'self_approval.restore', {'reason': 'Undo ending standing cover'}, revoked['id'], revoked['revision'])['permission']
    assert restored['ends_on'] is None
    h.command('admin', 'self_approval.grant', {**value, 'ends_on': '2027-01-01'}, status=422)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_governance_permissions_use_contract_roles(h, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    h.command(role, 'self_approval.grant', {'role': 'approver', 'actions': ['rota.approve'],
        'starts_on': '2026-01-01', 'reason': 'Explicit role permission test'}, status=200 if role == 'admin' else 403)
    response = h.client(role).post('/api/rules/catalogue/duties_per_week/check', json={'lines': []})
    assert response.status_code == (200 if role in {'admin', 'coordinator', 'approver'} else 403)
    h.command(role, 'department.settings', department_settings(h), h.seed['department_id'],
        h.get('admin', '/api/me')['organisation']['revision'], status=200 if role == 'admin' else 403)


def test_home_shows_own_rule_and_rota_only_with_current_self_review_permission(h, monkeypatch):
    assert_current_self_review_permission(h, monkeypatch)


@pytest.mark.postgres
def test_home_shows_own_rule_and_rota_only_with_current_self_review_permission_postgres(pg, monkeypatch):
    assert_current_self_review_permission(pg, monkeypatch)


def assert_current_self_review_permission(h, monkeypatch):
    end_day = date(2026, 10, 4)
    with transaction(h.app.state.engine) as db:
        zone = db.get(m.Organisation, h.seed['organisation_id']).timezone
    expires_at = local_midnight(end_day, zone)
    instant = expires_at - timedelta(minutes=1)
    assert instant.date() == expires_at.date()
    freeze_clock(monkeypatch, lambda: instant)

    def counts():
        return {x['key']: x['count'] for x in h.get('solo', '/api/home')['attention']}

    identity(h, 'solo', ['coordinator', 'approver'])
    workspace = h.command('solo', 'workspace.create', {'name': 'Own rota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['workspace']
    workspace = h.command('solo', 'shift.create', {'workspace_id': workspace['id'], 'assignment_type_id': h.seed['type'],
        'location_id': h.seed['locations'][0], 'service_ids': [h.seed['services'][0]],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
        'participants': [{'person_id': h.seed['people'][0], 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}]},
        revision=workspace['revision'])['workspace']
    workspace = h.lifecycle(workspace, 'submit', 'solo')['workspace']
    assert 'reviews' not in counts()
    policy = h.command('solo', 'policy.create', {'name': 'Own rule', 'owner': 'Test', 'source': 'Test fixture',
        'starts_on': '2026-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']

    assert not {'reviews', 'rules'} & counts().keys()
    grant_fields = {'identity_id': h.seed['identities']['solo'], 'starts_on': '2026-01-01',
                    'actions': ['rota.approve', 'rule.activate'], 'reason': 'Sole reviewer cover'}
    instant += timedelta(seconds=1)
    h.command('admin', 'self_approval.grant', {**grant_fields, 'ends_on': end_day.isoformat()})
    stale = h.lifecycle(workspace, 'approve', 'solo', status=409)
    assert stale['error']['code'] == 'STALE_INPUTS'
    current = counts()
    assert current.get('rules') == current.get('resubmit') == 1
    assert 'reviews' not in current

    instant += timedelta(seconds=1)
    workspace = h.lifecycle(workspace, 'reopen', 'solo')['workspace']
    h.lifecycle(workspace, 'submit', 'solo')
    refused = h.command('solo', 'policy.activate', {'reason': 'Routine update'}, policy['id'], policy['revision'], status=422)
    assert refused['error']['code'] == 'SELF_APPROVAL_REASON'
    warm_all(h.app.state.engine)
    current = counts()
    assert current.get('reviews') == current.get('rules') == 1

    # The cached Home entries expire at local midnight without a write.
    instant = expires_at
    assert not {'reviews', 'rules'} & counts().keys()

    instant += timedelta(seconds=1)
    grant = h.command('admin', 'self_approval.grant', {**grant_fields, 'no_end_date': True})['permission']
    warm_all(h.app.state.engine)
    current = counts()
    assert current.get('reviews') == current.get('rules') == 1
    instant += timedelta(seconds=1)
    h.command('admin', 'self_approval.revoke', {'reason': 'Independent reviewer returned'}, grant['id'], grant['revision'])
    assert not {'reviews', 'rules'} & counts().keys()


def policy_row(lines):
    return {'id': str(uuid4()), 'starts_on': '2026-01-01', 'ends_on': None,
            'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': lines}}}


def test_strength_defaults_and_relation_match_shared_resolver():
    broad = line(numbers={'max': {'value': 4, 'level': 'mandatory'}})
    narrow = line(who=person(str(uuid4())), numbers={'max': {'value': 2}})
    rule = {'id': 'duties_per_week'}
    assert default_line_strengths(rule, [broad, narrow])[1]['numbers']['max']['level'] == 'mandatory'
    explicit = {**narrow, 'numbers': {'max': {'value': 2, 'level': 'medium'}}}
    row = policy_row([broad, explicit])
    row['status'] = 'active'
    assert line_relations(row)[narrow['id']]['reason'] == 'strength_differs'
    copies = active_rules([row])
    assert all(not (c['definition']['scope'].get('conditions') or {}).get('conditions') or
               not any(x.get('field') == 'outranked_by' for x in c['definition']['scope']['conditions']['conditions'])
               for c in copies)
    changed = {**explicit, 'where': {'conditions': [{'field': 'service_ids', 'values': [str(uuid4())]}]}}
    assert line_relations(policy_row([broad, changed]))[narrow['id']]['reason'] == 'where_differs'
    row['definition']['rule']['lines'][1]['numbers']['max']['level'] = 'mandatory'
    relation = line_relations(row)[narrow['id']]
    assert relation['overrides'] and relation['line_no'] == 1 and relation['reason'] is None


def test_changed_line_dates_select_only_overlapping_current_rotas_including_removed_dates(h):
    broad = line(numbers={'max': {'value': 4}}, starts_on='2026-09-01', ends_on='2027-01-01')
    current = approve(h, propose(h, 'duties_per_week', [broad])['record'])
    september = h.workspace('September stale', '2026-09-01', '2026-10-01')
    december = h.workspace('December', '2026-12-01', '2027-01-01')
    january = h.workspace('January', '2027-01-01', '2027-02-01')
    extra = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}}, starts_on='2026-12-01', ends_on='2027-01-01')
    pending = propose(h, 'duties_per_week', [broad, extra], policy_id=current['id'], revision=current['revision'], acknowledged=[broad['id']])['record']
    review = h.get('approver', '/api/policies/' + pending['id'] + '/review')
    assert [item['workspace_id'] for item in review['effect']] == [december['id']]
    assert not review['ready'] and review['effect'][0]['status'] == 'not_checked'
    with transaction(h.app.state.engine) as db:
        actor = actor_for(h, db)
        row = db.get(m.Policy, pending['id'])
        row.definition = copy.deepcopy(row.definition)
        row.definition['rule']['lines'][1]['starts_on'] = '2027-01-01'
        row.definition['rule']['lines'][1]['ends_on'] = '2027-02-01'
        assert [w.id for w in affected_workspaces(db, actor, [row])] == [january['id']]


def test_observed_effect_counts_cleared_added_and_no_change_without_solver_claims():
    finding = {'code': 'MIN_REST', 'severity': 'error', 'person_id': 'person', 'rule_id': 'old',
               'detail': {'required_hours': 24, 'actual_hours': 16}}
    before = {'findings': [finding]}
    assert observed_effect(before, {'findings': []}) == {'status': 'ready', 'clears': 1, 'adds': 0,
        'effect': 'clears', 'summary': 'Clears 1 problem',
        'mandatory': {'clears': 1, 'adds': 0}, 'advisory': {'clears': 0, 'adds': 0}, 'recited': []}
    added = observed_effect({'findings': []}, before)
    assert added['adds'] == 1 and added['summary'] == 'Adds 1 problem'
    assert observed_effect(before, {'findings': [{**finding, 'rule_id': 'new'}]})['effect'] == 'no_change'
    assert observed_effect(before, {**before, 'quota_report': {'complete': False}})['status'] == 'unresolved'


def binding_fixture(h):
    broad = line(numbers={'max': {'value': 1, 'level': 'mandatory'}})
    current = approve(h, propose(h, 'duties_per_week', [broad])['record'])
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    return broad, current, workspace


def test_binding_relax_seed_preserves_strength_where_settings_and_requires_a_real_blocker(h):
    broad, current, workspace = binding_fixture(h)
    value = {'workspace_id': workspace['id'], 'person_id': h.seed['people'][0],
             'binding_line_id': broad['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    response = h.client('coordinator').post(f"/api/policies/{current['id']}/relax", json=value)
    assert response.status_code == 200, response.text
    seed = response.json()
    assert seed['binding_line_no'] == 1 and not seed['needs_approval'] and seed['scope'] == 'rota'
    for key in ('where', 'settings', 'numbers', 'weekdays', 'start_time', 'end_time'):
        assert seed['line'][key] == current['definition']['rule']['lines'][0][key]
    assert seed['line']['who']['conditions'][0]['values'] == [h.seed['people'][0]]
    assert seed['line']['starts_on'] == value['starts_on'] and seed['line']['ends_on'] == value['ends_on']
    invalid_slot = h.client('coordinator').post(f"/api/policies/{current['id']}/relax", json={**value, 'number': 'apply'})
    assert invalid_slot.status_code == 409 and invalid_slot.json()['error']['code'] == 'BINDING_LINE_REQUIRED'
    assert h.client('coordinator').post(f"/api/policies/{current['id']}/relax", json={**value, 'person_id': h.seed['people'][1]}).status_code == 409
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
        if role not in ('admin', 'coordinator'):
            assert h.client(role).post(f"/api/policies/{current['id']}/relax", json=value).status_code == 403


def test_safety_rule_never_produces_relax_seed(h):
    broad = line(numbers={'min': {'value': 1}})
    current = approve(h, propose(h, 'people_per_session', [broad])['record'])
    workspace = h.workspace()
    response = h.client('coordinator').post(f"/api/policies/{current['id']}/relax", json={
        'workspace_id': workspace['id'], 'person_id': h.seed['people'][0], 'binding_line_id': broad['id'],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})
    assert response.status_code == 422 and response.json()['error']['code'] == 'SAFETY_EXCEPTION'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('bulk', [False, True], ids=['direct', 'bulk'])
def test_known_line_effect_approves_and_activates_in_one_command_without_checkbox(request, database, bulk):
    from apps.worker.main import process_one
    from .test_contextual_feasibility import use_checked_activation
    h = request.getfixturevalue(database)
    broad, current, workspace = binding_fixture(h)
    extra = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}},
        starts_on='2026-10-04', ends_on='2026-10-11')
    pending = propose(h, 'duties_per_week', [broad, extra], policy_id=current['id'], revision=current['revision'],
        acknowledged=[broad['id']])['record']
    use_checked_activation(h)
    h.clients.clear()
    refused = h.command('approver', 'policy.activate', {'reason': 'Missing receipt cannot approve'},
        pending['id'], pending['revision'], status=409)
    assert refused['error']['code'] == 'RULE_CHECK_REQUIRED'
    job = h.command('approver', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]}, revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['effect']['status'] == 'ready' and result['effect']['clears'] == 1, result['effect']
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert review['ready'] and review['effect'][0]['clears'] == 1
    for change, status in (({k: v for k, v in result.items() if k != 'effect'}, 'unresolved'),
                           ({**result, 'bindings': [*result['bindings'], {'policy_id': str(uuid4())}]}, 'not_checked')):
        with transaction(h.app.state.engine) as db:
            db.get(m.Job, job['id']).result = change
        review = h.get('approver', f"/api/policies/{pending['id']}/review")
        assert not review['ready'] and review['effect'][0]['status'] == status
    with transaction(h.app.state.engine) as db:
        db.get(m.Job, job['id']).result = result
    decision = {'check_job_ids': [job['id']]}
    reason = 'Reviewed concrete saved-assignment effect'
    if bulk:
        result_batch = h.command('approver', 'bulk.apply', {'reason': reason, 'items': [{
            'item_kind': 'policy.activate', 'target_id': pending['id'],
            'expected_revision': pending['revision'], 'payload': decision}]})
        activated = result_batch['items'][0]['results'][0]
    else:
        activated = h.command('approver', 'policy.activate', {**decision, 'reason': reason},
            pending['id'], pending['revision'])
    assert activated['record']['status'] == 'active' and not activated['unresolved_acknowledged']
    assert activated['context_check_evidence'][0]['effect'] == result['effect']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current['id']).status == 'retired'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_retired_history_never_consumes_live_catalogue_limit_and_remains_pageable(request, database):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Policy), [{'id': str(uuid4()), 'department_id': h.seed['department_id'], 'kind': 'min_rest',
            'status': 'retired', 'active': False, 'name': f'History {n}', 'owner': 'Test', 'source': 'Retained history',
            'starts_on': date(2020, 1, 1), 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}},
            'created_by': h.seed['identities']['coordinator']} for n in range(4001)])
    assert all(p['status'] != 'retired' for p in h.get('coordinator', '/api/catalogues?include=policies')['policies'])
    first = h.get('auditor', '/api/policies?limit=4000')
    second = h.get('auditor', '/api/policies?limit=4000&offset=4000')
    assert first['has_more'] and not second['has_more']
    assert len(first['policies']) + len(second['policies']) == 4001
    assert not {p['id'] for p in first['policies']} & {p['id'] for p in second['policies']}


def test_picker_source_excludes_inactive_without_hiding_configuration_history(h):
    with transaction(h.app.state.engine, write=True) as db:
        for cls, ident in ((m.Service, h.seed['services'][1]), (m.Location, h.seed['locations'][1]), (m.AssignmentType, h.seed['type'])):
            db.get(cls, ident).active = False
    path = '/api/catalogues?include=services&include=locations&include=assignment_types'
    choices = h.get('coordinator', path + '&active_only=true')
    all_rows = h.get('coordinator', path)
    for kind in ('services', 'locations', 'assignment_types'):
        assert choices['inactive_count'][kind] == 1
        assert all(row['active'] for row in choices[kind])
        assert len(all_rows[kind]) == len(choices[kind]) + 1


def test_line_preview_uses_future_memberships_and_own_dates(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(m.Membership.__table__.update().where(m.Membership.person_id == h.seed['people'][0]).values(starts_on=date(2026, 12, 1)))
    lines = [line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}}, starts_on='2026-12-01', ends_on='2027-01-01')]
    checked = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={'lines': lines})
    assert checked.status_code == 200, checked.text
    assert any(p['person_id'] == h.seed['people'][0] for item in checked.json()['reach_details']['lines'] for p in item['people'])


def test_omitted_new_line_strength_uses_matching_broader_people_and_unsaved_effect_scope(h):
    second_category = h.configure('categories', {'name': 'Different rank', 'starts_on': '2026-01-01'})['record']['id']
    broad = line(who={'conditions': [{'field': 'category_ids', 'values': [second_category]}]},
                 numbers={'max': {'value': 4, 'level': 'high'}})
    matching = line(numbers={'max': {'value': 3, 'level': 'mandatory'}})
    person_line = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 2}},
                       starts_on='2026-12-01', ends_on='2027-01-01')
    december = h.workspace('December only', '2026-12-01', '2027-01-01')
    h.workspace('Unrelated September', '2026-09-01', '2026-10-01')
    current = approve(h, propose(h, 'duties_per_week', [broad, matching])['record'])
    checked = h.client('coordinator').post('/api/rules/catalogue/duties_per_week/check', json={
        'lines': [broad, matching, person_line], 'include_affected_drafts': True})
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body['lines'][-1]['numbers']['max']['level'] == 'mandatory'
    assert body['relations'][person_line['id']]['line_id'] == matching['id']
    assert [w['workspace_id'] for w in body['affected_drafts']] == [december['id']]
    saved = propose(h, 'duties_per_week', [broad, matching, person_line], policy_id=current['id'],
        revision=current['revision'], acknowledged=[matching['id']])['record']
    assert saved['definition']['rule']['lines'][-1]['numbers']['max']['level'] == 'mandatory'


def test_line_effect_is_known_after_unknown_search_without_claiming_feasibility(monkeypatch):
    from packages.domain import feasibility
    data = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {},
        'policies': [{'id': 'line-policy', 'definition': {'rule': {'kind': 'catalogue'}}}]}
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'unknown_time_limit'})
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': []})
    result = feasibility.check({'baseline': data, 'proposed': copy.deepcopy(data),
        'bindings': [{'policy_id': 'line-policy'}], 'fingerprint': 'current', 'check_seconds': 10})
    assert result['effect'] == {'status': 'ready', 'clears': 0, 'adds': 0, 'effect': 'no_change', 'summary': 'No change',
        'mandatory': {'clears': 0, 'adds': 0}, 'advisory': {'clears': 0, 'adds': 0}, 'recited': []}
    assert result['status'] == 'inconclusive' and not result['full_feasibility_established']
    assert not result['comparison_complete']


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_wish_priority_configuration_is_versioned_audited_and_undoable(request, database):
    h = request.getfixturevalue(database)
    before = h.get('admin', '/api/me')
    publications = {month: h.publish(h.shift(h.workspace(f'Ledger {month}', f'2026-{month:02}-01', f'2026-{month+1:02}-01'),
        start=f'2026-{month:02}-01T08:00:00+03:00', end=f'2026-{month:02}-01T12:00:00+03:00')['workspace'])['publication_id']
        for month in (7, 8, 9)}
    saved = save_settings(h, {}, generation_defaults={'scoring': {
        'wish_priority_decay': 0.8, 'wish_granted_credit': 1, 'wish_history_periods': 2}})
    with transaction(h.app.state.engine, write=True) as db:
        for month, metric, value in ((9, 'wishes_declined', 10), (8, 'wishes_declined', 10), (8, 'wishes_granted', 5), (7, 'wishes_declined', 100)):
            db.add(m.WishLedger(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                period_start=date(2026, month, 1), period_end=date(2026, month + 1, 1), metric=metric,
                value=value, publication_id=publications[month]))
        db.flush()
        settings = s.ScoringSettings(wish_priority_decay=0.8, wish_granted_credit=1, wish_history_periods=2)
        assert multipliers(db, h.seed['department_id'], [h.seed['people'][0]], settings, date(2026, 10, 1))[h.seed['people'][0]] == 2.4
    undone = h.command('admin', 'department.settings', {**saved['previous'], 'reason': 'Undo catch-up policy'},
        h.seed['department_id'], saved['organisation']['revision'])
    assert s.GenerationOptions.model_validate(undone['department']['generation_defaults']) == s.GenerationOptions.model_validate(
        before['department']['generation_defaults'])
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'department_settings').order_by(m.DefinitionHistory.object_revision)))
        assert versions[0].snapshot['generation_defaults']['scoring']['wish_priority_decay'] == 0.8


def test_legacy_unsupported_wish_approval_cannot_silently_schedule_or_accept_new_wishes(h):
    wish = h.command('staff', 'wish.create', payload(h))['wish']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).wish_settings = {'types': {'prefer_off': {'approval': 'always'}}}
    assert h.snapshot(h.workspace())['wishes'] == []
    assert h.command('staff', 'wish.create', payload(h, day='2026-10-06'), status=422)['error']['code'] == 'WISH_APPROVAL_UNSUPPORTED'
    save_settings(h, {})
    assert h.snapshot(h.workspace())['wishes'][0]['id'] == wish['id']
