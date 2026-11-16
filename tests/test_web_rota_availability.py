"""Who's available over a rota's dates (apps/web/src/rota_availability.ts `summarise`), run in Node from the compiled
client: the server's segments become stretches, including its outside-department reasons."""
import json
import shutil
import subprocess

import pytest

DAYS = ['2027-02-0' + str(d) for d in range(1, 8)]  # Mon 1 – Sun 7 Feb 2027


@pytest.fixture(scope='module')
def summarise(client_modules):
    node = shutil.which('node')

    def call(person):
        script = ("globalThis.document={cookie:'',addEventListener(){}};"  # timeline.js (the shared date axis) registers document listeners on load
                  f"const m=await import({json.dumps((client_modules / 'rota_availability.js').as_uri())});"
                  "const [p,days]=JSON.parse(process.argv[1]);console.log(JSON.stringify(m.summarise(p,days)));")
        out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps([person, DAYS])],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return call


def person(segments=()):
    return {'id': 'p1', 'display_name': 'Dr Corin Vale', 'group': 'R2', 'segments': list(segments),
            'items': []}


def seg(kind, a, b, label, **extra):
    return {'kind': kind, 'starts_on': a, 'ends_on': b, 'label': label, **extra}


def test_available_every_day(summarise):
    out = summarise(person())
    assert out['state'] == 'all' and out['words'] == 'Every day' and out['stretches'] == []


def test_leave_and_rotation_elsewhere_are_stretches_with_reasons(summarise):
    out = summarise(person([seg('leave', '2027-02-02', '2027-02-04', 'Leave', leave_type='Annual'),
                            seg('away', '2027-02-06', '2027-02-10', 'Cardiology')]))
    assert out['state'] == 'part' and out['away'] == 4 and out['words'] == 'Away 4 d'
    assert [(s['kind'], s['from'], s['to'], s['label']) for s in out['stretches']] == [
        ('leave', 1, 3, 'Annual'), ('away', 5, 7, 'Rotation elsewhere · Cardiology')]


def test_not_started_and_left_come_from_server_outside_segments(summarise):
    starts = summarise(person([seg('outside', '2027-02-01', '2027-02-04', 'Not started yet', reason='not_started')]))
    assert starts['words'].startswith('Starts ') and starts['stretches'][0]['label'] == 'Not started yet'
    left = summarise(person([seg('outside', '2027-02-06', '2027-02-08', 'Left the department', reason='left')]))
    assert left['words'].startswith('Last day ') and left['stretches'][-1]['label'] == 'Left the department'
    # Not being in the department wins over the server's own reasons on those days.
    both = summarise(person([seg('outside', '2027-02-01', '2027-02-03', 'Not started yet', reason='not_started'),
                             seg('leave', '2027-02-03', '2027-02-08', 'Leave')]))
    assert [s['kind'] for s in both['stretches']] == ['out', 'leave'] and both['state'] == 'none' and both['words'] == 'Away'


def test_outside_for_the_whole_rota_says_why_not_away(summarise):
    out = summarise(person([seg('outside', '2027-02-01', '2027-02-08', 'Not started yet', reason='not_started')]))
    assert out['state'] == 'none' and out['words'] == 'Not started yet'


def test_rank_is_the_one_held_on_the_rotas_first_day(client_modules):
    rank = lambda items: run_module(client_modules, 'return m.rankOf(input, "2027-02-01");', items)
    acon = {'kind': 'rank', 'starts_on': '2026-01-01', 'ends_on': '2027-02-10', 'label': 'Associate Consultant', 'status': 'active', 'details': {'category_id': 'acon'}}
    cons = {'kind': 'rank', 'starts_on': '2027-02-10', 'ends_on': None, 'label': 'Consultant', 'status': 'active', 'details': {'category_id': 'cons'}}
    assert rank([cons, acon]) == {'id': 'acon', 'name': 'Associate Consultant'}
    assert rank([cons]) == {'id': 'cons', 'name': 'Consultant'}  # joined during the rota: their first rank
    assert rank([{**acon, 'status': 'revoked'}]) is None


def test_people_group_by_rank_id_once_each_and_alike_names_carry_their_codes(client_modules):
    """PC-QA cycle 3 maintenance: two groups both headed ASSOCIATE CONSULTANT. Groups are keyed by rank id, each rank once
    even when its people are not next to each other; two ranks named alike are told apart by their codes."""
    def who(pid, rank_id, name):
        return {'id': pid, 'rank': {'id': rank_id, 'name': name} if rank_id else None}
    people = [who('a', 'acon', 'Associate Consultant'), who('b', 'asc', 'Associate consultant '), who('c', 'res', 'Resident'),
              who('d', 'acon', 'Associate Consultant'), who('e', None, None)]
    out = run_module(client_modules, 'return m.groupByRank(input, id => ({acon: "ACON", asc: "ASC"})[id] || "").map(([l, r]) => [l, r.map(x => x.id)]);', people)
    assert out == [['Associate Consultant · ACON', ['a', 'd']], ['Associate consultant · ASC', ['b']], ['Resident', ['c']], ['No rank', ['e']]]
    # Names that differ are never given codes.
    assert run_module(client_modules, 'return m.groupByRank(input, () => "X").map(([l]) => l);', people[2:3]) == ['Resident']


def run_module(folder, body, payload):
    node = shutil.which('node')
    script = ("globalThis.document={cookie:'',addEventListener(){}};"  # timeline.js (the shared date axis) registers document listeners on load
                  f"const m=await import({json.dumps((folder / 'rota_availability.js').as_uri())});"
              f"const input=JSON.parse(process.argv[1]);const out=(()=>{{{body}}})();console.log(JSON.stringify(out ?? null));")
    out = subprocess.run([node, '--input-type=module', '-e', script, json.dumps(payload)], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_views_show_who_can_work_in_the_rota_then_how_much_they_are_away(client_modules):
    """The default view is the people who can work in the rota (the server's rota_eligible); Available every day, Partly
    away and Away every day are within it; Everyone in the department is everyone. Without eligibility (a rota with no
    sessions) the views are over everyone and there is no Can work in this rota."""
    people = [{'id': 'a', 'eligible': True, 'state': 'all'}, {'id': 'b', 'eligible': True, 'state': 'part'},
              {'id': 'c', 'eligible': False, 'state': 'all'}, {'id': 'd', 'eligible': None, 'state': 'none'}]
    ids = lambda view, on: run_module(client_modules, f'return m.viewPeople(input, "{view}", {"true" if on else "false"}).map(x => x.id);', people)
    assert run_module(client_modules, 'return [m.viewsOf(true), m.viewsOf(false)];', None) == [
        ['eligible', 'free', 'part', 'none', 'everyone'], ['everyone', 'free', 'part', 'none']]
    assert ids('eligible', True) == ['a', 'b']
    assert ids('free', True) == ['a'] and ids('part', True) == ['b'] and ids('none', True) == []
    assert ids('everyone', True) == ['a', 'b', 'c', 'd']
    assert ids('free', False) == ['a', 'c'] and ids('none', False) == ['d']


def test_the_week_axis_is_the_timelines(client_modules):
    """Who's available draws its date axis from timeline.ts units (one helper): weeks from the department's week start,
    the rota's first and last cut at its ends (live check 6cea25859: weeks ran from the rota's own first day)."""
    node = shutil.which('node')
    script = ("globalThis.document={cookie:'',addEventListener(){}};"
              f"const m=await import({json.dumps((client_modules / 'timeline.js').as_uri())});"
              f"const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
              "const sunday=m.units('2027-02-01','2027-03-03','week');d.setWeekStart(0);"
              "console.log(JSON.stringify([sunday,m.units('2027-02-01','2027-03-03','week')]))")
    sunday, monday = json.loads(subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=30).stdout)
    assert sunday == [['2027-02-01', '2027-02-07'], ['2027-02-07', '2027-02-14'], ['2027-02-14', '2027-02-21'],
                      ['2027-02-21', '2027-02-28'], ['2027-02-28', '2027-03-03']]
    assert monday == [['2027-02-01', '2027-02-08'], ['2027-02-08', '2027-02-15'], ['2027-02-15', '2027-02-22'],
                      ['2027-02-22', '2027-03-01'], ['2027-03-01', '2027-03-03']]
