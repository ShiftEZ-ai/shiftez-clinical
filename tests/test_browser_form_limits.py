"""Form limits that a field alone did not enforce: a swap offer's last allowed time (not only its day), and the step
links of an all-on-one-page form, which bring their part into view."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_browser_requests import published, new_request
from .test_browser_rules_sheet import rules

pytestmark = pytest.mark.browser


def test_swap_offer_open_until_is_checked_to_the_minute(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h, days=2)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap a duty')
        until = dialog.get_by_label('Offer open until', exact=True)
        latest = dialog.locator('input[name="Offer open until"]').get_attribute('max')
        assert latest and latest[11:] != '23:59', latest
        until.fill(latest[:10] + 'T23:59')  # the right day, later than allowed on it
        dialog.get_by_label('Swap with', exact=True).select_option(h.seed['people'][1])
        dialog.get_by_label('Message to them', exact=True).fill('Could we swap these days?')
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        # The shared date-and-time field refuses it (date_picker.ts), with the latest allowed minute in words.
        expect(dialog.locator('.field-error')).to_contain_text(f'{latest[11:16]} or earlier.')
        assert h.get('staff', '/api/requests/all?type=swap')['items'] == []
        # The latest allowed minute itself is accepted.
        until.fill(latest)
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(page.get_by_text('Offer sent.', exact=False)).to_be_visible()
        assert len(h.get('staff', '/api/requests/all?type=swap')['items']) == 1
    finally:
        context.close()


def test_step_links_bring_their_part_into_view(browser, live_server, h):
    rules(h)
    b = h.seed['people'][1]
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/directory')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 700})
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Daily hours')
        sheet.get_by_role('button', name=f'Edit line for {names[b]}').click()
        dialog = page.get_by_role('dialog', name='Change Daily hours')
        # All on one page: every part is there, the links jump (no Back / Next).
        expect(dialog.locator('.form-step:visible')).to_have_count(4)
        expect(dialog.get_by_role('button', name='Next', exact=True)).to_have_count(0)
        dialog.locator('.stepper .step-link').filter(has_text='Review & propose').click()
        expect(dialog.locator('.form-step').last).to_be_in_viewport()
    finally:
        context.close()
