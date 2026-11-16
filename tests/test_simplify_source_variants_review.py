"""Independent fact versions retain every primitive source partition."""
from copy import deepcopy
from datetime import date
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.conditions import active_rules
from packages.domain.rule_lines import expand, fact_policy
from packages.domain.rule_migration_proof import canonical_full_model
from packages.domain.solver_rules import compatibility_issues
from packages.domain.time import local_midnight
from packages.domain.validation import person_in_scope
from .test_rule_catalogue_v2_api import proposed


def counted_scope(h):
    return {'conditions': {'match': 'all', 'conditions': [
        {'match': 'any', 'count': 'each', 'conditions': [
            {'field': 'service_ids', 'op': 'any', 'count': 'each', 'values': h.seed['services'][:2]},
            {'field': 'assignment_type_ids', 'op': 'any', 'values': [h.seed['type']]}]}]}}


def convert(h, body, *, scope=None, rows=None, listed=False):
    scope = {'person_ids': h.seed['people'][:2]} if scope is None else scope
    definition = {'rule': body, 'scope': scope}
    if rows is not None:
        definition.update(per_person=rows, listed_only=listed)
    proposal = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic independently versioned source', 'owner': 'Synthetic department',
        'source': 'Independent source-partition regression', 'starts_on': '2020-01-01',
        'definition': definition})['record']
    original = h.command('approver', 'policy.activate', {'reason': 'Independent original review'},
                         proposal['id'], proposal['revision'])['record']
    workspace = h.shift(h.workspace())['workspace']
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent conversion review'},
              conversion['id'], conversion['revision'])
    return original, workspace


def revise(h, person, numbers, *, settings=None, default=True):
    with transaction(h.app.state.engine) as db:
        previous = next(fact for fact in db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.status == 'active'))
                        if fact.person_id == person and
                        (next(iter(fact.data['line']['source_bindings'].values()))['line_index'] is None) == default)
        payload = {'kind': previous.kind, 'person_id': person, 'replaces_id': previous.id,
                   'expected_revision': previous.revision, 'reason': 'Independent person-specific source edit',
                   **deepcopy(previous.data)}
    payload.pop('exception_allowed', None)
    for key, value in numbers.items():
        payload['line']['numbers'][key]['value'] = value
    for kind, values in (settings or {}).items():
        payload['line']['settings'][kind].update(values)
    revised = h.command('coordinator', 'person.workload.propose', payload)['fact']
    return h.command('approver', 'person.workload.activate', {'reason': 'Independent edited fact review'},
                     revised['id'], revised['revision'])['fact']


def assert_native_bodies(snapshot, original, *, rows_only=False):
    copies = [copy for copy in active_rules(snapshot) if copy['id'] == original['id']]
    if rows_only:
        copies = [copy for copy in copies if copy.get('template_line')]
    native = [copy for fact in snapshot['scheduling_facts'] if fact['status'] == 'active'
              for copy in expand(fact_policy(fact), outrank=False)]
    body = lambda copy: json.dumps(copy['definition']['rule'], sort_keys=True)
    assert sorted(map(body, copies)) == sorted(map(body, native))
    assert all(copy['id'] == original['id'] for copy in copies)
    assert all(not any(key.startswith('_v2_') and key != '_v2_source' for key in copy) for copy in copies)
    assert compatibility_issues(snapshot) == []
    return copies


def revise_shared(h, *, settings):
    with transaction(h.app.state.engine) as db:
        current = next(policy for policy in db.scalars(select(m.Policy).where(
            m.Policy.kind == 'catalogue', m.Policy.status == 'active'))
            if policy.definition['rule']['id'] == 'workload_limit')
        payload = {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
                   'expected_policy_id': current.id, 'expected_revision': current.revision,
                   'lines': deepcopy(current.definition['rule']['lines']),
                   'reason': 'Independent shared quota settings edit'}
    payload['lines'][0]['settings']['block_quota'].update(settings)
    revised = h.command('coordinator', 'rule.propose', payload)['record']
    return h.command('approver', 'policy.activate', {'reason': 'Independent shared settings review'},
                     revised['id'], revised['revision'])['record']


def test_stricter_safety_person_line_is_visible_in_all_rule_reads(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'min_rest', 'hours': 8})
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Display fixture conversion'},
              conversion['id'], conversion['revision'])
    current = next(e['rule'] for e in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                    if e['id'] == 'duty_spacing')
    lines = deepcopy(current['definition']['rule']['lines'])
    person_line = deepcopy(lines[0])
    person_line['id'] = str(uuid.uuid4())
    person_line.pop('source_bindings', None)
    person_line['who'] = {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [person]}]}
    person_line['numbers']['min']['value'] = 12
    lines.append(person_line)
    proposed_rule = h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duty_spacing', 'catalogue_version': 2,
        'expected_policy_id': current['id'], 'expected_revision': current['revision'],
        'lines': lines, 'reason': 'Display fixture stricter person line'})['record']
    h.command('approver', 'policy.activate', {'reason': 'Display fixture review'},
              proposed_rule['id'], proposed_rule['revision'])
    workspace = h.workspace('Display fixture')
    workspace = h.shift(workspace, people=[person])['workspace']
    h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])

    mapped = h.get('coordinator', f'/api/workspaces/{workspace["id"]}/rule-map')
    rules = [row for row in mapped['rules'] if row.get('catalogue_id') == 'duty_spacing']
    assert len(rules) == 1
    rule = rules[0]
    assert [line['numbers']['min']['value'] for line in rule['lines']] == [8.0, 12.0]
    assert rule['line_people'][person][0][2] == 1

    explained = h.get('coordinator', f'/api/workspaces/{workspace["id"]}/rules-explained?who=person:{person}')
    sentence = next(row for row in explained['rules'] if row.get('catalogue_id') == 'duty_spacing')['sentences'][0]
    assert 'at least 12 hours of rest between duties' in sentence['text']

    entry = next(row for row in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                 if row['id'] == 'duty_spacing')
    assert [line['numbers']['min']['value'] for line in entry['rule']['definition']['rule']['lines']] == [8.0, 12.0]


@pytest.mark.parametrize('nested_count', [False, True])
@pytest.mark.parametrize('numbers', [{'min': 2, 'max': 7, 'target': 3}, {'min': 12, 'max': 14, 'target': 13}])
def test_live_default_range_rows_keep_every_approved_numeric_bound(h, numbers, nested_count):
    person = h.seed['people'][0]
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'range',
        'amount': 4, 'maximum_amount': 8, 'target_amount': 6,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
        scope=counted_scope(h) if nested_count else {}, rows=[{'person_id': person, 'amount': 3, 'settings': None}])
    revise(h, person, numbers, default=False)
    snapshot = h.snapshot(workspace)
    copies = assert_native_bodies(snapshot, original, rows_only=True)
    assert [(copy['definition']['rule']['comparison'], copy['definition']['rule']['amount']) for copy in copies] == [
        ('minimum', numbers['min']), ('maximum', numbers['max']), ('target', numbers['target'])]
    assert all(copy['template_line']['line'] == 0 and copy['template_line']['settings'] is None for copy in copies)
    source = next(policy for policy in snapshot['policies'] if policy['id'] == original['id'])
    assert (source['definition']['rule']['amount'], source['definition']['rule']['maximum_amount'],
            source['definition']['rule']['target_amount']) == (4, 8, 6)
    assert canonical_full_model(snapshot)['ready']


@pytest.mark.parametrize('change', ['fractional_metric', 'rolling_default'])
def test_live_default_and_person_row_keep_independent_quota_settings(h, change):
    person = h.seed['people'][0]
    row = {'person_id': person, 'amount': 3}
    if change == 'rolling_default':
        row.update(starts_on='2026-10-01', ends_on='2026-10-06')
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, scope={}, rows=[row])
    if change == 'fractional_metric':
        revise(h, person, {'max': 3.5}, settings={'block_quota': {'metric': 'scheduled_hours'}}, default=False)
    else:
        revise_shared(h, settings={'window_days': 7})
        # TEST-STALE: block_quota is non-safety; refresh the changed shared default per 425d832da/6ef713812.
        workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    snapshot = h.snapshot(workspace)
    copies = assert_native_bodies(snapshot, original, rows_only=True)
    assert len(copies) == 1 and copies[0]['template_line']['line'] == 0
    actual = copies[0]['definition']['rule']
    assert (actual['metric'], actual['amount'], actual['window_days']) == (
        ('scheduled_hours', 3.5, None) if change == 'fractional_metric' else ('duties', 3, None))
    default = next(copy for copy in active_rules(snapshot) if copy['id'] == original['id'] and not copy.get('template_line'))
    assert (default['definition']['rule']['metric'], default['definition']['rule']['amount'],
            default['definition']['rule']['window_days']) == ('duties', 4, 7 if change == 'rolling_default' else None)
    assert canonical_full_model(snapshot)['ready']


@pytest.mark.parametrize('nested_count', [False, True])
def test_split_generic_positive_row_keeps_each_approved_persons_scope_and_body(h, nested_count):
    first, second, third = h.seed['people']
    scope = {'conditions': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [first, second, third]}]}}
    if nested_count:
        scope['conditions']['conditions'].extend(counted_scope(h)['conditions']['conditions'])
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, scope=scope,
        rows=[{'person_id': None, 'service_ids': [h.seed['services'][0]], 'amount': 3}], listed=True)
    revise(h, first, {'max': 3.5}, settings={'block_quota': {'metric': 'scheduled_hours'}}, default=False)

    def verify(first_amount):
        snapshot = h.snapshot(workspace)
        copies = [copy for copy in active_rules(snapshot) if copy['id'] == original['id']]
        a, b = (local_midnight(date.fromisoformat(day), snapshot['organisation']['timezone'])
                for day in ('2026-10-04', '2026-10-05'))
        actual = {}
        body = lambda copy: json.dumps(copy['definition']['rule'], sort_keys=True)
        for person in (first, second, third):
            matched = [copy for copy in copies
                       if person_in_scope(snapshot, copy['definition']['scope'], person, a, b)]
            native = [copy for fact in snapshot['scheduling_facts']
                      if fact['status'] == 'active' and fact['person_id'] == person
                      for copy in expand(fact_policy(fact), outrank=False)]
            assert sorted(map(body, matched)) == sorted(map(body, native))
            actual[person] = [(copy['definition']['rule']['metric'], copy['definition']['rule']['amount'])
                              for copy in matched]
        assert actual == {first: [] if first_amount is None else [('scheduled_hours', first_amount)],
                          second: [('duties', 3)], third: [('duties', 3)]}
        assert len(copies) == (1 if first_amount is None else 2)
        assert all(copy['template_line']['line'] == 0 and copy['template_line']['person_id'] is None
                   and copy['template_line']['service_ids'] == [h.seed['services'][0]] for copy in copies)
        assert compatibility_issues(snapshot) == []
        assert canonical_full_model(snapshot)['ready']

    verify(3.5)
    revise(h, first, {'max': None}, default=False)
    verify(None)
    revise(h, first, {'max': 2.5}, default=False)
    verify(2.5)


@pytest.mark.parametrize('nested_count', [False, True])
@pytest.mark.parametrize('kind', ['quota', 'range_target', 'weekly_cap'])
def test_approved_multi_person_base_fact_edit_keeps_each_persons_values(h, kind, nested_count):
    first, second = h.seed['people'][:2]
    body = ({'kind': 'max_duties', 'maximum': 4, 'window': 'week'} if kind == 'weekly_cap' else
            {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'range' if kind == 'range_target' else 'maximum',
             'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    if kind == 'range_target':
        body.update(maximum_amount=8, target_amount=6)
    scope = {'person_ids': [first, second], **(counted_scope(h) if nested_count else {})}
    original, workspace = convert(h, body, scope=scope)
    revise(h, first, {'min': 3, 'target': 5} if kind == 'range_target' else {'max': 3})
    snapshot = h.snapshot(workspace)
    copies = assert_native_bodies(snapshot, original)
    a, b = (local_midnight(date.fromisoformat(day), snapshot['organisation']['timezone'])
            for day in ('2026-10-04', '2026-10-05'))
    actual = {person: [(copy['definition']['rule'].get('comparison', 'maximum'),
                       copy['definition']['rule'].get('amount', copy['definition']['rule'].get('maximum')))
                      for copy in copies if person_in_scope(snapshot, copy['definition']['scope'], person, a, b)]
              for person in (first, second)}
    expected = {first: [('minimum', 3), ('maximum', 8), ('target', 5)],
                second: [('minimum', 4), ('maximum', 8), ('target', 6)]} if kind == 'range_target' else {
                    first: [('maximum', 3)], second: [('maximum', 4)]}
    assert actual == expected
    model = canonical_full_model(snapshot)
    assert model['ready'], model


def test_clearing_and_restoring_multi_person_default_facts_never_emits_a_zero_bound(h):
    first, second = h.seed['people'][:2]
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    revise(h, first, {'max': None})
    copies = assert_native_bodies(h.snapshot(workspace), original)
    assert len(copies) == 1 and copies[0]['definition']['scope']['person_ids'] == [second]
    assert copies[0]['definition']['rule']['amount'] == 4
    revise(h, second, {'max': None})
    assert assert_native_bodies(h.snapshot(workspace), original) == []
    revise(h, first, {'max': 3})
    snapshot = h.snapshot(workspace)
    copies = assert_native_bodies(snapshot, original)
    assert len(copies) == 1 and copies[0]['definition']['scope']['person_ids'] == [first]
    assert copies[0]['definition']['rule']['amount'] == 3
    assert canonical_full_model(snapshot)['ready']


@pytest.mark.parametrize('scope_form', ['flat', 'singleton_any', 'counted'])
def test_divergent_default_variants_preserve_personal_row_exclusion_and_emit_the_row_once(h, scope_form):
    first, second = h.seed['people'][:2]
    scope = {'conditions': {'match': 'all', 'conditions': [
        {'field': 'person_ids', 'op': 'any', 'values': [first, second]}]}}
    if scope_form == 'singleton_any':
        scope['conditions']['match'] = 'any'
    elif scope_form == 'counted':
        scope['conditions']['conditions'].extend(counted_scope(h)['conditions']['conditions'])
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, scope=scope,
        rows=[{'person_id': second, 'amount': 7}])
    row_facts = [fact for fact in h.snapshot(workspace)['scheduling_facts']
                 if next(iter(fact['data']['line']['source_bindings'].values()))['line_index'] == 0]
    assert [fact['person_id'] for fact in row_facts] == [second]
    revise(h, first, {'max': 3})
    snapshot = h.snapshot(workspace)
    copies = [copy for copy in active_rules(snapshot) if copy['id'] == original['id']]
    assert sum(bool(copy.get('template_line')) for copy in copies) == 1
    a, b = (local_midnight(date.fromisoformat(day), snapshot['organisation']['timezone'])
            for day in ('2026-10-04', '2026-10-05'))
    amounts = {person: [copy['definition']['rule']['amount'] for copy in copies
                        if person_in_scope(snapshot, copy['definition']['scope'], person, a, b)]
               for person in (first, second)}
    assert amounts == {first: [3], second: [7]}
    assert compatibility_issues(snapshot) == []
    assert canonical_full_model(snapshot)['ready']


def test_listed_nested_count_scope_remains_valid_before_and_after_metric_edit(h):
    first, second = h.seed['people'][:2]
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, scope=counted_scope(h),
        rows=[{'person_id': first, 'amount': 3}, {'person_id': second, 'amount': 4}], listed=True)
    # TEST-STALE: the catalogue conversion is non-safety and must be reviewed into this draft
    # under the freeze matrix (425d832da/6ef713812); the later person fact is live.
    workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    assert_native_bodies(h.snapshot(workspace), original)
    revise(h, first, {'max': 3.5}, settings={'block_quota': {'metric': 'scheduled_hours'}}, default=False)
    snapshot = h.snapshot(workspace)
    copies = assert_native_bodies(snapshot, original)
    assert [(copy['definition']['rule']['metric'], copy['definition']['rule']['amount']) for copy in copies] == [
        ('scheduled_hours', 3.5), ('duties', 4)]
    assert canonical_full_model(snapshot)['ready']


def test_cleared_live_catalogue_default_does_not_turn_an_inherited_row_into_a_zero_limit(h):
    person = h.seed['people'][0]
    original, workspace = convert(h, {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum',
        'amount': 4, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, scope={},
        rows=[{'person_id': person, 'amount': None}])
    # TEST-STALE: workload_limit is a non-safety block_quota. The conversion and each shared-default review
    # follow the frozen-input decision (425d832da/6ef713812), so refresh this draft through governance.
    workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
    before = [copy for copy in active_rules(h.snapshot(workspace)) if copy['id'] == original['id']]
    assert len(before) == 2 and all(copy['definition']['rule']['amount'] == 4 for copy in before)
    for amount in (None, 5):
        with transaction(h.app.state.engine) as db:
            current = next(policy for policy in db.scalars(select(m.Policy).where(
                m.Policy.kind == 'catalogue', m.Policy.status == 'active'))
                if policy.definition['rule']['id'] == 'workload_limit')
            payload = {'catalogue_id': 'workload_limit', 'catalogue_version': 2,
                       'expected_policy_id': current.id, 'expected_revision': current.revision,
                       'lines': deepcopy(current.definition['rule']['lines']),
                       'reason': 'Independently clear or restore the shared workload default'}
        payload['lines'][0]['numbers']['max']['value'] = amount
        revised = h.command('coordinator', 'rule.propose', payload)['record']
        h.command('approver', 'policy.activate', {'reason': 'Independent shared-default review'},
                  revised['id'], revised['revision'])
        # TEST-STALE: the revised shared block_quota is another reviewed non-safety input; refresh its draft baseline per 425d832da/6ef713812.
        workspace = h.refresh_draft_baseline(h.detail(workspace)['workspace'])
        snapshot = h.snapshot(workspace)
        copies = [copy for copy in active_rules(snapshot) if copy['id'] == original['id']]
        if amount is None:
            assert copies == []
        else:
            assert len(copies) == 2 and all(copy['definition']['rule']['amount'] == amount for copy in copies)
            assert next(copy for copy in copies if copy.get('template_line'))['template_line']['amount'] is None
        assert compatibility_issues(snapshot) == []
        assert canonical_full_model(snapshot)['ready']
