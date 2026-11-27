"""Who is with whom in the browser: one team box (team_view.ts) with each member's own chip and relationship label, the
view's "Supervision teams" card property switching the box off, and a trainee (staff) on a phone seeing their supervisor."""
import re

import pytest
from playwright.sync_api import expect
from .test_browser import browser, live_server, sign_in, navigate, view_settings, close_popover, choose  # noqa: F401

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


def paired(h, w):
    trainee, supervisor, other = h.seed['people']
    typ = h.configure('assignment_types', {'name': 'Paired clinic', 'starts_on': '2020-01-01', 'definition': {
        'supervision': {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
                        'trainees_per_supervisor': 1}}})['record']['id']
    people = [{'person_id': trainee, 'start': START, 'end': END, 'participation': 'trainee', 'supervisor_person_id': supervisor},
              {'person_id': supervisor, 'start': START, 'end': END, 'participation': 'supervisor'},
              {'person_id': other, 'start': START, 'end': END}]
    return h.shift(w, typ=typ, people=people)['workspace']


def test_planner_shows_pairs_with_key_label_and_toggle(browser, live_server, h):
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    trainee, supervisor, _ = h.seed['people']
    paired(h, h.workspace('Pairs'))
    context, page = sign_in(browser, live_server, 'coordinator'); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'planner')
        team = page.locator('.wk-grid .team-box')
        expect(team).to_have_count(1)
        expect(team.locator('button.pchip')).to_have_count(2)
        chip = team.locator(draft_chip(trainee))
        # One team (D78) replaces the old matching dots and ↳: the supervisor's pill first in weight 600, no badge.
        expect(team.locator('.tm-badge, .wk-tag.sup, .wk-tag.tr')).to_have_count(0)
        assert int(team.locator('.tm-sup .pchip').evaluate('n=>getComputedStyle(n).fontWeight')) >= 600
        expect(chip).to_have_attribute('aria-label', re.compile(f'with {re.escape(names[supervisor])}'))
        expect(chip).to_have_attribute('aria-label', re.compile('^' + re.escape(names[trainee]) + ','))
        expect(chip).to_be_visible()
        supervising = team.locator(draft_chip(supervisor))
        expect(supervising).to_have_attribute('aria-label', re.compile('^' + re.escape(names[supervisor]) + ','))
        expect(supervising).to_be_visible()
        expect(page.locator(f'.wk-grid > .wk-row > .wk-cell > {DRAFT_CHIP}')).to_have_count(1)
        expect(supervising).to_have_attribute('aria-label', re.compile(f'supervising {re.escape(names[trainee])}'))
        expect(team).to_have_attribute('aria-label', re.compile(f'^{re.escape(names[supervisor])} supervising {re.escape(names[trainee])}'))
        expect(team).to_have_attribute('role', 'group')
        bar = page.locator('.planner-toolbar')
        view_settings(bar, 'Properties').get_by_role('button', name='Hide Supervision teams', exact=True).click()
        close_popover(page)
        # Off: no box, the members side by side in team order; each label still says who is with whom.
        expect(team).to_have_count(0)
        expect(page.locator(f'.pchip[aria-label*="with {names[supervisor]}"]')).to_have_count(1)
        plain = page.locator(f'.wk-grid .wk-cell > {DRAFT_CHIP} .pchip-name')
        expect(plain).to_have_count(3)
        order = plain.all_text_contents()
        assert order.index(names[trainee]) == order.index(names[supervisor]) + 1, order
        assert not errors
    finally:
        context.close()


def test_a_trainee_on_a_phone_sees_their_supervisor(browser, live_server, h):
    names = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    _, supervisor, other = h.seed['people']
    h.publish(paired(h, h.workspace('Published pairs')))
    context, page = sign_in(browser, live_server, 'staff', viewport={'width': 390, 'height': 844}); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        navigate(page, 'schedule/week/2026-10-04')
        item = page.locator('.ag-item').first
        # Their team: their supervisor and You (the item says it in full words), never "not on this session" for a
        # supervisor who is there.
        expect(item.locator('.ag-teams')).to_contain_text(names[supervisor])
        expect(item.locator('.team-box')).to_contain_text('You')
        expect(item).not_to_contain_text('not on this session')
        expect(item).not_to_contain_text(names[other])  # no other colleague
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('scheme', ['light', 'dark'])
def test_supervision_team_grouping_follows_each_sessions_recorded_supervisor(browser, live_server, h, scheme):
    trainee, first, second = h.seed['people']
    w = paired(h, h.workspace('Changing named supervision'))
    initial = h.detail(w)['shifts'][0]
    start, end = '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00'
    w = h.shift(w, start, end, typ=initial['assignment_type_id'], people=[
        {'person_id': trainee, 'start': start, 'end': end, 'participation': 'trainee', 'supervisor_person_id': second},
        {'person_id': second, 'start': start, 'end': end, 'participation': 'supervisor'}])['workspace']
    shifts = h.detail(w)['shifts']
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        page.emulate_media(color_scheme=scheme)
        navigate(page, f"schedule/people/2026-10-04?edit={w['id']}")
        choose(page.locator('.planner-toolbar'), 'Group', 'Group by', 'Supervision team')
        grid = page.locator('.people-grid')
        expect(grid.locator(f'.pg-row[data-row-person="{trainee}"]')).to_have_count(2)
        for supervisor, day in ((first, '2026-10-04'), (second, '2026-10-05')):
            shift = next(x for x in shifts if x['start'].startswith(day))
            chip = grid.locator(f'.pg-chip[data-person="{trainee}"][data-session="{shift["id"]}"]')
            expect(chip).to_have_count(1)
            group = chip.evaluate("n=>{let h=n.closest('.pg-row').previousElementSibling;while(h&&!h.classList.contains('pg-group'))h=h.previousElementSibling;return h?.textContent}")
            name = next(p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == supervisor)
            assert name in group and '8 h' in group
            chip.click()
            panel = page.locator('.wk-panel')
            expect(panel.get_by_role('heading', level=2)).to_have_text('Staff 01(test)')
            expect(panel.locator('.wk-panel-head')).to_contain_text('Mon' if day.endswith('05') else 'Sun')
            page.keyboard.press('Escape')
        choose(page.locator('.planner-toolbar'), 'Group', 'Group by', 'Rank')
        expect(grid.locator(f'.pg-row[data-row-person="{trainee}"]')).to_have_count(1)
        expect(grid.locator(f'.pg-row[data-row-person="{trainee}"] .pg-chip')).to_have_count(2)
    finally:
        context.close()
