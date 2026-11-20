"""Execute the compiled refresh with controlled I/O; no DOM or browser qualification.

The refresh reads the account first and only then the lists (owner change , 5 Nov 2026: changed roles or an
inactive staff binding gate the catalogue request too, so a role change needs no second list read), and a live change
reads only what its scopes touch (: ME_SCOPES, LIST_SCOPES, taken from the compiled source here)."""
import json
import re
import subprocess

import pytest


SCENARIOS = ['ordered', 'roles', 'list-failure', 'account-failure', 'signout-after-account', 'signout-during-lists',
             'quiet', 'local', 'no-account', 'scoped-lists']


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_refresh_reads_the_account_before_the_lists_and_preserves_session_and_failure_paths(client_modules, scenario):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    # refreshOnce(stale =  => false) since  (UX-RETRY-ROOT: an explicit Retry never joins a hung refresh).
    refresh, = re.findall(r'async function refreshOnce\([^)]*\)[^{]*\{.*?\n\}', source, re.S)
    scopes, = re.findall(r'const ME_SCOPES = .*?;\n', source)
    script = r"""
import assert from 'node:assert/strict';
const scenario=JSON.parse(process.argv[1]);
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const account=deferred(),lists=deferred(),calls=[];
const flush=()=>new Promise(r=>setImmediate(r));  // every pending continuation runs (no time passes)
let generation=0,me={roles:['coordinator']},accessRead={},inPlace=scenario==='local',
    view=scenario==='local'?'schedule':'people',routeParts=[],plannerMemo={},refreshForeground=scenario!=='quiet'?'people/':null,
    refreshFailed=false,refreshSkipped=false,redrawOwed=false,readCount=0,
    refreshScopes=scenario==='scoped-lists'?new Set(['rules']):undefined;
const savedKinds=new Set(scenario==='local'?['shift.update']:[]);
const sessionKey=()=>generation,pageStep=()=>'people/',readAfresh=()=>{},redraw=()=>calls.push('redraw'),
    background=path=>{calls.push(path);return account.promise;},
    applyDisplay=()=>calls.push('display'),loadNavBadges=()=>calls.push('badges'),
    readerBusy=()=>scenario==='quiet',renderPage=()=>calls.push('render'),refreshRelated=async()=>undefined,showStale=()=>{},
    sessionChanged=e=>e.code==='SESSION_CHANGED',showConnection=ok=>calls.push(ok?'online':'offline'),
    notify=()=>calls.push('notice'),document={querySelector:()=>false},
    forgetReads=()=>{};  // kept answers are dropped by scope before a refresh (QA-115/118); not what this test proves
let refreshChanges=[],foregroundRender=false;
function loadCatalogues(){
  calls.push('catalogues');readCount++;
  if(scenario==='signout-during-lists'){generation++;me=null;}
  return lists.promise;
}
""" + scopes + refresh + r"""
if(scenario==='no-account')me=null;
const run=refreshOnce();
if(['local','no-account'].includes(scenario)){
  await run;assert.deepEqual(calls,scenario==='local'?['redraw']:[]);
}else if(scenario==='scoped-lists'){
  // A rules change: the account cannot have changed, so only the lists are read.
  await flush();assert.deepEqual(calls,['display','catalogues']);
  lists.resolve();await run;
  assert.equal(refreshFailed,false);assert(calls.includes('render'));assert(!calls.includes('/api/me'));
}else{
  // The lists wait for the account: they are read only once it is applied.
  await flush();assert.deepEqual(calls,['/api/me']);
  if(scenario==='account-failure')account.reject(new Error('account unavailable'));
  else account.resolve({roles:scenario==='roles'?['staff']:['coordinator']});
  await flush();
  if(scenario!=='account-failure')assert.deepEqual(calls,['/api/me','display','catalogues']);
  if(scenario==='roles')assert.deepEqual(me.roles,['staff']);  // the lists were asked for as the new account
  if(scenario==='signout-after-account'){generation++;me=null;lists.reject(Object.assign(new Error('signed out'),{code:'SESSION_CHANGED'}));}
  else if(scenario==='list-failure')lists.reject(new Error('lists unavailable'));
  else lists.resolve();
  await run;
  if(['signout-after-account','signout-during-lists'].includes(scenario)){
    assert.equal(me,null);assert(!calls.includes('render'));assert(!calls.includes('online'));assert(!calls.includes('notice'));
  }else if(['list-failure','account-failure'].includes(scenario)){
    assert.equal(refreshFailed,true);assert(calls.includes('offline'));assert(calls.includes('notice'));assert(!calls.includes('render'));
    if(scenario==='account-failure')assert.equal(readCount,0);  // no lists for an account that could not be read
  }else{
    assert.equal(refreshFailed,false);assert(calls.includes('online'));assert(calls.includes('badges'));
    assert.equal(calls.includes('render'),scenario!=='quiet');
    if(scenario==='quiet')assert(refreshSkipped&&redrawOwed);
    assert.equal(readCount,1);  // one list read, a role change included
  }
}
console.log('refresh scenario passed');
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(scenario)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
