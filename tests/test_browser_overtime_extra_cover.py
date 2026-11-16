"""Extra cover: an admin makes a duty type overtime only and chooses what an unfilled place is; the planner footer says
an open extra-cover session apart from sessions that need staff."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_config, fill_catalogue, save_dialog, open_rota  # noqa: F401
from .test_overtime_extra_cover import extra_type

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def test_admin_sets_extra_cover_on_a_duty_type(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Duty types')
        page.get_by_role('button', name='New duty type', exact=True).click()
        dialog = fill_catalogue(page, 'Extra UCC evening')
        unfilled = dialog.get_by_label('If nobody volunteers', exact=True)
        expect(unfilled).to_be_hidden()  # only meaningful once the duty is extra cover
        dialog.get_by_label('Extra cover: overtime only', exact=True).check()
        expect(unfilled).to_have_value('open')
        unfilled.select_option('gap')
        save_dialog(dialog)
        saved = next(t for t in h.get('admin', '/api/catalogues')['assignment_types'] if t['name'] == 'Extra UCC evening')
        assert saved['definition']['extra_cover'] is True and saved['definition']['extra_cover_unfilled'] == 'gap'
    finally:
        context.close()


def test_editing_an_extra_cover_duty_type_shows_its_settings(browser, live_server, h):
    duty = extra_type(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, f"configuration/assignment_types?open={duty['id']}")
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_label('Extra cover: overtime only', exact=True)).to_be_checked()
        expect(dialog.get_by_label('If nobody volunteers', exact=True)).to_have_value('open')
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_open_extra_cover_is_said_apart_from_sessions_that_need_staff(browser, live_server, h, viewport):
    duty = extra_type(h)
    w = h.shift(h.workspace('Extra cover open'), typ=duty['id'], people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        # D9/A7: the footer reads the server's Fix list. Open extra cover is one could-improve cause said as extra
        # cover, never a must-fix gap like a session that needs staff.
        causes = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")['causes']
        assert [c['level'] for c in causes] == ['could'] and 'extra cover' in causes[0]['sentence'], causes
        footer = page.locator('.app-footer')
        expect(footer.get_by_role('button', name=re.compile('^Ready to submit'))).to_be_visible()
        expect(footer.get_by_role('button', name=re.compile('must fix'))).to_have_count(0)
        footer.get_by_role('button', name='1 could improve', exact=True).click()
        health = page.get_by_role('dialog', name='Rota health')
        # Said once, as extra cover: one item of the one issue list (4c8b40be3: the footer popover is the issue list).
        # The issue list's one item: a card, or a row for a cause without card fields (issue_marker.ts li.iss-row).
        item = health.locator('li.finding, li.iss-row')
        expect(item).to_have_count(1)
        expect(item).to_contain_text(causes[0]['sentence'])
    finally:
        context.close()
