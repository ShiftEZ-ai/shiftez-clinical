"""Catalogue lines, unlike legacy policy rows, exercise long chips, date ranges and a tall native table."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, wait_held, fonts_ready  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .rules_support import rules_by_family

pytestmark = pytest.mark.browser
LONG_RANK = 'Senior Department A Training Registrar'
CLINIC_NAMES = [
    'Primary Care', 'Urgent Care', 'Wellness Clinic', 'Pain Clinic', 'Procedure Clinic',
    'Sport Medicine', 'Diabetes Clinic', 'Asthma Clinic', 'Student Clinic', 'Travel Clinic',
    'Family Medicine Virtual Clinic', 'Occupational Health Clinic', 'Hypertension Clinic',
    'Osteoporosis Clinic', 'Dermatology Clinic', 'Follow-up Clinic', 'Screening Clinic',
    'Survivorship Clinic', 'Specialist Continuity Clinic', 'Chronic Care Clinic',
    'Family Medicine Specialist Continuity and Follow-up Clinic', 'Community Health Clinic',
    'Evening Clinic', 'Same Day Clinic',
]


def populated_catalogue(h, ends_on='2027-01-01', large_scope=False):
    flag(h)
    rank = next(r for r in h.get('admin', '/api/catalogues')['categories'] if r['id'] == h.seed['category'])
    h.configure('categories', {'name': LONG_RANK, 'starts_on': rank['starts_on']}, rank['id'], rank['revision'])
    ranks = [h.seed['category']] + [h.configure('categories', {
        'name': f'Senior Department A Training Registrar {i}', 'starts_on': '2026-01-01'
    })['record']['id'] for i in range(3)]
    clinic_names = (CLINIC_NAMES[:24 - len(h.seed['services'])] if large_scope else
                    [f'Family Medicine Specialist Continuity and Follow-up Clinic {i}' for i in range(3)])
    clinics = h.seed['services'] + [h.configure('services', {
        'name': name, 'starts_on': '2026-01-01'
    })['record']['id'] for name in clinic_names]
    # Different weekday scopes are real distinct lines, not duplicate fixtures rejected by the catalogue.
    lines = [line({'min': {'value': 1, 'level': 'mandatory'}, 'max': {'value': 12, 'level': 'mandatory'},
                   'target': {'value': 6, 'level': 'medium'}},
                  who={'conditions': [{'field': 'category_ids', 'values': ranks if mask < 24 else ranks[:1]}]},
                  where={'conditions': [{'field': 'service_ids', 'values': clinics}]},
                  weekdays=[day for day in range(7) if mask & (1 << day)],
                  starts_on='2026-09-01', ends_on=ends_on) for mask in range(1, 25)]
    catalogue_rule(h, 'duties_per_week', lines)
    return len(ranks), len(clinics)


FACTS = r'''selector => {
  const rows=document.querySelectorAll(selector);
  const failures=[];let facts=0,dates=0;
  for(const row of rows){
    for(const cell of row.querySelectorAll('.rt-who,.rt-where,[data-col=who],[data-col=where]')){
      const bounds=cell.getBoundingClientRect();
      const nodes=[...cell.querySelectorAll('.pill-item>*,.pill-more')].map(n=>({text:n.textContent,r:n.getBoundingClientRect()})).filter(n=>n.r.width&&n.r.height);
      facts+=nodes.length;
      for(let i=0;i<nodes.length;i++){
        const a=nodes[i];
        if(a.r.left<bounds.left-1||a.r.right>bounds.right+1)failures.push({text:a.text,reason:'outside own column'});
        for(let j=i+1;j<nodes.length;j++){
          const b=nodes[j];
          if(Math.min(a.r.right,b.r.right)-Math.max(a.r.left,b.r.left)>1&&Math.min(a.r.bottom,b.r.bottom)-Math.max(a.r.top,b.r.top)>1)
            failures.push({text:a.text,other:b.text,reason:'overlap'});
        }
      }
    }
    for(const date of row.querySelectorAll('.rule-valid>span')){
      dates++;
      const range=document.createRange();range.selectNodeContents(date);
      if(range.getClientRects().length!==1)failures.push({text:date.textContent,reason:'split date token'});
    }
  }
  return {facts,dates,failures,rootOverflow:document.documentElement.scrollWidth-innerWidth};
}'''


def assert_facts(page, selector):
    rows = page.locator(selector)
    # Measured once the rows are laid out (an empty box has no facts to measure) and in the app's own font.
    expect(rows.first).to_be_visible()
    expect(rows.last).to_be_visible()
    fonts_ready(rows.page)
    # A recheck can redraw the table; select and measure live rows in one JavaScript task.
    result = page.evaluate(FACTS, selector)
    assert result['facts'] > 0 and result['dates'] > 0, result
    assert not result['failures'], result
    assert result['rootOverflow'] <= 1, result


def assert_readable_wrapped_scope(row, selector, count, scroll):
    result = row.evaluate(r'''(row, selector) => {
      const cell=row.querySelector(selector),list=cell.querySelector('.pill-list');
      const pills=[...list.querySelectorAll('.rp')],words=[],clipped=[],lines=new Map();
      for(const pill of pills){
        const rect=pill.getBoundingClientRect(),y=Math.round(rect.top);
        lines.set(y,(lines.get(y)||0)+1);
        if(pill.scrollWidth>pill.clientWidth||pill.scrollHeight>pill.clientHeight)clipped.push(pill.textContent);
        const walker=document.createTreeWalker(pill,NodeFilter.SHOW_TEXT);
        while(walker.nextNode()){
          const node=walker.currentNode;
          for(const word of node.textContent.matchAll(/[A-Za-z]{3,}/g)){
            const range=document.createRange();range.setStart(node,word.index);range.setEnd(node,word.index+word[0].length);
            if(range.getClientRects().length!==1)words.push(word[0]);
          }
        }
      }
      const first=pills[0].getBoundingClientRect().top,lineHeight=parseFloat(getComputedStyle(pills[0]).lineHeight);
      const misplaced=[...row.querySelectorAll('.rule-num,.rule-valid')]
        .filter(n=>n.getBoundingClientRect().top>first+lineHeight).map(n=>n.textContent);
      return {pills:pills.length,words,clipped,misplaced,packedRows:[...lines.values()].filter(n=>n>1).length,
        height:row.getBoundingClientRect().height};
    }''', selector)
    assert result['pills'] == count, result
    assert not result['words'] and not result['clipped'], result
    assert not result['misplaced'], 'numbers and dates must remain beside the start of their scope: ' + str(result)
    assert result['packedRows'] >= 4, 'short scopes should share lines instead of forming a narrow stack: ' + str(result)
    assert result['height'] < scroll.evaluate('e=>e.clientHeight'), 'one 24-clinic row must fit in the reading viewport: ' + str(result)


def position(scroll):
    return scroll.evaluate('e=>({x:e.scrollLeft,y:e.scrollTop})')


def watch_native_scroll(scroll):
    # Observe before the input: the first changed pixel does not mean its native animation has finished.
    scroll.evaluate('''e=>{
        const s=e.__auditNativeScroll={before:{x:e.scrollLeft,y:e.scrollTop},lastX:e.scrollLeft,
            stableFrames:0,moved:false,settled:false,scrollends:0,wheel:[]};
        const ended=()=>s.scrollends++;
        const wheeled=event=>{
            const input={deltaX:event.deltaX,deltaY:event.deltaY};s.wheel.push(input);
            queueMicrotask(()=>input.defaultPrevented=event.defaultPrevented);
        };
        e.addEventListener('scrollend',ended);e.addEventListener('wheel',wheeled);
        const start=performance.now();
        function tick(){
            const x=e.scrollLeft;s.after={x,y:e.scrollTop};s.moved ||= x!==s.before.x;
            s.stableFrames=x===s.lastX?s.stableFrames+1:0;s.lastX=x;
            s.settled=s.moved&&s.stableFrames>=8;s.elapsed=performance.now()-start;
            if(s.settled||s.elapsed>5000){
                s.timedOut=!s.settled;e.removeEventListener('scrollend',ended);e.removeEventListener('wheel',wheeled);
            }else requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
    }''')


def settled_native_scroll(page, scroll):
    page.wait_for_function('''()=>{
        const s=document.querySelector('.rt-scroll').__auditNativeScroll;
        return s.settled||s.timedOut;
    }''')
    result = scroll.evaluate('e=>e.__auditNativeScroll')
    assert result['settled'], result
    assert result['after']['y'] == result['before']['y'], result
    return result


def wrap(page, label):
    page.locator('.rt-scroll thead').get_by_role('button', name=label, exact=True).click()
    page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()


# Wrapping and scroll depend on width and height, never on theme (no colour is read): each width once, the tall desktop,
# and both themes across them.
@pytest.mark.parametrize('width,scheme,height', [(1440, 'light', 900), (1100, 'dark', 900), (390, 'light', 900),
                                                (1440, 'dark', 1700)])
def test_populated_catalogue_facts_wrap_and_native_scroll(browser, live_server, h, width, scheme, height):
    rank_count, clinic_count = populated_catalogue(h, large_scope=True)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': height})
    try:
        page.emulate_media(color_scheme=scheme)
        # A shown skeleton exercises the shared reveal animation, including its retained final state.
        held = []
        page.route('**/api/rules/catalogue', lambda route: held.append(route))
        if width < 720:
            page.get_by_role('button', name='Menu', exact=True).click()
        page.get_by_role('button', name='Rules', exact=True).click()
        wait_held(page, held)
        skeleton = page.locator('#main .loading.page-sk')
        expect(skeleton).to_be_visible()
        expect(skeleton).not_to_have_class(re.compile(r'\bsk-wait\b'))
        page.route('**/api/rules/catalogue', lambda route: route.continue_())
        for route in held:
            route.continue_()
        expect(page.locator('#main[data-loaded-view=rules]')).to_have_count(1)
        expect(page.locator('.rt.fade-in')).to_have_count(1)
        expect(page.locator('#main [role=alert]')).to_have_count(0)
        page.get_by_role('button', name='Expand all rules', exact=True).click()
        expect(page.locator('.rt-line')).to_have_count(25)  # 24 authored lines plus seeded workload.
        rows = page.locator('.rt-line').filter(has=page.get_by_role('checkbox', name=re.compile(r'^Select line \d+ of Duties per week$')))
        expect(rows).to_have_count(24)
        assert_facts(page, '.rt-line')
        single = rows.last.locator('.rt-who')
        expect(single.locator('.pill-more')).to_have_count(0)
        expect(single.locator('.rp')).to_have_attribute('title', LONG_RANK)
        assert single.locator('.rp').evaluate('e=>e.scrollWidth>e.clientWidth'), 'the single long label needs truncation'
        scroll = page.locator('.rt-scroll')
        size = scroll.evaluate('e=>({height:e.clientHeight,total:e.scrollHeight,width:e.clientWidth,wide:e.scrollWidth})')
        assert size['height'] < size['total'], size
        # A long collection uses the available Rules pane, including a tall desktop, while its scrollbar stays above
        # the footer. This checks the reading outcome rather than encoding a percentage-height CSS implementation.
        fit = scroll.evaluate('''e => {const main=document.querySelector('#main'),r=e.getBoundingClientRect(),
            m=main.getBoundingClientRect(),f=document.querySelector('.app-footer').getBoundingClientRect(),
            key=e.closest('.rt')?.querySelector(':scope > .view-legend:not([hidden])'),
            padding=parseFloat(getComputedStyle(main).paddingBottom),
            bottom=key?key.getBoundingClientRect().top:Math.min(m.bottom,f.top,innerHeight);
            return {top:r.top,bottom:r.bottom,availableBottom:bottom,padding,viewport:innerHeight};}''')
        # The Rules key sits under the table (one legend, eb8635b6d; DESIGN.md: a view's key stays visible): the list
        # reaches the key, else the pane's padding above the footer.
        assert fit['bottom'] <= fit['availableBottom'] + 1, fit
        assert fit['availableBottom'] - fit['bottom'] <= fit['padding'] + 2, fit
        scroll.evaluate('e => e.scrollTop=e.scrollHeight')
        expect(page.locator('.rt-table tbody tr').last).to_be_in_viewport()
        scroll.evaluate('e => e.scrollTop=0')
        overflowing = size['wide'] > size['width']
        if width < 1440:
            assert overflowing, size
        # +N is an actual reachable control and exposes the complete collection.
        more = rows.first.locator('.rt-who .pill-more')
        # Its name says the hidden pills' kind (all ranks here), never "more Who".
        expect(more).to_have_accessible_name(re.compile(rf'^\d+ more ranks?: show all {rank_count}$'))
        more.click()
        all_who = page.get_by_role('dialog', name='All Who', exact=True)
        expect(all_who.locator('ol li')).to_have_count(rank_count)
        page.keyboard.press('Escape')
        expect(all_who).to_have_count(0)
        # Reading a middle row and ticking it does not snap back to the start after the redraw.
        # On a tall viewport line 12 is already visible, so establish actual table scroll before selecting it.
        scroll.evaluate('e => e.scrollTop=Math.min(e.clientHeight/2,e.scrollHeight-e.clientHeight)')
        selected = page.get_by_role('checkbox', name='Select line 12 of Duties per week', exact=True)
        selected.scroll_into_view_if_needed()
        selected.focus()
        before = position(scroll)
        assert before['y'] > 0, before
        page.keyboard.press('Space')
        expect(selected).to_be_checked()
        assert position(scroll) == before
        # The shared footer observer reserves the selection bar's actual height after it appears.
        expect(page.get_by_role('toolbar', name='Selected rules and lines', exact=True)).to_be_visible()
        page.wait_for_function('''() => {
            const table=document.querySelector('.rt-scroll'),bar=document.querySelector('#main .selection-bar:not([hidden])');
            return table.getBoundingClientRect().bottom <= bar.getBoundingClientRect().top;
        }''')
        # The native horizontal scrollbar is at the bounded viewport's bottom, reachable mid-list.
        scroll.scroll_into_view_if_needed()
        scroll.focus()
        if overflowing:
            expect(scroll).to_be_focused()
            watch_native_scroll(scroll)
            page.keyboard.press('ArrowRight')
            keyboard = settled_native_scroll(page, scroll)
            assert keyboard['after']['x'] > keyboard['before']['x'], keyboard
            rect = scroll.bounding_box()
            page.mouse.move(rect['x'] + min(rect['width'] - 20, 160), rect['y'] + rect['height'] / 2)
            # Wait for the preceding keyboard animation, then prove the opposite mouse input independently.
            watch_native_scroll(scroll)
            page.mouse.wheel(-180, 0)
            mouse = settled_native_scroll(page, scroll)
            assert mouse['wheel'] and mouse['wheel'][0] == {
                'deltaX': -180, 'deltaY': 0, 'defaultPrevented': False}, mouse
            assert mouse['after']['x'] < mouse['before']['x'], mouse
        end = scroll.evaluate('e=>e.getBoundingClientRect().bottom')
        assert end <= height, end
        shot(page, f'rules-layout-middle-{width}-{height}-{scheme}')
        # Header menus remain sticky while reading the middle of the table. Wrapping preserves that position.
        scroll.evaluate('e=>e.scrollLeft=0')
        page.locator('.rt-scroll thead').get_by_role('button', name='Rule / who', exact=True).click()
        before = position(scroll)
        page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        assert position(scroll) == before
        expect(rows.first.locator('.rt-who.dt-wrap .pill-more')).to_be_hidden()
        assert single.locator('.rp').evaluate('e=>e.scrollWidth<=e.clientWidth'), 'wrapping reveals the full single label'
        assert_facts(page, '.rt-line')
        wrap(page, 'Where')
        expect(rows.first.locator('.rt-where.dt-wrap .pill-item')).to_have_count(clinic_count)
        wrap(page, 'Valid')
        assert_facts(page, '.rt-line')
        assert_readable_wrapped_scope(rows.first, '.rt-where', clinic_count, scroll)
        shot(page, f'rules-layout-wrapped-{width}-{scheme}')
        wrap(page, 'Where')
        expect(rows.first.locator('.rt-where .pill-more')).to_be_visible()
        # A changed result set deliberately starts at the first row.
        page.get_by_role('searchbox', name='Search rules').fill('Duties per week')
        expect(page.locator('.rt-rule')).to_have_count(1)  # the search applies once typing pauses (RU-3)
        assert position(scroll)['y'] == 0
        navigate(page, 'rules/duties_per_week')
        if width < 720:
            page.get_by_role('button', name='View settings', exact=True).click()
            page.get_by_role('radio', name='Table', exact=True).click()
            page.keyboard.press('Escape')
        detail = page.locator('.dt-table tbody tr[data-row]')
        expect(page.locator('.rule-valid').first).to_be_visible()
        # The shared rule detail renderer keeps the same complete dates, too.
        assert_facts(page, '.dt-table tbody tr[data-row]')
        detail_scroll = page.locator('.dt-body>.table-scroll')
        for label in ['Who', 'Where', 'Valid']:
            detail_scroll.locator('thead').get_by_role('button', name=label, exact=True).click()
            page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(detail.first.locator('[data-col=where] .pill-more')).to_be_hidden()
        assert_facts(page, '.dt-table tbody tr[data-row]')
        assert_readable_wrapped_scope(detail.first, '[data-col=where]', clinic_count, detail_scroll)
        # The detail table uses the remaining pane too, leaving its New line action above the footer.
        fit = detail_scroll.evaluate('''e => {const main=document.querySelector('#main'),r=e.getBoundingClientRect(),
            m=main.getBoundingClientRect(),f=document.querySelector('.app-footer').getBoundingClientRect(),
            add=main.querySelector('.rc-add').getBoundingClientRect();
            return {bottom:r.bottom,actionTop:add.top,actionBottom:add.bottom,total:e.scrollHeight,height:e.clientHeight,
                availableBottom:Math.min(m.bottom,f.top,innerHeight),padding:parseFloat(getComputedStyle(main).paddingBottom)};}''')
        assert fit['height'] < fit['total'], fit
        assert fit['bottom'] <= fit['actionTop'] + 1, fit
        assert fit['actionBottom'] <= fit['availableBottom'] + 1, fit
        assert fit['availableBottom'] - fit['actionBottom'] <= fit['padding'] + 2, fit
        shot(page, f'rule-detail-wrapped-{width}-{scheme}')
        detail_scroll.evaluate('e => {e.scrollTop=e.scrollHeight;e.scrollLeft=0;}')
        expect(detail.last).to_be_in_viewport()
        expect(page.get_by_role('button', name='New line: a person, rank, clinic or #tag', exact=True)).to_be_in_viewport()
        assert page.locator('#main').evaluate('e=>e.scrollTop') == 0, 'the table owns scrolling, not the Rules page'
        detail.last.locator('.bulk-check').check()
        expect(page.get_by_role('toolbar', name='Bulk actions', exact=True)).to_be_visible()
        page.wait_for_function('''() => {
            const table=document.querySelector('[data-table=rule-line-db]>.dt-body>.table-scroll'),
                add=document.querySelector('#main .rc-add'),bar=document.querySelector('#main .selection-bar:not([hidden])'),
                limit=bar.getBoundingClientRect().top;
            return table.getBoundingClientRect().bottom <= limit && add.getBoundingClientRect().bottom <= limit;
        }''')
        detail_scroll.evaluate('e=>e.scrollTop=e.scrollHeight')
        expect(detail.last.locator('.bulk-check')).to_be_checked()
        expect(detail.last).to_be_in_viewport()
        assert detail.last.evaluate('e=>e.getBoundingClientRect().bottom<=e.closest(".table-scroll").getBoundingClientRect().bottom'), 'the last line remains above the selection bar'
        expect(page.get_by_role('button', name='New line: a person, rank, clinic or #tag', exact=True)).to_be_in_viewport()
        shot(page, f'rule-detail-selected-{width}-{height}-{scheme}')
    finally:
        context.close()


@pytest.mark.parametrize('role,width,open_line', [('auditor', 390, False), ('coordinator', 1100, True)])
def test_narrow_rule_table_headers_remain_reachable_after_wrap(browser, live_server, h, role, width, open_line):
    """The first data column cannot hide the next header, with or without a checkbox gutter or desktop dock."""
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}},
        who={'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]},
        where={'conditions': [{'field': 'service_ids', 'values': h.seed['services'][:2]}]})])
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        navigate(page, 'rules/duties_per_week')
        if width < 720:
            page.get_by_role('button', name='View settings', exact=True).click()
            page.get_by_role('radio', name='Table', exact=True).click()
            page.keyboard.press('Escape')
        scroll = page.locator('.dt-body>.table-scroll')
        row = scroll.locator('tbody tr[data-row]').first
        short = scroll.evaluate('''e => ({height:e.clientHeight,total:e.scrollHeight,
            content:e.firstElementChild.getBoundingClientRect().height})''')
        assert short['height'] == short['total'], short
        assert short['height'] - short['content'] <= 1, 'a short detail table must not stretch its rows: ' + str(short)
        if open_line:
            row.locator('[data-col=who]').click()
            expect(page.locator('.rc-line-panel')).to_be_visible()
        assert scroll.evaluate('e => e.clientWidth < 720'), 'exercise the actual narrow table, including a desktop dock'
        expect(scroll.locator('thead .bulk-check')).to_have_count(1 if role == 'coordinator' else 0)
        scroll.locator('thead').get_by_role('button', name='Who', exact=True).click()
        page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(row.locator('[data-col=who]')).to_have_class(re.compile(r'\bdt-wrap\b'))
        for label in ['Where', 'Valid']:
            scroll.locator('thead').get_by_role('button', name=label, exact=True).click()
            expect(page.get_by_role('menu', name=label + ' column', exact=True)).to_be_visible()
            page.keyboard.press('Escape')
        assert position(scroll)['x'] > 0, 'later columns remain reachable by native horizontal scrolling'
        if role == 'coordinator':
            # The fixed checkbox remains reachable without snapping the table back to its first data column.
            before = position(scroll)
            row.locator('.bulk-check').check()
            expect(row.locator('.bulk-check')).to_be_checked()
            assert position(scroll) == before
        shot(page, f'rule-headers-narrow-{role}-{width}')
    finally:
        context.close()


def test_pending_rule_proposal_keeps_all_changes_and_lines_reachable_on_phone(browser, live_server, h):
    flag(h)
    start = date(2027, 1, 1)
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}},
                  starts_on=str(start + timedelta(days=n)), ends_on=str(start + timedelta(days=n + 1)))
             for n in range(30)]
    active = catalogue_rule(h, 'duties_per_week', lines)
    h.command('coordinator', 'rule.propose', {
        'catalogue_id': 'duties_per_week', 'expected_policy_id': active['id'], 'expected_revision': active['revision'],
        'lines': [{**item, 'numbers': {'max': {'value': 6, 'level': 'mandatory'}}} for item in lines],
        'reason': 'Review every dated limit before approval'})
    context, page = sign_in(browser, live_server, 'approver', {'width': 390, 'height': 900})
    try:
        page.emulate_media(color_scheme='dark')
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        expect(waiting.locator('tr.rc-prop-changed')).to_have_count(20)
        page.get_by_role('button', name='View settings', exact=True).click()
        page.get_by_role('radio', name='Table', exact=True).click()
        page.keyboard.press('Escape')
        main = page.locator('#main')
        main.evaluate('e=>e.scrollTop=0')
        main.hover()
        page.mouse.wheel(0, 900)
        page.wait_for_function("() => document.querySelector('#main').scrollTop > 0")
        waiting.get_by_role('button', name='Show 10 more changes', exact=True).click()
        expect(waiting.locator('tr.rc-prop-changed')).to_have_count(30)
        last_change = waiting.locator('tr.rc-prop-changed').last
        last_change.scroll_into_view_if_needed()
        expect(last_change).to_be_in_viewport()
        table = page.locator('[data-table=rule-line-db]>.dt-body>.table-scroll')
        table.scroll_into_view_if_needed()
        expect(table.locator('tbody tr[data-row]')).to_have_count(30)
        table.evaluate('e=>e.scrollTop=e.scrollHeight')
        expect(table.locator('tbody tr[data-row]').last).to_be_in_viewport()
        shot(page, 'rule-detail-pending-phone')
    finally:
        context.close()


def test_search_keeps_horizontal_scroll_and_row_actions_capture_immediate_native_scroll(browser, live_server, h):
    populated_catalogue(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1100, 'height': 900})
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Expand all rules', exact=True).click()
        scroll = page.locator('.rt-scroll')
        # The scroll event need not have fired before a row action in the same JavaScript task.
        immediate = scroll.evaluate('''e=>{
            e.scrollLeft=80;e.scrollTop=180;
            const before={x:e.scrollLeft,y:e.scrollTop};
            e.querySelector('.rt-line .bulk-check').click();
            const next=document.querySelector('.rt-scroll');
            return {before,after:{x:next.scrollLeft,y:next.scrollTop}};
        }''')
        assert immediate['before']['x'] > 0 and immediate['before']['y'] > 0, immediate
        assert immediate['after'] == immediate['before'], immediate
        expect(page.locator('.rt-line .bulk-check').first).to_be_checked()
        # An ordinary native scroll (including one initiated programmatically) feeds the result-scope cache.
        before = scroll.evaluate('''e=>new Promise(resolve=>{
            e.addEventListener('scroll',()=>resolve({x:e.scrollLeft,y:e.scrollTop}),{once:true});
            e.scrollTo({left:0,top:0,behavior:'instant'});
            e.scrollTo({left:120,top:220,behavior:'instant'});
        })''')
        assert before['x'] > 0 and before['y'] > 0, before
        rules = page.locator('.rt-rule').count()
        page.get_by_role('searchbox', name='Search rules').fill('Duties per week')
        expect(page.locator('.rt-rule')).to_have_count(1)  # the search applies once typing pauses (RU-3)
        assert position(scroll) == {'x': before['x'], 'y': 0}
        page.get_by_role('searchbox', name='Search rules').fill('')
        expect(page.locator('.rt-rule')).to_have_count(rules)
        assert position(scroll) == {'x': before['x'], 'y': 0}
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'auditor'])
def test_switching_rule_and_issue_columns_starts_at_their_identifying_facts(browser, live_server, h, role):
    populated_catalogue(h, ends_on='2026-10-20')
    ending = next(x for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'ending')
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [ending['fingerprint']], 'note': 'Same issue columns fixture'})
    context, page = sign_in(browser, live_server, role, {'width': 390, 'height': 844})
    try:
        rules_by_family(h, page, role)  # the family list (Question A–F with its people tables is the default, 9f01206c2)
        navigate(page, 'rules')
        page.get_by_role('button', name='Expand all rules', exact=True).click()
        expect(page.locator('.rt-line')).to_have_count(25)  # Seeded workload remains visible beside the 24 dated lines.
        scroll = page.locator('#main .rt-scroll')

        def view(label):
            page.locator('#main .rt > .dt-toolbar').get_by_role('button', name=re.compile('^Saved views')).click()
            page.get_by_role('menu', name='Saved views', exact=True).get_by_role(
                'menuitemradio', name=re.compile('^' + re.escape(label) + r'\b')).click()

        def scroll_right():
            result = scroll.evaluate('''e=>new Promise(resolve=>{
                e.addEventListener('scroll',()=>resolve(e.scrollLeft),{once:true});
                e.scrollTo({left:e.scrollWidth,top:0,behavior:'instant'});
            })''')
            assert result > 0
            return result

        scroll_right()
        view('Needs attention')
        expect(page.locator('.rt-issue').first).to_be_visible()
        assert position(scroll) == {'x': 0, 'y': 0}
        expect(page.locator('.rt-issue .rt-rule-link').first).to_be_in_viewport()
        expect(page.locator('.rt-iline .rt-n').first).to_be_in_viewport()
        before = scroll_right()
        # Both issue views share columns, so changing only the result set keeps x.
        view('Acknowledged')
        expect(page.locator('.rt-issue').first).to_be_visible()
        acknowledged_x = position(scroll)['x']
        assert acknowledged_x == min(before, scroll.evaluate('e=>e.scrollWidth-e.clientWidth'))
        assert acknowledged_x > 0
        view('Needs attention')
        assert position(scroll)['x'] == acknowledged_x
        view('All rules')
        expect(page.locator('.rt-line')).to_have_count(25)
        assert position(scroll) == {'x': 0, 'y': 0}
        # The rule column starts in view horizontally; a Question group's people table (9f01206c2) may sit above the first
        # rule row on a phone, so its vertical place is not what this view switch is about.
        link = page.locator('.rt-rule .rt-rule-link').first
        expect(link).to_be_attached()
        assert link.evaluate('''e=>{const s=e.closest('.rt-scroll').getBoundingClientRect(),r=e.getBoundingClientRect();
            return r.left>=s.left-1&&r.left<s.right;}'''), 'the rule column starts at the left of the list'
    finally:
        context.close()


def test_acknowledge_and_undo_keep_the_native_issue_scroll_position(browser, live_server, h):
    populated_catalogue(h, ends_on='2026-10-20')
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1100, 'height': 900})
    try:
        navigate(page, 'rules')
        page.locator('#main button.dt-saved').click()
        page.get_by_role('menuitemradio', name=re.compile(r'^Needs attention\b')).click()
        rows = page.locator('.rt-issue')
        expect(rows).to_have_count(24)
        row = rows.nth(11)
        issue_id = row.get_attribute('data-issue')
        row.get_by_role('button', name='Let it end · acknowledge…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Acknowledge 1 issue', exact=True)
        expect(dialog).to_be_visible()
        scroll = page.locator('.rt-scroll')
        before = position(scroll)
        assert before['y'] > 0, before
        dialog.get_by_role('button', name='Acknowledge', exact=True).click()
        expect(page.locator(f'[data-issue="{issue_id}"]')).to_have_count(0)
        expect(rows).to_have_count(23)
        assert position(scroll) == before
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator(f'[data-issue="{issue_id}"]')).to_have_count(1)
        expect(rows).to_have_count(24)
        assert position(scroll) == before
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(390, 'dark'), (1440, 'light')])
def test_three_digit_line_numbers_stay_whole_in_rules_and_issue_views(browser, live_server, h, width, scheme):
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    future = today + timedelta(days=365)
    # Separate real validity periods avoid duplicates or artificial DOM numbering. The ending line is genuinely 141.
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}},
                  starts_on=str(future + timedelta(days=n)), ends_on=str(future + timedelta(days=n + 1)))
             for n in range(140)]
    ending_line = line({'max': {'value': 5, 'level': 'mandatory'}},
                       starts_on=str(today), ends_on=str(today + timedelta(days=14)))
    record = catalogue_rule(h, 'duties_per_week', [*lines, ending_line])
    assert record['definition']['rule']['lines'][140]['id'] == ending_line['id']
    ending = next(x for x in h.get('coordinator', '/api/rules/catalogue')['attention']
                  if x['kind'] == 'ending' and x['line_ids'] == [ending_line['id']])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        page.get_by_role('button', name='Expand all rules', exact=True).click()

        def whole_number(number):
            expect(number).to_have_text('141')
            number.scroll_into_view_if_needed()
            result = number.evaluate('''e => {
                const range=document.createRange();range.selectNodeContents(e);
                const rects=[...range.getClientRects()],text=range.getBoundingClientRect(),box=e.getBoundingClientRect();
                const next=e.nextElementSibling?.getBoundingClientRect();
                return {fragments:rects.length,clipped:e.scrollWidth>e.clientWidth,
                    contained:text.left>=box.left-1&&text.right<=box.right+1,
                    overlapsNext:!!next&&text.right>next.left};
            }''')
            assert result == {'fragments': 1, 'clipped': False, 'contained': True, 'overlapsNext': False}, result

        whole_number(page.locator(f'.rt-line[data-line="{ending_line["id"]}"] .rt-rail .rt-n'))

        def view(label):
            page.locator('#main button.dt-saved').click()
            page.get_by_role('menuitemradio', name=re.compile('^' + re.escape(label) + r'\b')).click()

        view('Needs attention')
        issue = page.locator('.rt-issue').filter(has=page.locator('.rt-n').filter(has_text=re.compile(r'^141$')))
        expect(issue).to_have_count(1)
        whole_number(issue.locator('.rt-n'))
        shot(page, f'line-141-needs-attention-{width}-{scheme}')
        issue.get_by_role('button', name='Let it end · acknowledge…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Acknowledge 1 issue', exact=True)
        dialog.get_by_role('button', name='Acknowledge', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(issue).to_have_count(0)
        view('Acknowledged')
        expect(issue).to_have_count(1)
        whole_number(issue.locator('.rt-n'))
        assert issue.get_attribute('data-issue') == ending['id']
        shot(page, f'line-141-acknowledged-{width}-{scheme}')
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 320])
def test_phone_collection_toolbar_is_two_rows_with_named_icon_tools(browser, live_server, h, width):
    """DL-047: on a phone every collection toolbar is two rows. Row 1: the saved view (its name shrinks, never wraps)
    and Filter, Group, Sort and View settings as named icons; row 2: Search alone, full width. A tool in use shows it by
    shape (Filter's count, a dot for the others), never by colour alone. Nothing overflows sideways at 320."""
    populated_catalogue(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 844})
    try:
        navigate(page, 'rules')
        bar = page.locator('#main .rt > .dt-toolbar')
        expect(bar).to_be_visible()
        saved, search = bar.locator('.dt-saved'), bar.locator('.dt-search-wrap')
        tools = bar.locator(':scope > .dt-tool:not(.dt-saved)')
        assert tools.count() >= 3
        middle = lambda box: box['y'] + box['height'] / 2
        row1 = middle(saved.bounding_box())
        for i in range(tools.count()):
            tool = tools.nth(i)
            assert abs(middle(tool.bounding_box()) - row1) < 4, ('tools share the saved view row', i)
            assert tool.get_attribute('aria-label') and tool.get_attribute('data-tip')
            label = tool.locator('.dt-tool-label')  # View settings is an icon with no label element
            assert not label.count() or label.evaluate('e=>e.getBoundingClientRect().width<=1')  # an icon; words for screen readers
        found, row = search.bounding_box(), bar.bounding_box()
        assert found['y'] > row1, 'Search is on the second row'
        assert found['width'] >= row['width'] - 2, 'Search takes the whole second row'
        label = saved.locator('.dt-tool-label')
        assert label.evaluate("e=>getComputedStyle(e).whiteSpace==='nowrap'&&getComputedStyle(e).textOverflow==='ellipsis'")
        group = bar.get_by_role('button', name=re.compile('^Group'))
        assert group.evaluate("e=>e.classList.contains('on')"), 'the Rules list is grouped by Question by default'
        assert group.evaluate("e=>getComputedStyle(e,'::after').content!=='none'"), 'a grouped list shows a dot on Group'
        assert page.evaluate('document.scrollingElement.scrollWidth <= innerWidth')
        assert bar.evaluate('e=>e.scrollWidth <= e.clientWidth + 1')
    finally:
        context.close()
