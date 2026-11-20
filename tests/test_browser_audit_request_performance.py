"""Focused audit regression: reading more requests must not repeat prior pages or hide searchable history."""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from packages.web_assets import bundle_directory
from .test_browser import browser, live_server, expect_loaded, navigate, sign_in, toolbar, COUNT_WORK  # noqa: F401
from .test_browser import whole_list

pytestmark = pytest.mark.browser


def receipt(name, result):
    """Optional audit artifacts; normal test runs never overwrite an earlier candidate's evidence."""
    root = Path(__file__).resolve().parents[1]
    bundle = bundle_directory(root / 'apps/web/dist')
    paths = [bundle / name for name in ('requests.js', 'table_view.js', 'rules_tree.js')]
    paths.append(root / 'apps/api/services/availability.py')
    result['artifact_sha256'] = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    print(json.dumps(result, indent=2))
    if output := os.getenv('AUDIT_PERFORMANCE_RECEIPTS'):
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name + '.json')).write_text(json.dumps(result, indent=2), encoding='utf-8')


def test_show_more_does_not_reread_history_and_search_reaches_unloaded_rows(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Audit scale person {n:03d}')
                for n in range(150 - len(h.seed['people']))]
        db.add_all(made)
        db.flush()
        everyone = [*h.seed['people'], *(p.id for p in made)]
        target_id, target_name = made[-1].id, made[-1].display_name
        for n, pid in enumerate(everyone):
            start = datetime(2020 if pid == target_id else 2024, 1, 1, tzinfo=timezone.utc)
            for k in range(20):
                at = start + timedelta(days=41 * k + n % 30)
                db.add(m.RequestRecord(department_id=h.seed['department_id'], person_id=pid, kind='leave',
                                       start=at, end=at + timedelta(days=1), private_reason='Audit scale fixture',
                                       status='approved', created_by=h.seed['identities']['coordinator']))
    context, page = sign_in(browser, live_server, 'approver')
    responses, timings = [], []
    page.on('response', lambda response: responses.append(response)
            if urlparse(response.url).path == '/api/requests/all'
            and 'offset' in parse_qs(urlparse(response.url).query)
            # Not the other views' one-row count read while Waiting for you is shown (requests.ts everyStages).
            and parse_qs(urlparse(response.url).query).get('limit') != ['1'] else None)
    try:
        page.evaluate("""() => { window.auditLongTasks = [];
            if (PerformanceObserver.supportedEntryTypes.includes('longtask')) {
                window.auditObserver = new PerformanceObserver(list => window.auditLongTasks.push(...list.getEntries().map(e => e.duration)));
                window.auditObserver.observe({type:'longtask'});
            }
        }""")
        began = time.perf_counter()
        navigate(page, 'requests')
        expect(page.locator('#main .dt-saved')).to_contain_text('Waiting for you')
        responses.clear()  # measured from the whole list (the first read was the empty Waiting for you)
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.get_by_text('Showing 100 of 3000', exact=True)).to_be_visible()
        timings.append({'shown': 100, 'milliseconds': round((time.perf_counter() - began) * 1000, 1)})
        for count in (200, 300, 400, 500):
            began = time.perf_counter()
            page.get_by_role('button', name='Show more', exact=True).click()
            expect(page.get_by_text(f'Showing {count} of 3000', exact=True)).to_be_visible()
            timings.append({'shown': count, 'milliseconds': round((time.perf_counter() - began) * 1000, 1)})
        packets = []
        for response in responses:
            data = response.json()
            packets.append({'url': urlparse(response.url).path + '?' + urlparse(response.url).query,
                            'status': response.status, 'ids': [r['id'] for r in data['items']],
                            'rows': len(data['items']), 'bytes': len(response.body())})
        all_ids = [identifier for packet in packets for identifier in packet['ids']]
        read_duplicates = len(all_ids) - len(set(all_ids))
        # The API confirms these 20 rows exist and are authorized for this reviewer.
        expected = h.get('approver', '/api/requests/all?person_id=' + target_id)
        assert expected['total'] == 20
        assert target_id not in [row['person_id'] for response in responses for row in response.json()['items']]
        page.get_by_role('searchbox', name='Search requests', exact=True).fill(target_name)
        try:
            expect(page.locator('#main tr.dt-open')).to_have_count(20, timeout=5000)
        except AssertionError:
            pass  # Keep the full failure union and its evidence below.
        found = page.locator('#main tr.dt-open').count()
        empty = page.get_by_role('heading', name='No matching requests', exact=True).count()
        result = {'runtime': 'current-source local real API and Chromium; disposable SQLite fixture',
                  'total_requests': 3000, 'people': 150, 'timings': timings,
                  'packets': [{key: value for key, value in p.items() if key != 'ids'} for p in packets],
                  'rows_transferred': len(all_ids), 'unique_rows': len(set(all_ids)), 'duplicate_rows': read_duplicates,
                  'unloaded_search_expected': 20, 'unloaded_search_visible_rows': found,
                  'unloaded_search_no_matching_message': bool(empty),
                  'long_tasks_ms': page.evaluate('window.auditLongTasks')}
        receipt('request-performance', result)
        failures = []
        if read_duplicates:
            failures.append(f'Show more transferred {read_duplicates} previously loaded rows out of {len(all_ids)} rows')
        if found != 20:
            failures.append(f'Search found {found} of 20 authorized matching requests outside the loaded page')
        assert not failures, '; '.join(failures)
        search = page.get_by_role('searchbox', name='Search requests', exact=True)
        search.fill('audit no such person or note')
        expect(page.locator('#main tr.dt-open')).to_have_count(0)
        expect(search).to_be_visible()  # A remote empty answer must leave a way to clear the query.
        search.fill('Audit scale person')
        expect(page.locator('#main tr.dt-open')).to_have_count(100)
        expect(page.get_by_text(f'Showing 100 of {len(made) * 20}', exact=True)).to_be_visible()
        names = page.locator('#main tr.dt-open td[data-col=person]').all_text_contents()
        assert len(set(names)) > 1, names  # Person remains a meaningful column after an empty-result reload.
        search.fill(target_name)
        expect(page.locator('#main tr.dt-open')).to_have_count(20)
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Older requests for one person')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        saved = h.get('approver', '/api/table-views?table=requests')['views']
        assert next(view for view in saved if view['name'] == 'Older requests for one person')['definition']['q'] == target_name
        page.reload()
        expect_loaded(page, 'requests')
        expect(page.get_by_role('searchbox', name='Search requests', exact=True)).to_have_value(target_name)
        expect(page.locator('#main tr.dt-open')).to_have_count(20)
    finally:
        context.close()


def test_inherited_rule_lines_do_not_block_the_catalogue(browser, live_server, h):
    """Isolated renderer diagnostic using actual built modules and a representative inherited-line payload."""
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        result = page.evaluate("""async () => {
            const countWork = """ + COUNT_WORK + """;
            const [catalogue, tree, dom, me, catalogues, source] = await Promise.all([
                window.importBundle('rule_catalogue.js'), window.importBundle('rules_tree.js'), window.importBundle('dom.js'),
                fetch('/api/me').then(r=>r.json()), fetch('/api/catalogues').then(r=>r.json()),
                fetch('/api/rules/catalogue').then(r=>r.json())
            ]);
            const original = source.entries.find(e=>e.id==='duties_per_week');
            // The app's own gate (app.ts permits): the page asks the context what this person may do (#656).
            const ctx = {me,catalogues,refresh:async()=>{},navigate:()=>{},setWorkspace:()=>{},
                permits:(...keys)=>keys.some(key=>dom.hasAccess(me.roles,key))};
            // Open the actual control once, outside the measurements. Its account/session-scoped state then
            // applies to every timed render without depending on the browser storage format.
            const initial=document.createElement('div');document.querySelector('#main').replaceChildren(initial);
            tree.rulesDatabase(ctx,initial,{...source,entries:[original],attention:[]},
                {parts:[],openTag:()=>{},navigate:()=>{}},true,async()=>{});
            const toggle=initial.querySelector('[data-key="toggle:duties_per_week"]');
            if(!toggle)throw new Error('The fixture rule must have an expansion control');
            if(toggle.getAttribute('aria-expanded')==='false')toggle.click();
            if(initial.querySelector('[data-key="toggle:duties_per_week"]').getAttribute('aria-expanded')!=='true')
                throw new Error('The fixture rule must be expanded before measuring its lines');
            const samples = [];
            for (const count of [250,500,1000]) {
                for (const inherited of [false,true]) {
                    const lines = Array.from({length:count},(_,i)=>({
                        id:'audit-line-'+i, numbers: i===0 ? {min:{value:1,level:'mandatory'}} :
                            {max:{value:5,level:'mandatory'}},
                        who:i ? {conditions:[{field:'person_ids',values:[catalogues.people[i%catalogues.people.length].id]}]} : null,
                        starts_on:'2027-01-01',ends_on:'2028-01-01',note:'Isolated renderer audit fixture '+i
                    }));
                    const entry = {...original,proposed:null,overlaps:0,line_overlaps:{},reach:{},
                        inherited: inherited ? Object.fromEntries(lines.slice(1).map(l=>[l.id,{min:{value:1,level:'mandatory',from:lines[0].id}}])) : {},
                        rule:{id:'audit-renderer-rule',revision:1,definition:{rule:{on:true,lines,settings:{}}}}
                    };
                    const host=document.createElement('div');document.querySelector('#main').replaceChildren(host);
                    const work=countWork(()=>tree.rulesDatabase(ctx,host,{...source,entries:[entry],attention:[]},
                        {parts:[],openTag:()=>{},navigate:()=>{}},true,async()=>{}));
                    const inheritedCells=[...host.querySelectorAll('.rc-inh')];
                    samples.push({count,inherited,milliseconds:work.ms,created:work.created,layout:work.layout,rows:host.querySelectorAll('li.rt-ln').length,
                        inheritedCells:inheritedCells.length,
                        firstInheritedCell:inheritedCells.length ? {text:inheritedCells[0].textContent,
                            tooltip:inheritedCells[0].getAttribute('data-tip')} : null,
                        correctInheritedValues:inheritedCells.every(cell=>cell.textContent==='1↰' &&
                            cell.getAttribute('data-tip')==='Inherited from Everyone · All clinics')});
                    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
                }
            }
            return {runtime:'current-source real Chromium; isolated renderer with synthetic line payload, no saved rule changes',samples};
        }""")
        receipt('rule-inheritance-performance', result)
        assert all(s['rows'] == s['count'] for s in result['samples'])
        assert all(s['correctInheritedValues'] for s in result['samples'])
        last = result['samples'][-1]
        assert last['inheritedCells'] == 999
        # The work counted, not timed (856 ms against a 600 ms wall-clock budget failed with nothing changed, maintenance): no
        # layout read while drawing, and elements that grow with the lines, inherited values costing no more than others.
        assert all(s['layout'] == [] for s in result['samples']), [s['layout'] for s in result['samples']]
        plain = next(s for s in result['samples'] if s['count'] == last['count'] and not s['inherited'])
        assert last['created'] <= last['count'] * 30 and last['created'] <= plain['created'] * 1.1, (last['created'], plain['created'])
    finally:
        context.close()
