"""maintenance/038/039 (cycle 4): staff and approver states that must say what is true. An approver's phone Home lists the
request waiting for them and names what needs a desktop; Requests tells a decider when nothing waits for them and
otherwise starts with what waits; a Schedule showing only your own duties says "You have no duties", never "Nothing
published", with Show everyone where the reader may see everyone's duties. Written for CI (not run on the PC)."""
import json
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, expect_loaded, route_fetch, view_settings, close_popover  # noqa: F401
from .test_browser_requests import PHONE, day, leave

pytestmark = pytest.mark.browser


def week(n):
    return f'{day(n)}T08:00:00+03:00', f'{day(n)}T12:00:00+03:00'


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_approver_phone_home_lists_the_request_waiting_and_names_desktop_work(browser, live_server, h, theme):
    leave(h)  # a staff member's leave request, waiting for an approver

    def with_rule(route):
        response = route_fetch(route)
        body = response.json()
        body['attention'].append({'key': 'rules', 'count': 1, 'sample': [{'name': 'Days in a row', 'by': 'Test Coordinator'}]})
        route.fulfill(response=response, body=json.dumps(body))
    context, page = sign_in(browser, live_server, 'approver', PHONE, phone_notice=True)
    try:
        page.emulate_media(color_scheme=theme)
        page.route('**/api/home', with_rule)
        page.reload()
        expect_loaded(page, 'home')
        main = page.locator('#main')
        request = main.locator('.home-needs-list li[data-need=requests]')
        expect(request).to_contain_text('Leave request')
        expect(request.get_by_role('button', name='Review, Leave request,')).to_be_visible()
        # A rule change is Rules work (desktop): it still counts, and the line says what it is.
        expect(main.locator('.home-elsewhere')).to_have_text('1 more item needs a desktop: Rule change')
        request.get_by_role('button', name='Review, Leave request,').click()
        expect(page).to_have_url(re.compile(r'#requests\?status=waiting$'))
        expect_loaded(page, 'requests')
        # Grouped by status, a card does not repeat its group's value (table_view.ts card, 0b98c6416): the heading says it.
        waiting = page.locator('#main .dt-card-group').first
        expect(waiting).to_contain_text('Waiting for approval')
        # Its cards follow the heading as siblings (not inside it), up to the next group's heading.
        group = "contains(concat(' ', @class, ' '), ' dt-card-group ')"
        cards = waiting.locator(f"xpath=following-sibling::*[not({group})][count(preceding-sibling::*[{group}]) = 1]"
                                "/descendant-or-self::*[contains(concat(' ', @class, ' '), ' dt-card ')]")
        expect(cards.filter(has_text='Leave')).to_have_count(1)
    finally:
        page.unroute_all(behavior='ignoreErrors')
        context.close()


@pytest.mark.parametrize('viewport', [None, PHONE], ids=['desktop', 'phone'])
def test_a_decider_lands_on_waiting_for_you_read_by_the_server(browser, live_server, h, viewport):
    """maintenance: whoever decides requests opens Requests on Waiting for you, read by the server (for_me=1), counted as
    their queue; empty, it says so and offers the whole list. Someone who decides nothing keeps the usual view."""
    decided = leave(h, on=day(1))
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Cover arranged'}, decided['id'], decided['revision'])
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'requests')
        views = page.locator('#main .dt-saved')
        expect(views).to_contain_text('Waiting for you')
        expect(page.get_by_role('heading', name='Nothing waiting for you', exact=True)).to_be_visible()
        page.get_by_role('button', name='Show all requests', exact=True).click()
        expect(views).to_contain_text('All requests')
        expect(page.locator('#main').get_by_text('Approved', exact=True).first).to_be_visible()
    finally:
        context.close()
    waiting = [leave(h, on=day(2)), leave(h, 'coordinator', h.seed['people'][1], day(3))]  # both wait for the approver
    queue = h.get('approver', '/api/requests/all?for_me=1')
    assert {r['id'] for r in queue['items']} == {r['id'] for r in waiting} and queue['counts']['to_decide'] == 2
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        with page.expect_response(lambda r: '/api/requests/all?' in r.url and 'for_me=1' in r.url) as read:
            navigate(page, 'requests')
        assert read.value.ok
        expect(page.locator('#main .dt-saved')).to_contain_text('Waiting for you')
        if viewport is None:  # the table's rows carry their ids: exactly the server's list
            expect(page.locator('#main tbody tr[data-row]')).to_have_count(2)
            assert set(page.locator('#main tbody tr[data-row]').evaluate_all('rs => rs.map(r => r.dataset.row)')) == {r['id'] for r in queue['items']}
        else:
            expect(page.locator('#main .dt-card')).to_have_count(2)
        page.locator('#main .dt-saved').click()
        menu = page.get_by_role('menu', name='Saved views')
        expect(menu.get_by_role('menuitemradio', name=re.compile('^Waiting for you'))).to_contain_text('2')  # counts.to_decide
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'staff', viewport)  # decides nothing: the usual view
    try:
        navigate(page, 'requests')
        expect(page.locator('#main .dt-saved')).not_to_contain_text('Waiting for you')
        page.locator('#main .dt-saved').click()
        expect(page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^Waiting for you'))).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('viewport,theme', [(None, 'light'), (PHONE, 'dark')], ids=['desktop-light', 'phone-dark'])
def test_staff_week_without_their_duties_says_so_not_nothing_published(browser, live_server, h, viewport, theme):
    """maintenance: the rota is published but the staff member is not on it this week (a colleague is)."""
    w = h.shift(h.workspace('Published without you', day(), day(7)), *week(0), people=[h.seed['people'][1]])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'staff', viewport)
    try:
        page.emulate_media(color_scheme=theme)
        navigate(page, f'schedule/week/{day()}')
        view = page.locator('#schedule-view')
        expect(view.get_by_role('heading', name='You have no duties this week', exact=True)).to_be_visible()
        expect(view).not_to_contain_text('Nothing published')
        # Staff read only their own duties: no Show everyone, and no My duties look-alike toggle that does nothing.
        expect(view.get_by_role('button', name='Show everyone', exact=True)).to_have_count(0)
        expect(page.locator('#main .planner-toolbar')).not_to_contain_text('My duties')
        expect(view.get_by_role('button', name=re.compile('^Next duty'))).to_have_count(0)  # none ahead
        navigate(page, 'home')
        expect(page.get_by_text('You have no duties coming up.', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Add to calendar', exact=True)).to_have_count(0)  # L18: nothing to add
    finally:
        context.close()


def test_staff_empty_week_offers_their_next_duty(browser, live_server, h):
    """L18: an empty week of your own duties gives the next step, your next duty, and opens its week."""
    w = h.shift(h.workspace('Later rota', day(14), day(21)), *week(15), people=[h.seed['people'][0]])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, f'schedule/week/{day()}')
        view = page.locator('#schedule-view')
        expect(view.get_by_role('heading', name='You have no duties this week', exact=True)).to_be_visible()
        view.get_by_role('button', name=re.compile('^Next duty: ')).click()
        expect(page).to_have_url(re.compile(rf'#schedule/week/{day(15)}$'))
        expect(view.locator('[data-shift]').first).to_be_visible()
    finally:
        context.close()


def test_my_duties_without_yours_offers_show_everyone(browser, live_server, h):
    """A coordinator with a staff profile turns My duties on: an empty week says it is theirs that is empty, and Show
    everyone turns My duties off (the colleague's duty is back)."""
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['coordinator']).person_id = h.seed['people'][2]
    w = h.shift(h.workspace('Published for a colleague', day(), day(7)), *week(0), people=[h.seed['people'][1]])['workspace']
    h.publish(w)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'schedule/week/{day()}')
        view = page.locator('#schedule-view')
        expect(view.locator('[data-shift]').first).to_be_visible()
        view_settings(page.locator('.planner-toolbar'), 'Layers').get_by_label('My duties', exact=True).check()
        close_popover(page)
        expect(view.get_by_role('heading', name='You have no duties this week', exact=True)).to_be_visible()
        view.get_by_role('button', name='Show everyone', exact=True).click()
        expect(view.get_by_role('heading', name='You have no duties this week', exact=True)).to_have_count(0)
        expect(view.locator('[data-shift]').first).to_be_visible()
    finally:
        context.close()


def test_credentialer_expiring_card_counts_people_per_day_and_clinic(browser, live_server, h):
    """L14: at department scale the card is one row per last day and clinic with how many people, not a list of names."""
    from datetime import timedelta
    from packages.domain.time import now as clock_now
    today = clock_now().date()
    with transaction(h.app.state.engine, write=True) as db:
        for person in h.seed['people'][:3]:
            db.add(m.Eligibility(department_id=h.seed['department_id'], person_id=person, service_id=h.seed['services'][0],
                                 starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=10)))
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'home')
        expiring = page.get_by_role('region', name='Expiring in 30 days')
        expect(expiring.locator('li')).to_have_count(1)
        expect(expiring.locator('li')).to_contain_text('3 people')
        expect(expiring.locator('li strong')).to_contain_text('Can work at')
        expect(expiring).to_contain_text('3 ending')
    finally:
        context.close()
