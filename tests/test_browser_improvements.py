"""End-to-end journeys for the planner, calendar and setup improvements (real browser, API and database)."""
import re
from urllib.parse import quote
import pytest
from playwright.sync_api import expect
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import transaction
from .test_browser_cp_sat import _joins, _check_after_apply
from .test_browser import auto_setup, auto_result, route_fetch, wait_run_created, browser, live_server, sign_in, navigate, open_scheduling, open_rota, planner_tool  # noqa: F401

pytestmark = pytest.mark.browser


def test_choose_which_suggestions_to_apply_then_undo(browser, live_server, h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Choose suggestions'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()  # drawn as soon as Run is pressed
        wait_run_created(page)  # the worker can only take the run once job.create has answered
        process_one(h.app.state.engine)
        page.get_by_role('button', name='Review latest result', exact=True).click(timeout=15000)
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        rows = dialog.locator('.dt-table tbody tr')
        expect(rows).to_have_count(2)
        rows.nth(0).locator('input.bulk-check').uncheck()
        joined = _joins(page)
        dialog.get_by_role('button', name='Apply chosen (1 of 2)', exact=True).click()
        expect(page.get_by_text('Suggestions applied to the draft.')).to_be_visible()
        _check_after_apply(page, h, w['id'], joined)  # the worker finishes the complete check Apply queued (BE-18)
        assert sum(bool(s['participants']) for s in h.detail(w)['shifts']) == 1
        # The applied result counts what was chosen and saved, not every suggestion (DEV: 449 of 461 read "461 added").
        expect(page.get_by_text('1 added · 0 removed', exact=True)).to_be_visible()
        expect(page.get_by_text('2 added', exact=False)).to_have_count(0)
        # After a reload the count comes from the stored result (server's applied record), not this browser's memory.
        page.reload()
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        auto_result(page.locator('#auto-panel'))
        expect(page.get_by_text('1 added · 0 removed', exact=True)).to_be_visible()
        expect(page.get_by_text('2 added', exact=False)).to_have_count(0)
    finally:
        context.close()


def test_chosen_apply_then_undo_restores_the_draft(browser, live_server, h):
    from apps.worker.main import process_one
    w = h.shift(h.workspace('Choose then undo'), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        page.get_by_role('button', name='Run auto-schedule', exact=True).click()
        expect(page.locator('#auto-panel').get_by_role('progressbar')).to_be_visible()
        wait_run_created(page)
        process_one(h.app.state.engine)
        page.get_by_role('button', name='Review latest result', exact=True).click(timeout=15000)
        dialog = page.get_by_role('dialog', name='Auto-schedule results')
        dialog.locator('.dt-table tbody tr').nth(0).locator('input.bulk-check').uncheck()
        joined = _joins(page)
        dialog.get_by_role('button', name='Apply chosen (1 of 2)', exact=True).click()
        expect(page.get_by_text('Suggestions applied to the draft.')).to_be_visible()
        _check_after_apply(page, h, w['id'], joined)  # the worker finishes the complete check Apply queued (BE-18)
        # Draft edits save straight away; Undo puts the previous arrangement back.
        page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        expect(page.get_by_text('Undone.', exact=False)).to_be_visible()
        assert not any(s['participants'] for s in h.detail(w)['shifts'])
    finally:
        context.close()


def test_resolve_walks_through_open_places(browser, live_server, h):
    w = h.shift(h.workspace('Resolve rota'), people=[])['workspace']
    h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        planner_tool(page, 'Resolve problems one by one')
        panel = page.locator('.wk-panel')
        expect(panel.locator('.resolve-bar')).to_contain_text('1 to resolve')
        panel.get_by_role('button', name=re.compile(r'^Assign (?!.* with a warning$)')).first.click()
        expect(page.get_by_text('Everything is resolved', exact=False)).to_be_visible(timeout=15000)
        assert h.detail(w)['shifts'][0]['participants']
    finally:
        context.close()


def test_arrow_keys_move_between_people_in_the_week(browser, live_server, h):
    a, b = h.seed['people'][:2]
    w = h.shift(h.workspace('Keyboard rota'), people=[a])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[b])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        chips = page.locator('.wk-grid button.pchip')
        expect(chips).to_have_count(2)
        chips.nth(0).focus()
        # The grid is one tab stop (S-17): arrows reach every control in it, so the session's own select box comes
        # first, then the next day's name.
        page.keyboard.press('ArrowRight')
        expect(page.locator('.wk-grid .wk-check').first).to_be_focused()
        page.keyboard.press('ArrowRight')
        expect(chips.nth(1)).to_be_focused()
        page.keyboard.press('ArrowLeft')
        page.keyboard.press('ArrowLeft')
        expect(chips.nth(0)).to_be_focused()
    finally:
        context.close()


def test_long_rota_is_scheduled_a_month_at_a_time(browser, live_server, h):
    w = h.workspace('Two month rota', '2026-10-04', '2026-12-04')
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    w = h.shift(w, start='2026-11-20T08:00:00+03:00', end='2026-11-20T12:00:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_scheduling(page)
        open_rota(page, w['id'])
        # It opens on the first month with an open place; a longer range is refused before anything runs.
        panel = page.locator('#auto-panel')
        auto_setup(panel)  # D76: the setup's fields open under Change
        expect(panel.get_by_label('From', exact=True)).to_have_value('Sun 4 Oct 2026')
        expect(panel.get_by_label('To', exact=True)).to_have_value('Sat 31 Oct 2026')
        panel.get_by_label('To', exact=True).fill('2026-12-03')
        # Said before Run is pressed (SR-28): Run is off and the panel says why.
        expect(panel.get_by_role('button', name='Run auto-schedule', exact=True)).to_be_disabled()
        expect(panel.locator('.auto-why')).to_have_text('Choose at most 31 days at a time.')
        assert h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'] == []
    finally:
        context.close()


def test_staff_subscribe_to_their_calendar_and_stop_sharing(browser, live_server, h):
    me = h.get('staff', '/api/me')['person_id']
    assert me == h.seed['people'][0], 'Seeded staff account must be linked to its person'
    h.publish(h.shift(h.workspace('Subscribed rota'), people=[me])['workspace'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page,'insights/exports')  # the subscription lives in Insights › Exports (and Home › Add to my calendar)
        page.get_by_role('button', name='Subscribe in my calendar', exact=True).click()
        link = page.get_by_label('Your private calendar link', exact=True)
        expect(link).to_have_value(re.compile(r'/calendar/[\w-]+\.ics$'))
        url = link.input_value()
        assert page.request.get(url).text().count('BEGIN:VEVENT') == 1
        # One tap per calendar app, each carrying the same private link.
        webcal = re.sub(r'^https?:', 'webcal:', url)
        expect(page.get_by_role('link', name='Apple or Outlook app', exact=True)).to_have_attribute('href', webcal)
        expect(page.get_by_role('link', name='Google Calendar', exact=True)).to_have_attribute('href', 'https://calendar.google.com/calendar/r?cid=' + quote(webcal, safe=''))
        expect(page.get_by_role('link', name='Outlook on the web', exact=True)).to_have_attribute('href', re.compile(r'^https://outlook\.office\.com/calendar/0/addfromweb\?url=' + re.escape(quote(url, safe=''))))
        # Same-button confirm in the link's ⋯ menu: the first click arms it, the second stops sharing.
        page.get_by_role('button', name='More link actions', exact=True).click()
        page.get_by_role('menuitem', name='Stop sharing', exact=True).click()
        page.get_by_role('menuitem', name='Confirm? Stop sharing', exact=True).click()
        expect(page.get_by_text('Calendar link stopped.')).to_be_visible()
        assert h.get('staff', '/api/calendar-feed')['active'] is False
    finally:
        context.close()


def test_calendar_link_lost_response_retries_without_disclosing_secret(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    lost = []

    def lose_first_create(route):
        if route.request.post_data_json['kind'] == 'calendar_feed.create' and not lost:
            response = route_fetch(route)
            assert response.status == 200
            lost.append(route.request.headers['idempotency-key'])
            route.abort('failed')  # the write committed; only its response was lost
        else:
            route.continue_()

    page.route('**/api/commands', lose_first_create)
    try:
        navigate(page, 'insights/exports')
        create = page.get_by_role('button', name='Subscribe in my calendar', exact=True)
        create.click()  # maintenance: sent again with the same key, it recovers the original committed outcome (one feed)
        expect(page.get_by_text('Your previous link cannot be shown again. Choose ⋯ › New link to replace it.', exact=True)).to_be_visible()
        expect(page.get_by_label('Your private calendar link', exact=True)).to_have_count(0)
        with transaction(h.app.state.engine) as db:
            assert len(list(db.scalars(select(m.CalendarFeed)))) == 1
        # Replacing the link stops the old one: it asks with the same-button confirm.
        page.get_by_role('button', name='More link actions', exact=True).click()
        page.get_by_role('menuitem', name='New link', exact=True).click()
        page.get_by_role('menuitem', name='Confirm? New link', exact=True).click()
        link = page.get_by_label('Your private calendar link', exact=True)
        expect(link).to_have_value(re.compile(r'/calendar/[\w-]+\.ics$'))
        assert page.request.get(link.input_value()).status == 200
        with transaction(h.app.state.engine) as db:
            feeds = list(db.scalars(select(m.CalendarFeed)))
            assert len(feeds) == 2 and sum(not feed.revoked for feed in feeds) == 1
    finally:
        context.close()


def test_setup_guide_lists_what_is_missing_with_a_fix(browser, live_server, h):
    h.configure('people', {'display_name': 'Unranked Newcomer'})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page,'configuration/setup')
        health = page.locator('.setup-health')
        expect(health).to_contain_text('People without a current rank')
        expect(health).to_contain_text('Unranked Newcomer')
        health.locator('li').filter(has_text='without a current rank').get_by_role('button', name='Show them').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'people')
        # Opens People filtered to exactly those people.
        expect(page.locator('.dt-table tbody tr')).to_have_count(1)
        expect(page.locator('.dt-table tbody')).to_contain_text('Unranked Newcomer')
    finally:
        context.close()


def test_add_a_rule_for_this_rota_only(browser, live_server, h):
    from .test_browser import save_dialog
    w = h.shift(h.workspace('Own rules rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'planner')
        open_rota(page, w['id'])
        planner_tool(page, 'Rules in this rota')
        page.get_by_role('button', name='Add a rule for this rota', exact=True).click()
        form = page.get_by_role('dialog')
        expect(form.get_by_label('Rule title', exact=True)).to_have_value('Own rules rota · ')
        form.get_by_label('Rule title', exact=True).fill('Own rules rota · rest')
        form.get_by_label('Source or departmental decision', exact=True).fill('Agreed for this period only')
        form.get_by_role('radio', name='Minimum rest', exact=True).check()
        save_dialog(form, 'Propose rule')
        rule = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == 'Own rules rota · rest')
        assert rule['definition']['workspace_id'] == w['id'] and rule['starts_on'] == w['starts_on']
    finally:
        context.close()


PHONE_CUT_OFF = """() => [...document.querySelectorAll('.topbar *, main *')].filter(e => {
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  return r.width && r.height && s.visibility !== 'hidden' && (r.right > innerWidth + 1 || r.left < -1)
    && !e.closest('[class*=scroll],.wk-grid,.month-grid,.dt-table,table,.rule-tabs,.rotation-grid,details:not([open]),.page-planner .view-legend');  // the schedule's key scrolls sideways by design
}).slice(0, 3).map(e => e.tagName + '.' + String(e.className).slice(0, 40) + ' ' + (e.textContent || '').trim().slice(0, 30))"""


@pytest.mark.parametrize('role', ['coordinator', 'staff'])
def test_nothing_is_cut_off_on_a_phone(browser, live_server, h, role):
    me = h.get('staff', '/api/me')['person_id']
    h.publish(h.shift(h.workspace('Phone rota'), people=[me] if me else None)['workspace'])
    h.shift(h.workspace('Phone draft', '2026-10-11', '2026-10-18'), start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00', people=[])
    pages = {'coordinator': ['overview', 'today', 'planner', 'timeline', 'published', 'people', 'directory', 'rules', 'availability', 'swaps', 'reports', 'data', 'calendar'],
             'staff': ['overview', 'today', 'timeline', 'published', 'availability', 'swaps', 'reports', 'calendar']}[role]
    context, page = sign_in(browser, live_server, role)
    try:
        page.set_viewport_size({'width': 375, 'height': 812})
        cut = {}
        for view in pages:
            navigate(page, view)
            page.wait_for_timeout(500)
            found = page.evaluate(PHONE_CUT_OFF)
            if found:
                cut[view] = found
        assert not cut, cut
    finally:
        context.close()


def test_leaving_the_planner_quickly_keeps_the_new_pages_top_bar(browser, live_server, h):
    h.shift(h.workspace('Race rota'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.set_viewport_size({'width': 390, 'height': 844})
        # Straight to the planner and away again before its data arrives.
        page.evaluate("location.hash='planner'; setTimeout(() => location.hash = 'overview', 0)")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'home')
        page.wait_for_timeout(1500)
        expect(page.locator('.topbar .planner-bar')).to_have_count(0)
        expect(page.locator('.topbar h1')).to_contain_text('Good')
    finally:
        context.close()


def test_messages_about_a_page_clear_when_you_leave_it(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        # A plain message, then a message that offers Undo.
        page.evaluate("""() => { const tray = document.querySelector('#toast-tray') || document.body.appendChild(Object.assign(document.createElement('div'), {id: 'toast-tray'}));
          tray.append(Object.assign(document.createElement('div'), {className: 'toast success', textContent: 'Saved on rules'}));
          tray.append(Object.assign(document.createElement('div'), {className: 'toast success with-action', textContent: 'Undo me'})); }""")
        navigate(page,'home')
        expect(page.locator('.toast', has_text='Saved on rules')).to_have_count(0)
        expect(page.locator('.toast', has_text='Undo me')).to_have_count(1)
    finally:
        context.close()


def test_empty_calendar_week_offers_the_next_scheduled_day(browser, live_server, h):
    """The schedule is one continuous timeline: the Year view shows where the next published day is and opens its week."""
    h.publish(h.shift(h.workspace('Later rota', '2026-11-29', '2026-12-06'), start='2026-12-01T08:00:00+03:00', end='2026-12-01T12:00:00+03:00')['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'schedule/week/2026-11-15?edit=none')
        expect(page.get_by_text('No sessions', exact=False).first).to_be_visible()
        navigate(page, 'schedule/year/2026-11-15?edit=none')
        day = page.locator('.sd-yday[aria-label^="1 Dec 2026:"]')
        expect(day).to_have_attribute('aria-label', re.compile('Later rota, published, 1 session'))
        expect(page.locator('.sd-yday[aria-label^="15 Nov 2026:"]')).to_have_attribute('aria-label', re.compile('not planned'))
        day.click()
        page.locator('.sd-panel').get_by_role('button', name='Open in Week').click()
        expect(page).to_have_url(re.compile('#schedule/week/2026-12-01'))
        expect(page.locator('#schedule-view')).to_have_attribute('aria-label', 'Week view')
    finally:
        context.close()
