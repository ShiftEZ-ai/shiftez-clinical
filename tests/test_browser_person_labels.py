"""Person labels (D75): the department's name default names people on every screen through the one label renderer, and
a shown label that is not the full name keeps the full name in its tip and in what a screen reader hears. People,
Requests, Home (Today and the duty's team), the person card's team and Where is everyone, at 1440 and 390, light and dark (LABEL_SHOTS=<folder> keeps a screenshot of each).
Fictional names only."""
import os
import re

import pytest
from playwright.sync_api import expect

from .test_browser import browser, live_server, sign_in, navigate, toolbar  # noqa: F401
from .test_browser import whole_list
from .test_browser_requests import leave
from .test_browser_home import populated
from .test_browser_where_is_everyone import serve, row
from .test_browser_person_card import TRAINEE, fixture as supervised_duty

from .test_browser import DRAFT_CHIP, draft_chip  # noqa: F401
pytestmark = pytest.mark.browser
FULL = 'Dr Fictional Diego Lewis'
SHORT = 'Dr F. Lewis'


def shot(page, screen, view, scheme):
    if os.getenv('LABEL_SHOTS'):
        size = 'desktop' if view['width'] > 720 else 'phone'
        page.screenshot(path=os.path.join(os.environ['LABEL_SHOTS'], f'{screen}--short-label--{size}-{scheme}.png'))


def labelled(name, full):
    """A name slot: the shorter label shown, the full name as its tip and read out after it."""
    expect(name).to_have_attribute('data-tip', full)
    expect(name.locator('.sr-only')).to_have_text(f', {full}')
    shown = name.evaluate("n=>[...n.childNodes].filter(c=>c.nodeType===3).map(c=>c.textContent).join('')")
    assert shown != full and shown.endswith(full.split()[-1]), shown


VIEWS = [(view, scheme) for view in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}) for scheme in ('light', 'dark')]


def short_names(h):
    department = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.display', {'names_default': 'short', 'reason': 'Short names for the label test'},
              department['id'], department['revision'])
    created = h.configure('people', {'display_name': FULL})
    return (created.get('record') or created)['id']


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_people_list_shows_the_label_and_keeps_the_full_name(browser, live_server, h, view, scheme):
    short_names(h)
    context, page = sign_in(browser, live_server, 'admin', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        page.get_by_label('Search people', exact=True).fill('Fictional Diego Lewis')
        name = page.locator('#main .person-cell strong').filter(has_text=SHORT)
        expect(name).to_have_count(1)
        shown = name.locator(f'span[data-tip="{FULL}"]')
        expect(shown).to_contain_text(SHORT)
        expect(shown.locator('.sr-only')).to_have_text(f', {FULL}')  # read out after the label
        shot(page, 'people', view, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_request_list_links_the_person_with_the_full_name_in_its_accessible_name(browser, live_server, h, view, scheme):
    person = short_names(h)
    leave(h, role='coordinator', person=person)
    leave(h, role='coordinator')  # a second person, so the list names who each request is for
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'requests')
        whole_list(page)  # the whole list, not the decider's Waiting for you
        link = page.locator('#main .person-link', has_text=SHORT).first
        expect(link).to_have_accessible_name(f'{SHORT}, {FULL}')
        expect(link).to_have_attribute('data-tip', FULL)
        expect(page.locator('#main .person-link', has_text=re.compile(r'^Dr Fictional Al'))).to_have_count(0)
        shot(page, 'requests', view, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_home_today_names_people_on_duty_with_the_label(browser, live_server, h, view, scheme):
    short_names(h)
    populated(h)
    full = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        today = page.locator('#main .home-today')
        today.get_by_role('button', name='Show people', exact=True).click()
        chips = today.locator('.home-today-people .rp.person')
        expect(chips.first).to_be_visible()
        for i in range(chips.count()):
            chip = chips.nth(i)
            name = next(n for n in full.values() if chip.get_attribute('data-tip').startswith(n + ' · '))
            expect(chip.locator('.sr-only')).to_have_text(f', {name}')
            assert name.split()[-1] in chip.inner_text() and not chip.inner_text().startswith(name)
        shot(page, 'home', view, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_where_is_everyone_rows_show_the_label(browser, live_server, h, view, scheme):
    short_names(h)
    context, page = sign_in(browser, live_server, 'coordinator', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        people, _ = serve(page, h)
        full = {p['id']: p['display_name'] for p in h.get('coordinator', '/api/catalogues')['people']}
        navigate(page, 'people')
        pid = next(p for p in people if len(full[p].split()) > 1)
        toolbar(page, 'Saved views').get_by_role('menuitemradio', name=re.compile(r'^Where is everyone')).click()
        who = row(page.locator('#main'), pid).locator('.tl-who')
        name = who.locator('.tl-nm b')
        expect(name).to_have_attribute('data-tip', full[pid])
        shown = name.evaluate('n=>n.firstChild.textContent')
        assert shown != full[pid] and shown.endswith(full[pid].split()[-1]), shown
        if view['width'] > 720:
            expect(who).to_have_accessible_name(re.compile('^' + re.escape(f'{shown}, {full[pid]}: open')))  # says both
        else:  # a phone: the name is not a button (M29); the full name is read after the label
            expect(name).to_contain_text(f'{shown}, {full[pid]}')
        shot(page, 'where-is-everyone', view, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_duty_team_shows_the_label_and_says_the_full_names(browser, live_server, h, view, scheme):
    short_names(h)
    supervised_duty(h)
    short = 'F. Trainee'  # "Fictional Card Trainee" with the department's Initial + family name
    context, page = sign_in(browser, live_server, 'staff', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'home')
        team = page.locator('.home-hero .team-box:not(.lg-mark)')
        expect(team).to_have_attribute('aria-label', re.compile(f'supervising {TRAINEE}'))  # the group says full names
        trainee = team.get_by_role('button', name=f'{short}, {TRAINEE}', exact=True)
        expect(trainee).to_have_text(short)
        shot(page, 'home-team', view, scheme)
        trainee.click()
        card = page.get_by_role('region', name=TRAINEE, exact=True)  # the hub and the card's heading keep the full name
        box = card.locator('.pc .team-box:not(.lg-mark)')
        expect(box).to_have_attribute('aria-label', re.compile(f'supervising {TRAINEE}'))
        expect(box.locator('.pchip-name', has_text=re.compile('^' + re.escape(short) + '$'))).to_have_count(1)
        shot(page, 'person-card-team', view, scheme)
    finally:
        context.close()


@pytest.mark.parametrize('view,scheme', VIEWS)
def test_department_person_label_names_people_and_keeps_its_history(browser, live_server, h, view, scheme):
    """D75/D80: with person labels on, the department's own Person label (row-versioned display settings) is the default
    everywhere; Configuration › Names & codes edits it with the shared editor and lists every saved version with Restore."""
    short_names(h)
    department = h.get('admin', '/api/me')['department']
    h.command('admin', 'department.display', {'person_labels_enabled': True, 'reason': 'Department person label',
        # One name format per label (PersonLabel.one_name, the editor's Name choice): the short name without its title,
        # which the contract's short default (SHORT) keeps.
        'person_label': {'parts': ['short'], 'separator': 'dot', 'honorific': 'none'}}, department['id'], department['revision'])
    context, page = sign_in(browser, live_server, 'admin', viewport=view)
    page.emulate_media(color_scheme=scheme)
    try:
        navigate(page, 'people')
        page.get_by_label('Search people', exact=True).fill('Fictional Diego Lewis')
        expect(page.locator('#main .person-cell strong').first).to_contain_text(re.compile(r'^F\. Lewis'))
        navigate(page, 'configuration/names')
        main = page.locator('#main')
        names = main.get_by_role('radiogroup', name='Name', exact=True)
        expect(names.get_by_role('radio', name='F. Lastname', exact=True)).to_have_attribute('aria-checked', 'true')
        expect(main.locator('.names-label .vs-prop[data-part="title"]')).to_have_count(0)
        history = main.locator('section.panel', has=page.get_by_role('heading', name='History', exact=True))
        expect(history).to_contain_text('Current')
        expect(history.get_by_role('button', name='Restore', exact=True).first).to_be_visible()
        shot(page, 'names-person-label', view, scheme)
    finally:
        context.close()


def test_name_pills_are_clean_unless_the_label_adds_the_rank(browser, live_server, h):
    """a pill is the clean name, never a rank before every chip ("CON Dr …");
    a rank shows only where a view's or the department's Person label adds it (its Rank part, here before the name)."""
    from .test_browser_qa_planner import checked_rota, settled
    from .test_browser import pick_view
    checked_rota(h)
    h.command('coordinator', 'table_view.create', {'table_key': 'planner', 'name': 'Fictional rank before the name',
        'definition': {'planner': {'mode': 'week', 'range': 'week', 'label': {'parts': ['level', 'full'], 'separator': 'dot', 'honorific': 'dr'}}}})
    context, page = sign_in(browser, live_server, 'coordinator', viewport={'width': 1440, 'height': 900})
    try:
        navigate(page, 'planner'); settled(page)
        names = page.locator(f'#schedule-view {DRAFT_CHIP} .pchip-name')
        expect(names.first).to_be_visible()
        expect(page.locator('#schedule-view .p-rank')).to_have_count(0)
        pick_view(page, 'Fictional rank before the name'); settled(page)
        expect(names.first).to_have_text(re.compile(r'^.+ · .+'))  # the rank, then the name, as the label says
        expect(page.locator('#schedule-view .p-rank')).to_have_count(0)  # once, in the label, never a second prefix
    finally:
        context.close()
