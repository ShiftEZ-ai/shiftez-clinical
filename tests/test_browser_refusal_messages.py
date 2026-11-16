"""One message per refused change (LEAD-UIUX, 8 Nov 2026). A form that shows the server's words owns its refusal: the
bulk impact dialog's stale preview is said on its error line only, with no generic toast beside it. Where no form owns
it (the Fix list's Assign), one toast says it, with the app's own re-read ("Show the latest"), never "Reload this page"."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_session, session_action  # noqa: F401
from .test_browser_fix_list_panel import DESKTOP, real_gaps

pytestmark = pytest.mark.browser

COMMANDS = re.compile(r'.*/api/commands$')
PREVIEW_WORDS = 'Inputs changed since the impact preview. Refresh the preview before applying this change.'


def refuse(kind, code, message):
    def handle(route):
        if (route.request.post_data_json or {}).get('kind') == kind:
            return route.fulfill(status=409, json={'error': {'code': code, 'message': message}})
        route.continue_()
    return handle


def test_a_stale_preview_in_the_impact_dialog_is_said_by_the_dialog_only(browser, live_server, h):
    h.shift(h.workspace('Refusal in a form'), people=h.seed['people'][:2])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_session(page)
        session_action(page, 'Close session')
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Closure reason', exact=True).fill('Reviewed test clinic closure')
        dialog.get_by_role('button', name='Calculate impact', exact=True).click()
        expect(dialog.get_by_text('Not saved yet.', exact=False)).to_be_visible(timeout=20000)
        page.route(COMMANDS, refuse('shift.action', 'STALE_PREVIEW', PREVIEW_WORDS))
        dialog.get_by_role('button', name='Apply previewed draft change', exact=True).click()
        expect(dialog).to_contain_text(PREVIEW_WORDS)  # the server's words, on the form, which stays open
        expect(page.locator('#toast-tray .toast.error')).to_have_count(0)  # never a second message beside it
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_stale_rota_outside_a_form_is_one_toast_with_the_re_read(browser, live_server, h):
    from .test_browser_team_box import open_view
    from .test_browser_fixlist_undo import open_panel_at
    w, gaps, gap = real_gaps(h, 'refusal toast')
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        row = open_panel_at(page, w, gap)
        row.locator('.fix-chip', has_text=re.compile(r'^\w{3} 8')).click()
        assign = row.locator('.fix-fill-box').get_by_role('button', name=re.compile(r'^Assign '))
        expect(assign.first).to_be_visible()
        page.route(COMMANDS, refuse('shift.update', 'STALE_REVISION', 'The rota changed.'))
        assign.first.click()
        toasts = page.locator('#toast-tray .toast.error')
        expect(toasts).to_have_count(1)
        expect(toasts.first).to_contain_text('Your change was not saved')
        expect(toasts.first).not_to_contain_text('Reload this page')
        expect(toasts.first.get_by_role('button', name='Show the latest', exact=True)).to_be_visible()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()
