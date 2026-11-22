"""The pinned Rules panel's rule-map answers, run as the real compiled client in Node (logic only, no DOM)."""
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def version(revision):
    return {'workspace': {'id': 'w1', 'revision': revision, 'validation': {'evaluated_at': f't{revision}'}}, 'current_fingerprint': f'f{revision}'}


def test_an_older_rule_map_answer_never_draws_over_the_newer_rota_version(client_modules):
    # A live refresh asks again for version 2 while version 1's request is still out; 2 answers first, then 1 fails.
    script = f"""const m=await import({json.dumps((client_modules/'planner_rules.js').as_uri())});
const [v1,v2]=JSON.parse(process.argv[1]),pin={{}},key=m.keyOf;
console.log(JSON.stringify({{
  older:m.answerApplies(pin,pin,v2,key(v1)), newer:m.answerApplies(pin,pin,v2,key(v2)),
  otherPin:m.answerApplies({{}},pin,v2,key(v2)), closed:m.answerApplies(null,pin,v2,key(v2)), noRota:m.answerApplies(pin,pin,null,key(v2))}}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([version(1), version(2)])],
                            capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {'older': False, 'newer': True, 'otherPin': False, 'closed': False, 'noRota': False}


def test_a_number_shows_what_a_revised_mandatory_rule_now_binds(client_modules):
    # The rule map's binding_value: null, or the number already shown, keeps one number; a stricter live value shows both.
    script = f"""const m=await import({json.dumps((client_modules/'planner_rules.js').as_uri())});
const n={{field:'maximum',value:5,unit:'duties',stricter:'lower'}};
console.log(JSON.stringify({{same:m.numBoth(n,null), stricter:m.numBoth(n,2), noNumber:m.numBoth(null,2),
  sameNote:m.bindsNote(n,null), stricterNote:m.bindsNote(n,2), zero:m.numBoth(n,0),
  alreadyShown:m.numBoth(n,5), alreadyShownNote:m.bindsNote(n,5)}}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {
        'same': '5 duties', 'stricter': 'captured 5, now binds 2 duties', 'noNumber': '',
        'sameNote': '', 'stricterNote': 'Captured 5, now binds 2 duties.', 'zero': 'captured 5, now binds 0 duties',
        'alreadyShown': '5 duties', 'alreadyShownNote': ''}


def test_a_rule_with_overlapping_lines_is_its_own_row_said_once_with_each_number(client_modules):
    # The server sends one conflict per overlapping pair of lines: three lines of one rest rule are three pairs. The panel
    # keeps them as that rule's row (each line's sentence and number once, and why the strictest binds), never as notices;
    # two different rules that can't both hold stay conflicts of their own.
    pair = lambda n, a, b, rules=('rest', 'rest'): {'conflict_id': f'c{n}', 'sentence': 'Rest between duties has overlapping lines that apply together.',
                                                    'line_ids': [a, b], 'rule_ids': list(rules), 'line_refs': [int(a[1:]), int(b[1:])], 'reach': {'sessions_count': 1}}
    conflicts = [pair(1, 'l1', 'l2'), pair(2, 'l1', 'l3'), pair(3, 'l2', 'l3'), pair(4, 'l7', 'l9', ('duties', 'clinic'))]
    sentence = lambda text, line: {'text': text, 'source_line_ids': [line]}
    told = {'rule_id': 'rest', 'sentences': [sentence('Everyone: at least 11 hours of rest between duties', 'l1'),
                                             sentence('Practitioners: at least 8 hours of rest between duties.', 'l2'),
                                             sentence('One person: at least 12 hours of rest between duties', 'l3'),
                                             sentence('Someone else: at least 10 hours of rest between duties', 'l4')]}
    script = f"""const m=await import({json.dumps((client_modules/'planner_rules.js').as_uri())});
const [cs,told]=JSON.parse(process.argv[1]),laps=m.ownOverlaps(cs);
console.log(JSON.stringify({{rules:[...laps.keys()],lines:[...laps.get('rest').entries()],words:m.overlapWords(told,laps.get('rest')),
  why:m.OVERLAP_WHY,other:m.overlapWords(told,laps.get('duties')),silent:m.overlapWords({{...told,sentences:[]}},laps.get('rest'))}}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([conflicts, told])],
                            capture_output=True, text=True, check=True, timeout=15)
    out = json.loads(result.stdout)
    assert out['rules'] == ['rest']  # one rule, however many pairs; the two-rule conflict is not an overlap
    assert out['lines'] == [['l1', 1], ['l2', 2], ['l3', 3]]
    words = out['words']
    assert words['text'] == ('Everyone: at least 11 hours of rest between duties. Practitioners: at least 8 hours of rest '
                             'between duties. One person: at least 12 hours of rest between duties.')
    assert words['why'] == out['why'] == 'Where these lines overlap, all of them apply, so the strictest binds.'
    assert words['lines'] == 'Lines #1, #2 and #3 overlap.'
    assert '#' not in words['text'] + words['why']  # a folded row never numbers lines
    assert out['other'] is None and out['silent'] is None
