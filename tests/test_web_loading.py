"""FE-15: nothing loads as a bare "Loading…" line; every wait shows the shared skeleton (loading.ts)."""
import json
import re
import subprocess
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'


def _code(path):
    """The source without comments (a comment may say "Loading…"); `//` inside a URL string is kept."""
    text = re.sub(r'/\*[\s\S]*?\*/', '', path.read_text(encoding='utf-8'))
    return re.sub(r'(?:^|(?<=[\s;{})]))//[^\n]*', '', text, flags=re.M)


# A literal that starts "Loading" is a skeleton's label (`skeleton(shape,'Loading the …')`, the words the live region
# says) and nothing else. A tooltip or accessible name that says why a control waits is not a loading state.
LOADING = re.compile(r"""(['"`])(Loading\b[^'"`\n]*)\1""")
SKELETON_LABEL = re.compile(r'skeleton\((?:[^()]|\([^()]*\))*,\s*$')
TOOLTIP_REASONS = {('app.ts', 'Loading the whole rota…')}  # D59: why a held write waits (its tip) and the footer's busy pill


def test_no_bare_loading_text():
    # C17: every variant ("Loading…", "Loading details…", "Loading the …", note('Loading …'), a ternary's branch).
    bare = []
    for path in SRC.glob('*.ts'):
        if path.name == 'loading.ts':  # the helper: its own default label
            continue
        code = _code(path)
        for m in LOADING.finditer(code):
            line_start = code.rfind('\n', 0, m.start()) + 1
            if SKELETON_LABEL.search(code[line_start:m.start()]) or (path.name, m.group(2)) in TOOLTIP_REASONS:
                continue
            bare.append((path.name, code.count('\n', 0, m.start()) + 1, m.group(2)))
    assert not bare, f'Use skeleton(shape, label) for a read or working(text) for work (DESIGN.md › Loading): {bare}'


def test_the_loading_guard_catches_variants():
    # The guard itself: the near-variants the exact-string check let through (UIUX audit C17).
    def bare(line):
        return [m.group(2) for m in LOADING.finditer(line) if not SKELETON_LABEL.search(line[:m.start()])]
    for line in ("detail.replaceChildren(el('p',{role:'status'},'Loading details…'))",
                 "filters.replaceChildren(note('Loading print options…'))",
                 "el('li',{class:'muted'},`Loading ${name}…`)",
                 "return ok?'Ready':'Loading the rules that apply…'"):
        assert bare(line), line
    assert not bare("box.append(skeleton('lines','Loading history…'))")
    assert not bare("host.append(skeleton(shapeOf(view),`Loading ${title}…`,retry,{fill:true}))")


# Work in progress said as bare muted words ("Checking…") instead of `working()`'s one status line with its spinner.
# Ratchet: the inline ones left (inside a sentence, a drag ghost, a centred export stage) may only go, never grow.
BARE_WORK = re.compile(r"""(?:el\('(?:p|span|div|li|small|strong)',\{[^}]*\},|note\()'([A-Z][a-z]+ing\b[^']*…)'""")
BARE_WORK_ALLOWED = {
    ('bulk.ts', 'Counting…'), ('bulk.ts', 'Matching…'),  # inline in the selection banner / the match count
    ('schedule_export.ts', 'Drawing…'),  # LABEL-PROPS lane: the centred print stage
    ('timeline_drag.ts', 'Checking…'),  # the drag ghost's governance line
    ('rule_simplify.ts', 'Checking behaviour is unchanged…'),  # the heading over its own progress bar
}


def test_work_in_progress_uses_working():
    found = {(p.name, m.group(1)) for p in SRC.glob('*.ts') for m in BARE_WORK.finditer(_code(p))}
    assert found <= BARE_WORK_ALLOWED, f'Use working(text) from loading.ts: {sorted(found - BARE_WORK_ALLOWED)}'
    assert BARE_WORK_ALLOWED <= found, f'Migrated: remove from BARE_WORK_ALLOWED: {sorted(BARE_WORK_ALLOWED - found)}'


def test_static_start_up_wait_keeps_the_shared_timing():
    # startup.ts runs before any module (it cannot import loading.ts): its 15 s, its limit and its words are the
    # skeleton's (DL-045).
    loading = (SRC / 'loading.ts').read_text(encoding='utf-8')
    startup = (SRC / 'startup.ts').read_text(encoding='utf-8')
    for name in ('RETRY_AFTER', 'READ_TIMEOUT'):
        shared = re.search(rf'\b{name}=([\d_]+)', loading).group(1)
        assert re.search(rf'\b{name} = ([\d_]+)', startup).group(1) == shared, name
    words = dict(re.findall(r"(slow|timedOut):'([^']+)'", loading))
    assert f"SLOW_WORDS = '{words['slow']}'" in startup and f"TIMED_OUT_WORDS = '{words['timedOut']}'" in startup
    assert 'Taking longer than usual' not in loading + startup  # DL-045 retired those words


def test_start_up_clock_matches_the_shared_clock(client_modules):
    # The start-up copy of loading.ts clock (it cannot import) gives the same m:ss, past an hour too.
    startup = (SRC / 'startup.ts').read_text(encoding='utf-8')
    copy = re.search(r'const startupClock = (\(ms: number\) => \{.*?\});', startup).group(1).replace('(ms: number)', '(ms)')
    script = (f"const m=await import({json.dumps((client_modules / 'loading.js').as_uri())});const c={copy};"
              "const ms=[0,999,1000,18000,59999,60000,754000,3600000,3725000];"
              "console.log(JSON.stringify({copy:ms.map(c),shared:ms.map(m.clock),limit:m.timedOutWords()}));")
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out['copy'] == out['shared'] == ['0:00', '0:00', '0:01', '0:18', '0:59', '1:00', '12:34', '60:00', '62:05']
    assert out['limit'] == 'Didn’t load in 1:00.'


def test_icons_are_drawn_at_even_sizes():
    # An odd glyph in an even box lands on half pixels: blurred and visibly off centre (DESIGN.md › Visual contract).
    css = (SRC.parent / 'public/assets/app.css').read_text(encoding='utf-8')
    odd = []
    for selector, body in re.findall(r'([^{}\n]*)\{([^}]*)\}', css):
        if not any(re.search(r'(\.icon|svg)(:[\w-]+(\([^)]*\))?)*$', part.strip()) for part in selector.split(',')):
            continue
        sizes = [int(v) for v in re.findall(r'(?<![-\w])(?:width|height):(\d+)px', body)]
        if any(v % 2 for v in sizes):
            odd.append(selector.strip() + '{' + body + '}')
    assert not odd, odd


# Owner, 8 Nov 2026: a wait is only its skeleton (at start-up, the bar). Nothing floats or is positioned over content: no
# spinner card, no "Still loading…"; the words are for screen readers. Past 15 s one quiet line in flow.
def test_no_loading_state_is_drawn_over_content():
    css = (SRC.parent / 'public/assets/app.css').read_text(encoding='utf-8')
    over = [f'{sel.strip()}{{{body}}}' for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css)
            # The bar's moving fill is a pseudo-element inside its own track: it covers nothing.
            if re.search(r'\.(?:loading|sk-[\w-]+|wk-loading|long-work)\b', sel) and not re.search(r'::(?:after|before)\s*$', sel)
            and re.search(r'position:\s*(?:absolute|fixed|sticky)', body)]
    assert not over, f'A loading state sits in flow, never over content (DESIGN.md › Loading): {over}'
    loading = _code(SRC / 'loading.ts')
    skeleton = loading[loading.index('export function skeleton('):loading.index('export function settle(')]
    assert 'spinner' not in skeleton and 'SLOW_AFTER' not in skeleton, 'A skeleton shows no spinner and no 3 s stage'
    assert 'Still loading…' not in _code(SRC / 'startup.ts')  # no floating "Still loading…" card; DL-045's line has no …


# A small DOM: enough for skeleton() in Node (no browser on this check).
FAKE_DOM = """
const timers=[];let clock=0;globalThis.setTimeout=(f,ms)=>{timers.push([clock+ms,f]);return timers.length;};globalThis.clearTimeout=()=>{};
globalThis.performance={now:()=>clock};const repeating=new Set();let ids=0;
globalThis.setInterval=(f,ms)=>{const id=--ids,run=()=>{if(!repeating.has(id))return;f();timers.push([clock+ms,run]);};repeating.add(id);timers.push([clock+ms,run]);return id;};globalThis.clearInterval=id=>repeating.delete(id);
globalThis.requestAnimationFrame=()=>0;globalThis.addEventListener=()=>{};
const advance=ms=>{clock+=ms;for(let t;(t=timers.filter(x=>x[0]<=clock).sort((a,b)=>a[0]-b[0])[0]);){timers.splice(timers.indexOf(t),1);t[1]();}};
class N{constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.dataset={};this.className='';this.parent=null;this.text='';
  this.style={props:{},setProperty:(k,v)=>{this.style.props[k]=v;},removeProperty:k=>{delete this.style.props[k];}};
  this.classList={add:c=>{this.className+=' '+c;},remove:c=>{this.className=this.className.split(' ').filter(x=>x!==c).join(' ');},
    contains:c=>this.className.split(' ').includes(c)};}
  get isConnected(){return true;}get offsetHeight(){return 40;}get offsetTop(){return 8;}
  set textContent(v){this.children=[];this.text=String(v);}get textContent(){return this.text+this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
  setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];}
  append(...c){for(const x of c){if(typeof x!=='string')x.parent=this;this.children.push(x);}}prepend(...c){for(const x of c.reverse()){if(typeof x!=='string')x.parent=this;this.children.unshift(x);}}
  remove(){const p=this.parent;if(p)p.children.splice(p.children.indexOf(this),1);}addEventListener(){}
  all(){return this.children.flatMap(c=>typeof c==='string'?[]:[c,...c.all()]);}}
globalThis.document={createElement:t=>new N(t),body:new N('body')};
const visible=n=>n.classList.contains('sr-only')?'':n.text+n.children.map(c=>typeof c==='string'?c:visible(c)).join('');
"""


def test_a_skeleton_shows_only_itself_then_its_elapsed_time_after_15_s(client_modules):
    script = (f"{FAKE_DOM}const m=await import({json.dumps((client_modules / 'loading.js').as_uri())});const out={{}};"
              "for(const shape of ['boot','table','lines']){const box=m.skeleton(shape,'Connecting to Rota…');"
              "advance(m.RETRY_AFTER-1);const before=visible(box),spinners=box.all().filter(n=>n.classList.contains('spinner')).length;"
              "advance(1);const lines=()=>box.all().filter(n=>n.classList.contains('sk-slow'));const said=[document.body.children[0].textContent];"
              "advance(3000);said.push(document.body.children[0].textContent);"
              "out[shape]={before,spinners,sr:box.children.filter(n=>n.classList.contains('sr-only')).map(n=>n.textContent),"
              "lines:lines().map(n=>n.textContent),buttons:box.all().filter(n=>n.tag==='button').length,at:box.children.indexOf(lines()[0]),"
              "count:box.children.length,said};"
              "box.removeAttribute('aria-busy');advance(2000);out[shape].stopped=lines().map(n=>n.textContent);}"
              "console.log(JSON.stringify(out));")
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    for shape, o in out.items():
        assert o['before'] == '' and o['spinners'] == 0, (shape, o)  # nothing visible but the moving skeleton
        assert o['sr'] == ['Connecting to Rota…'], (shape, o)  # the words, for screen readers only
        assert o['lines'] == ['Still loading · 0:18'], (shape, o)  # one quiet line, its clock ticking each second
        assert o['buttons'] == 0, (shape, o)  # never Retry while the read is live (DL-045)
        assert o['at'] == (o['count'] - 1 if shape == 'boot' else 0), (shape, o)  # under the bar; else at the top
        assert o['said'] == ['Still loading', 'Still loading'], (shape, o)  # said once, never the ticking clock
        assert o['stopped'] == ['Still loading · 0:18'], (shape, o)  # the clock stops once the wait is over


# UX-RETRY-ROOT: a Retry or Try again that reloads the app state goes through app.ts retryRefresh (loading.ts flight
# retry), never plain refresh(), which joins a run on its way: behind a hung one the Retry would send nothing.
RETRY_TO_REFRESH = re.compile(
    r"""(?:button\(\s*|label\s*:\s*)(['"`])(?:Retry|Try again)\1\s*,\s*(?:run\s*:\s*)?"""
    r"""(?:(?:async\s*)?\(\)\s*=>\s*\{?\s*(?:void\s+|await\s+)?)?(?:ctx\.)?refresh\b(?!\w)""")


def test_no_retry_calls_plain_refresh():
    found = [f'{p.name}: {m.group(0)}' for p in SRC.glob('*.ts') for m in RETRY_TO_REFRESH.finditer(_code(p))]
    assert not found, found


def test_the_retry_guard_catches_variants():
    for bad in ("button('Retry',refresh,'secondary small')", "button('Retry',()=>void refresh(),'x')",
                "button('Try again',()=>ctx.refresh(),'x')", "{label:'Retry',run:()=>refresh()}",
                "button('Retry',async()=>{await refresh();},'x')"):
        assert RETRY_TO_REFRESH.search(bad), bad
    for good in ("button('Retry',retryRefresh,'x')", "button('Retry',()=>void refreshRelated(),'x')",
                 "button('Retry',()=>confirmAction('Retry',async()=>{await refresh();}),'x')"):
        assert not RETRY_TO_REFRESH.search(good), good
