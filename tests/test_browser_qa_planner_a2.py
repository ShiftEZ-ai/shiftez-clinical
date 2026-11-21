"""Planner QA (stream A2), the items deferred in stream A: the rota and view live in the address; an approver's return
reason stays on the draft; the page's own save is not refreshed a second time by the live pulse; auto-schedule can
run on the open sessions only; Recent runs and Compare rotas are compact; a new rota starts after the latest one."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import auto_setup, wait_run_created, browser, live_server, sign_in, navigate, pick_view, planner_tool, open_scheduling  # noqa: F401
from .test_browser_cp_sat import cp_server  # noqa: F401 (one fixture: server and in-test worker both on CP-SAT)

pytestmark = pytest.mark.browser


def settled(page):
    expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible()
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)


def test_the_rota_and_view_survive_a_reload_and_a_shared_link(browser, live_server, h):
    first = h.shift(h.workspace('Address rota one'))['workspace']
    second = h.shift(h.workspace('Address rota two', '2026-10-11', '2026-10-18'),
                     '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        page.locator('.rota-switcher').click()
        page.get_by_role('listbox', name='Drafts').get_by_role('option', name=re.compile('Address rota two')).click()
        expect(page.locator('.rota-switcher h1')).to_have_text('Address rota two')
        pick_view(page, 'Month')
        # The view, its day and the draft live in the address.
        expect(page).to_have_url(re.compile(rf"#schedule/month/\d{{4}}-\d\d-\d\d\?edit={second['id']}$"))
        page.reload(); settled(page)
        expect(page.locator('.rota-switcher h1')).to_have_text('Address rota two')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Month view')
        # A link to the other rota opens it; a link to a rota that does not exist falls back with a message.
        page.evaluate("id => {location.hash = 'planner/' + id + '/list.rota'}", first['id'])
        expect(page.locator('.rota-switcher h1')).to_have_text('Address rota one')
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'List view')
        page.evaluate("() => {location.hash = 'planner/00000000-0000-0000-0000-000000000000/week.week'}")
        expect(page.locator('.toast').last).to_contain_text('That rota is not available')
        expect(page.locator('#main[data-loaded-view=schedule] .planner-main')).to_be_visible()
    finally:
        context.close()


def test_the_approvers_return_reason_stays_on_the_draft(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Returned planner rota'))['workspace'], 'submit')['workspace']
    h.command('approver', 'workspace.reopen', {'reason': 'Clinic B is short on Tuesday'}, w['id'], w['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        expect(page.locator('.planner-main')).to_contain_text('Returned for changes by')
        expect(page.locator('.planner-main')).to_contain_text('“Clinic B is short on Tuesday”')
    finally:
        context.close()


def test_own_save_is_not_refreshed_twice_by_the_live_pulse(browser, live_server, h):
    w = h.shift(h.workspace('Pulse rota'))['workspace']
    h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The live stream opens on the first periodic tick; its first pulse records the starting point.
        with page.expect_request(re.compile(r'/api/events'), timeout=30000):
            navigate(page, 'planner'); settled(page)
        page.wait_for_timeout(1000)
        lists, sent = [], []
        page.on('request', lambda r: lists.append(r.url) if '/api/workspaces?limit=50' in r.url else None)
        page.on('request', lambda r: sent.append(r.url.split('/api/')[1]) if '/api/' in r.url and not re.search(r'/api/(pulse|events)', r.url) else None)
        page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True).click()  # Undo lives in the top bar (S-05)
        expect(page.locator('.toast').last).to_contain_text('Undone')
        page.wait_for_timeout(2500)  # long enough for the live pulse of this change to arrive and be handled
        assert lists == [], lists  # its own pulse reads nothing
        # Undo applies in place: from the reply's delta (edit_delta.ts; the draft read again only when the reply has none),
        # never the session, the lists in memory or the rota list.
        detail = 'workspaces/' + w['id']
        reads = sum(u in (detail, detail + '?view=schedule') for u in sent)
        assert reads <= 1 and not [u for u in sent if u == 'me' or u.startswith(('catalogues', 'workspaces?'))], sent
    finally:
        context.close()


def test_auto_schedule_on_open_sessions_reports_problems_elsewhere(browser, cp_server, h):
    from apps.worker.main import process_one
    people = h.seed['people']
    h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.shift(h.workspace('Open-only rota'), people=people[:1])['workspace']
    w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', people=people[:1])['workspace']
    w = h.shift(w, '2026-10-06T08:00:00+03:00', '2026-10-06T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        open_scheduling(page)
        auto_setup(page.locator('#auto-panel'))
        page.get_by_label('Keep existing assignments', exact=True).check()  # keep every assignment
        page.get_by_label('Assign into', exact=True).select_option('open')
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        panel = page.locator('#auto-panel')
        expect(panel).to_contain_text('Outside these sessions · left as they are', timeout=15000)
        expect(panel.get_by_role('region', name='Outside these sessions · left as they are').locator('.issue-card')).to_have_count(1)
        # Recent runs: one compact row per run (the latest first), with its outcome and a Review button (the check runs from What to fill).
        auto_setup(panel)
        page.get_by_role('button', name='Check the whole rota', exact=True).click()
        process_one(h.app.state.engine)
        rows = panel.locator('.run-list li')
        expect(rows).to_have_count(2, timeout=15000)
        expect(rows.nth(0)).to_contain_text('Full rota check')
        row = rows.nth(1)
        expect(row).to_contain_text('Auto-schedule')
        expect(row.get_by_role('button', name=re.compile('^Review Auto-schedule'))).to_be_visible()
        assert panel.evaluate('p => p.scrollWidth <= p.clientWidth + 1')
    finally:
        context.close()


def test_a_blocked_run_shows_each_conflict_as_a_card_with_who_which_session_and_fixes(browser, cp_server, h):
    """A run that must keep conflicting work stops before searching; each conflict is an issue card naming the person,
    the session (duty, clinic, day, time) and the rule, with its fixes, grouped by kind with a count and the first few
    shown. Removing the person from the session is an ordinary undoable edit."""
    from apps.worker.main import process_one
    alex = h.seed['people'][0]
    rule = h.policy({'kind': 'min_rest', 'hours': 12})
    w = h.workspace('Conflicted rota')
    for day in range(4, 11):  # Alex works a morning and an afternoon every day: seven rest breaches
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[alex])['workspace']
        w = h.shift(w, f'2026-10-{day:02}T13:00:00+03:00', f'2026-10-{day:02}T17:00:00+03:00', people=[alex])['workspace']
    context, page = sign_in(browser, cp_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        open_scheduling(page)
        auto_setup(page.locator('#auto-panel'))
        page.get_by_label('Keep existing assignments', exact=True).check()  # keep every assignment
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        panel = page.locator('#auto-panel')
        found = panel.get_by_role('region', name='Resolve these first')
        expect(found).to_be_visible(timeout=15000)
        kind = found.get_by_role('list', name=re.compile(rf"^{re.escape(rule['name'])} \(\d+\)$"))
        expect(kind.locator(':scope > .issue-card')).to_have_count(5)  # the first five of seven
        card = kind.locator(':scope > .issue-card').first
        expect(card).to_contain_text('Alex Morgan')
        expect(card).to_contain_text(re.compile(r'Clinical review session · .+ · \w{3} \d+ Oct 2026 · \d\d:\d\d–\d\d:\d\d'))
        expect(card).to_contain_text(f"Alex Morgan: {rule['name']}")  # what is wrong: the rule it breaks
        expect(card).to_contain_text('Mandatory rule')
        # The one issue list (UIUX-AUDIT C1): a card carries the D78 shape (triangle for must fix), never a colour-only dot.
        expect(card.locator('.iss-glyph.error')).to_have_count(1)
        expect(found.locator('.issue-dot')).to_have_count(0)
        for name in ('Show on schedule', 'Details'):
            expect(card.get_by_role('button', name=name, exact=True)).to_be_visible()
        expect(card.get_by_role('button', name='Pick someone else instead of Alex Morgan', exact=True)).to_be_visible()
        found.get_by_role('button', name=re.compile(r'^Show all \d+$')).click()
        assert kind.locator(':scope > .issue-card').count() > 5
        card.get_by_role('button', name='Remove Alex Morgan from this session', exact=True).click()
        expect(page.locator('.toast').last.get_by_role('button', name='Undo')).to_be_visible()
    finally:
        context.close()


def test_a_new_rota_starts_after_the_latest_one(browser, live_server, h):
    h.workspace('Latest rota', '2026-11-01', '2026-11-29')  # four weeks
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner'); settled(page)
        page.locator('.rota-switcher').click()
        page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('button', name='New rota').click()
        dialog = page.get_by_role('dialog', name='New rota')
        expect(dialog.get_by_label('First day', exact=True)).to_have_value('Sun 29 Nov 2026')
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Sat 26 Dec 2026')
    finally:
        context.close()
