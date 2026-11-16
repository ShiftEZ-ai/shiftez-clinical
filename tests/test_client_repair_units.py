"""Cause cards from the real compiled findings.ts in Node: counted in the sessions and people they name, and two cause
groups over the same repair tasks shown as one card with both rules. (What is left on a rota is the server's Fix list.)"""
import json
import subprocess


def run(client_modules, body, data):
    script = (f"const m=await import({json.dumps((client_modules/'findings.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              + body)
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


GROUP = {'code': 'unfilled', 'severity': 'error', 'person_ids': [], 'fix_hints': []}


def test_two_rules_on_the_same_open_sessions_are_one_group_and_other_groups_stay(client_modules):
    groups = [{**GROUP, 'key': 'base', 'count': 2, 'finding_keys': ['b1', 'b2'], 'rule_ids': []},
              {**GROUP, 'key': 'kind', 'count': 2, 'finding_keys': ['k1', 'k2'], 'rule_ids': ['r-kind']},
              {**GROUP, 'key': 'quota', 'count': 1, 'finding_keys': ['q1'], 'rule_ids': ['r-week'], 'code': 'partial_window'}]
    tasks = [{'key': 't1', 'shift_ids': ['s1'], 'finding_keys': ['b1', 'k1'], 'observation_ids': []},
             {'key': 't2', 'shift_ids': ['s2'], 'finding_keys': ['b2', 'k2'], 'observation_ids': []},
             {'key': 't3', 'shift_ids': [], 'finding_keys': ['q1'], 'observation_ids': []}]
    got = run(client_modules, "console.log(JSON.stringify(m.mergeCauseGroups(i.g,i.t)));", {'g': groups, 't': tasks})
    assert [g['key'] for g in got] == ['base', 'quota']
    assert got[0]['finding_keys'] == ['b1', 'b2', 'k1', 'k2'] and got[0]['rule_ids'] == ['r-kind']
    # Same cause, other tasks (another session set): not merged. Without tasks: as given.
    other = [{**GROUP, 'key': 'a', 'count': 1, 'finding_keys': ['b1'], 'rule_ids': []},
             {**GROUP, 'key': 'b', 'count': 1, 'finding_keys': ['k2'], 'rule_ids': ['r-kind']}]
    assert [g['key'] for g in run(client_modules, "console.log(JSON.stringify(m.mergeCauseGroups(i.g,i.t)));",
                                  {'g': other, 't': tasks})] == ['a', 'b']
    assert run(client_modules, "console.log(JSON.stringify(m.mergeCauseGroups(i.g,[]).length));", {'g': groups, 't': []}) == 3


def test_a_cause_card_counts_sessions_and_people_never_breaches(client_modules):
    got = run(client_modules, "console.log(JSON.stringify(i.map(m.affected)));", [
        [{'shift_id': 's1'}, {'shift_id': 's1'}, {'shift_id': 's2'}],
        [{'person_id': 'p1', 'start': 'w1'}, {'person_id': 'p1', 'start': 'w2'}, {'person_id': 'p2', 'start': 'w1'}],
        [{'shift_id': 's1', 'person_id': 'p1'}],
        [{'code': 'X'}, {'code': 'Y'}]])
    assert got == ['2 sessions', '2 people', '1 session · 1 person', '2 problems']


def test_overlapping_causes_keep_all_references_and_count_unique_findings(client_modules):
    groups = [{**GROUP, 'key': 'first', 'count': 2, 'finding_keys': ['a', 'b'], 'rule_ids': ['r1']},
              {**GROUP, 'key': 'second', 'count': 2, 'finding_keys': ['a', 'c'], 'rule_ids': ['r2']},
              {**GROUP, 'key': 'no-pool', 'count': 1, 'finding_keys': ['a'], 'rule_ids': ['r1'],
               'code': 'no_eligible_people'}]
    tasks = [{'key': 't1', 'shift_ids': ['s1'], 'finding_keys': ['a', 'b', 'c'], 'observation_ids': []}]
    got = run(client_modules, 'console.log(JSON.stringify(m.mergeCauseGroups(i.g,i.t)));', {'g': groups, 't': tasks})
    assert [g['key'] for g in got] == ['first', 'no-pool']
    assert got[0]['count'] == 3 and set(got[0]['finding_keys']) == {'a', 'b', 'c'}
    assert got[1]['finding_keys'] == ['a']  # A second genuine cause for a shared parent remains actionable.
