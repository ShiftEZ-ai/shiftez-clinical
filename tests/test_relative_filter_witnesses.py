"""Quota subjects and inherited strengths use coherent duties, including their local day."""
from copy import deepcopy
from datetime import date
import json

import pytest

from packages.contracts.schemas import PolicyDefinition, Scope
from packages.domain import conditions, quotas
from packages.domain.time import aware, local_midnight
from packages.domain.validation import ancestor_ids, policy_applies
from .test_quota_skill_subjects import _quota
from .test_rule_conditions import quota_data
from .test_rule_map import U
from .test_rule_skill_precedence import _fixture, _line, _who


SKILL, CLINICS = U('witness-skill'), [U('witness-clinic-a'), U('witness-clinic-b')]


def snapshot(grant='clinic', *, zone='Asia/Riyadh', first='2026-10-04', last='2026-10-11'):
    source, person = quota_data()
    source['organisation'].update(timezone=zone, week_start=6)
    source['people'] = {person: {**source['people'][person], 'display_name': 'Synthetic clinician'}}
    source['memberships'] = [r for r in source['memberships'] if r['person_id'] == person]
    source['services'] = {clinic: {'department_id': 'd'} for clinic in CLINICS}
    source['types'] = {'T': {'department_id': 'd', 'definition': {'purpose': 'staffed'}}}
    source['locations'] = {'room': {'department_id': 'd', 'parent_id': None}}
    source['capabilities'] = {SKILL: {'active': True}}
    source['grants'] = [] if grant == 'none' else [{
        'person_id': person, 'department_id': 'foreign' if grant == 'foreign' else 'd',
        'capability_id': SKILL, 'service_id': None if grant in {'global', 'foreign'} else CLINICS[0],
        'starts_on': '2020-01-01', 'revoked': grant == 'revoked'}]
    source['workspace'] = {'id': U('witness-rota'), 'department_id': 'd', 'status': 'draft',
                           'starts_on': first, 'ends_on': last}
    a, b = (local_midnight(date.fromisoformat(day), zone) for day in (first, last))
    source['quota_context'] = {'complete': True, 'start': a.isoformat(), 'end': b.isoformat(), 'publications': []}
    source['shifts'], source['policies'] = [], []
    return source, person, a, b


def quota(source, scope, name='Minimum duties'):
    workspace = source['workspace']
    definition = PolicyDefinition.model_validate({'rule': {
        'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1,
        'starts_on': workspace['starts_on'], 'ends_on': workspace['ends_on']}}).model_dump(mode='json')
    # Internal selectors and historical date operands are read from snapshots, not admitted as new user input.
    definition['scope'] = deepcopy(scope)
    return {'id': U(name), 'name': name, 'kind': 'block_quota', 'department_id': 'd', 'revision': 1,
            'starts_on': '2020-01-01', 'ends_on': None, 'status': 'active', 'active': True,
            'definition': definition}


def assert_minimum(source, person, scope, expected):
    source['policies'] = [quota(source, scope)]
    findings = []
    report = quotas.evaluate_quotas(source, lambda code, *args, **kwargs: findings.append(code),
                                    ancestor_ids, lambda *args: None)
    assert report['complete'], (report, findings)
    assert [(r['person_id'], r['amount'], r['actual'], r['status']) for r in report['rows']] == (
        [(person, 1, 0, 'below_minimum')] if expected else [])
    assert ('QUOTA_BELOW_MINIMUM' in findings) is expected
    return report


def test_report_subject_witnesses_follow_stable_person_order(monkeypatch):
    source, person, _, _ = snapshot('none')
    people = [U(f'ordered-quota-person-{n}') for n in range(8)]
    source['people'] = {pid: {**source['people'][person], 'id': pid} for pid in reversed(people)}
    source['memberships'] = [{**row, 'person_id': pid} for pid in reversed(people) for row in source['memberships']]
    policy = quota(source, {})
    policy['definition']['rule']['comparison'] = 'maximum'
    source['policies'] = [policy]
    original, calls = quotas.scope_has_duty_witness, []

    def counted(data, scope, person_id, *args, **kwargs):
        calls.append(person_id)
        return original(data, scope, person_id, *args, **kwargs)

    monkeypatch.setattr(quotas, 'scope_has_duty_witness', counted)
    report = quotas.evaluate_quotas(source, lambda *args, **kwargs: None, ancestor_ids, lambda *args: None)
    assert report['complete'] and len(report['rows']) == len(people)
    assert calls == sorted(people)


@pytest.mark.parametrize('cached', [False, True])
def test_exhausted_report_budget_stops_static_and_cached_witnesses(monkeypatch, cached):
    source, person, a, b = snapshot('none')
    if cached:
        source['_quota_witness_cache'] = {}
    budget, calls = [1], []
    original = conditions.scope_applies

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(conditions, 'scope_applies', counted)
    assert quotas.duty_witness(source, {}, person, a, b, 'd', budget=budget)
    assert len(calls) == 1
    budget[0] = 0
    with pytest.raises(conditions.ScopeContextLimit, match='report-wide'):
        quotas.duty_witness(source, {}, person, a, b, 'd', budget=budget)
    assert len(calls) == 1


@pytest.mark.parametrize('grant,empty,not_empty', [
    ('clinic', True, True), ('global', False, True), ('foreign', True, False),
    ('revoked', True, False), ('none', True, False), ('starts_last_day', True, True),
    ('starts_after_window', True, False), ('ends_at_window_start', True, False),
])
@pytest.mark.parametrize('op', ['empty', 'not_empty'])
def test_empty_rota_minimum_uses_all_relevant_skill_grants(grant, empty, not_empty, op):
    source, person, _, _ = snapshot(grant)
    if grant == 'starts_last_day':
        source['grants'][0]['starts_on'] = '2026-10-10'
    elif grant == 'starts_after_window':
        source['grants'][0]['starts_on'] = '2026-10-11'
    elif grant == 'ends_at_window_start':
        source['grants'][0]['ends_on'] = '2026-10-04'
    scope = {'conditions': {'conditions': [{'field': 'capability_ids', 'op': op}]}}
    assert_minimum(source, person, scope, empty if op == 'empty' else not_empty)


@pytest.mark.parametrize('op,clinic,expected', [
    ('empty', 0, False), ('empty', 1, True), ('not_empty', 0, True), ('not_empty', 1, False),
])
def test_skill_presence_minimum_requires_one_coherent_clinic(op, clinic, expected):
    source, person, _, _ = snapshot()
    scope = {'service_ids': [CLINICS[clinic]],
             'conditions': {'conditions': [{'field': 'capability_ids', 'op': op}]}}
    assert_minimum(source, person, scope, expected)


@pytest.mark.parametrize('order', [(4, 6), (6, 4)])
@pytest.mark.parametrize('late_participant', [False, True])
def test_inherited_level_is_independent_of_duty_evaluation_order(h, order, late_participant):
    source, policy, person, _ = _fixture(h, 2, service=0)
    higher = _line(2, who=_who('category_ids', h.seed['category']), level='high')
    own = _line(3, who=_who('person_ids', person), level=None)
    policy['definition']['rule']['lines'] = [_line(1, level='low'), higher, own]
    for line in policy['definition']['rule']['lines']:
        line['where'] = {'conditions': [{'field': 'date', 'op': 'is', 'value': '2026-10-06'}]}
    source['policies'] = conditions.active_rules([policy])
    source['_line_index_cache'], source['_person_dates_cache'] = {}, {}
    inherited = [p for p in source['policies'] if p['template_line']['id'] == own['id']]
    shift = deepcopy(source['shifts'][0])
    for day in order:
        shift.update(start=f'2026-10-{day:02}T23:00:00+03:00', end=f'2026-10-{day + 1:02}T02:00:00+03:00')
        a = aware(f'2026-10-{day + 1:02}T00:00:00+03:00') if late_participant else aware(shift['start'])
        b = aware(shift['end'])
        strengths = [p['definition']['strength'] for p in inherited
                     if policy_applies(source, p, person, shift, a, b)]
        assert strengths == (['high'] if day == 6 else [])


def test_empty_date_scoped_quota_inherits_strength_from_a_later_virtual_duty(h):
    from packages.domain.validation import evaluate

    source, policy, person, _ = _quota(h, comparison='min')
    source['shifts'], source['focus_ids'] = [], []
    everyone, ranked = policy['definition']['rule']['lines']
    everyone['numbers']['min']['level'] = 'low'
    ranked['who'] = _who('category_ids', h.seed['category'])
    ranked['numbers']['min']['level'] = 'high'
    own = {'id': U('inherited-witness-minimum'), 'who': _who('person_ids', person),
           'numbers': {'min': {'value': 3, 'level': None}}, 'settings': deepcopy(everyone['settings'])}
    policy['definition']['rule']['lines'].append(own)
    for line in policy['definition']['rule']['lines']:
        line['where'] = {'conditions': [{'field': 'date', 'op': 'is', 'value': '2026-10-06'}]}
    policy['definition'] = PolicyDefinition.model_validate(policy['definition']).model_dump(mode='json')
    result = evaluate(source)
    assert result['quota_report']['complete'], result
    [minimum] = [row for row in result['quota_report']['rows'] if row['line_id'] == own['id']]
    assert (minimum['amount'], minimum['actual'], minimum['severity'], minimum['strength']) == (3, 0, 'advisory', 'high')


DATE_CASES = [
    pytest.param({'op': 'is', 'values': ['2026-10-06']}, True, id='legacy-is'),
    pytest.param({'op': 'is', 'value': '2026-10-06'}, True, id='raw-is'),
    pytest.param({'op': 'is', 'value': {'kind': 'absolute', 'value': '2026-10-06'}}, True, id='wrapped-is'),
    pytest.param({'op': 'is', 'value': '2026-10-11'}, False, id='is-outside'),
    pytest.param({'op': 'before', 'value': '2026-10-06'}, True, id='before'),
    pytest.param({'op': 'before', 'value': '2026-10-04'}, False, id='before-outside'),
    pytest.param({'op': 'after', 'value': '2026-10-05'}, True, id='after'),
    pytest.param({'op': 'after', 'value': '2026-10-10'}, False, id='after-outside'),
    pytest.param({'op': 'on_or_before', 'value': '2026-10-04'}, True, id='inclusive-first'),
    pytest.param({'op': 'on_or_after', 'value': '2026-10-11'}, False, id='inclusive-outside'),
    pytest.param({'op': 'between', 'value': ['2026-10-06', {'kind': 'absolute', 'value': '2026-10-07'}]}, True,
                 id='mixed-between'),
    pytest.param({'op': 'between', 'value': [{'kind': 'relative', 'token': 'duty_day'}, '2026-10-03']}, True,
                 id='relative-between'),
    pytest.param({'op': 'within', 'value': {'kind': 'relative', 'token': 'duty_week'}}, True, id='duty-relative'),
    pytest.param({'op': 'empty'}, False, id='empty-date'),
    pytest.param({'op': 'not_empty'}, True, id='nonempty-date'),
]


@pytest.mark.parametrize('line,expected', DATE_CASES)
@pytest.mark.parametrize('grant', ['clinic', 'global', 'none'])
def test_date_predicates_keep_empty_rota_quota_subjects_resolved(line, expected, grant):
    source, person, a, b = snapshot(grant)
    nodes = [{'field': 'date', **line}]
    if grant != 'none':
        nodes.append({'field': 'capability_ids', 'op': 'any', 'value': SKILL})
    else:
        # A date-only scope needs no duty catalogue to know that a local day exists.
        source['types'], source['locations'], source['services'] = {}, {}, {}
    scope = {'conditions': {'conditions': nodes}}
    assert conditions.scope_has_duty_witness(source, scope, person, a, b, 'd') is expected
    assert_minimum(source, person, scope, expected)


@pytest.mark.parametrize('zone', ['Asia/Riyadh', 'Europe/London'])
@pytest.mark.parametrize('grant', ['clinic', 'global', 'none'])
def test_date_witnesses_split_at_local_midnight_across_dst(zone, grant):
    source, person, a, b = snapshot(grant, zone=zone, first='2026-10-24', last='2026-10-27')
    condition = {'field': 'date', 'op': 'is', 'value': {'kind': 'absolute', 'value': '2026-10-25'}}
    scope = {'conditions': {'conditions': [condition]}}
    if grant != 'none':
        scope['conditions']['conditions'].append({'field': 'capability_ids', 'values': [SKILL]})
    else:
        source['types'], source['locations'], source['services'] = {}, {}, {}
    assert conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')
    assert_minimum(source, person, scope, True)
    condition['value']['value'] = '2026-10-27'
    assert not conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')


@pytest.mark.parametrize('op', ['empty', 'not_empty'])
@pytest.mark.parametrize('covered', [False, True])
@pytest.mark.parametrize('grant', ['clinic', 'none'])
def test_date_set_presence_uses_boundaries_from_all_sets(op, covered, grant):
    source, person, a, b = snapshot(grant)
    if grant == 'none':
        source['types'], source['locations'], source['services'] = {}, {}, {}
    spans = [('2026-10-04', '2026-10-05'), ('2026-10-06' if covered else '2026-10-07', '2026-10-10')]
    source['date_sets'] = {U(f'witness-set-{n}'): {'id': U(f'witness-set-{n}'), 'name': f'Set {n}', 'entries': [
        {'id': U(f'witness-entry-{n}'), 'name': 'Days', 'repeat': 'once', 'first_day': first, 'last_day': last}]}
        for n, (first, last) in enumerate(spans)}
    scope = {'conditions': {'conditions': [{'field': 'date_set_ids', 'op': op}]}}
    if grant == 'clinic':
        scope['conditions']['conditions'].append({'field': 'capability_ids', 'values': [SKILL]})
    expected = op == 'not_empty' or not covered
    assert conditions.scope_has_duty_witness(source, scope, person, a, b, 'd') is expected
    assert_minimum(source, person, scope, expected)


def test_nested_count_bucket_preserves_date_and_empty_skill_witnesses():
    source, person, _, _ = snapshot()
    scope = {'conditions': {'conditions': [
        {'field': 'date', 'op': 'is', 'value': '2026-10-06'},
        {'field': 'capability_ids', 'op': 'empty'},
        {'field': 'service_ids', 'op': 'any', 'values': CLINICS, 'count': 'each'}]}}
    source['_quota_policies'] = conditions.counting_views(source, [quota(source, scope)])
    report = assert_minimum(source, person, scope, True)
    assert conditions.bucket_atoms(report['rows'][0]['count_bucket'], 'service_ids') == (CLINICS[1],)


def test_transitive_override_scope_keeps_date_and_skill_presence_witnesses():
    source, person, a, b = snapshot()
    leaf = quota(source, {'conditions': {'conditions': [
        {'field': 'date', 'op': 'is', 'value': '2026-10-06'},
        {'field': 'capability_ids', 'op': 'empty'}]}}, 'Leaf')
    middle = quota(source, {'conditions': {'conditions': [
        {'field': conditions.OVERRIDDEN, 'values': [leaf['id']]}]}}, 'Middle')
    source['policies'] = [middle, leaf]
    scope = {'conditions': {'conditions': [{'field': conditions.OVERRIDDEN, 'values': [middle['id']]}]}}
    assert conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')
    source['grants'][0]['service_id'] = None
    assert not conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')


@pytest.mark.parametrize('bound', ['limit', 'budget'])
def test_date_projection_cannot_certify_unexamined_witnesses_when_bound_exhausted(bound):
    source, person, a, b = snapshot('none')
    scope = {'conditions': {'conditions': [{'field': 'date', 'op': 'is', 'value': '2026-10-06'}]}}
    with pytest.raises(conditions.ScopeContextLimit, match='narrower scope|reconciliation'):
        conditions.scope_has_duty_witness(source, scope, person, a, b, 'd', **{bound: 1 if bound == 'limit' else [1]})


@pytest.mark.parametrize('join', ['unsupported_only', 'any', 'all'])
@pytest.mark.parametrize('calendar', ['outside_date', 'empty_date', 'outside_date_set'])
def test_calendar_branch_cannot_hide_an_unsupported_clinic_context(join, calendar):
    source, person, a, b = snapshot()
    blocked = U('unsupported-witness-location')
    source['locations']['room']['location_type_id'] = 'permitted'
    source['locations'][blocked] = {'department_id': 'd', 'parent_id': None, 'location_type_id': 'blocked'}
    source['types']['T']['definition']['allowed_location_type_ids'] = ['permitted']
    unsupported = {'field': 'location_ids', 'values': [blocked]}
    if calendar == 'outside_date_set':
        set_id = U('outside-witness-date-set')
        source['date_sets'] = {set_id: {'id': set_id, 'name': 'Outside', 'entries': [
            {'id': U('outside-witness-entry'), 'name': 'Outside', 'repeat': 'once',
             'first_day': '2026-11-01', 'last_day': '2026-11-01'}]}}
        excluded = {'field': 'date_set_ids', 'values': [set_id]}
    else:
        excluded = ({'field': 'date', 'op': 'empty'} if calendar == 'empty_date' else
                    {'field': 'date', 'op': 'is', 'value': '2026-10-11'})
    selection = unsupported if join == 'unsupported_only' else {
        'match': join, 'conditions': [excluded, unsupported]}
    scope = {'conditions': {'conditions': [{'field': 'capability_ids', 'values': [SKILL]}, selection]}}
    if join == 'all':
        # Calendar exclusion proves the conjunction impossible regardless of physical setup.
        assert not conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')
        assert_minimum(source, person, scope, False)
    else:
        # The unsupported branch could still select a duty; do not certify an empty mandatory minimum.
        with pytest.raises(conditions.ScopeContextLimit, match='Duty setup'):
            conditions.scope_has_duty_witness(source, scope, person, a, b, 'd')
        source['policies'] = [quota(source, scope)]
        findings = []
        report = quotas.evaluate_quotas(source, lambda code, *args, **kwargs: findings.append(code),
                                        ancestor_ids, lambda *args: None)
        assert report['complete'] is False
        assert findings == ['QUOTA_SCOPE_LIMIT']


@pytest.mark.parametrize('normalized', [False, True])
def test_full_window_proration_reuses_the_subject_witness(monkeypatch, normalized):
    source, person, _, _ = snapshot()
    source['rotations'] = []
    scope = {'conditions': {'conditions': [{'field': 'person_ids', 'values': [person]}]}}
    if normalized:
        scope = Scope.model_validate(scope).model_dump(mode='json')
    calls, witness = [], quotas.scope_has_duty_witness
    def counted(*args, **kwargs):
        calls.append((args[2], args[3], args[4]))
        return witness(*args, **kwargs)
    monkeypatch.setattr(quotas, 'scope_has_duty_witness', counted)
    report = assert_minimum(source, person, scope, True)
    assert len(calls) == 1
    assert report['rows'][0]['amount'] == 1 and not report['rows'][0].get('proration')


@pytest.mark.parametrize('flat', [False, True])
def test_late_skill_grants_do_not_prorate_full_window_minimum(flat, monkeypatch):
    source, person, _, _ = snapshot()
    source['grants'][0]['starts_on'] = '2026-10-10'  # Saturday qualification does not end departmental membership.
    scope = ({'capability_ids': [SKILL]} if flat else
             {'conditions': {'conditions': [{'field': 'capability_ids', 'op': 'not_empty'}]}})
    source['policies'] = [quota(source, scope)]
    source['policies'][0]['definition']['rule']['amount'] = 3
    calls, witness = [], quotas.scope_has_duty_witness
    def counted(*args, **kwargs):
        calls.append(args[2])
        return witness(*args, **kwargs)
    monkeypatch.setattr(quotas, 'scope_has_duty_witness', counted)
    findings = []
    report = quotas.evaluate_quotas(source, lambda code, *args, **kwargs: findings.append(code),
                                     ancestor_ids, lambda *args: None)
    assert report['complete']
    [row] = report['rows']
    assert (row['amount'], row['actual'], row['status']) == (3, 0, 'below_minimum')
    assert not row.get('proration') and findings == ['QUOTA_BELOW_MINIMUM']
    assert calls == [person]


@pytest.mark.parametrize('tree,expected', [
    ({'conditions': [{'field': 'date', 'op': 'is', 'value': '2026-10-06'}]}, True),
    ({'conditions': [{'field': 'capability_ids', 'op': 'not_empty'}]}, True),
    ({'match': 'any', 'conditions': [
        {'field': 'date', 'op': 'is', 'value': '2026-10-06'},
        {'field': 'category_ids', 'values': ['R1']}]}, True),
    ({'match': 'all', 'conditions': [
        {'conditions': [{'field': 'date', 'op': 'is', 'value': '2026-10-06'}]},
        {'field': 'category_ids', 'values': ['R2']}]}, True),
    ({'match': 'all', 'conditions': [
        {'field': 'date', 'op': 'is', 'value': '2026-10-06'},
        {'match': 'any', 'conditions': []}]}, False),
    ({'match': 'any', 'conditions': [
        {'match': 'all', 'conditions': []}, {'field': 'category_ids', 'values': ['R2']}]}, True),
    ({'match': 'all', 'conditions': []}, False),
    ({'match': 'any', 'conditions': []}, False),
])
def test_quota_day_projection_preserves_person_and_empty_group_selection(tree, expected):
    source, person, a, b = snapshot('none')
    scope = conditions.quota_day_scope({'conditions': tree})
    assert conditions.scope_applies(source, scope, person, None, a, b) is expected


@pytest.mark.parametrize('count_field', ['capability_ids', 'service_ids'])
@pytest.mark.parametrize('date_scoped', [False, True])
def test_late_skill_count_buckets_keep_full_window_minimum(count_field, date_scoped):
    source, person, _, _ = snapshot()
    source['grants'][0]['starts_on'] = '2026-10-10'
    nodes = [{'field': 'capability_ids', 'values': [SKILL]}]
    if count_field == 'capability_ids':
        nodes[0]['count'] = 'each'
    else:
        nodes.append({'field': 'service_ids', 'values': CLINICS, 'count': 'each'})
    if date_scoped:
        nodes.insert(0, {'field': 'date', 'op': 'is', 'value': '2026-10-10'})
    source['policies'] = [quota(source, {'conditions': {'conditions': nodes}})]
    source['policies'][0]['definition']['rule']['amount'] = 3
    report = quotas.evaluate_quotas(source, lambda *args, **kwargs: None, ancestor_ids, lambda *args: None)
    assert report['complete']
    [row] = report['rows']
    assert conditions.bucket_atoms(row['count_bucket'], count_field) == ((SKILL,) if count_field == 'capability_ids' else (CLINICS[0],))
    assert (row['amount'], row['actual'], row['status']) == (3, 0, 'below_minimum')
    assert not row.get('proration')


def test_quota_presence_projection_keeps_each_rank_buckets_own_dated_membership():
    source, person, _, _ = snapshot('none')
    for membership in source['memberships']:
        membership.update({'ends_on': '2026-10-07'} if membership['category_id'] == 'R1'
                          else {'starts_on': '2026-10-07'})
    scope = {'conditions': {'conditions': [{'field': 'category_ids', 'values': ['R1', 'R2'], 'count': 'each'}]}}
    source['policies'] = [quota(source, scope)]
    source['policies'][0]['definition']['rule']['amount'] = 6
    report = quotas.evaluate_quotas(source, lambda *args, **kwargs: None, ancestor_ids, lambda *args: None)
    assert report['complete'] and len(report['rows']) == 2
    assert {conditions.bucket_atoms(row['count_bucket'], 'category_ids'): row['amount']
            for row in report['rows']} == {('R1',): 4, ('R2',): 3}
    assert {row['person_id'] for row in report['rows']} == {person}


@pytest.mark.parametrize('group', ['all_each', 'any_each', 'any_mixed', 'all_values'])
@pytest.mark.parametrize('person', ['first', 'second'])
def test_quota_day_projection_preserves_bucket_person_and_group_selection(group, person):
    people = {'field': 'person_ids', 'values': ['first', 'second'], 'count': 'each'}
    ranked = {'field': 'category_ids', 'values': ['rank']}
    tree = ({'match': 'all' if group == 'all_each' else 'any', 'count': 'each',
             'conditions': [people, ranked]} if group != 'any_mixed' else
            {'match': 'any', 'conditions': [people, {'field': 'person_ids', 'values': ['second']}]})
    if group == 'all_values':
        tree = {'conditions': [{'field': 'category_ids', 'values': ['rank', 'other'], 'op': 'all', 'count': 'each'}]}
    holds = lambda field, values=(): ({person} if field == 'person_ids' else
                                     {'rank', 'other'} if group == 'all_values' and person == 'second' else {'rank'})
    for key in conditions.bucket_keys(tree, lambda field, values=(): None):
        scope = {'conditions': {'conditions': [{'field': conditions.COUNT_BUCKET, 'values': [json.dumps([tree, key])]}]}}
        projected = conditions.scope_tree(conditions.quota_day_scope(scope))
        assert conditions.evaluate(projected, holds) == (key in conditions.bucket_keys(tree, holds))


def test_quota_presence_each_all_rank_gate_starts_only_when_both_memberships_overlap():
    source, person, _, _ = snapshot('none')
    for membership in source['memberships']:
        membership.pop('ends_on', None)
        membership['starts_on'] = '2026-10-07' if membership['category_id'] == 'R2' else '2020-01-01'
    scope = {'conditions': {'conditions': [{'field': 'category_ids', 'values': ['R1', 'R2'],
                                          'op': 'all', 'count': 'each'}]}}
    source['policies'] = [quota(source, scope)]
    source['policies'][0]['definition']['rule']['amount'] = 6
    report = quotas.evaluate_quotas(source, lambda *args, **kwargs: None, ancestor_ids, lambda *args: None)
    assert report['complete'] and len(report['rows']) == 2
    assert all(row['amount'] == 3 and row['proration']['working_days'] == 2 for row in report['rows'])
    assert {row['person_id'] for row in report['rows']} == {person}
