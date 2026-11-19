"""Rules at department scale (audit RU-1, RU-2, RU-3, RU-5, RU-6): a 1,000-line rule's page works its draft changes out
once per edit and checks a Keep change once; the Rules list waits for a pause in typing before it searches, and a tick
or opening one rule leaves the other rows as they are. Work is counted, not timed (wall-clock on a shared runner is
noise), as test_browser_rule_catalogue.test_a_thousand_lines_open_and_filter_within_budget does."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line

pytestmark = pytest.mark.browser
LINES = 1000


def big_rule(h):
    flag(h)
    day = lambda n: str(date(2027, 1, 1) + timedelta(days=n))  # noqa: E731 - one line per day: none is the same line
    return catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}}, starts_on=day(n), ends_on=day(n + 1)) for n in range(LINES)])


def checks(page):
    sent = []
    page.on('request', lambda r: sent.append(r.url) if r.method == 'POST' and re.search(r'/api/rules/catalogue/[^/]+/check$', r.url) else None)
    return sent


def test_a_keep_change_on_a_thousand_line_rule_is_worked_out_once_and_checked_once(browser, live_server, h):
    """RU-1: the table's cells read the draft's changes worked out once per edit (each number cell compared every line:
    millions of comparisons, 3 s per Keep change). RU-6: the page, the line's reach and its counting share one check."""
    big_rule(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        expect(main.locator('tr[data-row]').first).to_be_visible()
        main.locator('tr[data-row]').first.locator('td[data-col="who"]').click()
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        panel.get_by_label('Note', exact=True).fill('Agreed at the rota meeting')
        sent = checks(page)
        # The click's synchronous work: one table redraw with the draft changed. JSON.stringify counts the comparisons.
        work = page.evaluate("""() => {const keep=[...document.querySelectorAll('.rc-line-panel button')].find(b=>b.textContent.trim()==='Keep change');
            const real=JSON.stringify;let n=0;JSON.stringify=function(...a){n++;return real.apply(this,a);};
            try{keep.click();}finally{JSON.stringify=real;}return n;}""")
        assert work < 30 * LINES, work  # ~4 cells × 1,000 rows × 1,000 lines × 2 before
        page.clock.run_for(2000)  # past the page's 300 ms check and the panel's own reach and counting reads
        expect(page.locator('.app-footer')).to_contain_text('1 change not yet proposed')
        page.clock.run_for(1000)
        assert len(sent) == 1, sent
    finally:
        context.close()


def test_rules_search_waits_for_a_pause_in_typing(browser, live_server, h):
    """RU-3: thirteen keystrokes draw the list once (each used to draw it, opening every hit)."""
    big_rule(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(main.locator('li.rt-item').first).to_be_visible()
        page.evaluate("""() => {const body=document.querySelector('.rt-body');const replace=body.replaceChildren.bind(body);window.__draws=0;
            body.replaceChildren=(...children)=>{window.__draws++;return replace(...children);};}""")
        box = main.get_by_role('searchbox', name='Search rules')
        box.click()
        page.keyboard.type('Duties per week')
        assert page.evaluate('window.__draws') == 0
        page.clock.run_for(300)
        expect(page.locator('.app-footer .dt-count')).to_have_text(re.compile(r'^1 of \d+ rules · '))
        assert page.evaluate('window.__draws') == 1
    finally:
        context.close()


def test_ticking_or_opening_one_rule_leaves_the_other_rows_as_they_are(browser, live_server, h):
    """RU-2: a tick updates the boxes and the bar in place; opening one rule draws only that rule's rows."""
    big_rule(h)
    catalogue_rule(h, 'rest_between_duties', [line({'min': {'value': 11, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        other = main.locator('li.rt-item').filter(has_text='Rest between duties')
        expect(other).to_be_visible()
        other.evaluate('row => { row.dataset.kept = "yes"; }')
        main.get_by_role('button', name='Duties per week lines').click()
        expect(main.locator('li.rt-ln')).to_have_count(LINES)
        assert other.evaluate('row => row.isConnected && row.dataset.kept === "yes"'), 'opening one rule redrew the others'
        first = main.locator('li.rt-ln').first
        first.evaluate('row => { row.dataset.kept = "yes"; }')
        main.locator('li.rt-ln').nth(1).locator('input.bulk-check').check()
        expect(page.locator('.selection-bar:not([hidden])')).to_contain_text('1 item')
        assert first.evaluate('row => row.isConnected && row.dataset.kept === "yes"'), 'a tick redrew the table'
        assert other.evaluate('row => row.isConnected')
        # The rule's tri-state box follows the tick in place.
        expect(main.locator('li.rt-item').filter(has_text='Duties per week').locator('input.bulk-check')).to_have_js_property('indeterminate', True)
    finally:
        context.close()


def test_a_thousand_person_exceptions_open_with_linear_who_work(browser, live_server, h):
    flag(h)
    anchor = line({'max': {'value': 5, 'level': 'mandatory'}})
    start = date(2027, 1, 1)
    personal = [line({'max': {'value': 3, 'level': 'mandatory'}},
                     who={'conditions': [{'field': 'person_ids', 'values': h.seed['people'][:1]}]},
                     starts_on=str(start + timedelta(days=n)), ends_on=str(start + timedelta(days=n + 1)))
                for n in range(LINES)]
    catalogue_rule(h, 'duties_per_week', [anchor, *personal])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        item = page.locator('li.rt-item').filter(has=page.get_by_role('button', name='Duties per week lines', exact=True))
        expect(item).to_be_visible()
        work = item.evaluate('''item => {const open=item.querySelector('button[aria-label="Duties per week lines"]');
            const real=JSON.stringify;let calls=0;JSON.stringify=function(...args){calls++;return real.apply(this,args);};
            try{open.click();}finally{JSON.stringify=real;}return calls;}''')
        expect(item.locator('li.rt-ln')).to_have_count(LINES + 1)
        expect(item.locator('ol.rt-exc > li.rt-ln')).to_have_count(LINES)
        expect(item.locator('ol.rt-lns').first.locator(':scope > li.rt-ln')).to_have_attribute('data-line', anchor['id'])
        assert work < 30 * (LINES + 1), work
    finally:
        context.close()
