"""A rule line's change in words (Propose, History): a changed who reads in full beside a number change, from the real
compiled rule_catalogue.ts in Node (logic only, no DOM)."""
import json
import subprocess



def words(client_modules, was, now, entry=None):
    script = (f"const m=await import({json.dumps((client_modules/'rule_catalogue.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const names={label:(f,v)=>v,tag:()=>undefined},e=i.entry;"
              "console.log(JSON.stringify(m.changeWords(e,{kind:i.was?'changed':'added',line:i.now,was:i.was},names)));")
    body = {'was': was, 'now': now, 'entry': entry or {'id': 'r', 'name': 'R'}}
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(body)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def line(people, most):
    return {'id': 'l1', 'who': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': people}]},
            'where': None, 'numbers': {'max': {'value': most, 'level': 'mandatory'}}}


def test_fifth_person_and_maximum_changed_together_show_the_person(client_modules):
    was, now = words(client_modules, line(list('ABCDE'), 5), line(list('ABCDF'), 6))
    assert 'E' in was and 'F' in now and 'more' not in was + now and was != now


def test_a_number_only_change_keeps_the_scope_short(client_modules):
    was, now = words(client_modules, line(list('ABCDE'), 5), line(list('ABCDE'), 6))
    more = json.loads(subprocess.run(['node', '--input-type=module', '-e', "const m=await import(" + json.dumps(
        (client_modules/'dom.js').as_uri()) + ");console.log(JSON.stringify(m.moreWords(1)));"],
        capture_output=True, text=True, check=True, timeout=15).stdout)
    # The scope stays short: four people, then the shared "and N more", never the fifth by name.
    assert was.startswith('A, B, C, D ' + more) and now.startswith('A, B, C, D ' + more) and was != now


def test_a_fair_share_supervision_line_names_its_measure_and_period(client_modules):
    entry = {'id': 'fair_share', 'name': 'Fair share', 'default_settings': {'balance_goal': {'metric': 'duties'}}}
    added = line(['Consultant'], 1) | {'numbers': {'apply': {'value': None, 'level': 'medium'}},
                                       'settings': {'balance_goal': {'metric': 'supervision', 'period': 'month'}}}
    was, now = words(client_modules, None, added, entry)
    assert was is None and 'applies to trainee sessions supervised over the calendar month' in now


def test_one_team_lead_across_concurrent_duties_says_so(client_modules):
    entry = {'id': 'team_leads', 'name': 'Team leads', 'unit': 'people', 'default_settings': {},
             'numbers': {'min': [{'kind': 'team_lead_requirement'}], 'max': [{'kind': 'team_lead_requirement'}]}}
    one = {'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 1, 'level': 'mandatory'}}
    together = line(['UCC'], 1) | {'numbers': one, 'settings': {'team_lead_requirement': {'count_scope': 'concurrent_duties'}}}
    per_duty = together | {'settings': {'team_lead_requirement': {'count_scope': 'duty'}}}
    was, now = words(client_modules, per_duty, together, entry)
    assert '1 team lead across these duties together' in now and 'together' not in was
