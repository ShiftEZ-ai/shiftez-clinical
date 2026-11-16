"""INN-043: the compiled client keeps bidi isolates at render sinks only."""

import json
import subprocess


DOM_GLOBALS = r"""
class N {
  constructor(tag = '#text', value = '') {
    this.tagName = tag === '#text' ? '#text' : String(tag).toUpperCase();
    this.nodeType = tag === '#text' ? 3 : 1;
    this.value = value;
    this.childNodes = [];
    this.attributes = {};
    this.dataset = {};
    this.className = '';
    this.style = {setProperty(){}, removeProperty(){}};
  }
  append(...nodes) { for (const node of nodes.flat()) { if (node != null) this.childNodes.push(node); } }
  prepend(...nodes) { this.childNodes.unshift(...nodes.flat().filter(Boolean)); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'class') this.className = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  hasAttribute(key) { return key in this.attributes; }
  addEventListener() {}
  removeEventListener() {}
  get textContent() { return this.nodeType === 3 ? this.value : this.childNodes.map(n => n.textContent ?? '').join(''); }
  set textContent(value) { this.childNodes = [new N('#text', String(value))]; }
  get firstChild() { return this.childNodes[0] ?? null; }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  querySelectorAll(selector) {
    const out = [];
    const walk = node => { for (const child of node.children) { if (selector === 'bdi' && child.tagName === 'BDI') out.push(child); walk(child); } };
    walk(this);
    return out;
  }
  closest() { return null; }
  matches() { return false; }
}
globalThis.Node = N;
globalThis.Element = N;
globalThis.HTMLElement = N;
globalThis.HTMLButtonElement = class extends N {};
globalThis.HTMLAnchorElement = class extends N {};
globalThis.window = globalThis;
globalThis.location = {hash: ''};
globalThis.history = {pushState(){}, replaceState(){}};
globalThis.matchMedia = () => ({matches: false, addEventListener(){}});
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = callback => setImmediate(callback);
globalThis.cancelAnimationFrame = () => {};
globalThis.MutationObserver = class {observe(){} disconnect(){}};
globalThis.getComputedStyle = () => ({getPropertyValue: () => ''});
globalThis.document = {
  body: new N('body'),
  documentElement: new N('html'),
  activeElement: null,
  createElement: tag => new N(tag),
  createElementNS: (_ns, tag) => new N(tag),
  createTextNode: value => new N('#text', String(value)),
  addEventListener() {},
  removeEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};
"""


def run(client_modules, body):
    script = DOM_GLOBALS + (
        f"const d=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
        f"const p=await import({json.dumps((client_modules / 'person_label.js').as_uri())});"
        + body
    )
    result = subprocess.run(
        ['node', '--input-type=module', '-e', script],
        capture_output=True,
        text=True,
        encoding='utf-8',
        check=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def test_bidi_helpers_isolate_render_and_return_raw_names(client_modules):
    raw = '(test) د. سنبلة · R2'
    out = run(client_modules, f"""
const raw={json.dumps(raw)};
const marked=d.isolate(raw);
const sentence=d.el('p',{{}},'Saved ',marked,' created.');
const pill=d.bidi(raw,{{class:'pchip-name'}});
const allMarks='\\u2066left\\u2067'+marked+'\\u2066right\\u2069';
const name={{id:'p1',display_name:raw}};
console.log(JSON.stringify({{
  marked,
  unisolated:d.unisolated(allMarks),
  sentence:sentence.textContent,
  sentenceBdi:sentence.querySelectorAll('bdi').map(n=>n.textContent),
  pill:pill.textContent,
  pillParts:pill.children.map(n=>n.tagName === 'BDI' ? n.textContent : n.textContent),
  label:d.labelOf({{name:raw}}),
  full:p.fullNameOf(name),
  person:p.personName('p1',raw),
  known:p.personName(null,raw),
  rendered:p.nameText(null,raw).textContent,
}}));
""")
    assert out == {
        'marked': '\u2068' + raw + '\u2069',
        'unisolated': 'left' + raw + 'right',
        'sentence': 'Saved ' + raw + ' created.',
        'sentenceBdi': [raw],
        'pill': raw,
        'pillParts': ['(test) د. سنبلة', 'R2'],
        'label': raw,
        'full': raw,
        'person': raw,
        'known': raw,
        'rendered': raw,
    }


def test_bidi_dots_are_separate_runs_and_no_marks_escape_text_content(client_modules):
    out = run(client_modules, """
const label='عيادة ب · 3';
const pill=d.bidi(label);
const parts=pill.children.map(n=>n.textContent);
const sentence=d.el('p',{},'Created ',d.isolate('عطلة 2'),'!');
console.log(JSON.stringify({parts,separator:pill.childNodes[1]?.textContent,text:sentence.textContent,raw:d.unisolated(sentence.textContent)}));
""")
    assert out == {
        'parts': ['عيادة ب', '3'],
        'separator': ' · ',
        'text': 'Created عطلة 2!',
        'raw': 'Created عطلة 2!',
    }
