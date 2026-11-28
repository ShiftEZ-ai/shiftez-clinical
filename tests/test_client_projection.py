"""Execute the real compiled TypeScript projection functions in Node, not a browser.

These are logic tests only: they do not claim DOM, keyboard or visual qualification.
"""
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def js(client_modules, expression, payload):
    script=f"const m=await import({json.dumps((client_modules/'planner_logic.js').as_uri())});const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));"
    result=subprocess.run(['node','--input-type=module','-e',script,json.dumps(payload)],
        capture_output=True,text=True,check=True,timeout=15)
    return json.loads(result.stdout)


@pytest.mark.parametrize('day,expected',[
    ('2026-10-03',False),('2026-10-04',True),('2026-10-05',True),('2026-10-06',False)])
def test_overnight_sessions_appear_on_each_actual_local_date(client_modules,day,expected):
    shift={'start':'2026-10-04T22:00:00+03:00','end':'2026-10-05T02:00:00+03:00'}
    assert js(client_modules,'m.touchesDay(input.shift,input.day,"Asia/Riyadh")',{'shift':shift,'day':day}) is expected


def test_midnight_exclusive_end_does_not_invent_a_second_calendar_day(client_modules):
    shift={'start':'2026-10-04T20:00:00+03:00','end':'2026-10-05T00:00:00+03:00'}
    assert not js(client_modules,'m.touchesDay(input,"2026-10-05","Asia/Riyadh")',shift)


def test_month_is_the_same_day_columns_trimmed_to_the_rota(client_modules):
    # A month uses the week grid: one column per rota day in that month, nothing padded outside the rota.
    result=js(client_modules,'m.calendarPeriod(input,"month",0,6)',{'starts_on':'2026-10-04','ends_on':'2026-10-11'})
    assert result['days'][0]=='2026-10-04' and result['days'][-1]=='2026-10-10' and len(result['days'])==7
    assert not result['has_next'] and not result['has_previous'] and result['unit']=='month'
    full=js(client_modules,'m.calendarPeriod(input,"month",0,6)',{'starts_on':'2026-10-01','ends_on':'2026-12-01'})
    assert len(full['days'])==31 and full['has_next']


def test_other_ranges_and_switching_keeps_the_same_dates(client_modules):
    w={'starts_on':'2026-10-01','ends_on':'2026-12-01'}
    assert js(client_modules,'m.calendarPeriod(input,"day",3)',w)['days']==['2026-10-04']
    two=js(client_modules,'m.calendarPeriod(input,"2weeks",1)',w)
    assert len(two['days'])==14 and two['days'][0]=='2026-10-11'
    assert len(js(client_modules,'m.calendarPeriod(input,"rota",5)',w)['days'])==61
    assert js(client_modules,'m.pageOf(input,"month","2026-11-15")',w)==1
    assert js(client_modules,'m.pageOf(input,"week","2026-10-12")',w)==2


def test_month_navigation_handles_year_boundary_and_exclusive_end(client_modules):
    w={'starts_on':'2026-12-20','ends_on':'2027-02-01'}
    last=js(client_modules,'m.calendarPeriod(input,"month",99)',w)
    assert last['page']==1 and last['label']=='2027-01' and not last['has_next']
    first=js(client_modules,'m.calendarPeriod(input,"month",-2)',w)
    assert first['page']==0 and first['label']=='2026-12' and first['has_next']


def test_week_projection_is_bounded_to_workspace_and_not_off_by_one(client_modules):
    w={'starts_on':'2026-10-04','ends_on':'2026-10-12'}
    last=js(client_modules,'m.calendarPeriod(input,"week",999)',w)
    assert last['days']==['2026-10-11'] and last['page']==1 and not last['has_next']


def test_weeks_follow_the_first_weekday_when_a_rota_starts_mid_week(client_modules):
    # 1 Dec 2026 is a Tuesday; weeks run Sunday to Saturday, trimmed to the rota.
    w={'starts_on':'2026-12-01','ends_on':'2026-12-15'}
    first=js(client_modules,'m.calendarPeriod(input,"week",0)',w)
    second=js(client_modules,'m.calendarPeriod(input,"week",1)',w)
    third=js(client_modules,'m.calendarPeriod(input,"week",2)',w)
    assert first['days'][0]=='2026-12-01' and first['days'][-1]=='2026-12-05'
    assert second['days'][0]=='2026-12-06' and len(second['days'])==7
    assert third['days']==['2026-12-13','2026-12-14'] and not third['has_next']


@pytest.mark.parametrize('filters,count',[
    ({},2),({'search':'staff 01'},1),({'search':'CONTINUITY'},1),({'search':'satellite'},1),
    ({'person_id':'p2'},1),({'location_id':'l1'},1),({'assignment_type_id':'t2'},1),
    ({'location_id':'l1','person_id':'p2'},0),({'search':'private explanation'},0)])
def test_actual_filter_function_consumes_each_supported_saved_setting(client_modules,filters,count):
    value={'filters':filters,'shifts':[
        {'id':'a','location_id':'l1','assignment_type_id':'t1','type_snapshot':{'name':'Clinic'},'service_ids':['s1'],'participants':[{'person_id':'p1'}]},
        {'id':'b','location_id':'l2','assignment_type_id':'t2','type_snapshot':{'name':'Education'},'service_ids':['s2'],'participants':[{'person_id':'p2'}]}],
        'catalogues':{'locations':[{'id':'l1','name':'Main room'},{'id':'l2','name':'Satellite'}],
            'people':[{'id':'p1','display_name':'Staff 01(test)'},{'id':'p2','display_name':'Staff 02(test)'}],
            'services':[{'id':'s1','name':'Continuity'},{'id':'s2','name':'Academic'}]}}
    assert len(js(client_modules,'m.filterShifts(input.shifts,input.catalogues,input.filters)',value))==count


@pytest.mark.parametrize('path,method,code',[
    ('/api/me','GET','READ_UNAVAILABLE'),('/api/planner/preview','POST','PREVIEW_NOT_RECEIVED'),('/api/catalogue/preview','POST','PREVIEW_NOT_RECEIVED'),
    ('/api/planner/staffing-suggestions','POST','PREVIEW_NOT_RECEIVED'),
    ('/api/commands','POST','NETWORK_UNCERTAIN')])
def test_transport_errors_do_not_mislabel_a_read_or_preview_as_a_committed_save(client_modules,path,method,code):
    script=f"""globalThis.document={{cookie:'rota_csrf=fictional'}};
      globalThis.fetch=async()=>{{throw new TypeError('Synthetic network failure')}};
      const m=await import({json.dumps((client_modules/'api.js').as_uri())});
      try{{await m.request({json.dumps(path)},{{method:{json.dumps(method)}}});}}
      catch(error){{console.log(JSON.stringify({{code:error.code,status:error.status}}));}}"""
    result=subprocess.run(['node','--input-type=module','-e',script],
        capture_output=True,text=True,check=True,timeout=15)
    assert json.loads(result.stdout)=={'code':code,'status':0}


def test_a_lost_write_response_is_uncertain_even_when_the_browser_says_offline(client_modules):
    """navigator.onLine read after a failed fetch cannot prove nothing was sent: the server committed this write and only
    its response was lost. The command is sent again with its idempotency key and gets the committed result (one write,
    QA-091: tests/test_client_command_confirm.py), and a read still says it could not reach the server."""
    script=f"""globalThis.document={{cookie:'rota_csrf=fictional',createElement:()=>({{classList:{{contains:()=>false,add(){{}},remove(){{}}}},setAttribute(){{}}}})}};
      Object.defineProperty(globalThis,'navigator',{{value:{{onLine:false}},configurable:true}});
      const committed=new Map(),keys=[];
      globalThis.fetch=async(path,options)=>{{
        if(!options.method)throw new TypeError('Synthetic network failure');
        const key=new Headers(options.headers).get('Idempotency-Key');keys.push(key);
        const first=!committed.has(key);if(first)committed.set(key,{{feed:{{id:'committed-feed'}}}});
        if(first)throw new TypeError('Response lost after the commit');
        return new Response(JSON.stringify(committed.get(key)),{{headers:{{'Content-Type':'application/json'}}}});}};
      const m=await import({json.dumps((client_modules/'api.js').as_uri())});
      globalThis.window=Object.assign(new EventTarget(),{{setTimeout,clearTimeout}});  // after the import, as above: no page wiring
      const retried=await m.command('calendar_feed.create',{{}});
      const read=await m.request('/api/me').then(()=>null,e=>e.code);
      console.log(JSON.stringify({{retried,keys,writes:committed.size,read}}));"""
    out=json.loads(subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True,timeout=15).stdout)
    assert len(out['keys'])==2 and out['keys'][0]==out['keys'][1] and out['writes']==1
    assert out['retried']=={'feed':{'id':'committed-feed'}}  # the committed outcome, not a second feed
    assert out['read']=='READ_UNAVAILABLE'


def test_rule_catalogue_busy_is_a_failed_read_never_data_nor_a_down_server(client_modules):
    """A real 503 RULE_CATALOGUE_BUSY takes the normal error path (the page says it could not load and offers Retry):
    never data, not retried by itself, not a problem to report, and it does not flip the connection health banner."""
    script=f"""globalThis.document={{cookie:''}};
      let calls=0;
      globalThis.fetch=async()=>{{calls++;return new Response(JSON.stringify({{error:{{code:'RULE_CATALOGUE_BUSY',message:'The rule catalogue is still updating; try again'}}}}),{{status:503,headers:{{'Retry-After':'2','X-Correlation-ID':'busy'}}}});}};
      const api=await import({json.dumps((client_modules/'api.js').as_uri())});
      const failed=await api.request('/api/rules/catalogue').then(()=>null,e=>({{code:e.code,status:e.status,report:api.unexpectedError(e)}}));
      console.log(JSON.stringify({{failed,calls,health:api.serverHealth()}}));"""
    result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True,timeout=15)
    assert json.loads(result.stdout)=={'failed':{'code':'RULE_CATALOGUE_BUSY','status':503,'report':False},'calls':1,'health':'ok'}


@pytest.mark.parametrize('boundary', ['clearSessionState', 'logout'])
def test_feedback_diagnostics_are_isolated_when_the_signed_in_account_changes(client_modules,boundary):
    script=f"""globalThis.document={{cookie:''}};
      const api=await import({json.dumps((client_modules/'api.js').as_uri())});
      const feedback=await import({json.dumps((client_modules/'feedback_context.js').as_uri())});
      globalThis.window={{clearTimeout}};
      const snapshot=()=>({{errors:feedback.recentErrors().map(e=>e.message),requests:feedback.recentRequests()}});
      feedback.recordError('Previous account private diagnostic','toast');feedback.recordRequest('previous-account-request');
      let finish;
      globalThis.fetch=async path=>path==='/auth/logout'
        ?new Response('{{}}',{{headers:{{'X-Correlation-ID':'previous-account-logout'}}}})
        :new Promise(resolve=>{{finish=resolve;}});
      const pending=api.request('/api/me').catch(error=>error.code);
      await api.{boundary}();const cleared=snapshot();
      finish(new Response('{{}}',{{headers:{{'X-Correlation-ID':'previous-account-late-request'}}}}));
      const discarded=await pending,afterLate=snapshot();
      feedback.recordError('Current account diagnostic','toast');
      globalThis.fetch=async()=>new Response('{{}}',{{headers:{{'X-Correlation-ID':'current-account-request'}}}});
      await api.request('/api/me');
      console.log(JSON.stringify({{cleared,discarded,afterLate,current:snapshot()}}));"""
    result=json.loads(subprocess.run(['node','--input-type=module','-e',script],
        capture_output=True,text=True,check=True,timeout=15).stdout)
    assert result['cleared']=={'errors':[],'requests':[]}
    assert result['discarded']=='SESSION_CHANGED'
    assert result['afterLate']=={'errors':[],'requests':[]}
    assert result['current']=={'errors':['Current account diagnostic'],'requests':['current-account-request']}


@pytest.mark.parametrize('sign_in', ['login', 'oidcChoose'])
def test_a_failed_sign_out_then_another_account_signing_in_starts_with_no_diagnostics(client_modules,sign_in):
    """A shared device: the sign-out fails (its trace stays, the session did not end), then a different person signs in.
    Any change of the signed-in identity clears the feedback context, not only a sign-out that ended."""
    call={'login':"api.login('other.person','synthetic')",'oidcChoose':"api.oidcChoose('continuation','department')"}[sign_in]
    script=f"""globalThis.document={{cookie:''}};
      const api=await import({json.dumps((client_modules/'api.js').as_uri())});
      const feedback=await import({json.dumps((client_modules/'feedback_context.js').as_uri())});
      globalThis.window={{clearTimeout}};
      const snapshot=()=>({{errors:feedback.recentErrors().map(e=>e.message),requests:feedback.recentRequests()}});
      globalThis.fetch=async path=>path==='/auth/logout'
        ?new Response('{{"error":{{"code":"INTERNAL_ERROR","message":"Synthetic outage"}}}}',{{status:500,headers:{{'X-Correlation-ID':'previous-account-logout'}}}})
        :new Response('{{}}',{{headers:{{'X-Correlation-ID':'sign-in-request'}}}});
      const out=await api.logout();
      feedback.recordError('Previous account private diagnostic','toast');
      const kept=snapshot();
      await {call};
      console.log(JSON.stringify({{ended:out.ended,kept,after:snapshot()}}));"""
    result=json.loads(subprocess.run(['node','--input-type=module','-e',script],
        capture_output=True,text=True,check=True,timeout=15).stdout)
    assert result['ended'] is False
    # The session did not end: its trace stays (the failed sign-out itself and what the account had recorded).
    assert 'Previous account private diagnostic' in result['kept']['errors'] and result['kept']['requests']==['previous-account-logout']
    assert result['after']=={'errors':[],'requests':[]}




def test_duty_type_colours_are_distinct_chosen_ones_kept_and_stable(client_modules):
    """setTypeColours (the one duty-type colour assignment): a chosen colour stays; the others take the free hues one
    each in creation order (never two alike while hues are left); a type created later does not recolour older ones."""
    script=f"""const m=await import({json.dumps((client_modules/'dom.js').as_uri())});
      const t=(id,at,colour)=>({{id,created_at:'2026-01-0'+at,definition:colour?{{colour}}:{{}}}});
      const types=[t('d',4),t('a',1),t('b',2,'teal'),t('c',3),t('e',5),t('f',6),t('g',7)];
      m.setTypeColours(types);const first=types.map(x=>[x.id,m.typeColour(x.id)]);
      m.setTypeColours([...types,t('h',8)]);const later=types.map(x=>m.typeColour(x.id));
      m.setTypeColours([{{id:'s',created_at:'2026-01-01',colour:''}}]);const light=m.typeColour('s');
      console.log(JSON.stringify({{first,later,light,pal:m.PALETTE}}));"""
    out=json.loads(subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True,timeout=15).stdout)
    colours=dict(out['first'])
    assert colours['b']=='teal'
    autos=[colours[k] for k in 'acdefg']
    # Automatic hues skip slate and coral (data is never coral: coral reads as a problem, owner 21 Oct 2026), and teal
    # is chosen: five free hues, one each in creation order a, c, d, e, f; the sixth (g) reuses them from the start.
    free=[c for c in out['pal'] if c not in ('teal','slate','coral')]
    assert len(free)==5 and len(set(autos[:5]))==5 and not {'teal','slate','coral'}&set(autos)
    assert autos==free+free[:1]
    assert out['later']==[colours[k] for k in 'dabcefg']  # a newer type leaves the older ones alone
    assert out['light']=='teal'  # the light list from /api/me (colour field) gives the same first hue


def test_rank_colours_keep_chosen_and_take_their_own_palette_distinct_and_stable(client_modules):
    """rankColour (owner decision C21, DESIGN.md): a rank's chosen colour (the categories catalogue's `colour`) stays;
    ranks without one take the rank palette (RANK_PALETTE, never a duty, group, person, area or status hue: see
    tests/test_web_rank_hues.py) by their place among ALL ranks in creation order: twelve ranks never share a tint, the
    same after a reload (any list order), and unchanged when a newer rank is added or another is archived or chosen."""
    script=f"""const m=await import({json.dumps((client_modules/'dom.js').as_uri())});
      const r=(id,at,colour='')=>({{id,created_at:'2026-02-'+String(at).padStart(2,'0'),colour}});
      const ranks=[r('r4',4),r('r1',1,'amber'),r('r2',2),r('r3',3),r('r5',5,'coral'),r('r6',6)];
      m.setRankColours(ranks);const first=Object.fromEntries(ranks.map(x=>[x.id,m.rankColour(x.id)]));
      m.setRankColours([...ranks].reverse());const reloaded=Object.fromEntries(ranks.map(x=>[x.id,m.rankColour(x.id)]));
      m.setRankColours([...ranks,r('r7',7)]);const later=Object.fromEntries(ranks.map(x=>[x.id,m.rankColour(x.id)]));
      m.setRankColours([...ranks.map(x=>x.id==='r1'?{{...x,colour:'pink'}}:x)]);const edited=m.rankColour('r1');
      m.setTypeColours([{{id:'r2',colour:'indigo'}}]);const apart=m.rankColour('r2');
      const twelve=Array.from({{length:12}},(_,i)=>r('t'+i,i+1));m.setRankColours([...twelve].reverse());
      const many=twelve.map(x=>m.rankColour(x.id));
      const three=[r('a1',1),r('a2',2),r('a3',3)],tint=()=>m.rankColour('a3');m.setRankColours(three);const before=tint();
      m.setRankColours(three.map(x=>x.id==='a2'?{{...x,active:false}}:x));const archived=tint();
      m.setRankColours(three.map(x=>x.id==='a2'?{{...x,colour:'teal'}}:x));const chosen=tint();
      console.log(JSON.stringify({{first,reloaded,later,edited,apart,many,before,archived,chosen,none:m.rankColour(''),unknown:m.rankColour('gone'),pal:m.PALETTE,ranks:m.RANK_PALETTE}}));"""
    out=json.loads(subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True,timeout=15).stdout)
    c=out['first']
    assert c['r1']=='amber' and c['r5']=='coral'  # an admin's chosen colour stays, coral included
    autos=[c[k] for k in ('r2','r3','r4','r6')]
    assert autos==[out['ranks'][i] for i in (1,2,3,5)]  # its place among all ranks (r1, r5 chosen still count)
    assert not set(out['ranks'])&set(out['pal'])  # the rank palette is its own, never a duty hue
    assert out['many']==out['ranks'][:12] and len(set(out['many']))==12  # twelve ranks, twelve tints
    assert out['reloaded']==c and out['later']==c  # stable across reloads; a newer rank leaves older ones alone
    assert out['edited']=='pink'  # a colour edit shows at once (set when the catalogue loads)
    assert out['apart']==c['r2']  # duty-type colours are a separate assignment
    assert out['none']=='slate'  # no rank: neutral
    assert out['unknown'] in out['ranks']  # an old record keeps a stable rank tint
    assert out['before']==out['archived']==out['chosen']==out['ranks'][2]  # archiving or colouring rank 2 leaves rank 3


def test_saved_layers_keep_only_changed_layers_with_the_contract_defaults(client_modules):
    from packages.contracts import schemas as s
    defaults = s.PlannerLayers().model_dump()
    assert js(client_modules, 'm.LAYER_DEFAULTS', None) == defaults  # the client's copy of the contract defaults
    # A view saved before layers were kept, or with every layer at its default, compares equal (null).
    for value in (None, {}, defaults, {'draft': None, 'mode': None}):
        assert js(client_modules, 'm.savedLayers(input)', value) is None
    assert js(client_modules, 'm.savedLayers(input)', {'mine': True}) == {**defaults, 'mine': True}
    assert js(client_modules, 'm.savedLayers(input)', {'draft': False, 'mode': 'overlay'}) == {**defaults, 'draft': False, 'mode': 'overlay'}
    # Whether a draft shows, never which one: no rota or publication id survives.
    kept = js(client_modules, 'm.savedLayers(input)', {'draft': True, 'leave': False, 'workspace_id': 'w1', 'publication_id': 'p1'})
    assert kept == {**defaults, 'leave': False}
    # Rotations (People by days) is kept like Leave: on by default, saved only when turned off.
    assert defaults['rotations'] is True
    assert js(client_modules, 'm.savedLayers(input)', {'rotations': False}) == {**defaults, 'rotations': False}
