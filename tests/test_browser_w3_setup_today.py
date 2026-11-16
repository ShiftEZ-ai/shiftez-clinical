"""Setup guide headline states, per role, phone and dark."""
import json
import re

import pytest
from playwright.sync_api import expect

from .test_browser import route_fetch, browser, live_server, sign_in, navigate  # noqa: F401
from .test_resources_calendars import calendar, update

pytestmark = pytest.mark.browser


def no_page_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


def finish_required_steps(h, clinic_hours):
    """Every required step gets a record; clinics get opening hours only when asked."""
    cal = calendar(h)
    h.policy({'kind': 'maximum_participants', 'maximum': 3})
    h.workspace('Setup guide first rota')  # the coordinator's "Create your first rota" step
    if clinic_hours:
        for service in h.get('admin', '/api/catalogues')['services']:
            update(h, 'services', service, calendar_id=cal['id'])


def waiting_rule(h):
    """A proposed rule nobody has approved yet: a Data health item ("Rules waiting for approval") that is not a setup step."""
    h.command('coordinator', 'policy.create', {'name': 'Waiting rule', 'owner': 'Test policy owner', 'source': 'Constructed fixture',
              'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'max_hours', 'hours': 40}, 'scope': {},
              'severity': 'mandatory', 'exception_allowed': False, 'workspace_id': None}})


def empty_department(page):
    """Serve the department as brand new: every catalogue list empty."""
    def fulfil(route):
        body = route_fetch(route).json()
        route.fulfill(content_type='application/json',
                      body=json.dumps({k: [] if isinstance(v, list) else v for k, v in body.items()}))
    page.route(re.compile(r'/api/catalogues(\?|$)'), fulfil)
    page.reload()
    expect(page.get_by_role('button', name=re.compile(r'^Account menu, '))).to_be_visible()


def hero(page):
    return page.locator('.setup-hero h2')


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_setup_guide_new_department_says_set_up(browser, live_server, h, role):
    context, page = sign_in(browser, live_server, role)
    try:
        empty_department(page)
        navigate(page,'configuration/setup')
        expect(hero(page)).to_have_text('Set up your department')
        expect(page.get_by_role('img', name=re.compile(r'^0 of \d+ required steps complete$'))).to_be_visible()
        expect(page.locator('.setup-health')).to_contain_text('Nothing missing')
        # The trimmed aside: only Data health remains; no prose panels or the Ctrl K tip (the sidebar search has it).
        expect(page.locator('.setup-aside > section')).to_have_count(1)
        for gone in ['Why this order?', 'Adding many people?']:
            expect(page.get_by_role('heading', name=gone)).to_have_count(0)
        expect(page.locator('#main kbd')).to_have_count(0)
        people = page.locator('.setup-step').filter(has_text='People & eligibility')
        if role == 'coordinator':  # Imports is a coordinator page: bulk onboarding sits on the People step
            expect(people.get_by_role('button', name='Import spreadsheet', exact=True)).to_be_visible()
            people.get_by_role('button', name='Import spreadsheet', exact=True).click()
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'system')
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_setup_guide_ready_with_health_items_counts_them(browser, live_server, h, role):
    # Clinics without opening hours leave the Opening hours step undone (8df451143), so the three items are data facts.
    finish_required_steps(h, clinic_hours=True)
    h.configure('people', {'display_name': 'Unranked Newcomer'})  # no rank and no clinic
    waiting_rule(h)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page,'configuration/setup')
        items = page.locator('.setup-health li')
        expect(items).to_have_count(3)  # no rank, no clinic, a rule waiting for approval
        expect(hero(page)).to_have_text('Ready to plan · 3 things to fix')
        expect(page.locator('.setup-hero p')).to_have_count(0)  # ring and title only (C-05)
        expect(page.locator('.config-nav [data-section=setup]')).to_have_accessible_name('Data health, 3 things to fix')
        # People step is done, so its import shortcut is gone (People has its own).
        expect(page.locator('.setup-step').filter(has_text='People & eligibility')
               .get_by_role('button', name='Import spreadsheet')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['admin', 'coordinator'])
def test_setup_guide_ready_with_nothing_to_fix(browser, live_server, h, role):
    finish_required_steps(h, clinic_hours=True)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page,'configuration/setup')
        expect(hero(page)).to_have_text('Ready to plan')
        expect(page.locator('.setup-health')).to_contain_text('Nothing missing')
        expect(page.locator('.setup-health li')).to_have_count(0)
    finally:
        context.close()


def test_setup_guide_one_fix_is_singular_and_fits_phone_in_dark(browser, live_server, h):
    finish_required_steps(h, clinic_hours=True)
    waiting_rule(h)
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 390, 'height': 844})
    try:
        page.emulate_media(color_scheme='dark')
        navigate(page,'configuration/setup')
        expect(hero(page)).to_have_text('Ready to plan · 1 thing to fix')
        expect(page.locator('.setup-health li')).to_have_count(1)
        expect(page.locator('.setup-health').get_by_role('button', name='Review rules')).to_be_visible()
        no_page_overflow(page)
        assert page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches")
        colours = page.evaluate("""() => [getComputedStyle(document.querySelector('.setup-hero h2')).color,
            getComputedStyle(document.body).backgroundColor]""")
        assert colours[0] != colours[1], colours
    finally:
        context.close()
