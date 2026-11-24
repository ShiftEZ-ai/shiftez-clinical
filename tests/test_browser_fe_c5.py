"""Cycle-5 frontend fixes (SWEEP-2): the approver's review opens on a phone and on the rota's own week, and Approve shows
only to whoever decides the step (SX-02, SX-12, SX-01); a published session counts a staged absence as an open place,
offers Replace first for someone away, says a staged closing with its Undo and calls it Close everywhere (SX-04..07,
SX-16, SX-18); the planner's person panel names everyone on the session (SX-21); a scheduling group's last day is saved
as typed and the list's count follows an add (SX-22, SX-27); a duty already reported is not offered again (SX-19); and
the smaller S3/E fixes (SX-09, SX-10, SX-28, SX-E2, SX-E3)."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect
from packages.contracts.schemas import ABSENCE_REPORT_LABEL

from .test_browser import browser, live_server, sign_in, navigate, condition, pick, open_config, set_layout  # noqa: F401
from .test_browser import whole_list
from .test_browser_schedule import show
from .test_browser_published_changes import published, open_session
from .test_browser_p2_groups import display
from .test_browser_pwa import push  # noqa: F401
from .test_browser_operator_role_coverage import all_roles  # noqa: F401

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
DESKTOP = {'width': 1440, 'height': 900}
PHONE = {'width': 390, 'height': 844}
SHOTS = os.getenv('FE_C5_SHOTS')  # a folder: the after pictures for the handoff


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'after-{name}.png'))


def submitted(h, name='FE-C5 review', start='2026-10-04', end='2026-10-11'):
    w = h.workspace(name, start, end)
    w = h.shift(w, start=f'{start}T08:00:00+03:00', end=f'{start}T12:00:00+03:00')['workspace']
    return h.lifecycle(w, 'submit')['workspace']


# ---- SX-02 / SX-12 / SX-01: the review ----

def test_an_approver_opens_the_review_from_home_on_a_phone(browser, live_server, h):
    """SX-02 (D25/maintenance): with the phone scope on, Home › Rota to review › Review opens that rota's review (not the
    published week), and it stays the review after the page is drawn again."""
    rota = submitted(h)
    context, page = sign_in(browser, live_server, 'approver', PHONE, phone_notice=True)
    try:
        navigate(page, 'home')
        page.locator('#main').get_by_role('button', name=re.compile('^Review')).first.click()
        dialog = page.get_by_role('dialog', name='Approve rota')
        expect(dialog).to_be_visible()
        shot(page, 'SX-02-phone-review-dialog')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(page).to_have_url(re.compile(f'edit={rota["id"]}'))
        bar = page.locator('.planner-bar')
        expect(bar.get_by_role('button', name='Approve rota', exact=True)).to_be_visible()
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        page.reload()  # a phone that reloads (or wakes the tab) stays on the review, which asks again
        again = page.get_by_role('dialog', name='Approve rota')
        expect(again).to_be_visible()
        again.get_by_role('button', name='Cancel', exact=True).click()
        expect(page.locator('.planner-bar').get_by_role('button', name='Approve rota', exact=True)).to_be_visible()
        expect(page.locator('#main .desktop-only')).to_have_count(0)
        shot(page, 'SX-02-phone-review-bar')
    finally:
        context.close()


def test_a_review_link_opens_on_the_rotas_own_week(browser, live_server, h):
    """SX-12: ?approve=<rota> opens the rota on its first week, as ?edit= does, never today's week (a rota months away)."""
    published(h)  # a published rota covers today, as on the department's server
    rota = submitted(h, start='2027-03-07', end='2027-03-14')
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, f'schedule?approve={rota["id"]}')
        dialog = page.get_by_role('dialog', name='Approve rota')
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(page).to_have_url(re.compile(rf'schedule/week/2027-03-(0[7-9]|1[0-3])\?edit={rota["id"]}'))
        expect(page.locator('#schedule-view [data-shift]').first).to_be_visible()  # its session is on screen
        shot(page, 'SX-12-review-first-week')
    finally:
        context.close()


def test_approve_shows_only_to_whoever_decides_the_step(browser, live_server, all_roles):
    h = all_roles
    """SX-01 (FE half): a programme lead whose role does not review the pending step gets no Approve button (and so no
    empty dialog); the approver does."""
    rota = submitted(h)
    step = h.get('approver', f'/api/workspaces/{rota["id"]}')['review_progress']['pending_step']
    assert step['role'] == 'approver'
    # maintenance: the server says who decides; the button follows it.
    assert h.get('approver', f'/api/workspaces/{rota["id"]}')['review_progress']['can_decide'] is True
    assert h.get('programme_lead', f'/api/workspaces/{rota["id"]}')['review_progress']['can_decide'] is False
    for role, offered in (('programme_lead', False), ('approver', True)):
        context, page = sign_in(browser, live_server, role, DESKTOP)
        try:
            navigate(page, f'schedule/week/2026-10-04?edit={rota["id"]}')
            bar = page.locator('.planner-bar')
            expect(bar.get_by_role('button', name='Return to coordinator')).to_be_visible()
            expect(bar.get_by_role('button', name='Approve rota', exact=True)).to_have_count(1 if offered else 0)
            if not offered:
                shot(page, 'SX-01-programme-lead-no-approve')
        finally:
            context.close()


# ---- SX-04..07, SX-16, SX-18: a published session ----

def test_a_staged_absence_opens_the_place_and_a_staged_closing_says_so_with_undo(browser, live_server, h):
    """SX-04: Mark absent on the only person shows "+1 open place" on the published grid at once. SX-06: the change
    list's Find someone opens the open place's picker. SX-07/SX-16: Close the session (one name) reads as the session's
    state in its panel with Undo, and the change list calls it Close."""
    _, later = published(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_session(page, h, later, person=2)
        panel.get_by_role('button', name=re.compile('^Change .* on this session$')).click()
        page.get_by_role('menuitem', name='Mark absent · Sick').click()
        expect(panel).to_contain_text('Nobody is left on this session.')
        gap = page.locator('#schedule-view .pchip.gap').first
        expect(gap).to_contain_text('open place')  # SX-04: the place it leaves is open on the grid now
        shot(page, 'SX-04-staged-absence-open-place')
        # SX-06: from the change list, Find someone opens the fill picker for that session's open place.
        page.locator('.pc-chip').click()
        changes = page.locator('.dock-panel')
        expect(changes.get_by_role('heading', name=re.compile('^Changes to'))).to_be_visible()
        changes.get_by_role('button', name='Find someone').click()
        picker = page.locator('.dock-panel')
        expect(picker.get_by_role('heading', name='Fill the open place')).to_be_visible()
        expect(picker.get_by_role('searchbox', name='Find a person')).to_be_visible()
        shot(page, 'SX-06-find-someone-picker')
        # Back on the session: close it (the one name), then it reads as closed with Undo.
        picker.get_by_role('button', name=re.compile('Back to session')).click()
        expect(picker.get_by_role('button', name='Cancel session…')).to_have_count(0)
        picker.get_by_role('button', name='Close the session…').click()
        picker.get_by_label('Reason').fill('Clinic closed for the afternoon')
        picker.get_by_role('button', name='Close session').click()
        picker.get_by_role('button', name='Confirm close?').click()
        state = picker.locator('.pc-state').filter(has_text='This session is closed')
        expect(state).to_be_visible()
        expect(picker.get_by_role('button', name='Close the session…')).to_have_count(0)
        shot(page, 'SX-07-staged-closing-in-panel')
        page.locator('.pc-chip').click()
        listed = page.locator('.dock-panel .pc-changes')
        expect(listed.locator('.home-kind').filter(has_text='Close')).to_have_count(1)
        expect(listed).to_contain_text('· closed')
        expect(listed).not_to_contain_text('cancelled')
        shot(page, 'SX-16-change-list-close')
    finally:
        context.close()


def test_someone_away_is_replaced_first_and_the_recorded_reason_is_not_asked_again(browser, live_server, h):
    """SX-05/SX-18 (D13/D38): an absence already recorded on a published duty puts Replace <them>… first in the panel,
    their menu no longer offers Mark absent, and Replace says the recorded absence instead of asking Sick/Leave/Other."""
    _, later = published(h)
    taylor = h.seed['people'][2]
    h.command('coordinator', 'availability.record', {'person_id': taylor, 'kind': 'actual_absence',
              'start': f'{later.isoformat()}T13:00:00+03:00', 'end': f'{later.isoformat()}T16:00:00+03:00', 'private_reason': 'Fictional absence'})
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        panel = open_session(page, h, later, person=2)
        expect(panel).to_contain_text('Someone on this session is away.')
        replace = panel.locator('.pc-foot').get_by_role('button', name=re.compile('^Replace .+…$'))
        expect(replace).to_be_visible()
        shot(page, 'SX-05-away-replace-first')
        panel.get_by_role('button', name=re.compile('^Change .* on this session$')).click()
        menu = page.get_by_role('menu')
        expect(menu.get_by_role('menuitem', name='Replace…')).to_be_visible()
        expect(menu.get_by_role('menuitem', name='Mark absent')).to_have_count(0)
        page.keyboard.press('Escape')
        replace.click()
        expect(panel.get_by_role('radiogroup')).to_have_count(0)  # SX-18: the reason is not asked again
        expect(panel.locator('.pc-step')).to_contain_text('Away ·')
        shot(page, 'SX-18-replace-recorded-reason')
    finally:
        context.close()


# ---- SX-21 / SX-28: the planner's session panel ----

def test_a_persons_panel_names_everyone_on_the_session_and_its_footer_is_one_line(browser, live_server, h):
    a, b, _ = h.seed['people']
    w = h.workspace('FE-C5 two on one session')
    w = h.shift(w, people=[a, b])['workspace']
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f'schedule/week/2026-10-04?edit={w["id"]}')
        page.locator(f'#schedule-view {draft_chip(a, word=True)}').first.click()
        panel = page.locator('.wk-panel')
        also = panel.locator('.wk-also')
        expect(also).to_contain_text('Also on this session:')
        also.get_by_role('button', name=names[b]).click()  # SX-21: the other person is named and opens
        expect(panel.locator('#wk-panel-title')).to_have_text(names[b])
        foot = panel.locator('.wk-panel-foot')
        whole, lock = foot.get_by_role('button', name='Whole session'), foot.get_by_role('button', name='Lock')
        wb, lb = whole.bounding_box(), lock.bounding_box()
        assert abs((wb['y'] + wb['height'] / 2) - (lb['y'] + lb['height'] / 2)) <= 2  # SX-28: one line, centred together
        shot(page, 'SX-21-everyone-on-the-session')
    finally:
        context.close()


# ---- SX-22 / SX-27: Configuration › Scheduling groups ----

def test_a_groups_last_day_is_saved_as_typed_and_the_count_follows_an_add(browser, live_server, h):
    """SX-22: "Until (included)" 30 Nov is stored as the day after (1 Dec, exclusive) exactly once, reads back as 30 Nov,
    and saving the form unchanged keeps it. SX-27: the footer's count follows an add made in place."""
    from .test_browser_p2_groups import group, person
    group(h, 'FE-C5 existing')
    coordinator, first = display(h, 'coordinator'), person(h)
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        expect(page.locator('.app-footer .page-status')).to_contain_text('1 scheduling group')
        main.get_by_role('button', name='New scheduling group').first.click()
        dialog = page.get_by_role('dialog', name='New scheduling group')
        dialog.get_by_label('Name', exact=True).fill('FE-C5 November')
        condition(dialog.locator('.audience-conditions'), 'Person', [first])
        pick(dialog, 'Responsible', [coordinator])
        dialog.get_by_label('From', exact=True).fill('2027-11-01')
        dialog.get_by_label('Until (included)', exact=True).fill('2027-11-30')
        dialog.get_by_label('Reason', exact=True).fill('November group, last day the 30th')
        dialog.get_by_role('button', name='Add group').click()
        expect(dialog).to_have_count(0)
        row = main.locator('tr, .dt-card').filter(has_text='FE-C5 November').first
        expect(row).to_contain_text('until 30 Nov 2027')
        expect(page.locator('.app-footer .page-status')).to_contain_text('2 scheduling groups')  # SX-27
        saved = next(g for g in h.get('admin', '/api/planning-groups')['groups'] if g['definition']['name'] == 'FE-C5 November')
        assert saved['definition']['ends_on'] == '2027-12-01'  # SX-22: exclusive, one day after the last day included
        shot(page, 'SX-22-group-last-day')
        row.get_by_role('button', name='Edit FE-C5 November').click()
        dialog = page.get_by_role('dialog', name='Edit FE-C5 November')
        expect(dialog.get_by_label('Until (included)', exact=True)).to_have_value('Tue 30 Nov 2027')  # shown as typed, not 29 Nov
        dialog.get_by_label('Reason', exact=True).fill('Saved again unchanged')
        dialog.get_by_role('button', name='Save').click()
        expect(dialog).to_have_count(0)
        again = next(g for g in h.get('admin', '/api/planning-groups')['groups'] if g['definition']['name'] == 'FE-C5 November')
        assert again['definition']['ends_on'] == '2027-12-01' and again['revision'] == 2
    finally:
        context.close()


# ---- SX-19: a reported duty ----

def test_a_duty_already_reported_says_reported_on_the_schedule(browser, live_server, h):
    from .test_browser_fe_c4 import my_duties
    _, days, _ = my_duties(h)
    context, page = sign_in(browser, live_server, 'staff', DESKTOP)
    try:
        show(page, f'schedule/week/{days[1].isoformat()}')
        page.locator('#schedule-view .pchip:visible').nth(1).click()
        panel = page.locator('.dock-panel')
        panel.get_by_role('button', name=ABSENCE_REPORT_LABEL).click()
        dialog = page.get_by_role('dialog', name=ABSENCE_REPORT_LABEL + '?')
        dialog.get_by_role('button', name='Tell the coordinator').click()
        expect(dialog).to_have_count(0)
        page.locator('#schedule-view .pchip:visible').nth(1).click()
        panel = page.locator('.dock-panel')
        expect(panel.get_by_text('Reported', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name=ABSENCE_REPORT_LABEL)).to_have_count(0)
        page.reload()  # the server's list decides after a reload too
        page.locator('#schedule-view .pchip:visible').nth(1).click()
        expect(page.locator('.dock-panel').get_by_text('Reported', exact=True)).to_be_visible()
        shot(page, 'SX-19-reported-duty')
    finally:
        context.close()


# ---- SX-10 / SX-09 / SX-E2 / SX-E3 ----

def test_staff_have_no_auto_schedule_animation_choice(browser, live_server):
    for role, offered in (('staff', 0), ('coordinator', 1)):
        context, page = sign_in(browser, live_server, role, DESKTOP)
        try:
            page.get_by_role('button', name=re.compile('^Account menu,')).click()
            expect(page.get_by_role('menuitemcheckbox', name='Animate auto-schedule', exact=True)).to_have_count(offered)
        finally:
            context.close()


def test_an_iphone_is_not_promised_alerts_the_department_has_off(browser, push, live_server):
    """SX-09: push is ready on the server but the department has it off: the install row and Account › Notifications
    promise no alerts."""
    from .test_browser_pwa import IPHONE_SAFARI, _account, _device
    context, page = sign_in(browser, live_server, 'staff', viewport=PHONE, phone_notice=True, user_agent=IPHONE_SAFARI, has_touch=True, is_mobile=True)
    try:
        offer = page.locator('.app-offer[data-offer=install]')
        expect(offer).to_contain_text('An icon on your Home Screen')
        expect(offer).not_to_contain_text('alerts')
        _account(page, 'Notifications')
        device = _device(page)
        expect(device.get_by_text('Your department hasn’t turned alerts on')).to_be_visible()
        expect(device.get_by_text('Add to Home Screen to get alerts')).to_have_count(0)
        title, close = page.locator('.dock-panel .side-head h2'), page.locator('.dock-panel .side-head').get_by_role('button', name=re.compile('^Close'))
        tb, cb = title.bounding_box(), close.bounding_box()
        assert abs((tb['y'] + tb['height'] / 2) - (cb['y'] + cb['height'] / 2)) <= 3  # the × beside its title
        shot(page, 'SX-09-iphone-department-off')
    finally:
        context.close()


def test_department_notifications_say_ready_to_turn_on_and_name_the_coordinator_role(browser, push, live_server):
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        open_config(page, 'Notifications')
        pane = page.locator('#main')
        switch = pane.get_by_role('switch', name='Alerts on phones and computers')
        expect(switch).not_to_be_checked()
        expect(pane.locator('.dn-switch .badge')).to_have_text('Ready to turn on')  # SX-E2
        switch.check()
        expect(pane.locator('.dn-switch .badge')).to_have_text('Ready')
        expect(pane.get_by_role('cell', name='People with the Coordinator role', exact=True)).to_be_visible()  # SX-E3
        shot(page, 'SX-E2-E3-department-notifications')
    finally:
        context.close()


# ---- SX-08: the approver reads a Can't make it ----

# What each reader's Details says of a Can't make it (category "sick", note "Fictional family matter"), per maintenance: the
# reporter always reads their own words; a decider (approver) and a third party (coordinator) read what the department's
# leave_reason_display lets the server send: none, only that it is a Can't make it (no Details column at all); type, the
# category; details, category and note. The private reason never shows.
OWN_WORDS = 'Sick · Fictional family matter'
CANT_MAKE_IT_SHOWN = {**{(mode, 'staff'): OWN_WORDS for mode in ('none', 'type', 'details')},
                      **{(mode, viewer): shown for viewer in ('approver', 'coordinator')
                         for mode, shown in (('none', None), ('type', 'Sick'), ('details', OWN_WORDS))}}


def cant_make_it(h, category='sick', reason='Fictional private explanation', note='Fictional family matter'):
    """A staff Can't make it on their duty in two days (as Home sends it: the category beside the private reason)."""
    from .test_browser_fe_c4 import my_duties
    me, days, _ = my_duties(h)
    duty = next(x for x in h.get('staff', f'/api/calendar?start={days[1]}&end={days[1] + timedelta(days=1)}')['events'] if x.get('source_id'))
    report = h.command('staff', 'request.create', {'kind': 'absence_report', 'publication_id': duty['source_id'], 'assignment_id': duty['id'],
                                                   'reason': reason, 'note': note, **({'category': category} if category else {})})['record']
    assert report['absence_progress'] == 'waiting_for_approval'
    return me, report


def requests_row(page, viewer):
    navigate(page, 'requests')
    if viewer != 'staff':
        whole_list(page)  # the whole list, not the decider's Waiting for you
    return page.locator('tr').filter(has_text=ABSENCE_REPORT_LABEL).first


def reads(page, row, shown):
    """Details says exactly `shown`; with nothing to say there is no Details column (no label left empty)."""
    if shown is None:
        expect(row).to_be_visible()
        expect(page.get_by_role('columnheader', name='Details', exact=True)).to_have_count(0)
        expect(row.locator('.request-detail')).to_have_count(0)
        expect(row).not_to_contain_text('Fictional family matter')
    else:
        expect(row.locator('.request-detail')).to_have_text(shown)
    expect(row).not_to_contain_text('Fictional private explanation')


@pytest.mark.parametrize(('mode', 'viewer'), list(CANT_MAKE_IT_SHOWN))
def test_the_reason_and_note_of_a_cant_make_it_read_as_the_department_shows_them(browser, live_server, h, mode, viewer):
    """SX-08 (maintenance: absence reasons follow leave_reason_display; the client shows exactly what the server sends): Details
    says what this reader may see, with no stray separator or empty column when less is sent; the status is Waiting for
    approval until it is approved, then Finding cover, for everyone."""
    from .test_leave_disclosure import setting
    setting(h, mode)
    _, report = cant_make_it(h)
    shown = CANT_MAKE_IT_SHOWN[(mode, viewer)]
    context, page = sign_in(browser, live_server, viewer, DESKTOP)
    try:
        row = requests_row(page, viewer)
        expect(row).to_contain_text('Waiting for approval')
        expect(row).not_to_contain_text('Finding cover')
        reads(page, row, shown)
        shot(page, f'SX-08-{viewer}-reads-reason-{mode}')
        h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approved for the test'}, report['id'], report['revision'])
        page.reload()
        row = page.locator('tr').filter(has_text=ABSENCE_REPORT_LABEL).first
        expect(row).to_contain_text('Finding cover')
        reads(page, row, shown)
    finally:
        context.close()


def test_a_legacy_reason_reads_other_to_a_decider_at_type(browser, live_server, h):
    """A report from before categories (free text only) reads "Other" at type: the free text is never a category."""
    from .test_leave_disclosure import setting
    setting(h, 'type')
    cant_make_it(h, category=None, note='')
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        reads(page, requests_row(page, 'approver'), 'Other')
    finally:
        context.close()


def test_timeline_and_a_persons_work_open_with_an_approved_cant_make_it_in_range(browser, live_server, h):
    """maintenance v2: an approved Can't make it linked to its availability failed the timeline reads (500). People › Timeline
    and the person's own Schedule › Timeline both open, every timeline read answers, and neither shows an error."""
    from .test_leave_disclosure import setting
    setting(h, 'type')
    me, report = cant_make_it(h)
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Approved for the test'}, report['id'], report['revision'])
    name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == me)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    statuses = []
    page.on('response', lambda r: statuses.append((r.url, r.status)) if '/timeline?' in r.url else None)
    try:
        navigate(page, 'people')
        set_layout(page, 'Timeline')
        board = page.get_by_role('region', name='People timeline')
        expect(board.locator(f'.tl-row[data-row="{me}"]')).to_be_visible()
        expect(page.locator('#main .notice.error')).to_have_count(0)
        set_layout(page, 'Table')
        page.get_by_label('Search people', exact=True).fill(name)
        page.locator('#main .dt-body tbody tr, #main .dt-body .dt-card').filter(has_text=name).first.locator('strong').first.click()
        panel = page.get_by_role('region', name=name, exact=True)
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        work = panel.get_by_role('tabpanel', name='Schedule', exact=True).get_by_role('region', name='People timeline')
        expect(work.locator('.tl-row[data-row]')).to_have_count(1)
        expect(panel.locator('.notice.error')).to_have_count(0)
        assert any(f'/api/people/{me}/timeline?' in url for url, _ in statuses)
        assert all(status == 200 for _, status in statuses), statuses
    finally:
        context.close()


# ---- SX-14: the month's week labels and row dots ----

def test_month_week_labels_start_at_one_edge(browser, live_server, h):
    w = h.workspace('FE-C5 month labels', '2026-10-01', '2026-11-01')
    w = h.shift(w)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        show(page, f'schedule/month/2026-10-04?edit={w["id"]}')
        corners = page.locator('#schedule-view .mo-wk > .wk-corner')
        expect(corners.first).to_be_visible()
        lefts = corners.evaluate_all("cs => cs.map(c => Math.round(c.firstChild.nodeType === 3 ? (()=>{const r=document.createRange();r.selectNodeContents(c.firstChild);return r.getBoundingClientRect().left;})() : c.firstElementChild.getBoundingClientRect().left))")
        assert len(set(lefts)) == 1, lefts  # every "Wk n · …" starts at the same edge, short or long
        head = page.locator('#schedule-view .wk-rowhead').first
        dot, name = head.locator('.wk-swatch'), head.locator('.clamp-2')
        assert abs(dot.bounding_box()['y'] - name.bounding_box()['y']) < 12  # the dot stays on the name's first line
        shot(page, 'SX-14-month-labels')
    finally:
        context.close()


# ---- SX-17: an amendment is approved as changes ----

def test_an_amendment_is_approved_as_changes_counted_as_home_counts_them(browser, live_server, h):
    a, b, c = h.seed['people']
    tomorrow, _ = published(h)
    pub = h.get('coordinator', '/api/publications')['publications'][0]['id']
    snap = h.get('coordinator', '/api/publications/' + pub)['snapshot']['shifts']
    s = next(x for x in snap if x['start'].startswith(tomorrow.isoformat()))
    alex = next(p for p in s['participants'] if p['person_id'] == a)
    first = h.command('coordinator', 'publication.change', {'kind': 'absence', 'shift_id': s['id'], 'assignment_id': alex['id'],
                      'absence_reason': 'Sick', 'reason': 'Staff 01 absent'}, pub)
    rev = first['workspace']['revision']
    second = h.command('coordinator', 'publication.change', {'kind': 'add', 'shift_id': s['id'], 'person_id': c, 'reason': 'Staff 03 added'}, pub, rev)
    sent = h.command('coordinator', 'publication.changes.submit', {'urgency': 'routine', 'reason': 'Cover for Staff 01'}, pub, second['workspace']['revision'])
    amendment = sent['workspace']['id']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, f'schedule?approve={amendment}')
        dialog = page.get_by_role('dialog', name='Approve changes')
        expect(dialog).to_be_visible()
        what = dialog.get_by_role('region', name='What changes')
        expect(what.locator('.badge').first).to_have_text('2 changes · 1 session')
        expect(dialog.get_by_role('button', name='Approve changes', exact=True)).to_be_visible()
        shot(page, 'SX-17-approve-changes')
    finally:
        context.close()


# ---- SX-23: group planning has no hidden scope ----

def test_a_run_follows_the_group_scope_shown(browser, live_server, h):
    """SX-23(b): with the scope switched to All groups, Run sends no group and no people scope, and the server's job
    is the whole department (no generation_scope)."""
    from .test_browser import open_rota, open_scheduling
    from .test_browser_p2_groups import two_groups, cancel_runs
    from .test_planning_groups_drafts import settings
    rota, mine, _ = two_groups(h)
    settings(h, coordinator_identity_ids=[h.seed['identities']['coordinator']])  # the Rota Coordinator runs All groups
    rota = h.detail(rota)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, rota['id'])
        page.locator('.planner-bar').get_by_role('button', name=f'Scheduling group: {mine["definition"]["name"]}').click()
        page.get_by_role('option', name='All groups').click()
        expect(page.locator('.planner-bar').get_by_role('button', name='Scheduling group: All groups')).to_be_visible()
        open_scheduling(page)
        panel = page.locator('#auto-panel')
        with page.expect_request(lambda r: r.url.endswith('/api/commands') and '"job.create"' in (r.post_data or '')) as request:
            panel.get_by_role('button', name='Run auto-schedule', exact=True).click()
        payload = json.loads(request.value.post_data)['payload']
        assert not {'planning_group_version_id', 'lookahead', 'generation_scope'} & set(payload)
        expect(panel.get_by_role('progressbar', name='Auto-schedule progress')).to_be_visible()  # the server took it
        job = request.value.response().json()['job']
        job = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert not job.get('generation_scope') and not job.get('planning_group_version_id')
        cancel_runs(h, rota)
    finally:
        context.close()


def test_groups_need_rota_coordinators_and_work_without_making_a_rota(browser, live_server, h):
    """SX-23(a) / D41 (maintenance): with no Rota Coordinators, adding a group is refused in the same screen with the
    server's reason; once they are set, the period's plain shared rota is the group rota (scope button) with no
    "Make this the rota" step."""
    from .test_browser import open_rota
    from .test_browser_p2_groups import person
    from .test_planning_groups_drafts import settings
    coordinator, first = display(h, 'coordinator'), person(h)
    context, page = sign_in(browser, live_server, 'admin', DESKTOP)
    try:
        navigate(page, 'configuration/scheduling-groups')
        main = page.locator('#main')
        expect(main.get_by_text('None · needed before a group can be active')).to_be_visible()
        main.get_by_role('button', name='New scheduling group').first.click()
        dialog = page.get_by_role('dialog', name='New scheduling group')
        dialog.get_by_label('Name', exact=True).fill('FE-C5 Consultants')
        condition(dialog.locator('.audience-conditions'), 'Person', [first])
        pick(dialog, 'Responsible', [coordinator])
        dialog.get_by_label('Reason', exact=True).fill('Refused without Rota Coordinators')
        dialog.get_by_role('button', name='Add group').click()
        expect(dialog).to_contain_text('Choose Rota Coordinators in Scheduling groups before activating a group')
        shot(page, 'SX-23-groups-need-rota-coordinators')
    finally:
        context.close()
    settings(h, coordinator_identity_ids=[h.seed['identities']['coordinator']])  # who keeps the session definitions
    from .test_browser_p2_groups import group
    group(h, 'FE-C5 Consultants')
    w = h.workspace('FE-C5 plain shared rota')
    w = h.shift(w)['workspace']
    assert h.detail(w)['workspace']['purpose'] == 'integrated'  # the period's default rota, no designate
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_rota(page, w['id'])
        expect(page.locator('.planner-bar').get_by_role('button', name=re.compile('^Scheduling group: '))).to_be_visible()
        page.locator('.planner-bar').get_by_role('button', name='Rota tools').click()
        expect(page.get_by_role('menuitem', name='Make this the rota…')).to_have_count(0)
        shot(page, 'SX-23-group-rota-without-designate')
    finally:
        context.close()


# ---- SX-25: a run with nothing to apply ----

def test_a_run_with_nothing_to_apply_is_not_a_proposal(browser, live_server, h):
    from apps.worker.main import process_one
    from .test_browser import open_rota, open_scheduling
    w = h.workspace('FE-C5 nothing to apply')
    w = h.shift(w)['workspace']  # its one place is filled already
    for _ in range(2):
        w = h.detail(w)['workspace']
        h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])
        process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs?workspace_id=' + w['id'])['jobs'][0]
    full = h.get('coordinator', '/api/jobs/' + job['id'])['job']
    assert full['status'] == 'succeeded' and not full['result'].get('additions') and not full['result'].get('removals')
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        from .test_browser import route_fetch
        def counted(route):  # maintenance wiring: history rows say how many changes a run proposes (`result.change_count`)
            body = route_fetch(route).json()
            for j in body.get('jobs', []):
                if j.get('kind') == 'generation' and j.get('result') is not None:
                    j['result'].setdefault('change_count', 0)
            route.fulfill(json=body)
        page.route(re.compile(rf'.*/api/jobs\?workspace_id={w["id"]}.*'), counted)
        open_rota(page, w['id'])
        expect(page.locator('.app-footer .fix-pills, .app-footer .health-pills').first).to_be_visible()
        expect(page.locator('.app-footer .proposal-pill')).to_have_count(0)
        open_scheduling(page)
        runs = page.locator('#auto-panel .run-list')
        expect(runs.get_by_text('Nothing to apply').first).to_be_visible()
        expect(runs).not_to_contain_text('Out of date')
        shot(page, 'SX-25-nothing-to-apply')
    finally:
        context.close()
