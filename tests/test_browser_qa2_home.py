"""QA rc.13 (Home, People, Requests, Inbox, shell, phone): regressions found on the department-scale dev copy."""
import re
from datetime import date

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in  # noqa: F401

pytestmark = pytest.mark.browser


def test_palette_finds_a_surname_and_one_long_group_cannot_hide_the_others(browser, live_server, h):
    """'rob' finds "Dr. Keiko Reed" (a word's start, not only the name's), and 45 rotas that also match show 8 of them,
    leaving room for the person. Each group keeps one heading."""
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed['organisation_id'], display_name='Dr. Keiko Reed')
        db.add(person)
        db.flush()
        db.add(m.Membership(person_id=person.id, department_id=h.seed['department_id'], starts_on=date(2020, 1, 1), category_id=h.seed['category']))
    for n in range(45):
        h.workspace(f'Reed rota {n:02d}', '2026-11-01', '2026-11-08')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.keyboard.press('Control+k')
        menu = page.get_by_role('dialog', name='Command menu')
        menu.get_by_role('combobox').fill('ree')
        expect(menu.get_by_role('option', name=re.compile(r'^Dr\. Keiko Reed'))).to_have_count(1)
        expect(menu.get_by_role('option', name=re.compile(r'^Reed rota'))).to_have_count(8)
        headings = menu.locator('.palette-group').all_inner_texts()
        assert len(headings) == len(set(headings)), headings
    finally:
        context.close()


def test_a_target_for_some_clinics_and_days_says_which(browser, live_server, h):
    """Workload › Balance goals and targets: a target that counts only some duties names its clinics and days, so
    "0 / 2" beside a month of duties is not read as a wrong count."""
    from .test_browser_person_profile import W, open_person, person, related, section
    from .test_browser_person_record import tab
    from .test_browser_rule_catalogue import catalogue_rule, line
    clinic = h.seed['services'][0]
    name = next(s['name'] for s in h.get('coordinator', '/api/catalogues')['services'] if s['id'] == clinic)
    from packages.domain.time import now as clock_now
    first = clock_now().date().replace(day=1)
    until = first.replace(year=first.year + 1).isoformat()
    # As on the dev data ("Ordinary UCC weekend limit"): at most 2 duties at one clinic on Fridays and Saturdays, per 28 days.
    quota = {'starts_on': first.isoformat(), 'ends_on': until, 'metric': 'duties', 'assignment_type_ids': [], 'participation': [],
             'overtime': 'exclude', 'window_days': 28, 'window_alignment': 'fixed', 'prorate_department_days': False,
             'reference_days': 28, 'rounding': 'nearest'}
    rule = catalogue_rule(h, 'duties_per_period', [line({'max': {'value': 2, 'level': 'mandatory'}}, weekdays=[4, 5],
                                                        where={'conditions': [{'field': 'service_ids', 'values': [clinic]}]},
                                                        settings={'block_quota': quota}, starts_on=first.isoformat(), ends_on=until)])
    related(h)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 1440, 'height': 900})
    try:
        shown = tab(open_person(page, person(h)), W)
        target = section(shown, 'Balance goals and targets').locator('.pr-line').filter(has_text=rule['name']).first
        expect(target).to_contain_text(re.compile(r'Maximum 2 · at ' + re.escape(name) + r' · Fri, Sat · '))
    finally:
        context.close()


def test_offer_swap_on_home_opens_the_offer_on_that_duty(browser, live_server, h):
    """Home › Next duty › Offer swap: the New request form opens on Swap a duty with that duty already chosen (not
    "Choose…" among all of them), and the address does not keep the duty."""
    from .governance_support import identity
    from .test_browser_requests import day
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    w = h.workspace('Requests rota', day(), day(7))
    for n in (0, 1):
        w = h.shift(w, f'{day(n)}T08:00:00+03:00', f'{day(n)}T12:00:00+03:00', people=[h.seed['people'][0]])['workspace']
    w = h.shift(w, f'{day(2)}T08:00:00+03:00', f'{day(2)}T12:00:00+03:00', people=[h.seed['people'][1]])['workspace']
    h.publish(w)
    first = min(h.get('staff', '/api/swaps/options')['assignments'], key=lambda a: a['start'])
    context, page = sign_in(browser, live_server, 'staff', {'width': 390, 'height': 844}, phone_notice=True)
    try:
        page.locator('.home-hero').get_by_role('button', name='Offer swap', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        expect(dialog.get_by_role('radio', name=re.compile('^Swap a duty'))).to_be_checked()
        expect(dialog.get_by_label('Your duty to swap', exact=True)).to_have_value(first['assignment_id'])
        expect(page).not_to_have_url(re.compile('duty='))
    finally:
        context.close()


def test_a_swap_card_on_a_phone_shows_both_names_in_full(browser, live_server, h):
    """Requests on a phone: a swap's title ("A and B") wraps in its card; it is not cut with an ellipsis that no hover
    can open."""
    from .test_browser_requests import published, rows  # noqa: F401
    from .test_staff_swaps import offered
    from .governance_support import identity
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    with transaction(h.app.state.engine, write=True) as db:
        for pid, name in zip(h.seed['people'][:2], ('Dr. Elena Parker', 'Dr. Elena Owens')):
            db.get(m.Person, pid).display_name = name
    pid = published(h, days=2)
    first = h.get('staff', '/api/swaps/options')['assignments'][0]['assignment_id']
    offered(h, pid, first)
    context, page = sign_in(browser, live_server, 'staff', {'width': 390, 'height': 844}, phone_notice=True)
    try:
        page.evaluate("location.hash='requests'")
        title = page.locator('#main .dt-card .request-title strong').filter(has_text='Parker').first
        expect(title).to_have_text('Dr. Elena Parker and Dr. Elena Owens')
        assert title.evaluate('e => e.scrollWidth <= e.clientWidth + 1'), 'title is cut'
    finally:
        context.close()


def test_a_published_month_with_a_draft_over_it_still_says_published(browser, live_server, h):
    """Home › Rota: a month that is published keeps "Published" and its published bar when a draft overlaps it (an
    amendment, a test rota); the draft's state is said beside it, not instead of it."""
    from datetime import timedelta
    from .test_browser_home import local_today, month_start
    day = local_today()
    first, end = month_start(day, 0), month_start(day, 1)
    w = h.workspace('This month rota', first.isoformat(), end.isoformat())
    w = h.shift(w, start=f'{day.isoformat()}T08:00:00+03:00', end=f'{day.isoformat()}T12:00:00+03:00')['workspace']
    h.publish(w)
    h.workspace('Overlapping draft', (day + timedelta(days=1)).isoformat(), (day + timedelta(days=8)).isoformat())
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("location.hash='home'")
        month = page.locator('#main .home-month').nth(1)
        expect(month).to_contain_text(re.compile(r'^\w+Published · draft · '))
        expect(month.locator('.fill.published')).to_have_count(1)
        expect(month.get_by_role('button', name='Open Overlapping draft')).to_be_visible()
    finally:
        context.close()
