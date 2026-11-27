"""One real browser check for the combined calendar (the Schedule's People view) and private staff projection."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate
from .test_alpha_calendar import programme

pytestmark = pytest.mark.browser


def test_calendar_and_programme_forms(h, live_server, browser):
    programme(h)
    h.publish(h.shift(h.workspace(), people=h.seed['people'][:2])['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The team calendar is the Schedule's People view on the published layer.
        navigate(page, 'schedule/people/2026-10-04?edit=none')
        expect(page.get_by_role('rowheader', name='Staff 01(test)')).to_be_visible()
        expect(page.get_by_role('rowheader', name='Staff 02(test)')).to_be_visible()
        expect(page.get_by_label('Staff 01(test) · Clinical review session', exact=False).first).to_be_visible()
        page.get_by_role('button', name='Programmes', exact=True).click()
        page.get_by_role('button', name='Programme memberships', exact=True).click()
        expect(page.get_by_role('heading', name='Staff 01(test) · Family medicine residency')).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', viewport={'width':390,'height':844})
    try:
        # Staff see their own published duties only (fixed "My duties").
        navigate(page, 'schedule/people/2026-10-04')
        # A phone's row heads use the short name by default (app.ts labelNow, adf40abbe), "S. 01(test)" and the short code.
        expect(page.get_by_role('rowheader', name=re.compile(r'^S\. 01\(test\)(?=\s|$)'))).to_be_visible()
        expect(page.get_by_role('rowheader', name=re.compile(r'^(S\. 02\(test\)|Staff 02\(test\))(?=\s|$)'))).to_have_count(0)
        expect(page.get_by_label('Schedule source', exact=True)).to_have_count(0)
    finally:
        context.close()
