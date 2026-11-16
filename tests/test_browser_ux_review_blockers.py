"""UX review 21 Oct 2026 (rc.16 frontend batch 3): auto-schedule keeps assignments by default, one stale-data banner,
one source for the published footer health, the Schedule keeps its source, Undo for bulk draft edits, warnings grouped
by rule and person, rule detail on the rota covering today, role removal in the row menu, and word-by-word search."""
import re
from datetime import timedelta
from zoneinfo import ZoneInfo
import pytest
from playwright.sync_api import expect
from apps.api import models as m
from apps.api.db import transaction
from .test_browser import auto_setup, wait_run_created, browser, live_server, sign_in, navigate, open_scheduling, open_rota, open_rule, planner_tool  # noqa: F401
from .test_browser_qa_planner_a2 import settled
from .test_series_reconciliation import template
from packages.domain.time import now as clock_now

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('mode', ['fill_gaps', 'rebalance'], ids=['default-keep', 'unticked-replace'])
def test_greedy_auto_schedule_keeps_existing_assignments_by_default(browser, live_server, h, mode):
    """A-01: the greedy scheduler (the default engine) starts from the department setting (Keep assignments and fill
    gaps), like CP-SAT, and unticking it really asks this run to replace them."""
    w = h.shift(h.workspace('Keep by default'), people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        auto_setup(panel)
        keep = panel.get_by_label('Keep existing assignments', exact=True)
        expect(keep).to_be_checked()
        if mode == 'rebalance':
            keep.uncheck()
        panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(panel.get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        job = h.get('coordinator', '/api/jobs')['jobs'][0]
        with transaction(h.app.state.engine) as db:
            snapshot = db.get(m.Job, job['id']).input_snapshot
            assert snapshot['generation_engine'] == 'legacy'
            assert snapshot['generation_options']['mode'] == mode
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_failed_refresh_keeps_the_page_with_one_stale_banner_and_retry(browser, live_server, h, scheme):
    """G-01/G-02: data on screen stays with its header and toolbar, says how old it is, and retries in place; the footer
    dot says Offline in words. A page that shows nothing says only that it did not load (never "may be stale")."""
    context, page = sign_in(browser, live_server, 'admin')
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        search = page.locator('#main').get_by_role('searchbox').first
        expect(search).to_be_visible()
        page.route('**/api/**', lambda route: route.fulfill(status=500, content_type='application/json',
                                                            body='{"error":{"code":"INTERNAL_ERROR","message":"Synthetic outage"}}'))
        page.evaluate("window.dispatchEvent(new Event('online'))")  # a refresh, as the connection coming back asks for
        banner = page.locator('#main > .stale-banner')
        expect(banner).to_have_count(1)
        expect(banner).to_contain_text(re.compile(r'^Couldn’t refresh · showing data from \d\d:\d\d'))
        expect(search).to_be_visible()  # the toolbar stays
        expect(page.locator('#main tbody tr[data-row]').first).to_be_visible()  # and the data
        expect(page.locator('.toast').filter(has_text='could not be retrieved')).to_have_count(0)  # one message, not two
        # A server error is named as one, its message as text on the line and Report this problem (not "Offline"; N-11).
        expect(banner.get_by_text('Synthetic outage')).to_be_visible()
        expect(banner.get_by_role('button', name='Report this problem')).to_be_visible()
        expect(page.locator('#connection')).to_have_accessible_name('Server error · retrying')
        expect(page.locator('#connection .status-word')).to_have_text('Server error')
        page.unroute('**/api/**')
        banner.get_by_role('button', name='Retry', exact=True).click()
        expect(banner).to_have_count(0)
        expect(page.locator('#connection')).to_have_accessible_name('Connected')
        # Failing once, then any page that reads successfully: the banner never shows over fresh data.
        page.route('**/api/**', lambda route: route.fulfill(status=500, content_type='application/json',
                                                            body='{"error":{"code":"INTERNAL_ERROR","message":"Synthetic outage"}}'), times=3)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        expect(banner).to_have_count(1)
        page.unroute('**/api/**')
        navigate(page, 'requests')
        expect(page.locator('#main > .stale-banner')).to_have_count(0)
        # A page that showed nothing: one message with Retry inside it, no stale banner. Workload is opened here for the
        # first time: a page this tab has read before opens from its kept replies (api.ts keptReads) and never shows nothing.
        navigate(page, 'people')
        page.route(re.compile(r'/api/publications$'), lambda route: route.abort())
        page.evaluate("location.hash='insights/workload'")
        alert = page.locator('#main [role=alert]')
        expect(alert).to_contain_text('Couldn’t load Workload.')  # DL-045 (c5a282a5d): named by the open tab
        expect(alert).not_to_contain_text('stale')
        expect(page.locator('#main > .stale-banner')).to_have_count(0)
        page.unroute(re.compile(r'/api/publications$'))
        alert.get_by_role('button', name='Retry', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'insights')
    finally:
        context.close()


def test_a_failed_page_read_keeps_the_banner_and_its_time_through_a_healthy_pulse(browser, live_server, h):
    """G-01: the session and lists refresh fine but the page's own read fails: the page keeps its data under the banner,
    and a later healthy connection check neither clears it nor moves "saved data from" to now."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'insights/workload')
        page.route(re.compile(r'/api/publications$'), lambda route: route.fulfill(status=500, content_type='application/json',
                                                                                body='{"error":{"code":"INTERNAL_ERROR","message":"Synthetic page outage"}}'))
        # A refresh (/api/me and catalogues succeed): someone else's change moves the department's activity, and coming
        # back online asks for the pulse at once (app.ts onPulse; an unmoved pulse reads nothing, 60315a5de).
        h.workspace('Fictional rota that moves the pulse')
        page.evaluate("window.dispatchEvent(new Event('online'))")
        banner = page.locator('#main > .stale-banner')
        expect(banner).to_contain_text('Synthetic page outage')
        said = banner.inner_text()
        with page.expect_response(re.compile(r'/api/pulse$')) as pulse:
            page.clock.run_for(70_000)  # past the live stream's quiet minute: the periodic connection check runs
        assert pulse.value.ok
        expect(page.locator('#connection')).to_have_accessible_name('Connected')
        expect(banner).to_have_count(1)
        assert banner.inner_text() == said
    finally:
        context.close()


def _advisory_rota(h, name, start='2026-10-04', end='2026-10-11'):
    """One person on three days against an advisory "at most 1 duty a week": warnings on the 2nd and 3rd day."""
    alex = h.seed['people'][0]
    rule = h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, severity='advisory')
    w = h.workspace(name, start, end)
    for day in (4, 5, 6):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[alex])['workspace']
    return h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace'], rule


def test_published_footer_health_reads_the_stored_check_in_every_view(browser, live_server, h):
    """S-02: Week and List say the same, from the rota's stored check, even when a live re-check would differ."""
    w, rule = _advisory_rota(h, 'Published warnings')
    stored = h.publish(h.detail(w)['workspace'])
    warnings = sum(f['severity'] != 'error' for f in h.detail(w)['workspace']['validation']['findings'])
    assert warnings and stored
    h.command('approver', 'policy.retire', {'reason': 'The live re-check now has no warnings'}, rule['id'], rule['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        for layout in ('week', 'list'):
            if layout == 'week':  # the live re-check is read once per visit; the footer must not follow it
                with page.expect_response(re.compile(r'/api/publications/[^/]+/coverage$')):
                    navigate(page, f'schedule/{layout}/2026-10-04?edit=none')
            else:
                navigate(page, f'schedule/{layout}/2026-10-04?edit=none')
            settled(page)
            pills = page.locator('.app-footer .page-status .health-pills')
            # The published layer's Fix-list causes are the stored checks' actionable count.
            expect(pills).to_contain_text(f"{warnings} could improve")
    finally:
        context.close()


def test_schedule_opens_on_the_rota_covering_today_and_views_keep_the_source(browser, live_server, h):
    """S-03/S-04: today is covered by a published rota, so the Schedule opens on it (not on a future draft), and Year and
    Week keep showing it; the draft is one choice away in the draft switcher."""
    today = h.shift(h.workspace('Covers today', '2026-09-27', '2026-10-04'), '2026-10-01T08:00:00+03:00', '2026-10-01T12:00:00+03:00')['workspace']
    h.publish(h.detail(today)['workspace'])
    future = h.workspace('Future draft', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        settled(page)
        expect(page).to_have_url(re.compile(r'#schedule/week/2026-(09-2\d|10-0\d)\?edit=none$'))
        for key, name in (('y', 'year'), ('w', 'week'), ('m', 'month')):
            page.locator('body').press(key)
            expect(page).to_have_url(re.compile(rf'#schedule/{name}/[\d-]+\?edit=none$'))
        assert future['id'] not in page.url
    finally:
        context.close()


def test_a_draft_covering_today_opens_on_todays_week(browser, live_server, h):
    """S-04: the draft chosen because it covers today opens on today's week, not on its own first week. "Today" is the
    suite's clock (packages.domain.time.now, pinned by tests/conftest.py) in the department's zone."""
    today = clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date()
    w = h.workspace('Covers today and more', (today - timedelta(days=13)).isoformat(), (today + timedelta(days=22)).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        settled(page)
        expect(page).to_have_url(re.compile(rf"#schedule/week/{today.isoformat()}\?edit={w['id']}$"))
    finally:
        context.close()


def test_bulk_add_sessions_offers_undo_and_rota_tools_are_sectioned(browser, live_server, h):
    """S-14: "N sessions added" carries Undo; Undo last edit is a visible control; Rota tools has labelled sections."""
    template(h, days=(0,)); template(h, days=(1,))
    w = h.workspace()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        expect(page.locator('.planner-bar').get_by_role('button', name='Undo last edit', exact=True)).to_be_visible()
        page.get_by_role('button', name='Rota tools', exact=True).click()
        menu = page.get_by_role('menu', name='Rota tools', exact=True)
        expect(menu.get_by_role('group')).to_have_count(3)
        for group, item in (('Check', 'Rules in this rota'), ('Edit', 'Add recurring sessions'), ('Share', 'Print or export…')):
            expect(menu.get_by_role('group', name=group, exact=True).get_by_role('menuitem', name=item, exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        planner_tool(page, 'Add recurring sessions')
        form = page.get_by_role('dialog')
        # One multiple-choice field for one or several (C-07): the shared picker; on a list of three or fewer a tick is
        # one tap, so no Select all (X-37). Escape closes it (Done is the phone sheet's).
        form.locator('.pick-field').filter(has_text='Recurring sessions').locator('.pick-button').click()
        picker = page.locator('.picker')
        expect(picker.get_by_role('option')).to_have_count(2)
        expect(picker.get_by_role('button', name=re.compile(r'^Select all'))).to_have_count(0)
        for option in picker.get_by_role('option').all():
            option.check()
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        form.get_by_role('button', name='Calculate draft impact', exact=True).click()
        expect(form.get_by_text('Not saved yet: review what this changes', exact=False)).to_be_visible()
        form.get_by_role('button', name='Add sessions', exact=True).click()
        toast = page.locator('.toast').filter(has_text='2 sessions added to the draft.')
        expect(toast.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert len(h.detail(w)['shifts']) == 2
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert h.detail(w)['shifts'] == []
    finally:
        context.close()


def test_published_warnings_are_the_one_issue_list(browser, live_server, h):
    """S-15, now UIUX live check F3 (LEAD-UIUX, 7 Nov 2026): the published rota's stored warnings (ME-23) are the one
    issue list (issue_marker.ts issueList), not a table: each sentence once, nothing behind a closed disclosure, its
    sessions as chips naming who, and a chip shows its session on the schedule. A draft's warnings are its Fix list's
    could-improve causes (A7, tests/test_browser_fix_list.py)."""
    w, rule = _advisory_rota(h, 'Grouped warnings')
    person = h.snapshot(w)['people'][h.seed['people'][0]]['display_name']
    ruled = [f for f in h.detail(w)['workspace']['validation']['findings'] if f.get('rule_id') == rule['id']]
    assert ruled
    assert h.publish(h.detail(w)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-10-04?edit=none')
        settled(page)
        page.locator('.app-footer .health-pill.could').filter(has_text='could improve').click()  # one rota health (UIUX C3)
        pop = page.locator('.popover.issue-pop')
        expect(pop.locator('.iss-row').first).to_be_visible()
        expect(pop.locator('details')).to_have_count(0)
        texts = [re.sub(r' · .*$', '', t.strip()) for t in pop.locator('.iss-row .iss-text').all_text_contents()]
        assert len(texts) == len(set(texts)), texts  # each sentence once (C1, F1)
        go = pop.locator('button.fix-chip, button.iss-go').filter(has_text=person).first
        expect(go).to_be_visible()
        go.click()
        expect(pop).to_have_count(0)
    finally:
        context.close()


def test_rule_detail_measures_the_rota_covering_today_and_says_when_a_draft_is_empty(browser, live_server, h):
    """RU-02: the rule sheet reports on the draft covering today, not the empty draft open on the Schedule."""
    rule = h.policy({'kind': 'max_hours', 'hours': 12, 'window': 'day'})
    h.shift(h.workspace('Covers today', '2026-09-27', '2026-10-04'), '2026-10-01T08:00:00+03:00', '2026-10-01T12:00:00+03:00')
    empty = h.workspace('Empty later', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, empty['id'])
        navigate(page, 'rules')
        sheet = open_rule(page, rule['name'])
        expect(sheet.locator('.rl-sec-head h3').filter(has_text=re.compile('^In '))).to_have_text(re.compile('^In Covers today'))
    finally:
        context.close()


def test_rule_detail_says_no_duties_yet_for_an_empty_draft(browser, live_server, h):
    rule = h.policy({'kind': 'max_hours', 'hours': 12, 'window': 'day'})
    h.workspace('Only empty', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, rule['name'])
        section = sheet.locator('.rl-sec').filter(has_text='In Only empty')
        expect(section).to_contain_text('No duties yet.')
        expect(section.locator('.rl-health-count')).to_have_count(0)
    finally:
        context.close()


def test_command_menu_matches_each_word_at_a_word_start(browser, live_server, h):
    """G-03: words are matched separately, in any order, at word starts."""
    person = h.get('admin', '/api/catalogues')['people'][0]['display_name']
    first, last = person.split()[0], person.split()[-1]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.keyboard.press('Control+k')
        box = page.get_by_role('combobox', name='Search pages, people and actions')
        box.fill(f'{last[:3]} {first[:2]}'.lower())
        expect(page.get_by_role('option').filter(has_text=person)).to_be_visible()
        box.fill(f'{first} zzzz')
        expect(page.get_by_role('option').filter(has_text=person)).to_have_count(0)
    finally:
        context.close()
