"""Print or export › CSV of a People view, from the real compiled client in Node (logic only): the People search finds
people by name, code or tag (planner_people.findsPerson, the People view's own matcher), and the List drawn for the CSV
keeps the sessions of the people found as one Person line under the Schedule's filters, read by the shared session
evaluator (table_view.holdsForSomeone). The browser journey (People search → CSV rows) is still owed."""
import json
import subprocess


def run(client_modules, body, data):
    script = (f"const pp=await import({json.dumps((client_modules/'planner_people.js').as_uri())});"
              f"const tv=await import({json.dumps((client_modules/'table_view.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


PEOPLE = [{'id': 'p1', 'name': 'Nico Quinn', 'code': 'DEMO1', 'tags': ['Night']}, {'id': 'p2', 'name': 'Marta Harris', 'code': 'DEMO2', 'tags': []},
          {'id': 'p3', 'name': 'Pavel Jensen', 'code': 'DEMO3', 'tags': ['night shift']}]
RANKS = {'p1': ['r1'], 'p2': ['r2'], 'p3': ['r2']}
SESSIONS = [{'id': 's1', 'people': ['p1', 'p2']}, {'id': 's2', 'people': ['p2']}, {'id': 's3', 'people': ['p3']}, {'id': 's4', 'people': []}]
# The CSV's List: the Schedule's own filter (canonical tree) and, under it, the found people as one "is any of" line.
KEEP = """(()=>{const found=i.people.filter(p=>pp.findsPerson(p,i.q,x=>x.code)).map(p=>p.id);
  const line={field:'person_ids',op:'any',values:found},tree=i.own?{match:'all',conditions:[i.own,line]}:{match:'all',conditions:[line]};
  const facts=id=>f=>f==='person_ids'?[id]:f==='category_ids'?i.ranks[id]:[];
  return {found,kept:i.sessions.filter(s=>tv.holdsForSomeone(tree,f=>f==='person_ids'?s.people:s.people.flatMap(id=>facts(id)(f)),s.people.map(facts))).map(s=>s.id)};})()"""


def test_people_search_finds_by_name_code_or_tag_and_keeps_their_sessions(client_modules):
    data = {'people': PEOPLE, 'ranks': RANKS, 'sessions': SESSIONS, 'own': None}
    assert run(client_modules, KEEP, {**data, 'q': 'night'}) == {'found': ['p1', 'p3'], 'kept': ['s1', 's3']}  # tags
    assert run(client_modules, KEEP, {**data, 'q': 'demo2'}) == {'found': ['p2'], 'kept': ['s1', 's2']}           # code
    assert run(client_modules, KEEP, {**data, 'q': 'pavel'}) == {'found': ['p3'], 'kept': ['s3']}                # name


def test_found_people_meet_the_schedule_filter_as_one_person(client_modules):
    # Rank r2 and found by "night": the same participant must hold both (Pavel), never Nico's night tag with Marta's rank.
    own = {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': ['r2']}]}
    got = run(client_modules, KEEP, {'people': PEOPLE, 'ranks': RANKS, 'sessions': SESSIONS, 'own': own, 'q': 'night'})
    assert got == {'found': ['p1', 'p3'], 'kept': ['s3']}
