"""Every empty state offers its next step (LIVE-11): with nothing published, Insights leads every role that can open the
Schedule there, not only planners."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('role', ['staff', 'coordinator', 'approver'])
@pytest.mark.parametrize('tab', ['insights/fairness', 'insights/exports'])
def test_every_empty_state_has_an_action(browser, live_server, h, role, tab):
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, tab)
        state = page.locator('#main .empty-state').filter(has=page.get_by_role('heading', name='Nothing published yet', exact=True))
        expect(state).to_be_visible()
        state.get_by_role('button', name='Open Schedule', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()
