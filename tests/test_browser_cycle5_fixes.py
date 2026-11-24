"""an honest connection status and automatic recovery (D-1,
D-11), no "Saved" before the person saves (D-4), one-click Retry on a recoverable refusal (D-4), and an impossible
range said at its field with no "no duplicate or overlap" tick (D-3)."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, route_fetch  # noqa: F401
from .test_browser_fairness_dashboard import october, published_month, section
from .test_browser_loading import paused
from .test_browser_published_changes import published
from .test_browser_rule_catalogue import flag
from .test_browser_rule_catalogue_v2 import line, themed, v2_rule

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (390, 'dark')]
GATEWAY = {'status': 503, 'content_type': 'text/html', 'body': '<html><body>503 Service Unavailable</body></html>'}


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_server_that_stops_answering_says_reconnecting_and_recovers_by_itself(browser, live_server, width, theme):
    """D-1/D-11: reads refused by the gateway (503 while the server restarts or is overloaded) turn the footer to
    "Reconnecting…" (a spinner and the word), never "Connected", and never sign out (D63). When a read succeeds again
    the footer says Connected and the page reads afresh by itself (its "Couldn't refresh" banner goes)."""
    context, page = themed(browser, live_server, 'admin', width, theme)
    try:
        navigate(page, 'people')
        expect(page.locator('#main').get_by_text('Staff 01(test)', exact=True).first).to_be_visible()
        connection = page.locator('#connection')
        expect(connection).to_have_accessible_name('Connected')
        page.route('**/api/**', lambda route: route.fulfill(**GATEWAY))
        page.evaluate("window.dispatchEvent(new Event('online'))")  # a refresh, as any read the page makes
        expect(connection).to_have_accessible_name('Reconnecting… · the server is not answering')
        expect(connection.locator('.status-word')).to_have_text('Reconnecting…')
        expect(connection.locator('.spinner')).to_have_count(1)
        expect(page.locator('#main > .stale-banner')).to_have_count(1)
        expect(page.locator('#main').get_by_text('Staff 01(test)', exact=True).first).to_be_visible()  # the data stays
        expect(page.locator('.login-layout')).to_have_count(0)  # never signed out
        page.unroute('**/api/**')
        # The app's own next read (its periodic pulse) answers: nobody presses anything.
        page.evaluate("window.importBundle('api.js').then(m => m.background('/api/pulse'))")
        expect(connection).to_have_accessible_name('Connected')
        expect(page.locator('#main > .stale-banner')).to_have_count(0)
        expect(page.locator('#main').get_by_text('Staff 01(test)', exact=True).first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_slow_read_is_not_an_outage_a_page_says_taking_longer_and_a_refresh_says_updating(browser, live_server, width, theme):
    """a read still waiting is not an outage: the connection stays Connected, never
    "Server busy". An opening page's read: its skeleton past 15 s says "Still loading · 0:15", no Retry while live (no
    "Updating…"). A background refresh (after a save) past 10 s: the footer's left says a neutral "Updating…" until it
    lands. Both go once the answer comes."""
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    held = []
    try:
        navigate(page, 'home')
        expect(page.locator('#main .loading')).to_have_count(0)
        paused(page)
        # The rota list answers at once from boot's kept reply (api.ts keptReads); the Schedule's saved views are read
        # on its first opening only, so that read is the one the skeleton waits for.
        views = re.compile(r'/api/table-views\?table=planner')
        page.route(views, lambda route: held.append(route))
        page.evaluate("location.hash = '#schedule'")
        page.wait_for_function('() => document.querySelector("#main .loading")')
        page.clock.run_for(10_000)
        connection = page.locator('#connection')
        expect(connection).to_have_accessible_name('Connected')
        updating = page.locator('.app-footer .footer-updating')
        expect(updating).to_be_hidden()  # the page's own wait, said by its skeleton
        page.clock.run_for(5_000)
        slow = page.locator('#main .loading .sk-slow')
        expect(slow).to_contain_text('Still loading · 0:1')  # the one quiet line, its elapsed time (DL-045)
        expect(slow.get_by_role('button', name='Retry')).to_have_count(0)  # never Retry while the read is live
        for route in held:
            route.continue_()
        page.unroute(views)
        page.clock.resume()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'schedule', timeout=30_000)
        expect(connection).to_have_accessible_name('Connected')
        # A save confirmed after an unknown outcome reads the page again quietly (api.ts `replayed`): a background read.
        held.clear()
        paused(page)
        page.route('**/api/workspaces?**', lambda route: held.append(route))
        with page.expect_request(lambda r: '/api/workspaces?' in r.url):
            page.evaluate("window.dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'shift.update',payload:{},result:null,replayed:true}}))")
        page.clock.run_for(10_000)
        expect(updating).to_have_text('Updating…')
        expect(connection).to_have_accessible_name('Connected')
        for route in held:
            route.continue_()
        page.unroute('**/api/workspaces?**')
        page.clock.resume()
        expect(updating).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_opening_a_rota_says_no_save_until_the_person_saves(browser, live_server, h, theme):
    """D-4: opening a draft (its check out of date, workers serving jobs) shows no save state at all; the person's own
    save then says Saved."""
    w = h.shift(h.workspace('Footer save rota'))['workspace']
    context, page = themed(browser, live_server, 'coordinator', 1440, theme)
    try:
        page.route('**/health/service', lambda route: route.fulfill(status=200, json={'status': 'ok'}))

        def stale(route):
            reply = route_fetch(route, headers={k: v for k, v in route.request.headers.items() if k != 'if-none-match'})
            data = reply.json()
            data['workspace']['validation'] = {**(data['workspace'].get('validation') or {}), 'fingerprint': 'out-of-date'}
            data['workspace']['validation_current'] = False  # the server's word that its check is out of date
            route.fulfill(status=200, json=data)
        page.route(re.compile(r'.*/api/workspaces/' + w['id'] + r'(\?.*)?$'), stale)
        navigate(page, f"schedule/week/{w['starts_on']}?edit={w['id']}")
        expect(page.locator(f'#schedule-view {DRAFT_CHIP}').first).to_be_visible()
        # Whether or not the app checks the rota on opening (it no longer does for a current one), nothing about saving
        # shows until the person saves.
        expect(page.locator('#save-state')).to_be_hidden()
        assert page.locator('#save-state').get_attribute('aria-label') is None
        page.evaluate("window.importBundle('api.js').then(m => m.command('table_view.create', {table_key: 'people', name: 'My own save', definition: {}}))")
        expect(page.locator('#save-state')).to_have_attribute('aria-label', re.compile('^Saved'))
    finally:
        page.unroute_all(behavior='ignoreErrors')  # a rota read still in flight through `stale` must not fail teardown
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_recoverable_refusal_offers_retry_that_keeps_what_was_entered(browser, live_server, width, theme):
    """D-4: "The rota changed during preparation; refresh before applying again" (recoverable) comes with Retry in the
    form; Retry sends the form again with what was entered, and it saves."""
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    refused = []

    def commands(route):
        if not refused:
            refused.append(route.request.post_data_json)
            return route.fulfill(status=409, json={'error': {'code': 'STALE_INPUTS', 'details': {'recoverable': True},
                                                             'message': 'The rota changed during preparation; refresh before applying again'}})
        route.continue_()
    try:
        navigate(page, 'home')
        page.route('**/api/commands', commands)
        page.evaluate("""async () => {const D = await window.importBundle('dom.js'), A = await window.importBundle('api.js');
          D.formDialog('Name a view', '', form => {const f = D.field('View name', 'text', ''); f.input.required = true; form.append(f.wrap);
            return async () => { await A.command('table_view.create', {table_key: 'people', name: f.input.value, definition: {}}); };});}""")
        dialog = page.get_by_role('dialog', name='Name a view')
        dialog.get_by_label('View name', exact=True).fill('Kept through Retry')
        dialog.get_by_role('button', name='Save', exact=True).click()
        refusal = dialog.locator('.notice').filter(has_text='The rota changed during preparation')
        expect(refusal).to_be_visible()
        expect(dialog.get_by_label('View name', exact=True)).to_have_value('Kept through Retry')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.status == 200):
            refusal.get_by_role('button', name='Retry', exact=True).click()
        expect(dialog).to_have_count(0)
        assert refused[0]['payload']['name'] == 'Kept through Retry'
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_compare_with_names_each_goal_by_what_tells_it_apart(browser, live_server, h, width, theme):
    """C3-F4: goals that read alike (one rule's lines for the same group and period) are told apart on their cards and
    in Compare with by the facts the API sends: how each is shared, then a number in the rule's order. No two choices
    read the same."""
    published_month(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)

    def alike(route):
        reply = route_fetch(route)
        data = reply.json()
        base = next(g for g in data['goals'] if not g.get('pairs'))
        same = {'name': 'Fair share', 'audience_label': 'Consultant', 'metric': 'duties', 'period_start': '2026-10-01',
                'period_end': '2026-11-01', 'comparison': 'within_group'}
        data['goals'] = [{**base, **same, 'goal_id': f'g{n}', 'share_by': share, 'line': line}
                         for n, (share, line) in enumerate([('available_days', 0), ('equal', 1), ('available_days', 2)])] + [
            {**base, **same, 'goal_id': 'g3', 'audience_label': 'Resident', 'share_by': 'available_days', 'line': 3}]
        route.fulfill(response=reply, json=data)
    try:
        page.route(re.compile(r'.*/api/fairness/goals\?.*'), alike)
        navigate(page, 'insights/fairness')
        october(page)
        titles = section(page, 'goals').locator('.fx-goal-name h3').all_text_contents()
        assert len(titles) == 4 and len(set(titles)) == 4, titles
        assert 'Consultant · Duties · Shared equally' in titles, titles
        assert 'Resident · Duties' in titles, titles
        # Still alike after every fact: a number in the rule's order, never "line N" (maintenance, fairness.ts goalLabels).
        alike = 'Consultant · Duties · Shared by days each person is available'
        assert [t for t in titles if t.startswith(alike)] == [f'{alike} (1)', f'{alike} (2)'], titles
        choices = section(page, 'distribution').get_by_label('Compare with', exact=True).locator('option').all_text_contents()
        assert len(choices) == len(set(choices)) == 5, choices  # the reference and the four goals
        assert [c for c in choices if c != 'Reference (not a goal)'] == titles, (choices, titles)
    finally:
        context.close()


@pytest.mark.parametrize('width,theme', VIEWS)
def test_a_failed_read_of_swap_options_says_it_could_not_load_never_that_a_save_is_uncertain(browser, live_server, h, width, theme):
    """C3-F5: the swap options read refused while the database is unavailable (the server's message is worded for a
    write) says plainly that they could not be loaded, with Retry; nothing suggests a change may not be saved. Retry
    loads the form once the server answers."""
    published(h)
    context, page = themed(browser, live_server, 'staff', width, theme)
    try:
        page.route('**/api/swaps/options*', lambda route: route.fulfill(status=503, json={'error': {'code': 'DATABASE_UNAVAILABLE',
            'message': 'The system is unavailable, so we could not confirm whether this change was saved. Try again.'}}))
        navigate(page, 'requests')
        page.get_by_role('button', name='New request', exact=True).click()
        dialog = page.get_by_role('dialog', name='New request')
        dialog.get_by_role('radio', name='Swap a duty').check()
        failed = dialog.locator('.notice').filter(has_text='Couldn’t load the swap options.')
        expect(failed).to_contain_text('The system is unavailable right now. Try again in a moment.')
        expect(dialog).not_to_contain_text('saved')
        page.unroute('**/api/swaps/options*')
        dialog.get_by_role('button', name='Retry', exact=True).click()
        expect(failed).to_have_count(0)
        expect(dialog.get_by_role('button', name='Send offer', exact=True)).to_be_visible()  # the swap form, loaded
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()
