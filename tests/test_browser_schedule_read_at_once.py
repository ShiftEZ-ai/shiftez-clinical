"""The draft window stays usable before and during an on-demand whole read."""
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
        h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, {'person_id': person}, severity='advisory')
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

    def start(control):
        control.click()
        wait_held(page, held)
        assert len(held) == 1

    def release(*, status=None):
        route = held.pop(0)
        if status is None:
            route.continue_()
        else:
            route.fulfill(status=status, json={})
        if not held:
            page.unroute(whole_read)

    try:
        navigate(page, 'home')
        page.route(whole_read, intercept)
        address = f"schedule/week/2026-10-04?edit={w['id']}"
        view = page.locator('#schedule-view')
        if viewport == PHONE:
            page.evaluate('key => { location.hash = key; }', address)
            wait_held(page, held)
        else:
            navigate(page, address)
            expect(view.locator('[data-session]').first).to_be_attached()
            assert not held, 'The draft window requested the whole rota before an action'
        yield page, view, start, release
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_whole_rota_facts_wait_and_session_checks_show_the_last_check(browser, live_server, h, scheme):
    w, (clean, warned, _) = draft(h, warning=True)
    summary = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['summary']
    assert summary['must'] == 0 and summary['could'] > 0
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, start, release):
        footer = page.locator('.app-footer')
        expect(footer.locator('.score-pill')).to_have_count(0)
        tools = page.get_by_role('button', name='Rota tools', exact=True)
        tools.click()
        fix_list = page.get_by_role('menu', name='Rota tools').get_by_role('menuitem', name=re.compile(r'^Fix list'))
        expect(fix_list).to_contain_text(f"{summary['could']} could improve")
        expect(fix_list).to_be_enabled()
        page.keyboard.press('Escape')

        chip(view, clean).hover()
        expect(page.locator('.hovercard')).not_to_contain_text('h this rota')
        start(chip(view, warned))
        panel = page.locator('.dock-panel').filter(has=page.get_by_role('heading', name='Checks', exact=True))
        expect(panel).to_have_count(0)
        release()

        expect(panel).to_be_visible()
        expect(panel.locator('.iss-lines > li').first).to_be_visible()
        expect(panel.get_by_role('button', name='Edit session', exact=True)).to_be_visible()
        expect(panel).not_to_contain_text('Passes every check')
        expect(panel).not_to_contain_text('The rota changed since the last check.')
        expect(footer.locator('.health-pill.ok')).to_contain_text('Ready')
        expect(footer.locator('.health-pill.could')).to_have_text(f"{summary['could']} could improve")

@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_search_and_filter_wait_and_a_chosen_view_draws_after_the_read(browser, live_server, h, scheme):
    w, (first, _) = draft(h)
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, start, release):
        toolbar = page.locator('.planner-toolbar')
        search = toolbar.get_by_role('searchbox', name='Search sessions', exact=True)
        filters = toolbar.get_by_role('button', name=re.compile(r'^Filter\b'))
        expect(search).to_be_enabled()
        expect(filters).to_be_enabled()
        search.fill('unmatched')
        expect(chip(view, first)).to_have_count(0)
        search.fill('')
        expect(chip(view, first)).to_be_visible()
        filters.click()
        expect(page.get_by_role('dialog', name=re.compile(r'^Filter\b'))).to_be_visible()
        page.keyboard.press('Escape')
        choose_radio(view_settings(toolbar), 'Range', 'Day')
        expect(view).to_have_attribute('aria-label', 'Day view', timeout=PAGE_LOAD_MS)
        bar = view.locator('button.sd-bar.draft')
        expect(bar).to_be_visible()
        expect(view.locator('.empty-state')).to_have_count(0)
        start(bar)
        release()
        expect(view).to_have_attribute('aria-label', 'Day view')
        expect(bar).to_be_visible()

def test_next_week_keeps_the_drawn_sessions_until_the_whole_rota_arrives(browser, live_server, h):
    w, (first, second) = draft(h)
    with open_held(browser, live_server, w) as (page, view, start, release):
        toolbar = page.get_by_role('group', name='Schedule', exact=True)
        toolbar.get_by_role('button', name='Next week', exact=True).click()
        expect(chip(view, second)).to_be_visible(timeout=PAGE_LOAD_MS)
        expect(chip(view, first)).to_have_count(0)
        expect(view.get_by_text('No sessions this week', exact=True)).to_have_count(0)
        start(chip(view, second))
        release()
        expect(chip(view, second)).to_be_visible()

@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_held_writes_explain_the_wait_and_do_not_queue(browser, live_server, h, scheme):
    w, _ = draft(h)
    with open_held(browser, live_server, w, scheme=scheme) as (page, _, start, release):
        posts = []
        page.on('request', lambda r: posts.append(r) if r.method == 'POST' and urlsplit(r.url).path == '/api/commands' else None)
        submit = page.get_by_role('button', name='Submit for review…', exact=True)
        start(submit)
        expect(page.locator('dialog[open]')).to_have_count(0)
        for control in (
            page.get_by_role('button', name=re.compile(r'^Auto-schedule\b')),
            page.get_by_role('button', name='Add session', exact=True),
        ):
            control.click()
            expect(page.locator('dialog[open]')).to_have_count(0)
            expect(page.locator('#auto-panel')).to_be_hidden()
        assert not posts, 'A held control sent a command'

        release()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Submit .* for review$'))
        expect(dialog).to_have_count(1)
        expect(page.locator('#auto-panel')).to_be_hidden()
        expect(page.get_by_role('region', name='Add session', exact=True)).to_have_count(0)
        assert not posts, 'Opening the review dialog sent a command'

def test_the_whole_read_announces_ready_once_without_a_visible_banner(browser, live_server, h):
    w, _ = draft(h)
    with open_held(browser, live_server, w) as (page, _, start, release):
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
        start(page.get_by_role('button', name='Add session', exact=True))
        live = page.locator('#loading-live')
        expect(live).to_have_text(LOADING, timeout=PAGE_LOAD_MS)
        release()
        expect(page.get_by_role('region', name='Add session', exact=True)).to_be_visible()
        expect(live).to_have_attribute('aria-live', 'polite')
        assert page.evaluate('window.readyAnnouncements') == [READY]
        expect(page.get_by_text(READY, exact=True).and_(page.locator(':not(#loading-live)'))).to_have_count(0)


@pytest.mark.parametrize('viewport', [DESKTOP, PHONE], ids=['desktop', 'phone'])
def test_the_week_keeps_tab_and_arrow_focus(browser, live_server, h, viewport):
    w, (first, _) = draft(h)
    with open_held(browser, live_server, w, viewport=viewport) as (page, view, start, release):
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
            toolbar = page.locator('.planner-toolbar')
            add = toolbar.get_by_role('button', name='Add session', exact=True)
            toolbar.get_by_role('button', name=re.compile(r'^View settings\b')).focus()
            page.keyboard.press('Tab')
            expect(add).to_be_focused()
            expect(add).to_be_enabled()
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
    with open_held(browser, live_server, w, scheme=scheme) as (page, view, start, release):
        start(chip(view, first))
        release(status=503)
        failed = page.locator('.toast.error').filter(has_text='Couldn’t load the whole rota.')
        expect(failed).to_be_visible()
        expect(page.locator('.app-footer .health-pill').filter(has_text=LOADING)).to_have_count(0)
        expect(view).not_to_have_attribute('aria-busy', 'true')
        expect(chip(view, first)).to_be_enabled()

        chip(view, first).click()
        expect(page.locator('.dock-panel').filter(has=page.get_by_role('heading', name='Checks', exact=True))).to_be_visible()
        expect(chip(view, first)).to_be_visible()
