"""Home consumes full server-side audit batch counts without merging unrelated actions."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .test_browser_person_profile import open_person, person, W
from .test_browser_requests import day
from .test_qa2_home_backend import recent_fixture

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


@pytest.fixture
def h(pg):
    return pg


def test_recent_changes_shows_full_undo_and_each_earlier_batch(browser, live_server, h):
    recent_fixture(h)
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, 'home')
        changes = page.locator('.ov-changes li')
        expect(changes).to_have_count(4)
        expect(changes.nth(0)).to_contain_text('98 requests')
        expect(changes.nth(0)).to_contain_text('undid')
        for row, count in zip((1, 2, 3), (98, 12, 7)):
            expect(changes.nth(row)).to_contain_text(f'{count} requests')
    finally:
        context.close()


def test_workload_cards_do_not_wait_for_opportunity_calculations(browser, live_server, h):
    w = h.shift(h.workspace('Independent open place draft', day(), day(7)),
                f'{day()}T08:00:00+03:00', f'{day()}T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    pattern = '**/api/people/*/score?open_limit=20*'
    try:
        panel = open_person(page, person(h))
        page.route(pattern, lambda route: held.append(route))
        panel.get_by_role('tab', name=W, exact=True).click()
        shown = panel.get_by_role('tabpanel', name=W, exact=True)
        shown.get_by_label('Period', exact=True).select_option(label=w['name'])
        wait_held(page, held)
        expect(shown.locator('.ov-stat-label')).to_have_text(['Duties', 'Hours', 'Weekends', 'Fairness'])
        expect(shown.get_by_role('heading', name='Score in Independent open place draft (draft)', exact=True)).to_be_visible()
        held.pop().abort()
        expect(shown.get_by_text('Couldn’t load open places.', exact=False)).to_be_visible()  # P-16 wording
        expect(shown.locator('.ov-stat-label')).to_have_text(['Duties', 'Hours', 'Weekends', 'Fairness'])
        page.unroute(pattern)
        shown.get_by_role('button', name='Retry loading open places', exact=True).click()
        expect(shown.get_by_role('heading', name='Open places Staff 01(test) could fill', exact=True)).to_be_visible()
    finally:
        for route in held:
            route.abort()
        context.close()
