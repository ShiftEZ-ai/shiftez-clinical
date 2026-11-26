"""Phone scope : on a phone Rota is for employees. Home, the Schedule
(own published duties), Requests (incl. deciding them) and the Inbox open there for every role (QA-113); every other page says "Best on a desktop"
with Back to Home and Continue anyway. Desktops and tablets keep everything. Every other browser test bypasses the notice
(test_browser.Clocked), so the operator screens' phone layouts stay tested; these tests turn it on (phone_notice=True)."""
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import ROLE_NAMES

from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, save_dialog  # noqa: F401
from .test_browser_requests import published, day, new_request

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
DESKTOP = {'width': 1440, 'height': 1000}
SHOTS = os.getenv('PHONE_SCOPE_SHOTS')
# Desktop-only addresses and the page name the notice gives them. People opens on a phone as Where is everyone for
# whoever reads the timeline .
DESKTOP_ONLY = [('schedule/issues?view=checks', 'Fix list'), ('schedule/week?edit=00000000-0000-0000-0000-000000000000', 'Schedule planning'),
                ('programmes', 'Programmes & rotations'), ('insights', 'Insights'), ('rules', 'Rules'),
                ('configuration', 'Configuration'), ('access/people', 'Access & approvals'), ('system', 'System')]


def phone(browser, origin, role, theme='light', viewport=PHONE):
    context, page = sign_in(browser, origin, role, viewport, phone_notice=True)
    page.evaluate("t => localStorage.setItem('rota.prefs', JSON.stringify({forms:'all', theme:t}))", theme)
    page.reload()
    expect(page.locator('#main[data-loaded-view]')).to_have_count(1, timeout=30000)
    return context, page


def nav(page):
    return page.eval_on_selector_all('#department-nav [data-route]', 'bs => bs.map(b => b.dataset.route)')


def notice(page, name):
    box = page.locator('#main .desktop-only')
    expect(box.get_by_role('heading', name='Best on a desktop', exact=True)).to_be_visible(timeout=20000)
    expect(box.locator('p')).to_have_text(f'{name} is built for a larger screen.')  # one line naming the page (L16)
    return box


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'{name}.png'))


@pytest.mark.parametrize('role,theme,pages', [
    ('staff', 'light', ['home', 'schedule', 'requests', 'people']), ('staff', 'dark', ['home', 'schedule', 'requests', 'people']),
    ('approver', 'dark', ['home', 'schedule', 'requests', 'people', 'access']), ('coordinator', 'light', ['home', 'schedule', 'requests', 'people', 'access']),
    ('admin', 'dark', ['home', 'schedule', 'requests', 'people', 'access']), ('auditor', 'light', ['home', 'schedule', 'requests', 'people', 'access'])])
def test_phone_navigation_lists_only_phone_views(browser, live_server, h, role, theme, pages):
    """QA-113: every account that may open them gets the published Schedule and Requests, with or without a person link;
    everyone gets Home, its phone version (H-04); whoever reads the team's timeline gets People as Where is everyone
    , staff their contact directory; approvers and route readers Access & approvals › Review routes
    ."""
    context, page = phone(browser, live_server, role, theme)
    try:
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        assert nav(page) == pages
        expect_loaded(page, 'home')
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        # The employee Home: no planner panels, whatever else the account does; a coordinator keeps Today (a short list,
        # no table) and Away today (B6, D10).
        expect(page.locator('#main .home-rota, #main .ov-recent, #main .setup-card')).to_have_count(0)
        expect(page.locator('#main .home-today, #main .home-away')).to_have_count(2 if role in ('coordinator', 'admin') else 0)  # an admin acts as coordinator
        expect(page.locator('#main .home-today table')).to_have_count(0)
        expect(page.get_by_role('button', name='New rota', exact=True)).to_have_count(0)
        # Search lists the same pages only.
        page.keyboard.press('Control+k')
        palette = page.get_by_role('dialog').filter(has=page.get_by_role('combobox'))
        expect(palette.first).to_be_visible()
        expect(page.get_by_role('option', name=re.compile(r'^Rules'))).to_have_count(0)
        # Commands follow the same rule: no planning or setup actions, no People results.
        expect(page.get_by_role('option').first).to_be_visible()
        expect(page.get_by_role('option', name=re.compile(r'New rota|announcement|Auto-schedule', re.I))).to_have_count(0)
        expect(page.get_by_role('option', name=re.compile(r'Alex Morgan'))).to_have_count(0)
        page.keyboard.press('Escape')
        if role == 'staff':
            shot(page, f'home-staff-{theme}')
    finally:
        context.close()


@pytest.mark.parametrize('role', ROLE_NAMES)
def test_every_role_lands_on_a_usable_home_on_a_phone(browser, live_server, h, role):
    """H-04 / N-04: no account lands on "Best on a desktop" (whose Back to Home looped onto itself): Home is always the
    phone Home, the menu lists it, and nothing offers Home while on Home."""
    if role not in h.seed['identities']:
        identity(h, role, [role])
    context, page = phone(browser, live_server, role, 'dark')
    try:
        expect_loaded(page, 'home')
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        expect(page.get_by_role('button', name='Back to Home', exact=True)).to_have_count(0)
        expect(page.locator('#main .home-needs')).to_be_visible()
        assert 'home' in nav(page)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('role,theme', [('staff', 'light'), ('programme_lead', 'dark'), ('auditor', 'light')])
def test_employee_views_and_approvals_open_on_a_phone_for_every_role(browser, live_server, h, role, theme):
    """QA-113 : Requests and the published Schedule never show "Best on a desktop", whatever the role and
    with or without a person link; the route table's `phone` flag is the one place that decides."""
    assert role in ROLE_NAMES
    if role == 'programme_lead':
        identity(h, 'programme_lead', ['programme_lead'])
    context, page = phone(browser, live_server, role, theme)
    try:
        for view in ('requests', 'schedule'):
            navigate(page, view)
            expect_loaded(page, view)
            expect(page.locator('#main .desktop-only')).to_have_count(0)
            expect(page.get_by_role('heading', name='Best on a desktop', exact=True)).to_have_count(0)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            shot(page, f'{view}-{role}-{theme}')
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_approver_decides_review_routes_on_a_phone(browser, live_server, h, theme):
    """QA-113 : Access & approvals opens on a phone as its approvals part, with no gate and no
    "Not optimised for phones": each route a card with stacked steps and a 44px Activate route that keeps its reason
    and confirm. Access administration (self-approval, delegation, People & roles) stays on the desktop."""
    h.command('admin', 'review_route.create', {'name': 'Phone decided route', 'purpose': 'rota', 'starts_on': '2027-03-01',
        'ends_on': '2027-04-01', 'definition': {'schema_version': 1, 'steps': [{'key': 'clinical_review',
        'label': 'Independent clinical review', 'role': 'approver', 'escalation_role': 'programme_lead'}],
        'urgent_steps': None}, 'reason': 'Phone approval browser check'})
    context, page = phone(browser, live_server, 'approver', theme)
    try:
        assert 'access' in nav(page)
        navigate(page, 'access')
        expect_loaded(page, 'access')
        expect(page.locator('#main .desktop-only, #main .phone-banner')).to_have_count(0)
        expect(page.get_by_role('tab')).to_have_count(0)  # only Review routes: no tab into the desktop notice
        card = page.locator('section.route-card').filter(has_text='Phone decided route')
        expect(card.locator('.route-steps li')).to_have_count(1)
        expect(card.locator('.route-steps')).to_contain_text('Independent clinical review')
        expect(card.locator('table')).to_have_count(0)
        expect(page.get_by_role('button', name='Propose review route', exact=True)).to_have_count(0)
        expect(page.locator('#main')).to_contain_text('Self-approval permissions and review delegation need a desktop.')
        activate = card.get_by_role('button', name='Activate route', exact=True)
        assert activate.bounding_box()['height'] >= 44
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'access-approver-{theme}')
        activate.click()
        form = page.get_by_role('dialog')
        form.get_by_label('Reason', exact=True).fill('Independently activate on a phone')
        save_dialog(form, 'Confirm')
        expect(page.locator('.toast', has_text='Phone decided route is active.')).to_be_visible()
        expect(card.locator('.badge').first).to_have_text(re.compile('Active'))
    finally:
        context.close()


def test_staff_open_the_contact_directory_on_a_phone(browser, live_server, h):
    """finding a colleague is an employee view; staff's People opens on a phone, no gate."""
    context, page = phone(browser, live_server, 'staff')
    try:
        navigate(page, 'people')
        expect_loaded(page, 'people')
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        expect(page.get_by_label('Search people', exact=True)).to_be_visible()
        expect(page.locator('#main .dt-card').first).to_be_visible()
        expect(page.get_by_role('heading', name='No people yet', exact=True)).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_coordinator_planner_editing_keeps_the_desktop_notice_on_a_phone(browser, live_server, h):
    context, page = phone(browser, live_server, 'coordinator')
    try:
        page.evaluate('a => location.hash = a', DESKTOP_ONLY[1][0])
        expect(notice(page, 'Schedule planning').get_by_role('button', name='Continue anyway', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('view', ['home', 'requests'])
def test_phone_home_primary_is_an_icon_in_the_title_row_before_the_bell(browser, live_server, h, view):
    """H-24: on a phone, a page's icon-only action (New request; also Inbox's Mark all as read, by the same rule) sits in the top bar row (not
    a row of its own), beside the bell in both the visual and the keyboard order — the same on every page."""
    context, page = phone(browser, live_server, 'staff')
    try:
        expect_loaded(page, 'home')
        if view != 'home':
            navigate(page, view)
        top = page.locator('.topbar')
        new = top.get_by_role('button', name='New request', exact=True)
        bell = top.get_by_role('button', name=re.compile(r'^Inbox'))
        expect(new).to_be_visible()
        title, plus, inbox = (x.bounding_box() for x in (top.locator('h1'), new, bell))
        assert title['x'] < plus['x'] < inbox['x']
        assert abs((plus['y'] + plus['height'] / 2) - (inbox['y'] + inbox['height'] / 2)) < 4
        assert top.bounding_box()['height'] <= 64
        assert inbox['x'] - (plus['x'] + plus['width']) < 24  # next to the bell, not floating mid-bar
        new.focus()
        page.keyboard.press('Tab')
        expect(bell).to_be_focused()
        new.click()
        expect_loaded(page, 'requests')
        expect(page.get_by_role('dialog')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_every_desktop_only_route_shows_the_notice_and_leads_home(browser, live_server, h, theme):
    identity(h, 'coordinator_staff', ['coordinator', 'staff'], h.seed['people'][0])
    context, page = phone(browser, live_server, 'coordinator_staff', theme)
    try:
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        for address, name in DESKTOP_ONLY:
            page.evaluate('a => location.hash = a', address)
            box = notice(page, name)
            expect(box.get_by_role('button', name='Back to Home', exact=True)).to_be_visible()
            expect(box.get_by_role('button', name='Continue anyway', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'desktop-only-{theme}')
        # A deep link (e-mail, notification) never breaks: Back to Home leads to the employee Home.
        page.get_by_role('button', name='Back to Home', exact=True).click()
        expect_loaded(page, 'home')
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()


def test_continue_anyway_opens_the_page_with_a_banner_and_is_remembered_per_route(browser, live_server, h):
    context, page = phone(browser, live_server, 'coordinator')
    try:
        page.evaluate("location.hash = 'rules'")
        notice(page, 'Rules').get_by_role('button', name='Continue anyway', exact=True).click()
        expect_loaded(page, 'rules')
        banner = page.locator('#main .phone-banner')
        expect(banner).to_contain_text('Not optimised for phones.')
        # The navigation still lists only the phone views (a coordinator's Schedule changes published sessions, D10).
        assert nav(page) == ['home', 'schedule', 'requests', 'people', 'access']  # Review routes is a phone tab (QA-113)
        banner.get_by_role('button', name='Dismiss', exact=True).click()
        expect(banner).to_have_count(0)
        # Remembered for this route for the session (a reload too); dismissed stays dismissed this visit.
        navigate(page, 'home')
        navigate(page, 'rules')
        expect(page.locator('#main .desktop-only, #main .phone-banner')).to_have_count(0)
        page.reload()
        expect_loaded(page, 'rules')
        expect(page.locator('#main .phone-banner')).to_have_count(1)
        # Other routes still ask.
        page.evaluate("location.hash = 'configuration'")
        notice(page, 'Configuration')
        # Signing out leaves nothing in browser storage, the remembered choice included.
        page.get_by_role('button', name=re.compile(r'^Account menu, ')).click()
        page.get_by_role('menuitem', name='Sign out', exact=True).click()
        expect(page.get_by_label('Username or email', exact=True)).to_be_visible()
        assert page.evaluate("sessionStorage.getItem('fmuc.phone-continue')") is None
    finally:
        context.close()


@pytest.mark.parametrize('roles,theme', [(['coordinator', 'staff'], 'light'), (['admin', 'staff'], 'dark')])
def test_multi_role_accounts_get_the_employee_views_on_a_phone(browser, live_server, h, roles, theme):
    published(h)
    draft = h.workspace('Phone draft', day(10), day(17))  # planner work on Home's Needs you at a desk, never on a phone
    # A draft without a current check is a row a phone shows too, with its Fix list (coordinator ruling 7 Nov 2026,
    # home.ts 'unchecked'; test_browser_fix_list_qa3). Checked with nothing to fix, it is no row: check it first, so
    # the assertion below never depends on whether the automatic check finished before Home was read.
    h.lifecycle(draft, 'validate')
    username = '_'.join(roles)
    identity(h, username, roles, h.seed['people'][0])
    assert not [i for i in h.get('coordinator', '/api/home')['attention'] if i.get('rota_id') == draft['id']]
    context, page = phone(browser, live_server, username, theme)
    try:
        assert nav(page) == ['home', 'schedule', 'requests', 'people', 'access']  # Review routes is a phone tab (QA-113)
        expect_loaded(page, 'home')
        main = page.locator('#main')
        expect(main.locator('.home-rota, .ov-recent, .setup-card')).to_have_count(0)
        expect(main.locator('.home-today, .home-away')).to_have_count(2 if {'coordinator', 'admin'} & set(roles) else 0)  # an admin acts as coordinator
        expect(page.get_by_role('button', name='New rota', exact=True)).to_have_count(0)
        # No link into the planners' Schedule, which is desktop-only on a phone.
        expect(main.get_by_text('Open in Schedule')).to_have_count(0)
        expect(main.locator('.home-needs')).to_be_visible()
        expect(main.locator('.home-needs')).not_to_contain_text('Phone draft')
        # Their next duty as staff see it (Coming up), not the planner's My next duties.
        expect(main.get_by_role('heading', name='Coming up', exact=True)).to_be_visible()
        # The Schedule is their own published duties: no draft switcher, no planning.
        navigate(page, 'schedule')
        expect(page.locator('.rota-switcher')).to_have_count(0)
        expect(page.get_by_role('button', name='Auto-schedule')).to_have_count(0)
        expect(page.locator('#main .desktop-only')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_staff_ask_for_leave_and_an_approver_decides_it_on_a_phone(browser, live_server, h, theme):
    published(h)
    context, page = phone(browser, live_server, 'staff', theme)
    try:
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_role('radio', name=re.compile('^Leave')).check()
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
        expect(page.locator('#main .dt-card').filter(has_text='Leave')).to_contain_text('Waiting for approval')
    finally:
        context.close()
    context, page = phone(browser, live_server, 'approver', theme)
    try:
        # Home "Needs you" leads to the approvals list: what waits, its schedule impact, Approve / Decline.
        needs = page.locator('#main .home-needs')
        expect(needs).to_contain_text('Leave request')
        expect(needs.locator('li')).to_have_count(1)
        shot(page, f'home-approver-{theme}')
        needs.get_by_role('button', name='Review, Leave request,').click()
        expect_loaded(page, 'requests')
        card = page.locator('#main .dt-card').filter(has_text='Leave').first
        expect(card).to_contain_text('Approving opens 1 place')
        expect(card.get_by_role('button', name='Decline', exact=True)).to_be_visible()
        shot(page, f'approvals-{theme}')
        # The request opens full width on a phone: no resize grip (#246).
        card.locator('.request-title strong').click()
        expect(page.get_by_role('region', name='Annual')).to_be_visible()  # titled by its type
        expect(page.locator('.panel-grip:visible')).to_have_count(0)
        page.keyboard.press('Escape')
        expect(page.get_by_role('region', name='Leave')).to_have_count(0)
        card.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog', name='Approve leave').get_by_role('button', name='Approve', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Approved · ', exact=False)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
    assert h.get('approver', '/api/requests/all?type=leave')['items'][0]['stage'] == 'approved'


def test_desktop_keeps_everything_and_a_narrowed_window_takes_the_phone_scope(browser, live_server, h):
    context, page = phone(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        assert {'schedule', 'rules', 'configuration', 'people'} <= set(nav(page))
        page.keyboard.press('Control+k')
        expect(page.get_by_role('option', name='New rota')).to_be_visible()
        page.keyboard.press('Escape')
        navigate(page, 'rules')
        expect(page.locator('#main .desktop-only, #main .phone-banner')).to_have_count(0)
        # Narrowed below the phone breakpoint, the same rule applies at once; widened again, everything is back.
        page.set_viewport_size(PHONE)
        notice(page, 'Rules')
        assert nav(page) == ['home', 'schedule', 'requests', 'people', 'access']  # a coordinator changes published sessions on a phone (D10)
        page.set_viewport_size(DESKTOP)
        expect_loaded(page, 'rules')
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        assert 'rules' in nav(page)
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_the_phone_menu_is_a_modal_drawer(browser, live_server, h, theme):
    """N-03 / N-08: the phone menu opens over a scrim with the page behind inert; a tap on the scrim (or its close button)
    closes it and focus returns to Menu. Its head is the department and a close button: no pin, no key chip, no empty
    account strip."""
    context, page = phone(browser, live_server, 'staff', theme)
    try:
        menu = page.get_by_role('button', name='Menu', exact=True)
        menu.click()
        drawer = page.locator('.sidebar')
        expect(drawer).to_have_class(re.compile(r'\bnavigation-open\b'))
        expect(menu).to_have_attribute('aria-expanded', 'true')
        assert page.evaluate("document.querySelector('.workspace-shell').inert")
        for hidden in ['.pin-button', '.search-trigger kbd', '.brand-text strong', '.sidebar-bottom']:
            expect(drawer.locator(hidden)).to_be_hidden()
        expect(drawer.locator('.brand-text small')).to_be_visible()  # the department, once
        page.mouse.click(370, 500)  # the scrim, beside the 300px drawer
        expect(drawer).not_to_have_class(re.compile(r'\bnavigation-open\b'))
        expect(menu).to_have_attribute('aria-expanded', 'false')
        expect(menu).to_be_focused()
        assert not page.evaluate("document.querySelector('.workspace-shell').inert")
        menu.click()
        drawer.get_by_role('button', name='Close menu', exact=True).click()
        expect(drawer).not_to_have_class(re.compile(r'\bnavigation-open\b'))
        expect(menu).to_be_focused()
    finally:
        context.close()


def test_widening_past_the_phone_width_closes_the_phone_menu(browser, live_server, h):
    """N-03: the open phone menu makes the page behind it inert. Widened (a rotated tablet, a resized window) with the
    phone scope off, nothing redraws the page: the menu must still close, or the desktop page stays unusable."""
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    try:
        expect_loaded(page, 'home')
        page.get_by_role('button', name='Menu', exact=True).click()
        expect(page.locator('.sidebar')).to_have_class(re.compile(r'\bnavigation-open\b'))
        assert page.evaluate("document.querySelector('.workspace-shell').inert")
        page.set_viewport_size(DESKTOP)
        expect(page.locator('.sidebar')).not_to_have_class(re.compile(r'\bnavigation-open\b'))
        assert not page.evaluate("document.querySelector('.workspace-shell').inert")
        page.locator('#department-nav [data-route=people]').click()
        expect_loaded(page, 'people')
    finally:
        context.close()
