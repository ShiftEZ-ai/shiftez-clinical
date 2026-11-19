"""Cycle-9 frontend fixes for the everyday staff journeys (SWEEP-3, SW3-C and related findings)."""
import re
import time
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, route_fetch, expect_loaded, wait_held, reconnect  # noqa: F401
from .test_browser_fe_c4 import my_duties, PHONE, DESKTOP
from .test_browser_pwa import push  # noqa: F401

pytestmark = pytest.mark.browser


def test_offer_swap_opens_one_new_request_sheet_when_the_page_redraws_during_its_load(browser, live_server, h):
    """SW3-C-04: a refresh while Requests was still loading drew the page a second time from the same
    `?new=1&type=swap` link, so two New request sheets were stacked."""
    me, days, ids = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    held = []
    try:
        page.route(re.compile(r'/api/requests/all\?'), lambda route: held.append(route))
        page.evaluate("key => { location.hash = '#' + key; }", f'requests?new=1&type=swap&duty={ids[days[1].isoformat()]}')
        wait_held(page, held)
        first = len(held)
        reconnect(page)  # a refresh redraws the page under its first load
        deadline = time.monotonic() + 30
        while len(held) <= first and time.monotonic() < deadline:  # the redraw's own list read
            page.evaluate('0')
        assert len(held) > first, 'the refresh never redrew Requests'
        for route in held:  # released once each, before unroute (unroute continues whatever it still holds)
            route.continue_()
        page.unroute(re.compile(r'/api/requests/all\?'))
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_label('Your duty to swap')).to_have_value(ids[days[1].isoformat()])
        expect(page.locator('dialog[open]')).to_have_count(1)
        assert 'new=1' not in page.evaluate('location.hash')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_swap_check_says_checking_failed_with_retry_and_nobody_apart(browser, live_server, h):
    """SW3-C-06: "Nobody eligible" showed while the per-duty check was still on its way and again when it failed."""
    me, days, ids = my_duties(h)
    held, mode = [], {'now': 'hold'}

    def check(route):
        if 'assignment_id=' not in route.request.url:
            return route.fallback()
        if mode['now'] == 'hold':
            return held.append(route)
        if mode['now'] == 'fail':
            return route.fulfill(status=502, content_type='text/html', body='<html>Bad Gateway</html>')
        response = route_fetch(route)
        body = response.json()
        body['recipients'] = [{**r, 'eligible': False, 'message': 'Not eligible for this clinic'} for r in body['recipients']]
        route.fulfill(response=response, json=body)
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.route('**/api/swaps/options?*', check)
        navigate(page, f'requests?new=1&type=swap&duty={ids[days[1].isoformat()]}')
        dialog = page.get_by_role('dialog', name='New request')
        send = dialog.get_by_role('button', name='Send offer')
        expect(dialog.get_by_text('Checking who can take this duty…')).to_be_visible()
        expect(dialog.get_by_text('Nobody eligible', exact=False)).to_have_count(0)
        expect(send).to_be_disabled()
        wait_held(page, held)
        mode['now'] = 'fail'
        for route in held:
            route.fulfill(status=502, content_type='text/html', body='<html>Bad Gateway</html>')
        expect(dialog.get_by_text('Couldn’t check who can take this duty.', exact=False)).to_be_visible()
        expect(dialog.get_by_text('Nobody eligible', exact=False)).to_have_count(0)
        mode['now'] = 'nobody'
        dialog.locator('#swap-check').get_by_role('button', name='Retry').click()
        expect(dialog.get_by_text('Nobody eligible can take this duty.', exact=False)).to_be_visible()
        expect(dialog.get_by_text('Couldn’t check', exact=False)).to_have_count(0)
        expect(send).to_be_disabled()
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_swap_sheet_that_could_not_load_offers_retry_in_place(browser, live_server, h):
    """SW3-C-20: the sheet's load failure had no Retry ("Reload" loses the form)."""
    me, days, ids = my_duties(h)
    state = {'fail': True}

    def options(route):
        if state['fail']:
            return route.fulfill(status=502, content_type='text/html', body='<html>Bad Gateway</html>')
        route.fallback()
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.route('**/api/swaps/options', options)
        navigate(page, f'requests?new=1&type=swap&duty={ids[days[1].isoformat()]}')
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_text('Couldn’t load the swap options.', exact=False)).to_be_visible()  # C3-F5: names what
        state['fail'] = False
        dialog.get_by_role('button', name='Retry').click()
        expect(dialog.get_by_label('Your duty to swap')).to_have_value(ids[days[1].isoformat()])
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_wish_on_a_settled_rotas_days_is_refused_before_it_is_sent_naming_the_rota(browser, live_server, h):
    """SW3-C-05: the form gave no hint until Add wish, then dropped the rota's name; the points line vanished for a
    month outside the first read (±40 days around today)."""
    from .test_browser_home import local_today
    start = local_today() + timedelta(days=200)
    day = lambda n: (start + timedelta(days=n)).isoformat()  # noqa: E731
    w = h.workspace('FE-C9 settled rota', day(0), day(20))
    h.lifecycle(h.shift(w, start=day(2) + 'T08:00:00+03:00', end=day(2) + 'T12:00:00+03:00')['workspace'], 'submit')
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        sent = []
        page.route('**/api/commands', lambda route: (sent.append(route.request.post_data), route.fallback()))
        navigate(page, 'requests?new=1&type=wish')
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_label('From', exact=True).fill(day(5))
        dialog.get_by_label('To (included)', exact=True).fill(day(5))
        expect(dialog.get_by_text('The rota for these days (FE-C9 settled rota) is already settled', exact=False)).to_be_visible()
        expect(dialog.get_by_role('button', name='Add wish')).to_be_disabled()
        expect(dialog.get_by_text('points used in', exact=False)).to_be_visible()
        # A day after the rota is open again.
        dialog.get_by_label('From', exact=True).fill(day(25))
        dialog.get_by_label('To (included)', exact=True).fill(day(25))
        expect(dialog.get_by_text('is already settled', exact=False)).to_have_count(0)
        expect(dialog.get_by_role('button', name='Add wish')).to_be_enabled()
        assert not any('wish.create' in (body or '') for body in sent)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_list_and_its_csv_say_an_absent_doctor_is_absent_not_covering(browser, live_server, h):
    """SW3-C-02: the List (which Print or export › CSV is drawn from) said "Covered" with the absent doctor as its
    person, while the grid struck them through as absent."""
    w = h.shift(h.workspace('FE-C9 absent export'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    h.publish(w)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave', 'start': '2026-10-05T00:00:00+03:00',
                                                     'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Approved leave'})
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        navigate(page, 'schedule/list/2026-10-05')
        row = page.locator('#main tbody tr[data-session]').first
        expect(row).to_contain_text('Absent · needs cover')
        expect(row).to_contain_text('(absent)')
        expect(row).not_to_contain_text('Covered')
        csv = page.evaluate("async () => (await import(new URL('schedule_export.js', document.querySelector('script[type=module][src$=\"/app.js\"]').src).href)).csvOf(document.querySelector('#main'))")
        line = next(x for x in csv.split('\r\n') if '(absent)' in x)
        assert 'Absent · needs cover' in line and 'Covered' not in line
    finally:
        context.close()


def test_a_computer_is_not_told_to_allow_browser_alerts_the_department_has_off(browser, push, live_server):
    """SX-09 / SW3-C-08: Chrome with notifications blocked was given Site settings steps although the department has
    push off, so no alert could ever arrive."""
    from .test_browser_pwa import _account, _device
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        context.add_init_script("Object.defineProperty(Notification, 'permission', {get: () => 'denied'})")
        page.reload()
        expect_loaded(page, 'home')
        _account(page, 'Notifications')
        device = _device(page)
        expect(device.get_by_text('Your department hasn’t turned alerts on · updates appear in your Inbox')).to_be_visible()
        expect(device.get_by_text('Site settings')).to_have_count(0)
        expect(device.get_by_text('Turned off in', exact=False)).to_have_count(0)
    finally:
        context.close()


def test_the_browser_tab_icon_is_the_branded_emblem_the_installed_app_uses(browser, live_server):
    """SW3-C-11: the favicon was a generic "+" while the installed app's icons are the department emblem. The tab icon is
    the emblem file itself (static, no server read per page load); the installed icons are rendered from that same file
    (services/pwa.py icon_inputs), so they are resized copies, never byte-equal to it."""
    from pathlib import Path
    emblem = (Path(__file__).resolve().parents[1] / 'apps/web/public/assets/brand/emblem.png').read_bytes()
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        expect(page.locator('img.brand-emblem').first).to_be_visible()
        href = page.evaluate("document.querySelector('link[rel=icon]').href")
        assert href == page.evaluate("document.querySelector('img.brand-emblem').src")  # brand.ts keeps them in step
        apple = page.evaluate("document.querySelector('link[rel=apple-touch-icon]').href")
        tab, installed = context.request.get(href), context.request.get(apple)
        assert tab.ok and tab.headers['content-type'] == 'image/png'
        assert tab.body() == emblem  # the installed icons' source (no custom emblem in the seed)
        assert installed.ok and installed.headers['content-type'] == 'image/png'
    finally:
        context.close()


def test_home_waits_for_account_then_parallel_reads_and_never_reads_swap_options(browser, live_server, h):
    """The account gates leave-types access; permitted leave and baseline reads remain parallel. Home never reads
    slow swap options only to decide whether Offer swap shows (the sheet says when nobody can receive it)."""
    me, days, ids = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        seen = []
        page.on('request', lambda r: seen.append(r.url.split('/api/')[-1].split('?')[0]) if '/api/' in r.url else None)
        page.goto('about:blank')  # the signed-in page's own polling (pulse, events) is not this boot's
        seen.clear()
        held, leave, baseline = [], [], []
        page.route('**/api/me', lambda route: held.append(route))
        page.route('**/api/leave-types', lambda route: leave.append(route))
        page.route('**/api/pulse', lambda route: baseline.append(route))
        page.goto(live_server + '/')
        wait_held(page, held)
        assert not {'pulse', 'leave-types'} & set(seen), seen
        for route in held:
            route.continue_()
        page.unroute('**/api/me')
        # Both requests start while the other's reply is held: neither serialises the other.
        wait_held(page, leave)
        wait_held(page, baseline)
        assert 'home' not in seen, seen
        for route in leave + baseline:
            route.continue_()
        page.unroute('**/api/leave-types')
        page.unroute('**/api/pulse')
        expect_loaded(page, 'home')
        expect(page.locator('#main .home-hero').get_by_role('button', name='Offer swap', exact=True)).to_be_visible()
        expect(page.get_by_role('region', name='Coming up').locator('.home-duty-list li')).to_have_count(2)
        assert 'swaps/options' not in seen
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_rank_that_has_not_started_is_withdrawn_and_its_end_defaults_to_its_start(browser, live_server, h):
    """An upcoming rank's End form starts at its first day; Withdraw is a reasoned, one-press, undoable action."""
    from .test_browser_person_profile import open_person, section, person
    membership = h.command('coordinator', 'membership.create', {
        'person_id': h.seed['people'][0], 'category_id': h.seed['trainee_category'], 'starts_on': '2028-07-01'})['record']
    rank_name = next(r['name'] for r in h.get('coordinator', '/api/catalogues')['categories']
                     if r['id'] == membership['category_id'])
    reason = 'This future rank was assigned to the wrong doctor'
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_person(page, person(h))
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        row = section(panel, 'Rank').locator('li').filter(has_text=rank_name)
        expect(row).to_contain_text('Upcoming')
        row.get_by_role('button', name=f'End {rank_name}', exact=True).click()
        dialog = page.get_by_role('dialog', name=f'End {rank_name}', exact=True)
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Sat 1 Jul 2028')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        row.get_by_role('button', name=f'More for {rank_name}', exact=True).click()
        page.get_by_role('menuitem', name='Withdraw', exact=True).click()
        dialog = page.get_by_role('dialog', name=f'Withdraw {rank_name}?', exact=True)
        expect(dialog).to_contain_text("It won't apply. Published rotas keep it.")
        explanation = dialog.get_by_label('Reason', exact=True)
        expect(explanation).to_have_attribute('required', '')
        expect(explanation).to_have_attribute('minlength', '3')
        explanation.fill('No')
        assert explanation.evaluate('field => field.validity.tooShort')
        submit = dialog.locator('button[type="submit"]')
        submit.click()
        expect(explanation).to_be_focused()
        expect(dialog).to_be_visible()
        explanation.fill(reason)
        # D1: withdraw of an upcoming record is one press + Undo (Design Lead review on #768)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'membership.withdraw') as withdrawn:
            submit.click()
        assert withdrawn.value.status == 200
        command = withdrawn.value.request.post_data_json
        assert command['target_id'] == membership['id'] and command['payload'] == {'reason': reason}
        inverse = withdrawn.value.json()['undo']
        assert inverse['kind'] == 'membership.restore' and inverse['target_id'] == membership['id']
        expect(dialog).to_have_count(0)
        notice = page.locator('#toast-tray .toast').filter(has=page.get_by_text(f'{rank_name} withdrawn.', exact=True))
        expect(notice).to_be_visible()
        expect(row).to_have_count(0)
        assert not any(r['id'] == membership['id'] for r in h.get('coordinator', '/api/catalogues')['memberships'])
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'membership.restore') as restored:
            notice.get_by_role('button', name='Undo', exact=True).click()
        assert restored.value.status == 200
        assert restored.value.request.post_data_json == inverse
        expect(row).to_contain_text('Upcoming')
        expect(row.get_by_role('button', name=f'More for {rank_name}', exact=True)).to_be_visible()
        saved = next(r for r in h.get('coordinator', '/api/catalogues')['memberships'] if r['id'] == membership['id'])
        assert (saved['person_id'], saved['category_id'], saved['starts_on'], saved['ends_on']) == (
            membership['person_id'], membership['category_id'], membership['starts_on'], membership['ends_on'])
    finally:
        context.close()


def test_a_programme_membership_that_has_not_started_is_withdrawn_and_undo_restores_it(browser, live_server, h):
    """A future programme enrolment uses the More menu, a reasoned one-press action, and same-record Undo."""
    from .test_browser_person_profile import open_person, section, person
    programme = h.command('coordinator', 'catalogue.create', {
        'catalogue': 'programmes', 'value': {'name': 'Deferred training programme', 'starts_on': '2020-01-01'},
        'reason': 'Create a programme for the withdrawal journey'})['record']
    membership = h.command('coordinator', 'catalogue.create', {
        'catalogue': 'programme_memberships', 'value': {
            'person_id': h.seed['people'][0], 'programme_id': programme['id'],
            'starts_on': '2028-07-01', 'ends_on': '2029-01-01'},
        'reason': 'Arrange a programme membership that starts in the future'})['record']
    reason = 'The doctor will not join this programme'
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_person(page, person(h))
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        row = section(panel, 'Programmes').locator('li').filter(has_text=programme['name'])
        expect(row).to_have_count(1)
        expect(row).to_contain_text('Upcoming')
        expect(row.get_by_role('button', name=re.compile(r'^(End|Extend) '))).to_have_count(0)
        row.get_by_role('button', name=f'More for {programme["name"]}', exact=True).click()
        page.get_by_role('menuitem', name='Withdraw', exact=True).click()
        dialog = page.get_by_role('dialog', name=f'Withdraw {programme["name"]}?', exact=True)
        expect(dialog).to_contain_text("It won't apply. Published rotas keep it.")
        explanation = dialog.get_by_label('Reason', exact=True)
        expect(explanation).to_have_attribute('required', '')
        expect(explanation).to_have_attribute('minlength', '3')
        explanation.fill('No')
        assert explanation.evaluate('field => field.validity.tooShort')
        submit = dialog.locator('button[type="submit"]')
        submit.click()
        expect(explanation).to_be_focused()
        expect(dialog).to_be_visible()
        explanation.fill(reason)
        # D1: withdraw of an upcoming record is one press + Undo (Design Lead review on #768)
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'programme_membership.withdraw') as withdrawn:
            submit.click()
        assert withdrawn.value.status == 200
        command = withdrawn.value.request.post_data_json
        assert command['target_id'] == membership['id'] and command['payload'] == {'reason': reason}
        inverse = withdrawn.value.json()['undo']
        assert inverse['kind'] == 'programme_membership.restore' and inverse['target_id'] == membership['id']
        expect(dialog).to_have_count(0)
        notice = page.locator('#toast-tray .toast').filter(has=page.get_by_text(f'{programme["name"]} withdrawn.', exact=True))
        expect(notice).to_be_visible()
        expect(row).to_have_count(0)
        assert not any(r['id'] == membership['id'] for r in h.get('coordinator', '/api/catalogues')['programme_memberships'])
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and r.request.post_data_json.get('kind') == 'programme_membership.restore') as restored:
            notice.get_by_role('button', name='Undo', exact=True).click()
        assert restored.value.status == 200
        assert restored.value.request.post_data_json == inverse
        expect(row).to_have_count(1)
        expect(row).to_contain_text('Upcoming')
        expect(row.get_by_role('button', name=f'More for {programme["name"]}', exact=True)).to_be_visible()
        saved = next(r for r in h.get('coordinator', '/api/catalogues')['programme_memberships'] if r['id'] == membership['id'])
        assert (saved['person_id'], saved['programme_id'], saved['starts_on'], saved['ends_on']) == (
            membership['person_id'], membership['programme_id'], membership['starts_on'], membership['ends_on'])
    finally:
        context.close()


@pytest.mark.parametrize('lead_scope', ['owner', 'current_member'])
def test_programme_withdraw_is_scoped_to_the_leads_programmes_and_never_their_own_membership(browser, live_server, h, lead_scope):
    """An approver can read every programme without gaining a lead's withdrawal authority over them."""
    from .test_browser_person_profile import open_person, section, person
    lead, colleague = person(h), person(h, 1)
    for role in ('programme_lead', 'approver'):
        h.command('admin', 'role.grant', {
            'identity_id': h.seed['identities']['staff'], 'role': role, 'starts_on': '2020-01-01',
            'reason': 'Give the fictional lead department-wide reading access'})
    led = h.command('staff' if lead_scope == 'owner' else 'coordinator', 'catalogue.create', {
        'catalogue': 'programmes', 'value': {'name': 'Led withdrawal programme', 'starts_on': '2020-01-01'},
        'reason': 'Create the programme within this lead\'s authority'})['record']
    unrelated = h.command('coordinator', 'catalogue.create', {
        'catalogue': 'programmes', 'value': {'name': 'Unrelated withdrawal programme', 'starts_on': '2020-01-01'},
        'reason': 'Create a programme outside this lead\'s authority'})['record']
    if lead_scope == 'current_member':
        h.command('coordinator', 'catalogue.create', {
            'catalogue': 'programme_memberships', 'value': {
                'person_id': lead['id'], 'programme_id': led['id'],
                'starts_on': '2026-01-01', 'ends_on': '2027-01-01'},
            'reason': 'Establish the lead\'s current programme membership'})
    for person_id, programme_id in ((colleague['id'], led['id']), (colleague['id'], unrelated['id']), (lead['id'], led['id'])):
        h.command('coordinator', 'catalogue.create', {
            'catalogue': 'programme_memberships', 'value': {
                'person_id': person_id, 'programme_id': programme_id,
                'starts_on': '2028-07-01', 'ends_on': '2029-01-01'},
            'reason': 'Add an upcoming membership for withdrawal permission coverage'})

    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        panel = open_person(page, colleague)
        expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
        programmes = section(panel, 'Programmes')
        led_row = programmes.locator('li').filter(has_text=led['name'])
        expect(led_row).to_contain_text('Upcoming')
        expect(led_row.get_by_role('button', name=f"More for {led['name']}", exact=True)).to_be_visible()
        other_row = programmes.locator('li').filter(has_text=unrelated['name'])
        expect(other_row).to_be_visible()
        expect(other_row).to_contain_text('Upcoming')
        expect(other_row.get_by_role('button', name=f"More for {unrelated['name']}", exact=True)).to_have_count(0)

        panel = open_person(page, lead)
        own_row = section(panel, 'Programmes').locator('li').filter(has_text=led['name']).filter(has_text='Upcoming')
        expect(own_row).to_be_visible()
        expect(own_row.get_by_role('button', name=f"More for {led['name']}", exact=True)).to_have_count(0)
    finally:
        context.close()

    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_person(page, colleague)
        for programme in (led, unrelated):
            row = section(panel, 'Programmes').locator('li').filter(has_text=programme['name'])
            row.get_by_role('button', name=f"More for {programme['name']}", exact=True).click()
            expect(page.get_by_role('menuitem', name='Withdraw', exact=True)).to_be_visible()
            page.keyboard.press('Escape')
        panel = open_person(page, lead)
        own_row = section(panel, 'Programmes').locator('li').filter(has_text=led['name']).filter(has_text='Upcoming')
        own_row.get_by_role('button', name=f"More for {led['name']}", exact=True).click()
        expect(page.get_by_role('menuitem', name='Withdraw', exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_returned_rota_has_its_row_on_the_coordinators_home(browser, live_server, h):
    """SW3-B-04: the coordinator learned of a returned rota only from the Inbox. maintenance's /api/home `returned` item
    (stubbed here) is drawn with who returned it and why, and Open lands on that rota."""
    from .test_browser_home_phone_waiting import json_route
    w = h.workspace('FE-C9 returned rota')

    def home(body):
        body['attention'] = [{'key': 'returned', 'count': 1, 'sample': [{'rota_id': w['id'], 'rota_name': w['name'], 'returned_at': '2026-11-04T23:00:00+00:00',
                                                                           'by': 'Fictional approver', 'reason': 'Ask Taylor first'}]}]
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        json_route(page, '**/api/home', home)
        page.reload()
        expect_loaded(page, 'home')
        row = page.locator('[data-need=returned]')
        expect(row).to_contain_text('Returned to you')
        expect(row).to_contain_text('FE-C9 returned rota · Fictional approver · “Ask Taylor first”')
        row.get_by_role('button', name='Open', exact=True).click()
        expect_loaded(page, 'schedule')
        expect(page).to_have_url(re.compile(r'edit=' + re.escape(w['id'])))
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_needs_you_leaves_out_a_doctors_own_uncoverable_duty_and_shows_far_dates_with_their_year(browser, live_server):
    """SW3-C-01: a doctor's own duty overlapping unavailability (the coordinator's to cover) was a Needs-you item, and
    "Tue 7 Sep" ten months away had no year. SW3-C-13: Show opened a pinned publication that a later amendment supersedes."""
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        rows = page.evaluate("""async()=>{const H=await window.importBundle('home.js');const went=[];
          const ctx={me:{organisation:{timezone:'Asia/Riyadh'},person_id:'me'},catalogues:{people:[{id:'p1',display_name:'Alice Underwood'}]},navigate(to){went.push(to);},setWorkspace(){}};
          const far=(new Date().getUTCFullYear()+1)+'-09-07T05:30:00Z';
          const x={person_id:'me',start:far,end:far,shift_id:'s',publication_id:'pub'};
          const own=H.needsOf(ctx,{},{attention:[{key:'published_absence_conflicts',count:1,sample:[x],action:null}],rotas:0,my_requests:[],recent_changes:[]});
          const team=H.needsOf(ctx,{},{attention:[{key:'published_absence_conflicts',count:1,sample:[{...x,person_id:'p1'}],action:null}],rotas:0,my_requests:[],recent_changes:[]});
          team[0].go();
          return {own:own.length,context:team[0].context,went};}""")
        assert rows['own'] == 0
        from packages.domain.time import now
        assert str(now().year + 1) in rows['context'] and 'Alice Underwood' in rows['context']
        assert rows['went'][0].startswith('schedule/week/') and 'source=' not in rows['went'][0]
    finally:
        context.close()


def test_inbox_times_follow_the_department_day_as_its_groups_do(browser, live_server):
    """SW3-C-09: an item from the department's yesterday said "20 h ago" and one from two days back said "Yesterday"."""
    import json
    tz = '+03:00'  # the test department's zone (Asia/Riyadh)
    items = [{'id': 'n1', 'title': 'From yesterday', 'message': '', 'kind': 'announcement', 'created_at': f'2026-11-04T06:40:00{tz}', 'read_at': None},
             {'id': 'n2', 'title': 'From two days ago', 'message': '', 'kind': 'announcement', 'created_at': f'2026-11-03T23:30:00{tz}', 'read_at': None}]
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        page.clock.set_fixed_time(f'2026-11-05T02:40:00{tz}')
        page.route(re.compile(r'/api/notifications(\?|$)'), lambda route: route.fulfill(json={'notifications': items, 'has_more': False}))
        page.reload()
        expect_loaded(page, 'home')
        navigate(page, 'inbox')
        first = page.locator('#main').get_by_text('From yesterday').locator('xpath=ancestor::*[contains(@class,"note-top")]')
        expect(first).to_contain_text('Yesterday')
        expect(first).not_to_contain_text('h ago')
        second = page.locator('#main').get_by_text('From two days ago').locator('xpath=ancestor::*[contains(@class,"note-top")]')
        expect(second).not_to_contain_text('Yesterday')
        expect(second).to_contain_text('3 Nov')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_the_bell_stays_open_while_several_items_are_marked_read(browser, live_server, h):
    """SW3-C-10: marking one item read closed the bell popover, so marking several meant reopening it each time."""
    from apps.api.db import transaction
    from .test_browser_operator_role_coverage import unread_item
    with transaction(h.app.state.engine, write=True) as db:
        for title in ('First FE-C9 item', 'Second FE-C9 item'):
            unread_item(db, h, h.seed['identities']['staff'], title)
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.get_by_role('button', name=re.compile('^Inbox')).first.click()
        pop = page.locator('.note-pop')
        expect(pop).to_contain_text('2 unread')
        pop.get_by_role('button', name='Mark as read: First FE-C9 item', exact=True).click()
        expect(pop).to_contain_text('1 unread')
        expect(pop.get_by_role('button', name='Mark as read: First FE-C9 item', exact=True)).to_have_count(0)
        pop.get_by_role('button', name='Mark as read: Second FE-C9 item', exact=True).click()
        expect(pop).to_contain_text('All caught up')
        expect(pop).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('size', [PHONE, DESKTOP], ids=['phone', 'desktop'])
def test_a_doctor_opening_a_planning_address_gets_restricted_not_best_on_a_desktop(browser, live_server, size):
    """SW3-C-12: staff opening #schedule/month/…?edit=any were told "Best on a desktop · Continue anyway"."""
    context, page = sign_in(browser, live_server, 'staff', size, phone_notice=size is PHONE)
    try:
        page.evaluate("location.hash = '#schedule/month/2026-11-01?edit=any'")
        expect(page.locator('#main[data-loaded-view=restricted]')).to_be_visible()
        expect(page.locator('#main')).to_contain_text('Restricted page')
        expect(page.locator('#main')).not_to_contain_text('Best on a desktop')
        page.evaluate("location.hash = '#schedule/week/2026-11-01'")
        expect(page.locator('#main[data-loaded-view=schedule]')).to_be_visible()  # the published schedule still opens
    finally:
        context.close()


def test_the_cant_be_reached_screen_recovers_by_itself_when_the_connection_returns(browser, live_server):
    """SW3-C-19: after a reload while offline, "Rota can't be reached" stayed until Retry although the connection was back."""
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.route('**/api/me', lambda route: route.abort('internetdisconnected'))
        page.reload()
        expect(page.locator('.boot-failed')).to_contain_text('can’t be reached')
        page.unroute('**/api/me')
        page.evaluate("window.dispatchEvent(new Event('online'))")
        expect_loaded(page, 'home')
        expect(page.locator('.boot-failed')).to_have_count(0)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


def test_a_refused_request_leaves_no_not_saved_once_its_sheet_is_discarded(browser, live_server):
    """SW3-C-05 (tail): after a refused wish and Discard, the footer kept "⚠ Not saved"."""
    def refuse(route):
        if '"wish.create"' not in (route.request.post_data or ''):
            return route.fallback()
        route.fulfill(status=409, json={'error': {'code': 'WISHES_OFF', 'message': 'Refused for the test'}})
    context, page = sign_in(browser, live_server, 'staff', PHONE, phone_notice=True)
    try:
        page.route('**/api/commands', refuse)
        navigate(page, 'requests?new=1&type=wish')
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_label('Note (optional, seen by planners)', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Add wish').click()
        expect(dialog.get_by_text('Refused for the test', exact=False)).to_be_visible()
        footer = page.locator('.app-footer')
        expect(footer).to_contain_text('Not saved')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.get_by_role('button', name='Discard changes').click()
        expect(page.get_by_role('dialog', name='New request')).to_have_count(0)
        expect(footer).not_to_contain_text('Not saved')
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('role,planner', [('staff', False), ('coordinator', True)])
def test_a_doctors_view_and_export_settings_offer_no_planning_choices(browser, live_server, role, planner):
    """SW3-C-18: a doctor's View settings (the same ones Print or export › Adjust what shows opens) offered Coverage,
    people/rank/programme rows and Open places / Rule marks: planner tools for someone who sees only their own duties."""
    from .test_browser import view_settings
    context, page = sign_in(browser, live_server, role, DESKTOP)
    try:
        navigate(page, 'schedule/week/2026-10-05')
        panel = view_settings(page)
        layout = panel.get_by_role('radiogroup', name='Layout', exact=True)
        expect(layout.get_by_role('radio', name='Calendar', exact=True)).to_be_visible()
        expect(layout.get_by_role('radio', name='Coverage', exact=True)).to_have_count(1 if planner else 0)
        rows = [o.strip() for o in panel.get_by_label('Rows', exact=True).locator('option').all_inner_texts()]
        assert ('People' in rows) is planner and ('Ranks' in rows) is planner and 'Services' in rows
        panel = view_settings(page, 'Properties')
        expect(panel).to_contain_text('Team lead (TL)')
        if planner:
            expect(panel).to_contain_text('Open places')
        else:
            expect(panel).not_to_contain_text('Open places')
            expect(panel).not_to_contain_text('Rule marks')
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
