"""Audit M9: Requests keep the server's stage order, show recorded leave details by policy, and say every duty a request
overlaps (published and still being planned)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate  # noqa: F401
from .test_browser import whole_list
from .test_browser_requests import PHONE, day, leave, published, rows, no_horizontal_scroll
from .test_leave_disclosure import setting
from .test_leave_types import selected, value

pytestmark = pytest.mark.browser


def cards(page, viewport):
    return page.locator('#main .dt-card') if viewport else rows(page)


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['1440', '390'])
def test_default_order_puts_waiting_first_without_a_page_sort(browser, live_server, h, viewport):
    # Created oldest first: a newest-first list would show the withdrawn one on top.
    approved = leave(h, on=day(3))
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Fine'}, approved['id'], approved['revision'])
    leave(h, on=day(1))
    withdrawn = leave(h, on=day(2))
    h.command('staff', 'request.decide', {'decision': 'withdraw', 'reason': 'Not needed'}, withdrawn['id'], withdrawn['revision'])
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        navigate(page, 'requests')
        listed = cards(page, viewport)
        expect(listed).to_have_count(3)
        for n, word in enumerate(('Waiting for approval', 'Approved', 'Withdrawn')):
            expect(listed.nth(n)).to_contain_text(word)
        if viewport:
            no_horizontal_scroll(page)
    finally:
        context.close()


@pytest.mark.parametrize('mode', ['details', 'none'])
def test_coordinator_sees_recorded_leave_type_and_note_only_when_the_policy_shows_details(browser, live_server, h, mode):
    setting(h, mode)
    typ = selected(h)
    record = h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'leave_type_id': typ['id'], 'start': f'{day()}T00:00:00+03:00', 'end': f'{day(1)}T00:00:00+03:00',
        'private_reason': 'Recorded family note'})['record']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        facts = page.locator('.request-panel .request-facts')
        heading = page.locator('.request-panel h2')
        if mode == 'details':
            expect(rows(page).filter(has_text='Annual')).to_have_count(1)
            page.evaluate("id => location.hash = 'requests?open=' + id", record['id'])
            # The type is the panel's title; Details says only the note (not the type twice).
            expect(heading).to_have_text('Annual', timeout=20000)
            expect(facts).to_contain_text('Recorded family note')
            expect(facts).not_to_contain_text('Annual')
            h.configure('leave_types', value(typ, name='Vacation'), typ['id'], typ['revision'])
            page.reload()
            expect(heading).to_have_text('Vacation', timeout=20000)
            expect(facts).to_contain_text('Recorded family note')
        else:  # hidden: someone else's leave reads only as Leave, without its type or note
            expect(rows(page).filter(has_text='Leave')).to_have_count(1)
            expect(rows(page).filter(has_text='Annual')).to_have_count(0)
            page.evaluate("id => location.hash = 'requests?open=' + id", record['id'])
            expect(facts).to_contain_text('Dates', timeout=20000)
            expect(heading).not_to_contain_text('Annual')
            expect(facts).not_to_contain_text('Annual')
            expect(facts).not_to_contain_text('Recorded family note')
            h.configure('leave_types', value(typ, name='Vacation'), typ['id'], typ['revision'])
    finally:
        context.close()
    # Staff never see the recorded note, whatever the policy.
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        page.evaluate("id => location.hash = 'requests?open=' + id", record['id'])
        facts = page.locator('.request-panel .request-facts')
        expect(page.locator('.request-panel h2')).to_have_text('Vacation', timeout=20000)
        expect(facts).not_to_contain_text('Recorded family note')
    finally:
        context.close()


def test_assignable_leave_has_no_schedule_impact_or_blocking_status(browser, live_server, h):
    published(h)
    typ = h.configure('leave_types', {'name': 'Assignable study', 'short_code': 'ASSTDY',
                                     'counts_as_unavailable': False})['record']
    record = h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0], 'kind': 'recorded_leave',
        'leave_type_id': typ['id'], 'start': f'{day()}T00:00:00+03:00', 'end': f'{day(1)}T00:00:00+03:00',
        'private_reason': 'Synthetic study note'})['record']
    item = h.get('coordinator', '/api/requests/all?id=' + record['id'])['item']
    assert item['impact_count'] == 0 and item['impact'] == []
    assert item['schedule_impact'] == {'count': 0, 'published_count': 0, 'draft_count': 0, 'duties': []}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("id => location.hash = 'requests?open=' + id", record['id'])
        panel = page.locator('.request-panel')
        expect(panel).to_contain_text('Recorded; does not block duty assignment', timeout=20000)
        expect(panel).to_contain_text('No duty affected')
        expect(panel.locator('.request-impact-list li')).to_have_count(0)
    finally:
        context.close()


def request_over_published_and_draft(h):
    published(h)
    draft = h.workspace('Next rota', day(), day(7))
    h.shift(draft, f'{day(1)}T08:00:00+03:00', f'{day(1)}T12:00:00+03:00', people=[h.seed['people'][0]])
    return h.command('staff', 'request.create', {'person_id': h.seed['people'][0], 'kind': 'leave', 'private_reason': 'Planned leave',
        'start': f'{day()}T00:00:00+03:00', 'end': f'{day(2)}T00:00:00+03:00'})['record']


def test_reviewer_sees_published_and_draft_duties_a_request_affects(browser, live_server, h):
    request = request_over_published_and_draft(h)
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.evaluate("id => location.hash = 'requests?open=' + id", request['id'])
        panel = page.locator('.request-panel')
        expect(panel).to_contain_text('Affects 2 duties · 1 published, 1 not yet published', timeout=20000)
        duties = panel.locator('.request-impact-list li')
        expect(duties).to_have_count(2)
        expect(duties.filter(has_text='Requests rota').locator('.badge')).to_have_text('Published')
        expect(duties.filter(has_text='Next rota')).to_contain_text('08:00–12:00')
        expect(duties.filter(has_text='Next rota').locator('.badge')).to_have_count(1)
        expect(duties.filter(has_text='Next rota').locator('.badge')).to_have_text('Draft')
        cover = 'The coordinator arranges cover for its published duties.'
        expect(panel).to_contain_text(cover)
        panel.get_by_role('button', name='Approve', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve leave')
        expect(dialog).to_contain_text('Requests rota (Published)')
        expect(dialog).to_contain_text('1 duty in Next rota (not yet published)')
        expect(dialog).to_contain_text(cover)
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        duties.first.get_by_role('button', name=re.compile('^Show .* in Schedule$')).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['1440', '390'])
def test_staff_see_only_their_published_duty_in_schedule_impact(browser, live_server, h, viewport):
    request = request_over_published_and_draft(h)
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        page.evaluate("id => location.hash = 'requests?open=' + id", request['id'])
        panel = page.locator('.request-panel')
        expect(panel).to_contain_text("You're on duty", timeout=20000)  # their own request, said to them (R-25)
        expect(panel.locator('.request-impact-list li')).to_have_count(1)
        expect(panel).not_to_contain_text('Next rota')
        expect(panel).not_to_contain_text('arranges cover')
        if viewport:
            no_horizontal_scroll(page)
    finally:
        context.close()
