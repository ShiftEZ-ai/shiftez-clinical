"""rc.16 frontend fixes from the post-merge audit: wish words for every kind, the kinds of wish under Auto-schedule &
scoring, Report this problem only on unexpected errors, Home's next step for an approver, overtime offered on the
person hub, the overtime Fair share suggestion, and swap kinds (settings, the offer form, Requests history)."""
import re
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from packages.domain.time import now
from .governance_support import identity
from .test_browser import department_tab, browser, live_server, sign_in, navigate, open_config, no_opening_hours  # noqa: F401
from .test_browser_requests import published, new_request, row, day
from .test_browser_person_profile import person, open_person, W
from .test_resources_calendars import calendar, update
from .test_overtime_offered_api import offer

pytestmark = pytest.mark.browser


def test_every_wish_kind_reads_in_words_never_undefined(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        words = page.evaluate("""async () => {
            const {wishWords, wishKindLabel} = await window.importBundle('wishes.js');
            const {WISH_KINDS} = await window.importBundle('generated.js');
            return Object.fromEntries(WISH_KINDS.map(kind => [kind,
                [wishWords({kind, limit: kind === 'whole_weekends' ? null : 3, points: 2}, []), wishKindLabel(kind), wishKindLabel(kind, true)]]));
        }""")
    finally:
        context.close()
    assert len(words) == 8
    for kind, texts in words.items():
        assert all(t and 'undefined' not in t and '_' not in t and '{' not in t for t in texts), (kind, texts)
    assert words['max_days_in_row'][0] == 'at most 3 days in a row (2 pts)'
    assert words['whole_weekends'][0] == 'whole weekends (2 pts)'


def test_report_this_problem_only_on_unexpected_errors(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'home')
        shown = page.evaluate("""async () => {
            const {note, notify} = await window.importBundle('dom.js');
            const {ApiError} = await window.importBundle('api.js');
            const offered = n => !!n.querySelector('.notice-report');
            const result = {
                none: offered(note('Choose a person before saving.', 'error')),
                validation: offered(note('Review the form values.', 'error', new ApiError('SCHEMA_INVALID', 'Review the form values.', 422))),
                thrown: offered(note('Choose a person.', 'error', new Error('Choose a person.'))),
                offline: offered(note('You are offline.', 'error', new ApiError('NETWORK_OFFLINE', 'You are offline.', 0))),
                server: offered(note('Wishes could not be loaded. The service had a problem.', 'error', new ApiError('SERVER_ERROR', 'The service had a problem.', 503))),
                lost: offered(note('Connection lost.', 'error', new ApiError('READ_UNAVAILABLE', 'Connection lost.', 0))),
                bug: offered(note('x is undefined', 'error', new TypeError('x is undefined'))),
            };
            notify('The end date must be after the start date.', 'error');
            return result;
        }""")
        assert shown == {'none': False, 'validation': False, 'thrown': False, 'offline': False, 'server': True, 'lost': True, 'bug': True}
        toast = page.locator('.toast.error').filter(has_text='The end date must be after the start date.')
        expect(toast).to_be_visible()
        expect(toast.get_by_role('button', name='Report this problem')).to_have_count(0)
        # A refusal from the server (4xx) is the person's to fix: no report offered.
        # Finish boot's shared inbox read before intercepting the next one from the bell.
        page.evaluate("async () => { const {background} = await window.importBundle('api.js'); await background('/api/notifications'); }")
        # The bell's menu reads its own short page (/api/notifications?limit=…, maintenance), so the path is matched with any query.
        page.route(re.compile(r'/api/notifications(\?.*)?$'), lambda route: route.fulfill(status=409, json={'error': {'code': 'STALE', 'message': 'Reload the inbox.'}}))
        with page.expect_response(lambda r: '/api/notifications' in r.url and r.status == 409):
            page.locator('.bell').click()
        refused = page.locator('.toast.error').filter(has_text='Reload the inbox.')
        expect(refused).to_be_visible()
        expect(refused.get_by_role('button', name='Report this problem')).to_have_count(0)
    finally:
        context.close()


def test_an_approver_gets_the_people_or_rules_step_on_home(browser, live_server, h):
    [seeded] = h.get('admin', '/api/policies?status=active')['policies']
    h.command('approver', 'policy.retire', {'reason': 'Synthetic setup without department rules'}, seeded['id'], seeded['revision'])
    # Opening hours are done when every service has them (setup_guide.ts C-04), so the next step is the approver's Rules.
    cal = calendar(h)
    for service in h.get('admin', '/api/catalogues')['services']:
        update(h, 'services', service, calendar_id=cal['id'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'home')
        card = page.locator('.setup-card')
        expect(card).to_contain_text('Next: rules')  # one line with the step's own button (H-07)
        expect(card.get_by_text('An admin finishes setup in Configuration.')).to_have_count(0)
        card.get_by_role('button', name='Add rules', exact=True).click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
    finally:
        context.close()


def test_person_workload_shows_overtime_offered(browser, live_server, h):
    today = now().date()  # this calendar month, the period the tab opens on (the last day is not included)
    offer(h, h.seed['people'][0], today.isoformat(), (today + timedelta(days=1)).isoformat(), '08:00', '12:00')
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        panel = open_person(page, person(h))
        panel.get_by_role('tab', name=W, exact=True).click()
        expect(panel.locator('.pr-stats')).to_contain_text('Overtime offered')
    finally:
        context.close()


def test_a_new_overtime_fair_share_line_takes_the_catalogue_suggestion(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'home')
        out = page.evaluate("""async () => {
            const {suggestedSettings} = await window.importBundle('rule_catalogue.js');
            const {RULE_CATALOGUE_V2} = await window.importBundle('generated.js');
            const e = RULE_CATALOGUE_V2.find(x => x.id === 'fair_share');
            return [suggestedSettings(e, {}, {balance_goal: {metric: 'overtime_hours'}}),
                    suggestedSettings(e, {}, {balance_goal: {metric: 'overtime_duties', period: 'month'}}),
                    suggestedSettings(e, {}, {balance_goal: {metric: 'duties'}}),
                    suggestedSettings(e, {balance_goal: {metric: 'overtime_hours'}}, {balance_goal: {metric: 'overtime_hours', period: 'month'}})];
        }""")
    finally:
        context.close()
    assert out[0] == {'balance_goal': {'metric': 'overtime_hours', 'period': 'quarter', 'share_by': 'offered'}}
    assert out[1] == {'balance_goal': {'metric': 'overtime_duties', 'period': 'month', 'share_by': 'offered'}}  # chosen in this edit: kept
    assert out[2] == {'balance_goal': {'metric': 'duties'}}
    assert out[3] == {'balance_goal': {'metric': 'overtime_hours', 'period': 'month'}}  # metric unchanged: nothing rewritten


def test_swap_kinds_in_settings_and_the_offer_form(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap')
        kind = dialog.get_by_label('Kind of offer', exact=True)
        expect(kind.locator('option')).to_have_count(3)
        expect(dialog.get_by_label('Swap with', exact=True)).to_be_visible()
        kind.select_option('give')
        expect(dialog.get_by_label('Swap with', exact=True)).to_be_hidden()  # a give-away goes to every eligible colleague
        dialog.get_by_label('Message to colleagues', exact=True).fill('Anyone free?')
        dialog.get_by_role('button', name='Send offer', exact=True).click()
        expect(page.get_by_text('Offer shown to eligible colleagues', exact=False)).to_be_visible()
        expect(row(page, 'Open give-away')).to_be_visible()
    finally:
        context.close()
    [swap] = [s for s in h.get('staff', '/api/swaps')['swaps']]
    assert swap['kind'] == 'give' and swap['open_offer'] and swap['recipient_person_id'] is None
    # The admin allows only exchanges, without open offers: the form then names a colleague and offers no choice.
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Department settings')
        department_tab(page, 'Swaps')
        pane = page.locator('.config-pane')
        pane.get_by_label('Open swap offers', exact=True).uncheck()
        pane.get_by_role('group', name='Swap kinds allowed').get_by_role('checkbox', name='Give away', exact=True).uncheck()
        pane.get_by_role('button', name='Save settings', exact=True).click()
        page.get_by_role('dialog').get_by_label('Reason', exact=True).fill('Exchanges only')
        page.get_by_role('dialog').get_by_role('button', name='Save settings', exact=True).click()
        expect(page.get_by_role('button', name='Undo', exact=True)).to_be_visible()
    finally:
        context.close()
    department = h.get('admin', '/api/me')['department']
    assert department['swap_kinds_allowed'] == ['exchange'] and department['swap_open_offers'] is False
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap')
        expect(dialog.get_by_label('Swap with', exact=True)).to_be_visible()
        expect(dialog.get_by_label('Kind of offer', exact=True)).to_have_count(0)
    finally:
        context.close()


def test_requests_history_names_an_accepted_swap(browser, live_server, h):
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    publication = published(h)
    duty = h.get('staff', '/api/swaps/options')['assignments'][0]
    swap = h.command('staff', 'swap.offer', {'kind': 'give', 'publication_id': publication,
                     'assignment_id': duty['assignment_id'], 'recipient_person_id': h.seed['people'][1],
                     'expires_at': (now() + timedelta(days=1)).isoformat(), 'reason': 'Can you take this one?'})['swap']
    h.command('staff_two', 'swap.accept', {'reason': 'Happy to cover'}, swap['id'], swap['revision'])
    context, page = sign_in(browser, live_server, 'staff')
    try:
        navigate(page, 'requests')
        row(page, 'Swap').click()
        history = page.locator('.request-history')
        expect(history).to_contain_text('Accepted')
        expect(history).not_to_contain_text('accept ')
    finally:
        context.close()


def test_home_setup_card_only_for_who_can_act_and_hours_asked_once(browser, live_server, h):
    """H-02: an approver whose next step is an admin's sees no setup card. H-03: an admin whose Needs you already asks
    for opening hours gets the card's progress and Continue setup, without naming opening hours again."""
    no_opening_hours(h)
    [seeded] = h.get('admin', '/api/policies?status=active')['policies']
    h.command('approver', 'policy.retire', {'reason': 'Synthetic setup without department rules'}, seeded['id'], seeded['revision'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'home')
        expect(page.locator('.home-needs')).to_be_visible()
        expect(page.locator('.setup-card')).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'home')
        card = page.locator('.setup-card')
        expect(card.get_by_role('button', name='Continue setup', exact=True)).to_be_visible()
        if page.locator('.home-needs-list li').filter(has_text='without opening hours').count():
            expect(card).not_to_contain_text('opening hours')
    finally:
        context.close()


def test_credentialer_home_lists_what_ends_in_30_days(browser, live_server, h):
    from apps.api import models as m
    from apps.api.db import transaction
    from datetime import date
    today = now().date()
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][0], service_id=h.seed['services'][0],
                             starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=10)))
        db.add(m.Eligibility(department_id=h.seed['department_id'], person_id=h.seed['people'][1], service_id=h.seed['services'][0],
                             starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=90)))
    context, page = sign_in(browser, live_server, 'credentialer')
    try:
        navigate(page, 'home')
        expiring = page.get_by_role('region', name='Expiring in 30 days')
        expect(expiring.locator('li')).to_have_count(1)
        expect(expiring).to_contain_text('Can work at')
        expect(expiring).to_contain_text('1 ending')
    finally:
        context.close()


def test_approver_home_shows_decided_requests_only(browser, live_server, h):
    """Only leave and other requests someone approved or declined, latest decision first (not latest dates): not open
    ones, nor leave a coordinator recorded directly (nobody decided it)."""
    from .test_browser_requests import leave, day
    made = [leave(h, on=day(n)) for n in (1, 3, 5, 7)]
    # Decided in the order day 5, day 1, day 3.
    for request, decision in ((made[2], 'approve'), (made[0], 'approve'), (made[1], 'reject')):
        h.command('approver', 'request.decide', {'decision': decision, 'reason': 'Decided for the test'}, request['id'], request['revision'])
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][1], 'kind': 'recorded_leave', 'leave_type': 'Annual leave',
              'start': f'{day(9)}T00:00:00+03:00', 'end': f'{day(10)}T00:00:00+03:00', 'private_reason': 'Recorded directly'})
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'home')
        decided = page.get_by_role('region', name='Decided requests')
        rows = decided.locator('li')
        expect(rows).to_have_count(3)  # the open fourth request and the recorded leave are not decisions
        expect(rows.nth(0)).to_contain_text('Declined')  # day 3, decided last
        expect(rows.nth(1)).to_contain_text('Approved')  # day 1
        expect(rows.nth(2)).to_contain_text('Approved')  # day 5, decided first
    finally:
        context.close()


def test_swap_form_follows_allowed_kinds_from_options_and_says_when_swaps_are_off(browser, live_server, h):
    import json
    from .test_browser import route_fetch
    identity(h, 'staff_two', ['staff'], h.seed['people'][1])
    published(h)
    context, page = sign_in(browser, live_server, 'staff')
    try:
        allowed = {'kinds': ['give']}

        def options(route):
            body = route_fetch(route).json()
            body['allowed_kinds'] = allowed['kinds']
            route.fulfill(content_type='application/json', body=json.dumps(body))
        page.route('**/api/swaps/options', options)
        navigate(page, 'requests')
        dialog = new_request(page, 'Swap')
        # The server's allowed kinds win over the department record (both kinds there): only a give-away is left.
        expect(dialog.get_by_label('Kind of offer', exact=True)).to_have_count(0)
        expect(dialog.get_by_label('Swap with', exact=True)).to_be_hidden()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.get_by_role('button', name='Discard', exact=False).click()
        allowed['kinds'] = []
        dialog = new_request(page, 'Swap')
        expect(dialog.get_by_text('Swaps are switched off for this department.')).to_be_visible()
        expect(dialog.get_by_role('button', name='Send offer', exact=True)).to_be_disabled()
    finally:
        context.close()


def test_rescoring_is_mentioned_only_when_score_weights_change(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        pane.get_by_role('tab', name='Staff preferences', exact=True).click()
        pane.get_by_label('Wish points per person (0 = wishes off)', exact=True).fill('12')
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog', name='Save auto-schedule defaults', exact=True)
        expect(dialog).to_contain_text('New auto-schedule runs start from these.')
        expect(dialog).not_to_contain_text('recalculated')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        # A weight typed back to its saved value is no change either.
        pane.get_by_role('tab', name='How it decides', exact=True).click()
        coverage = pane.get_by_label('Coverage weight', exact=True)
        saved = coverage.input_value()
        coverage.fill(str(int(float(saved)) + 5))
        coverage.fill(saved)
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        expect(dialog).not_to_contain_text('recalculated')
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        coverage.fill(str(int(float(saved)) + 5))
        pane.get_by_role('button', name='Save defaults', exact=True).click()
        expect(dialog).to_contain_text('Scores of existing rotas are recalculated with the new weights.')
    finally:
        context.close()
