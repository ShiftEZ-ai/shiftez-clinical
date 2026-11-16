"""Configuration › Department settings › Leave on planning issues, and the issue cards it shapes (desktop and phone)."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, open_rota, open_config  # noqa: F401
from .test_leave_disclosure import NOTE, rota, setting

pytestmark = pytest.mark.browser
VIEWPORTS = [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}]


def test_admin_sets_how_leave_shows_with_undo(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        leave = page.get_by_label('Leave on planning issues', exact=True)
        expect(leave).to_have_value('type')  # the default
        leave.select_option('details')
        page.locator('.config-pane').get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Planners need the leave note')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert h.get('admin', '/api/me')['department']['leave_reason_display'] == 'details'
        page.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_label('Leave on planning issues', exact=True)).to_have_value('type')
        assert h.get('admin', '/api/me')['department']['leave_reason_display'] == 'type'
    finally:
        context.close()


@pytest.mark.parametrize('viewport', VIEWPORTS, ids=['desktop', 'phone'])
def test_leave_issue_card_shows_type_pill_dates_and_note(browser, live_server, h, viewport):
    w, _ = rota(h)
    setting(h, 'details')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        # The rota's checks one by one (the Fix list's All checks): the leave shows its type pill, dates and note.
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        issues = page.locator('#main')
        expect(issues.get_by_role('heading', name=re.compile('^All checks'))).to_be_visible(timeout=20000)
        row = issues.locator('tbody tr[data-row], .dt-card').filter(has_text='Annual').first
        expect(row.locator('.badge', has_text='Annual')).to_be_visible()
        expect(row).to_contain_text('3–5 Oct')
        expect(row).to_contain_text(NOTE)
        expect(issues).to_contain_text('On leave or away')
        expect(issues).not_to_contain_text('the reason stays private')
    finally:
        context.close()
    # Hidden: the card says only that they are away, and nothing about why.
    setting(h, 'none')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=viewport)
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        issues = page.locator('#main')
        expect(issues.get_by_role('heading', name=re.compile('^All checks'))).to_be_visible(timeout=20000)
        expect(issues).to_contain_text('the reason stays private')
        expect(issues).not_to_contain_text('Annual')
        expect(issues).not_to_contain_text(NOTE)
    finally:
        context.close()
