"""Docked item panels (side_panel.ts dockPanel): a person, request, rule or tag opens beside its list — never a modal
over it — named "Person 3 of 12", with ↑ ↓ through the list, the open item outlined, a resizable left edge, the
unsaved-changes guard on ↑ ↓, close and navigation, focus in and back, and a full-width sheet on a phone."""
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, open_config, planner_tool, press_escape, reconnect  # noqa: F401

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}


def people_row(page, index):
    return page.locator('#main .dt tbody tr[data-row]').nth(index)


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_person_panel_docks_beside_the_list_moves_with_arrows_and_returns_focus(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'coordinator')
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        first = people_row(page, 0)
        name = first.locator('strong').first.inner_text()
        first.locator('strong').first.click()
        panel = page.get_by_role('region', name=name)
        expect(panel).to_be_visible()
        # Docked, not modal: no dialog, the list beside it still works, and its row is outlined.
        expect(page.locator('dialog[open]')).to_have_count(0)
        expect(panel).not_to_have_attribute('aria-modal', 'true')
        expect(first).to_have_class(re.compile('is-explained'))
        main, side = page.locator('#main').bounding_box(), panel.bounding_box()
        assert main['x'] + main['width'] <= side['x'] + 1  # beside the page, not over it
        expect(panel.locator('.side-pos')).to_have_text(re.compile(r'^Person 1 of \d+$'))
        expect(panel.get_by_role('tab', selected=True)).to_be_focused()  # focus moved into the panel
        # ↓ opens the next person in the list's order; focus stays on ↓ so it can be pressed again.
        second = people_row(page, 1)
        other = second.locator('strong').first.inner_text()
        panel.get_by_role('button', name='Next person', exact=True).click()
        panel = page.get_by_role('region', name=other)
        expect(panel.locator('.side-pos')).to_have_text(re.compile(r'^Person 2 of \d+$'))
        expect(second).to_have_class(re.compile('is-explained'))
        expect(first).not_to_have_class(re.compile('is-explained'))
        expect(panel.get_by_role('button', name='Next person', exact=True)).to_be_focused()
        panel.get_by_role('button', name='Previous person', exact=True).click()
        expect(page.get_by_role('region', name=name).locator('.side-pos')).to_have_text(re.compile(r'^Person 1 of'))
        expect(page.get_by_role('button', name='Previous person', exact=True)).to_be_disabled()
        # Esc closes; focus goes back to the list it was opened from.
        press_escape(page)
        expect(page.locator('.dock-panel')).to_have_count(0)
        assert page.evaluate("() => !!document.activeElement.closest('#main .dt')")
        expect(first).not_to_have_class(re.compile('is-explained'))
    finally:
        context.close()


def test_panel_resizes_from_its_edge_and_keeps_the_width(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        people_row(page, 0).locator('strong').first.click()
        panel = page.locator('.dock-panel')
        grip = panel.get_by_role('separator', name='Resize panel')
        before = int(grip.get_attribute('aria-valuenow'))
        main = page.locator('#main').bounding_box()['width']
        grip.focus()
        page.keyboard.press('Shift+ArrowLeft')  # wider by a big step
        expect(grip).to_have_attribute('aria-valuenow', str(before + 64))
        # Dragging the edge left widens it too, and the page beside it gets narrower (it reflows, nothing overlaps).
        box = grip.bounding_box()
        page.mouse.move(box['x'] + 3, box['y'] + 100)
        page.mouse.down()
        page.mouse.move(box['x'] - 37, box['y'] + 100, steps=4)
        page.mouse.up()
        expect(grip).not_to_have_attribute('aria-valuenow', str(before + 64))
        wide = int(grip.get_attribute('aria-valuenow'))
        assert wide > before + 64
        assert page.locator('#main').bounding_box()['width'] < main - 64
        # Remembered for this panel in this browser.
        page.keyboard.press('Escape')
        page.reload()
        navigate(page, 'people')
        people_row(page, 0).locator('strong').first.click()
        expect(page.locator('.dock-panel').get_by_role('separator', name='Resize panel')).to_have_attribute('aria-valuenow', str(wide))
    finally:
        context.close()


def test_unsaved_tag_is_guarded_on_arrows_close_and_leaving(browser, live_server, h):
    for name in ('Alpha_team', 'Beta_team'):
        h.configure('tags', {'name': name, 'labels': ['people'], 'description': name + ' description'})
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        open_config(page, 'Tags')
        rows = page.locator('.config-pane tr[data-row]')
        expect(rows).to_have_count(2)
        first, second = ('Alpha_team', 'Beta_team') if 'Alpha_team' in rows.nth(0).inner_text() else ('Beta_team', 'Alpha_team')
        rows.nth(0).get_by_text(first + ' description').click()
        panel = page.get_by_role('region', name='#' + first)
        expect(panel.get_by_label('Name', exact=True)).to_be_focused()  # a form panel starts on its first field
        panel.get_by_label('Description', exact=True).fill('Changed, not saved')
        # ↓ with unsaved changes asks first; Keep editing stays on the tag.
        panel.get_by_role('button', name='Next tag', exact=True).click()
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        panel.get_by_role('button', name='Keep editing', exact=True).click()
        expect(panel.get_by_label('Description', exact=True)).to_have_value('Changed, not saved')
        # Discard goes on to the next tag.
        panel.get_by_role('button', name='Next tag', exact=True).click()
        panel.get_by_role('button', name='Discard changes', exact=True).click()
        panel = page.get_by_role('region', name='#' + second)
        expect(panel.locator('.side-pos')).to_have_text('Tag 2 of 2')
        expect(panel.get_by_label('Description', exact=True)).to_have_value(second + ' description')
        # Leaving the page with unsaved changes asks too, and the page stays.
        panel.get_by_label('Description', exact=True).fill('Changed again')
        page.evaluate("location.hash='people'")
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        expect(page.locator('#main')).to_have_attribute('data-loaded-view', 'configuration')
        # × asks the same; Discard closes, and nothing was saved.
        panel.get_by_role('button', name='Close panel', exact=True).click()
        panel.get_by_role('button', name='Discard changes', exact=True).click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        assert {t['name']: t['description'] for t in h.get('admin', '/api/catalogues')['tags']}[second] == second + ' description'
        # A clean panel closes with its page.
        page.locator('.config-pane tr[data-row]', has_text='Alpha_team').get_by_text('Alpha_team description').click()
        expect(page.get_by_role('region', name='#Alpha_team')).to_be_visible()
        navigate(page, 'people')
        expect(page.locator('.dock-panel')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_panel_is_a_full_width_sheet_on_a_phone(browser, live_server, h, scheme):
    context, page = sign_in(browser, live_server, 'coordinator', PHONE)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        card = page.locator('#main .dt-card[data-row]').first
        card.locator('.dt-card-title').click()
        panel = page.locator('.dock-panel')
        expect(panel).to_be_visible()
        box = panel.bounding_box()
        assert box['x'] == 0 and abs(box['width'] - PHONE['width']) <= 1
        expect(panel.get_by_role('separator', name='Resize panel')).to_be_hidden()  # no grip on a phone
        expect(page.locator('#main')).to_be_hidden()  # the page under the sheet is out of reach meanwhile
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel).to_have_count(0)
        expect(page.locator('#main')).to_be_visible()
    finally:
        context.close()


def side_pos(panel):
    """("Rule", 3, 52) from a panel's "Rule 3 of 52"."""
    noun, index, total = re.fullmatch(r'(\w+) (\d+) of (\d+)', panel.locator('.side-pos').inner_text()).groups()
    return noun, int(index), int(total)


def test_position_follows_the_list_when_it_is_drawn_again(browser, live_server, h):
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'people')
        rows = page.locator('#main .dt tbody tr[data-row]')
        total = rows.count()
        opened = people_row(page, 0).get_attribute('data-row')
        people_row(page, 0).locator('strong').first.click()
        panel = page.locator('.dock-panel')
        expect(panel.locator('.side-pos')).to_have_text(f'Person 1 of {total}')
        # Someone is added elsewhere; the live update draws the list again: "N of M" and ↑ ↓ follow the new list.
        h.configure('people', {'display_name': 'Aaron Aardvark (test)'})
        page.evaluate("dispatchEvent(new Event('online'))")
        expect(rows).to_have_count(total + 1)
        index = rows.evaluate_all('(rows, id) => rows.findIndex(r => r.dataset.row === id)', opened)
        expect(panel.locator('.side-pos')).to_have_text(f'Person {index + 1} of {total + 1}')
        expect(rows.nth(index)).to_have_class(re.compile('is-explained'))
        step, near = ('Previous person', index - 1) if index else ('Next person', 1)
        panel.get_by_role('button', name=step, exact=True).click()
        expect(rows.nth(near)).to_have_class(re.compile('is-explained'))
        expect(page.locator('.dock-panel .side-pos')).to_have_text(f'Person {near + 1} of {total + 1}')
    finally:
        context.close()


def test_rule_arrows_reach_rules_behind_show_more_and_follow_a_redraw(browser, live_server, h):
    from datetime import date
    from apps.api.db import transaction
    from apps.api import models as m
    ids = h.seed['identities']
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all([m.Policy(department_id=h.seed['department_id'], name=f'Scale rest {i:04}', kind='min_rest', status='active',
                             definition={'schema_version': 1, 'rule': {'kind': 'min_rest', 'hours': 11}, 'scope': {}, 'severity': 'mandatory', 'exception_allowed': False},
                             owner='Synthetic owner', source='Synthetic fixture', starts_on=date(2026, 1, 1), created_by=ids['coordinator'], approved_by=ids['approver'])
                    for i in range(53)])
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        navigate(page, 'rules')
        limits = page.locator('section.rule-group').filter(has=page.locator('.rule-group-name', has_text='Limits & rest'))
        expect(limits.locator('.rule-item')).to_have_count(50)
        more = limits.get_by_role('button', name=re.compile(r'^Show \d+ more$'))
        expect(more).to_be_visible()
        # The last rule drawn in the group: ↓ draws the rest of the group and opens the next rule.
        limits.locator('.rule-item').nth(49).locator('.rl-text').click()
        panel = page.locator('.dock-panel.rule-sheet')
        expect(panel.locator('.side-pos')).to_be_visible()
        noun, index, total = side_pos(panel)
        assert noun == 'Rule' and total > index
        panel.get_by_role('button', name='Next rule', exact=True).click()
        expect(panel.locator('.side-pos')).to_have_text(f'Rule {index + 1} of {total}')
        expect(more).to_have_count(0)
        expect(limits.locator('.rule-item').nth(50)).to_have_class(re.compile('is-explained'))
        expect(panel.get_by_role('button', name='Next rule', exact=True)).to_be_focused()
        # A rule added elsewhere: the list is drawn again and the open rule's place and count follow it.
        h.policy({'kind': 'min_rest', 'hours': 12})
        page.evaluate("dispatchEvent(new Event('online'))")
        expect(page.locator('.dock-panel.rule-sheet .side-pos')).to_have_text(re.compile(rf'^Rule \d+ of {total + 1}$'))
        expect(page.locator('.rule-item.is-explained')).to_have_count(1)
    finally:
        context.close()


@pytest.mark.parametrize('viewport,scheme', [(None, 'light'), (PHONE, 'dark')], ids=['desktop-light', 'phone-dark'])
def test_task_forms_dock_like_item_panels(browser, live_server, h, viewport, scheme):
    """The session editor and the rota's rules & targets are docked panels too (not modal side sheets)."""
    h.shift(h.workspace('Docked task forms'))
    context, page = sign_in(browser, live_server, 'coordinator', viewport)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'planner')
        add = page.get_by_role('button', name='Add session', exact=True)
        add.click()
        panel = page.get_by_role('region', name='Add session')
        expect(panel).to_be_visible()
        expect(page.locator('dialog[open]')).to_have_count(0)
        expect(page.locator('.shell > .dock-panel')).to_have_count(1)
        assert page.evaluate("() => !!document.activeElement.closest('.dock-panel')")  # focus moved in
        box = panel.bounding_box()
        if viewport:
            assert box['x'] == 0 and abs(box['width'] - PHONE['width']) <= 1
        else:
            main = page.locator('#main').bounding_box()
            assert main['x'] + main['width'] <= box['x'] + 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        # Its unsaved-changes guard: × asks, Discard closes, focus goes back to Add session.
        panel.get_by_label('Session start', exact=True).fill('2026-10-05T09:00')
        panel.get_by_role('button', name='Close panel', exact=True).click()
        expect(panel.get_by_role('alert')).to_contain_text('Not saved.')
        panel.get_by_role('button', name='Discard changes', exact=True).click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(add).to_be_focused()
        planner_tool(page, 'Workload targets for this rota')
        rules = page.get_by_role('region', name='Workload targets for this rota')
        expect(rules).to_be_visible()
        expect(page.locator('dialog[open]')).to_have_count(0)
        if not viewport:
            # A live update draws it again in place, without taking focus from the page beside it.
            add.focus()
            old = rules.locator('.dock-body').element_handle()
            reconnect(page)
            page.wait_for_function('node => !node.isConnected', arg=old)
            expect(rules.locator('.dock-body')).to_be_visible()
            expect(add).to_be_focused()
        page.keyboard.press('Escape')
        expect(page.locator('.dock-panel')).to_have_count(0)
    finally:
        context.close()
