"""The legend guard on Week, Month and the phone day agenda reviewed:
every mark the planner draws is stamped as its key entry, every entry has a mark on screen, and no mark of the shared
vocabulary is drawn without its key (tests/legend_guard.py). Two drafts between them draw each kind of mark: one of
supervision teams and their problems (test_browser_team_box), one with a team lead, a double booking, a locked
session, an open place, someone on leave and a staff wish. The phone's All Key list outlives a live redraw of the same view."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, holding, live_server, sign_in, wait_until  # noqa: F401
from .test_browser_kept_reads import live_pulse
from .test_browser_team_box import fixture as team_fixture, open_view
from .test_browser_legends import two_duties
from .test_browser_qa_planner import clinic
from .legend_guard import assert_legend_matches

pytestmark = pytest.mark.browser
ROOT = '#schedule-view'
DESKTOP, PHONE = {'width': 1440, 'height': 900}, {'width': 390, 'height': 844}


def at(day, start='08', end='12'):
    return f'2026-10-{day:02}T{start}:00:00+03:00', f'2026-10-{day:02}T{end}:00:00+03:00'


def marked_draft(h):
    """two_duties (Alex on Sun 4, Jordan leading Night cover on Mon 5) plus: Alex double-booked on Sun 4, a locked
    session on Tue 6, an open place on Wed 7, Taylor on leave while on duty Thu 8, and Alex's wish for Sun 4."""
    a, _, c = h.seed['people']
    w = two_duties(h, 'Legend guard rota')
    w = h.shift(w, *at(4, '10', '14'), people=[a])['workspace']
    w = h.shift(w, *at(6), people=[a])['workspace']
    w = h.shift(w, *at(7), people=[], typ=clinic(h))['workspace']
    w = h.shift(w, *at(8), people=[c])['workspace']
    locked = next(s['id'] for s in h.detail(w)['shifts'] if s['start'].startswith('2026-10-06'))
    w = h.command('coordinator', 'shift.action', {'workspace_id': w['id'], 'shift_ids': [locked], 'action': 'lock',
                  'reason': 'Keep the manual assignment'}, revision=w['revision'])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': c, 'kind': 'recorded_leave',
              'start': '2026-10-08T00:00:00+03:00', 'end': '2026-10-09T00:00:00+03:00', 'private_reason': 'Leave'})
    h.command('staff', 'wish.create', {'person_id': a, 'first_day': '2026-10-04', 'last_day': '2026-10-04', 'kind': 'prefer_off', 'points': 1})
    return h.lifecycle(h.detail(w)['workspace'], 'validate')['workspace']


def guarded(page, w, view, layout, marks):
    open_view(page, w, view, layout)
    expect(page.locator(f'{ROOT} .view-legend:visible')).to_have_count(1)
    assert_legend_matches(page, root=ROOT, expect_marks=marks)


def test_week_and_month_key_every_mark_they_draw(browser, live_server, h):
    teams, _ = team_fixture(h)
    marked = marked_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        guarded(page, teams, 'Week', 'Grid', ['team', 'issue:error', 'issue:warning', 'weekend', 'days'])
        guarded(page, teams, 'Month', 'Grid', ['weekend', 'days', 'issue:error'])
        guarded(page, marked, 'Week', 'Grid', ['tag:lead', 'clash', 'locked', 'open', 'absent', 'wish', 'issue:error', 'weekend', 'days'])
        guarded(page, marked, 'Month', 'Grid', ['tag:lead', 'wish', 'issue:error', 'weekend', 'days'])
    finally:
        context.close()


def test_phone_day_agenda_keys_every_mark_it_draws(browser, live_server, h):
    teams, _ = team_fixture(h)
    marked = marked_draft(h)
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    try:
        guarded(page, teams, 'Week', 'Agenda', ['team', 'issue:error', 'issue:warning'])
        # The agenda's "N open" pill (Wed 7) is the shared Open place entry (live check 6cea25859 found it unkeyed).
        guarded(page, marked, 'Week', 'Agenda', ['tag:lead', 'clash', 'issue:error', 'open'])
    finally:
        context.close()


def test_phone_all_key_stays_open_through_a_same_view_redraw(browser, live_server, h):
    """A live redraw of the same rota, view and period whose sessions read lands after All Key opened (first-attempt run
    45091667806 lost the list there): the list stays open on the redrawn key's +N and lists that key's entries, and
    Escape returns the focus to that +N."""
    marked = marked_draft(h)
    moved, sessions = [0], '**/api/schedule/sessions?**'
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    live_pulse(page, 'publication', moved)  # someone else published: the Schedule redraws in place
    try:
        open_view(page, marked, 'Week', 'Agenda')
        key = page.locator(f'{ROOT} .view-legend:visible')
        expect(key).to_have_count(1)
        more = key.locator(':scope > .lg-more > button')
        expect(more).to_be_visible()
        label, before = key.get_attribute('aria-label'), key.get_attribute('data-lg-key')
        panel = page.get_by_role('dialog', name=f'All {label}', exact=True)
        with holding(page) as held:
            held.hold(sessions)
            moved[0] = 1
            page.evaluate("window.dispatchEvent(new Event('online'))")
            wait_until(page, lambda: held[sessions], 'the live redraw never read the sessions')
            more.click()
            expect(panel).to_be_visible()
            held.release(sessions)
            expect(page.locator(f'{ROOT} .view-legend[data-lg-key="{before}"]')).to_have_count(0)  # the key was redrawn
        expect(key).to_have_count(1)
        expect(panel).to_be_visible()
        expect(page.locator('.popover')).to_have_count(1)
        expect(more).to_have_attribute('aria-expanded', 'true')
        expected = key.locator(':scope > li[data-lg-entry]').evaluate_all('ns => ns.map(n => n.innerHTML)')
        listed = panel.locator('ol > li > .lg-pop')
        expect(listed).to_have_count(len(expected))
        assert listed.evaluate_all('ns => ns.map(n => n.innerHTML)') == expected
        page.keyboard.press('Escape')
        expect(panel).to_have_count(0)
        expect(more).to_be_focused()
        expect(more).to_have_attribute('aria-expanded', 'false')
    finally:
        context.close()


def test_the_session_panel_keys_its_pills_and_team_boxes(browser, live_server, h):
    """The session panel (sessionSummary, Week's and the published Schedule's one panel) draws its people as pills in the
    session's duty hue and its teams as boxes: both are in the panel's own key, as drawn."""
    teams, _ = team_fixture(h)
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        open_view(page, teams, 'List')
        panel = page.locator('.wk-panel .wk-session')
        rows = page.locator('tr.dt-open')
        expect(rows.first).to_be_visible()
        for i in range(rows.count()):  # the first session drawn with a team box
            rows.nth(i).locator('td strong').first.click()
            expect(panel).to_be_visible()
            if panel.locator('.team-box').count():
                break
        expect(panel.locator('.team-box').first).to_be_visible()
        expect(panel.locator('.view-legend')).to_be_visible()
        names = assert_legend_matches(page, root='.wk-panel .wk-session', expect_marks=['team'])
        assert any(n.startswith('c:t:') for n in names), names  # the pills' duty hue
    finally:
        context.close()



def test_days_outside_the_draft_are_one_grey_and_keyed(browser, live_server, h):
    """maintenance: a one-week draft (18–24 Oct) in a month with published weeks around it. Every day outside the draft's dates,
    published (4–17) or not (1–3, 25–31), is the same out-of-range grey with no day numbers and the key's "Outside this
    rota"; published sessions there are the grey read-only chips keyed "Published, read-only". The Month's band names
    what it counts ("1 service without sessions")."""
    a, b, _ = h.seed['people']
    for first, last, person in ((4, 11, a), (11, 18, b)):
        w = h.workspace(f'Published {first} Oct', f'2026-10-{first:02}', f'2026-10-{last:02}')
        h.publish(h.shift(w, *at(first + 1), people=[person])['workspace'])
    draft = h.workspace('One week draft', '2026-10-18', '2026-10-25')
    draft = h.shift(draft, *at(19), people=[a], service=1)['workspace']
    context, page = sign_in(browser, live_server, 'coordinator', DESKTOP)
    try:
        for view in ('Month', 'Week'):
            guarded(page, draft, view, 'Grid', ['outside', 'ctx'])
            outside = page.locator(f'{ROOT} .wk-head .wk-day.sd-ctx, {ROOT} .mo-wk .wk-day.sd-ctx')
            expect(outside.first).to_be_visible()
            assert outside.locator('.wk-cov').count() == 0  # no day numbers outside the draft, published or not
        open_view(page, draft, 'Month')
        idle = page.locator(f'{ROOT} .mo-idle')
        expect(idle.first).to_be_visible()
        for text in idle.all_inner_texts():
            assert re.fullmatch(r'\d+ [a-z][a-z ]* without sessions', text), text  # the unit the Month groups by, named
    finally:
        context.close()
