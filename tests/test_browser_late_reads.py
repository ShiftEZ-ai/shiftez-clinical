"""Late answers never redraw what someone is reading: saved views arriving after a list is drawn update only its toolbar."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import form_submit, route_fetch, browser, live_server, sign_in, navigate, wait_held  # noqa: F401
from .governance_support import developer

pytestmark = pytest.mark.browser


def test_a_save_during_a_slow_list_read_shows_the_saved_record(browser, live_server, h):
    """Lists in memory: a refresh whose list read started before a save and lands after it never hides what was saved.
    Refreshes run one after another (a save during one runs again once it ends) and replies apply in the order the
    reads started, so the newest lists win (Programmes › Rotation types: create one while an older read is held)."""
    from .test_browser import fill_catalogue
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    pattern = re.compile(r'.*/api/catalogues\?known=.*')
    try:
        navigate(page, 'programmes')
        page.get_by_role('button', name='Rotation types', exact=True).click()
        # Answered as it stands now (before the save), released only after the save.
        page.route(pattern, lambda route: held.append((route, route_fetch(route))) if not held else route.continue_())
        h.configure('rotation_types', {'name': 'Already there', 'starts_on': '2020-01-01'})  # the live pulse refreshes: held
        wait_held(page, held, 40)
        page.get_by_role('button', name='New rotation type', exact=True).click()
        dialog = fill_catalogue(page, 'Procedure-UCC')
        expect(dialog.get_by_text('Configuration checked.', exact=True)).to_be_visible(timeout=20000)  # worked out as it is filled in
        form_submit(dialog).click()
        expect(dialog.get_by_role('button', name='Saving…')).to_be_visible()  # saved; its refresh waits for the held one
        route, stale = held[0]
        route.fulfill(response=stale)  # the older answer lands after the save
        expect(dialog).not_to_be_visible(timeout=30000)
        assert {t['name'] for t in h.get('coordinator', '/api/catalogues')['rotation_types']} >= {'Already there', 'Procedure-UCC'}
        expect(page.locator('#main').get_by_text('Procedure-UCC', exact=True)).to_be_visible()
        page.get_by_role('button', name='Rotations', exact=True).click()
        page.get_by_role('button', name='New rotation', exact=True).click()
        expect(page.get_by_role('dialog').get_by_label('Rotation type', exact=True).locator('option', has_text='Procedure-UCC')).to_have_count(1)
    finally:
        context.close()


def test_saved_views_arriving_late_keep_the_rows_on_screen(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route(re.compile(r'.*/api/table-views\?table=people$'), lambda route: held.append(route))
        navigate(page, 'people')
        row = page.locator('.dt-table tbody tr').first
        expect(row).to_be_visible()
        node = row.element_handle()
        assert held, 'the saved-views read was not held'
        held.pop().continue_()
        expect(page.locator('#main button.dt-saved')).to_be_visible()
        page.evaluate('() => new Promise(resolve => setTimeout(resolve, 300))')
        assert node.evaluate('n => n.isConnected'), 'saved views arriving late redrew the rows'
    finally:
        context.close()


def test_an_older_filtered_answer_never_draws_over_cleared_filters(browser, live_server, h):
    """Audit log: the answer for a filter that was cleared meanwhile lands after the unfiltered one; the cleared list
    stays (only the newest read is drawn). The merge-queue flake of test_audit_log_filters_on_the_server_and_clears."""
    h.configure('categories', {'name': 'Late filter rank', 'starts_on': '2020-01-01'})
    h.workspace('Late filter rota')
    context, page = sign_in(browser, live_server, developer(h))
    held = []
    try:
        navigate(page, 'system/audit')
        rows = page.locator('.audit-row')
        rotas = page.locator('.audit-row .role-chip').filter(has_text=re.compile('^Rotas$'))
        expect(rotas).not_to_have_count(0)
        before = rows.count()
        page.route(re.compile(r'.*/api/audit\?.*action=catalogue.*'), lambda route: held.append((route, route_fetch(route))))
        page.get_by_role('button', name=re.compile('^Filter')).click()
        pop = page.get_by_role('dialog', name='Filter')
        pop.get_by_label('Kind of change', exact=True).select_option('config')
        wait_held(page, held)
        with page.expect_event('requestfinished', lambda r: '/api/audit?' in r.url and 'action=' not in r.url):
            pop.get_by_role('button', name='Clear all filters', exact=True).click()
        page.keyboard.press('Escape')
        expect(rows).to_have_count(before)
        route, filtered = held[0]
        with page.expect_event('requestfinished'):
            route.fulfill(response=filtered)  # the older, filtered answer lands last
        page.evaluate('() => new Promise(resolve => setTimeout(resolve, 300))')
        expect(rotas).not_to_have_count(0)
        expect(rows).to_have_count(before)
    finally:
        context.close()
