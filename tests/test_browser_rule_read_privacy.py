"""Rules stay out of credentialing navigation, search and person records."""
import re

import pytest
from playwright.sync_api import expect

from packages.contracts import schemas as s
from .governance_support import identity
from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_palette_sources import search
from .test_browser_person_profile import open_person, person
from .test_browser_rule_catalogue import catalogue_rule, flag, line


pytestmark = pytest.mark.browser
NOTE = 'Synthetic private line hint'


@pytest.fixture
def private_line(h):
    flag(h)
    source = line({'max': {'value': 5}}, note=NOTE,
                  who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]})
    catalogue_rule(h, 'duties_per_week', [source])
    return source


def test_credentialer_has_no_rules_page(browser, live_server, h, private_line):
    context, page = sign_in(browser, live_server, 'credentialer')
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    try:
        expect(page.locator('nav').get_by_role('button', name='Rules', exact=True)).to_have_count(0)
        for route in ('rules', 'rules/duties_per_week/' + private_line['id']):
            page.goto(live_server + '/#' + route)
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'restricted')
        assert not any('/api/policies/reach' in url or re.search(r'/api/rules/catalogue/[^/]+/history', url)
                       for url in requests)
    finally:
        context.close()


def test_credentialer_palette_has_no_rule_items(browser, live_server, h, private_line):
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        for query in ('Duties per week', NOTE, 'Rules'):
            menu = search(page, query)
            expect(menu.get_by_role('listbox')).not_to_have_attribute('aria-busy', 'true')
            expect(menu.locator('.palette-group', has_text=re.compile('^Rules$'))).to_have_count(0)
            expect(menu.get_by_role('option').filter(has_text=re.compile(r'· line \d+'))).to_have_count(0)
            expect(menu).not_to_contain_text(NOTE)
            expect(menu.locator('.palette-item .truncate').filter(has_text=re.compile('^Rules$'))).to_have_count(0)
            menu.get_by_role('combobox').press('Escape')
    finally:
        context.close()


def test_credentialer_person_page_has_no_rules_panel(browser, live_server, h, private_line):
    context, page = sign_in(browser, live_server, 'credentialer')
    requests, denied = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('response', lambda response: denied.append(response.url) if response.status == 403 else None)
    try:
        panel = open_person(page, person(h))
        expect(panel.get_by_role('tab', name='Rules', exact=True)).to_have_count(0)
        expect(panel).not_to_contain_text('Which rule applies?')
        expect(panel.get_by_label('Day', exact=True)).to_have_count(0)
        expect(page.locator('#toast-tray .toast.error')).to_have_count(0)
        assert not denied
        assert not any('/api/policies/applies' in url for url in requests)
    finally:
        context.close()


@pytest.mark.parametrize('role', [role for role in s.ROLE_NAMES if role in s.PERMISSIONS['rules.read']])
def test_coordinator_rules_unchanged(browser, live_server, h, private_line, role):
    if role not in h.seed['identities']:
        identity(h, role, [role])
    context, page = sign_in(browser, live_server, role)
    try:
        expect(page.locator('nav').get_by_role('button', name='Rules', exact=True)).to_be_visible()
        navigate(page, 'rules')
        expect(page.locator('#main')).to_contain_text('Duties per week')
        menu = search(page, 'Duties per week')
        expect(menu.get_by_role('listbox')).not_to_have_attribute('aria-busy', 'true')
        expect(menu.locator('.palette-group', has_text=re.compile('^Rules$'))).to_be_visible()
        expect(menu.get_by_role('option').filter(has_text='Current rule')).to_be_visible()
        expect(menu.get_by_role('option').filter(has_text=NOTE)).to_contain_text('· line 1')
        menu.get_by_role('combobox').press('Escape')
        menu = search(page, NOTE)
        expect(menu.get_by_role('listbox')).not_to_have_attribute('aria-busy', 'true')
        expect(menu.get_by_role('option')).to_have_count(1)
        expect(menu.get_by_role('option')).to_contain_text(NOTE)
        menu.get_by_role('combobox').press('Escape')
        panel = open_person(page, person(h))
        expect(panel.get_by_role('tab', name='Rules', exact=True)).to_be_visible()
        with page.expect_response(lambda response: '/api/policies/applies?' in response.url and response.status == 200):
            panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(panel.get_by_label('Day', exact=True)).to_be_visible()
    finally:
        context.close()
