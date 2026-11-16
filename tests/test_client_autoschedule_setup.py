"""Auto-schedule panel: the run setup kept on this device is read back field by field from the compiled module in Node;
a bad field is dropped, never guessed, and nothing but the setup choices is kept."""
import json
import subprocess


def parse(client_modules, value):
    script = (f"const m=await import({json.dumps((client_modules/'autoschedule_setup.js').as_uri())});"
              "console.log(JSON.stringify(m.parseSetup(JSON.parse(process.argv[1]))));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(value)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_a_kept_setup_reads_back_and_bad_fields_are_dropped(client_modules):
    good = {'scope': 'open', 'keep': False, 'minutes': 12, 'fairness': 30, 'from': '2026-09-01', 'to': '2026-09-30',
            'clinics': ['c1'], 'preset': 'fair'}
    assert parse(client_modules, good) == good
    bad = {'scope': 'all', 'keep': 'yes', 'minutes': 1.5, 'fairness': '15', 'from': '2026-09-30', 'to': '2026-09-01',
           'clinics': [1], 'preset': 3, 'live': 'job-1', 'starting': True, 'resume': {'job': 'j'}}
    assert parse(client_modules, bad) == {}
    for value in (None, [], 'x', 5):
        assert parse(client_modules, value) == {}


def test_add_time_suggests_the_departments_step_within_the_room_left(client_modules):
    script = (f"const m=await import({json.dumps((client_modules/'autoschedule_setup.js').as_uri())});"
              "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(([c,r])=>m.addTimeStep(c,r))));")
    cases = [[{'extension_minutes': 12}, 60], [{'extension_minutes': 12}, 7], [None, 60], [{'extension_minutes': 0}, 60],
             [{'extension_minutes': 'x'}, 60], [{}, 3]]
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(cases)],
                         capture_output=True, text=True, check=True, timeout=15)
    from packages.contracts.schemas import GENERATION_CONTROL_DEFAULTS
    default = GENERATION_CONTROL_DEFAULTS['extension_minutes']
    assert json.loads(out.stdout) == [12, 7, default, default, default, min(default, 3)]


def run_module(client_modules, call, value):
    script = (f"const m=await import({json.dumps((client_modules/'autoschedule_setup.js').as_uri())});"
              f"const v=JSON.parse(process.argv[1]);console.log(JSON.stringify({call}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(value)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_a_kept_people_choice_reads_back_whole_or_not_at_all(client_modules):
    rank = {'field': 'category_ids', 'op': 'any', 'values': ['r1', 'r2']}
    group = {'match': 'any', 'conditions': [{'field': 'tags', 'op': 'all', 'values': ['a']}, {'field': 'person_ids', 'values': ['p1']}]}
    kept = {'match': 'all', 'conditions': [rank, group]}
    expected = {'match': 'all', 'conditions': [rank, {'match': 'any', 'conditions': [
        {'field': 'tags', 'op': 'all', 'values': ['a']}, {'field': 'person_ids', 'op': 'any', 'values': ['p1']}]}]}
    assert run_module(client_modules, 'm.parseSetup(v)', {'group': kept, 'pickGroup': True}) == {'group': expected, 'pickGroup': True}
    bad = [None, {'conditions': []}, {'match': 'some', 'conditions': [rank]},
           {'conditions': [{**rank, 'op': 'gt'}]}, {'conditions': [{**rank, 'values': []}]},
           {'conditions': [{**rank, 'values': [1]}]}, {'conditions': [rank, 'x']},
           {'conditions': [{'conditions': [{'conditions': [rank]}]}]}]  # two levels of groups: the builder never makes them
    for value in bad:
        assert run_module(client_modules, 'm.parseSetup(v)', {'group': value, 'pickGroup': 'yes'}) == {}
    # Only the fields the server accepts: one foreign line drops the whole choice (never a wider group).
    assert run_module(client_modules, "m.parseGroup(v,['category_ids'])", kept) is None
    assert run_module(client_modules, "m.parseGroup(v,['category_ids','tags','person_ids'])", kept) == expected


def test_group_runs_are_offered_only_as_the_server_advertises_them(client_modules):
    cases = [None, {}, {'generation_scope': None}, {'generation_scope': {'fields': [], 'protections': ['keep_existing']}},
             {'generation_scope': {'fields': ['category_ids'], 'protections': ['rebalance_selected']}},
             {'generation_scope': {'fields': ['category_ids', 1], 'protections': ['keep_existing']}},
             {'generation_scope': {'fields': ['category_ids'], 'protections': ['keep_existing', 'other']}},
             {'generation_scope': {'fields': ['tags'], 'protections': ['keep_existing', 'rebalance_selected'],
                                   'default_protection': 'keep_existing'}},
             {'generation_scope': {'fields': ['tags'], 'protections': ['keep_existing'], 'default_protection': 'rebalance_selected'}}]
    assert run_module(client_modules, 'v.map(c=>m.scopeCapability(c))', cases) == [
        None, None, None, None, None, None,
        {'fields': ['category_ids'], 'protections': ['keep_existing'], 'default_protection': 'keep_existing'},
        {'fields': ['tags'], 'protections': ['keep_existing', 'rebalance_selected'], 'default_protection': 'keep_existing'},
        # A default the server does not offer never applies: existing assignments are kept.
        {'fields': ['tags'], 'protections': ['keep_existing'], 'default_protection': 'keep_existing'}]
