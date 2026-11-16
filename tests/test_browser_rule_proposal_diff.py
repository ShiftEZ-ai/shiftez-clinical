"""The proposal banner agrees with the draft and stored diff, including historical sparse lines."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, expect_loaded, live_server, navigate, sign_in  # noqa: F401
from .test_browser_rule_catalogue import flag, shot
from .test_rule_catalogue_api import entry
from .test_rule_proposal_diff import legacy_rule, weekly_lines

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_one_added_line_is_the_only_change_in_the_waiting_banner(browser, live_server, h, width, scheme):
    flag(h)
    before = weekly_lines(h)
    live = legacy_rule(h, 'duties_per_week', before)
    context, page = sign_in(browser, live_server, 'coordinator', {'width': width, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        main = page.locator('#main')
        main.get_by_role('button', name='New line: a person, rank, clinic or #tag').click()
        page.get_by_role('combobox', name='Search New line').fill('Continuity clinic')
        page.get_by_role('option', name=re.compile('^Continuity clinic')).first.click()
        panel = page.locator('.rc-line-panel')
        panel.get_by_label('Maximum', exact=True).fill('2')
        panel.get_by_role('button', name=re.compile('^Maximum: advisory')).click()
        page.get_by_role('menuitemradio', name='Mandatory', exact=True).click()
        panel.get_by_role('button', name='Keep change').click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        page.locator('.topbar').get_by_role('button', name='Propose 1 change', exact=True).click()
        dialog = page.get_by_role('dialog', name=re.compile('^Propose'))
        expect(dialog.locator('.rc-diff-added')).to_have_count(1)
        expect(dialog.locator('.rc-diff-changed')).to_have_count(0)
        dialog.get_by_label('Reason', exact=True).fill('One additional clinic limit')
        dialog.get_by_role('button', name='Propose', exact=True).click()
        waiting = main.locator('.rc-waiting')
        expect(waiting).to_be_visible()
        for reload in (False, True):
            if reload:
                page.reload()
                expect_loaded(page, 'rules/duties_per_week')
            expect(waiting).to_be_visible()
            expect(waiting.locator('tr.rc-prop-added')).to_have_count(1)
            expect(waiting.locator('tr.rc-prop-added')).to_be_visible()
            expect(waiting.locator('tr.rc-prop-added')).to_contain_text('Continuity clinic')
            expect(waiting.locator('tr.rc-prop-changed')).to_have_count(0)
            expect(waiting.locator('tr.rc-prop-removed')).to_have_count(0)
            expect(waiting.locator('.rc-prop-note')).to_have_count(0)  # no "note or settings" change claimed
        shot(page, f'proposal-diff-{width}-{scheme}')
    finally:
        context.close()
    waiting = entry(h, 'duties_per_week')['proposed']
    assert len(waiting['diff']['added']) == 1 and waiting['diff']['changed'] == []
    assert waiting['definition']['rule']['lines'][:14] == before
    assert entry(h, 'duties_per_week')['rule']['id'] == live['id']


def test_browser_line_diff_ignores_only_object_key_order(browser, live_server):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        result = page.evaluate("""async () => {
            const {lineChanges} = await window.importBundle('rule_catalogue.js');
            const before = {id:'legacy', note:'Approved note', weekdays:[0,1],
                numbers:{max:{value:5,level:'mandatory'}},
                settings:{max_duties:{window:'week',include_overtime:true}}};
            const reorder = x => Array.isArray(x) ? x.map(reorder) : x && typeof x === 'object'
                ? Object.fromEntries(Object.entries(x).reverse().map(([k,v]) => [k,reorder(v)])) : x;
            const edits = [{note:'New note'}, {settings:{max_duties:{window:'week',include_overtime:false}}},
                {numbers:{max:{value:4,level:'mandatory'}}}, {weekdays:[1,0]}, {source_bindings:{}}];
            return {unchanged:lineChanges([before],[reorder(before)]),
                edited:edits.map(edit => lineChanges([before],[{...before,...edit}]).map(c => c.kind))};
        }""")
        assert result == {'unchanged': [], 'edited': [['changed']] * 5}
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_proposal_lists_setting_changes_was_now(browser, live_server, h, scheme):
    """RU-22: a settings change reads setting by setting, was and now, where the approver decides (not "Settings changed")."""
    from .test_rule_catalogue_api import propose
    flag(h)
    before = weekly_lines(h)
    live = legacy_rule(h, 'duties_per_week', before, settings={'max_duties': {'include_overtime': False}})
    propose(h, 'duties_per_week', before, settings={'max_duties': {'include_overtime': True}},
            policy_id=live['id'], revision=live['revision'])
    context, page = sign_in(browser, live_server, 'approver')
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, 'rules/duties_per_week')
        waiting = page.locator('#main .rc-waiting')
        expect(waiting).to_be_visible()
        expect(waiting).not_to_contain_text('Settings changed')
        rows = waiting.locator('.rc-settings-diff tbody tr')
        expect(rows).to_have_count(1)
        expect(rows.first.locator('td').nth(1)).to_have_text('No')
        expect(rows.first.locator('td').nth(2)).to_have_text('Yes')
    finally:
        context.close()
