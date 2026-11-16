"""Every dropdown reads well: a label never ends mid-word without an ellipsis and the full text (tooltip and accessible
name), a multiple choice shows the chips that fit then "+N", the shared picker keeps its own look inside dense grids, and a line editor
in a dialog sizes its columns to their content. Checked on key screens for several roles, desktop and phone, light
and dark."""
import re

import pytest
from playwright.sync_api import expect

from apps.api import models as m
from apps.api.db import transaction
from .test_browser import browser, live_server, sign_in, navigate, open_rule  # noqa: F401
from .test_rule_templates import propose, activate

pytestmark = pytest.mark.browser

LONG_NAME = 'Dr. Bruno Green, Consultant in General and Acute Care Medicine'
RANKS = ['Resident R1', 'Resident R2', 'Resident R3', 'Resident R4', 'Intern', 'Fellow', 'Registrar', 'Associate consultant']

# A visible dropdown whose label does not fit must end in an ellipsis and offer the whole text as a tooltip.
# A select's text is measured (its scrollWidth does not grow); the shared picker's summary is a plain span.
CLIPPED = """() => {
  const bad = [], ruler = document.createElement('canvas').getContext('2d');
  const shown = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  for (const s of document.querySelectorAll('select')) {
    const text = s.selectedOptions[0]?.textContent || '';
    if (!shown(s) || s.multiple || !text) continue;
    const cs = getComputedStyle(s);
    ruler.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const need = ruler.measureText(text).width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + (cs.appearance === 'none' ? 0 : 24);
    if (need <= s.clientWidth) continue;
    s.dispatchEvent(new PointerEvent('pointerover', {bubbles: true}));
    if (cs.textOverflow !== 'ellipsis' || s.title !== text) bad.push(`select ${s.getAttribute('aria-label') || s.name}: ${text}`);
  }
  for (const t of document.querySelectorAll('.pick-text')) {
    if (!shown(t) || t.scrollWidth <= t.clientWidth + 1) continue;
    const b = t.closest('button');
    if (getComputedStyle(t).textOverflow !== 'ellipsis' || !b?.title) bad.push(`picker ${b?.getAttribute('aria-label')}`);
  }
  return bad;
}"""


def no_clipped_dropdowns(page, where):
    assert page.evaluate(CLIPPED) == [], where


def lines_rule(h, ranks, person=None):
    s0, s1 = h.seed['services'][:2]
    lines = [{'amount': 2, 'category_ids': ranks[:5]}, {'category_ids': ranks[:1], 'ends_on': '2027-01-01', **({'person_id': person} if person else {})}]
    return activate(h, propose(h, {'kind': 'minimum_cover', 'minimum': 1}, {'service_ids': [s0, s1]}, name='Pooled morning target',
                               listed_only=True, per_person=lines))


def make_ranks(h):
    return [h.configure('categories', {'name': name, 'starts_on': '2020-01-01'})['record']['id'] for name in RANKS]


def open_change(page):
    navigate(page, 'rules')
    open_rule(page, 'Pooled morning target').get_by_role('button', name='Change…').click()
    dialog = page.get_by_role('dialog', name='Change Pooled morning target')
    expect(dialog.locator('.pp-row')).to_have_count(2)
    return dialog


@pytest.mark.parametrize('width,scheme', [(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')])
def test_no_visible_dropdown_label_is_clipped(browser, live_server, h, width, scheme):
    person = h.configure('people', {'display_name': LONG_NAME})['record']['id']
    lines_rule(h, make_ranks(h), person)
    viewport = {'width': width, 'height': 900 if width > 400 else 844}
    for role, screens in (('coordinator', ['rules', 'people', 'calendar', 'insights/workload']), ('admin', ['configuration']), ('staff', ['requests'])):
        context, page = sign_in(browser, live_server, role, viewport)
        page.emulate_media(color_scheme=scheme)
        try:
            for screen in screens:
                navigate(page, screen)
                no_clipped_dropdowns(page, f'{role} {screen}')
                toolbar = page.locator('#main .dt-toolbar').first
                if toolbar.count() and toolbar.get_by_role('button', name=re.compile('^Filter')).count():
                    toolbar.get_by_role('button', name=re.compile('^Filter')).click()
                    no_clipped_dropdowns(page, f'{role} {screen} filter')
                    page.keyboard.press('Escape')
            if role == 'coordinator':
                dialog = open_change(page)
                dialog.locator('.pp-editor').scroll_into_view_if_needed()
                no_clipped_dropdowns(page, 'rule change dialog')
                if width > 700:  # desktop: the line grid fits the dialog, no sideways scrolling
                    assert dialog.locator('.pp-grid').evaluate('g => g.scrollWidth <= g.clientWidth + 1')
                dialog.get_by_role('button', name='Cancel').click()
        finally:
            context.close()


def test_shared_picker_in_a_line_grid_summary_search_clear_keyboard_and_scale(browser, live_server, h):
    ranks = make_ranks(h)
    person = h.configure('people', {'display_name': LONG_NAME})['record']['id']
    with transaction(h.app.state.engine, write=True) as db:  # department scale: 150 people to choose from
        db.add_all([m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Scale person {n:03d}') for n in range(150)])
    lines_rule(h, ranks, person)
    context, page = sign_in(browser, live_server, 'coordinator')
    try:
        dialog = open_change(page)
        first, second = dialog.locator('.pp-row').nth(0), dialog.locator('.pp-row').nth(1)
        # Chips that fit the column, then "+N" for the rest (its tooltip lists them); the whole list as the name and tooltip.
        ranks_button = first.locator('.pick-button')
        chosen = sorted(RANKS[:5])  # in the list's order
        expect(ranks_button.locator('.pick-chip').first).to_have_text(chosen[0])
        shown = ranks_button.locator('.pick-chip:visible').count()
        assert 1 <= shown < 5
        expect(ranks_button.locator('.pick-more')).to_have_text(f'+{5 - shown}')
        expect(ranks_button.locator('.pick-more')).to_have_attribute('title', ', '.join(chosen[shown:]))
        expect(ranks_button).to_have_attribute('title', ', '.join(chosen))
        expect(ranks_button).to_have_accessible_name('Ranks: ' + ', '.join(chosen))
        # The list keeps its own look inside the grid: small checkboxes, every label shown, chosen ranks first, a search.
        ranks_button.click()
        picker = page.locator('.picker')
        expect(picker.locator('.picker-group').first).to_contain_text('Chosen')
        expect(picker.get_by_role('combobox', name='Search Ranks')).to_be_focused()
        for box in picker.locator('input[type=checkbox]').all():
            assert box.bounding_box()['height'] <= 20
        for label in picker.locator('.picker-label').all():
            assert label.bounding_box()['width'] > 20
        page.keyboard.type('r2')
        expect(picker.get_by_role('option')).to_have_count(1)
        picker.get_by_role('button', name=re.compile('^Clear')).click()
        page.keyboard.press('Escape')  # clears the search
        page.keyboard.press('Escape')  # closes the list, not the dialog
        expect(picker).to_have_count(0)
        expect(dialog).to_be_visible()
        expect(ranks_button).to_have_text('Any')
        expect(ranks_button).not_to_have_attribute('title', re.compile('.'))
        # A name longer than its column ends in an ellipsis; pointing at it shows the whole name.
        who = second.get_by_label('Who counts', exact=True)
        expect(who).to_have_css('text-overflow', 'ellipsis')
        who.hover()
        expect(who).to_have_attribute('title', LONG_NAME)
        # Keyboard (combobox/listbox): arrow opens, typing narrows 150+ people, Enter picks and focus returns.
        who.focus()
        page.keyboard.press('ArrowDown')
        expect(picker.get_by_role('listbox')).to_be_visible()
        expect(picker.get_by_role('option')).to_have_count(len(h.get('coordinator', '/api/catalogues')['people']) + 1)
        page.keyboard.type('Scale person 149')
        expect(picker.get_by_role('option')).to_have_count(1)
        page.keyboard.press('Enter')
        expect(picker).to_have_count(0)
        expect(who).to_be_focused()
        assert who.evaluate('s => s.selectedOptions[0].textContent') == 'Scale person 149'
        who.hover()
        expect(who).not_to_have_attribute('title', re.compile('.'))  # it fits now: no tooltip
    finally:
        context.close()
