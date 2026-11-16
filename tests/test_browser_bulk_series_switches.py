"""The contract-driven Change form exposes the usual-assignees switch; the retired close switch (D38/DL-009) is never offered."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_bulk_changes import bar, change_row, panel
from .test_bulk_series_counting import series


pytestmark = pytest.mark.browser


def test_recurring_switches_show_in_bulk_change_and_save_with_undo(browser, live_server, h):
    source = series(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, f'people/related/recurring_series?parent=people&of={h.seed["people"][0]}')
        page.get_by_role('checkbox', name=re.compile(r'^Select all .* shown')).check()
        bar(page).get_by_role('button', name='Change…').click()
        fixed = change_row(page, 'Keep the usual assignees on every session', 'Set to')
        # The switch defaults to Yes for a series with usual people (schemas.py), so No is the real change (223f443e4).
        fixed.locator('.bulk-value select').select_option(label='No')
        expect(page.get_by_text('Close a session when every usual assignee is away')).to_have_count(0)
        # A few records take the light path (maintenance): its button says what it applies, "Apply 1 change".
        panel(page).get_by_role('button', name=re.compile(r'^(Preview →|Continue|Apply \d+ changes?)$')).click()
        expect(page.locator('.toast', has_text=re.compile('changed'))).to_be_visible()
        saved = next(row for row in h.get('admin', '/api/catalogues')['recurring_series'] if row['id'] == source['id'])
        assert saved['definition'] == {**source['definition'], 'usual_assignees_fixed': False}
        page.locator('.app-footer').get_by_role('button', name='Undo last change').click()
        expect(page.locator('.toast', has_text='Undone')).to_be_visible()
        restored = next(row for row in h.get('admin', '/api/catalogues')['recurring_series'] if row['id'] == source['id'])
        assert restored['definition'] == source['definition']
    finally:
        context.close()
