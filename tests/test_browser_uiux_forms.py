"""Configured session defaults and complete-duty participant form journeys."""
import re
import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, open_session, save_dialog, expect_when

pytestmark = pytest.mark.browser


def open_planner(page):
    page.evaluate("location.hash='planner'")
    expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=20000)
    expect(page.locator('#main [role=alert]')).to_have_count(0)


def configured_type(h, start='08:30', end='12:00'):
    return h.configure('assignment_types', {'name': 'Configured form duty', 'starts_on': '2020-01-01',
        'definition': {'default_timing': {'start_time': start, 'end_time': end}}})['record']


@pytest.mark.parametrize('start,end,until', [
    ('08:30', '12:00', '2026-10-04T12:00'),
    ('20:00', '04:00', '2026-10-05T04:00'),
    ('16:00', '24:00', '2026-10-05T00:00'),
])
def test_new_session_uses_configured_clock_times_without_an_extra_reset(browser, live_server, h, start, end, until):
    typ = configured_type(h, start, end)
    h.workspace()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page)
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()  # the toolbar's, or an empty week's card's (SX-14: one shows)
        form = page.locator('.dock-panel')
        form.get_by_label('Duty type', exact=True).select_option(typ['id'])
        expect_when(form, 'Session start', '2026-10-04T' + start)
        expect_when(form, 'Session end', until)
    finally:
        context.close()


def test_manual_session_times_survive_type_selection_and_explicit_reset_keeps_the_day(browser, live_server, h):
    typ = configured_type(h, '20:00', '04:00')
    h.workspace()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page)
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()  # the toolbar's, or an empty week's card's (SX-14: one shows)
        form = page.locator('.dock-panel')
        form.get_by_label('Session start', exact=True).fill('2026-10-05T09:00')
        form.get_by_label('Session end', exact=True).fill('2026-10-05T12:30')
        form.get_by_label('Duty type', exact=True).select_option(typ['id'])
        expect_when(form, 'Session start', '2026-10-05T09:00')
        expect_when(form, 'Session end', '2026-10-05T12:30')
        form.get_by_role('button', name='Use default timing', exact=True).click()
        expect_when(form, 'Session start', '2026-10-05T20:00')
        expect_when(form, 'Session end', '2026-10-06T04:00')
    finally:
        context.close()


@pytest.mark.parametrize('copy', [False, True], ids=['edit', 'copy'])
def test_existing_and_copied_sessions_keep_their_custom_clock_times(browser, live_server, h, copy):
    typ = configured_type(h)
    h.shift(h.workspace(), start='2026-10-04T09:00:00+03:00', end='2026-10-04T12:30:00+03:00', typ=typ['id'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page)
        if copy:
            page.locator('.wk-rowhead').first.click(button='right')
            page.get_by_role('menuitem', name='Add this session on another day').click()
        else:
            open_session(page)
            page.get_by_role('button', name='Edit session', exact=True).click()
        form = page.locator('.dock-panel')
        expect_when(form, 'Session start', '2026-10-04T09:00')
        expect_when(form, 'Session end', '2026-10-04T12:30')
    finally:
        context.close()


def test_participant_options_are_inline_and_saves_still_book_the_whole_duty(browser, live_server, h):
    w = h.workspace()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_planner(page)
        page.locator('#main').get_by_role('button', name='Add session', exact=True).click()  # the toolbar's, or an empty week's card's (SX-14: one shows)
        form = page.locator('.dock-panel')
        form.get_by_label('Duty type', exact=True).select_option(h.seed['type'])
        form.get_by_label('Location', exact=True).select_option(h.seed['locations'][0])
        form.get_by_label('Continuity clinic', exact=True).check()
        form.get_by_role('button', name='Add participant', exact=True).click()
        form.get_by_label('Participant', exact=True).select_option(h.seed['people'][0])
        expect(form.get_by_label('Named supervisor', exact=True)).to_be_hidden()
        form.get_by_label('Participation role', exact=True).select_option('trainee')
        expect(form.get_by_label('Named supervisor', exact=True)).to_be_visible()
        form.get_by_label('Participation role', exact=True).select_option('staff')
        form.get_by_role('checkbox', name=re.compile('^Keep assigned — ')).check()
        save_dialog(form, 'Save draft session')
        saved = h.detail(w)['shifts'][0]
        assert saved['participants'][0]['fixed']
        assert saved['participants'][0]['start'] == saved['start']
        assert saved['participants'][0]['end'] == saved['end']
        assert saved['participants'][0]['segments'] == []
    finally:
        context.close()
