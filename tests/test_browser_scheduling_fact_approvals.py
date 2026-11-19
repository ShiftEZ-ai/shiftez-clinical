"""Pending person scheduling facts (quotas, targets, overtime offers, calendar protections) in the shared approvals:
Home's Needs you and the Requests collection, panel and decisions, per role, on desktop and phone."""
import os
import re
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import expect

from packages.contracts.schemas import ROLE_NAMES
from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import browser, live_server, fonts_ready, navigate, toolbar, whole_list_if_waiting  # noqa: F401
from .test_browser_requests import rows, no_horizontal_scroll
from .test_browser_rule_catalogue_v2 import themed, facts_panel, choose, reason
from .test_scheduling_facts_api import workload

pytestmark = pytest.mark.browser
SUMMARY = 'At most 3 duties per calendar week · Mandatory'  # the Rules' line sentence (D-094: never the server's summary)


def shot(page, name, width):
    """CATV2_SHOTS saves the Needs you and Requests captures for review."""
    fonts_ready(page)
    no_horizontal_scroll(page)
    if os.getenv('CATV2_SHOTS'):
        folder = Path(os.environ['CATV2_SHOTS'])
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"fact-{name}-{'desktop' if width == 1440 else 'phone'}-light.png"), animations='disabled')


def propose(h, person, maximum=3):
    return h.command('coordinator', 'person.workload.propose', workload(h, maximum, person_id=person))['fact']


def listed(page, width):
    return page.locator('#main .dt-card') if width == 390 else rows(page)


@pytest.mark.parametrize('width', [1440, 390])
def test_coordinator_proposes_approver_approves_from_needs_you_then_undo(browser, live_server, h, width):
    person = h.seed['people'][1]
    coordinator, page = themed(browser, live_server, 'coordinator')
    approver, review = None, None
    try:
        # The coordinator proposes a personal quota on the person (desktop: People is an operator screen).
        panel = facts_panel(page, person)
        panel.get_by_role('button', name='Add a personal quota', exact=True).click()
        dialog = page.get_by_role('dialog', name='Add a personal quota', exact=True)
        dialog.get_by_label('Maximum', exact=True).fill('3')
        choose(page, dialog, 'Period', 'Calendar week')
        reason(page, 'Part-time this term', 'Propose')
        expect(panel.locator('.ph-section').filter(has_text='Waiting for approval')).to_be_visible()
        [fact] = h.get('auditor', f'/api/people/{person}/scheduling-facts')['proposed']

        # The approver sees it in Needs you, with who proposed it, and opens it in Requests.
        approver, review = themed(browser, live_server, 'approver', width)
        need = review.locator('.home-needs li').filter(has_text='Person fact to approve')
        expect(need).to_contain_text('Jordan Lee')
        expect(need).to_contain_text(SUMMARY)
        expect(need).to_contain_text('proposed by Coordinator · Demo')
        shot(review, 'needs-you', width)
        need.get_by_role('button', name='Review', exact=True).click()
        side = review.get_by_role('region', name='Personal workload')
        expect(side).to_contain_text(SUMMARY)
        expect(side).to_contain_text('proposed by Coordinator · Demo')
        expect(side.locator('.request-history')).to_contain_text('Proposed')
        shot(review, 'requests-panel', width)

        # Approve: the shared fact review, a reason, then the fact applies; Undo from the message takes it back.
        side.get_by_role('button', name='Approve', exact=True).click()
        reason(review, 'Agreed with the person', 'Approve')
        toast = review.locator('#toast-tray .toast', has_text='Change approved.')
        expect(toast).to_be_visible()
        facts = h.get('auditor', f'/api/people/{person}/scheduling-facts')
        assert [f['id'] for f in facts['active']] == [fact['id']] and not facts['proposed']
        assert 'scheduling_facts' not in {a['key'] for a in h.get('approver', '/api/home')['attention']}
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(review.locator('#toast-tray').get_by_text('Undone.', exact=False)).to_be_visible()
        assert not h.get('auditor', f'/api/people/{person}/scheduling-facts')['active']
    finally:
        coordinator.close()
        if approver:
            approver.close()


@pytest.mark.parametrize('role,width', [('approver', 1440), ('approver', 390), ('coordinator', 1440), ('coordinator', 390),
                                        ('programme_lead', 1440), ('auditor', 1440), ('staff', 1440), ('staff', 390)])
def test_each_role_sees_and_decides_only_what_it_may(browser, live_server, h, role, width):
    member, outsider = h.seed['people'][0], h.seed['people'][2]
    if role == 'programme_lead':
        identity(h, role, [role], person=h.seed['people'][1])
        programme = h.configure('programmes', {'name': 'Synthetic programme', 'starts_on': '2026-01-01'})['record']
        with transaction(h.app.state.engine, write=True) as db:
            db.add_all([m.ProgrammeMembership(department_id=h.seed['department_id'], person_id=pid, programme_id=programme['id'],
                                              starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)) for pid in h.seed['people'][:2]])
    propose(h, member)
    propose(h, outsider, 4)
    context, page = themed(browser, live_server, role, width)
    try:
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        navigate(page, 'requests')
        whole_list_if_waiting(page)  # a decided fact stays listed; Waiting for you drops it (998bbca16)
        cards = listed(page, width)
        facts = cards.filter(has_text='Personal workload')
        expect(page.locator('#main[data-loaded-view]')).to_have_count(1)
        if role == 'staff':
            # Staff never see colleagues' (or their own) fact approvals.
            expect(page.locator('#main').get_by_text('No requests yet')).to_be_visible()
            expect(facts).to_have_count(0)
            return
        expect(facts).to_have_count(1 if role == 'programme_lead' else 2)
        first = facts.filter(has_text='Alex Morgan')
        expect(first).to_contain_text('proposed by Coordinator · Demo')
        if role == 'programme_lead':
            expect(cards.filter(has_text='Taylor Noor')).to_have_count(0)
        decides = role == 'approver'
        expect(first.get_by_role('button', name='Approve', exact=True)).to_have_count(1 if decides else 0)
        first.locator('.request-title strong, .dt-card-title').first.click()
        side = page.get_by_role('region', name='Personal workload')
        expect(side).to_contain_text(SUMMARY)
        expect(side.get_by_role('button', name='Approve', exact=True)).to_have_count(1 if decides else 0)
        expect(side.get_by_role('button', name='Decline', exact=True)).to_have_count(1 if decides else 0)
        expect(side.locator('.request-status')).to_contain_text(
            'You proposed it; another approver decides' if role == 'coordinator' else 'An approver decides')
        if decides:
            side.get_by_role('button', name='Decline', exact=True).click()
            reason(page, 'Not this term', 'Decline')
            expect(page.locator('#toast-tray').get_by_text('Declined.', exact=False)).to_be_visible()
            expect(facts).to_have_count(2)  # All requests retains the authorised fact history.
            if width == 390:
                # Cards say the shared status once in the group heading; the declined card must belong to that group.
                status_group = first.locator('xpath=parent::div/preceding-sibling::div[contains(@class,"dt-card-group")][1]')
                expect(status_group.locator('.dt-group-toggle')).to_have_attribute('data-group', 'Declined')
                side.get_by_role('button', name='Close panel', exact=True).click()
            else:
                expect(first).to_contain_text('Declined')
            expect(first.get_by_role('button', name='Approve', exact=True)).to_have_count(0)
            assert h.get('auditor', f'/api/people/{member}/scheduling-facts')['history'][0]['status'] == 'declined'
            toolbar(page, 'Saved views').get_by_role('menuitemradio', name=re.compile('^Waiting for approval')).click()
            expect(facts).to_have_count(1)
            expect(facts).to_contain_text('Taylor Noor')
            expect(facts.filter(has_text='Alex Morgan')).to_have_count(0)
        if width == 390:
            no_horizontal_scroll(page)
        assert not errors, errors
    finally:
        context.close()


def test_auditor_and_programme_lead_reach_requests_from_navigation(browser, live_server, h):
    propose(h, h.seed['people'][0])
    context, page = themed(browser, live_server, 'auditor')
    try:
        page.locator('#department-nav [data-route=requests]').click()
        expect(rows(page).filter(has_text='Personal workload')).to_have_count(1)
        expect(page.get_by_role('button', name='New request', exact=True)).to_have_count(0)
        expect(page.locator('#main input.bulk-check')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_pure_approver_discovers_terminal_calendar_protection_and_restores_it(browser, live_server, h, width, theme):
    from uuid import uuid4
    from .test_scheduling_facts_api import decide
    programme = h.configure('programmes', {'name': 'Fictional approval programme', 'starts_on': '2026-01-01'})['record']
    entry = h.configure('programme_calendar_entries', {'name': 'Fictional protected block', 'programme_id': programme['id'],
        'kind': 'academic_block', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'})['record']
    fact = h.command('coordinator', 'calendar.protection.propose', {'kind': 'protected_time', 'calendar_entry_id': entry['id'],
        'catalogue_id': 'protected_time', 'line': {'id': str(uuid4()), 'weekdays': [1], 'start_time': '09:00', 'end_time': '11:00',
            'starts_on': entry['starts_on'], 'ends_on': entry['ends_on'], 'numbers': {'apply': {'value': None, 'level': 'mandatory'}}},
        'reason': 'Fictional reviewed calendar protection'})['fact']
    fact = decide(h, fact)['fact']
    fact = decide(h, fact, action='retire')['fact']
    assert h.get('approver', '/api/me')['roles'] == ['approver']
    context, page = themed(browser, live_server, 'approver', width)
    try:
        page.emulate_media(color_scheme=theme)
        assert [row['id'] for row in h.get('approver', '/api/requests/all?decided=true&type=other&status=withdrawn')['items']] == [fact['id']]
        navigate(page, 'requests?type=other&status=withdrawn')
        row = page.locator(f'#main [data-row="{fact["id"]}"]')
        expect(row).to_be_visible()
        # D-094: the requested days (ends_on exclusive) and weekdays, never "Proposed <date>" or the server's ISO text.
        if width == 1440:  # the table's Dates and Details columns; the phone card carries fewer properties
            expect(row).to_contain_text('4–10 Oct 2026')
            expect(row).to_contain_text('Tue 09:00–11:00')
        expect(row).not_to_contain_text(re.compile(r'\d{4}-\d{2}-\d{2}|Proposed \d|Tuesday'))
        row.locator('.request-title strong, .dt-card-title').first.click()
        page.get_by_role('button', name='Manage protection', exact=True).click()
        panel = page.get_by_role('region', name='Protected time', exact=True)
        expect(panel.locator('.badge').filter(has_text='Retired')).to_have_count(1)
        expect(panel.get_by_role('button', name='Add protection', exact=True)).to_have_count(0)
        panel.get_by_role('button', name='Restore', exact=True).click()
        reason(page, 'Restore independently approved fictional protection', 'Restore')
        expect(panel.locator('.badge').filter(has_text='Active')).to_have_count(1)
        saved = h.get('approver', f"/api/programme-calendar-entries/{entry['id']}/protections")['protections']
        assert len(saved) == 1 and saved[0]['id'] == fact['id'] and saved[0]['status'] == 'active'
        page.reload()
        navigate(page, f"requests?open={fact['id']}")
        expect(page.get_by_role('button', name='Manage protection', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role', [role for role in ROLE_NAMES
                                  if role in {'staff', 'programme_lead'}])
def test_staff_and_lead_staff_person_filter_preserves_person_and_programme_scope(browser, live_server, h, role):
    member, colleague, outsider = h.seed['people']
    programme = h.configure('programmes', {'name': 'Fictional scoped programme', 'starts_on': '2026-01-01'})['record']
    for pid in (member, colleague):
        h.configure('programme_memberships', {'person_id': pid, 'programme_id': programme['id'],
            'starts_on': '2026-01-01', 'ends_on': '2027-01-01'})
    identity(h, 'lead-staff', ['programme_lead', 'staff'], person=member)
    records = {}
    for pid in (member, colleague, outsider):
        records[pid] = h.command('coordinator', 'request.create', {'person_id': pid, 'start': '2026-10-05T08:00:00+03:00',
            'end': '2026-10-05T12:00:00+03:00', 'private_reason': 'Fictional person filtering regression'})['record']['id']
    context, page = themed(browser, live_server, 'lead-staff' if role == 'programme_lead' else 'staff', 1440)
    try:
        for pid in (colleague, outsider, member):
            navigate(page, f'requests?person={pid}')
            if role == 'staff' and pid != member:
                expect(page.locator('#main')).to_contain_text('You can view only your own requests.')
            elif role == 'programme_lead' and pid == colleague:
                expect(page.locator(f'#main [data-row="{records[pid]}"]')).to_have_count(1)
                expect(page.locator(f'#main [data-row="{records[member]}"]')).to_have_count(0)
            elif pid == outsider:
                expect(page.locator('#main [data-row]')).to_have_count(0)
                expect(page.locator(f'#main [data-row="{records[member]}"]')).to_have_count(0)
            else:
                expect(page.locator(f'#main [data-row="{records[member]}"]')).to_have_count(1)
    finally:
        context.close()
# Include this file in hosted supervision and team-lead qualification.
