"""Immediate search ownership and managed dialog cleanup, using public components and real app journeys."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .test_browser import browser, expect_loaded, live_server, navigate, planner_tool, sign_in  # noqa: F401
from .test_browser_qa_planner import checked_rota, settled
from .test_browser_requests import leave
from .test_browser_schedule_export import open_export

pytestmark = pytest.mark.browser
DISPLAYS = [(1440, 'light'), (390, 'dark')]


def capture(page, tmp_path, name):
    folder = Path(os.getenv('PW_TRACE_DIR', str(tmp_path))).parent / 'screenshots'
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / (name + '.png')), animations='disabled')


def pause_clock(page):
    # The canonical browser fixture already installed Playwright's clock before navigation.
    page.clock.pause_at(page.evaluate('new Date(Date.now()+1000).toISOString()'))


def test_toolbar_updates_state_now_applies_once_and_releases_detached_work(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        pause_clock(page)
        page.evaluate('''async()=>{const{collectionToolbar}=await window.importBundle('table_view.js');
            window.searchProbe={q:'',states:[],applied:[]};const s=window.searchProbe;
            window.mountSearch=()=>{const t=collectionToolbar({label:'Lifecycle search',search:{value:s.q,
                label:'Lifecycle query',wait:200,onInput:q=>{s.q=q;s.states.push(q);},apply:()=>s.applied.push(s.q)}});
                t.root.id='lifecycle-search';document.querySelector('#main').append(t.root);return t;};mountSearch();}''')
        search = page.get_by_role('searchbox', name='Lifecycle query', exact=True)
        search.press_sequentially('alpha')
        assert page.evaluate('searchProbe') == {'q': 'alpha', 'states': ['a', 'al', 'alp', 'alph', 'alpha'], 'applied': []}
        page.clock.run_for(200)
        assert page.evaluate('searchProbe.applied') == ['alpha']
        search.fill('beta')
        search.press('Enter')
        search.press('Enter')
        assert page.evaluate('searchProbe.applied') == ['alpha', 'beta']
        page.clock.run_for(200)
        assert page.evaluate('searchProbe.applied') == ['alpha', 'beta']
        search.fill('kept on remount')
        page.evaluate("document.querySelector('#lifecycle-search').remove();mountSearch().search.focus()")
        expect(search).to_have_value('kept on remount')
        page.clock.run_for(200)
        assert page.evaluate('searchProbe.applied') == ['alpha', 'beta']
        expect(search).to_be_focused()
        page.evaluate('''async()=>{const{collectionToolbar}=await window.importBundle('table_view.js');
            const t=collectionToolbar({label:'Immediate',search:{value:'',label:'Immediate query',
                onInput:q=>searchProbe.immediate=q,apply:()=>searchProbe.immediateApplied=searchProbe.immediate}});
            document.querySelector('#main').append(t.root);}''')
        page.get_by_role('searchbox', name='Immediate query').fill('now')
        assert page.evaluate('[searchProbe.immediate,searchProbe.immediateApplied]') == ['now', 'now']
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', DISPLAYS)
def test_requests_clear_type_leave_and_reopen_keep_current_query_without_stale_reads(browser, live_server, h, tmp_path, width, scheme):
    leave(h)
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': width, 'height': 838})
    reads = []
    page.on('request', lambda r: reads.append(parse_qs(urlparse(r.url).query).get('q', [''])[0])
            if urlparse(r.url).path == '/api/requests/all' else None)
    # Diagnostic (PHONE-DUPREAD): the stack of every /api/requests/all fetch, said only when the read count is wrong.
    page.evaluate('''() => { const fetch0 = window.fetch; window.__reqStacks = [];
        window.fetch = function (input, ...rest) { const url = String(input && input.url || input);
            if (url.includes('/api/requests/all')) window.__reqStacks.push(url + '\\n' + new Error().stack);
            return fetch0.call(this, input, ...rest); }; }''')
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'requests')
        expect(page.locator('#main .dt-card' if width == 390 else '#main tr.dt-open')).to_have_count(1)
        pause_clock(page)
        search = page.get_by_role('searchbox', name='Search requests', exact=True)
        read_before = {''}  # the opening read; a page opened again shows a reply it holds for its query (api.ts keptReads)
        for query in ('unmatched private phrase', '', 'leave'):
            reads.clear()
            page.evaluate('() => { window.__reqStacks.length = 0; }')
            # State must survive leaving before the debounce runs; no settle/wait is allowed between these actions.
            search.fill(query)
            assert page.evaluate("async()=>(await window.importBundle('table_view.js')).tableSearch('requests')") == query
            page.evaluate("location.hash='home'")
            expect_loaded(page, 'home')
            kept = query in read_before
            if kept:  # its reply is held (and fresh: the clock is paused): drawn from it, nothing asked
                page.evaluate("location.hash='requests'")
                expect_loaded(page, 'requests')
            else:  # never read: asked for, with the current query
                with page.expect_response(lambda r: urlparse(r.url).path == '/api/requests/all'
                                          and parse_qs(urlparse(r.url).query).get('q', [''])[0] == query) as loaded:
                    page.evaluate("location.hash='requests'")
                    expect_loaded(page, 'requests')
                assert loaded.value.ok
                read_before.add(query)
            expect(search).to_have_value(query)
            search.focus()
            page.clock.run_for(200)
            expect(search).to_have_value(query)
            expect(search).to_be_focused()
            assert reads == ([] if kept else [query]), (reads, page.evaluate('window.__reqStacks.slice(-4)'))  # never a read of an earlier query
            assert 'unmatched private phrase' not in page.evaluate('JSON.stringify(Object.entries(localStorage))')
        reads.clear()
        search.press_sequentially(' final')
        assert reads == []
        with page.expect_response(lambda r: urlparse(r.url).path == '/api/requests/all'
                                  and parse_qs(urlparse(r.url).query).get('q') == ['leave final']) as loaded:
            search.press('Enter')
        assert loaded.value.ok
        expect(page.get_by_role('heading', name='No matching requests', exact=True)).to_be_visible()
        page.clock.run_for(200)
        search.press('Enter')
        assert reads == ['leave final'], reads
        capture(page, tmp_path, f'search-reopen-{width}-{scheme}')
        page.clock.resume()
        page.reload()
        expect_loaded(page, 'requests')
        expect(search).to_have_value('')  # Free text stays in memory, never durable storage.
    finally:
        context.close()


def test_requests_refresh_during_the_reopen_read_shares_that_read(browser, live_server, h):
    leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        expect(page.locator('#main tr.dt-open')).to_have_count(1)
        pause_clock(page)
        listed = lambda r: urlparse(r.url).path == '/api/requests/all' and parse_qs(urlparse(r.url).query).get('q') == ['held']
        reads, held = [], []
        page.on('request', lambda r: reads.append(r.url) if listed(r) else None)
        page.route(lambda url: urlparse(url).path == '/api/requests/all', lambda route: held.append(route) if listed(route.request) else route.continue_())
        page.evaluate("async()=>(await window.importBundle('table_view.js')).openFiltered('requests',{},'held')")
        page.evaluate("location.hash='home'")
        expect_loaded(page, 'home')
        with page.expect_request(listed):
            page.evaluate("location.hash='requests'")
        # A refresh while the route's read is held: its render reads the same list (and asks overtime again, at the same step).
        with page.expect_request(lambda r: urlparse(r.url).path == '/api/requests/overtime-eligibility'):
            page.evaluate("window.dispatchEvent(new Event('rota-recheck'))")
        for route in held:
            route.continue_()
        expect_loaded(page, 'requests')
        expect(page.get_by_role('searchbox', name='Search requests', exact=True)).to_have_value('held')
        assert len(reads) == 1, reads
    finally:
        context.close()


def test_managed_close_hook_is_synchronous_once_and_native_close_remains_eventual(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        page.evaluate('''async()=>{const{el,showDialog,closeDialogs}=await window.importBundle('dom.js');
            window.dialogProbe={hooks:0,native:0};const p=window.dialogProbe;
            const opener=el('button',{id:'lifecycle-opener',type:'button'},'Open lifecycle dialog');
            document.querySelector('#main').append(opener);
            opener.onclick=()=>{window.managed=showDialog('Lifecycle dialog','',(body,close,tryClose,onClose)=>{
                onClose(()=>p.hooks++);body.append(el('button',{type:'button',onclick:tryClose},'Cancel'));
            });managed.dialog.addEventListener('close',()=>p.native++);};window.closeManaged=closeDialogs;}''')
        opener = page.get_by_role('button', name='Open lifecycle dialog', exact=True)
        dialog = page.get_by_role('dialog', name='Lifecycle dialog', exact=True)
        for index, method in enumerate(('managed', 'Cancel', 'Close dialog', 'Escape', 'backdrop', 'native'), 1):
            opener.click()
            expect(dialog).to_be_visible()
            if method == 'managed':
                assert page.evaluate('''()=>{managed.close();managed.close();closeManaged();return dialogProbe;}''') == {'hooks': index, 'native': index - 1}
            elif method == 'Escape':
                page.keyboard.press('Escape')
            elif method == 'backdrop':
                box = dialog.bounding_box()
                assert box['x'] > 1 and box['y'] > 1
                page.mouse.click(box['x'] - 1, box['y'] - 1)
            elif method == 'native':
                page.evaluate('managed.dialog.close()')
            else:
                dialog.get_by_role('button', name=method, exact=True).click()
            expect(dialog).to_have_count(0)
            page.wait_for_function('n=>dialogProbe.native===n', arg=index)
            assert page.evaluate('dialogProbe.hooks') == index
            expect(opener).to_be_focused()
        # A nested clash promise still resolves through the queued native event after managed navigation cleanup.
        page.evaluate('''async()=>{const{showDialog,el}=await window.importBundle('dom.js');
            const{clashDialog}=await window.importBundle('double_booking.js');
            window.outer=showDialog('Outer lifecycle','',(body,close,_try,onClose)=>{
                onClose(()=>dialogProbe.outer=(dialogProbe.outer||0)+1);
                body.append(el('button',{type:'button'},'Outer action'));});
            window.clashAnswer='pending';clashDialog([],()=>undefined).then(a=>window.clashAnswer=a);}''')
        expect(page.get_by_role('dialog', name='Double bookings', exact=True)).to_be_visible()
        assert page.evaluate('''()=>{closeManaged();closeManaged();return dialogProbe.outer;}''') == 1
        page.wait_for_function("() => clashAnswer==='back'")
        expect(page.get_by_role('dialog')).to_have_count(0)
    finally:
        context.close()


def test_closed_export_ignores_late_host_draw_and_preserves_replacement_dialog(browser, live_server, h):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        settled(page)
        page.evaluate('''async()=>{const{exportDialog}=await window.importBundle('schedule_export.js');
            const{session}=await window.importBundle('print_presets.js');session.last=null;
            window.exportProbe={draws:0,restores:0};const p=window.exportProbe;
            const source={title:'Fictional late preview',period:'Fixture week',department:'Fictional department',
                status:'Draft',draft:true,by:'Fictional coordinator',filters:[],view:document.querySelector('#schedule-view').cloneNode(true)};
            exportDialog({views:[],rota:true,canShare:true,restore:()=>p.restores++,draw:()=>{p.draws++;
                return new Promise(resolve=>window.finishExport=()=>resolve([source]));}});}''')
        page.wait_for_function('() => exportProbe.draws===1')
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        preview = dialog.get_by_role('img', name='Preview of the pages', exact=True).element_handle()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        assert page.evaluate('exportProbe.restores') == 1
        before = preview.inner_html()
        page.evaluate('''async()=>{const{showDialog,el}=await window.importBundle('dom.js');
            showDialog('Replacement dialog','',(body)=>body.append(el('input',{'aria-label':'Replacement field'})));
            finishExport();await Promise.resolve();await Promise.resolve();}''')
        expect(page.get_by_role('textbox', name='Replacement field')).to_be_focused()
        assert preview.inner_html() == before
        assert page.evaluate('exportProbe') == {'draws': 1, 'restores': 1}
        page.keyboard.press('Escape')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', DISPLAYS)
def test_export_close_and_open_navigation_preserve_explicit_list_and_real_csv(browser, live_server, h, tmp_path, width, scheme):
    checked_rota(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 838})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'schedule/week')
        settled(page)
        dialog, _ = open_export(page)
        # Cancel and the new route share a task: native close has not fired when List takes ownership.
        dialog.evaluate("d=>{[...d.querySelectorAll('button')].find(b=>b.textContent==='Cancel').click();location.hash='schedule/list';}")
        expect(dialog).to_have_count(0)
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        expect(page.locator('#schedule-view [data-session]')).to_have_count(2)
        dialog, _ = open_export(page)
        dialog.get_by_role('radiogroup', name='Format').get_by_role('radio', name='Data (CSV)', exact=True).click()
        expect(dialog.get_by_role('button', name='Save CSV', exact=True)).to_be_enabled()
        capture(page, tmp_path, f'export-csv-{width}-{scheme}')
        with page.expect_download() as download:
            dialog.get_by_role('button', name='Save CSV', exact=True).click()
        text = Path(download.value.path()).read_text(encoding='utf-8-sig')
        assert text.splitlines()[0].startswith('When,') and 'Actions' not in text.splitlines()[0]
        assert len(text.splitlines()) > 1
        # Navigating with export still open closes it before reading the next route; Back keeps the explicit List.
        planner_tool(page, 'Export…')  # This session now uses CSV, whose preview is text rather than .export-page.
        dialog = page.get_by_role('dialog', name='Print or export', exact=True)
        expect(dialog.locator('.export-csv')).to_be_visible()
        navigate(page, 'home')
        expect(dialog).to_have_count(0)
        page.go_back()
        settled(page)
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        expect(page.locator('#schedule-view [data-session]')).to_have_count(2)
        capture(page, tmp_path, f'export-back-list-{width}-{scheme}')
    finally:
        context.close()
