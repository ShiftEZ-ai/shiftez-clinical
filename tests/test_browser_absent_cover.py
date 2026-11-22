"""A submitted rota whose only assignee on a session goes on leave after the check: the grid strikes the person through
and the day reads short in words and number (the absent person fills no place), not 1 of 1 filled."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_the_only_assignee_on_leave_leaves_the_day_short(browser, live_server, h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace('Leave after the check rota'), people=[person])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    w = h.lifecycle(w, 'submit')['workspace']
    detail = h.detail(w)
    shift, = detail['shifts']
    need = max(r.get('places_needed', r['required']) for r in detail['workspace']['validation']['coverage']
               if r['shift_id'] == shift['id'])
    assert need >= 1  # the session needs its one assignee: before the leave its day reads filled
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave',
        'leave_type': 'Annual leave', 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00',
        'private_reason': 'Synthetic leave'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        # Struck through as absent (the grid's key adds its own sample chip, .lg-mark).
        expect(page.locator('#schedule-view .pchip.absent:not(.lg-mark)')).to_have_count(1)
        day = page.get_by_role('columnheader', name=re.compile(rf'\b0 of {need} places filled'))
        expect(day).to_have_count(1)
        expect(day).to_have_class(re.compile(r'\bshort\b'))
        expect(day.locator('.wk-cov')).to_have_text(f'0/{need}')
        expect(page.get_by_role('columnheader', name=re.compile(rf'\b1 of {need} places filled'))).to_have_count(0)
    finally:
        context.close()
