"""Home: Needs you, the Rota months, Today by clinic, my duties, Away today and notices — every role, desktop and phone,
light and dark, then the journeys, failures and department scale (approved mockups HomeCoord / HomeStaff)."""
from packages.domain.time import now as clock_now
import json
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_browser import route_fetch, browser, live_server, sign_in, navigate, expect_loaded  # noqa: F401
from .test_home import big_department

pytestmark = pytest.mark.browser
SHOTS = os.getenv('HOME_SCREENSHOTS')  # a folder: save one screenshot per role, size and theme for review
PHONE = {'width': 390, 'height': 844}
DESKTOP = {'width': 1440, 'height': 1000}
PLANNERS = ('admin', 'coordinator', 'approver', 'programme', 'auditor')


def local_today():
    return clock_now().astimezone(ZoneInfo('Asia/Riyadh')).date()


def month_start(day, n=0):
    k = day.year * 12 + day.month - 1 + n
    return date(k // 12, k % 12 + 1, 1)


def populated(h):
    """A published rota from last month (today: a morning the staff member leads with a colleague, an afternoon on
    overtime, an evening; their next duties tomorrow and in three days), a draft after it with an open place, a leave
    request waiting, a proposed rule and a colleague on annual leave today."""
    a, b, c = h.seed['people']
    day = local_today()
    at = lambda d, hour: f'{d.isoformat()}T{hour:02}:00:00+03:00'
    end = max(month_start(day, 1), day + timedelta(days=8))
    w = h.workspace('Home published rota', month_start(day, -1).isoformat(), end.isoformat())
    for d, start, stop, people, service in ((day, 8, 12, [{'person_id': a, 'team_lead': True}, {'person_id': b}], 0),
                                            (day, 13, 17, [{'person_id': b, 'overtime': True}], 1),
                                            (day, 17, 21, [{'person_id': c}], 0),
                                            (day + timedelta(days=1), 8, 12, [{'person_id': a, 'team_lead': True}], 0),
                                            (day + timedelta(days=3), 13, 16, [{'person_id': a}], 1)):
        w = h.shift(w, start=at(d, start), end=at(d, stop), service=service,
                    people=[{**p, 'start': at(d, start), 'end': at(d, stop)} for p in people])['workspace']
    # A night duty from yesterday 22:00 still running this morning.
    night = (at(day - timedelta(days=1), 22), at(day, 6))
    w = h.shift(w, start=night[0], end=night[1], people=[{'person_id': b, 'start': night[0], 'end': night[1]}])['workspace']
    h.publish(w)
    h.command('staff', 'request.create', {'person_id': a, 'start': at(end, 9), 'end': at(end, 12), 'private_reason': 'Fictional private reason'})
    h.command('coordinator', 'policy.create', {'name': 'Home proposed rule', 'owner': 'Test policy owner', 'source': 'Constructed fixture',
        'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'maximum_participants', 'maximum': 3}, 'scope': {}, 'severity': 'mandatory'}})
    h.command('coordinator', 'availability.record', {'person_id': c, 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
        'start': at(day, 0), 'end': at(day + timedelta(days=3), 0), 'private_reason': 'Fictional family visit'})
    with transaction(h.app.state.engine, write=True) as db:
        for person_id, blocking, retracted in ((a, False, False), (b, True, True)):
            db.add(m.Availability(department_id=h.seed['department_id'], person_id=person_id, kind='recorded_leave',
                start=datetime.fromisoformat(at(day, 0)), end=datetime.fromisoformat(at(day + timedelta(days=1), 0)),
                recorded_by=h.seed['identities']['coordinator'], private_reason='Synthetic absence reason',
                counts_as_unavailable=blocking, retracted=retracted))
        db.add(m.DepartmentContent(department_id=h.seed['department_id'], name='Ramadan hours from 1 Mar', starts_on=day - timedelta(days=1),
                                   description='Clinics close at 15:00. Evening sessions move to the virtual clinic.'))
    identity(h, 'programme', ['programme_lead'])
    # Check the draft after input changes so this fixture exercises fresh Home attention.
    h.shift(h.workspace('Home draft rota', end.isoformat(), (end + timedelta(days=7)).isoformat()),
            start=at(end, 8), end=at(end, 12), people=[])


def no_horizontal_scroll(page):
    assert page.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')


def needs(page, title):
    return page.locator('#main .home-needs-list li').filter(has=page.locator('strong', has_text=re.compile('^' + re.escape(title) + '$')))


@pytest.mark.parametrize('phone_width', [320, 390])
def test_desktop_only_attention_is_counted_on_phone_and_only_empty_is_all_clear(browser, live_server, h, phone_width):
    """P3-UI-01: presentation restrictions cannot turn known credentialing work into an all-clear summary."""
    attention = [{'key': 'no_eligibility', 'count': 2}]

    def with_attention(route):
        response = route_fetch(route)
        body = response.json()
        body['attention'] = list(attention)
        route.fulfill(response=response, body=json.dumps(body))

    context, page = sign_in(browser, live_server, 'credentialer', viewport=DESKTOP, phone_notice=True)
    try:
        page.route('**/api/home', with_attention)
        # Reloads: Home opened again within the tab answers from its kept replies (api.ts keptReads), not the route.
        page.evaluate("location.hash='home'")
        page.reload()
        main = page.locator('#main')
        status = page.locator('.app-footer .page-status')
        expect(needs(page, 'People without clinics').locator('.home-count')).to_have_text('2')
        expect(status).not_to_contain_text('need')  # D-021: the card's rows, never repeated as a count in the footer
        page.set_viewport_size({'width': phone_width, 'height': PHONE['height']})
        expect(main.locator('.home-elsewhere')).to_have_text('1 item needs a desktop: People without clinics')
        expect(main.locator('.home-clear')).to_have_count(0)
        expect(main.locator('[data-need=no_eligibility]')).to_have_count(0)  # do not widen phone actions
        expect(status).not_to_contain_text('need')  # nor the items only a desktop can do
        page.reload()
        expect(main.locator('.home-elsewhere')).to_have_text('1 item needs a desktop: People without clinics')
        expect(main.locator('.home-clear')).to_have_count(0)
        expect(status).not_to_contain_text('need')  # nor the items only a desktop can do
        page.set_viewport_size(DESKTOP)
        expect(needs(page, 'People without clinics').locator('.home-count')).to_have_text('2')
        expect(main.locator('.home-elsewhere')).to_have_count(0)
        attention.clear()
        page.reload()
        expect(main.locator('.home-clear')).to_have_text('All clear ✓')
        # ME-17: the card says All clear; the footer does not say it again.
        expect(status).not_to_contain_text('Nothing needs you')
    finally:
        context.close()


def check_home(page, role):
    main = page.locator('#main')
    expect(main).to_have_attribute('data-loaded-view', 'home')
    expect(page.locator('.topbar h1')).to_have_text(re.compile(r'^Good (morning|afternoon|evening)'))
    expect(main.get_by_role('heading', name='Needs you', exact=True)).to_be_visible()
    heading = lambda name: main.get_by_role('heading', name=name, exact=True)
    for name in ('Rota', 'Today', 'Away today'):
        expect(heading(name)).to_have_count(1 if role in PLANNERS else 0)
    status = page.locator('.app-footer .page-status')
    if role in PLANNERS:
        expect(main.locator('.home-today table')).to_be_visible()
        expect(status).not_to_contain_text('Published to')  # no dates in the footer reviewed
        if role in ('approver', 'programme', 'auditor'):  # D-099: a draft's status is for whoever plans it
            expect(main.locator('.home-rota :is(.home-month, .home-none)').first).to_be_visible()
            expect(status).not_to_contain_text(re.compile(r'draft|needs a check|not checked'))
    elif role == 'staff':
        expect(main.locator('.home-hero')).to_contain_text(re.compile('Next duty|On duty now'))
        expect(heading('Coming up')).to_be_visible()
        expect(status).not_to_contain_text('Next duty')  # the hero says it; the footer never repeats it
    else:
        expect(status).not_to_contain_text('Nothing needs you')
    expect(main.locator('.home-notice-list li')).to_contain_text('Ramadan hours from 1 Mar')
    primary = {'admin': 'New rota', 'coordinator': 'New rota', 'staff': 'New request'}.get(role)
    actions = page.locator('.topbar .page-actions')
    if primary:
        expect(actions.get_by_role('button', name=primary, exact=True)).to_be_visible()
    else:
        expect(actions.get_by_role('button')).to_have_count(0)
    expect(main.locator('.loading')).to_have_count(0)
    expect(main.locator('[role=alert]')).to_have_count(0)


@pytest.mark.parametrize('role', ['admin', 'coordinator', 'approver', 'credentialer', 'programme', 'auditor', 'staff', 'operator'])
def test_every_role_desktop_and_phone_light_and_dark(browser, live_server, h, role):
    populated(h)
    context, page = sign_in(browser, live_server, role)
    try:
        for size in (DESKTOP, PHONE):
            page.set_viewport_size(size)
            for scheme in ('light', 'dark'):
                page.emulate_media(color_scheme=scheme)
                navigate(page, 'home')
                check_home(page, role)
                no_horizontal_scroll(page)
                if SHOTS:
                    page.screenshot(path=os.path.join(SHOTS, f'{role}-{size["width"]}-{scheme}.png'), full_page=True)
        main = page.locator('#main')
        if role == 'approver':
            row = needs(page, 'Leave request')
            expect(row.locator('.home-count')).to_have_text('1')
            expect(row).to_contain_text('Staff 01(test)')
            expect(needs(page, 'Rule change')).to_contain_text('Home proposed rule · proposed by Coordinator · Demo')
        if role == 'coordinator':
            row = needs(page, 'Home draft rota')
            expect(row).to_contain_text('Must fix before submitting')  # D9: its Fix list's must-fix causes
            expect(row.locator('.home-count')).to_have_text('1')
            # Rota: last month and this month published, the draft after them, today marked.
            months = main.locator('.home-month')
            expect(months).to_have_count(4)
            # How far the rota is published is shown by the month bars; neither the card head nor the footer repeats it.
            expect(main.locator('.home-rota .home-head')).not_to_contain_text('Published to')
            expect(months.nth(0)).to_contain_text('Published')
            expect(main.locator('.home-month .home-now')).to_have_count(1)
            expect(main.locator('.home-month').filter(has_text=re.compile(r'Draft · .*0% filled')).get_by_role('button', name='Open Home draft rota')).to_be_visible()
            # Today: a row per clinic, a column per part of the day, the people on duty (D14: never a ratio); the
            # names, with their TL and OT tags, behind Show people.
            today = main.locator('.home-today')
            # Columns are the sessions' own times (no invented day parts); the night duty still running names its day.
            yesterday = (local_today() - timedelta(days=1)).strftime('%a')
            expect(today.locator('thead th')).to_have_text([re.compile(n) for n in
                ('Clinic', f'{yesterday} 22:00–06:00', '08:00–12:00', '13:00–17:00', '17:00–21:00')])
            expect(today.locator('tbody tr')).to_have_count(2)
            expect(today.locator('.rp.person')).to_have_count(0)
            count = today.locator('tbody tr').filter(has_text='Continuity clinic').locator('.home-filled').first
            expect(count.locator('[aria-hidden=true]')).to_have_text('1')
            expect(count.locator('.sr-only')).to_have_text('1 on duty')
            expect(today).not_to_contain_text(re.compile(r'\d+/\d+'))
            today.get_by_role('button', name='Show people', exact=True).click()
            expect(today.get_by_role('button', name='Hide people', exact=True)).to_have_attribute('aria-expanded', 'true')
            expect(today.locator('.home-today-people tbody tr').filter(has_text='Continuity clinic').locator('.rp.person')).to_have_count(4)
            expect(today.locator('table .wk-tag.lead')).to_have_text('TL')
            expect(today.locator('table .wk-tag.ot')).to_have_text('OT')
            expect(today.get_by_role('list', name='Tags')).to_contain_text('Team lead')
            expect(today.locator('table').first.locator('tbody tr').filter(has_text='Review clinic').locator('td').nth(1)).to_have_text('—')
            away = main.locator('.home-away-list li')
            expect(away).to_have_count(1)
            expect(away).to_contain_text('Staff 03(test)')
            expect(away).to_contain_text('Annual · to')
            expect(main.get_by_role('region', name='Away today')).to_contain_text('1 person')
        if role == 'staff':
            # The pinned test clock is 09:00 today: the morning they lead is running, so it is the hero.
            hero = main.locator('.home-hero')
            expect(hero).to_contain_text('On duty now')
            expect(hero).to_contain_text('Continuity clinic')
            expect(hero.locator('.wk-tag.lead:not(.view-legend .wk-tag)')).to_have_text('TL')  # not the legend's sample of it
            expect(main.locator('.home-duties .home-duty-list li')).to_have_count(2)  # tomorrow and in three days
            expect(hero).to_contain_text('–12:00')  # when it ends is on the hero
            # D-111: staff Home has no screen status in the footer (the hero already says it).
            expect(page.locator('.app-footer .page-status')).to_have_text('')
            # Their own request waits on someone else: status under My requests, not work under Needs you.
            expect(needs(page, 'Leave request')).to_have_count(0)
            mine = main.get_by_role('region', name='My requests')
            expect(mine).to_contain_text('1 waiting')
            expect(mine.locator('.home-request-list li')).to_contain_text('Waiting for approval')
        if role == 'operator':
            main.get_by_role('button', name='Service health', exact=True).click()
            expect_loaded(page, 'system/operations')
    finally:
        context.close()


def test_journeys_open_the_right_page(browser, live_server, h):
    populated(h)
    draft = lambda: next(w for w in h.get('coordinator', '/api/workspaces')['workspaces'] if w['name'] == 'Home draft rota')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        needs(page, 'Home draft rota').get_by_role('button', name='Fix list', exact=True).click()
        expect_loaded(page, 'schedule')
        # On a desktop a draft's Fix list opens beside its planner (app.ts openIssues), not as the issues page.
        assert 'edit=' + draft()['id'] in page.url, page.url
        # The docked Fix list draws its levels as sections, not panels (fix_list.ts fixList, panel:false).
        fixes = page.get_by_role('region', name='Fix list', exact=True)
        expect(fixes).to_be_visible()
        expect(fixes.get_by_role('region', name='Must fix', exact=True)).to_be_visible()
        navigate(page, 'home')
        page.get_by_role('button', name='Open Home draft rota').click()
        expect_loaded(page, 'schedule')
        assert draft()['id'] in page.url
        navigate(page, 'home')
        page.locator('.home-today').get_by_role('button', name='Open in Schedule →').click()
        expect_loaded(page, 'schedule')
        assert local_today().isoformat() in page.url
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'home')
        needs(page, 'Leave request').get_by_role('button', name='Review').click()
        expect_loaded(page, 'requests')
        assert 'status=waiting' in page.url
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page, 'home')
        page.locator('.topbar .page-actions').get_by_role('button', name='New request', exact=True).click()
        expect_loaded(page, 'requests')
        # The link opens New request once, then drops `new=1` so a reload does not reopen it.
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog).to_be_visible()
        assert 'new=1' not in page.url
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        navigate(page, 'home')
        page.locator('.home-hero').get_by_role('button', name='Add to calendar', exact=True).click()
        expect(page.get_by_role('heading', name='Calendar subscription', exact=True)).to_be_visible()
        # The duty in the hero is running: there is nothing left to offer.
        expect(page.locator('.home-hero').get_by_role('button', name='Offer swap', exact=True)).to_have_count(0)
        # Staff-only accounts reach their hours and fairness from Home (Insights is not in their sidebar).
        page.get_by_role('button', name='My hours & fairness', exact=True).click()
        expect_loaded(page, 'insights')
    finally:
        context.close()


def test_failure_shows_retry_and_recovers(browser, live_server, h):
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route('**/api/home', lambda route: route.abort())
        page.reload()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        expect(main.get_by_role('alert')).to_contain_text('Couldn’t load what needs you.')
        # The failure is said inside its card; the footer keeps the rota's own status, which wins over the counts (H-14).
        expect(page.locator('.app-footer .page-status')).to_contain_text(re.compile(r'(draft|rota) (\d+%|·)'))
        # The rest of Home still works without it.
        expect(main.locator('.home-today table')).to_be_visible()
        page.unroute('**/api/home')
        # Its Retry reads only what needs you (H-13), named like every card's Retry; the reader keeps their place (H-12).
        main.get_by_role('button', name='Retry loading what needs you', exact=True).click()
        expect(main.get_by_role('heading', name='Needs you')).to_be_focused()
        expect(needs(page, 'Home draft rota')).to_be_visible()
        expect(main.get_by_role('alert')).to_have_count(0)
        # Each other card fails and retries alone.
        for path in ('**/api/home/rotas?**', '**/api/schedule/sessions?**', '**/api/calendar?**', '**/api/content?**'):
            page.route(path, lambda route: route.abort())
        page.reload()
        expect_loaded(page, 'home')
        for title in ('Rota', 'Today', 'Away today', 'Notices', 'My next duties'):
            if title == 'My next duties' and not h.get('coordinator', '/api/me').get('person_id'):
                continue
            expect(main.get_by_role('region', name=title, exact=True)).to_contain_text(f'Couldn’t load {title.lower()}.')
        # The lost connection is one alert (the line above the cards); each card says its own failure quietly.
        expect(main.get_by_role('alert')).to_have_count(1)
        expect(main.locator('.home-offline')).to_have_count(1)
        expect(needs(page, 'Home draft rota')).to_be_visible()
        page.unroute_all()
        for title in ('Rota', 'Today', 'Away today', 'Notices'):
            main.get_by_role('button', name=f'Retry loading {title.lower()}').click()
            expect(main.get_by_role('region', name=title, exact=True).get_by_role('alert')).to_have_count(0)
        expect(main.locator('.home-today table')).to_be_visible()
        expect(main.locator('.home-month')).to_have_count(4)
        expect(main.locator('.home-notice-list li')).to_contain_text('Ramadan hours from 1 Mar')
    finally:
        context.close()


def test_rota_months_name_the_published_days_and_start_keeps_the_month(browser, live_server, h):
    """A rota published from the 15th reads "Published 15–31 Oct" with its bar where those days are; Start for a later
    month opens New draft for that month."""
    day = local_today()
    first, end = day.replace(day=15), month_start(day, 1)
    w = h.workspace('Half month rota', first.isoformat(), end.isoformat())
    w = h.shift(w, start=f'{first.isoformat()}T08:00:00+03:00', end=f'{first.isoformat()}T12:00:00+03:00')['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        month = page.locator('.home-month').nth(1)
        span = f"{first:%d}".lstrip("0") + "–" + f"{end - timedelta(days=1):%d %b}"
        expect(month).to_contain_text('Published ' + span)
        fill = month.locator('.fill.published')
        expect(fill).to_have_count(1)
        assert fill.evaluate('f => f.style.getPropertyValue("--from")') != '0%'
        later = month_start(day, 2)
        page.get_by_role('button', name=f'Start the {later:%B} rota').click()
        form = page.get_by_role('dialog', name='New rota')
        expect(form.get_by_label('Rota name', exact=True)).to_have_value(f'{later:%B} rota')
        assert any(later.isoformat() in v or f'1 {later:%b}' in v for v in form.locator('input').evaluate_all('xs => xs.map(x => x.value)'))
        expect(form).to_contain_text(f'{(month_start(day, 3) - later).days} days')
    finally:
        context.close()


def test_open_places_today_show_as_a_dashed_chip(browser, live_server, h):
    """A gap in today's published check reads "1 open" beside the people of that session (from the stored coverage)."""
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def gap(route):
            body = route_fetch(route).json()
            shift = next(s for s in body['shifts'] if s['participants'] and len(s['participants']) == 1)
            body['coverage'].append({'shift_id': shift['id'], 'status': 'gap', 'required': 2, 'scheduled': 1})
            shift['staffing'] = {**(shift.get('staffing') or {}), 'places_open': 1}  # maintenance staffing, read first
            route.fulfill(content_type='application/json', body=json.dumps(body))
        page.route('**/api/schedule/sessions?**', gap)
        page.reload()  # a fresh read (no remembered ETag)
        expect_loaded(page, 'home')
        chip = page.locator('.home-today .rp.open')
        expect(chip).to_have_count(1)
        expect(chip).to_have_text('1 open')
    finally:
        context.close()


def test_empty_department_is_calm(browser, live_server, h):
    with transaction(h.app.state.engine, write=True) as db:
        dept = m.Department(organisation_id=h.seed['organisation_id'], name='Empty department')
        db.add(dept)
        db.flush()
        dept_id = dept.id
    identity(h, 'empty-approver', ['approver'], department=dept_id)
    identity(h, 'empty-coordinator', ['coordinator'], department=dept_id)
    for role in ('empty-approver', 'empty-coordinator'):
        context, page = sign_in(browser, live_server, role, PHONE)
        try:
            navigate(page, 'home')
            main = page.locator('#main')
            if role == 'empty-approver':  # people belong to the organisation: a coordinator is still told who has no rank
                expect(main.locator('.home-clear')).to_have_text('All clear ✓')
            expect(main.get_by_text('No published sessions today.', exact=True)).to_be_visible()
            expect(main.get_by_text('Nobody is away today.', exact=True)).to_be_visible()
            # No rota yet: one line, not four empty months (H-25); nothing to start before duty types exist (H-20).
            expect(main.locator('.home-rota')).to_contain_text('No rota yet.')
            expect(main.locator('.home-month')).to_have_count(0)
            if role == 'empty-approver':
                expect(main.locator('.home-clear')).to_have_text('All clear ✓')
                expect(page.locator('.app-footer .page-status')).not_to_contain_text('Nothing needs you')
            no_horizontal_scroll(page)
            if SHOTS:
                page.screenshot(path=os.path.join(SHOTS, f'{role}-390-light.png'), full_page=True)
        finally:
            context.close()


def test_department_scale(browser, live_server, h):
    big_department(h, today_sessions=24)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        reads = []
        page.on('request', lambda r: reads.append(r.url) if '/api/' in r.url else None)
        navigate(page, 'home')
        main = page.locator('#main')
        # One row per draft (home.py), each its own Fix list's must-fix causes (D9), never the places summed.
        gaps = main.locator('.home-needs-list li').filter(has_text='Must fix before submitting')
        expect(gaps).to_have_count(3)
        # Today: a row per clinic with counts per part of the day in use (H-01), never 72 name chips; the names on request.
        today = main.locator('.home-today')
        expect(today.locator('tbody tr')).to_have_count(2)
        expect(today.locator('.rp.person')).to_have_count(0)
        filled = today.locator('.home-filled [aria-hidden=true]').all_inner_texts()
        assert filled and all(re.fullmatch(r'\d+', f) for f in filled), filled
        assert sum(map(int, filled)) == 72, filled
        expect(today.locator('.home-filled .sr-only')).to_have_text([f'{f} on duty' for f in filled])
        today.get_by_role('button', name='Show people', exact=True).click()
        expect(today.locator('.rp.person')).to_have_count(72)
        today.get_by_role('button', name='Hide people', exact=True).click()
        expect(today.locator('.home-today-people')).to_be_hidden()
        expect(today.locator('table').first.locator('thead th')).to_have_text([re.compile(n) for n in ('Clinic', '07:00–11:00', '11:00–15:00', '15:00–19:00', '19:00–23:00')])
        expect(main.locator('.home-month')).to_have_count(4)
        expect(main.locator('[role=alert]')).to_have_count(0)
        page.set_viewport_size(PHONE)
        no_horizontal_scroll(page)
        # One read per card, never one per person or per rota.
        paths = [re.sub(r'\?.*', '', u.split('/api/')[1]) for u in reads]
        assert all(paths.count(p) <= 2 for p in set(paths)), paths
        print('Home API reads at department scale:', sorted(set(paths)))
    finally:
        context.close()


def calendar_query(url):
    parsed = urlsplit(url)
    return parse_qs(parsed.query) if parsed.path == '/api/calendar' else None


# My next duties: the personal read stays scoped and a detached look-ahead stops.
def test_my_duties_personal_read_is_scoped_and_detached_lookahead_stops(browser, live_server, h):
    # A person-linked coordinator can read the team, so the personal query must narrow it explicitly.
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = person
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    reads = []

    def hold_later(route):
        query = calendar_query(route.request.url)
        reads.append(query)
        if query and query.get('person_id') and (datetime.fromisoformat(query['end'][0]) -
                datetime.fromisoformat(query['start'][0])).days == 31:
            held.append((route, route_fetch(route)))
        else:
            route.continue_()

    try:
        # Home is the landing page and already reads its windows: hold reads, then open it afresh. A reload: opened
        # again within the tab, Home answers from its kept replies (api.ts keptReads) and asks nothing.
        page.route('**/api/calendar?**', hold_later)
        page.evaluate("location.hash='home'")
        with page.expect_request(lambda r: (calendar_query(r.url) or {}).get('person_id') == [person] and
                (datetime.fromisoformat(calendar_query(r.url)['end'][0]) -
                 datetime.fromisoformat(calendar_query(r.url)['start'][0])).days == 31):
            page.reload()
        expect_loaded(page, 'home')
        navigate(page,'requests')
        # The 14 days were empty: the three months after them are read together (H-22), never one after another.
        assert len(held) == 3
        pending = held[:]
        held.clear()
        for route, response in pending:
            route.fulfill(response=response)
        # The released reply would schedule another window from its handler: let the page's event loop turn twice
        # (a frame, then a task) instead of a fixed wait, then count.
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => setTimeout(r)))")
        later = [q for q in reads if q and q.get('person_id') and (datetime.fromisoformat(q['end'][0]) -
                 datetime.fromisoformat(q['start'][0])).days == 31]
        assert len(later) == 3, reads  # released replies start no further window
        first = next(q for q in reads if q and (datetime.fromisoformat(q['end'][0]) -
                     datetime.fromisoformat(q['start'][0])).days == 14)
        assert first.get('person_id') == [person], first
        print('Detached personal lookahead windows:', len(later))
    finally:
        for route, _ in held:
            route.abort()
        context.close()


def test_rota_month_with_an_empty_draft_says_there_is_nothing_to_fill(browser, live_server, h):
    """D-161: a coordinator's unchecked draft is explained once in Needs you, not repeated in the month strip or footer."""
    start = month_start(local_today(), 1)
    h.workspace('Empty next-month draft', start.isoformat(), month_start(local_today(), 2).isoformat())
    h.workspace('Last month draft', month_start(local_today(), -1).isoformat(), month_start(local_today()).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        month = page.locator('#main .home-month').nth(2)
        expect(month).to_contain_text('Draft · Empty next-month draft')
        expect(month).not_to_contain_text('no staffing minimum set')
        expect(page.locator('#main .home-rota')).not_to_contain_text('100%')
        # The footer names the coming month's draft, not last month's.
        expect(page.locator('.app-footer')).not_to_contain_text('no staffing minimum set')
    finally:
        context.close()


def test_recent_changes_leave_out_sign_ins(browser, live_server, h):
    """Sign-ins are not changes: Home's Recent changes leaves auth events out (services/home.py); the person record's
    audit still words them "signed in on a device" (test_browser_person_record.py)."""
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'home')
        expect(page.locator('#main h2').first).to_be_visible()  # Home is drawn; with only sign-ins there are no recent changes
        expect(page.locator('#main')).not_to_contain_text('signed in')
        expect(page.locator('#main .role-chip').filter(has_text='auth')).to_have_count(0)
    finally:
        context.close()


def test_open_on_a_month_with_several_drafts_lands_on_that_month_and_draft(browser, live_server, h):
    """Review maintenance item 11: a month with two drafts opens the Schedule on that month with its named draft in edit, not
    on whichever month the Schedule showed last."""
    start, end = month_start(local_today(), 2), month_start(local_today(), 3)
    ids = {h.workspace(name, start.isoformat(), end.isoformat())['id'] for name in ('Two drafts A', 'Two drafts B')}
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        # The Schedule last showed another month: Open must not land there.
        navigate(page, f'schedule/week/{local_today().isoformat()}?edit=none')
        navigate(page, 'home')
        month = page.locator(f'#main .home-month[data-month="{start.isoformat()[:7]}"]')
        expect(month).to_contain_text('+1 draft')
        month.get_by_label(re.compile(r'^Open the .+ drafts$')).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule')
        expect(page).to_have_url(re.compile(rf'#schedule/month/{start.isoformat()}\?edit=({"|".join(ids)})$'))
    finally:
        context.close()


def test_drafts_with_gaps_name_their_open_places(browser, live_server, h):
    """H-01/H-03/H-05/H-06/H-26: two checked drafts of next month with an open place each. Counts over several drafts
    do not say which drafts they hold, so blocking issues and gaps stay two rows (no head count); the month names the
    first draft, "+1 draft" and its open places (never "no places to fill"), count pills share one width, and rows and
    months carry their keys (data-need, data-month)."""
    start, end = month_start(local_today(), 1), month_start(local_today(), 2)
    at = lambda hour: f'{start.isoformat()}T{hour:02}:00:00+03:00'
    for name in ('Gap draft A', 'Gap draft B'):
        w = h.shift(h.workspace(name, start.isoformat(), end.isoformat()), start=at(8), end=at(12), people=[])['workspace']
        h.lifecycle(w, 'validate')
    context, page = sign_in(browser, live_server, 'coordinator')

    def no_places_counted(route):
        # The check found open places but counted no places needed (the scale case H-01): the strip must still say so.
        body = route_fetch(route).json()
        for w in body['workspaces']:
            if w.get('health') and w['health'].get('summary'):
                w['health']['summary'].update(places_needed=0, places_filled=0, places_open=3)
        route.fulfill(json=body)
    try:
        page.route('**/api/home/rotas?**', no_places_counted)
        page.reload()  # signing in already drew Home (and read the rotas) before the route was in place
        navigate(page, 'home')
        main = page.locator('#main')
        rows = main.locator('.home-needs-list li[data-need=blocking], .home-needs-list li[data-need=open_places]')
        expect(rows).to_have_count(2)
        expect(main.locator('.home-needs .home-head .home-aside')).to_have_text('')
        month = main.locator(f'.home-month[data-month="{start.isoformat()[:7]}"]')
        expect(month).to_contain_text('+1 draft')
        expect(month).to_contain_text('3 open')
        expect(month).not_to_contain_text('no places to fill')
        expect(main.locator('.home-month')).to_have_count(4)
        widths = main.locator('.home-count').evaluate_all('ns => ns.map(n => Math.round(n.getBoundingClientRect().width))')
        assert len(set(widths)) == 1, widths
        # A draft edited after its check: the old open places are stale, so the month asks for a check instead.
        page.unroute_all()

        def stale(route):
            body = route_fetch(route).json()
            for w in body['workspaces']:
                if w.get('health') and w['health'].get('summary'):
                    w['health'].update(fresh=False, checked=True)
                    w['health']['summary'].update(places_open=3)
            route.fulfill(json=body)
        page.route('**/api/home/rotas?**', stale)
        page.reload()
        expect(month).to_contain_text('needs a check')
        expect(month).not_to_contain_text('3 open')
        # A fresh check with no open place but a session missing its lead: that is a session, worded as one.
        page.unroute_all()

        def lead_gap(route):
            body = route_fetch(route).json()
            for w in body['workspaces']:
                if w.get('health') and w['health'].get('summary'):
                    w['health'].update(fresh=True, checked=True, open_places=1)
                    w['health']['summary'].update(places_needed=0, places_filled=0, places_open=0, sessions_with_gaps=1)
            route.fulfill(json=body)
        page.route('**/api/home/rotas?**', lead_gap)
        page.reload()
        expect(month).to_contain_text('1 session with open places')
        expect(month).not_to_contain_text('1 open')
    finally:
        context.close()


def test_cards_read_alongside_the_counts(browser, live_server, h):
    """H-09: the cards start their reads while /api/home is still on its way (no waterfall); Needs you fills in after."""
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route('**/api/home', lambda route: held.append(route))
        # A reload: Home opened again within the tab answers from its kept replies (api.ts keptReads) and reads nothing.
        page.evaluate("location.hash='home'")
        with page.expect_request(lambda r: '/api/home/rotas?' in r.url):
            page.reload()
        assert held, 'the Rota card read started before /api/home answered'
        expect(page.locator('#main .home-needs .loading')).to_have_count(1)
        for route in held:
            route.continue_()
        page.unroute('**/api/home')
        expect(needs(page, 'Home draft rota')).to_be_visible()
    finally:
        context.close()


def test_a_notice_to_acknowledge_is_work_in_needs_you(browser, live_server, h):
    """H-18/H-17: a notice waiting for the reader's acknowledgement is a Needs you row (never "All clear"), badged
    "Read and acknowledge"; acknowledging it clears the row."""
    from .test_announcement_receipts import notice
    notice(h, {'roles': ['staff']}, importance='must_acknowledge', name='Read the new handover rule')
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'home')
        main = page.locator('#main')
        row = main.locator('.home-needs-list li[data-need=acknowledge]')
        expect(row).to_contain_text('Notice to acknowledge')
        expect(row).to_contain_text('Read the new handover rule')
        expect(main.locator('.home-clear')).to_have_count(0)
        expect(page.locator('.app-footer .page-status')).not_to_contain_text('Nothing needs you')
        item = main.locator('.home-notice-list li').filter(has_text='Read the new handover rule')
        expect(item.locator('.badge')).to_have_text('Read and acknowledge')
        row.get_by_role('button', name='Read').click()
        expect(item.get_by_role('button', name=re.compile('^I acknowledge'))).to_be_focused()
        page.keyboard.press('Enter')
        expect(item).to_contain_text('Acknowledged')
        expect(row).to_have_count(0)
    finally:
        context.close()


def test_a_lost_connection_is_said_once_and_the_rota_status_stays(browser, live_server, h):
    """H-13/N-10/H-14: every card read failing to reach the server shows one line with one Retry (no problem report on a
    lost connection); when only the counts fail, the footer still says the rota status."""
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        for path in ('**/api/home/rotas?**', '**/api/schedule/sessions?**', '**/api/calendar?**', '**/api/content?**'):
            page.route(path, lambda route: route.abort())
        # Reloads on #home: Home opened again within the tab answers from its kept replies (api.ts keptReads,
        # c6d9edec1), so its card reads would never meet the routes (as 4571a4e0e).
        page.evaluate("location.hash='home'")
        page.reload()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        line = main.locator('.home-offline')
        expect(line).to_have_count(1)
        expect(line).to_contain_text('Can’t reach the server.')
        expect(main.get_by_role('button', name='Report this problem')).to_have_count(0)
        page.unroute_all()
        line.get_by_role('button', name='Retry', exact=True).click()
        expect(line).to_have_count(0)
        expect(main.locator('.home-month')).to_have_count(4)
        page.route('**/api/home', lambda route: route.abort())
        page.reload()
        expect_loaded(page, 'home')
        expect(page.locator('.app-footer .page-status')).to_contain_text(re.compile(r' draft '))
    finally:
        context.close()


def test_a_server_fault_on_several_cards_is_said_once(browser, live_server, h):
    """Several cards failing with a server fault: one line with one Retry and one Report this problem; the cards' own
    notes are quiet (no alert per card)."""
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        for path in ('**/api/home/rotas?**', '**/api/schedule/sessions?**', '**/api/calendar?**', '**/api/content?**'):
            page.route(path, lambda route: route.fulfill(status=500, json={'detail': 'Simulated failure'}))
        page.locator('#department-nav [data-route=home]').click()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        line = main.locator('.home-offline')
        expect(line).to_contain_text('Couldn’t load parts of Home.')
        expect(main.get_by_role('alert')).to_have_count(1)
        expect(main.get_by_role('button', name='Report this problem')).to_have_count(1)
        page.unroute_all()
        line.get_by_role('button', name='Retry', exact=True).click()
        expect(line).to_have_count(0)
        expect(main.locator('.home-month')).to_have_count(4)
    finally:
        context.close()


def test_a_later_failed_month_does_not_hide_the_next_duty(browser, live_server, h):
    """The look-ahead months are read together but judged in date order: a duty in the first month shows even when the
    second month's read fails."""
    person = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = person
    day = local_today()
    starts = (day + timedelta(days=20)).isoformat()

    def lookahead(route):
        query = calendar_query(route.request.url)
        if not query or query.get('person_id') != [person]:
            return route.continue_()
        offset = (date.fromisoformat(query['start'][0]) - day).days
        if offset == 45:
            return route.fulfill(status=500, json={'detail': 'boom'})
        events = [{'kind': 'duty', 'person_id': person, 'cancelled': False, 'label': 'Look-ahead duty',
                   'start': f'{starts}T08:00:00+03:00', 'end': f'{starts}T12:00:00+03:00',
                   'services': [{'name': 'Look-ahead clinic'}]}] if offset == 14 else []
        route.fulfill(json={'events': events, 'pair_names': {}})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        page.route('**/api/calendar?**', lookahead)
        page.locator('#department-nav [data-route=home]').click()
        duties = page.locator('#main .home-duties')
        expect(duties).to_contain_text('Look-ahead clinic')
        expect(duties.locator('[role=alert], .note.error')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('blocking_rotas', [1, 2], ids=['wider', 'same-size'])
def test_a_blocking_draft_and_a_wider_gap_count_stay_two_rows(browser, live_server, h, blocking_rotas):
    """Blocking issues and an open places count whose drafts may differ (A, and A+B; or A+B and A+C: the same count of
    drafts is not the same drafts) are two rows, each naming its drafts with its own count."""
    typ = h.configure('assignment_types', {'name': 'Three open places', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 3}]}})['record']['id']
    drafts = [h.shift(h.workspace(name), typ=typ, people=[])['workspace'] for name in ('Draft A', 'Draft B')]
    summaries = [h.get('coordinator', f"/api/workspaces/{draft['id']}/summary") for draft in drafts]
    places_open = sum(summary['places_open'] for summary in summaries)
    assert places_open > sum(summary['sessions_with_gaps'] for summary in summaries)

    def attention(route):
        body = route_fetch(route).json()
        body['attention'] = [
            {'key': 'blocking', 'count': 1, 'rota_id': drafts[0]['id'], 'rota_name': drafts[0]['name'], 'rotas': blocking_rotas},
            {'key': 'open_places', 'count': places_open, 'rota_id': drafts[0]['id'], 'rota_name': drafts[0]['name'], 'rotas': 2}]
        route.fulfill(json=body)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        page.route('**/api/home', attention)
        page.locator('#department-nav [data-route=home]').click()
        main = page.locator('#main')
        blocking = main.locator('.home-needs-list li[data-need=blocking]')
        gaps = main.locator('.home-needs-list li[data-need=open_places]')
        expect(blocking).to_contain_text('Must fix before submitting')  # D9: one unit, causes
        expect(gaps).to_contain_text('Draft A and 1 other')
        expect(blocking).to_contain_text('Draft A and 1 other' if blocking_rotas == 2 else 'Draft A')
        expect(main.locator('.home-needs-list li')).to_have_count(2)
        expect(blocking.locator('.home-count')).to_have_text('1')
        expect(gaps.locator('.home-count')).to_have_text(str(places_open))  # no Fix list attached: the server's count
    finally:
        context.close()


def test_cards_retried_one_by_one_clear_the_lost_connection_line(browser, live_server, h):
    """N-10: once every card that failed offline loaded again through its own Retry (Needs you included), the page-wide
    "Can't reach the server" line goes."""
    populated(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'requests')
        for path in ('**/api/home', '**/api/workspaces?**', '**/api/schedule/sessions?**', '**/api/calendar?**', '**/api/content?**'):
            page.route(path, lambda route: route.abort())
        # Reloads on #home: Home opened again within the tab answers from its kept replies (api.ts keptReads,
        # c6d9edec1), so its card reads would never meet the routes (as 4571a4e0e).
        page.evaluate("location.hash='home'")
        page.reload()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        line = main.locator('.home-offline')
        expect(line).to_have_count(1)
        # Needs you is retried while still offline and fails again: still one entry for that card, not two.
        main.get_by_role('button', name='Retry loading what needs you').click()
        expect(main.get_by_role('button', name='Retry loading what needs you')).to_be_visible()
        page.unroute_all()
        retries = main.locator('.home-card button[aria-label^="Retry loading"]')
        expect(retries.first).to_be_visible()
        while retries.count():
            retries.first.click()
        expect(main.locator('.home-month')).to_have_count(4)
        expect(needs(page, 'Home draft rota')).to_be_visible()
        expect(line).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('key,target', [('rules', 'rules/policy-under-review'), ('exceptions', 'rules?exceptions=1')])
def test_needs_you_rule_change_deep_links(browser, live_server, h, key, target):
    """maintenance: Needs you › Rule change(s) / Rule exception(s) opens what waits (the server's link_target), not the bare
    Rules list; without one it still opens Rules."""
    context, page = sign_in(browser, live_server, 'approver')

    def with_item(route):
        reply = route_fetch(route)
        data = reply.json()
        data['attention'] = [*data.get('attention', []), {'key': key, 'count': 1, 'link_target': '#' + target,
            'sample': [{'name': 'Weekly limit', 'by': 'Coordinator'}] if key == 'rules' else []}]
        route.fulfill(response=reply, body=json.dumps(data))

    try:
        page.route(re.compile(r'/api/home$'), with_item)
        page.evaluate("location.hash = 'home'; location.reload()")
        page.locator(f'#main [data-need="{key}"]').get_by_role('button', name='Review', exact=True).click()
        page.wait_for_function('t => location.hash.startsWith("#" + t.split("?")[0])', arg=target)
        assert page.evaluate('location.hash').startswith('#rules')
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_today_says_people_on_duty_never_a_ratio(browser, live_server, h, scheme):
    """D14 (B6): each cell is the people on duty, with or without a configured minimum; no "2/2" ratio or bar."""
    day = local_today().isoformat()
    w = h.workspace('Configured and unset staffing minima', day, (local_today() + timedelta(days=1)).isoformat())
    for minimum, hour in ((2, 8), (0, 13)):
        typ = h.configure('assignment_types', {'name': f'Fictional minimum {minimum}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': minimum}]}})['record']['id']
        w = h.shift(w, start=f'{day}T{hour:02}:00:00+03:00', end=f'{day}T{hour+4:02}:00:00+03:00', typ=typ,
                    people=h.seed['people'][:2] if minimum else h.seed['people'][:1])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'home')
        today = page.locator('.home-today')
        expect(today.locator('.home-filled[title="2 on duty"] [aria-hidden=true]')).to_have_text('2')
        expect(today.locator('.home-filled[title="1 on duty"] [aria-hidden=true]')).to_have_text('1')
        expect(today.locator('.home-bar-mini')).to_have_count(0)
        expect(today.locator('.rp.open')).to_have_count(0)  # nobody short: no open chip
        expect(today).not_to_contain_text(re.compile(r'\d+/\d+'))
    finally:
        context.close()


def test_a_replaced_proposal_is_not_ready_on_home(browser, live_server, h):
    """R3-N: a proposal that a newer run of the same sessions replaced is not "proposal ready" on Home: its month shows
    the shared badge and its next step opens Auto-schedule on the latest run."""
    from apps.worker.main import process_one
    start, end = month_start(local_today(), 1), month_start(local_today(), 2)
    w = h.shift(h.workspace('Replaced proposal', start.isoformat(), end.isoformat()),
                start=f'{start.isoformat()}T08:00:00+03:00', end=f'{start.isoformat()}T12:00:00+03:00', people=[])['workspace']
    run = lambda: h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation',
        'generation_options': {'mode': 'fill_gaps'}}, revision=h.detail(w)['workspace']['revision'])['job']
    older = run()
    assert process_one(h.app.state.engine)
    assert h.get('coordinator', '/api/jobs/' + older['id'])['job']['result']['additions']
    run()  # a newer run of the same sessions, still queued
    assert h.get('coordinator', '/api/jobs/' + older['id'])['job']['currency'] == 'superseded'
    context, page = sign_in(browser, live_server, 'coordinator', viewport=DESKTOP)
    try:
        navigate(page, 'home')
        month = page.locator(f'#main .home-month[data-month="{start.isoformat()[:7]}"]')
        expect(month.locator('.badge', has_text='Replaced by a newer run')).to_be_visible()
        expect(month).not_to_contain_text('proposal ready')
        month.get_by_role('button', name=f"Review latest run for {w['name']}", exact=True).click()
        expect(page).to_have_url(re.compile(rf"edit={w['id']}&review=proposal$"))
        expect(page.locator('#auto-panel')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('who', ['admin', 'owner'])
def test_self_approved_row_opens_your_own_decisions(browser, live_server, h, who):
    """The owner and administrators hold no audit role: Open lists their own self-approved decisions, not a refused page."""
    grant = {'actions': ['rule.activate'], 'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Only one approver this quarter'}
    if who == 'admin':
        h.command('admin', 'self_approval.grant', {**grant, 'identity_id': h.seed['identities']['admin']})
    else:
        identity(h, 'owner', ['coordinator', 'approver', 'admin'])
        h.command('admin', 'self_approval.grant', {**grant, 'identity_id': h.seed['identities']['owner']})
        rule = h.command('owner', 'policy.create', {'name': 'Own rest rule', 'owner': 'QA', 'source': 'Home self-approval test',
            'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
        h.command('owner', 'policy.activate', {'reason': 'Reviewed with the clinical lead by phone'}, rule['id'], rule['revision'])
    name = h.get(who, '/api/me')['display_name']
    context, page = sign_in(browser, live_server, who, viewport=DESKTOP)
    refused = []
    page.on('response', lambda r: refused.append(r.url) if r.status == 403 else None)
    try:
        navigate(page, 'home')
        page.locator('#main .home-needs-list li[data-need=self_approved]').get_by_role('button', name='Open', exact=True).click()
        panel = page.get_by_role('region', name='Self-approved decisions')
        row = panel.locator('.audit-row')
        expect(row).to_have_count(1)
        expect(row).to_contain_text(name)
        expect(row.locator('.audit-self')).to_contain_text('Self-approved')
        assert refused == []
    finally:
        context.close()


def test_a_rota_months_ahead_is_offered_for_review_on_home(browser, live_server, h):
    """A submitted rota three months ahead waits in Needs you as Rota to review, and Review opens its review."""
    start = local_today() + timedelta(days=99)
    at = lambda hour: f'{start.isoformat()}T{hour:02}:00:00+03:00'
    w = h.workspace('Far review rota', start.isoformat(), (start + timedelta(days=7)).isoformat())
    w = h.lifecycle(h.shift(w, start=at(8), end=at(12))['workspace'], 'submit')['workspace']
    context, page = sign_in(browser, live_server, 'approver', DESKTOP)
    try:
        navigate(page, 'home')
        row = needs(page, 'Rota to review')
        expect(row).to_contain_text('Far review rota')
        row.get_by_role('button', name=re.compile('^Review')).click()
        dialog = page.get_by_role('dialog', name='Approve rota')
        expect(dialog).to_be_visible()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(page).to_have_url(re.compile('edit=' + re.escape(w['id'])))
    finally:
        context.close()
