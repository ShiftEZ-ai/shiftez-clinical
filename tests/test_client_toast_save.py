"""maintenance: a message whose action saves (the toast's Undo) stays open, saying what the footer's save state says (api.ts
savingWords: "Saving…", then "Checking whether it saved…"), until that save settles. It is never closed by its own
timeout first; the outcome is then said by the message that follows. Real compiled dom.ts and api.ts in Node, a small
fake DOM, and timers on a virtual clock (no real waits)."""
import json
from pathlib import Path
import subprocess

# Just enough DOM for notify(): a tree, classes, attributes, text, events and simple selectors (tag, #id, .class,
# :not(.class), [attr], ':scope>' children, comma lists; a descendant selector matches on its last part).
FAKE_DOM = r"""
class N extends EventTarget{constructor(tag='#text',text=''){super();this.tagName=tag.toUpperCase();this.kids=[];this.parentElement=null;
  this.attrs={};this.dataset={};this._t=text;this.hidden=false;this.disabled=false;this.style={setProperty(){},removeProperty(){}};
  const self=this;this.classList={add:(...c)=>{for(const x of c)if(!self._cls().includes(x))self.attrs.class=[...self._cls(),x].join(' ');},
    remove:(...c)=>{self.attrs.class=self._cls().filter(x=>!c.includes(x)).join(' ');},contains:c=>self._cls().includes(c),
    toggle:(c,on)=>{(on??!self.classList.contains(c))?self.classList.add(c):self.classList.remove(c);}};}
  _cls(){return (this.attrs.class||'').split(' ').filter(Boolean);}
  get className(){return this.attrs.class||'';}set className(v){this.attrs.class=String(v);}
  get id(){return this.attrs.id||'';}set id(v){this.attrs.id=String(v);}
  setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}hasAttribute(k){return k in this.attrs;}removeAttribute(k){delete this.attrs[k];}
  get textContent(){return this.tagName==='#TEXT'?this._t:this.kids.map(k=>k.textContent).join('');}
  set textContent(v){this.kids.forEach(k=>k.parentElement=null);this.kids=[];if(v!=='')this.append(String(v));}
  _adopt(n){if(typeof n==='string')n=new N('#text',n);n.remove();n.parentElement=this;return n;}
  append(...ns){for(const n of ns)this.kids.push(this._adopt(n));}appendChild(n){this.append(n);return n;}
  prepend(...ns){this.kids.unshift(...ns.map(n=>this._adopt(n)));}
  before(...ns){const p=this.parentElement;for(const n of ns){const a=p._adopt(n);p.kids.splice(p.kids.indexOf(this),0,a);}}
  remove(){const p=this.parentElement;if(p){p.kids.splice(p.kids.indexOf(this),1);this.parentElement=null;}}
  replaceChildren(...ns){this.textContent='';this.append(...ns);}
  get children(){return this.kids.filter(k=>k.tagName!=='#TEXT');}get childNodes(){return [...this.kids];}
  get firstChild(){return this.kids[0]||null;}get lastElementChild(){return this.children.at(-1)||null;}get childElementCount(){return this.children.length;}
  get isConnected(){let n=this;while(n.parentElement)n=n.parentElement;return n===document.body;}
  contains(n){while(n){if(n===this)return true;n=n.parentElement;}return false;}
  closest(s){let n=this;while(n&&!n.matches(s))n=n.parentElement;return n;}
  matches(s){return s.split(',').some(part=>{const last=part.trim().replace(/^:scope>/,'').split(/[\s>]+/).at(-1);
    if(/:hover|:modal/.test(last))return false;
    const tag=(last.match(/^[a-z]+/)||[])[0],bare=last.replace(/:not\([^)]*\)/g,''),nots=[...last.matchAll(/:not\(([^)]*)\)/g)].map(x=>x[1]);
    const ok=(sel,n)=>[...sel.matchAll(/\.([\w-]+)/g)].every(x=>n.classList.contains(x[1]))&&[...sel.matchAll(/#([\w-]+)/g)].every(x=>n.id===x[1])
      &&[...sel.matchAll(/\[([\w-]+)\]/g)].every(x=>x[1]==='hidden'?n.hidden:n.hasAttribute(x[1]));
    return this.tagName!=='#TEXT'&&(!tag||this.tagName===tag.toUpperCase())&&ok(bare,this)&&!nots.some(x=>x&&ok(x,this));});}
  all(){return this.children.flatMap(k=>[k,...k.all()]);}
  querySelectorAll(s){const pool=s.trim().startsWith(':scope>')?this.children:this.all();return pool.filter(n=>n.matches(s));}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  getBoundingClientRect(){return {top:0,left:0,height:0,width:0};}focus(){document.activeElement=this;}
  click(){this.dispatchEvent(new Event('click'));}}
globalThis.Node=N;globalThis.HTMLElement=N;globalThis.location={hash:"",pathname:"/",search:""};globalThis.history={pushState(){},replaceState(){}};globalThis.innerHeight=900;globalThis.innerWidth=1440;
globalThis.MutationObserver=class{observe(){}disconnect(){}};globalThis.requestAnimationFrame=f=>setTimeout(f,0);
globalThis.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
const body=new N('body');
globalThis.document={cookie:'rota_csrf=fictional',body,activeElement:body,createElement:t=>new N(t),createElementNS:(_,t)=>new N(t),
  createTextNode:t=>new N('#text',String(t)),querySelector:s=>body.querySelector(s),querySelectorAll:s=>body.querySelectorAll(s),
  addEventListener(){},removeEventListener(){}};
"""

# Timers run on a virtual clock (Date.now too): the earliest fires 20 ms of real time after the last one; `paused` stops it.
CLOCK = r"""
const realSetTimeout=setTimeout;let clock=1e6,seq=0,pumping=false,paused=false;const timers=new Map();Date.now=()=>clock;
const pump=()=>{pumping=false;if(paused)return;let next=null;for(const [id,t] of timers)if(!next||t.at<next[1].at)next=[id,t];if(!next)return;
  timers.delete(next[0]);clock=Math.max(clock,next[1].at);next[1].run();if(timers.size&&!pumping){pumping=true;realSetTimeout(pump,20);}};
globalThis.setTimeout=(fn,ms=0,...args)=>{const id=++seq;timers.set(id,{at:clock+ms,run:()=>fn(...args)});if(!pumping){pumping=true;realSetTimeout(pump,20);}return id;};
globalThis.clearTimeout=id=>{timers.delete(id);};globalThis.setInterval=()=>0;globalThis.clearInterval=()=>{};
const until=async(what,f)=>{for(let i=0;i<1000;i++){if(f())return;await new Promise(r=>realSetTimeout(r,5));}throw new Error('never: '+what);};
"""


def test_a_slow_undo_keeps_its_toast_saying_the_footers_words_until_it_settles(client_modules):
    script = FAKE_DOM + CLOCK + f"""
globalThis.window=globalThis;const t=new EventTarget();
window.addEventListener=t.addEventListener.bind(t);window.removeEventListener=t.removeEventListener.bind(t);window.dispatchEvent=t.dispatchEvent.bind(t);
const sent=[];let release=null;
globalThis.fetch=async(path,options)=>{{const c=JSON.parse(options.body);sent.push(c.kind);
  const reply=r=>new Response(JSON.stringify(r),{{headers:{{'Content-Type':'application/json'}}}});
  if(c.kind==='shift.update')return reply({{workspace:{{id:'ws-1',revision:5,content_revision:2}}}});
  // The Undo: its first try never answers (the client gives up at its timeout), its replay waits for release().
  if(sent.filter(k=>k==='workspace.undo').length===1)return new Promise((_,fail)=>options.signal.addEventListener('abort',()=>fail(new DOMException('Aborted','AbortError'))));
  return new Promise(done=>{{release=()=>done(reply({{workspace:{{id:'ws-1',revision:7,content_revision:1}}}}));}});}};
const api=await import({json.dumps((client_modules / 'api.js').as_uri())});
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
await api.command('shift.update',{{assignments:['person-a']}},'shift-1',4);
dom.notify('Session saved','success');
const toasts=()=>body.querySelectorAll('.toast'),words=n=>n.querySelector(':scope>span:not(.spinner)').textContent;
const undo=toasts()[0].querySelector('.toast-action');const offered=undo.textContent;undo.click();
await until('saving',()=>toasts()[0]?.classList.contains('working'));
const saving={{toast:words(toasts()[0]),footer:api.savingWords()}};
await until('checking',()=>api.confirmingNow()&&release);
const start=clock;setTimeout(()=>{{paused=true;}},15000);await until('time passes',()=>paused);  // past a message's own 10 s, within the replay's 20 s
const checking={{toast:words(toasts()[0]),footer:api.savingWords(),count:toasts().length,action:!!toasts()[0].querySelector('.toast-action')}};
release();paused=false;setTimeout(()=>{{}},0);
await until('outcome',()=>toasts().length===1&&!toasts()[0].classList.contains('working'));
console.log(JSON.stringify({{offered,saving,checking,after:words(toasts()[0]),sent}}));
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['offered'] == 'Undo'
    assert got['saving'] == {'toast': 'Saving…', 'footer': 'Saving…'}
    # Still open long after a message's own lifetime, in the footer's words, with no second press to make.
    assert got['checking'] == {'toast': 'Checking whether it saved…', 'footer': 'Checking whether it saved…', 'count': 1, 'action': False}
    assert got['after'].startswith('Undone.')  # the outcome's own message, once the Undo settled
    assert got['sent'] == ['shift.update', 'workspace.undo', 'workspace.undo']  # one Undo: its replay, never a second one


def test_a_check_result_landing_after_the_person_moved_on_is_no_toast(client_modules):
    """maintenance: a session panel's result ("… 63 must fix left in the rota") lands with the refresh after the save's check,
    46 s later; the panel had closed. It is no late toast: it is said in the polite live region, and the footer's rota
    health (redrawn by that refresh) shows the counts. Only the person's own save message (with Undo) was a toast."""
    script = FAKE_DOM + CLOCK + f"""
globalThis.window=globalThis;const t=new EventTarget();
window.addEventListener=t.addEventListener.bind(t);window.removeEventListener=t.removeEventListener.bind(t);window.dispatchEvent=t.dispatchEvent.bind(t);
const loading=await import({json.dumps((client_modules / 'loading.js').as_uri())});
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
dom.notify('Session saved.','success');const saved=body.querySelectorAll('.toast').length;
const start=clock;setTimeout(()=>{{}},46000);await until('46 s later',()=>clock-start>=46000);
loading.say('Dr. Example added to the draft. 63 must fix left in the rota.');  // planner_week's closed-panel result
const live=body.querySelector('#loading-live');
console.log(JSON.stringify({{saved,toasts:body.querySelectorAll('.toast').map(n=>n.textContent),live:live?.textContent,polite:live?.getAttribute('aria-live'),role:live?.getAttribute('role')}}));
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['saved'] == 1 and all('must fix left' not in t for t in got['toasts'])
    assert got['live'] == 'Dr. Example added to the draft. 63 must fix left in the rota.'
    assert (got['polite'], got['role']) == ('polite', 'status')
    # The one place a closed panel's result went to a toast now says it politely (planner_week.ts drawPanels).
    source = (Path(__file__).resolve().parents[1] / 'apps/web/src/planner_week.ts').read_text(encoding='utf-8')
    assert 'if(panel.flash)sayPolitely(panel.flash)' in source and 'notify(panel.flash)' not in source
