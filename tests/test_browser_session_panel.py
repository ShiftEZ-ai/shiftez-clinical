"""The session side panel reviewed: the editor's tab bar is one opaque block with the panel head
while its form scrolls beneath; the open session's list row is marked (and follows the panel, and clears when it
closes); "‹ Back to" names the day and session; Participant and Participation role open the shared searchable picker
from the keyboard. Before/after screenshots: SESSION_PANEL_SHOTS=<folder>."""
import os
import re
import uuid
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import expect
from sqlalchemy import insert

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, open_rota, pick_view  # noqa: F401

pytestmark = pytest.mark.browser
EDGES = """tabs => {
  const body = tabs.closest('.dock-body'), r = tabs.getBoundingClientRect(), b = body.getBoundingClientRect();
  const inside = [r.top + 1, r.bottom - 1].flatMap(y => [0.1, 0.5, 0.9].map(f => document.elementFromPoint(r.left + r.width * f, y)));
  return {gap: r.top - (b.top + body.clientTop), scrolled: body.scrollTop,
          foreign: inside.filter(n => !tabs.contains(n)).map(n => n.tagName + '.' + n.className)};
}"""


def department(h):
    """A draft with sessions in two weeks and a department of 20 people (the participant list needs its search)."""
    s = h.seed
    with transaction(h.app.state.engine, write=True) as db:
        ids = [str(uuid.uuid4()) for _ in range(20)]
        db.execute(insert(m.Person), [{'id': p, 'organisation_id': s['organisation_id'], 'display_name': f'Dr Person {k:02}'} for k, p in enumerate(ids)])
        db.execute(insert(m.Membership), [{'person_id': p, 'department_id': s['department_id'], 'starts_on': date(2020, 1, 1), 'category_id': s['category']} for p in ids])
    w = h.workspace('Session panel rota')
    for day in (4, 5, 6):
        w = h.shift(w, f'2026-10-{day:02}T13:00:00+03:00', f'2026-10-{day:02}T17:00:00+03:00', people=s['people'][:2])['workspace']
    return w


def shot(page, name):
    if os.getenv('SESSION_PANEL_SHOTS'):
        folder = Path(os.environ['SESSION_PANEL_SHOTS']); folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f'{name}.png'))


def open_editor(page, w, scheme='light'):
    page.emulate_media(color_scheme=scheme)
    open_rota(page, w['id'])
    expect(page.locator('.health-pill.checking')).to_have_count(0, timeout=20000)
    pick_view(page, 'List')
    row = page.locator('tr.dt-open').first
    row.locator('td strong').first.click()
    details = page.locator('.wk-panel')  # the one session panel (S-09); Edit session opens the editor beside it
    expect(details.get_by_role('heading', name=re.compile(r'^Clinical review session · '))).to_be_visible()
    expect(details).to_contain_text('Sun 4 Oct')
    details.get_by_role('button', name='Edit session', exact=True).click()
    editor = page.locator('.dock-panel')
    expect(editor.get_by_role('heading', name='Edit session and participants')).to_be_visible()
    return row, editor


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_tabs_stay_one_block_with_the_head_while_the_form_scrolls(browser, live_server, h, scheme):
    w = department(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        _, editor = open_editor(page, w, scheme)
        for _ in range(3):
            editor.get_by_role('button', name='Add participant', exact=True).click()
        tabs = editor.get_by_role('tablist', name='Session editor')
        body = editor.locator('.dock-body')
        for width in (1440, 1280, 1100):
            page.set_viewport_size({'width': width, 'height': 800})
            for at in (40, 200, 10_000):
                body.evaluate('(b, at) => { b.scrollTop = at; }', at)
                edges = tabs.evaluate(EDGES)
                assert edges['scrolled'] > 0
                assert abs(edges['gap']) <= 0.5, f'{width}px, scrolled {at}: {edges["gap"]}px between the head and the tabs'
                assert not edges['foreign'], f'{width}px, scrolled {at}: {edges["foreign"]} shows through the tabs'
        page.set_viewport_size({'width': 1440, 'height': 800})
        body.evaluate('b => { b.scrollTop = 120; }')
        shot(page, f'after-session-editor-scrolled-{scheme}')
    finally:
        context.close()


def test_the_open_session_row_is_marked_and_follows_the_panel(browser, live_server, h):
    w = department(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        row, editor = open_editor(page, w)
        marked = page.locator('tr.dt-open.is-explained')
        expect(marked).to_have_count(1)
        expect(marked).to_have_attribute('data-row', row.get_attribute('data-row'))
        shot(page, 'after-open-row-marked')
        # Another session from the list: the mark moves with the panel.
        editor.get_by_role('button', name='Close panel').click()
        expect(page.locator('.dock-panel')).to_have_count(0)
        expect(marked).to_have_count(0)
        second = page.locator('tr.dt-open').nth(1)
        second.locator('td strong').first.click()
        session = page.locator('.wk-panel')  # the one session panel (S-09)
        expect(session.get_by_role('heading', name=re.compile(r'^Clinical review session · '))).to_be_visible()
        expect(session).to_contain_text('Mon 5 Oct')
        expect(marked).to_have_count(1)
        expect(marked).to_have_attribute('data-row', second.get_attribute('data-row'))
        page.locator('tr.dt-open').nth(2).locator('td strong').first.click()
        expect(session).to_contain_text('Tue 6 Oct')
        expect(marked).to_have_attribute('data-row', page.locator('tr.dt-open').nth(2).get_attribute('data-row'))
        page.keyboard.press('Escape')
        expect(session).to_have_count(0)
        expect(marked).to_have_count(0)
    finally:
        context.close()


def test_participant_and_role_pickers_work_by_keyboard(browser, live_server, h):
    w = department(h)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        _, editor = open_editor(page, w)
        editor.get_by_role('button', name='Add participant', exact=True).click()
        card = editor.locator('.nested-card').last
        person, role = card.get_by_label('Participant', exact=True), card.get_by_label('Participation role', exact=True)
        # Participant: a key typed on the field starts the search in the shared picker; Enter picks the match.
        person.focus()
        page.keyboard.type('Person 17')
        picker = page.locator('.picker')
        expect(picker.get_by_role('combobox', name='Search Participant')).to_be_focused()
        expect(picker.get_by_role('combobox', name='Search Participant')).to_have_value('Person 17')
        expect(picker.get_by_role('option')).to_have_count(1)
        page.keyboard.press('Enter')
        expect(picker).to_have_count(0)
        expect(person).to_be_focused()
        expect(person.locator('option:checked')).to_have_text('Dr Person 17')
        # Participation role: Enter opens it, arrows move, Enter picks; Esc closes without a change.
        role.focus()
        page.keyboard.press('Enter')
        expect(picker.get_by_role('listbox', name='Participation role')).to_be_visible()
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        expect(picker).to_have_count(0)
        expect(role).to_have_value('trainee')
        expect(role).to_be_focused()
        role.press('ArrowDown')
        expect(picker).to_have_count(1)
        page.keyboard.press('Escape')
        expect(picker).to_have_count(0)
        expect(role).to_have_value('trainee')
        expect(page.locator('.dock-panel')).to_have_count(1)
    finally:
        context.close()
