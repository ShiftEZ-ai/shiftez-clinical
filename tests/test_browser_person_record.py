"""The complete person record (People › a person): Workload & fairness, Leave & requests, History with sign-ins, and
Access (sign-in account, sessions and devices, sign out) — what each role reads, desktop and phone, light and dark,
loading, failure and empty states, and the journey through every tab and back."""
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from packages.domain.time import now as clock_now
from .conftest import TEST_PASSWORD
from .governance_support import developer, identity
from .test_browser import browser, live_server, sign_in, navigate, wait_held, route_fetch, expect_loaded  # noqa: F401
from .test_browser_people_merged import row
from .test_browser_requests import day
from .test_browser_person_profile import L, PRIVATE, TABS, W, open_person, person, related, section
from .test_browser_audit_date_units import stored_theme
from .test_browser_kept_reads import live_pulse, rebuild
from .test_browser_w3_setup_today import no_page_overflow
from .test_browser_rule_catalogue import catalogue_rule, line
from .test_leave_disclosure import setting

pytestmark = pytest.mark.browser
GOAL = {'balance_goal': {'metric': 'duties', 'period': 'rota'}}


def tab(panel, name):
    panel.get_by_role('tab', name=name, exact=True).click()
    shown = panel.get_by_role('tabpanel', name=name, exact=True)
    expect(shown.locator('.loading')).to_have_count(0)
    return shown


def armed_press(button, confirm):
    """Same-button confirm: the first press arms it (red, asking `confirm`), the second acts."""
    button.click()
    expect(button).to_have_class(re.compile(r'\barmed\b'))
    expect(button).to_contain_text(confirm)
    button.click()


def another_session(browser, origin, username='staff'):
    """A second signed-in device for `username` (its own cookies; nothing opened)."""
    context = browser.new_context()
    assert context.request.post(origin + '/auth/login', data={'username': username, 'password': TEST_PASSWORD},
                                headers={'origin': origin}).ok
    return context


@pytest.mark.parametrize('role,width,theme', [('coordinator', 1440, 'light'), ('staff', 390, 'dark'),
                                              ('auditor', 1440, 'dark'), ('programme_lead', 390, 'light')])
def test_workload_cards_goals_and_score_follow_the_role(browser, live_server, h, role, width, theme):
    goal = catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})], settings=GOAL)
    related(h)
    if role == 'programme_lead':
        identity(h, role, [role])
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        panel = open_person(page, person(h))
        shown = tab(panel, W)
        # No published rota covers today (the Requests rota starts in 5 days): it opens on this month.
        period = shown.get_by_label('Period', exact=True)
        expect(period.locator('option:checked')).to_have_text('This month')
        period.select_option(label='Requests rota')
        expect(shown.locator('.loading')).to_have_count(0)
        # One period for every card: the switch names it and its dates follow.
        expect(period.locator('option:checked')).to_have_text('Requests rota')
        expect(shown.locator('.pr-period > small')).to_have_text(re.compile(r'\d{4} – .*\d{4}$'))
        expect(shown.locator('.ov-stat-label')).to_have_text(['Duties', 'Hours', 'Weekends', 'Fairness'])
        goals = section(shown, 'Balance goals and targets')
        expect(goals).to_contain_text(goal['name'])
        expect(goals.locator('.pr-aim')).to_have_count(1)  # the fair-share marker
        expect(shown.get_by_role('heading', name=re.compile('^Score in Requests rota'))).to_be_visible()
        # Staff and programme leads never read other people's standing.
        fairness = shown.locator('.ov-stat').nth(3)
        # Fairness as a short status, never a bare fraction or a sentence.
        expect(fairness.locator('strong')).to_have_text(re.compile(r'^(Within tolerance|On aim|(Above|Below) by [\d.]+)$'))
        if role in ('staff', 'programme_lead'):
            expect(fairness).not_to_contain_text(re.compile(r'rank \d+ of'))
        else:
            expect(fairness).to_contain_text(re.compile(r'rank \d+ of \d+'))
        no_page_overflow(page)
    finally:
        context.close()


def test_standing_is_said_once_from_the_server_fairness(browser, live_server, h):
    """The goal's row says the standing in full, the Duties note only the aim and the Fairness card only the status, each
    from the server's one fairness row (fairness.py): same actual, aim, band and status."""
    goal = catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})], settings=GOAL)
    related(h)
    p = person(h)
    served = next(g for g in h.get('coordinator', f"/api/people/{p['id']}/fairness")['goals'] if g['name'] == goal['name'])
    r = served['people'][0]
    n = lambda x: f'{round(x, 1):g}'  # noqa: E731 - the page's one-decimal number
    assert r['status'] == 'within' and r['high'] is not None, r
    aim, band = f"fair share {n(r['expected'])}", f"±{n(r['high'] - r['expected'])}"
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        shown = tab(open_person(page, p), W)
        shown.get_by_label('Period', exact=True).select_option(label='Requests rota')
        expect(shown.locator('.loading')).to_have_count(0)
        row = section(shown, 'Balance goals and targets').locator('.pr-line').filter(has_text=goal['name'])
        unit = 'duty' if r['actual'] == 1 else 'duties'
        expect(row.locator('.pr-said')).to_have_text(f"{n(r['actual'])} {unit} · {aim} · within {band}")
        expect(shown.locator('.ov-stat').nth(0).locator('.pr-note')).to_have_text(aim)
        fairness = shown.locator('.ov-stat').nth(3)
        expect(fairness.locator('strong')).to_have_text('Within tolerance')
        expect(fairness.locator('.pr-note')).to_have_text(f"rank {r['rank']} of {r['peers']}")
        expect(fairness.locator('.pr-note')).to_have_attribute('data-tip', re.compile(re.escape(f'{aim} {band}')))
    finally:
        context.close()


def test_a_calendar_period_reads_only_its_own_numbers(browser, live_server, h):
    """Switching to a calendar period drops the rota's fairness and score; switching back restores them."""
    catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})], settings=GOAL)
    related(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 900})
    try:
        shown = tab(open_person(page, person(h)), W)
        period = shown.get_by_label('Period', exact=True)
        period.select_option(label='This week')
        expect(shown.locator('.loading')).to_have_count(0)
        expect(shown.locator('.pr-period > small')).to_have_text(re.compile(r'\d{4}$'))  # this week's dates
        expect(shown.locator('.ov-stat-label').filter(has_text='Fairness')).to_have_count(0)  # a rota's measure
        expect(shown.get_by_role('heading', name=re.compile('^Score in'))).to_have_count(0)
        period.select_option(label='Requests rota')
        expect(shown.get_by_role('heading', name=re.compile('^Score in Requests rota'))).to_be_visible()
        expect(shown.locator('.ov-stat-label')).to_have_text(['Duties', 'Hours', 'Weekends', 'Fairness'])
        no_page_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('loading', [False, True], ids=['fast', 'loading'])
def test_an_open_place_opens_its_session_in_its_draft(browser, live_server, h, loading):
    """The opportunity link keeps the draft and does not filter the empty session away."""
    w = h.shift(h.workspace('Open place draft', day(), day(7)), f'{day()}T08:00:00+03:00', f'{day()}T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    held = []
    holding = True
    try:
        shown = tab(open_person(page, person(h)), W)
        # Planners can still choose their draft (its name already says draft: no "(draft)" twice).
        shown.get_by_label('Period', exact=True).select_option(label='Open place draft')
        expect(shown.locator('.loading')).to_have_count(0)
        places = section(shown, re.compile('^Open places'))
        if loading:
            def hold_sessions(route):
                response = route_fetch(route)
                if holding:
                    held.append((route, response))
                else:
                    route.fulfill(response=response)
            page.route('**/api/schedule/sessions?*', hold_sessions)
        places.get_by_role('button', name='Show in Schedule').first.click()
        if loading:
            wait_held(page, held)
            expect(page.locator('#main .loading:not(.sk-wait)')).to_be_visible()
            holding = False
            while held:
                route, response = held.pop(0)
                route.fulfill(response=response)
            page.unroute_all(behavior='wait')
        expect(page).to_have_url(re.compile('edit=' + w['id']))
        expect(page).not_to_have_url(re.compile('person='))
        expect_loaded(page, 'schedule', timeout=60000)
        expect(page.locator('#main .issue-spot').first).to_be_visible()
        if SHOTS:
            page.screenshot(path=str(Path(SHOTS) / 'open-place-in-schedule.png'))
    finally:
        holding = False
        while held:
            route, response = held.pop(0)
            route.fulfill(response=response)
        page.unroute_all(behavior='wait')
        context.close()


def test_personal_schedule_offers_only_ranges_it_accepts(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        shown = tab(open_person(page, person(h)), 'Schedule')
        shown.get_by_role('button', name='Choose dates').click()
        presets = page.get_by_role('group', name='Quick ranges')
        expect(presets.get_by_role('button', name='Next month', exact=True)).to_be_visible()
        expect(presets.get_by_role('button', name='Next 3 months', exact=True)).to_have_count(0)
        if SHOTS:
            page.screenshot(path=str(Path(SHOTS) / 'schedule-date-presets.png'))
    finally:
        context.close()


@pytest.mark.parametrize('value,label,note', [('none', 'On leave or away', False), ('details', 'Annual', True)])
def test_leave_follows_the_reason_setting_and_staff_read_their_own(browser, live_server, h, value, label, note):
    related(h)
    setting(h, value)
    for role, own in [('coordinator', False), ('staff', True)]:
        context, page = sign_in(browser, live_server, role, {'width': 390 if own else 1440, 'height': 900})
        try:
            panel = open_person(page, person(h))
            leave = section(tab(panel, L), 'Leave and time away')
            expect(leave.locator('.ph-row')).to_have_count(1)
            expect(leave).to_contain_text('Annual' if own else label)
            (expect(leave).to_contain_text if note and not own else expect(leave).not_to_contain_text)(PRIVATE)
            # Requests and swaps for this person follow, from the one Requests list.
            expect(panel.get_by_role('tabpanel', name=L, exact=True).locator('.pr-requests [data-row]').first).to_be_visible()
        finally:
            context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
@pytest.mark.parametrize('unlimited', [False, True])
def test_admin_signs_out_one_device_then_everywhere_and_it_is_recorded(browser, live_server, h, width, theme, unlimited):
    if unlimited:
        department = h.get('admin', '/api/me')['department']
        h.command('admin', 'department.sessions', {'idle_minutes': 480, 'max_hours': 24,
                  'idle_unlimited': True, 'max_unlimited': True}, department['id'], department['revision'])
    staff = another_session(browser, live_server)
    phone = another_session(browser, live_server)
    context, page = sign_in(browser, live_server, 'admin', {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        panel = open_person(page, person(h))
        shown = tab(panel, 'Access')
        expect(shown.locator('.ov-stat-label')).to_have_text(['Sign-in account', 'Last sign-in'])
        expect(shown.locator('.ov-stat').first).to_contain_text('staff')
        sessions = shown.locator('[data-table="person-sessions"] [data-row]')
        expect(sessions).to_have_count(2)
        expect(sessions.first).to_contain_text('Chrome')
        expect(sessions.first).to_contain_text('Local network')
        if unlimited:
            expect(sessions.first).to_contain_text('No automatic expiry')
        # Same-button confirm: the first press arms it (red), the second signs that device out, also when a live update
        # rebuilds the panel between the presses (its new Sign out for that session is drawn armed).
        moved, reads = [0], []
        live_pulse(page, 'access', moved)
        page.on('requestfinished', lambda r: reads.append(r) if r.url.endswith('/sessions') else None)
        sign_out = sessions.first.get_by_role('button')
        sign_out.click()
        expect(sign_out).to_have_class(re.compile(r'\barmed\b'))
        rebuild(page, reads, moved, 1)
        moved[0] = 0
        expect(sign_out).to_have_class(re.compile(r'\barmed\b'))
        expect(sign_out).to_contain_text('Sign out?')
        sign_out.click()
        expect(shown.locator('[data-table="person-sessions"]')).to_contain_text('Signed out')
        expect(shown.locator('.ph-head').get_by_role('button')).to_have_accessible_name('Sign out everywhere')
        armed_press(shown.locator('.ph-head').get_by_role('button'), 'Sign out everywhere?')
        expect(page.locator('.toast').filter(has_text='signed out everywhere')).to_be_visible()
        expect(shown.locator('[data-table="person-sessions"] [data-row]').get_by_role('button')).to_have_count(0)
        no_page_overflow(page)
        assert staff.request.get(live_server + '/api/me').status == 401
        assert phone.request.get(live_server + '/api/me').status == 401
    finally:
        context.close()
        staff.close()
        phone.close()
    assert h.client('admin').get('/api/audit?limit=100').status_code == 403
    actions = [e['action'] for e in h.get('auditor', '/api/audit?limit=100')['events']]
    assert 'session.revoke' in actions and 'account.end_sessions' in actions, actions


@pytest.mark.parametrize('unlimited', [False, True])
def test_staff_see_their_own_devices_and_sign_out_the_others(browser, live_server, h, unlimited):
    if unlimited:
        department = h.get('admin', '/api/me')['department']
        h.command('admin', 'department.sessions', {'idle_minutes': 480, 'max_hours': 24,
                  'idle_unlimited': True, 'max_unlimited': True}, department['id'], department['revision'])
    other = another_session(browser, live_server)
    context, page = sign_in(browser, live_server, 'staff', {'width': 390, 'height': 900})
    try:
        panel = open_person(page, person(h))
        shown = tab(panel, 'Access')
        sessions = shown.locator('[data-table="person-sessions"] [data-row]')
        expect(sessions).to_have_count(2)
        expect(shown.locator('[data-table="person-sessions"]')).to_contain_text('This device')
        if unlimited:
            expect(shown.locator('[data-table="person-sessions"]')).to_contain_text('This device · No automatic expiry')
        expect(shown.locator('.ph-head').get_by_role('button')).to_have_accessible_name('Sign out other devices')
        armed_press(shown.locator('.ph-head').get_by_role('button'), 'Sign out all?')
        expect(shown.locator('[data-table="person-sessions"]')).to_contain_text('Signed out')
        expect(shown.get_by_role('button', name='Sign out other devices', exact=True)).to_have_count(0)
        assert other.request.get(live_server + '/api/me').status == 401
        page.reload()  # this device stays signed in: the app loads, not the sign-in form
        # (On a phone the reloaded person panel may cover the account menu, so the menu is not the signal.)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', re.compile('.'))
        expect(page.get_by_label('Password', exact=True)).to_have_count(0)
        assert page.request.get(live_server + '/api/me').status == 200
    finally:
        context.close()
        other.close()


def test_coordinator_never_reads_a_colleagues_devices(browser, live_server, h):
    other = another_session(browser, live_server)
    context, page = sign_in(browser, live_server, 'coordinator')
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/people/' in r.url else None)
    try:
        panel = open_person(page, person(h))
        expect(panel.get_by_role('tab', name='Access', exact=True)).to_have_count(0)
        for name in TABS['coordinator']:
            tab(panel, name)
        assert not [u for u in reads if '/sessions' in u or '/accounts' in u], reads
        assert h.client('coordinator').get(f'/api/people/{person(h)["id"]}/sessions').status_code == 403
    finally:
        context.close()
        other.close()


def test_history_joins_sign_ins_with_changes(browser, live_server, h):
    other = another_session(browser, live_server)
    context, page = sign_in(browser, live_server, developer(h), {'width': 390, 'height': 900})
    try:
        panel = open_person(page, person(h))
        shown = tab(panel, 'History')
        expect(shown.locator('.audit-row').filter(has_text='signed in on a device').first).to_contain_text('Chrome')
        no_page_overflow(page)
    finally:
        context.close()
        other.close()


def test_new_tabs_load_fail_retry_and_say_when_empty(browser, live_server, h):
    stranger = h.configure('people', {'display_name': 'Record empty colleague'})['record']
    context, page = sign_in(browser, live_server, 'admin', {'width': 390, 'height': 900})
    try:
        panel = open_person(page, stranger)
        held = []
        page.route('**/workload', lambda route: held.append(route))
        panel.get_by_role('tab', name=W, exact=True).click()
        wait_held(page, held)
        expect(panel.locator('.loading')).to_have_count(1)  # a skeleton while it reads
        held.pop().abort('failed')
        expect(panel).to_contain_text('Couldn’t load workload.')
        page.unroute('**/workload')
        panel.get_by_role('button', name='Retry loading workload', exact=True).click()
        expect(panel.get_by_role('heading', name='Nothing in this period', exact=True)).to_be_visible()
        shown = tab(panel, 'Access')
        expect(shown.locator('.ov-stat').first).to_contain_text('Not linked to a sign-in')
        expect(shown).to_contain_text('No sessions yet.')
        expect(shown.get_by_role('button', name='Sign out everywhere', exact=True)).to_have_count(0)
        no_page_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_journey_through_every_tab_and_back_returns_focus(browser, live_server, h, width):
    related(h)
    p = person(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'people')
        page.get_by_label('Search people', exact=True).fill(p['display_name'])
        r = row(page, p['display_name'])
        r.get_by_role('button', name='Open ' + p['display_name'], exact=True).click()  # one action: Open (P-02)
        panel = page.get_by_role('region', name=p['display_name'], exact=True)
        panel.get_by_role('tab', name='Overview', exact=True).focus()
        for name in TABS['coordinator'][1:]:
            page.keyboard.press('ArrowRight')
            page.keyboard.press('Enter')
            expect(panel.get_by_role('tab', name=name, exact=True)).to_have_attribute('aria-selected', 'true')
            expect(panel.get_by_role('tabpanel', name=name, exact=True).locator('.loading')).to_have_count(0)
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(r.get_by_role('button', name='Open ' + p['display_name'], exact=True)).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_all_tabs_fit_one_row_and_the_open_tab_is_never_clipped(browser, live_server, h, width):
    context, page = sign_in(browser, live_server, developer(h), {'width': width, 'height': 900})
    try:
        panel = open_person(page, person(h))
        row = panel.get_by_role('tablist')
        tab(panel, 'Access')
        fits = 'r => r.scrollWidth <= r.clientWidth + 1'
        visible = 'r => { const t = r.querySelector("[aria-selected=true]"), a = t.getBoundingClientRect(), b = r.getBoundingClientRect(); return a.left >= b.left - 1 && a.right <= b.right + 1; }'
        if width >= 1024:
            assert row.evaluate(fits)  # short labels: all eight on one row at the default width
            expect(row).not_to_have_class(re.compile('fade'))
        else:
            expect(row).to_have_class(re.compile(r'\bfade-start\b'))  # scrolled to Access: the first tabs fade, not clip
        assert row.evaluate(visible)
        tab(panel, 'Overview')
        assert row.evaluate(visible)
        expect(panel.get_by_role('tab', name='Workload & fairness', exact=True)).to_have_attribute('data-tip', 'Workload & fairness')
    finally:
        context.close()


def test_department_scale_record_pages_its_sessions(browser, live_server, h):
    """250 sessions for one person among 160 people: Access shows the newest 100 and says how many there are."""
    org, dept = h.seed['organisation_id'], h.seed['department_id']
    ids = [str(uuid.uuid4()) for _ in range(160)]
    now = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': i, 'organisation_id': org, 'display_name': f'Scale Person {k:03}'} for k, i in enumerate(ids)])
        db.execute(insert(m.Session), [{'id': str(uuid.uuid4()), 'identity_id': h.seed['identities']['staff'], 'department_id': dept,
                                        'token_hash': uuid.uuid4().hex, 'csrf_hash': uuid.uuid4().hex, 'revoked': k % 2 == 0,
                                        'created_at': now - timedelta(hours=k), 'expires_at': now + timedelta(hours=1),
                                        'device_label': 'Safari / iOS / Phone', 'network_label': 'Public network'} for k in range(250)])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        panel = open_person(page, person(h))
        shown = tab(panel, 'Access')
        expect(shown).to_contain_text('Newest 100 of 250.')
        expect(shown.locator('[data-table="person-sessions"]')).to_contain_text('Safari · iOS · Phone')
    finally:
        context.close()


SHOTS = os.getenv('PERSON_SHOTS')


@pytest.mark.skipif(not SHOTS, reason='PERSON_SHOTS=<folder> keeps a screenshot of every tab per role for review')
@pytest.mark.parametrize('role', [r for r in TABS if r != 'unlinked'])
def test_screenshots_of_every_tab(browser, live_server, h, role):
    catalogue_rule(h, 'fair_share', [line({'apply': {'value': None, 'level': 'medium'}})], settings=GOAL)
    related(h)
    if role == 'programme_lead':
        identity(h, role, [role])
    extra = another_session(browser, live_server)
    try:
        for width, theme in [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')]:
            context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
            try:
                stored_theme(page, theme)
                panel = open_person(page, person(h))
                for name in TABS[role]:
                    tab(panel, name)
                    page.screenshot(path=str(Path(SHOTS) / f'{role}-{name.split()[0].lower()}-{width}-{theme}.png'), full_page=True)
            finally:
                context.close()
    finally:
        extra.close()


def test_workload_opens_on_the_published_rota_covering_today_never_an_empty_future_draft(browser, live_server, h):
    """Default period: the published rota that covers today; a future draft (even an empty one) is only a choice."""
    today = clock_now().date()
    w = h.workspace('Current rota', (today - timedelta(days=2)).isoformat(), (today + timedelta(days=5)).isoformat())
    w = h.shift(w, f'{today}T08:00:00+03:00', f'{today}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    h.publish(w)
    h.workspace('Next month', (today + timedelta(days=30)).isoformat(), (today + timedelta(days=60)).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        shown = tab(open_person(page, person(h)), W)
        period = shown.get_by_label('Period', exact=True)
        expect(period.locator('option:checked')).to_have_text('Current rota')
        expect(period.locator('option', has_text='Next month (draft)')).to_have_count(1)
        expect(shown.locator('.ov-stat').first.locator('strong')).to_have_text('1')
    finally:
        context.close()


def test_a_daily_limit_is_one_line_with_its_fullest_day(browser, live_server, h):
    """A maximum checked every day of the period reads as one line ("Maximum 1 · 31 periods · fullest …"), not a line a day."""
    rule = catalogue_rule(h, 'duties_per_period', [line({'max': {'value': 1, 'level': 'mandatory'}})])
    related(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        shown = tab(open_person(page, person(h)), W)
        limits = section(shown, 'Limits').locator('.pr-line').filter(has_text=rule['name'])
        expect(limits).to_have_count(1)
        expect(limits).to_contain_text(re.compile(r'Maximum 1 · \d+ periods'))
    finally:
        context.close()
