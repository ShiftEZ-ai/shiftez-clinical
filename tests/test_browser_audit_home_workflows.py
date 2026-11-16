"""Home's request outcomes, date words and linked-person capabilities agree with their destinations."""
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from packages.domain.time import now as clock_now
from .governance_support import identity
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401

pytestmark = pytest.mark.browser
DISPLAYS = [(None, 'light'), (None, 'dark'), ({'width': 390, 'height': 844}, 'light'),
            ({'width': 390, 'height': 844}, 'dark')]


def date_label(day):
    return f'{day.day} {day:%b %Y}'


@pytest.mark.parametrize('viewport,theme', DISPLAYS)
def test_home_request_dates_and_saved_weekly_wish_match_requests(browser, live_server, h, viewport, theme):
    day = clock_now().date() + timedelta(days=4)
    person = h.seed['people'][0]
    for kind, start, end in (
        ('leave', f'{day}T00:00:00+03:00', f'{day + timedelta(days=1)}T00:00:00+03:00'),
        ('meeting', f'{day + timedelta(days=2)}T00:00:00+03:00', f'{day + timedelta(days=5)}T00:00:00+03:00'),
        ('administrative', f'{day + timedelta(days=6)}T09:00:00+03:00', f'{day + timedelta(days=6)}T12:30:00+03:00')):
        h.command('staff', 'request.create', {'person_id': person, 'kind': kind, 'start': start, 'end': end,
                                             'private_reason': 'Private fixture reason'})
    h.command('staff', 'availability_pattern.propose', {'person_id': person, 'weekdays': [1], 'start_time': '13:00',
        'end_time': '17:00', 'starts_on': day.isoformat()})
    wish_day = day + timedelta(days=30)
    h.command('staff', 'wish.create', {'person_id': person, 'first_day': str(wish_day), 'last_day': str(wish_day),
                                      'kind': 'prefer_off', 'points': 1})
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        page.evaluate("theme => localStorage.setItem('rota.prefs', JSON.stringify({theme}))", theme)
        page.reload()
        expect_loaded(page, 'home')
        # My requests: the four still waiting (the wish is open, not waiting), the first three with their days.
        panel = page.get_by_role('region', name='My requests')
        expect(panel).to_contain_text('4 waiting')
        expect(page.locator('#main')).not_to_contain_text('Private fixture reason')
        expected = [f'{date_label(day)} · all day',
                    f'{date_label(day + timedelta(days=2))} – {date_label(day + timedelta(days=4))} · all day',
                    f'{date_label(day + timedelta(days=6))} · 09:00–12:30']
        for text in expected[:2]:  # Home shows the first three waiting; Requests has them all
            expect(panel.get_by_text(text, exact=True)).to_be_visible()
        panel.get_by_role('button', name='All my requests', exact=True).click()
        expect_loaded(page, 'requests')
        for text in expected:
            expect(page.locator('.request-when').filter(has_text=text)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', DISPLAYS)
def test_home_linked_person_roles_only_offer_available_actions(browser, live_server, h, viewport, theme):
    person = h.seed['people'][0]
    # A published duty ahead: Add to calendar is offered only when there is something to add (L18).
    soon = clock_now().date() + timedelta(days=3)
    h.publish(h.shift(h.workspace('Linked roles rota', soon.isoformat(), (soon + timedelta(days=7)).isoformat()),
                      f'{soon}T08:00:00+03:00', f'{soon}T12:00:00+03:00', people=[person])['workspace'])
    roles = [('linked-approver', ['approver'], True, False),
             ('linked-credentialer', ['credentialer'], False, False),
             ('linked-operator', ['operator'], False, False),
             ('linked-staff-reviewer', ['staff', 'approver'], True, True)]
    for username, bindings, calendar, creates in roles:
        identity(h, username, bindings, person)
        context, page = sign_in(browser, live_server, username, viewport)
        try:
            page.evaluate("theme => localStorage.setItem('rota.prefs', JSON.stringify({theme}))", theme)
            page.reload()
            expect_loaded(page, 'home')
            expect(page.locator('.home-duties')).to_have_count(int(calendar))
            action = page.locator('.topbar .page-actions').get_by_role('button', name='New request', exact=True)
            expect(action).to_have_count(int(creates))
            expect(page.locator('#main [role=alert]')).to_have_count(0)
            # Subscription supports every linked active identity independently of the Schedule permission.
            expect(page.get_by_role('button', name='Add to calendar', exact=True)).to_be_visible()
            if creates:
                action.click()
                expect(page.get_by_role('dialog', name='New request')).to_be_visible()
                page.get_by_role('dialog', name='New request').get_by_role('button', name='Cancel', exact=True).click()
        finally:
            context.close()
