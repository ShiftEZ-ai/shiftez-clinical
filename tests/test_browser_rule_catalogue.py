"""The Rules page as the rule catalogue (docs/design/rule-catalogue.md §6), behind the admin display setting
`rule_catalogue`: one database (families › rules › numbered lines, mockup R9), the column menu (R9b), the rule page,
read-only roles and the 1,000-line budget. Selection, Numbers… and Needs attention: test_browser_rules_database.py."""
import os
import re
import uuid
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction

from .governance_support import identity
from .test_browser import browser, condition, live_server, navigate, open_config, sign_in, route_fetch, wait_held, expect_loaded, COUNT_WORK  # noqa: F401
from .rules_support import rules_by_family
from .test_rule_catalogue_conflicts import tag_people

pytestmark = pytest.mark.browser


def rules_action(page, name):
    """Open a secondary action on the Rules collection's shared menu."""
    page.get_by_role('button', name=re.compile('^More rule actions')).click()
    page.get_by_role('menuitem', name=name, exact=isinstance(name, str)).click()


def rule_action(page, name):
    """A rule's secondary actions use the shared menu in the top bar."""
    page.locator('.topbar').get_by_role('button', name='More actions for this rule', exact=True).click()
    page.get_by_role('menuitem', name=name, exact=True).click()


def flag(h, on=True, legacy=True):
    """Turn the catalogue on. `legacy`: the department stays on catalogue v1 (as an unconverted department does), marked
    by one approved v1 rule that is off and has no lines; a department with no rules at all authors v2."""
    dept = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.display', {'rule_catalogue': on, 'reason': 'Rule catalogue trial'}, dept['id'], dept['revision'])
    if on and legacy:
        record = h.command('coordinator', 'rule.propose', {'catalogue_id': 'weekends_in_a_row', 'on': False, 'lines': [],
                                                           'reason': 'Catalogue v1 department fixture'})['record']
        h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])


def catalogue_rule(h, entry, lines, settings=None):
    """A catalogue rule in force: proposed by the coordinator, activated by the approver."""
    body = {'catalogue_id': entry, 'lines': lines, 'reason': 'Catalogue fixture'}
    if entry == 'fair_share':
        current = next(e['rule'] for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == entry)
        if current:
            body.update(expected_policy_id=current['id'], expected_revision=current['revision'])
    if settings is not None:
        body['settings'] = settings
    check = h.client('coordinator').post(f'/api/rules/catalogue/{entry}/check', json={'lines': lines, 'settings': settings}).json()
    body['acknowledged_overlaps'] = [o['line_id'] for o in check['overlaps']]
    record = h.command('coordinator', 'rule.propose', body)['record']
    return h.command('approver', 'policy.activate', {'reason': 'Independent review'}, record['id'], record['revision'])['record']


def shot(page, name, *, full_page=True):
    """RC_SHOTS=<folder>: keep a screenshot for review (the PR's evidence); otherwise nothing."""
    if os.getenv('RC_SHOTS'):
        page.screenshot(path=os.path.join(os.environ['RC_SHOTS'], name + '.png'), full_page=full_page, animations='disabled')


def line(numbers, **more):
    return {'id': str(uuid.uuid4()), 'numbers': numbers, **more}


def selection(page):
    """The one selection bar (rules, lines or issues selected)."""
    return page.locator('.selection-bar:not([hidden])')


def bar_action(page, name):
    """Run a selection-bar action: on the bar, or in ⋯ More where a narrow bar folds its secondary actions."""
    bar = selection(page)
    button = bar.get_by_role('button', name=name, exact=True)
    if button.count():
        button.click()
    else:
        bar.get_by_role('button', name='More actions').click()
        page.get_by_role('menu', name='More actions').get_by_role('menuitem', name=name, exact=True).click()


def duplicate(page):
    """⋯ More › Duplicate… for the selected lines, keeping their dates."""
    selection(page).get_by_role('button', name='More actions').click()
    page.get_by_role('menuitem', name='Duplicate…').click()
    page.get_by_role('dialog', name='Duplicate lines').get_by_role('button', name='Duplicate', exact=True).click()


def set_numbers(page, level=None, **values):
    """Numbers… for the selected lines: the values given (blank keeps each line's own) and Mandatory / Advisory."""
    selection(page).get_by_role('button', name='Numbers…').click()
    pop = page.get_by_role('dialog', name='Numbers')
    for key, value in values.items():
        pop.get_by_label(key.capitalize(), exact=True).fill(str(value))
    if level:
        pop.get_by_role('radio', name=level, exact=True).click()  # the shared segmented() is a radio group (bc5e698e3)
    pop.get_by_role('button', name='Apply', exact=True).click()
    expect(pop).to_have_count(0)


def weekly(h, *, extra_lines=()):
    a = h.seed['people'][0]
    return catalogue_rule(h, 'duties_per_week', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, note='Department default'),
        line({'max': {'value': 3, 'level': 'mandatory'}}, who={'conditions': [{'field': 'person_ids', 'values': [a]}]}, starts_on='2026-10-01',
             ends_on='2026-11-15', note='Return from leave'), *extra_lines])


def weekly_group_conflict(h):
    """Two equal-tier lines share a person at one clinic, separate from the ordered overrides."""
    group = tag_people(h, 'Review group', h.seed['people'][:1])
    where = {'conditions': [{'field': 'service_ids', 'values': h.seed['services'][:1]}]}
    return [line({'max': {'value': value, 'level': 'mandatory'}}, where=where,
                 who={'conditions': [{'field': field, 'values': [ident]}]})
            for value, field, ident in ((5, 'category_ids', h.seed['category']), (6, 'person_ids', group))]


def test_the_admin_setting_switches_the_rules_page(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'rules')
        expect(page.locator('#main .rule-item, #main .empty-state').first).to_be_visible()  # the current page while off
        open_config(page, 'Department settings')
        page.get_by_label('Show the Rules page as the rule catalogue').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Trial the catalogue')
        dialog.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_text('The Rules page now shows the rule catalogue.')).to_be_visible()
        navigate(page, 'rules')
        expect(page.locator('.topbar h1')).to_have_text('Rules')
        # The seeded Fair share rule is visible; unused rule types wait under Filter and + Add rule.
        expect(page.locator('#main li.rt-item')).to_have_count(1)
        expect(page.locator('#main li.rt-item')).to_contain_text('Fair share')
        expect(page.get_by_role('button', name='Add rule', exact=True).first).to_be_visible()
    finally:
        context.close()
    assert h.get('staff', '/api/me')['department']['display_settings']['rule_catalogue'] is True


def test_a_catalogue_proposal_is_withdrawn_from_its_toast(browser, live_server, h):
    """U-13: a proposal's message carries Undo (as a legacy rule's does): it withdraws the proposal."""
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('switch', name='Rest between duties on or off').click()
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        row = page.locator('#main li.rt-item').filter(has_text='Rest between duties')
        expect(row).to_contain_text('Waiting for approval')
        page.locator('#toast-tray .toast', has_text='Proposed 1 rule').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='Proposal withdrawn.')).to_be_visible()
        expect(page.locator('#main li.rt-item').filter(has_text='Waiting for approval')).to_have_count(0)
        # the change that was proposed comes back as a draft, ready to adjust and propose again.
        expect(row).to_contain_text('2 changes')
        expect(page.locator('.topbar').get_by_role('button', name='Propose 2 changes')).to_be_visible()
    finally:
        context.close()
    assert next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'rest_between_duties')['proposed'] is None


def test_on_a_rule_page_undo_brings_the_draft_back_and_withdraw_records_reason(browser, live_server, h):
    """Undo of a proposal restores its draft; an explicit Withdraw records a reason."""
    flag(h)
    proposed = lambda: next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'rest_between_duties')['proposed']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        status = page.locator('.app-footer')

        def propose():
            # Switched on in the list (which adds its first line), then proposed from the rule's own page.
            navigate(page, 'rules')
            page.get_by_role('switch', name='Rest between duties on or off').click()
            page.locator('#main li.rt-item').filter(has_text='Rest between duties').get_by_role('button', name='Rest between duties', exact=True).click()
            expect(page).to_have_url(re.compile('#rules/rest_between_duties$'))
            expect(status).to_contain_text('2 changes not yet proposed')
            page.locator('.topbar').get_by_role('button', name='Propose 2 changes', exact=True).click()
            dialog = page.get_by_role('dialog')
            dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
            dialog.get_by_role('button', name='Propose', exact=True).click()
            expect(dialog).to_have_count(0)
            expect(page.locator('#toast-tray .toast', has_text='Proposed;')).to_be_visible()
        propose()
        assert proposed() is not None
        page.locator('#toast-tray .toast', has_text='Proposed;').get_by_role('button', name='Undo', exact=True).click()
        expect(page.locator('#toast-tray .toast', has_text='Proposal withdrawn.')).to_be_visible()
        expect(status).to_contain_text('2 changes not yet proposed')
        assert proposed() is None
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        expect(dialog).to_have_count(0)
        withdraw = page.locator('#main').get_by_role('button', name='Withdraw', exact=True)
        proposal = proposed()
        assert proposal is not None
        withdraw.click()  # asks for the reason first (ce85b38e9)
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Withdraw the corrected proposal')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and r.request.method == 'POST'
                                  and (r.request.post_data_json or {}).get('kind') == 'policy.decline'
                                  and (r.request.post_data_json or {}).get('target_id') == proposal['id']
                                  and (r.request.post_data_json or {}).get('expected_revision') == proposal['revision']
                                  and (r.request.post_data_json or {}).get('payload', {}).get('reason') == 'Withdraw the corrected proposal') as withdrawn:
            dialog.get_by_role('button', name='Withdraw', exact=True).click()
        assert withdrawn.value.status == 200, withdrawn.value.text()
        expect(page.locator('#main').get_by_role('button', name='Withdraw', exact=True)).to_have_count(0)
        assert proposed() is None
    finally:
        context.close()


def test_switching_on_an_empty_rule_lets_its_family_select_the_new_line(browser, live_server, h):
    """a rule's first line, created by switching it on, is picked by its family's box (the box kept the
    family's old, empty line list after the row was patched in place)."""
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        rules_by_family(h, page)  # Group: Family (Question, A–F, is the default)
        navigate(page, 'rules')
        family = page.get_by_role('checkbox', name='Select Gaps between duties')
        page.get_by_role('switch', name='Rest between duties on or off').click()
        expect(page.locator('#main li.rt-item').filter(has_text='Rest between duties')).to_contain_text('2 changes')
        expect(family).to_be_enabled()
        family.check()
        expect(page.locator('#main').get_by_text(re.compile(r'^1 line selected|1 selected'))).not_to_have_count(0)
    finally:
        context.close()


def test_a_rule_is_turned_on_by_proposal_and_approval(browser, live_server, h):
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('switch', name='Rest between duties on or off').click()  # a draft change: nothing sent yet
        expect(page.locator('#main li.rt-item').filter(has_text='Rest between duties')).to_contain_text('2 changes')
        page.locator('.topbar').get_by_role('button', name='Propose 2 changes').click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Rest after every duty')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        row = page.locator('#main li.rt-item').filter(has_text='Rest between duties')
        expect(row).to_contain_text('Waiting for approval')
        # A rule whose settings only the department can choose (duty types here) opens its page to add a line instead.
        expect(page.get_by_role('switch', name='Not after on or off')).to_be_disabled()
        page.locator('#main li.rt-item').filter(has_text='Not after').get_by_role('button', name='Not after', exact=True).click()
        expect(page).to_have_url(re.compile('#rules/not_after$'))
    finally:
        context.close()
    waiting = next(e for e in h.get('approver', '/api/rules/catalogue')['entries'] if e['id'] == 'rest_between_duties')['proposed']
    h.command('approver', 'policy.activate', {'reason': 'Agreed'}, waiting['id'], waiting['revision'])
    entry = next(e for e in h.get('auditor', '/api/rules/catalogue')['entries'] if e['id'] == 'rest_between_duties')
    assert entry['rule']['definition']['rule']['lines'][0]['numbers']['min']['value'] == 11


@pytest.mark.parametrize('scheme,detail', [('light', False), ('dark', True)])
def test_fair_share_open_form_keeps_its_baseline_when_new_lead_defaults_are_added(browser, live_server, h, scheme, detail):
    """`detail`: the draft moves from Rules to the rule's page after the seed; the refreshed entry must not give the old
    draft a fresh baseline (same policy id and revision), or the proposal would silently drop the new default."""
    flag(h, legacy=False)
    [opened] = h.get('admin', '/api/policies?status=active')['policies']
    loaded_ids = [line['id'] for line in opened['definition']['rule']['lines']]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules')
        page.get_by_role('switch', name='Fair share on or off', exact=True).click()
        expect(page.locator('#main li.rt-item').filter(has_text='Fair share')).to_contain_text('1 change')
        from .legacy_lead import historical_type
        historical_type(h, {'name': 'New led duty while Fair share is open', 'starts_on': '2020-01-01',
            'definition': {'lead': {'minimum': 1, 'maximum': 1}}})
        if detail:
            page.locator('#main li.rt-item').filter(has_text='Fair share').get_by_role('button', name='Fair share', exact=True).click()
            expect(page).to_have_url(re.compile('#rules/fair_share$'))
            expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ changes?$')).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Save the original open form')
        with page.expect_response(lambda r: r.url.endswith('/api/commands') and
                                  r.request.post_data_json.get('kind') == 'rule.propose') as response:
            dialog.get_by_role('button', name='Propose', exact=True).click()
        assert response.value.status == 409
        assert response.value.request.post_data_json['payload']['expected_line_ids'] == loaded_ids
        assert response.value.json()['error']['code'] == 'STALE_REVISION'
        expect(dialog.locator('.notice.error')).to_contain_text('reload and try again')
        [current] = h.get('admin', '/api/policies?status=active')['policies']
        assert current['id'] == opened['id'] and current['revision'] == opened['revision']
        assert len(current['definition']['rule']['lines']) == len(loaded_ids) + 1
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [
    (role, width, scheme) for role in ('auditor', 'approver', 'credentialer', 'programme_lead')
    for width, scheme in ((1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark'))
] + [('staff', 1440, 'light')])
def test_read_only_roles_read_the_catalogue_and_staff_do_not(browser, live_server, h, role, width, scheme):
    if role == 'programme_lead':
        identity(h, role, [role])
    assert h.get(role, '/api/me')['roles'] == [role]
    long_name = 'Alexandra Fictional Department A Training Registrar Morgan'
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed['people'][0]).display_name = long_name
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, role, viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        page.evaluate("location.hash='rules'")
        if role == 'staff':
            expect(page.get_by_text('Restricted page')).to_be_visible()
            return
        if role == 'credentialer':
            # G3/Design ruling, 11–12 Nov 2026: credentialers read their own catalogue kinds, never rule kinds.
            catalogues = h.get('credentialer', '/api/catalogues')
            assert catalogues['eligibilities'] is not None and catalogues['grants'] is not None
            assert catalogues['policies'] == [] and catalogues['exceptions'] == []
            expect(page.get_by_text('Restricted page')).to_be_visible()
            return
        # Approvers decide proposals; only coordinators and admins propose: the rule says On, as a fact, not a dead switch (U-19).
        expect(page.locator('li.rt-item').filter(has_text='Duties per week').locator('.rt-state')).to_have_text('On')
        expect(page.get_by_role('switch', name='Duties per week on or off')).to_have_count(0)
        expect(page.get_by_role('button', name=re.compile(r'^Propose'))).to_have_count(0)
        expand = page.get_by_role('button', name='Duties per week lines', exact=True)
        expand.focus()
        expect(expand).to_be_focused()
        expand.press('Enter')
        expect(page.locator('li.rt-ln')).to_have_count(2)
        expect(page.get_by_role('button', name='+ New line in Duties per week', exact=True)).to_have_count(0)
        opener = page.get_by_role('button', name='Open line 2 of Duties per week', exact=True)
        opener.focus()
        expect(opener).to_be_focused()
        opener.press('Enter')
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        expect(page).to_have_url(re.compile(r'#rules/duties_per_week/[0-9a-f-]{36}$'))
        detail_url = page.url
        expect(panel.get_by_role('button', name='Keep change', exact=True)).to_have_count(0)
        expect(panel.get_by_role('button', name='Remove line', exact=True)).to_have_count(0)
        expect(panel.locator('input:not([readonly]):not([disabled]):not([type=hidden]), textarea:not([readonly]):not([disabled]), select:not([disabled])')).to_have_count(0)
        expect(panel.get_by_role('button', name='Choose dates', exact=True)).to_be_hidden()
        expect(panel).to_contain_text(long_name)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        close = panel.get_by_role('button', name='Close panel', exact=True)
        close.focus()
        expect(close).to_be_focused()
        close.press('Escape')
        expect(panel).to_have_count(0)
        expect(page).to_have_url(re.compile(r'#rules/duties_per_week$'))
        expect(opener).to_have_count(0)  # the list's ↗ action navigated to this rule-detail page
        # Focus returns to the open line: its card on a phone (cards are focusable), else the page.
        line_id = detail_url.rsplit('/', 1)[1]
        expect(page.locator(f'article.dt-card[data-line="{line_id}"]') if width == 390 else page.locator('#main')).to_be_focused()
        expect(page.get_by_role('dialog', name='Discard changes?', exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('after_close', ['return', 'user_focus', 'new_panel', 'navigation'])
def test_linked_line_closed_before_mount_keeps_focus_ownership(browser, live_server, h, after_close):
    flag(h)
    rule = weekly(h)
    line_id = rule['definition']['rule']['lines'][1]['id']
    # The privacy ruling leaves credentialers without rules.read; use a rule reader to cover focus ownership.
    context, page = sign_in(browser, live_server, 'auditor', viewport={'width': 390, 'height': 900})
    held = []
    draining = False
    def hold_catalogue(route):
        response = route_fetch(route)
        if draining:
            route.fulfill(response=response)
        else:
            held.append((route, response))
    try:
        page.emulate_media(color_scheme='dark')
        # The line is opened by its link in a tab that has not read the rules yet: a page read before opens from its
        # kept replies at once (api.ts keptReads), so only a first read can still be on its way when the panel opens.
        # Pause settlement, holding that real read until the page skeleton has appeared. The panel then opens while its
        # marked card is still detached, exactly as on a loaded runner; no wait for that card before closing it.
        page.clock.pause_at(page.evaluate('new Date(Date.now()+1000).toISOString()'))
        page.route(re.compile(r'/api/rules/catalogue(\?|$)'), hold_catalogue)
        page.evaluate("id => { location.hash = 'rules/duties_per_week/' + id; }", line_id)
        wait_held(page, held)
        page.clock.run_for(300)
        expect(page.locator('#main .loading.page-sk')).not_to_have_class(re.compile(r'\bsk-wait\b'))
        route, response = held.pop()
        route.fulfill(response=response)
        panel = page.locator('.rc-line-panel')
        expect(panel).to_be_visible()
        card = page.locator(f'article.dt-card[data-line="{line_id}"]')
        expect(card).to_have_count(0)
        close = panel.get_by_role('button', name='Close panel', exact=True)
        close.focus()
        close.press('Escape')
        expect(panel).to_have_count(0)
        expect(page).to_have_url(re.compile(r'#rules/duties_per_week$'))
        expect(page.locator('#main')).to_be_focused()
        expect(card).to_have_count(0)

        if after_close == 'user_focus':
            menu = page.get_by_role('button', name='Menu', exact=True)
            menu.focus()
            expect(menu).to_be_focused()
            # Moving back to the fallback must not revive the cancelled intent.
            page.locator('#main').focus()
        elif after_close == 'new_panel':
            page.evaluate("""async id => {const {dockPanel,rowMark}=await window.importBundle('side_panel.js');
              dockPanel({key:'focus-successor',className:'focus-successor',title:'Another line',mark:rowMark(id),build:()=>{}});
            }""", line_id)
            expect(page.locator('.focus-successor h2')).to_be_focused()
        elif after_close == 'navigation':
            # Closing the linked panel already replaced its URL: leave the rule before navigating back to it.
            page.evaluate("location.hash='rules'")
            page.clock.resume()
            expect_loaded(page, 'rules')  # from the kept catalogue reply: no read to wait for
            expect(page.locator('#main')).to_have_attribute('data-loaded-tab', '')
            expect(page).to_have_url(re.compile(r'#rules$'))
            # The destination contains the very same marked card but did not open its panel.
            page.evaluate("location.hash='rules/duties_per_week'")
            expect(page).to_have_url(re.compile(r'#rules/duties_per_week$'))

        if after_close != 'navigation':
            page.clock.run_for(400)
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        expect(page.locator('#main')).to_have_attribute('data-loaded-tab', 'duties_per_week')
        expect(card).to_have_count(1)
        if after_close == 'navigation':
            expect(card).to_be_visible()
            expect(page.locator('.dock-panel')).to_have_count(0)
        if after_close == 'return':
            expect(card).to_be_focused()
        else:
            expect(card).not_to_be_focused()
            if after_close == 'new_panel':
                expect(page.locator('.focus-successor h2')).to_be_focused()
                page.locator('.focus-successor').get_by_role('button', name='Close panel', exact=True).press('Escape')
                # A connected opener still takes precedence over the marked card.
                expect(page.locator('#main')).to_be_focused()
            elif after_close == 'user_focus':
                expect(page.locator('#main')).to_be_focused()
    finally:
        draining = True
        while held:
            route, response = held.pop(0)
            route.fulfill(response=response)
        page.unroute_all(behavior='wait')
        context.close()


def test_the_page_says_when_the_catalogue_did_not_load(browser, live_server, h):
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        page.route('**/api/rules/catalogue', lambda route: route.fulfill(status=503, json={'error': {'code': 'UNAVAILABLE', 'message': 'Down'}}))
        page.evaluate("location.hash='rules'")
        expect(page.locator('#main')).to_contain_text('Couldn’t load Rules.')
        expect(page.get_by_role('button', name='Retry')).to_be_visible()
    finally:
        context.close()


def test_long_lists_show_their_first_items_and_the_rest_on_request(browser, live_server, h):
    """Department scale: a proposal of 30 lines lists 20 changes and "Show 10 more changes"; 60 rules not in the
    catalogue stay discoverable through contextual search and paging without a permanent panel."""
    from .test_rule_templates import activate, propose
    flag(h)
    for n in range(60):
        activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name=f'Old rest rule {n:02}'))
    day = lambda n: str(date(2027, 1, 1) + timedelta(days=n))  # noqa: E731
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}}, starts_on=day(n), ends_on=day(n + 1)) for n in range(30)]
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_week', 'lines': lines, 'reason': 'Catalogue fixture'})
    context, page = sign_in(browser, live_server, 'admin')
    try:
        navigate(page, 'rules')
        expect(page.locator('#main .rc-kept')).to_have_count(0)
        rules_action(page, 'Other department rules (60)')
        kept = page.get_by_role('region', name='Other department rules', exact=True)
        seen = set()
        for _ in range(60):  # bounded if a paging regression repeats a page
            seen.update(kept.locator('[data-row]').evaluate_all('(rows) => rows.map(r => r.dataset.row)'))
            next_page = kept.get_by_role('button', name='Next page', exact=True)
            if not next_page.count() or not next_page.is_enabled():
                break
            next_page.click()
        expected = {p['id'] for p in h.get('admin', '/api/catalogues')['policies'] if p['name'].startswith('Old rest rule ')}
        assert len(expected) == 60 and seen == expected
        kept.get_by_role('searchbox', name='Search other department rules', exact=True).fill('Old rest rule 59')
        expect(kept.locator('[data-row]')).to_have_count(1)
        expect(kept).to_contain_text('Old rest rule 59')
        kept.get_by_role('button', name='Close panel', exact=True).click()
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('.rc-waiting')
        expect(waiting.locator('tr.rc-prop-added')).to_have_count(20)
        waiting.get_by_role('button', name='Show 10 more changes', exact=True).click()
        expect(waiting.locator('tr.rc-prop-added')).to_have_count(30)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (390, 'dark')])
def test_the_rules_database_opens_rules_to_their_lines_and_the_rule_page(browser, live_server, h, width, scheme):
    flag(h)
    weekly(h)
    catalogue_count = len(h.get('coordinator', '/api/rules/catalogue')['entries'])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.emulate_media(color_scheme=scheme)
        rules_by_family(h, page)  # Group: Family (Question, A–F, is the default)
        navigate(page, 'rules')
        main = page.locator('#main')
        # Families as group rows, rules in catalogue order (not A–Z), each with its switch and line count.
        expect(main.locator('section.rt-q').first.locator('.rt-qhead h3')).to_contain_text('Staffing on a session')
        rule = main.locator('li.rt-item').filter(has_text='Duties per week')
        expect(rule).to_contain_text('2 lines')
        expect(rule.locator('.rt-title .rt-rule-link')).to_have_text('Duties per week')
        expect(rule.locator('.rt-meta')).to_contain_text('2 lines')
        expect(rule.locator('.rt-sentence')).to_have_count(0)
        toggle = rule.locator('.rt-toggle')
        expect(toggle).to_have_accessible_name('Duties per week lines')
        expect(toggle).to_have_attribute('aria-expanded', 'false')
        expect(toggle).to_have_attribute('aria-controls', re.compile(r'^rt-lines-'))
        expect(main.locator('li.rt-item').filter(has_text='Duties per month')).to_contain_text('no lines')
        expect(page.get_by_role('switch', name='Duties per week on or off')).to_be_checked()
        expect(page.locator('.app-footer .dt-count')).to_have_text(f'{catalogue_count} rules')
        expect(page.locator('.app-footer .footer-total')).to_contain_text(re.compile(r'^2 on · 3 lines\b'))
        # A rule opens to its numbered lines: who and where as pills, the numbers as the key reads them.
        rule.get_by_role('button', name='Duties per week lines').click()
        lines = main.locator('li.rt-ln')
        expect(lines).to_have_count(2)
        expect(lines.first.locator('.rt-ln-num')).to_contain_text('≤ 5')
        expect(lines.nth(1).locator('.rp.person')).to_have_text('Alex Morgan')
        expect(main.get_by_role('button', name='+ New line in Duties per week')).to_be_visible()
        shot(page, f'rules-{width}-{scheme}')
        # The static toggle name exposes state through aria-expanded; Alt+click closes all rules.
        toggle_all = main.locator('[data-key="toggle-all"]')
        toggle_all.click()
        expect(toggle_all).to_have_attribute('aria-expanded', 'true')
        rule.get_by_role('button', name='Duties per week lines', exact=True).click(modifiers=['Alt'])
        expect(toggle_all).to_have_attribute('aria-expanded', 'false')
        expect(lines).to_have_count(0)
        rule.get_by_role('button', name='Duties per week lines').click()
        # Search finds lines (and opens their rule); a search that leaves nothing keeps the way back.
        search = main.get_by_role('searchbox', name='Search rules')
        search.fill('Return from leave')
        expect(lines).to_have_count(1)
        search.fill('zzz')
        expect(main.get_by_text('No matching rules')).to_be_visible()
        search.fill('')
        # ↗ opens the line in its rule's page, in the line editor; ← Rules comes back to the same open rule.
        lines.nth(1).get_by_role('button', name='Open line 2 of Duties per week').click()
        expect(page).to_have_url(re.compile('#rules/duties_per_week/'))
        expect(page.locator('.rc-line-panel')).to_contain_text('Line 2 of 2')
        page.locator('.rc-line-panel').get_by_role('button', name='Close panel').click()  # a sheet over the page on phones
        main.get_by_role('button', name='← Rules').click()
        expect(main.locator('li.rt-ln')).to_have_count(2)
    finally:
        context.close()
    assert not errors


def test_a_thousand_lines_open_and_filter_within_budget(browser, live_server, h):
    """Department scale: a rule with 1,000 lines opens, and a search over them redraws in work that grows with the lines
    shown (about 22 elements a line today, budget 30) without one forced layout of the table. The work is counted, not
    timed: the 300/600 ms wall-clock budget failed on a busy runner with nothing changed (#283). The time is kept in the
    receipt."""
    flag(h)
    day = lambda n: str(date(2027, 1, 1) + timedelta(days=n))  # noqa: E731 - one line per day: none is the same line
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}}, starts_on=day(n), ends_on=day(n + 1)) for n in range(1000)]
    catalogue_rule(h, 'duties_per_week', lines)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        expect(page.locator('.app-footer')).to_contain_text('1001 lines')
        main.get_by_role('button', name='Duties per week lines').click()
        expect(main.locator('li.rt-ln')).to_have_count(1000)
        # The work a search does: two synchronous redraws of the 1,000 lines (test_browser.COUNT_WORK).
        work = page.evaluate(f"""() => {{const box=document.querySelector('#main input[type=search]');
            const work=({COUNT_WORK})(() => {{box.value='Everyone';box.dispatchEvent(new Event('input'));box.value='';box.dispatchEvent(new Event('input'));}});
            return {{...work,ms:work.ms/2}};}}""")
        from .test_browser_audit_request_performance import receipt
        receipt('rule-filter-performance', {'lines': 1000, 'average_sync_redraw_ms': work['ms'], 'elements_created': work['created'], 'layout_reads': work['layout']})
        assert work['layout'] == [], work['layout']  # a layout read mid-redraw lays out all 1,000 lines
        assert work['created'] <= 2 * 1000 * 30, work['created']  # two redraws of 1,000 lines each
    finally:
        context.close()


def test_rules_view_settings_offer_only_open_all_or_collapsed(browser, live_server, h):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='View settings', exact=True).click()
        expect(page.get_by_role('radio', name='Open all rules', exact=True)).to_be_visible()
        expect(page.get_by_role('radio', name='Collapsed', exact=True)).to_be_visible()
        expect(page.get_by_role('radio', name='Table', exact=True)).to_have_count(0)
        page.get_by_role('radio', name='Open all rules', exact=True).click()
        expect(page.locator('#main [data-key="toggle-all"]')).to_have_attribute('aria-expanded', 'true')
    finally:
        context.close()


def test_lines_that_overlap_are_marked_and_the_rules_filtered(browser, live_server, h):
    flag(h)
    equal_tier = weekly_group_conflict(h)
    saved = weekly(h, extra_lines=equal_tier)['definition']['rule']['lines']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        rule = main.locator('li.rt-item').filter(has_text='Duties per week')
        expect(rule.locator(':scope > .rt-row')).not_to_contain_text('⚠ Overlaps')
        rule.get_by_role('button', name='Duties per week lines').click()
        for part in saved[:2]:
            flags = rule.locator(f'li.rt-ln[data-line="{part["id"]}"] .rt-ln-flags')
            expect(flags).not_to_contain_text('⚠ Overlaps')
        for part in equal_tier:
            flags = rule.locator(f'li.rt-ln[data-line="{part["id"]}"] .rt-ln-flags')
            expect(flags).to_contain_text('⚠ Overlaps')
        main.get_by_role('button', name=re.compile('^Filter')).click()
        condition(page.locator('.dt-pop'), 'Checks', ['Overlaps'])
        page.keyboard.press('Escape')
        expect(main.get_by_role('button', name=re.compile('^Filter: Overlaps'))).to_be_visible()
        expect(main.locator('li.rt-item')).to_have_count(1)
        expect(page.locator('.app-footer .dt-count')).to_have_text(re.compile(r'^1 of \d+ rules$'))  # E13: in the footer only
    finally:
        context.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_the_rules_list_has_no_horizontal_overflow_and_groups_scope(browser, live_server, h, width):
    flag(h)
    weekly(h)
    clinics = h.seed['services'] + [h.configure('services', {'name': f'Clinic {n}', 'starts_on': '2026-01-01'})['record']['id'] for n in range(3)]
    catalogue_rule(h, 'people_per_session', [line({'max': {'value': 2, 'level': 'mandatory'}}, where={'conditions': [{'field': 'service_ids', 'values': clinics}]})])
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        navigate(page, 'rules')
        main = page.locator('#main')
        scroll = main.locator('.rt-scroll')
        assert scroll.evaluate('n => n.scrollWidth <= n.clientWidth'), 'the Rules list must fit its pane'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'the Rules list must not overflow the viewport'
        main.get_by_role('button', name='People per session lines').click()
        groups = main.locator('.rt-wh')
        expect(groups).to_have_count(1)
        expect(groups.first.locator('.pill-more')).to_have_text(f'+{len(clinics) - 3}')  # first three, then +N
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'opened lines must not overflow the viewport'
    finally:
        context.close()


def test_a_quota_line_sets_its_own_counting_window(browser, live_server, h):
    """rc.16 (DEV gap G2): a Duties per week line with an at-least number counts as a quota; its own counting end
    (settings.block_quota.ends_on, apart from its Valid dates) is edited in the line's Settings beside Repeats while the
    line is valid, and proposed with the line."""
    from .test_browser_rule_catalogue_v2 import reason
    flag(h)
    quota = line({'min': {'value': 2, 'level': 'mandatory'}}, starts_on='2026-09-01', ends_on='2027-09-01', note='Counted weekly')
    catalogue_rule(h, 'duties_per_week', [quota])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, f'rules/duties_per_week/{quota["id"]}')
        panel = page.locator('.rc-line-panel')
        panel.get_by_role('button', name='Edit…', exact=True).click()
        pop = page.locator('.rc-settings')
        pop.get_by_role('radiogroup', name='Counting ends (last day)', exact=True).get_by_role('radio', name='Own value', exact=True).click()
        end = pop.get_by_role('textbox', name='Counting ends (last day)', exact=True)
        end.fill('30/11/2026')
        end.press('Tab')
        # Repeats while the line is valid sits beside it (the server refuses it here: this rule adjusts for days in the department).
        expect(pop.get_by_role('radiogroup', name='Repeats while the line is valid', exact=True)).to_be_visible()
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        expect(panel.locator('.rc-lp-settings')).to_contain_text('Counting ends (last day)30 Nov 2026')
        panel.get_by_role('button', name='Keep change', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        # The line link ends with its panel: a reload shows the rule, not the closed line.
        expect(page).to_have_url(re.compile(r'#rules/duties_per_week$'))
        page.locator('.topbar').get_by_role('button', name=re.compile(r'^Propose \d+ change')).click()
        reason(page, 'Counting ends with the term', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('waiting for approval')
        proposed = next(e for e in h.get('coordinator', '/api/rules/catalogue')['entries'] if e['id'] == 'duties_per_week')['proposed']
        own = proposed['definition']['rule']['lines'][0]['settings']['block_quota']
        assert own == {'ends_on': '2026-12-01'}, own
    finally:
        context.close()


def test_a_rule_page_discards_its_unproposed_changes(browser, live_server, h):
    """Lane E QA: a rule's own page offers Discard changes beside Propose while there are changes not yet proposed
    (same-button confirm, as on the Rules list); after it the page reads the version in force."""
    flag(h)
    catalogue_rule(h, 'duties_per_week', [line({'max': {'value': 5}})])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/duties_per_week')
        topbar = page.locator('.topbar')
        expect(topbar.get_by_role('button', name='Discard changes')).to_be_hidden()
        page.locator('#main tbody tr[data-row]').first.get_by_label(re.compile('^Max: ')).click()
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).fill('6')
        page.get_by_role('dialog', name='Edit Max').get_by_label('Max', exact=True).press('Enter')
        expect(topbar.get_by_role('button', name='Propose 1 change')).to_be_enabled()
        discard = topbar.get_by_role('button', name='Discard changes')
        discard.click()  # armed: press twice
        topbar.get_by_role('button', name='Discard all?').click()
        expect(topbar.get_by_role('button', name='Propose', exact=True)).to_be_disabled()
        expect(topbar.get_by_role('button', name='Discard changes')).to_be_hidden()
        expect(page.locator('.app-footer')).not_to_contain_text('not yet proposed')
    finally:
        context.close()
