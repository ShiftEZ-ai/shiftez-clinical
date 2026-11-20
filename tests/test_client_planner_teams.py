"""Supervision teams and lead context, from the real compiled planner_teams.ts in Node (logic only, no DOM): each
supervisor once with their recorded trainees beneath, a missing supervisor said, per-assignment team membership
(CA-03), and the lead-context projections the Schedule reads (TL-02/TL-03)."""
import json
from pathlib import Path
import subprocess
import pytest

from .test_client_autoschedule_live import CLICKS, FAKE_DOM, node

ROOT = Path(__file__).resolve().parents[1]


def run(client_modules, body, data):
    script = (f"const m=await import({json.dumps((client_modules/'planner_teams.js').as_uri())});const i=JSON.parse(process.argv[1]);"
              f"console.log(JSON.stringify(({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def p(pid, sup=None, **extra):
    return {'person_id': pid, 'supervisor_person_id': sup, **extra}


def test_supervisor_once_with_trainees_beneath_and_missing_supervisor_said(client_modules):
    people = [p('t1', 'c'), p('x'), p('c', team_lead=True), p('t2', 'c'), p('t3', 'gone')]
    order = run(client_modules, "m.teamOrder(i).map(e=>[e.p.person_id,e.depth,e.missing,e.trainees])", people)
    assert order == [['x', 0, False, 0], ['c', 0, False, 2], ['t1', 1, False, 0], ['t2', 1, False, 0], ['t3', 0, True, 0]]
    text = run(client_modules, "m.teamText(i,id=>id.toUpperCase(),q=>q.team_lead?' TL':'')", people)
    assert text == 'X · C TL (supervising T1, T2) · T3 (with GONE, not on this session)'


def test_partial_rows_and_cycles_list_everyone_once(client_modules):
    # A supervisor with two partial rows heads one team; a recorded cycle still lists both people.
    people = [p('c', start='08'), p('t', 'c'), p('c', start='12'), p('a', 'b'), p('b', 'a')]
    order = run(client_modules, "m.teamOrder(i).map(e=>[e.p.person_id,e.depth])", people)
    assert order == [['c', 0], ['t', 1], ['c', 0], ['a', 0], ['b', 1]]
    # The Week/Month pill: one group per team, each person once; teamParts (List) reads the same groups.
    groups = run(client_modules, "m.teamGroups(i).map(g=>g.map(e=>e.p.person_id))", people)
    assert groups == [['c', 't'], ['c'], ['a', 'b']]


def test_team_groups_keep_every_trainee_with_their_supervisor(client_modules):
    # Several trainees (one of them a trainee's trainee) sit in their supervisor's one group; others stand alone.
    people = [p('t1', 'c'), p('x'), p('c'), p('t2', 'c'), p('t3', 't1'), p('y', 'gone')]
    groups = run(client_modules, "m.teamGroups(i).map(g=>g.map(e=>[e.p.person_id,e.depth]))", people)
    assert groups == [[['x', 0]], [['c', 0], ['t1', 1], ['t3', 2], ['t2', 1]], [['y', 0]]]
    assert run(client_modules, "m.teamParts(i,id=>id.toUpperCase())", people) == [
        'X', 'C (supervising T1, T3, T2)', 'Y (with GONE, not on this session)']


def test_team_membership_is_per_assignment_and_ignores_team_lead(client_modules):
    day1 = [p('t', 'b'), p('b')]
    day2 = [p('t', 'c'), p('c')]
    lead_only = [p('l', team_lead=True), p('s')]
    got = run(client_modules, "[m.teamOf(i.d1,i.d1[0]),m.teamOf(i.d2,i.d2[0]),m.teamOf(i.d1,i.d1[1]),m.teamOf(i.lo,i.lo[0]),m.teamOf(i.lo,i.lo[1])]",
              {'d1': day1, 'd2': day2, 'lo': lead_only})
    assert got == ['b', 'c', 'b', '', '']


def test_lead_context_projections(client_modules):
    ctx = {'workspace_revision': 4, 'input_revision': 1, 'context_hash': 'h', 'people': [{'person_id': 'a', 'can_lead': True, 'failures': []},
           {'person_id': 'b', 'can_lead': False, 'failures': [{'start': 's', 'end': 'e', 'code': 'LEAD_NOT_QUALIFIED', 'sources': []}]}],
           'segments': [{'start': 's', 'end': 'e', 'enabled': True, 'minimum': 1, 'maximum': 1, 'continuity': 'whole_duty', 'sources': []}]}
    got = run(client_modules, "[m.leadState(i,4).state,m.leadState(i,5).state,m.canLead(m.leadState(i,4),'a'),m.canLead(m.leadState(i,4),'b'),"
              "m.canLead(m.leadState(i,4),'z'),m.canLead(m.leadState(i,5),'a'),m.leadNeed(i),m.leadNeed({...i,segments:[]})]", ctx)
    assert got[:6] == ['ready', 'stale', True, False, None, None]
    assert got[6] == {'enabled': True, 'minimum': 1, 'maximum': 1, 'wholeDuty': True, 'conflict': False}
    assert got[7]['enabled'] is False and got[7]['maximum'] is None


def test_lead_context_shares_only_an_answer_in_flight(client_modules):
    """Who may lead changes with rules and people without a new rota revision: identical questions asked together share
    one request, but a later ask (the picker reopened) always reads the server again; a failure keeps its error."""
    script = ("let calls=0,fail=false;globalThis.document={cookie:''};"
              "globalThis.fetch=async()=>{calls++;return fail?new Response('{}',{status:503}):new Response(JSON.stringify({n:calls}),{status:200});};"
              "const input={shift:{},person_ids:['a']};"
              "const [x,y]=await Promise.all([m.leadContext('w',1,input),m.leadContext('w',1,input)]);"
              "const z=await m.leadContext('w',1,input);fail=true;"
              "const e=await m.leadContext('w',1,input).catch(e=>e);"
              "return {together:[x.n,y.n],later:z.n,calls,status:e.status,code:e.code};")
    got = run(client_modules, f"await (async()=>{{{script}}})()", None)
    assert got == {'together': [1, 1], 'later': 2, 'calls': 3, 'status': 503, 'code': 'SERVER_ERROR'}


def test_person_display_keeps_the_person_their_recorded_team_or_everyone(client_modules):
    """Search › Show (PlannerViewSettings.person_display) on one session: display only, recorded same-session
    supervision only (never rank), and a session the person is not on stays whole."""
    people = [p('c'), p('t1', 'c'), p('t2', 'c'), p('x'), p('t3', 'x')]
    show = lambda person, how: run(client_modules, f"m.shownPeople(i,{json.dumps(person)},{json.dumps(how)}).map(e=>e.person_id)", people)
    assert show('t1', 'duty') == ['c', 't1', 't2', 'x', 't3']
    assert show('t1', 'person') == ['t1']
    assert show('t1', 'team') == ['c', 't1', 't2']
    assert show('c', 'team') == ['c', 't1', 't2']
    assert show('x', 'team') == ['x', 't3']
    assert show('nobody', 'team') == ['c', 't1', 't2', 'x', 't3']
    assert run(client_modules, "m.shownPeople(i,'solo','team').map(e=>e.person_id)", [p('solo'), p('y')]) == ['solo']


def test_person_display_never_changes_the_session_roster(client_modules):
    """Display only: the projection returns a new list and leaves the session's own participants whole, and the
    Schedule never rebuilds a shift's participants from it (panels and commands — remove, edit, assign, lead — keep
    reading and writing every assignment). Root review of fbb369a6."""
    people = [p('c'), p('t1', 'c'), p('x', note='kept')]
    out = run(client_modules, "(()=>{const before=JSON.stringify(i);const shown=m.shownPeople(i,'t1','person');"
              "return [shown.length,i.length,JSON.stringify(i)===before,shown!==i];})()", people)
    assert out == [1, 3, True, True]
    app = (ROOT / 'apps' / 'web' / 'src' / 'app.ts').read_text(encoding='utf-8')
    week = (ROOT / 'apps' / 'web' / 'src' / 'planner_week.ts').read_text(encoding='utf-8')
    assert 'weekView.personView=' in app and 'shownPeople(' not in app
    assert 'participants:kept' not in app + week and 'hidden_people' not in app + week


def test_a_person_display_never_invents_a_missing_supervisor(client_modules):
    """Absence is judged on the whole session, never on what a person display draws: a supervisor assigned here but
    not drawn is named as the trainee's supervisor and never said to be "not on this session"; a supervisor truly
    absent from the session still is. Current redesign acceptance P1 (Day teamText / Week chips from drawn.shown)."""
    roster = [p('c'), p('t1', 'c'), p('t2', 'c'), p('q'), p('t3', 'gone')]
    drawn = [roster[1], roster[4]]
    order = run(client_modules, "m.teamOrder(i.drawn,i.roster).map(e=>[e.p.person_id,e.depth,e.missing])", {'drawn': drawn, 'roster': roster})
    assert order == [['t1', 0, False], ['t3', 0, True]]
    text = run(client_modules, "m.teamText(i.drawn,id=>id.toUpperCase(),()=>'',i.roster)", {'drawn': drawn, 'roster': roster})
    assert text == 'T1 (with C) · T3 (with GONE, not on this session)'
    # The whole session drawn: unchanged (no extra "with" for a trainee already beneath their supervisor).
    assert run(client_modules, "m.teamText(i,id=>id.toUpperCase())", roster) == 'C (supervising T1, T2) · Q · T3 (with GONE, not on this session)'
    src = ROOT / 'apps' / 'web' / 'src'
    week = (src / 'planner_week.ts').read_text(encoding='utf-8')
    schedule = (src / 'schedule.ts').read_text(encoding='utf-8')
    tools = (src / 'planner_tools.ts').read_text(encoding='utf-8')
    # Every renderer that says "not on this session" from a drawing passes the whole session as the roster.
    assert 'teamGroups(drawnPeople(s).shown,s.participants as Row[])' in week
    assert 'teamGroups(drawn.shown,s.participants as Row[])' in schedule and 'teamText(rest,nameOf,tl,s.participants as Row[])' in schedule
    assert 'teamParts(d.shown,nameOf,p=>tl(p,s),s.participants as Row[])' in tools


def test_list_more_opens_the_whole_session_not_a_projected_popover(client_modules):
    """List › People under a person display: every drawn pill shows and "+N" counts exactly the people left out and
    opens the session (everyone in full, with their checks), never an "All people" popover of the drawing."""
    src = ROOT / 'apps' / 'web' / 'src'
    tools = (src / 'planner_tools.ts').read_text(encoding='utf-8')
    menu = (src / 'table_menu.ts').read_text(encoding='utf-8')
    assert "rest:{count:drawnPeople(s).hidden.length,open:()=>week?.details(s)}" in tools
    assert "pchip-lite others" not in tools
    assert 'overflowMore(rest.count' in menu  # the shared "+N"
    # The shared "+N" counts the people left out and opens the whole session once: no popover of the drawn pills.
    out = node(client_modules, 'table_menu.js', FAKE_DOM + CLICKS + (
        "let opened=0;const w=m.pillList(['a','b'],{label:'people',shown:1,rest:{count:2,open:()=>opened++}}),"
        "more=w.querySelector('.pill-more');more.click();"
        "console.log(JSON.stringify({text:more.textContent,name:more.attrs['aria-label'],popup:more.attrs['aria-haspopup']??null,"
        "opened,hidden:w.querySelectorAll('.pill-over').length}));"))
    assert out == {'text': '+2', 'name': '2 more people: show all 4', 'popup': None, 'opened': 1, 'hidden': 0}


def test_fill_candidates_and_grip_values_do_not_depend_on_mount_timing():
    """app.ts builds a page off-page and shows it after its reads. The fill panel's candidates paint when the answer
    comes (a paint deferred to one frame and skipped while detached stuck at "Checking…"); gripped headings are watched
    again once the page is shown, so a saved width's separator has its aria-valuenow after a reload."""
    src = ROOT / 'apps' / 'web' / 'src'
    week = (src / 'planner_week.ts').read_text(encoding='utf-8')
    app = (src / 'app.ts').read_text(encoding='utf-8')
    menu = (src / 'table_menu.ts').read_text(encoding='utf-8')
    assert 'requestAnimationFrame(render)' not in week and 'const render=()=>{if(!list.isConnected)return;' not in week
    assert 'restoreRulesScroll(main);trackWidths(main);' in app
    assert 'unwatchWidth.get(th)?.();unwatchWidth.set(th,observeSize(th,widthOf));' in menu  # observed afresh


def test_gripped_headings_keep_their_own_names():
    """A width grip is a named separator inside a heading: the heading keeps its own name ("Rule / who", "N people"),
    never "N people Resize Row headings". Rules headings are named like dataTable's; Schedule corners and detail tables
    take their visible text through one helper that leaves explicit, interactive and empty headings alone."""
    src = ROOT / 'apps' / 'web' / 'src'
    rules = (src / 'rules_tree.ts').read_text(encoding='utf-8')
    week = (src / 'planner_week.ts').read_text(encoding='utf-8')
    menu = (src / 'table_menu.ts').read_text(encoding='utf-8')
    # The Rules list has no table headings (one row per rule); the issue table's headings are its only gripped ones.
    assert rules.count(',grip(') == 1
    assert "el('th',{scope:'col','aria-label':c.label,'data-col':c.key," in rules
    # Named first: the corner then takes tabindex=-1 so its grip keeps focus (maintenance, 0d503d2ec), and a heading that
    # already has a tabindex is an interactive one, which gripHeadingName leaves unnamed.
    assert "{gripHeadingName(at);if(!at.hasAttribute('tabindex'))at.tabIndex=-1;at.append(columnGrip({" in week
    assert "const interactiveHeading=(th:Element)=>th.matches('[tabindex],[data-rk-pin]')" in menu
    assert "gripHeadingName(c.th,c.label);\n    c.th.append(columnGrip(" in menu
    assert "if(label&&!h.hasAttribute('aria-label')&&!h.hasAttribute('aria-labelledby')&&!interactiveHeading(h))" in menu
