"""New scheduling options stay on existing reviewed, audited, undoable configuration paths."""
import copy
from datetime import date, timedelta
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from packages.domain.conditions import active_rules, with_tags
from .governance_support import retire_rule


def selector(field, value):
    return s.Conditions.model_validate({'conditions': [{'field': field, 'values': [value]}]}).model_dump(mode='json')


def propose_current(h, payload, *, status=200):
    """Use the current catalogue revision, including the department's seeded Fair share rule."""
    version = payload.get('catalogue_version', 1)
    entries = h.get('coordinator', f'/api/rules/catalogue?version={version}&include_hidden=true')['entries']
    current = next(row['rule'] for row in entries if row['id'] == payload['catalogue_id'])
    expected = {'expected_policy_id': current['id'], 'expected_revision': current['revision']} if current else {}
    return h.command('coordinator', 'rule.propose', {**payload, **expected}, status=status)


def options(h, concept):
    person = h.seed['people'][0]
    if concept == 'credit':
        return 'people_per_session', 'min', 2, 'minimum_cover', {
            'credit_conditions': selector('person_ids', person), 'credit_minimum_others': 1}
    if concept == 'overtime':
        return 'days_in_a_row', 'max', 3, 'max_consecutive_days', {
            'include_overtime': True, 'overtime_extension_days': 1}
    if concept == 'counting':
        start = date.fromisoformat(h.workspace()['starts_on'])
        return 'workload_limit', 'target', 3, 'block_quota', {
            'metric': 'duties', 'starts_on': start.isoformat(),
            'ends_on': (start + timedelta(days=19)).isoformat(), 'window_alignment': 'fixed',
            'window_days': 13, 'window_anchor_on': (start - timedelta(days=2)).isoformat(),
            'prorate_partial_windows': True}
    body = {'metric': 'duties', 'weight': 0.37, 'distribution': concept}
    if concept == 'people':
        body['optimize_greedy'] = True
    if concept == 'share':
        body.update(within=selector('service_ids', h.seed['services'][0]), target_share=0.3)
    if concept == 'pair_limit':
        body.update(metric='pair_exposure', aim='target',
                    partners=selector('person_ids', person), pairing='supervised')
        return 'fair_share', 'target', 1, 'balance_goal', body
    return 'fair_share', 'apply', None, 'balance_goal', body


@pytest.mark.parametrize('concept', ['credit', 'overtime', 'counting', 'people', 'share', 'period_spread', 'pair_limit'])
def test_options_are_reviewed_persisted_audited_and_undoable(h, concept):
    entry, number, value, kind, settings = options(h, concept)
    line = {'id': str(uuid.uuid4()), 'numbers': {number: {'value': value, 'level': 'medium' if entry in ('fair_share', 'workload_limit') else 'mandatory'}},
            'settings': {kind: settings}}
    payload = {'catalogue_id': entry, 'catalogue_version': 2, 'lines': [line],
               'reason': 'Synthetic configurable scheduling option'}
    first = propose_current(h, payload)['record']
    assert first['status'] == 'proposed'
    assert not [p for p in active_rules(h.snapshot(h.workspace())['policies']) if p['id'] == first['id']]
    h.command('coordinator', 'policy.activate', {'reason': 'Own proposal cannot self-approve'},
              first['id'], first['revision'], status=403)
    active = h.command('approver', 'policy.activate', {'reason': 'Independent option review'},
                       first['id'], first['revision'])['record']
    expanded = active_rules([{**active, 'active': True}])
    assert expanded and all(expanded[0]['definition']['rule'][key] == value for key, value in settings.items())
    changed = copy.deepcopy(line)
    if concept == 'credit':
        changed['settings'][kind]['credit_minimum_others'] = 2
    elif concept == 'overtime':
        changed['settings'][kind]['overtime_extension_days'] = 2
    elif concept == 'counting':
        changed['settings'][kind]['window_days'] = 11
    else:
        changed['settings'][kind]['weight'] = 0.61
    replacement = h.command('coordinator', 'rule.propose', {
        **payload, 'lines': [changed], 'expected_policy_id': active['id'],
        'expected_revision': active['revision']})['record']
    newer = h.command('approver', 'policy.activate', {'reason': 'Review changed configuration'},
                      replacement['id'], replacement['revision'])['record']
    assert retire_rule(h, newer, 'Undo configuration change')['restored_ids'] == [active['id']]
    stored = next(p for p in h.get('auditor', '/api/policies?status=active')['policies'] if p['id'] == active['id'])
    assert stored['definition']['rule']['lines'][0]['settings'][kind] == settings
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == active['id'])))) >= 2
        assert list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.target_id == newer['id'])))


@pytest.mark.parametrize('concept', ['credit', 'share', 'pair_limit'])
def test_embedded_selectors_cannot_reference_unknown_records(h, concept):
    entry, number, value, kind, settings = options(h, concept)
    key = {'credit': 'credit_conditions', 'share': 'within', 'pair_limit': 'partners'}[concept]
    settings[key] = selector('person_ids', str(uuid.uuid4()))
    propose_current(h, {'catalogue_id': entry, 'catalogue_version': 2,
        'lines': [{'id': str(uuid.uuid4()), 'numbers': {number: {'value': value, 'level': 'medium' if entry == 'fair_share' else 'mandatory'}}, 'settings': {kind: settings}}],
        'reason': 'Unknown synthetic reference must fail'}, status=404)


def test_fill_order_uses_department_defaults_and_existing_undo(h):
    me = h.get('admin', '/api/me')
    org, dept = me['organisation'], me['department']
    payload = {'department_name': dept['name'], 'department_revision': dept['revision'],
               'organisation_name': org['name'], 'timezone': org['timezone'], 'week_start': org['week_start'],
               'reason': 'Configure synthetic allocation stages',
               'generation_defaults': {'allocation_order': 'tiered', 'fill_tier_time_share': 0.25,
                                       'pair_limit_max_opportunities': 12000}}
    saved = h.command('admin', 'department.settings', payload, dept['id'], org['revision'])
    assert saved['department']['generation_defaults']['allocation_order'] == 'tiered'
    assert saved['department']['generation_defaults']['fill_tier_time_share'] == 0.25
    assert saved['department']['generation_defaults']['pair_limit_max_opportunities'] == 12000
    previous = saved['previous']
    assert previous['generation_defaults']['allocation_order'] == 'weighted'
    assert previous['generation_defaults']['fill_tier_time_share'] == 0.5
    restored = h.command('admin', 'department.settings', {**previous, 'reason': 'Undo allocation stages'},
                         dept['id'], saved['organisation']['revision'])
    assert restored['department']['generation_defaults']['allocation_order'] == 'weighted'
    assert restored['department']['generation_defaults']['fill_tier_time_share'] == 0.5
    assert restored['department']['generation_defaults']['pair_limit_max_opportunities'] == s.GenerationOptions().pair_limit_max_opportunities


@pytest.mark.postgres
@pytest.mark.parametrize('concept', ['credit', 'counting', 'share', 'pair_limit'])
def test_postgres_new_rule_configuration_lifecycle(pg, concept):
    test_options_are_reviewed_persisted_audited_and_undoable(pg, concept)


@pytest.mark.parametrize('model, value', [
    (s.MinimumCover, {'kind': 'minimum_cover', 'minimum': 1, 'credit_minimum_others': 1}),
    (s.MinimumCover, {'kind': 'minimum_cover', 'minimum': 1, 'credit_conditions': selector('service_ids', str(uuid.uuid4())), 'credit_minimum_others': 1}),
    (s.ConsecutiveDays, {'kind': 'max_consecutive_days', 'maximum': 3, 'include_overtime': False, 'overtime_extension_days': 1}),
    (s.BalanceGoal, {'kind': 'balance_goal', 'metric': 'duties', 'distribution': 'share'}),
    (s.BalanceGoal, {'kind': 'balance_goal', 'metric': 'duties', 'weight': 0.00001}),
    (s.BalanceGoal, {'kind': 'balance_goal', 'metric': 'duties', 'distribution': 'pair_limit', 'amount': 1}),
    (s.WorkloadPeriod, {'kind': 'calendar_week', 'anchor_on': '2040-01-01'}),
    (s.GenerationOptions, {'allocation_order': 'unknown'}),
])
def test_new_configuration_rejects_incoherent_combinations(model, value):
    with pytest.raises(ValidationError):
        model.model_validate(value)


def test_embedded_selectors_expand_tags_in_primitive_and_catalogue_settings():
    tag, person = str(uuid.uuid4()), str(uuid.uuid4())
    conditions = selector('person_ids', tag)
    rule = {'kind': 'minimum_cover', 'minimum': 1, 'credit_conditions': conditions, 'credit_minimum_others': 1}
    catalogue = {'kind': 'catalogue', 'id': 'people_per_session', 'settings': {'minimum_cover': rule},
                 'lines': [{'id': str(uuid.uuid4()), 'numbers': {'min': {'value': 1}},
                            'settings': {'minimum_cover': rule}}]}
    data = with_tags({'tags': {tag: {'name': 'Synthetic group', 'members': {'people': [person]}}},
                      'policies': [{'definition': {'scope': {}, 'rule': body}} for body in (rule, catalogue)]})
    primitive, grouped = [p['definition']['rule'] for p in data['policies']]
    trees = [primitive['credit_conditions'], grouped['settings']['minimum_cover']['credit_conditions'],
             grouped['lines'][0]['settings']['minimum_cover']['credit_conditions']]
    assert all(person in tree['conditions'][0]['values'] for tree in trees)
