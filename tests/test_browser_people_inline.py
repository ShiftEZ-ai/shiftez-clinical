"""An inline People edit keeps what the people read added (aliases, current facts) across navigation (maintenance review)."""
import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, navigate, sign_in, toolbar, close_popover  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_inline_save_keeps_enriched_row_after_navigation(browser, live_server, h, scheme):
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.SourceMapping(department_id=h.seed['department_id'], source_name='Synthetic alias', source_key='alias:inlinealiasperson',
                               target_kind='people', target_id=person))
    name = next(p['display_name'] for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person)
    context, page = sign_in(browser, live_server, 'admin', color_scheme=scheme)
    try:
        navigate(page, 'people')
        # Short code is hidden by default for an admin: show it (the setting is kept for the second visit).
        toolbar(page, 'View settings').locator('.dt-pop-item', has_text='Short code').locator('input').check()
        close_popover(page)
        search = page.get_by_role('searchbox', name='Search people', exact=True)
        search.fill('inlinealiasperson')
        row = page.locator('#main tbody tr[data-row]').filter(has_text=name)
        expect(row).to_have_count(1)
        row.locator('td[data-col=code]').click()
        editor = page.get_by_role('textbox', name='Short code', exact=True)
        editor.fill('QZX')
        with page.expect_response(lambda r: '/api/commands' in r.url):
            editor.press('Enter')
        expect(row.locator('td[data-col=code]')).to_have_text('QZX')
        navigate(page, 'home')
        navigate(page, 'people')
        search.fill('inlinealiasperson')  # the alias came with the read, not the write reply
        expect(page.locator('#main tbody tr[data-row]').filter(has_text=name)).to_have_count(1)
        expect(page.locator('#main tbody tr[data-row]').filter(has_text=name).locator('td[data-col=code]')).to_have_text('QZX')
    finally:
        context.close()
