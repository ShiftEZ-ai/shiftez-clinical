"""The shared capture renderer (feedback screenshots, Schedule PNG export) draws with every stylesheet the page links, in
the page's order, from the real compiled feedback_capture.ts in Node with a stub page: schedule_teams.css (team layout)
comes before app.css, as index.html links them; fonts are inlined per sheet; a failed read is not kept."""
import json
import subprocess

STUB = """
const sheets=i.sheets,asked=[];let fail=i.fail;
globalThis.location={href:'https://rota.test/schedule',origin:'https://rota.test'};
globalThis.document={querySelectorAll:q=>q==='link[rel=stylesheet]'?Object.keys(sheets).map(h=>({href:'https://rota.test'+h})):[]};
globalThis.FileReader=class{readAsDataURL(b){b.text().then(t=>{this.result='data:font;'+t;this.onload();});}};
globalThis.fetch=async(url,o)=>{const u=String(url);asked.push([u,o?.credentials]);if(fail){fail=false;throw new Error('offline');}
  const path=new URL(u).pathname;return {text:async()=>sheets[path],blob:async()=>new Blob(['bytes of '+path])};};
"""


def run(client_modules, data, body):
    script = (f"const i=JSON.parse(process.argv[1]);{STUB}const m=await import({json.dumps((client_modules/'feedback_capture.js').as_uri())});"
              f"console.log(JSON.stringify(await ({body})));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(data)], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


SHEETS = {'/assets/schedule_teams.css': '.pchip-team{flex-wrap:wrap}',
          '/assets/app.css': '@font-face{src:url(/assets/fonts/Geist.woff2)}.pchip-name{flex:1 1 0}.x{mask:url("data:image/svg+xml,a")}'}


def test_every_linked_sheet_in_page_order_with_fonts_inlined_read_once(client_modules):
    got = run(client_modules, {'sheets': SHEETS, 'fail': False},
              "(async()=>{const a=await m.appStyles(),b=await m.appStyles();return {css:a,same:a===b,asked};})()")
    css = got['css']
    # Team layout is drawn, and before app.css as on screen (the cascade depends on the order).
    assert 0 <= css.index('.pchip-team{flex-wrap:wrap}') < css.index('.pchip-name{flex:1 1 0}')
    assert 'url(data:font;bytes of /assets/fonts/Geist.woff2)' in css and 'url(/assets/fonts' not in css
    assert 'url("data:image/svg+xml,a")' in css  # an inline image stays as it is
    # Read once per page, same-origin credentials only.
    assert got['same'] is True
    assert got['asked'] == [['https://rota.test/assets/schedule_teams.css', 'same-origin'], ['https://rota.test/assets/app.css', 'same-origin'],
                            ['https://rota.test/assets/fonts/Geist.woff2', 'same-origin']]


def test_a_failed_read_is_said_and_the_next_capture_reads_again(client_modules):
    got = run(client_modules, {'sheets': SHEETS, 'fail': True},
              "(async()=>{let error='';try{await m.appStyles();}catch(e){error=e.message;}return {error,css:await m.appStyles()};})()")
    assert got['error'] == 'offline'
    assert '.pchip-team{flex-wrap:wrap}' in got['css'] and '.pchip-name{flex:1 1 0}' in got['css']


def test_capture_resolves_versioned_stylesheet_relative_font_urls(client_modules):
    version = 'a' * 64
    sheets = {f'/assets/{version}/app.css': '@font-face{src:url(fonts/Geist.woff2)}'}
    got = run(client_modules, {'sheets': sheets, 'fail': False}, '(async()=>({css:await m.appStyles(),asked}))()')
    assert got['asked'] == [[f'https://rota.test/assets/{version}/app.css', 'same-origin'],
                            [f'https://rota.test/assets/{version}/fonts/Geist.woff2', 'same-origin']]
    assert f'url(data:font;bytes of /assets/{version}/fonts/Geist.woff2)' in got['css']
