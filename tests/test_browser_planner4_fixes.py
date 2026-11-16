"""PLANNER-4: a D65 refusal (build/handoff/PLANNER-4.md) is compact: only the findings the change introduced, without
detail.overlap, and its message may be the clash's own words. The exchange dialog still shows the clash's checks and says
it once."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser_planner import two_people_week

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_a_compact_exchange_refusal_shows_its_clash_once_with_its_checks(browser, live_server, h, scheme):
    a, b = h.seed['people'][:2]
    w = two_people_week(h, 'Compact refusal rota')
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[a])['workspace']
    shifts = h.detail(w)['shifts']
    sunday = next(s for s in shifts if s['start'].startswith('2026-10-04'))['participants'][0]
    elsewhere = next(s for s in shifts if s['start'].startswith('2026-10-05') and s['participants'][0]['person_id'] == a)
    clash = 'Clinical review session and Clinical review session overlap; cancel one occurrence'
    # The D65 shape: the clash sits on the other duty of the person moved (not one of the two exchanged sessions).
    refusal = {'error': {'code': 'SWAP_INVALID', 'message': clash, 'details': {'findings': [
        {'code': 'TIME_OVERLAP', 'message': clash, 'severity': 'error', 'person_id': a, 'shift_id': elsewhere['id'],
         'start': '2026-10-05T05:00:00+00:00', 'end': '2026-10-05T09:00:00+00:00'}]}}}
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        page.route('**/api/planner/preview', lambda route: route.fulfill(status=422, json=refusal))
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        page.locator(f".wk-grid {draft_chip(b)}").focus()
        page.keyboard.press('Shift+F10')
        page.get_by_role('menuitem', name=re.compile('^Exchange with')).click()
        dialog = page.get_by_role('dialog', name='Exchange two assignments')
        dialog.get_by_label('Second assignment', exact=True).select_option(sunday['id'])
        checks = dialog.locator('.preview-findings').filter(has_text='Checks for changed sessions · 1')
        expect(checks).to_be_visible(timeout=20000)
        lead = dialog.locator('.notice.error, .note.error, [role=alert]').filter(has_text='cancel one occurrence').first
        expect(lead).to_be_visible()
        assert lead.inner_text().count('cancel one occurrence') == 1, lead.inner_text()
    finally:
        context.close()
