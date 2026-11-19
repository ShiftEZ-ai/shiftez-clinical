"""Balance between people (a rule's Settings): on by default, everyone under the rule is compared together for fairness;
off, each line is balanced on its own. Proposed and approved like any rule change; read-only roles read it as a fact;
rules that share out no target do not show it. The line panel reads one rule's reach (policy_id), grouped by the
server's matched paths."""
import pytest
from playwright.sync_api import expect

from .governance_support import identity
from .test_browser import browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, shot, weekly
from packages.contracts.schemas import PERMISSIONS
from .test_browser_rule_filter_builder import compound, entry

pytestmark = pytest.mark.browser
RULES_READ_ONLY = tuple(role for role in PERMISSIONS['rules.read'] if role not in PERMISSIONS['rule.propose'])


def settings(page):
    page.locator('.topbar').get_by_role('button', name='Settings', exact=True).click()
    pop = page.get_by_role('dialog', name='Duties per week settings')
    expect(pop).to_be_visible()
    return pop


def reason(page, text, button):
    dialog = page.get_by_role('dialog')
    dialog.get_by_label('Reason', exact=True).fill(text)
    dialog.get_by_role('button', name=button, exact=True).click()


def test_balance_off_is_proposed_approved_and_in_force(browser, live_server, h):
    flag(h)
    weekly(h)
    assert entry(h, 'duties_per_week')['rule']['definition']['balance_between_people'] is True
    coordinator, page = sign_in(browser, live_server, 'coordinator')
    approver, review = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules/duties_per_week')
        pop = settings(page)
        switch = pop.get_by_role('switch', name='Balance between people')
        expect(switch).to_be_checked()
        expect(pop).to_contain_text('Compare everyone under this rule together for fairness.')
        switch.click()
        pop.get_by_role('button', name='Keep', exact=True).click()
        expect(pop).to_have_count(0)
        page.locator('.topbar').get_by_role('button', name='Propose 1 change').click()
        expect(page.get_by_role('dialog')).to_contain_text('Balance between people: Off — each line is balanced on its own')
        expect(page.get_by_role('dialog').get_by_label('I have read which lines give way and that is intended')).to_have_count(0)
        reason(page, 'Each line on its own', 'Propose')
        expect(page.locator('.rc-waiting')).to_contain_text('Balance between people off')
        waiting = entry(h, 'duties_per_week')['proposed']
        assert waiting['definition']['balance_between_people'] is False and waiting['diff']['balance_between_people'] is False

        navigate(review, 'rules/duties_per_week')
        box = review.locator('.rc-waiting')
        expect(box).to_contain_text('Balance between people off')
        box.get_by_role('button', name='Review and approve').click()
        # B5 (f1d80dcc4): an independent approver decides in the docked panel, named after the change; no reason (maintenance).
        panel = review.get_by_role('region', name='Change to Duties per week', exact=True)
        expect(panel).to_contain_text('Each line on its own')
        panel.get_by_role('button', name='Approve and activate', exact=True).click()
        expect(panel).to_have_count(0)
        expect(review.locator('.rc-waiting')).to_have_count(0)
        assert entry(h, 'duties_per_week')['rule']['definition']['balance_between_people'] is False

        # In force: the coordinator's Settings start from it; turning it back on is a new change.
        page.reload()
        pop = settings(page)
        expect(pop.get_by_role('switch', name='Balance between people')).not_to_be_checked()
        pop.get_by_role('button', name='Cancel', exact=True).click()
        expect(page.locator('.topbar').get_by_role('button', name='Propose 1 change')).to_have_count(0)
    finally:
        coordinator.close()
        approver.close()


@pytest.mark.parametrize('role', RULES_READ_ONLY)
def test_read_only_roles_read_balance_as_a_fact(browser, live_server, h, role):
    if role == 'programme_lead':
        identity(h, role, [role])
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, role)
    try:
        navigate(page, 'rules/duties_per_week')
        pop = settings(page)
        expect(pop.locator('.rc-balance')).to_contain_text('Balance between people: On')
        expect(pop.locator('.rc-balance')).to_contain_text('Everyone under this rule is compared together for fairness.')
        expect(pop.get_by_role('switch')).to_have_count(0)
        expect(pop.get_by_role('button', name='Keep', exact=True)).to_have_count(0)
        shot(page, f'balance-read-only-{role}', full_page=False)
    finally:
        context.close()


def test_rules_that_share_out_no_target_have_no_balance(browser, live_server, h):
    flag(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules/shift_order')
        page.locator('.topbar').get_by_role('button', name='Settings', exact=True).click()
        pop = page.get_by_role('dialog', name='Shift order settings')
        expect(pop).to_be_visible()
        expect(pop.locator('.rc-balance')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('width,scheme', [(w, c) for w in (1440, 390) for c in ('light', 'dark')])
def test_balance_switch_screens(browser, live_server, h, width, scheme):
    flag(h)
    weekly(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        pop = settings(page)
        expect(pop.get_by_role('switch', name='Balance between people')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'balance-{width}-{scheme}', full_page=False)
    finally:
        context.close()


def test_the_line_panel_reads_one_rules_reach(browser, live_server, h):
    flag(h)
    rule = compound(h)
    line_id = rule['definition']['rule']['lines'][1]['id']
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        with page.expect_request(lambda r: '/api/policies/reach?details=true' in r.url) as asked:
            navigate(page, f'rules/duties_per_week/{line_id}')
        assert f"policy_id={rule['id']}" in asked.value.url
        panel = page.locator('.rc-line-panel')
        panel.get_by_role('button', name='2 people').first.click()
        pop = page.locator('.picker')  # the shared picker (picker.ts)
        # Grouped by the "or" row that matched each person (the server's matched paths), for a read-only role too.
        expect(pop.locator('.picker-group').first).to_contain_text('Rank is any of')
    finally:
        context.close()
