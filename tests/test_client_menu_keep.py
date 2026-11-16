"""D-057 (a redraw never loses what the person has open), for action menus: a ⋯ or right-click menu opened with `keep`
(dom.ts menu()) stays open when a redraw replaces its button, on the button now matching, filled again by its host
(menuSource) for the record as it is now, the focus on the same item; an item chosen runs for the current record, never
the one the menu was opened on; and it closes when its record is gone. Real compiled dom.ts in Node with a small fake DOM."""
import json
import subprocess

from .test_client_toast_save import FAKE_DOM

# The fake DOM's selectors know classes, ids and bare attributes: menus also ask for [attr=value], [attr^=value] and
# :not(:disabled).
SELECTORS = r"""
const base=N.prototype.matches;
N.prototype.matches=function(s){return s.split(',').some(part=>{const last=part.trim().split(/[\s>]+/).at(-1);
  const wants=[...last.matchAll(/\[([\w-]+)(\^?=)"?([^"\]]*)"?\]/g)];
  if(!wants.every(([,k,op,v])=>{const have=this.getAttribute(k);return have!=null&&(op==='='?have===v:have.startsWith(v));}))return false;
  if(last.includes(':not(:disabled)')&&(this.disabled||this.hasAttribute('disabled')))return false;
  return base.call(this,last.replace(/\[[\w-]+\^?=[^\]]*\]/g,'').replace(':not(:disabled)',''));});};
globalThis.window=globalThis;const t=new EventTarget();
window.addEventListener=t.addEventListener.bind(t);window.removeEventListener=t.removeEventListener.bind(t);window.dispatchEvent=t.dispatchEvent.bind(t);
globalThis.cancelAnimationFrame=clearTimeout;
N.prototype.getClientRects=function(){return this.isConnected?[{}]:[];};
document.documentElement={clientWidth:1440};
globalThis.getComputedStyle=()=>({overflowX:'visible'});globalThis.ResizeObserver=class{observe(){}unobserve(){}disconnect(){}};
const frames=async()=>{for(let i=0;i<4;i++)await new Promise(r=>setTimeout(r,5));};
"""


def test_a_kept_menu_follows_its_record_across_a_redraw_and_acts_on_it_as_it_is_now(client_modules):
    script = FAKE_DOM + SELECTORS + f"""
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
const host=document.createElement('div');body.append(host);
const rows={{r1:{{name:'First'}}}},ran=[];
// The host's items for a button's record as it is now (what a list's row actions or a session's chip menu are).
const actions=b=>{{const r=rows[b.getAttribute('data-row')];return r?[
  {{label:'Rename',icon:'edit',run:()=>ran.push('rename '+r.name)}},{{label:'Remove',icon:'trash',run:()=>ran.push('remove '+r.name)}}]:[];}};
dom.menuSource(host,actions);
const draw=()=>{{const b=document.createElement('button');b.id='more-r1';b.setAttribute('data-row','r1');host.replaceChildren(b);return b;}};
const open=b=>dom.menu(b,'Row actions',actions(b),{{keep:'#more-r1'}});
const menu=()=>body.querySelector('[role=menu]'),items=()=>menu()?.querySelectorAll('[role=menuitem]')||[];
const out={{}};

// 1. A redraw replaces the button; the record changed meanwhile.
let b=draw();open(b);items()[1].focus();const panel=menu();
rows.r1={{name:'Second'}};let next=draw();await frames();
out.kept={{open:menu()===panel,expanded:next.getAttribute('aria-expanded'),focus:document.activeElement.textContent,inMenu:panel.contains(document.activeElement)}};
items()[0].click();await frames();
out.acted={{ran:[...ran],closed:!menu(),expanded:next.getAttribute('aria-expanded')}};

// 2. The page's own redraw hands it over at once (closeOrphanPopover), before any frame.
ran.length=0;b=next;open(b);rows.r1={{name:'Third'}};next=draw();dom.closeOrphanPopover();
out.atOnce={{open:!!menu(),expanded:next.getAttribute('aria-expanded')}};

// 3. An item pressed in the instant between the redraw and the hand-over: the stale item never runs.
b=next;const stale=items()[1];rows.r1={{name:'Fourth'}};next=draw();stale.click();await frames();
out.stale={{ran:[...ran],closed:!menu()}};

// 4. Closing it (as Escape does) names the button now on the page closed and returns the focus to it.
ran.length=0;b=next;open(b);next=draw();await frames();
dom.closePopover();out.escape={{closed:!menu(),expanded:next.getAttribute('aria-expanded'),focus:document.activeElement===next}};

// 5. The record is gone after the redraw: the menu closes, nothing runs.
b=next;open(b);const last=items()[0];delete rows.r1;host.replaceChildren();last.click();await frames();
out.gone={{closed:!menu(),ran:[...ran]}};

// 6. The button is back but the record has no actions left: closed too.
rows.r1={{name:'Fifth'}};b=draw();open(b);delete rows.r1;draw();await frames();
out.noActions={{closed:!menu()}};

// 7. A menu without `keep` still closes when its button is redrawn away (SW3-D-02).
rows.r1={{name:'Sixth'}};b=draw();dom.menu(b,'Row actions',actions(b));draw();await frames();
out.unkept={{closed:!menu()}};
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    # Still the same open panel, on the new button, the focus on the item it was on.
    assert got['kept'] == {'open': True, 'expanded': 'true', 'focus': 'Remove', 'inMenu': True}
    # The item runs for the record as it is now (not the one the menu was opened on), then the menu closes.
    assert got['acted'] == {'ran': ['rename Second'], 'closed': True, 'expanded': 'false'}
    assert got['atOnce'] == {'open': True, 'expanded': 'true'}
    assert got['stale'] == {'ran': ['remove Fourth'], 'closed': True}
    assert got['escape'] == {'closed': True, 'expanded': 'false', 'focus': True}
    assert got['gone'] == {'closed': True, 'ran': []}
    assert got['noActions'] == {'closed': True}
    assert got['unkept'] == {'closed': True}


# Kept selectors name a button inside its own table or bar: the fake DOM matches descendant parts too (ancestors in
# order, ' ' and '>' alike), so a decoy table or bar earlier on the page is never the one a kept menu moves to.
DESCENDANTS = r"""
const one=N.prototype.matches;
N.prototype.matches=function(s){return s.split(',').some(part=>{const parts=part.trim().replace(/^:scope>/,'').split(/\s*>\s*|\s+/).filter(Boolean);
  if(!one.call(this,parts.at(-1)))return false;let n=this.parentElement;
  for(let i=parts.length-2;i>=0;i--){while(n&&!one.call(n,parts[i]))n=n.parentElement;if(!n)return false;n=n.parentElement;}return true;});};
"""


def test_a_column_menu_follows_its_column_across_a_redraw_and_acts_on_it_as_it_is_now(client_modules):
    script = FAKE_DOM + SELECTORS + DESCENDANTS + f"""
const tm=await import({json.dumps((client_modules / 'table_menu.js').as_uri())});
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
// A decoy table first on the page with the same column key: the kept menu must stay with its own table's column.
const decoy=document.createElement('div');decoy.setAttribute('data-table','other');
const host=document.createElement('div');host.setAttribute('data-table','rules');body.append(decoy,host);
const state={{v:1,wrap:false,cols:['who','where']}},ran=[];
const heading=(table,key,o)=>{{const th=document.createElement('th');th.setAttribute('data-col',key);
  th.append(tm.columnHeader({{label:key,wrap:{{on:state.wrap,set:on=>ran.push(table+' '+key+' wrap '+on+' v'+state.v)}},
    filter:o.filter,keep:o.unkept?undefined:'[data-table="'+table+'"] thead th[data-col="'+key+'"] > .dt-sort'}}));return th;}};
const typing=p=>{{const i=document.createElement('input');i.id='typed';p.append(i);}};
const draw=(o={{}})=>{{for(const [t,box] of [['other',decoy],['rules',host]]){{const thead=document.createElement('thead'),tr=document.createElement('tr');
  tr.append(...state.cols.map(k=>heading(t,k,{{unkept:o.unkept,filter:k==='who'?typing:undefined}})));
  thead.append(tr);box.replaceChildren(thead);}}}};
const btn=(t,k)=>(t==='rules'?host:decoy).querySelector('th[data-col="'+k+'"] .dt-sort');
const pop=()=>body.querySelector('.dt-col-pop'),wrapItem=()=>[...(pop()?.querySelectorAll('[role=menuitemcheckbox]')||[])].find(x=>x.textContent==='Wrap this column');
const out={{}};

// 1. Open, the focus on Wrap; a late refresh redraws both tables with the column now wrapped.
draw();btn('rules','who').click();const panel=pop();wrapItem().focus();
state.v=2;state.wrap=true;draw();await frames();
out.kept={{open:pop()===panel,expanded:btn('rules','who').getAttribute('aria-expanded'),decoy:btn('other','who').getAttribute('aria-expanded'),
  focus:document.activeElement.textContent,checked:wrapItem()?.getAttribute('aria-checked')}};
wrapItem().click();await frames();
out.acted={{ran:[...ran],closed:!pop(),expanded:btn('rules','who').getAttribute('aria-expanded')}};

// 2. Pressed in the instant between the redraw and the hand-over: the new heading's item runs, never the stale one.
ran.length=0;btn('rules','who').click();const stale=wrapItem();state.v=3;state.wrap=false;draw();stale.click();await frames();
out.stale={{ran:[...ran],closed:!pop()}};

// 3. In its Filter view (a field being typed) the redraw only moves it: the same field stays, nothing is rebuilt.
btn('rules','who').click();[...pop().querySelectorAll('[role=menuitem]')].find(x=>x.textContent.startsWith('Filter by'))?.click();
const typed=pop().querySelector('#typed');typed.focus();draw();await frames();
out.filter={{open:!!pop(),same:pop()?.querySelector('#typed')===typed,focus:document.activeElement===typed,expanded:btn('rules','who').getAttribute('aria-expanded')}};
dom.closePopover();

// 4. The column is gone after the redraw (hidden): the menu closes.
btn('rules','where').click();state.cols=['who'];draw();await frames();
out.gone={{closed:!pop()}};

// 5. A heading without `keep` closes on a redraw, as before.
draw({{unkept:true}});btn('rules','who').click();draw({{unkept:true}});await frames();
out.unkept={{closed:!pop()}};
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    # The same panel on this table's new heading (not the decoy's), its items the column as it is now, the focus kept.
    assert got['kept'] == {'open': True, 'expanded': 'true', 'decoy': 'false', 'focus': 'Wrap this column', 'checked': 'true'}
    assert got['acted'] == {'ran': ['rules who wrap false v2'], 'closed': True, 'expanded': 'false'}
    assert got['stale'] == {'ran': ['rules who wrap true v3'], 'closed': True}
    assert got['filter'] == {'open': True, 'same': True, 'focus': True, 'expanded': 'true'}
    assert got['gone'] == {'closed': True}
    assert got['unkept'] == {'closed': True}


def test_the_selection_bars_more_menu_follows_its_bar_across_a_redraw(client_modules):
    script = FAKE_DOM + SELECTORS + DESCENDANTS + f"""
const sb=await import({json.dumps((client_modules / 'selection_bar.js').as_uri())});
// Two bars on the page (a list's and the Bulk editor's): the decoy comes first.
const decoy=sb.selectionBar(),bar=sb.selectionBar(),ran=[];body.append(decoy.root,bar.root);
const state=(v,extra=['Export'])=>({{count:2,noun:['line','lines'],clear(){{}},actions:[{{label:'Approve',icon:'check',primary:true,run(){{}}}},
  ...extra.map(l=>({{label:l,icon:'download',more:true,run:()=>ran.push(l+' v'+v)}}))]}});
const more=b=>b.root.querySelector('.sel-more'),menu=()=>body.querySelector('[role=menu]'),items=()=>[...(menu()?.querySelectorAll('[role=menuitem]')||[])];
const out={{}};
decoy.show(state(1));bar.show(state(1));
more(bar).click();const panel=menu();items()[0].focus();
decoy.show(state(2));bar.show(state(2,['Export','Archive']));await frames();
out.kept={{open:menu()===panel,expanded:more(bar).getAttribute('aria-expanded'),decoy:more(decoy).getAttribute('aria-expanded'),
  items:items().map(x=>x.textContent),focus:document.activeElement.textContent}};
items()[0].click();await frames();
out.acted={{ran:[...ran],closed:!menu()}};
// Nothing extra left after the redraw: More is gone, and its menu closes.
more(bar).click();bar.show(state(3,[]));await frames();
out.gone={{closed:!menu(),more:!!more(bar)}};
// The selection cleared: closed too.
bar.show(state(4));more(bar).click();bar.show(null);await frames();
out.cleared={{closed:!menu()}};
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['kept'] == {'open': True, 'expanded': 'true', 'decoy': 'false', 'items': ['Export', 'Archive'], 'focus': 'Export'}
    assert got['acted'] == {'ran': ['Export v2'], 'closed': True}
    assert got['gone'] == {'closed': True, 'more': False}
    assert got['cleared'] == {'closed': True}
