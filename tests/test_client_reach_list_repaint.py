"""A rule line panel repaints its reach links (rule_catalogue.ts keepSame) whenever its check or reach answers. The
reach list a reader has just opened lives beside its link, with the arrow dom.ts tether() mounts after it: a repaint
that changes nothing keeps both, so a late answer never closes the list under its click. Real compiled picker.ts and
rule_catalogue.ts in Node with a small fake DOM."""
import json
import subprocess

from .test_client_menu_keep import SELECTORS
from .test_client_toast_save import FAKE_DOM

# What the repaint compares, the arrow and the picker need beyond the fake DOM: Element, after(), cloneNode(), outerHTML,
# value, scrollIntoView(), previousElementSibling and boxes.
CLONE = r"""
globalThis.Element=N;
Object.defineProperty(N.prototype,'value',{get(){return this._v??'';},set(v){this._v=String(v);}});
N.prototype.scrollIntoView=function(){};
Object.defineProperty(N.prototype,'previousElementSibling',{get(){const s=this.parentElement?.children||[];return s[s.indexOf(this)-1]||null;}});
N.prototype.after=function(...ns){const p=this.parentElement;ns.forEach((n,i)=>{const a=p._adopt(n);p.kids.splice(p.kids.indexOf(this)+1+i,0,a);});};
N.prototype.cloneNode=function(deep){const c=new N(this.tagName.toLowerCase(),this._t);c.attrs={...this.attrs};if(deep)c.append(...this.kids.map(k=>k.cloneNode(true)));return c;};
Object.defineProperty(N.prototype,'outerHTML',{get(){if(this.tagName==='#TEXT')return this._t;
  const a=Object.keys(this.attrs).sort().map(k=>` ${k}="${this.attrs[k]}"`).join('');return `<${this.tagName}${a}>${this.kids.map(k=>k.outerHTML).join('')}</${this.tagName}>`;}});
Object.defineProperty(N.prototype,'offsetWidth',{get(){return 300;}});Object.defineProperty(N.prototype,'offsetHeight',{get(){return 200;}});
// The list laid out under its link (as in the failing run's trace), so tether() draws the arrow on its top edge.
const rect=N.prototype.getBoundingClientRect;N.prototype.getBoundingClientRect=function(){
  return this.classList.contains('picker')?{top:327,bottom:527,left:940,right:1240,width:300,height:200}
    :this.classList.contains('rc-reach-link')?{top:303,bottom:319,left:940,right:990,width:50,height:16}:rect.call(this);};
globalThis.getComputedStyle=()=>({overflowX:'visible',getPropertyValue:()=>''});
"""


def test_a_repaint_that_changes_nothing_keeps_the_open_reach_list_and_its_arrow(client_modules):
    script = FAKE_DOM + SELECTORS + CLONE + f"""
const {{openPicker}}=await import({json.dumps((client_modules / 'picker.js').as_uri())});
const {{keepSame}}=await import({json.dumps((client_modules / 'rule_catalogue.js').as_uri())});
const link=text=>{{const b=document.createElement('button');b.className='rc-reach-link';b.setAttribute('aria-expanded','false');b.append(text);return b;}};
const box=document.createElement('span');box.className='rc-lp-count';body.append(box);
const first=link('3 people');keepSame(box,[' · ',first]);
openPicker(first,box,{{label:'3 people this line applies to',choices:[{{value:'p1',label:'First person'}}],multiple:false,
  selected:new Set(),recent:false,footer:'Reach fixture · this week'}});
const picker=()=>box.querySelector('.picker');
const out={{opened:{{picker:!!picker(),arrow:!!box.querySelector('.pop-arrow'),expanded:first.getAttribute('aria-expanded')}}}};
// The check's answer repaints the same link (a new button, same words): the list, its arrow and its link stay.
const panel=picker();keepSame(box,[' · ',link('3 people')]);
out.same={{picker:picker()===panel,connected:panel.isConnected,arrow:!!box.querySelector('.pop-arrow'),link:box.querySelector('.rc-reach-link')===first}};
// A different reach still repaints the link.
keepSame(box,[' · ',link('4 people')]);
out.changed={{picker:!!picker(),words:box.querySelector('.rc-reach-link').textContent}};
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    # The real picker mounts its panel and its arrow inside the link's box.
    assert got['opened'] == {'picker': True, 'arrow': True, 'expanded': 'true'}
    assert got['same'] == {'picker': True, 'connected': True, 'arrow': True, 'link': True}
    assert got['changed'] == {'picker': False, 'words': '4 people'}
