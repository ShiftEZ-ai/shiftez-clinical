"""Web pickups of the rc.12 backend fixes: an empty rota has no score ("n/a") and is not "All clear"; a blocked
person's headcount shortfall is said inside that one blocking issue, with its interval and counts."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def test_empty_rota_has_no_score_and_is_not_all_clear(browser, live_server, h):
    w = h.lifecycle(h.workspace('Empty rota'), 'validate')['workspace']
    assert h.detail(w)['workspace']['validation']['score']['score'] is None
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        # The footer uses the same empty-rota prerequisite as submission.
        expect(page.get_by_text('Add a session before submitting', exact=True).first).to_be_attached()
        expect(page.get_by_role('button', name=re.compile('^Schedule score'))).to_have_count(0)
        expect(page.locator('.health-pill.ok')).to_have_count(0)
    finally:
        context.close()


def test_blocked_persons_cover_gap_is_inside_one_blocking_issue(browser, live_server, h):
    typ = h.configure('assignment_types', {'name': 'Single cover', 'starts_on': '2020-01-01',
                                           'definition': {'requirements': [{'minimum': 1}]}})['record']
    person = h.seed['people'][0]
    h.command('coordinator', 'availability.record', {'person_id': person, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic leave'})
    w = h.shift(h.workspace('Grouped gap'), typ=typ['id'], people=[person])['workspace']
    validation = h.detail(w)['workspace']['validation']
    assert validation['error_count'] == 1
    root, = [f for f in validation['findings'] if f['severity'] == 'error']
    assert [d['code'] for d in root['detail']['derived_findings']] == ['COVERAGE_GAP']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        # A7 (D9): the draft footer counts the Fix list's must-fix causes; the gap is no cause of its own.
        blocking = page.locator('.app-footer .fix-pills').get_by_role('button', name=re.compile('^1 must fix'))
        expect(blocking).to_be_visible()
        blocking.click()
        expect(page.locator('.popover.fix-pop .iss-row')).to_have_count(1)  # the one issue list (live check F6)
        page.keyboard.press('Escape')
        # The rota's checks one by one (the Fix list's All checks): the leave's row says the shortfall it causes.
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        issues = page.locator('#main')
        expect(issues.get_by_role('heading', name=re.compile('^All checks'))).to_be_visible(timeout=20000)
        expect(issues.locator('tbody tr[data-row], .dt-card').filter(has_text='Also leaves')).to_have_count(1)
        expect(issues).to_contain_text('Also leaves 0 of 1 needed, 08:00–12:00')
    finally:
        context.close()
