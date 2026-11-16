"""An ended session keeps the work on screen: signing in again as the same account returns to it (maintenance)."""
from datetime import timedelta
import pytest
from playwright.sync_api import expect
from sqlalchemy import update
from apps.api.auth import digest
from apps.api.models import Session
from packages.domain.time import now
from .conftest import TEST_PASSWORD
from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401

pytestmark = pytest.mark.browser
SUBMIT = 'Submit for review\u2026'
NOTE = 'Note for the approver (optional)'  # the submit note, labelled for its reader (2c36ef362)


def end_sessions(h, page):
    """Ends the browser's session only: the harness's own API sessions (h.detail) stay signed in."""
    token = next(c['value'] for c in page.context.cookies() if c['name'] == 'rota_session')
    with h.app.state.engine.begin() as db:
        db.execute(update(Session).where(Session.token_hash == digest(token)).values(expires_at=now() - timedelta(minutes=1)))


def submit_typed(page, h, w, typed):
    """Opens Submit for review, types a reason, lets the session end and presses Submit: the sign-in covers the page."""
    navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
    page.get_by_role('button', name=SUBMIT, exact=True).click()
    dialog = page.get_by_role('dialog', name=f"Submit {w['name']} for review", exact=True)
    dialog.get_by_label(NOTE, exact=True).fill(typed)
    end_sessions(h, page)
    dialog.get_by_role('button', name='Submit for review', exact=True).click()
    shade = page.locator('dialog.login-resume')
    expect(shade.get_by_role('status')).to_be_visible()
    return dialog, shade


def identify_again(shade, username):
    """The sign-in is identifier first: the username or email (prefilled and hidden on a remembered account's chip),
    Continue, then the password step. Returns the password field and its Sign in button."""
    field = shade.get_by_label('Username or email', exact=True)
    if field.is_visible():
        field.fill(username)
    shade.get_by_role('button', name='Continue', exact=True).click()
    password = shade.get_by_label('Password', exact=True)
    password.fill(TEST_PASSWORD)
    return password, shade.get_by_role('button', name='Sign in', exact=True)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_expired_session_keeps_open_form(browser, live_server, h, scheme):
    w = h.shift(h.workspace('Kept form rota'))['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        dialog, shade = submit_typed(page, h, w, 'Typed before the session ended')
        assert h.detail(w)['workspace']['status'] == 'draft'
        identify_again(shade, 'coordinator')[1].click()
        expect(shade).to_have_count(0)
        expect(dialog.get_by_label(NOTE, exact=True)).to_have_value('Typed before the session ended')
        dialog.get_by_role('button', name='Submit for review', exact=True).click()
        expect(dialog).to_have_count(0, timeout=15000)
        assert h.detail(w)['workspace']['status'] == 'submitted'
    finally:
        context.close()


def test_another_account_after_expiry_starts_afresh(browser, live_server, h):
    w = h.shift(h.workspace('Not for the next account'))['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        dialog, shade = submit_typed(page, h, w, 'Private to the first account')
        if not shade.get_by_label('Username or email', exact=True).is_visible():
            shade.get_by_role('button', name='Use another account', exact=True).click()
        expect(shade.get_by_label('Username or email', exact=True)).to_be_visible()
        identify_again(shade, 'approver')[1].click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=20000)
        expect(dialog).to_have_count(0)
        expect(page.get_by_text('Private to the first account')).to_have_count(0)
    finally:
        context.close()


def test_failed_account_read_after_sign_in_unlocks_and_retries(browser, live_server, h):
    """A failed /api/me after re-sign-in shows the error, unlocks the form and clears busy; the retry returns to the work."""
    w = h.shift(h.workspace('Retry after account read'))['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        dialog, shade = submit_typed(page, h, w, 'Kept through a failed read')
        failed = []

        def fail_once(route):
            if failed:
                route.continue_()
            else:
                failed.append(1)
                route.fulfill(status=500, content_type='application/json', body='{}')
        page.route('**/api/me', fail_once)
        password, sign = identify_again(shade, 'coordinator')
        sign.click()
        expect(shade.locator('#login-error')).not_to_be_empty()
        expect(sign).not_to_have_attribute('aria-busy', 'true')
        expect(password).to_be_editable()
        sign.click()
        expect(shade).to_have_count(0)
        assert failed
        expect(dialog.get_by_label(NOTE, exact=True)).to_have_value('Kept through a failed read')
    finally:
        context.close()
