"""The client's findings walk (findings.ts plainFindings) leaves out only keys that never hold a finding. Over a checked
rota at department scale and the other replies that carry findings, it returns the same reply and changes the same
values as the walk over every key, in Node."""
from datetime import timedelta
import json
import subprocess
from zoneinfo import ZoneInfo

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import planning
from packages.domain.time import aware
from tests.test_backend_scale import scale_department
from tests.test_block_quotas import quota
from tests.test_contextual_feasibility import shortage_review, use_checked_activation

# Each reply walked twice: as sent (narrow) and with no key left out (whole), listing every value each walk set.
SCRIPT = r"""
import {readFileSync} from 'node:fs';
import {isDeepStrictEqual} from 'node:util';
globalThis.document={cookie:''};
const m=await import(process.argv[1]);
const changes=(before,after,path,out)=>{
  if(after===null||typeof after!=='object'){if(!Object.is(before,after))out.push(path+'='+JSON.stringify(after??null));return out;}
  for(const key of Object.keys(after)){const at=path+'.'+key;
    if(before===null||typeof before!=='object'||!(key in before))out.push(at+' added='+JSON.stringify(after[key]??null));
    else changes(before[key],after[key],at,out);}
  return out;
};
// The walk's work: how many objects it lists the keys of.
const opened=value=>{let count=0;const proxies=new WeakMap();
  const wrap=v=>{if(!v||typeof v!=='object')return v;let p=proxies.get(v);
    if(!p){p=new Proxy(v,{ownKeys:t=>{count++;return Reflect.ownKeys(t);},get:(t,k,r)=>wrap(Reflect.get(t,k,r))});proxies.set(v,p);}return p;};
  m.plainFindings(wrap(value));return count;};
const result={};
for(const [name,file] of Object.entries(JSON.parse(process.argv[2]))){
  const text=readFileSync(file,'utf8'),left=[...m.NO_FINDINGS];
  const narrow=m.plainFindings(JSON.parse(text)),work=opened(JSON.parse(text));
  m.NO_FINDINGS.clear();
  const whole=m.plainFindings(JSON.parse(text)),wholeWork=opened(JSON.parse(text));
  left.forEach(key=>m.NO_FINDINGS.add(key));
  const sent=JSON.parse(text);
  result[name]={equal:isDeepStrictEqual(narrow,whole),narrow:changes(sent,narrow,'$',[]),whole:changes(sent,whole,'$',[]),
    opened:work,wholeOpened:wholeWork};
}
console.log(JSON.stringify(result));
"""


def checked_rota(h):
    """The scale department's draft, checked, with its sessions from templates and every kind of finding the walk words:
    a cover gap, a double booking, a leave clash and a rule window before the rota."""
    w, data, _, _ = scale_department(h)
    person = next(iter(data['people']))
    quota(h, scope={'person_id': person}, starts_on='2026-09-01', ends_on='2026-12-01', window_days=28, amount=0)
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == w['id']).order_by(m.Shift.start, m.Shift.id)))
        templates = {t.id: t for t in db.scalars(select(m.RecurringSeries).where(m.RecurringSeries.name.like('Scale template %')))}
        by_person = {p: t for t in templates.values() for p in t.definition['default_person_ids']}
        assigned = {}
        for a in db.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == w['id'])):
            assigned.setdefault(a.shift_id, []).append(a)
        zone = ZoneInfo(db.get(m.Organisation, h.seed['organisation_id']).timezone)
        for shift in shifts:
            template = by_person[assigned[shift.id][0].person_id]
            shift.series_id, shift.series_revision, shift.series_snapshot = template.id, template.revision, record(template)
            shift.occurrence_on = aware(shift.start).astimezone(zone).date()
        gap, clash, other, away = shifts[0], shifts[1], shifts[2], shifts[3]
        db.delete(assigned[gap.id][0])
        db.add(m.Assignment(shift_id=other.id, person_id=assigned[clash.id][0].person_id, start=other.start, end=other.end))
        db.add(m.Availability(person_id=assigned[away.id][0].person_id, department_id=h.seed['department_id'],
                              start=away.start - timedelta(hours=1), end=away.end + timedelta(hours=1), kind='recorded_leave',
                              private_reason='Synthetic leave', recorded_by=h.seed['identities']['coordinator']))
        organisation = db.get(m.Organisation, h.seed['organisation_id'])
        organisation.availability_revision += 1
        db.flush()
        planning.revalidate(db, workspace)
    return w['id']


def walked(client_modules, tmp_path, replies):
    """Each reply walked as sent and walked whole: both results, the values each changed and the objects each opened."""
    files = {}
    for name, body in replies.items():
        files[name] = tmp_path / f'{name}.json'
        files[name].write_bytes(body)
    script = ['node', '--input-type=module', '-e', SCRIPT, (client_modules / 'findings.js').as_uri(),
              json.dumps({name: str(file) for name, file in files.items()})]
    result = json.loads(subprocess.run(script, capture_output=True, text=True, check=True).stdout)
    for name, walk in result.items():
        assert walk['equal'], name
        assert walk['narrow'] == walk['whole'], name
    return result


def test_findings_walk_skips_only_keys_without_findings(h, client_modules, tmp_path):
    wid = checked_rota(h)
    client = h.client('coordinator')
    paths = {'rota': f'/api/workspaces/{wid}', 'fix_list': f'/api/workspaces/{wid}/fix-list',
             'fix_list_lean': f'/api/workspaces/{wid}/fix-list?view=lean', 'summary': f'/api/workspaces/{wid}/summary',
             'causes': f'/api/workspaces/{wid}/summary?view=causes', 'home': '/api/home',
             'home_rotas': '/api/home/rotas?start=2026-09-01&end=2026-12-01&limit=100',
             'requests': '/api/requests/all?limit=100', 'wishes': '/api/wishes?start=2026-09-01&end=2026-12-01'}
    replies = {}
    for name, path in paths.items():
        response = client.get(path)
        assert response.status_code == 200, (name, response.text[:300])
        replies[name] = response.content
    findings = json.loads(replies['rota'])['workspace']['validation']['findings']
    assert {'COVERAGE_GAP', 'TIME_OVERLAP', 'UNAVAILABLE'} <= {f['code'] for f in findings}
    assert any(f.get('outside_rota') for f in findings)
    result = walked(client_modules, tmp_path, replies)
    # The walk did its work on the rota: plain words for its findings and the look-back marked.
    rota = result['rota']['narrow']
    assert sum(c.startswith('$.workspace.validation.findings.') and '.technical added=' in c for c in rota) >= 3
    assert any(c.endswith('.reach added="before"') for c in rota)
    # Budget: the walk opens 3,329 of the rota's 17,104 objects (measured); every read of the rota pays for it.
    assert result['rota']['opened'] <= 4000, (result['rota']['opened'], result['rota']['wholeOpened'])


def test_rule_check_replies_walk_the_same(h, client_modules, tmp_path):
    """A rule check's job keeps its findings under checks.baseline and checks.proposed (never left out)."""
    use_checked_activation(h)
    _, _, job = shortage_review(h)
    assert any(d['code'] == 'INSUFFICIENT_WHOLE_DUTY_CANDIDATES' for d in job['result']['checks']['baseline']['diagnostics'])
    full = h.client('approver').get(f"/api/jobs/{job['id']}", params={'full': 'true'})
    check = h.client('coordinator').post('/api/policies/check', json={'definition': {
        'rule': {'kind': 'min_rest', 'hours': 12}, 'scope': {}, 'severity': 'mandatory'}})
    assert full.status_code == check.status_code == 200, (full.text[:300], check.text[:300])
    job_reply = h.client('approver').get(f"/api/jobs/{job['id']}")
    result = walked(client_modules, tmp_path, {'job': job_reply.content, 'job_full': full.content,
                                               'policy_check': check.content})
    assert any('.checks.baseline.' in c and '.technical added=' in c for c in result['job']['narrow'])


TEMPLATE_COPY = r"""
globalThis.document={cookie:''};
const m=await import(process.argv[1]);
// A department's own custom fields that happen to be named like a finding's keys, kept in the session's template copies.
const own={code:'COVERAGE_GAP',message:'Our own words',severity:'error'};
const reply={workspace:{starts_on:'2026-10-01',ends_on:'2026-11-01',validation:{findings:[
  {code:'COVERAGE_GAP',message:'raw',severity:'error',shift_id:'s1'}]}},
  shifts:[{id:'s1',series_snapshot:{definition:{attributes:{...own}}},type_snapshot:{definition:{attributes:{...own}}}}]};
m.plainFindings(reply);
console.log(JSON.stringify({finding:reply.workspace.validation.findings[0],series:reply.shifts[0].series_snapshot.definition.attributes,
  type:reply.shifts[0].type_snapshot.definition.attributes,own}));
"""


def test_template_custom_fields_named_like_a_finding_are_never_rewritten(client_modules):
    """Fix: a department's custom field that looked like a finding (code, message and severity) was rewritten in the
    session's template copy; it no longer is, while the reply's real findings still get their plain words."""
    out = json.loads(subprocess.run(['node', '--input-type=module', '-e', TEMPLATE_COPY,
                                     (client_modules / 'findings.js').as_uri()], capture_output=True, text=True, check=True).stdout)
    assert out['finding']['technical'] == 'raw' and out['finding']['message'] != 'raw'
    assert out['series'] == out['own'] and out['type'] == out['own']
