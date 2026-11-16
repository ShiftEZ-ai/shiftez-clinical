"""The live preview's model stays at the state it installed (gaps, late snapshots, emptied
sessions); a viewer's Still choice is kept with their display preferences; a fill that cannot fly gives its flight slot back. Real compiled client
modules in Node, with the few browser globals the imports touch."""
import json
import subprocess


# Enough of a browser for the client modules to load (dom.js and feedback_context.js run code at import).
GLOBALS = ("const t=new EventTarget();let listeners=0;globalThis.window=globalThis;"
           "globalThis.addEventListener=(...a)=>{listeners++;t.addEventListener(...a);};"
           "globalThis.removeEventListener=t.removeEventListener.bind(t);globalThis.dispatchEvent=t.dispatchEvent.bind(t);"
           "globalThis.location={hash:''};globalThis.history={pushState(){},replaceState(){}};"
           "globalThis.matchMedia=()=>({matches:false,addEventListener(){}});"
           "const store=new Map();globalThis.sessionStorage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v)};"
           "const onPage=[];globalThis.document={cookie:'',addEventListener(){},querySelectorAll:s=>s==='[data-live-pause]'?onPage:[]};")


def node(client_modules, module, body):
    script = f"{GLOBALS}const m=await import({json.dumps((client_modules/module).as_uri())});{body}"
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


# A model on a draft where session s1 has a and s2 has b; `ev(seq, adds, removes)` is a detail-only live event.
MODEL = ("const ev=(seq,adds=[],removes=[],truncated=false)=>({seq,truncated,adds:adds.map(([shift,person])=>({shift,person})),"
         "removes:removes.map(([shift,person])=>({shift,person}))});"
         "const snap=(seq,pairs)=>({seq,pairs:pairs.map(([shift,person])=>({shift,person}))});"
         "const x=m.liveModel(['s1|a','s2|b'],s=>s!=='ctx');const state=()=>{x.take();return [...x.target].sort();};")


def test_a_snapshot_after_missed_events_empties_the_session_the_solver_emptied(client_modules):
    out = node(client_modules, 'autoschedule_motion.js', MODEL + (
        # Events 1-2 dropped from the buffer, 3 truncated: lost until a snapshot; it has nobody on s1 any more.
        "x.observe([ev(3,[],[],true)]);const lost=x.lost;"
        "x.install(snap(3,[['s2','b'],['s3','a'],['ctx','z']]));"
        "console.log(JSON.stringify({lost,after:x.lost,state:state()}));"))
    assert out == {'lost': True, 'after': False, 'state': ['s2|b', 's3|a']}  # s1|a gone; context pairs stay out


def test_a_late_snapshot_replays_the_events_seen_after_it(client_modules):
    out = node(client_modules, 'autoschedule_motion.js', MODEL + (
        # Event 1 missed; 2 and 4 seen (3 missed too); the snapshot read for the gap answers with seq 3, after 4 arrived.
        "x.observe([ev(2,[['s4','c']]),ev(4,[['s5','d']],[['s2','b']])]);const lost=x.lost;"
        "x.install(snap(3,[['s1','a'],['s2','b'],['s4','c']]));const replayed=state();"
        "x.observe([ev(4,[['s5','d']],[['s2','b']]),ev(5,[['s6','e']])]);"
        "console.log(JSON.stringify({lost,replayed,after:x.lost,final:state(),seen:x.seen}));"))
    assert out['lost'] is True
    assert out['replayed'] == ['s1|a', 's4|c', 's5|d']  # 4 replayed on the installed 3, not ignored
    assert out['after'] is False and out['final'] == ['s1|a', 's4|c', 's5|d', 's6|e'] and out['seen'] == 5


def test_a_snapshot_older_than_the_installed_state_changes_nothing_and_a_gap_waits_for_a_newer_one(client_modules):
    out = node(client_modules, 'autoschedule_motion.js', MODEL + (
        "x.observe([ev(1,[['s3','c']]),ev(2,[['s4','d']])]);x.install(snap(1,[['s1','a']]));const kept=state();"
        # 3 is missing and 4 is seen: a snapshot at 2 cannot install past the gap, so the model stays lost.
        "x.observe([ev(4,[['s5','e']])]);x.install(snap(2,[['s1','a'],['s3','c'],['s4','d']]));const waiting=x.lost;"
        "x.install(snap(4,[['s1','a'],['s5','e']]));"
        "console.log(JSON.stringify({kept,waiting,after:x.lost,final:state()}));"))
    assert out['kept'] == ['s1|a', 's2|b', 's3|c', 's4|d']
    assert out['waiting'] is True and out['after'] is False and out['final'] == ['s1|a', 's5|e']


def test_in_order_events_apply_without_a_snapshot(client_modules):
    out = node(client_modules, 'autoschedule_motion.js', MODEL + (
        "x.observe([ev(2,[['s3','c']]),ev(1,[['s1','c']],[['s1','a']])]);x.observe([ev(1,[['s9','z']])]);"
        "console.log(JSON.stringify({lost:x.lost,state:state()}));"))
    assert out == {'lost': False, 'state': ['s1|c', 's2|b', 's3|c']}  # a repeated event is not applied twice


def test_catch_up_preserves_cancelled_and_closed_duties_while_active_sessions_can_empty(client_modules):
    logic = json.dumps((client_modules/'planner_logic.js').as_uri())
    out = node(client_modules, 'autoschedule_motion.js', (
        f"const logic=await import({logic});"
        "const shifts=new Map([['cancelled',{cancelled:true}],['closed',{closed:true}],['active',{}]]);"
        "const x=m.liveModel(['cancelled|a','closed|b','active|c'],s=>shifts.has(s)&&!logic.isClosed(shifts.get(s)));"
        "x.observe([{seq:3,truncated:true,adds:[],removes:[]}]);x.install({seq:3,pairs:[]});x.take();"
        "const caught=[...x.target].sort();"
        "x.observe([{seq:4,truncated:false,adds:[{shift:'active',person:'d'},{shift:'closed',person:'z'}],"
        "removes:[{shift:'cancelled',person:'a'},{shift:'closed',person:'b'}]}]);x.take();"
        "console.log(JSON.stringify({caught,final:[...x.target].sort(),lost:x.lost}));"))
    assert out == {'caught': ['cancelled|a', 'closed|b'],
                   'final': ['active|d', 'cancelled|a', 'closed|b'], 'lost': False}


def test_configuration_preview_builds_rows_the_people_view_can_discover(client_modules):
    options = json.dumps((client_modules/'generation_options.js').as_uri())
    out = node(client_modules, 'autoschedule_live.js', (
        "const made=[];globalThis.Node=class {constructor(){this.attrs={};this.style={setProperty(){}};made.push(this);}"
        "setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];}"
        "append(){}replaceChildren(){}querySelectorAll(){return [];}querySelector(){return null;}};"
        "document.createElement=()=>new Node();document.createTextNode=()=>new Node();"
        "globalThis.setTimeout=()=>0;globalThis.clearTimeout=()=>{};"
        "const host={replaceChildren(){}};"
        f"const settings=(await import({options})).animationSettings(undefined);m.preview(host,()=>settings);"
        "const rows=made.filter(n=>n.className==='pg-row person-group');"
        "console.log(JSON.stringify(rows.map(n=>({person:n.attrs['data-row-person'],legacy:'data-person' in n.attrs}))));"))
    assert out == [{'person': person, 'legacy': False} for person in 'abcde']


def test_still_motion_is_the_viewers_own_choice_and_sign_out_forgets_it(client_modules):
    """REQ-MOTION: no Pause buttons; a personal Still choice (account menu) kept with the viewer's display preferences."""
    out = node(client_modules, 'theme.js', (
        "globalThis.localStorage=globalThis.sessionStorage;globalThis.localStorage.removeItem=k=>store.delete(k);"
        "document.documentElement={removeAttribute(){},setAttribute(){}};globalThis.getComputedStyle=()=>({getPropertyValue:()=>''});"
        "const before=m.motionStill();m.setMotionStill(true);const kept=JSON.parse(store.get('rota.prefs')).still;"
        "m.setMotionStill(false);const off=JSON.parse(store.get('rota.prefs')).still??null;m.setMotionStill(true);m.clearPrefs();"
        "console.log(JSON.stringify({before,kept,off,cleared:m.motionStill()}));"))
    assert out == {'before': False, 'kept': True, 'off': None, 'cleared': False}


def test_fills_for_filtered_out_people_rows_give_their_flight_slots_back(client_modules):
    options = json.dumps((client_modules/'generation_options.js').as_uri())
    out = node(client_modules, 'autoschedule_live.js', (
        "const grid={querySelectorAll:()=>[],querySelector:()=>null};"  # People view with every row filtered out
        "const ctx={shift:()=>undefined,day:()=>'1',duty:()=>({code:'C',hue:'teal',name:'Clinic',time:''}),name:p=>p,label:p=>p};"
        "const view=m.peopleView(grid,ctx),f={add(){}},settings=(await import(" + options + ")).animationSettings(undefined);"
        "m.play(view,Array.from({length:40},(_,i)=>({t:'fill',s:'s'+i,p:'p'+i})),settings,f);"
        "console.log(JSON.stringify({flying:m.inFlight(),cap:settings.max_concurrent_flights}));"))
    assert out == {'flying': 0, 'cap': 16}


def test_a_step_awaited_for_a_view_that_has_gone_never_resumes_it(client_modules):
    """Rota A's delayed job read or lazy module load resolves after B is shown; nothing
    after the await runs for A (no liveRun(A) disposing B's run, no finish card with A's Apply)."""
    out = node(client_modules, 'generation_options.js', (
        "let resolveA,resolveB,shown='A';const ran=[];"
        "const forA=m.whileFresh(()=>shown==='A'),forB=m.whileFresh(()=>shown==='B');"
        "forA(new Promise(r=>{resolveA=r;})).then(v=>ran.push('A:'+v));"
        "shown='B';forB(new Promise(r=>{resolveB=r;})).then(v=>ran.push('B:'+v));"
        "resolveA('job');resolveB('job');await new Promise(r=>setTimeout(r,10));"
        # A failed read still reaches its handler (the panel's own checks decide what to say).
        "let caught='';await forA(Promise.reject(new Error('down'))).catch(e=>{caught=e.message;});"
        "console.log(JSON.stringify({ran,caught}));"))
    assert out == {'ran': ['B:job'], 'caught': 'down'}


def test_a_chip_the_preview_changes_is_a_copy_without_its_saved_actions(client_modules):
    """A relabelled or swapped saved chip is replaced by a copy with no listeners (click,
    menu, hover card, drag), aria-disabled and without its rule mark; a chip already copied is kept."""
    out = node(client_modules, 'autoschedule_live.js', (
        "const node=(attrs,listeners)=>{const n={attrs,listeners,dataset:{},classes:new Set(['pchip','draggable']),"
        "setAttribute(k,v){n.attrs[k]=v;},removeAttribute(k){delete n.attrs[k];},classList:{remove:c=>n.classes.delete(c)},"
        "cloneNode(){const y=node({...n.attrs},[]);Object.assign(y.dataset,n.dataset);return y;},replaceWith(y){n.replaced=y;}};return n;};"
        "const x=node({draggable:'true',title:'Drag'},['click','contextmenu','dragstart']);Object.assign(x.dataset,{rk:'{}',rkCard:'1'});"
        "const y=m.frozen(x);const again=m.frozen(y);"
        "console.log(JSON.stringify({replaced:x.replaced===y,listeners:y.listeners.length,disabled:y.attrs['aria-disabled'],"
        "draggable:'draggable' in y.attrs,title:'title' in y.attrs,rk:'rk' in y.dataset,card:'rkCard' in y.dataset,"
        "marked:'livePreview' in y.dataset,kept:again===y&&!y.replaced,none:m.frozen(null)}));"))
    assert out == {'replaced': True, 'listeners': 0, 'disabled': 'true', 'draggable': False, 'title': False,
                   'rk': False, 'card': False, 'marked': True, 'kept': True, 'none': None}


def test_idle_panel_redraws_leave_exactly_one_live_pulse_listener(client_modules):
    """Each redrawn scheduling panel replaces the previous panel's live-pulse listener."""
    out = node(client_modules, 'generation_options.js', (
        "const own=m.ownedListener('live-pulse');let woken=[];"
        "for(let i=0;i<20;i++)own(()=>woken.push(i));"  # 20 idle Week/People redraws, no pulse in between
        "dispatchEvent(new Event('live-pulse'));console.log(JSON.stringify(woken));"))
    assert out == [19]


def test_a_delayed_finish_survives_a_week_people_redraw(client_modules):
    """The finishing run's id stays until its finish lands, so the panel drawn while the
    first panel's read was pending (Week → People) finishes it; the first panel's continuation never resumes."""
    out = node(client_modules, 'generation_options.js', (
        "const state={live:'job1'};let view='week';const finished=[];let release;"
        "const panel=name=>{const ours=m.whileFresh(()=>view===name);"
        "return m.liveFinisher(state,id=>ours(new Promise(r=>{release=r;})).then(()=>finished.push(name+':'+id)));};"
        "const week=panel('week');week();week();"  # a second read while pending starts no second finish
        "const held=release;view='people';const people=panel('people');people();const during=state.live;"
        "held();release();await new Promise(r=>setTimeout(r,10));people();"
        "console.log(JSON.stringify({during,finished,live:state.live}));"))
    assert out == {'during': 'job1', 'finished': ['people:job1'], 'live': ''}


# A Week cell in a minimal DOM: enough of Element for weekView with instant changes (Resume reconciling).
FAKE_DOM = (
    r"class N{constructor(t){this.tag=t;this.kids=[];this.parentElement=null;this.attrs={};this.dataset={};this._t='';}"
    r"get className(){return this.attrs.class||'';}set className(v){this.attrs.class=v;}"
    r"get classList(){const s=()=>this.className.split(/\s+/).filter(Boolean);return{contains:c=>s().includes(c),"
    r"add:c=>{if(!s().includes(c))this.className=[...s(),c].join(' ');},remove:c=>{this.className=s().filter(x=>x!==c).join(' ');}};}"
    r"setAttribute(k,v){if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-(.)/g,(_,c)=>c.toUpperCase())]=v;else this.attrs[k]=v;}"
    r"getAttribute(k){return k.startsWith('data-')?this.dataset[k.slice(5)]??null:this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];}"
    r"get textContent(){return this._t+this.kids.map(k=>k.textContent).join('');}set textContent(v){this._t=String(v);this.kids=[];}"
    r"get firstElementChild(){return this.kids[0]||null;}get tagName(){return this.tag.toUpperCase();}"
    r"replaceChildren(...ns){this.textContent='';this.append(...ns);}"
    r"append(...ns){for(const n of ns){if(typeof n==='string'){this._t+=n;continue;}n.remove();n.parentElement=this;this.kids.push(n);}}"
    r"remove(){const p=this.parentElement;if(p){p.kids.splice(p.kids.indexOf(this),1);this.parentElement=null;}}"
    r"before(n){const p=this.parentElement;n.remove();n.parentElement=p;p.kids.splice(p.kids.indexOf(this),0,n);}"
    r"replaceWith(...ns){for(const n of ns)this.before(n);this.remove();}get children(){return[...this.kids];}getAnimations(){return[];}animate(){return{};}"
    r"closest(s){let n=this;while(n&&!n.matches(s))n=n.parentElement;return n;}"
    r"matches(s){const cls=[...s.replace(/:not\([^)]*\)|\[[^\]]*\]/g,'').matchAll(/\.([\w-]+)/g)].map(x=>x[1]);"
    r"const nots=[...s.matchAll(/:not\(\.([\w-]+)\)/g)].map(x=>x[1]),at=[...s.matchAll(/\[data-([\w-]+)=\x22([^\x22]*)\x22\]/g)];"
    r"return cls.every(c=>this.classList.contains(c))&&!nots.some(c=>this.classList.contains(c))&&at.every(([,k,v])=>this.dataset[k]===v);}"
    r"all(){return this.kids.flatMap(k=>[k,...k.all()]);}querySelector(s){return this.all().find(n=>n.matches(s))||null;}"
    r"querySelectorAll(s){return this.all().filter(n=>n.matches(s));}}"
    r"globalThis.Node=N;globalThis.CSS={escape:s=>s};document.createElement=t=>new N(t);document.createTextNode=t=>t;"
)

# The fake DOM plus clicks: listeners kept per node, click() runs them once each.
CLICKS = (r"N.prototype.addEventListener=function(k,f){(this.on??={})[k]=[...(this.on[k]||[]),f];};"
          r"N.prototype.click=function(){for(const f of this.on?.click||[])f({target:this,stopPropagation(){}});};")


def test_a_week_session_emptied_on_resume_keeps_its_cell_for_the_next_fill(client_modules):
    """With Open places hidden, an instant leave (or a shift away) removes a session's
    last chip; its cell is taken before it goes, so the gap and a later fill land there instead of being dropped."""
    options = json.dumps((client_modules/'generation_options.js').as_uri())
    out = node(client_modules, 'autoschedule_live.js', FAKE_DOM + (
        "const ctx={shift:()=>undefined,day:()=>'1',duty:()=>({code:'C',hue:'teal',name:'Clinic',time:''}),name:p=>p,label:p=>p};"
        "const settings=(await import(" + options + ")).animationSettings(undefined),f={add(o){o.first?.();o.write();o.last?.();o.play?.();}};"
        "const grid=document.createElement('div'),cells={};"
        "for(const s of ['s1','s2']){const cell=cells[s]=document.createElement('div');cell.className='wk-cell';grid.append(cell);"
        "const chip=document.createElement('span');chip.className='pchip duty-teal';chip.dataset.shift=s;chip.dataset.person=s==='s1'?'a':'c';chip.dataset.livePreview='';cell.append(chip);}"
        "const view=m.weekView(grid,ctx),seen=s=>cells[s].kids.map(k=>k.dataset.person||k.textContent);"
        "m.play(view,[{t:'leave',s:'s1',p:'a'}],settings,f,true);const emptied=seen('s1');"
        "m.play(view,[{t:'fill',s:'s1',p:'b'}],settings,f,true);const refilled=seen('s1');"
        "m.play(view,[{t:'shift',from:'s2',to:'s1',p:'c'},{t:'fill',s:'s2',p:'d'}],settings,f,true);"
        "console.log(JSON.stringify({emptied,refilled,s1:seen('s1'),s2:seen('s2')}));"))
    assert out == {'emptied': ['+ 1 open place'], 'refilled': ['b'], 's1': ['b', 'c'], 's2': ['d']}


def test_a_live_change_never_puts_anyone_inside_or_leaves_them_in_a_supervision_team_box(client_modules):
    """A Week team box (team_view.ts, one row per member) holds a supervisor's and their trainees' own chips. A fill lands in the
    session's cell, not inside the team; a change to a member opens the team into plain chips (live events carry no
    supervision), and the redraw at Done groups again."""
    options = json.dumps((client_modules/'generation_options.js').as_uri())
    out = node(client_modules, 'autoschedule_live.js', FAKE_DOM + (
        "const ctx={shift:()=>undefined,day:()=>'1',duty:()=>({code:'C',hue:'teal',name:'Clinic',time:''}),name:p=>p,label:p=>p};"
        "const settings=(await import(" + options + ")).animationSettings(undefined),f={add(o){o.first?.();o.write();o.last?.();o.play?.();}};"
        "const mk=(cls,data={})=>{const n=document.createElement('span');n.className=cls;Object.assign(n.dataset,data);return n;};"
        "const grid=document.createElement('div'),cell=mk('wk-cell'),team=mk('team-box card');grid.append(cell);cell.append(team);"
        "for(const p of ['a','b']){const row=mk('tm-row');team.append(row);row.append(mk('pchip duty-teal',{shift:'s1',person:p,livePreview:''}));}"
        "const view=m.weekView(grid,ctx),seen=n=>n.kids.map(k=>k.classList.contains('team-box')?k.querySelectorAll('.pchip').map(x=>x.dataset.person):k.dataset.person||k.textContent);"
        "m.play(view,[{t:'fill',s:'s1',p:'c'}],settings,f,true);const filled=seen(cell);"
        "m.play(view,[{t:'person',s:'s1',from:'b',to:'d'}],settings,f,true);"
        "console.log(JSON.stringify({filled,changed:seen(cell)}));"))
    assert out == {'filled': [['a', 'b'], 'c'], 'changed': ['a', 'd', 'c']}


# A fake clock and document listeners for lingerDone (D-123): `later(ms)` runs the timers due by then.
CLOCK = ("let now=0,seq=0;const timers=new Map();globalThis.setTimeout=(f,ms)=>{timers.set(++seq,{f,at:now+ms});return seq;};"
         "globalThis.clearTimeout=id=>timers.delete(id);"
         "const later=ms=>{now+=ms;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.f();}};"
         "const on={};document.addEventListener=(k,f)=>{(on[k]??=new Set()).add(f);};"
         "document.removeEventListener=(k,f)=>{on[k]?.delete(f);};"
         "const fire=(k,inPanel)=>{for(const f of [...(on[k]||[])])f({target:{closest:s=>inPanel&&s.includes('#auto-panel')?{}:null}});};"
         "const live=()=>timers.size+Object.values(on).reduce((n,s)=>n+s.size,0);")


def test_done_leaves_the_footer_after_its_moment_or_at_the_first_panel_interaction(client_modules):
    """D-123: "5/5 Done" is shown once, DONE_STRIP_MS or until the person works in the Auto-schedule panel, then the
    footer's own status returns; a run disposed meanwhile leaves no timer or listener behind."""
    out = node(client_modules, 'autoschedule_live.js', CLOCK + (
        "let gone=0;m.lingerDone(()=>gone++);"
        "later(m.DONE_STRIP_MS-1);const before=gone;fire('pointerdown',false);fire('focusin',false);const outside=gone;"
        "later(1);const timed=gone,afterTime=live();later(m.DONE_STRIP_MS);"
        "let pressed=0;m.lingerDone(()=>pressed++);fire('focusin',true);const atOnce=pressed,afterPress=live();"
        "fire('keydown',true);later(m.DONE_STRIP_MS);"
        "let disposed=0;const stop=m.lingerDone(()=>disposed++);stop();const afterStop=live();later(m.DONE_STRIP_MS);"
        "console.log(JSON.stringify({ms:m.DONE_STRIP_MS,before,outside,timed,afterTime,end:gone,atOnce,afterPress,"
        "pressed,disposed,afterStop}));"))
    assert out == {'ms': 3000, 'before': 0, 'outside': 0, 'timed': 1, 'afterTime': 0, 'end': 1, 'atOnce': 1,
                   'afterPress': 0, 'pressed': 1, 'disposed': 0, 'afterStop': 0}
