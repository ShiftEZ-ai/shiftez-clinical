"""Auto-schedule review: whether a proposal's problem is already in the draft, from the real compiled findings.ts in Node.

The draft's own check may only be compared when it and the proposal were both made from the rota's current inputs; a
problem the proposal makes worse is never called unchanged."""
import json
import subprocess

CURRENT = 'fp-now'
ERROR = {'code': 'MAX_DUTIES', 'severity': 'error', 'rule_id': 'r1', 'shift_id': None, 'person_id': 'p1',
         'start': '2026-09-01T00:00:00+03:00', 'detail': {'actual': 6, 'maximum': 5}}
CHECK = {'fingerprint': CURRENT, 'findings': [ERROR],
         'coverage': [{'shift_id': 's1', 'status': 'gap', 'required': 2, 'available_eligible': 1}]}


def run(client_modules, body, data):
    script = (f"const m=await import({json.dumps((client_modules/'findings.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              + body)
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def compare(client_modules, check, current, proposal_input, findings, places):
    return run(client_modules, "const c=m.draftComparison(i.check,i.current,i.input);"
               "console.log(JSON.stringify(c&&{f:i.findings.map(c.finding),p:i.places.map(([s,n])=>c.place(s,n))}));",
               {'check': check, 'current': current, 'input': proposal_input, 'findings': findings, 'places': places})


def test_same_issue_same_numbers_is_already_in_the_draft_and_a_worse_one_is_changed(client_modules):
    worse = {**ERROR, 'detail': {'actual': 7, 'maximum': 5}}
    other = {**ERROR, 'person_id': 'p2'}
    got = compare(client_modules, CHECK, CURRENT, CURRENT, [ERROR, worse, other], [['s1', 1], ['s1', 2], ['s2', 1]])
    assert got == {'f': ['draft', 'changed', 'new'], 'p': ['draft', 'changed', 'new']}


def test_a_stale_draft_check_or_an_old_proposal_says_nothing(client_modules):
    # The draft check was made before the rota changed: it cannot tell what the proposal brought.
    assert compare(client_modules, {**CHECK, 'fingerprint': 'fp-old'}, CURRENT, CURRENT, [ERROR], []) is None
    # The proposal was made from other inputs than the rota now.
    assert compare(client_modules, CHECK, CURRENT, 'fp-old', [ERROR], []) is None
    # No check, or no current fingerprint.
    assert compare(client_modules, None, CURRENT, CURRENT, [ERROR], []) is None
    assert compare(client_modules, CHECK, None, None, [ERROR], []) is None


def test_a_group_is_already_in_the_draft_only_when_every_problem_is(client_modules):
    got = run(client_modules, "console.log(JSON.stringify(i.map(m.sinceOfMany)));",
              [['draft', 'draft'], ['new', 'new'], ['draft', 'new', 'changed'], ['changed'], []])
    assert got == [{'since': 'draft'}, {'since': 'new'}, {'since': 'changed', 'sinceCount': 2}, {'since': 'changed'}, {}]


def test_a_period_count_says_the_servers_numbers_and_an_unfilled_cause_claims_no_fill(client_modules):
    got = run(client_modules, "console.log(JSON.stringify({n:m.findingNumbers(i.f),why:m.causeWhy(i.c,[])}));",
              {'f': {'code': 'QUOTA_BELOW_MINIMUM', 'detail': {'actual': 3, 'amount': 4, 'display': '3 of 4'}},
               'c': {'code': 'unfilled', 'required': 2, 'possible': 5, 'actual': 1}})
    assert got['n'] == '3 of 4'
    assert got['why'] == 'Needs 2; 5 possible, 1 assigned' and 'Auto-schedule' not in got['why']
