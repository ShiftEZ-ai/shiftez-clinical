"""Window-first draft reads and action replay, executed from the real compiled client."""
import json

import pytest

from .test_client_app_fragments import cut, node


def app_function(client_modules, name):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    return cut(source, rf'\n((?:async )?function {name}\([^\n]*\{{.*?\n\}})\n')


@pytest.mark.parametrize('boot', [False, True])
def test_read_ahead_starts_compact_and_published_window_reads_but_never_whole(client_modules, boot):
    got = node(r"""
const sent=[],read=path=>{sent.push(path);return Promise.resolve({});},get=read,background=read;
const rotaMeta=(id,reader)=>reader('/api/workspaces/'+id+'?view=compact');
const wholeRota=(id)=>read('/api/workspaces/'+id+'?view=schedule');
const routeQuery=new URLSearchParams('edit=w'),view='schedule',wholeSeen=new Set(),scheduleEpoch=2,
  workspaceListOffset=0,planDayFromRoute=true,schedView='week',layerMode='edit',sourcePublication='',
  planDay='2026-10-04',me={organisation:{week_start:6}},CAPABILITY={};
let ahead=null;const schedSubPage=()=>false,permits=()=>true,phoneLite=()=>false,
  periodNow=()=>({days:['2026-10-04','2026-10-10']}),addDays=()=> '2026-10-11';
""" + app_function(client_modules, 'readAhead') + f"\nreadAhead({json.dumps(boot)});" + r"""
console.log(JSON.stringify({sent,id:ahead.id,epoch:ahead.epoch,range:ahead.range}));""")
    assert got == {'sent': (['/api/workspaces?limit=50&offset=0'] if boot else []) + [
        '/api/workspaces/w?view=compact', '/api/schedule/sessions?start=2026-10-04&end=2026-10-11'],
        'id': 'w', 'epoch': 2, 'range': 'start=2026-10-04&end=2026-10-11'}


WHOLE_STATE = r"""
let scheduleEpoch=0,draftReadIn='';const schedView='week',wholeSeen=new Set(),wholeReads=new Map(),
  scheduleCache={drafts:new Map()},sent=[],answers=[];
function get(path,options){sent.push({path,options});return new Promise((resolve,reject)=>answers.push({resolve,reject}));}
const rotaWhole=(id,reader)=>reader('/api/workspaces/'+id+'?view=schedule');
const rota=revision=>({workspace:{id:'w',revision},shifts:[]});
"""


def test_whole_reads_coalesce_until_answered_and_keep_the_newer_draft(client_modules):
    got = node(WHOLE_STATE + app_function(client_modules, 'wholeRota') + r"""
const a=wholeRota('w'),b=wholeRota('w');const same=a===b;
scheduleCache.drafts.set('w',rota(5));answers[0].resolve(rota(4));
const revisions=(await Promise.all([a,b])).map(d=>d.workspace.revision);
console.log(JSON.stringify({same,revisions,sent,seen:wholeSeen.has('w'),pending:wholeReads.size,
  kept:scheduleCache.drafts.get('w').workspace.revision}));""")
    assert got == {'same': True, 'revisions': [5, 5], 'sent': [{'path': '/api/workspaces/w?view=schedule', 'options': {'quiet': False}}],
                   'seen': True, 'pending': 0, 'kept': 5}


def test_a_failed_whole_read_is_forgotten_and_the_next_action_can_read_again(client_modules):
    got = node(WHOLE_STATE + app_function(client_modules, 'wholeRota') + r"""
const a=wholeRota('w');answers[0].reject(new Error('read failed'));const error=await a.catch(e=>e.message);
const failed={seen:wholeSeen.has('w'),pending:wholeReads.size,kept:scheduleCache.drafts.size};
const b=wholeRota('w');answers[1].resolve(rota(3));await b;
console.log(JSON.stringify({error,failed,reads:sent.length,seen:wholeSeen.has('w'),kept:scheduleCache.drafts.get('w').workspace.revision}));""")
    assert got == {'error': 'read failed', 'failed': {'seen': False, 'pending': 0, 'kept': 0},
                   'reads': 2, 'seen': True, 'kept': 3}


def test_an_old_epoch_cannot_cache_a_reply_or_drop_a_newer_read(client_modules):
    got = node(WHOLE_STATE + app_function(client_modules, 'wholeRota') + r"""
const old=wholeRota('w');scheduleEpoch++;const current=wholeRota('w');answers[0].resolve(rota(1));await old;
const afterOld={pending:wholeReads.size,kept:scheduleCache.drafts.size};answers[1].resolve(rota(2));await current;
console.log(JSON.stringify({afterOld,reads:sent.length,kept:scheduleCache.drafts.get('w').workspace.revision,draftReadIn}));""")
    assert got == {'afterOld': {'pending': 1, 'kept': 0}, 'reads': 2, 'kept': 2, 'draftReadIn': 'week'}


def test_a_fix_list_read_does_not_mark_the_planner_whole_seen(client_modules):
    got = node(WHOLE_STATE + app_function(client_modules, 'wholeRota') + r"""
const read=wholeRota('w',true,false);answers[0].resolve(rota(1));await read;
console.log(JSON.stringify({seen:wholeSeen.has('w'),sent,kept:scheduleCache.drafts.size}));""")
    assert got == {'seen': False, 'sent': [{'path': '/api/workspaces/w?view=schedule', 'options': {'quiet': True}}], 'kept': 1}


@pytest.mark.parametrize('changed', [False, True])
def test_window_parts_page_sessions_findings_and_coverage_and_reject_mixed_revisions(client_modules, changed):
    got = node(r"""
const sent=[],meta={workspace:{id:'w',revision:3,validation:{valid:false}},health:{fresh:true}},addDays=()=> '2026-10-11';
async function get(path){sent.push(path);const q=new URL('https://example.test'+path).searchParams,
  shift=q.get('part')==='shifts',offset=Number(q.get('offset'));
return {revision:""" + ('offset?4:3' if changed else '3') + r""",limit:1,has_more:shift&&offset===0,
  coverage_has_more:!shift&&offset===0,items:shift?[{id:'s'+offset}]:offset?[]:[{code:'OPEN'}],
  coverage:shift?[]:[{shift_id:'s'+offset}]};}
""" + app_function(client_modules, 'readWindow') + r"""
const result=await readWindow(meta,['2026-10-04','2026-10-10']);console.log(JSON.stringify({result,sent}));""")
    assert len(got['sent']) == 4
    queries = [url.split('?', 1)[1] for url in got['sent']]
    assert all('start=2026-10-04&end=2026-10-11' in query for query in queries)
    assert {query.split('&')[0] + '&' + query.split('&')[-1] for query in queries} == {
        'part=shifts&offset=0', 'part=shifts&offset=1', 'part=validation&offset=0', 'part=validation&offset=1'}
    assert all(url.startswith('/api/workspaces/w/parts?') for url in got['sent'])
    if changed:
        assert got['result'] is None
    else:
        assert got['result'] == {'partial': True, 'health': {'fresh': True}, 'shifts': [{'id': 's0'}, {'id': 's1'}],
                                'workspace': {'id': 'w', 'revision': 3, 'validation': {
                                    'valid': False, 'findings': [{'code': 'OPEN'}], 'coverage': [{'shift_id': 's0'}, {'shift_id': 's1'}]}}}


@pytest.mark.parametrize(('held', 'change', 'expected'), [
    (None, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 3}}, False),
    ({'workspace': {'revision': 3}}, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 3}}, True),
    ({'workspace': {'revision': 4}}, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 3}}, True),
    ({'workspace': {'revision': 2}}, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 3}}, False),
    ({'partial': True, 'workspace': {'revision': 4}}, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 3}}, False),
    ({'workspace': {'revision': 4}}, {'scope': 'workspace', 'id': 'other', 'workspace': {'revision': 3}}, False),
    ({'workspace': {'revision': 4}}, {'scope': 'rules', 'id': 'w', 'workspace': {'revision': 3}}, False),
    ({'workspace': {'revision': 4}}, {'scope': 'workspace', 'id': 'w'}, False),
    ({'workspace': {'revision': 4}}, {'scope': 'workspace', 'id': 'w', 'workspace': {'revision': 0}}, False),
])
def test_only_a_whole_draft_that_already_includes_a_live_change_can_ignore_it(client_modules, held, change, expected):
    got = node(f"const scheduleCache={{drafts:new Map([['w',{json.dumps(held)}]])}};" +
               "const remoteEdit=c=>c.scope==='workspace'?c:null;" + app_function(client_modules, 'heldAlready') +
               f"console.log(JSON.stringify(heldAlready({json.dumps(change)}))); ")
    assert got is expected


REPLAY_IO = r"""
const handlers=new Map(),messages=[],calls=[],CSS={escape:s=>s};
class Element {
  constructor(){this.localName='button';this.attrs={'data-assignment':'a'};this.dataset={};this.isConnected=true;}
  closest(s){
    if(s===WINDOW_ONLY)return this.windowOnly?this:null;
    if(s==='.pchip-more')return this.chipMenu?this:null;
    if(s==='button'&&this.ruleMark)return null;
    return this;
  }
  matches(s){return !!this.ruleMark&&s.startsWith('[data-rk-pin=');}
  querySelector(s){return s==='.pchip-more'?{dispatchEvent:e=>calls.push('menu '+e.type)}:null;}
  hasAttribute(a){return a in this.attrs;}getAttribute(a){return this.attrs[a];}
  focus(){calls.push('focus');}click(){calls.push('click');}dispatchEvent(e){calls.push(e.type);}
  getBoundingClientRect(){return {left:10,bottom:20};}
}
class MouseEvent {constructor(type,options={}){this.type=type;Object.assign(this,options);}
  preventDefault(){this.prevented=true;}stopImmediatePropagation(){this.stopped=true;}}
class KeyboardEvent extends MouseEvent {}
const original=new Element(),view={addEventListener:(t,f)=>handlers.set(t,f),contains:()=>true};
let current=new Element(),pending,answer,loads=0;
const document={querySelector:()=>({querySelector:()=>current})},afterWired=(_,f)=>f(),
  focusSpot=()=>({key:'add',nth:0}),sameKey=()=>current?[current]:[],notify=(...v)=>messages.push(v);
function load(pressed,replay){loads++;pending=new Promise(r=>{answer=r;}).then(()=>{replay();return true;});return pending;}
function send(type,options={}){const e=type==='keydown'?new KeyboardEvent(type,options):new MouseEvent(type,options);
  e.target=original;handlers.get(type)(e);return {prevented:!!e.prevented,stopped:!!e.stopped};}
"""


def replay_code(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    constants = cut(source, r'\n(const WHOLE_USE = .*?)\nfunction onWholeUse\(')
    return constants + REPLAY_IO + app_function(client_modules, 'onWholeUse') + "\nonWholeUse(view,'#schedule-view',WHOLE_USE,load);\n"


@pytest.mark.parametrize(('event', 'options', 'actions', 'message'), [
    ('click', {}, ['focus', 'click'], []),
    ('dblclick', {}, ['focus', 'dblclick'], []),
    ('contextmenu', {}, ['focus', 'contextmenu'], []),
    ('keydown', {'key': 'x'}, ['focus', 'keydown'], []),
    ('keydown', {'key': 'ContextMenu'}, ['focus', 'keydown'], []),
    ('keydown', {'key': 'F10', 'shiftKey': True}, ['focus', 'keydown'], []),
    ('dragstart', {}, ['focus'], [['Whole rota loaded · drag again', 'info']]),
])
def test_whole_use_waits_and_replays_each_action_once_despite_repeat_presses(client_modules, event, options, actions, message):
    got = node(replay_code(client_modules) + f"const first=send({json.dumps(event)},{json.dumps(options)});" + r"""
send('click');send('contextmenu');const before=[...calls];answer();await pending;
console.log(JSON.stringify({first,before,loads,calls,messages}));""")
    assert got == {'first': {'prevented': True, 'stopped': True}, 'before': [], 'loads': 1, 'calls': actions, 'messages': message}


@pytest.mark.parametrize('key', ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'Tab', 'Escape', 'Enter', ' '])
def test_focus_and_native_button_keys_do_not_start_a_whole_read_on_keydown(client_modules, key):
    got = node(replay_code(client_modules) + f"const event=send('keydown',{{key:{json.dumps(key)}}});" +
               "console.log(JSON.stringify({event,loads,calls}));")
    assert got == {'event': {'prevented': False, 'stopped': False}, 'loads': 0, 'calls': []}


@pytest.mark.parametrize(('action', 'event', 'options', 'calls'), [
    ('ruleMark', 'keydown', {'key': 'Enter', 'shiftKey': True}, ['focus', 'keydown']),
    ('chipMenu', 'click', {}, ['focus', 'menu click']),
])
def test_rule_enter_and_the_chip_menu_replay_on_the_new_control(client_modules, action, event, options, calls):
    got = node(replay_code(client_modules) +
               f"original.{action}=true;const first=send({json.dumps(event)},{json.dumps(options)});" + r"""
send('click');const before=[...calls];answer();await pending;
console.log(JSON.stringify({first,before,loads,calls}));""")
    assert got == {'first': {'prevented': True, 'stopped': True}, 'before': [], 'loads': 1, 'calls': calls}


@pytest.mark.parametrize('modifier', ['ctrlKey', 'metaKey', 'altKey'])
def test_modified_x_is_left_to_the_browser(client_modules, modifier):
    got = node(replay_code(client_modules) + f"const event=send('keydown',{{key:'x',{modifier}:true}});" +
               "console.log(JSON.stringify({event,loads}));")
    assert got == {'event': {'prevented': False, 'stopped': False}, 'loads': 0}


def test_window_only_overflow_does_not_load_whole(client_modules):
    got = node(replay_code(client_modules) + r"""
original.windowOnly=true;const event=send('click');console.log(JSON.stringify({event,loads,calls}));""")
    assert got == {'event': {'prevented': False, 'stopped': False}, 'loads': 0, 'calls': []}


def test_a_target_missing_after_the_read_is_said_and_never_replayed(client_modules):
    got = node(replay_code(client_modules) + r"""
send('click');current=null;answer();await pending;console.log(JSON.stringify({loads,calls,messages}));""")
    assert got == {'loads': 1, 'calls': [], 'messages': [['This duty is no longer in the rota.', 'info']]}


def test_a_toolbar_action_is_refound_before_its_busy_label_changes(client_modules):
    got = node(replay_code(client_modules) + r"""
original.attrs={};send('click');original.textContent='Loading';answer();await pending;
console.log(JSON.stringify({loads,calls,messages}));""")
    assert got == {'loads': 1, 'calls': ['focus', 'click'], 'messages': []}


@pytest.mark.parametrize('modifier', ['ctrlKey', 'metaKey', 'shiftKey'])
def test_a_deferred_click_keeps_the_modifier_that_selects_instead_of_opening(client_modules, modifier):
    got = node(replay_code(client_modules) + r"""
const replayed=[];
const record=e=>replayed.push({ctrlKey:!!e.ctrlKey,metaKey:!!e.metaKey,shiftKey:!!e.shiftKey});
// HTMLElement.click() generates a click without keyboard modifiers.
current.click=()=>record({});current.dispatchEvent=record;
""" + f"send('click',{{{modifier}:true}});" + r"""
answer();await pending;console.log(JSON.stringify({loads,replayed}));""")
    assert got == {'loads': 1, 'replayed': [{key: key == modifier for key in ('ctrlKey', 'metaKey', 'shiftKey')}]}


LOAD_IO = r"""
const w={id:'w'},WHOLE_LOADING='Loading the whole rota…',wholeSeen=new Set(),panel={
  setAttribute(key){if(key==='aria-busy')this.busy=true;},removeAttribute(key){if(key==='aria-busy')this.busy=false;},busy:false},announced=[],errors=[],toasts=[];
let renderSerial=1,loadingWhole=false,workspaceId='w',view='schedule',reads=0,redraws=0,replays=0,released=0,busied=0,opened=null,chip=null;
const mySerial=renderSerial;
const document={activeElement:null,querySelector:s=>s==='#schedule-view'?panel:chip},CSS={escape:s=>s},
  say=s=>announced.push(s),busy=()=>{busied++;return ()=>{released++;};},
  openCardTarget=()=>opened,showHoverOn=()=>{shown++;},reportError=e=>errors.push(e.message),
  notify=(words,tone)=>toasts.push([words,tone]),sessionChanged=()=>false,couldntLoad=what=>'Couldn’t load '+what+'.';
let shown=0,focused=0;
async function redraw(){redraws++;renderSerial++;document.activeElement=null;}
"""


def load_code(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    return LOAD_IO + cut(source, r'\n    (const loadWhole = async .*?\n    \};)\n')


@pytest.mark.parametrize('pressed', [False, True])
@pytest.mark.parametrize('outcome', ['read', 'kept', 'failed', 'left', 'action_failed'])
def test_loading_whole_releases_busy_state_and_distinguishes_read_and_action_failures(client_modules, pressed, outcome):
    got = node(load_code(client_modules) + f"const outcome={json.dumps(outcome)};" + r"""
let cached=outcome==='kept';const held=()=>cached?{workspace:w}:null;
async function wholeRota(){reads++;if(outcome==='failed')throw new Error('read failure');if(outcome==='left')workspaceId='other';cached=true;}
const replay=()=>{replays++;if(outcome==='action_failed')throw new Error('action failure');};
""" + f"const ok=await loadWhole({'{}' if pressed else 'null'},replay);" + r"""
console.log(JSON.stringify({ok,reads,redraws,replays,released,busied,busy:panel.busy,announced,errors,toasts,seen:wholeSeen.has('w')}));""")
    successful = outcome not in ('failed', 'left')
    assert got == {'ok': successful, 'reads': 0 if outcome == 'kept' else 1,
                   'redraws': int(successful), 'replays': int(successful), 'released': int(pressed), 'busied': int(pressed),
                   'busy': False, 'announced': ['Loading the whole rota…'] + (['Ready to edit'] if successful else []),
                   'errors': ['action failure'] if outcome == 'action_failed' else [],
                   'toasts': [['Couldn’t load the whole rota.', 'error']] if outcome == 'failed' else [],
                   'seen': outcome == 'kept'}


@pytest.mark.parametrize(('mode', 'available', 'expected'), [
    ('focus', True, {'shown': 1, 'focused': 1, 'sameFocus': True}),
    ('hover', True, {'shown': 1, 'focused': 0, 'sameFocus': False}),
    ('left', True, {'shown': 0, 'focused': 0, 'sameFocus': False}),
    ('focus', False, {'shown': 0, 'focused': 0, 'sameFocus': True}),
])
def test_an_open_name_card_is_restored_on_the_redrawn_chip_without_stealing_focus(client_modules, mode, available, expected):
    got = node(load_code(client_modules) + f"const mode={json.dumps(mode)};" + r"""
let cached=false;const held=()=>cached?{workspace:w}:null,wholeRota=async()=>{cached=true;};opened={matches:()=>true,dataset:{assignment:'a'}};
if(mode==='focus')document.activeElement=opened;
""" + (r"""chip={matches:()=>mode==='hover',focus(){focused++;document.activeElement=chip;}};""" if available else '') + r"""
await loadWhole();console.log(JSON.stringify({shown,focused,sameFocus:document.activeElement===chip}));""")
    assert got == expected


@pytest.mark.parametrize('whole', [False, True])
def test_card_totals_and_rest_require_whole_data_but_other_same_day_duties_stay_visible(client_modules, whole):
    from .test_client_autoschedule_live import GLOBALS
    from .test_client_planner_facts import A, B, OTHER_CLINIC

    source = (client_modules / 'planner_week.js').read_text(encoding='utf-8')
    person_facts = cut(source, r'\n    (function personFacts\(s, pid\) \{.*?\n    \})\n')
    got = node(GLOBALS + f"const m=await import({json.dumps((client_modules / 'planner_week.js').as_uri())});" +
               f"const rota={json.dumps([A, B, OTHER_CLINIC])},hooks={{windowOnly:{json.dumps(not whole)}}};" + r"""
const {attendance,workFacts}=m,facts=new Map(),dayOf=s=>s.start.slice(0,10),weekFrom=0,line=(...v)=>v,time=()=> '08:00';
for(const s of rota)s.type_snapshot={name:'Duty'};
""" + person_facts + "\nconsole.log(JSON.stringify(personFacts(rota[1],'p1')));")
    assert got == ([['info', '16 h this week · 16 h this rota', 'Rested 24 h']] if whole else []) + [
        ['info', 'Also today', 'Duty 08:00']]


@pytest.mark.parametrize('source', ['window', 'held', 'drawn'])
def test_top_bar_actions_receive_whole_data_from_the_drawn_or_held_copy_or_one_read(client_modules, source):
    compiled = (client_modules / 'app.js').read_text(encoding='utf-8')
    with_draft = cut(compiled, r'\n    (const withDraft = .*?;)\n')
    got = node(f"const source={json.dumps(source)};" + r"""
let reads=0;const w={id:'w'},full={workspace:{revision:4},shifts:['whole']},
  whole=source==='drawn'?full:null,windowDraft=source==='window',readDraft=async()=>{reads++;return full;},
  held=()=>source==='drawn'||(!windowDraft||reads)?full:null,reportError=e=>{throw e;};
async function loadWhole(_,act){await readDraft();return act();}
""" + with_draft + r"""
const used=await withDraft(d=>({revision:d.workspace.revision,shifts:d.shifts}))();console.log(JSON.stringify({reads,used}));""")
    assert got == {'reads': int(source == 'window'), 'used': {'revision': 4, 'shifts': ['whole']}}
