"""Available for overtime: eligible staff offer days from New request, a coordinator approves it, ineligible staff are
not offered the kind, and the approved offer shows on the person's Quotas & offers as one row."""
import copy
import re
import uuid

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_overtime_volunteer import offer_rule, offer, decide, facts, conditions, REASON
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, holding, wait_held  # noqa: F401
from .test_browser_requests import new_request, row, PHONE
from .test_browser_live_scopes import command

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_eligible_staff_offer_overtime_and_a_coordinator_approves(browser, live_server, h, viewport):
    offer_rule(h)
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Available for overtime')
        # The one separate-dates list (R-19): today is chosen; more come from its calendar (chips, Shift-click, paste).
        dates = dialog.get_by_role('group', name='Dates')
        expect(dates.locator('.date-chip')).to_have_count(1)
        expect(dates.get_by_role('button', name='Add dates')).to_be_visible()
        dialog.get_by_role('button', name='Send offer').click()
        expect(page.locator('#toast-tray').get_by_text('Overtime availability submitted', exact=False)).to_be_visible()
        if not viewport:
            expect(row(page, 'Available for overtime')).to_contain_text('1 date')
    finally:
        context.close()
    item = next(i for i in h.get('coordinator', '/api/requests/all')['items'] if i.get('kind') == 'overtime_availability')
    assert item['stage'] == 'waiting' and item['overtime']['dates']
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        navigate(page, f"requests?open={item['id']}")
        panel = page.get_by_role('region', name='Available for overtime')
        expect(panel.get_by_role('button', name='Approve', exact=True)).to_be_visible()
        expect(panel.locator('.request-status')).not_to_contain_text('decides')  # the decider needs no "A coordinator decides" (R-27)
        panel.get_by_role('button', name='Approve', exact=True).click()
        page.get_by_role('dialog', name='Approve available for overtime').get_by_role('button', name='Approve', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Approved · ', exact=False)).to_be_visible()
        assert h.get('coordinator', '/api/requests/all?id=' + item['id'])['item']['stage'] == 'approved'
        navigate(page, f"people?open={h.seed['people'][0]}&tab=facts")
        expect(page.get_by_text('Volunteered · 1 day')).to_be_visible()
        expect(page.get_by_role('button', name='Open the overtime request')).to_be_visible()
    finally:
        context.close()


def test_staff_without_an_overtime_line_are_not_offered_the_kind(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_role('radio', name='Leave', exact=False).first).to_be_visible()
        expect(dialog.get_by_role('radio', name='Available for overtime', exact=False)).to_have_count(0)
    finally:
        context.close()


def offers_overtime(page, offered):
    page.get_by_role('button', name='New request', exact=True).click()
    dialog = page.get_by_role('dialog', name='New request')
    expect(dialog.get_by_role('radio', name='Leave', exact=False).first).to_be_visible()
    expect(dialog.get_by_role('radio', name='Available for overtime', exact=False)).to_have_count(1 if offered else 0)
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()


def test_overtime_eligibility_recovers_from_a_failed_read_and_a_new_line(browser, live_server, h):
    """Neither a failed eligibility read nor an answer from before a new line sticks: the next visit to Requests asks again."""
    context, page = sign_in(browser, live_server, 'staff')
    try:
        failing = lambda route: route.fulfill(status=503, body='{}', content_type='application/json')
        page.route('**/api/requests/overtime-eligibility*', failing)
        navigate(page, 'requests')
        offers_overtime(page, False)
        page.unroute('**/api/requests/overtime-eligibility*', failing)
        navigate(page, 'home')
        navigate(page, 'requests')
        offers_overtime(page, False)  # answered, and no line applies yet
        # The new line is approved in another tab, through the live server, so this tab hears of it (a harness write
        # stays in the test process and sends no pulse).
        approver_context, approver = sign_in(browser, live_server, 'approver')
        offer_rule(h, activate=lambda rule: command(approver, 'policy.activate', {'reason': REASON}, rule['id'], rule['revision']))
        approver_context.close()
        navigate(page, 'home')
        navigate(page, 'requests')
        offers_overtime(page, True)
    finally:
        context.close()


def test_a_new_request_opened_before_eligibility_lands_gains_the_overtime_kind(browser, live_server, h):
    """New request opens at once with the kinds known then; the eligibility answer that lands afterwards adds Available for
    overtime to the open dialog instead of leaving it out until the next visit."""
    offer_rule(h)
    eligibility = '**/api/requests/overtime-eligibility*'
    context, page = sign_in(browser, live_server, 'staff')
    try:
        with holding(page) as held:
            held.hold(eligibility)
            navigate(page, 'requests')
            wait_held(page, held[eligibility])
            page.get_by_role('button', name='New request', exact=True).click()
            dialog = page.get_by_role('dialog', name='New request')
            expect(dialog.get_by_role('radio', name='Leave', exact=False).first).to_be_visible()
            expect(dialog.get_by_role('radio', name='Available for overtime', exact=False)).to_have_count(0)
            held.release(eligibility)
            offer = dialog.get_by_role('radio', name='Available for overtime', exact=False)
            expect(offer).to_have_count(1)
            offer.check()
            expect(dialog.get_by_role('button', name='Send offer')).to_be_visible()
    finally:
        context.close()


def test_staff_offer_overtime_for_a_clinic_their_line_allows(browser, live_server, h):
    """Staff have no department lists: the clinics and duty types they may choose come from their own overtime lines."""
    offer_rule(h)
    with transaction(h.app.state.engine) as db:
        clinic = db.get(m.Service, h.seed['services'][0]).name
        others = [db.get(m.Service, sid).name for sid in h.seed['services'][1:]]
        duty = db.get(m.AssignmentType, h.seed['type']).name
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Available for overtime')
        picker = dialog.get_by_label('Clinic', exact=True)
        expect(picker.locator('option')).to_have_text(['Any clinic allowed', clinic])  # only what the line allows
        assert not set(others) & set(picker.locator('option').all_inner_texts())
        picker.select_option(label=clinic)
        dialog.get_by_label('Duty type', exact=True).select_option(label=duty)
        dialog.get_by_role('button', name='Send offer').click()
        expect(page.locator('#toast-tray').get_by_text('Overtime availability submitted', exact=False)).to_be_visible()
        item = next(i for i in h.get('coordinator', '/api/requests/all')['items'] if i.get('kind') == 'overtime_availability')
        assert item['overtime']['service_id'] == h.seed['services'][0]
        assert item['overtime']['assignment_type_id'] == h.seed['type']
        navigate(page, 'home')
        navigate(page, f"requests?open={item['id']}")
        panel = page.get_by_role('region', name='Available for overtime')
        expect(panel).to_contain_text(clinic)  # reopened, the offer still names the clinic and duty type
        expect(panel).to_contain_text(duty)
    finally:
        context.close()



def test_staff_offer_overtime_for_one_clinic_on_an_unscoped_line(browser, live_server, h):
    """A line for any clinic still lets staff name one: the server lists the clinics and duty types they could work."""
    offer_rule(h, where=None)
    with transaction(h.app.state.engine) as db:
        clinics = sorted(db.get(m.Service, sid).name for sid in h.seed['services'])
        clinic_id = h.seed['services'][-1]
        clinic = db.get(m.Service, clinic_id).name
        duty = db.get(m.AssignmentType, h.seed['type']).name
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Available for overtime')
        picker = dialog.get_by_label('Clinic', exact=True)
        expect(picker.locator('option')).to_have_text(['Any clinic allowed', *clinics])
        picker.select_option(label=clinic)
        dialog.get_by_label('Duty type', exact=True).select_option(label=duty)
        dialog.get_by_role('button', name='Send offer').click()
        expect(page.locator('#toast-tray').get_by_text('Overtime availability submitted', exact=False)).to_be_visible()
    finally:
        context.close()
    item = next(i for i in h.get('coordinator', '/api/requests/all')['items'] if i.get('kind') == 'overtime_availability')
    assert (item['overtime']['service_id'], item['overtime']['assignment_type_id']) == (clinic_id, h.seed['type'])


def test_one_volunteered_day_under_two_overtime_lines_reads_as_one_day(browser, live_server, h):
    """Approving one date that two clinic lines cover makes a record per line; the person's profile still says 1 day."""
    first, second = h.seed['services'][:2]
    rule, _ = offer_rule(h, where=conditions('service_ids', first))
    lines = rule['definition']['rule']['lines']
    line = {**lines[0], 'id': str(uuid.uuid4()), 'where': conditions('service_ids', second)}
    rule = h.command('coordinator', 'rule.propose', {'catalogue_id': 'overtime_offers', 'expected_policy_id': rule['id'],
        'expected_revision': rule['revision'], 'lines': [*lines, line], 'reason': 'Synthetic second overtime line'})['record']
    h.command('approver', 'policy.activate', {'reason': REASON}, rule['id'], rule['revision'])
    decide(h, offer(h)['record'])  # no clinic chosen, so both lines apply
    assert len(facts(h)) == 2
    with transaction(h.app.state.engine) as db:
        clinics = [db.get(m.Service, sid).name for sid in (first, second)]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"people?open={h.seed['people'][0]}&tab=facts")
        volunteered = page.locator('.ph-rows li').filter(has_text='Volunteered')
        expect(volunteered).to_have_count(1)
        expect(volunteered).to_contain_text('Volunteered · 1 day')
        expect(volunteered).not_to_contain_text(' – ')  # one date, not a range
        for clinic in clinics:
            expect(volunteered).to_contain_text(clinic)
    finally:
        context.close()

def test_volunteered_row_counts_compact_days_and_shows_its_status(browser, live_server, h):
    """One record covers Mon 5 and Wed 7 Oct (a range with weekdays): the row says 2 days, and its status follows the
    facts' day — Ended after it, Scheduled before it, Active when a record applies, Not effective today otherwise."""
    offer_rule(h)
    decide(h, offer(h, dates=["2026-10-05", "2026-10-07"])['record'])
    [fact] = facts(h)
    assert (fact['starts_on'], fact['ends_on'], fact['data']['line']['weekdays']) == ('2026-10-05', '2026-10-08', [0, 2])
    assert 'offer_dates' not in fact['data']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def as_of(day, live):
            page.unroute('**/scheduling-facts*')
            page.route('**/scheduling-facts*', h.route_json('coordinator',
                f"/api/people/{h.seed['people'][0]}/scheduling-facts", lambda body: {
                    **body, 'on': day,
                    'effective': [{'policy_id': fact['id'], 'name': 'Volunteered'}] if live else []}))
            # A new tab memory: the facts are read again, not the reply this tab kept from the previous day (keptReads).
            page.reload()
            navigate(page, f"people?open={h.seed['people'][0]}&tab=facts")
            return page.locator('.ph-rows li').filter(has_text='Volunteered')
        for day, live, status in [('2026-10-20', True, 'Ended'), ('2026-10-01', True, 'Scheduled'),
                                  ('2026-10-06', True, 'Active'), ('2026-10-06', False, 'Not effective today')]:
            volunteered = as_of(day, live)
            expect(volunteered).to_contain_text('Volunteered · 2 days')
            expect(volunteered.locator('.ph-state')).to_have_text(status)
    finally:
        context.close()


def test_staff_withdraw_an_approved_overtime_offer(browser, live_server, h):
    offer_rule(h)
    row_ = offer(h)["record"]
    row_ = decide(h, row_)["record"]
    assert facts(h)  # the approved offer grants overtime
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, f"requests?open={row_['id']}")
        panel = page.get_by_role('region', name='Available for overtime')
        expect(panel).to_contain_text('Approved')
        panel.get_by_role('button', name='Withdraw offer', exact=True).click()
        page.get_by_role('dialog', name='Withdraw offer').get_by_role('button', name='Withdraw offer', exact=True).click()
        expect(page.locator('#toast-tray').get_by_text('Offer withdrawn.', exact=True)).to_be_visible()
    finally:
        context.close()
    assert h.get('coordinator', '/api/requests/all?id=' + row_['id'])['item']['stage'] == 'withdrawn'
    assert facts(h) == []  # withdrawing retires the offer it granted


def test_editing_an_older_overtime_offer_keeps_it_whenever_free(browser, live_server, h):
    """An offer saved before the volunteer setting existed reads as off, and changing it keeps it off."""
    person = h.seed['people'][0]
    row_ = h.command('coordinator', 'person.overtime.propose', {'kind': 'overtime_offer', 'person_id': person,
        'catalogue_id': 'overtime_offers', 'line': {'id': str(uuid.uuid4()),
            'where': conditions('service_ids', h.seed['services'][0]),
            'numbers': {'apply': {'value': None, 'level': 'medium'}},
            'settings': {'allocation_overtime': {'required_policy_ids': [], 'volunteer_required': False}}},
        'reason': REASON})['fact']
    row_ = h.command('approver', 'person.overtime.activate', {'reason': REASON}, row_['id'], row_['revision'])['fact']
    with transaction(h.app.state.engine, write=True) as db:  # as saved before the setting existed
        stored = db.get(m.SchedulingFact, row_['id'])
        data = copy.deepcopy(stored.data)
        data['line']['settings']['allocation_overtime'].pop('volunteer_required')
        stored.data = data
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f"people?open={person}&tab=facts")
        page.get_by_role('button', name=re.compile('^Change ')).filter(has_text='Change').first.click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_role('switch', name='Only when they volunteer')).not_to_be_checked()
        dialog.get_by_label('Reason', exact=True).fill('Synthetic limit change')
        dialog.locator('button[type=submit]').click()
        expect(dialog).to_be_hidden()
    finally:
        context.close()
    proposed = h.get('coordinator', f'/api/people/{person}/scheduling-facts')['proposed']
    assert [f['data']['line']['settings']['allocation_overtime'].get('volunteer_required') for f in proposed] == [False]
