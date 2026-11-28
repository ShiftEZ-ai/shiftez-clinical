"""Visible rule information, truthful lazy history and readable phone scope rows."""
import json
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity, retire_rule
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_rule_templates import propose, activate
from .rules_support import rule_status

pytestmark = pytest.mark.browser


def test_rule_tabs_label_metadata_and_build_only_open_details(browser, live_server, h):
    activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1}, name='Named cover',
                        listed_only=True, per_person=[{'person_id': h.seed['people'][0]}]))
    context, page = sign_in(browser, live_server, 'coordinator', {'width': 390, 'height': 844})
    try:
        navigate(page, 'rules')
        item = page.locator('.rule-item').filter(has_text='Named cover')
        expect(item).to_contain_text('At least 1 person per session')
        expect(item.locator('.rl-who')).to_contain_text('1 person')
        expect(page.locator('.rule-detail')).to_have_count(0)
        item = open_rule(page, 'Named cover')
        expect(item.locator('.rule-detail')).to_have_count(1)
        expect(item.locator('.rule-detail .rule-facts dt')).to_have_text(['Effective', 'Exception requests', 'Checked against'])
        expect(item.locator('.rl-sentence')).to_contain_text('Applies only to the people and programmes listed below.')
        expect(item.locator('.rule-detail summary')).to_have_count(0)
        tabs = item.get_by_role('tablist', name='Rule information')
        details = tabs.get_by_role('tab', name='Details', exact=True)
        source = tabs.get_by_role('tab', name='Source', exact=True)
        details.press('End')
        expect(source).to_be_focused()
        source.press('Enter')  # tabs activate manually: arrows and Home/End move focus, Enter opens
        expect(source).to_have_attribute('aria-selected', 'true')
        panel = item.locator('#' + source.get_attribute('aria-controls'))
        expect(panel).to_be_visible()
        assert panel.get_attribute('aria-labelledby') == source.get_attribute('id')
        expect(panel).to_contain_text('Policy owner')
        expect(panel).to_contain_text('Source or decision')
        source.press('Home')
        expect(details).to_be_focused()
        details.press('Enter')
        expect(item.locator('.rl-excs')).to_be_visible()
        assert page.evaluate('() => document.scrollingElement.scrollWidth <= innerWidth')
        source.click()
        page.keyboard.press('Escape')  # closing and reopening the sheet keeps the rule's tab
        expect(item).to_have_count(0)
        item = open_rule(page, 'Named cover')
        expect(item.locator('.rule-detail')).to_have_count(1)
        expect(source).to_have_attribute('aria-selected', 'true')
        page.keyboard.press('Escape')
        page.get_by_label('Find rules', exact=True).fill('Named cover')  # so does a re-render of the list
        item = open_rule(page, 'Named cover')
        source = item.get_by_role('tab', name='Source', exact=True)
        expect(source).to_have_attribute('aria-selected', 'true')
        expect(item.locator('#' + source.get_attribute('aria-controls'))).to_be_visible()
    finally:
        context.close()


def test_rule_history_shares_inflight_read_and_retries_failure(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest requirement'))
    activate(h, propose(h, {'kind': 'max_hours', 'hours': 40, 'window': 'week'}, name='Hours requirement'))
    response = h.get('coordinator', '/api/policies?status=retired&effective=true')
    context, page = sign_in(browser, live_server, 'coordinator')
    held = []
    try:
        page.route('**/api/policies?status=retired&effective=true', lambda route: held.append(route))
        navigate(page, 'rules')
        for title in ('Rest requirement', 'Hours requirement'):
            if title == 'Hours requirement':
                page.keyboard.press('Escape')
                expect(page.locator('.dock-panel.rule-sheet')).to_have_count(0)
            sheet = open_rule(page, title)
            sheet.get_by_role('tab', name='History', exact=True).click()
            expect(sheet.get_by_text('Loading earlier versions…', exact=True)).to_be_visible()
        assert len(held) == 1  # the second rule shares the read already in flight
        held.pop().fulfill(status=503, content_type='application/json', body=json.dumps({'detail': 'History unavailable'}))
        expect(sheet.get_by_role('button', name='Retry loading history')).to_have_count(1)
        sheet.get_by_role('button', name='Retry loading history').click()
        expect(sheet.get_by_text('Loading earlier versions…', exact=True)).to_have_count(1)
        assert len(held) == 1
        held.pop().fulfill(status=200, content_type='application/json', body=json.dumps(response))
        expect(sheet.get_by_text('No earlier versions. This is the first version of this rule.', exact=True)).to_have_count(1)
        # The first rule's history, opened again (on its History tab), reads the successful page-owned cache.
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        sheet = open_rule(page, 'Rest requirement')
        expect(sheet.get_by_role('tab', name='History', exact=True)).to_have_attribute('aria-selected', 'true')
        expect(sheet.get_by_text('No earlier versions. This is the first version of this rule.', exact=True)).to_have_count(1)
        assert not held
    finally:
        context.close()


def test_long_rule_section_shows_fifty_then_all_and_builds_only_requested_scope_tables(browser, live_server, h):
    for number in range(51):
        propose(h, {'kind': 'minimum_cover', 'minimum': 1}, name=f'Cover scope {number:02}',
                listed_only=True, per_person=[{'person_id': h.seed['people'][0]}])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        covers = page.locator('.rule-item').filter(has_text='Cover scope')
        expect(covers).to_have_count(50)
        expect(page.locator('.rule-item')).to_have_count(51)  # Capped cover section plus seeded Fair share.
        expect(page.locator('.rl-excs')).to_have_count(0)
        sheet = open_rule(page, 'Cover scope 00')
        expect(page.locator('.rl-excs')).to_have_count(1)  # built only for the opened rule
        page.keyboard.press('Escape')
        expect(sheet).to_have_count(0)
        expect(page.locator('.rl-excs')).to_have_count(0)
        page.get_by_role('button', name='Show 1 more', exact=True).click()
        expect(covers).to_have_count(51)
        expect(page.locator('.rule-item')).to_have_count(52)
        expect(page.locator('.rl-excs')).to_have_count(0)  # the rest stay unbuilt
        expect(page.get_by_role('button', name='Show 1 more', exact=True)).to_have_count(0)
        page.get_by_label('Find rules', exact=True).fill('Cover scope 50')
        expect(page.locator('.rule-item')).to_have_count(1)
        expect(page.locator('.rl-excs')).to_have_count(0)
    finally:
        context.close()


def test_rules_history_failure_cannot_replace_current_rules_and_can_retry(browser, live_server, h):
    activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Current rest requirement'))
    response = h.get('coordinator', '/api/policies?status=retired&effective=true')
    context, page = sign_in(browser, live_server, 'coordinator')
    held, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    pattern = '**/api/policies?status=retired&effective=true'
    try:
        page.route(pattern, lambda route: held.append(route))
        navigate(page, 'rules')
        with page.expect_request(pattern):
            rule_status(page, 'History')
        rule_status(page, 'Current')
        with page.expect_response(pattern):
            held.pop().fulfill(status=503, json={'detail': 'History unavailable'})
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(page.locator('.rule-toolbar button.dt-saved')).to_have_attribute('aria-label', re.compile(r'^Saved views: Current\b'))
        expect(page.locator('.rule-item')).to_have_count(2)
        expect(page.locator('.rule-item').filter(has_text='Current rest requirement')).to_contain_text('Current rest requirement')
        expect(page.locator('#rule-status-panel .note.error')).to_have_count(0)
        # A request event can precede the route callback; let each handler reply
        # and wait for its response instead of immediately popping the collector.
        page.route(pattern, lambda route: route.fulfill(status=503, json={'detail': 'History unavailable'}), times=1)
        with page.expect_request(pattern), page.expect_response(pattern) as failed_history:
            rule_status(page, 'History')
        assert failed_history.value.status == 503
        retry = page.get_by_role('button', name='Retry loading history', exact=True)
        expect(retry).to_be_visible()
        page.route(pattern, lambda route: route.fulfill(status=200, json=response), times=1)
        with page.expect_request(pattern), page.expect_response(pattern) as retried_history:
            retry.click()
        assert retried_history.value.status == 200
        expect(page.get_by_text('No history yet', exact=True)).to_be_visible()
        assert not errors
    finally:
        context.close()


def test_rules_history_reads_every_retained_page(browser, live_server, h):
    """Retained history is paged (/api/policies has_more/offset): History lists the rules on every page, not the first."""
    for name in ('Retained rule one', 'Retained rule two'):
        rule = propose(h, {'kind': 'min_rest', 'hours': 11}, name=name)
        h.command('approver', 'policy.decline', {'reason': 'Synthetic retained history'}, rule['id'], rule['revision'])
    full = h.get('coordinator', '/api/policies?status=retired&effective=true')
    assert {'Retained rule one', 'Retained rule two'} <= {p['name'] for p in full['policies']} and not full['has_more']
    first = full['policies'][:1]
    page_one = {**full, 'policies': first, 'has_more': True, 'limit': 1,
                'effective': {k: v for k, v in (full.get('effective') or {}).items() if k == first[0]['id']}}
    context, page = sign_in(browser, live_server, 'coordinator')
    offsets = []
    try:
        page.route('**/api/policies?status=retired&effective=true', lambda route: route.fulfill(json=page_one))
        page.on('request', lambda r: offsets.append(r.url.split('offset=')[1]) if '/api/policies?status=retired' in r.url and 'offset=' in r.url else None)
        navigate(page, 'rules')
        rule_status(page, 'History')
        for name in ('Retained rule one', 'Retained rule two'):  # page one holds one row, so one of them is on page two
            expect(page.locator('.rule-item').filter(has_text=name)).to_have_count(1)
        assert offsets == ['1']
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_rules_effective_date_empty_states_preserve_attention_and_history_recovery(browser, live_server, h, viewport):
    rule = propose(h, {'kind': 'min_rest', 'hours': 11}, name='Fictional October rest',
                   starts_on='2026-10-01', ends_on='2026-11-01')
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        navigate(page, 'rules')
        rule_status(page, 'Needs attention')
        attention = page.get_by_role('region', name='Needs attention', exact=True)
        expect(attention.locator('.rule-item')).to_contain_text(rule['name'])
        effective = page.get_by_label('Effective on', exact=True)
        effective.fill('2026-09-01')
        expect(attention.get_by_role('heading', name='No matching rules', exact=True)).to_be_visible()
        expect(attention.get_by_text('Every rule is active and working.', exact=False)).to_have_count(0)
        expect(effective).to_be_visible()
        effective.fill('')
        expect(attention.locator('.rule-item')).to_contain_text(rule['name'])

        rule = activate(h, rule)
        retire_rule(h, rule, 'Synthetic historical date boundary')
        propose(h, {'kind': 'max_hours', 'hours': 40, 'window': 'week'}, name='Fictional current hours')
        page.reload()
        navigate(page, 'rules')
        rule_status(page, 'History')
        history = page.get_by_role('region', name='History', exact=True)
        expect(history.locator('.rule-item')).to_contain_text(rule['name'])
        effective.fill('2026-09-01')
        expect(history.get_by_role('heading', name='No matching rules', exact=True)).to_be_visible()
        expect(history.get_by_role('heading', name='No history yet', exact=True)).to_have_count(0)
        expect(effective).to_be_visible()
        effective.fill('')
        expect(history.locator('.rule-item')).to_contain_text(rule['name'])
    finally:
        context.close()


@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}], ids=['desktop', 'phone'])
def test_true_zero_rules_keep_quick_start_and_retired_only_rules_keep_lazy_history(browser, live_server, h, viewport):
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Never authored synthetic department')
        db.add(department)
        db.flush()
        department_id = department.id
    for role in ('coordinator', 'approver'):
        identity(h, 'zero-' + role, [role], department=department_id)
    assert not h.get('zero-coordinator', '/api/catalogues')['policies']
    assert not h.get('zero-coordinator', '/api/policies?status=retired&limit=1')['policies']
    context, page = sign_in(browser, live_server, 'zero-coordinator', viewport)
    requests = []
    page.on('request', lambda request: requests.append(request.url) if request.url.endswith('/api/policies?status=retired&effective=true') else None)
    try:
        navigate(page, 'rules')
        expect(page.get_by_role('heading', name='Start with a common rule', exact=True)).to_be_visible()
        expect(page.locator('.rule-toolbar')).to_have_count(0)
        assert not requests

        proposal = h.command('zero-coordinator', 'policy.create', {'name': 'Fictional historical rest',
            'owner': 'Synthetic owner', 'source': 'Synthetic retired-only catalogue', 'starts_on': '2020-01-01',
            'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
        rule = h.command('zero-approver', 'policy.activate', {'reason': 'Independent synthetic review'},
                         proposal['id'], proposal['revision'])['record']
        retire_rule(h, rule, 'Synthetic retired-only catalogue', role='zero-approver', department=department_id)
        # 21e096a0d: retired versions left the operational catalogue; the paged history list holds them.
        assert not h.get('zero-coordinator', '/api/catalogues')['policies']
        history = h.get('zero-coordinator', '/api/policies?status=retired')['policies']
        assert [(p['id'], p['status']) for p in history] == [(rule['id'], 'retired')]
        page.reload()
        navigate(page, 'rules')
        expect(page.get_by_role('heading', name='Start with a common rule', exact=True)).to_have_count(0)
        expect(page.locator('.rule-toolbar button.dt-saved')).to_be_visible()
        expect(page.get_by_role('region', name='Current', exact=True).get_by_role('heading', name='No current rules', exact=True)).to_be_visible()
        rule_status(page, 'Needs attention')
        attention = page.get_by_role('region', name='Needs attention', exact=True)
        expect(attention.get_by_role('heading', name='Nothing needs attention', exact=True)).to_be_visible()
        expect(attention).not_to_contain_text('Every rule is active')
        assert not requests
        rule_status(page, 'History')
        history = page.get_by_role('region', name='History', exact=True)
        expect(history.locator('.rule-item')).to_contain_text(rule['name'])
        assert len(requests) == 1
        rule_status(page, 'Current')
        rule_status(page, 'History')
        expect(history.locator('.rule-item')).to_contain_text(rule['name'])
        assert len(requests) == 1
    finally:
        context.close()
