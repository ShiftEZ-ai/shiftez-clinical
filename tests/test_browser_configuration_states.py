"""Configuration loading and error states stay inside the pane (audit C-21, C-22, C-34; perf AD-8, AD-9): a slow
section marks its nav item at once and shows the pane busy; a failed read keeps the nav (and Department settings'
form when only the sign-in sessions read failed); an inline edit is one command with no re-read of the session; a big
impact preview groups repeated findings and a form closed straight away asks nothing. Admin, 1440 light."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401

pytestmark = pytest.mark.browser


def nav(page, kind):
    return page.locator(f'.config-nav [data-section="{kind}"]')


def test_a_slow_section_marks_its_nav_item_at_once_and_shows_the_pane_busy(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    held = []
    try:
        navigate(page, 'configuration/locations')
        page.route('**/api/review-routes*', lambda route: held.append(route))
        nav(page, 'department').click()
        wait_held(page, held)
        expect(nav(page, 'department')).to_have_attribute('aria-current', 'page')
        expect(nav(page, 'locations')).not_to_have_attribute('aria-current', 'page')
        busy = page.locator('.config-pane[data-kind=locations][aria-busy=true]')
        expect(busy).to_have_count(1)
        expect(busy.get_by_text('Loading Department settings…')).to_be_attached()  # the skeleton once 300 ms pass
        for route in held:
            route.continue_()
        page.unroute('**/api/review-routes*')
        expect(page.locator('.config-pane[data-kind=department]').get_by_label('Department name', exact=True)).to_be_visible()
        expect(page.locator('.config-pane[aria-busy=true]')).to_have_count(0)
    finally:
        context.close()


def test_a_failed_access_read_keeps_the_form_and_its_panel_retries(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/locations')
        page.route('**/api/access', lambda route: route.fulfill(status=500, json={'detail': 'Synthetic access failure'}))
        nav(page, 'department').click()
        pane = page.locator('.config-pane[data-kind=department]')
        expect(pane.get_by_label('Department name', exact=True)).to_be_visible()
        expect(page.get_by_text('Couldn’t load Configuration.', exact=False)).to_have_count(0)
        sessions = pane.locator('section.panel').filter(has=page.get_by_role('heading', name='Sign-in sessions', exact=True))
        expect(sessions).to_contain_text('Couldn’t load the sign-in session settings.')
        page.unroute('**/api/access')
        sessions.get_by_role('button', name='Retry', exact=True).click()
        expect(pane.get_by_role('button', name='Edit session settings')).to_be_visible()
    finally:
        context.close()


def test_a_failed_section_read_keeps_the_nav(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/locations')
        page.route('**/api/policies/capabilities', lambda route: route.fulfill(status=500, json={'detail': 'Synthetic capabilities failure'}))
        nav(page, 'auto_schedule').click()
        pane = page.locator('.config-pane[data-kind=auto_schedule]')
        expect(pane.get_by_role('alert')).to_contain_text('Couldn’t load auto-schedule & scoring.')
        expect(pane.get_by_role('heading', name='Auto-schedule & scoring', exact=True)).to_be_visible()
        nav(page, 'locations').click()  # the nav stays usable
        expect(page.locator('.config-pane[data-kind=locations] .dt-body')).to_be_visible()
    finally:
        context.close()


def test_an_inline_edit_is_one_command_and_switching_off_recounts_the_nav(browser, live_server, h):
    place = h.configure('locations', {'name': 'Fictional inline room', 'starts_on': '2020-01-01', 'location_type_id': h.get('admin', '/api/catalogues')['location_types'][0]['id'], 'capacity': 1})['record']
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'configuration/locations')
        before = int(nav(page, 'locations').locator('.nav-count-muted').inner_text())
        sent = []
        page.on('request', lambda r: sent.append((r.method, re.sub(r'\?.*', '', r.url.split('/api/', 1)[-1]))) if '/api/' in r.url else None)
        row = page.locator('.config-pane .dt-body tbody tr').filter(has_text='Fictional inline room')
        row.locator('td[data-col=name]').click()
        editor = page.get_by_role('textbox', name='Location', exact=True)
        editor.fill('Fictional inline room 2')
        with page.expect_response(lambda r: '/api/commands' in r.url):
            editor.press('Enter')
        expect(page.locator('.config-pane .dt-body tbody tr').filter(has_text='Fictional inline room 2')).to_have_count(1)
        expect(page.locator('.toast').filter(has_text='saved')).to_be_visible()
        assert [s for s in sent if s[1] != 'notifications'] == [('POST', 'commands')], sent  # no /api/me, catalogues or leave types
        sent.clear()
        with page.expect_response(lambda r: '/api/commands' in r.url):
            page.locator('.config-pane .dt-body tbody tr').filter(has_text='Fictional inline room 2').locator('td[data-col=status]').click()
        expect(nav(page, 'locations').locator('.nav-count-muted')).to_have_text(str(before - 1))
        assert [s for s in sent if s[1] != 'notifications'] == [('POST', 'commands')], sent
        assert h.get('admin', '/api/catalogues')['locations'] and place['id']
    finally:
        context.close()


def big_preview(route):
    finding = lambda i: {'code': 'BOOK_FULL_DURATION', 'severity': 'error', 'message': 'Book the complete configured duration.', 'shift_id': f's{i}'}
    impacts = [{'workspace_id': f'w{n}', 'name': f'Fictional rota {n}', 'status': 'draft', 'introduced': [finding(i) for i in range(500)], 'resolved': [],
                'before_error_count': 0, 'after_error_count': 500, 'before_warning_count': 0, 'after_warning_count': 0} for n in range(4)]
    route.fulfill(json={'scope': 'Synthetic scope', 'message': 'Synthetic', 'impacts': impacts, 'workspace_count': 4, 'partial': False,
                        'inventory_only': False, 'source_fingerprint': 'synthetic'})


def test_department_scale_preview_groups_repeated_findings(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        page.route('**/api/catalogue/preview', big_preview)
        navigate(page, 'configuration/locations')
        page.locator('.config-pane .dt-body tbody tr').first.get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Edit'))
        changes = dialog.locator('.configuration-impact-comparison > div').filter(has=page.locator('h4', has_text='finding changes')).locator('table')
        expect(changes).to_have_count(4)
        expect(changes.first.locator('tbody tr')).to_have_count(1)  # 500 of one cause: one row with its count
        expect(changes.first).to_contain_text('Book the complete configured duration. · 500 times')
        assert dialog.evaluate('d => d.querySelectorAll("*").length') <= 1500
    finally:
        context.close()


def test_a_form_closed_straight_away_asks_for_no_preview(browser, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    asked = []
    try:
        navigate(page, 'configuration/locations')
        page.route('**/api/catalogue/preview', lambda route: (asked.append(route), route.continue_()))
        page.clock.install()
        page.locator('.config-pane .dt-body tbody tr').first.get_by_role('button', name='Edit', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Edit'))
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        page.clock.run_for(2000)  # past the 500 ms pause after which an open form asks for its preview
        page.evaluate('0')
        assert asked == []
    finally:
        context.close()


def pointer_click(page, locator):
    """A real pointer click at the element's centre (page.mouse): whatever covers that point gets it, as for a person.
    A JS click (or Playwright's actionability retries) would reach a covered or inert element anyway."""
    box = locator.bounding_box()
    assert box, 'not on screen'
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)


# Each nav item's centre hits that item: no overlay, no inert ancestor, nothing clipped under the sticky footer.
HITS = """() => { const bottom = innerHeight - (document.querySelector('.app-footer')?.getBoundingClientRect().height || 0);
  return [...document.querySelectorAll('.config-nav [data-section]')].flatMap(b => {
    b.scrollIntoView({block: 'nearest'}); const r = b.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    return y > 0 && y < bottom && hit?.closest('[data-section]') === b && !b.closest('[inert]') ? [] : [b.dataset.section]; }); }"""


def test_the_nav_takes_pointer_clicks_while_a_section_loads(browser, live_server):
    """maintenance: while a section reads its data only the section left is inert; its nav takes a real pointer click (an
    inert nav ignored them), so Names & codes opens, with the switch to design the department's Person label at the
    top. Every nav item's centre is that item (nothing laid over it, none under the footer), at 1440 x 900 too."""
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1440, 'height': 900})
    held = []
    try:
        navigate(page, 'configuration/locations')
        assert page.evaluate(HITS) == []
        page.route('**/api/review-routes*', lambda route: held.append(route))
        pointer_click(page, nav(page, 'department'))
        wait_held(page, held)
        expect(page.locator('.config-pane[data-kind=locations]')).to_have_attribute('inert', '')
        assert page.evaluate(HITS) == []  # still live while the section loads
        pointer_click(page, nav(page, 'names'))
        pane = page.locator('.config-pane[data-kind=names]')
        expect(pane.get_by_role('heading', name='Names & codes', exact=True)).to_be_visible()
        top = pane.locator('.names-top section.panel').first
        expect(top.get_by_role('checkbox', name='Design the department’s person label')).to_be_visible()
        for route in held:
            route.continue_()
        page.unroute('**/api/review-routes*')
        expect(pane.locator('[inert]')).to_have_count(0)
        assert page.evaluate(HITS) == []
    finally:
        context.close()
