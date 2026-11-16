"""The Fix list's Filter (fix_list.ts causeMatches), from the real compiled client in Node: rule lines follow the shared
condition builder's truth (conditions.ts treeMatches / lineMatches) person by person, and the rule's population (`who`)
never meets a negative or an "all" line by naming one of the values. The Filter arrives as the collections' FilterState
and `who` as one tree per rule line (a7708038c, shared Filter)."""
import json
import subprocess

import pytest

WHO_A = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': ['A']}]}


def cause(people=('p1',)):
    return {'id': 'c1', 'level': 'must', 'checks': 1, 'sentence': 'Synthetic cause', 'meta': '', 'action': None, 'evidence': [],
            'score_gain': None, 'rule_lines': [], 'finding_keys': [], 'acknowledgeable': False, 'person_ids': list(people), 'who': [WHO_A]}


def tree(**line):
    return {'match': 'all', 'conditions': [{'field': 'category_ids', **line}]}


def matches(client_modules, facts, conditions, people=('p1',), filters=None):
    script = (f"const m=await import({json.dumps((client_modules/'fix_list.js').as_uri())});const a=JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(m.causeMatches(a.cause,{filters:a.filters||{},conditions:a.conditions},id=>a.facts[id]||{})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script,
                          json.dumps({'cause': cause(people), 'facts': facts, 'conditions': conditions, 'filters': filters})],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


@pytest.mark.parametrize('line', [{'op': 'is_not', 'value': 'A', 'values': []}, {'op': 'none', 'values': ['A']}],
                         ids=['is-not-value', 'none-of-values'])
def test_rank_is_not_a_is_false_for_a_person_whose_rank_is_a(client_modules, line):
    assert matches(client_modules, {'p1': {'category_ids': ['A']}}, tree(**line)) is False


def test_rank_has_all_a_and_b_is_false_for_a_person_with_only_a(client_modules):
    assert matches(client_modules, {'p1': {'category_ids': ['A']}}, tree(op='all', values=['A', 'B'])) is False


def test_the_same_lines_still_hold_where_their_truth_does(client_modules):
    """Not a weaker filter: each operator keeps its own truth (and `who` still meets a plain "is any of")."""
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, tree(op='none', values=['A'])) is True
    assert matches(client_modules, {'p1': {'category_ids': ['A', 'B']}}, tree(op='all', values=['A', 'B'])) is True
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, tree(op='any', values=['A'])) is True  # the population names A
    # Two people, one with A and one with B: nobody has all of A and B.
    assert matches(client_modules, {'p1': {'category_ids': ['A']}, 'p2': {'category_ids': ['B']}},
                   tree(op='all', values=['A', 'B']), people=('p1', 'p2')) is False


def test_the_plain_rows_meet_the_population_or_someone_in_the_cause(client_modules):
    """The Filter's plain rows (a property is any of values): the population names one, or someone in the cause has one."""
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, None, filters={'category_ids': ['A']}) is True   # who names A
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, None, filters={'category_ids': ['B']}) is True   # p1 has B
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, None, filters={'category_ids': ['C']}) is False
    assert matches(client_modules, {'p1': {'category_ids': ['B']}}, None, filters={'category_ids': []}) is True      # no rule
    assert matches(client_modules, {'p1': {'category_ids': ['A']}}, tree(op='none', values=['A']), filters={'category_ids': ['A']}) is False
