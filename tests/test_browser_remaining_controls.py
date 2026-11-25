"""Account, calendar and desktop-alert controls preserve their promised outcomes.

Real app/API, disposable local fixtures and existing browser helpers. No real
password entry. Platform clipboard rejection / missing Notification are explicit
capability boundary simulations, not mocked application responses or OS delivery.
"""
import re
from datetime import timedelta

import pytest
from playwright.sync_api import expect
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, save_dialog, expect_loaded  # noqa: F401
from .test_browser_accounts import users_file  # noqa: F401

pytestmark = pytest.mark.browser


def _command(kind):
    return lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST' and r.request.post_data_json['kind'] == kind


def _account_action(page, name, action):
    # Managed accounts append their sign-in name to the menu's accessible label.
    row = page.get_by_role('row').filter(has=page.get_by_role('cell', name=name, exact=True))
    card = page.locator('.dt-card').filter(has=page.locator('.dt-card-title').get_by_text(name, exact=True))
    row.or_(card).get_by_role('button', name=re.compile('^More actions for ')).click()
    page.get_by_role('menuitem', name=action, exact=True).click()


def _confirmed_menu_command(page, name, action, kind):
    _account_action(page, name, action)
    confirm = page.get_by_role('menuitem', name='Confirm? '+action, exact=True)
    expect(confirm).to_be_visible()
    with page.expect_response(_command(kind)) as saved:
        confirm.click()
    assert saved.value.status == 200
    return saved.value.json()


def test_owned_identity_disable_enable_and_end_all_sessions(browser, live_server, h):
    actor = 'coverage_session_staff'
    target = identity(h, actor, ['staff'])
    name = 'Fictional '+actor
    before_roles = [r for r in h.get('admin', '/api/access')['bindings'] if r['identity_id'] == target]
    contexts = []
    try:
        admin, page = sign_in(browser, live_server, 'admin')
        contexts.append(admin)
        first, _ = sign_in(browser, live_server, actor)
        contexts.append(first)
        navigate(page, 'access/people')
        _account_action(page, name, 'Disable sign-in')
        form = page.get_by_role('dialog', name='Disable sign-in', exact=True)
        form.get_by_label('Reason', exact=True).fill('Fictional lost-device exercise')
        save_dialog(form, 'Disable sign-in')
        row = page.locator('tr[data-row="'+target+'"]')
        expect(row.get_by_role('cell', name='Disabled', exact=True)).to_be_visible()
        assert first.request.get(live_server+'/api/me').status == 401
        assert next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == target)['disabled']

        _account_action(page, name, 'Enable sign-in')
        form = page.get_by_role('dialog', name='Enable sign-in', exact=True)
        form.get_by_label('Reason', exact=True).fill('Fictional device recovered')
        save_dialog(form, 'Enable sign-in')
        page.reload()
        expect_loaded(page, 'access')
        expect(row.get_by_role('cell', name='Active', exact=True)).to_be_visible()
        assert [r for r in h.get('admin', '/api/access')['bindings'] if r['identity_id'] == target] == before_roles
        second, _ = sign_in(browser, live_server, actor)
        contexts.append(second)
        third, _ = sign_in(browser, live_server, actor)
        contexts.append(third)
        assert second.request.get(live_server+'/api/me').json()['roles'] == ['staff']
        ended = _confirmed_menu_command(page, name, 'End all sessions', 'account.end_sessions')
        assert ended['ended'] >= 2  # May include the first session: Disable deliberately keeps it for Undo.
        for context in (first, second, third):
            assert context.request.get(live_server+'/api/me').status == 401
        assert not next(i for i in h.get('admin', '/api/access')['identities'] if i['id'] == target)['disabled']
        fresh, _ = sign_in(browser, live_server, actor)
        contexts.append(fresh)
        assert fresh.request.get(live_server+'/api/me').json()['roles'] == ['staff']
        assert admin.request.get(live_server+'/api/me').status == 200
    finally:
        for context in reversed(contexts):
            context.close()


def test_unused_invitation_revoke_new_link_and_resend(users_file, browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        name = 'Unused coverage invitation (test)'
        navigate(page, 'access/people')
        page.get_by_role('button', name='Invite person', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Name shown in the app', exact=True).fill(name)
        form.get_by_label('Email', exact=True).fill('unused.coverage@example.test')
        form.get_by_label('Reason', exact=True).fill('Fictional invitation for a test account')
        form.get_by_role('button', name='Create invitation', exact=True).click()
        shown = page.get_by_role('dialog', name='Invitation link', exact=True)
        first = shown.get_by_label('One-time link', exact=True).input_value().rsplit('/', 1)[1]
        shown.get_by_role('button', name='Done', exact=True).click()
        target = next(i['id'] for i in h.get('admin', '/api/access')['identities'] if i['display_name'] == name)
        assert context.request.get(live_server+'/api/account-links/'+first).status == 200

        _confirmed_menu_command(page, name, 'Revoke invitation', 'account.revoke_invite')
        row = page.locator('tr[data-row="'+target+'"]')
        # Revoked/undated is distinct from a genuinely expired invitation.
        expect(row.get_by_role('cell', name='No active invitation', exact=True)).to_be_visible()
        assert context.request.get(live_server+'/api/account-links/'+first).status == 404
        account = h.get('admin', '/api/access')['accounts'][target]
        assert not account['invite_open'] and account['invite_expires_at'] is None and account['accepted_at'] is None

        _account_action(page, name, 'New invitation link')
        shown = page.get_by_role('dialog', name='Invitation link', exact=True)
        second = shown.get_by_label('One-time link', exact=True).input_value().rsplit('/', 1)[1]
        shown.get_by_role('button', name='Done', exact=True).click()
        assert context.request.get(live_server+'/api/account-links/'+second).status == 200
        expect(row.get_by_role('cell', name='Invited', exact=True)).to_be_visible()

        _account_action(page, name, 'Resend invitation')
        shown = page.get_by_role('dialog', name='Invitation link', exact=True)
        latest = shown.get_by_label('One-time link', exact=True).input_value().rsplit('/', 1)[1]
        shown.get_by_role('button', name='Done', exact=True).click()
        assert context.request.get(live_server+'/api/account-links/'+second).status == 404
        assert context.request.get(live_server+'/api/account-links/'+latest).status == 200
        page.reload()
        expect_loaded(page, 'access')
        expect(page.get_by_label('One-time link', exact=True)).to_have_count(0)
        expect(row.get_by_role('cell', name='Invited', exact=True)).to_be_visible()
        assert h.get('admin', '/api/access')['accounts'][target]['accepted_at'] is None
        # A real dated expiry keeps its separate label; only the undated revoked state changes wording.
        with transaction(h.app.state.engine, write=True) as db:
            link = db.scalar(select(m.AccountLink).where(m.AccountLink.identity_id == target,
                m.AccountLink.purpose == 'invite', m.AccountLink.revoked_at.is_(None), m.AccountLink.used_at.is_(None)))
            link.expires_at = m.now()-timedelta(minutes=1)
        page.reload()
        expect_loaded(page, 'access')
        expect(row.get_by_role('cell', name='Invitation expired', exact=True)).to_be_visible()
        expired = h.get('admin', '/api/access')['accounts'][target]
        assert not expired['invite_open'] and expired['invite_expires_at'] is not None
    finally:
        context.close()


def _link_action(page, name):
    """New link / Stop sharing: in the link's ⋯ menu, each ending the current link, so each asks twice (same-button confirm)."""
    page.get_by_role('button', name='More link actions', exact=True).click()
    page.get_by_role('menuitem', name=name, exact=True).click()
    page.get_by_role('menuitem', name='Confirm? ' + name, exact=True).click()


@pytest.mark.parametrize('clipboard', ['native', 'rejected'])
def test_calendar_new_link_copy_once_only_and_revocation(browser, live_server, h, clipboard):
    person = h.get('staff', '/api/me')['person_id']
    assert person, 'This fixture must have a linked staff person'
    h.publish(h.shift(h.workspace('Private calendar recovery'), people=[person])['workspace'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live_server)
        navigate(page, 'insights/exports')
        page.get_by_role('button', name='Subscribe in my calendar', exact=True).click()
        field = page.get_by_label('Your private calendar link', exact=True)
        expect(field).to_have_value(re.compile(r'/calendar/[\w-]+\.ics$'))
        original = field.input_value()
        assert context.request.get(original).text().count('BEGIN:VEVENT') == 1
        _link_action(page, 'New link')
        expect(page.get_by_role('button', name='More link actions', exact=True)).to_be_visible()
        expect(field).to_be_focused()  # focus follows the change, not dropped to the page
        page.wait_for_function('old => {const field=document.querySelector(".feed-url"); return field && field.value !== old;}', arg=original)
        current = field.input_value()
        assert context.request.get(original).status == 404
        assert context.request.get(current).status == 200
        if clipboard == 'rejected':
            page.evaluate("Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText:async()=>{throw new DOMException('Test clipboard denied','NotAllowedError')}}})")
        page.get_by_role('button', name='Copy link', exact=True).click()
        if clipboard == 'native':
            expect(page.get_by_text('Link copied. Add it in your calendar app as a subscription (from URL).', exact=True)).to_be_visible()
            assert page.evaluate('expected => navigator.clipboard.readText().then(actual => actual === expected)', current)
        else:
            expect(field).to_be_focused()
            assert field.evaluate('(input) => input.selectionStart === 0 && input.selectionEnd === input.value.length')
        page.reload()
        expect_loaded(page, 'insights')
        expect(field).to_have_count(0)
        expect(page.get_by_role('button', name='Copy link', exact=True)).to_have_count(0)
        assert page.evaluate('token => !JSON.stringify(localStorage).includes(token) && !JSON.stringify(sessionStorage).includes(token)', current.rsplit('/', 1)[1])
        assert context.request.get(current).status == 200
        _link_action(page, 'Stop sharing')
        expect(page.get_by_text('Calendar link stopped.', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Subscribe in my calendar', exact=True)).to_be_focused()
        assert context.request.get(current).status == 404
        assert h.get('staff', '/api/calendar-feed')['active'] is False
        page.reload()
        expect_loaded(page, 'insights')
        expect(page.get_by_role('button', name='Subscribe in my calendar', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='More link actions', exact=True)).to_have_count(0)
    finally:
        context.close()


def _alerts_menu(page):
    """Account › Notifications › This device: on a computer whose department has no push alerts, the tab alerts."""
    page.get_by_role('button', name=re.compile('^Account menu, ')).click()
    page.get_by_role('menuitem', name='Notifications', exact=True).click()
    return page.locator('.np-panel').get_by_role('button', name=re.compile('^Turn (on|off)$'))


@pytest.mark.parametrize('permission', ['granted', 'denied'])
def test_desktop_alert_permission_and_preference_survive_reload(browser, live_server, h, permission):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        # Chromium's native grantPermissions grants listed permissions and rejects all others.
        context.grant_permissions(['notifications'] if permission == 'granted' else [], origin=live_server)
        assert page.evaluate('Notification.permission') == permission
        control = _alerts_menu(page)
        if permission == 'denied':
            # Department push is off, so browser permission cannot change the outcome; keep the single Inbox line.
            panel = page.locator('.np-panel')
            expect(panel.get_by_text("Your department hasn’t turned alerts on · updates appear in your Inbox", exact=True)).to_be_visible()
            expect(panel.locator('.np-steps')).to_have_count(0)
            expect(control).to_have_count(0)
            assert page.evaluate("localStorage.getItem('rota.alerts')") != 'on'
        else:
            expect(control).to_have_text('Turn on')
            control.click()
            expect(page.get_by_text('Desktop alerts are on: new inbox items show even when this tab is in the background.', exact=True)).to_be_visible()
            assert page.evaluate("localStorage.getItem('rota.alerts')") == 'on'
            page.reload()
            expect_loaded(page, 'home')
            control = _alerts_menu(page)
            expect(control).to_have_text('Turn off')
            control.click()
            expect(page.get_by_text('Desktop alerts are off.', exact=True)).to_be_visible()
            assert page.evaluate("localStorage.getItem('rota.alerts')") == 'off'
            page.reload()
            expect_loaded(page, 'home')
            expect(_alerts_menu(page)).to_have_text('Turn on')
    finally:
        context.close()


def test_desktop_alert_unsupported_browser_keeps_alerts_off(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        # This simulates only browser capability absence. It does not simulate permission grant or OS delivery.
        page.add_init_script('delete window.Notification;')
        page.reload()
        expect_loaded(page, 'home')
        assert page.evaluate("'Notification' in window") is False
        expect(_alerts_menu(page)).to_have_count(0)
        expect(page.locator('.np-panel').get_by_text("Your department hasn’t turned alerts on · updates appear in your Inbox", exact=True)).to_be_visible()
        assert page.evaluate("localStorage.getItem('rota.alerts')") != 'on'
    finally:
        context.close()
