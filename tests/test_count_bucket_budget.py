"""Count-each admission and expansion fail closed before allocating over-budget buckets."""
import copy
import time
import tracemalloc

import pytest
from pydantic import ValidationError

from packages.contracts import schemas as s
from packages.contracts.condition_limits import MAX_COUNT_BUCKETS, count_bucket_bound
from packages.domain import conditions
from .test_condition_count_contracts import catalogue, condition, tree


def each(size, field='tags', prefix='v'):
    return condition(field, [f'{prefix}{n}' for n in range(size)], count='each')


def boundary(shape, over):
    if shape == 'and':
        return tree(tree(each(3 if over else 20)), tree(each(167 if over else 25, 'capability_ids')))
    if shape == 'or':
        return tree(tree(each(250)), tree(each(251 if over else 250, prefix='w')), match='any')
    if shape == 'and_each':
        return (tree(tree(each(1)), tree(each(1, 'capability_ids')), tree(each(167, 'programme_ids')), count='each')
                if over else tree(tree(each(10)), tree(each(25, 'capability_ids')), count='each'))
    if shape == 'or_each':
        return tree(tree(each(250)), tree(each(251 if over else 250, prefix='w')), match='any', count='each')
    if shape == 'ordinary_or':
        return tree(tree(*(condition('tags', [f'branch{n}']) for n in range(3 if over else 20)), match='any'),
                    each(167 if over else 25, 'capability_ids'))
    # A Together OR still supplies requirement variants to the surrounding AND.
    return tree(tree(each(166 if over else 10), each(1 if over else 10, prefix='w'), match='any'),
                each(3 if over else 25, 'capability_ids'))


@pytest.mark.parametrize('shape', ['and', 'or', 'and_each', 'or_each', 'ordinary_or', 'nested'])
@pytest.mark.parametrize('cached', [False, True])
def test_exact_limit_and_one_over_nested_count_trees(shape, cached, monkeypatch):
    allowed, excessive = boundary(shape, False), boundary(shape, True)
    assert count_bucket_bound(allowed) == MAX_COUNT_BUCKETS
    assert count_bucket_bound(excessive) == MAX_COUNT_BUCKETS + 1
    definition = {'rule': {'kind': 'minimum_cover', 'minimum': 1}, 'scope': {'conditions': allowed}}
    assert s.PolicyDefinition.model_validate(definition).scope.conditions.model_dump() == allowed
    def holds(_):
        return None
    if cached:
        holds.bucket_cache = {}
        holds.bucket_context = (None, None, None, None)
    keys = conditions.bucket_keys(allowed, holds)
    assert len(keys) == {'and_each': 35, 'ordinary_or': 25}.get(shape, MAX_COUNT_BUCKETS)
    policy = {'id': 'budget-line', 'definition': definition}
    assert len(conditions.counting_views({}, [policy])) == len(keys)
    definition['scope']['conditions'] = excessive
    with pytest.raises(ValidationError, match='Scope line.*at least 501.*limit is 500'):
        s.PolicyDefinition.model_validate(definition)

    def no_allocation(*args, **kwargs):
        pytest.fail('Over-budget tree reached bucket allocation')

    monkeypatch.setattr(conditions, '_buckets', no_allocation)
    monkeypatch.setattr(conditions, '_line_values', no_allocation)
    for reader in (conditions.bucket_keys, conditions.matched_paths):
        with pytest.raises(ValueError, match='at least 501'):
            reader(excessive, holds)
    if cached:
        assert id(excessive) not in holds.bucket_cache, 'Rejected trees must not enter the cache'
    with pytest.raises(ValueError, match='Line budget-line.*at least 501'):
        conditions.counting_views({}, [policy])


def test_original_twenty_by_five_hundred_growth_probe_is_rejected_without_allocation(monkeypatch):
    excessive = tree(*(each(500) for _ in range(20)))
    assert count_bucket_bound(excessive) == MAX_COUNT_BUCKETS + 1
    with pytest.raises(ValidationError, match='at least 501'):
        s.PolicyDefinition.model_validate({'rule': {'kind': 'minimum_cover', 'minimum': 1},
                                          'scope': {'conditions': excessive}})
    monkeypatch.setattr(conditions, 'product', lambda *args: pytest.fail('Product was constructed'))
    with pytest.raises(ValueError, match='at least 501'):
        conditions.bucket_keys(excessive, lambda _: None)
    together = copy.deepcopy(excessive)
    for child in together['conditions']:
        child.pop('count')
    assert count_bucket_bound(together) == 1
    assert s.PolicyDefinition.model_validate({'rule': {'kind': 'minimum_cover', 'minimum': 1},
                                            'scope': {'conditions': together}})


@pytest.mark.parametrize('over', [False, True])
def test_combined_catalogue_who_where_budget_names_the_line(over):
    definition = catalogue('people_per_session', who=tree(each(3 if over else 20)),
                           where=tree(each(167 if over else 25, 'service_ids')))
    line_id = definition['rule']['lines'][0]['id']
    if over:
        with pytest.raises(ValidationError, match=f'Line {line_id}.*at least 501'):
            s.PolicyDefinition.model_validate(definition)
    else:
        assert s.PolicyDefinition.model_validate(definition)


@pytest.mark.parametrize('count', [500, 501])
def test_resolved_tags_cannot_bypass_runtime_budget(count, monkeypatch):
    source = {'tags': {'clinics': {'name': 'Clinics', 'members': {'services': [f'c{n}' for n in range(count)]}}}}
    policy = {'id': 'tag-line', 'template_line': {'id': 'catalogue-line'}, 'definition': {
        'rule': {'kind': 'max_duties', 'maximum': 6},
        'scope': {'conditions': tree(condition('service_ids', ['clinics'], count='each'))}}}
    assert s.PolicyDefinition.model_validate(policy['definition'])
    if count == MAX_COUNT_BUCKETS:
        assert len(conditions.counting_views(source, [policy])) == count
    else:
        monkeypatch.setattr(conditions, '_buckets', lambda *args: pytest.fail('Buckets were allocated'))
        with pytest.raises(ValueError, match='Line catalogue-line.*at least 501'):
            conditions.counting_views(source, [policy])
        # An empty sibling does not make eagerly expanded oversized children safe.
        policy['definition']['scope']['conditions']['conditions'].append(condition('service_ids', ['empty'], count='each'))
        source['tags']['empty'] = {'name': 'Empty', 'members': {}}
        with pytest.raises(ValueError, match='at least 501'):
            conditions.counting_views(source, [policy])


@pytest.mark.parametrize('catalogued', [False, True])
def test_api_rejects_one_over_budget_with_actionable_line_and_count(h, catalogued):
    excessive = boundary('or', True)
    if catalogued:
        payload = catalogue('people_per_session', who=excessive)['rule']
        line_id = payload['lines'][0]['id']
        reply = h.command('coordinator', 'rule.propose', {'catalogue_id': payload['id'], 'lines': payload['lines'],
                                                        'reason': 'Synthetic bucket budget'}, status=422)
        assert line_id in str(reply)
    else:
        reply = h.command('coordinator', 'policy.create', {'name': 'Budget rejection', 'owner': 'Test',
            'source': 'Synthetic bucket budget', 'starts_on': '2020-01-01',
            'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 1},
                           'scope': {'conditions': excessive}}}, status=422)
        assert 'Scope line' in str(reply)
    assert 'at least 501' in str(reply) and 'limit is 500' in str(reply) and 'Select fewer' in str(reply)


def test_api_checks_resolved_tag_budget_before_saving(h, monkeypatch):
    from apps.api.services import policies
    tag = h.configure('tags', {'name': 'Clinics budget', 'labels': ['services']})['record']
    # Synthetic member expansion isolates the budget from unrelated catalogue/reference volume.
    monkeypatch.setattr(policies, 'tag_data', lambda *args: {
        tag['id']: {'name': tag['name'], 'members': {'services': [f'c{n}' for n in range(501)]}}})
    reply = h.command('coordinator', 'policy.create', {'name': 'Resolved budget rejection', 'owner': 'Test',
        'source': 'Synthetic bucket budget', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 6},
                      'scope': {'conditions': tree(condition('service_ids', [tag['id']], count='each'))}}}, status=422)
    assert reply['error']['code'] == 'VALUE_INVALID'
    assert 'Scope line' in str(reply) and 'at least 501' in str(reply)


@pytest.mark.parametrize('count', [500, 501])
def test_none_only_catalogue_line_checks_resolved_tags_before_admission(h, monkeypatch, count):
    from apps.api.services import policies
    tag = h.configure('tags', {'name': 'Clinics budget', 'labels': ['services']})['record']
    monkeypatch.setattr(policies, 'tag_data', lambda *args: {
        tag['id']: {'name': tag['name'], 'members': {'services': [f'c{n}' for n in range(count)]}}})
    line = catalogue('people_per_session', where=tree(condition('service_ids', [tag['id']], count='each')))['rule']['lines'][0]
    line['numbers']['min']['value'] = None
    reply = h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session', 'lines': [line],
                                                    'reason': 'Synthetic None-only budget'}, status=422 if count > 500 else 200)
    if count > 500:
        assert reply['error']['code'] == 'SOLVER_RULE_INCOMPATIBLE'
        assert line['id'] in str(reply) and 'at least 501' in str(reply)
    else:
        assert reply['record']['definition']['rule']['lines'][0]['id'] == line['id']


def test_saved_proposal_tag_growth_keeps_catalogue_readable_with_a_budget_issue(h, monkeypatch):
    from apps.api.services import policies
    tag = h.configure('tags', {'name': 'Clinics budget', 'labels': ['services']})['record']
    members = {'services': [h.seed['services'][0]]}
    monkeypatch.setattr(policies, 'tag_data', lambda *args: {tag['id']: {'name': tag['name'], 'members': members}})
    line = catalogue('people_per_session', where=tree(condition('service_ids', [tag['id']], count='each')))['rule']['lines'][0]
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'people_per_session', 'lines': [line],
                                           'reason': 'Synthetic tag growth'})
    members['services'] = [f'c{n}' for n in range(501)]
    entry = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'people_per_session')
    [issue] = entry['proposed']['check']['issues']
    assert issue['code'] == 'INVALID_RULE_DEFINITION' and line['id'] in issue['message'] and 'at least 501' in issue['message']


@pytest.fixture(scope='module')
def maximum_department_bucket_expansion():
    source = {'people': {f'p{n}': {} for n in range(150)}, 'shifts': [{'id': f's{n}'} for n in range(500)]}
    policy = {'id': 'maximum-line', 'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 1},
                                                'scope': {'conditions': boundary('and', False)}}}
    tracemalloc.start()
    began = time.process_time()
    try:
        views = conditions.counting_views(source, [policy])
        elapsed = time.process_time() - began
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f'500 buckets / 150 people / 500 sessions: {elapsed:.3f} CPU s, {peak / 1024 / 1024:.2f} MiB peak')
    return views, elapsed, peak


def test_maximum_bucket_line_at_department_scale(maximum_department_bucket_expansion):
    views, _, peak = maximum_department_bucket_expansion
    assert len(views) == MAX_COUNT_BUCKETS and len({v['count_bucket'] for v in views}) == MAX_COUNT_BUCKETS
    assert peak < 16 * 1024 * 1024


@pytest.mark.cpu_budget
def test_maximum_bucket_line_at_department_scale_cpu_budget(maximum_department_bucket_expansion):
    _, elapsed, _ = maximum_department_bucket_expansion
    assert elapsed < 2
