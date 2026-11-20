"""Accepted rota targets follow the evaluated rule and expose genuine content drift."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import job_result_projection
from packages.domain import cp_sat, generation
from packages.domain.conditions import INHERITED_LEVEL
from packages.domain.quotas import evaluate_quotas
from packages.domain.score import schedule_score
from packages.domain.validation import ancestor_ids, evaluate, exception_for
from .test_finding_causes import cause_data, cause_snapshot, type_definition
from .test_quota_working_day_proration import add_quota, checked_row, working_data
from .test_rule_catalogue_v2_api import proposed
from .test_rule_lines import line, person as person_scope, rule as catalogue_rule
from .test_rule_lines_inherit import n
from .test_self_approval import rota_review_access
from .test_tags import members


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_live_target_content_change_is_stale_without_replacing_the_accepted_amount(h):
    person = h.seed['people'][0]
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
        {'person_id': person}, severity='advisory')
    workspace = h.shift(h.workspace())['workspace']
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])

    def change_target(amount):
        with transaction(h.app.state.engine) as db:
            fact = next(row for row in db.scalars(select(m.SchedulingFact).where(
                m.SchedulingFact.person_id == person, m.SchedulingFact.status == 'active'))
                if any(pin['policy_id'] == source['id']
                       for pin in row.data['line'].get('source_bindings', {}).values()))
            payload = {'kind': fact.kind, 'person_id': person, 'replaces_id': fact.id,
                       'expected_revision': fact.revision, 'reason': 'Revise the configured target', **deepcopy(fact.data)}
        payload.pop('exception_allowed', None)
        payload['line']['numbers']['target']['value'] = amount
        revised = h.command('coordinator', 'person.workload.propose', payload)['fact']
        h.command('approver', 'person.workload.activate', {'reason': 'Independent workload review'},
                  revised['id'], revised['revision'])

    change_target(20)
    payload = {'policy_id': source['id'], 'person_id': person, 'amount': 32 / 7,
               'reason': 'Accept the working-window target'}
    accepted = h.command('coordinator', 'workspace.target_override', payload, workspace['id'], workspace['revision'])
    workspace = accepted['workspace']
    [override] = workspace['target_overrides']
    assert override['policy_revision'] == source['revision']
    assert override['amount'] == 4.57
    assert not any(f['code'] == 'TARGET_OVERRIDE_STALE' for f in accepted['validation']['findings'])

    change_target(24)
    checked = h.lifecycle(workspace, 'validate')['validation']
    target = next(row for row in checked['quota_report']['rows'] if row['policy_id'] == source['id'])
    assert target['policy_revision'] == override['policy_revision']  # Content drift under the same captured carrier.
    assert any(f['code'] == 'TARGET_OVERRIDE_STALE' and f['rule_id'] == source['id'] for f in checked['findings'])
    assert not checked['valid'] and not checked['quota_report']['complete']
    assert (target['amount'], target['status']) == (4.57, 'unresolved')
    assert target['target_calculation']['override_amount'] == 4.57
    assert target['target_calculation']['override_stale'] is True and 'proration' not in target
    assert not any(goal['policy_id'] == source['id'] for goal in checked['goals'])
    data = h.snapshot(workspace)
    data['workspace']['target_overrides'][0].pop('rule_fingerprints')
    legacy = evaluate(data)
    legacy_row = next(row for row in legacy['quota_report']['rows'] if row['policy_id'] == source['id'])
    assert legacy_row['policy_revision'] == override['policy_revision']
    assert (legacy_row['amount'], legacy_row['status']) == (4.57, 'unresolved')
    assert not legacy['quota_report']['complete']
    assert any(f['code'] == 'TARGET_OVERRIDE_STALE' and f['detail']['adjustment_id'] == override['id']
               for f in legacy['findings'])
    workspace = h.get('coordinator', '/api/workspaces/' + workspace['id'])['workspace']
    renewed = h.command('coordinator', 'workspace.target_override', payload, workspace['id'], workspace['revision'])
    assert not any(f['code'] == 'TARGET_OVERRIDE_STALE' for f in renewed['validation']['findings'])
    assert renewed['workspace']['target_overrides'][0]['rule_fingerprints'] != override['rule_fingerprints']


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('sole_owner', [False, True], ids=['independent', 'sole-owner'])
def test_legacy_target_reconfirmation_uses_rota_review(h, sole_owner):
    person = h.seed['people'][0]
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
                       'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
                      {'person_id': person}, severity='advisory')
    permission = rota_review_access(h, permission_state='active') if sole_owner else None
    operator = 'coordinator' if sole_owner else 'admin'
    reviewer = operator if sole_owner else 'approver'
    workspace = h.command(operator, 'workspace.create', {
        'name': 'Legacy workload draft', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
    })['workspace']
    workspace = h.shift(workspace, role=operator)['workspace']
    target = {'policy_id': source['id'], 'person_id': person, 'amount': 6}
    draft = h.command(operator, 'workspace.target_override', {**target, 'reason': 'Agreed workload'},
                      workspace['id'], workspace['revision'])['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        stored = db.get(m.Workspace, draft['id'])
        person_name = db.get(m.Person, person).display_name
        [legacy] = stored.target_overrides
        adjustment_id = legacy['id']
        stored.target_overrides = [{key: value for key, value in legacy.items() if key != 'rule_fingerprints'}]

    checked = h.lifecycle(draft, 'validate', role=operator)['validation']
    [stale] = [f for f in checked['findings'] if f['code'] == 'TARGET_OVERRIDE_STALE']
    assert (stale['rule_id'], stale['person_id']) == (source['id'], person)
    assert person_name in stale['message'] and source['name'] in stale['message']
    assert adjustment_id not in stale['message']
    detail = stale['detail']
    assert detail == {'adjustment_id': adjustment_id, 'person_name': person_name, 'rule_name': source['name'],
                      'amount': target['amount'], 'reconfirm_command': 'workspace.target_override'}
    [stale_row] = [r for r in checked['quota_report']['rows'] if r['policy_id'] == source['id']]
    assert stale_row['target_calculation']['override_stale'] is True

    path = f"/api/workspaces/{draft['id']}"
    fix_list = h.get(operator, path + '/fix-list')
    [evidence] = [row for cause in fix_list['causes'] for row in cause['evidence']
                  if row['code'] == 'TARGET_OVERRIDE_STALE']
    assert evidence['detail'] == detail

    current = h.get(operator, path)['workspace']
    reconfirm_reason = 'Reconfirm the agreed workload against the current rule'
    refreshed = h.command(operator, detail['reconfirm_command'], {
        'policy_id': stale['rule_id'], 'person_id': stale['person_id'],
        'amount': detail['amount'], 'reason': reconfirm_reason,
    }, current['id'], current['revision'])['workspace']
    [adjustment] = refreshed['target_overrides']
    assert adjustment['id'] == adjustment_id and adjustment['rule_fingerprints']
    submission = h.lifecycle(refreshed, 'submit', role=operator)
    [item] = submission['review_items']
    assert (item['kind'], item['id']) == ('target_override', adjustment_id)
    approval_reason = 'Verified the workload target against the current rule'
    approved = h.command(reviewer, 'workspace.approve', {'reason': approval_reason,
        'decisions': [{'item_id': item['id'], 'kind': 'adjustment', 'accept': True}]},
        draft['id'], submission['workspace']['revision'])['workspace']

    assert approved['status'] == 'approved'
    verified = h.get(operator, path)['workspace']['validation']
    assert all(f['code'] != 'TARGET_OVERRIDE_STALE' for f in verified['findings'])
    [applied] = [r for r in verified['quota_report']['rows'] if r['policy_id'] == source['id']]
    assert applied['amount'] == applied['target_calculation']['override_amount'] == target['amount']
    assert applied['status'] != 'unresolved'
    with transaction(h.app.state.engine) as db:
        events = {event.action: event for event in db.scalars(select(m.AuditEvent).where(
            m.AuditEvent.target_id == draft['id'], m.AuditEvent.reason.in_([reconfirm_reason, approval_reason])))}
        assert events['workspace.target_override'].actor_id == h.seed['identities'][operator]
        assert events['workspace.approve'].actor_id == h.seed['identities'][reviewer]
        if sole_owner:
            assert not events['workspace.target_override'].detail.get('self_approved')
            assert events['workspace.approve'].detail['self_approved'] == [
                {'action': 'rota.approve', 'permission_id': permission}]
        else:
            assert all(not event.detail.get('self_approved') for event in events.values())


@pytest.mark.parametrize('change', ['revision', 'draft_dates'])
def test_target_binding_follows_source_content_across_revision_and_draft_dates(working_data, change):
    data, person = working_data
    policy = add_quota(data, person, comparison='target', prorate_department_days=True)
    scope = policy['definition']['scope']
    selector = f"{policy['id']}:line"
    policy['_catalogue_sources'] = {selector: {'policy_id': policy['id'], 'line': 0, 'tier': 3,
        'starts_on': policy['starts_on'], 'ends_on': policy['ends_on'], 'definition': {'scope': deepcopy(scope)},
        'inherit': {'target': {'sources': [], 'fallback': 'medium'}}}}
    scope['conditions'] = {'match': 'all', 'conditions': [
        {'field': INHERITED_LEVEL, 'op': 'any', 'values': [f'{selector}#target#medium']}]}
    _, original = checked_row(data)
    fingerprint = original['target_calculation']['rule_fingerprint']
    data['workspace']['target_overrides'] = [{'policy_id': policy['id'], 'policy_revision': policy['revision'],
        'person_id': person, 'amount': 4.57, 'reason': 'Accepted working-window target',
        'rule_fingerprints': [fingerprint]}]
    if change == 'revision':
        policy['revision'] += 1
    else:
        data['workspace']['starts_on'] = '2026-10-01'
    checked, row = checked_row(data)
    assert row['policy_revision'] == policy['revision'] and row['starts_on'] == data['workspace']['starts_on']
    if change == 'draft_dates':
        assert row['scope'] != original['scope']  # The evaluator rebound the inherited-level window.
    assert row['amount'] == 4.57 and row['target_calculation']['rule_fingerprint'] == fingerprint
    assert not any(f['code'] == 'TARGET_OVERRIDE_STALE' for f in checked['findings'])


@pytest.mark.parametrize('engine', [generation, cp_sat], ids=['greedy', 'cp_sat'])
@pytest.mark.parametrize('stale', [False, True])
def test_schedulers_and_score_share_the_accepted_target(working_data, engine, stale):
    data, person = working_data
    type_definition(data, requirements=[])
    data['shifts'][0]['participants'] = []
    policy = add_quota(data, person, comparison='target', amount=16, prorate_department_days=True)
    _, original = checked_row(data)
    adjustment = {'id': '00000000-0000-4000-8000-000000000001', 'policy_id': policy['id'],
        'policy_revision': policy['revision'], 'person_id': person, 'amount': 4.57,
        'reason': 'Accepted working-window target',
        'rule_fingerprints': [original['target_calculation']['rule_fingerprint']]}
    data['workspace']['target_overrides'] = [adjustment]
    if stale:
        policy['definition']['rule']['amount'] = 20
    result = engine.generate(data, time_budget_seconds=5)
    checked = result.get('validation') or evaluate(data)
    [row] = checked['quota_report']['rows']
    assert row['amount'] == row['target_calculation']['override_amount'] == 4.57
    assert 'proration' not in row
    if stale:
        assert result['status'] != 'candidate_feasible'
        assert row['status'] == 'unresolved' and not checked['quota_report']['complete']
        assert any(f['code'] == 'TARGET_OVERRIDE_STALE' for f in checked['findings'])
        assert not checked.get('goals', [])
        assert not any('vs target' in item['text'] for item in schedule_score(data, checked)['contributions'])
        if engine is cp_sat:
            assert result['status'] == 'invalid_inputs'
        reply = job_result_projection('generation', result)['result']
        [issue] = [row for row in reply['issues'] if row['code'] == 'TARGET_OVERRIDE_STALE']
        person_name = data['people'][person]['display_name']
        assert issue['rule_id'] == policy['id'] and issue['person_id'] == person
        assert issue['detail'] == {'adjustment_id': adjustment['id'], 'rule_name': policy['name'],
                                   'person_name': person_name, 'amount': adjustment['amount'],
                                   'reconfirm_command': 'workspace.target_override'}
        assert adjustment['id'] not in issue['message']
        assert person_name in issue['message'] and policy['name'] in issue['message']
        assert 'The rule content changed.' in issue['message']
    else:
        assert result['status'] == 'candidate_feasible', result
        assert checked['goals'][0]['people'][0]['expected'] == 4.57
        score = schedule_score(data, checked)
        assert any('4.57' in item['text'] for item in score['contributions'] if item['rule_id'] == policy['id'])


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_live_tag_membership_keeps_another_persons_target_override(h):
    person, colleague = h.seed['people'][:2]
    tag = h.configure('tags', {'name': 'Workload group', 'labels': ['people']})['record']
    tag = members(h, tag, add=[('people', person)])['record']
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'prorate_department_days': True},
        {'conditions': {'conditions': [{'field': 'person_ids', 'values': [tag['id']]}]}}, severity='advisory')
    draft = h.shift(h.workspace())['workspace']
    draft = h.command('coordinator', 'workspace.target_override', {'policy_id': source['id'], 'person_id': person,
        'amount': 4.57, 'reason': 'Accept the rota target'}, draft['id'], draft['revision'])['workspace']

    members(h, tag, add=[('people', colleague)])
    checked = h.lifecycle(draft, 'validate')['validation']
    rows = {row['person_id']: row for row in checked['quota_report']['rows'] if row['policy_id'] == source['id']}
    assert set(rows) == {person, colleague}
    assert rows[person]['amount'] == 4.57 and rows[person]['status'] != 'unresolved'
    assert 'override_amount' not in rows[colleague]['target_calculation']
    assert all(f['code'] != 'TARGET_OVERRIDE_STALE' for f in checked['findings'])


def bind_target(data, policy, person):
    row = next(row for row in evaluate(data)['quota_report']['rows'] if row['person_id'] == person)
    data['workspace']['target_overrides'] = [{'policy_id': policy['id'], 'policy_revision': policy['revision'],
        'person_id': person, 'amount': 4.57, 'reason': 'Accept the rota target',
        'rule_fingerprints': [row['target_calculation']['rule_fingerprint']]}]


@pytest.mark.parametrize('change', ['person-line', 'guarded-line', 'value', 'validity'])
def test_catalogue_override_tracks_its_line_instead_of_precedence_guards(working_data, change):
    data, person = working_data
    other = next(row for row in data['memberships'] if row['person_id'] != person)
    dates = {'starts_on': '2026-09-27', 'ends_on': '2026-10-04'}
    base = line(numbers={'target': n(16, 'medium')}, **dates)
    policy = catalogue_rule('duties_per_month', base, department_id=data['workspace']['department_id'],
                            settings={'block_quota': {**dates, 'prorate_department_days': True}})
    data['policies'] = [policy]
    bind_target(data, policy, person)

    if change in {'person-line', 'guarded-line'}:
        who = person_scope(other['person_id'])
        if change == 'guarded-line':
            who['conditions'].append({'field': 'category_ids', 'values': [other['category_id']]})
        own = line(who=who, numbers={'target': n(12, 'medium')}, **dates)
        policy['definition']['rule']['lines'].append(own)
    elif change == 'value':
        base['numbers']['target']['value'] = 20
    else:
        base['ends_on'] = '2026-10-03'

    checked = evaluate(data)
    rows = {row['person_id']: row for row in checked['quota_report']['rows']}
    assert rows[person]['amount'] == 4.57
    stale = [f for f in checked['findings'] if f['code'] == 'TARGET_OVERRIDE_STALE']
    if change in {'person-line', 'guarded-line'}:
        assert rows[other['person_id']]['line_id'] == own['id']
        assert not stale and rows[person]['status'] != 'unresolved'
    else:
        assert [(f['person_id'], f['rule_id']) for f in stale] == [(person, policy['id'])]
        assert rows[person]['status'] == 'unresolved'


def test_legacy_target_override_detects_policy_validity_changes(working_data):
    data, person = working_data
    policy = add_quota(data, person, comparison='target', prorate_department_days=True)
    bind_target(data, policy, person)
    policy['ends_on'] = '2026-10-03'
    checked = evaluate(data)
    assert [(f['person_id'], f['rule_id']) for f in checked['findings'] if f['code'] == 'TARGET_OVERRIDE_STALE'] == [
        (person, policy['id'])]


@pytest.mark.parametrize('comparison', ['target', 'maximum'])
def test_unused_target_override_is_visible_only_in_the_full_quota_check(working_data, comparison):
    data, person = working_data
    policy = add_quota(data, person, comparison='target', prorate_department_days=True)
    bind_target(data, policy, person)
    policy['definition']['rule'].update(comparison=comparison, prorate_department_days=False)
    checked = evaluate(data)
    report = checked['quota_report']
    [row] = report['rows']
    assert row['comparison'] == comparison and 'target_calculation' not in row
    if comparison == 'maximum':
        assert row['amount'] == policy['definition']['rule']['amount']
    assert not report['complete']
    [stale] = [f for f in checked['findings'] if f['code'] == 'TARGET_OVERRIDE_STALE']
    assert (stale['person_id'], stale['rule_id'], stale['detail']['amount']) == (person, policy['id'], 4.57)
    assert data['people'][person]['display_name'] in stale['message'] and policy['name'] in stale['message']

    for view, filters in (({**data, 'quota_subjects': {person}}, {}), (data, {'policy_ids': {policy['id']}})):
        codes = []
        narrowed = evaluate_quotas(view, lambda code, *args, **kwargs: codes.append(code),
                                   ancestor_ids, exception_for, **filters)
        assert narrowed['rows'] and narrowed['complete']
        assert 'TARGET_OVERRIDE_STALE' not in codes


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_override_admission_names_an_out_of_window_target_as_not_applicable(h):
    person = h.seed['people'][0]
    source = h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 16,
        'starts_on': '2026-11-01', 'ends_on': '2026-11-08', 'prorate_department_days': True},
        {'person_id': person}, severity='advisory')
    draft = h.workspace()
    rejected = h.command('coordinator', 'workspace.target_override', {'policy_id': source['id'], 'person_id': person,
        'amount': 4.57, 'reason': 'Accept the rota target'}, draft['id'], draft['revision'], status=422)
    assert rejected['error']['code'] == 'TARGET_NOT_APPLICABLE'
