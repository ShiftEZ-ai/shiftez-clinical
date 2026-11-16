"""S7 / F9: Home's setup card and the Configuration badge read setup progress through one helper (setup_guide.ts
setupCounts): lists held since boot count themselves, the deferred ones come from a single counts_only read shared
while in flight, and Opening hours still sees boot's services without a calendar. From the real compiled module."""
import json
import subprocess

BOOT = {'_versions': {'locations': 'a', 'services': 'b', 'assignment_types': 'c', 'categories': 'd', 'people': 'e'},
        'locations': [{'id': 'l1'}], 'assignment_types': [{'id': 't1'}], 'categories': [{'id': 'c1'}], 'people': [{'id': 'p1'}],
        'services': [{'id': 's1', 'calendar_id': 'cal1'}, {'id': 's2', 'calendar_id': None}]}


def run(client_modules, catalogues, body):
    script = ("const i=JSON.parse(process.argv[1]),asked=[];"
              "globalThis.fetch=async(url)=>{asked.push(String(url));return new Response(JSON.stringify({counts:{operating_calendars:1,policies:3}}),"
              "{status:200,headers:{'content-type':'application/json'}});};"
              f"const m=await import({json.dumps((client_modules/'setup_guide.js').as_uri())});"
              "const ctx={me:{organisation:{timezone:'UTC'}},catalogues:i};"
              f"console.log(JSON.stringify(await ({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(catalogues)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_one_counts_read_for_the_deferred_steps_shared_while_in_flight(client_modules):
    got = run(client_modules, BOOT, "(async()=>{const [a,b]=await Promise.all([m.setupCounts(ctx),m.setupCounts(ctx)]);"
              "const steps=m.setupSteps(ctx,{catalogue:()=>{},page:()=>{}},null,a).filter(s=>!s.optional);"
              "return {a,same:a===b,asked,steps:steps.map(s=>[s.id,s.count,m.stepDone(s)]),words:m.missingSetupWords(ctx)};})()")
    assert got['a'] == {'operating_calendars': 1, 'policies': 3} and got['same'] is True
    # Only the lists boot does not hold are counted, in one read; no list (no `known=`) is fetched.
    assert len(got['asked']) == 1 and 'counts_only=true' in got['asked'][0] and 'known=' not in got['asked'][0]
    assert 'include=operating_calendars' in got['asked'][0] and 'include=policies' in got['asked'][0]
    assert 'include=services' not in got['asked'][0]
    steps = {s[0]: s[1:] for s in got['steps']}
    # One calendar exists but s2 has none: Opening hours is not done (boot's services answer it exactly).
    assert steps['hours'] == [1, False]
    assert steps['rules'] == [3, True] and steps['types'] == [1, True]
    assert got['words'] == ''


def test_missing_duty_types_are_said_from_the_boot_lists(client_modules):
    got = run(client_modules, {**BOOT, 'assignment_types': []}, "Promise.resolve({words:m.missingSetupWords(ctx),asked})")
    assert got == {'words': 'No duty types yet', 'asked': []}
