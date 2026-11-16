"""The installable app and its alerts (mockups P1–P5, PLAN D27–D36): manifest and icons, the worker under CSP, the
install offer once, consent per device, My notifications and the department's policy.

Everything runs against the real push API (build/contracts/PWA-BE.md) with an ephemeral sender key. Browser push
transport and the OS permission prompt cannot be exercised headless: `MOCK_PUSH` stands in for PushManager and
Notification.requestPermission only, with a real P-256 browser key so the server's own validation runs.
"""
import base64
import json
import os
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from playwright.sync_api import expect
from packages.contracts.schemas import ABSENCE_REPORT_LABEL

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_browser import browser, live_server, sign_in, navigate, open_config, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
IPHONE_SAFARI = ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
                 'Version/18.0 Mobile/15E148 Safari/604.1')
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


@pytest.fixture
def push(h):
    """The server's push sender, ready: an ephemeral VAPID key (memory and the disposable database only, never printed)
    and the system switch on. Each test turns the department policy on itself. List it before live_server."""
    key = ec.generate_private_key(ec.SECP256R1())
    h.server_env = {**getattr(h, 'server_env', {}), 'FMUC_DATA_KEY': os.urandom(32).hex(),
                    'FMUC_VAPID_PRIVATE_KEY': b64(key.private_numbers().private_value.to_bytes(32, 'big')),
                    'FMUC_VAPID_KEY_ID': 'test-active', 'FMUC_VAPID_SUBJECT': 'mailto:push@example.org'}
    h.command('operator', 'pwa.system.update', {**s.PwaSystemSettings(enabled=True).model_dump(), 'reason': 'Enable disposable push'}, revision=1)
    h.public_key = b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return h


def policy(h, revision=1, **values):
    """The department's alert policy (admin command, as Configuration › Notifications saves it)."""
    return h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'], 'push_enabled': True,
                     'lock_screen_detail': 'event_date', 'reason': 'Department alerts for the test', **values}, revision=revision)


def department_email(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).email_settings = s.EmailSettings(
            smtp_enabled=True, sender_address='rota@example.org', smtp_host='relay.example.org').model_dump(mode='json')


def commands(context):
    """The commands this browser sends, in order (kind, payload, expected_revision)."""
    sent = []
    context.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
    return lambda kind: [{**c['payload'], 'expected_revision': c.get('expected_revision')} for c in sent if c['kind'] == kind]


# The device's side: what PushManager and the permission prompt would do, recorded across reloads.
MOCK_PUSH = '''(() => {
  const state = JSON.parse(localStorage.getItem('test.push') || '{"permission":"default"}');
  const save = () => localStorage.setItem('test.push', JSON.stringify(state));
  Object.defineProperty(Notification, 'permission', {configurable: true, get: () => state.permission});
  Notification.requestPermission = async () => { state.asked = (state.asked || 0) + 1; state.permission = state.answer || 'granted'; save(); return state.permission; };
  const sub = () => state.endpoint ? {endpoint: state.endpoint, expirationTime: null,
    toJSON: () => ({endpoint: state.endpoint, expirationTime: null, keys: __BROWSER_KEYS__}),
    unsubscribe: async () => { state.endpoint = null; save(); return true; }} : null;
  PushManager.prototype.getSubscription = async function () { return sub(); };
  PushManager.prototype.subscribe = async function (options) {
    state.key = btoa(String.fromCharCode(...new Uint8Array(options.applicationServerKey))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    state.endpoint = 'https://fcm.googleapis.com/fcm/send/test-endpoint'; save(); return sub(); };
})();'''
_browser = ec.generate_private_key(ec.SECP256R1())
MOCK_PUSH = MOCK_PUSH.replace('__BROWSER_KEYS__', json.dumps({
    'p256dh': b64(_browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
    'auth': b64(os.urandom(16))}))
pushed = lambda page: page.evaluate("JSON.parse(localStorage.getItem('test.push') || '{}')")


def _notifications(page, count):
    """The alerts this app shows, once there are `count` (the worker shows them asynchronously)."""
    return page.evaluate('''async count => { const reg = await navigator.serviceWorker.ready, until = performance.now() + 30000;
        for (;;) { const shown = await reg.getNotifications();
          if (shown.length >= count || performance.now() > until) return shown.map(n => ({title: n.title, body: n.body, url: n.data.url}));
          await new Promise(next => setTimeout(next, 50)); } }''', count)


def _account(page, item):
    page.get_by_role('button', name=re.compile('^Account menu, ')).click()
    page.get_by_role('menuitem', name=item, exact=True).click()


def _device(page):
    panel = page.locator('.np-panel')
    expect(panel.get_by_role('heading', name='This device')).to_be_visible()
    return panel


def test_manifest_icons_and_head_make_the_app_installable(browser, live_server):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(live_server)
        expect(page.get_by_role('heading', name='Sign in', exact=True)).to_be_visible()
        expect(page.locator('.app-offer')).to_have_count(0)  # D31: nothing on sign-in
        head = page.evaluate('''async () => {
            const link = document.querySelector('link[rel=manifest]').href, manifest = await (await fetch(link)).json();
            return {manifest, icons: manifest.icons.map(i => ({...i, src: new URL(i.src, link).href})),
              apple: document.querySelector('link[rel=apple-touch-icon]').href,
              themes: [...document.querySelectorAll('meta[name=theme-color]')].map(m => m.media)};
        }''')
        manifest = head['manifest']
        assert (manifest['id'], manifest['start_url'], manifest['scope'], manifest['display']) == ('/', '/', '/', 'standalone')
        assert manifest['short_name'] == s.BRANDING_DEFAULTS['short_name']
        assert sorted(head['themes']) == ['(prefers-color-scheme: dark)', '(prefers-color-scheme: light)']
        # D36: PNG icons generated from the branding emblem, 192 and 512 for any purpose and maskable, and the 180 Apple icon.
        assert {(i['sizes'], i['purpose']) for i in head['icons']} == {(f'{n}x{n}', p) for n in (192, 512) for p in ('any', 'maskable')}
        for url, size in [(i['src'], int(i['sizes'].split('x')[0])) for i in head['icons']] + [(head['apple'], 180)]:
            png = page.request.get(url)
            assert png.ok and png.headers['content-type'] == 'image/png', url
            body = png.body()
            assert body[:8] == b'\x89PNG\r\n\x1a\n' and int.from_bytes(body[16:20], 'big') == int.from_bytes(body[20:24], 'big') == size
            assert body[25] == 2, 'opaque RGB: launchers draw no see-through corners'
    finally:
        context.close()


def test_worker_registers_under_csp_and_shows_a_visible_alert_for_every_push(browser, live_server):
    context, page = sign_in(browser, live_server, 'staff', service_workers='allow')
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        context.grant_permissions(['notifications'], origin=live_server)
        assert "script-src 'self'" in page.request.get(live_server + '/').headers['content-security-policy']
        page.evaluate('() => navigator.serviceWorker.ready')
        cdp = context.new_cdp_session(page)
        registrations = []
        cdp.on('ServiceWorker.workerRegistrationUpdated', lambda e: registrations.extend(e['registrations']))
        cdp.send('ServiceWorker.enable')  # reports the registration; the next round trip delivers the event
        page.wait_for_function('() => navigator.serviceWorker.controller !== null')
        registration = next(r for r in registrations if r['scopeURL'] == live_server + '/' and not r['isDeleted'])
        # The app is open and visible: the alert still shows (iOS revokes permission after a silent push). An
        # unreadable payload still shows one, with the app's name.
        for data in (json.dumps({'v': 1, 'title': 'Duty changed', 'body': 'Open Rota to see it', 'url': '/#inbox', 'unread': 3}), 'not json'):
            cdp.send('ServiceWorker.deliverPushMessage', {'origin': live_server, 'registrationId': registration['registrationId'], 'data': data})
        shown = _notifications(page, 2)
        assert {(n['title'], n['body']) for n in shown} == {('Duty changed', 'Open Rota to see it'), (s.BRANDING_DEFAULTS['short_name'], 'You have a rota update')}
        assert {n['url'] for n in shown} == {live_server + '/#inbox'}
        # Signing in, its CSRF cookie and the start-up screen are untouched by the worker: the app still works.
        navigate(page, 'requests')
        assert not errors, errors
    finally:
        context.close()


def _offer_install(page):
    page.evaluate('''() => { const e = new Event('beforeinstallprompt'); window.__prompted = 0;
        e.prompt = async () => { window.__prompted++; }; e.userChoice = Promise.resolve({outcome: 'dismissed'});
        window.dispatchEvent(e); }''')


def test_install_is_offered_once_on_phone_home_and_stays_in_the_account_menu(browser, live_server):
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE, phone_notice=True)
    try:
        offer = page.locator('.app-offer[data-offer=install]')
        expect(offer).to_have_count(0)  # until the browser says it can install
        _offer_install(page)
        expect(offer.get_by_role('button', name='Install', exact=True)).to_be_visible()
        offer.get_by_role('button', name='Install', exact=True).click()
        page.wait_for_function('() => window.__prompted === 1')  # the browser's own install dialog
        _offer_install(page)
        offer.get_by_role('button', name='Not now', exact=True).click()
        expect(offer).to_have_count(0)
        page.reload()
        expect_loaded(page, 'home')
        _offer_install(page)
        page.get_by_role('button', name=re.compile('^Account menu, ')).click()
        expect(page.get_by_role('menuitem', name='Install app', exact=True)).to_be_visible()  # D31: still reachable
        expect(offer).to_have_count(0)  # D31: no repeat after Not now
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff')
    try:
        _offer_install(page)
        page.get_by_role('button', name=re.compile('^Account menu, ')).click()
        expect(page.get_by_role('menuitem', name='Install app', exact=True)).to_be_visible()
        expect(page.locator('.app-offer')).to_have_count(0)  # a computer's Home has no row
    finally:
        context.close()


def test_an_iphone_tab_gets_the_add_to_home_screen_guide_not_alerts(browser, push, live_server):
    policy(push)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE, phone_notice=True, user_agent=IPHONE_SAFARI, has_touch=True, is_mobile=True)
    try:
        offer = page.locator('.app-offer[data-offer=install]')
        offer.get_by_role('button', name='How to add', exact=True).click()
        guide = page.get_by_role('dialog', name=f"Add {s.BRANDING_DEFAULTS['short_name']} to your Home Screen")
        expect(guide.locator('.install-steps li')).to_have_count(4)
        guide.get_by_role('button', name='Using Chrome or Edge?').click()
        expect(guide.locator('.install-steps li')).to_have_count(3)
        guide.get_by_role('button', name='Done', exact=True).click()
        offer.get_by_role('button', name='Not now', exact=True).click()
        # The server is ready, but a browser tab on iPhone cannot receive alerts: the guide instead of Turn on.
        expect(page.locator('.app-offer[data-offer=alerts]')).to_have_count(0)
        _account(page, 'Notifications')
        device = _device(page)
        expect(device.get_by_text('Add to Home Screen to get alerts')).to_be_visible()
        expect(device.get_by_role('button', name='Turn on', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_alerts_are_turned_on_only_by_the_persons_tap_and_can_be_tested_and_turned_off(browser, push, live_server):
    policy(push)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE, phone_notice=True, service_workers='allow')
    try:
        sent = commands(context)
        context.add_init_script(MOCK_PUSH)
        page.reload()
        expect_loaded(page, 'home')
        row = page.locator('.app-offer[data-offer=alerts]')
        expect(row.get_by_role('button', name='Turn on', exact=True)).to_be_visible()
        assert not pushed(page).get('asked'), 'no system question on page load'
        row.get_by_role('button', name='Turn on', exact=True).click()
        expect(page.get_by_text(re.compile('^Alerts are on for this '))).to_be_visible()
        state = pushed(page)
        assert state['asked'] == 1 and state['key'] == push.public_key  # subscribed to this server's sender key
        [subscribed] = sent('push.subscribe')
        assert subscribed['endpoint'] == 'https://fcm.googleapis.com/fcm/send/test-endpoint' and subscribed['key_id'] == 'test-active'
        assert subscribed['explicit_consent'] is True and subscribed['expiration_time'] is None
        expect(row).to_have_count(0)
        page.reload()
        expect_loaded(page, 'home')
        expect(page.locator('.app-offer')).to_have_count(0)
        _account(page, 'Notifications')
        device = _device(page)
        expect(device.get_by_text(re.compile('(^A|a)lerts on$'))).to_be_visible()
        device.get_by_role('button', name='Send a test', exact=True).click()
        expect(page.get_by_text('Test sent. It should show on this device shortly.')).to_be_visible()
        [tested] = sent('push.test')
        device.get_by_role('button', name='Turn off', exact=True).click()
        expect(device.get_by_role('button', name='Turn on', exact=True)).to_be_visible()
        assert sent('push.unsubscribe')[0]['installation_id'] == tested['installation_id'] and pushed(page)['endpoint'] is None
        assert push.get('staff', '/api/me/push')['installations'][0]['state'] == 'revoked'
    finally:
        context.close()


def test_blocked_alerts_show_the_settings_steps(browser, push, live_server):
    policy(push)
    context, page = sign_in(browser, live_server, 'staff', service_workers='allow')
    try:
        context.add_init_script("localStorage.setItem('test.push', JSON.stringify({permission: 'denied'}));" + MOCK_PUSH)
        page.reload()
        expect_loaded(page, 'home')
        _account(page, 'Notifications')
        device = _device(page)
        expect(device.get_by_text('Off', exact=True)).to_be_visible()
        expect(device.locator('.np-steps li')).to_have_count(3)
        expect(device.get_by_role('button', name='Turn on', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_my_notifications_save_and_survive_reload_and_two_cannot_be_turned_off(browser, h, live_server):
    policy(h)
    department_email(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        sent = commands(context)
        _account(page, 'Notifications')
        panel = _device(page)
        # D28: Can't make it stays an alert (email may be added); the server lists only this person's events.
        required = panel.get_by_role('combobox', name="Can’t make it reported", exact=True)
        expect(required.locator('option')).to_have_text(['Alert and email', 'Alert'])
        expect(panel.get_by_role('heading', name='Needs my decision', exact=True)).to_be_visible()
        # D35: department email is set up, so each event offers email.
        published = panel.get_by_role('combobox', name='Rota published', exact=True)
        expect(published.locator('option')).to_have_text(['Alert and email', 'Alert', 'Email', 'Inbox only'])
        published.select_option('email')
        expect(page.get_by_text('Rota published: Email.')).to_be_visible()
        [saved] = sent('notification.preferences.update')
        assert saved['event_overrides']['publication'] == {'push': 'off', 'email': 'on'} and saved['expected_revision'] == 1
        assert saved['detail'] is None, 'an event change leaves the lock-screen level as it was (inherited)'
        # The lock screen: the department allows up to Event and date; a person may choose less.
        panel.get_by_role('radio', name='Generic', exact=True).click()
        expect(page.get_by_text('On the lock screen: Generic.')).to_be_visible()
        assert sent('notification.preferences.update')[-1]['detail'] == 'generic'
        page.reload()
        expect_loaded(page, 'home')
        _account(page, 'Notifications')
        panel = _device(page)
        expect(panel.get_by_role('combobox', name='Rota published', exact=True)).to_have_value('email')
        expect(panel.get_by_role('radio', name='Generic', exact=True)).to_have_attribute('aria-checked', 'true')
        # Undo puts the previous version back (a new version on the server).
        panel.get_by_role('combobox', name='Rota published', exact=True).select_option('alert')
        toast = page.locator('#toast-tray .toast', has_text='Rota published: Alert.')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone.', exact=True)).to_be_visible()
        expect(panel.get_by_role('combobox', name='Rota published', exact=True)).to_have_value('email')
    finally:
        context.close()
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).email_settings = {}
    context, page = sign_in(browser, live_server, 'staff')
    try:
        _account(page, 'Notifications')
        panel = _device(page)
        expect(panel.locator('li', has_text='A duty of mine changed').get_by_text('Alert', exact=True)).to_be_visible()
        expect(panel.locator('li', has_text='A duty of mine changed').locator('select')).to_have_count(0)
        expect(panel.get_by_role('heading', name='Needs my decision')).to_have_count(0)
        # D35: no department email, no email choice.
        expect(panel.get_by_role('combobox', name='Rota published', exact=True).locator('option')).to_have_text(['Alert', 'Inbox only'])
    finally:
        context.close()


def test_admin_sets_the_department_policy_with_a_reason_history_and_undo(browser, h, live_server):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        sent = commands(context)
        open_config(page, 'Notifications')
        pane = page.locator('.config-pane')
        switch = pane.get_by_role('switch', name='Alerts on phones and computers')
        expect(switch).not_to_be_checked()  # off until an admin turns it on
        expect(pane.get_by_text('Not ready · the server has no sender key yet')).to_be_visible()
        expect(pane.get_by_role('columnheader', name='Email')).to_have_count(0)  # D35: no department email
        expect(pane.get_by_role('cell', name='People with the Coordinator role', exact=True)).to_be_visible()  # who hears it, from the server
        required = pane.get_by_role('combobox', name='Alert for A duty of mine changed', exact=True)
        expect(required.locator('option')).to_have_text(['On', 'Urgent'])  # D28: never Off
        switch.check()
        pane.get_by_role('combobox', name='Alert for Swap or give-away', exact=True).select_option('urgent')
        pane.get_by_role('combobox', name='Alert for Rota review', exact=True).select_option('off')
        pane.locator('.lvl-card', has_text='Event and date').click()
        pane.get_by_role('button', name='Save…').click()
        dialog = page.get_by_role('dialog', name='Save notifications')
        for change in ('Alerts on phones and computers: Off → On', 'Swap or give-away · Alert: On → Urgent',
                       'Rota review · Alert: On → Off', 'On the lock screen: Generic → Event and date'):
            expect(dialog.get_by_text(change)).to_be_visible()
        dialog.get_by_label('Reason').fill('Pilot alerts for the department')
        dialog.get_by_role('button', name='Save').click()
        expect(page.get_by_text('Notifications saved.')).to_be_visible()
        [saved] = sent('notification.policy.update')
        assert saved['push_enabled'] is True and saved['lock_screen_detail'] == 'event_date' and saved['expected_revision'] == 1
        assert (saved['event_defaults']['swap']['push'], saved['event_defaults']['swap']['urgency']) == ('on', 'high')
        assert saved['event_defaults']['review']['push'] == 'off' and saved['event_defaults']['duty_changed']['push'] == 'on'
        assert {'retry', 'delivery'} <= set(saved) and saved['reason'] == 'Pilot alerts for the department'
        current = h.get('admin', '/api/me/push')
        assert current['enabled'] and current['policy']['events']['swap']['push'] == 'on' and current['policy']['lock_screen_detail'] == 'event_date'
        # A later change made elsewhere; after reload, Undo last change brings the saved version back.
        policy(h, revision=current['policy_revision'], lock_screen_detail='generic', reason='Back to generic')
        page.reload()
        expect_loaded(page, 'configuration')
        expect(pane.locator('.lvl-card.on')).to_contain_text('Generic')
        pane.get_by_role('button', name='History', exact=True).click()
        history = page.get_by_role('dialog', name='Notifications history')
        expect(history.get_by_role('cell', name='Pilot alerts for the department', exact=True)).to_be_visible()
        history.get_by_role('button', name='Close dialog').click()
        pane.get_by_role('button', name='Undo last change', exact=True).click()
        dialog = page.get_by_role('dialog', name='Undo the last notifications change')
        dialog.get_by_label('Reason').fill('Generic was a mistake')
        dialog.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Last notifications change undone.')).to_be_visible()
        expect(pane.locator('.lvl-card.on')).to_contain_text('Event and date')
        expect(pane.get_by_role('combobox', name='Alert for Swap or give-away', exact=True)).to_have_value('urgent')
    finally:
        context.close()
    department_email(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Notifications')
        expect(page.locator('.config-pane').get_by_role('columnheader', name='Email')).to_be_visible()  # D35
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'configuration')
        expect(page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name='Notifications', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_cant_make_it_offers_alerts_for_when_it_is_covered(browser, push, live_server):
    """P3 after-cant-make-it: asked again only in context (even after Not now on Home); the report keeps its own Undo."""
    from .test_browser_fe_c4 import my_duties
    my_duties(push)
    policy(push)
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE, phone_notice=True, service_workers='allow')
    try:
        sent = commands(context)
        context.add_init_script(MOCK_PUSH)
        page.reload()
        expect_loaded(page, 'home')
        page.locator('.app-offer[data-offer=alerts]').get_by_role('button', name='Not now', exact=True).click()
        page.locator('#main .home-hero').get_by_role('button', name=ABSENCE_REPORT_LABEL, exact=True).click()
        page.get_by_role('dialog', name=ABSENCE_REPORT_LABEL + '?').get_by_role('button', name='Tell the coordinator').click()
        offer = page.locator('#toast-tray .toast', has_text='Alert when it’s covered?')
        expect(offer.get_by_role('button', name='Turn on', exact=True)).to_be_visible()
        expect(page.locator('#toast-tray .toast').get_by_role('button', name='Undo', exact=True)).to_be_visible()
        assert not pushed(page).get('asked'), 'the question waits for the tap'
        offer.get_by_role('button', name='Turn on', exact=True).click()
        expect(page.get_by_text(re.compile('^Alerts are on for this '))).to_be_visible()
        assert pushed(page)['asked'] == 1 and len(sent('push.subscribe')) == 1
    finally:
        context.close()


def test_admin_chooses_the_icon_background_and_a_name_change_keeps_it(browser, h, live_server):
    """P7: the icon background is a branding version with a reason; saving the name later never resets it."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        sent = commands(context)
        navigate(page, 'configuration/department')
        panel = page.locator('.brand-settings')
        group = panel.get_by_role('radiogroup', name='Icon background')
        expect(group.get_by_role('radio', name='White', exact=True)).to_have_attribute('aria-checked', 'true')
        group.get_by_role('radio', name='Dark', exact=True).click()
        dialog = page.get_by_role('dialog', name='Icon background: Dark')
        dialog.get_by_label('Reason').fill('A dark icon for the pilot')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('Name and logo saved.')).to_be_visible()
        expect(panel.get_by_role('radiogroup', name='Icon background').get_by_role('radio', name='Dark', exact=True)).to_have_attribute('aria-checked', 'true')
        [chosen] = sent('branding.update')
        assert chosen['icon_background'] == '#16191b' and chosen['app_name'] == s.BRANDING_DEFAULTS['app_name']
        panel.get_by_role('button', name='Edit', exact=True).click()
        edit = page.get_by_role('dialog', name='Name and logo', exact=True)
        edit.get_by_label('Short name', exact=True).fill('Rota pilot')
        edit.get_by_label('Reason for the change', exact=True).fill('Pilot name')
        edit.get_by_role('button', name='Save', exact=True).click()
        expect(edit).to_have_count(0)
        settings = h.get('admin', '/api/admin/branding')['settings']
        assert (settings['short_name'], settings['icon_background']) == ('Rota pilot', '#16191b')
    finally:
        context.close()
