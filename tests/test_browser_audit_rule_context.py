"""Other department rules stay discoverable without a permanent catalogue panel."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import EXPLICIT_ROLES, ROLE_NAMES
from .governance_support import identity
from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, rules_action
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('role,width,scheme', [
    ('admin', 1440, 'light'),
    ('coordinator', 390, 'dark'),
    ('approver', 1440, 'dark'),
    ('auditor', 390, 'light'),
    ('credentialer', 1440, 'light'),
    ('programme_lead', 390, 'dark'),
])
def test_additional_rules_are_counted_context_with_complete_readable_effects(browser, live_server, h, role, width, scheme):
    if role == 'programme_lead':
        identity(h, role, [role])
    effective_roles = h.get(role, '/api/me')['roles']
    assert effective_roles == (sorted(set(ROLE_NAMES) - EXPLICIT_ROLES) if role == 'admin' else [role])
    flag(h)
    current = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Clinical rest for the department'))
    future = activate(h, propose(h, {'kind': 'min_rest', 'hours': 12}, name='Next period clinical rest', starts_on='2030-01-01', ends_on='2030-02-01'))
    policies = h.get('admin', '/api/catalogues')['policies']
    kept = [p for p in policies if p['status'] == 'active' and p['kind'] != 'catalogue' and not p.get('definition', {}).get('workspace_id')]
    before = {p['id']: p for p in kept}
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        if role == 'credentialer':  # no rules read permission: the Rules page is restricted and the rule lookup refused
            page.evaluate("location.hash='rules'")
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'restricted')
            assert h.client(role).get('/api/policies/applies', params={'person_id': h.seed['people'][0], 'day': '2026-10-05'}).status_code == 403
            return
        navigate(page, 'rules')
        expect(page.locator('#main .rc-kept')).to_have_count(0)
        page.get_by_role('button', name=re.compile('^More rule actions')).click()
        lookup = page.get_by_role('menuitem', name='Which rule applies?', exact=True)
        expect(lookup).to_be_visible()
        lookup.click()
        dialog = page.get_by_role('dialog', name='Which rule applies?', exact=True)
        with page.expect_response(lambda response: '/api/policies/applies?' in response.url) as applies:
            dialog.get_by_label('Person', exact=True).select_option(h.seed['people'][0])
        assert applies.value.status == 200
        expect(dialog).to_contain_text(current['name'])
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(page.get_by_role('heading', name='Not in the catalogue yet', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
        opener = page.get_by_role('button', name=re.compile('^More rule actions'))
        expect(opener).to_be_visible()
        expect(opener).not_to_have_attribute('aria-haspopup', 'dialog')
        opener.focus()
        expect(opener).to_be_focused()
        opener.press('Enter')
        page.get_by_role('menuitem', name=f'Other department rules ({len(kept)})', exact=True).click()
        panel = page.get_by_role('region', name='Other department rules', exact=True)
        expect(panel).to_be_visible()
        search = panel.get_by_role('searchbox', name='Search other department rules', exact=True)
        search.fill(future['name'])
        expect(panel.locator(f'[data-row="{future["id"]}"]')).to_be_visible()
        expect(panel).to_contain_text('At least 12 hours rest between duties.')
        expect(panel).to_contain_text('1 Jan 2030 – 31 Jan 2030')
        expect(panel).not_to_contain_text('1 Feb 2030')
        search.fill(current['name'])
        expect(panel.locator(f'[data-row="{current["id"]}"]')).to_be_visible()
        expect(panel).to_contain_text('At least 11 hours rest between duties.')
        # Moving these rules is the department's Simplify rules proposal (rule_simplify.ts), not a per-rule move.
        expect(panel.get_by_role('button', name='Move rules into the catalogue…', exact=True)).to_have_count(0)
        panel.get_by_role('button', name=current['name'], exact=True).click()
        detail = page.locator('.dock-panel.rule-sheet')
        expect(detail).to_be_visible()
        expect(detail).to_contain_text('11')
        expect(detail.get_by_role('tab', name='Source', exact=True)).to_be_visible()
        expect(detail.get_by_role('tab', name='History', exact=True)).to_be_visible()
        impact = detail.get_by_role('button', name='Preview impact', exact=True)
        if role in ('admin', 'coordinator', 'approver'):
            expect(impact).to_be_visible()
            with page.expect_response(lambda response: response.url.endswith('/api/policies/' + current['id'] + '/impact')) as checked:
                impact.click()
            assert checked.value.status == 200
            preview = page.get_by_role('dialog', name='What this rule changes', exact=True)
            expect(preview).to_be_visible()
            page.keyboard.press('Escape')
            expect(preview).to_have_count(0)
        else:
            expect(impact).to_have_count(0)
            assert h.client(role).get('/api/policies/' + current['id'] + '/impact').status_code == 403
        if {'admin', 'coordinator'} & set(effective_roles):
            expect(detail.get_by_role('button', name='Change…', exact=True)).to_be_visible()
        else:
            expect(detail.get_by_role('button', name='Change…', exact=True)).to_have_count(0)
        if 'approver' in effective_roles:
            expect(detail.get_by_role('button', name='Retire rule', exact=True)).to_be_visible()
        else:
            expect(detail.get_by_role('button', name='Retire rule', exact=True)).to_have_count(0)
            # Someone who proposes rules but cannot retire them is told who does.
            hint = detail.get_by_text('An approver retires rules.', exact=True)
            if {'admin', 'coordinator'} & set(effective_roles):
                expect(hint).to_be_visible()
            else:
                expect(hint).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        detail.get_by_role('button', name='Back to Other department rules', exact=True).click()
        expect(panel).to_be_visible()
        panel.get_by_role('searchbox', name='Search other department rules', exact=True).fill(current['name'])
        panel.get_by_role('button', name=current['name'], exact=True).click()
        expect(detail).to_be_visible()
        detail.get_by_role('button', name='Close panel', exact=True).press('Escape')
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(opener).to_be_focused()
    finally:
        context.close()
    after = {p['id']: p for p in h.get('admin', '/api/catalogues')['policies'] if p['id'] in before}
    assert after == before  # discovery never converts, changes or retires a rule


def test_no_additional_rule_context_is_offered_when_none_remain(browser, live_server, h):
    flag(h)
    assert not [p for p in h.get('admin', '/api/catalogues')['policies'] if p['status'] == 'active' and p['kind'] != 'catalogue' and not p.get('definition', {}).get('workspace_id')]
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'rules')
        expect(page.locator('#main .rc-kept')).to_have_count(0)
        page.get_by_role('button', name=re.compile('^More rule actions')).click()
        expect(page.get_by_role('menuitem', name=re.compile(r'^Other department rules'))).to_have_count(0)
        page.keyboard.press('Escape')
        expect(page.get_by_role('button', name='Move rules into the catalogue…', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [
    ('admin', 390, 'light'), ('auditor', 390, 'dark'),
    ('admin', 1440, 'dark'), ('auditor', 1440, 'light'),
])
def test_completed_rule_history_keeps_dates_whole_and_native_scroll_reachable(browser, live_server, h, role, width, scheme):
    flag(h)
    current = None
    for version in range(13):
        current = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11 + version},
            name=f'Specialist continuity and urgent care rest requirement version {version + 1}',
            starts_on='2026-08-23', ends_on='2027-01-03',
            **({'replaces_id': current['id']} if current else {})))
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 844})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        rules_action(page, 'Other department rules (1)')
        panel = page.get_by_role('region', name='Other department rules', exact=True)
        panel.get_by_role('button', name=current['name'], exact=True).click()
        detail = page.locator('.dock-panel.rule-sheet')
        detail.get_by_role('tab', name='History', exact=True).click()
        history = detail.locator('.rule-history-body')
        expect(history.locator('.loading')).to_have_count(0)
        expect(history.locator('tbody tr')).to_have_count(12)
        dates = history.locator('.rule-valid')
        expect(dates.first).to_have_text('23 Aug 2026 – 2 Jan 2027')
        # Each complete date is atomic; only the range boundary may wrap.
        assert dates.locator(':scope > span').evaluate_all('''nodes=>nodes.length===24&&nodes.every(n=>{
            const r=document.createRange();r.selectNodeContents(n);return r.getClientRects().length===1;
        })''')
        scroll = history.locator('.table-scroll')
        size = scroll.evaluate('e=>({height:e.clientHeight,total:e.scrollHeight,width:e.clientWidth,wide:e.scrollWidth})')
        assert size['total'] > size['height'] and size['height'] <= min(844 * 0.7, 760) + 1, size
        scroll.evaluate('e=>e.scrollTop=(e.scrollHeight-e.clientHeight)/2')
        scroll.scroll_into_view_if_needed()
        scroll.focus()
        scroll.press('Shift+Tab')
        page.keyboard.press('Tab')
        expect(scroll).to_be_focused()
        assert scroll.evaluate('e=>e.scrollTop') > 0
        assert scroll.evaluate('''e=>{const r=e.getBoundingClientRect();return r.bottom<=innerHeight&&
            e.contains(document.elementFromPoint(r.left+r.width/2,r.bottom-3));}''')
        if width == 390:
            assert size['wide'] > size['width'], size
            page.keyboard.press('ArrowRight')
            page.wait_for_function('e=>e.scrollLeft>0', arg=scroll.element_handle())
        impact = detail.get_by_role('button', name='Preview impact', exact=True)
        if role == 'admin':
            expect(impact).to_be_in_viewport()
        else:
            expect(impact).to_have_count(0)
        detail.get_by_role('tab', name='Source', exact=True).click()
        expect(detail).to_contain_text('Test policy owner')
        detail.get_by_role('tab', name='Details', exact=True).click()
        effective = detail.locator('.rule-facts').get_by_text('Effective', exact=True)
        effective.scroll_into_view_if_needed()
        expect(effective).to_be_in_viewport()
        impact = detail.get_by_role('button', name='Preview impact', exact=True)
        if role == 'admin':
            expect(impact).to_be_in_viewport()
        else:
            expect(impact).to_have_count(0)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_catalogue_exception_count_independent_approval_reload_and_person_source(browser, live_server, h, width, scheme):
    person_id = h.seed['people'][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed['identities']['admin']).person_id = person_id
    flag(h)
    person_name = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == person_id)['display_name']
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    payload = {'policy_id': policy['id'], 'person_id': person_id, 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    earlier = h.command('coordinator', 'exception.create', {**payload, 'reason': 'Previously reviewed bounded exception'})['record']
    h.command('approver', 'exception.approve', {'reason': 'Independent earlier review'}, earlier['id'], earlier['revision'])
    proposed = h.command('admin', 'exception.create', {**payload, 'reason': 'Pending bounded exception for catalogue journey'})['record']
    before_policy = next(p for p in h.get('admin', '/api/catalogues')['policies'] if p['id'] == policy['id'])
    # Admin has the approver role and is the subject of this exception.
    assert 'exception.approve' not in h.get('admin', '/api/me')['self_approval']
    refusal = h.command('admin', 'exception.approve', {'reason': 'Own request must remain pending'}, proposed['id'], proposed['revision'], status=403)
    assert refusal['error']['code'] == 'SELF_APPROVAL'
    author_context, author = sign_in(browser, live_server, 'admin', viewport={'width': width, 'height': 900})
    review_context, review = sign_in(browser, live_server, 'approver', viewport={'width': width, 'height': 900})
    try:
        for page in (author, review):
            page.emulate_media(color_scheme=scheme)
            navigate(page, 'rules')
            opener = page.get_by_role('button', name=re.compile('^More rule actions, .* waiting$'))
            opener.focus()
            opener.press('Enter')
            page.get_by_role('menuitem', name='Exceptions (1 pending)', exact=True).click()
            panel = page.get_by_role('region', name='Exceptions', exact=True)
            expect(panel).to_be_visible()
            if page == author:
                expect(panel.get_by_role('button', name='Request exception', exact=True)).to_be_visible()
            else:
                expect(panel.get_by_role('button', name='Request exception', exact=True)).to_have_count(0)
            panel.get_by_role('searchbox', name='Search exceptions', exact=True).fill(proposed['reason'])
            record = panel.locator(f'[data-row="{proposed["id"]}"]')
            expect(record).to_be_visible()
            expect(record).to_contain_text('4 Oct 2026 – 10 Oct 2026')
            approve = record.get_by_role('button', name='Approve exception', exact=True)
            if page == author:
                expect(approve).to_have_count(0)
                record.get_by_role('button', name=person_name, exact=True).click()
                expect(page).to_have_url(re.compile(r'#people\?open=' + person_id + '$'))
                expect(page.get_by_role('region', name=person_name, exact=True)).to_be_visible()
                navigate(page, 'rules')
                page.reload()
                page.get_by_role('button', name=re.compile('^More rule actions')).click()
                expect(page.get_by_role('menuitem', name='Exceptions (1 pending)', exact=True)).to_be_visible()
                page.keyboard.press('Escape')
            else:
                expect(approve).to_be_visible()
                with page.expect_response(lambda response: response.url.endswith('/api/commands')
                                          and response.request.post_data_json.get('kind') == 'exception.approve') as decision:
                    approve.click()
                    dialog = page.get_by_role('dialog', name='Approve bounded exception', exact=True)
                    # RU-15: the approver decides on the facts: the person, the dates and the reason given.
                    expect(dialog).to_contain_text(person_name)
                    expect(dialog).to_contain_text('4 Oct 2026 – 10 Oct 2026')
                    expect(dialog).to_contain_text(proposed['reason'])
                    dialog.get_by_label('Reason', exact=True).fill('Independent review of the bounded person exception')
                    dialog.get_by_role('button', name='Confirm', exact=True).click()
                assert decision.value.status == 200
                assert decision.value.request.post_data_json['target_id'] == proposed['id']
                assert decision.value.request.post_data_json['expected_revision'] == proposed['revision']
                expect(panel).to_have_count(0)
                page.get_by_role('button', name=re.compile('^More rule actions')).click()
                expect(page.get_by_role('menuitem', name='Exceptions', exact=True)).to_be_visible()
                page.keyboard.press('Escape')
                page.reload()
                rules_action(page, 'Exceptions')
                panel = page.get_by_role('region', name='Exceptions', exact=True)
                panel.get_by_role('searchbox', name='Search exceptions', exact=True).fill(proposed['reason'])
                record = panel.locator(f'[data-row="{proposed["id"]}"]')
                if record.evaluate("n => n.matches('.dt-card')"):  # phone cards: the status is the group heading above it
                    expect(record.locator('xpath=../preceding-sibling::div[contains(@class,"dt-card-group")][1]')).to_contain_text('Approved')
                else:
                    expect(record).to_contain_text('Approved')
                expect(record.get_by_role('button', name='Approve exception', exact=True)).to_have_count(0)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        author_context.close()
        review_context.close()
    saved = next(r for r in h.get('admin', '/api/catalogues')['exceptions'] if r['id'] == proposed['id'])
    assert saved['status'] == 'approved' and saved['approved_by'] == h.seed['identities']['approver']
    assert saved['revision'] == proposed['revision'] + 1 and saved['policy_revision'] == proposed['policy_revision']
    assert next(p for p in h.get('admin', '/api/catalogues')['policies'] if p['id'] == policy['id']) == before_policy
