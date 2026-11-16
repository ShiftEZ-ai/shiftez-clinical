"""Real client modules: same-key Fix list and panel consumers own one wait together."""
import json
import subprocess

import pytest


COMMON = """
import assert from 'node:assert/strict';
const api=await import(API_URL),candidates=await import(CANDIDATES_URL);
const key='workspace:7:session::finding',body=f=>({workspace_id:'workspace',shift_id:'session',finding_key:f});
const calls=[];
const deferred=()=>{let resolve,reject;const p=new Promise((yes,no)=>{resolve=yes;reject=no;});return {p,resolve,reject};};
const read=(body,o)=>{const d=deferred();calls.push({body,o,...d});return d.p;};
const asks=candidates.askRegistry(read);api.onSessionReset(()=>asks.reset());
const hold=(surface,k=key,finding='')=>asks.held(surface,k,body,finding);
const stop=surface=>asks.stop(surface);
const stopped=(n=0)=>calls[n].o.stopped();
const result={candidates:[{person_id:'p1',status:'ok'}],source_fingerprint:'fp'};
globalThis.window={clearTimeout};
"""


def check(client_modules, script):
    imports = {'API_URL': 'api.js', 'CANDIDATES_URL': 'candidates.js'}
    source = COMMON
    for marker, filename in imports.items():
        source = source.replace(marker, json.dumps((client_modules / filename).as_uri()))
    run = subprocess.run(['node', '--input-type=module', '-e', source + script],
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize('creator', ['fix', 'panel'])
@pytest.mark.parametrize('leaving_creator', [False, True])
@pytest.mark.parametrize('switch', [False, True])
def test_same_key_wait_survives_either_surface_leaving(client_modules, creator, leaving_creator, switch):
    other = 'panel' if creator == 'fix' else 'fix'
    leaving, remaining = (creator, other) if leaving_creator else (other, creator)
    left_result = 'first' if leaving == creator else 'second'
    kept_result = 'second' if left_result == 'first' else 'first'
    action = f"const next=hold({json.dumps(leaving)},'different-session');" if switch else f"stop({json.dumps(leaving)});"
    check(client_modules, f"""
const first=hold({json.dumps(creator)}),second=hold({json.dumps(other)});
const firstResult=first.final.catch(e=>e.code),secondResult=second.final.catch(e=>e.code);
assert.equal(calls.length,2,'each same-key consumer owns one server token');
assert.notEqual(first.final,second.final);
assert.equal(hold({json.dumps(creator)}),first,'the same surface joins its existing same-key consumer');
assert.equal(calls.length,2);
let pages=0;second.on.add(()=>pages++);
{action}
const leftIndex={0 if left_result == 'first' else 1},keptIndex={1 if left_result == 'first' else 0};
assert.equal(stopped(leftIndex),true,'the leaving consumer is stopped');
assert.equal(stopped(keptIndex),false,'the other surface still needs its own request');
calls[leftIndex].o.onPage(result);assert.equal(pages,0,'a stopped consumer cannot publish a late page');
calls[keptIndex].o.onPage(result);assert.equal(pages,1);
calls[leftIndex].resolve(result);calls[keptIndex].resolve(result);
assert.equal(await {left_result}Result,'STOPPED');
assert.deepEqual(await {kept_result}Result,result);
stop({json.dumps(remaining)});
assert.equal(stopped(keptIndex),true,'the last consumer stops its wait');
if({json.dumps(switch)}){{calls[2].resolve(result);await next.final;}}
""")


@pytest.mark.parametrize('reset', [False, True])
def test_last_close_or_signout_stops_old_wait_without_evicting_reopened_same_key(client_modules, reset):
    action = 'api.clearSessionState();' if reset else "stop('fix');stop('panel');"
    check(client_modules, f"""
const first=hold('fix'),second=hold('panel');const oldFailure=first.final.catch(e=>e.code);
const otherFailure=second.final.catch(e=>e.code);
let pages=0;first.on.add(()=>pages++);
{action}
assert.equal(stopped(0),true);assert.equal(stopped(1),true);
const reopened=hold('panel');assert.equal(calls.length,3);assert.equal(stopped(2),false);
assert.equal(stopped(0),true,'reopening must not revive the old wait');
calls[0].o.onPage(result);assert.equal(pages,0,'late old pages cannot update consumers');
calls[0].reject(Object.assign(new Error('stopped'),{{code:'STOPPED'}}));assert.equal(await oldFailure,'STOPPED');
calls[1].reject(Object.assign(new Error('stopped'),{{code:'STOPPED'}}));assert.equal(await otherFailure,'STOPPED');
const joined=hold('fix');assert.equal(calls.length,4,'old cleanup must preserve the replacement');
assert.notEqual(joined.final,reopened.final);calls[2].resolve(result);calls[3].resolve(result);
assert.deepEqual(await reopened.final,result);assert.deepEqual(await joined.final,result);
""")


def test_late_post_cancels_only_the_abandoned_job_and_preserves_reopened_request(client_modules):
    check(client_modules, """
const posts=[],released=[];
const jobRead=(body,o)=>candidates.askCandidates(body,{
  post:()=>{const d=deferred();posts.push(d);return d.p;},
  wait:async(job,x)=>x.stop()?null:{status:'succeeded',result},
  release:(job,token)=>released.push([job.id,token])
},o);
const local=candidates.askRegistry((b,o)=>jobRead(b,o));
const first=local.held('fix',key,body,'');const failure=first.final.catch(e=>e.code);
local.stop('fix');
const reopened=local.held('panel',key,body,'');
posts[0].resolve({job:{id:'abandoned'},consumer_token:'abandoned-token'});assert.equal(await failure,'STOPPED');
assert.deepEqual(released,[['abandoned','abandoned-token']]);
const joined=local.held('fix',key,body,'');assert.equal(posts.length,3);
assert.notEqual(joined.final,reopened.final);
posts[1].resolve({job:{id:'current'},consumer_token:'current-token'});
posts[2].resolve({job:{id:'current-2'},consumer_token:'current-token-2'});
assert.deepEqual(await reopened.final,{...result,provisional_page:false});
assert.deepEqual(await joined.final,{...result,provisional_page:false});
assert.deepEqual(released,[['abandoned','abandoned-token']]);
""")


def test_issue_gone_fallback_runs_only_for_an_active_wait(client_modules):
    check(client_modules, """
const old=hold('fix',key,'finding');const failure=old.final.catch(e=>e.code);
stop('fix');
calls[0].reject(Object.assign(new Error('gone'),{code:'ISSUE_GONE'}));await failure;
assert.equal(calls.length,1,'an abandoned ask cannot start a fallback job');
const active=hold('panel',key,'finding');
calls[1].reject(Object.assign(new Error('gone'),{code:'ISSUE_GONE'}));
await Promise.resolve();assert.equal(calls.length,3);assert.equal(calls[2].body.finding_key,'');
calls[2].resolve(result);assert.equal((await active.final).issue_gone,true);
""")
