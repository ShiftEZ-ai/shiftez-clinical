"""The Schedule has one period navigator (in its toolbar, with Today) in every view, with or without a side panel."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_rota, pick_view, view_settings, choose_radio, choose_layout, close_popover, fonts_ready  # noqa: F401
from .test_browser_overflow import Guard

pytestmark = pytest.mark.browser
VIEWS = ('Week', 'Month', 'Timeline', 'People', 'Coverage', 'List')


def show(page, view):
    if view == 'Timeline':
        choose_layout(view_settings(page.locator('.planner-toolbar')), 'Timeline')
        close_popover(page)
    else:
        pick_view(page, view)


def one_navigator(page, view):
    nav = page.locator('.wk-nav')
    expect(nav, f'{view}: one period navigator').to_have_count(1)
    expect(page.locator('.planner-toolbar .wk-nav').get_by_role('button', name='Go to today')).to_have_count(1)


def test_long_fortnight_is_readable_and_navigable_at_every_width(browser, live_server, h):
    me = h.get('admin', '/api/me')
    h.command('admin', 'department.settings', {
        'department_name': me['department']['name'], 'department_revision': me['department']['revision'],
        'organisation_name': me['organisation']['name'], 'timezone': 'Asia/Riyadh', 'week_start': 6,
        'reason': 'Synthetic Sunday week start',
    }, me['department']['id'], me['organisation']['revision'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'schedule/people/2026-10-01?edit=none')
        nav = page.locator('.planner-toolbar .wk-nav')
        expect(nav.locator('strong')).to_have_text('Sun 27 Sep – Sat 10 Oct')
        guard = Guard(page, 'staff')
        guard.check('fixed long fortnight')
        assert not guard.lines(), '\n'.join(guard.lines())
        page.set_viewport_size({'width': 320, 'height': 900})
        nav.get_by_role('button', name='Next 2 weeks', exact=True).click()
        expect(nav.locator('strong')).to_have_text('Sun 11 Oct – Sat 24 Oct')
        nav.get_by_role('button', name='Previous 2 weeks', exact=True).click()
        expect(nav.locator('strong')).to_have_text('Sun 27 Sep – Sat 10 Oct')
        # A remote period always offers Today, independently of the wall clock's current day.
        navigate(page, 'schedule/people/2000-10-01?edit=none')
        nav.get_by_role('button', name='Go to today', exact=True).click()
        expect(nav.get_by_role('button', name='Go to today', exact=True)).to_be_disabled()
    finally:
        context.close()


def test_one_period_navigator_in_every_view_with_and_without_a_panel(browser, live_server, h):
    w = h.workspace('One navigator', '2026-10-04', '2026-10-11')
    w = h.shift(w, people=[])['workspace']
    w = h.shift(w, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        for view in VIEWS:
            show(page, view)
            one_navigator(page, view)
        # A side panel redraws the view around it: still one navigator, in every view it stays open across.
        pick_view(page, 'Week')
        page.locator('button.pchip.gap').first.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        one_navigator(page, 'Week with a panel')
        page.locator('.wk-panel').get_by_role('tab', name='Why', exact=True).click()
        expect(page.locator('.why .wk-loading')).to_have_count(0)
        one_navigator(page, 'Week with the Why tab')
        for view in VIEWS:
            show(page, view)
            expect(page.locator('.wk-panel')).to_be_visible()
            one_navigator(page, view + ' with a panel')
        page.get_by_role('button', name='Close panel', exact=True).click()
        one_navigator(page, 'List after closing the panel')
    finally:
        context.close()


FITS = '''() => { const bar = document.querySelector('.planner-toolbar'), nav = bar.querySelector('.wk-nav');
    return nav.scrollWidth <= nav.clientWidth && nav.getBoundingClientRect().right <= bar.getBoundingClientRect().right + 0.5
        && document.documentElement.scrollWidth <= document.documentElement.clientWidth; }'''


@pytest.mark.parametrize('role', ['staff', 'coordinator'])
@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_period_fits_a_320px_toolbar_whatever_the_font(browser, live_server, h, role, scheme):
    """Runner images measure text differently (another fallback font): at 320px the weekday names drop ("27 Sept – 10 Oct")
    and a still wider font ends in an ellipsis inside the toolbar, never past it."""
    context, page = sign_in(browser, live_server, role, viewport={'width': 320, 'height': 800})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'schedule/people')
        label = page.locator('.planner-toolbar .wk-nav strong')
        fonts_ready(page)
        assert re.fullmatch(r'\d+ \w+ – \d+ \w+', label.inner_text()), label.inner_text()  # drawn text: no weekday names
        assert page.evaluate(FITS)
        label.evaluate("n => { n.style.setProperty('font-family', 'monospace'); n.style.setProperty('letter-spacing', '6px'); }")  # a much wider font
        assert page.evaluate(FITS)
        assert page.evaluate("() => getComputedStyle(document.querySelector('.planner-toolbar .wk-nav strong')).textOverflow") == 'ellipsis'
        page.set_viewport_size({'width': 1440, 'height': 800})
        page.wait_for_function(r"() => /^\w{3} \d+ \w+ – \w{3} \d+ \w+$/.test(document.querySelector('.planner-toolbar .wk-nav strong').innerText)")  # weekday names again
    finally:
        context.close()
