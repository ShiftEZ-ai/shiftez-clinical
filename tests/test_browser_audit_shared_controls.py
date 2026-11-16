"""Independent edits and locked values retain their shared-control protections."""
import re
import pytest
from playwright.sync_api import expect

from .test_browser import (session_action, browser, live_server, sign_in, navigate, open_config, open_rota,
                           open_session, save_dialog, toolbar, planner_tool)  # noqa: F401
from .rules_support import rule_action, rule_row

pytestmark = pytest.mark.browser


def test_saving_a_view_does_not_clear_an_unsaved_tag_guard(browser, live_server, h):
    h.configure('tags', {'name': 'GuardAudit', 'labels': ['people'],
                         'description': 'Original description'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_config(page, 'Tags')
        page.locator('.config-pane tr[data-row]').get_by_text('Original description', exact=True).click()
        panel = page.get_by_role('region', name='#GuardAudit', exact=True)
        panel.get_by_label('Description', exact=True).fill('Unsaved description')

        toolbar(page.locator('.config-pane'), 'Saved views').get_by_role('menuitem', name='New view…').click()
        dialog = page.get_by_role('dialog', name='New view', exact=True)
        dialog.get_by_label('View name', exact=True).fill('Independent view')
        save_dialog(dialog, 'Save view')
        expect(page.get_by_text('View saved.', exact=True)).to_be_visible()
        expect(panel.get_by_label('Description', exact=True)).to_have_value('Unsaved description')

        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        panel.get_by_role('button', name='Keep editing', exact=True).click()
        expect(panel.get_by_label('Description', exact=True)).to_have_value('Unsaved description')
        assert h.get('admin', '/api/catalogues')['tags'][0]['description'] == 'Original description'
        assert h.get('coordinator', '/api/table-views?table=catalogue-tags')['views'][0]['name'] == 'Independent view'
        save_dialog(panel, 'Save tag')
        assert h.get('admin', '/api/catalogues')['tags'][0]['description'] == 'Unsaved description'
        page.locator('.config-pane tr[data-row]').get_by_text('Unsaved description', exact=True).click()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        page.locator('.config-pane tr[data-row]').get_by_text('Unsaved description', exact=True).click()
        panel.get_by_label('Description', exact=True).fill('Discard this edit')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        panel.get_by_role('button', name='Discard changes', exact=True).click()
        expect(panel).to_have_count(0)
        assert h.get('admin', '/api/catalogues')['tags'][0]['description'] == 'Unsaved description'
    finally:
        context.close()


def test_expanded_tag_pills_keep_their_open_action(browser, live_server, h):
    person = h.seed['people'][0]
    for name in ('Audit_One', 'Audit_Two', 'Audit_Three'):
        tag = h.configure('tags', {'name': name, 'labels': ['people']})['record']
        h.command('admin', 'tag.members', {'add': [{'kind': 'people', 'id': person}],
                                          'reason': 'Synthetic tag expansion audit'}, tag['id'], tag['revision'])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        row = page.locator('#main tr[data-row="' + person + '"]')
        more = row.get_by_role('button', name='1 more tag: show all 3', exact=True)
        more.click()
        expanded = page.get_by_role('dialog', name='All tags', exact=True)
        expect(expanded.get_by_role('button', name='Tag Audit_Two, 1 member', exact=True)).to_be_visible()
        expanded.get_by_role('button', name='Tag Audit_Two, 1 member', exact=True).click()
        details = page.get_by_role('dialog', name='Tag Audit_Two', exact=True)
        expect(details.get_by_role('button', name='Open tag', exact=True)).to_be_visible()
        details.get_by_role('button', name='Open tag', exact=True).click()
        panel = page.get_by_role('region', name='#Audit_Two', exact=True)
        expect(panel).to_be_visible()
        panel.get_by_role('button', name='Close panel', exact=True).click()
        more.click()
        expanded.get_by_role('button', name='Tag Audit_Two, 1 member', exact=True).press('Enter')
        expect(details.get_by_role('button', name='Open tag', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(details).to_have_count(0)
        expect(more).to_be_focused()
        expect(more).to_have_attribute('aria-expanded', 'false')
        more.press('Enter')
        expanded.get_by_role('button', name='Tag Audit_Two, 1 member', exact=True).press('Enter')
        expect(details.get_by_role('button', name='Open tag', exact=True)).to_be_visible()
        details.get_by_role('button', name='Open tag', exact=True).press('Enter')
        expect(panel).to_be_visible()
    finally:
        context.close()


def test_nested_rank_form_save_or_cancel_leaves_person_panel_clean(browser, live_server, h):
    person = h.configure('people', {'display_name': 'Nested form audit'})['record']
    category = h.get('admin', '/api/catalogues')['categories'][0]
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        row = page.locator('#main tr[data-row="' + person['id'] + '"]')
        for save in (False, True):
            row.locator('strong').first.click()
            panel = page.get_by_role('region', name='Nested form audit', exact=True)
            panel.get_by_role('tab', name='Overview', exact=True).click()
            panel.get_by_role('button', name='Add rank', exact=True).click()
            dialog = page.get_by_role('dialog', name='Add rank', exact=True)
            if save:
                dialog.get_by_label('Rank', exact=True).select_option(category['id'])
                save_dialog(dialog, 'Add rank')
                expect(panel).to_contain_text(category['name'])
            else:
                dialog.get_by_role('button', name='Cancel', exact=True).click()
                expect(dialog).to_have_count(0)
            panel.get_by_role('button', name='Close panel', exact=True).click()
            expect(panel).to_have_count(0)
        assert any(m['person_id'] == person['id'] for m in h.get('admin', '/api/catalogues')['memberships'])
    finally:
        context.close()


def test_readonly_staffing_dates_do_not_expose_calendar_controls(browser, live_server, h):
    w = h.shift(h.workspace('Locked staffing date audit'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_rota(page, w['id'])
        open_session(page)
        session_action(page, 'Find staffing or replacement options')
        panel = page.locator('.dock-panel')
        start = panel.get_by_label('Proposed reserved start', exact=True)
        expect(start).to_have_attribute('readonly', '')
        expect(panel.locator('.date-box .date-open').first).to_be_hidden()
        expect(panel.locator('.date-box .date-open').nth(1)).to_be_hidden()
        expect(panel.get_by_label('Proposed reserved start time', exact=True)).to_have_attribute('readonly', '')
        assert h.detail(w)['workspace']['revision'] == w['revision']
    finally:
        context.close()


def test_saving_inline_rota_rule_keeps_other_edits_and_uses_current_revision(browser, live_server, h):
    """Rules for a rota change in the rules panel (the server's rule map; the dock's own list is retired): each save
    uses the rota's current revision, an unsaved change is guarded, and a second rule changes after the first."""
    duties = h.policy({'kind': 'max_duties', 'maximum': 5, 'window': 'week'})
    hours = h.policy({'kind': 'max_hours', 'hours': 40, 'window': 'week'})
    w = h.shift(h.workspace('Independent inline rule edits'))['workspace']
    context, page = sign_in(browser, live_server, 'coordinator')
    why = 'Agreed with the department for this rota'

    def change(name, value, close=False):
        row = rule_row(panel, name)
        if not row.get_by_role('form', name='Adjust for this rota').count():
            rule_action(row, 'Adjust for this rota')
        row.get_by_label(f'New number for “{name}” in this rota').fill(value)
        if close:  # typed and not saved: closing asks first
            panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
            expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
            panel.get_by_role('button', name='Keep editing', exact=True).click()
        row.get_by_label('Justification', exact=True).fill(why)
        row.get_by_role('button', name='Use for this rota', exact=True).click()
        # Saved: the row says it changed for this rota; the opened row says from and to which number (Why v2, 452627a96).
        saved = rule_row(panel, name)
        expect(saved.locator('.rk-row-head')).to_contain_text('Changed for this rota.')
        expect(saved).to_contain_text(re.compile(rf'(Lowered|Raised) from \d+ to {value} for this rota'))
    try:
        open_rota(page, w['id'])
        expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
        planner_tool(page, 'Rules in this rota')
        panel = page.get_by_role('dialog', name='Rules in this rota')
        change(duties['name'], '4')
        change(hours['name'], '36', close=True)
        change(duties['name'], '3')
        panel.get_by_role('button', name='Close rules panel (Esc)', exact=True).click()
        expect(panel).to_have_count(0)
        amounts = {r['policy_id']: r['amount'] for r in h.detail(w)['workspace']['rule_adjustments']}
        assert amounts[duties['id']] == 3 and amounts[hours['id']] == 36
    finally:
        context.close()
