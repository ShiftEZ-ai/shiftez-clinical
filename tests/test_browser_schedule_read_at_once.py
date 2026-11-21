"""The schedule stays readable while the whole rota is in flight."""
from contextlib import contextmanager
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from .test_browser import (  # noqa: F401
    PAGE_LOAD_MS, browser, choose_radio, live_server, navigate, sign_in, view_settings, wait_held,
)
from .test_browser_grid_keyboard import tab_into

pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
PHONE = {'width': 390, 'height': 844}
LOADING = 'Loading the whole rota…'
READY = 'Ready to edit'


def draft(h, *, warning=False):
    person = h.seed['people'][1]
    if warning:
        h.policy({'kind': 'restriction'}, {'person_id': person}, severity='advisory')
    w = h.workspace('Schedule read fixture', end='2026-10-18')
    w = h.shift(w)['workspace']
    if warning:
        w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                    people=[person])['workspace']
    w = h.shift(w, start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00')['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    return w, [s['id'] for s in sorted(h.detail(w)['shifts'], key=lambda s: s['start'])]


def chip(view, session):
    return view.locator(f'button.pchip[data-assignment][data-shift="{session}"]')


@contextmanager
def open_held(browser, live_server, w, *, scheme='light', viewport=DESKTOP):
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport,
                            color_scheme=scheme, reduced_motion='reduce')
    held = []

    def whole_read(url):
        parts = urlsplit(url)
        return parts.path == f"/api/workspaces/{w['id']}" and parts.query == 'view=schedule'

    def intercept(route):
        if route.request.method == 'GET':
            held.append(route)
        else:
            route.continue_()

    def release(*, status=None):
        route = held.pop(0)
        if status is None:
            route.continue_()
        else:
            route.fulfill(status=status, json={})
        page.unroute(whole_read)

    try:
        navigate(page, 'home')
        page.route(whole_read, intercept)
        page.evaluate('key => { location.hash = key; }', f"schedule/week/2026-10-04?edit={w['id']}")
        view = page.locator('#schedule-view')
        if viewport != PHONE:
            expect(view).to_have_attribute('data-reading', '', timeout=PAGE_LOAD_MS)
            expect(view.locator('[data-session]').first).to_be_attached()
        wait_held(page, held)
        yield page, view, release
    finally:
        context.close()


def loading_toast(page):
    return page.get_by_role('region', name='Messages', exact=True).get_by_role('status').filter(
        has_text=re.compile(rf'^{re.escape(LOADING)}$'),
    )


def open_session(page, session_chip):
    session_chip.click()
    panel = page.locator('.dock-panel').filter(has=page.get_by_role('heading', name='Checks', exact=True))
    expect(panel).to_be_visible()
    expect(panel.get_by_role('button', name='Edit session', exact=True)).to_have_count(0)
    return panel


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_whole_rota_facts_wait_and_session_checks_show_the_last_check(browser, live_server, h, scheme):
    w, (clean, warned, _) = draft(h, warning=True)
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
    assert summary['must'] == 0 and summary['could'] > 0
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, release):
        footer = page.locator('.app-footer')
        pills = footer.locator('.health-pill')
        expect(pills).to_have_count(1)
        expect(pills).to_have_text(LOADING)
        expect(footer.locator('.health-pill.ok, .health-pill.err, .score-pill')).to_have_count(0)
        expect(view).to_have_attribute('aria-label', re.compile(r' · from the last check$'))

        mark = view.locator(f'[data-session~="{warned}"] .iss-mark').first
        expect(mark).to_have_css('filter', 'grayscale(1)')

        tools = page.get_by_role('button', name='Rota tools', exact=True)
        tools.click()
        fix_list = page.get_by_role('menu', name='Rota tools').get_by_role('menuitem', name=re.compile(r'^Fix list'))
        expect(fix_list).to_have_text('Fix list')
        expect(fix_list).to_have_attribute('aria-disabled', 'true')
        page.keyboard.press('Escape')

        chip(view, clean).hover()
        hover = page.locator('.hovercard')
        expect(hover).to_contain_text('Rules: from the last check')
        expect(hover).not_to_contain_text('Rules: all pass')

        panel = open_session(page, chip(view, warned))
        expect(panel).to_contain_text('From the last check')
        expect(panel.locator('.iss-lines > li').first).to_be_visible()
        expect(panel).not_to_contain_text('Passes every check')
        expect(panel).not_to_contain_text('The rota changed since the last check.')
        panel.get_by_role('button', name='Close panel', exact=True).click()

        release()
        expect(page.locator('#schedule-view[data-reading]')).to_have_count(0)
        expect(footer.locator('.health-pill.ok')).to_contain_text('Ready')
        expect(footer.locator('.health-pill.could')).to_have_text(f"{summary['could']} could improve")
        expect(pills.filter(has_text=LOADING)).to_have_count(0)
        tools.click()
        expect(fix_list).to_contain_text(f"{summary['could']} could improve")
        expect(fix_list).to_be_enabled()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_search_and_filter_wait_and_a_chosen_view_draws_after_the_read(browser, live_server, h, scheme):
    w, (first, _) = draft(h)
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, release):
        toolbar = page.locator('.planner-toolbar')
        search = toolbar.get_by_role('searchbox', name='Search sessions', exact=True)
        filters = toolbar.get_by_role('button', name=re.compile(r'^Filter\b'))
        for control in (search, filters):
            expect(control).to_have_attribute('aria-disabled', 'true')
            expect(control).to_have_accessible_description(LOADING)
        search.focus()
        page.keyboard.type('unmatched')
        expect(search).to_have_value('')
        expect(chip(view, first)).to_be_visible()
        filters.click(force=True)
        expect(page.get_by_role('dialog', name=re.compile(r'^Filter\b'))).to_have_count(0)

        panel = open_session(page, chip(view, first))
        panel.get_by_role('button', name='Close panel', exact=True).click()
        choose_radio(view_settings(toolbar), 'Range', 'Day')
        expect(view).to_have_attribute('aria-label', 'Week view · from the last check')
        expect(chip(view, first)).to_be_visible()
        expect(view.locator('.empty-state')).to_have_count(0)

        release()
        expect(view).to_have_attribute('aria-label', 'Day view', timeout=PAGE_LOAD_MS)
        expect(view.locator('button.sd-bar.draft')).to_be_visible()
        expect(view.locator('.empty-state')).to_have_count(0)


def test_next_week_keeps_the_drawn_sessions_until_the_whole_rota_arrives(browser, live_server, h):
    w, (first, second) = draft(h)
    with open_held(browser, live_server, w) as (page, view, release):
        toolbar = page.get_by_role('group', name='Schedule', exact=True)
        toolbar.get_by_role('button', name='Next week', exact=True).click()
        expect(view).to_have_attribute('aria-busy', 'true')
        expect(chip(view, first)).to_be_visible()
        expect(view.get_by_text('No sessions this week', exact=True)).to_have_count(0)
        expect(chip(view, second)).to_have_count(0)

        release()
        expect(chip(view, second)).to_be_visible(timeout=PAGE_LOAD_MS)
        expect(chip(view, first)).to_have_count(0)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_held_writes_explain_the_wait_and_do_not_queue(browser, live_server, h, scheme):
    w, _ = draft(h)
    with open_held(browser, live_server, w, scheme=scheme) as (page, _, release):
        posts = []

        def record(request):
            if request.method == 'POST' and urlsplit(request.url).path == '/api/commands':
                posts.append(request)

        page.on('request', record)
        toast = loading_toast(page)
        expect(toast).to_have_count(0)
        dialog = page.get_by_role('dialog')
        submit = page.get_by_role('button', name='Submit for review…', exact=True)
        for control in (
            submit,
            page.get_by_role('button', name=re.compile(r'^Auto-schedule\b')),
            page.get_by_role('button', name='Undo last edit', exact=True),
            page.get_by_role('button', name='Add session', exact=True),
        ):
            expect(control).to_have_attribute('aria-disabled', 'true')
            expect(control).to_have_js_property('disabled', False)
            control.focus()
            expect(control).to_be_focused()
            control.click(force=True)
            expect(toast).to_be_visible()
            expect(dialog).to_have_count(0)
            expect(page.locator('#auto-panel')).to_be_hidden()
            assert not posts, 'A held control sent a command'
            # Each press must explain itself, even when the previous press said the same thing.
            toast.get_by_role('button', name='Dismiss message', exact=True).click()
            expect(toast).to_have_count(0)

        release()
        expect(submit).to_be_enabled(timeout=PAGE_LOAD_MS)
        expect(dialog).to_have_count(0)
        expect(page.locator('#auto-panel')).to_be_hidden()
        assert not posts, 'A held control was replayed after loading'
        submit.click()
        expect(dialog).to_have_count(1)
        expect(dialog).to_have_accessible_name(re.compile(r'^Submit .* for review$'))


def test_the_whole_read_announces_ready_once_without_a_visible_banner(browser, live_server, h):
    w, _ = draft(h)
    with open_held(browser, live_server, w) as (page, _, release):
        page.evaluate("""() => {
          window.readyAnnouncements = [];
          new MutationObserver(records => {
            for (const record of records) {
              if (record.target.id !== 'loading-live') continue;
              for (const child of record.addedNodes) {
                if (child.textContent === 'Ready to edit') window.readyAnnouncements.push(child.textContent);
              }
            }
          }).observe(document.body, {childList: true, subtree: true});
        }""")
        release()
        live = page.locator('#loading-live')
        expect(live).to_have_text(READY, timeout=PAGE_LOAD_MS)
        expect(live).to_have_attribute('aria-live', 'polite')
        assert page.evaluate('window.readyAnnouncements') == [READY]
        expect(page.get_by_text(READY, exact=True).and_(page.locator(':not(#loading-live)'))).to_have_count(0)


@pytest.mark.parametrize('viewport', [DESKTOP, PHONE], ids=['desktop', 'phone'])
def test_the_week_keeps_tab_and_arrow_focus(browser, live_server, h, viewport):
    w, (first, _) = draft(h)
    with open_held(browser, live_server, w, viewport=viewport) as (page, view, release):
        if viewport == PHONE:
            expect(view).to_have_count(0)
            release()
            expect(view).to_have_attribute('aria-label', 'Week view', timeout=PAGE_LOAD_MS)
            expect(view.locator(f'button.ag-item[data-session="{first}"]')).to_be_visible()
            expect(page.locator('#schedule-view[data-reading]')).to_have_count(0)
            layout = page.get_by_role('radiogroup', name='Calendar layout')
            layout.get_by_role('radio', name='Grid', exact=True).click()
            expect(chip(view, first)).to_be_visible(timeout=PAGE_LOAD_MS)
        else:
            expect(view).to_have_attribute('data-reading', '')
            toolbar = page.locator('.planner-toolbar')
            held = toolbar.get_by_role('button', name='Add session', exact=True)
            toolbar.get_by_role('button', name=re.compile(r'^View settings\b')).focus()
            page.keyboard.press('Tab')
            expect(held).to_be_focused()
            expect(held).to_have_attribute('aria-disabled', 'true')
            toast = loading_toast(page)
            expect(toast).to_have_count(0)
            page.keyboard.press('Enter')
            expect(toast).to_be_visible()
            expect(held).to_be_focused()
            toast.get_by_role('button', name='Dismiss message', exact=True).click()
            expect(toast).to_have_count(0)
        expect(page.locator('#schedule-view[inert]')).to_have_count(0)

        grid = view.get_by_role('grid')
        tab_into(page, grid)
        focused = grid.locator(':focus')
        expect(focused).to_have_count(1)
        before = focused.element_handle()
        page.keyboard.press('ArrowRight')
        expect(focused).to_have_count(1)
        assert focused.evaluate('(node, previous) => node !== previous', before)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_failed_whole_read_keeps_the_week_readable_and_retry_draws_it_whole(browser, live_server, h, scheme):
    w, (first, _) = draft(h)
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, release):
        release(status=503)
        failed = page.locator('.read-failed').filter(has_text='Couldn’t load the whole rota.')
        expect(failed).to_be_visible()
        expect(page.locator('.app-footer .health-pill').filter(has_text=LOADING)).to_have_count(0)
        expect(view).to_have_attribute('data-reading', '')
        expect(chip(view, first)).to_be_visible()

        failed.get_by_role('button', name='Retry', exact=True).click()
        expect(page.locator('#schedule-view[data-reading]')).to_have_count(0, timeout=PAGE_LOAD_MS)
        expect(chip(view, first)).to_be_visible()
        expect(failed).to_have_count(0)
