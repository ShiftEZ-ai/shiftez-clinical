"""UX audit B2 (web): the step a button takes is its name, locking is one click with Undo, lists never keep a typed
search, one way to record leave, the auto-schedule review counts its cards, plain words. Desktop at 1440 and 1024, light
and dark; the employee views on a phone."""
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction

from .test_browser import browser, live_server, navigate, open_rota, open_config, sign_in, reconnect, wait_held, wait_until  # noqa: F401
from .test_browser_rule_catalogue import flag
from .test_leave_types import selected

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP = [({'width': 1440, 'height': 1000}, 'light'), ({'width': 1024, 'height': 900}, 'dark')]
PHONE = {'width': 390, 'height': 844}


def themed(browser, origin, role, viewport, theme):
    context, page = sign_in(browser, origin, role, viewport=viewport)
    page.emulate_media(color_scheme=theme)
    return context, page


def two_sessions(h, name='Lock rota'):
    w = h.workspace(name)
    for day in (4, 5):
        w = h.shift(w, start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00')['workspace']
    return h.lifecycle(w, 'validate')['workspace']


def locked(h, w):
    return [s['locked'] for s in sorted(h.detail(w)['shifts'], key=lambda s: s['start'])]


@pytest.mark.parametrize('viewport,theme', DESKTOP, ids=['1440-light', '1024-dark'])
def test_locking_is_one_click_with_undo_and_shows_where_it_is(browser, live_server, h, viewport, theme):
    w = two_sessions(h)
    context, page = themed(browser, live_server, 'coordinator', viewport, theme)
    try:
        open_rota(page, w['id'])
        # The session panel: Lock, no reason, no impact preview; Undo in the message.
        page.locator(DRAFT_CHIP).first.click()
        panel = page.locator('.wk-panel')
        panel.get_by_role('button', name='Lock', exact=True).click()
        expect(page.locator('dialog.dialog')).to_have_count(0)  # no reason or impact step
        toast = page.locator('.toast').filter(has_text='Session locked')
        expect(toast).to_be_visible()
        lock_mark = page.get_by_role('button', name=re.compile(r'^Unlock .* on 4 Oct 2026'))
        expect(lock_mark).to_be_visible()  # the locked state is on the schedule
        assert locked(h, w) == [True, False]
        toast.get_by_role('button', name='Undo').click()
        expect(lock_mark).to_have_count(0)
        assert locked(h, w) == [False, False]
        # Right-click a person: Lock this session; then the lock mark unlocks it in one click.
        page.locator(DRAFT_CHIP).first.click(button='right')
        page.get_by_role('menuitem', name=re.compile('^Lock this session')).click()
        expect(lock_mark).to_be_visible()
        assert locked(h, w) == [True, False]
        bar = page.locator('#selection-bar')
        before_unlock = bar.get_attribute('data-bar')
        lock_mark.click()
        expect(lock_mark).to_have_count(0)
        assert locked(h, w) == [False, False]
        # The unlock patches the grid before the refresh replaces its selection bar.
        expect(bar).not_to_have_attribute('data-bar', before_unlock)
        # The selection bar never calls a staffed session "open"; Lock there is instant too.
        page.locator('.wk-check').first.check()
        expect(bar).not_to_contain_text('open')
        bar.get_by_role('button', name=re.compile('More')).click()
        page.get_by_role('menuitem', name='Lock').click()
        expect(lock_mark).to_be_visible()
        assert locked(h, w) == [True, False]
        expect(page.locator('dialog.dialog')).to_have_count(0)
    finally:
        context.close()


def test_a_right_click_menu_stays_on_its_session_when_a_refresh_redraws_the_schedule(browser, live_server, h):
    """D-057: a redraw already on its way when a person's right-click menu opens used to close it (its name on the grid
    was replaced). The menu stays open on the same person's place, the focus inside it, and it is filled again for the
    session as it is now: locked elsewhere meanwhile, it offers Unlock, never the Lock it was opened with."""
    w = two_sessions(h, 'Kept menu rota')
    context, page = themed(browser, live_server, 'coordinator', *DESKTOP[0])
    try:
        open_rota(page, w['id'])
        chip = page.locator(DRAFT_CHIP).first
        expect(chip).to_be_visible()
        # A draft opens on its window: its first menu reads it whole; the refresh below then reads the whole rota again.
        chip.click(button='right')
        expect(page.get_by_role('menu')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('menu')).to_have_count(0)
        held = []
        draft = re.compile(rf"/api/workspaces/{w['id']}(\?.*)?$")
        page.route(draft, lambda route: held.append(route))
        # Someone else locks the first session; this page's background refresh is reading the rota when the menu opens.
        now = h.detail(w)
        first = sorted(now['shifts'], key=lambda s: s['start'])[0]
        h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [first['id']], 'action': 'lock',
                                                  'reason': 'Locked elsewhere'}, None, now['workspace']['revision'])
        reconnect(page)
        wait_held(page, held)
        chip.click(button='right')
        menu = page.get_by_role('menu')
        expect(menu.get_by_role('menuitem', name=re.compile('^Lock this session'))).to_be_visible()
        chip.evaluate('b => { window.fixtureOldChip = b; }')
        for route in held:  # continue first: removing the last interceptor resumes held routes (Playwright 1.63+, test_browser.Held)
            route.continue_()
        page.unroute(draft)
        wait_until(page, lambda: page.evaluate('!window.fixtureOldChip.isConnected'), 'the schedule was never redrawn')
        unlock = menu.get_by_role('menuitem', name=re.compile('^Unlock this session'))
        expect(unlock).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile('^Lock this session'))).to_have_count(0)
        expect(menu.locator(':focus')).to_have_count(1)  # the focus stays in the menu
        expect(chip).to_have_attribute('aria-expanded', 'true')  # the name now on the grid
        unlock.click()
        expect(page.get_by_role('button', name=re.compile(r'^Unlock .* on 4 Oct 2026'))).to_have_count(0)
        assert locked(h, w) == [False, False]
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', DESKTOP, ids=['1440-light', '1024-dark'])
def test_draft_button_says_submit_for_review_and_the_logo_goes_home(browser, live_server, h, viewport, theme):
    w = two_sessions(h, 'Submit rota')
    context, page = themed(browser, live_server, 'coordinator', viewport, theme)
    try:
        open_rota(page, w['id'])
        expect(page.get_by_role('button', name='Publish…', exact=True)).to_have_count(0)
        page.get_by_role('button', name='Submit for review…', exact=True).click()
        dialog = page.get_by_role('dialog', name='Submit Submit rota for review')
        expect(dialog.get_by_role('button', name='Submit for review', exact=True)).to_be_visible()
        # One way out of a dialog besides Cancel: the × (no second "Close" word).
        expect(dialog.get_by_role('button', name='Close dialog')).to_have_text('')
        dialog.get_by_role('button', name='Close dialog').click()
        expect(dialog).to_have_count(0)
        page.get_by_role('link', name=re.compile('^Home')).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
    finally:
        context.close()


def test_lists_never_keep_a_typed_search_and_say_when_rows_are_hidden(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'people')
        table = page.locator('#main .dt').first
        strip = table.locator('.dt-filtered')
        expect(strip).to_be_hidden()
        table.get_by_role('searchbox').first.fill('Staff 01')
        expect(strip).to_contain_text(re.compile(r'Filtered: 1 of \d+ people shown'))
        strip.get_by_role('button', name='Show all').click()
        expect(strip).to_be_hidden()
        expect(table.get_by_role('searchbox').first).to_have_value('')
        table.get_by_role('searchbox').first.fill('Staff 01')
        page.reload()
        navigate(page, 'people')
        expect(page.locator('#main .dt').first.get_by_role('searchbox').first).to_have_value('')
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', DESKTOP, ids=['1440-light', '1024-dark'])
def test_coordinator_records_leave_in_one_place_with_a_type_and_dates_that_follow(browser, live_server, h, viewport, theme):
    context, page = themed(browser, live_server, 'coordinator', viewport, theme)
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_role('radio', name=re.compile('^Leave An approver'))).to_have_count(0)  # one way in
        dialog.get_by_role('radio', name=re.compile('^Leave or absence')).check()
        dialog.get_by_label('Staff 01(test)', exact=True).check()
        expect(dialog.get_by_label('Type', exact=True)).to_be_visible()  # the one Type picker (maintenance), no free text
        dialog.get_by_label('Type', exact=True).select_option(label='Annual')
        dialog.get_by_label('All day').uncheck()
        start = dialog.get_by_label('Unavailable from', exact=True)
        start.fill('2026-10-12T08:00')
        start.press('Tab')
        expect(dialog.get_by_label('Unavailable until', exact=True)).to_have_value('Mon 12 Oct 2026')  # the end follows
        dialog.get_by_label('Unavailable until', exact=True).fill('2026-10-14T16:00')
        dialog.get_by_label('All day').check()
        expect(dialog.get_by_label('First day', exact=True)).to_have_value('Mon 12 Oct 2026')  # typed dates kept
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Wed 14 Oct 2026')
        dialog.get_by_label('Reason', exact=True).fill('Family trip')
        dialog.get_by_role('button', name='Record leave', exact=True).click()
        expect(dialog).to_have_count(0)
        with transaction(h.app.state.engine) as db:
            row = db.query(m.Availability).filter(m.Availability.person_id == h.seed['people'][0]).one()
            assert row.leave_type_id == selected(h, 'ANNUAL')['id'] and row.kind == 'recorded_leave'
    finally:
        context.close()


def test_own_request_says_someone_else_decides(browser, live_server, h):
    department = h.get('staff', '/api/me')['department']['id']
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RoleBinding(identity_id=h.seed['identities']['staff'], department_id=department, role='approver', starts_on=date(2020, 1, 1)))
    made = h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave', 'start': '2026-10-20T00:00:00+03:00',
                                                 'end': '2026-10-21T00:00:00+03:00', 'private_reason': 'Own leave'})['record']
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE)
    try:
        navigate(page, f"requests?open={made['id']}")
        panel = page.locator('.request-panel')
        expect(panel).to_contain_text("Another approver decides: you can't approve your own request")
        expect(panel.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_staff_see_approved_leave_on_their_published_duty(browser, live_server, h, theme):
    w = h.shift(h.workspace('Published with leave'), start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    h.publish(w)
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave', 'start': '2026-10-05T00:00:00+03:00',
                                                     'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Approved leave'})
    context, page = themed(browser, live_server, 'staff', PHONE, theme)
    try:
        navigate(page, 'schedule/week/2026-10-05')
        expect(page.locator('.ag-item.mine').first).to_contain_text('Leave approved · cover being arranged', timeout=15000)
    finally:
        context.close()


def test_resolve_issues_offers_find_someone_for_an_open_place(browser, live_server, h):
    w = h.lifecycle(h.shift(h.workspace('Gap rota'), people=[])['workspace'], 'validate')['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/issues?draft={w['id']}&view=checks")
        page.locator('#main tbody tr[data-row]').first.get_by_role('checkbox').check()
        bar = page.get_by_role('toolbar', name='Bulk actions')
        expect(bar.get_by_role('button', name='Replace…')).to_have_count(0)
        bar.get_by_role('button', name='Find someone…').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page.locator('.wk-panel')).to_be_visible(timeout=15000)
    finally:
        context.close()


def test_new_draft_after_a_whole_month_is_the_next_whole_month(browser, live_server, h):
    h.workspace('October', start='2026-10-01', end='2026-11-01')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule')
        page.get_by_role('button', name=re.compile('^(New rota|Switch draft · |Choose a draft)')).first.click()
        if not page.get_by_role('dialog', name='New rota', exact=True).count():
            page.get_by_role('dialog', name='Drafts', exact=True).get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        expect(dialog.get_by_label('First day', exact=True)).to_have_value('Sun 1 Nov 2026')
        expect(dialog.get_by_label('Last day', exact=True)).to_have_value('Mon 30 Nov 2026')
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', DESKTOP, ids=['1440-light', '1024-dark'])
def test_opening_a_rule_changes_nothing_until_turned_on(browser, live_server, h, viewport, theme):
    flag(h)
    context, page = themed(browser, live_server, 'coordinator', viewport, theme)
    try:
        navigate(page, 'rules/duties_per_week')
        summary = page.locator('.rc-summary')
        expect(summary).to_contain_text('Off')
        footer = page.locator('.app-footer')
        expect(footer).to_contain_text('0 lines')  # U-21: the line count is the footer's, not the header's
        propose = page.get_by_role('button', name='Propose', exact=True)
        expect(propose).to_be_disabled()
        expect(propose).to_have_attribute('data-tip', 'Nothing to propose yet: change a line first.')
        summary.get_by_role('switch').check()
        expect(summary).to_contain_text('On')
        expect(footer).to_contain_text('1 line')  # the count follows the draft
        expect(page.get_by_role('button', name='Propose 2 changes', exact=True)).to_be_enabled()
    finally:
        context.close()


def test_auto_schedule_settings_keep_priorities_apart_from_advanced(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        # How it decides holds the score weights auto-schedule optimises; search tuning is under Advanced.
        expect(pane.get_by_role('tab', name='How it decides', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(pane.get_by_label('Coverage weight', exact=True)).to_be_visible()
        expect(pane.get_by_label('People tried first for each place', exact=True)).to_be_hidden()
        expect(pane).not_to_contain_text('Tolerance')
        expect(pane.get_by_label('First priority', exact=True)).to_have_count(0)
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        expect(pane.get_by_label('People tried first for each place', exact=True)).to_be_visible()
        expect(pane.get_by_label('Coverage weight', exact=True)).to_be_hidden()
        assert page.evaluate('location.hash') == '#configuration/auto_schedule'
        open_config(page, 'Opening hours')
        assert page.evaluate('location.hash') == '#configuration/opening-hours'  # the address says the section's name
    finally:
        context.close()
