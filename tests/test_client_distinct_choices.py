"""Same-named choices (two people with one name) told apart in the one shared field and picker, from the real compiled
dom.ts in Node (logic only): by their hint (a short code), else their group (a rank), else their place in a stable order
by id; unique labels stay as they are. Sample labels are synthetic, not human names."""
import json

from .test_client_staff_states import node

SAME = 'Member 001 (test)'


def told(client_modules, choices):
    return node(client_modules, 'dom.js', f"m.distinct({json.dumps(choices)}).map(c=>[c.value,c.label,c.hint??null])")


def test_same_named_people_are_told_apart_by_code_then_rank_then_order(client_modules):
    # By short code (the hint moves into the name, so it is not said twice); the repeats follow their ids.
    got = told(client_modules, [{'value': 'p2', 'label': SAME, 'hint': 'BB', 'group': 'Rank A'},
                                {'value': 'x', 'label': 'Member 002 (test)', 'hint': 'CC'},
                                {'value': 'p1', 'label': SAME, 'hint': 'AA', 'group': 'Rank A'}])
    assert got == [['p1', f'{SAME} · AA', None], ['x', 'Member 002 (test)', 'CC'], ['p2', f'{SAME} · BB', None]]
    # No code, or the same one: by rank.
    got = told(client_modules, [{'value': 'p2', 'label': SAME, 'group': 'Rank B'}, {'value': 'p1', 'label': SAME, 'group': 'Rank A'}])
    assert [g[1] for g in got] == [f'{SAME} · Rank A', f'{SAME} · Rank B'] and [g[0] for g in got] == ['p1', 'p2']
    # Nothing tells them apart: their place in the order by id, the same on every open.
    got = told(client_modules, [{'value': 'p2', 'label': SAME, 'group': 'Rank A'}, {'value': 'p1', 'label': SAME, 'group': 'Rank A'}])
    assert got == [['p1', f'{SAME} · 1', None], ['p2', f'{SAME} · 2', None]]


def test_unique_labels_are_unchanged_and_telling_apart_twice_changes_nothing(client_modules):
    unique = [{'value': 'a', 'label': 'Clinic A'}, {'value': 'b', 'label': 'Clinic B'}, {'value': '', 'label': 'Clinic A'}]
    assert node(client_modules, 'dom.js', f"(()=>{{const c={json.dumps(unique)};return m.distinct(c)===c;}})()") is True
    twice = [{'value': 'p2', 'label': SAME}, {'value': 'p1', 'label': SAME}]
    assert node(client_modules, 'dom.js', f"(()=>{{const once=m.distinct({json.dumps(twice)});return m.distinct(once)===once;}})()") is True
