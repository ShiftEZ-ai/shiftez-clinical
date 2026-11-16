"""B7 failed check and long work: the elapsed clock (loading.ts) and the words for what stopped a check (job_state_api.ts,
maintenance's cause or changed facts), from the real compiled modules in Node."""
import json
import subprocess


def run(client_modules, body, *args):
    script = (f"const l=await import({json.dumps((client_modules/'loading.js').as_uri())});"
              f"const j=await import({json.dumps((client_modules/'job_state_api.js').as_uri())});" + body)
    out = subprocess.run(['node', '--input-type=module', '-e', script, *args], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_clock_is_minutes_and_seconds_never_an_estimate(client_modules):
    assert run(client_modules, "console.log(JSON.stringify([0,999,72000,153000,725000,-5].map(l.clock)))") == \
        ['0:00', '0:00', '1:12', '2:33', '12:05', '0:00']


def test_a_stopped_check_names_its_cause(client_modules):
    cases = [{'cause': {'kind': 'wish', 'person': 'Dr. Felix Parker', 'date': '2026-12-17'}},
             {'changed_facts': [{'kind': 'availability', 'actor': {'display_name': 'Rota Coordinator'}, 'first_day': '2026-12-03'}]},
             {'changed_facts': [{'kind': 'wish', 'actor': None, 'person_name': 'Sam Lee', 'start': '2026-12-04T05:00:00Z'}]},
             {'cause': {'kind': 'edit'}},
             {'code': 'SOMETHING_ELSE'},
             None]
    got = run(client_modules, "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(d=>j.stoppedWords(153000,d))))", json.dumps(cases))
    assert got == [
        {'lead': 'The check stopped after 2:33.', 'cause': 'Dr. Felix Parker added a wish for Thu 17 Dec while it ran.'},
        {'lead': 'The check stopped after 2:33.', 'cause': 'Rota Coordinator recorded leave for Thu 3 Dec while it ran.'},
        {'lead': 'The check stopped after 2:33.', 'cause': 'A wish for Sam Lee on Fri 4 Dec was added while it ran.'},
        {'lead': 'The check stopped after 2:33.', 'cause': 'A change was made while it ran.'},
        {'lead': 'The check stopped after 2:33.', 'cause': ''},
        {'lead': 'The check stopped after 2:33.', 'cause': ''},
    ]
