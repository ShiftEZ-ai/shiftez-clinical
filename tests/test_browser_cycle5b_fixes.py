"""Fairness shows the rota of a chosen month
through the toolbar's ‹ month › (the rota in focus by default, R-3), and the grid counts the check's own places: a
skill or rank shortfall is not a filled place (R-2)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, pick_view, route_fetch  # noqa: F401
from .test_browser_fairness_dashboard import published_month, section
from .test_browser_published_changes import local_today
from .test_browser_rule_catalogue_v2 import themed

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (390, 'dark')]


@pytest.mark.parametrize('width,theme', VIEWS)
def test_fairness_shows_the_rota_of_the_month_chosen_in_its_toolbar(browser, live_server, h, width, theme):
    """R-3: the toolbar's ‹ month › (the shared period navigation) chooses whose rota the score and Balance goals show;
    with no month chosen it is this month. Going back to October shows an October rota's goals, asked for by its id."""
    published_month(h)  # October 2026: two published weeks with a duties goal
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    asked = []
    page.on('request', lambda r: asked.append(r.url) if '/api/fairness/goals?' in r.url else None)
    try:
        navigate(page, 'insights/fairness')
        nav = page.locator('.fx-toolbar .wk-nav')
        today = local_today()
        expect(nav.locator('strong')).to_have_text(today.strftime('%B %Y'))  # this month: no rota in focus
        for _ in range((today.year - 2026) * 12 + today.month - 10):  # back to October 2026, one month a press
            shown = nav.locator('strong').inner_text()
            nav.get_by_role('button', name='Previous month').click()
            expect(nav.locator('strong')).not_to_have_text(shown)
        expect(nav.locator('strong')).to_have_text('October 2026')
        goals = section(page, 'goals')
        expect(goals.locator('.fx-head small')).to_contain_text(re.compile(r'^On October week [12]'))
        october = [w['id'] for w in h.get('coordinator', '/api/workspaces')['workspaces'] if w['name'].startswith('October week')]
        assert any(any('workspace_id=' + rid in u for rid in october) for u in asked), asked
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_fairness_opens_on_the_rota_in_focus(browser, live_server, h):
    """R-3: opened after a rota (the December draft being planned), Fairness shows that rota's month and goals."""
    published_month(h)
    draft = h.workspace('December plan', '2026-12-06', '2026-12-13')
    h.shift(draft, start='2026-12-06T08:00:00+03:00', end='2026-12-06T12:00:00+03:00')
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-12-06?edit={draft['id']}")
        expect(page.get_by_role('button', name='Switch draft · December plan')).to_be_visible()
        navigate(page, 'insights/fairness')
        expect(page.locator('.fx-toolbar .wk-nav strong')).to_have_text('December 2026')
        expect(section(page, 'goals').locator('.fx-head small')).to_contain_text('On December plan')
    finally:
        context.close()


def test_a_skill_shortfall_is_an_open_place_in_the_grid_counts(browser, live_server, h):
    """R-2 (BE-FIX-B coverage contract): two people on a session that needs two, one of them lacking the required
    skill: the check says 1 of 2 places filled (places_*). The Coverage view counts that, not the basic headcount 2/2."""
    w = h.shift(h.workspace('Composition gap rota'), people=h.seed['people'][:2])['workspace']
    h.lifecycle(w, 'validate')
    context, page = themed(browser, live_server, 'coordinator')

    def places(route):
        reply = route_fetch(route, headers={k: v for k, v in route.request.headers.items() if k != 'if-none-match'})
        data = reply.json()
        for c in (data.get('workspace', {}).get('validation') or {}).get('coverage') or []:
            c.update(required=2, scheduled=2, available_eligible=2, status='covered', places_needed=2, places_filled=1, places_open=1)
        route.fulfill(status=200, json=data)
    try:
        page.route(re.compile(r'.*/api/workspaces/' + w['id'] + r'(\?.*)?$'), places)
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        pick_view(page, 'Coverage')
        cell = page.locator('.cov-cell').filter(has_text='1/2')
        expect(cell).to_have_count(1)
        expect(page.locator('.cov-cell.full')).to_have_count(0)
    finally:
        context.close()
