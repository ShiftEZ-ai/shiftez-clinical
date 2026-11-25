"""Application branding in the client: the public answer is read field by field (only this site's own image paths, the
generated defaults for anything unusable), and the static start-up page and manifest name the generated default app.
Start-up and sign-in never wait for the branding read or a logo's decode, and a late answer never overwrites a newer one."""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from packages.contracts import schemas as s

ROOT = Path(__file__).resolve().parents[1]


def asset(client_modules, name):
    """A default image as the module resolves it: its file URL's path (`/C:/...` on Windows)."""
    return urlparse((client_modules / name).as_uri()).path


def parse(client_modules, value):
    script = (f"const m=await import({json.dumps((client_modules/'brand.js').as_uri())});"
              "console.log(JSON.stringify(m.parseBranding(JSON.parse(process.argv[1]))));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(value)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_public_branding_is_read_field_by_field_with_only_local_images(client_modules):
    good = {'short_name': 'KR', 'app_name': 'Rota', 'institution_name': 'Hospital', 'emblem_url': '/api/branding/images/a',
            'wordmark_url': '/api/branding/images/b', 'emblem_alt': '', 'wordmark_alt': 'Hospital', 'revision': 3}
    assert parse(client_modules, good) == good
    defaults = {**s.BRANDING_DEFAULTS, **{kind + '_url': asset(client_modules, s.BRANDING_DEFAULTS[kind + '_url'])
                for kind in ('emblem', 'wordmark')}, 'revision': 0}
    for value in (None, [], 'x', {'revision': 0}):
        assert parse(client_modules, value) == defaults
    hostile = {**good, 'short_name': 'x' * 161, 'app_name': '  ', 'emblem_url': '//evil.example/x.png', 'wordmark_url': 'javascript:alert(1)',
               'emblem_alt': 5, 'revision': 1.5}
    assert parse(client_modules, hostile) == {**good, 'short_name': s.BRANDING_DEFAULTS['short_name'],
        'app_name': s.BRANDING_DEFAULTS['app_name'],
        'emblem_url': asset(client_modules, s.BRANDING_DEFAULTS['emblem_url']), 'wordmark_url': asset(client_modules, s.BRANDING_DEFAULTS['wordmark_url']),
        'emblem_alt': s.BRANDING_DEFAULTS['emblem_alt'], 'revision': 0}
    for url in ('data:image/png;base64,AA', 'https://example.org/a.png', '/a\\b', '/a b'):
        assert parse(client_modules, {**good, 'emblem_url': url})['emblem_url'] == asset(client_modules, s.BRANDING_DEFAULTS['emblem_url'])


def test_static_startup_fallbacks_name_the_generated_default_app():
    # index.html and the web manifest cannot run code: they keep the default name, checked against its one owner.
    name = s.BRANDING_DEFAULTS['app_name']
    html = (ROOT / 'apps/web/public/index.html').read_text(encoding='utf-8')
    assert f'<title>{name} · ' in html and f'<h1 data-brand-name>{name}</h1>' in html
    # The start-up screen's emblem is the generated default (the app repaints it, like every .brand-emblem).
    assert f'<img class="boot-emblem brand-emblem" src="/assets/{s.BRANDING_DEFAULTS["emblem_url"]}"' in html
    # The web manifest is generated from the active branding by the API (one owner); the page links it there.
    assert '<link rel="manifest" href="/api/pwa/manifest.webmanifest">' in html
    # startup.ts runs only when app.js failed to load: it names the default short name at once, then the configured one
    # when the public branding still answers (C29).
    startup = (ROOT / 'apps/web/src/startup.ts').read_text(encoding='utf-8')
    assert f"'{s.BRANDING_DEFAULTS['short_name']} can’t be reached'" in startup
    assert "fetch('/api/branding'" in startup and '`${short} can’t be reached`' in startup
    for kind in ('emblem', 'wordmark'):
        assert (ROOT / 'apps/web/public/assets' / s.BRANDING_DEFAULTS[kind + '_url'].lstrip('/')).is_file()


APPLY = r"""
const pending={};
globalThis.Image=class{set src(u){this.u=u;}get src(){return this.u;}
  decode(){return new Promise((ok,no)=>{pending[this.u]={ok:()=>{this.naturalWidth=1;this.naturalHeight=1;ok();},no:()=>no(new Error('x'))};});}};
const painted={};const slot=k=>[{set textContent(v){painted[k]=v;}}];
globalThis.document={title:'',querySelectorAll:s=>s==='[data-brand-name]'?slot('name'):s==='[data-brand-short]'?slot('short'):[]};
const m=await import(process.argv[1]);
const tick=()=>new Promise(r=>setImmediate(r));
const out={};const b=(rev,tag)=>({...m.brand(),short_name:'S '+tag,app_name:'App '+tag,emblem_url:'/e/'+tag,wordmark_url:'/w/'+tag,emblem_alt:tag,revision:rev});
// A logo that never loads: the name shows at once, the shown logo stays, and nothing waits for it.
let settled=false;m.applyBranding(b(2,'slow')).then(()=>{settled=true;});m.documentTitle('Sign in');
await tick();out.slow={name:m.brand().app_name,painted:painted.name,short:painted.short,title:document.title,emblem:m.brand().emblem_url,settled};
// Newer branding asked for while the older one's logos load: the older late logo never overwrites it.
m.applyBranding(b(3,'new'));pending['/e/new'].ok();await tick();pending['/e/slow'].ok();await tick();
out.race={name:m.brand().app_name,emblem:m.brand().emblem_url,alt:m.brand().emblem_alt};
// An older revision is ignored; a failing logo keeps the one shown.
await m.applyBranding(b(1,'old'));out.older=m.brand().app_name;
const failing=m.applyBranding(b(4,'bad'));pending['/e/bad'].no();pending['/w/bad'].no();await failing;
out.failed={name:m.brand().app_name,emblem:m.brand().emblem_url};
console.log(JSON.stringify(out));
"""


def test_branding_never_waits_for_a_logo_and_a_late_answer_never_overwrites_newer(client_modules):
    out = json.loads(subprocess.run(['node', '--input-type=module', '-e', APPLY, (client_modules/'brand.js').as_uri()],
                                    capture_output=True, text=True, check=True, timeout=15).stdout)
    default = asset(client_modules, s.BRANDING_DEFAULTS['emblem_url'])
    assert out['slow'] == {'name': 'App slow', 'painted': 'App slow', 'short': 'S slow', 'title': 'Sign in · App slow', 'emblem': default,
                           'settled': False}
    assert out['race'] == {'name': 'App new', 'emblem': '/e/new', 'alt': 'new'}
    assert out['older'] == 'App new'
    assert out['failed'] == {'name': 'App bad', 'emblem': '/e/new'}


def test_start_up_and_sign_in_never_wait_for_the_branding_read():
    # The boot-ready boundary (account read, sign-in config) awaits no branding read or logo: it is started once, unawaited.
    app = (ROOT / 'apps/web/src/app.ts').read_text(encoding='utf-8')
    assert app.count("'/api/branding'") == 1 and app.count('readBranding()') == 1
    assert "void fetch('/api/branding'" in app and '\nreadBranding();' in app
    assert not re.search(r'await[^;]*(readBranding|applyBranding|/api/branding)', app)
