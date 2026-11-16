"""maintenance: the Rules page's questions read (GET /api/rules-explained?group_by=question) is never held as its answer.
Run as the real compiled client in Node (api.ts + rules_explained.ts, no DOM): a first read that fails, or one that never
answers, leaves nothing behind; the Rules page's Retry (readAfresh, then the read) sends exactly one new request, and its
answer carries the groups."""
import json
import subprocess


def test_a_failed_or_hung_questions_read_is_never_held_and_retry_sends_exactly_one_request(client_modules):
    groups = {'groups': [{'key': 'B', 'letter': 'B', 'label': 'Staffing in each session', 'order': 1}], 'rules': [],
              'conflicts': []}
    script = f"""const api=await import({json.dumps((client_modules/'api.js').as_uri())});
const rules=await import({json.dumps((client_modules/'rules_explained.js').as_uri())});
globalThis.document={{cookie:''}};globalThis.window=globalThis;globalThis.addEventListener??=()=>{{}};
const sent=[];let answer='fail';
globalThis.fetch=(path)=>{{sent.push(path);const now=answer;
  if(now==='hang')return new Promise(()=>{{}});
  if(now==='fail')return Promise.resolve(new Response(JSON.stringify({{error:{{code:'INTERNAL_ERROR',message:'The service had a problem.'}},correlation_id:'ref-qa133'}}),{{status:500}}));
  return Promise.resolve(new Response(process.argv[1],{{status:200}}));}};
const out={{}};
// 1. The page opens: the read is sent and fails.
out.first=await rules.departmentRules('2026-11-08').then(()=>'ok',e=>[e.code,e.message,api.unexpectedError(e)]);out.afterFirst=sent.length;
// 2. Retry: drop this held question read (the shared cache owner), then read again: one new request and its answer is the groups.
answer='ok';rules.forgetDepartmentRules('2026-11-08');api.readAfresh();const got=await rules.departmentRules('2026-11-08');out.afterRetry=sent.length;out.groups=got.groups;
// 3. A read that never answers is not joined by Retry either: one new request, which answers.
answer='hang';api.leavePage();void rules.departmentRules('2026-11-09').catch(()=>{{}});const before=sent.length;
// The stuck state: asking again without dropping the held read joins it and sends nothing (the dead Retry).
answer='ok';void rules.departmentRules('2026-11-09');out.joined=sent.length-before;
rules.forgetDepartmentRules('2026-11-09');api.readAfresh();const again=await rules.departmentRules('2026-11-09');out.hungRetry=sent.length-before;out.hungGroups=again.groups;
out.paths=[...new Set(sent.map(p=>p.split('&')[0]))];
console.log(JSON.stringify(out));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(groups)],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    # The failure note shows the error's words and reference and offers Report this problem (an unexpected error).
    assert got['first'] == ['INTERNAL_ERROR', 'The service had a problem. Reference: ref-qa133', True]
    assert got['afterFirst'] == 1
    assert got['afterRetry'] == 2, got  # exactly one new request
    assert got['groups'] == groups['groups']
    assert got['joined'] == 0, got
    assert got['hungRetry'] == 1 and got['hungGroups'] == groups['groups'], got
    assert got['paths'] == ['/api/rules-explained?group_by=question']


def test_a_chip_whose_tooltip_says_why_carries_the_reason_in_its_accessible_name(client_modules):
    """maintenance: the reason a tooltip gives (an auto-closed session's "every usual assignee is away · …") is in the chip's
    accessible name too, after its visible word (WCAG 2.5.3); a reason equal to the word adds nothing."""
    script = f"""globalThis.document={{cookie:''}};const dom=await import({json.dumps((client_modules/'dom.js').as_uri())});
const chip=t=>({{textContent:t,dataset:{{}},attrs:{{}},setAttribute(k,v){{this.attrs[k]=v;}}}});
const a=dom.reasoned(chip('Closed'),'Closed: every usual assignee is away · Dr Test (on leave)');
const b=dom.reasoned(chip('Closed'),'Staff meeting');const c=dom.reasoned(chip('Closed'),'Closed');
console.log(JSON.stringify([a,b,c].map(x=>[x.attrs['aria-label']??null,x.dataset.tip])));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        ['Closed: every usual assignee is away · Dr Test (on leave)', 'Closed: every usual assignee is away · Dr Test (on leave)'],
        ['Closed: Staff meeting', 'Staff meeting'], [None, 'Closed']]
