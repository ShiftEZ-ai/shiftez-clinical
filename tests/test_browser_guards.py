"""The unsaved-changes guard (real browser). Undo after Archive: test_browser_rota_archive.py; the same-button confirm
itself: test_browser_armed_confirm.py."""
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, toolbar  # noqa: F401

pytestmark = pytest.mark.browser


def test_unsaved_changes_guard(browser, live_server, h):
    # Undo after Archive (the reversible rota action): test_browser_rota_archive.py.
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # Unsaved changes: an untouched dialog closes at once; a changed one asks first.
        navigate(page, 'people')
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view')
        expect(dialog).to_be_visible()
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)

        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog.get_by_label('View name').fill('Half typed')
        page.keyboard.press('Escape')
        bar = dialog.locator('.unsaved-bar')
        expect(bar).to_contain_text('Not saved. Close anyway?')
        expect(dialog).to_be_visible()
        bar.get_by_role('button', name='Keep editing', exact=True).click()
        expect(bar).to_have_count(0)
        expect(dialog.get_by_label('View name')).to_have_value('Half typed')
        # A click on the backdrop is the same as Close.
        page.mouse.click(5, 5)
        expect(bar).to_be_visible()
        bar.get_by_role('button', name='Discard changes', exact=True).click()
        expect(dialog).to_have_count(0)
        assert h.get('coordinator', '/api/table-views?table=people')['views'] == []
    finally:
        context.close()
