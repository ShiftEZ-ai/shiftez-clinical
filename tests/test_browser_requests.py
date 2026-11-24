"""Requests page: leave, weekly availability, wishes and swaps in one list with one status flow, per role, on desktop and
phone, light and dark; New request for every kind; review (single and bulk), Undo, deep links and the schedule it changes."""
from packages.domain.time import now as clock_now
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity, swap_automation
from .test_staff_wishes import department_settings
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, wait_held, filter_by, toolbar, close_popover, route_fetch, choose  # noqa: F401
from .test_browser import whole_list, reconnect

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
SOON = clock_now().date() + timedelta(days=5)  # duties stay in the future whenever the suite runs


def day(n=0):
    return (SOON + timedelta(days=n)).isoformat()


def leave(h, role='staff', person=None, on=None, kind='leave'):
    on = on or day()
    return h.command(role, 'request.create', {'person_id': person or h.seed['people'][0], 'start': f'{on}T00:00:00+03:00',
                                              'end': f'{on}T23:59:00+03:00', 'private_reason': 'Family matter', 'kind': kind})['record']


def published(h, people=None, days=1):
    """A published rota from SOON with the staff member's duty on its first day (and a colleague's on the next)."""
    w = h.workspace('Requests rota', day(), day(7))
    w = h.shift(w, f'{day()}T08:00:00+03:00', f'{day()}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    if days > 1:
        w = h.shift(w, f'{day(1)}T08:00:00+03:00', f'{day(1)}T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    return h.publish(w)['publication_id']


def rows(page):
    return page.locator('#main tr.dt-open')


def row(page, text):
    return rows(page).filter(has_text=text).first


def new_request(page, kind):
    page.get_by_role('button', name='New request', exact=True).click()
    dialog = page.get_by_role('dialog', name='New request')
    dialog.get_by_role('radio', name=re.compile('^' + re.escape(kind))).check()
    return dialog


def no_horizontal_scroll(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


CONTRAST = '''els => els.map(el => {
  const rgb = c => (c.match(/[\\d.]+/g) || []).map(Number);
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  let bg = null;
  for (let n = el; n && !bg; n = n.parentElement) { const c = rgb(getComputedStyle(n).backgroundColor); if (c.length === 3 || c[3] > 0) bg = c; }
  const a = lum(rgb(getComputedStyle(el).color)), b = lum(bg || [255, 255, 255]);
  return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
})'''


@pytest.mark.parametrize('role,viewport,theme', [
    ('staff', None, 'light'), ('staff', PHONE, 'dark'), ('coordinator', None, 'dark'), ('coordinator', PHONE, 'light'),
    ('approver', None, 'light'), ('approver', PHONE, 'dark')])
def test_each_role_sees_its_list_on_desktop_and_phone(browser, live_server, h, role, viewport, theme):
    published(h)
    leave(h)
    h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [1], 'start_time': '13:00', 'end_time': '17:00', 'starts_on': day()})
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'first_day': day(40), 'last_day': day(40), 'kind': 'prefer_off', 'points': 2})
    leave(h, 'coordinator', h.seed['people'][1], day(2), 'meeting')
    context, page = sign_in(browser, live_server, role, viewport)
    try:
        page.evaluate("t => localStorage.setItem('rota.prefs', JSON.stringify({forms:'all', theme:t}))", theme)
        page.reload()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        navigate(page, 'requests')
        if role != 'staff':
            whole_list(page)  # the whole list, not the decider's Waiting for you
        main = page.locator('#main')
        cards = main.locator('.dt-card') if viewport else rows(page)
        # Staff see their own three; reviewers see everyone's four.
        expect(cards).to_have_count(3 if role == 'staff' else 4)
        expect(main.get_by_text('Waiting for approval').first).to_be_visible()
        # A reviewer reads what approving opens; staff read that they are on duty then (R-25).
        expect(main.get_by_text(re.compile(r"You're on duty" if role == 'staff' else r'Approving opens 1 place'))).to_be_visible()
        new = page.get_by_role('button', name='New request', exact=True)
        expect(new).to_have_count(0 if role == 'approver' else 1)  # approvers decide; they have nothing to ask for here
        # Wishes are on: their calendar for everyone, the points meter for staff (their own points).
        controls = main.get_by_role('group', name='Requests', exact=True).locator('.request-wishes')
        expect(controls.get_by_role('button', name='Wish calendar', exact=True)).to_be_visible()
        expect(controls.get_by_role('meter')).to_have_count(1 if role == 'staff' else 0)
        if role == 'approver':
            # Weekly availability is the coordinator's to decide: read-only here, while leave can be approved.
            pattern = (main.locator('.dt-card') if viewport else rows(page)).filter(has_text='Every week').first
            expect(pattern.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
            first = (main.locator('.dt-card') if viewport else rows(page)).filter(has_text='Leave').first
            expect(first.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-theme', theme)
        if viewport:
            no_horizontal_scroll(page)
        ratios = page.evaluate(CONTRAST, [*main.locator('.badge, .request-title strong, .request-impact.has-impact').element_handles()][:12])
        assert ratios and min(ratios) >= 4.5, ratios
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('role,viewport', [('staff', PHONE), ('coordinator', None), ('approver', PHONE)])
def test_empty_requests_keep_wish_controls_and_load_calendar_only_when_opened(browser, live_server, h, role, viewport):
    context, page = sign_in(browser, live_server, role, viewport)
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/wishes?' in r.url else None)
    try:
        navigate(page, 'requests')
        # QA-039: a decider lands on Waiting for you, which says so when empty.
        expect(page.get_by_role('heading', name='No requests yet' if role == 'staff' else 'Nothing waiting for you', exact=True)).to_be_visible()
        controls = page.locator('#main').get_by_role('group', name='Requests', exact=True)
        calendar = controls.get_by_role('button', name='Wish calendar', exact=True)
        expect(calendar).to_be_visible()
        if viewport and role != 'staff':
            # No points beside it: on a phone the calendar sits among the toolbar's icon tools, never on a row of its own.
            tool = controls.locator(':scope > .dt-tool').first
            expect(tool).to_be_visible()
            assert abs(calendar.bounding_box()['y'] - tool.bounding_box()['y']) < 4
        expect(calendar).to_have_attribute('aria-expanded', 'false')
        expect(calendar).to_have_attribute('aria-controls', 'request-wish-calendar')
        assert not reads, reads  # The calendar is read only when opened.
        calendar.click()
        expect(page.get_by_role('region', name='Wishes', exact=True)).to_be_visible()
        expect(calendar).to_have_attribute('aria-expanded', 'true')
        assert reads
        no_horizontal_scroll(page)
        calendar.click()
        expect(page.locator('#request-wish-calendar')).to_be_hidden()
        expect(calendar).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()


@pytest.mark.parametrize('role,period,used,catch_up', [
    ('staff', 'week', 0, False), ('staff', 'quarter', 10, False), ('staff_coordinator', 'month', 4, True)])
def test_wish_controls_keep_actual_staff_points_and_catch_up(browser, live_server, h, role, period, used, catch_up):
    if role == 'staff_coordinator':
        identity(h, role, ['staff', 'coordinator'], h.seed['people'][0])
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_period': period}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    today = clock_now().astimezone(ZoneInfo(h.get(role, '/api/me')['organisation']['timezone'])).date()
    if used:
        h.command(role, 'wish.create', {'person_id': h.seed['people'][0], 'first_day': today.isoformat(),
                  'last_day': today.isoformat(), 'kind': 'prefer_off', 'points': used})
    if catch_up:
        w = h.workspace('Wish points catch-up', today.isoformat(), (today + timedelta(days=7)).isoformat())
        w = h.shift(w, f'{today}T08:00:00+03:00', f'{today}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
        h.publish(w)  # A real declined wish gives this staff coordinator a multiplier.
    points = h.get(role, '/api/requests/all')['wishes']
    assert points['used'] == used and points['budget_points'] == 10 and points['period'] == period
    assert points['multiplier'] == (1.4 if catch_up else 1)
    context, page = sign_in(browser, live_server, role, PHONE)
    try:
        navigate(page, 'requests')
        controls = page.locator('#main').get_by_role('group', name='Requests', exact=True).get_by_role('group', name='Wish points', exact=True)
        expect(controls.get_by_role('meter')).to_have_attribute('value', str(used))
        expect(controls.get_by_role('meter')).to_have_attribute('max', '10')
        expect(controls.get_by_text(re.compile(rf'^Wish points: {used} of 10 used in [^·]+$'))).to_be_visible()
        if catch_up:
            expect(controls.get_by_text('Next wishes count ×1.4', exact=True)).to_be_visible()
        else:
            expect(controls.get_by_text('Next wishes count', exact=False)).to_have_count(0)
        no_horizontal_scroll(page)
    finally:
        context.close()


def test_requests_without_wishes_have_no_calendar_or_points_help(browser, live_server, h):
    h.command('admin', 'department.settings', {**department_settings(h), 'generation_defaults': {'scoring': {'wish_budget_points': 0}}},
              h.seed['department_id'], h.get('admin', '/api/me')['organisation']['revision'])
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        expect(page.get_by_role('heading', name='No requests yet', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Wish calendar', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='New request', exact=True)).to_be_visible()
    finally:
        context.close()


# Auditors and programme leads read person-fact approvals here (tests/test_browser_scheduling_fact_approvals.py).
@pytest.mark.parametrize('role', ['credentialer', 'operator'])
def test_roles_without_requests_are_restricted(browser, live_server, h, role):
    context, page = sign_in(browser, live_server, role)
    try:
        page.evaluate("location.hash='requests'")
        expect(page.get_by_role('heading', name='Restricted page', exact=True)).to_be_visible(timeout=20000)
        expect(page.locator('#department-nav [data-route=requests]')).to_have_count(0)
    finally:
        context.close()


def test_empty_filtered_failure_and_retry(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        # QA-039: a decider lands on Waiting for you; empty, it says so and offers the whole list.
        expect(page.get_by_role('heading', name='Nothing waiting for you', exact=True)).to_be_visible()
        page.get_by_role('button', name='Show all requests', exact=True).click()
        expect(page.get_by_role('heading', name='Nothing to review', exact=True)).to_be_visible()
        page.close()
    finally:
        context.close()
    leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        page.get_by_role('searchbox', name='Search requests').fill('nothing like this')
        expect(page.get_by_role('heading', name='No matching requests', exact=True)).to_be_visible()
        page.get_by_role('searchbox', name='Search requests').fill('')
        # The list cannot load: say so and offer Retry (never an empty list).
        # A reload: going back within the tab shows the kept reply at once (api.ts keptReads), never a failure.
        page.route('**/api/requests/all*', lambda r: r.abort())
        page.reload()
        expect(page.get_by_text('Couldn’t load Requests.', exact=False)).to_be_visible(timeout=20000)
        page.unroute('**/api/requests/all*')
        page.get_by_role('button', name='Retry', exact=True).click()
        expect(rows(page)).to_have_count(1, timeout=20000)
    finally:
        context.close()


def test_an_obsolete_search_cannot_hold_the_skeleton_over_a_newer_empty_answer(browser, live_server, h):
    """search A is held, newer search B answers empty (No matching requests at once, not a
    skeleton while A is on its way); A settling afterwards changes nothing."""
    leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    held = []
    try:
        navigate(page, 'requests')
        expect(rows(page)).to_have_count(1)

        def hold_a(route):
            if parse_qs(urlparse(route.request.url).query).get('q') == ['annual']:
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/requests/all?*', hold_a)
        search = page.get_by_role('searchbox', name='Search requests')
        search.fill('annual')
        wait_held(page, held)
        search.fill('nothing like this')
        empty = page.get_by_role('heading', name='No matching requests', exact=True)
        expect(empty).to_be_visible()
        held.pop().continue_()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(empty).to_be_visible()
    finally:
        for route in held:
            route.continue_()
        page.unroute_all(behavior='wait')
        context.close()


def test_overlapping_leave_shows_the_server_warning_and_keeps_both_requests(browser, live_server, h):
    original = leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Synthetic overlapping leave')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.locator('#toast-tray')).to_contain_text('Warning: This overlaps existing waiting or approved leave, or a recorded absence.')
        expect(rows(page)).to_have_count(2)
        records = h.get('staff', '/api/requests')['requests']
        assert len(records) == 2 and next(r for r in records if r['id'] == original['id'])['revision'] == original['revision']
    finally:
        context.close()


def test_staff_ask_for_leave_a_wish_and_a_swap_then_reviewers_decide(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    pid = published(h, days=2)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        expect(page.get_by_role('heading', name='No requests yet', exact=True)).to_be_visible()
        # Leave: the kind first, then its fields.
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').check()
        dialog.get_by_label('First day', exact=True).fill(day())
        dialog.get_by_label('Last day', exact=True).fill(day())
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('Reason', exact=True).fill('Family event')
        dialog.get_by_role('button', name='Send request', exact=True).click()
        expect(page.get_by_text('Request submitted, not yet approved.')).to_be_visible()
        expect(row(page, 'Leave')).to_contain_text('Waiting for approval')
        expect(row(page, 'Leave')).to_contain_text("You're on duty")  # your own leave, not the reviewer's "Approving opens" (R-25)
        # A wish, with the points meter in the form; Undo takes it back.
        dialog = new_request(page, 'Wish')
        expect(dialog.locator('.wish-budget meter')).to_be_visible()
        dialog.get_by_label('From', exact=True).fill(day(30))
        dialog.get_by_label('Points', exact=True).select_option('3')
        expect(dialog.locator('.wish-budget')).to_contain_text('left after this wish')
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        toast = page.locator('#toast-tray .toast', has_text='Wish saved')
        expect(row(page, 'Day off')).to_contain_text('Open')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert all(w['status'] == 'withdrawn' for w in h.get('staff', '/api/requests/all?type=wish')['items'])
        # A swap of the published duty.
        dialog = new_request(page, 'Swap a duty')
        dialog.get_by_label('Swap with', exact=True).select_option(label=h.get('staff', '/api/swaps/options')['recipients'][0]['display_name'])
        dialog.get_by_label('Message to them', exact=True).fill('Could we swap these days?')
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(page.get_by_text('Offer sent.', exact=False)).to_be_visible()
        expect(row(page, 'Swap').locator('.badge')).to_contain_text('Waiting for ' + h.get('staff', '/api/swaps/options')['recipients'][0]['display_name'])
    finally:
        context.close()
    request = h.get('approver', '/api/requests/all?type=leave')['items'][0]
    swap = h.get('staff', '/api/requests/all?type=swap')['items'][0]
    # The approver decides the leave from its side panel (#requests?open=<id>), with its history.
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.evaluate("id => location.hash = 'requests?open=' + id", request['id'])
        panel = page.get_by_role('region', name='Annual')
        expect(panel.get_by_text('Submitted', exact=True)).to_be_visible(timeout=20000)
        panel.get_by_role('button', name='Approve', exact=True).click()
        confirm = page.get_by_role('dialog', name='Approve leave')
        confirm.get_by_role('button', name='Approve', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Approved · ', exact=False)).to_be_visible()
        panel = page.get_by_role('region', name='Annual')
        expect(panel.locator('.request-history')).to_contain_text('Approved')
        expect(panel).to_contain_text('Affects 1 duty · published')
        # Show in Schedule opens the published rota at that week.
        panel.get_by_role('button', name=re.compile('^Show .* in Schedule$')).click()
        expect_loaded(page, 'published')
    finally:
        context.close()
    # The schedule reflects it: the published duty now meets known unavailability.
    findings = h.get('coordinator', f'/api/publications/{pid}/coverage')['evaluation']['findings']
    assert any(f['code'] == 'UNAVAILABLE' and f['person_id'] == h.seed['people'][0] for f in findings)
    # The swap: the colleague offers a duty back, the proposer confirms, a coordinator prepares it, publication applies it
    # (the manual path; test_browser_governance covers automatic preparation and publication).
    swap_automation(h, False, False)
    options = h.get('staff_two', '/api/swaps/options')['assignments']
    h.command('staff_two', 'swap.counteroffer', {'assignment_id': options[0]['assignment_id'], 'reason': 'This one in return'}, swap['id'], swap['revision'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        row(page, 'Swap').get_by_role('button', name='Confirm swap', exact=True).click()
        page.get_by_role('dialog', name='Confirm swap').get_by_label('Reason', exact=True).fill('Agreed with my colleague')
        page.get_by_role('dialog', name='Confirm swap').get_by_role('button', name='Confirm swap', exact=True).click()
        expect(row(page, 'Swap')).to_contain_text('Accepted by colleague')
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("location.hash='requests?type=swap'")
        expect_loaded(page, 'requests')
        expect(page.get_by_role('button', name=re.compile('^Remove filter Type: Swap'))).to_be_visible()
        row(page, 'Swap').get_by_role('button', name='Prepare amendment', exact=True).click()
        dialog = page.get_by_role('dialog', name='Prepare consented amendment')
        dialog.get_by_label('Preparation reason', exact=True).fill('Both agreed')
        dialog.get_by_role('button', name='Prepare draft amendment', exact=True).click()
        expect_loaded(page, 'schedule', timeout=30000)
    finally:
        context.close()
    draft = next(w for w in h.get('coordinator', '/api/workspaces?limit=50&offset=0')['workspaces'] if w['status'] == 'draft')
    h.publish(h.detail(draft['id'])['workspace'])
    assert h.get('staff', '/api/requests/all?type=swap')['items'][0]['stage'] == 'approved'


def test_new_request_layout_and_a_kind_with_nothing_to_send(browser, live_server, h):
    """Leave: All day has its own row, so From and Until sit side by side. Swap with no colleague to offer to: the
    form says so and its button is disabled (not a click that fails). The panel's subtitle does not repeat its title."""
    published(h)
    made = leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Leave')
        dialog.get_by_label('All day').uncheck()
        start, end = (dialog.get_by_label(n, exact=True) for n in ('Request from', 'Request until'))
        expect(start).to_be_visible()
        assert abs(start.bounding_box()['y'] - end.bounding_box()['y']) < 2
        dialog.get_by_role('radio', name=re.compile('^Swap a duty')).check()
        expect(dialog.get_by_text('No colleague can receive a swap offer yet: offers go to colleagues whose staff sign-in is linked to their profile.')).to_be_visible()
        expect(dialog.get_by_role('button', name='Ask your coordinator instead', exact=True)).to_be_visible()
        expect(dialog.get_by_role('button', name='Send offer', exact=True)).to_be_disabled()
        dialog.get_by_role('radio', name=re.compile('^Leave')).check()
        expect(dialog.get_by_role('button', name='Send request', exact=True)).to_be_enabled()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.get_by_role('button', name=re.compile('^Discard')).click()
        page.evaluate("id => location.hash = 'requests?open=' + id", made['id'])
        panel = page.get_by_role('region', name='Other')
        expect(panel.locator('.side-head small:not(.side-pos)')).to_have_text('Leave')  # titled Other (untyped, #268): the kind moves to the subtitle; your own name is not repeated (R-07)
    finally:
        context.close()


def test_bulk_decisions_with_one_reason_and_deep_links(browser, live_server, h):
    for n in range(3):
        leave(h, 'coordinator', h.seed['people'][n % 2], day(n))
    h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [2], 'start_time': '08:00', 'end_time': '10:00', 'starts_on': day()})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.locator('#department-nav [data-route=requests] .nav-count')).to_have_text('3')  # waiting on this approver
        for n in range(2):
            rows(page).filter(has_text='Leave').nth(n).locator('input.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Decline 2').click()
        dialog = page.get_by_role('dialog', name='Decline 2 requests')
        dialog.get_by_label('Reason', exact=True).fill('Too many people away that week')
        dialog.get_by_role('button', name='Decline 2', exact=True).click()
        dialog.get_by_role('button', name=re.compile(r'^Confirm decline 2\?')).click()  # armed: the same button confirms
        expect(page.get_by_text('2 requests declined.')).to_be_visible()
        declined = [i for i in h.get('approver', '/api/requests/all?status=declined')['items']]
        assert len(declined) == 2 and {i['decision_reason'] for i in declined} == {'Too many people away that week'}
        # New request opens from a link (Home links here); approvers have nothing to ask for, so nothing opens.
        page.evaluate("location.hash='requests?status=waiting'")
        expect(page.get_by_role('button', name=re.compile('^Remove filter Status: Waiting for approval'))).to_be_visible(timeout=20000)
        expect(rows(page)).to_have_count(2)  # one leave, one weekly pattern
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff')
    try:
        page.evaluate("location.hash='requests?new=1&type=weekly'")
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_role('radio', name=re.compile('^Unavailable every week'))).to_be_checked(timeout=20000)
        expect(page).not_to_have_url(re.compile('new=1'))
        dialog.get_by_label('Tuesday').check()
        dialog.get_by_role('button', name='Send for approval', exact=True).click()
        expect(page.get_by_text('Weekly availability sent for approval.')).to_be_visible()
    finally:
        context.close()


def test_status_views_replace_status_tabs_with_counts_and_a_warning(browser, live_server, h):
    """Status is a view (the shared views picker, first in the toolbar): each with its count; what waits on this person
    stays on the button whichever other view is shown. A decider lands on Waiting for you (QA-039)."""
    made = [leave(h, 'coordinator', h.seed['people'][n % 2], day(n)) for n in range(3)]
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Cover arranged'}, made[0]['id'], made[0]['revision'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        toolbar = page.locator('#main .dt-toolbar')
        views = toolbar.locator('.dt-saved')
        expect(views).to_have_accessible_name('Saved views: Waiting for you')
        expect(rows(page)).to_have_count(2)
        # Before Search, Filter, Group, Sort and View settings .
        assert toolbar.evaluate('''t => { const s = t.querySelector(".dt-saved"); return [...t.querySelectorAll("input[type=search], .dt-tool:not(.dt-saved)")]
          .filter(n => n.matches("input") || /^(Filter|Group|Sort|View settings)/.test(n.getAttribute("aria-label") || ""))
          .every(n => n.compareDocumentPosition(s) & Node.DOCUMENT_POSITION_PRECEDING) }''')
        views.click()
        menu = page.get_by_role('menu', name='Saved views')
        expect(menu.get_by_role('menuitemradio', name=re.compile('^Waiting for you'))).to_have_attribute('aria-checked', 'true')
        expect(menu.get_by_role('menuitemradio', name=re.compile('^Waiting for you'))).to_contain_text('2')
        expect(menu.get_by_role('menuitemradio', name=re.compile('^All requests'))).to_contain_text('3')  # the whole list's counts
        expect(menu.get_by_role('menuitemradio', name=re.compile('^Approved'))).to_contain_text('1')
        menu.get_by_role('menuitemradio', name=re.compile('^Waiting for approval')).click()
        expect(views).to_have_accessible_name('Saved views: Waiting for approval, 2 waiting for you')
        expect(rows(page)).to_have_count(2)
        # The view's own status filter is the view, not one the person set: Filter stays plain (X-01).
        expect(toolbar.get_by_role('button', name=re.compile(r'^Filter( \(\d+ from the view\))?$'))).to_be_visible()  # F-C2
        views.click()
        menu.get_by_role('menuitemradio', name=re.compile('^All requests')).click()
        expect(rows(page)).to_have_count(3)
        expect(page.locator('#main [role=tablist]')).to_have_count(0)
        # At 1024 the warning on the views button does not push View settings onto a row of its own: the search narrows.
        page.set_viewport_size({'width': 1024, 'height': 800})
        rows_of = toolbar.evaluate('t => [...t.querySelectorAll("button, input")].filter(c => c.getClientRects().length).map(c => [c.getAttribute("aria-label") || c.textContent, Math.round(c.getBoundingClientRect().bottom)])')
        assert max(b for _, b in rows_of) - min(b for _, b in rows_of) < 16, rows_of
    finally:
        context.close()


@pytest.fixture
def department_scale_request_list(browser, live_server, h):
    """150 people and years of requests: the first page shows the waiting ones first and loads quickly; more on demand."""
    scale_requests(h)
    context, page = sign_in(browser, live_server, 'approver', PHONE)
    try:
        began = time.perf_counter()
        navigate(page, 'requests')
        # The approver lands on Waiting for you: the 15 waiting of 3,000, read by the server (for_me=1).
        expect(page.locator('#main .dt-card')).to_have_count(15)
        elapsed = time.perf_counter() - began
        whole_list(page)  # the whole list, not the decider's Waiting for you
        yield page, elapsed
    finally:
        context.close()


def scale_requests(h):
    """3,000 fictional leave requests of 150 people, 15 of them waiting."""
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Scale person {n:03d}') for n in range(150 - len(h.seed['people']))]
        db.add_all(made)
        db.flush()
        creator = h.seed['identities']['coordinator']
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for n, pid in enumerate([*h.seed['people'], *(p.id for p in made)]):
            for k in range(20):
                at = start + timedelta(days=41 * k + n % 30)
                db.add(m.RequestRecord(department_id=h.seed['department_id'], person_id=pid, kind='leave', start=at, end=at + timedelta(days=1),
                                       private_reason='Scale fixture', status='submitted' if k == 19 and n % 10 == 0 else 'approved', created_by=creator))


def test_department_scale_list(department_scale_request_list):
    page, _ = department_scale_request_list
    expect(page.get_by_text('Showing 100 of 3000')).to_be_visible()
    expect(page.locator('#main .dt-card-group').first).to_contain_text('Waiting for approval')
    page.get_by_role('button', name='Show more', exact=True).click()
    expect(page.get_by_text('Showing 200 of 3000')).to_be_visible()
    no_horizontal_scroll(page)
    # One count: the footer's server total, not also the toolbar's "200 requests" of loaded rows; how many are loaded
    # is said once, by "Showing 200 of 3000" at the list's end (C14).
    expect(page.locator('.app-footer .dt-count')).to_have_text('3000 requests')
    expect(page.locator('#main .dt-toolbar .dt-count')).to_be_hidden()


@pytest.mark.wall_benchmark
def test_department_scale_request_list_cpu_budget(department_scale_request_list):
    _, elapsed = department_scale_request_list
    assert elapsed < 15


def test_approvals_can_be_undone_singly_and_in_bulk(browser, live_server, h):
    first = leave(h, on=day(1))
    for n in range(2):
        leave(h, 'coordinator', h.seed['people'][1], day(2 + n))
    context, page = sign_in(browser, live_server, 'approver')
    try:
        # One approval from its panel, then Undo from its message: it waits again, and the history says so.
        page.evaluate("id => location.hash = 'requests?open=' + id", first['id'])
        panel = page.get_by_role('region', name='Other')
        panel.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog', name='Approve leave').get_by_role('button', name='Approve', exact=True).click()
        page.locator('#toast-tray .toast', has_text='Approved · ').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Undone.', exact=False)).to_be_visible()
        expect(page.get_by_role('region', name='Other').locator('.request-history')).to_contain_text('Decision undone')
        assert h.get('approver', '/api/requests/all?id=' + first['id'])['item']['stage'] == 'waiting'
        page.keyboard.press('Escape')
        whole_list(page)  # decided rows leave Waiting for you; the whole list shows them Approved
        # Bulk: one Undo puts the whole batch back.
        for n in range(3):
            rows(page).filter(has_text='Leave').nth(n).locator('input.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Approve 3').click()
        dialog = page.get_by_role('dialog', name='Approve 3 requests')
        dialog.get_by_label('Reason', exact=True).fill('Cover arranged for all three')
        dialog.get_by_role('button', name='Approve all', exact=True).click()
        # Whole-row substring matching also finds "Approved" across the Approve/Decline buttons.
        expect(rows(page).get_by_text('Approved', exact=True)).to_have_count(3)
        page.locator('#toast-tray .toast', has_text='3 requests approved.').get_by_role('button', name='Undo', exact=True).click()
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(3)
        assert {i['stage'] for i in h.get('approver', '/api/requests/all?type=leave')['items']} == {'waiting'}
    finally:
        context.close()


@pytest.mark.parametrize('role,source', [('approver', 'request'), ('coordinator', 'pattern')])
@pytest.mark.parametrize('decision', ['Approve', 'Decline'])
def test_bulk_decision_and_undo_use_one_command_each(browser, live_server, h, role, source, decision):
    """A held batch answer keeps the list waiting; one command decides all, and its one Undo puts all back."""
    from .test_browser import route_fetch
    for n in range(3):
        if source == 'request':
            leave(h, 'coordinator', h.seed['people'][1], day(1 + n))
        else:
            h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [n],
                      'start_time': '13:00', 'end_time': '17:00', 'starts_on': day()})
    context, page = sign_in(browser, live_server, role)
    held, commands = [], []
    draining = False
    try:
        navigate(page, 'requests?status=waiting' if source == 'request' else 'requests')
        if source != 'request':
            whole_list(page)  # decided rows leave Waiting for you; the whole list shows their decision
        nav_count = page.locator('#department-nav [data-route=requests] .nav-count')
        expect(nav_count).to_have_text('3')
        nav_item = page.locator('#department-nav [data-route=requests]')
        expect(nav_item).to_have_attribute('data-tip', re.compile(r'· 3 '))
        def hold_batch(route):
            kind = route.request.post_data_json['kind']
            commands.append(kind)
            if kind != 'request.decide_many':
                return route.continue_()
            assert len(route.request.post_data_json['payload']['items']) == 3
            answer = route_fetch(route)  # committed on the server; the answer is held
            if draining:
                return route.fulfill(response=answer)
            held.append((route, answer))
        page.route(re.compile(r'.*/api/commands$'), hold_batch)
        for n in range(3):
            rows(page).nth(n).locator('input.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name=f'{decision} 3').click()
        dialog = page.get_by_role('dialog', name=f'{decision} 3 requests')
        if decision == 'Approve':
            dialog.get_by_label('Reason', exact=True).fill('Cover arranged')
            dialog.get_by_role('button', name='Approve all', exact=True).click()
        else:  # Decline many: its own reason and the armed confirm (R-15)
            dialog.get_by_label('Reason', exact=True).fill('Cover arranged')
            dialog.get_by_role('button', name='Decline 3', exact=True).click()
            dialog.get_by_role('button', name=re.compile(r'^Confirm decline 3\?')).click()
        wait_held(page, held)
        expect(dialog.get_by_role('button', name='Saving…', exact=True)).to_be_disabled()
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(3)
        expect(rows(page).get_by_text('Approved' if decision == 'Approve' else 'Declined', exact=True)).to_have_count(0)
        said = '3 requests ' + ('approved.' if decision == 'Approve' else 'declined.')
        expect(page.locator('#toast-tray .toast', has_text=said)).to_have_count(0)
        route, answer = held.pop(0)
        route.fulfill(response=answer)
        toast = page.locator('#toast-tray .toast', has_text=said)
        if source == 'request':
            expect(rows(page)).to_have_count(0)
            expect(page.locator('.app-footer')).to_contain_text('0 requests')
        else:
            expect(rows(page).get_by_text('Approved' if decision == 'Approve' else 'Declined', exact=True)).to_have_count(3)
        expect(nav_count).to_have_count(0)
        expect(nav_item).not_to_have_attribute('data-tip', re.compile(r'\d'))  # the kept frame drops the old count's tip
        # A real pulse after this confirmed save keeps the list and its badge; hover holds its Undo toast.
        expect(dialog).to_have_count(0)
        toast.hover()
        with page.expect_response('**/api/pulse') as pulse:
            page.clock.run_for(61_000)
        pulse.value.finished()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(3)
        expect(nav_count).to_have_text('3')
        assert {i['stage'] for i in h.get(role, '/api/requests/all?type=' + ('leave' if source == 'request' else 'weekly'))['items']} == {'waiting'}
        assert commands == ['request.decide_many', 'request.undo']
        expect(page.locator('#toast-tray .toast', has_text='Decisions saved.')).to_have_count(0)
    finally:
        draining = True
        while held:
            route, answer = held.pop(0)
            route.fulfill(response=answer)
        page.unroute_all(behavior='wait')
        context.close()


def test_a_pre_save_requests_reload_cannot_replace_confirmed_decisions(browser, live_server, h):
    from .test_browser import route_fetch
    for n in range(3):
        leave(h, 'coordinator', h.seed['people'][1], day(n))
    context, page = sign_in(browser, live_server, 'approver')
    held = []
    draining = False
    captured = False
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        def hold_search(route):
            nonlocal captured
            if captured or parse_qs(urlparse(route.request.url).query).get('q') != ['Jordan']:
                return route.continue_()
            captured = True
            response = route_fetch(route)
            if draining:
                route.fulfill(response=response)
            else:
                held.append((route, response))
        page.route('**/api/requests/all?*', hold_search)
        page.get_by_role('searchbox', name='Search requests').fill('Jordan')
        wait_held(page, held)
        for n in range(3):
            rows(page).nth(n).locator('input.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Approve 3').click()
        dialog = page.get_by_role('dialog', name='Approve 3 requests')
        dialog.get_by_label('Reason', exact=True).fill('Cover arranged')
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/commands')
                                  and r.request.post_data_json['kind'] == 'request.decide_many') as committed:
            dialog.get_by_role('button', name='Approve all', exact=True).click()
        assert committed.value.status == 200
        expect(dialog).to_have_count(0)
        expect(rows(page).get_by_text('Approved', exact=True)).to_have_count(3)
        route, response = held.pop(0)
        with page.expect_response(lambda r: parse_qs(urlparse(r.url).query).get('q') == ['Jordan']) as delivered:
            route.fulfill(response=response)
        delivered.value.finished()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(rows(page).get_by_text('Approved', exact=True)).to_have_count(3)
        expect(page.locator('#department-nav [data-route=requests] .nav-count')).to_have_count(0)
    finally:
        draining = True
        while held:
            route, response = held.pop(0)
            route.fulfill(response=response)
        page.unroute_all(behavior='wait')
        context.close()


def test_bulk_refusal_reports_no_committed_decisions(browser, live_server, h):
    """A request changed after selection refuses the whole batch rather than saving earlier rows."""
    from .test_browser import route_fetch
    for n in range(3):
        leave(h, 'coordinator', h.seed['people'][1], day(1 + n))
    context, page = sign_in(browser, live_server, 'approver')
    submitted = []
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        def refuse_second(route):
            value = route.request.post_data_json
            if value['kind'] != 'request.decide_many':
                return route.continue_()
            submitted.extend(value['payload']['items'])
            changed = submitted[1]
            h.command('approver', 'request.decide', {'decision': 'reject', 'reason': 'Changed meanwhile'},
                      changed['id'], changed['expected_revision'])
            route.fulfill(response=route_fetch(route))
        page.route(re.compile(r'.*/api/commands$'), refuse_second)
        for n in range(3):
            rows(page).nth(n).locator('input.bulk-check').check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Approve 3').click()
        dialog = page.get_by_role('dialog', name='Approve 3 requests')
        dialog.get_by_label('Reason', exact=True).fill('Cover arranged')
        dialog.get_by_role('button', name='Approve all', exact=True).click()
        expect(dialog).to_contain_text('0 of 3 done. Stopped at request 2:')
        expect(dialog.get_by_role('button', name='Approve all', exact=True)).to_be_enabled()
        expect(page.locator('#toast-tray .toast', has_text='3 requests approved.')).to_have_count(0)
        current = {i['id']: i['stage'] for i in h.get('approver', '/api/requests/all?type=leave')['items']}
        assert [current[i['id']] for i in submitted] == ['waiting', 'declined', 'waiting']
    finally:
        page.unroute_all(behavior='wait')
        context.close()


@pytest.mark.parametrize('waiting,approved,refresh_first', [pytest.param(130, 0, False, id='130-0'), pytest.param(50, 180, False, id='50-180'),
                                                             pytest.param(50, 180, True, id='50-180-refresh-first')])
def test_bulk_decision_preserves_unloaded_rows_and_undo_when_paging(browser, live_server, h, waiting, approved, refresh_first):
    with transaction(h.app.state.engine, write=True) as db:
        start = datetime.fromisoformat(f'{day()}T08:00:00+03:00')
        made = [m.RequestRecord(department_id=h.seed['department_id'], person_id=h.seed['people'][1], kind='leave',
                               start=start + timedelta(days=n), end=start + timedelta(days=n, hours=4),
                               private_reason='Bulk paging fixture', status='submitted', created_by=h.seed['identities']['coordinator'])
                for n in range(waiting)]
        made.extend(m.RequestRecord(department_id=h.seed['department_id'], person_id=h.seed['people'][1], kind='leave',
                                    start=start - timedelta(days=300 - n), end=start - timedelta(days=300 - n) + timedelta(hours=4),
                                    private_reason='Previously approved fixture', status='approved', created_by=h.seed['identities']['coordinator'])
                    for n in range(approved))
        db.add_all(made)
        db.flush()
        ids = {r.id for r in made}
    context, page = sign_in(browser, live_server, 'approver')
    appends, refreshes = [], []
    draining = False
    def hold_paging(route):
        query = parse_qs(urlparse(route.request.url).query)
        if draining or query.get('status'):
            return route.continue_()
        held = appends if query.get('offset') == ['200'] else refreshes if query.get('offset') == ['0'] else None
        if held is None:
            return route.continue_()
        response = route_fetch(route)
        if draining:
            return route.fulfill(response=response)
        held.append((route, response))
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(rows(page)).to_have_count(100)
        page.get_by_role('checkbox', name='Select all requests shown', exact=True).check()
        decided = min(waiting, 100)
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name=f'Approve {decided}').click()
        dialog = page.get_by_role('dialog', name=f'Approve {decided} requests')
        dialog.get_by_label('Reason', exact=True).fill('Cover arranged for the batch')
        dialog.get_by_role('button', name='Approve all', exact=True).click()
        # The list is read again in its order: waiting first, then the decided rows.
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(min(100, waiting - decided))
        expect(rows(page)).to_have_count(100)
        page.get_by_role('button', name='Show more', exact=True).click()
        expect(rows(page)).to_have_count(200 if approved else 130)
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(waiting - decided)
        shown = rows(page).evaluate_all("els => els.map(el => el.getAttribute('data-row'))")
        assert set(shown) <= ids and len(shown) == len(set(shown))
        page.locator('#toast-tray .toast', has_text=f'{decided} requests approved.').get_by_role('button', name='Undo', exact=True).click()
        expect(rows(page).get_by_text('Waiting for approval', exact=True)).to_have_count(waiting)
        if approved and not refresh_first:
            # A real page refresh overtakes the pending final page. It must preserve the requested 230-row window,
            # rather than capturing the previous 200 rows and replacing Show more's result with that shorter list.
            # (`online` alone reads nothing while the pulse has not moved: reconnect is the background refresh.)
            page.route('**/api/requests/all?*', hold_paging)
            page.get_by_role('button', name='Show more', exact=True).click()
            wait_held(page, appends)
            reconnect(page)
            wait_held(page, refreshes)
            route, response = appends.pop(0)
            route.fulfill(response=response)
            draining = True
            while refreshes:
                route, response = refreshes.pop(0)
                route.fulfill(response=response)
        elif approved:
            # The other order: the refresh is on its way when Show more is pressed on the page it will replace. The
            # redrawn page reads the window Show more asked for too, instead of dropping it with the page it replaces.
            page.route('**/api/requests/all?*', hold_paging)
            reconnect(page)
            wait_held(page, refreshes)
            page.get_by_role('button', name='Show more', exact=True).click()
            wait_held(page, appends)
            draining = True
            while refreshes:
                route, response = refreshes.pop(0)
                route.fulfill(response=response)
            # The redrawn page's read of that window is the same GET as Show more's, still on its way: it shares it
            # (api.ts inflight), so it lands only with Show more's answer. The final count is the redrawn page's: one
            # that dropped the window would end at 200 rows once the page it replaced is gone.
            route, response = appends.pop(0)
            route.fulfill(response=response)
        expect(rows(page)).to_have_count(waiting + approved)
        shown = rows(page).evaluate_all("els => els.map(el => el.getAttribute('data-row'))")
        assert set(shown) == ids and len(shown) == len(set(shown))
        expect(page.get_by_role('button', name='Show more', exact=True)).to_have_count(0)
        expect(page.locator('.app-footer')).to_contain_text(f'{waiting + approved} requests')
        assert h.get('approver', '/api/requests/all?type=leave&status=waiting')['total'] == waiting
    finally:
        draining = True
        while appends or refreshes:
            route, response = (appends or refreshes).pop(0)
            route.fulfill(response=response)
        page.unroute_all(behavior='wait')
        context.close()


def test_an_open_requests_page_shows_others_wishes_and_weekly_availability(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.get_by_role('heading', name='Nothing to review', exact=True)).to_be_visible()
        # Someone else adds a wish and proposes weekly availability: this open page shows both without a reload.
        h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'first_day': day(40), 'last_day': day(40), 'kind': 'prefer_off', 'points': 1})
        expect(rows(page).filter(has_text='Day off')).to_have_count(1, timeout=30000)
        h.command('staff', 'availability_pattern.propose', {'person_id': h.seed['people'][0], 'weekdays': [1], 'start_time': '13:00',
                                                             'end_time': '17:00', 'starts_on': day()})
        expect(rows(page).filter(has_text='Every week')).to_have_count(1, timeout=30000)
        expect(page.locator('#department-nav [data-route=requests] .nav-count')).to_have_text('1', timeout=30000)  # one to approve
    finally:
        context.close()


def test_a_link_filters_on_top_of_saved_filters_without_changing_them(browser, live_server, h):
    r = leave(h)
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Fine'}, r['id'], r['revision'])
    leave(h, on=day(1))
    h.command('staff', 'wish.create', {'person_id': h.seed['people'][0], 'first_day': day(40), 'last_day': day(40), 'kind': 'prefer_off', 'points': 1})
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        filter_by(page.locator('#main'), 'Type', ['Leave'])  # the person's own filter, kept in their view
        expect(rows(page)).to_have_count(2)
        page.evaluate("location.hash='requests?status=waiting'")
        link = page.get_by_role('button', name=re.compile(r'^Remove filter Status: Waiting for approval \(from the link\)'))
        expect(link).to_be_visible(timeout=20000)
        own = page.get_by_role('button', name=re.compile(r'^Filter: Leave \(1 active\)$'))
        expect(own).to_be_visible()  # still there
        expect(rows(page)).to_have_count(1)  # both hold: the waiting leave only
        link.click()
        expect(link).to_have_count(0)
        expect(page).not_to_have_url(re.compile('status='))
        expect(rows(page)).to_have_count(2)  # the saved view again
        page.reload()
        navigate(page, 'requests')
        expect(own).to_be_visible()
        expect(rows(page)).to_have_count(2)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 1024])
def test_profile_requests_are_a_compact_list_with_a_link_to_the_collection(browser, live_server, h, width):
    people = h.get('coordinator', '/api/catalogues')['people']
    b = next(p for p in people if p['id'] == h.seed['people'][1])
    leave(h)  # another person's request: not in this person's list
    second = leave(h, 'coordinator', b['id'], day(1))
    third = leave(h, 'coordinator', b['id'], day(2), 'meeting')
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        main = page.locator('#main [data-table="requests"]')
        navigate(page, 'people?open=' + b['id'])
        panel = page.get_by_role('region', name=b['display_name'], exact=True)
        panel.get_by_role('tab', name='Leave & requests', exact=True).click()
        items = panel.locator('.pr-requests [data-row]')
        expect(items).to_have_count(2)  # this person's only, with type, dates, status and schedule impact
        assert sorted(items.evaluate_all('rows => rows.map(r => r.dataset.row)')) == sorted([second['id'], third['id']])
        expect(items.first.locator('.badge')).to_be_visible()
        expect(panel.locator('.bulk-check, [data-table="person-requests"]')).to_have_count(0)  # no checkboxes or table
        # Nothing clips: the list fits the panel at this width.
        assert panel.evaluate('p => [...p.querySelectorAll(".pr-requests, .pr-requests *")].every(n => n.scrollWidth <= n.clientWidth + 1 || getComputedStyle(n).overflow === "visible")')
        assert panel.evaluate('p => p.querySelector(".pr-requests").getBoundingClientRect().right <= p.getBoundingClientRect().right + 1')
        panel.get_by_role('link', name='Open in Requests →', exact=True).click()
        expect_loaded(page, 'requests')
        expect(main.locator('[data-row]')).to_have_count(2)
        expect(page.get_by_role('button', name='Remove filter Person: ' + b['display_name'] + ' (from the link)', exact=True)).to_be_visible()
    finally:
        context.close()


def test_a_declined_swap_is_open_again_after_undo(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h, days=2)
    a = h.get('staff', '/api/swaps/options')['assignments'][0]
    h.command('staff', 'swap.offer', {'publication_id': a['publication_id'], 'assignment_id': a['assignment_id'], 'recipient_person_id': h.seed['people'][1],
                                      'expires_at': (clock_now() + timedelta(days=1)).isoformat(), 'reason': 'Could we swap?'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests?type=swap')
        row(page, 'Swap').get_by_role('button', name=re.compile('^More actions')).click()
        page.get_by_role('menuitem', name='Decline swap').click()
        dialog = page.get_by_role('dialog', name='Decline swap request')
        dialog.get_by_label('Reason', exact=True).fill('Not this week')
        dialog.get_by_role('button', name='Decline swap', exact=True).click()
        expect(row(page, 'Swap')).to_contain_text('Declined')
        page.locator('#toast-tray .toast', has_text='Swap declined.').get_by_role('button', name='Undo', exact=True).click()
        expect(row(page, 'Swap').locator('.badge')).to_contain_text('Waiting for Jordan Lee')
        expect(rows(page)).to_have_count(1)  # one thread: the reopened swap, not the declined one beside it
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff_two')
    try:
        navigate(page, 'requests?type=swap')
        expect(row(page, 'Swap').get_by_role('button', name='Offer a duty in return', exact=True)).to_be_visible()
        rows(page).first.locator('td').nth(1).click()
        expect(page.get_by_role('region', name=re.compile('Jordan Lee')).locator('.request-history')).to_contain_text('Step undone')
    finally:
        context.close()


def test_saved_view_search_reaches_the_server_after_a_second_render(browser, live_server, h):
    """A page drawn again while the saved views are still on their way still searches with the view's saved search."""
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Saved search person {n:03d}') for n in range(110)]
        db.add_all(made)
        db.flush()
        for n, person in enumerate(made):
            at = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)
            db.add(m.RequestRecord(department_id=h.seed['department_id'], person_id=person.id, kind='leave', start=at,
                                   end=at + timedelta(days=1), private_reason='', status='approved', created_by=h.seed['identities']['coordinator']))
    target = made[-1].display_name
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        search = page.get_by_role('searchbox', name='Search requests', exact=True)
        search.fill(target)
        expect(page.locator('#main tr.dt-open')).to_have_count(1)
        toolbar(page, 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('One person')
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).not_to_be_visible()
        held = []
        page.route('**/api/table-views*', lambda route: held.append(route))  # the saved views are held until the page is drawn again
        page.reload()
        expect_loaded(page, 'requests')
        wait_held(page, held)
        page.evaluate("location.hash = '#requests?again=1'")  # drawn again before the saved views arrive
        expect_loaded(page, 'requests')
        for route in held:
            route.continue_()
        page.unroute('**/api/table-views*')
        expect(search).to_have_value(target)
        expect(page.locator('#main tr.dt-open')).to_have_count(1)
    finally:
        context.close()


def test_recorded_leave_says_its_type_once_on_the_phone_card(browser, live_server, h):
    """The card's title is the leave type ("Annual"); Details never repeats it ("Annual / Details: Annual")."""
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
              'start': f'{day(2)}T09:00:00+03:00', 'end': f'{day(2)}T12:00:00+03:00', 'private_reason': 'Family event'})
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        card = page.locator('#main .dt-card').filter(has=page.locator('.dt-card-title', has_text=re.compile('^Annual'))).first
        expect(card).to_be_visible()
        expect(card).to_contain_text('Dates')
        expect(card).not_to_contain_text(re.compile(r'Details\s*Annual'))
    finally:
        context.close()


@pytest.fixture
def department_scale_request_table(browser, live_server, h):
    """The approver's desktop table of 3,000 requests, grouped by status, with a watch on the rows already drawn."""
    scale_requests(h)
    context, page = sign_in(browser, live_server, 'approver', {'width': 1440, 'height': 900})
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(page.get_by_text('Showing 100 of 3000')).to_be_visible()
        page.evaluate("""() => {window.firstRow = document.querySelector('#main tbody tr[data-row]'); window.removedRows = 0;
          new MutationObserver(list => {for (const m of list) for (const n of m.removedNodes) if (n.dataset?.row) window.removedRows++;})
            .observe(document.querySelector('#main .dt-body'), {childList: true, subtree: true});
          window.longTasks = []; new PerformanceObserver(l => window.longTasks.push(...l.getEntries().map(e => e.duration))).observe({type: 'longtask'});}""")
        yield page
    finally:
        context.close()


def test_a_same_day_request_checks_your_duties_without_an_error(browser, live_server, h):
    """R-01: a timed request inside one day (Other request's default) asks the calendar through the next day, so it says
    whether you are on duty instead of "could not be checked" (the API refuses an empty range with a 422)."""
    context, page = sign_in(browser, live_server, 'staff')
    refused = []
    page.on('response', lambda r: refused.append((r.url, r.status)) if '/api/calendar' in r.url and r.status >= 400 else None)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Other request')
        expect(dialog.get_by_text('No duty of yours on these dates.')).to_be_visible()
        expect(dialog.get_by_text('could not be checked', exact=False)).to_have_count(0)
        assert not refused, refused
    finally:
        context.close()


def show_more(page, shown):
    page.get_by_role('button', name='Show more', exact=True).click()
    expect(page.get_by_text(f'Showing {shown} of 3000')).to_be_visible()
    expect(page.locator('#main tbody tr[data-row]')).to_have_count(shown)


def test_department_scale_show_more_appends_rows(department_scale_request_table):
    """SH-02: Show more adds the next 100 rows to the table; the rows already drawn stay the same nodes."""
    page = department_scale_request_table
    for shown in (200, 300):
        show_more(page, shown)
    assert page.evaluate("window.firstRow.isConnected && window.firstRow === document.querySelector('#main tbody tr[data-row]')")
    assert page.evaluate('window.removedRows') == 0


@pytest.mark.wall_benchmark
def test_department_scale_show_more_cpu_budget(department_scale_request_table):
    """SH-02 budget: at 300 → 400 rows, Show more costs no task over 200 ms (it was 528 ms at 300 rows)."""
    page = department_scale_request_table
    for shown in (200, 300):
        show_more(page, shown)
    page.evaluate('window.longTasks = []')
    show_more(page, 400)
    worst = page.evaluate('Math.max(0, ...window.longTasks)')
    assert worst < 200, f'Show more took a {worst:.0f} ms task'


def test_new_request_is_names_only_and_a_phone_keeps_the_fields_at_the_top(browser, live_server, h):
    """R-03, R-04, R-34: no subtitle or policy sentence per kind; on a phone the chosen kind collapses to one row with
    Change, so Type is on screen; Swap a duty says up front that there is no duty to offer."""
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_text('Choose what you need', exact=False)).to_have_count(0)
        expect(dialog.get_by_text('An approver decides', exact=False)).to_have_count(0)
        swap = dialog.get_by_role('radio', name='Swap a duty')
        expect(swap).to_be_disabled()
        expect(swap).to_have_accessible_description('No upcoming published duty to offer')
        dialog.get_by_role('radio', name='Leave', exact=True).check()
        expect(dialog.get_by_label('Type', exact=True)).to_be_in_viewport()
        expect(dialog.get_by_role('radio', name='Wish')).to_be_hidden()  # the chooser keeps only the chosen kind
        dialog.get_by_role('button', name='Change kind', exact=True).click()  # names what changes
        expect(dialog.get_by_role('radio', name='Wish')).to_be_visible()
        expect(dialog.get_by_role('radio', name='Leave', exact=True)).to_be_focused()
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_decline_many_is_one_armed_dialog_at_every_width(browser, live_server, h, viewport):
    """R-15: Decline N is the same dialog on a desktop and a phone (reason, schedule impact, armed solid-red confirm);
    Approve N takes an optional note, as a single Approve does."""
    for n in range(3):
        leave(h, 'coordinator', h.seed['people'][n % 2], day(n))
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        boxes = page.locator('#main :is(tr.dt-open, .dt-card) input.bulk-check')
        boxes.nth(0).check()
        boxes.nth(1).check()
        bulk = page.get_by_role('toolbar', name='Bulk actions')
        bulk.get_by_role('button', name='Approve 2').click()
        approve = page.get_by_role('dialog', name='Approve 2 requests')
        expect(approve.get_by_label('Reason', exact=True)).not_to_have_attribute('required', '')
        approve.get_by_role('button', name='Cancel', exact=True).click()
        bulk.get_by_role('button', name='Decline 2').click()
        dialog = page.get_by_role('dialog', name='Decline 2 requests')
        expect(dialog.locator('.bulk-sheet-impact')).to_be_visible()
        dialog.get_by_label('Reason', exact=True).fill('Too many people away that week')
        dialog.get_by_role('button', name='Decline 2', exact=True).click()
        confirm = dialog.get_by_role('button', name=re.compile(r'^Confirm decline 2\?'))
        expect(confirm).to_have_class(re.compile(r'\barmed\b'))
        confirm.click()
        expect(page.get_by_text('2 requests declined.')).to_be_visible()
    finally:
        context.close()


def test_your_own_request_reads_as_yours_and_a_decider_keeps_the_decision_in_reach(browser, live_server, h):
    """R-07, R-25, R-27: your own leave does not repeat your name and says you are on duty (not the reviewer's
    "Approving opens 1 place"); the approver's panel drops "An approver decides" and keeps Approve in a sticky footer."""
    published(h)
    made = leave(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, f"requests?open={made['id']}")
        panel = page.locator('.request-panel')
        expect(panel.locator('.request-facts')).to_be_visible()
        expect(panel.locator('.request-facts dt', has_text='Person')).to_have_count(0)
        expect(panel.locator('.side-head small:not(.side-pos)')).not_to_contain_text('Alex Morgan')
        expect(panel.get_by_text(re.compile(r"^You're on duty"))).to_be_visible()
        expect(panel.get_by_text('Approving opens', exact=False)).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, f"requests?open={made['id']}")
        panel = page.locator('.request-panel')
        foot = panel.locator('.request-foot')
        expect(foot.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        assert foot.evaluate('f => getComputedStyle(f).position') == 'sticky'
        expect(panel.locator('.request-status')).not_to_contain_text('An approver decides')
        expect(panel.locator('.request-facts dt', has_text='Person')).to_have_count(1)
    finally:
        context.close()



def test_a_swap_grouped_under_both_people_keeps_its_action_in_each_group(browser, live_server, h):
    """grouped by Person, an accepted swap is drawn under each of its two people; each copy has its own
    Prepare amendment (a cached button node moved to the last group only)."""
    from .test_browser_governance import agreed_swap_pages
    from .governance_support import transition
    swap_automation(h, False, False)
    contexts, errors = [], []
    try:
        _, swap_id, _ = agreed_swap_pages(browser, live_server, h, contexts, errors)
        swap = h.get('staff', '/api/requests/all?type=swap')['items'][0]
        assert swap['id'] == swap_id
        transition(h, swap, 'confirm')
        context, page = sign_in(browser, live_server, 'coordinator'); contexts.append(context)
        navigate(page, 'requests?type=swap')
        expect(rows(page)).not_to_have_count(0)
        # Reviewers' requests start grouped by Status, so Group opens on its level editor (the shared choose()).
        choose(page.locator('#main'), 'Group', 'Group by', 'Person')
        copies = page.locator(f'#main tr[data-row="{swap_id}"]')
        expect(copies).to_have_count(2)
        for i in range(2):
            expect(copies.nth(i).get_by_role('button', name='Prepare amendment', exact=True)).to_have_count(1)
        assert not errors
    finally:
        for c in contexts:
            c.close()


def test_details_that_first_arrive_with_show_more_are_shown(browser, live_server, h):
    """the first page has no details; the next page (Show more) has an approved meeting that still needs
    scheduling. Its Details must show (a column hidden when the table was built stayed hidden)."""
    from .test_browser import route_fetch
    approved = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'meeting',
        'start': f'{day(3)}T09:00:00+03:00', 'end': f'{day(3)}T10:00:00+03:00', 'private_reason': 'Fictional meeting'})['record']
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Fictional approval'}, approved['id'], approved['revision'])
    template = h.get('coordinator', '/api/requests/all?id=' + approved['id'])['item']
    waiting = {**template, 'stage': 'waiting', 'status': 'waiting'}

    def pages(route):
        query = parse_qs(urlparse(route.request.url).query)
        if 'id' in query:
            return route.continue_()
        data = route_fetch(route).json()
        if query.get('offset', ['0']) == ['0']:
            data['items'] = [{**waiting, 'id': f'00000000-0000-4000-8000-{n:012d}'} for n in range(100)]
        else:
            data['items'] = [template]
        first = query.get('offset', ['0']) == ['0']
        data.update(total=101, offset=0 if first else 100, limit=100, has_more=first)
        route.fulfill(json=data)
    context, page = sign_in(browser, live_server, 'coordinator')
    page.route('**/api/requests/all?*', pages)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        expect(rows(page)).to_have_count(100)
        page.get_by_role('button', name='Show more', exact=True).click()
        expect(rows(page)).to_have_count(101)
        expect(page.locator(f'#main tr[data-row="{approved["id"]}"]')).to_contain_text('Scheduling action pending')
    finally:
        context.close()


def test_toast_undo_pointer_click_reverts_wish(browser, live_server, h):
    """LIVE-02 / LIVE-13: the toast's Undo is a real pointer target (at least 24 px) and a mouse click at its centre undoes."""
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Wish')
        dialog.get_by_label('From', exact=True).fill(day(30))
        dialog.get_by_role('button', name='Add wish', exact=True).click()
        undo = page.locator('#toast-tray .toast', has_text='Wish saved').get_by_role('button', name='Undo', exact=True)
        expect(undo).to_be_visible()
        box = undo.bounding_box()
        assert box['width'] >= 24 and box['height'] >= 24, box
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert all(w['status'] == 'withdrawn' for w in h.get('staff', '/api/requests/all?type=wish')['items'])
    finally:
        context.close()


def record_annual(h, person=None, on=None):
    return h.command('coordinator', 'availability.record', {'person_id': person or h.seed['people'][0], 'kind': 'recorded_leave',
                     'leave_type': 'Annual leave', 'start': f'{on or day(2)}T00:00:00+03:00', 'end': f'{on or day(3)}T00:00:00+03:00',
                     'private_reason': 'Family event'})['record']


def colleague(h):
    return next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][1])


def test_import_provenance_not_shown_in_list(browser, live_server, h):
    """LIVE-05: recorded leave says its type once (no "Leave" under "Annual": the icon says it) and a decided row says
    nothing about duties it does not touch (no "No duty affected" on every row)."""
    record_annual(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        annual = row(page, 'Annual')
        expect(annual).to_be_visible()
        expect(annual.locator('.request-title small')).to_have_count(0)
        # The Schedule column is not drawn when no row has an impact; either way the row never says it.
        expect(annual).not_to_contain_text('No duty affected')
    finally:
        context.close()


def test_retract_leave_acts_at_once_with_undo(browser, live_server, h):
    """UX-03 / UX-02, D42: Retract leave record can be undone, so its reason dialog acts at once (no second press)
    and its message offers Undo."""
    record = record_annual(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        page.get_by_role('button', name=re.compile('^More actions for Annual')).first.click()
        page.get_by_role('menuitem', name='Retract leave record', exact=True).click()
        dialog = page.get_by_role('dialog', name='Retract leave record')
        dialog.get_by_role('button', name='Retract', exact=True).click()
        toast = page.locator('#toast-tray .toast', has_text='Record retracted.')
        expect(toast).to_be_visible()
        expect(dialog).to_have_count(0)
        assert h.get('coordinator', '/api/requests/all?id=' + record['id'])['item']['stage'] != 'approved'
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Undone.', exact=False)).to_be_visible()
        assert h.get('coordinator', '/api/requests/all?id=' + record['id'])['item']['stage'] == 'approved'
    finally:
        context.close()


def test_filing_for_someone_else_names_them(browser, live_server, h):
    """LIVE-46: a coordinator filing a request for a colleague reads about that colleague's duties, never "of yours"."""
    other = colleague(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Other request')
        dialog.get_by_label('Person', exact=True).select_option(other['id'])
        expect(dialog.get_by_text(f"{other['display_name']} has no duty on these dates.")).to_be_visible()
        expect(dialog.get_by_text('No duty of yours', exact=False)).to_have_count(0)
    finally:
        context.close()


def test_request_type_change_keeps_person(browser, live_server, h):
    """LIVE-47: the person chosen stays when the kind changes, and Person is the shared searchable people list."""
    other = colleague(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Other request')
        person = dialog.get_by_label('Person', exact=True)
        person.select_option(other['id'])
        expect(person.locator('optgroup')).not_to_have_count(0)  # grouped by rank, as every people choice
        dialog.get_by_role('radio', name=re.compile('^Leave or absence')).check()
        # A short people list is checkboxes (a long one the shared picker's hidden <select multiple>).
        expect(dialog.locator('fieldset[name="People"] input:checked')).to_have_attribute('value', other['id'])
        dialog.get_by_role('radio', name=re.compile('^Other request')).check()
        expect(dialog.get_by_label('Person', exact=True)).to_have_value(other['id'])
    finally:
        context.close()


def test_cleared_search_shows_loading_not_empty(browser, live_server, h):
    """LIVE-48: while the list reads again after a search is cleared, it never says "Nothing to review"."""
    leave(h)
    context, page = sign_in(browser, live_server, 'approver')
    held = []
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        search = page.get_by_role('searchbox', name='Search requests')
        search.fill('zzzz-nothing')
        expect(page.get_by_text('No matching requests')).to_be_visible()
        page.route(re.compile(r'/api/requests/all\?(?!.*q=)'), lambda route: held.append(route))
        search.fill('')
        wait_held(page, held)
        page.set_viewport_size({'width': 1300, 'height': 900})  # any redraw meanwhile
        expect(page.get_by_text('Nothing to review', exact=True)).to_have_count(0)
        for route in held:  # released once each, before unroute: unroute itself continues whatever it still holds
            route.continue_()
        page.unroute(re.compile(r'/api/requests/all\?(?!.*q=)'))
        expect(rows(page)).not_to_have_count(0)
    finally:
        context.close()


def test_overtime_offer_names_uncovered_days_before_sending(browser, live_server, h):
    """LIVE-45: an offer on a day no overtime rule covers is refused in the form, with the day written out; nothing is
    sent, so the footer never says "Not saved"."""
    other = colleague(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    sent = []
    page.on('request', lambda r: sent.append(r.post_data or '') if '/api/commands' in r.url and r.method == 'POST' else None)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Available for overtime')
        dialog.get_by_label('Person', exact=True).select_option(other['id'])
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(dialog.get_by_text(re.compile(r"can’t be offered overtime on \d{1,2} \w{3} \d{4}"))).to_be_visible()
        assert not [body for body in sent if 'request.create' in body]
        expect(page.locator('#save-state')).not_to_contain_text('Not saved')
    finally:
        context.close()


def test_leave_balance_on_phone_and_negative_approval_confirmation(browser, live_server, h):
    from .test_leave_balances import configure, request_between

    configure(h, 0)
    made = request_between(h, day(), day(1))
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'requests')
        expect(page.get_by_text('0 calendar days remaining', exact=False)).to_be_visible()
        no_horizontal_scroll(page)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver', PHONE)
    try:
        navigate(page, f"requests?open={made['id']}")
        panel = page.locator('.request-panel')
        expect(panel.get_by_text('-1 calendar day after approval', exact=False)).to_be_visible()
        panel.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog', name='Approve leave').get_by_role('button', name='Approve', exact=True).click()
        warning = page.get_by_role('dialog', name='Confirm negative leave balance')
        expect(warning.get_by_text('-1 calendar day after approval', exact=False)).to_be_visible()
        no_horizontal_scroll(page)
        warning.get_by_role('button', name='Cancel', exact=True).click()
        expect(panel.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        panel.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog', name='Approve leave').get_by_role('button', name='Approve', exact=True).click()
        warning.get_by_role('button', name='Approve below zero', exact=True).click()
        expect(panel.locator('.request-history')).to_contain_text('Approved below zero')
    finally:
        context.close()
