"""Route lookups and the feedback reporter's page labels use the real compiled client in Node."""
import json

from .test_client_app_fragments import cut, node


def test_route_maps_have_no_inherited_entries_and_keep_real_routes(client_modules):
    got = node(f"const r=await import({json.dumps((client_modules / 'routes.js').as_uri())});" + r"""
const context={today:'2026-11-17',planner:true,publication:'version-1'};
const parsed=hash=>{const route=r.parseRoute(hash,context);return {...route,query:[...route.query]};};
const unknown=['constructor','__proto__','hasOwnProperty','toString'];
console.log(JSON.stringify({
  nullPrototypes:[r.ROUTE_ALIASES,r.PAGE_TABS].map(map=>Object.getPrototypeOf(map)===null),
  absent:unknown.map(key=>r.ROUTE_ALIASES[key]===undefined&&r.PAGE_TABS[key]===undefined),
  unknown:unknown.map(key=>parsed('#'+key)),
  aliases:['#overview','#directory','#planner/draft-1/person.rota?mode=edit',
    '#timeline','#calendar','#published'].map(parsed),
  tabs:Object.fromEntries(Object.entries(r.PAGE_TABS).map(([page,tabs])=>[page,tabs.map(tab=>tab.key)]))
}));""")
    assert got['nullPrototypes'] == [True, True]
    assert got['absent'] == [True] * 4
    assert got['unknown'] == [
        {'page': key, 'parts': [], 'query': []}
        for key in ['constructor', '__proto__', 'hasOwnProperty', 'toString']
    ]
    assert got['aliases'] == [
        {'page': 'home', 'parts': [], 'query': []},
        {'page': 'people', 'parts': [], 'query': []},
        {'page': 'schedule', 'parts': ['people'], 'query': [['edit', 'draft-1'], ['mode', 'edit']]},
        {'page': 'schedule', 'parts': ['year', '2026-11-17'], 'query': []},
        {'page': 'schedule', 'parts': ['people', '2026-11-17'], 'query': [['leave', '1']]},
        {'page': 'schedule', 'parts': ['month'], 'query': [['source', 'version-1']]},
    ]
    assert got['tabs'] == {
        'insights': ['fairness', 'workload', 'exports'],
        'access': ['people', 'routes', 'history'],
        'system': ['imports', 'bulk', 'operations', 'audit', 'feedback'],
    }


def test_feedback_page_label_keeps_unknown_words_and_known_labels(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    paths = cut(source, r'\n(const paths = withoutPrototype\(\{.*?\n\}\);)\n')
    reporter = cut(source, r'\n(installFeedback\(\{.*?\n\s*\} \}\);)\n')
    got = node(f"const {{parseRoute,PAGE_TABS,withoutPrototype}}=await import({json.dumps((client_modules / 'routes.js').as_uri())});"
               f"const {{CAPABILITY}}=await import({json.dumps((client_modules / 'dom.js').as_uri())});"
               f"const {{TERMS}}=await import({json.dumps((client_modules / 'terms.js').as_uri())});" + r"""
const SECTION_PERMISSION={},navigate=()=>{};let host;
const installFeedback=h=>{host=h;};
""" + paths + reporter + r"""
console.log(JSON.stringify({
  nullPrototype:Object.getPrototypeOf(paths)===null,
  unknown:['constructor','__proto__','hasOwnProperty','toString'].flatMap(key=>
    ['#'+key,'/'+key,key].map(route=>host.pageLabel(route))),
  known:['','#overview','/directory','#planner','#system/audit','#insights/fairness'].map(route=>host.pageLabel(route))
}));""")
    assert got['nullPrototype'] is True
    assert got['unknown'] == [key for key in ['constructor', '__proto__', 'hasOwnProperty', 'toString'] for _ in range(3)]
    assert got['known'] == ['Home', 'Home', 'People', 'Schedule', 'System › Audit log', 'Insights › Fairness']
