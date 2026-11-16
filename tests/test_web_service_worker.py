"""Execute the shipped worker's install/activate/fetch lifecycle without storing any account data."""
import json
from pathlib import Path
import subprocess

from packages.web_assets import bundle_directory

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_worker_caches_the_complete_bundle_and_keeps_data_on_the_network():
    web = ROOT / 'apps/web/dist'
    bundle = bundle_directory(web)
    expected = ['/'] + sorted('/' + p.relative_to(web).as_posix() for p in bundle.rglob('*')
                             if p.is_file() and p.suffix != '.gz')
    script = r'''
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const origin='https://rota.test', handlers={},stores=new Map(),network=[];
const key=r=>new URL(typeof r==='string'?r:r.url,origin).href;
const cache=()=>({entries:new Map(),async addAll(files){for(const f of files)this.entries.set(key(f),new Response(f));},
  async match(r){return this.entries.get(key(r))?.clone();},async put(r,v){this.entries.set(key(r),v);}});
stores.set('rota-shell-v1',cache());stores.set('rota-shell-old',cache());stores.set('another-app',cache());
const caches={async open(n){if(!stores.has(n))stores.set(n,cache());return stores.get(n);},
  async keys(){return [...stores.keys()];},async delete(n){return stores.delete(n);}};
let offline=false,skipped=false,claimed=false;
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{URL,Response,caches,location:{origin},
  self:{addEventListener:(n,f)=>handlers[n]=f,skipWaiting:async()=>{skipped=true;},clients:{claim:async()=>{claimed=true;}}},
  fetch:async r=>{network.push(key(r));if(offline)throw new Error('offline');return new Response('network');}});
async function lifecycle(name){let done;handlers[name]({waitUntil:p=>done=p});await done;}
await lifecycle('install');assert(skipped);
await lifecycle('activate');assert(claimed);
const names=await caches.keys(),shell=names.find(n=>n.startsWith('rota-shell-'));
assert.deepEqual(names.sort(),['another-app',shell].sort());
const kept=await caches.open(shell),files=[...kept.entries.keys()].map(u=>new URL(u).pathname).sort();
assert.deepEqual(files,JSON.parse(process.argv[2]).sort());
function request(path,method='GET'){let answer;handlers.fetch({request:{url:origin+path,method},respondWith:p=>answer=p});return answer;}
for(const p of ['/api/me','/api/branding','/auth/login','/calendar/private.ics','/assets/app.js','/assets/not-this-version/missing.js','/?state=private'])
  assert.equal(request(p),undefined,p);
assert.equal(request('/', 'POST'),undefined);
const module=files.find(p=>p.endsWith('/app.js'));offline=true;
assert.equal(await (await request(module)).text(),module);assert.equal(network.length,0);
assert.equal(await (await request('/')).text(),'/');
offline=false;assert.equal(await (await request('/')).text(),'network');
offline=true;assert.equal(await (await request('/')).text(),'network');
kept.entries.delete(key(module));await assert.rejects(request(module),/offline/);
offline=false;assert.equal(await (await request(module)).text(),'network');
offline=true;assert.equal(await (await request(module)).text(),'network');
const put=kept.put;kept.put=async()=>{throw new Error('quota');};kept.entries.delete(key(module));
offline=false;assert.equal(await (await request(module)).text(),'network');kept.put=put;
assert([...kept.entries.keys()].every(k=>!new URL(k).pathname.startsWith('/api')));
console.log(JSON.stringify({shell,files:files.length}));
'''
    out = subprocess.run(['node', '--input-type=module', '-e', script, str(web / 'sw.js'), json.dumps(expected)],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)['shell'] == 'rota-shell-' + bundle.name


def test_shipped_worker_shows_every_push_and_opens_only_this_apps_pages():
    """Push: a visible alert for every message (iOS revokes permission after silent pushes), readable or not, and the
    icon number from its unread count. A tap hands an open window the address (its own unsaved-changes guard decides)
    or opens one; an address from elsewhere opens the inbox instead."""
    web = ROOT / 'apps/web/dist'
    script = r'''
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const origin='https://rota.test',handlers={},shown=[],badges=[],posted=[],opened=[];let tabs=[];
const caches={open:async()=>({}),keys:async()=>[]},fetch=async u=>{assert.equal(u,'/api/pwa/manifest.webmanifest');return new Response(JSON.stringify({short_name:'Rota'}));};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{URL,Response,caches,fetch,location:{origin},
  self:{addEventListener:(n,f)=>handlers[n]=f,navigator:{setAppBadge:async n=>badges.push(n),clearAppBadge:async()=>badges.push(0)},
    registration:{showNotification:async(title,o)=>shown.push({title,...o})},
    clients:{matchAll:async()=>tabs,openWindow:async u=>opened.push(u)}}});
async function fire(name,event){let done;handlers[name]({...event,waitUntil:p=>done=p});await done;}
const push=data=>fire('push',{data:data===undefined?null:{json:()=>JSON.parse(data)}});
await push(JSON.stringify({v:1,notification_id:'n1',delivery_id:'d1',title:'Duty changed',body:'Open Rota to see it',url:'/n/n1',tag:'duty-n1',unread:{count:3,revision:4,scope:'department',department_id:'d'}}));
await push('not json');await push(undefined);
await push(JSON.stringify({web_push:8030,notification:{title:'Rota published',body:'December',navigate:'/#schedule',app_badge:0}}));
await push(JSON.stringify({title:'Elsewhere',url:'https://evil.example/phish'}));
assert.deepEqual(shown.map(n=>[n.title,n.body,n.data.url]),[['Duty changed','Open Rota to see it',origin+'/n/n1'],
  ['Rota','You have a rota update',origin+'/#inbox'],['Rota','You have a rota update',origin+'/#inbox'],
  ['Rota published','December',origin+'/#schedule'],['Elsewhere','You have a rota update',origin+'/#inbox']]);
assert.equal(shown[0].tag,'duty-n1');assert.equal(shown[1].tag,undefined);assert.equal(shown[0].icon,'/api/pwa/apple-touch-icon.png');
assert.deepEqual(badges,[3,0]);
const click=(data,action='')=>fire('notificationclick',{action,notification:{data,close(){}}});
await click({url:origin+'/#requests'});assert.deepEqual(opened,[origin+'/#requests']);
tabs=[{url:origin+'/#home',postMessage:m=>posted.push(m),focus:async()=>'focused'},{url:'https://other.test/',postMessage:()=>assert.fail('foreign tab')}];
await click({url:origin+'/#schedule',actions:{review:origin+'/#requests'}},'review');
assert.deepEqual(JSON.parse(JSON.stringify(posted)),[{type:'fmuc-open',url:origin+'/#requests'}]);assert.equal(opened.length,1);
console.log('ok');
'''
    out = subprocess.run(['node', '--input-type=module', '-e', script, str(web / 'sw.js')], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr
