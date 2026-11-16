"""rc.12 Rules QA: Simplify rules blockers with their actions, and rule Settings in plain words (read-only as facts)."""
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, navigate, route_fetch  # noqa: F401
from .test_browser_rule_catalogue import flag, rules_action
from .test_browser_rule_catalogue_v2 import line, reason, themed, v2_rule

pytestmark = pytest.mark.browser


def blocked_department(h):
    """An old department whose move is blocked twice: a rule change waiting for approval, and a listed-only rule whose
    person row has no number of its own (it inherits the rule's default)."""
    flag(h)
    a, b = h.seed['people'][:2]
    source = h.command('coordinator', 'policy.create', {
        'name': 'Weekly clinical target', 'owner': 'Synthetic department', 'source': 'Synthetic inherited default', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, 'listed_only': True,
                       'per_person': [{'person_id': a, 'amount': None}, {'person_id': b, 'amount': 3}]}})['record']
    h.command('approver', 'policy.activate', {'reason': 'Independent review'}, source['id'], source['revision'])
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_month', 'reason': 'Monthly cap',
                                              'lines': [line({'max': {'value': 20, 'level': 'mandatory'}})]})


@pytest.mark.parametrize('width,theme', [(1440, 'light'), (1440, 'dark')])
def test_simplify_blockers_say_what_stops_the_move_and_open_what_clears_it(browser, live_server, h, width, theme):
    blocked_department(h)
    context, page = themed(browser, live_server, 'coordinator', width, theme)
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Simplify rules'))
        panel = page.locator('.rs-panel')
        blockers = panel.locator('.rs-blockers')
        expect(blockers).to_contain_text('2 things stop the move. Propose is off until they are cleared.')
        expect(blockers).to_contain_text('Rule changes are waiting for approval')
        expect(blockers).to_contain_text('Weekly clinical target')
        expect(blockers).to_contain_text('no number of their own')
        expect(blockers).not_to_contain_text('frozen')  # the server's engine wording stays out
        propose = panel.get_by_role('button', name='Propose', exact=True)
        expect(propose).to_be_disabled()
        # Review maintenance: its why is visible words beside it and its description (not a tooltip only).
        expect(propose).to_have_accessible_description(re.compile('Clear what stops the move'))
        expect(panel.locator('.why-unavailable')).to_contain_text('Clear what stops the move')
        blockers.locator('li', has_text='Duties per month').get_by_role('button', name='Open proposal').click()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        assert page.evaluate('location.hash') == '#rules/duties_per_month'
        expect(page.locator('.rs-panel')).to_have_count(0)
        navigate(page, 'rules')
        rules_action(page, re.compile('^Simplify rules'))
        page.locator('.rs-blockers').get_by_role('button', name='Open rule').click()
        expect(page.get_by_role('heading', name='Weekly clinical target')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        # The server names the person whose row inherits (person_ids): exactly that row opens, not the other one.
        navigate(page, 'rules')
        rules_action(page, re.compile('^Simplify rules'))
        inherited = page.locator('.rs-blockers li.rs-blocker', has_text='Weekly clinical target')
        a = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
        expect(inherited.get_by_role('button', name=re.compile('^Open row'))).to_have_count(1)
        inherited.get_by_role('button', name='Open row: ' + a['display_name'], exact=True).click()
        expect(page.get_by_role('dialog', name='Change Weekly clinical target')).to_be_visible()
    finally:
        context.close()


def test_rule_settings_read_in_plain_words_with_help(browser, live_server, h):
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'})])
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/workload_limit')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        # A Workload line's measure and period are not "its own settings".
        expect(pop.locator('.rc-settings-note')).to_have_count(0)
        expect(pop).to_contain_text('Adjust for time in the department')
        expect(pop).to_contain_text('Someone in the department for part of the period gets a matching share of the number.')
        expect(pop).to_contain_text('Days for the full number')
        expect(pop).to_contain_text('All duty types')
        for jargon in ('Prorate', 'Reference days', 'Counted per week, month or block', 'Duty limit'):
            expect(pop).not_to_contain_text(jargon)
        expect(pop.get_by_role('button', name='Keep', exact=True)).to_be_in_viewport()
    finally:
        context.close()


@pytest.mark.parametrize('role', ['approver', 'auditor'])
def test_read_only_settings_are_facts_not_disabled_controls(browser, live_server, h, role):
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'})])
    context, page = themed(browser, live_server, role, 1440, 'dark')
    try:
        navigate(page, 'rules/workload_limit')
        page.get_by_role('button', name='Settings', exact=True).click()
        pop = page.locator('.rc-settings')
        expect(pop.locator('.rc-setting-fact').filter(has_text='Duty types')).to_contain_text('All duty types')
        expect(pop.locator('input, select, .pick-button')).to_have_count(0)
        expect(pop.get_by_role('button', name='Close', exact=True)).to_be_visible()
    finally:
        context.close()


def test_phone_settings_keep_their_buttons_above_the_footer(browser, live_server, h):
    flag(h, legacy=False)
    v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'})])
    context, page = themed(browser, live_server, 'coordinator', 390, 'light')
    try:
        navigate(page, 'rules/workload_limit')
        page.get_by_role('button', name='Settings', exact=True).click()
        keep = page.locator('.rc-settings').get_by_role('button', name='Keep', exact=True)
        expect(keep).to_be_in_viewport()
        footer_top = page.evaluate("document.querySelector('.app-footer').getBoundingClientRect().top")
        assert keep.bounding_box()['y'] + keep.bounding_box()['height'] <= footer_top
    finally:
        context.close()


def test_line_counter_counts_every_line_and_one_person_reads_their_name(browser, live_server, h):
    """Opened by link, the line panel counts all of the rule's lines (not the 50 on the table's page); a line for one
    person says their name, never "Each of 1 person"."""
    flag(h, legacy=False)
    people = [p['id'] for p in h.get('admin', '/api/catalogues')['people']][:2]
    day = lambda n: (date(2026, 11, 1) + timedelta(days=n)).isoformat()  # noqa: E731
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'},
                  who={'conditions': [{'field': 'person_ids', 'values': [people[0]]}]}, starts_on=day(n), ends_on=day(n + 1))
             for n in range(59)]
    # The first line: one person, in force today.
    lines.insert(0, line({'max': {'value': 4, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'},
                         who={'conditions': [{'field': 'person_ids', 'values': [people[1]]}]}))
    rule = v2_rule(h, 'workload_limit', lines)
    first = next(l['id'] for l in rule['definition']['rule']['lines'] if not l.get('starts_on'))
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, f'rules/workload_limit/{first}')
        panel = page.locator('.rc-line-panel')
        expect(page.get_by_text(re.compile(r'Line \d+ of 60$'))).to_be_visible()
        named = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == people[1])
        expect(panel.locator('.rc-says')).to_contain_text((named.get('name') or named['display_name']) + ':')
        expect(panel.locator('.rc-says')).not_to_contain_text('Each of 1 person')
    finally:
        context.close()


def test_exceptions_panel_fits_and_search_has_one_focus_ring(browser, live_server, h):
    flag(h)
    a, b = h.seed['people'][:2]
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    payload = {'policy_id': policy['id'], 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    h.command('coordinator', 'exception.create', {**payload, 'person_id': a,
              'reason': 'A long reason that explains why this person needs a bounded exception for one week of clinics'})
    context, page = themed(browser, live_server, 'admin', 1024, 'light')
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Exceptions'))
        panel = page.get_by_role('region', name='Exceptions', exact=True)
        scroller = panel.locator('.table-scroll').first
        expect(scroller).to_be_visible()
        widths = scroller.evaluate("n => [n.scrollWidth, n.clientWidth, [...n.querySelectorAll('thead th')].map(t => t.textContent + ':' + Math.round(t.getBoundingClientRect().width))]")
        assert widths[0] <= widths[1] + 1, f'A column is clipped in the Exceptions panel: {widths}'
        search = panel.get_by_role('searchbox').first
        search.focus()
        assert search.evaluate("n => getComputedStyle(n).boxShadow") == 'none'  # the box around it carries the one ring
        page.get_by_role('button', name='Close panel', exact=True).click()
        rules_action(page, re.compile(r'^Other department rules'))
        other = page.get_by_role('region', name='Other department rules', exact=True).locator('.table-scroll').first
        expect(other).to_be_visible()
        assert other.evaluate('n => n.scrollWidth <= n.clientWidth + 1'), 'A column is clipped in Other department rules'
    finally:
        context.close()


def test_waiting_change_is_activated_with_one_verb(browser, live_server, h):
    flag(h)
    h.command('coordinator', 'rule.propose', {'catalogue_id': 'duties_per_month', 'reason': 'Monthly cap',
                                              'lines': [line({'max': {'value': 20, 'level': 'mandatory'}})]})
    context, page = themed(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_month')
        waiting = page.locator('.rc-waiting')
        expect(waiting.get_by_role('button', name=re.compile('^Approve$'))).to_have_count(0)  # SW3-B-16: one name, Review and approve
        waiting.get_by_role('button', name='Review and approve', exact=True).click()
        # B5 (f1d80dcc4): the decision docks in a panel named after the change; its one verb is Approve and activate.
        review = page.get_by_role('region', name='Add a line for everyone · turns the rule on', exact=True)
        expect(review.get_by_role('button', name='Approve and activate', exact=True)).to_be_visible()
        expect(review.get_by_role('button', name=re.compile('^(Approve|Activate)$'))).to_have_count(0)
    finally:
        context.close()


def test_proposal_blocker_lists_exactly_the_servers_proposals(browser, live_server, h):
    """Each waiting proposal the server names (proposal_ids) is one row with its own Open proposal; nothing is guessed."""
    blocked_department(h)
    plan = h.get('coordinator', '/api/rules/catalogue/migration?version=2&background=true')
    [blocker] = [b for b in plan['blocked'] if b['kind'] == 'proposal']
    assert len(blocker['proposal_ids']) == 1
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Simplify rules'))
        listed = page.locator('.rs-blockers li.rs-blocker', has_text='Rule changes are waiting').locator('.rs-blocker-list > li')
        expect(listed).to_have_count(1)
        expect(listed).to_contain_text('Duties per month')
    finally:
        context.close()


def test_simplify_propose_waits_for_a_fresh_passing_background_proof(browser, live_server, h):
    """With a rota being planned the preview shows at once; the behaviour check runs as a job and Propose turns on only
    when it passes for this plan."""
    from apps.worker.main import process_one
    flag(h, legacy=False)
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.workspace('Rota being planned')
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-proof')).to_contain_text('Checking behaviour is unchanged')
        expect(panel.locator('.rs-proof')).not_to_contain_text('minutes')  # RU-17: progress, not a fixed estimate
        expect(panel.locator('.rs-map [data-row]')).to_have_count(2)  # authored duty cap and seeded Fair share are already previewed
        propose = panel.get_by_role('button', name='Propose', exact=True)
        expect(propose).to_be_disabled()
        # Row 96 exposes the disabled reason as the control's accessible description (the visible reason is not a hover-only tip).
        expect(propose).to_have_accessible_description(re.compile('Wait for the behaviour check'))
        expect(panel.locator('.why-unavailable')).to_contain_text('Wait for the behaviour check')
        expect(panel.get_by_role('button', name='Preview again', exact=True)).to_have_count(0)
        for _ in range(100):  # the panel queues the job; the worker runs it
            if process_one(h.app.state.engine):
                break
            page.wait_for_timeout(100)
        else:
            raise AssertionError('The panel did not queue the behaviour check')
        expect(panel.locator('.notice.success')).to_contain_text('Behaviour unchanged on 1 rota', timeout=20000)
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_be_enabled()
        assert len([e for e in h.get('auditor', '/api/audit')['events'] if e['action'] == 'rule.catalogue.check']) == 1
    finally:
        context.close()


@pytest.mark.parametrize('state', ['failed', 'cancelled'])
def test_simplify_failed_or_cancelled_proof_keeps_propose_off_and_offers_a_new_check(browser, live_server, h, state):
    flag(h, legacy=False)
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.workspace('Rota being planned')
    context, page = themed(browser, live_server, 'coordinator')
    def ended(route):
        response = route_fetch(route)
        payload = response.json()
        payload.update(proof_state=state, proof=None,
                       proof_job={'id': 'job-1', 'status': state, 'error_code': 'STALE_INPUTS' if state == 'failed' else None})
        route.fulfill(response=response, json=payload)
    try:
        page.route('**/api/rules/catalogue/migration?version=2&background=true', ended)
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-proof.error')).to_contain_text('the rules or rotas changed' if state == 'failed' else 'cancelled')
        expect(panel.locator('.rs-proof').get_by_role('button', name='Check again', exact=True)).to_be_visible()
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_be_disabled()
    finally:
        context.close()


def test_simplify_unchecked_preview_waits_for_a_coordinator_or_approver(browser, live_server, h):
    """Roles that cannot start the check see why Propose is off; nothing is queued for them."""
    flag(h, legacy=False)
    h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    h.workspace('Rota being planned')
    context, page = themed(browser, live_server, 'auditor')
    try:
        navigate(page, 'rules')
        rules_action(page, 'Simplify rules')
        panel = page.locator('.rs-panel')
        expect(panel.locator('.rs-proof')).to_contain_text('A coordinator or approver starts it')
        expect(panel.get_by_role('button', name='Propose', exact=True)).to_have_count(0)
        assert not [j for j in h.get('coordinator', '/api/jobs')['jobs'] if j['kind'] == 'catalogue_proof']
    finally:
        context.close()


def row_action(page, row, label):
    row.get_by_role('button', name=re.compile('^More actions')).click()
    page.get_by_role('menuitem', name=label, exact=True).click()


def exception_row(h, role, exception):
    return next(r for r in h.get(role, '/api/catalogues')['exceptions'] if r['id'] == exception['id'])


def test_exception_end_and_undo_are_governed_reasoned_and_shown_only_when_allowed(browser, live_server, h):
    flag(h)
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    person = h.seed['people'][0]
    exc = h.command('coordinator', 'exception.create', {'policy_id': policy['id'], 'person_id': person,
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': 'Synthetic bounded exception'})['record']
    exc = h.command('approver', 'exception.approve', {'reason': 'Independent review'}, exc['id'], exc['revision'])['record']
    coordinator, cpage = themed(browser, live_server, 'coordinator')
    approver, page = themed(browser, live_server, 'approver')
    try:
        # can_end is false for the coordinator on an approved exception: no control is drawn.
        navigate(cpage, 'rules')
        rules_action(cpage, re.compile('^Exceptions'))
        crow = cpage.get_by_role('region', name='Exceptions', exact=True).locator('[data-row]').first
        expect(crow).to_contain_text('Approved')
        expect(crow.get_by_role('button', name=re.compile('^More actions'))).to_have_count(0)

        navigate(page, 'rules')
        rules_action(page, re.compile('^Exceptions'))
        panel = page.get_by_role('region', name='Exceptions', exact=True)
        row_action(page, panel.locator('[data-row]').first, 'End exception…')
        reason(page, 'Rotation finished early', 'End exception')
        expect(page.locator('#toast-tray')).to_contain_text('Exception ended.')
        assert exception_row(h, 'auditor', exc)['status'] == 'ended'
        rules_action(page, re.compile('^Exceptions'))
        row = page.get_by_role('region', name='Exceptions', exact=True).locator('[data-row]').first
        expect(row).to_contain_text('Ended')
        row.get_by_role('button', name=re.compile('^More actions')).click()
        expect(page.get_by_role('menuitem', name='End exception…')).to_have_count(0)
        page.get_by_role('menuitem', name='Undo', exact=True).click()
        reason(page, 'Ended by mistake', 'Undo')
        expect(page.locator('#toast-tray')).to_contain_text('Exception restored.')
        restored = exception_row(h, 'auditor', exc)
        assert restored['status'] == 'approved' and restored['approved_by'] == exc['approved_by']
        actions = [e['action'] for e in h.get('auditor', '/api/audit')['events'] if e['target_id'] == exc['id']]
        assert 'exception.end' in actions and 'exception.undo' in actions
        # The person's own exception rows carry the same controls.
        navigate(page, f'people?open={person}')
        person_panel = page.locator('.person-panel')
        person_panel.get_by_role('tab', name='Rules', exact=True).click()
        row_action(page, person_panel.locator('[data-row]').first, 'End exception…')
        expect(page.get_by_role('dialog', name='End exception')).to_be_visible()
    finally:
        coordinator.close()
        approver.close()


def test_coordinator_withdraws_own_request_and_undo_brings_it_back(browser, live_server, h):
    flag(h)
    policy = h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'}, exception=True)
    exc = h.command('coordinator', 'exception.create', {'policy_id': policy['id'], 'person_id': h.seed['people'][0],
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11', 'reason': 'Synthetic bounded exception'})['record']
    context, page = themed(browser, live_server, 'coordinator', 390, 'dark')
    try:
        navigate(page, 'rules')
        rules_action(page, re.compile('^Exceptions'))
        row_action(page, page.get_by_role('region', name='Exceptions', exact=True).locator('[data-row]').first, 'Withdraw request…')
        reason(page, 'Requested in error', 'Withdraw')
        expect(page.locator('#toast-tray')).to_contain_text('Exception request withdrawn.')
        assert exception_row(h, 'auditor', exc)['status'] == 'withdrawn'
        rules_action(page, re.compile('^Exceptions'))
        exceptions = page.get_by_role('region', name='Exceptions', exact=True)
        expect(exceptions).to_contain_text('Withdrawn')  # a phone groups the cards by status
        row = exceptions.locator('[data-row]').first
        row_action(page, row, 'Undo')
        reason(page, 'Still needed', 'Undo')
        expect(page.locator('#toast-tray')).to_contain_text('Exception request restored.')
        assert exception_row(h, 'auditor', exc)['status'] == 'proposed'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        context.close()


def test_line_opened_by_link_uses_one_combined_read(browser, live_server, h):
    """#rules/<rule>/<line>: the rule, its check and the line's reach come from one detail read, not the whole
    catalogue, a check and a reach read in turn."""
    flag(h, legacy=False)
    rule = v2_rule(h, 'workload_limit', [
        line({'max': {'value': 5, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'}),
        line({'max': {'value': 3, 'level': 'mandatory'}}, measure='duties', period={'kind': 'calendar_week'},
             who={'conditions': [{'field': 'person_ids', 'values': [h.seed['people'][0]]}]})])
    second = rule['definition']['rule']['lines'][1]['id']
    context, page = themed(browser, live_server, 'coordinator')
    seen = []
    try:
        page.on('request', lambda r: seen.append(r.url) if '/api/' in r.url else None)
        navigate(page, f'rules/workload_limit/{second}')
        panel = page.locator('.rc-line-panel')
        named = next(p for p in h.get('admin', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])
        expect(panel.locator('.rc-says')).to_contain_text((named.get('name') or named['display_name']) + ':')
        reads = [u for u in seen if '/api/rules/catalogue' in u or '/api/policies/reach' in u]
        assert [u for u in reads if '/detail?' in u and f'line_id={second}' in u], reads
        assert not [u for u in reads if re.search(r'/api/rules/catalogue\?|/check$|/api/policies/reach', u)], reads
    finally:
        context.close()


def test_line_without_a_broader_line_says_its_actual_strength(browser, live_server, h):
    """A blank strength with no broader line to take it from reads as the strength that applies (mandatory for a
    Workload maximum), never "(strength from the broader line)"."""
    flag(h, legacy=False)
    rule = v2_rule(h, 'workload_limit', [line({'max': {'value': 5, 'level': None}}, measure='duties', period={'kind': 'calendar_week'})])
    only = rule['definition']['rule']['lines'][0]['id']
    context, page = themed(browser, live_server, 'coordinator')
    try:
        navigate(page, f'rules/workload_limit/{only}')
        says = page.locator('.rc-line-panel .rc-says')
        expect(says).to_contain_text('(mandatory)')
        expect(says).not_to_contain_text('broader line')
    finally:
        context.close()
