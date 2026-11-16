"""Phone Home's Needs you, from the real compiled home.ts in Node (logic only, no DOM): a draft is one row counted in its
Fix list's must-fix causes (the server's fix_list.summary, D9) with Fix list, shown on a phone too (D10); what only a
desktop does still waits there."""
import json
import subprocess
from packages.domain.time import now

ITEMS = [
    {'key': 'blocking', 'count': 77, 'rota_id': 'r1', 'rota_name': 'October', 'rotas': 1,
     'fix_list': {'summary': {'must': 3, 'could': 5, 'checked_at': None}}},
    {'key': 'open_places', 'count': 4, 'rota_id': 'r1', 'rota_name': 'October', 'rotas': 1},
    {'key': 'requests', 'count': 1, 'sample': []},
    {'key': 'failed_notifications', 'count': 1},
]


def needs(client_modules, items):
    script = (f"const m=await import({json.dumps((client_modules/'home.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const went=[];const ctx={me:{organisation:{timezone:'UTC'},person_id:'p0'},catalogues:{},navigate:t=>went.push(t),setWorkspace:()=>{}};"
              "const rows=m.needsOf(ctx,{},{attention:i,rotas:1,my_requests:[],recent_changes:[]});"
              "rows.find(r=>r.key==='blocking')?.go();"
              "console.log(JSON.stringify({rows:rows.map(r=>({key:r.key,count:r.count,context:r.context,action:r.action,phone:m.onPhone(r)})),went}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(items)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_a_draft_is_one_row_of_its_must_fix_causes_with_fix_list_on_a_phone(client_modules):
    got = needs(client_modules, ITEMS)
    by = {r['key']: r for r in got['rows']}
    # One row for the draft: the Fix list's causes, never its findings (77) or open places (4).
    assert 'open_places' not in by
    assert by['blocking']['count'] == 3 and by['blocking']['action'] == 'Fix list' and by['blocking']['phone']
    assert by['blocking']['context'] == 'Must fix before submitting'
    assert got['went'] == ['schedule/issues?draft=r1']
    assert by['requests']['phone']
    assert not by['failed_notifications']['phone']


def test_nothing_must_be_fixed_says_what_could_improve(client_modules):
    got = needs(client_modules, [{'key': 'open_places', 'count': 5, 'rota_id': 'r2', 'rota_name': 'November', 'rotas': 1,
                                  'fix_list': {'summary': {'must': 0, 'could': 2, 'checked_at': None}}}])
    assert got['rows'] == [{'key': 'open_places', 'count': 2, 'context': '2 could improve', 'action': 'Fix list', 'phone': True}]


def test_an_approved_absence_on_a_published_duty_is_a_phone_replace_for_the_coordinator(client_modules):
    """B6, D10, D13: an approved absence over an issued duty (/api/home `published_absence_conflicts` with its
    PublishedOperationalAction) is a phone row whose Replace opens that session on the published Schedule; an amendment
    already open is continued instead."""
    day = now().date().isoformat()
    sample = {'publication_id': 'pub1', 'workspace_id': 'w1', 'shift_id': 's1', 'assignment_id': 'a1', 'person_id': 'p9',
              'start': f'{day}T13:00:00Z', 'end': f'{day}T16:00:00Z'}
    action = {'key': 'amend_publication', 'publication_id': 'pub1', 'workspace_id': 'w1', 'shift_id': 's1', 'date': day,
              'amendment_shift_state': 'not_created'}
    script = (f"const m=await import({json.dumps((client_modules/'home.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              "const went=[];const ctx={me:{organisation:{timezone:'UTC'},person_id:'p0'},catalogues:{people:[{id:'p9',display_name:'Felix Parker'}]},navigate:t=>went.push(t),setWorkspace:()=>{}};"
              "const rows=m.needsOf(ctx,{},{attention:i,rotas:1,my_requests:[],recent_changes:[]});rows[0].go();"
              "console.log(JSON.stringify({row:{action:rows[0].action,phone:m.onPhone(rows[0]),context:rows[0].context},went}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script,
                          json.dumps([{'key': 'published_absence_conflicts', 'count': 1, 'sample': [sample], 'action': action}])],
                         capture_output=True, text=True, check=True, timeout=15)
    got = json.loads(out.stdout)
    assert got['row']['action'] == 'Replace' and got['row']['phone'] is True
    assert got['row']['context'].startswith('Felix Parker · ')
    assert got['went'] == [f'schedule/week/{day}?source=pub1&shift=s1']


def test_an_approvers_phone_lists_requests_and_rota_reviews_and_leaves_rules_to_a_desktop(client_modules):
    """maintenance: on a phone an approver finishes requests, person facts and a rota review (D25: it opens the review);
    a rule change, a rule exception and Ready to publish wait for a desktop (they still count there)."""
    got = needs(client_modules, [
        {'key': 'requests', 'count': 2, 'sample': [{'person': 'Test Person', 'kind': 'leave', 'start': '2026-11-10T00:00:00Z', 'end': '2026-11-11T00:00:00Z'}]},
        {'key': 'scheduling_facts', 'count': 1, 'sample': []},
        {'key': 'reviews', 'count': 1, 'rota_id': 'r9', 'rota_name': 'December', 'rotas': 1},
        {'key': 'rules', 'count': 1, 'sample': [{'name': 'Days in a row', 'by': 'Test Coordinator'}]},
        {'key': 'exceptions', 'count': 1},
        {'key': 'to_publish', 'count': 1, 'rota_id': 'r8', 'rota_name': 'November', 'rotas': 1},
    ])
    phone = {r['key']: r['phone'] for r in got['rows']}
    assert phone == {'requests': True, 'scheduling_facts': True, 'reviews': True, 'rules': False, 'exceptions': False, 'to_publish': False}
