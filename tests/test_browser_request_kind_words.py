"""Requests name each kind from the contract's list: a rotation change asked for on the rotation is named as such."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401
from .test_browser_rotation_board import residency

pytestmark = pytest.mark.browser


def test_rotation_change_request_is_named_in_requests(browser, live_server, all_roles):
    h = all_roles
    _, _, types, (trainee, _, _), planned = residency(h)
    first = next(r for r in planned if r['person_id'] == trainee and r['starts_on'] == '2026-10-01')
    h.command('staff', 'rotation.request_change', {'rotation_id': first['id'], 'rotation_type_id': types[2]['id'], 'starts_on': '2026-10-01',
                                                   'ends_on': '2026-11-01', 'private_reason': 'Fictional elective swap'})
    context, page = sign_in(browser, live_server, 'programme_lead')
    try:
        navigate(page, 'requests')
        expect(page.locator('#main [data-row]').filter(has_text='Rotation change').first).to_be_visible()
    finally:
        context.close()
