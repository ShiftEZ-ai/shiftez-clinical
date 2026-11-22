"""Common journeys never dead-end (DESIGN.md › No dead ends): an empty list, an empty week and missing setup each carry
the way forward in place, and auto-schedule says why it cannot run before anyone presses Run."""
import pytest
from playwright.sync_api import expect
from datetime import date

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, open_rota, open_scheduling, new_draft  # noqa: F401
from .test_browser import whole_list, save_dialog

pytestmark = pytest.mark.browser


def test_legacy_qa_2027_draft_offers_capture_baseline_before_auto_schedule(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        actor_id = h.seed['identities']['coordinator']
        workspace = m.Workspace(id=m.uid(), created_at=m.now(), department_id=h.seed['department_id'],
                                created_by=actor_id, last_edited_by=actor_id, name='QA 2027 legacy baseline',
                                starts_on=date(2027, 10, 3), ends_on=date(2027, 10, 10), status='draft',
                                revision=1, content_revision=1)
        db.add(workspace)
        db.flush()
        w = {'id': workspace.id}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2027-10-03?edit={w['id']}")
        expect(page.get_by_role('button', name='Capture baseline', exact=True)).to_be_visible()
        page.get_by_role('button', name='Capture baseline', exact=True).click()
        expect(page.locator('.toast').last).to_contain_text('Draft baseline captured')
        expect(page.get_by_role('button', name='Auto-schedule', exact=True)).to_be_visible()
    finally:
        context.close()


def test_an_empty_draft_offers_add_session_in_the_week_and_in_auto_schedule(browser, live_server, h):
    w = h.workspace('Empty draft')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        empty = page.locator('.wk-empty')
        expect(empty).to_contain_text('No sessions this week')
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        expect(panel.get_by_role('status').filter(has_text='This draft has no sessions yet.')).to_be_visible()
        expect(panel.get_by_role('button', name='Run auto-schedule')).to_be_disabled()
        # Beside Run: the draft has no sessions, never "every session is locked".
        expect(panel.locator('.auto-why')).to_have_text('This draft has no sessions, so there is nothing to fill.')
        panel.get_by_role('button', name='Add session', exact=True).click()
        expect(page.get_by_role('heading', name='Add session', exact=True)).to_be_visible()
    finally:
        context.close()


def test_new_draft_says_when_there_are_no_recurring_sessions_yet(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule')
        new_draft(page)
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        # A start that cannot succeed is disabled (G-01) with its reason on the choice (DL-007), no line under the field.
        patterns = dialog.get_by_label('Start from').locator('option[value="patterns"]')
        expect(patterns).to_be_disabled()
        expect(patterns).to_have_attribute('data-hint', 'None set up yet · Configuration')
        expect(dialog.locator('.missing-setup')).to_have_count(0)
        expect(dialog.get_by_text('Use all active recurring sessions')).to_be_hidden()
        # Nothing to choose, nothing asked: the rota is created without a recurring session.
        dialog.get_by_label('Rota name', exact=True).fill('No recurring sessions')
        save_dialog(dialog)
    finally:
        context.close()



def test_a_reviewer_sees_draft_duties_a_leave_request_breaks_before_approving(browser, live_server, h):
    from .test_browser_requests import day
    draft = h.workspace('Next rota', day(), day(7))
    h.shift(draft, f'{day(1)}T08:00:00+03:00', f'{day(1)}T12:00:00+03:00', people=[h.seed['people'][0]])
    h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave', 'private_reason': 'Planned leave',
        'start': f'{day()}T00:00:00+03:00', 'end': f'{day(2)}T00:00:00+03:00'})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        impact = page.locator('#main .request-impact.has-impact')
        expect(impact).to_contain_text('1 duty in Next rota (not yet published)')
        page.locator('#main').get_by_role('button', name='Approve', exact=True).click()
        expect(page.get_by_role('dialog')).to_contain_text('1 duty in Next rota (not yet published)')
    finally:
        context.close()


def test_resolve_issues_offers_find_someone_on_a_short_session_row(browser, live_server, h):
    from packages.domain.rota_summary import issue_key
    w = h.workspace('Short rota')
    w = h.shift(w, people=[])['workspace']
    w = h.lifecycle(w, 'validate')['workspace']
    gap = next(f for f in w['validation']['findings'] if f['code'] == 'COVERAGE_GAP')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        # Grouped by cause, the reason heads the group and the row explains the cause ("… places are free but not assigned").
        expect(page.locator('#main tbody tr.dt-group').filter(has_text='still needs someone')).to_have_count(1)
        row = page.locator(f'#main tbody tr[data-row="{issue_key(gap)}"]')
        row.get_by_role('button', name='Find someone…').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()
