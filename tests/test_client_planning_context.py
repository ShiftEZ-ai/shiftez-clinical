"""The server's words on currency and line validity, read by the real compiled client in Node: an auto-schedule result is
current only when the server says `result_stale === false` (null or absent is unknown, never current), and a saved
rule line's validity is the server's unless its dates were edited in a draft."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS


def run(client_modules, module, body, data):
    script = (f"const m=await import({json.dumps((client_modules/module).as_uri())});const i=JSON.parse(process.argv[1]);"
              + body)
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_only_an_explicit_false_from_the_server_is_current(client_modules):
    job = {'kind': 'generation', 'input_fingerprint': 'f1'}
    cases = [{**job, 'result_stale': False}, {**job, 'result_stale': None}, dict(job), {**job, 'result_stale': True},
             {**job, 'result_stale': False, 'input_fingerprint': 'f0'}, {'kind': 'validation', 'input_fingerprint': 'f1'},
             # G1FP: a deferred run not yet prepared has no effective fingerprint: not known, never "the rota changed".
             {**job, 'input_fingerprint': None, 'result_stale': None},
             # Not compared yet and still queued (null fingerprint, or the server's explicit 'preparing'): no claim.
             {**job, 'status': 'queued', 'input_fingerprint': None},
             {**job, 'status': 'queued', 'input_fingerprint': 'f0', 'currency': 'preparing'},
             # A finished run with no fingerprint is never applicable; a compared mismatch is still "the rota changed".
             {**job, 'status': 'succeeded', 'input_fingerprint': None, 'result_stale': False},
             {**job, 'status': 'succeeded', 'input_fingerprint': 'f0', 'result_stale': False}]
    got = run(client_modules, 'planning_context.js', "console.log(JSON.stringify(i.map(j=>m.currency(j,'f1'))));", cases)
    assert got == ['', 'unknown', 'unknown', 'scheduler', 'rota', '', 'unknown', '', '', 'unknown', 'rota']


def test_saved_line_validity_is_the_servers_until_its_dates_change(client_modules):
    line = {'id': 'l1', 'starts_on': '2026-12-01', 'ends_on': None}
    # The rule's own dates make the line future on the server even though the line alone reads as active today.
    entry = {'rule': {'definition': {'rule': {'lines': [line]}}},
             'line_validity': {'l1': {'effective': 'future', 'starts_on': '2027-01-01', 'ends_on': None}}}
    body = ("const day='2026-12-15';m.learnValidity(i.entry,day);"
            "console.log(JSON.stringify([m.effectiveOf(i.line,day),m.validFacets(i.line,day),"
            "m.effectiveOf({...i.line,starts_on:'2026-12-10'},day),m.effectiveOf({id:'new',ends_on:'2026-12-10'},day)]));")
    saved, facets, edited, unknown = run(client_modules, 'rule_catalogue.js', body, {'entry': entry, 'line': line})
    assert saved == 'future' and 'Starts later' in facets and 'Ended' not in facets
    assert edited == 'active'  # an edited date (not saved yet) is read from the line itself
    assert unknown == 'ended'


def test_line_validity_from_a_different_catalogue_day_is_not_reused(client_modules):
    line = {'id': 'dated', 'starts_on': '2026-12-01', 'ends_on': None}
    entry = {'rule': {'definition': {'rule': {'lines': [line]}}},
             'line_validity': {'dated': {'effective': 'future', 'starts_on': '2027-01-01', 'ends_on': None}}}
    got = run(client_modules, 'rule_catalogue.js',
              "m.learnValidity(i.entry,'2026-12-15');console.log(JSON.stringify([m.effectiveOf(i.line,'2026-12-15'),"
              "m.effectiveOf(i.line,'2027-01-02')]));", {'entry': entry, 'line': line})
    assert got == ['future', 'active']


def test_supervisor_name_uses_only_the_offered_or_recorded_association(client_modules):
    rows = [{'supervision': {'proposal': {'supervisor_person_id': 's1'},
                            'choices': [{'supervisor_person_id': 's1', 'display_name': 'Checked supervisor'}]}},
            {'supervisor_person_id': 's2'}, {'participation': 'trainee', 'team_lead': True},
            {'supervision': {'proposal': None, 'choices': [{'supervisor_person_id': 's3', 'display_name': 'Not chosen'}]}}]
    got = run(client_modules, 'planner_teams.js',
              "console.log(JSON.stringify(i.map(r=>m.supervisedBy(r,id=>'Recorded '+id))));", rows)
    assert got == ['Checked supervisor', 'Recorded s2', '', '']


def test_supervised_assign_requires_a_chosen_plan_and_its_source_fingerprint(client_modules):
    rows = [{'display_name': 'Trainee', 'supervision': {'proposal': None, 'choices': []}},
            {'display_name': 'Trainee', 'supervision': {'proposal': None, 'choices': [{'supervisor_person_id': 's'}]}},
            {'display_name': 'Trainee', 'supervision': {'proposal': {'supervisor_person_id': 's'}, 'choices': []}}]
    got = run(client_modules, 'planner_week.js',
              "console.log(JSON.stringify(await Promise.all(i.map(async c=>{try{await m.assignCandidate({}, {}, c, {});"
              "return 'Unexpected write';}catch(e){return e.message;}}))));", rows)
    assert got == ['No qualified supervisor is available for this placement.', 'Choose who supervises Trainee first.',
                   'Open the session again to check the named supervisor before assigning.']


def test_operational_warning_does_not_claim_every_absence_was_approved(client_modules):
    got = run(client_modules, 'findings.js',
              "console.log(JSON.stringify([m.publishedAbsenceLabel(1),m.publishedAbsenceLabel(2,true)]));", {})
    assert got == ['Published duty overlaps unavailability', 'Your duties overlap unavailability']


# "People recorded for this duty" (planning_context.ts requirementEvidence) in a minimal DOM, its fixes drawn by the
# REAL app.ts fixActions (its compiled source, run with the few app globals it reads): each row offers only the fixes
# the server served for that person's own exclusions.

DIALOG_DOM = r"""
class M extends EventTarget{constructor(t){super();this.tagName=String(t).toUpperCase();this.kids=[];this.parentElement=null;this.attrs={};this.dataset={};this.disabled=false;this.hidden=false;this.open=false;}
get className(){return this.attrs.class||'';}set className(v){this.attrs.class=String(v);}
get classList(){const s=()=>this.className.split(/\s+/).filter(Boolean);return{contains:c=>s().includes(c),add:(...c)=>{this.className=[...new Set([...s(),...c])].join(' ');},remove:(...c)=>{this.className=s().filter(x=>!c.includes(x)).join(' ');},toggle:(c,on)=>{(on??!s().includes(c))?this.classList.add(c):this.classList.remove(c);}};}
setAttribute(k,v){this.attrs[k]=String(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-(.)/g,(_,c)=>c.toUpperCase())]=String(v);}
getAttribute(k){return this.attrs[k]??null;}hasAttribute(k){return k in this.attrs;}removeAttribute(k){delete this.attrs[k];}
get textContent(){return this.kids.map(k=>typeof k==='string'?k:k.textContent).join('');}set textContent(v){this.replaceChildren(String(v));}
append(...ns){for(const n of ns){if(typeof n==='string'){this.kids.push(n);continue;}n.remove();n.parentElement=this;this.kids.push(n);}}
prepend(...ns){const rest=this.kids;this.kids=[];this.append(...ns);this.kids.push(...rest);}
replaceChildren(...ns){for(const k of this.kids)if(typeof k!=='string')k.parentElement=null;this.kids=[];this.append(...ns);}
remove(){const p=this.parentElement;if(p){p.kids.splice(p.kids.indexOf(this),1);this.parentElement=null;}}
get children(){return this.kids.filter(k=>typeof k!=='string');}
get isConnected(){let n=this;while(n.parentElement)n=n.parentElement;return n===document.body;}
contains(x){for(let n=x;n;n=n.parentElement)if(n===this)return true;return false;}
all(){return this.children.flatMap(k=>[k,...k.all()]);}
matches(sel){return sel.split(',').some(p=>{p=p.trim();if(/[\s:>~+]/.test(p))return false;const m=p.match(/^([a-z][a-z0-9]*)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i);if(!m)return false;
  if(m[1]&&m[1].toUpperCase()!==this.tagName)return false;if(m[2]&&!m[2].split('.').filter(Boolean).every(c=>this.classList.contains(c)))return false;
  for(const a of m[3].match(/\[[^\]]+\]/g)||[]){const q=a.match(/^\[([\w-]+)(?:="?([^"\]]*)"?)?\]$/);if(!q)return false;if(q[2]===undefined?!this.hasAttribute(q[1]):this.getAttribute(q[1])!==q[2])return false;}return true;});}
closest(s){for(let n=this;n;n=n.parentElement)if(n.matches(s))return n;return null;}
querySelector(s){return this.all().find(n=>n.matches(s))||null;}querySelectorAll(s){return this.all().filter(n=>n.matches(s));}
focus(){document.activeElement=this;}blur(){}showModal(){this.open=true;}close(){this.open=false;}click(){this.dispatchEvent(new Event('click'));}
getBoundingClientRect(){return{left:0,right:0,top:0,bottom:0};}getClientRects(){return[];}get offsetParent(){return null;}get offsetWidth(){return 0;}get style(){return{setProperty(){}};}}
globalThis.Node=globalThis.Element=globalThis.HTMLElement=M;for(const k of ['HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','HTMLDialogElement'])globalThis[k]=class{};
globalThis.CSS={escape:s=>s};globalThis.requestAnimationFrame=f=>setTimeout(f,0);globalThis.innerWidth=1440;
Object.assign(document,{body:new M('body'),activeElement:null,removeEventListener(){},createElement:t=>new M(t),createElementNS:(ns,t)=>new M(t),createTextNode:t=>String(t),
  querySelector:s=>document.body.querySelector(s),querySelectorAll:s=>document.body.querySelectorAll(s)});
const replies=[],asked=[];
globalThis.fetch=async(path,opts)=>{asked.push({path,body:JSON.parse(opts.body)});const r=replies.shift();
  return new Response(JSON.stringify(r.body),{status:r.status,headers:{'content-type':'application/json'}});};
// Waits are on state, bounded by a number of event-loop turns (never a time): a clear error when it never comes.
const until=async(what,done)=>{for(let turn=0;turn<1000;turn++){if(done())return;await new Promise(r=>setImmediate(r));}throw new Error('Never reached: '+what);};
// A read is in flight from load()'s own 'Reading…' until its answer (rows or an error) is drawn, or the dialog closed.
const loaded=()=>until('the evidence read to be drawn',()=>{const d=document.querySelector('aside');return !d||d.querySelector('p[role=status]').textContent!=='Reading…';});
"""


def real_fix_actions(client_modules):
    """app.ts fixActions as compiled, unchanged: its text from app.js, from its signature to its closing brace."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    start = source.index('function fixActions(')
    depth = 0
    for i in range(source.index(') {', start) + 2, len(source)):  # the body's brace, past the signature's default {}
        depth += {'{': 1, '}': -1}.get(source[i], 0)
        if depth == 0:
            text = source[start:i + 1]
            assert text[:-1].rstrip().endswith('return out;'), text[-80:]
            return text
    raise AssertionError('fixActions not found whole')


def evidence_dialog(client_modules, replies, people, steps=''):
    """Open requirementEvidence for one observation with the real fixActions; `replies` are the evidence reads in order."""
    script = (GLOBALS + DIALOG_DOM +
              f"const dom=await import({json.dumps((client_modules/'dom.js').as_uri())});"
              f"const m=await import({json.dumps((client_modules/'planning_context.js').as_uri())});"
              "const i=JSON.parse(process.argv[1]);replies.push(...i.replies);const did=[];"
              "const app={permits:()=>true,find:(k,id)=>i.people[id],fullNameOf:p=>p.display_name,CAPABILITY:{COORDINATOR:'coordinator'},button:dom.button,"
              "personDetails:(p,pos,tab)=>did.push(['personDetails',p.id,tab]),navigate:r=>did.push(['navigate',r]),"
              "causeWhat:()=>'',catalogueEditable:()=>false,reportError:e=>did.push(['error',String(e)])};"
              "const fixActions=new Function('app','with(app){return ('+i.source+');}')(app);"
              "const data={workspace:{id:'w1',status:'draft'}};let checked=0;"
              "m.requirementEvidence({workspace:'w1',fingerprint:'fp1',observation:{id:'o1',shift_id:'s1'},name:(k,id)=>'Name '+id,zone:'UTC',"
              "fixes:(hints,close)=>fixActions(data,hints,()=>{did.push(['closed']);close();}),check:()=>{checked++;}});await loaded();"
              "const dlg=()=>document.querySelector('aside');"
              "const view=()=>{const d=dlg();if(!d)return {open:false};const rows=d.querySelector('ul.why-list');"
              "return {open:d.isConnected,status:d.querySelector('p[role=status]').textContent,"
              "rows:rows.children.map(li=>({text:li.textContent,buttons:li.querySelectorAll('button').map(b=>b.textContent)})),"
              "global:d.querySelectorAll('div.actions').map(a=>a.querySelectorAll('button').map(b=>b.textContent))};};"
              "const out={first:view()};" + steps + "out.asked=asked.map(a=>a.body);out.did=did;out.checked=checked;console.log(JSON.stringify(out));")
    data = {'replies': replies, 'people': people, 'source': real_fix_actions(client_modules)}
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


PEOPLE = {p: {'id': p, 'display_name': 'Dr ' + p.upper()} for p in ('p1', 'p2', 'p3')}


def hint(action, person):
    return {'action': action, 'prefill': {'starts_on': '2026-10-05', 'ends_on': '2026-10-06', 'person_id': person,
                                          'shift_ids': ['s1'], 'rule_id': None, 'service_ids': [], 'assignment_type_ids': []}}


def reply(candidates, fix_hints=(), total=None, has_more=False):
    return {'status': 200, 'body': {'candidates': candidates, 'total': len(candidates) if total is None else total,
                                    'offset': 0, 'limit': 50, 'has_more': has_more, 'fix_hints': list(fix_hints)}}


def person(pid, *exclusions, retained=False):
    return {'person_id': pid, 'retained': retained, 'exclusions': list(exclusions)}


def away(code, *hints):
    return {'code': code, 'related_records': [], 'fix_hints': list(hints)}


def test_a_row_offers_open_leave_only_from_its_own_served_hint_and_closes_through_the_dialog(client_modules):
    """p1's served UNAVAILABLE hint (named, permitted by the server) is its row's Open leave; p2 has a recorded
    unavailability whose hint the server withheld (no permission): no action; p3 has nothing recorded."""
    out = evidence_dialog(client_modules, [reply([person('p1', away('UNAVAILABLE', hint('open_leave', 'p1'))),
                                                 person('p2', away('UNAVAILABLE')), person('p3')])], PEOPLE,
                          "dlg().querySelector('ul.why-list').children[0].querySelector('button').click();await until('the dialog to close',()=>!dlg());out.after=view();")
    rows = out['first']['rows']
    assert [r['buttons'] for r in rows] == [['Open leave'], [], []]
    assert rows[0]['text'].startswith('Name p1 · Recorded unavailability') and 'nothing recorded rules them out' in rows[2]['text']
    assert rows[1]['text'] == 'Name p2 · Recorded unavailability'  # no record, category or reason
    assert out['did'] == [['closed'], ['personDetails', 'p1', 'leave']] and out['after'] == {'open': False}


def test_a_nameless_hint_in_a_row_still_renders_nothing(client_modules):
    out = evidence_dialog(client_modules, [reply([person('p1', away('UNAVAILABLE', hint('open_leave', None)))])], PEOPLE)
    assert [r['buttons'] for r in out['first']['rows']] == [[]]  # fixActions keeps its named-person guard


def test_mixed_served_hints_stay_and_duplicates_are_one_button(client_modules):
    leave, rotation = hint('open_leave', 'p1'), hint('open_rotation', 'p1')
    out = evidence_dialog(client_modules, [reply([person('p1', away('UNAVAILABLE', leave, leave), away('EXTERNAL_ROTATION', rotation, rotation))])], PEOPLE)
    assert [r['buttons'] for r in out['first']['rows']] == [['Open leave', 'Open rotations']]


def test_the_dialogs_own_fixes_paging_freshness_and_errors_are_unchanged(client_modules):
    """The top-level (population) hints still render in the dialog's own fix area, as before; Next and Previous read
    pages with the same fingerprint; a stale check says so and offers Check rota; a lost issue says so."""
    page1 = reply([person('p1')], fix_hints=[hint('open_rotation', None), hint('open_leave', None)], total=51, has_more=True)
    page2 = reply([person('p2', away('UNAVAILABLE', hint('open_leave', 'p2')))], fix_hints=[hint('open_rotation', None)], total=51)
    stale = {'status': 409, 'body': {'error': {'code': 'STALE_INPUTS', 'message': 'Stale'}}}
    steps = ("const navButton=t=>dlg().querySelectorAll('div.actions')[0].querySelectorAll('button').find(b=>b.textContent===t);"
             "navButton('Next').click();await loaded();out.second=view();"
             "navButton('Previous').click();await loaded();out.stale=view();"
             "navButton('Check rota').click();await until('the dialog to close',()=>!dlg());out.afterCheck=view();")
    out = evidence_dialog(client_modules, [page1, page2, stale], PEOPLE, steps)
    assert out['first']['status'] == '1–1 of 51 people checked for this duty'
    assert out['first']['global'] == [['Next'], ['Open rotations']]  # the nameless leave hint draws nothing here either
    assert out['second']['status'] == '51–51 of 51 people checked for this duty'
    assert [r['buttons'] for r in out['second']['rows']] == [['Open leave']]
    assert out['second']['global'] == [['Previous'], ['Open rotations']]
    assert out['stale']['rows'] == [{'text': 'The rota changed since its check. Check the rota first.', 'buttons': []}]
    assert out['stale']['global'][0] == ['Check rota'] and out['checked'] == 1 and out['afterCheck'] == {'open': False}
    assert out['asked'] == [{'expected_fingerprint': 'fp1', 'session_ids': ['s1'], 'offset': o, 'limit': 50} for o in (0, 50, 0)]
    lost = evidence_dialog(client_modules, [{'status': 404, 'body': {'error': {'code': 'NOT_FOUND', 'message': 'Gone'}}}], PEOPLE)
    assert lost['first']['rows'] == [{'text': 'This issue is no longer in the rota check.', 'buttons': []}]
