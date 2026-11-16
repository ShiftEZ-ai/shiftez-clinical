"""The real shared collection keeps reading position, focus and explicit wrapping across redraws."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, toolbar, close_popover, save_dialog  # noqa: F401
from .test_browser import whole_list
from .test_browser_requests import day, leave

pytestmark = pytest.mark.browser


def collection(page):
    """Exercise the shipped component with enough rows/columns for both native scroll axes."""
    navigate(page, 'people')
    page.evaluate("""async () => {
        const {dataTable}=await window.importBundle('table_view.js');
        const rows=Array.from({length:120},(_,i)=>({id:'row-'+String(i).padStart(3,'0'),
            name:'Row'+String(i).padStart(3,'0'),band:['Alpha','Beta','Gamma'][Math.floor(i%60/20)]}));
        const selected=new Set();
        const build=()=>{const table=dataTable({id:'names',noun:['audit row','audit rows'],rows,rowId:r=>r.id,
            rowLabel:r=>r.name,searchLabel:'Search audit rows',pageSize:60,groupLimit:10,defaults:{group:'band'},
            selection:{set:selected,changed:()=>table.refresh()},columns:[
                {key:'name',label:'Name',text:r=>r.name,cell:r=>r.name},
                {key:'date',label:'Date',cell:()=> '17 Nov 2026'},
                {key:'band',label:'Band',text:r=>r.band,facet:r=>r.band,cell:r=>r.band},
                ...Array.from({length:8},(_,i)=>({key:'allocation'+i,label:'Recorded allocation '+(i+1),
                    cell:()=> 'Allocation value for window '+(i+1)}))]});
        table.root.id='audit-collection';return table;};
        window.buildAuditCollection=build;
        const table=build();
        document.querySelector('#main').replaceChildren(table.root);
        window.auditCollection=table;
    }""")
    root = page.locator('#audit-collection')
    expect(root.locator('tbody tr[data-row]')).to_have_count(30)
    scroller = root.locator('.dt-body > .table-scroll')
    dimensions = scroller.evaluate('(e)=>({x:e.scrollWidth-e.clientWidth,y:e.scrollHeight-e.clientHeight})')
    assert dimensions['x'] > 120 and dimensions['y'] > 180, dimensions
    return root, scroller


def position(scroller):
    return scroller.evaluate('(e)=>{e.scrollLeft=120;e.scrollTop=180;return {x:e.scrollLeft,y:e.scrollTop}}')


def unchanged(scroller, before):
    expect(scroller).to_have_js_property('scrollLeft', before['x'])
    expect(scroller).to_have_js_property('scrollTop', before['y'])


@pytest.mark.parametrize('width,dark', [(1440, False), (1100, True)])
def test_collection_redraw_selection_expansion_and_wrap_keep_scroll_and_focus(browser, live_server, width, dark):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme='dark' if dark else 'light')
        root, scroller = collection(page)
        # A reader using the scroll region itself does not lose keyboard ownership to the page.
        scroller.focus()
        before = position(scroller)
        page.evaluate('window.auditCollection.refresh()')
        unchanged(scroller, before)
        expect(scroller).to_be_focused()

        # External selection redraws the rows; the equivalent checkbox remains selected and focused.
        box = root.get_by_role('checkbox', name='Select Row025', exact=True)
        box.focus()
        before = position(scroller)
        box.press('Space')
        expect(box).to_be_checked()
        expect(box).to_be_focused()
        unchanged(scroller, before)

        group = root.locator('.dt-group-toggle[data-group="Beta"]')
        for expanded in ('false', 'true'):
            group.focus()
            before = position(scroller)
            group.press('Enter')
            expect(group).to_have_attribute('aria-expanded', expanded)
            expect(group).to_be_focused()
            unchanged(scroller, before)

        more = root.get_by_role('button', name='Show 10 more in Alpha', exact=True)
        more.focus()
        before = position(scroller)
        more.press('Enter')
        expect(root.locator('tbody tr[data-row]')).to_have_count(40)
        expect(scroller).to_be_focused()  # the consumed Show more control gives ownership to its scroll region
        unchanged(scroller, before)

        name_header = root.locator('th[data-col="name"]').get_by_role('button', name='Name', exact=True)
        name_header.click()
        before = scroller.evaluate('(e)=>({x:e.scrollLeft,y:e.scrollTop})')
        assert before['x'] > 0 and before['y'] > 0
        page.get_by_role('menu', name='Name column', exact=True).get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(name_header).to_be_focused()
        unchanged(scroller, before)

        # Short raw text follows the same one-line / explicit Wrap contract as long text.
        scroller.evaluate('(e)=>{e.scrollLeft=0}')
        day = root.locator('td[data-col="date"] .dt-clip').first
        expect(day).to_have_text('17 Nov 2026')
        expect(day).to_have_attribute('title', '17 Nov 2026')
        expect(day).to_have_css('white-space', 'nowrap')
        header = root.locator('th[data-col="date"]').get_by_role('button', name='Date', exact=True)
        header.click()
        before = scroller.evaluate('(e)=>({x:e.scrollLeft,y:e.scrollTop})')
        page.get_by_role('menu', name='Date column', exact=True).get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(day).to_have_css('white-space', 'normal')
        expect(day).to_have_text('17 Nov 2026')
        expect(header).to_be_focused()
        unchanged(scroller, before)
    finally:
        context.close()


def test_new_collection_search_filter_sort_page_and_layout_start_at_top(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        root, scroller = collection(page)
        search = root.get_by_role('searchbox', name='Search audit rows', exact=True)
        # Server paging and caller-owned filters explicitly replace the result scope.
        before = position(scroller)
        page.evaluate('window.auditCollection.refresh(true)')
        expect(scroller).to_have_js_property('scrollTop', 0)
        expect(scroller).to_have_js_property('scrollLeft', before['x'])
        before = position(scroller)
        search.fill('Row0')
        search.press('Enter')
        expect(scroller).to_have_js_property('scrollTop', 0)
        expect(scroller).to_have_js_property('scrollLeft', before['x'])
        search.fill('')
        search.press('Enter')

        before = position(scroller)
        toolbar(root, 'Sort').get_by_role('menuitemradio', name='Name', exact=True).click()
        expect(scroller).to_have_js_property('scrollTop', 0)
        expect(scroller).to_have_js_property('scrollLeft', before['x'])

        before = position(scroller)
        from .test_browser import filter_by
        filter_by(root, 'Band', ['Beta'])
        expect(scroller).to_have_js_property('scrollTop', 0)
        expect(scroller).to_have_js_property('scrollLeft', before['x'])
        # Grouped by Band, the group heading names the band; its plain-text column is not repeated (AC-02).
        expect(root.locator('.dt-group-toggle')).to_have_count(1)
        expect(root.locator('.dt-group-toggle[data-group="Beta"]')).to_have_count(1)
        expect(root.locator('tbody tr[data-row]')).to_have_count(10)
        expect(root.locator('tbody tr[data-row] td[data-col="band"]')).to_have_count(0)
        toolbar(root, 'Filter').get_by_role('button', name='Clear all filters', exact=True).click()
        close_popover(page)

        before = position(scroller)
        root.get_by_role('button', name='Next page', exact=True).click()
        expect(root.locator('.dt-pager')).to_contain_text('Page 2 of 2')
        expect(root.get_by_role('button', name='Previous page', exact=True)).to_be_focused()
        expect(root.locator('tbody tr[data-row] td[data-col="name"]').first).to_have_text('Row060')
        expect(scroller).to_have_js_property('scrollTop', 0)
        expect(scroller).to_have_js_property('scrollLeft', before['x'])

        position(scroller)
        settings = toolbar(root, 'View settings')
        settings.get_by_role('radio', name='Cards', exact=True).click()
        expect(scroller).to_have_count(0)
        expect(root.locator('.dt-card')).to_have_count(30)
        settings.get_by_role('radio', name='Table', exact=True).click()
        expect(scroller).to_have_js_property('scrollTop', 0)
        close_popover(page)
    finally:
        context.close()


def test_detached_collection_mount_measures_height_at_its_initial_scroll_position(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        root, scroller = collection(page)
        assert scroller.evaluate('(e)=>({x:e.scrollLeft,y:e.scrollTop})') == {'x': 0, 'y': 0}
        mounted = page.evaluate("""async () => {
            const {captureTableScroll,restoreTableScroll}=await window.importBundle('table_view.js');
            const main=document.querySelector('#main');
            captureTableScroll(main);
            const next=window.buildAuditCollection();
            // Loading can keep the page detached after the draw's height-measurement frame has elapsed.
            await new Promise(requestAnimationFrame);
            const detached=!next.root.isConnected,before=next.root.style.getPropertyValue('--dt-top');
            main.replaceChildren(next.root);
            restoreTableScroll(main);
            window.auditCollection=next;
            const scroll=next.root.querySelector('.dt-body>.table-scroll');
            return {detached,before,offset:next.root.style.getPropertyValue('--dt-top'),
                gap:document.querySelector('.app-footer').getBoundingClientRect().top-scroll.getBoundingClientRect().bottom,
                overflow:scroll.scrollHeight>scroll.clientHeight,x:scroll.scrollLeft,y:scroll.scrollTop};
        }""")
        assert mounted['detached'] and mounted['before'] == '', mounted
        assert mounted['offset'] and mounted['overflow'], mounted
        assert 0 <= mounted['gap'] <= 64, mounted
        assert (mounted['x'], mounted['y']) == (0, 0), mounted
        expect(root.locator('tbody tr[data-row]')).to_have_count(30)
    finally:
        context.close()


def test_same_collection_rebuilt_after_a_page_refresh_restores_its_native_viewport(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        root, scroller = collection(page)
        before = position(scroller)
        # renderPage captures the old page, builds content detached, then mounts it after loading settles.
        page.evaluate("""async () => {
            const {captureTableScroll,restoreTableScroll}=await window.importBundle('table_view.js');
            const main=document.querySelector('#main');
            captureTableScroll(main);
            const next=window.buildAuditCollection();
            main.replaceChildren(document.createElement('div'));
            await new Promise(requestAnimationFrame);
            main.replaceChildren(next.root);
            restoreTableScroll(main);
            window.auditCollection=next;
        }""")
        unchanged(scroller, before)
        expect(root.locator('tbody tr[data-row]')).to_have_count(30)
        # A route/result-scope change cannot borrow the previous page's vertical offset.
        page.evaluate("""async () => {
            const {captureTableScroll,restoreTableScroll}=await window.importBundle('table_view.js');
            const main=document.querySelector('#main');
            captureTableScroll(main);
            history.replaceState(null,'','#people?scope=another');
            const next=window.buildAuditCollection();
            main.replaceChildren(next.root);
            restoreTableScroll(main);
            window.auditCollection=next;
        }""")
        expect(scroller).to_have_js_property('scrollTop', 0)
    finally:
        context.close()


def test_request_panel_approval_and_undo_keep_position_but_link_filter_resets_it(browser, live_server, h):
    target = leave(h, on=day(2))
    for n in range(30):
        leave(h, on=day(n + 1), kind='meeting')
    assert h.get('approver', '/api/me')['roles'] == ['approver']
    context, page = sign_in(browser, live_server, 'approver', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        root = page.locator('#main .requests .dt')
        scroller = root.locator('.dt-body > .table-scroll')
        expect(root.locator('tbody tr[data-row]')).to_have_count(31)
        root.locator(f'tr[data-row="{target["id"]}"] .request-title').click()
        panel = page.get_by_role('region', name='Other', exact=True)  # untyped leave is Other (maintenance)
        expect(panel.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        assert page.evaluate("new URLSearchParams(location.hash.split('?')[1]).get('open')") == target['id']
        before = position(scroller)
        assert before['y'] > 0

        panel.get_by_role('button', name='Approve', exact=True).click()
        decision = page.get_by_role('dialog', name='Approve leave', exact=True)
        decision.get_by_label('Reason', exact=True).fill('Synthetic review for scroll retention')
        save_dialog(decision, 'Approve')
        expect(panel.locator('.request-status')).to_contain_text('Approved')
        unchanged(scroller, before)
        assert h.get('approver', '/api/requests/all?id=' + target['id'])['item']['stage'] == 'approved'

        page.locator('#toast-tray .toast', has_text='Approved · ').get_by_role('button', name='Undo', exact=True).click()
        expect(panel.locator('.request-history')).to_contain_text('Decision undone')
        unchanged(scroller, before)
        assert h.get('approver', '/api/requests/all?id=' + target['id'])['item']['stage'] == 'waiting'

        panel.get_by_role('button', name='Close panel', exact=True).click()
        position(scroller)
        # A genuine URL filter still replaces the result scope, even though the collection's own filters did not change.
        with page.expect_response(lambda response: '/api/requests/all?' in response.url and 'type=other' in response.url) as fetched:
            navigate(page, 'requests?type=other')
        assert fetched.value.status == 200
        expect(root.locator('tbody tr[data-row]')).to_have_count(30)
        assert scroller.evaluate('(e)=>e.scrollHeight-e.clientHeight') > 180
        expect(scroller).to_have_js_property('scrollTop', 0)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_automatic_phone_cards_report_the_rendered_layout_and_can_switch_to_table(browser, live_server, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 390, 'height': 844})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'people')
        root = page.locator('#main [data-table="people"]')
        expect(root.locator('.dt-card')).to_have_count(3)
        expect(root.locator('tbody tr[data-row]')).to_have_count(0)
        settings = toolbar(root, 'View settings')
        cards = settings.get_by_role('radio', name='Cards', exact=True)
        table = settings.get_by_role('radio', name='Table', exact=True)
        expect(cards).to_be_checked()
        expect(table).not_to_be_checked()
        table.focus()
        table.press('Enter')
        expect(table).to_be_checked()
        expect(cards).not_to_be_checked()
        expect(root.locator('tbody tr[data-row]')).to_have_count(3)
        expect(root.locator('.dt-card')).to_have_count(0)
        close_popover(page)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        # The explicit choice persists; a reload must not silently revert to the phone default.
        page.reload()
        expect(root.locator('tbody tr[data-row]')).to_have_count(3)
        expect(toolbar(root, 'View settings').get_by_role('radio', name='Table', exact=True)).to_be_checked()
    finally:
        context.close()
