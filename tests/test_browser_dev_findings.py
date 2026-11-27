"""Frontend findings from configuring DEV (2026-10-14): self-approval reaches every rules view, existing line settings
count when a line is renewed, a proposal always ends in an outcome, no help box floats over an open menu, and the
same-button confirm stays armed long enough to press again."""
import re
import uuid

import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, navigate, open_rota, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag

pytestmark = pytest.mark.browser
VIEWS = [(1440, 'light'), (1440, 'dark')]


def line(numbers, **more):
    return {'id': str(uuid.uuid4()), 'numbers': numbers, **more}


def themed(browser, origin, role, width=1440, scheme='light'):
    context, page = sign_in(browser, origin, role, viewport={'width': width, 'height': 900})
    page.emulate_media(color_scheme=scheme)
    return context, page


def policy(h, ident):
    return next(p for p in h.get('approver', '/api/catalogues')['policies'] if p['id'] == ident)


# ---- 1. Self-approval: the proposer with a permission reviews and activates from every rules view ----

def solo(h, permitted=True):
    """A coordinator who is also the department's only approver; `permitted`: holds a rule.activate permission."""
    name = 'solo' if permitted else 'pair'
    identity(h, name, ['coordinator', 'approver'])
    if permitted:
        h.command('admin', 'self_approval.grant', {'identity_id': h.seed['identities'][name], 'actions': ['rule.activate'],
                  'starts_on': '2026-01-01', 'ends_on': '2027-01-01', 'reason': 'Only approver this quarter'})
    return name


def self_approve(page, why):
    """B5 (f1d80dcc4): Activate… docks the decision panel, named after the change; for your own proposal it asks for your
    reason once, beside the decision (its use of the permission is recorded as self-approved, §3): "Approve and activate"
    waits for it and no second dialog asks again. Returns the panel and any dialog, for the caller to see none is left."""
    review = page.get_by_role('region', name='Add a line for everyone · turns the rule on', exact=True)
    approve = review.get_by_role('button', name='Approve and activate', exact=True)
    expect(review.locator('.rc-lp-foot .why-unavailable')).to_have_text('Give a reason (3 characters or more)')
    expect(approve).to_be_disabled()
    expect(review.get_by_label('Reason', exact=True)).to_have_count(1)
    review.get_by_label('Reason', exact=True).fill(why)
    approve.click()
    return review, page.get_by_role('dialog')


@pytest.mark.parametrize('width,scheme', VIEWS)
def test_proposer_with_permission_reviews_and_activates_own_catalogue_proposal(browser, live_server, h, width, scheme):
    flag(h)
    who = solo(h)
    proposal = h.command(who, 'rule.propose', {'catalogue_id': 'duties_per_week', 'reason': 'Own proposal',
                         'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})]})['record']
    context, page = themed(browser, live_server, who, width, scheme)
    try:
        assert 'rule.activate' in page.evaluate("() => fetch('/api/me').then(r => r.json()).then(m => m.self_approval)")
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        # Their own proposal: Activate… (the normal review) beside Withdraw, never instead of it.
        expect(waiting.get_by_role('button', name='Withdraw', exact=True)).to_be_visible()
        waiting.get_by_role('button', name='Review and approve', exact=True).click()
        review, confirm = self_approve(page, 'Only approver this quarter; checked the lines')
        expect(confirm).to_have_count(0)
        expect(review).to_have_count(0)
        assert policy(h, proposal['id'])['status'] == 'active'
    finally:
        context.close()


def test_proposer_without_permission_is_offered_withdraw_only(browser, live_server, h):
    flag(h)
    who = solo(h, permitted=False)
    h.command(who, 'rule.propose', {'catalogue_id': 'duties_per_week', 'reason': 'Own proposal',
              'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})]})
    context, page = sign_in(browser, live_server, who)
    try:
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        expect(waiting.get_by_role('button', name='Withdraw', exact=True)).to_be_visible()
        expect(waiting.get_by_role('button', name='Review and approve', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('permitted', [True, False], ids=['permitted', 'not-permitted'])
@pytest.mark.parametrize('entry_point', ['source', 'more menu'])
def test_own_proposal_review_respects_permission_at_source_and_more_menu(browser, live_server, h, permitted, entry_point):
    flag(h, on=entry_point == 'source')
    who = solo(h, permitted)
    rule = h.command(who, 'policy.create', {'name': 'Own rest rule', 'owner': 'QA', 'source': 'Browser test',
                     'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    context, page = sign_in(browser, live_server, who)
    try:
        if entry_point == 'source':
            navigate(page, 'rules?source=' + rule['id'])
            page.get_by_role('button', name='Review…', exact=True).click()
        else:
            navigate(page, 'rules')
            page.get_by_role('button', name='More for Own rest rule', exact=True).click()
            page.get_by_role('menuitem', name='Check rule against rotas', exact=True).click()
        review = page.get_by_role('dialog', name='Review Own rest rule', exact=True)
        expect(review).to_be_visible()
        activate = review.get_by_role('button', name='Activate', exact=True)
        if not permitted:
            expect(activate).to_have_count(0)  # a check only: another approver decides
            return
        review.get_by_label('Reason', exact=True).fill('Only approver this quarter; checked it')
        activate.click()
        expect(review).to_have_count(0)
        assert policy(h, rule['id'])['status'] == 'active'
    finally:
        context.close()


@pytest.mark.parametrize('permitted', [True, False], ids=['permitted', 'not-permitted'])
def test_bulk_activation_uses_only_independently_decidable_rules(browser, live_server, h, permitted):
    flag(h, on=False)
    who = solo(h, permitted)
    own = h.command(who, 'policy.create', {'name': 'Own rest rule', 'owner': 'QA', 'source': 'Browser test',
                    'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 11}}})['record']
    other = h.command('coordinator', 'policy.create', {'name': 'Independent daily hours', 'owner': 'QA', 'source': 'Browser test',
                      'starts_on': '2020-01-01', 'definition': {'rule': {'kind': 'max_hours', 'hours': 12, 'window': 'day'}}})['record']
    context, page = sign_in(browser, live_server, who)
    try:
        navigate(page, 'rules')
        bulk = page.get_by_role('toolbar', name='Bulk actions', exact=True)
        page.get_by_role('checkbox', name='Select Own rest rule', exact=True).check()
        if not permitted:
            expect(bulk.get_by_role('button', name=re.compile(r'^Activate '))).to_have_count(0)
        page.get_by_role('checkbox', name='Select Independent daily hours', exact=True).check()
        count = 2 if permitted else 1
        bulk.get_by_role('button', name=f'Activate {count}', exact=True).click()
        review = page.get_by_role('dialog', name=re.compile(r'^Review '))
        activate = review.get_by_role('button', name=f'Activate {count} lines' if permitted else 'Activate', exact=True)
        expect(activate).to_be_visible()
        reason = review.get_by_label('Reason', exact=True)
        if permitted:
            reason.fill('Routine update')
            activate.click()
            expect(page.locator('.toast.error', has_text='You are deciding your own work: add a reason')).to_be_visible()
            expect(review).to_be_visible()
            assert policy(h, own['id'])['status'] == 'proposed'
            assert policy(h, other['id'])['status'] == 'proposed'
        reason.fill('Reviewed both synthetic proposals; only approver this quarter')
        activate.click()
        expect(review).to_have_count(0)
        assert policy(h, other['id'])['status'] == 'active'
        assert policy(h, own['id'])['status'] == ('active' if permitted else 'proposed')
        if permitted:
            events = h.get('auditor', '/api/audit?self_approved=true')['events']
            assert any(e['action'] == 'policy.activate_many' and any(a['action'] == 'rule.activate'
                       for a in e['detail'].get('self_approved', [])) for e in events)
    finally:
        context.close()


# ---- 2. A renewed line keeps the settings it already has ----

@pytest.mark.parametrize('entry', ['not_after', 'companion_cover'])
def test_changing_an_existing_line_keeps_its_own_required_settings(browser, live_server, h, entry):
    flag(h)
    types = [t['id'] for t in h.get('admin', '/api/catalogues')['assignment_types']]
    if entry == 'not_after':
        settings = {'forbidden_transition': {'from_type_ids': types[:1], 'to_type_ids': types[-1:]}}
        numbers = {'apply': {'level': 'mandatory'}}
    else:
        settings = {'minimum_cover': {'when_person_ids': [h.seed['people'][0]]}}
        numbers = {'min': {'value': 1, 'level': 'mandatory'}}
    # The settings live on the line (as converted rules hold them), none at the rule level.
    saved = line(numbers, settings=settings, ends_on='2026-12-31')
    catalogue_rule(h, entry, [saved])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'rules/{entry}/{saved["id"]}')
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Note', exact=True).fill('Renewed for next year')
        panel.get_by_role('button', name='Keep change').click()
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose 1 change to '))
        expect(dialog).to_be_visible()
        expect(page.locator('.toast', has_text='in Settings first')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('Renewal')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(page.locator('#main .rc-waiting')).to_be_visible()
    finally:
        context.close()
    proposed = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == entry)['proposed']
    assert proposed['definition']['rule']['lines'][0]['settings'] == settings


# ---- 3. Propose always ends in an outcome ----

@pytest.mark.parametrize('where', ['rule page', 'rules list'])
def test_a_check_that_cannot_run_says_so_with_retry(browser, live_server, h, where):
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5, 'level': 'mandatory'}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        refused = {'on': True}

        def check(route):
            if refused['on']:
                route.fulfill(status=429, json={'error': {'code': 'RATE_LIMITED', 'message': 'Too many requests; wait a minute and try again'}})
            else:
                route.continue_()
        # Save starts the same check that Propose shares: intercept before either can send it.
        page.route(re.compile(r'/api/rules/catalogue/[^/]+/check$'), check)
        main.locator('tbody tr[data-row]').first.locator('td').filter(has_text='5').first.click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('4')
        page.get_by_role('dialog', name='Edit Max').get_by_role('button', name='Save').click()
        if where == 'rules list':
            # The draft is kept and proposed from the list: "← Rules" leaves without the unsaved-changes guard (OD-10).
            main.get_by_role('button', name='← Rules').click()
            expect(page).to_have_url(re.compile('#rules$'))
            expect(main.locator('.rc-crumb')).to_have_count(0)
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        toast = page.locator('.toast.error', has_text='Nothing was proposed: the changes could not be checked.')
        expect(toast).to_contain_text('Too many requests')
        refused['on'] = False
        toast.get_by_role('button', name='Retry', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile(r'^Propose 1 change'))
        expect(dialog).to_be_visible()
        dialog.get_by_label('Reason', exact=True).fill('Four is enough')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(dialog).to_have_count(0)
        page.unroute(re.compile(r'/api/rules/catalogue/[^/]+/check$'))
    finally:
        context.close()
    assert next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']


# ---- 4. No help box over an open menu ----

@pytest.mark.parametrize('width,scheme', VIEWS)
def test_help_never_floats_over_the_open_rota_tools_menu(browser, live_server, h, width, scheme):
    w = h.shift(h.workspace('Menu rota'))['workspace']
    context, page = themed(browser, live_server, 'coordinator', width, scheme)
    try:
        open_rota(page, w['id'])
        page.get_by_role('button', name='Rota tools', exact=True).click()
        menu = page.get_by_role('menu', name='Rota tools')
        expect(menu.get_by_role('menuitem', name='Delete draft…', exact=True)).to_be_visible()
        # Controls drawn under the menu (toolbar, day headers) still get pointer events as it opens: their help must
        # not open above the menu as a detached box.
        hidden = page.evaluate("""() => {const m=document.querySelector('[role=menu][aria-label="Rota tools"]').getBoundingClientRect();
          const under=[...document.querySelectorAll('#main [data-tip], #main [data-rk]')].filter(n=>{const r=n.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
            return r.width&&x>m.left&&x<m.right&&y>m.top&&y<m.bottom;});
          for(const n of under)n.dispatchEvent(new PointerEvent('pointerover',{bubbles:true}));return under.length;}""")
        assert hidden > 0
        page.clock.run_for(800)
        expect(page.locator('[role=tooltip]')).to_have_count(0)
        expect(menu).to_be_visible()
    finally:
        context.close()


def test_help_whose_words_went_away_shows_no_empty_box(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.evaluate("""async () => {const {el}=await window.importBundle('dom.js');
          const b=el('button',{id:'fading-help',type:'button','data-tip':'Soon gone'},'Help');document.querySelector('#main').prepend(b);
          b.dispatchEvent(new PointerEvent('pointerover',{bubbles:true}));b.dataset.tip='';}""")
        page.clock.run_for(800)
        expect(page.locator('[role=tooltip]')).to_have_count(0)
    finally:
        context.close()


def test_self_approval_is_announced_on_the_rule_page(browser, live_server, h):
    """RU-23 / RU-11: the proposer who may self-approve is told the activation is recorded as self-approved (never
    "Another person approves it"); History marks a self-approved version."""
    flag(h)
    who = solo(h)
    h.command(who, 'rule.propose', {'catalogue_id': 'duties_per_week', 'reason': 'Own proposal',
              'lines': [line({'max': {'value': 5, 'level': 'mandatory'}})]})
    context, page = themed(browser, live_server, who)
    try:
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        expect(waiting.locator('.rc-prop-who')).to_contain_text('recorded as self-approved')
        expect(waiting.locator('.rc-prop-who')).not_to_contain_text('Another person approves it')
        waiting.get_by_role('button', name='Review and approve', exact=True).click()
        review, confirm = self_approve(page, 'Only approver this quarter')
        expect(confirm).to_have_count(0)
        expect(review).to_have_count(0)
        expect(page.locator('.rc-waiting')).to_have_count(0)
        from .test_browser_rule_catalogue import rule_action
        rule_action(page, 'History')
        expect(page.get_by_role('dialog', name='History · Duties per week').get_by_text('Self-approved', exact=True)).to_be_visible()
    finally:
        context.close()
