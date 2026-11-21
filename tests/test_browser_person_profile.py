"""The person hub (People › a person): every permitted source in one panel, exact links to each source and back, and
the actor's own authority deciding what is read — for every role, desktop and phone, light and dark."""
from .browser_seed import seeded
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import developer, identity
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, wait_held, wait_until  # noqa: F401
from .test_browser_people_merged import expect_tabs, row
from .test_browser_requests import published, day
from .test_browser_audit_request_swaps import offer_payload
from .test_browser_audit_date_units import stored_theme
from .test_browser_date_picker import calendar
from .test_browser_w3_setup_today import no_page_overflow
from .test_leave_disclosure import setting

pytestmark = pytest.mark.browser
PRIVATE = 'Private dentist appointment'
# Tabs per role when opening someone else (people[0] is the staff account's own person).
W, L = 'Workload & fairness', 'Leave & requests'
TABS = {'admin': ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers', 'Access'], 'coordinator': ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers'],
        'approver': ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers'], 'credentialer': ['Overview', 'Schedule'],
        'programme_lead': ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers'], 'auditor': ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers', 'History'],
        'staff': ['Overview', 'Schedule', W, L, 'Quotas & offers', 'Access'], 'unlinked': []}
# Reads that would expose another person's private records, per role (never made for a role that may not read them).
# Person-record reads: sign-ins and devices only for administrators and the person; leave for the request roles.
ACCESS, LEAVE, RECORD = ('/accounts', '/sessions'), ('/availability?',), ('/workload', '/fairness', '/score')
FORBIDDEN = {'credentialer': ('/api/calendar?', '/api/requests/all', '/api/audit', '/api/access', '/api/wishes', *ACCESS, *LEAVE, *RECORD),
             'programme_lead': ('/api/audit', '/api/access', *ACCESS, *LEAVE),
             'auditor': ('/api/requests/all', '/api/access', *ACCESS, *LEAVE), 'approver': ('/api/audit', '/api/access', '/api/wishes', *ACCESS),
             'coordinator': ('/api/audit', '/api/access', '/api/wishes', *ACCESS), 'admin': ('/api/audit', '/api/wishes', '/api/access'),
             'staff': ('/api/catalogues', '/api/audit', '/api/access'),
             'unlinked': ('/api/me/profile', '/api/calendar?', '/api/requests/all', '/api/audit', '/api/access', '/api/catalogues', *ACCESS, *LEAVE, *RECORD)}


def person(h, index=0):
    return next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][index])


def open_person(page, p):
    navigate(page, 'people')
    page.get_by_label('Search people', exact=True).fill(p['display_name'])
    row(page, p['display_name']).locator('strong').first.click()
    panel = page.get_by_role('region', name=p['display_name'], exact=True)
    expect(panel).to_be_visible()
    return panel


def section(panel, title):
    return panel.locator('.ph-section').filter(has=panel.page.get_by_role('heading', name=title, exact=True))


def shot(page, name):
    if folder := os.getenv('PW_COVERAGE_ARTIFACT_DIR'):
        output = Path(folder) / 'person-profile'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / (name + '.png')), full_page=True)


@seeded
def related(h, programme_role='admin'):
    """A published duty, a recorded absence with a private note, and actual programme records for people[0]."""
    published(h)
    a, b = h.seed['people'][:2]
    dates = {'starts_on': day(), 'ends_on': day(7)}
    programme = h.configure('programmes', {'name': 'Profile continuity programme', **dates}, role=programme_role)['record']
    cohort = h.configure('cohorts', {'name': 'Profile cohort', 'programme_id': programme['id'], **dates})['record']
    h.configure('programme_memberships', {'person_id': a, 'programme_id': programme['id'], **dates})
    h.configure('mentorships', {'person_id': a, 'mentor_id': b, 'programme_id': programme['id'], **dates})
    h.configure('rotations', {'person_id': a, 'mentor_id': b, 'cohort_id': cohort['id'],
                            'service_id': h.seed['services'][0], 'department_available': True, **dates})
    h.configure('programme_calendar_entries', {'name': 'Profile academic block', 'kind': 'academic_block',
                                              'programme_id': programme['id'], **dates})
    h.command('coordinator', 'availability.record', {'person_id': a, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
              'start': f'{day(2)}T09:00:00+03:00', 'end': f'{day(2)}T12:00:00+03:00', 'private_reason': PRIVATE})
    request = h.command('staff', 'request.create', {'person_id': a, 'start': f'{day(1)}T09:00:00+03:00',
                        'end': f'{day(1)}T11:00:00+03:00', 'private_reason': 'Profile request reason', 'kind': 'leave'})['record']
    h.command('coordinator', 'request.create', {'person_id': b, 'start': f'{day(2)}T09:00:00+03:00',
              'end': f'{day(2)}T11:00:00+03:00', 'private_reason': 'Other person private request', 'kind': 'meeting'})
    return request


def profile_dates(tab, first, last):
    start, end = tab.get_by_label('From', exact=True), tab.get_by_label('Through', exact=True)
    expect(start).to_be_attached()
    if start.is_visible():
        start.fill(first)
        end.fill(last)
        end.press('Tab')
        return
    # Phone panels expose the shared calendar, rather than the typed date fields.
    pop = calendar(tab.page, tab)
    for value in (first, last):
        target = date.fromisoformat(value)
        focus = pop.locator('.dp-day[tabindex="0"]')
        current = date.fromisoformat(focus.get_attribute('data-day'))
        months = (target.year - current.year) * 12 + target.month - current.month
        direction = 'PageDown' if months > 0 else 'PageUp'
        # The picker's year keys keep old history periods quick to reach.
        for _ in range(abs(months) // 12):
            focus.press('Shift+' + direction)
        for _ in range(abs(months) % 12):
            focus.press(direction)
        pop.locator(f'.dp-day[data-day="{value}"]').click()
    pop.get_by_role('button', name=re.compile(r'^Use ')).click()
    expect(pop).to_be_hidden()
    for field, value in ((start, first), (end, last)):
        selected = date.fromisoformat(value)
        expect(field).to_have_value(f'{selected:%a} {selected.day} {selected:%b %Y}')


def work_dates(panel, first, last):
    profile_dates(panel.get_by_role('tabpanel', name='Schedule', exact=True), first, last)


@pytest.mark.parametrize('role', list(TABS))
@pytest.mark.parametrize('width,theme', [(1440, 'light'), (390, 'dark')])
def test_every_role_reads_only_its_permitted_sources(browser, live_server, h, role, width, theme):
    if role == 'programme_lead':
        # A programme lead reads the programmes they lead; creating one makes them its lead (training.lead_programmes).
        identity(h, role, [role])
    related(h, role if role == 'programme_lead' else 'admin')
    if role == 'unlinked':
        identity(h, role, ['staff'])
    target = person(h)  # staff: their own record; everyone else: a colleague
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url else None)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        stored_theme(page, theme)
        reads.clear()
        panel = open_person(page, target)
        tabs = panel.get_by_role('tab')
        if TABS[role]:
            expect_tabs(tabs, TABS[role])
        else:
            expect(tabs).to_have_count(0)
            expect(panel.locator('.pc')).to_be_visible()  # the person card (person_card.ts) alone
        for i in range(len(TABS[role])):
            tabs.nth(i).click()
            expect(tabs.nth(i)).to_have_attribute('aria-selected', 'true')
            expect(panel.locator('.loading[aria-busy=true]')).to_have_count(0)
            no_page_overflow(page)
        if 'Schedule' in TABS[role]:
            panel.get_by_role('tab', name='Schedule', exact=True).click()
            work_dates(panel, day(), day(6))
            expect(section(panel, 'Academic calendar')).to_contain_text('Profile academic block')
            if role != 'credentialer':
                # The absence reads as the department setting allows (default: its type, never the private note).
                expect(panel.locator('[data-table="person-work"]')).to_contain_text('Annual')
        # Private records stay with the roles that may read them.
        expect(panel).not_to_contain_text(PRIVATE)
        expect(panel).not_to_contain_text('Other person private request')
        if role == 'programme_lead':
            requests = [urlparse(u) for u in reads if urlparse(u).path == '/api/requests/all']
            assert requests, 'A programme lead can read this trainee’s requests'
            assert all(parse_qs(u.query).get('person_id') == [target['id']] for u in requests), reads
        assert not [u for u in reads if any(f in u for f in FORBIDDEN[role])], reads
        assert not errors, errors
        shot(page, f'hub-{role}-{width}-{theme}')
    finally:
        context.close()


@pytest.mark.parametrize('value,label,note', [('none', 'On leave or away', False), ('type', 'Annual', False), ('details', 'Annual', True)])
def test_work_absence_follows_the_leave_reason_setting(browser, live_server, h, value, label, note):
    related(h)
    if value != 'type':
        setting(h, value)
    for role, own in [('coordinator', False), ('staff', True)]:
        context, page = sign_in(browser, live_server, role)
        try:
            panel = open_person(page, person(h))
            panel.get_by_role('tab', name='Schedule', exact=True).click()
            work_dates(panel, day(), day(6))
            absence = panel.locator('[data-table="person-work"] [data-row^="availability:"]')
            expect(absence).to_contain_text('Annual' if own else label)
            if note and not own:
                expect(absence).to_contain_text(PRIVATE)
            else:
                expect(absence).not_to_contain_text(PRIVATE)
        finally:
            context.close()


@pytest.mark.parametrize('role,width,theme', [('credentialer', 1440, 'light'), ('staff', 390, 'dark')])
def test_person_skills_show_clinic_scope_for_catalogue_and_self_records(browser, live_server, h, role, width, theme):
    p = person(h)
    clinic = h.get('coordinator', '/api/catalogues')['services'][0]
    for label, service in [('Profile clinic skill', clinic['id']), ('Profile general skill', None)]:
        skill = h.configure('capabilities', {'name': label, 'starts_on': '2020-01-01'})['record']
        h.command('credentialer', 'grant.create', {'person_id': p['id'], 'capability_id': skill['id'],
                  'service_id': service, 'starts_on': '2026-01-01', 'evidence': 'Synthetic scoped qualification'})
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        panel = open_person(page, p)
        skills = section(panel, 'Skills')
        for label, scope in [('Profile clinic skill', clinic['name']), ('Profile general skill', 'All clinics')]:
            expect(skills.locator('.ph-row').filter(has_text=label)).to_contain_text(scope)
        # Read-only for staff (their own records), actionable for the credentialer.
        expect(skills.get_by_role('button', name='Grant skill', exact=True)).to_have_count(1 if role == 'credentialer' else 0)
        page.reload()
        expect(section(panel, 'Skills').locator('.ph-row').filter(has_text='Profile clinic skill')).to_contain_text(clinic['name'])
        no_page_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('role,width,theme', [('approver', 1440, 'dark'), ('auditor', 390, 'light')])
def test_person_rules_show_only_their_exceptions_and_preserve_approval_authority(browser, live_server, h, role, width, theme):
    p, other = person(h), person(h, 1)
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    payload = {'policy_id': policy['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    own = h.command('coordinator', 'exception.create', {**payload, 'person_id': p['id'], 'reason': 'Profile bounded exception'})['record']
    unrelated = h.command('coordinator', 'exception.create', {**payload, 'person_id': other['id'], 'reason': 'Other person exception'})['record']
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        panel = open_person(page, p)
        panel.get_by_role('tab', name='Rules', exact=True).click()
        exceptions = panel.locator('[data-table="person-exceptions"]')
        record = exceptions.locator(f'[data-row="{own["id"]}"]')
        expect(record).to_contain_text('Profile bounded exception')
        expect(exceptions.locator(f'[data-row="{unrelated["id"]}"]')).to_have_count(0)
        expect(record).to_contain_text('4 Oct 2026 – 10 Oct 2026')
        # The exact rule opens in its place; Back returns to the person, on the same tab, at the same link.
        link = record.get_by_role('button', name=policy['name'], exact=True)
        link.click()
        source = page.locator('.rule-sheet')
        expect(source).to_have_attribute('data-rule-ids', policy['id'])
        source.get_by_role('button', name='Back to ' + p['display_name'], exact=True).click()
        expect(panel.get_by_role('tab', name='Rules', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(record.get_by_role('button', name=policy['name'], exact=True)).to_be_focused()
        approve = record.get_by_role('button', name='Approve exception', exact=True)
        if role == 'approver':
            approve.click()
            dialog = page.get_by_role('dialog', name='Approve bounded exception', exact=True)
            dialog.get_by_label('Reason', exact=True).fill('Independent profile exception review')
            dialog.get_by_role('button', name='Confirm', exact=True).click()
            expect(dialog).to_have_count(0)
            expect(record).to_contain_text('Approved')
        else:
            expect(approve).to_have_count(0)
        page.reload()
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(record).to_contain_text('Approved' if role == 'approver' else 'Waiting for approval')
        expect(exceptions.locator(f'[data-row="{unrelated["id"]}"]')).to_have_count(0)
        no_page_overflow(page)
        # A retired rule keeps its exact (historical) source.
        current = next(r for r in h.get('admin', '/api/catalogues')['policies'] if r['id'] == policy['id'])
        h.command('approver', 'policy.retire', {'reason': 'Historical profile source'}, policy['id'], current['revision'])
        page.reload()
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(record.get_by_role('button', name='Approve exception', exact=True)).to_have_count(0)
        record.get_by_role('button', name=policy['name'], exact=True).click()
        source = page.locator('.rule-sheet')
        expect(source).to_have_attribute('data-rule-ids', policy['id'])
        expect(source.get_by_role('button', name='Edit', exact=True)).to_have_count(0)
        source.get_by_role('button', name='Back to ' + p['display_name'], exact=True).click()
        expect(record).to_contain_text('Profile bounded exception')
    finally:
        context.close()
    saved = {r['id']: r for r in h.get('coordinator', '/api/catalogues')['exceptions']}
    assert saved[own['id']]['status'] == ('approved' if role == 'approver' else 'proposed')
    assert saved[unrelated['id']]['status'] == 'proposed'


def test_person_exception_approval_respects_aliases_self_and_explicit_permission(browser, live_server, h):
    actor, beneficiary = person(h), person(h, 1)
    identity(h, 'profile_exception_creator', ['coordinator'], person=actor['id'])
    reviewer = identity(h, 'profile_exception_alias', ['approver'], person=actor['id'])
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    payload = {'policy_id': policy['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    alias = h.command('profile_exception_creator', 'exception.create', {**payload, 'person_id': actor['id'],
                      'reason': 'Same person alternate sign-in'})['record']
    own = h.command('coordinator', 'exception.create', {**payload, 'person_id': actor['id'],
                    'reason': 'Exception for the reviewer'})['record']
    independent = h.command('coordinator', 'exception.create', {**payload, 'person_id': beneficiary['id'],
                            'reason': 'Independent request for a colleague'})['record']
    context, page = sign_in(browser, live_server, 'profile_exception_alias')
    try:
        panel = open_person(page, beneficiary)
        panel.get_by_role('tab', name='Rules', exact=True).click()
        table = panel.locator('[data-table="person-exceptions"]')
        independent_row = table.locator(f'[data-row="{independent["id"]}"]')
        expect(independent_row.get_by_role('button', name='Approve exception', exact=True)).to_be_visible()

        panel = open_person(page, actor)
        panel.get_by_role('tab', name='Rules', exact=True).click()
        alias_row = panel.locator(f'[data-table="person-exceptions"] [data-row="{alias["id"]}"]')
        own_row = panel.locator(f'[data-table="person-exceptions"] [data-row="{own["id"]}"]')
        expect(alias_row).to_be_visible()
        expect(alias_row.get_by_role('button', name='Approve exception', exact=True)).to_have_count(0)
        expect(own_row).to_be_visible()
        expect(own_row.get_by_role('button', name='Approve exception', exact=True)).to_have_count(0)
        # A deliberate waiver changes the authority after reload, including identity aliases and the subject.
        h.command('admin', 'self_approval.grant', {'identity_id': reviewer, 'actions': ['exception.approve'],
                  'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Synthetic explicit exception review permission'})
        page.reload()
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(own_row.get_by_role('button', name='Approve exception', exact=True)).to_be_visible()
        alias_row.get_by_role('button', name='Approve exception', exact=True).click()
        dialog = page.get_by_role('dialog', name='Approve bounded exception', exact=True)
        dialog.get_by_label('Reason', exact=True).fill('Reviewed using the explicit exception permission')
        dialog.get_by_role('button', name='Confirm', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(alias_row).to_contain_text('Approved')
        page.reload()
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(alias_row).to_contain_text('Approved')
        expect(alias_row.get_by_role('button', name='Approve exception', exact=True)).to_have_count(0)
        panel = open_person(page, beneficiary)
        panel.get_by_role('tab', name='Rules', exact=True).click()
        expect(independent_row).to_contain_text('Waiting for approval')
    finally:
        context.close()
    saved = {r['id']: r for r in h.get('coordinator', '/api/catalogues')['exceptions']}
    assert saved[alias['id']]['status'] == 'approved'
    assert saved[alias['id']]['approved_by'] == reviewer
    assert saved[own['id']]['status'] == saved[independent['id']]['status'] == 'proposed'


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_person_hub_journeys_to_each_source_and_back(browser, live_server, h, width, theme):
    request = related(h)
    p, mentor = person(h), person(h, 1)
    clinic = next(s['name'] for s in h.get('coordinator', '/api/catalogues')['services'] if s['id'] == h.seed['services'][0])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        stored_theme(page, theme)
        panel = open_person(page, p)
        expect_tabs(panel.get_by_role('tab'), ['Overview', 'Schedule', W, L, 'Rules', 'Quotas & offers'])
        expect(panel.locator('.ph-section h3')).to_have_text(['Details', 'Rank', 'Programmes', 'Clinics', 'Skills'])
        expect(section(panel, 'Programmes')).to_contain_text('Profile continuity programme')
        shot(page, f'overview-{width}-{theme}')
        # Work: published duties and time away, then rotations, attachments and academic dates, each once.
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        work_dates(panel, day(), day(6))
        work = panel.locator('[data-table="person-work"]')
        expect(work.locator('[data-row^="duty:"]')).to_have_count(1)
        expect(work.locator('[data-row^="rotation:"]')).to_have_count(0)
        expect(section(panel, 'Rotations')).to_contain_text(clinic + ' · Profile cohort')
        expect(section(panel, 'Attachments')).to_contain_text('With ' + mentor['display_name'])
        expect(section(panel, 'Academic calendar')).to_contain_text('Profile academic block')
        d = date.fromisoformat(day())
        expect(work.locator('[data-row^="duty:"] .date-units > span')).to_have_text(
            [f'{d.day} {d:%b %Y} 08:00', '– 12:00'])
        no_page_overflow(page)
        shot(page, f'work-{width}-{theme}')
        # Requests: the same records and decisions as the Requests page; its detail goes Back to the person.
        panel.get_by_role('tab', name=L, exact=True).click()
        requests = panel.get_by_role('tabpanel', name=L, exact=True)
        item = requests.locator(f'[data-row="{request["id"]}"]')
        expect(item).to_be_visible()
        expect(requests.locator('.pr-requests [data-row]')).to_have_count(2)  # the request and the recorded absence; not a colleague's
        expect(requests.get_by_role('button', name='Wish calendar', exact=True)).to_have_count(0)
        item.get_by_role('button').first.click()
        detail = page.locator('.request-panel')
        expect(detail).to_contain_text('Profile request reason')
        detail.get_by_role('button', name='Back to ' + p['display_name'], exact=True).click()
        expect(panel.get_by_role('tab', name=L, exact=True)).to_have_attribute('aria-selected', 'true')
        expect(requests.locator(f'[data-row="{request["id"]}"]')).to_be_visible()
        page.wait_for_function("id => document.activeElement?.closest('[data-row]')?.dataset.row === id", arg=request['id'])
        shot(page, f'leave-{width}-{theme}')
        # A related person opens their own exact profile (a reloadable address).
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        section(panel, 'Attachments').get_by_role('button', name=mentor['display_name'], exact=True).click()
        expect(page).to_have_url(re.compile(r'#people\?open=' + mentor['id'] + '$'))
        expect(page.get_by_role('region', name=mentor['display_name'], exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role('region', name=mentor['display_name'], exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('action', ['restore-request', 'keep-user-focus', 'close-panel', 'leave-page'])
def test_back_to_person_keeps_focus_intent_while_requests_load(browser, live_server, h, action):
    request, p = related(h), person(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 900})
    try:
        panel = open_person(page, p)
        panel.get_by_role('tab', name=L, exact=True).click()
        item = panel.locator(f'.pr-requests [data-row="{request["id"]}"]')
        item.get_by_role('button').first.click()
        detail = page.locator('.request-panel')
        expect(detail).to_contain_text('Profile request reason')
        held = []
        page.route('**/api/requests/all?person_id=**', lambda route: held.append(route))
        detail.get_by_role('button', name='Back to ' + p['display_name'], exact=True).click()
        wait_held(page, held)
        # A pending read may outlast the former three-second focus observer.
        page.clock.fast_forward(3500)
        chosen = panel.get_by_role('button', name='Close panel', exact=True)
        if action == 'keep-user-focus':
            chosen.focus()
        if action == 'leave-page':
            with page.expect_event('requestfailed', predicate=lambda request: '/api/requests/all?person_id=' in request.url):
                navigate(page, 'home')
            expect(page.locator('.dock-panel')).to_have_count(0)
            assert page.evaluate("!document.activeElement?.closest('.dock-panel')")
            return
        if action == 'close-panel':
            chosen.click()
            expect(page.locator('.dock-panel')).to_have_count(0)
            focused = page.evaluate_handle('document.activeElement')
            # The late read settles (answered, or dropped by the panel that closed): either way it must change nothing.
            settled = []
            for event in ('requestfinished', 'requestfailed'):
                page.on(event, lambda request: settled.append(request) if '/api/requests/all?person_id=' in request.url else None)
            held.pop().continue_()
            wait_until(page, lambda: settled, "the person's late requests read never settled")
            expect(page.locator('.dock-panel')).to_have_count(0)
            assert page.evaluate('(before) => document.activeElement === before', focused)
            return
        held.pop().continue_()
        expect(item).to_be_visible()
        expect(chosen if action == 'keep-user-focus' else item.get_by_role('button').first).to_be_focused()
    finally:
        context.close()


def test_work_and_history_loading_failure_retry_and_empty(browser, live_server, h):
    stranger = h.configure('people', {'display_name': 'Profile empty colleague'})['record']
    context, page = sign_in(browser, live_server, developer(h), {'width': 390, 'height': 900})
    try:
        panel = open_person(page, stranger)
        expect(section(panel, 'Rank')).to_contain_text('None')
        expect(section(panel, 'Programmes')).to_have_count(0)  # nothing to say: no empty section
        expect(section(panel, 'Details').locator('dt')).to_have_text(['Short code'])  # only what is recorded
        held = []
        page.route('**/api/calendar?**', lambda route: held.append(route))
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        wait_held(page, held)
        expect(panel.locator('.loading')).to_have_count(1)  # a skeleton while it reads
        held.pop().abort('failed')
        retry = panel.get_by_role('button', name='Retry loading schedule', exact=True)
        expect(panel).to_contain_text('Couldn’t load schedule.')
        page.unroute('**/api/calendar?**')
        retry.click()
        expect(panel.get_by_role('heading', name='Nothing in these dates', exact=True)).to_be_visible()
        panel.get_by_role('tab', name='History', exact=True).click()
        tab = panel.get_by_role('tabpanel', name='History', exact=True)
        expect(tab.locator('.audit-row').first).to_contain_text('added a person')
        expect(tab.locator('.dt-pager button')).to_have_count(0)  # one page: no pager
        profile_dates(tab, '2020-01-01', '2020-01-02')
        expect(tab.get_by_role('heading', name='Nothing matches', exact=True)).to_be_visible()
        no_page_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_staff_self_records_are_scoped_and_colleague_has_no_private_reads(browser, live_server, h, width):
    related(h)
    own, colleague = person(h), person(h, 1)
    context, page = sign_in(browser, live_server, 'staff', {'width': width, 'height': 900})
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url else None)
    try:
        panel = open_person(page, own)
        expect_tabs(panel.get_by_role('tab'), TABS['staff'])
        expect(section(panel, 'Clinics')).to_be_visible()
        expect(panel.get_by_role('button', name='Authorise eligibility', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Add rank', exact=True)).to_have_count(0)
        assert any('/api/me/profile' in url for url in reads)
        assert not any('/api/catalogues' in url or '/api/audit' in url or '/api/access' in url for url in reads)
        panel.get_by_role('tab', name=L, exact=True).click()
        expect(panel.locator('.pr-requests [data-row]')).to_have_count(2)
        panel.get_by_role('button', name='Close panel', exact=True).click()
        reads.clear()
        panel = open_person(page, colleague)
        expect(panel.locator('.pc')).to_be_visible()  # a colleague: the card alone
        expect(panel.get_by_role('tab')).to_have_count(0)
        expect(panel).not_to_contain_text('Other person private request')
        assert not any(any(endpoint in url for endpoint in ('/api/me/profile', '/api/calendar?', '/api/requests/all', '/api/audit', '/api/access', '/api/catalogues')) for url in reads)
        no_page_overflow(page)
    finally:
        context.close()


def test_person_request_link_retains_received_swap_after_reload(browser, live_server, h):
    swap = h.command('staff', 'swap.offer', offer_payload(h))['swap']
    target = person(h, 1)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.goto(live_server + '/#requests?person=' + target['id'])
        expect_loaded(page, 'requests')
        expect(page.locator('#main [data-row]')).to_have_count(1)
        expect(page.locator('#main [data-row]')).to_have_attribute('data-row', swap['id'])
        expect(page.get_by_role('button', name='Remove filter Person: ' + target['display_name'] + ' (from the link)', exact=True)).to_be_visible()
        for participant in [person(h), target]:
            expect(page.locator('#main [data-row]').get_by_role('button', name=participant['display_name'], exact=True)).to_be_visible()
        page.reload()
        expect_loaded(page, 'requests')
        expect(page.locator('#main [data-row]')).to_have_attribute('data-row', swap['id'])
        page.locator('#main [data-row] [data-col="type"]').click()
        detail = page.locator('.request-panel')
        for participant in [person(h), target]:
            expect(detail.get_by_role('button', name=participant['display_name'], exact=True)).to_be_visible()
        link = detail.get_by_role('button', name=target['display_name'], exact=True)
        link.focus()
        link.press('Enter')
        expect(page).to_have_url(re.compile(r'#people\?open=' + target['id'] + '$'))
        expect(page.get_by_role('region', name=target['display_name'], exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role('region', name=target['display_name'], exact=True)).to_be_visible()
    finally:
        context.close()


def test_person_work_schedule_action_clears_old_draft_source_and_survives_reload(browser, live_server, h):
    related(h)
    p = person(h)
    publication = h.get('coordinator', '/api/publications')['publications'][0]['id']
    draft = h.workspace('Different profile source draft', day(30), day(37))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/week/{day(30)}?edit={draft["id"]}&source={publication}')
        expect(page).to_have_url(re.compile('source=' + publication))
        panel = open_person(page, p)
        panel.get_by_role('tab', name='Schedule', exact=True).click()
        work_dates(panel, day(), day(6))
        panel.get_by_role('button', name='Show in Schedule', exact=True).click()
        for reload in (False, True):
            if reload:
                page.reload()
            expect_loaded(page, 'schedule')
            expect(page).to_have_url(re.compile(r'#schedule/week/' + day() + r'\?'))
            query = parse_qs(urlparse(page.url).fragment.split('?', 1)[1])
            assert query.get('person') == [p['id']] and query.get('edit') == ['none'], query
            assert 'source' not in query, query
            expect(page.locator(f'#main [data-person~="{p["id"]}"]').first).to_be_visible()
            expect(page.get_by_role('button', name='New session', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_programme_and_workload_person_links_share_the_exact_profile(browser, live_server, h):
    p = person(h)
    related(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def exact_profile(link):
            link.click()
            expect(page).to_have_url(re.compile(r'#people\?open=' + p['id'] + '$'))
            panel = page.get_by_role('region', name=p['display_name'], exact=True)
            expect(panel.get_by_role('tab', name='Overview', exact=True)).to_have_attribute('aria-selected', 'true')
            panel.get_by_role('button', name='Close panel', exact=True).click()

        navigate(page, 'programmes/programme_memberships')
        membership = page.locator('#main [data-row]').filter(has_text='Profile continuity programme')
        membership.get_by_role('button', name='Edit', exact=True).click()
        editor = page.get_by_role('dialog', name='Edit programme membership', exact=True)
        expect(editor).to_be_visible()
        editor.get_by_role('button', name='Cancel', exact=True).click()
        exact_profile(membership.get_by_role('button', name=p['display_name'], exact=True))

        # DL-027: Back from the profile returns to the list as it was: its search and its scroll.
        navigate(page, 'programmes/programme_memberships')
        search = page.locator('#main input[type=search]').first
        search.fill('Profile continuity')
        expect(membership).to_have_count(1)
        scroller = page.locator('#main .dt-body > .table-scroll').first
        scrolled = scroller.evaluate('n => (n.scrollTop = n.scrollHeight, n.scrollTop)')
        membership.get_by_role('button', name=p['display_name'], exact=True).click()
        expect(page).to_have_url(re.compile(r'#people\?open=' + p['id'] + '$'))
        page.go_back()
        expect(page).to_have_url(re.compile(r'#programmes/programme_memberships$'))
        expect(page.get_by_role('region', name=p['display_name'], exact=True)).to_have_count(0)
        expect(search).to_have_value('Profile continuity')
        expect(membership).to_have_count(1)
        assert scroller.evaluate('n => n.scrollTop') == scrolled

        navigate(page, 'insights/workload')
        workload = page.locator('#main [data-row]').filter(has=page.get_by_role('button', name=p['display_name'], exact=True))
        workload.get_by_role('button', name='View duties', exact=True).click()
        duties = page.get_by_role('region', name='Duties · ' + p['display_name'])
        expect(duties).to_be_visible()
        duties.get_by_role('button', name='Close panel').click()
        exact_profile(workload.get_by_role('button', name=p['display_name'], exact=True))
    finally:
        context.close()


@pytest.mark.parametrize('role', ['auditor', 'programme_lead'])
def test_readonly_person_wishes_remain_fixed_to_the_open_person(browser, live_server, h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    p = person(h)
    context, page = sign_in(browser, live_server, role, {'width': 390, 'height': 900})
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/wishes?' in r.url else None)
    try:
        panel = open_person(page, p)
        panel.get_by_role('tab', name=L, exact=True).click()
        expect(panel.get_by_role('button', name='Next month', exact=True)).to_be_visible()
        expect(panel.get_by_label('Whose wishes', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Add a wish', exact=True)).to_have_count(0)
        panel.get_by_role('button', name='Next month', exact=True).click()
        expect(panel.get_by_role('button', name='Previous month', exact=True)).to_be_visible()
        assert reads and all(parse_qs(urlparse(url).query).get('person_id') == [p['id']] for url in reads)
    finally:
        context.close()


def test_person_history_pages_subject_changes_not_changes_by_that_person(browser, live_server, h):
    p = person(h)
    # Real audit rows distinguish subject from author. The API tests separately cover receipt-based joins.
    with transaction(h.app.state.engine, write=True) as db:
        for n in range(52):
            db.add(m.AuditEvent(department_id=h.seed['department_id'],
                actor_id=h.seed['identities']['coordinator'], action='catalogue.update', target_id=p['id'],
                reason=f'Profile subject change {n:02}', detail={'catalogue': 'people'}, correlation_id=f'profile-{n}'))
        db.add(m.AuditEvent(department_id=h.seed['department_id'],
            actor_id=h.seed['identities']['staff'], action='catalogue.update', target_id=h.seed['people'][1],
            reason='Unrelated subject by this person', detail={'catalogue': 'people'}, correlation_id='profile-other'))
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        panel = open_person(page, p)
        panel.get_by_role('tab', name='History', exact=True).click()
        tab = panel.get_by_role('tabpanel', name='History', exact=True)
        older, newer = tab.get_by_role('button', name='Older', exact=True), tab.get_by_role('button', name='Newer', exact=True)
        expect(tab.locator('.dt-pager')).to_contain_text('1–50')
        expect(tab).not_to_contain_text('Unrelated subject by this person')
        expect(newer).to_be_disabled()
        older.focus()
        older.press('Enter')
        expect(tab.locator('.dt-pager')).to_contain_text('51–5')
        expect(newer).to_be_focused()  # Older is unavailable on the last page
        expect(older).to_be_disabled()
        newer.press('Enter')
        expect(tab.locator('.dt-pager')).to_contain_text('1–50')
        expect(older).to_be_focused()
        # A failed read offers Retry; the pager returns with it.
        page.route('**/api/audit?**', lambda route: route.abort('failed'))
        older.press('Enter')
        retry = tab.get_by_role('button', name='Retry loading history', exact=True)
        expect(retry).to_be_visible()
        page.unroute('**/api/audit?**')
        retry.click()
        expect(tab.locator('.dt-pager')).to_contain_text('51–5')
        # Looking up another period does not create an unsaved person edit.
        tab.get_by_label('From', exact=True).fill('2020-01-01')
        tab.get_by_label('From', exact=True).press('Tab')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        expect(page.locator('.unsaved-bar')).to_have_count(0)
    finally:
        context.close()


@pytest.fixture
def department_scale_person_hub(browser, live_server, h):
    """160 people with ranks and clinics, 600 history events: the hub opens without reading the catalogues again."""
    org, dept = h.seed['organisation_id'], h.seed['department_id']
    ids = [str(uuid.uuid4()) for _ in range(160)]
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Person), [{'id': i, 'organisation_id': org, 'display_name': f'Scale Person {k:03}'} for k, i in enumerate(ids)])
        db.execute(insert(m.Eligibility), [{'person_id': i, 'department_id': dept, 'service_id': s, 'starts_on': date(2020, 1, 1)}
                                           for i in ids for s in h.seed['services']])
        db.execute(insert(m.AuditEvent), [{'id': str(uuid.uuid4()), 'department_id': dept, 'actor_id': h.seed['identities']['admin'],
                                           'action': 'catalogue.update', 'target_id': ids[k % 3], 'reason': f'Scale change {k}',
                                           'detail': {'catalogue': 'people'}, 'correlation_id': f'scale-{k}'} for k in range(600)])
    context, page = sign_in(browser, live_server, developer(h))
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url else None)
    try:
        navigate(page, 'people')
        page.get_by_label('Search people', exact=True).fill('Scale Person 000')
        reads.clear()
        started = time.perf_counter()
        row(page, 'Scale Person 000').locator('strong').first.click()
        panel = page.get_by_role('region', name='Scale Person 000', exact=True)
        expect(section(panel, 'Clinics').locator('.ph-row')).to_have_count(len(h.seed['services']))
        elapsed = time.perf_counter() - started
        yield panel, reads, elapsed
    finally:
        context.close()


def test_department_scale_person_hub_opens_quickly(department_scale_person_hub):
    panel, reads, _ = department_scale_person_hub
    assert not any('/api/catalogues' in u for u in reads), reads
    panel.get_by_role('tab', name='History', exact=True).click()
    expect(panel.get_by_role('tabpanel', name='History', exact=True).locator('.dt-pager')).to_contain_text('1–50')


@pytest.mark.wall_benchmark
def test_department_scale_person_hub_cpu_budget(department_scale_person_hub):
    _, _, elapsed = department_scale_person_hub
    assert elapsed < 5


def test_unlinked_staff_cannot_read_a_colleague_as_self(browser, live_server, h):
    identity(h, 'unlinked_profile_staff', ['staff'])
    p = person(h)
    context, page = sign_in(browser, live_server, 'unlinked_profile_staff')
    reads = []
    page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url else None)
    try:
        panel = open_person(page, p)
        expect(panel.locator('.pc')).to_be_visible()
        expect(panel.get_by_role('tab', name='Schedule', exact=True)).to_have_count(0)
        assert not any('/api/me/profile' in url or '/api/calendar?' in url or '/api/requests/all' in url for url in reads)
    finally:
        context.close()


@pytest.mark.parametrize('role', ['coordinator', 'credentialer'])
def test_person_schedule_reads_rotations_older_than_the_catalogue(browser, live_server, h, role):
    """The rotations catalogue holds only the last 12 months onward; a person's Schedule reads any period's rotations,
    as trainee and as mentor."""
    a, b = h.seed['people'][:2]
    dates = {'starts_on': '2024-01-01', 'ends_on': '2024-02-01'}
    programme = h.configure('programmes', {'name': 'Profile history programme', 'starts_on': '2023-07-01'})['record']
    cohort = h.configure('cohorts', {'name': 'Profile history cohort', 'programme_id': programme['id'], 'starts_on': '2023-07-01', 'ends_on': '2024-07-01'})['record']
    h.configure('programme_memberships', {'person_id': a, 'programme_id': programme['id'], 'starts_on': '2023-07-01', 'ends_on': '2024-07-01'})
    h.configure('rotations', {'person_id': a, 'mentor_id': b, 'cohort_id': cohort['id'], 'service_id': h.seed['services'][0],
                              'department_available': True, **dates})
    assert not [r for r in h.get('coordinator', '/api/catalogues')['rotations'] if r['cohort_id'] == cohort['id']]
    context, page = sign_in(browser, live_server, role, {'width': 1440, 'height': 900})
    try:
        for p, text in ((person(h), 'Profile history cohort'), (person(h, 1), 'Mentoring ' + person(h)['display_name'])):
            panel = open_person(page, p)
            panel.get_by_role('tab', name='Schedule', exact=True).click()
            work_dates(panel, '2024-01-01', '2024-01-31')
            expect(section(panel, 'Rotations')).to_contain_text(text)
    finally:
        context.close()
