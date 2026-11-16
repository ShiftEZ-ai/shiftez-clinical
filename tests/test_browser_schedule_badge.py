"""The Schedule's sidebar count: open places in drafts (coordinators) and rotas waiting for review (approvers)."""
import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def test_schedule_nav_item_counts_open_places_and_rotas_waiting(browser, live_server, h):
    typ = h.configure('assignment_types', {'name': 'Badge clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    w = h.workspace('Badge draft')
    w = h.shift(w, people=[], typ=typ)['workspace']  # one open place
    h.lifecycle(w, 'validate')
    review = h.shift(h.workspace('Badge review', '2026-11-01', '2026-11-08'), '2026-11-01T08:00:00+03:00', '2026-11-01T12:00:00+03:00', typ=typ)['workspace']
    h.lifecycle(h.lifecycle(review, 'validate')['workspace'], 'submit')
    for role, label in (('coordinator', '1 session with open places in drafts'), ('approver', '1 rota waiting for review')):
        context, page = sign_in(browser, live_server, role)
        try:
            item = page.locator('#department-nav [data-route=schedule]')
            expect(item).to_have_accessible_description(label)
            expect(page.locator('#department-nav [data-route=planner]')).to_have_count(0)
        finally:
            context.close()
