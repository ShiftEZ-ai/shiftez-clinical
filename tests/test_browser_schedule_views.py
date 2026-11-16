"""Schedule views: one empty state, double bookings said on both sides, clean day headers, the tools menu, steps that
never get lost, staff's own duties without planner maths (frontend audit S-01, S-02, S-05, S-07, S-08, S-10, S-16, S-19,
S-21, S-26, S-31, S-33, S-34, S-35); every view draws rows a person can see and click, with a draft open and
with published days only (S-14)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, route_fetch, wait_held, save_dialog, planner_tool, planner_filter, filter_pick, navigate, view_settings, choose_radio, close_popover  # noqa: F401
from datetime import date, timedelta

from .test_browser_schedule import at, checked, october, show, settled, today
from .test_browser_qa_planner import clinic, settled as planner_settled

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def booked(h):
    """A checked draft, 18-24 Oct, where Alex works two sessions at the same time on 20 Oct."""
    a = h.seed['people'][0]
    d = h.shift(h.workspace('Clash draft', '2026-10-18', '2026-10-25'), *at(20), people=[a])['workspace']
    d = h.shift(d, *at(20, '09', '13'), people=[a])['workspace']
    return checked(h, d)


def view(page, name='Week'):
    return page.get_by_role('region', name=f'{name} view')


def test_every_view_says_nothing_here_the_same_way_with_its_next_step(browser, live_server, h):
    """S-01 / S-02: one empty card in every view: a draft with no sessions offers Add session; the filters hiding
    everything offer Show all; with no draft a coordinator is offered New rota. Month shows its days only."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        # The draft runs to 20 Oct; its last days have no sessions.
        show(page, f"schedule/week/2026-10-18?edit={d['id']}")
        card = view(page).locator('.empty-state')
        expect(card).to_contain_text('No sessions this week')
        card.get_by_role('button', name='Add session', exact=True).click()
        form = page.get_by_role('region', name='Add session')
        expect(form).to_be_visible()
        form.get_by_role('button', name='Cancel', exact=True).click()
        expect(form).to_have_count(0)
        # A search matching nothing: hidden by the filters, and one press shows everything again.
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        search = page.get_by_role('searchbox').first
        search.fill('zzzz-nothing')
        expect(view(page).locator('.empty-state')).to_contain_text('Hidden by filters')
        view(page).get_by_role('button', name='Show all', exact=True).click()
        settled(page)
        expect(view(page).locator('.empty-state')).to_have_count(0)
        expect(page.get_by_role('searchbox').first).to_have_value('')
        # The same card in the Coverage view.
        show(page, f"schedule/coverage/2026-10-18?edit={d['id']}")
        expect(view(page, 'Coverage').locator('.empty-state')).to_contain_text('No sessions this week')
        # No draft: a coordinator is offered New rota.
        show(page, 'schedule/week/2026-12-09?edit=none')
        expect(view(page).locator('.empty-state').get_by_role('button', name='New rota', exact=True)).to_be_visible()
        # Month (SW-26, b28682108): the day grid stays, with the same one card under it (never above it) and its next
        # step; its corner names the row axis.
        show(page, 'schedule/month/2026-12-09?edit=none')
        month = view(page, 'Month')
        expect(month.locator('.empty-state')).to_have_count(1)
        card = month.locator('.mo-wk ~ .mo-none .empty-state')
        expect(card).to_contain_text('No sessions this month')
        expect(card.get_by_role('button', name='New rota', exact=True)).to_be_visible()
        expect(month.locator('.wk-corner').first).to_have_text('Week')
    finally:
        context.close()


def test_a_double_booking_is_said_on_both_sessions_everywhere(browser, live_server, h):
    """S-07 / S-08 / S-35: both sessions carry the mark (24px tall); the person's hover card leads with it; the List says
    "Double-booked", never "Covered"; the Coverage cell shows the issue and has no day-column menus."""
    d = booked(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-20?edit={d['id']}")
        marks = view(page).locator('.clash-mark')
        expect(marks).to_have_count(2)
        assert all(m.bounding_box()['height'] >= 24 for m in marks.all())
        chip = view(page).locator('button.pchip:not(.gap):not(.sd-greychip)').first
        chip.hover()
        card = page.locator('.hovercard').filter(has_text='Double-booked').first
        expect(card).to_contain_text('also in')
        show(page, f"schedule/list/2026-10-20?edit={d['id']}")
        statuses = view(page, 'List').locator('tbody tr')
        expect(statuses.filter(has_text='Double-booked')).to_have_count(2)
        expect(statuses.filter(has_text='Covered')).to_have_count(0)
        show(page, f"schedule/coverage/2026-10-20?edit={d['id']}")
        cov = view(page, 'Coverage')
        expect(cov.locator('button.cov-cell.issue').first).to_be_visible()
        expect(cov.locator('button.cov-cell.issue').first).to_have_attribute('aria-label', re.compile('must fix'))  # UIUX L5: the shared words
        expect(cov.locator('button.cov-cell.issue .iss-mark.error').first).to_be_visible()  # the shared D78 mark, not a "!"
        expect(cov.locator('thead .dt-column-options')).to_have_count(0)  # a fixed day matrix: no column menus
        # Day: both bars count the clash as an issue.
        show(page, f"schedule/day/2026-10-20?edit={d['id']}")
        expect(view(page, 'Day').locator('.sd-issue')).to_have_count(2)
    finally:
        context.close()


def test_day_headers_tools_menu_and_footer_say_each_thing_once(browser, live_server, h):
    """S-16 / S-02 / S-05 / S-31 / S-33 / S-10: published days are grey without the word on each; the corner names the
    rows; the Rota tools menu shows whole (no inner scroll, no Undo copy) and its Delete draft or Archive entry shows whole; the
    footer does not repeat the period and zone; the session panel does not repeat its ring."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 1000})
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        grid = view(page).locator('.wk-grid')
        expect(grid.locator('.wk-head .wk-day').filter(has_text='Published')).to_have_count(0)
        expect(grid.get_by_role('columnheader', name=re.compile('published, read-only$'))).not_to_have_count(0)
        expect(grid.locator('.wk-corner').first).to_have_text('Service')
        expect(page.locator('.app-footer')).not_to_contain_text('Asia/Riyadh')
        page.get_by_role('button', name='Rota tools', exact=True).click()
        menu = page.get_by_role('menu', name='Rota tools')
        expect(menu).to_be_visible()
        expect(menu.get_by_role('menuitem', name=re.compile('^Undo last edit'))).to_have_count(0)
        discard = menu.get_by_role('menuitem', name=re.compile('^(Delete draft|Archive)…'))
        expect(discard).to_be_in_viewport(ratio=1)
        assert menu.evaluate('m => m.scrollHeight <= m.clientHeight + 1')
        page.keyboard.press('Escape')
    finally:
        context.close()


def test_two_quick_steps_move_two_weeks_while_the_first_still_loads(browser, live_server, h):
    """S-26: › pressed twice while the next week's sessions load moves two weeks, and the view says it is busy."""
    october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-10-07?edit=none')
        held = []
        page.route('**/api/schedule/sessions**', lambda route: held.append(route) if not held else route.continue_())
        label = page.get_by_role('group', name='Schedule').locator('strong').first
        page.get_by_role('button', name='Next week', exact=True).click()
        wait_held(page, held)
        expect(page.locator('#schedule-view')).to_have_attribute('aria-busy', 'true')
        page.get_by_role('button', name='Next week', exact=True).click()
        try:  # the first week's read may have been given up (the page moved on): it only has to let go
            held[0].fulfill(response=route_fetch(held[0]))
        except Exception:
            pass
        page.unroute('**/api/schedule/sessions**')
        expect(label).to_have_text('Sun 18 Oct – Sat 24 Oct')
        settled(page)
        assert '2026-10-21' in page.evaluate('location.hash')
    finally:
        context.close()


def test_staff_see_their_own_duties_without_planner_maths(browser, live_server, h):
    """S-19 / S-21 / S-34: staff's own schedule has no filled/needed tallies or bars (partial data); the phone agenda does
    not say "Your duty" or "You" on every item and marks today; no unexplained person colour."""
    october(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        show(page, 'schedule/week/2026-10-07')
        grid = view(page).locator('.wk-grid')
        expect(grid).to_be_visible()
        expect(grid.locator('.wk-cov, .wk-bar, .wk-sum')).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        show(page, 'schedule/week/2026-10-07')
        agenda = view(page).locator('.ag')
        expect(agenda).to_be_visible()
        expect(agenda).not_to_contain_text('Your duty')
        expect(agenda.locator('.ag-item')).not_to_contain_text(re.compile(r'\bYou\b'))
    finally:
        context.close()


def test_copy_picks_a_target_day_with_presets_and_an_optional_note(browser, live_server, h):
    """S-12 / S-13: Copy asks for the first day of the copies (date picker and presets), not a day offset; Exchange has
    one plain line and says Exchange. Both keep their typed reason (OD-17 is the owner's)."""
    _, d = october(h)
    before = len([s for s in h.detail(d)['shifts'] if s['start'].startswith('2026-10-16')])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        view(page).locator('.wk-check').first.check()
        bar = page.locator('#selection-bar')
        copy = bar.get_by_role('button', name='Copy', exact=True)
        if copy.count():
            copy.click()
        else:
            bar.get_by_role('button', name=re.compile('More')).click()
            page.get_by_role('menuitem', name='Copy', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Copy 1 session to'))
        first = dialog.get_by_label('First day of the copies', exact=True)  # the shared date picker shows the day
        expect(first).to_have_value('Wed 21 Oct 2026')
        dialog.get_by_role('button', name='+2 weeks', exact=True).click()
        expect(first).to_have_value('Wed 28 Oct 2026')
        expect(dialog.get_by_label('Day offset')).to_have_count(0)
        # OD-17: an undoable draft edit takes an optional note; left empty, the history records the routine reason.
        expect(dialog.get_by_label('Change reason', exact=True)).not_to_have_attribute('required', '')
        dialog.get_by_label('First day of the copies', exact=True).fill('2026-10-16')
        save_dialog(dialog, 'Copy into draft')
        expect(dialog).to_have_count(0)
        assert len([s for s in h.detail(d)['shifts'] if s['start'].startswith('2026-10-16')]) == before + 1
        planner_tool(page, 'Exchange two assignments')
        swap = page.get_by_role('dialog', name='Exchange two assignments')
        expect(swap.get_by_role('button', name='Exchange', exact=True)).to_be_visible()
        expect(swap.get_by_label('Change reason', exact=True)).not_to_have_attribute('required', '')
        expect(swap).not_to_contain_text('employee-consent')
    finally:
        context.close()


def test_day_bars_mark_issues_and_the_export_leaves_live_chrome_out(browser, live_server, h):
    """S-15: a Day bar of a double-booked session carries the issue mark and says it in its name. S-24: the export sheet
    has no collapse chevrons or filled/needed bars."""
    d = booked(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/day/2026-10-20?edit={d['id']}")
        bars = view(page, 'Day').locator('button.sd-bar:not(.key)')
        expect(bars.filter(has=page.locator('.sd-issue'))).to_have_count(2)
        expect(bars.first).to_have_attribute('aria-label', re.compile(r'\d issues?'))
        show(page, f"schedule/week/2026-10-20?edit={d['id']}")
        planner_tool(page, 'Export…')
        sheet = page.get_by_role('dialog', name='Print or export', exact=True).locator('.export-page').first
        expect(sheet.locator('.wk-grid')).to_be_visible()
        expect(sheet.locator('.wk-bar, .wk-toggle svg')).to_have_count(0)
    finally:
        context.close()


def test_an_undated_year_link_opens_where_it_is_and_the_draft_list_shows_dates_whole(browser, live_server, h):
    """S-30: an undated view link chooses the draft of the default week around today, never one months away. S-06: the
    draft switcher shows each rota's dates and status whole, without a helper line."""
    # A rota published for today's week (the audit's case), and a draft months later.
    now = date.fromisoformat(today())
    current = h.shift(h.workspace('This week', now.isoformat(), (now + timedelta(days=7)).isoformat()),
                      f'{now.isoformat()}T08:00:00+03:00', f'{now.isoformat()}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    h.publish(current)
    later = h.workspace('Far away draft', (now + timedelta(days=120)).isoformat(), (now + timedelta(days=127)).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("location.hash = 'schedule/year'")
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=45000)
        assert later['id'] not in page.evaluate('location.hash')
        page.locator('.rota-switcher').click()
        picker = page.get_by_role('dialog', name='Drafts')
        expect(picker).to_be_visible()
        # Found by its own list footer, not its label (review maintenance: a phone sheet is named another way).
        assert picker.evaluate('n => n.getBoundingClientRect().width') >= 450
        expect(picker).not_to_contain_text('One is editable')
        hints = picker.locator('.picker-row .picker-hint, .picker-row small')
        for hint in hints.all():
            assert hint.evaluate('n => n.scrollWidth <= n.clientWidth + 1'), hint.inner_text()
    finally:
        context.close()


def test_new_rota_starts_on_the_next_full_week(browser, live_server, h):
    """S-32: with nothing planned ahead, New rota starts on the department's week start (Sunday here), not mid-week."""
    from datetime import date
    week_start = (h.get('coordinator', '/api/me')['organisation']['week_start'] + 1) % 7  # Python: Monday is 0
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-12-09?edit=none')
        view(page).locator('.empty-state').get_by_role('button', name='New rota', exact=True).click()
        dialog = page.get_by_role('dialog', name='New rota', exact=True)
        from datetime import datetime
        first = dialog.get_by_label('First day', exact=True).input_value()  # shown as "Sun 4 Oct 2026"
        assert (datetime.strptime(first, '%a %d %b %Y').weekday() + 1) % 7 == week_start, first
    finally:
        context.close()


def test_print_sheet_has_a_footer_and_recurring_updates_keep_help_out_of_the_way(browser, live_server, h):
    """S-23: the print sheet dialog has Cancel and its primary in a footer, no repeated subtitle, and a "Clinics" field.
    S-13: Update recurring sessions explains date handling in contextual help, not a permanent warning."""
    from .test_browser_audit_print_output import print_selector_dialog
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        dialog = print_selector_dialog(page)
        footer = dialog.locator('.form-footer')
        expect(footer.get_by_role('button', name='Cancel', exact=True)).to_be_visible()
        expect(footer.get_by_role('button', name='Open print sheet', exact=True)).to_be_visible()
        expect(dialog).not_to_contain_text('A print sheet of a published rota')
        expect(dialog.get_by_role('group', name='Clinics', exact=True)).to_be_visible()
        footer.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_have_count(0)
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        planner_tool(page, 'Update recurring sessions')
        form = page.get_by_role('dialog', name='Update recurring sessions')
        expect(form.locator('.notice.warning')).to_have_count(0)
        expect(form.get_by_role('button', name='How dates carry over', exact=True)).to_be_visible()
    finally:
        context.close()


def test_copy_keeps_its_size_limit_when_the_target_day_changes(browser, live_server, h):
    """S-12 (review maintenance): more than 100 selected sessions cannot be copied in one change, and choosing another target
    day (a preset, typing, the calendar) keeps saying so instead of clearing the limit."""
    w = h.workspace('Big copy draft', '2026-10-18', '2026-10-25')
    for i in range(101):
        day, hour = 18 + i % 7, 6 + i // 7  # 101 sessions, at most one per hour per day
        w = h.shift(w, f'2026-10-{day}T{hour:02}:00:00+03:00', f'2026-10-{day}T{hour:02}:30:00+03:00', people=[])['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/week/2026-10-18?edit={w['id']}")
        boxes = view(page).locator('.wk-check')
        expect(boxes).to_have_count(101)
        boxes.evaluate_all('boxes => boxes.forEach(b => b.click())')
        bar = page.locator('#selection-bar')
        expect(bar).to_contain_text('101')
        copy = bar.get_by_role('button', name='Copy', exact=True)
        if copy.count():
            copy.click()
        else:
            bar.get_by_role('button', name=re.compile('More')).click()
            page.get_by_role('menuitem', name='Copy', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Copy 101 sessions to'))
        first = dialog.get_by_label('First day of the copies', exact=True)
        limit = 'Copy up to 100 sessions at a time (101 selected).'
        expect(dialog.locator('.field-error').filter(has_text=limit)).to_be_visible()
        dialog.get_by_role('button', name='+2 weeks', exact=True).click()
        assert first.evaluate('n => n.validationMessage') == limit
        first.fill('2026-11-02')
        first.blur()
        assert first.evaluate('n => n.validationMessage') == limit
        expect(dialog.locator('.field-error').filter(has_text=limit)).to_be_visible()
        sent = []
        page.on('request', lambda r: sent.append(r.url) if r.method == 'POST' and '/api/commands' in r.url else None)
        dialog.get_by_role('button', name='Copy into draft', exact=True).click()
        expect(dialog).to_be_visible()
        assert not sent, sent
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['week', 'day', 'coverage'])
def test_a_person_filter_hiding_every_session_says_hidden_by_filters(browser, live_server, h, layout):
    """S-01 (review maintenance): a Person filter that hides every session of a populated draft is "Hidden by filters" with Show
    all, in every view, never "No sessions" with Add session."""
    _, d = october(h)
    nobody = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][2])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/{layout}/2026-10-14?edit={d['id']}")
        filters = planner_filter(page)
        filter_pick(filters, 'Person', nobody)
        page.keyboard.press('Escape')
        name = {'week': 'Week', 'day': 'Day', 'coverage': 'Coverage'}[layout]
        card = view(page, name).locator('.empty-state')
        expect(card).to_contain_text('Hidden by filters')
        expect(card.get_by_role('button', name='Add session', exact=True)).to_have_count(0)
        card.get_by_role('button', name='Show all', exact=True).click()
        settled(page)
        expect(view(page, name).locator('.empty-state')).to_have_count(0)
    finally:
        context.close()


def test_a_never_checked_draft_says_not_checked_and_an_absence_is_not_a_double_booking(browser, live_server, h):
    """Review maintenance: "Checking…" only while a check runs. An approver reading a draft with no check (its read carries no
    validation; nobody re-checks it for them) sees "Not checked yet", not a spinner that never stops. Coverage marks a
    double booking with the one evaluator (clashesOf): an absence overlapping a session is not one."""
    a = h.seed['people'][0]
    d = h.shift(h.workspace('Unchecked draft', '2026-10-18', '2026-10-25'), *at(20), people=[a])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': a, 'start': '2026-10-20T09:00:00+03:00',
              'end': '2026-10-20T10:00:00+03:00', 'kind': 'actual_absence', 'private_reason': 'Fictional appointment'})
    flags = h.detail(d)['shifts'][0].get('double_booking_conflicts') or []
    assert flags and all(f['code'] != 'TIME_OVERLAP' for f in flags), flags
    context, page = sign_in(browser, live_server, 'approver')

    def unchecked(route):
        response = route_fetch(route)
        if route.request.method != 'GET' or 'json' not in (response.headers.get('content-type') or ''):
            route.fulfill(response=response)
            return
        data = response.json()
        data['workspace']['validation'] = None
        route.fulfill(response=response, json=data)
    page.route(re.compile(r'/api/workspaces/' + d['id'] + r'(\?|$)'), unchecked)
    try:
        show(page, f"schedule/week/2026-10-18?edit={d['id']}")
        status = view(page).locator('.wk-status')
        expect(status).to_contain_text('Not checked yet')
        expect(status.locator('.spinner')).to_have_count(0)
        show(page, f"schedule/coverage/2026-10-18?edit={d['id']}")
        cov = view(page, 'Coverage')
        expect(cov.locator('button.cov-cell').first).to_be_visible()
        expect(cov.locator('button.cov-cell.issue')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('layout', ['list', 'day', 'coverage'])
def test_every_view_opens_the_one_session_panel(browser, live_server, h, layout):
    """S-09: List, Day and Coverage open the same session panel as the Week grid (no second "details" panel): the
    session's people and checks inline, Edit session first in its footer, then Lock and ⋯ (Close session…)."""
    d = booked(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, f"schedule/{layout}/2026-10-20?edit={d['id']}")
        name = {'list': 'List', 'day': 'Day', 'coverage': 'Coverage'}[layout]
        if layout == 'list':
            view(page, name).locator('tbody tr').first.locator('td').nth(2).click()
        elif layout == 'day':
            view(page, name).locator('.sd-bar').first.click()
        else:
            view(page, name).locator('button.cov-cell').first.click()
            page.locator('.cov-popover .ag-item').first.click()  # a session item (the one item renderer) opens it
        panel = page.locator('.wk-panel')
        expect(panel.get_by_role('heading', name='People', exact=True)).to_be_visible()
        expect(panel.get_by_role('heading', name='Checks', exact=True)).to_be_visible()
        expect(panel).to_contain_text('Double-booked')
        expect(page.locator('.dock-panel')).to_have_count(0)  # one panel, not a second "details" one
        foot = panel.locator('.wk-panel-foot')
        expect(foot.locator('button').first).to_have_accessible_name('Edit session')
        foot.get_by_role('button', name='More session actions', exact=True).click()
        expect(page.get_by_role('menuitem', name='Close session', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
    finally:
        context.close()


def test_schedule_tools_live_in_the_top_bar_with_or_without_a_draft(browser, live_server, h):
    """S-18: the same ⋯ in the same place: a draft's Rota tools and the published schedule's Schedule tools are both in
    the top bar, never at the end of the collection toolbar."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-10-04?edit=none')
        expect(page.locator('.topbar').get_by_role('button', name='Schedule tools', exact=True)).to_be_visible()
        expect(page.locator('.planner-toolbar').get_by_role('button', name='Schedule tools', exact=True)).to_have_count(0)
        show(page, f"schedule/week/2026-10-14?edit={d['id']}")
        expect(page.locator('.topbar').get_by_role('button', name='Rota tools', exact=True)).to_be_visible()
        expect(page.locator('.planner-toolbar').get_by_role('button', name=re.compile('tools$'))).to_have_count(0)
    finally:
        context.close()


def test_opening_another_rota_never_mixes_its_bar_with_the_old_body(browser, live_server, h):
    """S-27: from the published week, opening a draft's Year view while its days still load: the top bar names the draft
    and the old body (its New rota toolbar, its empty week) has already given way to the loading skeleton."""
    _, d = october(h)
    held = []
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-12-09?edit=none')
        expect(page.locator('#main .planner-body .empty-state')).to_be_visible()
        page.route(re.compile(r'/api/schedule/days\?'), lambda route: held.append(route))
        page.evaluate("r => location.hash = r", f"schedule/year/2026-11-01?edit={d['id']}")
        wait_held(page, held)
        expect(page.locator('.topbar .rota-switcher')).to_contain_text('Mid-October draft')
        body = page.locator('#main .planner-body')
        expect(body.locator('.loading')).to_be_visible()
        expect(body.locator('.empty-state')).to_have_count(0)
        expect(body.get_by_role('button', name='New rota', exact=True)).to_have_count(0)
        for route in held:
            route.continue_()
        page.unroute(re.compile(r'/api/schedule/days\?'))
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(view(page, 'Year')).to_be_visible()
    finally:
        context.close()


def test_a_rota_that_fails_to_load_is_a_page_error_not_stale_data(browser, live_server, h):
    """S-28 (lane test "500 → page error"): opening a draft whose read fails says this page didn't load, with Retry and a
    way to the schedule without it, instead of "Couldn't refresh" over the previous, unrelated page."""
    _, d = october(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        show(page, 'schedule/week/2026-12-09?edit=none')
        page.route(re.compile(r'/api/workspaces/' + d['id'] + r'(\?|$)'), lambda route: route.fulfill(status=500, json={'detail': 'Draft read failed'}))
        page.evaluate("r => location.hash = r", f"schedule/week/2026-10-14?edit={d['id']}")
        alert = page.locator('#main').get_by_role('alert')
        expect(alert).to_contain_text('Couldn’t load the schedule.')
        expect(page.locator('.stale-banner')).to_have_count(0)
        expect(page.locator('#main .planner-body')).to_have_count(0)  # not the old week under a banner
        expect(alert.get_by_role('button', name='Retry', exact=True)).to_be_visible()
        page.get_by_role('button', name='Show the schedule without this rota', exact=True).click()
        expect(page.locator('#main .planner-body')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('layout,shape', [('list', 'table'), ('day', 'timeline'), ('week', 'calendar')])
def test_the_schedule_loads_in_its_views_own_shape(browser, live_server, h, layout, shape):
    """S-29: the first load of a Schedule view shows a skeleton in that view's shape, not a month grid for every view."""
    held = []
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        # The rota list answers at once from boot's kept reply (api.ts keptReads); the Schedule's saved views are read
        # on its first opening only, so that read is the one the skeleton waits for.
        page.route(re.compile(r'/api/table-views\?table=planner'), lambda route: held.append(route))
        page.evaluate("r => location.hash = r", f'schedule/{layout}/2026-10-14')
        wait_held(page, held)
        expect(page.locator(f'#main .page-sk.sk-{shape}')).to_have_count(1)
        for route in held:
            route.continue_()
        page.unroute(re.compile(r'/api/table-views\?table=planner'))
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


VIEWS = ["month", "week", "day", "timeline", "people", "coverage", "list"]

# A session as drawn: the innermost element naming it (its duty or a person on it), widened to the first box taller than
# 24px inside its grid (its cell or row); a click at that box's middle must land inside it, so the session is visible and reachable.
VISIBLE_ROW = """names => {
  const root = document.querySelector('#schedule-view');
  if (!root) return {ok: false, why: 'no view'};
  // Inside the view's grid or table: the key above a grid names duties too.
  const named = [...root.querySelectorAll('[role=grid] *, [role=table] *, table *')].filter(n => names.some(x => n.textContent.includes(x))
    && ![...n.children].some(c => names.some(x => c.textContent.includes(x))));
  for (const leaf of named) {
    let box = leaf;
    const small = n => n.getBoundingClientRect().height <= 24 || n.getBoundingClientRect().width <= 24;
    while (small(box) && box !== root && !box.matches('[role=grid], [role=table], table')) box = box.parentElement;
    if (small(box) || box === root || box.matches('[role=grid], [role=table], table')) continue;
    box.scrollIntoView({block: 'center', inline: 'nearest'});
    const r = box.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (hit && box.contains(hit)) return {ok: true, height: r.height, text: box.textContent.slice(0, 60)};
  }
  return {ok: false, why: 'no visible session', named: named.length};
}"""


def names(h, typ):
    catalogues = h.get("admin", "/api/catalogues")
    duty = next(t for t in catalogues["assignment_types"] if t["id"] == typ)
    # Family names: the Month view shortens given names ("A. Morgan").
    people = [p["display_name"].split()[-1] for p in catalogues["people"] if p["id"] in h.seed["people"][:2]]
    return [duty["name"], *people]


def expect_visible_row(page, names_shown, label):
    planner_settled(page)
    result = page.evaluate(VISIBLE_ROW, names_shown)
    assert result["ok"], (label, result)


@pytest.mark.parametrize("layer", ["draft", "published"])
def test_every_view_draws_visible_rows(browser, live_server, h, layer):
    a, b, _ = h.seed["people"]
    typ = clinic(h)
    w = h.workspace("Views " + layer)
    w = h.shift(w, people=[a, b], typ=typ)["workspace"]  # 4 Oct, staffed by two
    if layer == "published":
        w = h.publish(w)["workspace"]
    edit = w["id"] if layer == "draft" else "none"
    shown = names(h, typ)
    context, page = sign_in(browser, live_server, "coordinator")
    try:
        for v in VIEWS:
            navigate(page, f"schedule/{v}/2026-10-04?edit={edit}")
            expect_visible_row(page, shown, v)
        # Counts: duty rows whose cells show counts (View settings › Cells show).
        navigate(page, f"schedule/week/2026-10-04?edit={edit}")
        panel = view_settings(page.locator(".planner-toolbar"))
        choose_radio(panel, "Rows", "Duties")
        choose_radio(panel, "Cells show", "Counts")
        close_popover(page)
        expect(page.locator("#schedule-view .ct-grid")).to_be_visible()
        expect_visible_row(page, shown, "counts")
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_published_session_details_show_the_live_check(browser, live_server, h, scheme):
    """maintenance review: a published-only session's details show the published rota's live check, not "Not checked yet"."""
    h.policy({'kind': 'restriction'}, {'person_id': h.seed['people'][0]}, severity='advisory')
    w = h.shift(h.workspace('Published with a warning'))['workspace']
    h.lifecycle(w, 'validate')
    h.publish(h.detail(w)['workspace'])
    context, page = sign_in(browser, live_server, 'coordinator', color_scheme=scheme)
    try:
        with page.expect_response(lambda r: '/api/publications/' in r.url and r.url.endswith('/coverage')):
            navigate(page, 'schedule/week/2026-10-04?edit=none')
        settled(page)
        page.locator('#schedule-view button.pchip').first.click()
        details = page.locator('.dock-panel')
        expect(details).to_contain_text('Published')
        expect(details.get_by_text('Not checked yet', exact=True)).to_have_count(0)
        expect(details.locator('.iss-row.warning')).to_have_count(1)  # the one issue list (issue_marker.ts)
    finally:
        context.close()


def test_personal_week_properties_say_row_details_and_keep_the_time_and_place_label(browser, live_server, h):
    """C4-F03: on your own read-only Week a duty's chip keeps its time and place (S-12); Time, Location, Service and Hours are
    row details there and View settings says so. Hide all and showing Time again only change the row."""
    from .test_browser_requests import published, day
    published(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, f'schedule/week/{day()}')
        chip = page.locator('.wk-grid .pchip:not(.gap) .pchip-name').first
        expect(chip).to_contain_text('08:00')
        bar = page.locator('.planner-toolbar')
        panel = view_settings(bar, 'Properties')
        expect(panel).to_contain_text('On each card or row · drag to reorder')
        expect(panel.get_by_role('button', name='Hide Time · row detail', exact=True)).to_be_visible()
        panel.get_by_role('button', name='Hide all', exact=True).click()
        close_popover(page)
        expect(page.locator('.wk-rowhead .wk-fact')).to_have_count(0)
        expect(chip).to_contain_text('08:00')  # the label, not a property: it stays
        view_settings(bar, 'Properties').get_by_role('button', name='Show Time · row detail', exact=True).click()
        close_popover(page)
        expect(page.locator('.wk-rowhead .wk-fact-time').first).to_be_visible()
        expect(chip).to_contain_text('08:00')
    finally:
        context.close()


def test_planner_week_properties_keep_the_card_wording(browser, live_server, h):
    """The shared Properties page is unchanged where it is not your own duties: a coordinator's draft Week."""
    w = h.shift(h.workspace('Card wording rota'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        panel = view_settings(page.locator('.planner-toolbar'), 'Properties')
        expect(panel).to_contain_text('On each card · drag to reorder')
        expect(panel.get_by_role('button', name='Hide Time', exact=True)).to_be_visible()
        expect(panel).not_to_contain_text('row detail')
    finally:
        context.close()


def test_saved_people_name_descending_round_trips_and_undo_restores_it(browser, live_server, h):
    """B4-F01: People Name descending is saved as name_desc (not ascending), comes back on reload, and Update to ascending
    is undone back to descending. A view left on Hours keeps the default hours_desc."""
    from .test_browser import pick_view
    w = h.shift(h.workspace('Saved order rota'), people=h.seed['people'][:2])['workspace']
    saved = lambda: {v['name']: v['definition']['planner'].get('people_sort', 'hours_desc') for v in h.get('coordinator', '/api/table-views?table=planner')['views']}
    context, page = sign_in(browser, live_server, 'coordinator')
    bar = page.locator('.planner-toolbar')

    def direction(d):
        bar.get_by_role('button', name=re.compile('^Sort')).click()
        page.get_by_role('radiogroup', name='Sort direction').get_by_role('radio', name=d).click()

    def new_view(name):
        bar.get_by_role('button', name='Saved views').click()
        page.get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view')
        dialog.get_by_label('View name', exact=True).fill(name)
        dialog.get_by_role('button', name='Save view', exact=True).click()
        expect(dialog).to_have_count(0)

    names = lambda: page.locator('.people-grid .person-group .pg-name strong').all_inner_texts()
    try:
        navigate(page, f"schedule/week/2026-10-04?edit={w['id']}")
        pick_view(page, 'People')
        new_view('Hours order (test)')
        bar.get_by_role('button', name=re.compile('^Sort')).click()
        page.get_by_role('menuitemradio', name='Name').click()
        direction('Descending')
        new_view('Name descending (test)')
        # The contract omits the default order; saved() reads it back as hours_desc.
        assert saved() == {'Hours order (test)': 'hours_desc', 'Name descending (test)': 'name_desc'}, saved()
        page.reload()
        pick_view(page, 'Name descending (test)')
        expect(page.locator('.people-grid')).to_be_visible()
        assert names() == sorted(names(), reverse=True), names()
        direction('Ascending')
        view_settings(bar).get_by_role('button', name='Update view', exact=True).click()
        expect(page.locator('.toast').get_by_role('button', name='Undo')).to_be_visible()
        assert saved()['Name descending (test)'] == 'name', saved()
        page.locator('.toast').get_by_role('button', name='Undo').click()
        expect(page.locator('.toast').filter(has_text='Undone')).to_be_visible()
        assert saved() == {'Hours order (test)': 'hours_desc', 'Name descending (test)': 'name_desc'}, saved()
        assert 'Name descending (test)' not in {v['name'] for v in h.get('admin', '/api/table-views?table=planner')['views']}
    finally:
        context.close()


def test_list_columns_are_saved_with_the_schedule_view(browser, live_server, h):
    """The List's columns are part of the saved Schedule view (its top-level hidden): a view hiding Service hides it again
    after another view, the default view shows it, and a column hidden by hand offers Update view, which saves it."""
    from .test_browser import pick_view, toolbar
    from .test_browser_qa_planner import checked_rota
    checked_rota(h)
    for name, hidden in (('No service (test)', ['service']), ('All columns (test)', [])):
        h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': name,
            'definition': {'planner': {'mode': 'list', 'range': 'week'}, 'hidden': hidden}})
    saved = lambda: {v['name']: v['definition'].get('hidden', []) for v in h.get('coordinator', '/api/table-views?table=planner')['views']}
    context, page = sign_in(browser, live_server, 'coordinator')
    bar = page.locator('.planner-toolbar')
    column = lambda key: page.locator(f'[data-table=planner-list] thead th[data-col={key}]')
    try:
        navigate(page, 'schedule/list'); planner_settled(page)
        expect(column('service')).to_have_count(1)
        pick_view(page, 'No service (test)')
        expect(column('service')).to_have_count(0)
        expect(column('people')).to_have_count(1)
        pick_view(page, 'All columns (test)')
        expect(column('service')).to_have_count(1)
        pick_view(page, 'No service (test)')
        expect(column('service')).to_have_count(0)
        toolbar(bar, 'Saved views').get_by_role('menuitemradio', name='Default view', exact=True).click()
        close_popover(page)
        # Default view opens on its own layout (Week, applyPlannerView); its List shows every column.
        pick_view(page, 'List')
        expect(column('service')).to_have_count(1)
        # Hidden by hand in a saved view: the view is changed until updated, and the update keeps the column hidden.
        pick_view(page, 'All columns (test)')
        view_settings(bar, 'Columns').get_by_role('checkbox', name='Status', exact=True).uncheck()
        close_popover(page)
        expect(column('status')).to_have_count(0)
        view_settings(bar).get_by_role('button', name='Update view', exact=True).click()
        expect(page.locator('.toast').get_by_role('button', name='Undo')).to_be_visible()
        assert saved() == {'No service (test)': ['service'], 'All columns (test)': ['status']}, saved()
        close_popover(page)
        pick_view(page, 'No service (test)')
        expect(column('status')).to_have_count(1)
        pick_view(page, 'All columns (test)')
        expect(column('status')).to_have_count(0)
        expect(column('service')).to_have_count(1)
    finally:
        context.close()
