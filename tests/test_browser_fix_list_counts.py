"""One Fix list, one count (D9): Home, the footer, Rota tools, the Fix list page and Submit all say the server's must-fix
causes for the same draft (GET /api/workspaces/{id}/fix-list), never a client recount. Real backend data (maintenance)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_rota  # noqa: F401

pytestmark = pytest.mark.browser


def test_home_footer_fix_list_and_submit_say_the_same_must_fix_count(browser, live_server, h):
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    w = h.shift(h.workspace('Fix list one count'), people=[])['workspace']
    fix = h.get('coordinator', f"/api/workspaces/{w['id']}/fix-list")
    must, could = fix['summary']['must'], fix['summary']['could']
    assert must >= 1 and must == sum(c['level'] == 'must' for c in fix['causes'])
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'home')
        row = page.locator('.home-needs-list li').filter(has_text='Fix list one count')
        expect(row.locator('.home-count')).to_have_text(str(must))
        open_rota(page, w['id'])
        expect(page.locator('.app-footer .fix-pills').get_by_role('button', name=re.compile(f'^{must} must fix'))).to_be_visible()
        if could:
            expect(page.locator('.app-footer .fix-pills')).to_contain_text(f'{could} could improve')
        page.get_by_role('button', name=re.compile(r'^Submit for review')).first.click()
        dialog = page.get_by_role('dialog', name=re.compile('^Submit .* for review$'))
        expect(dialog.locator('.fix-gate')).to_contain_text(f'{must} cause')
        expect(dialog.locator('.fix-row')).to_have_count(must)
        expect(dialog.locator('button[type=submit]')).to_be_disabled()
        page.keyboard.press('Escape')
        navigate(page, f'schedule/issues?draft={w["id"]}')
        expect(page.locator('#main .fix-gate')).to_contain_text(f'{must} must fix before you submit')
        expect(page.locator('#main section[data-level=must] .fix-level .badge')).to_have_text(str(must))
        # FIXLIST-UX: each row's line is the server's plain one; the scorer's units (checks) are never on a row.
        metas = page.locator('.fix-list > li[data-cause] .home-need-text small')
        expect(metas.first).to_be_visible()
        for meta in metas.all_text_contents():
            assert not re.search(r'\b\d+ checks?\b', meta), meta
    finally:
        context.close()
