"""D16 extends the existing rota adjustment path; permanent rules never change."""
from tests.historical_staffing import retained_staffing
import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.planning import adjusted_rules
from apps.api.services.rota_adjustments import adjustment_review_items, remove_rejected_adjustments
from apps.worker.main import claim_job, prepare_deferred_generation
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules, scope_applies
from packages.domain.rule_map import weakens_safety
from packages.domain.time import aware
from .governance_support import identity
from .test_be5_governance import binding_fixture
from .test_rule_catalogue_api import approve, line, propose
from .test_catalogue_preview import state
from tests.legacy_lead import historical_type


def scoped_payload(policy, binding, person, **more):
    return {'policy_id': policy['id'], 'line_id': binding['id'], 'number': 'max', 'person_id': person,
            'mode': 'set', 'amount': 2, 'reason': 'Agreed workload for this rota only', **more}


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_scoped_adjustment_applies_immediately_only_to_person_line_and_dates_and_is_undoable(request, database):
    h = request.getfixturevalue(database)
    binding, policy, rota = binding_fixture(h)
    before = h.snapshot(rota)
    value = scoped_payload(policy, binding, h.seed['people'][0])
    result = h.command('coordinator', 'workspace.rule_adjust', value, rota['id'], rota['revision'])
    rota = result['workspace']
    stored = rota['rule_adjustments'][0]
    assert stored['id'] and stored['reason'] == value['reason']
    assert stored['starts_on'] == rota['starts_on'] and stored['ends_on'] == rota['ends_on']
    assert not [f for f in result['validation']['findings'] if f.get('rule_id') == policy['id']]
    snapshot = h.snapshot(rota)
    resolved = active_rules(snapshot['policies'], context=snapshot)
    for pid, expected in ((h.seed['people'][0], 2), (h.seed['people'][1], 1)):
        a, b = aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00')
        matches = [p for p in resolved if p['id'] == policy['id'] and p['starts_on'] <= '2026-10-04'
                   and (not p.get('ends_on') or '2026-10-04' < p['ends_on']) and
                   scope_applies(snapshot, p['definition']['scope'], pid, snapshot['shifts'][0], a, b)]
        assert [p['definition']['rule']['maximum'] for p in matches] == [expected]
    mapped = h.get('coordinator', f"/api/workspaces/{rota['id']}/rule-map")
    entry = next(row for row in mapped['rules'] if row['id'] == policy['id'])
    assert entry['scoped_adjustments'][0]['reason'] == value['reason']
    assert entry['lines'][0]['id'] == binding['id']
    job = h.command('coordinator', 'job.create', {'workspace_id': rota['id'], 'kind': 'generation'}, revision=rota['revision'])['job']
    with transaction(h.app.state.engine) as db:
        # 18b89db3e deliberately defers the full generation graph until worker lease;
        # verify the adjustment identity after that bounded preparation step.
        assert db.get(m.Job, job['id']).input_snapshot['_deferred_generation']
        assert 'policies' not in db.get(m.Job, job['id']).input_snapshot
        claim = claim_job(db)
        assert claim and claim['id'] == job['id']
    prepare_deferred_generation(h.app.state.engine, claim, h.settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Job, job['id']).input_snapshot['policies'] == snapshot['policies']
        assert db.get(m.Policy, policy['id']).definition == policy['definition']
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == 'workspace.rule_adjust', m.AuditEvent.target_id == rota['id']))
        assert event.reason == value['reason']
    undone = h.command('coordinator', 'workspace.undo', {'reason': 'Undo scoped workload'}, rota['id'], rota['revision'])['workspace']
    assert not undone['rule_adjustments'] and h.snapshot(undone)['policies'] == before['policies']


def test_multiple_scopes_coexist_replace_by_scope_and_clear_only_the_selected_scope(h):
    binding, policy, rota = binding_fixture(h)
    first = scoped_payload(policy, binding, h.seed['people'][0], starts_on='2026-10-04', ends_on='2026-10-06')
    second = scoped_payload(policy, binding, h.seed['people'][0], starts_on='2026-10-06', ends_on='2026-10-11', amount=3)
    third = scoped_payload(policy, binding, h.seed['people'][1], amount=4)
    for value in (first, second, third):
        rota = h.command('coordinator', 'workspace.rule_adjust', value, rota['id'], rota['revision'])['workspace']
    original_id = rota['rule_adjustments'][0]['id']
    rota = h.command('coordinator', 'workspace.rule_adjust', {**first, 'amount': 5}, rota['id'], rota['revision'])['workspace']
    assert len(rota['rule_adjustments']) == 3 and rota['rule_adjustments'][-1]['id'] == original_id
    refused = h.command('coordinator', 'workspace.rule_adjust', {**first, 'ends_on': '2026-10-07'}, rota['id'], rota['revision'], status=409)
    assert refused['error']['code'] == 'ADJUSTMENT_OVERLAP'
    rota = h.command('coordinator', 'workspace.rule_adjust', {**first, 'mode': 'clear', 'amount': None}, rota['id'], rota['revision'])['workspace']
    assert len(rota['rule_adjustments']) == 2 and original_id not in {r['id'] for r in rota['rule_adjustments']}


def test_projection_retains_source_strength_precedence_and_dates_without_changing_other_slots():
    person_id, policy_id = str(uuid4()), str(uuid4())
    binding = line(numbers={'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 4, 'level': 'high'}},
                   starts_on='2026-10-01', ends_on='2026-11-01')
    policy = {'id': policy_id, 'status': 'active', 'starts_on': '2026-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [binding]}}}
    # This catalogue has only max; use hours, whose min/max slots both exist.
    policy['definition']['rule']['id'] = 'hours_per_week'
    entry = {**scoped_payload(policy, binding, person_id), 'id': str(uuid4()), 'starts_on': '2026-10-10', 'ends_on': '2026-10-20'}
    original = copy.deepcopy(policy)
    result = adjusted_rules(policy, [entry])
    assert policy == original
    copies = active_rules([result])
    maximum = [p for p in copies if (p.get('template_line') or {}).get('slot') == 'max']
    assert maximum and all(p['definition']['severity'] == 'advisory' and p['definition']['strength'] == 'high' for p in maximum)
    assert all(p['template_line']['id'] == binding['id'] and p['template_line']['line'] == 0 for p in copies)
    assert any(p['starts_on'] == '2026-10-10' and p['ends_on'] == '2026-10-20' for p in maximum)
    assert any(p['ends_on'] == '2026-10-10' for p in maximum)
    assert any(p['starts_on'] == '2026-10-20' for p in maximum)


def test_shared_rejection_helpers_preserve_other_rows_and_support_legacy_ids():
    rota = SimpleNamespace(id=str(uuid4()), rule_adjustments=[{'policy_id': str(uuid4()), 'reason': 'First'},
        {'id': str(uuid4()), 'policy_id': str(uuid4()), 'reason': 'Second'}],
        target_overrides=[{'policy_id': str(uuid4()), 'person_id': str(uuid4()), 'reason': 'Target'}])
    items = adjustment_review_items(rota)
    assert items == adjustment_review_items(rota) and len({i['id'] for i in items}) == 3
    items[0]['source']['reason'] = 'Changed copy'
    assert rota.rule_adjustments[0]['reason'] == 'First'
    assert set(remove_rejected_adjustments(rota, [items[0]['id'], items[2]['id'], str(uuid4())])) == {items[0]['id'], items[2]['id']}
    assert len(rota.rule_adjustments) == 1 and rota.rule_adjustments[0]['reason'] == 'Second' and not rota.target_overrides


def test_department_scale_person_projection_stays_disjoint_with_bounded_conditions():
    people = [str(uuid4()) for _ in range(150)]
    binding = line(numbers={'max': {'value': 1, 'level': 'high'}}, starts_on='2026-10-01', ends_on='2026-11-01')
    policy = {'id': str(uuid4()), 'status': 'active', 'starts_on': '2026-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'catalogue', 'id': 'duties_per_week', 'lines': [binding]}}}
    entries = [{**scoped_payload(policy, binding, pid), 'id': str(uuid4()),
                'starts_on': '2026-10-01', 'ends_on': '2026-11-01'} for pid in people]
    result = adjusted_rules(policy, entries)
    copies = active_rules([result])
    assert len(result['definition']['rule']['lines']) == len(copies) == len(people) + 1
    assert all(p['template_line']['id'] == binding['id'] and p['template_line']['line'] == 0
               and p['definition']['strength'] == 'high' for p in copies)
    def depth(tree):
        return 1 + max((depth(row) for row in (tree or {}).get('conditions', [])), default=0)
    assert max(depth(row.get('who')) for row in result['definition']['rule']['lines']) <= 3
    a, b = aware('2026-10-04T08:00:00+03:00'), aware('2026-10-04T12:00:00+03:00')
    for pid, maximum in ((people[0], 2), (people[-1], 2), (str(uuid4()), 1)):
        assert [p['definition']['rule']['maximum'] for p in copies
                if scope_applies({'organisation': {'timezone': 'Asia/Riyadh'}}, p['definition']['scope'], pid, None, a, b)] == [maximum]


def preview(h, rota, adjustment, status=200, **more):
    response = h.client('coordinator').post(f"/api/workspaces/{rota['id']}/adjustment-effect", json={
        'expected_revision': rota['revision'], 'adjustment': adjustment, **more})
    assert response.status_code == status, response.text
    return response.json()


def test_effect_projection_uses_owned_fix_list_hook_without_inventing_a_delta(monkeypatch):
    from apps.api.services.rule_effects import effect_projection
    from packages.domain import rota_summary
    data, validation = {'focus_ids': [], 'shifts': []}, {'findings': []}
    monkeypatch.delattr(rota_summary, 'fix_list_effect_projection', raising=False)
    assert 'fix_list_delta' not in effect_projection(data, validation, data, validation)
    calls = []
    projected = {'before': {'validation': validation, 'summary': {}, 'fix_list': {'causes': [{'id': 'before'}]}},
                 'after': {'validation': validation, 'summary': {}, 'fix_list': {'causes': [{'id': 'after'}]}},
                 'fix_list_delta': {'cleared': ['before'], 'new': ['after']}}
    def hook(*args):
        calls.append(args)
        return projected
    monkeypatch.setattr(rota_summary, 'fix_list_effect_projection', hook, raising=False)
    result = effect_projection(data, validation, data, validation)
    assert calls == [(data, validation, data, validation)]
    assert {key: result[key] for key in projected} == projected
    assert result['cleared_checks'] == result['new_must_checks'] == 0 and result['effect']['status'] == 'ready'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_adjustment_effect_is_read_only_and_matches_command_then_replacement_and_clear(request, database):
    h = request.getfixturevalue(database)
    binding, policy, rota = binding_fixture(h)
    value = scoped_payload(policy, binding, h.seed['people'][0])
    stored, detail = state(h), h.detail(rota)
    effect = preview(h, rota, value)
    assert effect['preview'] and not effect['committed'] and effect['cleared_checks'] == 1
    assert effect['new_must_checks'] == 0 and effect['effect']['status'] == 'ready'
    assert state(h) == stored and h.detail(rota) == detail
    assert effect['fingerprint'] == h.snapshot(rota)['fingerprint']
    saved = h.command('coordinator', 'workspace.rule_adjust', value, rota['id'], rota['revision'])
    rota = saved['workspace']
    assert saved['validation']['findings'] == effect['after']['validation']['findings']
    replaced = preview(h, rota, {**value, 'amount': 3})
    assert replaced['before']['validation']['findings'] == saved['validation']['findings']
    assert replaced['effect']['effect'] == 'no_change'
    clear = preview(h, rota, {**value, 'mode': 'clear', 'amount': None})
    assert clear['new_must_checks'] == 1 and clear['cleared_checks'] == 0
    assert h.detail(rota)['workspace']['rule_adjustments'] == rota['rule_adjustments']
    preview(h, {**rota, 'revision': rota['revision'] - 1}, value, status=409)
    h.command('coordinator', 'workspace.archive', {'reason': 'Discard preview fixture'}, rota['id'], rota['revision'])
    preview(h, {**rota, 'revision': rota['revision'] + 1}, value, status=409)


def test_cap_fit_uses_eligibility_current_absence_and_joint_overlap_without_saving(h):
    binding, policy, rota = binding_fixture(h)
    for _ in range(2):
        rota = h.shift(rota, start='2026-10-06T08:00:00+03:00', end='2026-10-06T12:00:00+03:00', people=[])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][2], 'kind': 'actual_absence',
        'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00',
        'private_reason': 'Private absence must never appear in ranked fit'})
    value = scoped_payload(policy, binding, h.seed['people'][0], amount=3)
    stored, detail = state(h), h.detail(rota)
    result = preview(h, rota, value, fit_person_ids=h.seed['people'])
    fit = result['ranked_fit']
    assert fit['complete'] and not fit['optimal']
    assert {row['person_id'] for row in fit['candidates']} == set(h.seed['people'][:2])
    assert all(row['eligible_open_places'] == len(row['shift_ids']) == 1 and row['new_must_checks'] == 0
               and row['estimated'] for row in fit['candidates'])
    assert fit['candidates'][0]['person_id'] == h.seed['people'][0]
    assert fit['excluded'] == [{'person_id': h.seed['people'][2], 'code': 'NO_ELIGIBLE_OPEN_PLACES'}]
    assert result['after']['summary']['places_open'] == result['before']['summary']['places_open'] == 2
    assert state(h) == stored and h.detail(rota) == detail
    assert 'Private absence' not in str(result)
    preview(h, rota, value, status=422, fit_person_ids=[h.seed['people'][0]] * 2)
    preview(h, rota, value, status=404, fit_person_ids=[str(uuid4())])
    preview(h, rota, {**value, 'mode': 'clear', 'amount': None}, status=422, fit_person_ids=h.seed['people'])


def test_cap_fit_excludes_full_lead_only_sessions_from_open_place_ranking(h):
    binding, policy, rota = binding_fixture(h)
    typ = historical_type(h, {'name': 'Lead-only ranked fit', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'maximum': 2}],
                       'lead': {'minimum': 1, 'maximum': 1,
                                'capability_ids': [h.seed['supervisor_capability']], 'category_ids': []}}})['id']
    full = retained_staffing(h, rota, start='2026-10-07T08:00:00+03:00', end='2026-10-07T12:00:00+03:00',
                   typ=typ, people=h.seed['people'][1:])['workspace']
    lead_shift_id = h.detail(full)['shifts'][-1]['id']
    result = preview(h, full, scoped_payload(policy, binding, h.seed['people'][0], amount=3),
                     fit_person_ids=h.seed['people'])
    fit = result['ranked_fit']
    assert all(lead_shift_id not in row['shift_ids'] for row in fit['candidates'])


def test_mixed_catalogue_classifies_only_the_binding_slot_and_keeps_safety_lines():
    binding = line(numbers={'max': {'value': 1}}, starts_on='2026-10-01', ends_on='2026-11-01')
    protected = line(numbers={'apply': {}}, starts_on='2026-10-01', ends_on='2026-11-01')
    policy = {'id': str(uuid4()), 'status': 'active', 'starts_on': '2026-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
                                     'lines': [binding, protected]}}}
    entry = {**scoped_payload(policy, binding, str(uuid4())), 'id': str(uuid4()),
             'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    assert not weakens_safety(policy, entry)
    result = adjusted_rules(policy, [entry])
    assert any(p['definition']['rule']['kind'] == 'restriction' for p in active_rules([result]))
    unsafe = {**entry, 'line_id': protected['id'], 'number': 'apply', 'mode': 'off'}
    assert weakens_safety(policy, unsafe) and adjusted_rules(policy, [unsafe]) == policy


def test_scopes_cannot_silently_override_whole_rule_pause_or_overlapping_everyone_scope(h):
    binding, policy, rota = binding_fixture(h)
    value = scoped_payload(policy, binding, h.seed['people'][0])
    pause = {'policy_id': policy['id'], 'mode': 'off', 'reason': 'Pause whole rule'}
    rota = h.command('coordinator', 'workspace.rule_adjust', pause, rota['id'], rota['revision'])['workspace']
    h.command('coordinator', 'workspace.rule_adjust', value, rota['id'], rota['revision'], status=409)
    rota = h.command('coordinator', 'workspace.rule_adjust', {**pause, 'mode': 'clear'}, rota['id'], rota['revision'])['workspace']
    rota = h.command('coordinator', 'workspace.rule_adjust', value, rota['id'], rota['revision'])['workspace']
    h.command('coordinator', 'workspace.rule_adjust', pause, rota['id'], rota['revision'], status=409)
    h.command('coordinator', 'workspace.rule_adjust', {**value, 'person_id': None}, rota['id'], rota['revision'], status=409)


def test_scoped_commands_refuse_safety_invalid_scope_and_unprivileged_roles(h):
    binding, policy, rota = binding_fixture(h)
    value = scoped_payload(policy, binding, h.seed['people'][0])
    for role in s.ROLE_NAMES:
        if role not in h.seed['identities']:
            identity(h, role, [role])
        if role not in ('admin', 'coordinator'):
            h.command(role, 'workspace.rule_adjust', value, rota['id'], rota['revision'], status=403)
        response = h.client(role).post(f"/api/workspaces/{rota['id']}/adjustment-effect", json={
            'expected_revision': rota['revision'], 'adjustment': value})
        assert response.status_code == (200 if role in ('admin', 'coordinator') else 403), response.text
    for extra in ({'line_id': str(uuid4())}, {'starts_on': '2026-10-01', 'ends_on': '2026-10-11'},
                  {'starts_on': '2026-10-06'}, {'number': 'apply'}, {'reason': ''}):
        h.command('coordinator', 'workspace.rule_adjust', {**value, **extra}, rota['id'], rota['revision'], status=422)
    safety_line = line(numbers={'min': {'value': 12}})
    safety = approve(h, propose(h, 'rest_between_duties', [safety_line])['record'])
    refused = h.command('coordinator', 'workspace.rule_adjust', {**value, 'policy_id': safety['id'],
        'line_id': safety_line['id'], 'number': 'min', 'amount': 0}, rota['id'], rota['revision'], status=409)
    assert refused['error']['code'] == 'SAFETY_RULE'
    rejected = preview(h, rota, {**value, 'policy_id': safety['id'], 'line_id': safety_line['id'],
        'number': 'min', 'amount': 0}, status=409)
    assert rejected['error']['code'] == 'SAFETY_RULE'


@pytest.mark.parametrize('field,value', [('wish_priority_decay', 0.8), ('wish_granted_credit', 1), ('wish_history_periods', 2)])
def test_wish_priority_policy_cannot_be_overridden_for_a_generation_run(h, field, value):
    rota = h.shift(h.workspace())['workspace']
    refused = h.command('coordinator', 'job.create', {'workspace_id': rota['id'], 'kind': 'generation',
        'generation_options': {'scoring': {field: value}}}, revision=rota['revision'], status=422)
    assert refused['error']['code'] == 'DEPARTMENT_OPTIONS_ONLY'
