"""Always-run Schedule smoke (UX layout smoke, 7 Nov 2026). Live 9d55e1057 broke every draft Week and Month with people
("Cannot access 'pillParts' before initialization", a const read in the grid draw before its declaration) because no
selected browser test drew them. scripts/affected_tests.py selects this file for every apps/web change. Each layout, on a
draft with people and on a published rota, loads, raises no script or console error and draws a person; the selection
panel and the session panel open once. Fictional data only (the seeded test people)."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, press_escape  # noqa: F401
from .test_browser_schedule import october, watch

pytestmark = pytest.mark.browser
DESKTOP, PHONE = {'width': 1440, 'height': 900}, {'width': 390, 'height': 844}
LAYOUTS = ('week', 'month', 'day', 'timeline', 'people', 'coverage', 'list')
# Views mark each drawn duty with data-person (planner_week highlightFocus); the Day bars name their people instead.
PERSON = ('#schedule-view :is(.pchip,.pg-chip,.ag-item,.cov-cell,tr)[data-person~="{id}"],'
          '#schedule-view .sd-bar[aria-label*="Staff 01"]')
FAILED = 'Couldn’t load the schedule.'


def open_layout(page, route):
    """Go to a Schedule address and wait until it is drawn, or has failed (a failed draw never sets data-loaded-view)."""
    page.evaluate("r => {if (location.hash !== '#' + r) {document.querySelector('#main').removeAttribute('data-loaded-view'); location.hash = r;}}", route)
    expect(page.locator('#main[data-loaded-view=schedule] #schedule-view, #main [role=alert]').first).to_be_attached(timeout=45000)
    expect(page.locator('#schedule-view .loading')).to_have_count(0, timeout=45000)


def check(page, errors, route, person, problems):
    """Open one layout and note what is wrong with it, so one run names every broken layout."""
    open_layout(page, route)
    drawn, failed = page.locator(PERSON.format(id=person)), page.locator('#main [role=alert]')
    try:
        expect(drawn.or_(failed).first).to_be_attached(timeout=15000)
    except AssertionError:
        pass
    if page.get_by_text(FAILED, exact=False).count():
        problems.append(f'{route}: {failed.first.inner_text()}')
    elif not drawn.count():
        problems.append(f'{route}: no person drawn')
    if errors:
        problems.append(f'{route}: {errors[:3]}')
        errors.clear()


def test_every_schedule_layout_draws_a_draft_and_a_published_rota(browser, live_server, h):
    _, draft = october(h)  # published 4–13 Oct (Alex on 4 and 12 Oct), draft 14–20 Oct (Alex on 14 Oct)
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    errors, problems = watch(page), []
    try:
        for layout in LAYOUTS:
            check(page, errors, f"schedule/{layout}/2026-10-14?edit={draft['id']}", alex, problems)
            check(page, errors, f'schedule/{layout}/2026-10-12?edit=none', alex, problems)
        assert not problems, problems
        # The selection panel (a day head selects) and the session panel (a person's duty opens it), on the draft Week.
        open_layout(page, f"schedule/week/2026-10-14?edit={draft['id']}")
        page.locator('.wk-day[data-rk*="2026-10-14"]').first.click()
        expect(page.locator('.rk-panel').get_by_role('heading', level=2)).to_be_visible()
        press_escape(page)  # nothing selected: the panel shows the rota's rules (Why panel v2, 539c1930b)
        expect(page.get_by_role('dialog', name='Rules in this rota')).to_be_visible()
        expect(page.locator('.rk-selected')).to_have_count(0)
        page.locator(f'#schedule-view button.pchip[data-assignment][data-person="{alex}"]').first.click()
        expect(page.locator('.wk-panel')).to_be_visible()
        assert not errors, errors
    finally:
        context.close()


def test_the_phone_agenda_draws_a_draft_and_a_published_rota(browser, live_server, h):
    _, draft = october(h)
    alex = h.seed['people'][0]
    context, page = sign_in(browser, live_server, 'coordinator', viewport=PHONE)
    errors, problems = watch(page), []
    try:
        for route in (f"schedule/week/2026-10-14?edit={draft['id']}", 'schedule/week/2026-10-12?edit=none'):
            check(page, errors, route, alex, problems)
            if not page.locator('#main .ag .ag-item').count():
                problems.append(f'{route}: not the agenda')
        assert not problems, problems
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
