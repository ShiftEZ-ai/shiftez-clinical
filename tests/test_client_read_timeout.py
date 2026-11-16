"""DL-045: every read ends. The real compiled api.ts in Node on a virtual clock (test_client_command_confirm's harness): a
read with no answer by READ_TIMEOUT is aborted and rejects READ_TIMEOUT, "Didn’t load in 1:00." (never a connection
outage); a slow read that answers in time is data; a read the page abandoned is PAGE_LEFT, not a timeout; and the Retry
after a timeout is a fresh read that recovers."""
from .test_client_command_confirm import run


def test_a_read_with_no_answer_ends_at_the_limit_and_retry_reads_afresh(client_modules):
    got = run(client_modules, "(n)=>n===1?'hang':n===2?'slow':n===3?'hang':'ok'",
              "const L=await import(url.replace('api.js','loading.js'));const start=clock,health=[];"
              "window.addEventListener('server-health',e=>health.push(e.detail));"
              "const late=await m.get('/api/hung').then(()=>null,e=>({code:e.code,message:e.message,after:clock-start}));"
              "const slow=await m.get('/api/slow').then(r=>!!r.workspace,e=>e.code);"
              "const left=m.get('/api/left').then(()=>null,e=>e.code);m.leavePage();const gone=await left;"
              "const retried=await m.get('/api/hung').then(r=>!!r.workspace,e=>e.code);"
              "console.log(JSON.stringify({late,slow,gone,retried,fetches:n,health,now:m.serverHealth(),limit:L.READ_TIMEOUT}));")
    assert got['limit'] == 60_000
    assert got['late'] == {'code': 'READ_TIMEOUT', 'message': 'Didn’t load in 1:00.', 'after': 60_000}
    assert got['slow'] is True  # 12 s is slow, not timed out
    assert got['gone'] == 'PAGE_LEFT'  # abandoned by the page, not a timeout
    assert got['retried'] is True and got['fetches'] == 4  # Retry is a new request, never the aborted one
    assert got['health'] == [] and got['now'] == 'ok'  # slow is not down
