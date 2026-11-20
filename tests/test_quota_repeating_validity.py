"""Open validity repeats configured periods without rewriting earlier finite counting bodies."""
from copy import deepcopy
from datetime import date
import time
from uuid import uuid4

import pytest

from apps.api.services.rule_catalogue import with_blocks
from packages.contracts.rule_catalogue import workload_fields, workload_settings
from packages.contracts.schemas import BlockQuota, CatalogueRule
from packages.domain.conditions import active_rules
from packages.domain.quotas import applicable_policies
from packages.domain.rule_catalogue_migration import plan_v2
from packages.domain.rule_lines import attention
from packages.domain.validation import evaluate
from .test_quota_windows import data
from .test_rule_lines import line, rule as catalogue_rule


def rule(**changes):
    return {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'maximum', 'amount': 3,
            'starts_on': '2026-10-02', 'ends_on': None, 'repeat': True,
            'window_alignment': 'fixed', 'window_days': 7, **changes}


def bounds(value, first, last):
    return [(p['definition']['rule']['starts_on'], p['definition']['rule']['ends_on'])
            for p in applicable_policies(data(value, first, last))]


def test_open_fixed_windows_remain_active_after_the_previous_hidden_end():
    assert bounds(rule(), '2028-01-03', '2028-01-10') == [
        ('2027-12-31', '2028-01-07'), ('2028-01-07', '2028-01-14')]
    finite = rule(ends_on='2027-01-01', repeat=False)
    before = deepcopy(finite)
    assert bounds(finite, '2028-01-03', '2028-01-10') == [] and finite == before


@pytest.mark.parametrize('alignment,days,expected', [
    ('calendar_week', None, [('2028-01-02', '2028-01-09'), ('2028-01-09', '2028-01-16')]),
    ('calendar_month', None, [('2028-01-01', '2028-02-01')]),
    ('rolling', 3, [('2028-01-01', '2028-01-04'), ('2028-01-02', '2028-01-05'),
                    ('2028-01-03', '2028-01-06')]),
])
def test_open_windows_load_complete_intersecting_periods(alignment, days, expected):
    last = '2028-01-10' if alignment != 'rolling' else '2028-01-04'
    assert bounds(rule(window_alignment=alignment, window_days=days), '2028-01-03', last) == expected


def test_explicit_repeat_end_and_policy_validity_still_stop_the_obligation():
    source = data(rule(ends_on='2028-01-08'), '2028-01-03', '2028-01-10')
    assert [p['definition']['rule']['ends_on'] for p in applicable_policies(source)] == ['2028-01-07', '2028-01-08']
    source = data(rule(), '2028-01-03', '2028-01-10')
    source['policies'][0]['ends_on'] = '2028-01-08'
    assert [p['definition']['rule']['ends_on'] for p in applicable_policies(source)] == ['2028-01-07', '2028-01-08']


def test_new_undated_repeating_authoring_has_no_fabricated_end_and_existing_finite_is_lossless():
    fresh = line(numbers={'target': {'value': 2, 'level': 'medium'}})
    [created] = with_blocks('duties_per_month', {}, [fresh], '2026-10-03')
    assert created['starts_on'] == '2026-10-03' and created['ends_on'] is None
    assert created['settings']['block_quota']['repeat'] is True
    configured = catalogue_rule('duties_per_month', created,
                                settings={'block_quota': {'window_alignment': 'calendar_month'}})
    [copy] = active_rules([configured])
    assert copy['definition']['rule']['repeat'] and copy['definition']['rule']['ends_on'] is None
    existing = line(numbers={'target': {'value': 2, 'level': 'medium'}}, settings={
        'block_quota': {'starts_on': '2026-10-03', 'ends_on': '2027-01-01', 'window_alignment': 'calendar_month'}})
    assert with_blocks('duties_per_month', {}, [existing], '2027-01-02') == [existing]


def test_counting_end_before_open_or_later_validity_is_visible_in_attention():
    body = {'block_quota': {'starts_on': '2026-10-03', 'ends_on': '2027-01-01', 'window_alignment': 'calendar_month'}}
    own = line(numbers={'target': {'value': 2, 'level': 'medium'}}, settings=body)
    row = catalogue_rule('duties_per_month', own)
    issues = attention([row], {own['id']: {'synthetic-person'}}, '2026-10-04')
    [issue] = [item for item in issues if item['kind'] == 'counting_ends']
    assert issue['facts'] == {'counting_end': '2027-01-01', 'validity_end': None}
    assert row['definition']['rule']['lines'][0] == own
    own['ends_on'] = '2027-01-01'
    assert not [item for item in attention([row], {own['id']: {'synthetic-person'}}, '2026-10-04')
                if item['kind'] == 'counting_ends']


@pytest.fixture(scope='module')
def far_away_fixed_anchor_windows():
    source = data(rule(starts_on='1900-01-01', window_anchor_on='1899-12-30'), '2099-01-01', '2099-02-01')
    original = source['policies'][0]
    source['policies'] = [{**original, 'id': str(index)} for index in range(150)]
    started = time.process_time()
    policies = applicable_policies(source)
    elapsed = time.process_time() - started
    return policies, elapsed


def test_far_away_fixed_anchor_expands_only_workspace_windows_within_cpu_budget(far_away_fixed_anchor_windows):
    policies, _ = far_away_fixed_anchor_windows
    assert len(policies) <= 150 * 6
    assert all('209' in p['definition']['rule']['starts_on'] for p in policies)
    for value in (rule(), rule(ends_on='2036-01-01')):
        BlockQuota.model_validate(value)


@pytest.mark.cpu_budget
def test_far_away_fixed_anchor_expansion_cpu_budget(far_away_fixed_anchor_windows):
    _, elapsed = far_away_fixed_anchor_windows
    assert elapsed < 2


def test_api_new_open_quota_round_trip_approval_future_counting_and_attention(h):
    from .test_rule_catalogue_api import approve, entry, person, propose

    own = line(who=person(h.seed['people'][0]), numbers={'target': {'value': 2, 'level': 'medium'}})
    proposed = propose(h, 'duties_per_month', [own])['record']
    saved = proposed['definition']['rule']['lines'][0]
    assert saved['ends_on'] is None and saved['settings']['block_quota']['repeat'] is True
    approve(h, proposed)
    current = entry(h, 'duties_per_month')['rule']
    assert current['definition']['rule']['lines'][0] == saved
    workspace = h.workspace(start='2028-01-03', end='2028-01-10')
    snapshot = h.snapshot(workspace)
    report = evaluate(snapshot)['quota_report']
    assert report['complete']
    [row] = [row for row in report['rows'] if row['policy_id'] == current['id']]
    assert (row['starts_on'], row['ends_on'], row['actual'], row['amount']) == ('2028-01-01', '2028-02-01', 0, 1)
    assert row['proration'] == {'configured_amount': 2, 'amount': 1, 'working_days': 5, 'full_working_days': 22,
                               'starts_on': '2028-01-01', 'ends_on': '2028-02-01',
                               'calendar_id': snapshot['department']['operating_calendar_id'], 'rounding': 'up'}
    finite = deepcopy(saved)
    finite['settings']['block_quota'].update(repeat=False, starts_on='2026-10-03', ends_on='2027-01-01')
    pending = propose(h, 'duties_per_month', [finite], policy_id=current['id'], revision=current['revision'])['record']
    activate = approve(h, pending)
    attention_items = h.get('coordinator', '/api/rules/catalogue')['attention']
    assert any(item['kind'] == 'counting_ends' and item['rule_id'] == activate['id']
               and item['facts']['validity_end'] is None for item in attention_items)


def test_repeat_window_context_counts_prior_publications_after_the_old_finite_end(h):
    person = h.seed['people'][0]
    previous = h.workspace(start='2028-01-01', end='2028-01-02')
    previous = h.shift(previous, start='2028-01-01T08:00:00+03:00', end='2028-01-01T12:00:00+03:00',
                       people=[person])['workspace']
    h.publish(previous)
    quota = h.policy(rule(amount=1), {'person_id': person})
    workspace = h.workspace(start='2028-01-04', end='2028-01-05')
    workspace = h.shift(workspace, start='2028-01-04T08:00:00+03:00', end='2028-01-04T12:00:00+03:00',
                        people=[person])['workspace']
    snapshot = h.snapshot(workspace)
    row = next(row for row in evaluate(snapshot)['quota_report']['rows'] if row['policy_id'] == quota['id'])
    assert (row['starts_on'], row['ends_on'], row['actual'], row['status']) == (
        '2027-12-31', '2028-01-07', 2, 'above_maximum')
    assert snapshot['quota_context']['complete'] and snapshot['quota_context']['publications']


def test_converted_listed_quota_opts_into_repeat_without_rewriting_its_source(h):
    from .test_rule_catalogue_api import approve, propose
    from .test_rule_catalogue_v2_api import proposed

    source = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic finite listed quota', 'owner': 'Synthetic owner', 'source': 'Synthetic evidence',
        'starts_on': '2020-01-01', 'definition': {
            'rule': rule(ends_on='2027-01-01', repeat=False), 'listed_only': True,
            'per_person': [{'category_ids': [h.seed['category']], 'amount': 2}]}})['record']
    source = h.command('approver', 'policy.activate', {'reason': 'Independent synthetic approval'},
                       source['id'], source['revision'])['record']
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent synthetic conversion'},
              conversion['id'], conversion['revision'])
    current = next(item['rule'] for item in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                   if item['id'] == 'workload_limit')
    rows = deepcopy(current['definition']['rule']['lines'])
    for own in rows:
        for field in ('mode', 'measure', 'period'):
            own.pop(field, None)
        if any(pin['policy_id'] == source['id'] for pin in own['source_bindings'].values()):
            own['ends_on'] = None
            own['settings']['block_quota'].update(repeat=True, ends_on=None)
    change = propose(h, 'workload_limit', rows, catalogue_version=2,
                     settings=current['definition']['rule']['settings'],
                     policy_id=current['id'], revision=current['revision'])['record']
    approve(h, change)
    snapshot = h.snapshot(h.workspace(start='2028-01-03', end='2028-01-10'))
    report = evaluate(snapshot)['quota_report']
    assert any(row['policy_id'] == source['id'] and row['amount'] == 2 for row in report['rows'])
    original = next(item for item in h.get('auditor', '/api/policies?status=retired')['policies'] if item['id'] == source['id'])
    assert original['definition'] == source['definition']


@pytest.mark.parametrize('kind,number', [('workload', 'max'), ('target', 'target')])
def test_new_person_counting_fact_uses_open_repeating_default(h, kind, number):
    payload = {'kind': kind, 'catalogue_id': 'workload_limit', 'person_id': h.seed['people'][0],
               'reason': 'Synthetic open personal counting window', 'line': line(
                   numbers={number: {'value': 2, 'level': 'medium' if kind == 'target' else 'mandatory'}},
                   measure='duties', period={'kind': 'fixed', 'days': 7})}
    proposed = h.command('coordinator', 'person.workload.propose', payload)['fact']
    saved = proposed['data']['line']
    assert saved['starts_on'] and saved['ends_on'] is None
    assert saved['settings']['block_quota']['repeat'] is True
    active = h.command('approver', 'person.workload.activate', {'reason': 'Independent synthetic personal approval'},
                       proposed['id'], proposed['revision'])['fact']
    current = h.get('coordinator', f"/api/people/{payload['person_id']}/scheduling-facts")
    assert next(item for item in current['active'] if item['id'] == active['id'])['counting_attention'] == []
    rows = evaluate(h.snapshot(h.workspace(start='2028-01-03', end='2028-01-10')))['quota_report']['rows']
    assert any(row['policy_id'] == active['id'] and row['amount'] == 2 for row in rows)


def test_editing_a_finite_person_counting_fact_preserves_its_body(h):
    body = {key: value for key, value in rule(ends_on='2027-01-01', repeat=False).items()
            if key not in ('kind', 'comparison', 'amount', 'repeat')}
    payload = {'kind': 'workload', 'catalogue_id': 'workload_limit', 'person_id': h.seed['people'][0],
               'reason': 'Synthetic finite personal counting evidence', 'line': line(
                   numbers={'max': {'value': 2}}, settings={'block_quota': body})}
    proposed = h.command('coordinator', 'person.workload.propose', payload)['fact']
    active = h.command('approver', 'person.workload.activate', {'reason': 'Independent synthetic finite approval'},
                       proposed['id'], proposed['revision'])['fact']
    changed = h.command('coordinator', 'person.workload.propose', {**payload,
        'line': active['data']['line'], 'replaces_id': active['id'], 'expected_revision': active['revision']})['fact']
    assert changed['data']['line']['settings'] == active['data']['line']['settings']
    assert 'repeat' not in changed['data']['line']['settings']['block_quota']
    current = h.get('coordinator', f"/api/people/{payload['person_id']}/scheduling-facts")
    warning = next(item for item in current['active'] if item['id'] == active['id'])['counting_attention']
    assert warning == [{'kind': 'counting_ends', 'line': 0, 'line_id': active['data']['line']['id'],
                        'counting_end': '2027-01-01', 'validity_end': None}]


def test_explicit_null_counting_end_rejects_stale_own_value_and_overrides_inheritance():
    period = {'kind': 'fixed', 'days': 7, 'repeat': True, 'ends_on': None}
    settings = {'block_quota': {'repeat': True, 'ends_on': '2027-01-01'}}
    with pytest.raises(ValueError, match='conflict with the canonical workload settings'):
        workload_settings('duties', period, settings, {'max': {'value': 2}})
    assert workload_settings('duties', period, {}, {'max': {'value': 2}})['block_quota']['ends_on'] is None
    raw = {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
           'settings': {'block_quota': {'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 7,
                                      'repeat': True, 'starts_on': '2026-10-02', 'ends_on': '2027-01-01'}},
           'lines': [line(numbers={'max': {'value': 2}}, measure='duties', period=period)]}
    stored = CatalogueRule.model_validate(raw).model_dump(mode='json')
    own = stored['lines'][0]
    assert own['settings']['block_quota']['ends_on'] is None
    assert workload_fields(stored, own)['period']['ends_on'] is None


@pytest.mark.parametrize('explicit_end', [False, True])
def test_repeat_counting_end_stays_open_when_finite_line_validity_is_extended(explicit_end):
    body = {'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 7, 'repeat': True,
            'starts_on': '2026-10-02', **({'ends_on': None} if explicit_end else {})}
    raw = {'kind': 'catalogue', 'catalogue_version': 2, 'id': 'workload_limit',
           'lines': [line(numbers={'max': {'value': 2}}, starts_on='2026-10-02', ends_on='2027-01-01',
                          settings={'block_quota': body})]}
    stored = CatalogueRule.model_validate(raw).model_dump(mode='json')
    own = stored['lines'][0]
    decorated = {**own, **workload_fields(stored, own)}
    assert decorated['ends_on'] == '2027-01-01' and decorated['period']['ends_on'] is None
    reloaded = CatalogueRule.model_validate({**stored, 'lines': [decorated]}).model_dump(mode='json')
    assert reloaded['lines'][0]['settings']['block_quota']['ends_on'] is None
    extended = deepcopy(reloaded)
    extended['lines'][0]['ends_on'] = '2029-01-01'
    source = data(rule(), '2028-01-03', '2028-01-10')
    policy = catalogue_rule('workload_limit')
    policy['definition']['rule'] = extended
    source['policies'] = [policy]
    assert applicable_policies(source)


def test_api_typed_finite_end_clear_opens_repeat_and_counts_future_periods(h):
    from .test_rule_catalogue_api import approve, person, propose

    own = line(who=person(h.seed['people'][0]), numbers={'target': {'value': 2, 'level': 'medium'}},
               starts_on='2026-10-03', settings={'block_quota': {
                   'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 7,
                   'starts_on': '2026-10-03', 'ends_on': '2027-01-01'}})
    original = approve(h, propose(h, 'workload_limit', [own], catalogue_version=2)['record'])
    current = next(item['rule'] for item in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                   if item['id'] == 'workload_limit')
    edited = deepcopy(current['definition']['rule']['lines'][0])
    edited['period'].update(repeat=True, ends_on=None)
    edited['settings']['block_quota']['repeat'] = True
    propose(h, 'workload_limit', [edited], catalogue_version=2, policy_id=current['id'],
            revision=current['revision'], status=422)
    edited['settings']['block_quota'].pop('ends_on')
    changed = approve(h, propose(h, 'workload_limit', [edited], catalogue_version=2,
                                policy_id=current['id'], revision=current['revision'])['record'])
    body = changed['definition']['rule']['lines'][0]['settings']['block_quota']
    assert body['repeat'] is True and body['ends_on'] is None
    future = evaluate(h.snapshot(h.workspace(start='2028-01-03', end='2028-01-10')))['quota_report']['rows']
    assert any(row['policy_id'] == changed['id'] and row['amount'] == 2 for row in future)
    archived = next(row for row in h.get('auditor', '/api/policies?status=retired')['policies']
                    if row['id'] == original['id'])
    assert archived['definition'] == original['definition']


def test_api_open_counting_end_survives_round_trip_and_line_validity_extension(h):
    from .test_rule_catalogue_api import approve, person, propose

    own = line(who=person(h.seed['people'][0]), numbers={'target': {'value': 2, 'level': 'medium'}},
               starts_on='2026-10-03', ends_on='2027-01-01', settings={'block_quota': {
                   'metric': 'duties', 'window_alignment': 'fixed', 'window_days': 7,
                   'repeat': True, 'starts_on': '2026-10-03', 'ends_on': None}})
    approve(h, propose(h, 'workload_limit', [own], catalogue_version=2)['record'])
    for extend in (False, True):
        current = next(item['rule'] for item in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                       if item['id'] == 'workload_limit')
        edited = deepcopy(current['definition']['rule']['lines'][0])
        assert edited['period']['ends_on'] is None
        if extend:
            edited['ends_on'] = '2029-01-01'
        changed = approve(h, propose(h, 'workload_limit', [edited], catalogue_version=2,
                                    policy_id=current['id'], revision=current['revision'])['record'])
        assert changed['definition']['rule']['lines'][0]['settings']['block_quota']['ends_on'] is None
    future = evaluate(h.snapshot(h.workspace(start='2028-01-03', end='2028-01-10')))['quota_report']['rows']
    assert any(row['policy_id'] == changed['id'] and row['amount'] == 2 for row in future)


def legacy_bound_counting_rule():
    source = {'id': str(uuid4()), 'kind': 'block_quota', 'name': 'Synthetic finite counting source',
              'status': 'active', 'active': True, 'revision': 1, 'department_id': 'd',
              'starts_on': '2020-01-01', 'ends_on': None,
              'definition': {'rule': rule(ends_on='2027-01-01', repeat=False),
                             'scope': {}, 'severity': 'mandatory'}}
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked']
    target = deepcopy(plan['targets'][0]['row'])
    target['status'] = 'active'
    own = target['definition']['rule']['lines'][0]['settings']['block_quota']
    # These additive fields were absent from already converted records.
    for field in ('repeat', 'window_anchor_on', 'prorate_partial_windows'):
        own.pop(field, None)
    return source, target, {'source_bindings': plan['bindings'], 'scheduling_facts': []}


def test_legacy_bound_period_noop_omits_unset_repeat_without_changing_canonical_storage():
    _, target, _ = legacy_bound_counting_rule()
    stored = CatalogueRule.model_validate(target['definition']['rule']).model_dump(mode='json')
    own = stored['lines'][0]
    fields = workload_fields(stored, own)
    assert 'repeat' not in fields['period']
    assert CatalogueRule.model_validate({**stored, 'lines': [{**own, **fields}]}).model_dump(mode='json') == stored


@pytest.mark.parametrize('field,global_value,own_value', [
    ('repeat', True, False),
    ('window_anchor_on', '2026-09-30', None),
    ('prorate_partial_windows', True, False),
])
@pytest.mark.parametrize('override', [False, True])
def test_bound_counting_settings_inherit_rule_values_with_line_overrides(field, global_value, own_value, override):
    source, target, context = legacy_bound_counting_rule()
    original = deepcopy((source, target, context))
    body = target['definition']['rule']
    body['settings'] = {'block_quota': {field: global_value}}
    if override:
        body['lines'][0]['settings']['block_quota'][field] = own_value
    snapshot = deepcopy((source, target, context))
    preview = active_rules([target])[0]['definition']['rule']
    runtime = active_rules([target], context=context)[0]['definition']['rule']
    expected = own_value if override else global_value
    assert preview.get(field, own_value) == runtime.get(field, own_value) == expected
    assert (source, target, context) == snapshot and source == original[0]


def test_explicit_typed_repeat_false_overrides_bound_global_in_read_preview_and_runtime():
    source, target, context = legacy_bound_counting_rule()
    body = target['definition']['rule']
    body['settings'] = {'block_quota': {'repeat': True}}
    own = body['lines'][0]
    fields = workload_fields(body, own)
    assert fields['period']['repeat'] is True
    fields['period']['repeat'] = False
    changed = CatalogueRule.model_validate({**body, 'lines': [{**own, **fields}]}).model_dump(mode='json')
    target['definition']['rule'] = changed
    stored = changed['lines'][0]
    assert stored['settings']['block_quota']['repeat'] is False
    assert workload_fields(changed, stored)['period']['repeat'] is False
    assert active_rules([target])[0]['definition']['rule']['repeat'] is False
    assert active_rules([target], context=context)[0]['definition']['rule'].get('repeat', False) is False
    assert source['definition']['rule']['repeat'] is False


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_api_converted_quota_global_repeat_and_typed_false_agree_with_runtime(backend, request):
    from .test_rule_catalogue_api import approve, propose
    from .test_rule_catalogue_v2_api import proposed

    h = request.getfixturevalue(backend)
    source = h.command('coordinator', 'policy.create', {
        'name': 'Synthetic inherited counting period', 'owner': 'Synthetic owner', 'source': 'Synthetic evidence',
        'starts_on': '2020-01-01', 'definition': {
            'rule': rule(ends_on='2027-01-01', repeat=False), 'listed_only': True,
            'per_person': [{'category_ids': [h.seed['category']], 'amount': 2}]}})['record']
    source = approve(h, source)
    conversion = proposed(h)
    h.command('approver', 'rule.catalogue.activate', {'reason': 'Independent synthetic conversion'},
              conversion['id'], conversion['revision'])
    workspace = h.workspace(start='2026-10-03', end='2026-10-10')

    def current():
        return next(item['rule'] for item in h.get('coordinator', '/api/rules/catalogue?version=2')['entries']
                    if item['id'] == 'workload_limit')

    def runtime():
        nonlocal workspace
        workspace = h.refresh_draft_baseline(workspace)
        snapshot = h.snapshot(workspace)
        return next(item['definition']['rule'] for item in active_rules(snapshot['policies'], context=snapshot)
                    if item['id'] == source['id'])

    stored = current()
    rows = deepcopy(stored['definition']['rule']['lines'])
    for own in rows:
        for field in ('mode', 'measure', 'period'):
            own.pop(field, None)
        own['settings']['block_quota'].pop('repeat', None)
    settings = {'block_quota': {'repeat': True}}
    inherited = approve(h, propose(h, 'workload_limit', rows, catalogue_version=2, settings=settings,
                                  policy_id=stored['id'], revision=stored['revision'])['record'])
    assert 'repeat' not in inherited['definition']['rule']['lines'][0]['settings']['block_quota']
    stored = current()
    assert stored['definition']['rule']['lines'][0]['period']['repeat'] is True
    assert runtime()['repeat'] is True

    rows = deepcopy(stored['definition']['rule']['lines'])
    rows[0]['period']['repeat'] = False
    changed = approve(h, propose(h, 'workload_limit', rows, catalogue_version=2, settings=settings,
                                policy_id=stored['id'], revision=stored['revision'])['record'])
    assert changed['definition']['rule']['lines'][0]['settings']['block_quota']['repeat'] is False
    assert current()['definition']['rule']['lines'][0]['period']['repeat'] is False
    assert runtime()['repeat'] is False
    archived = next(item for item in h.get('auditor', '/api/policies?status=retired')['policies']
                    if item['id'] == source['id'])
    assert archived['definition'] == source['definition']


def test_bound_source_default_inherits_globals_without_mutating_preserved_storage():
    source = {'id': str(uuid4()), 'kind': 'max_duties', 'name': 'Synthetic inherited default',
              'status': 'active', 'active': True, 'revision': 1, 'department_id': 'd',
              'starts_on': '2020-01-01', 'ends_on': None,
              'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'},
                             'scope': {}, 'severity': 'mandatory', 'listed_only': True,
                             'per_person': [{'category_ids': [str(uuid4())], 'amount': None}]}}
    plan = plan_v2([source], department_id='d')
    assert not plan['blocked']
    target = deepcopy(plan['targets'][0]['row'])
    target['status'] = 'active'
    body = target['definition']['rule']
    own = body['lines'][0]
    own['settings']['max_duties'].pop('include_overtime', None)
    own['source_default']['settings']['max_duties'].pop('include_overtime', None)
    body['settings'] = {'max_duties': {'include_overtime': False}}
    context = {'source_bindings': plan['bindings'], 'scheduling_facts': []}
    before = deepcopy((source, target, context))
    assert active_rules([target], context=context)[0]['definition']['rule']['include_overtime'] is False
    assert (source, target, context) == before
