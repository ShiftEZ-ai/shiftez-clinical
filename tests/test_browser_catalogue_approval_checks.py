"""Catalogue and standalone proposals retain the production CP-SAT approval gates and review path."""
from dataclasses import replace
import re

import pytest
from playwright.sync_api import expect

from apps.api.main import create_app
from apps.worker.main import process_one
from .test_browser import route_fetch, browser, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import catalogue_rule, flag, line, shot
from .test_browser_rule_checks import adds_label, no_new_label

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


@pytest.fixture
def h(pg):
    # The fixture API and real HTTP server both enforce production's receipt requirement.
    pg.app.state.engine.dispose()
    pg.settings = replace(pg.settings, generation_engine='cp_sat')
    pg.app = create_app(pg.settings)
    pg.server_env = {'ROTA_GENERATION_ENGINE': 'cp_sat'}
    return pg


def policy(h, ident):
    # Retired versions load through the pageable history endpoint, not the operational catalogue (21e096a0d).
    return next(p for p in h.get('approver', '/api/catalogues')['policies']
                + h.get('approver', '/api/policies?status=retired')['policies'] if p['id'] == ident)


def pending_action(page, count):
    button = page.get_by_role('button', name=re.compile('^More rule actions'))
    # Wait for the refreshed count before opening a menu built from the current proposals.
    expect(button).to_have_accessible_name(f'More rule actions, {count} {"item" if count == 1 else "items"} waiting')
    menu = page.get_by_role('menu', name='More rule actions', exact=True)
    if not menu.is_visible():
        button.focus()
        button.press('Enter')
    return menu.get_by_role('menuitem', name=f'Other pending rules ({count})', exact=True)


def pending(page, count):
    action = pending_action(page, count)
    expect(action).to_be_visible()
    action.focus()
    action.press('Enter')
    panel = page.get_by_role('region', name='Other pending rules', exact=True)
    expect(panel.get_by_role('searchbox', name='Search pending rules', exact=True)).to_be_focused()
    return panel


def check_and_activate(page, h, workspace, proposal, title, adds_problems=False):
    review = page.get_by_role('dialog', name='Review ' + title, exact=True)
    expect(review).to_be_visible()
    expect(review.get_by_role('cell', name=workspace['name'], exact=True)).to_be_visible()
    expect(review.get_by_text('Not checked', exact=True)).to_be_visible()
    before = h.detail(workspace)['shifts']
    decisions = []
    page.on('request', lambda r: decisions.append(r.post_data_json) if r.method == 'POST'
            and r.url.endswith('/api/commands') and r.post_data_json['kind'] == 'policy.activate' else None)
    review.get_by_label('Reason', exact=True).fill('Independent checked approval')
    shot(page, 'catalogue-review-controls-' + proposal['id'], full_page=False)
    expect(review.locator('.rule-gate')).to_contain_text(f"Activation needs a fresh check of {workspace['name']} first.")
    expect(review.get_by_role('button', name='Activate', exact=True)).to_be_disabled()
    assert policy(h, proposal['id'])['status'] == 'proposed'
    review.get_by_role('button', name='Check rota', exact=True).click()
    expect(review.get_by_text('Waiting to check', exact=True)).to_be_visible()
    job = h.get('approver', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
    assert job['kind'] == 'feasibility'
    assert job['checked_rules'][0]['policy_id'] == proposal['id']
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['comparison_complete']
    review.get_by_role('button', name='Refresh checks', exact=True).click()
    acknowledgement = review.get_by_label(
        'Activate even though some affected rotas are not checked or still have problems.', exact=True)
    if adds_problems:
        count = len(result['new_or_worse_findings'])
        assert count > 0 and not result['no_new_problems']
        expect(review.get_by_text(adds_label(count), exact=True)).to_be_visible(timeout=15000)
        expect(acknowledgement).to_be_visible()
        acknowledgement.check()
    else:
        assert result['no_new_problems'] and result['new_or_worse_findings'] == []
        expect(review.get_by_text(
            no_new_label(result),
            exact=True)).to_be_visible(timeout=15000)
        expect(acknowledgement).not_to_be_visible()
    assert h.detail(workspace)['shifts'] == before
    review.get_by_role('button', name='Activate', exact=True).click()
    expect(review).to_have_count(0)
    assert decisions[-1]['payload']['check_job_ids'] == [job['id']]
    assert decisions[-1]['payload']['acknowledge_unresolved'] is adds_problems
    assert policy(h, proposal['id'])['status'] == 'active'
    assert h.detail(workspace)['shifts'] == before
    return before


def check_and_approve_in_panel(page, h, workspace, proposal, title):
    """B5 (FE-B, f1d80dcc4): a catalogue rule's approver decides in the docked panel; opening it starts the effect check of
    each overlapping draft (the same CP-SAT feasibility job) and Approve and activate stays off until it is known."""
    review = page.get_by_role('region', name=title, exact=True)
    effect = review.locator('.rc-effect')
    expect(effect.locator('li')).to_have_count(1)
    expect(effect.get_by_text(workspace['name'], exact=True)).to_be_visible()
    before = h.detail(workspace)['shifts']
    decisions = []
    page.on('request', lambda r: decisions.append(r.post_data_json) if r.method == 'POST'
            and r.url.endswith('/api/commands') and r.post_data_json['kind'] == 'policy.activate' else None)
    approve = review.get_by_role('button', name='Approve and activate', exact=True)
    expect(effect).to_contain_text('Waiting to check · ')
    shot(page, 'catalogue-review-controls-' + proposal['id'], full_page=False)
    expect(approve).to_be_disabled()
    # A check asked for and not started is shown as waiting; approving without it is offered only once it ended incomplete.
    expect(review.get_by_label('Approve without a complete check of these drafts', exact=True)).not_to_be_visible()
    assert policy(h, proposal['id'])['status'] == 'proposed'
    # "Waiting to check" shows before the job is stored; on a loaded runner the list can still be empty: wait for the job.
    page.wait_for_function("""async id => { const r = await fetch('/api/jobs?workspace_id=' + id);
        return r.ok && (await r.json()).jobs.length > 0; }""", arg=workspace['id'])
    job = h.get('approver', '/api/jobs?workspace_id=' + workspace['id'])['jobs'][0]
    assert job['kind'] == 'feasibility'
    assert job['checked_rules'][0]['policy_id'] == proposal['id']
    assert process_one(h.app.state.engine)
    result = h.get('approver', '/api/jobs/' + job['id'])['job']['result']
    assert result['comparison_complete']
    assert result['no_new_problems'] and result['new_or_worse_findings'] == []
    [row] = h.get('approver', f"/api/policies/{proposal['id']}/review")['effect']
    assert row['job_id'] == job['id'] and row['status'] == 'ready' and not row.get('adds'), row
    clears = row.get('clears') or 0
    words = f"Clears {clears} check{'' if clears == 1 else 's'}" if clears else 'No change'
    # The panel reads the effect again until it is known; a known effect needs no acknowledgement.
    expect(effect).to_contain_text(words, timeout=15000)
    expect(approve).to_be_enabled()
    expect(review.get_by_label('Approve without a complete check of these drafts', exact=True)).not_to_be_visible()
    assert h.detail(workspace)['shifts'] == before
    approve.click()
    expect(review).to_have_count(0)
    # The check job is the evidence; an independent approval sends no invented reason or acknowledgement (maintenance).
    assert decisions[-1]['payload'] == {'check_job_ids': [job['id']]}
    assert policy(h, proposal['id'])['status'] == 'active'
    assert h.detail(workspace)['shifts'] == before


@pytest.mark.parametrize('origin', ['new', 'revision', 'off'])
def test_catalogue_approval_checks_an_overlapping_rota_before_activation(browser, live_server, h, origin):
    flag(h)
    lines = [line({'max': {'value': 5, 'level': 'mandatory'}})]
    old = None
    if origin in {'revision', 'off'}:
        old = catalogue_rule(h, 'duties_per_week', lines)
    workspace = h.shift(h.workspace('Catalogue approval context'))['workspace']
    contexts = []
    try:
        if origin == 'revision':
            lines[0]['numbers']['max']['value'] = 4
        proposal = h.command('coordinator', 'rule.propose', {
            'catalogue_id': 'duties_per_week', 'lines': lines, 'on': origin != 'off',
            'expected_policy_id': old['id'] if old else None,
            'expected_revision': old['revision'] if old else None, 'reason': 'Catalogue proposal fixture'})['record']
        context, page = sign_in(browser, live_server, 'approver'); contexts.append(context)
        navigate(page, 'rules/duties_per_week')
        page.locator('.rc-waiting').get_by_role('button', name='Review and approve', exact=True).click()
        # The panel is named after the change: the one added or changed line, else the rule.
        title = {'new': 'Add a line for everyone · turns the rule on', 'revision': 'Change a line for everyone', 'off': 'Change to Duties per week'}[origin]
        check_and_approve_in_panel(page, h, workspace, proposal, title)
        page.reload()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        expect(page.locator('.rc-waiting')).to_have_count(0)
        # An approver reads On / Off beside the name (U-19), not a switch.
        expect(page.locator('#main .rc-summary b')).to_have_text('Off' if origin == 'off' else 'On')
        if old:
            assert policy(h, old['id'])['status'] == 'retired'
    finally:
        for context in contexts:
            context.close()


@pytest.mark.parametrize('scope', ['person', 'rota'])
def test_standalone_proposal_stays_discoverable_and_can_be_checked_in_catalogue_mode(browser, live_server, h, scope):
    flag(h)
    workspace = h.shift(h.workspace('Standalone approval context'))['workspace']
    title = 'Person rest proposal' if scope == 'person' else 'Rota rest proposal'
    viewport = {'width': 390, 'height': 844} if scope == 'rota' else None
    if scope == 'rota':
        h.command('coordinator', 'policy.create', {'name': title, 'owner': 'Synthetic department',
            'source': 'Rota proposal browser regression', 'starts_on': '2026-10-01',
            'definition': {'rule': {'kind': 'min_rest', 'hours': 12}, 'workspace_id': workspace['id']}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    try:
        if scope == 'person':
            navigate(page, 'people')
            page.get_by_role('button', name=re.compile('^More actions for Alex Morgan')).click()
            page.get_by_role('menuitem', name='Add a rule for this person', exact=True).click()
            form = page.get_by_role('dialog', name='Add rule', exact=True)
            form.get_by_label('Rule title', exact=True).fill(title)
            form.get_by_label('Policy owner', exact=True).fill('Synthetic department')
            form.get_by_label('Source or departmental decision', exact=True).fill('Person proposal browser regression')
            form.get_by_label('Effective from', exact=True).fill('2026-10-01')
            form.get_by_role('button', name='Propose rule', exact=True).click()
            expect(form).to_have_count(0)
        proposal = next(p for p in h.get('coordinator', '/api/catalogues')['policies'] if p['name'] == title)
        navigate(page, 'rules')
        menu = row_menu(page, pending(page, 1), title)
        expect(menu.get_by_role('menuitem', name='Withdraw', exact=True)).to_be_visible()
        menu.get_by_role('menuitem', name='Review', exact=True).click()
        review = page.get_by_role('dialog', name='Review ' + title, exact=True)
        expect(review.get_by_role('button', name='Activate', exact=True)).to_have_count(0)
        review.get_by_role('button', name='Close dialog', exact=True).click()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver', viewport)
    try:
        navigate(page, 'rules')
        panel = pending(page, 1)
        if scope == 'rota':
            shot(page, 'pending-rules-phone')
        row_menu(page, panel, title).get_by_role('menuitem', name='Review', exact=True).click()
        check_and_activate(page, h, workspace, proposal, title, adds_problems=scope == 'person')
        page.reload()
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'rules')
        page.get_by_role('button', name='More rule actions', exact=True).click()
        expect(page.get_by_role('menu', name='More rule actions', exact=True).get_by_role('menuitem', name='Other pending rules (1)', exact=True)).to_have_count(0)
        page.keyboard.press('Escape')
    finally:
        context.close()


def row_menu(page, panel, title):
    """A pending rule's decisions are in its row's ⋯ menu (U-04: no Actions column cut off a 640 px panel)."""
    panel.get_by_role('button', name=f'More actions for {title}', exact=True).click()
    return page.get_by_role('menu', name='Row actions')


def test_pending_rules_preserve_read_only_independence_withdraw_and_decline(browser, live_server, h):
    flag(h)
    records = [h.command(role, 'policy.create', {'name': title, 'owner': 'Synthetic department',
        'source': 'Pending access regression', 'starts_on': '2026-10-01',
        'definition': {'rule': {'kind': 'min_rest', 'hours': 12}}})['record']
        for role, title in [('admin', 'Own proposal'), ('coordinator', 'Other proposal')]]
    context, page = sign_in(browser, live_server, 'auditor')
    try:
        navigate(page, 'rules')
        panel = pending(page, 2)
        expect(panel.locator('[data-row]')).to_have_count(2)
        expect(panel.get_by_role('button', name='More actions for Own proposal', exact=True)).to_have_count(0)  # reads only
        panel.get_by_role('button', name='Own proposal', exact=True).click()
        expect(page.get_by_role('region', name='Own proposal', exact=True)).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'admin', viewport={'width': 1280, 'height': 900})
    try:
        navigate(page, 'rules')
        panel = pending(page, 2)
        # Every row's decisions are reachable without scrolling the 640 px panel sideways.
        assert panel.evaluate("p => [...p.querySelectorAll('.table-scroll')].every(s => s.scrollWidth <= s.clientWidth + 1)")
        menu = row_menu(page, panel, 'Own proposal')
        expect(menu.get_by_role('menuitem', name='Decline', exact=True)).to_have_count(0)
        menu.get_by_role('menuitem', name='Review', exact=True).click()
        review = page.get_by_role('dialog', name='Review Own proposal', exact=True)
        expect(review.get_by_role('button', name='Activate', exact=True)).to_have_count(0)
        review.get_by_role('button', name='Close dialog', exact=True).click()
        # Withdrawal records the proposer's reason; Undo puts it back waiting (U-15).
        menu = row_menu(page, pending(page, 2), 'Own proposal')
        menu.get_by_role('menuitem', name='Withdraw', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('No longer needed after review')
        dialog.get_by_role('button', name='Withdraw', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(pending_action(page, 1)).to_be_visible()
        page.keyboard.press('Escape')
        assert policy(h, records[0]['id'])['status'] == 'retired'
        page.locator('#toast-tray .toast', has_text='Proposal withdrawn.').get_by_role('button', name='Undo', exact=True).click()
        expect(pending_action(page, 2)).to_be_visible()
        page.keyboard.press('Escape')
        assert policy(h, records[0]['id'])['status'] == 'proposed'
        menu = row_menu(page, pending(page, 2), 'Other proposal')
        expect(menu.get_by_role('menuitem', name='Withdraw', exact=True)).to_have_count(0)
        menu.get_by_role('menuitem', name='Decline', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Not required after independent review')
        dialog.get_by_role('button', name='Decline', exact=True).click()
        expect(dialog).to_have_count(0)
        assert policy(h, records[1]['id'])['status'] == 'retired'
        expect(page.locator('#toast-tray .toast', has_text='Proposal declined.').get_by_role('button', name='Undo', exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role,width,scheme', [('approver', 1440, 'light'), ('approver', 390, 'dark'),
                                                ('coordinator', 1440, 'dark'), ('auditor', 390, 'light')])
def test_older_kind_proposal_is_a_row_of_waiting_for_approval_and_opens_its_review(browser, live_server, h, role, width, scheme):
    """An older-kind proposal is a row of the built-in Waiting for approval view, labelled Older rule; its count and the
    ⋯ badge count it once each, and its name opens the decision panel Other pending rules' Review opens, with the same
    permissions: Activate for another approver, none for its proposer, the rule sheet for a reader."""
    flag(h)
    workspace = h.shift(h.workspace('Waiting view context'))['workspace']
    title = 'Older rest proposal'
    proposal = h.command('coordinator', 'policy.create', {'name': title, 'owner': 'Synthetic department',
        'source': 'Waiting view regression', 'starts_on': '2026-10-01',
        'definition': {'rule': {'kind': 'min_rest', 'hours': 12}}})['record']
    assert proposal['status'] == 'proposed'
    context, page = sign_in(browser, live_server, role, {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme, reduced_motion='reduce')
        navigate(page, 'rules')
        row = page.locator(f'#main li[data-older-rule="{proposal["id"]}"]')
        expect(pending_action(page, 1)).to_be_visible()  # the ⋯ badge still says it waits
        page.keyboard.press('Escape')
        expect(row).to_have_count(0)  # All rules lists catalogue rules only
        page.locator('#main button.dt-saved').click()
        item = page.get_by_role('menuitemradio', name=re.compile('^Waiting for approval'))
        expect(item).to_contain_text('1')
        item.click()
        expect(row).to_be_visible()
        expect(row.locator('.badge', has_text='Older rule')).to_be_visible()
        expect(row.locator('.rt-waiting-chip')).to_have_text('Waiting for approval')
        expect(page.locator('#main .dt-count')).to_have_text(re.compile(r'^1 of \d+ rules?$|^1 rule$'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot(page, f'waiting-older-{role}-{width}-{scheme}')
        name = row.get_by_role('button', name=title, exact=True)
        name.focus()
        page.keyboard.press('Enter')
        if role == 'auditor':
            expect(page.get_by_role('region', name=title, exact=True)).to_be_visible()  # reads only
            return
        review = page.get_by_role('dialog', name='Review ' + title, exact=True)
        expect(review).to_be_visible()
        expect(review.get_by_role('cell', name=workspace['name'], exact=True)).to_be_visible()
        activate = review.get_by_role('button', name='Activate', exact=True)
        if role == 'coordinator':
            expect(activate).to_have_count(0)  # its proposer never approves it
        else:
            expect(activate).to_be_visible()
            expect(review.get_by_role('button', name='Decline', exact=True)).to_be_visible()
        review.get_by_role('button', name='Close dialog', exact=True).click()
        expect(review).to_have_count(0)
        assert policy(h, proposal['id'])['status'] == 'proposed'
    finally:
        context.close()


def test_pending_rules_decline_together_with_one_reason_and_one_undo(browser, live_server, h):
    """U-16: many waiting proposals (a Simplify blocker) are declined together, one reason and one Undo."""
    flag(h)
    records = [h.command('coordinator', 'policy.create', {'name': f'Waiting rule {n}', 'owner': 'Synthetic department',
        'source': 'Bulk decline', 'starts_on': '2026-10-01', 'definition': {'rule': {'kind': 'min_rest', 'hours': 10 + n}}})['record']
        for n in range(3)]
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        panel = pending(page, 3)
        panel.get_by_role('checkbox', name=re.compile('^Select all')).check()
        page.get_by_role('toolbar', name='Bulk actions').get_by_role('button', name='Decline 3').click()
        dialog = page.get_by_role('dialog', name='Decline 3 proposals')
        dialog.get_by_label('Reason', exact=True).fill('Replaced by the catalogue')
        dialog.get_by_role('button', name='Decline', exact=True).click()
        # One bulk change, as the legacy page declines several: Preview, then the armed same-button confirm.
        apply = page.locator('.dock-panel.bulk-panel .bulk-foot .button.primary')
        apply.click(); apply.click()
        toast = page.locator('#toast-tray .toast', has_text='3 proposals declined.')
        expect(toast).to_be_visible()
        assert all(policy(h, r['id'])['status'] == 'retired' for r in records)
        toast.get_by_role('button', name='Undo', exact=True).click()
        expect(pending_action(page, 3)).to_be_visible()
        page.keyboard.press('Escape')
        assert all(policy(h, r['id'])['status'] == 'proposed' for r in records)
    finally:
        context.close()
