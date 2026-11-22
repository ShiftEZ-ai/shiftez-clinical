"""A waiting change's "gives way" lines (rule_catalogue.ts `checkWords().gives`, issue #1047): one short line each, who
and number only, from the real compiled client in Node; drawn as plain information, never in the warning list."""
import json
import re
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'
ENTRY = {'id': 'duties_per_week', 'name': 'Duties per week', 'numbers': {'max': []}, 'levels': ['mandatory']}


def gives(client_modules, lines, overlap):
    script = (f"const m=await import({json.dumps((client_modules/'rule_catalogue.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const names={label:(f,v)=>v,tag:()=>undefined};"
              "console.log(JSON.stringify(m.checkWords(i.entry,()=>i.lines,names).gives(i.overlap)));")
    body = {'entry': ENTRY, 'lines': lines, 'overlap': overlap}
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(body)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def line(lid, most, who=None, **more):
    return {'id': lid, 'who': {'match': 'all', 'conditions': [{'field': 'category_ids', 'op': 'any', 'values': [who]}]} if who else None,
            'where': None, 'numbers': {'max': {'value': most, 'level': 'mandatory'}}, **more}


def test_a_line_that_gives_way_reads_who_and_number_only(client_modules):
    lines = [line('a', 5), line('b', 6, 'Senior grade', weekdays=[0, 1, 2], ends_on='2027-03-31')]
    said = gives(client_modules, lines, {'line_id': 'a', 'winner_ids': ['b'], 'numbers': ['max'], 'people': 11})
    assert said == 'Everyone ≤ 5 gives way to Senior grade ≤ 6 for 11 people'


def test_two_lines_that_read_alike_are_said_in_full_to_tell_them_apart(client_modules):
    lines = [line('a', 5), line('b', 6, 'Senior grade', weekdays=[0, 1, 2]), line('c', 6, 'Senior grade', weekdays=[3, 4])]
    said = gives(client_modules, lines, {'line_id': 'a', 'winner_ids': ['b', 'c'], 'people': 1})
    assert said.startswith('Everyone ≤ 5 gives way to “Senior grade') and said.endswith('for 1 person')
    assert said.count('“') == 2


def test_the_waiting_change_draws_gives_way_lines_apart_from_its_warnings():
    text = (SRC / 'rule_catalogue.ts').read_text(encoding='utf-8')
    body = text[text.index('function proposalNote('):text.index('function proposalPanel(')]
    said = re.search(r"const said=c\?\[(.*?)\]:\[\];", body).group(1)
    assert 'overlaps' not in said  # the warnings and their count never include a line that gives way
    drawn = re.search(r"el\('ul',\{class:'rc-prop-gives'\}(.*?)\)\]:\[\]\)", body).group(1)
    assert 'gives.map' in drawn and "icon('alert')" not in drawn
