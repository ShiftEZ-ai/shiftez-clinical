"""QA stream B2 (Rules & configuration): Fairness re-reads only the sections a Measure or Group change affects; exact
duplicate rules are retired from Group similar rules; Undo after proposing and after activating a rule (governed:
withdraw, retire-and-restore) and after removing a line; a proposed rule is brought into view; the settings save bar
on a phone; a save reloads only the lists it changed."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import route_fetch, browser, live_server, sign_in, navigate, open_config, save_dialog, open_rule  # noqa: F401
from .test_browser_fairness_dashboard import october, group_by, section, published_month
from .test_rule_templates import activate, propose

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def policies(h):
    # Retired versions load through the pageable history endpoint, not the operational catalogue (21e096a0d).
    return {p['id']: p for p in h.get('coordinator', '/api/catalogues')['policies']
            + h.get('coordinator', '/api/policies?status=retired')['policies']}


def test_fairness_measure_and_group_changes_reload_only_what_depends_on_them(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page,'insights/fairness')
        october(page)
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        asked = []
        page.on('request', lambda r: asked.append(r.url) if '/api/fairness/' in r.url else None)
        group_by(page, 'Rank')
        expect(section(page, 'distribution')).to_contain_text('By rank')
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        assert len(asked) == 1 and 'group_by=category_ids' in asked[0], asked  # trends per person reuse the ungrouped answer
        asked.clear()
        page.get_by_label('Measure', exact=True).select_option('hours')
        expect(section(page, 'distribution').get_by_role('button', name=re.compile('^Alex Morgan: 12 h'))).to_be_visible()
        expect(page.locator('.fx-page[data-ready=true]')).to_have_count(1)
        # Only the distribution depends on the measure (one read serves the people's rows too); goals, trends and exposure don't.
        assert len(asked) == 1 and '/distribution?' in asked[0] and 'metric=hours' in asked[0] and 'group_by=category_ids' in asked[0], asked
        asked.clear()
        page.get_by_label('Measure', exact=True).select_option('duties')  # back: every answer is still held
        expect(section(page, 'distribution').get_by_role('button', name=re.compile('^Alex Morgan: 3 duties'))).to_be_visible()
        assert asked == [], asked
    finally:
        context.close()


def test_duplicate_rules_are_retired_from_group_similar_rules_with_undo(browser, live_server, h):
    first = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest between duties'))
    copies = [activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name=f'Rest (imported copy {i})')) for i in (1, 2)]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        section_ = page.get_by_role('dialog', name='Group similar rules').get_by_role('region', name='Duplicate rules')
        expect(section_).to_contain_text('An approver retires them')
        expect(section_.get_by_role('button', name=re.compile('^Retire'))).to_have_count(0)
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        page.get_by_role('button', name='Group similar rules').click()
        section_ = page.get_by_role('dialog', name='Group similar rules').get_by_role('region', name='Duplicate rules')
        # One row per rule kept, retiring all its copies.
        name = '2 copies of Rest between duties'
        expect(section_.locator('.group-row')).to_have_count(1)
        expect(section_.locator('.change-summary')).to_contain_text('3 rules say exactly the same')
        sent = []
        page.on('request', lambda r: sent.append(r.post_data_json) if r.url.endswith('/api/commands') and r.method == 'POST' else None)
        section_.get_by_role('button', name='Retire ' + name).click()  # D42: reversible, acts at once with Undo
        expect(section_.locator('.badge', has_text='Retired')).to_be_visible()
        assert [b['kind'] for b in sent] == ['policy.retire'] * 2
        assert all(b['payload']['reason'].startswith('Duplicate of Rest between duties: ') for b in sent)
        now = policies(h)
        assert [now[c['id']]['status'] for c in copies] == ['retired'] * 2 and now[first['id']]['status'] == 'active'
        section_.get_by_role('button', name='Undo retiring ' + name).click()
        expect(section_.get_by_role('button', name='Retire ' + name)).to_be_visible()
        assert [policies(h)[c['id']]['status'] for c in copies] == ['active'] * 2
    finally:
        context.close()


def test_a_proposed_rule_comes_into_view_and_undo_withdraws_it(browser, live_server, h):
    for i in range(30):  # enough rules that the new one starts below the fold
        activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name=f'Rest rule {i:02}'))
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        page.get_by_label('Find rules', exact=True).fill('Rest rule 0')  # a filter that hides the new rule
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Rule title', exact=True).fill('Zz newest weekly limit')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        form.get_by_role('radio', name='Days in a row', exact=True).check()
        save_dialog(form, 'Propose rule')
        row = page.locator('.rule-item').filter(has_text='Zz newest weekly limit')
        expect(row).to_be_focused()
        expect(row).to_have_class(re.compile('rule-new'))
        expect(row).to_be_in_viewport()
        expect(page.get_by_label('Find rules', exact=True)).to_have_value('')  # cleared to show it
        made = next(p for p in policies(h).values() if p['name'] == 'Zz newest weekly limit')
        assert made['status'] == 'proposed'
        page.locator('.toast').filter(has_text='Rule proposed.').get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Proposal withdrawn.')).to_be_visible()
        assert policies(h)[made['id']]['status'] == 'retired'
    finally:
        context.close()


def test_activation_undo_retires_the_rule_and_restores_the_version_it_replaced(browser, live_server, h):
    old = activate(h, propose(h, {'kind': 'min_rest', 'hours': 11}, name='Rest'))
    new = propose(h, {'kind': 'min_rest', 'hours': 12}, name='Rest', replaces_id=old['id'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        navigate(page, 'rules')
        page.locator('.rule-item').filter(has_text='Rest').get_by_role('button', name=re.compile('^Review')).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Independent review')
        dialog.get_by_role('button', name='Activate', exact=True).click()
        toast = page.locator('.toast').filter(has_text='Rule activated.')
        expect(toast).to_be_visible()
        assert policies(h)[new['id']]['status'] == 'active' and policies(h)[old['id']]['status'] == 'retired'
        toast.get_by_role('button', name='Undo').click()
        expect(page.get_by_text('Activation undone: the rule is retired and what it replaced applies again.')).to_be_visible()
        now = policies(h)
        assert now[new['id']]['status'] == 'retired' and now[old['id']]['status'] == 'active'
    finally:
        context.close()


def test_removing_a_line_can_be_undone_before_saving(browser, live_server, h):
    """Exceptions removed in the change dialog are not saved until it is proposed: Cancel (Discard changes) brings every
    one back, in its place."""
    a, b, c = h.seed['people']
    rule = activate(h, propose(h, {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, name='Weekly duties', listed_only=True,
                               per_person=[{'person_id': a, 'amount': 3}, {'person_id': b, 'amount': 4}, {'person_id': c}]))
    who = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        sheet = open_rule(page, 'Weekly duties')
        sheet.locator('.rl-sheet-foot').get_by_role('button', name='Change…').click()
        dialog = page.get_by_role('dialog', name='Change Weekly duties')
        rows = dialog.locator('.rl-exc')
        expect(rows.locator('strong')).to_have_text([who[a], who[b], who[c]])
        dialog.get_by_role('button', name=f'Remove line for {who[b]}').click()
        expect(rows.locator('strong')).to_have_text([who[a], who[c]])
        dialog.get_by_role('button', name=f'Remove line for {who[c]}').click()
        expect(rows.locator('strong')).to_have_text([who[a]])
        # The one-row editor's Remove asks first (same button), and also only changes the dialog.
        dialog.get_by_role('button', name=f'Edit line for {who[a]}').click()
        remove = dialog.locator('.rl-exc.open').get_by_role('button', name='Remove line')
        remove.click()
        expect(rows).to_have_count(1)
        dialog.locator('.rl-exc.open').get_by_role('button', name='Remove?').click()
        expect(rows).to_have_count(0)
        # Nothing was saved: closing asks, and discarding keeps the rule as it was.
        dialog.locator('.form-footer').get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog.get_by_role('alert')).to_contain_text('Not saved.')
        dialog.get_by_role('button', name='Discard changes').click()
        expect(dialog).to_have_count(0)
        assert not [p for p in policies(h).values() if p['definition'].get('replaces_id') == rule['id']]
        sheet.locator('.rl-sheet-foot').get_by_role('button', name='Change…').click()
        expect(rows.locator('strong')).to_have_text([who[a], who[b], who[c]])
        expect(rows.locator('small')).to_have_text(['maximum 3', 'maximum 4', 'the rule’s numbers'])  # back in its place
    finally:
        context.close()


def test_auto_schedule_settings_save_from_the_bottom_on_a_phone(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'admin', PHONE)
    try:
        open_config(page, 'Auto-schedule & scoring')
        pane = page.locator('.config-pane')
        bar = pane.get_by_role('region', name='Unsaved changes')
        expect(bar).to_be_hidden()
        pane.get_by_role('tab', name='Advanced', exact=True).click()
        field = pane.get_by_label('People tried first for each place', exact=True)
        field.scroll_into_view_if_needed()
        field.fill('5')
        expect(bar).to_be_visible()
        expect(bar).to_be_in_viewport()  # pinned above the status footer
        footer = page.locator('.app-footer').bounding_box()
        assert bar.bounding_box()['y'] + bar.bounding_box()['height'] <= footer['y'] + 1
        bar.get_by_role('button', name='Discard').click()
        bar.get_by_role('button', name='Discard?').click()
        expect(bar).to_be_hidden()
        expect(pane.get_by_label('People tried first for each place', exact=True)).to_have_value('3')
        pane.get_by_label('People tried first for each place', exact=True).fill('4')
        bar.get_by_role('button', name='Save defaults', exact=True).click()
        dialog = page.get_by_role('dialog')
        dialog.get_by_label('Reason', exact=True).fill('Search a little wider')
        dialog.get_by_role('button', name='Save defaults', exact=True).click()
        expect(bar).to_be_hidden()
        assert h.get('admin', '/api/me')['department']['generation_defaults']['shortlist_width'] == 4
    finally:
        context.close()


def test_a_rule_save_reloads_only_the_lists_it_changed(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        asked = []
        page.on('request', lambda r: asked.append(r.url.split('/api/')[-1]) if '/api/' in r.url else None)
        page.get_by_role('button', name='Add rule', exact=True).click()
        form = page.get_by_role('dialog')
        form.get_by_label('Rule title', exact=True).fill('Targeted refresh rule')
        form.get_by_label('Source or departmental decision', exact=True).fill('Constructed browser test')
        form.get_by_role('radio', name='Minimum rest', exact=True).check()
        with page.expect_response(lambda r: '/api/catalogues?known=' in r.url) as delta:
            save_dialog(form, 'Propose rule')
        assert set(delta.value.json()) == {'policies', '_versions'}  # only the rules came back
        expect(page.locator('.rule-item').filter(has_text='Targeted refresh rule')).to_be_visible()
        assert 'catalogues' not in asked, asked  # never the whole set again
        assert not [u for u in asked if u.startswith(('workspaces?limit=50', 'swaps?', 'requests?'))], asked  # counts a rule can't change (unread: the live pulse)
        assert not [u for u in asked if u.startswith('table-views')], asked  # saved views are read once per list
    finally:
        context.close()


def test_staff_read_their_own_wording_and_a_perfect_score_has_no_empty_breakdown(browser, live_server, h):
    published_month(h)
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    try:
        navigate(page,'insights/fairness')
        october(page)
        expect(section(page, 'distribution').get_by_role('heading', name='Your duties', exact=True)).to_be_visible()
        expect(section(page, 'people').get_by_role('heading', name='Your duties by month', exact=True)).to_be_visible()
    finally:
        context.close()
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        def perfect(route):
            data = route_fetch(route).json()
            data['schedule'] = {**(data.get('schedule') or {}), 'score': 100, 'publishable': True, 'why': [], 'components': {}, 'people': {}}
            route.fulfill(json=data)
        page.route('**/api/fairness/goals?*', perfect)
        navigate(page,'insights/fairness')
        october(page)
        score = section(page, 'score')
        expect(score.locator('.fx-big')).to_have_text('100')
        expect(score.get_by_role('button', name='Why this score')).to_have_count(0)
    finally:
        context.close()
