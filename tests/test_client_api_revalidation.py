"""maintenance: a big read revalidates with its ETag (the real compiled api.ts in Node, no DOM): the second read in a tab
sends If-None-Match and a 304 answers with the kept reply; sign-out forgets it; no read goes to the browser's HTTP
cache (cache 'no-store': kept replies live in this tab's memory only, so a reload reads in full)."""
import json
import subprocess


def test_a_repeat_read_revalidates_with_its_etag_and_sign_out_forgets_it(client_modules):
    script = (f"const m=await import({json.dumps((client_modules/'api.js').as_uri())});"
              "globalThis.document={cookie:''};globalThis.window=globalThis;const sent=[];"
              "globalThis.fetch=async(p,o)=>{const tag=new Headers(o.headers).get('If-None-Match');sent.push({tag,cache:o.cache});"
              "return tag==='\"v1\"'?new Response(null,{status:304,headers:{ETag:'\"v1\"'}})"
              ":new Response(JSON.stringify({people:[1,2]}),{status:200,headers:{ETag:'\"v1\"'}});};"
              "const a=await m.get('/api/catalogues');const b=await m.get('/api/catalogues');"
              "m.clearSessionState();const c=await m.get('/api/catalogues');"
              "console.log(JSON.stringify({a,b,c,sent}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['a'] == got['b'] == got['c'] == {'people': [1, 2]}
    assert got['sent'] == [{'tag': None, 'cache': 'no-store'}, {'tag': '"v1"', 'cache': 'no-store'},
                           {'tag': None, 'cache': 'no-store'}]
