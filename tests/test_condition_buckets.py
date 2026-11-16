"""One condition tree supplies matching paths and bounded, deterministic counting buckets."""
import copy
import gc
import hashlib
import json
import time
from collections import Counter

import coverage
import pytest

from packages.domain.conditions import (TAG_FIELDS, bucket_keys, counting_views, evaluate,
                                        bucket_atoms, bucket_count, has_each, has_person_lines, matched_paths, rule_holds,
                                        tag_scope, with_tags)
from packages.domain.rule_map import rule_map
from .test_rule_conditions import NOV, OCT, SEPT, SHIFT, data, line, tree
from .test_rule_map import big_department


@pytest.mark.parametrize('field,value,expected', [
    ('category_ids', 'R1', [True, False, False]),
    ('category_ids', 'R2', [False, True, True]),
    ('capability_ids', 'S', [False, True, True]),
    ('programme_ids', 'P', [False, True, False]),
    ('rotation_type_ids', 'RT', [False, True, False]),
    ('cohort_ids', 'C', [False, True, False]),
    ('tags', 'night', [True, True, True]),
    ('person_ids', 'p', [True, True, True]),
    ('service_ids', 'clinic', [True, True, True]),
    ('location_ids', 'wing', [True, True, True]),
    ('assignment_type_ids', 'T', [True, True, True]),
])
@pytest.mark.parametrize('op', ['any', 'none'])
def test_dated_fixture_matches_and_default_buckets_are_unchanged(field, value, expected, op):
    conditions = tree(line(field, value, op=op))
    before = json.dumps(conditions, sort_keys=True)
    expected = expected if op == 'any' else [not hit for hit in expected]
    for when, hit in zip((SEPT, OCT, NOV), expected):
        holds = rule_holds(data(), 'p', SHIFT, *when)
        assert evaluate(conditions, holds) is hit
        assert bucket_keys(conditions, holds) == (((),) if hit else ())
    assert not has_each(conditions)
    assert json.dumps(conditions, sort_keys=True) == before


def test_all_tags_requires_both_names_and_resolves_tag_ids_without_an_impossible_extra_value():
    source = data()
    source['tags'] = {
        'night-tag': {'name': 'Night_Capable', 'members': {'people': ['p']}},
        'senior-tag': {'name': 'Senior', 'members': {'people': ['p', 'q']}},
    }
    source = with_tags(source)
    conditions = tree(line('tags', 'night-tag', 'senior-tag', op='all'))
    resolved = tag_scope(source, {'conditions': conditions})['conditions']
    assert resolved['conditions'][0]['values'] == ['Night_Capable', 'Senior']
    assert evaluate(resolved, rule_holds(source, 'p', SHIFT, *OCT))
    assert not evaluate(resolved, rule_holds(source, 'q', SHIFT, *OCT))
    assert bucket_keys(resolved, rule_holds(source, 'p', SHIFT, *OCT)) == ((),)
    assert conditions['conditions'][0]['values'] == ['night-tag', 'senior-tag']


@pytest.mark.parametrize('op,expected', [('all', False), ('any', False), ('none', True)])
@pytest.mark.parametrize('count', ['together', 'each'])
def test_empty_tag_members_match_nobody_and_exclude_nobody(op, expected, count):
    source = data()
    source['tags'] = {'empty': {'name': 'Empty', 'members': {}}}
    settings = {'count': count} if op != 'none' else {}
    stored = tree({**line('programme_ids', 'empty', op=op), **settings})
    resolved = tag_scope(source, {'conditions': stored})['conditions']
    for person in ('p', 'q'):
        holds = rule_holds(source, person, SHIFT, *OCT)
        assert evaluate(resolved, holds) is expected
        assert bucket_keys(resolved, holds) == (((),) if expected else ())
        assert evaluate(tree(line('programme_ids', op=op)), holds) is expected


def test_bucket_labels_select_only_the_named_field_and_its_own_each_branch():
    key = (('0:group', '1'), ('0.1.0:location_ids', 'wing'), ('1:service_ids', 'clinic'))
    assert bucket_atoms(key, 'location_ids') == ('wing',)
    assert bucket_atoms(key, 'tags') == ()
    assert bucket_count(key, 'location_ids', ['0.1.0']) == 'each'
    assert bucket_count(key, 'tags', ['0.1.2']) == 'each'
    assert bucket_count(key, 'tags', ['0.10', '1']) == 'together'
    assert bucket_count(((':group', '0'),), 'tags', ['0.1']) == 'each'
    assert bucket_count((), 'location_ids', ['0']) == 'together'


def test_bucket_keys_are_cached_across_views_only_within_the_same_check_context(monkeypatch):
    from packages.domain import conditions
    from packages.domain.validation import scope_matches
    source = data()
    source['_bucket_keys_cache'] = {}
    source['people']['p']['tags'] = ['Night', 'Senior']
    policy = {'id': 'cover', 'definition': {'scope': {
        'conditions': tree({**line('tags', 'Night', 'Senior'), 'count': 'each'})},
        'rule': {'kind': 'minimum_cover', 'minimum': 1}}}
    views = counting_views(source, [policy])
    original, calls = conditions._buckets, []
    check_budget, checks = conditions.check_count_bucket_budget, []
    def budget(node, label):
        checks.append(node)
        return check_budget(node, label)
    monkeypatch.setattr(conditions, 'check_count_bucket_budget', budget)
    def buckets(node, holds, path, constrained=False):
        if not path:
            calls.append(node)
        return original(node, holds, path, constrained)
    monkeypatch.setattr(conditions, '_buckets', buckets)
    def matched(check, person='p', shift=SHIFT, when=OCT):
        return [scope_matches(check, view['definition']['scope'], person, shift, *when) for view in views]
    assert matched(source) == [True, True]
    assert matched(source) == [True, True]
    assert len(calls) == 1
    assert len(checks) == 1
    assert matched(source, person='q') == [False, False]
    assert matched(source, shift=dict(SHIFT)) == [True, True]
    assert matched(source, when=SEPT) == [True, True]
    assert len(calls) == 4
    assert len(checks) == 1, 'An immutable tree is budget-checked once across people, sessions and intervals'
    next_check = {**source, '_bucket_keys_cache': {}, 'people': {'p': {'tags': ['Senior']}}}
    assert matched(next_check) == [False, True]
    assert len(calls) == 5
    assert len(checks) == 2
    uncached = {k: v for k, v in next_check.items() if k != '_bucket_keys_cache'}
    assert matched(uncached) == [False, True]
    assert len(calls) == 7
    assert len(checks) == 4


def test_session_only_bucket_keys_reuse_one_result_across_people(monkeypatch):
    from packages.domain import conditions
    source = {**data(), '_bucket_keys_cache': {}}
    selected = tree({**line('service_ids', 'clinic', 'other'), 'count': 'each'})
    original, calls = conditions._buckets, []
    def buckets(node, holds, path, constrained=False):
        if not path:
            calls.append(node)
        return original(node, holds, path, constrained)
    monkeypatch.setattr(conditions, '_buckets', buckets)
    for person in ('p', 'q', None):
        assert bucket_keys(selected, rule_holds(source, person, SHIFT, *OCT)) == ((('0:service_ids', 'clinic'),),)
    assert len(calls) == 1


@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('method', ['policy_applies', 'workload_matches'])
def test_counted_policy_cache_reuses_session_filters_without_reusing_person_answers(monkeypatch, mixed, method):
    from packages.domain import validation
    source = {**data(), '_bucket_keys_cache': {}, '_policy_applies_cache': {}, '_policy_applies_people': True,
              '_workload_matches_cache': {}}
    shift = {**SHIFT, 'id': 'duty'}
    children = [{**line('service_ids', 'clinic', 'other'), 'count': 'each'}]
    if mixed:
        children.append(tree(line('tags', 'Night')))
    policy = {'id': 'cover', 'department_id': 'd', 'status': 'active',
              'definition': {'scope': {'conditions': tree(*children)}, 'rule': {'kind': 'minimum_cover', 'minimum': 1}}}
    view = next(v for v in counting_views(source, [policy]) if v['count_bucket'][-1][1] == 'clinic')
    assert validation.person_free(view['definition']['scope']) is not mixed
    checked = '_policy_applies' if method == 'policy_applies' else 'scope_applies'
    original, calls = getattr(validation, checked), []
    def applies(*args):
        calls.append(args[2])
        return original(*args)
    monkeypatch.setattr(validation, checked, applies)
    matches = getattr(validation, method)
    assert matches(source, view, 'p', shift, *OCT)
    assert matches(source, view, 'q', shift, *OCT) is not mixed
    assert matches(source, view, 'p', shift, *OCT)
    assert len(calls) == (2 if mixed else 1)


@pytest.mark.parametrize('external', [False, True])
def test_workload_match_cache_keeps_dated_membership_and_fresh_snapshot_answers(monkeypatch, external):
    from packages.domain import validation
    source = {**data(), '_workload_matches_cache': {}}
    policy = {'definition': {'scope': {'conditions': tree(line('programme_ids', 'P'))}}}
    shift = None if external else SHIFT
    original, calls = validation.scope_applies, []
    def matches(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(validation, 'scope_applies', matches)
    for when, expected in zip((SEPT, OCT, NOV), (False, True, False)):
        assert validation.workload_matches(source, policy, 'p', shift, *when) is expected
        assert validation.workload_matches(source, policy, 'p', shift, *when) is expected
    assert len(calls) == 3
    fresh = {**source, 'programme_memberships': [], '_workload_matches_cache': {}}
    assert not validation.workload_matches(fresh, policy, 'p', shift, *OCT)


def test_internal_person_selectors_remain_person_dependent():
    from packages.domain.conditions import COUNT_BUCKET, INTERNAL
    for field in INTERNAL - {COUNT_BUCKET}:
        assert has_person_lines(tree(line(field, 'source')))


@pytest.mark.parametrize('op', ['any', 'all'])
def test_each_line_counts_every_matched_value_once_with_sorted_case_normalized_keys(op):
    conditions = tree({**line('tags', 'Senior', 'Night', 'Senior', op=op), 'count': 'each'})
    holds = lambda field: {'night', 'senior'}
    expected = ((('0:tags', 'night'),), (('0:tags', 'senior'),))
    assert has_each(conditions)
    assert [bucket_keys(conditions, holds) for _ in range(10)] == [expected] * 10
    assert bucket_keys(conditions, lambda field: {'night'}) == (
        ((('0:tags', 'night'),),) if op == 'any' else ())


def test_seven_duties_are_seven_together_or_four_in_each_overlapping_bucket():
    together = tree(line('tags', 'A', 'B'))
    each = tree({**line('tags', 'A', 'B'), 'count': 'each'})
    duties = [{'a'}] * 3 + [{'b'}] * 3 + [{'a', 'b'}]
    counts = lambda conditions: Counter(key for held in duties for key in bucket_keys(conditions, lambda field: held))
    assert counts(together) == {(): 7}
    assert counts(each) == {(('0:tags', 'a'),): 4, (('0:tags', 'b'),): 4}
    assert max(counts(together).values()) > 6 and max(counts(each).values()) <= 6


@pytest.mark.parametrize('match', ['all', 'any'])
def test_each_group_uses_matching_child_branches_after_its_boolean_gate(match):
    conditions = {**tree(line('tags', 'A'), line('capability_ids', 'S'), match=match), 'count': 'each'}
    all_held = lambda field: {'a'} if field == 'tags' else {'S'}
    assert bucket_keys(conditions, all_held) == (((':group', '0'),), ((':group', '1'),))
    tags_only = lambda field: {'a'} if field == 'tags' else set()
    assert bucket_keys(conditions, tags_only) == ((((':group', '0'),),) if match == 'any' else ())


def test_nested_and_combines_each_nodes_and_or_keeps_the_matching_path():
    choices = tree({**line('service_ids', 'B', 'A'), 'count': 'each'}, line('service_ids', 'C'), match='any')
    conditions = tree(choices, {**line('tags', 'senior', 'night'), 'count': 'each'})
    holds = lambda field: {'A', 'B'} if field == 'service_ids' else {'senior', 'night'}
    assert bucket_keys(conditions, holds) == (
        (('0.0:service_ids', 'A'), ('1:tags', 'night')),
        (('0.0:service_ids', 'A'), ('1:tags', 'senior')),
        (('0.0:service_ids', 'B'), ('1:tags', 'night')),
        (('0.0:service_ids', 'B'), ('1:tags', 'senior')),
    )
    assert matched_paths(conditions, holds) == ['0.0', '1']


def test_each_group_includes_only_the_each_values_on_its_selected_child_path():
    conditions = {**tree(line('tags', 'A'), {**line('service_ids', 'B', 'C'), 'count': 'each'}), 'count': 'each'}
    holds = lambda field: {'a'} if field == 'tags' else {'B', 'C'}
    assert bucket_keys(conditions, holds) == (
        ((':group', '0'),),
        ((':group', '1'), ('1:service_ids', 'B')),
        ((':group', '1'), ('1:service_ids', 'C')),
    )


def test_any_matching_multiple_together_branches_counts_once_in_the_shared_bucket():
    conditions = tree(line('tags', 'A'), tree(line('tags', 'B')), match='any')
    holds = lambda field: {'a', 'b'}
    assert bucket_keys(conditions, holds) == ((),)
    assert matched_paths(conditions, holds) == ['0', '1.0']


def test_reached_paths_belong_to_the_requested_bucket_only():
    choices = {**tree(line('service_ids', 'clinic'), line('location_ids', 'wing'), match='any'), 'count': 'each'}
    conditions = tree(choices, line('person_ids', 'q', op='none'))
    holds = rule_holds(data(), 'p', SHIFT, *OCT)
    keys = bucket_keys(conditions, holds)
    assert keys == ((('0:group', '0'),), (('0:group', '1'),))
    assert [matched_paths(conditions, holds, key) for key in keys] == [['0.0'], ['0.1']]


def test_unknown_counted_fields_reject_impossible_branches_without_changing_legacy_projection():
    conditions = tree(line('service_ids', 'A'), line('service_ids', 'A', op='none'))
    unknown = lambda field: None
    assert evaluate(conditions, unknown) and bucket_keys(conditions, unknown) == ((),)
    conditions['conditions'][0]['count'] = 'each'
    assert bucket_keys(conditions, unknown) == ()
    assert matched_paths(conditions, unknown) == []
    policy = {'id': 'impossible', 'definition': {'scope': {'conditions': conditions},
              'rule': {'kind': 'block_quota', 'metric': 'duties', 'comparison': 'minimum', 'amount': 1}}}
    assert counting_views(data(), [policy]) == []


def test_unknown_any_alternatives_keep_their_exclusions_on_their_own_branches():
    conditions = tree(
        tree({**line('service_ids', 'A'), 'count': 'each'}, line('service_ids', 'B', op='none')),
        tree({**line('service_ids', 'B'), 'count': 'each'}, line('service_ids', 'A', op='none')), match='any')
    assert bucket_keys(conditions, lambda field: None) == (
        (('0.0:service_ids', 'A'),), (('1.0:service_ids', 'B'),))


def test_unknown_multi_held_fields_can_satisfy_disjoint_positive_clauses():
    conditions = tree({**line('service_ids', 'A'), 'count': 'each'}, line('service_ids', 'B'))
    assert bucket_keys(conditions, lambda field: None) == ((('0:service_ids', 'A'),),)
    assert bucket_keys(conditions, lambda field: {'A', 'B'}) == ((('0:service_ids', 'A'),),)
    assert bucket_keys(conditions, lambda field: {'A'}) == ()


@pytest.mark.parametrize('field', ['person_ids', 'assignment_type_ids'])
def test_unknown_single_held_fields_require_one_compatible_value(field):
    conditions = tree({**line(field, 'A', 'B'), 'count': 'each'}, line(field, 'B', 'C'))
    assert bucket_keys(conditions, lambda field: None) == (((f'0:{field}', 'B'),),)
    conditions['conditions'][1] = line(field, 'C')
    assert bucket_keys(conditions, lambda field: None) == ()


def test_unknown_all_forced_values_cannot_be_excluded():
    conditions = tree({**line('tags', 'Night', 'Senior', op='all'), 'count': 'each'}, line('tags', 'Senior', op='none'))
    assert bucket_keys(conditions, lambda field: None) == ()
    conditions['conditions'][1] = line('tags', 'Other', op='none')
    assert bucket_keys(conditions, lambda field: None) == ((('0:tags', 'night'),), (('0:tags', 'senior'),))


def test_unknown_each_group_all_keeps_the_complete_gate_for_every_child_bucket():
    conditions = {**tree(line('service_ids', 'A'), line('service_ids', 'A', op='none')), 'count': 'each'}
    assert bucket_keys(conditions, lambda field: None) == ()
    conditions['conditions'][1] = line('service_ids', 'B')
    assert bucket_keys(conditions, lambda field: None) == (((':group', '0'),), ((':group', '1'),))


def test_unknown_locations_use_one_listed_places_ancestor_chain_without_enumerating_records():
    source = data()
    source['locations']['elsewhere'] = {'parent_id': None}
    holds = rule_holds(source, None, None, *OCT)
    conditions = tree({**line('location_ids', 'wing', 'elsewhere'), 'count': 'each'}, line('location_ids', 'room'))
    assert bucket_keys(conditions, holds) == ((('0:location_ids', 'wing'),),)
    conditions['conditions'][1] = line('location_ids', 'wing', op='none')
    assert bucket_keys(conditions, holds) == ((('0:location_ids', 'elsewhere'),),)
    conditions['conditions'][0]['values'] = ['wing']
    assert bucket_keys(conditions, holds) == ()


def test_unknown_rank_buckets_remain_possible_when_no_current_person_holds_the_rank():
    conditions = tree({**line('category_ids', 'R1', 'R2'), 'count': 'each'})
    holds = rule_holds(data(), None, SHIFT, *SEPT)
    assert bucket_keys(conditions, holds) == ((('0:category_ids', 'R1'),), (('0:category_ids', 'R2'),))


@pytest.mark.parametrize('flat,field,value', [
    ({'assignment_type_id': 'T'}, 'assignment_type_ids', 'U'),
    ({'person_id': 'p'}, 'person_ids', 'q'),
    ({'location_id': 'wing'}, 'location_ids', 'elsewhere'),
])
def test_counting_views_prune_unknown_contradictions_with_legacy_flat_scope_fields(flat, field, value):
    source = data()
    source['locations']['elsewhere'] = {'parent_id': None}
    policy = {'id': 'impossible', 'definition': {'scope': {
        **flat, 'conditions': tree({**line(field, value), 'count': 'each'})}, 'rule': {'kind': 'minimum_cover', 'minimum': 1}}}
    assert counting_views(source, [policy]) == []


def test_appended_flat_constraints_preserve_bucket_paths_and_shared_clinic_possibility():
    for flat, field, value in [({'location_id': 'wing'}, 'location_ids', 'room'),
                               ({'service_ids': ['A']}, 'service_ids', 'B')]:
        policy = {'id': 'possible', 'definition': {'scope': {
            **flat, 'conditions': tree({**line(field, value), 'count': 'each'})}, 'rule': {'kind': 'max_duties', 'maximum': 6}}}
        views = counting_views(data(), [policy])
        assert [view['count_bucket'] for view in views] == [((f'0.0:{field}', value),)]
        assert views[0]['_count_scope'] is policy['definition']['scope']


def test_counting_views_retain_legacy_flat_programme_overlap_semantics():
    from packages.domain.conditions import scope_applies
    source = data()
    scope = {'programme_id': 'P', 'conditions': tree({**line('service_ids', 'clinic'), 'count': 'each'})}
    policy = {'id': 'overlap', 'definition': {'scope': scope, 'rule': {'kind': 'max_duties', 'maximum': 6}}}
    [view] = counting_views(source, [policy])
    crosses_membership_end = (OCT[0].replace(day=31, hour=22), NOV[0].replace(day=1, hour=2))
    assert scope_applies(source, scope, 'p', SHIFT, *crosses_membership_end)
    assert scope_applies(source, view['definition']['scope'], 'p', SHIFT, *crosses_membership_end)


def test_unknown_bucket_witnesses_equal_the_union_of_concrete_assignments():
    from itertools import product
    from random import Random
    rng = Random(19)
    subsets = [set(), {'a'}, {'b'}, {'a', 'b'}]
    profiles = [{'service_ids': services, 'tags': tags, 'assignment_type_ids': typ}
                for services, tags, typ in product(subsets, subsets, ({'a'}, {'b'}, {'other'}))]
    def random_line():
        field = rng.choice(['service_ids', 'tags', 'assignment_type_ids'])
        op = rng.choice(['any', 'none', 'all'] if field == 'tags' else ['any', 'none'])
        node = line(field, *rng.sample(['a', 'b'], rng.choice([1, 2])), op=op)
        if op != 'none' and rng.choice([False, True]):
            node['count'] = 'each'
        return node
    for _ in range(40):
        children = [random_line(), tree(random_line(), random_line(), match=rng.choice(['any', 'all']))]
        if rng.choice([False, True]):
            children[1]['count'] = 'each'
        conditions = {**tree(*children, match=rng.choice(['any', 'all'])), 'count': 'each'}
        possible = {key for held in profiles for key in bucket_keys(conditions, lambda field: held[field])}
        assert set(bucket_keys(conditions, lambda field: None)) == possible, conditions


@pytest.mark.parametrize('nested', [False, True])
def test_none_excludes_at_top_or_nested_and_and_never_appears_as_a_reached_record(nested):
    exclusion = line('person_ids', 'q', op='none')
    conditions = tree(line('tags', 'Night'), tree(exclusion) if nested else exclusion)
    source = data()
    source['people']['q']['tags'] = ['Night']
    assert bucket_keys(conditions, rule_holds(source, 'p', SHIFT, *OCT)) == ((),)
    assert matched_paths(conditions, rule_holds(source, 'p', SHIFT, *OCT)) == ['0']
    assert bucket_keys(conditions, rule_holds(source, 'q', SHIFT, *OCT)) == ()
    assert matched_paths(conditions, rule_holds(source, 'q', SHIFT, *OCT)) == []
    # Runtime remains harmless for an invalid legacy none/each node; contracts reject saving it.
    exclusion['count'] = 'each'
    assert bucket_keys(conditions, rule_holds(source, 'p', SHIFT, *OCT)) == ((),)


@pytest.mark.parametrize('field,kind', sorted(TAG_FIELDS.items()))
def test_each_tag_value_expands_into_member_records_of_the_lines_kind(field, kind):
    source = {'tags': {'group': {'name': 'Group', 'members': {kind: ['B', 'A', 'B'], 'unrelated': ['Z']}}}}
    stored = tree({**line(field, 'group', 'A'), 'count': 'each'})
    resolved = tag_scope(source, {'conditions': stored})['conditions']
    assert resolved['conditions'][0]['values'] == ['B', 'A']
    assert bucket_keys(resolved, lambda field: {'A', 'B', 'Z'}) == (
        ((f'0:{field}', 'A'),), ((f'0:{field}', 'B'),))
    assert stored['conditions'][0]['values'] == ['group', 'A']
    empty = tag_scope({'tags': {'group': {'name': 'Group', 'members': {}}}},
                      {'conditions': tree({**line(field, 'group'), 'count': 'each'})})['conditions']
    assert bucket_keys(empty, lambda field: None) == ()


def test_descendant_location_counts_in_the_listed_parent_bucket():
    conditions = tree({**line('location_ids', 'wing'), 'count': 'each'})
    assert bucket_keys(conditions, rule_holds(data(), 'p', SHIFT, *OCT)) == ((('0:location_ids', 'wing'),),)


def test_count_on_an_exclusion_is_ignored_for_projection_and_tag_resolution():
    original = tree(line('person_ids', 'p'), line('person_ids', 'p', op='none'))
    invalid = copy.deepcopy(original)
    invalid['conditions'][1]['count'] = 'each'
    assert not has_each(invalid)
    assert bucket_keys(invalid, lambda field: None) == bucket_keys(original, lambda field: None)
    source = {'tags': {'group': {'name': 'Group', 'members': {'services': ['A', 'B']}}}}
    original = {'conditions': tree(line('service_ids', 'group', op='none'))}
    invalid = copy.deepcopy(original)
    invalid['conditions']['conditions'][0]['count'] = 'each'
    assert tag_scope(source, invalid)['conditions']['conditions'][0]['values'] == \
        tag_scope(source, original)['conditions']['conditions'][0]['values']


def test_counting_views_preserves_legacy_objects_and_does_not_change_stored_each_definitions():
    legacy = {'id': 'old', 'definition': {'scope': {'conditions': tree(line('tags', 'Night'))}, 'rule': {'kind': 'max_hours', 'hours': 40}}}
    counted = {'id': 'new', 'definition': {'scope': {'conditions': tree({**line('service_ids', 'B', 'A'), 'count': 'each'})},
                                       'rule': {'kind': 'max_duties', 'maximum': 6}}}
    policies = [legacy, counted]
    stored = json.dumps(policies, sort_keys=True)
    views = counting_views(data(), policies)
    assert views[0] is legacy and views[0]['definition'] is legacy['definition']
    assert [view['count_bucket'] for view in views[1:]] == [
        (('0.0:service_ids', 'A'),), (('0.0:service_ids', 'B'),)]
    assert counting_views(data(), views) == views
    assert json.dumps(policies, sort_keys=True) == stored
    assert all(view['definition']['rule'] is counted['definition']['rule'] for view in views[1:])


@pytest.mark.parametrize('conditions', [
    tree({**line('service_ids', 'clinic', 'other'), 'count': 'each'}),
    {**tree(line('service_ids', 'clinic'), line('location_ids', 'wing')), 'count': 'each'},
    {**tree(line('service_ids', 'clinic'), line('service_ids', 'other'), match='any'), 'count': 'each'},
    tree(line('tags', 'Night', 'Senior', op='all')),
])
def test_old_scope_to_catalogue_line_round_trip_preserves_each_and_all_semantics(conditions):
    from packages.domain.rule_catalogue_migration import who_where
    from packages.domain.rule_lines import scope_of
    stored = copy.deepcopy(conditions)
    who, where = who_where({'conditions': conditions})
    restored = scope_of({'who': who, 'where': where})['conditions']
    assert restored == conditions
    for person in ('p', 'q'):
        holds = rule_holds(data(), person, SHIFT, *OCT)
        assert evaluate(restored, holds) == evaluate(conditions, holds)
        assert bucket_keys(restored, holds) == bucket_keys(conditions, holds)
    assert conditions == stored


def test_rule_grouping_never_treats_together_and_each_as_equivalent():
    from packages.domain.rule_groups import expanded, plan
    from .test_rule_simplification import SERVICE, WEEK_START, rule
    conditions = tree(line('service_ids', SERVICE['Clinic A'], SERVICE['Clinic B']))
    together = rule('Clinical duty cap', {'kind': 'max_duties', 'maximum': 6}, {'conditions': conditions})
    each = copy.deepcopy(together)
    each['id'] = '00000000-0000-4000-8000-999999999999'
    each['definition']['scope']['conditions']['conditions'][0]['count'] = 'each'
    assert expanded([together], {together['id']: 'same'}, WEEK_START) != expanded([each], {each['id']: 'same'}, WEEK_START)
    assert not any(set(group['rule_ids']) == {together['id'], each['id']} and 'payload' in group
                   for group in plan([together, each], week_start=WEEK_START))


@pytest.mark.parametrize('field', ['person_ids', 'service_ids'])
@pytest.mark.parametrize('match', ['all', 'any'])
def test_grouping_preserves_root_each_and_is_independent_of_policy_order(field, match):
    from packages.domain.rule_groups import expanded
    from .test_rule_simplification import PERSON, SERVICE, WEEK_START, rule
    values = list(PERSON.values())[:2] if field == 'person_ids' else list(SERVICE.values())[:2]
    together = rule('Restriction scope', {'kind': 'restriction'}, {
        'conditions': tree(line(field, values[0]), line(field, values[1]), match=match)})
    explicit_together = copy.deepcopy(together)
    explicit_together['definition']['scope']['conditions']['count'] = 'together'
    each = copy.deepcopy(together)
    each['id'] = '00000000-0000-4000-8000-999999999998'
    each['definition']['scope']['conditions']['count'] = 'each'
    mapping = {together['id']: 'same', each['id']: 'same'}
    counted = expanded([each], mapping, WEEK_START)
    combined = expanded([together], mapping, WEEK_START)
    assert counted != combined
    assert expanded([explicit_together], mapping, WEEK_START) == combined
    assert expanded([each, together], mapping, WEEK_START) == expanded([together, each], mapping, WEEK_START)
    duplicate = {**each, 'id': '00000000-0000-4000-8000-999999999997'}
    assert expanded([each, duplicate], {**mapping, duplicate['id']: 'same'}, WEEK_START) == counted


def test_grouping_preserves_repeated_each_nodes_and_their_independent_bucket_paths():
    from packages.domain.rule_groups import expanded
    from .test_rule_simplification import WEEK_START, rule
    counted = {**line('tags', 'A', 'B'), 'count': 'each'}
    single = rule('Rank cover', {'kind': 'minimum_cover', 'minimum': 1}, {'conditions': tree(counted)})
    repeated = copy.deepcopy(single)
    repeated['definition']['scope']['conditions']['conditions'].append(copy.deepcopy(counted))
    assert len(counting_views({}, [single])) == 2
    assert len(counting_views({}, [repeated])) == 4
    assert expanded([single], {single['id']: 'same'}, WEEK_START) != expanded([repeated], {single['id']: 'same'}, WEEK_START)


def test_full_department_rule_map_matches_the_current_dated_facts_golden_result():
    # Requalified the 919434f fixture for rotation-dated cohort membership; only 25 cohort-scoped rules change.
    result = rule_map(big_department())
    encoded = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
    assert len(result['rules']) == 974 and len(result['shifts']) == 531
    assert hashlib.sha256(encoded).hexdigest() == '4bd3dd4b354ec1be069a4a89728261f4510896ddad6ce3cff401eaf1eb8ad803'


@pytest.fixture(scope='module')
def department_bucket_expansion():
    source = {'people': {f'p{i}': {'tags': ['night']} for i in range(150)},
              'shifts': [{'id': f's{i}', 'service_ids': [f'clinic{i % 12}']} for i in range(500)],
              'services': {f'clinic{i}': {} for i in range(12)}}
    policies = [{'id': f'r{i}', 'definition': {
        'scope': {'conditions': tree(line('service_ids', 'clinic2', 'clinic0', 'clinic1'))},
        'rule': {'kind': 'max_duties', 'maximum': 6}}} for i in range(1000)]
    # Match scheduler budgets: exclude earlier fixtures' collection and other workers' wall-clock contention.
    gc.collect()
    began = time.process_time()
    together = counting_views(source, policies)
    baseline = time.process_time() - began
    each = copy.deepcopy(policies)
    for policy in each:
        policy['definition']['scope']['conditions']['conditions'][0]['count'] = 'each'
    gc.collect()
    began = time.process_time()
    expanded = counting_views(source, each)
    elapsed = time.process_time() - began
    print(f'condition buckets CPU: together={baseline:.4f}s each={elapsed:.4f}s; 1000 lines, 150 people, 500 sessions, 3000 views')
    return source, each, together, expanded, elapsed


def test_bucket_expansion_for_a_department_month_is_bounded_by_listed_values(department_bucket_expansion):
    source, each, together, expanded, _ = department_bucket_expansion
    assert len(together) == 1000 and len(expanded) == 3000
    expected = ((('0.0:service_ids', 'clinic0'),), (('0.0:service_ids', 'clinic1'),), (('0.0:service_ids', 'clinic2'),))
    assert all(tuple(view['count_bucket'] for view in expanded[n:n + 3]) == expected for n in range(0, 3000, 3))
    # More people and sessions do not create more buckets or alter their deterministic keys.
    larger = {**source, 'people': {**source['people'], **{f'p{i}': {} for i in range(150, 300)}},
              'shifts': source['shifts'] + [{'id': f's{i}'} for i in range(500, 1000)]}
    assert [view['count_bucket'] for view in counting_views(larger, each)] == [view['count_bucket'] for view in expanded]


@pytest.mark.cpu_budget
def test_bucket_expansion_for_a_department_month_is_bounded_by_listed_values_cpu_budget(department_bucket_expansion):
    *_, elapsed = department_bucket_expansion
    assert elapsed < (10 if coverage.Coverage.current() else 2), elapsed


@pytest.mark.parametrize('field,value,dated', [
    ('service_ids', 'clinic', False), ('person_ids', 'p', False), ('tags', 'night', False),
    ('category_ids', 'R1', True), ('programme_ids', 'P', True),
])
def test_counted_scopes_detect_only_their_actual_dated_person_filters(field, value, dated):
    from packages.domain.conditions import has_dated_person_lines, scope_tree
    policy = {'id': 'counted-scope', 'definition': {'rule': {'kind': 'minimum_cover', 'minimum': 1},
        'scope': {'conditions': tree(tree({**line(field, value), 'count': 'each'}))}}}
    [view] = counting_views({}, [policy])
    assert has_dated_person_lines(scope_tree(view['definition']['scope'])) is dated
