"""Release QA sweep 2026-10-20: regressions for each finding, on the overflow guard's adversarial data
(very long and Arabic names, an unbreakable place name, three waiting leave requests, 120 audit events)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, fonts_ready, no_opening_hours  # noqa: F401
from .test_browser import wait_held, whole_list
from .test_browser_overflow import adversarial, ARABIC, LONG
from .rules_support import rule_status

pytestmark = pytest.mark.browser


def test_coordinator_desktop_rules_requests_coverage_setup_and_people(browser, live_server, h):
    no_opening_hours(h)  # setup still incomplete, so Configuration opens on the Setup guide
    adversarial(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        # The seeded Fair share catalogue rule (migration 0109) is titled and summarised by its catalogue entry,
        # in the topic its numbers belong to (never "catalogue" under Other rules).
        row = main.locator('.rl-row').filter(has_text='Fair share')
        expect(row.locator('.rl-text strong')).to_have_text('Fair share')
        expect(row).to_contain_text('Share eligible workload')
        expect(main.locator('.rl-text strong').filter(has_text=re.compile(r'^catalogue$'))).to_have_count(0)
        body = row.evaluate("r => r.closest('.rule-group-body').id")
        expect(main.locator(f'[aria-controls="{body}"] .rule-group-name')).to_have_text('Targets & commitments')
        expect(main.locator('.rule-group-name').filter(has_text='Other rules')).to_have_count(0)
        row.locator('.rl-text').click()
        expect(page.locator('.dock-panel')).to_contain_text('Fair share')
        page.keyboard.press('Escape')
        # Nothing waiting and no filter: says so, not "No matching rules".
        rule_status(page, 'Waiting for approval')
        expect(main.get_by_role('heading', name='Nothing waiting for approval', exact=True)).to_be_visible()
        expect(main.get_by_text('No matching rules')).to_have_count(0)

        # Requests: a status pill stays on one line beside a very long name.
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        badges = main.locator('td[data-col=status] .badge')
        expect(badges).to_have_count(3)
        for k in range(3):
            assert badges.nth(k).evaluate('b => b.getClientRects().length === 1 && b.getBoundingClientRect().height < 26'), 'status pill wraps'
        # Filtered to nothing: the empty state says so; the toolbar's strip has the one way back (C5).
        main.get_by_role('searchbox').first.fill('zzqqxx-no-match')
        expect(main.get_by_role('heading', name='No matching requests', exact=True)).to_be_visible()
        expect(main.locator('.dt-filtered').get_by_role('button', name='Show all', exact=True)).to_be_visible()
        expect(main.locator('.empty-state').get_by_role('button', name='Show all')).to_have_count(0)

        # Coverage: a long place name truncates (full name in its tooltip); every day stays in view.
        navigate(page, 'schedule/coverage')
        table = main.locator('.coverage-table')
        heads = table.locator('thead th')
        expect(heads).to_have_count(9)
        fonts_ready(page)
        assert table.evaluate("""t => { const box = t.closest('.table-scroll').getBoundingClientRect();
            return [...t.querySelectorAll('thead th')].every(th => th.getBoundingClientRect().right <= box.right + 1); }"""), 'a day is out of view'
        long_row = table.locator('tbody th[scope=row]').first
        expect(long_row).to_have_attribute('title', re.compile('Supercalifragilistic'))
        # Weekend days are shaded; a duty's name shows once, not again on its next row (another time or place).
        expect(table.locator('thead th.weekend')).to_have_count(2)
        assert table.evaluate("""t => { const names = [...t.querySelectorAll('tbody tr:not(.cov-sum) th[scope=row]')]
            .map(th => th.querySelector('.cov-name:not(.sr-only)')?.textContent ?? null);
            return names.every((n, i) => n === null || i === 0 || n !== names[i - 1]); }"""), 'a duty name repeats'

        # Setup guide › Data health (maintenance: clinics without hours are the Opening hours step's to say, so every
        # remaining item carries its own fix). Configuration keeps the previous card inert until its replacement is ready.
        navigate(page, 'configuration')
        expect(main.locator('.setup-health')).to_be_visible()
        expect(main.locator('[inert] .setup-health')).to_have_count(0)

        # People with a person open: the toolbar keeps one row.
        navigate(page, 'people')
        main.locator('.dt-body tbody tr.dt-open strong').first.click()
        expect(page.locator('.dock-panel')).to_be_visible()
        fonts_ready(page)
        tops = main.locator('.dt-toolbar').first.evaluate("""t => [...t.children].filter(n => n.offsetParent && !n.hidden)
            .map(n => Math.round(n.getBoundingClientRect().top))""")
        assert max(tops) - min(tops) < 24, tops  # one row (controls of different heights centre on it)
    finally:
        context.close()


def test_approver_phone_home_needs_line_is_short(browser, live_server, h):
    adversarial(h)
    context, page = sign_in(browser, live_server, 'approver', viewport={'width': 390, 'height': 844})
    try:
        navigate(page, 'home')
        need = page.locator('.home-needs-list li').filter(has_text=re.compile('Leave requests|Requests to decide')).locator('.home-need-text small')
        # The first request and how many more, not every requester's full name.
        expect(need).to_have_text(re.compile(r' · 2 more$'))
        assert need.inner_text().count(LONG.split()[3].split('-')[0]) == 1
    finally:
        context.close()


def test_staff_phone_schedule_focus_toolbar_and_home_loading(browser, live_server, h):
    adversarial(h)
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 844})
    try:
        navigate(page, 'schedule/list')
        main = page.locator('#main')
        # Nothing published and nothing filtered: the shared words for a read-only Schedule (scheduleEmpty, S-01), no "match" wording.
        expect(main.get_by_role('heading', name='You have no duties this month', exact=True)).to_be_visible()
        # Other ranges never read "this 2 weeks" / "this rota": the shared wording the empty states use.
        assert page.evaluate("""async () => { const {rangeWords} = await window.importBundle('planner_logic.js');
            return ['month', 'week', 'day', '2 weeks', '3 days', 'period', 'rota'].map(u => 'No sessions ' + rangeWords(u).where); }""") == [
            'No sessions this month', 'No sessions this week', 'No sessions this day', 'No sessions in this period',
            'No sessions in this period', 'No sessions in this period', 'No sessions in this rota']
        # The main area takes focus after navigation, without a ring around the whole page.
        expect(main).to_be_focused()
        assert main.evaluate('m => getComputedStyle(m).outlineStyle') == 'none'
        # No control is left alone on a row of the toolbar: the search and the period navigation have rows of their own
        # (DL-047: row 1 the saved view and the icon tools, then the period, then Search).
        rows = page.locator('.planner-toolbar').evaluate("""t => { const r = {};
            for (const n of t.children) { if (!n.offsetParent || n.matches('.dt-search-wrap, .toolbar-search, .wk-nav')) continue;
              const b = n.getBoundingClientRect(), y = Math.round((b.top + b.bottom) / 2 / 20); r[y] = (r[y] || 0) + 1; }
            return Object.values(r); }""")
        assert all(n > 1 for n in rows), page.locator('.planner-toolbar').evaluate("t => [...t.children].map(n => [n.className, n.offsetParent ? 1 : 0, Math.round(n.getBoundingClientRect().left), Math.round(n.getBoundingClientRect().top), Math.round(n.getBoundingClientRect().width)])")
        # DL-047 at 390: row 1 holds the saved view and every icon tool.
        first = page.locator('.planner-toolbar').evaluate("""t => { const mid = n => { const b = n.getBoundingClientRect(); return Math.round((b.top + b.bottom) / 2); };
            const saved = t.querySelector(':scope > .dt-saved'), tools = [...t.querySelectorAll(':scope > .dt-tool:not(.dt-saved)')].filter(n => n.offsetParent);
            return {saved: !!saved && !!saved.offsetParent, tools: tools.length, off: tools.filter(n => Math.abs(mid(n) - mid(saved)) > 4).map(n => n.getAttribute('aria-label') || n.textContent)}; }""")
        assert first['saved'] and first['tools'] >= 1 and first['off'] == [], first
        # Arabic-only names get a short code in their own script.
        assert page.evaluate(f"async () => (await window.importBundle('dom.js')).autoCode({ARABIC!r})") not in ('', '—', '–')
        # A fresh tab has no retained calendar reply to show before the held read finishes.
        fresh = context.new_page()
        held = []
        fresh.route(re.compile(r'/api/calendar\?.*\bperson_id='),
                    lambda route: route.continue_() if held else held.append(route))
        fresh.goto(live_server + '/#home', wait_until='domcontentloaded')
        wait_held(fresh, held)
        coming_up = fresh.get_by_role('region', name='Coming up', exact=True)
        expect(coming_up.locator('.loading:not(.sk-wait)')).to_be_visible()
        held[0].continue_()
        expect(coming_up.locator('.loading')).to_have_count(0)
    finally:
        context.close()


def test_auditor_requests_empty_state_and_audit_log_words(browser, live_server, h):
    adversarial(h)
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': 1440, 'height': 900})
    try:
        # Requests stays for auditors; its empty state fits the person facts they read, never "Ask for leave".
        navigate(page, 'requests')
        expect(page.locator('#main').get_by_role('heading', name='No person facts waiting', exact=True)).to_be_visible()
        expect(page.locator('#main').get_by_text('Ask for leave')).to_have_count(0)
        navigate(page, 'system/audit')
        main = page.locator('#main')
        expect(main.locator('.audit-text').filter(has_text='changed the members of a tag').first).to_be_visible()
        expect(main.locator('.audit-row .role-chip').filter(has_text=re.compile(r'^Tags$')).first).to_be_visible()
        expect(main.locator('.audit-text').filter(has_text=re.compile(r' members tag'))).to_have_count(0)
    finally:
        context.close()
