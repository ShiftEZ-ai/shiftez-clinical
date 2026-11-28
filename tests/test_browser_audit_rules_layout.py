"""Catalogue lines, unlike legacy policy rows, exercise long chips, date ranges and a tall native table."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in, wait_held, fonts_ready, toolbar, choose  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .test_browser_kept_reads import live_pulse, rebuild
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
    for(const cell of row.querySelectorAll('.rt-who,.rt-where,.rt-ln-who,[data-col=who],[data-col=where]')){
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


# Wrapping and scroll depend on width and height, never on theme (no colour is read): each width once, the tall desktop,
# and both themes across them.
@pytest.mark.parametrize('width,scheme,height', [(1440, 'light', 900), (1100, 'dark', 900), (390, 'light', 900),
                                                (1440, 'dark', 1700)])
def test_populated_catalogue_facts_wrap_and_native_scroll(browser, live_server, h, width, scheme, height):
    """opened lines stay readable, keep their place, and reveal complete facts."""
    rank_count, _ = populated_catalogue(h, large_scope=True)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': height})
    try:
        page.emulate_media(color_scheme=scheme)
        held, released = [], False
        def route_handler(route):
            if released:
                route.continue_()
            else:
                held.append(route)
        page.route('**/api/rules/catalogue', route_handler)
        # The page cannot finish loading while its catalogue read is held: open it from the navigation, not navigate().
        if width < 720:
            page.get_by_role('button', name='Menu', exact=True).click()
        page.get_by_role('button', name='Rules', exact=True).click()
        wait_held(page, held)
        expect(page.locator('#main .loading.page-sk')).to_be_visible()
        expect(page.locator('#main .loading.page-sk')).not_to_have_class(re.compile(r'\bsk-wait\b'))
        released = True
        for route in held:
            route.continue_()
        main = page.locator('#main')
        expect(page.locator('#main[data-loaded-view=rules]')).to_have_count(1)
        expect(main.locator('.rt.fade-in')).to_have_count(1)
        expect(main.locator('[role=alert]')).to_have_count(0)
        expect(main.locator('.rt-scroll')).to_be_visible()
        all_rules = main.locator('.rt-lhead').get_by_role('button', name='All rules', exact=True)
        all_rules.click()
        lines = main.locator('.rt-ln')
        expect(lines).to_have_count(25)
        authored = lines.filter(has=page.get_by_role('checkbox', name=re.compile(r'^Select line \d+ of Duties per week$')))
        expect(authored).to_have_count(24)
        assert_facts(page, '.rt-ln')
        long = lines.filter(has=page.locator(f'.rt-ln-who .rp[title="{LONG_RANK}"]')).last
        expect(long).to_be_visible()
        # Who never overlaps the line's other cells: beside it on a desktop, below it on a phone. (.rt-ln-what only groups
        # them, display:contents on a desktop, so it has no box of its own; a Locator is no evaluate argument either.)
        overlaps = long.evaluate('''row => {const who=row.querySelector('.rt-ln-who').getBoundingClientRect();
            return [...row.querySelectorAll('.rt-ln-when,.rt-ln-num,.rt-ln-flags,.rt-ln-open')].map(n=>({cls:n.className,r:n.getBoundingClientRect()}))
              .filter(({r})=>r.width&&r.height&&Math.min(who.right,r.right)-Math.max(who.left,r.left)>1&&Math.min(who.bottom,r.bottom)-Math.max(who.top,r.top)>1)
              .map(({cls})=>cls);}''')
        assert not overlaps, f'Who overlaps the next line cell: {overlaps}'
        expect(long.locator('.pill-more')).to_have_count(0)
        scroll = main.locator('.rt-scroll')
        size = scroll.evaluate('e=>({height:e.clientHeight,total:e.scrollHeight,width:e.clientWidth,wide:e.scrollWidth})')
        assert size['height'] < size['total'], size
        scroll.evaluate('e=>e.scrollTop=e.scrollHeight')
        # The list's last row (as the table's last tbody row was): the last group's last item. All rules opens the empty
        # rules too, so the last line (.rt-ln) is not the bottom of the list.
        expect(main.locator('.rt-scroll section.rt-q').last.locator(':scope > .rt-list > li').last).to_be_in_viewport()
        fit = scroll.evaluate('''e => {const main=document.querySelector('#main'),r=e.getBoundingClientRect(),
            m=main.getBoundingClientRect(),f=document.querySelector('.app-footer').getBoundingClientRect(),
            key=e.closest('.rt')?.querySelector(':scope > .view-legend:not([hidden])'),
            padding=parseFloat(getComputedStyle(main).paddingBottom),
            bottom=key?key.getBoundingClientRect().top:Math.min(m.bottom,f.top,innerHeight);
            return {bottom:r.bottom,availableBottom:bottom,padding};}''')
        assert fit['bottom'] <= fit['availableBottom'] + 1, fit
        assert fit['availableBottom'] - fit['bottom'] <= fit['padding'] + 2, fit
        more = lines.locator('.pill-more').first
        expect(more).to_have_accessible_name(re.compile(rf'^\d+ more ranks?: show all {rank_count}$'))
        more.click()
        all_who = page.get_by_role('dialog', name='All Who', exact=True)
        expect(all_who.locator('ol li')).to_have_count(rank_count)
        page.keyboard.press('Escape')
        expect(all_who).to_have_count(0)
        selected = page.get_by_role('checkbox', name='Select line 12 of Duties per week', exact=True)
        # Scrolled, with line 12 in view at any viewport height: its row just below the top of the list's own box.
        selected.evaluate('''c => {const s=c.closest('.rt-scroll'),row=c.closest('li');
            const top=row.getBoundingClientRect().top-s.getBoundingClientRect().top+s.scrollTop;
            s.scrollTop=Math.max(1,Math.min(top-8,s.scrollHeight-s.clientHeight));}''')
        expect(selected).to_be_in_viewport()
        before = position(scroll)
        assert before['y'] > 0, before
        selected.check()
        expect(selected).to_be_checked()
        assert position(scroll) == before
        bar = page.get_by_role('toolbar', name='Selected rules and lines', exact=True)
        expect(bar).to_be_visible()
        assert scroll.bounding_box()['y'] + scroll.bounding_box()['height'] <= bar.bounding_box()['y'] + 1
        rule_count = main.locator('.rt-item').count()
        page.get_by_role('searchbox', name='Search rules').fill('Duties per week')
        expect(main.locator('.rt-item')).to_have_count(1)
        assert position(scroll)['y'] == 0
        page.get_by_role('searchbox', name='Search rules').fill('')
        expect(main.locator('.rt-item')).to_have_count(rule_count)
        assert position(scroll)['y'] == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_rules_toolbar_sorts_rule_names_and_lines_by_who(browser, live_server, h):
    """the one-row Rules list keeps the toolbar Sort control, including the line-by-who order."""
    flag(h)
    categories = h.get('admin', '/api/catalogues')['categories']
    first = next(row for row in categories if row['id'] == h.seed['category'])
    h.configure('categories', {'name': 'Zulu rank', 'starts_on': first['starts_on']}, first['id'], first['revision'])
    alpha = h.configure('categories', {'name': 'Alpha rank', 'starts_on': '2026-01-01'})['record']['id']
    catalogue_rule(h, 'duties_per_week', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, who={'conditions': [{'field': 'category_ids', 'values': [h.seed['category']]}]}),
        line({'max': {'value': 6, 'level': 'mandatory'}}, who={'conditions': [{'field': 'category_ids', 'values': [alpha]}]}),
    ])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        choose(page, 'Sort', 'Sort by', 'Name')
        # The list is grouped (by question by default): Name sorts the rules within each group.
        sections = page.locator('#main section.rt-q')
        expect(sections.first).to_be_visible()
        for section in sections.all():
            names = section.locator('.rt-item .rt-rule-link').all_inner_texts()
            assert names == sorted(names, key=str.casefold), names
        item = page.locator('.rt-item').filter(has=page.get_by_role('button', name='Duties per week lines', exact=True))
        item.get_by_role('button', name='Duties per week lines', exact=True).click()
        labels = item.locator('.rt-ln-who').all_inner_texts()
        assert labels == ['Alpha rank', 'Zulu rank'], labels
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme,height', [(1440, 'light', 900), (1440, 'dark', 1700)])
def test_rule_detail_page_keeps_dates_wrapped_scope_and_bulk_bar(browser, live_server, h, width, scheme, height):
    """The one-row Rules list leaves the rule detail table's 17 layout guards intact."""
    _, clinic_count = populated_catalogue(h, large_scope=True)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': height})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        detail = page.locator('.dt-table tbody tr[data-row]')
        expect(page.locator('.rule-valid').first).to_be_visible()
        assert_facts(page, '.dt-table tbody tr[data-row]')
        detail_scroll = page.locator('.dt-body>.table-scroll')
        for label in ['Who', 'Where', 'Valid']:
            detail_scroll.locator('thead').get_by_role('button', name=label, exact=True).click()
            page.get_by_role('menuitemcheckbox', name='Wrap this column', exact=True).click()
        expect(detail.first.locator('[data-col=where] .pill-more')).to_be_hidden()
        assert_facts(page, '.dt-table tbody tr[data-row]')
        assert_readable_wrapped_scope(detail.first, '[data-col=where]', clinic_count, detail_scroll)
        fit = detail_scroll.evaluate('''e => {const main=document.querySelector('#main'),r=e.getBoundingClientRect(),
            m=main.getBoundingClientRect(),f=document.querySelector('.app-footer').getBoundingClientRect(),
            add=main.querySelector('.rc-add').getBoundingClientRect();
            return {bottom:r.bottom,actionTop:add.top,actionBottom:add.bottom,total:e.scrollHeight,height:e.clientHeight,
                availableBottom:Math.min(m.bottom,f.top,innerHeight),padding:parseFloat(getComputedStyle(main).paddingBottom)};}''')
        assert fit['height'] < fit['total'], fit
        assert fit['bottom'] <= fit['actionTop'] + 1, fit
        assert fit['actionBottom'] <= fit['availableBottom'] + 1, fit
        assert fit['availableBottom'] - fit['actionBottom'] <= fit['padding'] + 2, fit
        detail_scroll.evaluate('e=>{e.scrollTop=e.scrollHeight;e.scrollLeft=0;}')
        expect(detail.last).to_be_in_viewport()
        new_line = page.get_by_role('button', name='New line: a person, rank, clinic or #tag', exact=True)
        expect(new_line).to_be_in_viewport()
        assert page.locator('#main').evaluate('e=>e.scrollTop') == 0
        detail.last.locator('.bulk-check').check()
        expect(page.get_by_role('toolbar', name='Bulk actions', exact=True)).to_be_visible()
        page.wait_for_function('''() => {
            const table=document.querySelector('[data-table=rule-line-db]>.dt-body>.table-scroll'),
                add=document.querySelector('#main .rc-add'),bar=document.querySelector('#main .selection-bar:not([hidden])');
            return table.getBoundingClientRect().bottom <= bar.getBoundingClientRect().top &&
                add.getBoundingClientRect().bottom <= bar.getBoundingClientRect().top;
        }''')
        detail_scroll.evaluate('e=>e.scrollTop=e.scrollHeight')
        expect(detail.last.locator('.bulk-check')).to_be_checked()
        expect(detail.last).to_be_in_viewport()
        assert detail.last.evaluate('e=>e.getBoundingClientRect().bottom<=e.closest(".table-scroll").getBoundingClientRect().bottom')
        expect(new_line).to_be_in_viewport()
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


def test_search_keeps_vertical_scroll_and_row_actions_on_the_rules_list(browser, live_server, h):
    populated_catalogue(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1100, 'height': 900})
    try:
        navigate(page, 'rules')
        page.locator('#main .rt-lhead').get_by_role('button', name='All rules', exact=True).click()
        scroll = page.locator('.rt-scroll')
        total_rules = page.locator('.rt-item').count()
        immediate = scroll.evaluate('''e=>{
            e.scrollTop=180;
            const box=e.querySelector('.rt-ln .bulk-check'), before={x:e.scrollLeft,y:e.scrollTop};
            box.click();
            return {before,after:{x:e.scrollLeft,y:e.scrollTop},checked:box.checked};
        }''')
        assert immediate['before']['y'] > 0 and immediate['after'] == immediate['before'], immediate
        selected = page.locator('.rt-ln .bulk-check').first
        expect(selected).to_be_checked()
        assert scroll.evaluate('e=>e.scrollLeft') == 0
        page.get_by_role('searchbox', name='Search rules').fill('Duties per week')
        expect(page.locator('.rt-item')).to_have_count(1)
        assert position(scroll) == {'x': 0, 'y': 0}
        page.get_by_role('searchbox', name='Search rules').fill('')
        expect(page.locator('.rt-item')).to_have_count(total_rules)
        assert position(scroll) == {'x': 0, 'y': 0}
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'auditor'])
def test_switching_rule_and_issue_views_starts_at_their_identifying_facts(browser, live_server, h, role):
    populated_catalogue(h, ends_on='2026-10-20')
    ending = next(x for x in h.get('coordinator', '/api/rules/catalogue')['attention'] if x['kind'] == 'ending')
    h.command('coordinator', 'rule.acknowledge', {'fingerprints': [ending['fingerprint']], 'note': 'Same issue columns fixture'})
    context, page = sign_in(browser, live_server, role, {'width': 390, 'height': 844})
    try:
        rules_by_family(h, page, role)
        navigate(page, 'rules')
        scroll = page.locator('#main .rt-scroll')
        assert scroll.evaluate('e=>e.scrollLeft') == 0

        def view(label):
            page.locator('#main .dt-saved').click()
            page.get_by_role('menuitemradio', name=re.compile('^' + re.escape(label) + r'\b')).click()

        view('Needs attention')
        expect(page.locator('.rt-issue').first).to_be_visible()
        assert position(scroll)['y'] == 0
        expect(page.locator('.rt-issue .rt-rule-link').first).to_be_in_viewport()
        expect(page.locator('.rt-iline .rt-n').first).to_be_in_viewport()
        # A phone reads each issue as one block (issue 1639): neither issue view scrolls sideways, so the rule name and
        # the actions never part.
        for label in ['Acknowledged', 'Needs attention']:
            scroll.evaluate('e=>e.scrollLeft=e.scrollWidth')
            assert position(scroll)['x'] == 0, position(scroll)
            view(label)
            expect(page.locator('.rt-issue').first).to_be_visible()
            expect(page.locator('.rt-issue .rt-rule-link').first).to_be_in_viewport()
        view('All rules')
        expect(page.locator('.rt-scroll')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        expect(page.locator('.rt-item .rt-rule-link').first).to_be_attached()
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
        # Another person's change rebuilds the page in the background (as a live update after Undo can): the list stays
        # on screen, moved into the new page while its catalogue is read, at the same place, and the rebuilt list too.
        held, moved, catalogue = [], [0], re.compile(r'/api/rules/catalogue(\?|$)')
        live_pulse(page, 'workspace', moved)  # someone else changed a draft: the Rules page follows that scope
        page.route(catalogue, lambda route: held.append(route))
        shown = scroll.element_handle()
        rebuild(page, held, moved, 1)
        moved[0] = 0
        assert position(scroll) == before
        page.unroute(catalogue)
        for route in held:
            route.continue_()
        page.wait_for_function('e => !e.isConnected', arg=shown)
        expect(rows).to_have_count(24)
        assert position(scroll) == before
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(390, 'dark'), (1440, 'light')])
def test_three_digit_line_numbers_stay_whole_in_rules_and_issue_views(browser, live_server, h, width, scheme):
    flag(h)
    today = date.fromisoformat(h.get('coordinator', '/api/rules/catalogue')['day'])
    future = today + timedelta(days=365)
    # Separate real validity periods avoid duplicates or artificial DOM numbering. The ending line is genuinely 141;
    # the Rules-list rail exception is a recorded design exception.
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
