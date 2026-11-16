"""QA follow-ups (rules & configuration): People's chips keep the catalogue's order on every row; Auto-schedule & scoring
gives "Existing assignments" room for its long choices and every group its heading line; the audit log's first day
heading is spaced from the toolbar."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_config  # noqa: F401
from .governance_support import developer

pytestmark = pytest.mark.browser


def test_clinic_chips_read_in_the_same_order_on_every_row(browser, live_server, h):
    order = [s['name'] for s in h.get('admin', '/api/catalogues')['services']]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        rows = page.locator('#main tbody tr')
        expect(rows).to_have_count(3)
        index = list(page.locator('#main thead th').all_inner_texts()).index('Clinical eligibility')
        for row in rows.all():
            chips = row.locator('td').nth(index).locator('.role-chip').all_inner_texts()
            assert chips == [n for n in order if n in chips], chips
    finally:
        context.close()


def test_auto_schedule_fields_fit_and_groups_read_alike(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        page.get_by_role('tab', name='Staff preferences', exact=True).click()
        wishes = page.locator('fieldset').filter(has=page.locator('legend', has_text='Staff wishes'))
        # A heading like every other group: the legend with its help button (the help is a tooltip, not a paragraph).
        expect(wishes.locator('legend').get_by_role('button', name='About Staff wishes', exact=True)).to_be_visible()
        assert wishes.locator('legend').bounding_box()['y'] < wishes.get_by_label('Wish points renew each', exact=True).bounding_box()['y']
        page.get_by_role('tab', name='How it decides', exact=True).click()
        mode_width = page.get_by_label('Default for each run', exact=True).bounding_box()['width']
        page.get_by_role('tab', name='Timing', exact=True).click()  # search time and its ceiling (6e92c7b43)
        budget = page.get_by_role('textbox', name='Search time', exact=True)
        assert mode_width > 1.5 * budget.bounding_box()['width']  # two columns: its choice is not cut off
    finally:
        context.close()


def test_audit_day_heading_is_spaced_from_the_toolbar(browser, live_server, h):
    h.configure('location_types', {'name': 'Procedure room', 'description': '', 'starts_on': '2026-10-03', 'active': True})
    context, page = sign_in(browser, live_server, developer(h))
    try:
        navigate(page, 'system/audit')
        day = page.locator('.audit-day').first
        expect(day).to_be_visible()
        search = page.get_by_label('Search history', exact=True)
        assert day.bounding_box()['y'] - (search.bounding_box()['y'] + search.bounding_box()['height']) >= 10
    finally:
        context.close()
