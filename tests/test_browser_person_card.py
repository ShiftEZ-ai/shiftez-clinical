"""The person card (D67/D68, contact-card v2): the person hub's Overview shows today's duty and team, every visible contact as a
row (tap the number to copy; Call / Message only where the type dials) and More (a text button), exactly as GET /api/people/{id}/card permits each role. Contact types are configured
in Configuration › People › Contact types (who sees each, one-tap dialling) with Undo; a person's contact is edited
through person.contact.set with Undo. Screenshots: CONTACT_FE_SHOTS (a folder) when set. Fictional names only."""
from datetime import timedelta
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from packages.contracts import schemas as s
from packages.domain.time import now as clock_now
from .test_browser import browser, live_server, navigate, sign_in, save_dialog, pick  # noqa: F401
from .test_browser_people_merged import row
from .test_browser_w3_setup_today import no_page_overflow

pytestmark = pytest.mark.browser
PHONE = {'width': 390, 'height': 844}
TRAINEE = 'Fictional Card Trainee'
MOBILE, PAGER, EMAIL = '+966 11 000 0177', '4417', 'fictional.card.trainee@example.test'
VERBS = {'call': ('Call', 'tel:'), 'email': ('Email', 'mailto:')}  # the card's words for each CONTACT_KIND_ACTIONS action


def shoot(page, name):
    if folder := os.getenv('CONTACT_FE_SHOTS'):
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f'{name}.png'))


def contact_type(h, legacy):
    return next(t for t in h.get('admin', '/api/contact-types')['contact_types'] if t['legacy_field'] == legacy)


def retype(h, typ, **changes):
    """The type with `changes`, through the ordinary audited catalogue update."""
    return h.configure('contact_types', {**{k: typ[k] for k in s.ContactTypeInput.model_fields}, **changes}, typ['id'], typ['revision'])['record']


def fixture(h):
    """A trainee on duty now, supervised by the staff account's own person, with a mobile and a pager."""
    zone = h.get('staff', '/api/me')['organisation']['timezone']
    now = clock_now().astimezone(ZoneInfo(zone)).replace(second=0, microsecond=0)  # the app's clock, not the wall's
    start = max(now.replace(hour=0, minute=0), now - timedelta(minutes=30 + now.minute % 15))
    end = start + timedelta(hours=4)
    trainee = h.configure('people', {'display_name': TRAINEE, 'work_phone': MOBILE, 'pager': PAGER, 'work_email': EMAIL})['record']['id']
    supervisor = h.seed['people'][0]
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'starts_on': '2020-01-01', 'category_id': h.seed['trainee_category']})
    h.command('credentialer', 'eligibility.create', {'person_id': trainee, 'service_id': h.seed['services'][0], 'starts_on': '2020-01-01'})
    supervision = {'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 2}
    typ = h.configure('assignment_types', {'name': 'Fictional card clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'supervision': supervision, 'maximum_duration_minutes': 960}})['record']['id']
    w = h.workspace('Fictional card rota', start.date().isoformat(), (start.date() + timedelta(days=7)).isoformat())
    a, b = start.isoformat(), end.isoformat()
    w = h.shift(w, a, b, typ=typ, people=[{'person_id': supervisor, 'start': a, 'end': b, 'participation': 'supervisor'},
        {'person_id': trainee, 'start': a, 'end': b, 'participation': 'trainee', 'supervisor_person_id': supervisor}])['workspace']
    h.publish(w)
    return trainee


def open_card(page, name):
    navigate(page, 'people')
    page.get_by_label('Search people', exact=True).fill(name)
    row(page, name).locator('strong').first.click()
    panel = page.get_by_role('region', name=name, exact=True)
    expect(panel.locator('.pc .loading')).to_have_count(0)
    expect(panel.locator('.pc')).to_be_visible()
    return panel


def more(panel):
    """More is a text button (never a disclosure arrow): More / Show less with aria-expanded."""
    toggle = panel.get_by_role('button', name=re.compile('^(More|Show less)$'))
    if toggle.get_attribute('aria-expanded') != 'true':
        toggle.click()
    expect(toggle).to_have_text('Show less')
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    return panel.locator('.pc-more-body')


def fact(panel, label):
    return panel.locator('.pc-facts dt').filter(has_text=re.compile(f'^{label}$'))


def fits(panel, width):
    """The panel fills exactly the window's width (a phone sheet), never wider, never offset."""
    panel.page.wait_for_function("w => {const r = document.querySelector('.person-panel').getBoundingClientRect(); return Math.abs(r.left) < 1 && Math.abs(r.width - w) < 1;}", arg=width)
    assert panel.page.evaluate('document.documentElement.scrollWidth') <= width


def contacts(panel):
    """The card's contact rows, always shown (never behind More)."""
    return panel.get_by_role('list', name='Contact')


def contact_row(panel, label):
    return contacts(panel).get_by_role('listitem').filter(has=panel.page.get_by_role('button', name=re.compile(f'^Copy {re.escape(label)} ')))


def copies(page, panel, label, value):
    """Both ways copy the value in full (tapping it, and its Copy button) and say so through the announced toast."""
    line = contact_row(panel, label)
    for target in (line.get_by_role('button', name=f'Copy {label} {value}', exact=True), line.get_by_role('button', name=f'Copy {label}', exact=True)):
        page.evaluate("navigator.clipboard.writeText('')")
        page.locator('#toast-tray .toast').evaluate_all('ts => ts.forEach(t => t.remove())')
        target.click()
        expect(page.locator('#toast-tray')).to_have_attribute('aria-live', 'polite')
        expect(page.locator('#toast-tray .toast').filter(has_text='Copied.')).to_have_count(1)
        assert page.evaluate('navigator.clipboard.readText()') == value


def expect_actions(panel, card):
    """Every row: Copy, and the kind's action from the contract's one mapping (CONTACT_KIND_ACTIONS) exactly where the
    server's dial target has that action's scheme (a type's setting can switch it off)."""
    for c in card['contacts']:
        line = contact_row(panel, c['name'])
        expect(line).to_contain_text(c['value'])
        expect(line.get_by_role('button', name='Copy ' + c['name'], exact=True)).to_be_visible()
        action = s.CONTACT_KIND_ACTIONS.get(c['kind'])
        verb, scheme = VERBS[action] if action else ('', '')
        if action and (c.get('dial_target') or '').startswith(scheme):
            expect(line.get_by_role('link', name=f"{verb} {TRAINEE}, {c['name']} {c['value']}", exact=True)).to_have_attribute('href', c['dial_target'])
        else:
            expect(line.get_by_role('link')).to_have_count(0)


# What More holds per role (the server's permitted sections).
SECTIONS = {'staff': [], 'coordinator': ['Hours', 'Can work', 'Rules'], 'admin': ['Hours', 'Can work', 'Rules']}


@pytest.mark.parametrize('role', list(SECTIONS))
def test_card_shows_exactly_what_each_role_may_see(browser, live_server, h, role):
    trainee = fixture(h)
    pager = contact_type(h, 'pager')
    card = h.get(role, f'/api/people/{trainee}/card')
    actions = card['actions']  # what the server permits this role to do
    context, page = sign_in(browser, live_server, role)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            panel = open_card(page, TRAINEE)
            # Today's duty with its team: the supervisor's name opens their card; the trainee is this card.
            expect(panel.locator('.pc-when')).to_contain_text('Fictional card clinic')
            team = panel.locator('.pc .team-box:not(.lg-mark)')
            expect(team).to_have_attribute('aria-label', re.compile(f'supervising {TRAINEE}'))
            # Every visible contact is a row: its type's label, then the number in full; Call only where the type dials.
            expect(contacts(panel).get_by_role('listitem')).to_have_count(len(card['contacts']))
            expect_actions(panel, card)
            expect(contact_row(panel, 'Mobile').get_by_role('link', name=f'Call {TRAINEE}, Mobile {MOBILE}')).to_have_attribute('href', 'tel:+966110000177')
            # The work email is for the roles its type names; where shown, Email opens mail to it.
            if any(c['kind'] == 'email' for c in card['contacts']):
                expect(contacts(panel).get_by_role('link', name=re.compile(f'^Email {TRAINEE}, '))).to_have_attribute('href', 'mailto:' + EMAIL)
            else:
                expect(panel).not_to_contain_text(EMAIL)
            expect(panel.get_by_role('button', name='Call', exact=True)).to_have_count(0)  # no separate Call / Copy buttons
            expect(panel.locator('summary')).to_have_count(0)
            # Editing follows the contract's permission (catalogue.department), on the contact's own row.
            expect(contacts(panel).get_by_role('button', name=re.compile('^Edit '))).to_have_count(len(card['contacts']) if role in s.PERMISSIONS['catalogue.department'] else 0)
            box = more(panel)
            for label in ('Hours', 'Can work', 'Rules'):
                expect(fact(box, label)).to_have_count(1 if label in SECTIONS[role] else 0)
            expect(box.get_by_role('button', name='Add leave', exact=True)).to_have_count(1 if 'availability.record' in actions else 0)
            no_page_overflow(page)
            shoot(page, f'card-{role}-1440-{theme}')
            # The open panel follows the window (no reopening): at phone width a full-width sheet with 44px rows.
            page.set_viewport_size(PHONE)
            fits(panel, PHONE['width'])
            assert all(row.bounding_box()['height'] >= 44 for row in contacts(panel).get_by_role('listitem').all())
            no_page_overflow(page)
            shoot(page, f'card-{role}-390-{theme}')
            # Widened again, it docks beside the page at its own width.
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.wait_for_function("() => {const r = document.querySelector('.person-panel').getBoundingClientRect(); return r.left > 700 && r.right <= innerWidth + 1;}")
            page.keyboard.press('Escape')
        assert not errors, errors
    finally:
        context.close()


def test_who_sees_each_contact_follows_its_configuration(browser, live_server, h):
    fixture(h)
    retype(h, contact_type(h, 'work_phone'), visibility={'mode': 'roles', 'roles': ['coordinator']})
    for role, shown in (('staff', False), ('coordinator', True)):
        context, page = sign_in(browser, live_server, role)
        try:
            panel = open_card(page, TRAINEE)
            expect(contacts(panel)).to_contain_text(PAGER)
            if shown:
                expect(contacts(panel)).to_contain_text(MOBILE)
            else:
                expect(panel).not_to_contain_text(MOBILE)
        finally:
            context.close()


def test_contact_rows_on_a_phone_copy_and_call_where_configured(browser, live_server, h):
    trainee = fixture(h)
    pager = retype(h, contact_type(h, 'pager'), one_tap=True, dial={'line': '+15550100', 'join': 'wait'})
    context, page = sign_in(browser, live_server, 'staff', PHONE)
    context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=live_server)
    try:
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            # Home's duty shows the team; the trainee's name opens their card (a full-width sheet on a phone).
            navigate(page, 'home')
            page.locator('.home-hero .team-box').get_by_role('button', name=TRAINEE, exact=True).click()
            panel = page.get_by_role('region', name=TRAINEE, exact=True)
            expect_actions(panel, h.get('staff', f'/api/people/{trainee}/card'))
            mobile, pagers = contact_row(panel, 'Mobile'), contact_row(panel, pager['name'])
            expect(mobile.get_by_role('link', name=f'Call {TRAINEE}, Mobile {MOBILE}')).to_have_attribute('href', 'tel:+966110000177')
            expect(pagers.get_by_role('link', name=f'Call {TRAINEE}, {pager["name"]} {PAGER}')).to_have_attribute('href', 'tel:+15550100;4417')
            for target in [*contacts(panel).get_by_role('link').all(), *contacts(panel).get_by_role('button').all()]:
                box = target.bounding_box()
                assert box['height'] >= 44 and box['width'] >= 24, box
            # The number sits left of its Call button on the same row.
            assert mobile.get_by_role('button', name=f'Copy Mobile {MOBILE}').bounding_box()['x'] < mobile.get_by_role('link').bounding_box()['x']
            # Safe-area aware: the card pads its end by the device's bottom inset.
            assert 'safe-area-inset-bottom' in page.evaluate("[...document.styleSheets].flatMap(s => {try {return [...s.cssRules]} catch {return []}}).map(r => r.cssText).filter(t => t.startsWith('.pc {')).join(' ')")
            copies(page, panel, 'Mobile', MOBILE)
            no_page_overflow(page)
            shoot(page, f'contacts-390-{theme}')
            page.keyboard.press('Escape')
        # An explicitly disabled type keeps its number (to copy) and has no Call button.
        retype(h, pager, action_off=True)
        page.reload()
        page.locator('.home-hero .team-box').get_by_role('button', name=TRAINEE, exact=True).click()
        panel = page.get_by_role('region', name=TRAINEE, exact=True)
        expect(contact_row(panel, pager['name'])).to_contain_text(PAGER)
        expect(contact_row(panel, pager['name']).get_by_role('link')).to_have_count(0)
        copies(page, panel, pager['name'], PAGER)
    finally:
        context.close()


def test_contact_types_table_edits_with_undo_and_history(browser, live_server, h):
    pager = contact_type(h, 'pager')
    pager = retype(h, pager, visibility={'mode': 'everyone', 'roles': list(s.ROLE_NAMES)})
    before = len(h.get('admin', f"/api/catalogues/contact_types/{pager['id']}/history")['versions'])
    context, page = sign_in(browser, live_server, 'admin')
    try:
        for theme in ('light', 'dark'):
            page.emulate_media(color_scheme=theme)
            navigate(page, 'configuration/contact_types')
            table = page.locator('#main .dt')
            expect(table.locator('thead th[scope=col]')).to_contain_text(['Contact type', 'Kind', 'Who can see it', 'One-tap call', 'Status'])
            line = page.locator('#main .dt-body tbody tr').filter(has_text=pager['name'])
            expect(line).to_contain_text('Pager')
            expect(line).to_contain_text('Everyone')
            no_page_overflow(page)
            shoot(page, f'contact-types-1440-{theme}')
        line.get_by_role('button', name='Edit', exact=True).click()
        form = page.get_by_role('dialog', name='Edit contact type')
        form.get_by_label('Who can see it', exact=True).select_option('roles')
        pick(form, 'Roles that see it', ['Coordinator'])
        save_dialog(form, 'Save version')
        expect(line).to_contain_text('Coordinator')
        assert contact_type(h, 'pager')['visibility'] == {'mode': 'roles', 'roles': ['coordinator']}
        page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        expect(line).to_contain_text('Everyone')
        assert contact_type(h, 'pager')['visibility']['mode'] == 'everyone'
        # The edit and its Undo are two more versions in its history (who, why, when).
        versions = h.get('admin', f"/api/catalogues/contact_types/{pager['id']}/history")['versions']
        assert len(versions) == before + 2
    finally:
        context.close()


def test_admin_edits_a_contact_from_the_card_and_undoes_it(browser, live_server, h):
    fixture(h)
    context, page = sign_in(browser, live_server, 'admin')
    try:
        panel = open_card(page, TRAINEE)
        pager = contact_type(h, 'pager')
        contacts(panel).get_by_role('button', name='Edit ' + pager['name'], exact=True).click()
        form = page.get_by_role('dialog', name='Edit ' + pager['name'])
        form.get_by_label(pager['name'], exact=True).fill('4418')
        form.get_by_role('button', name='Save', exact=True).click()
        expect(form).to_have_count(0)
        expect(contact_row(panel, pager['name'])).to_contain_text('4418')
        page.locator('.toast').get_by_role('button', name='Undo', exact=True).click()
        expect(contact_row(panel, pager['name'])).to_contain_text(PAGER)
        expect(contacts(panel)).not_to_contain_text('4418')
    finally:
        context.close()
